"""Optional Pipecat/Deepgram adapter with explicit credentials and one connection.

Pipecat 1.13 owns frames, VAD finalization and the Deepgram SDK protocol.
This adapter removes its implicit reconnect loop and delivers empty finalize
acknowledgements so GreatSage can commit already received final segments.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from functools import lru_cache

_PROBE_CONNECT_TIMEOUT = 8.0
_PROBE_CLEANUP_TIMEOUT = 2.0


def _configuration(config):
    key = config.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("streaming_asr_credentials_required")
    endpoint = config.get("base_url", "https://api.deepgram.com")
    if not isinstance(endpoint, str) or endpoint.rstrip("/") != "https://api.deepgram.com":
        raise ValueError("streaming_asr_endpoint_not_allowed")
    model = config.get("model", "nova-3")
    if model not in {"nova-3", "nova-3-general"}:
        raise ValueError("streaming_asr_model_not_supported")
    language = config.get("language", "zh")
    aliases = {"zh-CN": "zh", "zh-Hans": "zh", "zh-Hant": "zh-TW",
               "en-US": "en", "en-GB": "en", "ja-JP": "ja"}
    language = aliases.get(language, language)
    if language not in {"zh", "zh-TW", "en", "ja"}:
        raise ValueError("streaming_asr_language_not_supported")
    return key.strip(), model, language


def _quiet_sdk_logging():
    # Frame __str__ and upstream exception messages can contain transcripts or
    # credentials. Keep GreatSage's structured events as the audit surface.
    from loguru import logger

    logger.disable("pipecat")
    logger.disable("deepgram")
    for name in ("pipecat", "deepgram"):
        sdk_logger = logging.getLogger(name)
        sdk_logger.handlers = [logging.NullHandler()]
        sdk_logger.propagate = False
        sdk_logger.setLevel(logging.CRITICAL + 1)
    for name, sdk_logger in tuple(logging.Logger.manager.loggerDict.items()):
        if isinstance(sdk_logger, logging.Logger) and name.startswith(("pipecat.", "deepgram.")):
            sdk_logger.handlers = [logging.NullHandler()]
            sdk_logger.propagate = False
            sdk_logger.setLevel(logging.CRITICAL + 1)


@lru_cache(maxsize=1)
def _service_type():
    _quiet_sdk_logging()
    from deepgram.core.events import EventType
    from deepgram.listen.v1.types import (
        ListenV1CloseStream, ListenV1ConfigureFailure, ListenV1Error, ListenV1KeepAlive, ListenV1Results,
    )
    from pipecat.frames.frames import ErrorFrame, InterimTranscriptionFrame, TranscriptionFrame
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.transcriptions.language import Language
    from pipecat.utils.time import time_now_iso8601

    class SafeDeepgramSTTService(DeepgramSTTService):
        def __init__(self, key, model, language, on_safe_error):
            self._safe_error_callback = on_safe_error
            self._failed_code = None
            self._stopping = False
            self._failure_event = asyncio.Event()
            super().__init__(
                api_key=key,
                # The sole allowed host is the SDK default: avoid its custom-URL
                # exception fallback and never provide a proxy or callback URL.
                sample_rate=16000,
                encoding="linear16",
                channels=1,
                multichannel=False,
                mip_opt_out=True,
                audio_passthrough=False,
                settings=self.Settings(
                    # Finalize may have no ACK if all audio was already final.
                    # SDK pause endpoints complement the local, still-required gate.
                    model=model, language=Language(language), endpointing=300,
                    interim_results=True, punctuate=True, smart_format=False,
                ),
            )

        async def _fail(self, code):
            if self._failed_code or self._stopping:
                return
            self._failed_code = code
            self._failure_event.set()
            # Never attach an upstream exception: tracebacks and response bodies
            # are outside the application's metadata-only error contract.
            await super().push_error_frame(
                ErrorFrame(error=code), force_treat_as_permanent=True,
            )
            if self._safe_error_callback:
                try:
                    result = self._safe_error_callback(code)
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # The callback cannot expose its own free-form error either.

        async def push_error(self, error_msg, exception=None, **kwargs):
            await self._fail("streaming_asr_failed")

        async def push_error_frame(self, error, **kwargs):
            await self._fail("streaming_asr_failed")

        async def _on_error(self, error):
            await self._fail("streaming_asr_transport_failed")

        async def _connect(self):
            if self._failed_code or self._stopping:
                return
            if self._connection_task is None:
                await super()._connect()
            else:
                await self._connection_settled.wait()

        async def _connection_handler(self):
            # DeepgramSTTService does not use WebsocketService's reconnect flag.
            # Replace only its retry loop; transport and wire messages stay SDK-owned.
            tasks = []
            connected = False
            try:
                async with self._client.listen.v1.connect(**self._build_connect_kwargs()) as connection:
                    if self._stopping:
                        return
                    connected = True
                    self._connection = connection
                    connection.on(EventType.MESSAGE, self._on_message)
                    connection.on(EventType.ERROR, self._on_error)
                    self._connection_settled.set()
                    await self._call_event_handler("on_connected")
                    listener = self.create_task(connection.start_listening())
                    failure = self.create_task(self._failure_event.wait())
                    keepalive = self.create_task(self._keepalive_handler())
                    tasks = [listener, failure, keepalive]
                    done, _ = await asyncio.wait((listener, failure), return_when=asyncio.FIRST_COMPLETED)
                    if listener in done:
                        await listener
                        await self._fail("streaming_asr_connection_closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._fail("streaming_asr_transport_failed" if connected else "streaming_asr_connection_failed")
            finally:
                self._connection = None
                self._connection_settled.set()
                for task in tasks:
                    if not task.done():
                        await self.cancel_task(task)

        async def _keepalive_handler(self):
            while not self._stopping and not self._failed_code:
                await asyncio.sleep(5)
                if self._connection:
                    try:
                        await self._connection.send_keep_alive(ListenV1KeepAlive(type="KeepAlive"))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await self._fail("streaming_asr_transport_failed")
                        return

        async def run_stt(self, audio):
            if not self._failed_code and not self._stopping:
                if self._connection is None:
                    await self._fail("streaming_asr_connection_unavailable")
                else:
                    try:
                        await self._connection.send_media(audio)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await self._fail("streaming_asr_transport_failed")
            yield None

        async def _on_message(self, message):
            if self._failed_code or self._stopping:
                return
            if isinstance(message, (ListenV1Error, ListenV1ConfigureFailure)):
                await self._fail("streaming_asr_transport_failed")
                return
            if not isinstance(message, ListenV1Results):
                return
            alternatives = message.channel.alternatives if message.channel else []
            text = alternatives[0].transcript if alternatives else ""
            language = Language(self._settings.language)
            if message.is_final:
                from_finalize = bool(getattr(message, "from_finalize", False))
                if from_finalize:
                    self.confirm_finalize()
                if text or from_finalize or getattr(message, "speech_final", False):
                    await self.emit_stt_usage_metrics()
                    await self.push_frame(TranscriptionFrame(
                        text, self._user_id, time_now_iso8601(), language, result=message,
                    ))
            elif text:
                await self.push_frame(InterimTranscriptionFrame(
                    text, self._user_id, time_now_iso8601(), language, result=message,
                ))

        async def _request_reconnect(self):
            await self._fail("streaming_asr_restart_required")

        async def _reconnect(self):
            await self._fail("streaming_asr_restart_required")

        async def _do_reconnect(self):
            await self._fail("streaming_asr_restart_required")

        async def process_frame(self, frame, direction):
            try:
                await super().process_frame(frame, direction)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._fail("streaming_asr_processing_failed")

        async def _disconnect(self):
            self._stopping = True
            connection, task = self._connection, self._connection_task
            self._connection = None
            try:
                if connection:
                    await asyncio.wait_for(
                        connection.send_close_stream(ListenV1CloseStream(type="CloseStream")),
                        timeout=1.0,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Intentional shutdown cannot surface an upstream error body.
            finally:
                if task and task is not asyncio.current_task():
                    await self.cancel_task(task, timeout=1.0)
                self._connection_task = None
                self._connection_settled.set()

        async def stop(self, frame):
            self._stopping = True
            await super().stop(frame)

        async def cancel(self, frame):
            self._stopping = True
            await super().cancel(frame)

    return SafeDeepgramSTTService


def create_streaming_asr(config, on_safe_error=None):
    """Create an unconnected service; callback receives only a stable error code.

    Imports are lazy so ordinary API/local modes do not load the optional SDK.
    Configuration never falls back to another provider's key or language.
    """
    key, model, language = _configuration(config)
    return _service_type()(key, model, language, on_safe_error)


async def probe_streaming_connection(config):
    """Verify one authenticated SDK WebSocket handshake without sending audio.

    The ten-second budget reserves eight seconds for setup and two for cleanup.
    Object construction alone is never success. Cancellation propagates after
    cleanup; settings probes neither open Windows audio nor retry another host.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = {"ok": False, "provider": "deepgram_streaming",
              "code": "streaming_asr_connection_failed", "handshake_ms": None}
    service = None
    try:
        service = create_streaming_asr(config)
        from pipecat.clocks.system_clock import SystemClock
        from pipecat.processors.frame_processor import FrameProcessorSetup
        from pipecat.utils.asyncio.task_manager import TaskManager

        clock = SystemClock()
        clock.start()
        setup = FrameProcessorSetup(
            clock=clock, task_manager=TaskManager(), pipeline_worker=None,
            audio_in_sample_rate=16000, enable_metrics=False,
            enable_usage_metrics=False, enable_tracing=False,
        )
        await asyncio.wait_for(service.setup(setup), timeout=_PROBE_CONNECT_TIMEOUT)
        if service._connection is not None and service._failed_code is None:
            result.update(ok=True, code="streaming_asr_connected",
                          handshake_ms=round((loop.time() - started) * 1000, 1))
        elif service._failed_code:
            result["code"] = service._failed_code
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        result["code"] = "streaming_asr_connection_timeout"
    except (ImportError, ModuleNotFoundError):
        result["code"] = "streaming_asr_dependency_unavailable"
    except ValueError:
        result["code"] = "streaming_asr_configuration_invalid"
    except Exception:
        result["code"] = "streaming_asr_connection_failed"
    finally:
        if service is not None:
            try:
                await asyncio.wait_for(service.cleanup(), timeout=_PROBE_CLEANUP_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                result.update(ok=False, code="streaming_asr_cleanup_failed")
    return result
