"""Consecutive speech, delayed providers and command boundaries without devices."""
import asyncio

import pytest

from greatsage.runtime import Runtime
from greatsage.segmentation import SpeechEvent


class Providers:
    async def close(self):
        pass


@pytest.fixture
async def voice(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "")
    runtime = Runtime(tmp_path, providers=Providers())
    runtime.listening = True
    runtime.routed = []

    async def route(message, speech_end):
        runtime.routed.append(message)

    runtime._route_message = route
    yield runtime
    await runtime.close()


def stream(runtime):
    identity = runtime._voice_identity("microphone:fixture")
    runtime.streaming_turns[identity.source] = {
        "origin": 100, "settled_end": -1., "turn": None, "pending": [], "committing": 0}
    return identity


async def result(runtime, identity, text, start, end, **flags):
    await runtime._streaming_transcript(identity, text, True, "", {
        "start": start, "duration": end - start, **flags})


async def commits(runtime, source):
    tail = runtime.streaming_turns[source].get("commit_tail")
    if tail:
        async with asyncio.timeout(3):
            await tail


async def test_late_first_endpoint_preserves_both_turns_and_only_replies_when_settled(voice):
    identity = stream(voice)
    await voice._handle_segment(identity, SpeechEvent("start", media_start=0))
    await result(voice, identity, "第一句话", 0, 1)
    await voice._handle_segment(identity, SpeechEvent("final", speech_end=101,
        media_end=1.3, media_voice_end=1))
    await voice._handle_segment(identity, SpeechEvent("start", media_start=1.5))
    assert voice.voice_activity(identity.source) == {"speech_phase": "speaking", "pending_turns": 1}
    await result(voice, identity, "", 1, 1.3, from_finalize=True)
    await commits(voice, identity.source)
    assert [row["text"] for row in voice.memory.history()] == ["第一句话"]
    assert not voice.routed and identity.source not in voice.voice_disabled

    await voice._handle_segment(identity, SpeechEvent("final", speech_end=102.4,
        media_end=2.7, media_voice_end=2.4))
    await result(voice, identity, "第二句话", 1.3, 2.7, from_finalize=True)
    await commits(voice, identity.source)
    history = voice.memory.history()
    assert [row["text"] for row in history] == ["第一句话", "第二句话"]
    assert [row["text"] for row in voice.routed] == ["第二句话"]
    assert all("_voice_context" not in row and "_voice_approval_ids" not in row for row in history)
    assert voice.voice_activity(identity.source) == {"speech_phase": "idle", "pending_turns": 0}


async def test_stable_final_coverage_can_finish_without_finalize_ack_but_interim_cannot(voice):
    identity = stream(voice)
    await voice._handle_segment(identity, SpeechEvent("start", media_start=0))
    await result(voice, identity, "前半句", 0, .8, speech_final=True)
    await voice._streaming_transcript(identity, "不稳定的后半句", False, "", {"start": .8, "duration": .5})
    await voice._handle_segment(identity, SpeechEvent("final", speech_end=100.9,
        media_end=1.3, media_voice_end=.9))
    assert not voice.memory.history() and not voice.routed
    # No ACK: stable final media coverage through the submitted local boundary.
    await result(voice, identity, "后半句", .8, 1.3)
    await commits(voice, identity.source)
    assert [row["text"] for row in voice.routed] == ["前半句 后半句"]


async def test_short_noise_after_pending_valid_speech_never_becomes_a_command(voice):
    identity = stream(voice)
    await voice._handle_segment(identity, SpeechEvent("start", media_start=0))
    await result(voice, identity, "有效问题", 0, 1)
    await voice._handle_segment(identity, SpeechEvent("final", speech_end=101,
        media_end=1.3, media_voice_end=1))
    await voice._handle_segment(identity, SpeechEvent("start", media_start=1.5))
    await voice._handle_segment(identity, SpeechEvent("discard", media_end=1.9))
    await result(voice, identity, "", 1, 1.3, from_finalize=True)
    await result(voice, identity, "噪声误识别的确认", 1.3, 1.9, from_finalize=True)
    await commits(voice, identity.source)
    assert [row["text"] for row in voice.memory.history()] == ["有效问题"]
    assert [row["text"] for row in voice.routed] == ["有效问题"]


@pytest.mark.parametrize("reason", ["crossing", "overflow"])
async def test_unassignable_or_excessive_pending_turns_fail_visibly_without_commands(voice, reason):
    identity = stream(voice)
    await voice._handle_segment(identity, SpeechEvent("start", media_start=0))
    if reason == "crossing":
        await voice._handle_segment(identity, SpeechEvent("final", speech_end=101,
            media_end=1.3, media_voice_end=1))
        await voice._handle_segment(identity, SpeechEvent("start", media_start=1.5))
        await result(voice, identity, "不能确定属于哪一轮", 0, 2, from_finalize=True)
        expected = "streaming_asr_segment_overlap"
    else:
        for index in range(5):
            await voice._handle_segment(identity, SpeechEvent("final", speech_end=101 + index,
                media_end=1.3 + index, media_voice_end=1 + index))
            await voice._handle_segment(identity, SpeechEvent("start", media_start=1.5 + index))
        expected = "streaming_asr_turn_queue_full"
    assert voice.voice_errors[identity.source] == expected
    assert identity.source in voice.voice_disabled
    assert not voice.memory.history() and not voice.routed
    assert not voice.pending_voice


@pytest.mark.parametrize("finish", ["noise", "failure"])
async def test_batch_old_final_waits_for_newer_speech_and_releases_after_noise_or_failure(voice, finish):
    identity = voice._voice_identity("microphone:fixture")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def transcribe(config, pcm):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return {"text": "刚才的有效问题"}
        raise RuntimeError("synthetic unavailable")

    voice.providers.transcribe = transcribe
    await voice._handle_segment(identity, SpeechEvent("start"))
    await voice._handle_segment(identity, SpeechEvent("final", b"\0" * 960, 101))
    first = next(iter(voice.final_tasks))
    await entered.wait()
    await voice._handle_segment(identity, SpeechEvent("start"))
    release.set()
    await first
    assert [row["text"] for row in voice.memory.history()] == ["刚才的有效问题"]
    assert not voice.routed
    if finish == "noise":
        await voice._handle_segment(identity, SpeechEvent("discard"))
    else:
        await voice._handle_segment(identity, SpeechEvent("final", b"\0" * 960, 102))
        await asyncio.gather(*voice.final_tasks)
    assert [row["text"] for row in voice.routed] == ["刚才的有效问题"]
    assert voice.routed[0]["_voice_deferred"]
    assert voice.voice_activity(identity.source) == {"speech_phase": "idle", "pending_turns": 0}


async def test_delayed_asr_confirmation_cannot_approve_a_tool_created_after_capture(voice):
    identity = voice._voice_identity("microphone:fixture")
    voice._route_message = Runtime._route_message.__get__(voice)
    voice.settings.update({"mode": "conversation"})
    entered, release = asyncio.Event(), asyncio.Event()
    approvals, replies = [], []

    async def transcribe(config, pcm):
        entered.set()
        await release.wait()
        return {"text": "确认"}

    async def approve(message):
        approvals.append(message)
        return True

    async def submit(message, speech_end):
        replies.append(message)

    voice.providers.transcribe = transcribe
    voice.tasks.handle_voice = approve
    voice.submit_message = submit
    await voice._handle_segment(identity, SpeechEvent("start"))
    await voice._handle_segment(identity, SpeechEvent("final", b"\0" * 960, 101))
    first = next(iter(voice.final_tasks))
    await entered.wait()
    voice.tasks.approvals.add("new-pending-call")
    release.set()
    await first
    assert not approvals and len(replies) == 1
    # A fresh utterance begins while that same call already awaits approval.
    await voice._handle_segment(identity, SpeechEvent("start"))
    await voice._handle_segment(identity, SpeechEvent("final", b"\0" * 960, 102))
    await asyncio.gather(*voice.final_tasks)
    assert len(approvals) == 1 and len(replies) == 1
    assert approvals[0]["_voice_approval_ids"] == frozenset({"new-pending-call"})


@pytest.mark.parametrize("new_turn", ["speaking", "finished"])
async def test_new_speech_during_reply_cleanup_defers_old_response_until_quiet(voice, new_turn):
    identity = voice._voice_identity("microphone:fixture")
    voice._route_message = Runtime._route_message.__get__(voice)
    voice.settings.update({"mode": "conversation"})
    entered, release = asyncio.Event(), asyncio.Event()
    responses = []
    pending_task = None

    async def pause():
        if asyncio.current_task() is pending_task:
            entered.set()
            await release.wait()

    async def respond(message, speech_end, proactive):
        responses.append(message)

    voice.pause_background = pause
    voice._respond = respond
    message = voice.memory.add_message("user", "完整的前一句", identity.source,
        identity.session_id, "race-fixture")
    pending_task = asyncio.create_task(voice._route_voice_message(message, 101, identity, voice.data_epoch, 0))
    await entered.wait()
    await voice._handle_segment(identity, SpeechEvent("start"))
    if new_turn == "finished":
        voice.mic_speaking = False
        newer = voice.memory.add_message("user", "已经完成的后一句", identity.source,
            identity.session_id, "newer-fixture")
        await voice._route_voice_message(newer, 102, identity, voice.data_epoch, 1)
        await voice.reply_task
    release.set()
    await pending_task
    if new_turn == "finished":
        assert [row["text"] for row in responses] == ["已经完成的后一句"]
        assert identity.source not in voice.pending_voice
        return
    assert not responses and voice.reply_task is None
    assert identity.source in voice.pending_voice
    await voice._handle_segment(identity, SpeechEvent("discard"))
    await voice.reply_task
    assert [row["text"] for row in responses] == ["完整的前一句"]
