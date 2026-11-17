"""Run identities fence late task results across cancellation and session changes."""
import asyncio

import pytest

from greatsage import tasks
from greatsage.runtime import Runtime
from test_tasks import Scripted, say, status, until


class Stubborn(Scripted):
    def __init__(self):
        super().__init__()
        self.started, self.cancelled, self.release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stream_chat(self, config, messages):
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
        async for item in super().stream_chat(config, messages):
            yield item


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setattr(tasks, "CANCEL_WAIT_SECONDS", .03)
    instance = Runtime(tmp_path, providers=Stubborn())
    yield instance
    instance.providers.release.set()
    if instance.tasks.jobs:
        await asyncio.wait(instance.tasks.jobs.values(), timeout=2)
    await instance.close()


def bare_task(runtime, name="extract"):
    id = runtime.memory.create_task("minutes", "SYNTHETIC lifecycle", "console", runtime.session_id,
                                    "fixture-task", {}, [(name, name)])
    return runtime.memory.task(id)


@pytest.mark.parametrize("operation", ["switch", "cancel", "close"])
async def test_stubborn_model_is_detached_without_losing_job_or_overwriting_status(runtime, operation):
    say(runtime, "新版界面下周二内部试用。")
    task = await runtime.tasks.start_minutes({}, "console", runtime.session_id)
    await asyncio.wait_for(runtime.providers.started.wait(), 3)
    id = task["id"]
    previous = runtime.tasks.jobs[id]
    if operation == "switch":
        old_session = runtime.session_id
        await asyncio.wait_for(runtime.reset_session(), .8)
        assert runtime.session_id != old_session
    elif operation == "close":
        await asyncio.wait_for(runtime.tasks.close(), .8)
    else:
        await asyncio.wait_for(runtime.tasks.cancel(id), .8)
    expected = "cancelled" if operation == "cancel" else "interrupted"
    assert runtime.memory.task(id)["status"] == expected
    assert id not in runtime.tasks.run_tokens
    assert runtime.tasks.jobs.get(id) is previous and not previous.done()
    with pytest.raises(ValueError, match="退出|关闭"):
        await runtime.tasks.resume(id)
    runtime.providers.release.set()
    await until(lambda: id not in runtime.tasks.jobs)
    assert runtime.memory.task(id)["status"] == expected
    assert not runtime.memory.task(id)["artifacts"]
    assert runtime.memory.step(id, "extract")["result"] is None
    assert not any(row["role"] == "assistant" for row in runtime.memory.history())
    if operation == "switch":
        # Explicit continuation binds a fresh run to the current session, while
        # retaining the original task's source selection and completed steps.
        await runtime.tasks.resume(id)
        await until(status(runtime, id, "succeeded", "failed"))
        assert runtime.memory.task(id)["status"] == "succeeded"


async def test_switch_synchronously_revokes_queued_and_waiting_tasks_and_running_calls(runtime):
    active, queued = bare_task(runtime), bare_task(runtime)
    runtime.memory.update_task(active["id"], status="waiting_approval")
    call = runtime.memory.add_tool_call(active["id"], "approval", "write_file", {}, "write", "x.md",
                                       "ask", "fixture", "awaiting_approval")
    running = runtime.memory.add_tool_call(queued["id"], "running", "write_file", {}, "write", "y.md",
                                          "allow", "fixture", "running")
    runtime.tasks.approvals.add(call["id"])
    runtime.tasks.grants[active["id"]] = {"write_file"}
    runtime.tasks.run_tokens[queued["id"]] = object()
    await runtime.tasks.suspend_for_session()
    assert not runtime.tasks.approvals and not runtime.tasks.grants and not runtime.tasks.run_tokens
    assert all(runtime.memory.task(row["id"])["status"] == "interrupted" for row in (active, queued))
    assert runtime.memory.tool_call(call["id"])["status"] == "expired"
    assert runtime.memory.tool_call(running["id"])["status"] == "unknown"
    with pytest.raises(ValueError):
        await runtime.tasks.resolve(call["id"], True)


@pytest.mark.parametrize("mutation", ["settings", "data_epoch"])
@pytest.mark.parametrize("boundary", ["step", "model"])
async def test_direct_await_boundaries_reject_changed_identity(runtime, mutation, boundary):
    task, started, release = bare_task(runtime), asyncio.Event(), asyncio.Event()

    async def work():
        started.set()
        await release.wait()
        return {"late": "must not persist"}

    if boundary == "step":
        job = asyncio.create_task(runtime.tasks._step(task["id"], "extract", work))
        await started.wait()
    else:
        job = asyncio.create_task(runtime.tasks._model_once(task, "extract", "fixture", [{"role": "user", "content": "fixture"}],
                                                           runtime._provider("llm"), [], 1))
        await runtime.providers.started.wait()
    if mutation == "settings":
        runtime.settings.update({"global_prompt": "SYNTHETIC changed instructions"})
    else:
        runtime.data_epoch += 1
    release.set()
    runtime.providers.release.set()
    with pytest.raises(tasks.TaskError, match="运行状态"):
        await job
    step = runtime.memory.step(task["id"], "extract")
    assert step["result"] is None and not step["detail"].get("calls")


async def test_worker_queue_rechecks_identity_before_any_local_tool_effect(runtime, monkeypatch):
    task = bare_task(runtime)
    entered, release, effects = asyncio.Event(), asyncio.Event(), []
    original_to_thread = asyncio.to_thread

    async def queued_thread(function, *args, **kwargs):
        if function.__name__ != "execute_local":
            return await original_to_thread(function, *args, **kwargs)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return await original_to_thread(function, *args, **kwargs)

    async def graph(task, approval=None):
        await runtime.tasks._call(task, "fixture", {"name": "list_files", "arguments": "{}"})
        return False

    monkeypatch.setattr(asyncio, "to_thread", queued_thread)
    monkeypatch.setattr(runtime.tasks.graphs, "run", graph)
    monkeypatch.setattr(runtime.tasks.runner, "run", lambda *args: (effects.append(args) or {}, None))
    runtime.tasks._schedule(task["id"])
    await entered.wait()
    await runtime.tasks.suspend_for_session()
    release.set()
    await until(lambda: task["id"] not in runtime.tasks.jobs)
    assert not effects
    assert runtime.memory.task(task["id"])["status"] == "interrupted"
    assert runtime.memory.tool_call_by_key(task["id"], "fixture")["status"] == "unknown"


async def test_late_action_model_cannot_propose_tools_or_reply_to_old_or_new_session(runtime, tmp_path):
    message = say(runtime, "读取测试文件后继续操作")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "input.md").write_text("SYNTHETIC", encoding="utf-8")
    runtime.providers.turns = [[{"text": "late answer"}, {"tool_calls": [
        {"id": "late", "name": "write_file", "arguments": '{"path":"late.md","content":"late"}'}]}]]
    task = await runtime.tasks.start_action(message, [{"role": "user", "content": message["text"]}], "", [
        {"id": "read", "name": "read_file", "arguments": '{"path":"input.md"}'}], "fixture-action")
    await asyncio.wait_for(runtime.providers.started.wait(), 3)
    await runtime.reset_session()
    runtime.providers.release.set()
    await until(lambda: task["id"] not in runtime.tasks.jobs)
    detail = runtime.memory.task(task["id"])
    assert detail["status"] == "interrupted"
    assert [call["tool"] for call in detail["tool_calls"]] == ["read_file"]
    assert not runtime.tasks.approvals_pending() and not (workspace / "late.md").exists()
    assert not any(row["role"] == "assistant" for row in runtime.memory.history())


async def test_old_run_finally_cannot_clear_replacement_token_or_finish_replacement_state(runtime, monkeypatch):
    task, started, release = bare_task(runtime), asyncio.Event(), asyncio.Event()

    async def graph(task, approval=None):
        started.set()
        await release.wait()
        return False

    monkeypatch.setattr(runtime.tasks.graphs, "run", graph)
    runtime.tasks._schedule(task["id"])
    await started.wait()
    newer = runtime.tasks.run_tokens[task["id"]] = object()
    runtime.memory.update_task(task["id"], status="queued")
    release.set()
    await until(lambda: task["id"] not in runtime.tasks.jobs)
    assert runtime.tasks.run_tokens[task["id"]] is newer
    assert runtime.memory.task(task["id"])["status"] == "queued"


@pytest.mark.parametrize("boundary", ["checkpoint_delete", "changed"])
@pytest.mark.parametrize("mutation", ["session", "settings"])
async def test_resume_control_rechecks_identity_after_each_await(runtime, monkeypatch, boundary, mutation):
    task, started, release = bare_task(runtime), asyncio.Event(), asyncio.Event()
    runtime.memory.finish_task(task["id"], "interrupted", None)
    runtime.memory.add_tool_call(task["id"], "expired", "write_file", {}, "write", "x.md",
                                 "ask", "fixture", "expired")
    original_changed = runtime.tasks.changed
    paused = False

    async def checkpoint_delete(id):
        if boundary == "checkpoint_delete":
            started.set()
            await release.wait()

    async def changed(id):
        nonlocal paused
        if boundary == "changed" and not paused:
            paused = True
            started.set()
            await release.wait()
        await original_changed(id)

    monkeypatch.setattr(runtime.tasks.graphs, "delete", checkpoint_delete)
    monkeypatch.setattr(runtime.tasks, "changed", changed)
    request = asyncio.create_task(runtime.tasks.resume(task["id"]))
    await started.wait()
    if mutation == "session":
        await runtime.reset_session()
    else:
        runtime.settings.update({"global_prompt": "SYNTHETIC different instructions"})
    release.set()
    with pytest.raises(ValueError, match="运行状态"):
        await request
    assert task["id"] not in runtime.tasks.jobs
    assert runtime.memory.task(task["id"])["status"] == "interrupted"


@pytest.mark.parametrize("boundary", ["checkpoint_inspect", "approval_event"])
@pytest.mark.parametrize("mutation", ["session", "settings"])
async def test_approval_control_does_not_requeue_or_keep_grants_after_identity_change(runtime, monkeypatch, boundary, mutation):
    task, started, release = bare_task(runtime), asyncio.Event(), asyncio.Event()
    runtime.memory.update_task(task["id"], status="waiting_approval")
    call = runtime.memory.add_tool_call(task["id"], "approval", "write_file", {"path": "x.md", "content": "fixture"},
                                       "write", "x.md", "ask", "fixture", "awaiting_approval")
    runtime.tasks.approvals.add(call["id"])
    original_emit = runtime.emit

    async def inspect(task):
        if boundary == "checkpoint_inspect":
            started.set()
            await release.wait()
        return {"pending_calls": [call["id"]]}

    async def emit(kind, *args, **kwargs):
        if kind == "approval_resolved" and boundary == "approval_event":
            started.set()
            await release.wait()
        return await original_emit(kind, *args, **kwargs)

    monkeypatch.setattr(runtime.tasks.graphs, "inspect", inspect)
    monkeypatch.setattr(runtime, "emit", emit)
    request = asyncio.create_task(runtime.tasks.resolve(call["id"], True, "task"))
    await started.wait()
    if mutation == "session":
        await runtime.reset_session()
    else:
        runtime.settings.update({"global_prompt": "SYNTHETIC different instructions"})
    release.set()
    with pytest.raises(ValueError, match="运行状态"):
        await request
    assert task["id"] not in runtime.tasks.jobs
    assert runtime.memory.task(task["id"])["status"] == "interrupted"
    assert runtime.memory.tool_call(call["id"])["status"] == "expired"
    assert not runtime.tasks.approvals and not runtime.tasks.grants


@pytest.mark.parametrize("exception", [ValueError("SYNTHETIC late error"), OSError("SYNTHETIC late OS error")])
async def test_late_tool_exception_preserves_unknown_ledger(runtime, monkeypatch, exception):
    task, started, release = bare_task(runtime), asyncio.Event(), asyncio.Event()

    async def delayed_thread(function, *args, **kwargs):
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        raise exception

    async def graph(task, approval=None):
        await runtime.tasks._call(task, "late", {"name": "list_files", "arguments": "{}"})
        return False

    monkeypatch.setattr(asyncio, "to_thread", delayed_thread)
    monkeypatch.setattr(runtime.tasks.graphs, "run", graph)
    runtime.tasks._schedule(task["id"])
    await started.wait()
    await runtime.tasks.cancel(task["id"])
    before = runtime.memory.tool_call_by_key(task["id"], "late")
    assert before["status"] == "unknown"
    release.set()
    await until(lambda: task["id"] not in runtime.tasks.jobs)
    after = runtime.memory.tool_call_by_key(task["id"], "late")
    assert (after["status"], after["error"]) == (before["status"], before["error"])
    assert runtime.memory.task(task["id"])["status"] == "cancelled"


@pytest.mark.parametrize("boundary", ["model", "terminal_event"])
async def test_runtime_database_close_precedes_late_job_without_further_database_access(tmp_path, monkeypatch, boundary):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setattr(tasks, "CANCEL_WAIT_SECONDS", .03)
    instance = Runtime(tmp_path, providers=Stubborn())
    started, release = asyncio.Event(), asyncio.Event()
    closed, late_access, jobs = False, [], []
    try:
        if boundary == "model":
            say(instance, "新版界面下周二内部试用。")
            task = await instance.tasks.start_minutes({}, "console", instance.session_id)
            await asyncio.wait_for(instance.providers.started.wait(), 3)
        else:
            task = bare_task(instance)
            original_changed = instance.tasks.changed
            count = 0

            async def changed(id):
                nonlocal count
                count += 1
                if count == 2:
                    started.set()
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            pass
                else:
                    await original_changed(id)

            async def graph(task, approval=None):
                return False

            monkeypatch.setattr(instance.tasks, "changed", changed)
            monkeypatch.setattr(instance.tasks.graphs, "run", graph)
            instance.tasks._schedule(task["id"])
            await started.wait()
        jobs = list(instance.tasks.jobs.values())
        for name in ("task", "finish_snapshot", "update_task", "update_step", "update_tool_call", "add_event", "add_message"):
            original = getattr(instance.memory, name)

            def tracked(*args, _name=name, _original=original, **kwargs):
                if closed:
                    late_access.append(_name)
                return _original(*args, **kwargs)

            monkeypatch.setattr(instance.memory, name, tracked)
        await asyncio.wait_for(instance.close(), .8)
        closed = True
        instance.providers.release.set()
        release.set()
        await asyncio.wait_for(asyncio.gather(*jobs), 3)
        await asyncio.wait_for(instance.tasks._graph_cleanup, 3)
        assert not late_access
        assert not instance.tasks.jobs
    finally:
        instance.providers.release.set()
        release.set()
        if jobs:
            await asyncio.wait(jobs, timeout=2)
        if not closed:
            await instance.close()
