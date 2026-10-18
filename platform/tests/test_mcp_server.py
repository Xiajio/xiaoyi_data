"""MCP 工具层测试：工具注册契约 + 直接调用（stdio 传输属集成范围）。

直接调用依赖 FastMCP 装饰器返回原函数；若 SDK 行为变化，这里的失败会立刻暴露。
"""

from __future__ import annotations

import asyncio

import pytest

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import create_engine_for, init_db, session_scope
from xiaoyi_catalog.models import DatasetVersion
from xiaoyi_catalog.pairing import PairingError
from xiaoyi_catalog import mcp_server

from sqlalchemy import select

from conftest import FakeAdapter, make_raw

EXPECTED_TOOLS = {"dataset_search", "dataset_evidence", "pairing_propose", "snapshot_status", "snapshot_export",
                  "agent_run_create", "agent_run_status", "agent_run_cancel", "agent_run_retry", "dataset_files",
                  "download_request", "snapshot_bundle", "job_submit", "job_status", "dataset_build_create", "dataset_build_status", "experiment_import", "experiments_compare", "job_cancel", "job_retry"}


def test_registers_exactly_the_contract_tools():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    engine = create_engine_for(db)
    init_db(engine)
    collect(engine, FakeAdapter([make_raw("a/zh-sentiment", tasks=("text-classification",), languages=("zh",),
                                          license="mit", card="中文情感分类", size_rows=100)]), CollectScope())
    engine.dispose()
    monkeypatch.setenv("XIAOYI_CATALOG_DB", str(db))
    monkeypatch.setenv("XIAOYI_CATALOG_DEV_AUTH", "1")
    monkeypatch.setenv("XIAOYI_CATALOG_EMBED", "0")  # 关闭模型加载，走结构化退化
    return db


class TestToolCalls:
    def test_search_evidence_and_pairing_flow(self, seeded_db):
        results = mcp_server.dataset_search({"modalities": ["text"], "languages": ["zh"]}, query="情感")
        assert results["ranking"] == "keyword"
        assert results["results"][0]["source_key"] == "a/zh-sentiment"
        dataset_id = results["results"][0]["id"]
        assert results["results"][0]["dataset_version_id"] is not None

        evidence = mcp_server.dataset_evidence(dataset_id)
        assert evidence["dataset"]["source_key"] == "a/zh-sentiment"
        assert evidence["evidence"][0]["kind"] == "card"
        assert evidence["evidence"][0]["dataset_version_id"] == evidence["dataset_version_id"]

        engine = create_engine_for(seeded_db)
        with session_scope(engine) as session:
            version_id = session.scalar(select(DatasetVersion.id))
        engine.dispose()
        proposal = mcp_server.pairing_propose(
            {"external_id": "lingxi-idea-mcp", "title": "MCP 链路", "requirements": {"modalities": ["text"]}},
            [version_id], "验证工具链路")
        assert proposal["created"][0]["status"] == "pending"

        status = mcp_server.snapshot_status("lingxi-idea-mcp")
        assert status["pairings"][0]["warnings"] == [] and status["snapshots"] == []

        with pytest.raises(PairingError, match="不存在"):
            mcp_server.snapshot_export(999)

    def test_evidence_for_unknown_dataset(self, seeded_db):
        assert "error" in mcp_server.dataset_evidence(4242)


def machine_token(db, username="agent", *, role="researcher", kind="agent"):
    from xiaoyi_catalog.auth import create_user, mint_session
    engine = create_engine_for(db)
    try:
        with session_scope(engine) as session:
            user = create_user(session, username, "fixture-password-123", role, kind)
            return mint_session(session, user)
    finally:
        engine.dispose()


def test_all_tools_require_independent_agent_token(seeded_db, monkeypatch):
    monkeypatch.delenv("XIAOYI_CATALOG_DEV_AUTH")
    monkeypatch.delenv("XIAOYI_CATALOG_AGENT_TOKEN", raising=False)
    with pytest.raises(PermissionError, match="TOKEN"):
        mcp_server.dataset_search({})
    token = machine_token(seeded_db, "human", kind="human")
    monkeypatch.setenv("XIAOYI_CATALOG_AGENT_TOKEN", token)
    with pytest.raises(PermissionError, match="智能体"):
        mcp_server.dataset_evidence(1)
    token = machine_token(seeded_db)
    monkeypatch.setenv("XIAOYI_CATALOG_AGENT_TOKEN", token)
    assert mcp_server.dataset_search({})["results"]
    run = mcp_server.agent_run_create({"goal": "整理文本数据", "sources": ["manual"]})
    assert run["actor"] == "agent" and run["status"] == "queued"
    assert token not in str(run)
    assert mcp_server.agent_run_status(run["id"])["events"]
    assert mcp_server.agent_run_cancel(run["id"])["status"] == "cancelled"
    assert mcp_server.agent_run_retry(run["id"])["status"] == "queued"


def test_read_only_and_expired_machine_tokens(seeded_db, monkeypatch):
    from datetime import timedelta
    from xiaoyi_catalog.models import utcnow
    from xiaoyi_catalog.models_assets import AuthSession
    monkeypatch.delenv("XIAOYI_CATALOG_DEV_AUTH")
    token = machine_token(seeded_db, "reader-agent", role="reader")
    monkeypatch.setenv("XIAOYI_CATALOG_AGENT_TOKEN", token)
    assert mcp_server.dataset_search({})["results"]
    with pytest.raises(PermissionError, match="读取"):
        mcp_server.agent_run_create({"goal": "整理文本数据"})
    with pytest.raises(PermissionError, match="读取"):
        mcp_server.download_request(1, ["data.csv"], 100)
    engine = create_engine_for(seeded_db)
    with session_scope(engine) as session:
        session.scalar(select(AuthSession)).expires_at = utcnow() - timedelta(seconds=1)
    engine.dispose()
    with pytest.raises(PermissionError, match="过期"):
        mcp_server.snapshot_status("anything")


def test_agent_cannot_cancel_another_agents_task(seeded_db, monkeypatch):
    from xiaoyi_catalog.agent_runtime import RuntimeErrorBase
    monkeypatch.delenv("XIAOYI_CATALOG_DEV_AUTH")
    token_a = machine_token(seeded_db, "agent-a")
    token_b = machine_token(seeded_db, "agent-b")
    monkeypatch.setenv("XIAOYI_CATALOG_AGENT_TOKEN", token_a)
    run = mcp_server.agent_run_create({"goal": "整理文本数据"})
    monkeypatch.setenv("XIAOYI_CATALOG_AGENT_TOKEN", token_b)
    with pytest.raises(RuntimeErrorBase, match="自己"):
        mcp_server.agent_run_cancel(run["id"])


def test_evidence_is_pinned_to_requested_version(seeded_db):
    first = mcp_server.dataset_search({})["results"][0]
    engine = create_engine_for(seeded_db)
    collect(engine, FakeAdapter([make_raw("a/zh-sentiment", revision="new", license="gpl-3.0",
        card="New card with different terms")]), CollectScope())
    engine.dispose()
    old = mcp_server.dataset_evidence(first["id"], first["dataset_version_id"])
    assert old["dataset"]["license"] == "mit"
    assert all("New card" not in e["excerpt"] for e in old["evidence"])
    latest = mcp_server.dataset_evidence(first["id"])
    assert latest["dataset"]["license"] == "gpl-3.0"
    assert latest["dataset_version_id"] != old["dataset_version_id"]


def test_no_approval_or_execution_tool_registered():
    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert not names.intersection({"approve_snapshot", "review_pairing", "decide_download", "process_next_download", "execute_run"})


def test_local_import_requests_are_not_consumed_by_download_worker(seeded_db):
    from xiaoyi_catalog.models_assets import DownloadRequest
    from xiaoyi_catalog.downloads import process_next_download
    engine = create_engine_for(seeded_db)
    with session_scope(engine) as session:
        version_id = session.scalar(select(DatasetVersion.id))
        request = DownloadRequest(dataset_version_id=version_id, requested_by="researcher", status="approved",
            manifest={"acquisition": "local"}, manifest_hash="fixture", approved_hash="fixture", max_bytes=100)
        session.add(request)
        session.flush()
        ident = request.id
    assert not process_next_download(engine, fetcher=lambda *args: pytest.fail("must not fetch local imports"))
    with session_scope(engine) as session:
        assert session.get(DownloadRequest, ident).status == "approved"
    engine.dispose()
