from __future__ import annotations

import json
from datetime import timedelta
import pytest
from sqlalchemy import func, select

from conftest import FakeAdapter, day, make_raw
from xiaoyi_catalog.auth import Principal
from xiaoyi_catalog.collection_runtime import (
    MAX_ATTEMPTS, cancel_collection_job, create_collection_job, create_collection_schedule,
    job_to_dict, list_job_events, process_next_collection, retry_collection_job,
    schedule_to_dict, tick_collection_schedules, update_collection_schedule,
)
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import DatasetVersion, Enrichment, Run, Source, utcnow
from xiaoyi_catalog.models_collection import CollectionJob, CollectionSchedule
from xiaoyi_catalog.worker import run_once

ALICE = Principal(1, "alice", "researcher")
REVIEWER = Principal(2, "reviewer", "reviewer")


def queue(engine, scope=None):
    with session_scope(engine) as session:
        return create_collection_job(session, scope or {}, ALICE).id


def get_job(engine, job_id):
    with session_scope(engine) as session:
        return job_to_dict(session.get(CollectionJob, job_id))


def adapter_for(*keys, **kwargs):
    return FakeAdapter([make_raw(key, last_modified=day(3, 1), license="mit", card="A source card") for key in keys], **kwargs)


def test_normalized_scope_dedupes_but_does_not_grant_other_requester_ownership(engine):
    with session_scope(engine) as session:
        first = create_collection_job(session, {"languages": ["ZH", "en", "en"], "search": " text "}, ALICE)
        second = create_collection_job(session, {"languages": ["en", "zh"], "search": "text"}, "bob")
        assert first.id == second.id and second.actor == "alice"
        with pytest.raises(PermissionError):
            cancel_collection_job(session, second.id, Principal(3, "bob", "researcher"))
        with pytest.raises(PermissionError):
            cancel_collection_job(session, second.id, Principal(4, "bot", "admin", "agent"))
        assert job_to_dict(first)["requested_by"] == "alice"


def test_worker_collects_without_llm_and_candidates_are_never_confirmed(engine, monkeypatch):
    for key in ("XIAOYI_LLM_BASE_URL", "XIAOYI_LLM_API_KEY", "XIAOYI_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("xiaoyi_catalog.worker.execute_run", lambda *a, **k: pytest.fail("No agent requested"))
    job_id = queue(engine)
    assert run_once(engine, collection_adapter_factory=lambda: adapter_for("a/one"))
    result = get_job(engine, job_id)
    assert result["status"] == "completed" and result["analysis_status"] == "rule_based"
    assert result["stats"]["processed"] == 1 and result["stats"]["new"] == 1
    with session_scope(engine) as session:
        assert {entry.status for entry in session.scalars(select(Enrichment))} == {"candidate"}
        assert {entry.dataset_version_id for entry in session.scalars(select(Enrichment))} == set(result["dataset_version_ids"])
        assert list_job_events(session, job_id)[-1]["kind"] == "completed"


def test_partial_failure_retry_retains_old_audit_and_does_not_leak_credentials(engine):
    first_id = queue(engine)
    process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one", "a/two", failures={"a/two": TimeoutError("hf_secret_123") }))
    first = get_job(engine, first_id)
    assert first["status"] == "partial" and first["stats"]["failed"] == 1
    assert first["stats"]["failures"][0]["source_key"] == "a/two"
    with session_scope(engine) as session:
        assert not session.scalar(select(Source)).cursor
        assert "hf_secret_123" not in json.dumps(list_job_events(session, first_id))
        retry = retry_collection_job(session, first_id, ALICE)
        retry_id = retry.id
        assert retry.retry_of == first_id and retry.trigger == "retry"
    process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one", "a/two"))
    retried = get_job(engine, retry_id)
    assert retried["status"] == "completed" and retried["stats"]["new"] == 1 and retried["stats"]["duplicates"] == 1
    assert get_job(engine, first_id)["status"] == "partial"
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(DatasetVersion.id))) == 2


def test_interrupt_resumes_same_run_and_counts_without_duplicate_side_effects(engine):
    job_id = queue(engine)
    failed_once = adapter_for("a/one", "a/two", failures={"a/two": KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        process_next_collection(engine, adapter_factory=lambda: failed_once)
    before = get_job(engine, job_id)
    assert before["status"] == "running" and before["run_id"]
    with session_scope(engine) as session:
        assert session.get(Run, before["run_id"]).status == "interrupted"
    process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one", "a/two"))
    after = get_job(engine, job_id)
    assert after["status"] == "completed" and after["run_id"] == before["run_id"]
    assert after["stats"]["new"] == 2 and after["stats"]["processed"] == 2 and after["attempts"] == 2
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(Run.id))) == 1
        assert session.scalar(select(func.count(DatasetVersion.id))) == 2
        assert session.scalar(select(func.count(Enrichment.id))) == 4


def test_hard_kill_after_commit_before_job_progress_recovers_run_stats(engine, monkeypatch):
    import xiaoyi_catalog.collection_runtime as runtime
    job_id = queue(engine)
    real_collect = runtime.collect
    def kill_after_collect(*args, **kwargs):
        kwargs.pop("progress", None)
        real_collect(*args, **kwargs)
        raise KeyboardInterrupt()
    monkeypatch.setattr(runtime, "collect", kill_after_collect)
    with pytest.raises(KeyboardInterrupt):
        process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one"))
    monkeypatch.setattr(runtime, "collect", real_collect)
    assert get_job(engine, job_id)["stats"]["new"] == 0
    process_next_collection(engine, adapter_factory=lambda: pytest.fail("Completed collect must not repeat network"))
    assert get_job(engine, job_id)["stats"]["new"] == 1
    assert get_job(engine, job_id)["status"] == "completed"


def test_cancel_running_does_not_become_item_failure_or_advance_watermark(engine):
    job_id = queue(engine)
    adapter = adapter_for("a/one", "a/two")
    fetch = adapter.fetch_detail
    def cancel_on_second(ref):
        if ref.source_key == "a/two":
            with session_scope(engine) as session:
                state = cancel_collection_job(session, job_id, ALICE)
                assert state.status == "running" and state.active_key
        return fetch(ref)
    adapter.fetch_detail = cancel_on_second
    process_next_collection(engine, adapter_factory=lambda: adapter)
    result = get_job(engine, job_id)
    assert result["status"] == "cancelled" and result["stats"]["failed"] == 0
    assert result["stats"]["new"] == 1
    with session_scope(engine) as session:
        assert session.get(Run, result["run_id"]).status == "cancelled"
        assert not session.scalar(select(Source)).cursor
        assert session.get(CollectionJob, job_id).active_key is None


def test_lease_loss_propagates_and_keeps_job_resumable(engine):
    from xiaoyi_catalog.agent_runtime import LeaseLost
    job_id = queue(engine)
    adapter = adapter_for("a/one", failures={"a/one": LeaseLost("lost")})
    with pytest.raises(LeaseLost):
        process_next_collection(engine, adapter_factory=lambda: adapter)
    result = get_job(engine, job_id)
    assert result["status"] == "running" and result["stats"]["failed"] == 0
    process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one"))
    assert get_job(engine, job_id)["status"] == "completed"


def test_repeated_interruptions_fail_clearly_at_attempt_limit(engine):
    job_id = queue(engine)
    adapter = adapter_for("a/one", failures={"a/one": KeyboardInterrupt()})
    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(KeyboardInterrupt):
            process_next_collection(engine, adapter_factory=lambda: adapter)
    process_next_collection(engine, adapter_factory=lambda: pytest.fail("Over budget"))
    result = get_job(engine, job_id)
    assert result["status"] == "failed" and result["error_kind"] == "retry_limit"
    assert result["attempts"] == MAX_ATTEMPTS
    with session_scope(engine) as session:
        assert session.get(Run, result["run_id"]).status == "failed"


def test_schedules_coalesce_missed_intervals_and_same_scope_without_overlap(engine):
    now = utcnow()
    with session_scope(engine) as session:
        first = create_collection_schedule(session, {"name": "daily", "interval_minutes": 1440}, REVIEWER)
        second = create_collection_schedule(session, {"name": "same scope", "interval_minutes": 60}, REVIEWER)
        first.next_run_at = second.next_run_at = now - timedelta(days=8)
        ids = first.id, second.id
    queued = tick_collection_schedules(engine, now=now)
    assert len(queued) == 1
    assert tick_collection_schedules(engine, now=now) == []
    assert tick_collection_schedules(engine, now=now + timedelta(hours=2)) == []
    with session_scope(engine) as session:
        first, second = (session.get(CollectionSchedule, ident) for ident in ids)
        assert first.last_job_id == second.last_job_id == queued[0]
        assert schedule_to_dict(first)["next_run_at"].endswith("+00:00")
        assert session.scalar(select(func.count(CollectionJob.id))) == 1
        assert first.next_run_at.replace(tzinfo=now.tzinfo) == now + timedelta(days=1)


def test_pause_stops_future_dispatch_and_keeps_existing_job_explicitly_cancellable(engine):
    now = utcnow()
    with session_scope(engine) as session:
        schedule = create_collection_schedule(session, {"interval_minutes": 15}, REVIEWER)
        schedule.next_run_at = now - timedelta(minutes=15)
        schedule_id = schedule.id
    assert len(tick_collection_schedules(engine, now=now)) == 1
    with session_scope(engine) as session:
        schedule = update_collection_schedule(session, schedule_id, {"enabled": False}, REVIEWER)
        assert schedule_to_dict(schedule)["next_run_at"] is None
        assert session.get(CollectionJob, schedule.last_job_id).status == "queued"
    assert tick_collection_schedules(engine, now=now + timedelta(days=10)) == []
    with session_scope(engine) as session:
        with pytest.raises(PermissionError):
            create_collection_schedule(session, {}, Principal(5, "agent", "admin", "agent"))


def test_stale_versions_remain_visible_to_later_analysis_and_review_is_preserved(engine):
    first_id = queue(engine)
    process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one"))
    first = get_job(engine, first_id)
    with session_scope(engine) as session:
        for entry in session.scalars(select(Enrichment)):
            entry.status = "confirmed"
        original_ids = set(session.scalars(select(Enrichment.id)))
    second_id = queue(engine)
    process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one"))
    second = get_job(engine, second_id)
    assert second["stats"]["skipped_stale"] == 1
    assert second["dataset_version_ids"] == first["dataset_version_ids"]
    assert second["analysis_status"] == "rule_based"
    with session_scope(engine) as session:
        assert set(session.scalars(select(Enrichment.id))) == original_ids
        assert set(session.scalars(select(Enrichment.status))) == {"confirmed"}


def test_list_failure_is_tracked_and_safe_to_retry(engine):
    job_id = queue(engine)
    process_next_collection(engine, adapter_factory=lambda: adapter_for(list_error=PermissionError("token=hf_secret")))
    result = get_job(engine, job_id)
    assert result["status"] == "failed" and result["error_kind"] == "permission"
    assert result["analysis_status"] == "pending"
    assert result["error_type"] == "PermissionError" and result["http_status"] is None
    assert result["error"] == result["stats"]["failures"][0]["message"]
    assert "hf_secret" not in json.dumps(result)
    with session_scope(engine) as session:
        run = session.get(Run, result["run_id"])
        assert run.status == "failed" and run.stats["failures"]
        assert "hf_secret" not in json.dumps(run.stats)


def test_scope_bound_stays_bounded_when_source_changes_during_resume(engine):
    job_id = queue(engine, {"limit": 2})
    adapter = adapter_for("a/one", "a/two", failures={"a/two": KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        process_next_collection(engine, adapter_factory=lambda: adapter)
    resumed = adapter_for("a/two", "a/new", "a/one")
    process_next_collection(engine, adapter_factory=lambda: resumed)
    result = get_job(engine, job_id)
    assert result["status"] == "completed" and result["stats"]["new"] == 2
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(DatasetVersion.id))) == 2


def test_expired_worker_cannot_overwrite_successor_run_progress(engine):
    from xiaoyi_catalog.agent_runtime import LeaseLost
    job_id = queue(engine)
    old = adapter_for("a/one", "a/two")
    old_fetch = old.fetch_detail
    lost = False
    def guard():
        if lost:
            raise LeaseLost("expired worker")
    def takeover(ref):
        nonlocal lost
        lost = True
        process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one", "a/two"))
        return old_fetch(ref)
    old.fetch_detail = takeover
    with pytest.raises(LeaseLost):
        process_next_collection(engine, adapter_factory=lambda: old, guard=guard)
    result = get_job(engine, job_id)
    assert result["status"] == "completed" and result["stats"]["new"] == 2
    with session_scope(engine) as session:
        run = session.get(Run, result["run_id"])
        assert run.status == "completed" and run.stats["new"] == 2
        assert run.stats["dataset_version_ids"] == result["dataset_version_ids"]


def test_cancel_after_crash_closes_running_record(engine):
    job_id = queue(engine)
    with pytest.raises(KeyboardInterrupt):
        process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one", failures={"a/one": KeyboardInterrupt()}))
    with session_scope(engine) as session:
        cancel_collection_job(session, job_id, ALICE)
    process_next_collection(engine, adapter_factory=lambda: pytest.fail("Cancelled"))
    result = get_job(engine, job_id)
    with session_scope(engine) as session:
        assert result["status"] == "cancelled"
        assert session.get(Run, result["run_id"]).status == "cancelled"


def test_job_insert_and_schedule_dispatch_rollback_atomically(engine, monkeypatch):
    with pytest.raises(RuntimeError):
        with session_scope(engine) as session:
            create_collection_job(session, {}, ALICE)
            raise RuntimeError("caller rolled back")
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(CollectionJob.id))) == 0
    now = utcnow()
    with session_scope(engine) as session:
        schedule = create_collection_schedule(session, {"interval_minutes": 15}, REVIEWER)
        schedule.next_run_at = now - timedelta(minutes=30)
        schedule_id = schedule.id
    import xiaoyi_catalog.collection_runtime as runtime
    real_create = runtime.create_collection_job
    def fail_after_insert(*args, **kwargs):
        real_create(*args, **kwargs)
        raise RuntimeError("dispatch interrupted before due date advance")
    monkeypatch.setattr(runtime, "create_collection_job", fail_after_insert)
    with pytest.raises(RuntimeError):
        tick_collection_schedules(engine, now=now)
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(CollectionJob.id))) == 0
        assert session.get(CollectionSchedule, schedule_id).last_job_id is None
    monkeypatch.setattr(runtime, "create_collection_job", real_create)
    assert len(tick_collection_schedules(engine, now=now)) == 1


def test_retry_pins_failed_ref_even_when_it_leaves_latest_listing(engine):
    first_id = queue(engine, {"limit": 2})
    process_next_collection(engine, adapter_factory=lambda: adapter_for("a/failure", "a/success", failures={"a/failure": TimeoutError()}))
    with session_scope(engine) as session:
        retry_id = retry_collection_job(session, first_id, ALICE).id
    adapter = adapter_for("a/failure", "a/new", "a/success")
    adapter.list = lambda scope: (_ for _ in ()).throw(AssertionError("Original scope fills the budget"))
    requested = []
    fetch = adapter.fetch_detail
    def capture(ref):
        requested.append((ref.source_key, ref.revision))
        return fetch(ref)
    adapter.fetch_detail = capture
    process_next_collection(engine, adapter_factory=lambda: adapter)
    result = get_job(engine, retry_id)
    assert result["status"] == "completed" and result["stats"]["new"] == 1
    assert requested == [("a/failure", "rev-1")]
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(DatasetVersion.id))) == 2
        assert session.get(Run, result["run_id"]).retry_of == get_job(engine, first_id)["run_id"]


def test_enrichment_only_retry_keeps_original_versions_and_skips_network(engine, monkeypatch):
    import xiaoyi_catalog.collection_runtime as runtime
    original_enrich = runtime.enrich_version
    def fail_enrichment(*args):
        raise ValueError("transient schema conversion failure")
    monkeypatch.setattr(runtime, "enrich_version", fail_enrichment)
    first_id = queue(engine)
    process_next_collection(engine, adapter_factory=lambda: adapter_for("a/one"))
    first = get_job(engine, first_id)
    assert first["status"] == "partial" and first["analysis_status"] == "pending"
    with session_scope(engine) as session:
        retry_id = retry_collection_job(session, first_id, ALICE).id
    monkeypatch.setattr(runtime, "enrich_version", original_enrich)
    process_next_collection(engine, adapter_factory=lambda: pytest.fail("No source request for enrichment-only retry"))
    result = get_job(engine, retry_id)
    assert result["status"] == "completed" and result["analysis_status"] == "rule_based"
    assert result["dataset_version_ids"] == first["dataset_version_ids"]
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(Enrichment.id))) == 2


@pytest.mark.parametrize("failure_phase", ["list", "item", "factory"])
def test_safe_error_type_and_http_status_cover_source_failures(engine, failure_phase):
    class RateLimited(Exception):
        response = type("Response", (), {"status_code": 429})()
    error = RateLimited("GET https://secret.local/?token=hf_secret authorization=secret")
    job_id = queue(engine)
    if failure_phase == "list":
        factory = lambda: adapter_for(list_error=error)
    elif failure_phase == "item":
        factory = lambda: adapter_for("a/one", failures={"a/one": error})
    else:
        def factory():
            raise error
    process_next_collection(engine, adapter_factory=factory)
    result = get_job(engine, job_id)
    assert result["status"] == "failed" and result["analysis_status"] == "pending"
    assert result["error_type"] == "RateLimited" and result["http_status"] == 429
    assert result["stats"]["failures"][0]["error_type"] == "RateLimited"
    assert result["stats"]["failures"][0]["http_status"] == 429
    assert "secret" not in json.dumps(result)
    with session_scope(engine) as session:
        assert "secret" not in json.dumps(list_job_events(session, job_id))
