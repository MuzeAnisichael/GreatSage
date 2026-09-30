"""HTTP/WS integration without microphones, real credentials or paid requests."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from greatsage.runtime import Runtime
from greatsage.server import create_app


class FixtureProviders:
    async def stream_chat(self, config, messages):
        yield {"text": "已收到合成测试。"}

    async def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "SERVER_TEST_CREDENTIAL")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    runtime = Runtime(tmp_path, providers=FixtureProviders())
    runtime.settings.update({"voice_enabled": False})
    app = create_app(tmp_path, "fixture-token", runtime=runtime)
    with TestClient(app) as session:
        session.headers["Authorization"] = "Bearer fixture-token"
        yield session, runtime


def test_http_and_websocket_require_token_and_reject_foreign_origin(client):
    session, runtime = client
    assert session.get("/health", headers={"Authorization": ""}).status_code == 200
    assert session.get("/api/settings", headers={"Authorization": ""}).status_code == 401
    assert session.get("/api/history", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert session.get("/api/history", headers={"Host": "foreign.example"}).status_code == 400
    for path, headers in [("/ws?token=wrong", {}),
                           ("/ws?token=fixture-token", {"Origin": "https://foreign.example"}),
                           ("/ws?token=%E4%B8%AD%E6%96%87", {})]:
        with pytest.raises(WebSocketDisconnect):
            with session.websocket_connect(path, headers=headers):
                pass
    assert not runtime.subscribers


def test_settings_roundtrip_hides_credentials_and_rejects_unavailable_features(client):
    session, runtime = client
    response = session.get("/api/settings")
    assert response.status_code == 200
    assert "SERVER_TEST_CREDENTIAL" not in response.text
    assert response.json()["llm"]["key_configured"]
    assert session.put("/api/settings", json=response.json()).status_code == 200
    assert session.put("/api/settings", json={"timed_reminders": True}).status_code == 400
    assert not runtime.listening
    assert "no-store" in response.headers["cache-control"]


def test_chat_stream_is_audited_then_deleted_with_derived_answer(client):
    session, runtime = client
    with session.websocket_connect("/ws?token=fixture-token") as stream:
        assert stream.receive_json()["kind"] == "state"
        sent = session.post("/api/chat", json={"text": "请记住：测试代号是蓝色星星"})
        assert sent.status_code == 200
        events = []
        while len(events) < 40:
            event = stream.receive_json()
            events.append(event)
            if event["kind"] == "response_done":
                break
        assert any(event["kind"] == "response_delta" for event in events)
        assert events[-1]["data"]["complete"]
        assert len(session.get("/api/memories").json()) == 1
        history = session.get("/api/history").json()
        assert [entry["role"] for entry in history] == ["user", "assistant"]
        assert session.delete(f"/api/history/{sent.json()['id']}").status_code == 200
        assert session.get("/api/history").json() == []
        assert session.get("/api/memories").json() == []
        assert "蓝色星星" not in session.get("/api/events").text


def test_memory_revision_session_persistence_and_explicit_clear(client):
    session, runtime = client
    created = session.post("/api/memories", json={"text": "独立显式记忆"}).json()
    changed = session.put(f"/api/memories/{created['id']}", json={"text": "修正后的独立记忆"}).json()
    assert changed["id"] != created["id"]
    before = session.get("/api/status").json()["session_id"]
    assert session.post("/api/sessions").json()["session_id"] != before
    assert session.get("/api/memories").json()[0]["text"] == "修正后的独立记忆"
    assert session.post("/api/history/clear", json={}).status_code == 400
    assert session.post("/api/history/clear", json={"confirmation": "DELETE"}).status_code == 200
    assert len(session.get("/api/memories").json()) == 1
    assert session.delete(f"/api/memories/{changed['id']}").status_code == 200
    assert session.get("/api/memories").json() == []


def test_validation_and_unavailable_audio(client):
    session, _ = client
    assert session.post("/api/chat", json={"text": " "}).status_code == 400
    assert session.post("/api/listening", json={"enabled": "yes"}).status_code == 400
    assert session.post("/api/playback", json={"playing": "yes"}).status_code == 400
    assert session.get("/api/recordings/not-a-uuid").status_code == 404
    assert session.get("/api/audio/expired-audio").status_code == 404
    assert session.get("/api/settings", headers={"Content-Length": "2000001"}).status_code == 413


def test_materials_tasks_approvals_and_artifact_endpoints(client, tmp_path):
    session, runtime = client
    note = tmp_path / "import" / "会议.md"
    note.parent.mkdir()
    note.write_text("# 周会\n周五发布。\n", encoding="utf-8")
    assert session.post("/api/materials/import", json={"path": "relative.md"}).status_code == 400
    assert session.post("/api/materials/import", json={"path": str(note)}, headers={"Authorization": ""}).status_code == 401
    material_id = session.post("/api/materials/import", json={"path": str(note)}).json()["materials"][0]["id"]
    chunk = session.get(f"/api/materials/{material_id}").json()["chunks"][0]
    assert chunk["heading"] == "周会" and chunk["line_start"] == 1
    assert session.get("/api/materials/search", params={"q": "周五发布"}).json()[0]["material_id"] == material_id
    for body in ({"kind": "shell"}, {"document": "yes"}, {"material_ids": ["missing"]}, {"title": "x" * 81}):
        assert session.post("/api/tasks", json=body).status_code == 400
    artifact = runtime.memory.create_artifact("minutes", "周会纪要", "- 周五发布 [S1]\n",
                                              {"S1": {"kind": "chunk", "id": chunk["id"], "label": "会议.md", "excerpt": "周五发布"}})
    exported = session.get(f"/api/artifacts/{artifact['id']}/export")
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "filename*=UTF-8''" in exported.headers["content-disposition"] and "## 来源" in exported.text
    assert session.put(f"/api/artifacts/{artifact['id']}", json={"content": " "}).status_code == 400
    assert session.put(f"/api/artifacts/{artifact['id']}", json={"content": "- 周五发布 [S1]\n补充\n"}).json()["version"] == 2
    assert session.get("/api/approvals").json() == []
    assert session.post("/api/tool-calls/missing/approve", json={}).status_code == 400
    assert session.post("/api/tool-calls/missing/approve", json={"scope": "forever"}).status_code == 400
    tools = {tool["name"]: tool for tool in session.get("/api/tools").json()["tools"]}
    assert not tools["delete_file"]["standing_allow"] and not tools["run_command"]["voice"]
    assert session.put("/api/settings", json={"tool_rules": [{"tool": "run_command", "match": "*", "action": "allow"}]}).status_code == 400
    assert session.delete(f"/api/materials/{material_id}").status_code == 200
    assert session.get(f"/api/artifacts/{artifact['id']}").json()["status"] == "stale"
    assert session.delete(f"/api/artifacts/{artifact['id']}").status_code == 200
    assert session.get(f"/api/artifacts/{artifact['id']}").status_code == 404


def test_audit_export_requires_auth_and_explicit_body_and_respects_source_deletion(client):
    session, runtime = client
    source = runtime.memory.add_message('user', 'snapshot private body')
    id = runtime.memory.save_snapshot('audit-trace', 'response', {}, [{'content': source['text']}], [source['id']], retain_content=True)
    assert session.get('/api/audit', headers={'Authorization': ''}).status_code == 401
    assert session.get('/api/audit?trace_id=audit-trace').json()[0]['id'] == id
    assert 'snapshot private body' not in session.get(f'/api/audit/{id}').text
    assert 'snapshot private body' in session.post(f'/api/audit/{id}/export', json={'include_content': True}).text
    assert session.post(f'/api/audit/{id}/export', json={'include_content': 'true'}).status_code == 400
    assert session.get(f'/api/records/{source["id"]}').json()['record']['text'] == source['text']
    assert session.delete(f'/api/history/{source["id"]}').status_code == 200
    assert 'snapshot private body' not in session.post(f'/api/audit/{id}/export', json={'include_content': True}).text
