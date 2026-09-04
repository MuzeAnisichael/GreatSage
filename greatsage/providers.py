"""Async chat, transcription and speech adapters with explicit provider selection.

Provider credentials are supplied in config['api_key'] by the configuration layer.
No response body, request payload, URL or credential is included in ProviderError.
Local ASR downloads happen only through an explicit warmup() call.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import locale
import math
import re
import sys
import tempfile
import threading
import time
import wave
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class ProviderError(RuntimeError):
    """Safe-to-log provider failure; never includes upstream free-form messages."""
    def __init__(self, provider: str, code: str, status: int | None = None) -> None:
        self.provider, self.code, self.status = provider, code, status
        suffix = f" HTTP {status}" if status is not None else ""
        super().__init__(f"{provider}:{suffix} {code}")


_DEFAULT_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "http://127.0.0.1:1234/v1",
    "ollama": "http://127.0.0.1:11434",
}
_USAGE_FIELDS = {
    "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
    "cached_tokens", "cache_write_tokens", "cache_read_tokens", "reasoning_tokens",
    "audio_tokens", "text_tokens", "image_tokens", "video_tokens", "cost", "seconds",
    "input_audio_seconds", "output_audio_seconds", "input_audio_duration", "audio_duration",
    "duration", "characters", "prompt_tokens_details", "completion_tokens_details",
    "input_tokens_details", "output_tokens_details", "cost_details", "upstream_inference_cost",
    "upstream_inference_prompt_cost", "upstream_inference_completions_cost",
}
_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac",
         "opus": "audio/ogg", "aac": "audio/aac", "pcm": "audio/pcm"}


def _provider(config: dict) -> str:
    value = config.get("provider", "openrouter")
    if value in ("faster-whisper", "faster_whisper", "local"):
        return "faster_whisper"
    if value in (*_DEFAULT_URLS, "system"):
        return value
    raise ProviderError("provider", "unsupported_provider")


def _number(config: dict, key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(config.get(key, default))
    except (ValueError, TypeError):
        raise ProviderError("configuration", f"invalid_{key}") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ProviderError("configuration", f"invalid_{key}")
    return value


def _language(value: str | None) -> str | None:
    if not value or value.casefold() in ("auto", "automatic"):
        return None
    return value.replace("_", "-").split("-")[0].lower()


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 192000:
        raise ProviderError("audio", "invalid_sample_rate")
    if len(pcm) % 2:
        raise ProviderError("audio", "unaligned_pcm16")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return output.getvalue()


def _usage(value) -> dict:
    """Retain numeric metering fields, excluding arbitrary upstream strings."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in _USAGE_FIELDS & value.keys():
        item = value[key]
        if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
            result[key] = item
        elif isinstance(item, dict):
            result[key] = _usage(item)
    return result


def _generation_usage(response: httpx.Response) -> dict:
    identifier = response.headers.get("x-generation-id", "")
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,180}", identifier):
        return {"generation_id": identifier}
    return {}


async def _sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    data: list[str] = []
    event = "message"
    size = 0
    async for line in response.aiter_lines():
        if len(line) > 1_000_000:
            raise ProviderError("stream", "event_too_large")
        if not line:
            if data:
                yield event, "\n".join(data)
            data, event, size = [], "message", 0
        elif line.startswith("data:"):
            value = line[5:]
            value = value[1:] if value.startswith(" ") else value
            data.append(value)
            size += len(value)
            if size > 1_000_000:
                raise ProviderError("stream", "event_too_large")
        elif line.startswith("event:"):
            event = line[6:].strip()
        # SSE comments, IDs and retry hints do not carry answer text.
    if data:
        yield event, "\n".join(data)


def _json(value: str | bytes, provider: str) -> dict:
    try:
        result = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderError(provider, "invalid_json_response") from None
    if not isinstance(result, dict):
        raise ProviderError(provider, "invalid_response_shape")
    if result.get("error"):
        raise ProviderError(provider, "upstream_error")
    return result


class Providers:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._local_executor: ThreadPoolExecutor | None = None
        self._models: dict[tuple, object] = {}
        self._closed = False

    def _request(self, config: dict, endpoint: str) -> tuple[str, dict, httpx.Timeout]:
        if self._closed:
            raise ProviderError("provider", "closed")
        provider = _provider(config)
        if provider not in _DEFAULT_URLS:
            raise ProviderError(provider, "operation_not_supported")
        base = str(config.get("base_url") or _DEFAULT_URLS[provider]).rstrip("/")
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment)
            httpx.URL(base)
        except (ValueError, httpx.InvalidURL):
            valid = False
        if not valid:
            raise ProviderError(provider, "invalid_base_url")
        if provider == "ollama" and base.endswith("/api") and endpoint.startswith("/api/"):
            endpoint = endpoint[4:]
        headers = {"Accept": "application/json"}
        key = config.get("api_key")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        elif provider == "openrouter":
            raise ProviderError(provider, "api_key_missing")
        timeout = _number(config, "timeout_seconds", 45, 0.1, 600)
        return base + endpoint, headers, httpx.Timeout(timeout, connect=min(10, timeout), pool=10)

    @staticmethod
    def _status(response: httpx.Response, provider: str) -> None:
        if response.is_success:
            return
        code = {
            400: "invalid_request", 401: "authentication_failed", 402: "insufficient_credit",
            403: "access_denied", 404: "model_or_endpoint_not_found", 408: "timeout",
            413: "audio_or_request_too_large", 422: "invalid_request", 429: "rate_limited",
        }.get(response.status_code, "upstream_unavailable" if response.status_code >= 500 else "http_error")
        raise ProviderError(provider, code, response.status_code)

    async def stream_chat(self, config: dict, messages: list) -> AsyncIterator[dict]:
        provider = _provider(config)
        if provider not in _DEFAULT_URLS:
            raise ProviderError(provider, "chat_not_supported")
        if not config.get("model"):
            raise ProviderError(provider, "model_missing")
        ollama = provider == "ollama"
        endpoint = "/api/chat" if ollama else "/chat/completions"
        url, headers, timeout = self._request(config, endpoint)
        payload = {"model": config["model"], "messages": messages, "stream": True}
        limit = config.get("max_tokens", config.get("max_output_tokens"))
        if ollama:
            payload["think"] = False
            options = {}
            if "temperature" in config:
                options["temperature"] = config["temperature"]
            if limit is not None:
                options["num_predict"] = limit
            if options:
                payload["options"] = options
        else:
            if config.get("include_usage", True):
                payload["stream_options"] = {"include_usage": True}
            if "temperature" in config:
                payload["temperature"] = config["temperature"]
            if limit is not None:
                payload["max_tokens"] = limit
        total_timeout = _number(config, "total_timeout_seconds", 120, 0.1, 600)
        finished = False
        try:
            async with asyncio.timeout(total_timeout):
                async with self._client.stream("POST", url, headers=headers, json=payload,
                                               timeout=timeout) as response:
                    self._status(response, provider)
                    if ollama:
                        async for line in response.aiter_lines():
                            if not line.strip():
                                continue
                            if len(line) > 1_000_000:
                                raise ProviderError(provider, "event_too_large")
                            item = _json(line, provider)
                            message = item.get("message", {})
                            if not isinstance(message, dict):
                                raise ProviderError(provider, "invalid_response_shape")
                            text = message.get("content")
                            if isinstance(text, str) and text:
                                yield {"text": text}
                            if item.get("done"):
                                finished = True
                                usage = {}
                                for source, destination in (
                                    ("prompt_eval_count", "prompt_tokens"),
                                    ("eval_count", "completion_tokens"),
                                ):
                                    if isinstance(item.get(source), int):
                                        usage[destination] = item[source]
                                if usage:
                                    usage["total_tokens"] = sum(usage.values())
                                yield {"usage": usage}
                                break
                    else:
                        async for event, data in _sse_events(response):
                            if event == "error":
                                raise ProviderError(provider, "upstream_stream_error")
                            if data.strip() == "[DONE]":
                                finished = True
                                break
                            if event in ("ping", "heartbeat"):
                                continue
                            item = _json(data, provider)
                            usage = _usage(item.get("usage"))
                            if usage:
                                usage.update(_generation_usage(response))
                                yield {"usage": usage}
                            choices = item.get("choices", [])
                            if not isinstance(choices, list):
                                raise ProviderError(provider, "invalid_response_shape")
                            for choice in choices:
                                if not isinstance(choice, dict):
                                    raise ProviderError(provider, "invalid_response_shape")
                                if choice.get("index", 0) != 0:
                                    continue
                                reason = choice.get("finish_reason")
                                if reason in ("error", "content_filter"):
                                    raise ProviderError(provider, "generation_not_completed")
                                if reason is not None:
                                    finished = True
                                delta = choice.get("delta") or {}
                                if not isinstance(delta, dict):
                                    raise ProviderError(provider, "invalid_response_shape")
                                text = delta.get("content")
                                if isinstance(text, str) and text:
                                    yield {"text": text}
                    if not finished:
                        raise ProviderError(provider, "stream_ended_before_completion")
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError(provider, "timeout") from None
        except httpx.HTTPError:
            raise ProviderError(provider, "network_error") from None

    async def transcribe(self, config: dict, pcm: bytes, sample_rate: int = 16000) -> dict:
        provider = _provider(config)
        if not pcm:
            return {"text": "", "usage": {"input_audio_seconds": 0}}
        wav = _wav(pcm, sample_rate)
        if len(wav) > 25 * 1024 * 1024:
            raise ProviderError(provider, "audio_too_large_split_into_segments")
        if provider == "faster_whisper":
            return await self._local_call(self._local_transcribe, config, wav,
                                          timeout=_number(config, "timeout_seconds", 90, 0.1, 600))
        if provider not in ("openrouter", "openai", "openai_compatible"):
            raise ProviderError(provider, "transcription_not_supported")
        if not config.get("model"):
            raise ProviderError(provider, "model_missing")
        url, headers, timeout = self._request(config, "/audio/transcriptions")
        language = _language(config.get("language"))
        try:
            async with asyncio.timeout(_number(config, "total_timeout_seconds", 65, 0.1, 600)):
                if provider == "openrouter":
                    payload = {"model": config["model"], "input_audio": {
                        "data": base64.b64encode(wav).decode("ascii"), "format": "wav"}}
                    if language:
                        payload["language"] = language
                    response = await self._client.post(url, headers=headers, json=payload, timeout=timeout)
                else:
                    fields = {"model": config["model"], "response_format": "json"}
                    if language:
                        fields["language"] = language
                    response = await self._client.post(url, headers=headers, data=fields,
                                                      files={"file": ("segment.wav", wav, "audio/wav")},
                                                      timeout=timeout)
                self._status(response, provider)
                data = _json(response.content, provider)
                if not isinstance(data.get("text"), str):
                    raise ProviderError(provider, "transcription_text_missing")
                usage = _usage(data.get("usage"))
                usage.update(_generation_usage(response))
                usage.setdefault("input_audio_seconds", len(pcm) / (sample_rate * 2))
                return {"text": data["text"].strip(), "usage": usage}
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError(provider, "timeout") from None
        except httpx.HTTPError:
            raise ProviderError(provider, "network_error") from None

    async def synthesize(self, config: dict, text: str, language: str = "zh-CN") -> dict:
        provider = _provider(config)
        if not text.strip():
            raise ProviderError(provider, "speech_text_empty")
        if provider == "system":
            cancelled = threading.Event()
            try:
                return await asyncio.to_thread(self._system_synthesize, config, text, language, cancelled)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        if provider not in ("openrouter", "openai", "openai_compatible"):
            raise ProviderError(provider, "speech_not_supported")
        if not config.get("model"):
            raise ProviderError(provider, "model_missing")
        fmt = config.get("response_format", "mp3")
        if fmt not in _MIME or (provider == "openrouter" and fmt not in ("mp3", "pcm")):
            raise ProviderError(provider, "unsupported_speech_format")
        if fmt == "pcm":
            if config.get("pcm_sample_rate") is None:
                raise ProviderError(provider, "pcm_sample_rate_required")
            _wav(b"", config["pcm_sample_rate"])
        url, headers, timeout = self._request(config, "/audio/speech")
        payload = {"model": config["model"], "input": text, "response_format": fmt}
        if config.get("voice"):
            payload["voice"] = config["voice"]
        else:
            raise ProviderError(provider, "voice_missing")
        for key in ("speed", "instructions"):
            if config.get(key) is not None:
                payload[key] = config[key]
        audio = bytearray()
        try:
            async with asyncio.timeout(_number(config, "total_timeout_seconds", 90, 0.1, 600)):
                async with self._client.stream("POST", url, headers=headers, json=payload,
                                               timeout=timeout) as response:
                    self._status(response, provider)
                    mime = response.headers.get("content-type", "").split(";")[0].lower()
                    if mime and mime not in (*_MIME.values(), "application/octet-stream", "audio/x-wav"):
                        raise ProviderError(provider, "speech_response_is_not_audio")
                    accepted = {"", "application/octet-stream", _MIME[fmt]}
                    if fmt == "wav":
                        accepted.add("audio/x-wav")
                    if mime not in accepted:
                        raise ProviderError(provider, "speech_format_mismatch")
                    async for part in response.aiter_bytes():
                        audio.extend(part)
                        if len(audio) > 32 * 1024 * 1024:
                            raise ProviderError(provider, "speech_response_too_large")
                    usage = {"characters": len(text), **_generation_usage(response)}
            if not audio:
                raise ProviderError(provider, "speech_audio_empty")
            if fmt == "pcm":
                # Raw PCM lacks its sample rate. Do not silently assume one for
                # arbitrary providers; configure it using that model's contract.
                rate = config["pcm_sample_rate"]
                return {"audio": _wav(bytes(audio), rate), "mime": "audio/wav", "usage": usage}
            return {"audio": bytes(audio), "mime": _MIME[fmt], "usage": usage}
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError(provider, "timeout") from None
        except httpx.HTTPError:
            raise ProviderError(provider, "network_error") from None

    async def _local_call(self, function, *args, timeout: float):
        if self._closed:
            raise ProviderError("faster_whisper", "closed")
        if self._local_executor is None:
            self._local_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="greatsage-asr")
        future = asyncio.get_running_loop().run_in_executor(self._local_executor, function, *args)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise ProviderError("faster_whisper", "local_inference_timeout") from None

    def _local_model(self, config: dict, allow_download: bool = False):
        model = config.get("model") or "small"
        cache_dir = str(Path(config.get("cache_dir") or ".runtime/models").resolve())
        device = config.get("device", "cpu")
        compute = config.get("compute_type", "int8" if device == "cpu" else "float16")
        threads = int(config.get("cpu_threads", 4))
        key = (model, cache_dir, device, compute, threads)
        if key in self._models:
            return self._models[key]
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
        except ImportError:
            raise ProviderError("faster_whisper", "install_local_dependencies") from None
        try:
            path = Path(model)
            if not path.is_dir():
                path = Path(download_model(model, cache_dir=cache_dir, local_files_only=not allow_download))
            # WhisperModel otherwise downloads a fallback tokenizer even with
            # local_files_only=True. Reject incomplete caches before constructing.
            if not (path / "tokenizer.json").is_file():
                raise ProviderError("faster_whisper", "local_model_tokenizer_missing_run_warmup")
            loaded = WhisperModel(str(path), device=device, compute_type=compute, cpu_threads=threads,
                                  download_root=cache_dir, local_files_only=True)
        except ProviderError:
            raise
        except Exception:
            code = "model_download_or_load_failed" if allow_download else "local_model_unavailable_run_warmup"
            raise ProviderError("faster_whisper", code) from None
        self._models[key] = loaded
        return loaded

    def _local_transcribe(self, config: dict, wav: bytes) -> dict:
        model = self._local_model(config)
        try:
            segments, info = model.transcribe(io.BytesIO(wav), language=_language(config.get("language")),
                                              beam_size=int(config.get("beam_size", 3)),
                                              condition_on_previous_text=False, vad_filter=False)
            text = "".join(segment.text for segment in segments).strip()
            return {"text": text, "usage": {"input_audio_seconds": float(info.duration)},
                    "language": info.language}
        except Exception:
            raise ProviderError("faster_whisper", "local_transcription_failed") from None

    async def warmup(self, config: dict) -> dict:
        """Explicitly authorize downloading/loading the selected local ASR model.

        This method is never called automatically by transcription or startup.
        Cancellation stops awaiting native ASR work; running CTranslate2 inference
        itself finishes on its worker. Pending work is cancelled on close().
        """
        if _provider(config) != "faster_whisper":
            raise ProviderError(_provider(config), "warmup_only_supports_local_asr")
        await self._local_call(self._local_model, config, True,
                               timeout=_number(config, "warmup_timeout_seconds", 600, 1, 1800))
        return {"ready": True, "model": config.get("model") or "small"}

    async def load_local(self, config: dict) -> dict:
        """Prepare an already cached local ASR model without downloading files."""
        if _provider(config) != "faster_whisper":
            raise ProviderError(_provider(config), "load_only_supports_local_asr")
        await self._local_call(self._local_model, config, False,
                               timeout=_number(config, "timeout_seconds", 90, 0.1, 600))
        return {"ready": True, "model": config.get("model") or "small"}

    @staticmethod
    def _voice_info(token) -> dict:
        codes = token.GetAttribute("Language").split(";")
        languages = []
        for code in codes:
            try:
                value = locale.windows_locale.get(int(code, 16), "")
                if value:
                    languages.append(value.replace("_", "-"))
            except ValueError:
                pass
        return {"id": token.Id, "name": token.GetDescription(),
                "language": languages[0] if languages else "", "languages": languages}

    @classmethod
    def _system_voices(cls) -> list:
        if sys.platform != "win32":
            raise ProviderError("system", "windows_required")
        try:
            import pythoncom
            import win32com.client
        except ImportError:
            raise ProviderError("system", "install_local_dependencies") from None
        pythoncom.CoInitialize()
        voice = None
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            return [cls._voice_info(token) for token in voice.GetVoices()]
        except Exception:
            raise ProviderError("system", "voice_enumeration_failed") from None
        finally:
            voice = None
            pythoncom.CoUninitialize()

    async def voices(self, config: dict) -> list:
        if _provider(config) != "system":
            # Cloud voice catalogs are model-specific; do not fabricate voices.
            return []
        return await asyncio.to_thread(self._system_voices)

    @classmethod
    def _system_synthesize(cls, config: dict, text: str, language: str,
                           cancelled: threading.Event) -> dict:
        if sys.platform != "win32":
            raise ProviderError("system", "windows_required")
        try:
            import pythoncom
            import win32com.client
        except ImportError:
            raise ProviderError("system", "install_local_dependencies") from None
        deadline = time.monotonic() + _number(config, "timeout_seconds", 30, 0.1, 180)
        pythoncom.CoInitialize()
        voice = stream = selected = tokens = None
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            tokens = list(voice.GetVoices())
            requested = config.get("voice")
            target_language = language.replace("_", "-").lower()
            if requested:
                selected = next((token for token in tokens
                                 if token.Id == requested or token.GetDescription() == requested), None)
                if selected is None:
                    raise ProviderError("system", "configured_voice_not_installed")
            else:
                selected = next((token for token in tokens if any(
                    item.lower() == target_language for item in cls._voice_info(token)["languages"])), None)
                if selected is None:
                    selected = next((token for token in tokens if any(
                        _language(item) == _language(language)
                        for item in cls._voice_info(token)["languages"])), None)
                if selected is None:
                    raise ProviderError("system", "requested_language_voice_not_installed")
            voice.Voice = selected
            voice.Rate = int(_number(config, "rate", 0, -10, 10))
            with tempfile.TemporaryDirectory(prefix="greatsage-speech-") as folder:
                output = Path(folder) / "speech.wav"
                stream = win32com.client.Dispatch("SAPI.SpFileStream")
                stream.Format.Type = 18  # SAFT16kHz16BitMono
                stream.Open(str(output), 3, False)  # SSFMCreateForWrite
                try:
                    voice.AudioOutputStream = stream
                    voice.Speak(text, 17)  # async + plain text, never interpret SSML
                    while not voice.WaitUntilDone(100):
                        if cancelled.is_set() or time.monotonic() >= deadline:
                            voice.Speak("", 2)  # purge only; still routed to the file
                            raise ProviderError("system", "speech_cancelled" if cancelled.is_set() else "timeout")
                finally:
                    stream.Close()
                audio = output.read_bytes()
            return {"audio": audio, "mime": "audio/wav", "usage": {"characters": len(text)},
                    "voice": cls._voice_info(selected)}
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("system", "speech_synthesis_failed") from None
        finally:
            voice = stream = selected = tokens = None
            pythoncom.CoUninitialize()

    async def close(self) -> None:
        self._closed = True
        if self._owns_client:
            await self._client.aclose()
        if self._local_executor:
            self._local_executor.shutdown(wait=False, cancel_futures=True)
        self._models.clear()
