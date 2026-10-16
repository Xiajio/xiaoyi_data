"""Regressions for scientific provenance, recovery, constraints and ranking."""
from datetime import datetime, timezone
from pathlib import Path
import pytest
from sqlalchemy import select, text
from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import create_engine_for, init_db, session_scope
from xiaoyi_catalog.enrich import enrich_all, enrich_version
from xiaoyi_catalog.embeddings import embed_all
from xiaoyi_catalog.match import match_datasets
from xiaoyi_catalog.models import Dataset, DatasetVersion, Evidence, Enrichment, Run, Pairing, Idea
from xiaoyi_catalog.pairing import propose_pairing, review_pairing, freeze_snapshot, approve_snapshot, PairingError
from xiaoyi_catalog.search import SearchFilter, version_to_dict
from conftest import FakeAdapter, make_raw, day

IDEA = {"external_id": "regression", "title": "text study", "requirements": {
    "modalities": ["text"], "license_class": ["permissive"], "languages": ["en"],
    "task_categories": ["text-classification"], "min_rows": 10}}


def good(revision="one", **kwargs):
    return make_raw("org/test", revision=revision, license="mit", tasks=("text-classification",),
                    languages=("en",), size_rows=20, card="evidence " + revision, **kwargs)


def test_old_revision_uses_captured_metadata_and_evidence(engine):
    collect(engine, FakeAdapter([good()]), CollectScope())
    collect(engine, FakeAdapter([make_raw("org/test", revision="two", license="gpl-3.0", tasks=("image-classification",), card="new card")]), CollectScope())
    with session_scope(engine) as session:
        versions = session.scalars(select(DatasetVersion).order_by(DatasetVersion.id)).all()
        old, new = versions
        assert version_to_dict(session, old)["license"] == "mit"
        pid = propose_pairing(session, IDEA, [old.id], "fixed evidence")["created"][0]["id"]
        review_pairing(session, pid, "confirmed")
        snap = freeze_snapshot(session, IDEA["external_id"])
        entry = snap.content["datasets"][0]
        assert entry["dataset"]["license"] == "mit"
        assert entry["evidence"][0]["excerpt"] == "evidence one"
        assert entry["dataset_version_id"] == old.id
        assert all(e["dataset_version_id"] == old.id for e in entry["evidence"])
        enrich_version(session, old)
        assert session.scalar(select(Enrichment).where(Enrichment.dataset_version_id == old.id)) is not None
        with pytest.raises(PairingError, match="硬约束"):
            badpid = propose_pairing(session, IDEA, [new.id], "bad")["created"][0]["id"]
            review_pairing(session, badpid, "confirmed")
        approve_snapshot(session, snap.id)


def test_failed_old_item_is_retried_after_newer_item_succeeds(engine):
    raws = [make_raw("org/fail", last_modified=day(1, 1)), make_raw("org/pass", last_modified=day(2, 1))]
    first = collect(engine, FakeAdapter(raws, failures={"org/fail": TimeoutError()}), CollectScope())
    assert not first.stats["watermark_advanced"]
    second = collect(engine, FakeAdapter(raws), CollectScope())
    assert second.stats["new"] == 1
    with session_scope(engine) as session:
        assert len(session.scalars(select(Dataset)).all()) == 2


def test_interrupt_commits_run_and_completed_items(engine):
    raws = [good(), make_raw("org/interrupt")]
    with pytest.raises(KeyboardInterrupt):
        collect(engine, FakeAdapter(raws, failures={"org/interrupt": KeyboardInterrupt()}), CollectScope())
    with session_scope(engine) as session:
        run = session.scalar(select(Run))
        assert run.status == "interrupted" and run.finished_at is not None
        assert run.stats["new"] == 1
        assert session.scalar(select(Dataset).where(Dataset.source_key == "org/test")) is not None


def test_hard_constraints_checked_again_at_freeze_and_approve(engine):
    collect(engine, FakeAdapter([good()]), CollectScope())
    with session_scope(engine) as session:
        version = session.scalar(select(DatasetVersion))
        pid = propose_pairing(session, IDEA, [version.id], "study")["created"][0]["id"]
        review_pairing(session, pid, "confirmed")
        idea = session.scalar(select(Idea))
        idea.requirements = {**IDEA["requirements"], "languages": ["zh"]}
        with pytest.raises(PairingError, match="硬约束"):
            freeze_snapshot(session, IDEA["external_id"])
        idea.requirements = IDEA["requirements"]
        snap = freeze_snapshot(session, IDEA["external_id"])
        idea.requirements = {**IDEA["requirements"], "min_rows": 100}
        with pytest.raises(PairingError, match="变更"):
            approve_snapshot(session, snap.id)


def test_needs_info_invalidates_pending_snapshot(engine):
    collect(engine, FakeAdapter([good()]), CollectScope())
    with session_scope(engine) as session:
        version = session.scalar(select(DatasetVersion))
        pid = propose_pairing(session, IDEA, [version.id], "study")["created"][0]["id"]
        review_pairing(session, pid, "confirmed")
        snap = freeze_snapshot(session, IDEA["external_id"])
        review_pairing(session, pid, "needs-info", "missing evidence")
        assert snap.status == "invalidated"


def test_sqlite_naive_datetime_can_be_enriched(engine):
    collect(engine, FakeAdapter([good(last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc))]), CollectScope())
    assert enrich_all(engine)["processed"] == 1


def test_vectors_are_filtered_before_top_k(engine):
    raws = [make_raw("org/outside", tasks=("image-classification",), card="perfect"),
            make_raw("org/inside-bad", tasks=("text-classification",), card="bad", downloads=1000),
            make_raw("org/inside-good", tasks=("text-classification",), card="good", downloads=1)]
    collect(engine, FakeAdapter(raws), CollectScope())
    class Embedder:
        model_name, dim = "fixed-vectors", 2
        def embed(self, texts):
            return [[1.0, 0.0] if "perfect" in text or text == "query" else
                    [0.8, 0.6] if "good" in text else [0.0, 1.0] for text in texts]
    embed_all(engine, Embedder())
    with session_scope(engine) as session:
        result = match_datasets(session, SearchFilter(modalities=("text",)), query_text="query", embedder=Embedder(), vector_k=1)
        assert result["results"][0]["source_key"] == "org/inside-good"


def test_migration_does_not_invent_historical_metadata(tmp_path):
    engine = create_engine_for(tmp_path / "legacy.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE dataset_version (id INTEGER PRIMARY KEY, dataset_id INTEGER, revision VARCHAR(128), changelog TEXT, created_at DATETIME, updated_at DATETIME)"))
        conn.execute(text("INSERT INTO dataset_version (id, dataset_id, revision) VALUES (1, 42, 'old')"))
    init_db(engine)
    init_db(engine)  # idempotent upgrade
    with session_scope(engine) as session:
        version = session.get(DatasetVersion, 1)
        assert version.metadata_json == {} and version.provenance_status == "legacy_unverified"
        assert version_to_dict(session, version)["license_class"] == "unknown"
    engine.dispose()


def test_version_metadata_cannot_be_rewritten(engine):
    collect(engine, FakeAdapter([good()]), CollectScope())
    with pytest.raises(ValueError, match="immutable"):
        with session_scope(engine) as session:
            version = session.scalar(select(DatasetVersion))
            version.metadata_json = {**version.metadata_json, "license": "gpl-3.0"}


def test_manual_registration_keeps_access_conditions(engine):
    from xiaoyi_catalog.adapters.manual import register_manual
    run = register_manual(engine, {"source_key":"official/mesh", "revision":"2026-01", "url":"https://example.org/data",
        "access_conditions":"public research archive", "modalities":["3d"], "license":"cc-by-4.0"})
    assert run.stats["new"] == 1
    with session_scope(engine) as session:
        version = session.scalar(select(DatasetVersion))
        assert "public research archive" in version.metadata_json["card_text"]
        assert version.metadata_json["modalities"] == ["3d"]
        assert session.scalar(select(Evidence).where(Evidence.kind == "metadata")) is not None


def test_scale_upper_bounds_filter_and_block_unknown(engine):
    from xiaoyi_catalog.match import requirement_to_filter, missing_for_requirement
    from xiaoyi_catalog.search import search_datasets
    collect(engine, FakeAdapter([good(), make_raw("org/unknown")]), CollectScope())
    requirements = {"max_rows": 50, "max_bytes": 100}
    assert missing_for_requirement({"size_rows":20,"size_bytes":101}, requirements)
    assert missing_for_requirement({"size_rows":20,"size_bytes":None}, requirements)
    with session_scope(engine) as session:
        assert search_datasets(session, requirement_to_filter(requirements)) == []
        results = search_datasets(session, requirement_to_filter({"max_rows":50}))
        assert [r["source_key"] for r in results] == ["org/test"]


def test_hf_permission_errors_are_not_silently_missing_cards(tmp_path):
    from test_huggingface_adapter import FakeInfo, make_adapter
    adapter, _ = make_adapter([FakeInfo("org/private")],
        card_errors={"org/private":PermissionError("restricted")}, tmp=tmp_path)
    with pytest.raises(PermissionError):
        adapter.fetch_detail(next(iter(adapter.list(CollectScope()))))
