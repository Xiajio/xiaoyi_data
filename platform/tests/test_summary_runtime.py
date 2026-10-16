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
    # 失败后再次提交重置为生成中（重试语义）
    assert submit(engine, dataset, version)["status"] == "generating"
    assert read(engine, ident).value["attempts"] == 0


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
