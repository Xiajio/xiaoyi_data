from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import FakeAdapter, make_raw
from xiaoyi_catalog.auth import create_user, mint_session
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.service import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "http-collection.db"), dev_auth=True)
    with TestClient(app) as client:
        yield client


def test_collection_http_queue_validation_and_manual_control(client):
    response = client.post("/api/collection-jobs", json={"search": "news", "limit": 5})
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "queued" and job["scope"]["limit"] == 5
    ident = job["id"]
    assert client.get(f"/api/collection-jobs/{ident}").json()["id"] == ident
    assert client.post("/api/collection-jobs", json={"search": "news", "limit": 5}).json()["id"] == ident
    assert client.get(f"/api/collection-jobs/{ident}/events").json()["items"]
    assert client.post("/api/collection-jobs", json={"limit": 301}).status_code == 422
    assert client.post("/api/collection-jobs", json={"endpoint": "http://internal.local"}).status_code == 422
    assert client.post("/api/collection-jobs", json={"languages": [""]}).status_code == 422
    assert client.post(f"/api/collection-jobs/{ident}/cancel").json()["status"] == "cancelled"
    retried = client.post(f"/api/collection-jobs/{ident}/retry")
    assert retried.status_code == 201, retried.text
    assert retried.json()["status"] == "queued"
    assert client.get("/api/collection-jobs/9999").status_code == 404
    assert client.get("/api/collection-jobs/9999/events").status_code == 404


def test_schedule_http_enable_pause_and_validation(client):
    response = client.post("/api/collection-schedules", json={
        "name": "每日 HF", "scope": {"limit": 50}, "interval_minutes": 1440, "enabled": False})
    assert response.status_code == 201, response.text
    schedule = response.json()
    ident = schedule["id"]
    assert schedule["enabled"] is False and schedule["next_run_at"] is None
    enabled = client.post(f"/api/collection-schedules/{ident}", json={"enabled": True}).json()
    assert enabled["enabled"] is True and enabled["next_run_at"]
    assert enabled["scope"]["limit"] == 50
    paused = client.post(f"/api/collection-schedules/{ident}", json={"enabled": False}).json()
    assert paused["enabled"] is False and paused["next_run_at"] is None
    assert client.get("/api/collection-schedules").json()["items"][0]["id"] == ident
    assert client.post(f"/api/collection-schedules/{ident}", json={"interval_minutes": 1}).status_code == 422
    assert client.post(f"/api/collection-schedules/{ident}", json={"enabled": None}).status_code == 422
    assert client.post(f"/api/collection-schedules/{ident}", json={"scope": {"endpoint": "bad"}}).status_code == 422
    assert client.post("/api/collection-schedules/9999", json={"enabled": False}).status_code == 404


def test_real_identity_controls_schedule_and_job_ownership(tmp_path):
    app = create_app(str(tmp_path / "permissions.db"), dev_auth=False)
    tokens = {}
    with session_scope(app.state.engine) as session:
        for username, role, kind in [
            ("reader", "reader", "human"), ("alice", "researcher", "human"),
            ("bob", "researcher", "human"), ("reviewer", "reviewer", "human"),
            ("machine", "admin", "agent")]:
            user = create_user(session, username, "test-password-123" if kind == "human" else "", role, kind)
            tokens[username] = mint_session(session, user)
    headers = lambda user: {"Authorization": "Bearer " + tokens[user]}
    with TestClient(app) as client:
        assert client.get("/api/collection-jobs").status_code == 401
        assert client.get("/api/collection-jobs", headers=headers("reader")).status_code == 200
        assert client.post("/api/collection-jobs", headers=headers("reader"), json={}).status_code == 403
        job = client.post("/api/collection-jobs", headers=headers("alice"), json={"search": "private-scope"}).json()
        assert client.post(f"/api/collection-jobs/{job['id']}/cancel", headers=headers("bob")).status_code in {400, 403}
        schedule = {"name": "Daily", "enabled": True, "interval_minutes": 1440, "scope": {"limit": 5}}
        for user in ("reader", "alice", "machine"):
            assert client.post("/api/collection-schedules", headers=headers(user), json=schedule).status_code == 403
        allowed = client.post("/api/collection-schedules", headers=headers("reviewer"), json=schedule)
        assert allowed.status_code == 201, allowed.text
        assert allowed.json()["created_by"] == "reviewer"
        assert client.post(f"/api/collection-jobs/{job['id']}/cancel", headers=headers("reviewer")).json()["status"] == "cancelled"


def test_completed_collection_exposes_pinned_evidence_without_model(client, monkeypatch):
    from xiaoyi_catalog.collection_runtime import process_next_collection
    from xiaoyi_catalog.models import DatasetVersion
    for key in ("XIAOYI_LLM_BASE_URL", "XIAOYI_LLM_MODEL", "XIAOYI_LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    adapter = FakeAdapter([make_raw("research/news", revision="a" * 40, license="mit",
        tags=("modality:text",), card="## Dataset\nA text dataset.\n## Features\n- text: string")])
    adapter.kind = "huggingface"
    response = client.post("/api/collection-jobs", json={"search": "news", "limit": 5})
    ident = response.json()["id"]
    assert process_next_collection(client.app.state.engine, adapter_factory=lambda: adapter)
    job = client.get(f"/api/collection-jobs/{ident}").json()
    assert job["status"] == "completed", job
    assert job["analysis_status"] == "rule_based"
    assert job["stats"]["new"] == 1
    assert len(job["dataset_version_ids"]) == 1
    version_id = job["dataset_version_ids"][0]
    evidence = client.get(f"/api/dataset-versions/{version_id}/evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["revision"] == "a" * 40
    assert evidence.json()["items"]
    assert {e["dataset_version_id"] for e in evidence.json()["items"]} == {version_id}
    with session_scope(client.app.state.engine) as session:
        version = session.get(DatasetVersion, version_id)
        assert version.metadata_json["license"] == "mit"
    assert client.get("/api/dataset-versions/9999/evidence").status_code == 404
