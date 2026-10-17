"""目录服务测试：FastAPI TestClient 覆盖全部端点与错误路径。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import create_engine_for, init_db, session_scope
from xiaoyi_catalog.enrich import enrich_all
from xiaoyi_catalog.models import Dataset, DatasetVersion
from xiaoyi_catalog.pairing import propose_pairing
from xiaoyi_catalog.service import create_app

from sqlalchemy import select

from conftest import FakeAdapter, make_raw


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "catalog.db"
    engine = create_engine_for(db)
    init_db(engine)
    collect(engine, FakeAdapter([
        make_raw("a/zh-sentiment", tasks=("text-classification",), languages=("zh",), license="mit",
                 card="中文情感分类数据集卡片", size_rows=100, downloads=10),
        make_raw("b/imgs", tasks=("image-classification",), license="gpl-3.0", card="images", downloads=50),
    ]), CollectScope())
    enrich_all(engine)
    engine.dispose()
    app = create_app(str(db), dev_auth=True)
    with TestClient(app) as test_client:
        yield test_client


def _version_id(client, dataset_id):
    return client.get(f"/api/datasets/{dataset_id}").json()["versions"][0]["id"]


class TestReadEndpoints:
    def test_overview_counts(self, client):
        stats = client.get("/api/overview").json()
        assert stats["datasets"] == 2
        assert stats["enrichment_candidates"] == 4  # 每集 schema + quality
        assert stats["pairings_pending"] == 0 and stats["ideas"] == 0

    def test_datasets_filter_and_pagination(self, client):
        all_items = client.get("/api/datasets").json()
        assert all_items["total"] == 2 and all_items["items"][0]["source_key"] == "b/imgs"  # 按下载量
        assert client.get("/api/datasets", params={"modality": "text"}).json()["total"] == 1
        assert client.get("/api/datasets", params={"license_class": "copyleft"}).json()["total"] == 1
        assert client.get("/api/datasets", params={"q": "情感"}).json()["total"] == 1
        assert client.get("/api/datasets", params={"size": 1, "page": 2}).json()["items"][0]["source_key"] == "a/zh-sentiment"

    def test_dataset_detail_404(self, client):
        assert client.get("/api/datasets/999").status_code == 404

    def test_enrichment_queue_and_detail(self, client):
        queue = client.get("/api/review/enrichments", params={"status": "candidate"}).json()
        assert len(queue["items"]) == 4 and queue["items"][0]["dataset"]
        schema_items = client.get("/api/review/enrichments", params={"kind": "schema"}).json()["items"]
        assert all(item["kind"] == "schema" for item in schema_items)


class TestReviewFlow:
    def test_enrichment_review_writes_review_and_audit(self, client):
        target = client.get("/api/review/enrichments").json()["items"][0]
        response = client.post(f"/api/review/enrichments/{target['id']}", json={"decision": "confirmed", "note": "人工核对"})
        assert response.json()["status"] == "confirmed"
        assert client.post(f"/api/review/enrichments/{target['id']}", json={"decision": "confirmed"}).status_code == 400
        assert client.post("/api/review/enrichments/999", json={"decision": "confirmed"}).status_code == 404
        audits = client.get("/api/audit").json()["items"]
        assert audits[0]["action"] == "enrichment_review" and "人工核对" in audits[0]["detail"]

    def test_pairing_review_freeze_approve_export_chain(self, client):
        version_id = _version_id(client, 1)
        # 经 MCP 同款的 pairing 层直接落一条提案（模拟智能体侧调用）
        engine = create_engine_for(client.app.state.db_path)
        with session_scope(engine) as session:
            proposal = propose_pairing(session, {"external_id": "idea-svc", "title": "服务端到端",
                                                 "requirements": {"modalities": ["text"]}},
                                       [version_id], "测试提案")
            pairing_id = proposal["created"][0]["id"]
        engine.dispose()

        pending = client.get("/api/pairings", params={"status": "pending"}).json()["items"]
        assert pending[0]["id"] == pairing_id and pending[0]["proposed_by"] == "agent"

        response = client.post(f"/api/pairings/{pairing_id}/review", json={"decision": "confirmed", "note": "ok"})
        assert response.json()["status"] == "confirmed"

        frozen = client.post("/api/snapshots/freeze", json={"idea_external_id": "idea-svc", "note": "首版"})
        assert frozen.json()["status"] == "pending"
        snapshot_id = frozen.json()["id"]

        assert client.get(f"/api/snapshots/{snapshot_id}/export").status_code == 400  # 未批准不可导出
        approved = client.post(f"/api/snapshots/{snapshot_id}/approve", json={"role": "lead"})
        assert approved.json()["status"] == "approved"
        exported = client.get(f"/api/snapshots/{snapshot_id}/export").json()
        assert exported["licenses"] == ["mit"] and exported["idea"]["external_id"] == "idea-svc"

        ideas = client.get("/api/ideas").json()["items"]
        assert ideas[0]["external_id"] == "idea-svc"
        assert ideas[0]["status"]["snapshots"][0]["status"] == "approved"
        actions = [item["action"] for item in client.get("/api/audit").json()["items"]]
        assert "pairing_review" in actions and "snapshot_freeze" in actions and "snapshot_approve" in actions

    def test_pairing_review_validation(self, client):
        assert client.post("/api/pairings/999/review", json={"decision": "confirmed"}).status_code == 400
        assert client.post("/api/snapshots/freeze", json={"idea_external_id": "nothing"}).status_code == 400


class TestOpsEndpoints:
    def test_runs_listed(self, client):
        runs = client.get("/api/runs").json()["items"]
        assert runs and runs[0]["status"] == "completed" and runs[0]["stats"]["new"] == 2


class TestAgentEndpoints:
    def test_match_filters_by_requirements_and_degrades_without_embedder(self, client):
        response = client.post("/api/match", json={"requirements": {"modalities": ["text"]}, "query": "情感"})
        payload = response.json()
        assert response.status_code == 200 and payload["ranking"] == "keyword"  # 无模型仍执行关键词检索
        assert [item["source_key"] for item in payload["results"]] == ["a/zh-sentiment"]
        empty = client.post("/api/match", json={"requirements": {"modalities": ["3d"]}}).json()
        assert empty["results"] == []

    def test_propose_creates_pending_pairing_with_audit(self, client):
        version_id = _version_id(client, 1)
        response = client.post("/api/pairings", json={
            "idea": {"external_id": "agent-idea-1", "title": "智能体提案测试",
                     "requirements": {"modalities": ["text"]}, "source_refs": ["10.0/t"]},
            "dataset_version_ids": [version_id], "rationale": "覆盖目标模态"})
        assert response.status_code == 201
        assert response.json()["created"][0]["status"] == "pending"
        status = client.get("/api/ideas/agent-idea-1/status").json()
        assert status["pairings"][0]["proposed_by"] == "agent"
        actions = [item["action"] for item in client.get("/api/audit").json()["items"]]
        assert "pairing_propose" in actions

    def test_propose_validation_errors(self, client):
        bad_idea = client.post("/api/pairings", json={"idea": {"title": "缺 id"}, "dataset_version_ids": [1],
                                                      "rationale": "x"})
        assert bad_idea.status_code == 400
        missing_version = client.post("/api/pairings", json={
            "idea": {"external_id": "agent-idea-2", "title": "t", "requirements": {"modalities": ["text"]}},
            "dataset_version_ids": [999], "rationale": "x"})
        assert missing_version.status_code == 400
        assert "999" in missing_version.json()["detail"]

    def test_idea_status_unknown_is_empty_shape(self, client):
        assert client.get("/api/ideas/no-such/status").json() == {"idea": None, "pairings": [], "snapshots": []}
