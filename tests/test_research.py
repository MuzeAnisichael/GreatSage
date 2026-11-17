"""Research workflow boundaries with real Runtime, SQLite and LangGraph.

Only the bounded delegation call is a fixture: no model, devices or network.
The fixture deliberately waits across mutations/cancellation, so lifecycle
checks are exercised independently of a cooperative provider.
"""
import asyncio
import copy
import importlib
import time

import pytest

from greatsage.runtime import Runtime


class NoModels:
    async def stream_chat(self, config, messages):
        raise AssertionError("research integration tests must not call a model")
        yield  # pragma: no cover - keep the provider's async-generator interface

    async def embed(self, config, texts):
        raise AssertionError("research must not index or extract memories")

    async def close(self):
        pass


class Delegate:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = None
        self.cancelled = asyncio.Event()
        self.invalid = None
        self.inline_markers = False

    async def __call__(self, service, config, sources, counter, **kwargs):
        self.calls.append(copy.deepcopy(sources))
        self.started.set()
        if self.release is not None:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                # Deliberately model a provider which ignores cancellation.
                await self.release.wait()
        source = sources[0]
        evidence = {"source_id": source["id"], "quote": source["text"]}
        if self.invalid == "quote":
            evidence["quote"] = "This exact quote never appeared in the selected sources."
        elif self.invalid:
            evidence["source_id"] = self.invalid
        supported = {"text": "SYNTHETIC supported conclusion" + (" [S2]" if self.inline_markers else ""),
                     "evidence": [evidence]}
        return {
            "plan": ["整理选定来源", "逐项核对证据"],
            "findings": [supported,
                         {"text": "SYNTHETIC unresolved deadline", "evidence": []},
                         {"text": "SYNTHETIC unsupported forecast", "evidence": []}],
            "review": [
                {**supported, "status": "supported", "reason": "The selected source says this."},
                {"text": "SYNTHETIC unresolved deadline", "status": "uncertain",
                 "evidence": [], "reason": "The deadline has not been confirmed."},
                {"text": "SYNTHETIC unsupported forecast", "status": "unsupported",
                 "evidence": [], "reason": "No selected source supports this prediction."
                 + (" [S999]" if self.inline_markers else "")},
            ],
            "children": [{"name": "organizer", "status": "succeeded"},
                         {"name": "reviewer", "status": "succeeded"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    instance = Runtime(tmp_path, providers=NoModels())
    yield instance
    await instance.close()


@pytest.fixture
def delegate(monkeypatch):
    fixture = Delegate()
    module = importlib.import_module("greatsage.research")
    monkeypatch.setattr(module, "run_delegation", fixture)
    return fixture


async def until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        await asyncio.sleep(.01)
    raise AssertionError("research condition did not arrive")


async def settled(runtime, task_id):
    await until(lambda: runtime.memory.task(task_id)["status"]
                in ("succeeded", "failed", "cancelled", "interrupted"))
    return runtime.memory.task(task_id)


async def approval(runtime, task_id):
    await until(lambda: runtime.tasks.approvals_pending()
                or runtime.memory.task(task_id)["status"] in ("failed", "cancelled"))
    detail = runtime.memory.task(task_id)
    assert detail["status"] == "waiting_approval", detail["error"]
    return next(call for call in runtime.tasks.approvals_pending() if call["task_id"] == task_id)


def seed(runtime, tmp_path):
    source = runtime.memory.add_message(
        "user", "SYNTHETIC Orion release is planned for Friday.", "text", runtime.session_id)
    path = tmp_path / "selected.md"
    path.write_text("# Orion\nThe reviewer is SYNTHETIC Alice.\n", encoding="utf-8")
    material = runtime.memory.import_materials(str(path))["materials"][0]["id"]
    return source, material, path


async def start(runtime, material, **extra):
    return await runtime.tasks.start_research(
        {"title": "SYNTHETIC evidence report", "material_ids": [material],
         "save_path": "notes/research.md", **extra}, "console", runtime.session_id)


async def test_document_evidence_and_single_click_save_use_real_ledger(runtime, delegate, tmp_path, monkeypatch):
    source, material, _ = seed(runtime, tmp_path)
    private = runtime.memory.add_message("user", "SYNTHETIC unselected session", "text", runtime.memory.new_session())
    runtime.settings.update({"voice_approval": True, "tool_rules": [
        {"tool": "export_artifact", "match": "*", "action": "allow"}]})
    executed = []
    original = runtime.tasks.runner.run

    def counted(call_id, spec, arguments):
        executed.append(call_id)
        return original(call_id, spec, arguments)

    monkeypatch.setattr(runtime.tasks.runner, "run", counted)
    task = await start(runtime, material)
    call = await approval(runtime, task["id"])
    target = tmp_path / "workspace" / "notes" / "research.md"
    assert not target.exists() and not executed
    assert call["tool"] == "export_artifact"
    assert not call["voice"] and not call["task_scope"]
    selected = {row["id"] for row in delegate.calls[0]}
    assert source["id"] in selected and private["id"] not in selected
    artifact = runtime.memory.artifact(runtime.memory.task(task["id"])["artifacts"][0]["id"])
    assert artifact["kind"] == "document" and artifact["version"] == 1
    assert "SYNTHETIC supported conclusion" in artifact["content"]
    assert "SYNTHETIC unresolved deadline" in artifact["content"]
    assert "SYNTHETIC unsupported forecast" in artifact["content"]
    assert artifact["citations"] and all(row["valid"] for row in artifact["citations"])
    assert {row["source_id"] for row in artifact["citations"]} <= selected
    assert not artifact["unknown_markers"]
    assert call["arguments"]["version"] == artifact["version"]
    assert len(call["arguments"]["content_hash"]) == 64
    with pytest.raises(ValueError):
        await runtime.tasks.resolve(call["id"], True, "task", "click")
    with pytest.raises(ValueError):
        await runtime.tasks.resolve(call["id"], True, "once", "voice")
    await runtime.tasks.resolve(call["id"], True, "once", "click")
    detail = await settled(runtime, task["id"])
    assert detail["status"] == "succeeded", detail["error"]
    assert {row["name"] for row in detail["steps"] if row["status"] == "succeeded"} == {
        "collect", "research", "compose", "save"}
    assert "SYNTHETIC supported conclusion" in target.read_text(encoding="utf-8")
    assert executed == [call["id"]]
    assert runtime.memory.tool_call(call["id"])["approval"]["via"] == "click"
    with pytest.raises(ValueError):
        await runtime.tasks.resolve(call["id"], True, "once", "click")
    assert executed == [call["id"]], "a duplicated approval must not execute another export"
    assert len(delegate.calls) == 1


async def test_rejecting_export_preserves_reviewable_document(runtime, delegate, tmp_path):
    _, material, _ = seed(runtime, tmp_path)
    task = await start(runtime, material)
    call = await approval(runtime, task["id"])
    document_id = runtime.memory.task(task["id"])["artifacts"][0]["id"]
    await runtime.tasks.resolve(call["id"], False, "once", "click")
    detail = await settled(runtime, task["id"])
    assert detail["status"] == "succeeded", detail["error"]
    assert runtime.memory.tool_call(call["id"])["status"] == "denied"
    assert "SYNTHETIC supported conclusion" in runtime.memory.artifact(document_id)["content"]
    assert not (tmp_path / "workspace" / "notes" / "research.md").exists()


async def test_only_evidence_mappings_create_citations_not_free_text_or_quotes(runtime, delegate, tmp_path):
    source, material, _ = seed(runtime, tmp_path)
    revised = runtime.memory.revise_message(source["id"], "SYNTHETIC original text mentions [S3].")
    delegate.inline_markers = True
    task = await start(runtime, material, title="SYNTHETIC report [S123]")
    call = await approval(runtime, task["id"])
    artifact = runtime.memory.artifact(runtime.memory.task(task["id"])["artifacts"][0]["id"])
    assert {row["source_id"] for row in artifact["citations"]} == {revised["id"]}
    assert not artifact["unknown_markers"]
    for marker in ("S2", "S3", "S123", "S999"):
        assert f"[{marker}]" not in artifact["content"]
        assert f"［{marker}］" in artifact["content"]
    await runtime.tasks.resolve(call["id"], False)
    await settled(runtime, task["id"])


@pytest.mark.parametrize("invalid", ["quote", "nonexistent-source", "unselected-source"])
async def test_unverifiable_evidence_fails_without_artifact_or_export(runtime, delegate, tmp_path, invalid):
    _, material, _ = seed(runtime, tmp_path)
    if invalid == "unselected-source":
        invalid = runtime.memory.add_message("user", "SYNTHETIC excluded evidence", "text",
                                             runtime.memory.new_session())["id"]
    delegate.invalid = invalid
    task = await start(runtime, material)
    detail = await settled(runtime, task["id"])
    assert detail["status"] == "failed", detail
    assert not detail["artifacts"] and not detail["tool_calls"]
    assert not (tmp_path / "workspace" / "notes" / "research.md").exists()


@pytest.mark.parametrize("mutation", ["message_revision", "message_delete", "material_rescan", "session", "settings"])
async def test_late_delegation_cannot_cross_changed_sources_or_run_identity(runtime, delegate, tmp_path, mutation):
    source, material, path = seed(runtime, tmp_path)
    delegate.release = asyncio.Event()
    task = await start(runtime, material)
    await asyncio.wait_for(delegate.started.wait(), 3)
    if mutation == "message_revision":
        runtime.memory.revise_message(source["id"], "SYNTHETIC correction: release is Monday.")
    elif mutation == "message_delete":
        runtime.memory.delete_message(source["id"])
    elif mutation == "material_rescan":
        path.write_text("# Orion\nSYNTHETIC source has changed.\n", encoding="utf-8")
        runtime.memory.rescan_materials()
    elif mutation == "session":
        await runtime.reset_session()
    else:
        runtime.settings.update({"global_prompt": "SYNTHETIC updated instructions"})
    delegate.release.set()
    detail = await settled(runtime, task["id"])
    assert detail["status"] in ("failed", "cancelled"), detail
    assert not detail["artifacts"] and not detail["tool_calls"]
    assert not any(row["metadata"].get("task_id") == task["id"] for row in runtime.memory.history())


async def test_cancel_rejects_late_result_even_when_delegate_ignores_cancel(runtime, delegate, tmp_path):
    _, material, _ = seed(runtime, tmp_path)
    delegate.release = asyncio.Event()
    task = await start(runtime, material)
    await asyncio.wait_for(delegate.started.wait(), 3)
    cancellation = asyncio.create_task(runtime.tasks.cancel(task["id"]))
    try:
        await asyncio.wait_for(delegate.cancelled.wait(), 3)
    finally:
        delegate.release.set()
    await asyncio.wait_for(cancellation, 3)
    detail = await settled(runtime, task["id"])
    assert detail["status"] == "cancelled"
    assert not detail["artifacts"] and not detail["tool_calls"]


@pytest.mark.parametrize("mutation", ["artifact_version", "source_delete", "session"])
async def test_pending_export_cannot_save_after_approval_identity_changes(runtime, delegate, tmp_path, mutation):
    source, material, _ = seed(runtime, tmp_path)
    task = await start(runtime, material)
    call = await approval(runtime, task["id"])
    document_id = runtime.memory.task(task["id"])["artifacts"][0]["id"]
    if mutation == "artifact_version":
        artifact = runtime.memory.artifact(document_id)
        runtime.memory.add_artifact_version(document_id, artifact["content"] + "\nSYNTHETIC edited later\n")
    elif mutation == "source_delete":
        runtime.memory.delete_message(source["id"])
    else:
        await runtime.reset_session()
    try:
        await runtime.tasks.resolve(call["id"], True, "once", "click")
    except ValueError:
        # Either reject the stale click immediately or fail the guarded save.
        pass
    else:
        detail = await settled(runtime, task["id"])
        assert detail["status"] != "succeeded", detail
    assert runtime.memory.tool_call(call["id"])["status"] != "succeeded"
    assert not (tmp_path / "workspace" / "notes" / "research.md").exists()
    assert runtime.memory.artifact(document_id)["content"], "the reviewed document is retained"


async def test_restart_requires_fresh_click_and_reuses_completed_document(tmp_path, monkeypatch, delegate):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    original = Runtime(tmp_path, providers=NoModels())
    try:
        _, material, _ = seed(original, tmp_path)
        task = await start(original, material)
        old = await approval(original, task["id"])
        document_id = original.memory.task(task["id"])["artifacts"][0]["id"]
        await until(lambda: task["id"] not in original.tasks.jobs)
    finally:
        await original.close()
    resumed = Runtime(tmp_path, providers=NoModels())
    try:
        await resumed.start()
        assert resumed.memory.task(task["id"])["status"] == "interrupted"
        assert resumed.memory.tool_call(old["id"])["status"] == "expired"
        with pytest.raises(ValueError):
            await resumed.tasks.resolve(old["id"], True)
        await resumed.tasks.resume(task["id"])
        fresh = await approval(resumed, task["id"])
        assert not (tmp_path / "workspace" / "notes" / "research.md").exists()
        assert len(delegate.calls) == 1, "completed source review must not run again on approval recovery"
        assert [item["id"] for item in resumed.memory.task(task["id"])["artifacts"]] == [document_id]
        assert resumed.memory.artifact(document_id)["version"] == 1
        await resumed.tasks.resolve(fresh["id"], True, "once", "click")
        detail = await settled(resumed, task["id"])
        assert detail["status"] == "succeeded", detail["error"]
        assert (tmp_path / "workspace" / "notes" / "research.md").exists()
        assert len([call for call in detail["tool_calls"] if call["status"] == "succeeded"]) == 1
    finally:
        await resumed.close()


async def test_export_rechecks_artifact_after_click_while_execution_is_queued(runtime, delegate, tmp_path):
    _, material, _ = seed(runtime, tmp_path)
    task = await start(runtime, material)
    call = await approval(runtime, task["id"])
    await until(lambda: task["id"] not in runtime.tasks.jobs)
    artifact = runtime.memory.artifact(runtime.memory.task(task["id"])["artifacts"][0]["id"])
    await runtime.tasks.slot.acquire()
    try:
        await runtime.tasks.resolve(call["id"], True, "once", "click")
        runtime.memory.add_artifact_version(artifact["id"], artifact["content"] + "\nSYNTHETIC queued edit\n")
    finally:
        runtime.tasks.slot.release()
    detail = await settled(runtime, task["id"])
    assert detail["status"] == "failed", detail
    assert runtime.memory.tool_call(call["id"])["status"] != "succeeded"
    assert not (tmp_path / "workspace" / "notes" / "research.md").exists()


async def test_resume_preserves_accounted_calls_and_cumulative_budget(runtime, delegate, tmp_path, monkeypatch):
    _, material, _ = seed(runtime, tmp_path)
    attempts = 0

    async def measured(service, config, sources, counter, **callbacks):
        nonlocal attempts
        attempts += 1
        role = "source_researcher"
        snapshot = await callbacks["before_request"](
            role, [{"role": "user", "content": "SYNTHETIC measured request"}],
            {**config, "max_tokens": 64, "tools": []}, [row["id"] for row in sources])
        await callbacks["on_usage"](role, {"prompt_tokens": attempts * 10, "completion_tokens": 3})
        await callbacks["on_request_end"](snapshot, "completed")
        if attempts == 1:
            raise ValueError("SYNTHETIC failure after an accounted request")
        return await delegate(service, config, sources, counter, **callbacks)

    monkeypatch.setattr(importlib.import_module("greatsage.research"), "run_delegation", measured)
    task = await start(runtime, material)
    failed = await settled(runtime, task["id"])
    assert failed["status"] == "failed"
    first_budget = dict(failed["state"]["research_budget"])
    assert first_budget["calls"] == 1 and first_budget["reserved_tokens"] > 0
    assert len(runtime.memory.step(task["id"], "research")["detail"]["calls"]) == 1
    await runtime.tasks.resume(task["id"])
    call = await approval(runtime, task["id"])
    detail = runtime.memory.step(task["id"], "research")["detail"]
    assert [entry["usage"]["prompt_tokens"] for entry in detail["calls"]] == [10, 20]
    assert all(entry["status"] == "completed" for entry in detail["calls"])
    budget = runtime.memory.task(task["id"])["state"]["research_budget"]
    assert budget["calls"] == 2
    assert budget["reserved_tokens"] == first_budget["reserved_tokens"] * 2
    assert budget["active_seconds"] >= first_budget["active_seconds"]
    await runtime.tasks.resolve(call["id"], False)
    await settled(runtime, task["id"])


@pytest.mark.parametrize("completed", [False, True])
async def test_material_rescan_clears_research_copies_even_after_completion(
        runtime, delegate, tmp_path, monkeypatch, completed):
    _, material, path = seed(runtime, tmp_path)

    async def source_quoting_plan(service, config, sources, counter, **callbacks):
        report = await delegate(service, config, sources, counter, **callbacks)
        report["plan"] = [sources[0]["text"]]
        return report

    monkeypatch.setattr(importlib.import_module("greatsage.research"), "run_delegation", source_quoting_plan)
    task = await start(runtime, material, use_session=False)
    call = await approval(runtime, task["id"])
    artifact_id = runtime.memory.task(task["id"])["artifacts"][0]["id"]
    old_text = delegate.calls[0][0]["text"]
    assert old_text in runtime.memory.step(task["id"], "research")["detail"]["plan"]
    if completed:
        await runtime.tasks.resolve(call["id"], False)
        assert (await settled(runtime, task["id"]))["status"] == "succeeded"
    else:
        report = runtime.memory.step(task["id"], "research")["result"]
        assert report["review"][0]["evidence"][0]["quote"] == old_text
    path.write_text("# Orion\nSYNTHETIC replacement without the old reviewer.\n", encoding="utf-8")
    runtime.memory.rescan_materials()
    step = runtime.memory.step(task["id"], "research")
    assert step["result"] is None
    assert runtime.memory.step(task["id"], "collect")["result"] is None
    assert not {"plan", "uncertainties", "review_summary"}.intersection(step["detail"])
    assert old_text not in repr(step)
    artifact = runtime.memory.artifact(artifact_id)
    assert artifact["content"] and artifact["status"] == "stale"
    assert all(not cite["valid"] and cite["excerpt"] is None for cite in artifact["citations"])
    if not completed:
        await runtime.tasks.cancel(task["id"])
    runtime.memory.delete_task(task["id"])
    assert not runtime.memory._db.execute(
        "SELECT 1 FROM research_sources WHERE task_id=?", (task["id"],)).fetchone()


async def test_worker_entry_rechecks_identity_after_thread_pool_queuing(runtime, delegate, tmp_path, monkeypatch):
    _, material, _ = seed(runtime, tmp_path)
    task = await start(runtime, material)
    call = await approval(runtime, task["id"])
    queued, release = asyncio.Event(), asyncio.Event()
    real_to_thread = asyncio.to_thread
    executed = []
    original_run = runtime.tasks.runner.run

    async def delayed_worker(function, *args, **kwargs):
        if getattr(function, "__name__", "") == "execute_local":
            queued.set()
            await release.wait()
        return await real_to_thread(function, *args, **kwargs)

    def counted(*args):
        executed.append(args[0])
        return original_run(*args)

    monkeypatch.setattr(asyncio, "to_thread", delayed_worker)
    monkeypatch.setattr(runtime.tasks.runner, "run", counted)
    await runtime.tasks.resolve(call["id"], True)
    try:
        await asyncio.wait_for(queued.wait(), 3)
        await runtime.reset_session()
    finally:
        release.set()
    detail = await settled(runtime, task["id"])
    assert detail["status"] == "failed", detail
    assert runtime.memory.tool_call(call["id"])["status"] == "failed"
    assert not executed, "a stale queued worker must not enter the filesystem tool"
    assert not (tmp_path / "workspace" / "notes" / "research.md").exists()
