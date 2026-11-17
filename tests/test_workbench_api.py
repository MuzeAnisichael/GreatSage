"""Workbench session navigation through the real local HTTP routes, offline."""
import pytest
from fastapi.testclient import TestClient

from greatsage.runtime import Runtime
from greatsage.server import create_app


class OfflineProviders:
    async def stream_chat(self, config, messages):
        raise AssertionError("Navigation must not invoke a model")
        yield

    async def close(self):
        pass


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    instance = Runtime(tmp_path, providers=OfflineProviders())
    instance.settings.update({"voice_enabled": False, "memory_extraction_enabled": False,
                              "embedding": {"enabled": False}})
    monkeypatch.setattr(instance, "_schedule_compression", lambda: None)
    with TestClient(create_app(tmp_path, "workbench-test", runtime=instance)) as client:
        client.headers["Authorization"] = "Bearer workbench-test"
        yield client, instance


def test_activate_returns_selected_history_and_rejects_missing_session_without_mutation(workbench):
    client, runtime = workbench
    first = runtime.session_id
    original = runtime.memory.add_message("user", "Yesterday synthetic conversation", session_id=first)
    second = client.post("/api/sessions").json()["session_id"]
    runtime.memory.add_message("user", "Different synthetic conversation", session_id=second)
    assert client.post(f"/api/sessions/{first}/activate", headers={"Authorization": ""}).status_code == 401
    response = client.post(f"/api/sessions/{first}/activate")
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == data["status"]["session_id"] == first
    assert [item["id"] for item in data["history"]] == [original["id"]]
    assert not data["status"]["listening"]
    assert [row["id"] for row in client.get("/api/sessions").json() if row["active"]] == [first]
    before = (runtime.epoch, runtime.data_epoch, runtime.generation)
    assert client.post("/api/sessions/not-a-session/activate").status_code == 400
    assert (runtime.epoch, runtime.data_epoch, runtime.generation) == before
    assert runtime.memory.active_session() == first


def test_task_and_artifact_lists_filter_sessions_and_include_revision_lineage(workbench):
    client, runtime = workbench
    first, second = runtime.session_id, runtime.memory.new_session()

    def task(session_id):
        id = runtime.memory.create_task("research", "Synthetic task", "user_text", session_id,
                                        "synthetic-trace", {}, [])
        runtime.memory.update_task(id, status="succeeded")
        return id

    original_task, revision_task = task(first), task(second)
    original = runtime.memory.create_artifact("document", "Synthetic document", "First version", {}, original_task)
    revised = runtime.memory.commit_revision(original["id"], 1, revision_task, "Second version", {})
    only_second = runtime.memory.create_artifact("document", "Second document", "Separate document", {}, revision_task)
    detached = runtime.memory.create_artifact("document", "Detached document", "Unrelated document", {})
    assert revised["version"] == 2
    assert {row["id"] for row in client.get("/api/tasks").json()} == {original_task, revision_task}
    assert [row["id"] for row in client.get("/api/tasks", params={"session_id": first}).json()] == [original_task]
    assert [row["id"] for row in client.get("/api/tasks", params={"session_id": second}).json()] == [revision_task]
    assert {row["id"] for row in client.get("/api/artifacts", params={"session_id": first}).json()} == {original["id"]}
    assert {row["id"] for row in client.get("/api/artifacts", params={"session_id": second}).json()} == {original["id"], only_second["id"]}
    assert {row["id"] for row in client.get("/api/artifacts").json()} == {original["id"], only_second["id"], detached["id"]}
    assert client.get("/api/tasks", params={"session_id": "missing"}).json() == []
    assert client.get("/api/artifacts", params={"session_id": "missing"}).json() == []


def test_clear_history_creates_a_valid_empty_active_session(workbench):
    client, runtime = workbench
    old = runtime.session_id
    runtime.memory.add_message("user", "Synthetic erased history", session_id=old)
    assert client.post("/api/history/clear", json={"confirmation": "DELETE"}).status_code == 200
    new = client.get("/api/status").json()["session_id"]
    assert new != old and runtime.memory.active_session() == new
    sessions = client.get("/api/sessions").json()
    assert len(sessions) == 1 and sessions[0]["id"] == new and sessions[0]["active"]
    assert client.get("/api/history", params={"session_id": new}).json() == []
    assert client.post(f"/api/sessions/{old}/activate").status_code == 400


def test_source_review_and_revision_routes_preserve_scope_and_version(workbench, monkeypatch):
    client, runtime = workbench
    monkeypatch.setattr(runtime.tasks, "_schedule", lambda *_args, **_kwargs: None)
    message = runtime.memory.add_message("user", "Synthetic selected source", session_id=runtime.session_id)
    unrelated = runtime.memory.add_message("user", "Private outside source", session_id=runtime.memory.new_session())
    created = client.post("/api/tasks", json={"kind": "research", "title": "Synthetic workbench",
                                             "selection_mode": "relevant"})
    assert created.status_code == 200
    task_id = created.json()["id"]
    selected = client.get(f"/api/tasks/{task_id}/sources").json()
    assert [item["id"] for item in selected["candidates"]] == [message["id"]]
    assert client.get(f"/api/tasks/{task_id}/sources/{message['id']}").json()["text"] == message["text"]
    assert client.get(f"/api/tasks/{task_id}/sources/{unrelated['id']}").status_code == 404
    assert client.post(f"/api/tasks/{task_id}/reselect", json={"update_sources": "yes"}).status_code == 400
    reselected = client.post(f"/api/tasks/{task_id}/reselect", json={"pinned_source_ids": [message["id"]]})
    assert reselected.status_code == 200 and reselected.json()["id"] != task_id
    artifact = runtime.memory.create_artifact("document", "Synthetic report", "First version [S1]",
        {"S1": {"kind": "message", "id": message["id"], "label": "Synthetic source", "excerpt": message["text"]}}, task_id)
    base = f"/api/artifacts/{artifact['id']}"
    review = client.get(base + "/review").json()
    assert review["citations"][0]["locator"]["session_id"] == runtime.session_id
    assert not review["semantic_verification"]
    assert client.post(base + "/regenerate", json={"instructions": "Synthetic correction", "expected_version": 99}).status_code == 400
    revision = client.post(base + "/regenerate", json={"instructions": "Synthetic correction", "expected_version": 1})
    assert revision.status_code == 200
    params = runtime.memory.task(revision.json()["id"])["params"]
    assert params["revision_artifact_id"] == artifact["id"] and params["expected_version"] == 1
    assert runtime.memory.record(params["correction_source_id"])["role"] == "user"
    assert client.put(base, json={"content": "Edited version", "expected_version": 1}).status_code == 200
    assert client.put(base, json={"content": "Stale editor", "expected_version": 1}).status_code == 400
    assert client.get(base + "/review").json()["diff"]["from_version"] == 1
