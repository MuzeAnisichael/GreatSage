"""Single-batch extraction, metadata audit and in-flight invalidation boundaries."""
import asyncio
import json
import time

import pytest

from greatsage.runtime import Runtime
from greatsage.providers import ProviderError
from test_memory_extraction import ProposalProvider


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    instance = Runtime(tmp_path, providers=ProposalProvider())
    instance.settings.update({"memory_extraction_enabled": True, "memory_confirmation": "mostly"})
    yield instance
    await instance.close()


def add_source(runtime, **kwargs):
    return runtime.memory.add_message("user", "我喜欢中文回答。", "text", runtime.session_id,
                                      "extraction-fixture", **kwargs)


async def test_single_batch_uses_only_user_originals_and_records_metadata(runtime):
    original = add_source(runtime, metadata={"unexpected": "PRIVATE_METADATA_MUST_STAY_LOCAL"})
    runtime.memory.add_message("assistant", "ASSISTANT_MUST_STAY_LOCAL", "assistant", runtime.session_id)
    runtime.memory.add_message("observation", "DESKTOP_MUST_STAY_LOCAL", "system", runtime.session_id)
    result = await runtime.extract_memories()
    assert result["saved"] == result["pending"] == 1
    assert result["processed_sources"] == 1
    candidate = runtime.memory.memory_candidates_list()[0]
    assert candidate["source_ids"] == [original["id"]]
    assert candidate["origin"] == "extracted"
    assert len(runtime.providers.calls) == 1
    sent = json.dumps(runtime.providers.calls, ensure_ascii=False)
    assert "MUST_STAY_LOCAL" not in sent
    snapshot = runtime.memory.snapshots()[0]
    assert snapshot["purpose"] == "memory_extraction"
    assert snapshot["metadata"]["outcome"] == "completed"
    state = runtime.memory_extraction_status()
    assert state["phase"] == "completed" and state["pending"] == 1
    events = [event for event in runtime.memory.events() if event["kind"] in ("memory_extraction", "memory_updated")]
    assert "中文" not in json.dumps(events, ensure_ascii=False)
    await runtime.extract_memories()
    assert len(runtime.providers.calls) == 1


@pytest.mark.parametrize("change", ["settings", "epoch", "session", "generation", "delete", "data"])
async def test_late_extraction_cannot_commit_after_identity_or_source_change(runtime, change):
    source = add_source(runtime)
    runtime.providers.release = asyncio.Event()
    task = asyncio.create_task(runtime.extract_memories())
    await runtime.providers.started.wait()
    if change == "settings":
        runtime.settings.update({"memory_confirmation": "auto"})
    elif change == "epoch":
        runtime.epoch += 1
    elif change == "session":
        runtime.session_id = runtime.memory.new_session()
    elif change == "generation":
        runtime.generation += 1
    elif change == "data":
        runtime.data_epoch += 1
    else:
        runtime.memory.delete_message(source["id"])
    runtime.providers.release.set()
    await task
    assert not runtime.memory.memory_candidates_list()
    assert not runtime.memory.list_memories()
    assert runtime.memory_extraction_status()["phase"] == "discarded"
    if change != "delete":
        assert len(runtime.memory.extraction_sources()) == 1


async def test_framework_propagates_cancel_even_if_provider_swallows_cancel(runtime):
    add_source(runtime)

    async def ignores_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    runtime.providers.on_call = ignores_cancel
    task = asyncio.create_task(runtime.extract_memories())
    await runtime.providers.started.wait()
    await runtime.pause_background()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.memory_extraction_status()["phase"] == "cancelled"
    assert not runtime.memory.memory_candidates_list()
    assert len(runtime.memory.extraction_sources()) == 1


async def test_runtime_discards_result_even_if_entire_extractor_swallows_cancel(runtime, monkeypatch):
    from test_memory_extraction import proposal

    original = add_source(runtime)
    started = asyncio.Event()

    async def ignores_cancel(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return [proposal(original["id"])]

    monkeypatch.setattr("greatsage.memory_extraction.extract_memory_proposals", ignores_cancel)
    task = asyncio.create_task(runtime.extract_memories())
    await started.wait()
    await runtime.pause_background()
    await task
    assert runtime.memory_extraction_status()["phase"] == "discarded"
    assert not runtime.memory.memory_candidates_list()


async def test_manual_scope_survives_backoff_without_processing_older_session(runtime):
    old = runtime.session_id
    add_source(runtime)
    runtime.session_id = runtime.memory.new_session()
    add_source(runtime)
    await runtime.request_memory_extraction(session_id=runtime.session_id)
    runtime.background.failure("memory_extraction")
    await runtime.compression_task
    assert runtime.memory_extraction_status()["scheduled"]
    assert not runtime.providers.calls
    runtime.background.retry()
    await runtime._background_cycle()
    assert len(runtime.memory.extraction_sources(session_id=old)) == 1
    assert not runtime.memory.extraction_sources(session_id=runtime.session_id)
    assert not runtime.memory_extraction_status()["scheduled"]


async def test_request_is_nonblocking_scoped_and_disabled_requests_make_no_calls(runtime):
    old = runtime.session_id
    add_source(runtime)
    runtime.session_id = runtime.memory.new_session()
    add_source(runtime)
    runtime.providers.release = asyncio.Event()
    status = await runtime.request_memory_extraction(session_id=runtime.session_id)
    assert status["scheduled"] and status["phase"] == "queued"
    await runtime.providers.started.wait()
    runtime.providers.release.set()
    await runtime.compression_task
    assert len(runtime.memory.extraction_sources(session_id=old)) == 1
    assert not runtime.memory.extraction_sources(session_id=runtime.session_id)
    runtime.settings.update({"memory_extraction_enabled": False})
    with pytest.raises(ValueError, match="启用"):
        await runtime.request_memory_extraction()
    await runtime.extract_memories()
    assert len(runtime.providers.calls) == 1


async def test_foreground_busy_defers_and_provider_failure_has_backoff_and_safe_error(runtime):
    add_source(runtime)
    runtime.reply_task = asyncio.create_task(asyncio.Event().wait())
    await runtime.extract_memories()
    assert not runtime.providers.calls
    await runtime.interrupt()
    runtime.providers.failure = RuntimeError("SECRET_RESPONSE_MUST_NOT_APPEAR")
    await runtime.extract_memories()
    state = runtime.memory_extraction_status()
    assert state["phase"] == "failed" and state["error"] == "extraction_failed"
    assert state["background"]["retry_in_seconds"] > 0
    await runtime.extract_memories()
    assert len(runtime.providers.calls) == 1
    assert "SECRET_RESPONSE" not in json.dumps(runtime.memory.events())
    assert len(runtime.memory.extraction_sources()) == 1


async def test_pending_sources_remain_discoverable_after_restart(runtime):
    add_source(runtime)
    await runtime.pause_background()
    restarted = Runtime(runtime.data_dir, providers=ProposalProvider())
    try:
        assert restarted.session_id != runtime.session_id
        await restarted.extract_memories()
        assert restarted.memory.extraction_status()["processed_sources"] == 1
        assert len(restarted.providers.calls) == 1
    finally:
        await restarted.close()


async def test_oversized_first_batch_is_deferred_so_ninth_source_can_progress(runtime):
    for _ in range(8):
        runtime.memory.add_message("user", "很长的原文" * 5000, "text", runtime.session_id)
    add_source(runtime)
    await runtime.extract_memories()
    assert runtime.memory_extraction_status()["deferred_sources"] == 8
    assert runtime.memory_extraction_status()["processed_sources"] == 0
    assert not runtime.providers.calls
    await runtime.extract_memories()
    assert len(runtime.providers.calls) == 1
    state = runtime.memory_extraction_status()
    assert state["processed_sources"] == 1 and state["deferred_sources"] == 8
    assert state["pending_sources"] == 8
    await runtime.extract_memories()
    assert len(runtime.providers.calls) == 1


async def test_uncooperative_cancel_cannot_block_foreground_or_spawn_replacement(runtime, monkeypatch):
    from test_memory_extraction import proposal

    original = add_source(runtime)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def uncooperative(*args, **kwargs):
        calls.append(True)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return [proposal(original["id"])]

    monkeypatch.setattr("greatsage.memory_extraction.extract_memory_proposals", uncooperative)
    await runtime.request_memory_extraction()
    task = runtime.compression_task
    await started.wait()
    began = time.monotonic()
    try:
        await runtime.interrupt()
        assert time.monotonic() - began < 1
        assert not task.done()
        runtime._schedule_compression()
        assert runtime.compression_task is task
        await runtime.extract_memories()
        assert len(calls) == 1
    finally:
        release.set()
        await task
    assert not runtime.memory.memory_candidates_list()
    assert runtime.memory_extraction_status()["phase"] == "discarded"


async def test_deterministic_failure_stops_across_restart_until_configuration_changes(runtime):
    add_source(runtime)
    runtime.providers.failure = ProviderError("fixture", "invalid_request")
    await runtime.extract_memories()
    assert runtime.memory_extraction_status()["phase"] == "needs_attention"
    await runtime.extract_memories()
    assert len(runtime.providers.calls) == 1
    restarted = Runtime(runtime.data_dir, providers=ProposalProvider())
    try:
        await restarted.extract_memories()
        assert not restarted.providers.calls
        assert restarted.memory_extraction_status()["needs_attention"]
        restarted.settings.update({"llm": {"max_tokens": 769}})
        await restarted.extract_memories()
        assert len(restarted.providers.calls) == 1
        assert not restarted.memory_extraction_status()["needs_attention"]
    finally:
        await restarted.close()


async def test_explicit_request_retries_attention_without_automatic_paid_loop(runtime):
    add_source(runtime)
    runtime.providers.failure = ProviderError("fixture", "authentication_failed")
    await runtime.extract_memories()
    runtime.providers.failure = None
    await runtime.request_memory_extraction(session_id=runtime.session_id)
    await runtime.compression_task
    assert len(runtime.providers.calls) == 2
    assert runtime.memory.extraction_status()["processed_sources"] == 1


async def test_transient_failure_is_limited_to_three_automatic_attempts(runtime):
    add_source(runtime)
    runtime.providers.failure = ProviderError("fixture", "timeout")
    for _ in range(3):
        runtime.background.state("memory_extraction")["retry_at"] = 0
        await runtime.extract_memories()
    assert len(runtime.providers.calls) == 3
    assert runtime.memory_extraction_status()["needs_attention"]
    runtime.background.state("memory_extraction")["retry_at"] = 0
    await runtime.extract_memories()
    assert len(runtime.providers.calls) == 3
    assert runtime.memory.extraction_status()["processed_sources"] == 0
