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

EXPECTED_TOOLS = {"dataset_search", "dataset_evidence", "pairing_propose", "snapshot_status", "snapshot_export"}


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
    monkeypatch.setenv("XIAOYI_CATALOG_EMBED", "0")  # 关闭模型加载，走结构化退化
    return db


class TestToolCalls:
    def test_search_evidence_and_pairing_flow(self, seeded_db):
        results = mcp_server.dataset_search({"modalities": ["text"], "languages": ["zh"]}, query="情感")
        assert results["ranking"] == "downloads"
        assert results["results"][0]["source_key"] == "a/zh-sentiment"
        dataset_id = results["results"][0]["id"]

        evidence = mcp_server.dataset_evidence(dataset_id)
        assert evidence["dataset"]["source_key"] == "a/zh-sentiment"
        assert evidence["evidence"][0]["kind"] == "card"

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
