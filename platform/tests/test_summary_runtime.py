from __future__ import annotations

import pytest
from sqlalchemy import func, select

from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.llm import ModelResponse
from xiaoyi_catalog.models import Audit, Dataset, DatasetVersion, Enrichment, Evidence, Source
from xiaoyi_catalog.summary_runtime import (DatasetSummary, execute_summary, process_next_summary,
                                            submit_summary)
from xiaoyi_catalog.worker import run_once


class FakeModel:
    def __init__(self, *, forged=False, fail=False, mixed=False):
        self.calls = 0
        self.forged, self.fail, self.mixed = forged, fail, mixed
        self.inputs = []

    def complete(self, schema, instruction, data):
        self.calls += 1
        self.inputs.append(data)
        if self.fail:
            self.fail = False
            raise TimeoutError("provider timeout with secret key")
        excerpt = data["evidence"][0]["excerpt"] if data["evidence"] else ""
        claims = []
        if data["evidence"]:
            if not self.forged:
                claims.append({"text": "卡片说明数据集包含 100 行样本。",
                               "evidence_ids": [data["evidence"][0]["id"]], "quotes": [excerpt[:40]]})
            if self.forged or self.mixed:
                claims.append({"text": "编造一条无法核验的陈述。",
                               "evidence_ids": [999 if self.forged else data["evidence"][0]["id"]],
                               "quotes": ["原文中不存在的引文"]})
        body = {"rationale": "该数据集为新闻文本分类语料，许可宽松，证据来自留存卡片。",
                "claims": claims,
                "uncertainty": ["元数据与卡片摘录不代表已下载完整数据文件。"]}
        return ModelResponse(schema.model_validate(body), "fake-summary",
                             {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def seed(engine, *, provenance="verified", card="中文新闻语料，包含 100 行样本，含训练与测试切分。"):
    with session_scope(engine) as session:
        source = Source(kind="manual")
        session.add(source)
        session.flush()
        dataset = Dataset(source_id=source.id, source_key="lab/news", name="新闻语料", fingerprint="fp-1",
                          modalities=["text"], license="mit", license_class="permissive", size_rows=100,
                          downloads=1200, card_text=card)
        session.add(dataset)
        session.flush()
        version = DatasetVersion(dataset_id=dataset.id, revision="rev-1", provenance_status=provenance,
                                 metadata_json={"source_key": "lab/news", "name": "新闻语料", "license": "mit",
                                                "license_class": "permissive", "size_rows": 100})
        session.add(version)
        session.flush()
        evidence = Evidence(dataset_id=dataset.id, dataset_version_id=version.id, kind="card",
                            excerpt=card, location="rev-1/README.md")
        session.add(evidence)
        session.flush()
        return dataset.id, version.id, evidence.id


def read(engine, ident):
    with session_scope(engine) as session:
        row = session.get(Enrichment, ident)
        session.expunge(row)
        return row


def submit(engine, dataset, version):
    with session_scope(engine) as session:
        return submit_summary(session, dataset, version, "reviewer")


def test_submit_is_idempotent_and_audited(engine):
    dataset, version, _ = seed(engine)
    first = submit(engine, dataset, version)
    second = submit(engine, dataset, version)
    assert first["id"] == second["id"] and first["status"] == "generating"
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Enrichment)) == 1
        assert session.scalar(select(func.count()).select_from(Audit)
                              .where(Audit.action == "agent_summary")) == 1


def test_submit_rejects_unverified_version(engine):
    dataset, version, _ = seed(engine, provenance="historical_unverified")
    with pytest.raises(ValueError):
        submit(engine, dataset, version)


def test_process_publishes_grounded_candidate(engine):
    dataset, version, evidence = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    assert process_next_summary(engine, llm=FakeModel())
    row = read(engine, ident)
    assert row.status == "candidate" and row.kind == "llm_summary"
    assert row.evidence_ids == [evidence]
    assert row.value["summary_kind"] == "dataset_review" and row.value["rationale"]
    assert row.value["claims"][0]["evidence_ids"] == [evidence]
    assert row.value["model"] == "fake-summary" and row.value["usage"]["total_tokens"] == 15
    # 已确认/候选状态再次提交保持幂等，不重复生成
    assert submit(engine, dataset, version)["id"] == ident


def test_forged_citation_fails_after_bounded_attempts(engine):
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    model = FakeModel(forged=True)
    assert process_next_summary(engine, llm=model)
    assert read(engine, ident).status == "generating"  # 首次失败保留重试
    assert process_next_summary(engine, llm=model)
    assert read(engine, ident).status == "generating"
    assert process_next_summary(engine, llm=model)
    row = read(engine, ident)
    assert row.status == "failed" and "未通过原文核验" in row.value["error"]
    assert row.value["attempts"] == 3
    # 手动重试创建新修订；失败证明保持不变。
    retry = submit(engine, dataset, version)
    assert retry["status"] == "generating" and retry["id"] != ident
    assert read(engine, ident).value["attempts"] == 3
    assert read(engine, retry["id"]).parent_enrichment_id == ident


def test_unverifiable_claims_are_demoted_but_grounded_ones_publish(engine):
    dataset, version, evidence = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    assert process_next_summary(engine, llm=FakeModel(mixed=True))
    row = read(engine, ident)
    assert row.status == "candidate"
    assert row.evidence_ids == [evidence]
    assert len(row.value["claims"]) == 1 and row.value["demoted_claims"] == 1
    assert any("引用未通过原文核验" in item and "编造一条无法核验的陈述" in item for item in row.value["uncertainty"])


def test_worker_run_once_processes_summary(engine):
    dataset, version, evidence = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    assert run_once(engine, llm=FakeModel())
    assert read(engine, ident).status == "candidate"


def test_provider_error_keeps_queue_actionable(engine):
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    execute_summary(engine, ident, llm=FakeModel(fail=True))
    row = read(engine, ident)
    assert row.status == "generating" and "超时" in row.value["error"]
    assert "secret key" not in row.value["error"]


def test_dataset_summary_contract_bounds():
    with pytest.raises(Exception):
        DatasetSummary.model_validate({"rationale": "", "claims": [], "uncertainty": []})
    summary = DatasetSummary.model_validate({"rationale": "总结", "claims": [],
                                             "uncertainty": ["证据不足"]})
    assert summary.uncertainty == ["证据不足"]


def _collect_one(engine, monkeypatch, *, auto="1"):
    from conftest import FakeAdapter, make_raw
    from xiaoyi_catalog.auth import Principal
    from xiaoyi_catalog.collection_runtime import create_collection_job, process_next_collection
    monkeypatch.setenv("XIAOYI_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("XIAOYI_LLM_MODEL", "fake")
    monkeypatch.setenv("XIAOYI_LLM_API_KEY", "fake")
    monkeypatch.setenv("XIAOYI_CATALOG_AUTO_SUMMARY", auto)
    adapter = FakeAdapter([make_raw("a/one", license="mit", card="中文新闻分类语料，包含训练与测试切分。")])
    with session_scope(engine) as session:
        create_collection_job(session, {}, Principal(1, "alice", "researcher"))
    process_next_collection(engine, adapter_factory=lambda: adapter)


def test_collection_queues_summary_when_llm_configured(engine, monkeypatch):
    _collect_one(engine, monkeypatch)
    with session_scope(engine) as session:
        row = session.scalar(select(Enrichment).where(Enrichment.kind == "llm_summary",
                                                      Enrichment.producer.like("agent-summary/%")))
        assert row is not None and row.status == "generating"
        ident = row.id
    assert process_next_summary(engine, llm=FakeModel())
    assert read(engine, ident).status == "candidate"


def test_collection_skips_summary_when_disabled(engine, monkeypatch):
    _collect_one(engine, monkeypatch, auto="0")
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(Enrichment.id)).where(
            Enrichment.kind == "llm_summary")) == 0


def test_backfill_submits_only_versions_without_summary(engine, monkeypatch):
    monkeypatch.setenv("XIAOYI_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("XIAOYI_LLM_MODEL", "fake")
    monkeypatch.setenv("XIAOYI_LLM_API_KEY", "fake")
    seed(engine)
    from xiaoyi_catalog.summary_runtime import backfill_summaries
    assert backfill_summaries(engine) == {"submitted": 1, "scanned": 1}
    assert backfill_summaries(engine)["submitted"] == 0


@pytest.fixture()
def board_client(engine):
    from fastapi.testclient import TestClient
    from xiaoyi_catalog.service import create_app
    with TestClient(create_app(str(engine.url.database), dev_auth=True)) as client:
        yield client


def board(client):
    response = client.get("/api/agent-summaries")
    assert response.status_code == 200
    return response.json()


def test_board_only_reports_a_started_leased_attempt_as_running(engine, board_client):
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    initial = board(board_client)
    assert initial["running"] == [] and initial["counts"]["queued"] == 1
    assert initial["queue"][0]["id"] == ident and initial["queue"][0]["position"] == 1

    class ObservingModel(FakeModel):
        def complete(self, *args):
            active = board(board_client)
            assert active["counts"]["running"] == 1 and active["counts"]["queued"] == 0
            assert active["queue"] == []
            assert active["running"][0]["id"] == ident
            assert active["running"][0]["attempts"] == 1
            assert active["running"][0]["started_at"]
            assert "owner" not in active["running"][0]
            return super().complete(*args)

    assert run_once(engine, llm=ObservingModel())
    completed = board(board_client)
    assert completed["running"] == [] and completed["counts"]["generating"] == 0
    assert completed["counts"]["candidate"] == 1
    assert "execution" not in read(engine, ident).value


def test_board_rejects_expired_or_replaced_crash_marker_and_worker_recovers(engine, board_client):
    from datetime import timedelta
    from xiaoyi_catalog.models import utcnow
    from xiaoyi_catalog.models_runtime import WorkerLease
    from xiaoyi_catalog.worker import acquire_lease, release_lease
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    assert acquire_lease(engine, "crashed")
    with session_scope(engine) as session:
        session.get(Enrichment, ident).value = {"attempts": 1, "execution": {
            "owner": "crashed", "id": "abandoned-attempt", "started_at": utcnow().isoformat()}}
        session.get(WorkerLease, "research_worker").expires_at = utcnow() - timedelta(seconds=1)
    expired = board(board_client)
    assert expired["running"] == [] and expired["counts"]["queued"] == 1
    assert expired["queue"][0]["id"] == ident
    # A new worker can spend time on other work without reviving an old marker.
    assert acquire_lease(engine, "replacement")
    assert board(board_client)["running"] == []
    release_lease(engine, "replacement")
    assert run_once(engine, llm=FakeModel())
    assert read(engine, ident).status == "candidate"
    assert read(engine, ident).value["attempts"] == 2


def test_expired_summary_cannot_publish_and_recovery_keeps_one_candidate(engine, board_client):
    from datetime import timedelta
    from xiaoyi_catalog.agent_runtime import LeaseLost
    from xiaoyi_catalog.models import utcnow
    from xiaoyi_catalog.models_runtime import WorkerLease
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]

    class ExpiringModel(FakeModel):
        def complete(self, *args):
            with session_scope(engine) as session:
                session.get(WorkerLease, "research_worker").expires_at = utcnow() - timedelta(seconds=1)
            assert board(board_client)["running"] == []
            return super().complete(*args)

    with pytest.raises(LeaseLost):
        run_once(engine, llm=ExpiringModel())
    interrupted = read(engine, ident)
    assert interrupted.status == "generating" and "execution" not in interrupted.value
    assert "claims" not in interrupted.value
    assert run_once(engine, llm=FakeModel())
    assert read(engine, ident).status == "candidate"
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(Audit.id)).where(Audit.action == "agent_summary_completed")) == 1


def test_replaced_worker_result_cannot_overwrite_new_summary(engine):
    from datetime import timedelta
    from xiaoyi_catalog.agent_runtime import LeaseLost
    from xiaoyi_catalog.models import utcnow
    from xiaoyi_catalog.models_runtime import WorkerLease
    from xiaoyi_catalog.worker import acquire_lease, release_lease
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]

    class ReplacedModel(FakeModel):
        def complete(self, *args):
            with session_scope(engine) as session:
                session.get(WorkerLease, "research_worker").expires_at = utcnow() - timedelta(seconds=1)
            assert acquire_lease(engine, "new-worker")
            execute_summary(engine, ident, llm=FakeModel(), owner="new-worker")
            release_lease(engine, "new-worker")
            return super().complete(*args)

    with pytest.raises(LeaseLost):
        run_once(engine, llm=ReplacedModel())
    result = read(engine, ident)
    assert result.status == "candidate" and result.value["attempts"] == 2
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(Audit.id)).where(Audit.action == "agent_summary_completed")) == 1


def test_failed_attempt_returns_to_queue_without_running_marker(engine, board_client):
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    assert run_once(engine, llm=FakeModel(fail=True))
    pending = board(board_client)
    assert pending["running"] == [] and pending["queue"][0]["attempts"] == 1
    assert "超时" in pending["queue"][0]["error"]
    assert "execution" not in read(engine, ident).value


def test_lease_without_execution_marker_does_not_start_legacy_queue(engine, board_client):
    from xiaoyi_catalog.worker import acquire_lease, release_lease
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    assert acquire_lease(engine, "busy-on-qa")
    pending = board(board_client)
    assert pending["running"] == [] and pending["queue"][0]["id"] == ident
    blocked_model = FakeModel()
    execute_summary(engine, ident, llm=blocked_model)
    assert blocked_model.calls == 0
    release_lease(engine, "busy-on-qa")


def test_repeated_interruptions_reach_retry_limit_without_another_model_call(engine):
    from xiaoyi_catalog.agent_runtime import LeaseLost
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]

    class InterruptedModel(FakeModel):
        def complete(self, *args):
            self.calls += 1
            raise LeaseLost("worker stopped")

    model = InterruptedModel()
    for _ in range(3):
        with pytest.raises(LeaseLost):
            run_once(engine, llm=model)
    assert run_once(engine, llm=model)
    row = read(engine, ident)
    assert model.calls == 3 and row.status == "failed" and row.value["attempts"] == 3
    assert "重试上限" in row.value["error"]
    retry = submit(engine, dataset, version)
    assert retry["status"] == "generating" and retry["id"] != ident
    assert read(engine, retry["id"]).value == {"attempts": 0}
    assert read(engine, ident).value["attempts"] == 3


@pytest.mark.parametrize("cleanup", [False, True], ids=["publish", "cleanup"])
def test_sqlite_summary_write_serializes_attempt_check_and_replacement(engine, cleanup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import update
    from xiaoyi_catalog.models import utcnow
    from xiaoyi_catalog.models_runtime import WorkerLease
    from xiaoyi_catalog.summary_runtime import _leased_summary, _serialize_summary_write
    from xiaoyi_catalog.worker import acquire_lease
    dataset, version, _ = seed(engine)
    ident = submit(engine, dataset, version)["id"]
    assert acquire_lease(engine, "old-worker")
    with session_scope(engine) as session:
        session.get(Enrichment, ident).value = {"attempts": 1, "execution": {
            "id": "old-attempt", "owner": "old-worker", "started_at": utcnow().isoformat()}}
    checked, finish_write, replacement_started, replacement_done = Event(), Event(), Event(), Event()

    def old_write():
        with session_scope(engine) as session:
            if cleanup:
                _serialize_summary_write(session)
                row = session.get(Enrichment, ident, with_for_update=True)
            else:
                row = _leased_summary(session, ident, "old-worker", "old-attempt")
            assert row.value["execution"]["id"] == "old-attempt"
            checked.set()
            assert finish_write.wait(5)
            row.value = {"attempts": 1} if cleanup else {"attempts": 1, "rationale": "old result"}

    def replacement():
        assert checked.wait(5)
        replacement_started.set()
        with session_scope(engine) as session:
            session.execute(update(WorkerLease).where(WorkerLease.name == "research_worker")
                            .values(owner="new-worker"))
            row = session.get(Enrichment, ident)
            row.value = {"attempts": 2, "execution": {"id": "new-attempt", "owner": "new-worker"}}
        replacement_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        old_future = pool.submit(old_write)
        replacement_future = pool.submit(replacement)
        try:
            assert replacement_started.wait(5)
            assert not replacement_done.wait(0.2), "takeover must wait for checked write/cleanup to commit"
        finally:
            finish_write.set()
        old_future.result(timeout=5)
        replacement_future.result(timeout=5)
    assert read(engine, ident).value["execution"]["id"] == "new-attempt"
