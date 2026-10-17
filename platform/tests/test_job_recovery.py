"""Fault boundaries between legacy state, fenced jobs and durable output receipts."""
from datetime import timedelta
import json
import sqlite3
import pytest
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from xiaoyi_catalog.auth import Principal
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import Enrichment, utcnow
from xiaoyi_catalog.models_assets import DownloadRequest, Asset
from xiaoyi_catalog.models_jobs import Job, Attempt, Artifact
from xiaoyi_catalog.models_samples import DatasetBuild, Experiment, ProfileRun, SampleRelation
from xiaoyi_catalog.jobs import enqueue, claim, cancel, finish, Guard, bridge_legacy, run_queue_once, digest
from xiaoyi_catalog.mining import execute_mining, EXACT_CANDIDATES_SQL, BAND_CANDIDATES_SQL
from test_summary_runtime import seed, submit, FakeModel
from test_sample_engine import source, build


def summary_job(engine):
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    bridge_legacy(engine, "interactive")
    with session_scope(engine) as session:
        return ident, session.scalar(select(Job.id).where(Job.kind == "summary.run"))


def test_cancelled_summary_is_not_resurrected_by_legacy_bridge(engine):
    ident, job_id = summary_job(engine)
    with session_scope(engine) as session:
        cancel(session, job_id, Principal(1, "admin", "admin"))
    for _ in range(3):
        bridge_legacy(engine, "interactive")
    with session_scope(engine) as session:
        assert session.get(Job, job_id).status == "cancelled"
        old = session.get(Enrichment, ident)
        assert old.status == "cancelled" and len(old.content_hash) == 64
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_summary_retry_has_no_artifact_until_completed(engine):
    ident, job_id = summary_job(engine)
    model = FakeModel(fail=True)
    assert run_queue_once(engine, "interactive", llm=model)
    with session_scope(engine) as session:
        assert session.get(Job, job_id).status == "queued"
        assert session.get(Enrichment, ident).status == "generating"
        assert session.scalar(select(Attempt.status)) == "retryable"
        assert session.scalar(select(func.count()).select_from(Artifact)) == 0
    assert run_queue_once(engine, "interactive", llm=model)
    with session_scope(engine) as session:
        assert session.get(Job, job_id).status == "succeeded"
        assert session.get(Job, job_id).attempts == 2
        assert session.scalar(select(Artifact)).manifest == {"id": ident, "status": "candidate"}


def test_summary_attempt_budget_survives_bridge_polls(engine):
    ident, job_id = summary_job(engine)
    with session_scope(engine) as session:
        session.get(Job, job_id).max_attempts = 1
    assert run_queue_once(engine, "interactive", llm=FakeModel(fail=True))
    for _ in range(3):
        assert not run_queue_once(engine, "interactive", llm=FakeModel())
    with session_scope(engine) as session:
        job = session.get(Job, job_id)
        assert job.status == "failed" and job.attempts == job.max_attempts == 1
        assert session.get(Enrichment, ident).status == "failed"
        assert session.scalar(select(func.count()).select_from(Artifact)) == 0


@pytest.mark.parametrize("kind", ["download.run", "asset.import", "build.run", "experiment.import"])
def test_queue_cancel_updates_incomplete_domain_state(engine, source, kind):
    asset_id = source("rows.txt", "a\nb\n")
    with session_scope(engine) as session:
        if kind in {"download.run", "asset.import"}:
            asset = session.get(Asset, asset_id)
            item = session.get(DownloadRequest, asset.download_request_id)
            item.status = "approved"
            payload = {"id" if kind == "download.run" else "request_id": item.id}
        else:
            build_row = DatasetBuild(requested_by="fixture", recipe={}, recipe_hash="r" * 64, input_manifest=[])
            session.add(build_row)
            session.flush()
            item, payload = build_row, {"build_id": build_row.id}
            if kind == "experiment.import":
                item = Experiment(build_id=build_row.id, requested_by="fixture", model={}, protocol={},
                                  prediction_asset_id=asset_id, prediction_sha256="a" * 64)
                session.add(item)
                session.flush()
                payload = {"experiment_id": item.id}
        job = enqueue(session, kind, payload, "fixture")
        cancel(session, job.id, Principal(1, "fixture", "researcher"))
        assert item.status == "cancelled"


@pytest.mark.parametrize("expire", [False, True])
def test_committed_receipt_wins_over_late_cancel_and_exhausted_budget(engine, expire):
    with session_scope(engine) as session:
        ident = enqueue(session, "build.run", {"build_id": 1}, "fixture").id
        session.get(Job, ident).max_attempts = 1
    job = claim(engine, "cpu", "first")
    guard = Guard(engine, job)
    result = {"build_id": 1, "manifest_hash": "verified"}
    with session_scope(engine) as session:
        guard(session)
        session.add(Artifact(job_id=ident, manifest=result, content_hash=digest(result)))
    with session_scope(engine) as session:
        cancel(session, ident, Principal(1, "fixture", "researcher"))
        if expire:
            session.get(Job, ident).lease_until = utcnow() - timedelta(seconds=1)
    if expire:
        assert claim(engine, "cpu", "replacement") is None
    else:
        finish(engine, guard, "cancelled")
    with session_scope(engine) as session:
        saved = session.get(Job, ident)
        assert saved.status == "succeeded" and saved.result == result
        assert saved.attempts == 1
        assert session.scalar(select(func.count()).select_from(Artifact)) == 1


def test_mining_commit_receipt_prevents_duplicate_profile_on_replay(engine, source):
    asset_id = source("rows.jsonl", "\n".join(json.dumps(row) for row in [
        {"text": "alpha beta", "label": "x"}, {"text": "alpha beta", "label": "y"}]))
    build_id, _, _ = build(engine, {"asset_ids": [asset_id], "modality": "text", "text_column": "text", "label_column": "label"})
    payload = {"build_id": build_id}
    with session_scope(engine) as session:
        ident = enqueue(session, "mining.run", payload, "fixture").id
    guard = Guard(engine, claim(engine, "cpu", "worker"))
    with guard:
        first = execute_mining(engine, payload, guard=guard)
        again = execute_mining(engine, payload, guard=guard)
    assert first == again and first["findings"] > 0
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(ProfileRun).where(ProfileRun.producer == "candidate-mining/1")) == 1
        assert session.scalar(select(func.count()).select_from(SampleRelation)) == first["findings"]
        assert session.scalar(select(func.count()).select_from(Artifact).where(Artifact.job_id == ident)) == 1


def test_duplicate_bucket_lookup_does_not_sort_or_join_entire_bucket():
    database = sqlite3.connect(":memory:")
    try:
        database.executescript("""CREATE TABLE features (id TEXT, benchmark INTEGER, content TEXT, label TEXT, modality TEXT, split TEXT, signature TEXT, PRIMARY KEY(id,benchmark));
        CREATE INDEX content_idx ON features(content,id,benchmark);
        CREATE TABLE bands (band INTEGER, value TEXT, id TEXT, benchmark INTEGER);
        CREATE INDEX bands_idx ON bands(band,value,id,benchmark);""")
        count = 10000
        database.executemany("INSERT INTO features VALUES(?,0,'duplicate','x','text','train','0000')", ((str(i),) for i in range(count)))
        database.executemany("INSERT INTO bands VALUES(0,'same',?,0)", ((str(i),) for i in range(count)))
        for query, args in [(EXACT_CANDIDATES_SQL, ("duplicate", 8)), (BAND_CANDIDATES_SQL, (0, "same", 8))]:
            plan = " ".join(row[3] for row in database.execute("EXPLAIN QUERY PLAN " + query, args))
            assert "TEMP B-TREE" not in plan
            steps = []
            database.set_progress_handler(lambda: steps.append(1) or 0, 100)
            assert len(database.execute(query, args).fetchall()) == 8
            database.set_progress_handler(None, 0)
            assert len(steps) < 20  # bounded work even though the duplicate bucket contains 10k entries
    finally:
        database.close()


def test_collection_queue_cancel_releases_dedup_key_and_closes_underlying_run(engine):
    from xiaoyi_catalog.models import Run
    from xiaoyi_catalog.models_collection import CollectionJob, CollectionEvent
    with session_scope(engine) as session:
        original = Run(kind="collect", adapter="fixture", status="running", input_scope={})
        session.add(original)
        session.flush()
        item = CollectionJob(actor="fixture", scope={}, active_key="same-scope", run_id=original.id)
        session.add(item)
        session.flush()
        job = enqueue(session, "collection.run", {"id": item.id}, "fixture")
        cancel(session, job.id, Principal(1, "fixture", "researcher"))
        assert item.status == "cancelled" and item.stage == "finished" and item.active_key is None
        assert original.status == "cancelled"
        session.flush()
        assert session.scalar(select(CollectionEvent.kind).where(CollectionEvent.job_id == item.id)) == "cancelled"
        session.add(CollectionJob(actor="fixture", scope={}, active_key="same-scope"))
        session.flush()  # A fresh collection of the same scope must not hit the unique active key.
