import asyncio
import contextlib
import logging
from types import SimpleNamespace

import pytest

from greatsage.streaming_asr import _configuration, create_streaming_asr


@pytest.mark.parametrize("config,code", [
    ({}, "credentials_required"),
    ({"api_key": " "}, "credentials_required"),
    ({"api_key": "fixture", "base_url": "https://api.deepgram.com.attacker.invalid"}, "endpoint_not_allowed"),
    ({"api_key": "fixture", "base_url": "http://api.deepgram.com"}, "endpoint_not_allowed"),
    ({"api_key": "fixture", "base_url": "https://api.deepgram.com?token=secret"}, "endpoint_not_allowed"),
    ({"api_key": "fixture", "model": "unrelated"}, "model_not_supported"),
    ({"api_key": "fixture", "language": "auto"}, "language_not_supported"),
])
def test_configuration_rejects_implicit_credentials_and_unapproved_destinations(monkeypatch, config, code):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "must-not-be-used")
    with pytest.raises(ValueError, match=code):
        _configuration(config)


@pytest.fixture
def sdk(monkeypatch):
    pytest.importorskip("loguru")
    from greatsage.streaming_asr import _quiet_sdk_logging

    _quiet_sdk_logging()
    pytest.importorskip("pipecat.services.deepgram.stt")
    from loguru import logger
    from deepgram.listen.v1.types import ListenV1Results
    from pipecat.frames.frames import ErrorFrame, InterimTranscriptionFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.services.deepgram import stt

    credentials = []

    def client(**kwargs):
        credentials.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(stt, "AsyncDeepgramClient", client)
    frames = []

    async def capture(self, frame, direction=FrameDirection.DOWNSTREAM):
        frames.append(frame)

    monkeypatch.setattr(FrameProcessor, "push_frame", capture)

    def result(text="", *, final=True, finalize=False, speech_final=False):
        return ListenV1Results.model_validate({
            "channel": {"alternatives": [{"transcript": text, "confidence": 0.99, "words": []}]},
            "channel_index": [0, 1],
            "metadata": {"request_id": "fixture", "model_uuid": "fixture",
                         "model_info": {"name": "nova-3", "version": "fixture", "arch": "nova"}},
            "is_final": final, "from_finalize": finalize, "speech_final": speech_final,
            "start": 1.25, "duration": 0.75,
        })

    def make(**config):
        return create_streaming_asr({"api_key": "explicit-fixture-key", **config})

    async def cancel(task, **kwargs):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def tasks(service):
        service.create_task = lambda coroutine, *args: asyncio.create_task(coroutine)
        service.cancel_task = cancel

    return SimpleNamespace(
        make=make, frames=frames, credentials=credentials, result=result, tasks=tasks,
        final=TranscriptionFrame, partial=InterimTranscriptionFrame, error=ErrorFrame, logger=logger,
    )


@pytest.mark.parametrize("language,expected", [
    ("zh", "zh"), ("zh-CN", "zh"), ("zh-Hans", "zh"),
    ("zh-TW", "zh-TW"), ("zh-Hant", "zh-TW"), ("en", "en"), ("ja", "ja"),
])
def test_sdk_construction_preserves_language_and_local_endpointing(sdk, language, expected):
    service = sdk.make(language=language)
    service._sample_rate = service._init_sample_rate  # Filled by SDK setup in the actual pipeline.
    options = service._build_connect_kwargs()
    assert options["language"] == expected
    assert options["model"] == "nova-3"
    assert options["endpointing"] == "300"
    assert options["sample_rate"] == "16000"
    assert options["encoding"] == "linear16"
    assert options["channels"] == "1"
    assert options["mip_opt_out"] == "true"
    assert sdk.credentials[-1] == {"api_key": "explicit-fixture-key"}
    assert service._connection is None


async def test_partial_final_and_empty_finalize_preserve_server_boundaries(sdk):
    service = sdk.make()
    interim = sdk.result("未完成", final=False)
    final = sdk.result("完整文字")
    acknowledgement = sdk.result(finalize=True)
    await service._on_message(interim)
    await service._on_message(final)
    service.request_finalize()
    await service._on_message(acknowledgement)
    assert [type(frame) for frame in sdk.frames] == [sdk.partial, sdk.final, sdk.final]
    assert [frame.text for frame in sdk.frames] == ["未完成", "完整文字", ""]
    assert sdk.frames[-1].finalized is True
    assert sdk.frames[-1].result is acknowledgement
    assert sdk.frames[1].result is final
    assert final.start == 1.25 and final.duration == 0.75
    assert final.speech_final is False and acknowledgement.from_finalize is True


async def test_untrusted_errors_fail_once_without_payload_logging_or_late_results(sdk, caplog):
    service = sdk.make()
    codes, output = [], []
    service._safe_error_callback = codes.append
    sink = sdk.logger.add(lambda message: output.append(str(message)), level="TRACE")
    try:
        await service._on_error("secret-credential and private transcript")
        await service._on_error("another private response body")
        await service._on_message(sdk.result("late private text"))
        logging.getLogger("deepgram.connection").error("private SDK exception")
    finally:
        sdk.logger.remove(sink)
    assert codes == ["streaming_asr_transport_failed"]
    assert not service.is_usable
    assert len(sdk.frames) == 1 and isinstance(sdk.frames[0], sdk.error)
    assert sdk.frames[0].exception is None
    assert sdk.frames[0].error == codes[0]
    assert not output and "private" not in caplog.text


async def test_failed_connection_is_attempted_once_without_audio_replay(sdk):
    service = sdk.make()
    sdk.tasks(service)
    attempts = []

    @contextlib.asynccontextmanager
    async def connect(**kwargs):
        attempts.append(kwargs)
        raise RuntimeError("untrusted provider response with secret")
        yield

    service._client = SimpleNamespace(listen=SimpleNamespace(v1=SimpleNamespace(connect=connect)))
    await service._connect()
    await service._connection_task
    await service._connect()
    await service._request_reconnect()
    assert [frame async for frame in service.run_stt(b"synthetic")] == [None]
    assert len(attempts) == 1
    assert service._failed_code == "streaming_asr_connection_failed"
    assert service._connection is None


async def test_sdk_error_payload_received_as_message_fails_the_source(sdk):
    from deepgram.listen.v1.types import ListenV1Error

    service = sdk.make()
    await service._on_message(ListenV1Error.model_construct(
        type="Error", description="private upstream body", code="private vendor code",
    ))
    assert service._failed_code == "streaming_asr_transport_failed"
    assert len(sdk.frames) == 1 and sdk.frames[0].exception is None


async def test_send_failure_never_retries_and_cancellation_drops_late_results(sdk):
    service = sdk.make()
    sdk.tasks(service)
    sends, closed, attempts = [], [], []
    listening = asyncio.Event()

    async def send_media(audio):
        sends.append(audio)
        raise RuntimeError("upstream body")

    async def start_listening():
        await listening.wait()

    async def close_stream(frame):
        closed.append(frame)

    connection = SimpleNamespace(
        on=lambda *args: None, start_listening=start_listening,
        send_media=send_media, send_close_stream=close_stream,
    )

    @contextlib.asynccontextmanager
    async def connect(**kwargs):
        attempts.append(kwargs)
        yield connection

    service._client = SimpleNamespace(listen=SimpleNamespace(v1=SimpleNamespace(connect=connect)))
    await service._connect()
    await anext(service.run_stt(b"one chunk"))
    await anext(service.run_stt(b"must not replay"))
    await service._connection_task
    assert sends == [b"one chunk"] and len(attempts) == 1
    await service._disconnect()
    frame_count = len(sdk.frames)
    await service._on_message(sdk.result("late"))
    assert len(sdk.frames) == frame_count
    assert service._connection_task is None


async def test_disconnect_cancels_sdk_listener_and_preserves_cancellation(sdk):
    service = sdk.make()
    sdk.tasks(service)
    disconnected, listener_cancelled = asyncio.Event(), asyncio.Event()
    close_requests = []

    async def listen():
        try:
            await asyncio.Event().wait()
        finally:
            listener_cancelled.set()

    async def close_stream(frame):
        close_requests.append(frame)

    connection = SimpleNamespace(on=lambda *args: None, start_listening=listen, send_close_stream=close_stream)

    @contextlib.asynccontextmanager
    async def connect(**kwargs):
        try:
            yield connection
        finally:
            disconnected.set()

    service._client = SimpleNamespace(listen=SimpleNamespace(v1=SimpleNamespace(connect=connect)))
    await service._connect()
    await asyncio.sleep(0)
    handler = service._connection_task
    await service._disconnect()
    assert handler.cancelled()
    assert listener_cancelled.is_set() and disconnected.is_set()
    assert len(close_requests) == 1
    assert not sdk.frames and service._connection_task is None


@pytest.mark.parametrize("handshake", ["success", "failed", "timeout"])
async def test_connection_probe_requires_sdk_handshake_and_closes_without_audio(sdk, monkeypatch, handshake):
    from greatsage import streaming_asr

    service = sdk.make()
    attempts, close_requests, media = [], [], []
    exited = asyncio.Event()

    async def listen():
        await asyncio.Event().wait()

    async def close_stream(frame):
        close_requests.append(frame)

    async def send_media(audio):
        media.append(audio)

    connection = SimpleNamespace(
        on=lambda *args: None, start_listening=listen,
        send_close_stream=close_stream, send_media=send_media,
    )

    @contextlib.asynccontextmanager
    async def connect(**kwargs):
        attempts.append(kwargs)
        try:
            if handshake == "failed":
                raise RuntimeError("private auth response")
            if handshake == "timeout":
                await asyncio.Event().wait()
            yield connection
        finally:
            exited.set()

    service._client = SimpleNamespace(listen=SimpleNamespace(v1=SimpleNamespace(connect=connect)))
    monkeypatch.setattr(streaming_asr, "create_streaming_asr", lambda config: service)
    monkeypatch.setattr(streaming_asr, "_PROBE_CONNECT_TIMEOUT", 0.05)
    result = await streaming_asr.probe_streaming_connection({"api_key": "fixture"})
    assert len(attempts) == 1 and not media
    assert exited.is_set() and service._connection_task is None
    assert result["ok"] is (handshake == "success")
    assert result["code"] == {
        "success": "streaming_asr_connected",
        "failed": "streaming_asr_connection_failed",
        "timeout": "streaming_asr_connection_timeout",
    }[handshake]
    assert len(close_requests) == (1 if handshake == "success" else 0)
    assert "private" not in str(result)
