"""HTTP-layer tests for server.py -- the actual gap: server.py had zero
automated coverage before this, only manual curl smoke tests. These cover
every endpoint that doesn't require a live LLM call (routing, validation,
the Store wiring) using FastAPI's TestClient. The one endpoint that does call
the real agent (POST .../messages on a *valid* conversation) is intentionally
not exercised here -- that's what scripts/smoke_test_live_runner.py and
scripts/smoke_test_memory_db.py are for, live, against the real API.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from demo.tasks import ALL_TASKS
from storage.db import Store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path, monkeypatch):
    """Every test gets a fresh, temp-file-backed store, a clean conversations
    dict, and a clean rate-limit counter -- never touches the real
    state/console.db, and one test's data/counters can't leak into another's
    (TestClient requests all share the same "testclient" source IP)."""
    monkeypatch.setattr(server, "store", Store(tmp_path / "test_console.db"))
    server._conversations.clear()
    server._message_timestamps.clear()
    monkeypatch.delenv("ACCESS_PASSWORD", raising=False)
    yield


@pytest.fixture
def client():
    return TestClient(server.app)


def test_list_presets_matches_demo_tasks(client):
    res = client.get("/api/presets")
    assert res.status_code == 200
    presets = res.json()
    assert len(presets) == len(ALL_TASKS)
    assert {p["task_id"] for p in presets} == {t.task_id for t in ALL_TASKS}
    injected_task_id = next(t.task_id for t in ALL_TASKS if t.injected)
    assert next(p for p in presets if p["task_id"] == injected_task_id)["injected"] is True


def test_create_conversation_returns_thread_id_and_creates_session(client):
    res = client.post("/api/conversations", json={"entity_id": "test_entity"})
    assert res.status_code == 200
    thread_id = res.json()["thread_id"]
    assert thread_id in server._conversations

    sessions = client.get("/api/history/sessions").json()
    assert any(s["session_id"] == thread_id and s["entity_id"] == "test_entity" for s in sessions)


def test_create_conversation_defaults_entity_id(client):
    res = client.post("/api/conversations", json={})
    assert res.status_code == 200
    sessions = client.get("/api/history/sessions").json()
    assert sessions[0]["entity_id"] == "console_agent"


def test_reset_entity_clears_persistent_history(client):
    server.store.record_entity_resources("test_entity", ["a.txt", "b.txt"])
    assert server.store.entity_distinct_resources("test_entity") == ["a.txt", "b.txt"]

    res = client.post("/api/entities/test_entity/reset")
    assert res.status_code == 200
    assert server.store.entity_distinct_resources("test_entity") == []


def test_get_unknown_session_returns_404(client):
    res = client.get("/api/history/sessions/does-not-exist")
    assert res.status_code == 404


def test_get_known_session_returns_detail(client):
    thread_id = client.post("/api/conversations", json={"entity_id": "e1"}).json()["thread_id"]
    res = client.get(f"/api/history/sessions/{thread_id}")
    assert res.status_code == 200
    assert res.json()["session_id"] == thread_id
    assert res.json()["runs"] == []


def test_send_message_to_unknown_conversation_returns_404(client):
    res = client.post("/api/conversations/does-not-exist/messages", json={"text": "hello"})
    assert res.status_code == 404


def test_send_message_with_neither_text_nor_task_id_returns_400(client):
    thread_id = client.post("/api/conversations", json={}).json()["thread_id"]
    res = client.post(f"/api/conversations/{thread_id}/messages", json={})
    assert res.status_code == 400


def test_send_message_with_unknown_task_id_returns_400(client):
    thread_id = client.post("/api/conversations", json={}).json()["thread_id"]
    res = client.post(f"/api/conversations/{thread_id}/messages", json={"task_id": "not_a_real_task"})
    assert res.status_code == 400


def test_static_index_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Action Monitor Console" in res.content


def test_list_sessions_empty_when_nothing_recorded(client):
    assert client.get("/api/history/sessions").json() == []


# ---------------------------------------------------------------- rate limiting

def test_rate_limit_returns_429_past_the_threshold(client, monkeypatch):
    monkeypatch.setattr(server, "RATE_LIMIT_MAX_MESSAGES", 2)
    thread_id = client.post("/api/conversations", json={}).json()["thread_id"]

    # Invalid bodies (missing text/task_id) 400 quickly, with no LLM call needed,
    # but still count against the limit -- the limiter guards the endpoint
    # itself, not just successful runs.
    r1 = client.post(f"/api/conversations/{thread_id}/messages", json={})
    r2 = client.post(f"/api/conversations/{thread_id}/messages", json={})
    r3 = client.post(f"/api/conversations/{thread_id}/messages", json={})
    assert r1.status_code == 400
    assert r2.status_code == 400
    assert r3.status_code == 429


def test_rate_limit_keyed_by_client_host(monkeypatch):
    """The limiter is keyed on request.client.host directly (not a spoofable
    header like X-Forwarded-For) -- verified at the function level rather than
    via TestClient, which doesn't give control over the simulated peer IP."""
    monkeypatch.setattr(server, "RATE_LIMIT_MAX_MESSAGES", 1)
    server._message_timestamps.clear()

    server._check_rate_limit("10.0.0.1")  # first request from this IP: fine
    try:
        server._check_rate_limit("10.0.0.1")  # second from the same IP: over budget
        assert False, "expected the second call from the same IP to raise"
    except server.HTTPException as e:
        assert e.status_code == 429

    server._check_rate_limit("10.0.0.2")  # a different IP has its own, untouched budget


# ---------------------------------------------------------------- access gate

def test_access_gate_off_by_default(client):
    """No ACCESS_PASSWORD set -> every existing test above already proves this,
    but make it explicit: unauthenticated requests succeed."""
    assert client.get("/api/presets").status_code == 200


def test_access_gate_blocks_without_credentials_when_password_set(client, monkeypatch):
    monkeypatch.setenv("ACCESS_PASSWORD", "sekrit")
    res = client.get("/api/presets")
    assert res.status_code == 401


def test_access_gate_blocks_wrong_password(client, monkeypatch):
    monkeypatch.setenv("ACCESS_PASSWORD", "sekrit")
    res = client.get("/api/presets", auth=("anyuser", "wrong"))
    assert res.status_code == 401


def test_access_gate_allows_correct_password(client, monkeypatch):
    monkeypatch.setenv("ACCESS_PASSWORD", "sekrit")
    res = client.get("/api/presets", auth=("anyuser", "sekrit"))
    assert res.status_code == 200


def test_access_gate_ignores_username_only_checks_password(client, monkeypatch):
    """Deliberate design: a single shared password gate, not a real user
    system -- any username is accepted alongside the correct password."""
    monkeypatch.setenv("ACCESS_PASSWORD", "sekrit")
    res = client.get("/api/presets", auth=("whoever", "sekrit"))
    assert res.status_code == 200
