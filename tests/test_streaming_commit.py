"""Confirmed streaming turns survive a later VAD start; no devices or network."""
import asyncio
import time

from greatsage.runtime import Runtime
from greatsage.segmentation import SpeechEvent


class NoNetworkProviders:
    async def close(self):
        pass


async def test_confirmed_turn_commits_when_next_turn_starts_during_event_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "")
    runtime = Runtime(tmp_path, providers=NoNetworkProviders())
    runtime.listening = True
    source = "microphone:1"
    identity = runtime._voice_identity(source)
    origin = time.time() - 2
    runtime.streaming_turns[source] = {"origin": origin, "settled_end": -1.0, "turn": None}
    routed = []
    entered, release = asyncio.Event(), asyncio.Event()
    commit = None
    original_emit = runtime.emit

    async def route(message, speech_end):
        routed.append(message)

    async def emit(kind, data=None, *args, **kwargs):
        nonlocal commit
        if kind == "metrics" and (data or {}).get("measurement") == "local_vad_end_and_remote_finalize":
            commit = asyncio.current_task()
            entered.set()
            await release.wait()
        return await original_emit(kind, data, *args, **kwargs)

    monkeypatch.setattr(runtime, "_route_message", route)
    monkeypatch.setattr(runtime, "emit", emit)
    try:
        await runtime._handle_segment(identity, SpeechEvent("start"))
        await runtime._handle_segment(identity, SpeechEvent("final", b"", origin + 1))
        await runtime._streaming_transcript(identity, "已经说完的第一句。", True, "", {
            "start": 0, "duration": 1, "from_finalize": True,
        })
        async with asyncio.timeout(3):
            await entered.wait()
        # Both endpoints have settled. This is not the pending-endpoint overlap case.
        assert runtime.streaming_turns[source]["turn"] is None
        assert runtime.streaming_turns[source]["settled_end"] == 1
        await runtime._handle_segment(identity, SpeechEvent("start"))
        assert runtime.segment_versions[source] == 2
        assert source not in runtime.voice_disabled
        release.set()
        async with asyncio.timeout(3):
            await commit

        assert [row["text"] for row in runtime.memory.history()] == ["已经说完的第一句。"]
        assert not routed  # Retain history, but do not answer over newer speech.
        # A duplicated old interval must still stay out of the newly active turn.
        await runtime._streaming_transcript(identity, "重复的第一句。", True, "", {
            "start": 0, "duration": 1, "from_finalize": True,
        })
        assert runtime.streaming_turns[source]["turn"]["text"] == []
        assert len(runtime.memory.history()) == 1 and not routed
        await runtime._handle_segment(identity, SpeechEvent("discard", media_end=2))
        await runtime._streaming_transcript(identity, "", True, "", {
            "start": 1, "duration": 1, "from_finalize": True,
        })
        assert [row["text"] for row in routed] == ["已经说完的第一句。"]
        assert routed[0]["_voice_deferred"]
    finally:
        release.set()
        await runtime.close()
