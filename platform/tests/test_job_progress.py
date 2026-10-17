from datetime import timedelta
import pytest
from sqlalchemy import event
from xiaoyi_catalog.auth import Principal
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import utcnow
from xiaoyi_catalog.models_jobs import Job
from xiaoyi_catalog.jobs import enqueue, claim, Guard, finish, cancel, JobError, JobCancelled
from xiaoyi_catalog.agent_runtime import LeaseLost


def running(engine):
    with session_scope(engine) as session:
        enqueue(session, "build.run", {"build_id": 1}, "alice")
    job = claim(engine, "cpu", "first")
    return job, Guard(engine, job)


def test_reports_are_memory_only_and_heartbeat_persists_one_latest_snapshot(engine):
    job, guard = running(engine)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for count in range(1000):
            guard.report_progress("reading_rows", count, invalid_rows=0)
        assert statements == []
        assert guard._renew_lease()
        assert len(statements) == 1 and statements[0].lstrip().upper().startswith("UPDATE")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    with session_scope(engine) as session:
        assert session.get(Job, job.id).progress == {
            "stage": "reading_rows", "processed": 999, "total": None, "counts": {"invalid_rows": 0}}
    copy = guard.progress_snapshot()
    copy["counts"]["invalid_rows"] = 88
    assert guard.progress_snapshot()["counts"]["invalid_rows"] == 0


def test_finish_persists_last_buffered_count_without_waiting_for_heartbeat(engine):
    job, guard = running(engine)
    guard.report_progress("completed", 17, 17, shards=2)
    finish(engine, guard, "succeeded", {"samples": 17})
    with session_scope(engine) as session:
        saved = session.get(Job, job.id)
        assert saved.progress == {"stage": "completed", "processed": 17, "total": 17, "counts": {"shards": 2}}
        assert saved.status == "succeeded"


def test_cancel_prevents_pending_progress_at_heartbeat_and_finish(engine):
    job, guard = running(engine)
    guard.report_progress("reading_rows", 500)
    with session_scope(engine) as session:
        before = dict(session.get(Job, job.id).progress)
        cancel(session, job.id, Principal(1, "alice", "researcher"))
    assert not guard._renew_lease()
    with pytest.raises(JobCancelled):
        guard.report_progress("reading_rows", 1000)
    finish(engine, guard, "cancelled")
    with session_scope(engine) as session:
        saved = session.get(Job, job.id)
        assert saved.status == "cancelled" and saved.progress == before


def test_expired_owner_cannot_overwrite_new_attempt_progress(engine):
    old, guard = running(engine)
    guard.report_progress("reading_rows", 900)
    with session_scope(engine) as session:
        session.get(Job, old.id).lease_until = utcnow() - timedelta(seconds=1)
    new = claim(engine, "cpu", "replacement")
    replacement = Guard(engine, new)
    replacement.report_progress("reading_rows", 200)
    assert replacement._renew_lease()
    assert not guard._renew_lease()
    with pytest.raises(LeaseLost):
        guard.report_progress("completed", 1000)
    with pytest.raises(LeaseLost):
        finish(engine, guard, "succeeded")
    with session_scope(engine) as session:
        assert session.get(Job, old.id).progress["processed"] == 200


@pytest.mark.parametrize("stage,processed,total,counts", [
    ("x" * 65, 1, None, {}), ("bad\nstage", 1, None, {}), ("reading", -1, None, {}),
    ("reading", True, None, {}), ("reading", 2, 1, {}), ("reading", 2**63, None, {}),
    ("reading", 1, None, {"bad": "sample content"}), ("reading", 1, None, {"bad": {"nested": 1}}),
    ("reading", 1, None, {"flag": True}), ("reading", 1, None, {"x" * 49: 1}),
    ("reading", 1, None, {f"n{i}": i for i in range(17)}),
])
def test_progress_rejects_unbounded_or_nonnumeric_payloads(engine, stage, processed, total, counts):
    _, guard = running(engine)
    with pytest.raises(JobError):
        guard.report_progress(stage, processed, total, **counts)
