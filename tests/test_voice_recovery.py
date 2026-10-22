"""One-source retry races with synthetic PCM and controlled pipeline lifetimes."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from greatsage.audio import AudioChunk
from greatsage.runtime import Runtime
from greatsage.server import create_app


class Providers:
    async def close(self):
        pass


class Capture:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


class Pipeline:
    instances = []
    start_gate = None

    def __init__(self, identity, config=None, *_args, **_kwargs):
        self.identity = identity
        self.segmenter = object()
        self.streaming = False
        self.pending_frames = 0
        self.state = "starting"
        self.closed = False
        self.received = []
        self.close_gate = None
        self.started = asyncio.Event()
        self.instances.append(self)

    async def start(self):
        self.started.set()
        if self.start_gate:
            await self.start_gate.wait()
        if self.closed:
            raise RuntimeError("OLD_PRIVATE_UPSTREAM_ERROR")
        self.state = "running"

    async def close(self):
        self.closed = True
        if self.close_gate:
            await self.close_gate.wait()
        self.state = "closed"

    async def feed(self, chunk):
        self.received.append(chunk)
        return True


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "")
    monkeypatch.setattr("greatsage.voice_pipeline.SourceVoicePipeline", Pipeline)
    Pipeline.instances = []
    Pipeline.start_gate = None
    subject = Runtime(tmp_path, providers=Providers())
    subject.capture = Capture()
    subject.listening = True
    subject.capture_sources = {"microphone:2": "running", "system": "running"}
    yield subject
    Pipeline.start_gate = None
    for pipeline in Pipeline.instances:
        if pipeline.close_gate:
            pipeline.close_gate.set()
    await subject.close()


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


async def failed_source(runtime, source="microphone:2"):
    pipeline = Pipeline(runtime._voice_identity(source))
    pipeline.state = "running"
    runtime.voice_pipelines[source] = pipeline
    await runtime._voice_failed(pipeline.identity, "streaming_asr_connection_closed")
    return pipeline


async def test_retry_preserves_other_source_retires_callbacks_and_drops_queued_and_dispatch_pcm(runtime):
    old = await failed_source(runtime)
    other = Pipeline(runtime._voice_identity("system"))
    other.state = "running"
    runtime.voice_pipelines["system"] = other
    old_pcm = AudioChunk("microphone:2", b"\0" * 960)
    other_pcm = AudioChunk("system", b"\0" * 960)
    runtime.audio_queue.put_nowait((runtime.epoch, old_pcm))
    runtime.audio_queue.put_nowait((runtime.epoch, other_pcm))
    result = await runtime.retry_voice_source("microphone:2")
    fresh = runtime.voice_pipelines["microphone:2"]
    assert fresh is not old and not runtime._voice_current(old.identity)
    assert runtime.voice_pipelines["system"] is other and not other.closed
    assert result["listening"] and runtime.capture.stops == 0
    assert any(row["kind"] == "audio_source" and row["data"]["kind"] == "pipeline_started"
               for row in runtime.memory.events())
    assert next(row for row in result["sources"] if row["source"] == "microphone:2")["state"] == "running"
    assert runtime.audio_queue.get_nowait()[1] is other_pcm
    assert runtime.audio_queue.empty()
    # Dispatch callbacks that were already scheduled are rejected by capture time.
    runtime._enqueue(old_pcm)
    assert runtime.audio_queue.empty()
    cutoff = runtime.voice_cutoffs["microphone:2"]
    runtime._enqueue(AudioChunk("microphone:2", b"\0" * 960, timestamp=cutoff + .01))
    assert runtime.audio_queue.empty()  # Whole straddling chunk is discarded.
    fresh_pcm = AudioChunk("microphone:2", b"\0" * 960, timestamp=cutoff + .1)
    runtime._enqueue(fresh_pcm)
    runtime.consumer_task = asyncio.create_task(runtime._audio_consumer())
    await until(lambda: bool(fresh.received))
    await runtime._voice_failed(old.identity, "streaming_asr_transport_failed")
    await runtime._streaming_transcript(old.identity, "PRIVATE_OLD_TEXT", True, "", {})
    assert fresh.received == [fresh_pcm] and not runtime.voice_disabled
    assert not runtime.memory.history()


async def test_double_retry_coalesces_and_stop_wins_during_cleanup(runtime):
    old = await failed_source(runtime)
    old.close_gate = asyncio.Event()
    retry = asyncio.create_task(runtime.retry_voice_source("microphone:2"))
    await until(lambda: "microphone:2" in runtime.voice_retries)
    status = await runtime.retry_voice_source("microphone:2")
    assert len(Pipeline.instances) == 1
    assert next(row for row in status["sources"] if row["source"] == "microphone:2")["recovery_action"] is None
    stop = asyncio.create_task(runtime.set_listening(False))
    await until(lambda: not runtime.listening)
    old.close_gate.set()
    await asyncio.gather(retry, stop)
    assert len(Pipeline.instances) == 1 and not runtime.voice_pipelines


@pytest.mark.parametrize("operation", ["session", "settings", "capture"])
async def test_invalidation_while_retry_starts_cannot_resurrect_a_source(runtime, operation):
    await failed_source(runtime)
    Pipeline.start_gate = asyncio.Event()
    retry = asyncio.create_task(runtime.retry_voice_source("microphone:2"))
    await until(lambda: len(Pipeline.instances) == 2 and Pipeline.instances[-1].started.is_set())
    if operation == "session":
        await runtime.reset_session()
    elif operation == "settings":
        # The settings route invalidates capture before its first cleanup await.
        runtime.settings.update({"voice_enabled": False})
        await runtime.set_listening(False)
    else:
        await runtime._capture_state({"kind": "source_stopped", "source": "microphone"}, runtime.capture_generation)
    Pipeline.start_gate.set()
    await retry
    assert "microphone:2" not in runtime.voice_pipelines
    assert not runtime.voice_retries
    assert "PRIVATE_" not in json.dumps(runtime.memory.events())


async def test_late_old_start_failure_does_not_disable_successful_retry(runtime):
    Pipeline.start_gate = asyncio.Event()
    runtime.consumer_task = asyncio.create_task(runtime._audio_consumer())
    runtime._enqueue(AudioChunk("microphone:2", b"\0" * 960))
    await until(lambda: bool(Pipeline.instances) and Pipeline.instances[0].started.is_set())
    old = Pipeline.instances[0]
    await runtime._voice_failed(old.identity, "streaming_asr_connection_closed")
    old_gate, Pipeline.start_gate = Pipeline.start_gate, None
    await runtime.retry_voice_source("microphone:2")
    fresh = runtime.voice_pipelines["microphone:2"]
    old_gate.set()
    await asyncio.sleep(.01)
    assert runtime.voice_pipelines["microphone:2"] is fresh and not runtime.voice_disabled
    assert not fresh.received and "PRIVATE_" not in json.dumps(runtime.memory.events())


async def test_configuration_failure_surfaces_exact_safe_guidance_without_retry_loop(runtime, monkeypatch):
    await failed_source(runtime)
    runtime.settings.update({"asr": {"provider": "deepgram_streaming", "base_url": "https://api.deepgram.com",
                                     "model": "nova-3", "api_key_env": "DEEPGRAM_API_KEY"}})

    def missing_key(*_args):
        raise ValueError("streaming_asr_credentials_required")

    monkeypatch.setattr("greatsage.streaming_asr.create_streaming_asr", missing_key)
    status = await runtime.retry_voice_source("microphone:2")
    row = next(row for row in status["sources"] if row["source"] == "microphone:2")
    assert row["error_code"] == "streaming_asr_credentials_required"
    assert row["recovery_action"] == "configure_asr"
    with pytest.raises(ValueError):
        await runtime.retry_voice_source("microphone:2")
    assert len(Pipeline.instances) == 1


async def test_failed_cleanup_requires_global_restart_without_opening_another_pipeline(runtime):
    old = Pipeline(runtime._voice_identity("microphone:2"))

    async def broken_close():
        raise RuntimeError("PRIVATE_CLEANUP_ERROR")

    old.close = broken_close
    runtime.voice_pipelines["microphone:2"] = old
    await runtime._voice_failed(old.identity, "streaming_asr_connection_closed")
    status = await runtime.retry_voice_source("microphone:2")
    row = next(row for row in status["sources"] if row["source"] == "microphone:2")
    assert row["recovery_action"] == "restart_listening" and row["state"] == "failed"
    assert row["error_code"] == "voice_pipeline_cleanup_failed"
    assert len(Pipeline.instances) == 1 and not runtime.voice_retries
    assert "PRIVATE_" not in json.dumps(runtime.memory.events())


async def test_device_failure_before_pcm_survives_all_sources_stopped(runtime):
    runtime.capture_sources = {"microphone:default": "starting"}
    await runtime._capture_state({"kind": "source_stopped", "source": "microphone"}, runtime.capture_generation)
    await runtime._capture_state({"kind": "all_sources_stopped", "source": "microphone"}, runtime.capture_generation)
    status = runtime.audio_status()
    assert not status["listening"]
    assert status["sources"] == [{"source": "microphone:default", "state": "stopped", "streaming": None,
                                  "pending_frames": None, "error_code": "voice_source_stopped",
                                  "recovery_action": "restart_listening"}]
    with pytest.raises(ValueError):
        await runtime.retry_voice_source("microphone:default")


async def test_old_all_stopped_event_cannot_stop_a_new_capture_generation(runtime):
    entered, release = asyncio.Event(), asyncio.Event()
    original_emit = runtime.emit

    async def delayed_emit(kind, *args, **kwargs):
        if kind == "audio_source":
            entered.set()
            await release.wait()
        return await original_emit(kind, *args, **kwargs)

    runtime.emit = delayed_emit
    event = asyncio.create_task(runtime._capture_state(
        {"kind": "all_sources_stopped", "source": "system"}, runtime.capture_generation))
    await entered.wait()
    runtime.capture_generation += 1
    runtime.capture_sources = {"system": "running"}
    runtime.voice_disabled.clear()
    runtime.voice_errors.clear()
    release.set()
    await event
    assert runtime.listening and runtime.capture.stops == 0
    assert runtime.capture_sources == {"system": "running"}


def test_audio_status_routes_are_authenticated_metadata_only_and_validate_source(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    subject = Runtime(tmp_path, providers=Providers())
    subject.voice_errors = {"microphone:2": "PRIVATE_ERROR", "PRIVATE_SOURCE": "voice_source_stopped"}
    subject.voice_disabled = set(subject.voice_errors)
    subject.capture_sources = {"microphone:2": "stopped"}
    app = create_app(tmp_path, "fixture-token", runtime=subject)
    with TestClient(app) as client:
        assert client.get("/api/audio/status").status_code == 401
        assert client.post("/api/audio/retry", json={"source": "microphone:2"}).status_code == 401
        client.headers["Authorization"] = "Bearer fixture-token"
        response = client.get("/api/audio/status")
        assert response.status_code == 200 and "PRIVATE" not in response.text
        assert response.json()["sources"][0]["recovery_action"] == "restart_listening"
        for source in (None, [], "microphone:PRIVATE", "process:2", "microphone:2"):
            assert client.post("/api/audio/retry", json={"source": source}).status_code == 400
