"""PostgreSQL/pgvector integration tests; CI supplies a disposable test database.

Each test uses its own schema so a repeated CI invocation cannot inherit state from
an earlier run. Extension installation remains in public; only the generated schema
is removed after the test. No application database is used implicitly.
"""
import os
import uuid
import pytest
from sqlalchemy import text, select, event
from xiaoyi_catalog.db import create_engine_for, init_db, session_scope
from xiaoyi_catalog.models import Dataset, DatasetVersion, VectorEmbedding
from xiaoyi_catalog.models_runtime import AgentRun
from xiaoyi_catalog.operations import expected_schema_revision
from xiaoyi_catalog.agent_runtime import create_run
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.enrich import enrich_all
from xiaoyi_catalog.embeddings import HashEmbedder, embed_all
from xiaoyi_catalog.match import match_datasets
from xiaoyi_catalog.search import SearchFilter
from conftest import FakeAdapter, make_raw


@pytest.fixture
def postgres_engine():
    url = os.environ.get("XIAOYI_TEST_POSTGRES")
    if not url:
        pytest.skip("No isolated PostgreSQL test database configured")
    admin = create_engine_for(url)
    schema = "xiaoyi_test_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine_for(url)
    @event.listens_for(engine, "connect")
    def set_path(connection, _record):
        # Committed session-level setting survives migration transactions.
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}", public')
        connection.commit()
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def test_pg_migrations_versions_vector_and_task(postgres_engine):
    engine = postgres_engine
    init_db(engine)
    init_db(engine)
    collect(engine, FakeAdapter([make_raw("postgres/text", revision="a"*40, license="mit",
        tasks=("text-classification",), card="Test text data", size_rows=10)]), CollectScope())
    enrich_all(engine)
    embedder = HashEmbedder()
    embed_all(engine, embedder)
    with session_scope(engine) as session:
        assert session.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == expected_schema_revision()
        assert session.scalar(text("SELECT pg_typeof(vector)::text FROM vector_embedding LIMIT 1")) == "vector"
        result = match_datasets(session, SearchFilter(modalities=("text",)), query_text="text", embedder=embedder)
        assert result["ranking"] == "hybrid_rrf"
        assert {"keyword", "vector"} <= set(result["branches"])
        assert result["retrieval_version"] == "catalog-retrieval/1"
        assert result["results"][0]["source_key"] == "postgres/text"
        assert result["results"][0]["similarity"] > 0
        run = create_run(session, {"goal":"PostgreSQL persistent task", "sources":["manual"]}, "ci")
        ident = run.id
    with session_scope(engine) as session:
        assert session.get(AgentRun, ident).status == "queued"


@pytest.mark.parametrize("engine_fixture", ["engine", "postgres_engine"], ids=["sqlite", "postgres"])
def test_vectors_filter_before_ranking_and_keep_versions(request, engine_fixture):
    engine = request.getfixturevalue(engine_fixture)
    init_db(engine)
    collect(engine, FakeAdapter([
        make_raw("p/outside", card="perfect", tasks=("image-classification",)),
        make_raw("p/bad", card="bad", tasks=("text-classification",), downloads=1000),
        make_raw("p/good", card="good", tasks=("text-classification",), downloads=1),
    ]), CollectScope())
    class Embedder:
        model_name, dim = "fixed-version-vectors", 2
        def embed(self, texts):
            # Source keys are part of embedding text: p/good remains the same
            # after its card changes. Match this fixture's card line exactly.
            vectors = {"query": [1.0, 0.0], "perfect": [1.0, 0.0],
                       "good": [0.8, 0.6], "bad": [0.0, 1.0]}
            return [vectors[value.rsplit("\n", 1)[-1]] for value in texts]
    embedder = Embedder()
    embed_all(engine, embedder)
    with session_scope(engine) as session:
        result = match_datasets(session, SearchFilter(modalities=("text",)), query_text="query", embedder=embedder, vector_k=1)
        assert result["results"][0]["source_key"] == "p/good"
        assert result["results"][0]["similarity"] == pytest.approx(0.8)
    collect(engine, FakeAdapter([make_raw("p/good", revision="new", card="bad", tasks=("text-classification",))]), CollectScope())
    embed_all(engine, embedder)
    with session_scope(engine) as session:
        dataset_id = session.scalar(select(Dataset.id).where(Dataset.source_key == "p/good"))
        assert len(session.scalars(select(VectorEmbedding).where(VectorEmbedding.dataset_id == dataset_id)).all()) == 2
        result = match_datasets(session, SearchFilter(modalities=("text",)), query_text="query", embedder=embedder)
        assert {item["source_key"] for item in result["results"]} == {"p/bad", "p/good"}
        assert all(item["similarity"] == pytest.approx(0) for item in result["results"])


def test_pg_legacy_adoption_does_not_fabricate_provenance(postgres_engine):
    engine = postgres_engine
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE dataset_version (id INTEGER PRIMARY KEY, dataset_id INTEGER, revision VARCHAR(128), changelog TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)")
        connection.exec_driver_sql("INSERT INTO dataset_version (id, dataset_id, revision) VALUES (1, 42, 'legacy')")
    init_db(engine)
    with session_scope(engine) as session:
        version = session.get(DatasetVersion, 1)
        assert version.metadata_json == {} and version.provenance_status == "legacy_unverified"
        assert version.kind == "source" and version.parent_version_id is None

def test_pg_collection_schedule_and_active_scope_are_idempotent(postgres_engine):
    from datetime import timedelta
    from xiaoyi_catalog.auth import Principal
    from xiaoyi_catalog.collection_runtime import (
        create_collection_schedule, create_collection_job, tick_collection_schedules,
        process_next_collection)
    from xiaoyi_catalog.models_collection import CollectionJob, CollectionSchedule
    from xiaoyi_catalog.models import utcnow
    engine = postgres_engine
    init_db(engine)
    with session_scope(engine) as session:
        schedule = create_collection_schedule(session, {
            "name": "PG daily", "scope": {"search": "text", "limit": 5},
            "interval_minutes": 1440, "enabled": True},
            Principal(1, "reviewer", "reviewer", "human"))
        schedule_id = schedule.id
    due = utcnow() + timedelta(days=2)
    tick_collection_schedules(engine, now=due)
    tick_collection_schedules(engine, now=due)
    with session_scope(engine) as session:
        jobs = session.scalars(select(CollectionJob)).all()
        assert len(jobs) == 1
        assert create_collection_job(session, {"search": "text", "limit": 5}, "reviewer").id == jobs[0].id
        assert session.get(CollectionSchedule, schedule_id).last_job_id == jobs[0].id
    adapter = FakeAdapter([make_raw("pg/text", revision="a"*40, card="Text data", tasks=("text-classification",))])
    adapter.kind = "huggingface"
    assert process_next_collection(engine, adapter_factory=lambda: adapter)
    with session_scope(engine) as session:
        job = session.scalar(select(CollectionJob))
        assert job.status == "completed"
        assert job.dataset_version_ids and job.analysis_status == "rule_based"


def test_pg_summary_execution_is_visible_only_during_a_leased_attempt(postgres_engine):
    from xiaoyi_catalog.llm import ModelResponse
    from xiaoyi_catalog.models import Enrichment
    from xiaoyi_catalog.summary_runtime import active_summary_executions, submit_summary
    from xiaoyi_catalog.worker import run_once

    engine = postgres_engine
    init_db(engine)
    collect(engine, FakeAdapter([make_raw("pg/summary", revision="a" * 40,
        license="mit", card="Dataset card", tasks=("text-classification",))]), CollectScope())
    with session_scope(engine) as session:
        dataset = session.scalar(select(Dataset))
        version = session.scalar(select(DatasetVersion))
        ident = submit_summary(session, dataset.id, version.id, "ci")["id"]
        assert active_summary_executions(session) == []

    class ObservingModel:
        def complete(self, schema, instruction, data):
            with session_scope(engine) as session:
                active = active_summary_executions(session)
                assert [row.id for row in active] == [ident]
                assert active[0].value["attempts"] == 1
            return ModelResponse(schema.model_validate({
                "rationale": "PostgreSQL summary integration check",
                "claims": [], "uncertainty": ["Only source metadata is available"],
            }), "test-summary", {"total_tokens": 1})

    assert run_once(engine, llm=ObservingModel())
    with session_scope(engine) as session:
        assert active_summary_executions(session) == []
        summary = session.get(Enrichment, ident)
        assert summary.status == "candidate" and "execution" not in summary.value


def test_pg_queue_skips_locked_jobs_and_fences_expired_attempts(postgres_engine):
    """Two real PG connections exercise row locks, then reject stale publication."""
    from datetime import timedelta
    from sqlalchemy.orm import Session
    from xiaoyi_catalog.jobs import enqueue,claim,Guard,finish,digest
    from xiaoyi_catalog.models_jobs import Job,Artifact
    from xiaoyi_catalog.models import utcnow
    from xiaoyi_catalog.agent_runtime import LeaseLost
    engine=postgres_engine
    init_db(engine)
    with session_scope(engine) as session:
        first=enqueue(session,"profile.run",{"asset_id":101},"pg-fixture", "pg-lock-first").id
        second=enqueue(session,"profile.run",{"asset_id":102},"pg-fixture", "pg-lock-second").id
    with Session(engine) as blocking, blocking.begin():
        assert blocking.scalar(select(Job).where(Job.id==first).with_for_update()).id==first
        # Configure the only free pooled connection to fail quickly if SKIP LOCKED regresses.
        with engine.connect() as probe:
            probe.exec_driver_sql("SET statement_timeout = '5s'")
            probe.commit()
        claimed=claim(engine,"cpu","worker-independent")
        assert claimed.id==second
    original=claim(engine,"cpu","worker-old")
    assert original.id==first
    old_guard=Guard(engine,original)
    with session_scope(engine) as session:
        session.get(Job,first).lease_until=utcnow()-timedelta(seconds=1)
    replacement=claim(engine,"cpu","worker-new")
    assert replacement.id==first and replacement.fence!=original.fence
    with pytest.raises(LeaseLost):
        finish(engine,old_guard,"succeeded",{"stale":True})
    # Even a pending INSERT must roll back when the result transaction loses its fence.
    with pytest.raises(LeaseLost):
        with session_scope(engine) as session:
            value={"stale":True}
            session.add(Artifact(job_id=first,content_hash=digest(value),manifest=value))
            old_guard(session)
    with session_scope(engine) as session:
        assert session.scalar(select(Artifact).where(Artifact.job_id==first)) is None
        assert session.get(Job,first).owner=="worker-new"
    accepted={"accepted":True}
    finish(engine,Guard(engine,replacement),"succeeded",accepted)
    with session_scope(engine) as session:
        assert session.get(Job,first).status=="succeeded"
        assert session.scalar(select(Artifact).where(Artifact.job_id==first)).manifest==accepted


def test_pg_sample_worker_publishes_rows_json_receipt_and_portable_export(postgres_engine,tmp_path,monkeypatch):
    """Run the actual CPU and I/O workers against PostgreSQL, including Parquet output."""
    import json
    import runpy
    import zipfile
    from sqlalchemy import func
    from xiaoyi_catalog.auth import Principal
    from xiaoyi_catalog.jobs import enqueue,run_queue_once
    from xiaoyi_catalog.models import Source
    from xiaoyi_catalog.models_jobs import Job,Artifact
    from xiaoyi_catalog.models_samples import Sample,BuildSample,DatasetBuild
    from xiaoyi_catalog.sample_demo import register_fixture
    from xiaoyi_catalog.sample_engine import create_build,publish_build,asset_path
    engine=postgres_engine
    monkeypatch.setenv("XIAOYI_STORAGE_ROOT",str(tmp_path/"pg-assets"))
    init_db(engine)
    with session_scope(engine) as session:
        source=Source(kind="manual",endpoint="pg-synthetic-sample-fixture")
        session.add(source);session.flush()
        dataset=Dataset(source_id=source.id,source_key="pg/sample-build",fingerprint="pg-sample-fixture",license="CC0",license_class="permissive")
        session.add(dataset);session.flush()
        version=DatasetVersion(dataset_id=dataset.id,revision="fixture/1",provenance_status="verified",metadata_json={"synthetic":True,"license":"CC0"})
        session.add(version);session.flush()
        version_id=version.id
    file=tmp_path/"pg-rows.csv"
    file.write_text("text,label,patient\nalpha,a,p1\nalpha,a,p1\nbeta,b,p1\ngamma,a,p2\n",encoding="utf-8")
    aid=register_fixture(engine,version_id,file)
    with session_scope(engine) as session:
        build=create_build(session,{"asset_ids":[aid],"modality":"text","label_column":"label","group_column":"patient","shard_rows":2},"pg-fixture")
        build_id=build.id
        job_id=enqueue(session,"build.run",{"build_id":build_id},"pg-fixture").id
    assert run_queue_once(engine,"cpu",owner="pg-build-worker")
    with session_scope(engine) as session:
        job=session.get(Job,job_id)
        assert job.status=="succeeded",job.error
        build=session.get(DatasetBuild,build_id)
        assert build.status=="ready" and build.sample_count==3
        assert build.manifest["counts"]["input_rows"]==4
        assert build.manifest["counts"]["exact_duplicates"]==1
        assert session.scalar(select(func.count()).select_from(Sample))==3
        assert session.scalar(select(func.count()).select_from(BuildSample))==3
        groups=session.execute(select(BuildSample.group_key,func.count(func.distinct(BuildSample.split))).where(BuildSample.build_id==build_id).group_by(BuildSample.group_key)).all()
        assert groups and all(count==1 for _,count in groups)
        receipt=session.scalar(select(Artifact).where(Artifact.job_id==job_id))
        assert receipt.manifest["manifest_hash"]==build.manifest_hash
        publish_build(session,build_id,Principal(0,"pg-human-reviewer","reviewer"),build.manifest_hash)
        export_id=enqueue(session,"build.export",{"build_id":build_id},"pg-fixture").id
    assert run_queue_once(engine,"io",owner="pg-export-worker")
    with session_scope(engine) as session:
        export=session.get(Job,export_id)
        assert export.status=="succeeded",export.error
        assert session.scalar(select(Artifact).where(Artifact.job_id==export_id)).manifest==export.result
        path=asset_path(export.result["storage_key"])
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read("manifest.json"))["counts"]["output_rows"]==3
        archive.extractall(tmp_path/"pg-export")
    loader=runpy.run_path(str(tmp_path/"pg-export"/"load.py"))
    assert len(list(loader["load"]()))==3
