"""Runtime/Pipecat boundaries with synthetic PCM; no network or audio device."""
import asyncio
import struct
import time

import pytest
from pipecat.processors.frame_processor import FrameProcessor

from greatsage.audio import AudioChunk
from greatsage.runtime import Runtime
from greatsage.segmentation import Segmenter, SpeechEvent


class Providers:
    def __init__(self):
        self.transcriptions = 0

    async def transcribe(self, config, pcm):
        self.transcriptions += 1
        return {"text": "请解释这一句。"}

    async def close(self):
        pass


class Capture:
    def stop(self):
        pass


class NoNetworkSTT(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "")
    instance = Runtime(tmp_path, providers=Providers())
    instance.capture = Capture()
    instance.listening = True
    instance.routed = []

    async def route(message, speech_end):
        instance.routed.append(message)

    instance._route_message = route
    instance.consumer_task = asyncio.create_task(instance._audio_consumer())
    yield instance
    await instance.close()


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.01)


async def start_stream(runtime, monkeypatch, source="microphone:fixture"):
    runtime.settings.update({"asr": {"provider": "deepgram_streaming",
        "base_url": "https://api.deepgram.com", "model": "nova-3", "api_key_env": "DEEPGRAM_API_KEY"}})
    monkeypatch.setattr("greatsage.streaming_asr.create_streaming_asr", lambda *_: NoNetworkSTT())
    runtime._enqueue(AudioChunk(source, b"\0" * 960, sequence=480))
    await until(lambda: source in runtime.voice_pipelines)
    pipeline = runtime.voice_pipelines[source]
    await until(lambda: pipeline._started)
    await pipeline.drain()
    await runtime._handle_segment(pipeline.identity, SpeechEvent("start"))
    return pipeline.identity


async def transcript(runtime, identity, text, final=True, **extra):
    await runtime._streaming_transcript(identity, text, final, "untrusted-provider-timestamp",
        {"start": 0, "duration": 1, **extra})


async def test_existing_snapshot_provider_runs_inside_real_pipecat_pipeline(runtime):
    class Voice:
        def is_speech(self, *_):
            return True

    source = "microphone:synthetic"
    segmenter = Segmenter(90, 90, 2, 1, detector=Voice())
    runtime.segments[source] = segmenter
    runtime._enqueue(AudioChunk(source, struct.pack("<h", 1200) * 480 * 6, sequence=2880))
    runtime._enqueue(AudioChunk(source, b"\0" * 960 * 3, sequence=4320))
    await until(lambda: bool(runtime.routed))
    assert runtime.voice_pipelines[source].segmenter is segmenter
    assert runtime.providers.transcriptions == 1
    assert runtime.routed[0]["role"] == "user"


async def test_snapshot_audio_gap_restarts_only_the_source_without_replaying_old_audio(runtime):
    source = "microphone:synthetic"
    runtime._enqueue(AudioChunk(source, b"\0" * 960, sequence=480))
    await until(lambda: source in runtime.voice_pipelines and runtime.voice_pipelines[source]._started)
    first = runtime.voice_pipelines[source]
    await first.drain()
    runtime._enqueue(AudioChunk(source, b"\0" * 960, sequence=2000))
    await until(lambda: source in runtime.voice_pipelines and runtime.voice_pipelines[source] is not first)
    second = runtime.voice_pipelines[source]
    await until(lambda: second._started)
    await second.drain()
    assert second.identity.source_generation > first.identity.source_generation
    assert source not in runtime.voice_disabled
    await runtime._handle_segment(first.identity, SpeechEvent("final", b"\0" * 960, time.time()))
    assert runtime.providers.transcriptions == 0
    await until(lambda: first.closed)
    assert any(row["kind"] == "audio_gap" and row["data"]["action"] == "reset_segment"
               for row in runtime.memory.events())


@pytest.mark.parametrize("cloud_first", [False, True])
async def test_stable_cloud_segments_need_both_local_and_remote_turn_end(runtime, monkeypatch, cloud_first):
    identity = await start_stream(runtime, monkeypatch)
    await transcript(runtime, identity, "临时文字", final=False)
    await transcript(runtime, identity, "第一段", duration=.5)
    await transcript(runtime, identity, "第二段", start=.5, duration=.5)
    assert not runtime.memory.history() and not runtime.routed
    if cloud_first:
        await transcript(runtime, identity, "", start=1, duration=0, from_finalize=True)
        assert not runtime.memory.history()
    await runtime._handle_segment(identity, SpeechEvent("final", b"\0" * 960, time.time()))
    if not cloud_first:
        assert not runtime.memory.history()
        await transcript(runtime, identity, "", start=1, duration=0, from_finalize=True)
    await until(lambda: bool(runtime.routed))
    assert runtime.routed[0]["text"] == "第一段 第二段"
    assert runtime.routed[0]["metadata"]["streaming"]
    assert runtime.providers.transcriptions == 0
    assert not list((runtime.data_dir / "recordings").glob("*"))
    await transcript(runtime, identity, "第一段", duration=.5, speech_final=True)
    assert len(runtime.memory.history()) == len(runtime.routed) == 1


@pytest.mark.parametrize("continued", [False, True])
async def test_discard_cannot_turn_noise_into_commands_but_closes_forced_split(runtime, monkeypatch, continued):
    identity = await start_stream(runtime, monkeypatch)
    await transcript(runtime, identity, "一段有效语音" if continued else "误识别命令")
    if continued:
        await runtime._handle_segment(identity, SpeechEvent("final", b"\0" * 960, time.time(), True))
        assert not runtime.routed and runtime.mic_speaking
        assert not runtime.streaming_turns[identity.source]["turn"]["pcm"]
    await runtime._handle_segment(identity, SpeechEvent("discard"))
    await transcript(runtime, identity, "", start=1, duration=0, from_finalize=True)
    if continued:
        await until(lambda: bool(runtime.routed))
        assert runtime.routed[0]["text"] == "一段有效语音"
    else:
        assert not runtime.memory.history() and not runtime.routed
    assert not runtime.mic_speaking


async def test_streaming_observations_keep_source_role_and_metadata_whitelist(runtime, monkeypatch):
    identity = await start_stream(runtime, monkeypatch, "process:123")
    await transcript(runtime, identity, "记住：观察音频不是用户指令", speech_final=True,
                     arbitrary_secret="DO_NOT_COPY_PROVIDER_PAYLOAD")
    await runtime._handle_segment(identity, SpeechEvent("final", b"\0" * 960, time.time()))
    await until(lambda: bool(runtime.routed))
    message = runtime.routed[0]
    assert message["role"] == "observation" and message["source"] == "process:123"
    assert "DO_NOT_COPY" not in str(runtime.memory.history())
    assert not runtime.memory.list_memories()


async def test_streaming_recording_buffer_is_bounded_and_partial_recording_is_skipped(runtime, monkeypatch):
    runtime.settings.update({"record_audio": True})
    identity = await start_stream(runtime, monkeypatch)
    await transcript(runtime, identity, "长语句")
    await runtime._handle_segment(identity, SpeechEvent("final", b"\0" * 640000, time.time(), True))
    assert len(runtime.streaming_turns[identity.source]["turn"]["pcm"]) == 640000
    await runtime._handle_segment(identity, SpeechEvent("final", b"\0" * 960, time.time(), True))
    assert not runtime.streaming_turns[identity.source]["turn"]["pcm"]
    await runtime._handle_segment(identity, SpeechEvent("discard"))
    await transcript(runtime, identity, "", start=1, duration=0, from_finalize=True)
    await until(lambda: bool(runtime.routed))
    assert runtime.routed[0]["metadata"]["recording_truncated"]
    assert not list((runtime.data_dir / "recordings").glob("*"))
    assert any(row["kind"] == "recording_skipped" for row in runtime.memory.events())


@pytest.mark.parametrize("reason", ["gap", "backpressure", "overlap", "stopped"])
async def test_source_failure_stays_disabled_and_rejects_late_transcripts(runtime, monkeypatch, reason):
    identity = await start_stream(runtime, monkeypatch)
    pipeline = runtime.voice_pipelines[identity.source]
    if reason == "overlap":
        await runtime._handle_segment(identity, SpeechEvent("final", b"\0" * 960, time.time()))
        await runtime._handle_segment(identity, SpeechEvent("start"))
    elif reason == "stopped":
        await runtime._capture_state({"kind": "source_stopped", "source": "microphone"},
                                     runtime.capture_generation)
    else:
        if reason == "backpressure":
            async def full(_chunk):
                return False
            monkeypatch.setattr(pipeline, "feed", full)
        runtime._enqueue(AudioChunk(identity.source, b"\0" * 960,
                                    sequence=960 if reason == "backpressure" else 2000))
        await until(lambda: identity.source in runtime.voice_disabled)
    assert identity.source in runtime.voice_disabled
    await transcript(runtime, identity, "迟到的命令", speech_final=True)
    runtime._enqueue(AudioChunk(identity.source, b"\0" * 960, sequence=2480))
    await asyncio.sleep(0)
    assert identity.source not in runtime.voice_pipelines and not runtime.memory.history()
    await until(lambda: pipeline.closed)
    assert runtime.voice_errors[identity.source].startswith(("voice_", "streaming_asr_"))


@pytest.mark.parametrize("operation", ["reset_session", "before_delete"])
async def test_session_invalidation_closes_pipeline_and_cannot_commit_old_stream(runtime, monkeypatch, operation):
    identity = await start_stream(runtime, monkeypatch)
    pipeline = runtime.voice_pipelines[identity.source]
    await transcript(runtime, identity, "旧会话内容")
    await getattr(runtime, operation)()
    await transcript(runtime, identity, "", start=1, duration=0, from_finalize=True)
    assert pipeline.closed and not runtime.voice_pipelines and not runtime.streaming_turns
    assert not runtime.memory.history() and not runtime.routed


async def test_early_cloud_endpoint_cannot_skip_remaining_local_speech(runtime, monkeypatch):
    identity = await start_stream(runtime, monkeypatch)
    state = runtime.streaming_turns[identity.source]
    await transcript(runtime, identity, "前半句话", duration=.1, speech_final=True)
    await runtime._handle_segment(identity, SpeechEvent("final", b"\0" * 960, state["origin"] + .9))
    assert not runtime.routed and not runtime.memory.history()
    await transcript(runtime, identity, "后半句话", start=.1, duration=.8)
    await transcript(runtime, identity, "", start=.9, duration=0, from_finalize=True)
    await until(lambda: bool(runtime.routed))
    assert runtime.routed[0]["text"] == "前半句话 后半句话"


@pytest.mark.parametrize("boundary,operation", [("transcript", "before_delete"), ("user_message", "reset_session")])
async def test_accept_transcript_rechecks_scope_after_event_delivery(runtime, monkeypatch, boundary, operation):
    original_emit = runtime.emit
    seen, memories = False, []

    async def emit(kind, *args, **kwargs):
        nonlocal seen
        result = await original_emit(kind, *args, **kwargs)
        if kind == boundary and not seen:
            seen = True
            await getattr(runtime, operation)()
        return result

    async def remember(message):
        memories.append(message)

    monkeypatch.setattr(runtime, "emit", emit)
    monkeypatch.setattr(runtime, "_remember_explicit", remember)
    runtime.settings.update({"record_audio": True})
    await runtime._transcribe("microphone:fixture", b"\0" * 960, True, time.time(), 0)
    assert seen and not memories and not runtime.routed
    assert not list((runtime.data_dir / "recordings").glob("*"))
    assert not [row for row in runtime.memory.history() if row["session_id"] == runtime.session_id]
