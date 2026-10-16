from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from xiaoyi_catalog.auth import create_user, mint_session
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models_qa import QaTurn
from xiaoyi_catalog.service import create_app


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv("XIAOYI_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("XIAOYI_LLM_MODEL", "test-model")
    monkeypatch.setenv("XIAOYI_LLM_API_KEY", "not-a-real-key")
    monkeypatch.setenv("XIAOYI_LLM_TIMEOUT", "60")
    app = create_app(str(tmp_path / "qa-api.db"), dev_auth=False)
    tokens = {}
    with session_scope(app.state.engine) as session:
        for name, role in (("reader", "reader"), ("other", "admin")):
            user = create_user(session, name, "test-password-123", role)
            tokens[name] = mint_session(session, user)
    with TestClient(app) as client:
        yield app, client, {name: {"Authorization": "Bearer " + token} for name, token in tokens.items()}


def conversation(client, headers):
    response = client.post("/api/qa-conversations", json={}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def question(client, ident, headers, *, key="question_001", text="当前收集了多少数据集？"):
    return client.post(f"/api/qa-conversations/{ident}/turns", headers=headers,
                       json={"question": text, "request_key": key})


def test_reader_can_ask_but_cannot_write_catalog(clients):
    app, client, headers = clients
    reader = headers["reader"]
    assert client.get("/api/qa-capabilities").status_code == 401
    capabilities = client.get("/api/qa-capabilities", headers=reader).json()
    assert capabilities["configured"] and capabilities["model"] == "test-model"
    assert "not-a-real-key" not in str(capabilities) and "model.example" not in str(capabilities)
    ident = conversation(client, reader)
    response = question(client, ident, reader)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued" and response.json()["answer"] is None
    assert client.post("/api/datasets/register", headers=reader, json={}).status_code == 403
    assert client.post("/api/qa-conversations/1/approve", headers=reader, json={}).status_code == 403
    assert client.post("/api/qa-conversations", headers=reader,
                       json={"actor": "other", "title": "fake"}).status_code == 422
    assert client.post("/api/qa-conversations", headers={**reader, "Origin": "https://attacker.example"},
                       json={}).status_code == 403


def test_owner_scope_even_other_admin_cannot_see_or_cancel(clients):
    _, client, headers = clients
    ident = conversation(client, headers["reader"])
    turn = question(client, ident, headers["reader"]).json()
    assert client.get("/api/qa-conversations", headers=headers["other"]).json() == {"items": []}
    assert client.get(f"/api/qa-conversations/{ident}", headers=headers["other"]).status_code == 404
    assert question(client, ident, headers["other"]).status_code == 404
    for action in ("cancel", "retry"):
        assert client.post(f"/api/qa-turns/{turn['id']}/{action}", headers=headers["other"]).status_code == 404
    detail = client.get(f"/api/qa-conversations/{ident}", headers=headers["reader"]).json()
    assert detail["actor"] == "reader" and len(detail["turns"]) == 1


def test_idempotency_and_one_pending_turn(clients, monkeypatch):
    app, client, headers = clients
    ident = conversation(client, headers["reader"])
    first = question(client, ident, headers["reader"])
    assert question(client, ident, headers["reader"]).json()["id"] == first.json()["id"]
    assert question(client, ident, headers["reader"], text="different").status_code == 409
    assert question(client, ident, headers["reader"], key="question_002").status_code == 409
    monkeypatch.delenv("XIAOYI_LLM_API_KEY")
    assert question(client, ident, headers["reader"]).json()["id"] == first.json()["id"]
    with session_scope(app.state.engine) as session:
        assert len(session.scalars(select(QaTurn)).all()) == 1


def test_queued_cancel_and_bounded_retry(clients):
    app, client, headers = clients
    ident = conversation(client, headers["reader"])
    turn = question(client, ident, headers["reader"]).json()
    url = f"/api/qa-turns/{turn['id']}"
    cancelled = client.post(url + "/cancel", headers=headers["reader"])
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["finished_at"]
    retried = client.post(url + "/retry", headers=headers["reader"])
    assert retried.status_code == 200 and retried.json()["status"] == "queued"
    assert client.post(url + "/retry", headers=headers["reader"]).status_code == 409
    with session_scope(app.state.engine) as session:
        row = session.get(QaTurn, turn["id"])
        row.status, row.attempts, row.model_calls = "failed", 2, 2
    assert client.post(url + "/retry", headers=headers["reader"]).status_code == 409


def test_missing_config_is_explicit_503_without_placeholder_turn(clients, monkeypatch):
    app, client, headers = clients
    monkeypatch.delenv("XIAOYI_LLM_API_KEY")
    ident = conversation(client, headers["reader"])
    response = question(client, ident, headers["reader"])
    assert response.status_code == 503 and "配置" in response.json()["detail"]
    assert client.get("/api/qa-capabilities", headers=headers["reader"]).json()["configured"] is False
    with session_scope(app.state.engine) as session:
        assert session.scalar(select(QaTurn.id)) is None


def test_question_validation_and_completed_cancel_idempotence(clients):
    app, client, headers = clients
    ident = conversation(client, headers["reader"])
    assert question(client, ident, headers["reader"], text="  ").status_code == 422
    assert question(client, ident, headers["reader"], text="a" * 4001).status_code == 422
    turn = question(client, ident, headers["reader"]).json()
    with session_scope(app.state.engine) as session:
        row = session.get(QaTurn, turn["id"])
        row.status, row.answer = "completed", {"claims": [], "uncertainty": ["未查询"]}
    response = client.post(f"/api/qa-turns/{turn['id']}/cancel", headers=headers["reader"])
    assert response.json()["status"] == "completed" and response.json()["answer"]["uncertainty"] == ["未查询"]


def test_concurrent_duplicate_requests_queue_one_turn(clients):
    from concurrent.futures import ThreadPoolExecutor
    app, client, headers = clients
    ident = conversation(client, headers["reader"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: question(client, ident, headers["reader"]), range(2)))
    assert [result.status_code for result in results] == [202, 202]
    assert results[0].json()["id"] == results[1].json()["id"]
    with session_scope(app.state.engine) as session:
        assert len(session.scalars(select(QaTurn)).all()) == 1
