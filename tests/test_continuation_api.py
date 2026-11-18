"""Exercise the brief's public HTTP entry points across two actual app lifetimes."""
import json
import time

from fastapi.testclient import TestClient

from greatsage.runtime import Runtime
from greatsage.server import create_app


def app(directory):
    runtime = Runtime(directory)
    model_calls = []

    async def inference(task, step, purpose, messages, config, schema, source_ids):
        payload = json.loads(messages[-1]["content"])
        model_calls.append(purpose)
        if purpose == "continuation_brief":
            return {"items": [{"unit": unit["unit"], "section": "next" if "负责" in unit["text"] else "decision"}
                              for unit in payload["units"]]}
        row = next(row for row in payload["rows"] if "顾言" in row["text"])
        return {"items": [{"target": row["target"], "old": "顾言", "new": "林岚", "section": "next"}]}

    runtime.tasks._model_json = inference
    runtime._schedule_compression = lambda: None
    runtime.settings.update({"voice_enabled": False, "memory_extraction_enabled": False})
    client = TestClient(create_app(directory, "brief-test", runtime=runtime))
    client.headers["Authorization"] = "Bearer brief-test"
    return client, runtime, model_calls


def wait_for(client, task, *statuses):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        data = client.get(f"/api/tasks/{task}").json()
        if data["status"] in statuses:
            return data
        assert data["status"] not in {"failed", "cancelled"}, data.get("error")
        time.sleep(.03)
    raise AssertionError(data["status"])


def pending(client, task):
    wait_for(client, task, "waiting_approval")
    return next(call for call in client.get("/api/approvals").json() if call["task_id"] == task)


def test_restart_resume_revise_and_revoke_through_http(tmp_path):
    path = tmp_path / "workspace" / "notes" / "brief.md"
    first_client, first, first_calls = app(tmp_path)
    with first_client as client:
        session = first.session_id
        decision = first.memory.add_message("user", "已经决定首批只开放 Windows。", "text", session)
        first.memory.add_message("user", "顾言负责整理兼容清单。", "text", session)
        foreign = first.memory.new_session()
        assert client.get("/api/continuation/scope", params={"session_id": session}).json()["message_count"] == 2
        body = {"kind": "continuation", "session_ids": [foreign]}
        assert client.post("/api/tasks", json=body).status_code == 400
        body.update(session_ids=[session], local_date="2000-01-01", save_path="notes/brief.md")
        assert client.post("/api/tasks", json=body).status_code == 400
        body.pop("local_date")
        created = client.post("/api/tasks", json=body).json()
        assert created["profile"] == "continuation"
        task = created["id"]
        old = pending(client, task)
        doc = old["arguments"]["artifact_id"]
        assert first_calls == ["continuation_brief"] and not path.exists()

    second_client, runtime, calls = app(tmp_path)
    with second_client as client:
        assert runtime.session_id == session and not runtime.listening
        assert not calls and not client.get("/api/approvals").json()
        detail = client.get(f"/api/artifacts/{doc}").json()
        assert (detail["session_id"], detail["profile"], detail["version"]) == (session, "continuation", 1)
        stale = f"/api/tool-calls/{old['id']}/approve"
        assert client.post(stale, json={}).status_code == 400
        client.post(f"/api/sessions/{foreign}/activate").raise_for_status()
        resume = f"/api/tasks/{task}/resume"
        assert client.post(resume).status_code == 400
        client.post(f"/api/sessions/{session}/activate").raise_for_status()
        client.post(resume).raise_for_status()
        fresh = pending(client, task)
        assert fresh["id"] != old["id"] and not calls and not path.exists()
        assert client.post(stale, json={}).status_code == 400
        client.post(f"/api/tool-calls/{fresh['id']}/approve", json={}).raise_for_status()
        wait_for(client, task, "succeeded")
        assert path.read_text(encoding="utf-8") == runtime.memory.export_markdown(doc)
        correction = {"expected_version": 1, "instructions": "负责人从顾言改为林岚。"}
        task2 = client.post(f"/api/artifacts/{doc}/regenerate", json=correction).json()["id"]
        pending2 = pending(client, task2)
        review = client.get(f"/api/artifacts/{doc}/review").json()
        assert review["diff"]["from_version"] == 1 and calls == ["continuation_patch"]
        assert client.post(f"/api/artifacts/{doc}/regenerate", json=correction).status_code == 400
        client.delete(f"/api/history/{decision['id']}").raise_for_status()
        assert client.post(f"/api/tool-calls/{pending2['id']}/approve", json={}).status_code == 400
        assert not (tmp_path / "workspace" / pending2["arguments"]["path"]).exists()
        assert client.get(f"/api/artifacts/{doc}").json()["invalid_markers"]
