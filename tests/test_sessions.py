"""Persistent conversation entry points and late-result isolation, without I/O models."""
import asyncio
import contextlib
import copy
import json
import time

import pytest

from greatsage.audio import AudioChunk
from greatsage.memory import MemoryStore
from greatsage.runtime import Runtime


class OfflineProviders:
    def __init__(self):
        self.calls = []

    async def stream_chat(self, config, messages):
        self.calls.append(copy.deepcopy(messages))
        yield {"text": "Synthetic response."}

    async def close(self):
        pass


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    instance = Runtime(tmp_path, providers=OfflineProviders())
    instance.settings.update({"voice_enabled": False, "semantic_decisions": False,
                              "memory_extraction_enabled": False, "embedding": {"enabled": False}})
    # Keep these Runtime isolation tests independent of task orchestration;
    # HTTP and task lifecycle tests exercise the real suspension implementation.
    instance.suspensions = []

    async def suspend():
        instance.suspensions.append(instance.session_id)

    monkeypatch.setattr(instance.tasks, "suspend_for_session", suspend, raising=False)
    monkeypatch.setattr(instance, "_schedule_compression", lambda: None)
    yield instance
    await instance.close()


def test_pointer_migrates_and_persists_without_copying_history(tmp_path):
    store = MemoryStore(tmp_path)
    first = store.new_session()
    store.add_message("user", "SYNTHETIC original body", session_id=first)
    recent = store.new_session()
    assert store.active_session() == recent
    store.activate_session(first)
    assert [item["id"] for item in store.sessions() if item["active"]] == [first]
    assert "SYNTHETIC original body" not in json.dumps(store.sessions())
    assert set(store.session(first)) == {"id", "created_at", "message_count", "last_message_at", "active"}
    with pytest.raises(ValueError, match="会话不存在"):
        store.activate_session("missing")
    store.close()
    reopened = MemoryStore(tmp_path)
    assert reopened.active_session() == first
    assert len(reopened.sessions()) == 2 and len(reopened.history()) == 1
    reopened.clear_history()
    assert reopened.sessions() == []
    fresh = reopened.active_session()
    assert fresh not in {first, recent} and reopened.session(fresh)["active"]
    assert not reopened.history()
    reopened.close()


async def test_continue_selected_history_and_create_new_session(runtime):
    first = runtime.session_id
    original = runtime.memory.add_message("user", "SYNTHETIC yesterday topic", session_id=first)
    second = (await runtime.reset_session())["session_id"]
    assert second != first and runtime.memory.active_session() == second
    await runtime.activate_session(first)
    await runtime.chat("Continue yesterday's topic.")
    await runtime.reply_task
    assert original["text"] in json.dumps(runtime.providers.calls[-1])
    assert [row["role"] for row in runtime.memory.history(session_id=first)] == ["user", "user", "assistant"]
    assert not runtime.memory.history(session_id=second)
    assert runtime.suspensions == [first, second]
    assert runtime.session_state()["active_session_id"] == first
    assert runtime.session_state()["sessions"][0]["active"]


async def test_unknown_session_has_no_effect_and_switches_are_serial(runtime, monkeypatch):
    old = runtime.session_id
    before = (runtime.epoch, runtime.data_epoch, runtime.generation, runtime.memory_extraction_epoch)
    with pytest.raises(ValueError, match="会话不存在"):
        await runtime.activate_session("missing")
    assert before == (runtime.epoch, runtime.data_epoch, runtime.generation, runtime.memory_extraction_epoch)
    assert not runtime.suspensions and runtime.memory.active_session() == old
    left, right = runtime.memory.new_session(), runtime.memory.new_session()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def suspend():
        calls.append(runtime.session_id)
        if len(calls) == 1:
            started.set()
            await release.wait()

    monkeypatch.setattr(runtime.tasks, "suspend_for_session", suspend)
    first = asyncio.create_task(runtime.activate_session(left))
    await started.wait()
    second = asyncio.create_task(runtime.activate_session(right))
    await asyncio.sleep(0)
    assert calls == [old] and runtime.session_id == old
    release.set()
    assert await first == {"session_id": left}
    assert await second == {"session_id": right}
    assert calls == [old, left] and runtime.memory.active_session() == right


async def test_late_answer_cannot_save_emit_or_start_tools_after_switch(runtime, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    tool_starts = []

    async def late(config, messages):
        yield {"text": "SYNTHETIC old partial", "tool_calls": [{"id": "old-call", "name": "read_file", "arguments": "{}"}]}
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # Simulate a transport that finishes despite cancellation.

    async def start_action(*args):
        tool_starts.append(args)

    monkeypatch.setattr(runtime.providers, "stream_chat", late)
    monkeypatch.setattr(runtime.tasks, "start_action", start_action)
    events = asyncio.Queue()
    runtime.subscribers.add(events)
    await runtime.chat("Old request")
    previous = runtime.reply_task
    await started.wait()
    target = runtime.memory.new_session()
    await asyncio.wait_for(runtime.activate_session(target), 2)
    assert not previous.done() and runtime.session_id == target
    while not events.empty():
        events.get_nowait()
    runtime.state = "new_session_state"
    release.set()
    await previous
    assert not tool_starts and runtime.state == "new_session_state"
    assert not runtime.memory.history(session_id=target)
    assert not [row for row in runtime.memory.history() if row["role"] == "assistant"]
    assert not [event for event in list(events._queue) if event["kind"] in {"response_done", "response_delta", "state"}]


async def test_late_transcript_and_audio_queue_are_discarded(runtime, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def transcribe(config, pcm):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"text": "SYNTHETIC stale speech", "usage": {}}

    monkeypatch.setattr(runtime.providers, "transcribe", transcribe, raising=False)
    runtime.listening = True
    old_epoch = runtime.epoch
    task = asyncio.create_task(runtime._transcribe("microphone:fixture", b"\0" * 960, True, time.time(), 0))
    runtime.final_tasks.add(task)
    await started.wait()
    runtime._enqueue(AudioChunk("microphone:fixture", b"\0" * 960), epoch=old_epoch)
    await runtime.reset_session()
    assert runtime.audio_queue.empty()
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    runtime._enqueue(AudioChunk("microphone:fixture", b"\0" * 960), epoch=old_epoch)
    assert runtime.audio_queue.empty() and not runtime.memory.history() and not runtime.providers.calls
    runtime.listening = False  # No real capture device was opened by this fixture.


async def test_restart_restores_entry_only_until_explicit_interaction(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    first = Runtime(tmp_path, providers=OfflineProviders())
    session_id = first.session_id
    first.memory.add_message("user", "SYNTHETIC restored history", session_id=session_id)
    task_id = first.memory.create_task("minutes", "Unfinished synthetic task", "console", session_id, "trace", {}, [])
    first.memory.update_task(task_id, status="running")
    await first.close()
    restored = Runtime(tmp_path, providers=OfflineProviders())
    schedules = []
    monkeypatch.setattr(restored, "_schedule_compression", lambda: schedules.append(True))
    try:
        await restored.start()
        await asyncio.sleep(0)
        assert restored.session_id == session_id and len(restored.memory.sessions()) == 1
        assert restored.memory.task(task_id)["status"] == "interrupted"
        assert not restored.tasks.jobs and not restored.listening and not restored.providers.calls
        assert not schedules and not restored._background_enabled
        await restored.activate_session(session_id)
        assert restored._background_enabled
    finally:
        await restored.close()
