import asyncio
import base64
import io
import json
import os
import sys
import types
import wave

import httpx
import pytest

from greatsage.providers import ProviderError, Providers


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes, step=7):
        self.content, self.step, self.closed = content, step, False

    async def __aiter__(self):
        for start in range(0, len(self.content), self.step):
            yield self.content[start:start + self.step]

    async def aclose(self):
        self.closed = True


def service(handler):
    return Providers(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_openrouter_fragmented_utf8_sse_with_usage_and_keepalive():
    stream = ByteStream((': keep alive\n\n'
                         'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
                         'data: {"choices":[{"delta":{"content":"。"},"finish_reason":"stop"}]}\n\n'
                         'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"cost":0.001}}\n\n'
                         'data: [DONE]\n\n').encode())

    def handler(request):
        assert request.url == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret-test-key"
        body = json.loads(request.content)
        assert "secret-test-key" not in request.content.decode()
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    provider = service(handler)
    events = [item async for item in provider.stream_chat(
        {"provider": "openrouter", "model": "example/model", "api_key": "secret-test-key"},
        [{"role": "user", "content": "hi"}])]
    assert "".join(item.get("text", "") for item in events) == "你好。"
    assert events[-1]["usage"]["cost"] == 0.001
    assert stream.closed
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_stopping_chat_closes_response_before_reading_remaining_answer():
    stream = ByteStream(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
                        b'data: {"choices":[{"delta":{"content":"second"}}]}\n\n'
                        b'data: [DONE]\n\n')
    provider = service(lambda _: httpx.Response(200, stream=stream))
    answer = provider.stream_chat({"provider": "openai", "model": "example"}, [])
    assert await anext(answer) == {"text": "first"}
    await answer.aclose()
    assert stream.closed
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_total_timeout_cancels_a_stalled_stream_and_closes_connection():
    class StalledStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b': accepted\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    stream = StalledStream()
    provider = service(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(ProviderError, match="timeout"):
        _ = [item async for item in provider.stream_chat(
            {"provider": "openai", "model": "test", "total_timeout_seconds": 0.1}, [])]
    assert stream.closed
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_ollama_ndjson_disables_thinking_and_only_yields_spoken_content():
    def handler(request):
        assert request.url == "http://127.0.0.1:11434/api/chat"
        assert json.loads(request.content)["think"] is False
        return httpx.Response(200, stream=ByteStream(
            b'{"message":{"thinking":"internal","content":"hello"},"done":false}\n'
            b'{"message":{"content":""},"done":true,"prompt_eval_count":4,"eval_count":1}\n'))

    provider = service(handler)
    events = [item async for item in provider.stream_chat(
        {"provider": "ollama", "base_url": "http://127.0.0.1:11434/api", "model": "local"}, [])]
    assert events == [{"text": "hello"}, {"usage": {"prompt_tokens": 4, "completion_tokens": 1,
                                                    "total_tokens": 5}}]
    await provider._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("content,code", [
    (b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', "stream_ended_before_completion"),
    (b'data: {"error":{"message":"secret-test-key"}}\n\n', "upstream_error"),
    (b'event: error\ndata: secret-test-key\n\n', "upstream_stream_error"),
    (b'data: not-json-secret-test-key\n\n', "invalid_json_response"),
])
async def test_incomplete_and_failed_streams_are_not_reported_as_success(content, code):
    provider = service(lambda _: httpx.Response(200, stream=ByteStream(content)))
    with pytest.raises(ProviderError) as error:
        _ = [event async for event in provider.stream_chat({"provider": "openai", "model": "test"}, [])]
    assert error.value.code == code
    assert "secret-test-key" not in str(error.value)
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_http_failure_does_not_expose_body_or_retry_another_provider():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(401, json={"error": {"message": "key secret-test-key payload private-audio"}})

    provider = service(handler)
    with pytest.raises(ProviderError) as error:
        await provider.transcribe({"provider": "openrouter", "model": "whisper",
                                   "api_key": "secret-test-key"}, b"\x00\x00" * 160)
    assert error.value.code == "authentication_failed"
    assert error.value.status == 401
    assert "secret-test-key" not in str(error.value)
    assert "private-audio" not in str(error.value)
    assert calls == ["openrouter.ai"]
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_openrouter_asr_receives_valid_wav_language_and_returns_actual_usage():
    pcm = b"\x01\x00" * 8000

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/v1/audio/transcriptions"
        assert body["language"] == "zh"
        assert body["input_audio"]["format"] == "wav"
        with wave.open(io.BytesIO(base64.b64decode(body["input_audio"]["data"])), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getframerate() == 16000
            assert wav.readframes(8000) == pcm
        return httpx.Response(200, json={"text": "  已识别。  ", "usage": {"cost": 0.0001, "seconds": 0.5}},
                              headers={"x-generation-id": "gen-test-123"})

    provider = service(handler)
    result = await provider.transcribe({"provider": "openrouter", "model": "openai/whisper-1",
                                        "api_key": "secret", "language": "zh-CN"}, pcm)
    assert result["text"] == "已识别。"
    assert result["usage"] == {"cost": 0.0001, "seconds": 0.5, "input_audio_seconds": 0.5,
                               "generation_id": "gen-test-123"}
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_openai_asr_uses_multipart_file_upload():
    def handler(request):
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert b'name="file"; filename="segment.wav"' in request.content
        assert b"RIFF" in request.content
        assert b'name="model"' in request.content
        return httpx.Response(200, json={"text": "text"})

    provider = service(handler)
    result = await provider.transcribe({"provider": "openai", "model": "whisper-1"}, b"\0\0" * 160)
    assert result["text"] == "text"
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_tts_returns_audio_not_fabricated_cost():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/v1/audio/speech"
        assert body["voice"] == "alloy"
        assert body["response_format"] == "mp3"
        return httpx.Response(200, content=b"ID3test-audio", headers={"content-type": "audio/mpeg",
                                                                     "x-generation-id": "gen-speech"})

    provider = service(handler)
    result = await provider.synthesize({"provider": "openrouter", "model": "speech", "api_key": "secret",
                                        "voice": "alloy"}, "hello")
    assert result["audio"] == b"ID3test-audio"
    assert result["mime"] == "audio/mpeg"
    assert result["usage"] == {"characters": 5, "generation_id": "gen-speech"}
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_pcm_speech_requires_explicit_sample_rate_and_wraps_wav():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"\x01\x00" * 240, headers={"content-type": "audio/pcm"})

    provider = service(handler)
    config = {"provider": "openai", "model": "speech", "voice": "alloy", "response_format": "pcm"}
    with pytest.raises(ProviderError, match="pcm_sample_rate_required"):
        await provider.synthesize(config, "hello")
    assert not calls
    result = await provider.synthesize({**config, "pcm_sample_rate": 24000}, "hello")
    with wave.open(io.BytesIO(result["audio"]), "rb") as wav:
        assert wav.getframerate() == 24000
        assert wav.getnframes() == 240
    assert result["mime"] == "audio/wav"
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_success_status_json_is_not_mislabeled_as_speech():
    provider = service(lambda _: httpx.Response(200, json={"error": "private"}))
    with pytest.raises(ProviderError, match="speech_response_is_not_audio"):
        await provider.synthesize({"provider": "openai", "model": "speech", "voice": "alloy"}, "hello")
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_network_error_does_not_echo_request_url_or_key():
    def handler(request):
        raise httpx.ConnectError("secret-test-key https://private-host", request=request)

    provider = service(handler)
    with pytest.raises(ProviderError) as error:
        await provider.transcribe({"provider": "openai", "model": "whisper"}, b"\0\0" * 16)
    assert str(error.value) == "openai: network_error"
    await provider._client.aclose()


@pytest.mark.asyncio
async def test_local_model_download_is_only_permitted_by_explicit_warmup(monkeypatch, tmp_path):
    flags = []
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "tokenizer.json").write_text("{}")

    def download(model, cache_dir, local_files_only):
        flags.append(local_files_only)
        if local_files_only:
            raise FileNotFoundError("Not cached")
        return str(model_dir)

    def fake_model(path, **kwargs):
        assert kwargs["local_files_only"] is True
        assert kwargs["compute_type"] == "int8"
        assert kwargs["device"] == "cpu"
        return object()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=fake_model))
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", types.SimpleNamespace(download_model=download))
    provider = service(lambda _: pytest.fail("No network should be used by the HTTP provider"))
    config = {"provider": "faster_whisper", "model": "tiny", "cache_dir": str(tmp_path)}
    with pytest.raises(ProviderError, match="local_model_unavailable_run_warmup"):
        await provider.transcribe(config, b"\0\0" * 160)
    assert flags == [True]
    assert await provider.warmup(config) == {"ready": True, "model": "tiny"}
    assert flags == [True, False]
    await provider.close()
    await provider._client.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32" or os.getenv("GREATSAGE_SAPI_TESTS") != "1",
                    reason="explicit Windows SAPI test opt-in required")
async def test_installed_system_voice_creates_wav_without_playback():
    provider = Providers()
    try:
        voices = await provider.voices({"provider": "system"})
        assert voices
        selected = next((v for v in voices if v["language"].startswith("zh")), voices[0])
        result = await provider.synthesize({"provider": "system", "voice": selected["id"]},
                                           "你好，这是语音合成测试。", selected["language"])
        assert result["mime"] == "audio/wav"
        with wave.open(io.BytesIO(result["audio"]), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getframerate() == 16000
            assert wav.getnframes() > 1000
        print({"voice_language": selected["language"], "wav_bytes": len(result["audio"]),
               "voice_count": len(voices)})
    finally:
        await provider.close()
