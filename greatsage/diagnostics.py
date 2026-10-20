"""Offline, metadata-only checks of the installed or frozen voice runtime."""
from __future__ import annotations

import asyncio
from importlib.metadata import PackageNotFoundError, version
import sys
import time

from . import __version__


async def check_voice_runtime() -> dict:
    """Load lazy SDK/native imports and pass synthetic silence through Pipecat.

    No application settings, user data, hardware or authenticated service probe
    is used. Eight seconds cover checks; two seconds are reserved for cleanup.
    """
    started = time.perf_counter()
    report = {
        "ok": False, "code": "voice_runtime_check_failed",
        "versions": {"greatsage": __version__, "python": ".".join(map(str, sys.version_info[:3]))},
        "checks": {"pipeline": "not_run", "deepgram_imports": "not_run", "cleanup": "not_run"},
        "hardware": "not_tested", "network": "not_tested", "recognition": "not_tested",
    }
    pipeline = service = None
    stage = "imports"

    async def ignore_segment(_identity, _event):
        pass

    try:
        async with asyncio.timeout(8):
            for name in ("pipecat-ai", "deepgram-sdk", "soxr", "sentencex", "loudness"):
                report["versions"][name] = version(name)
            from .audio import AudioChunk
            from .voice_pipeline import SourceVoicePipeline, VoiceIdentity

            stage = "pipeline"
            pipeline = SourceVoicePipeline(
                VoiceIdentity("microphone:0", "offline-diagnostic", 0), {}, ignore_segment)
            await pipeline.start()
            if not await pipeline.feed(AudioChunk("microphone:0", b"\0" * 2880)):
                raise RuntimeError("diagnostic_frame_not_accepted")
            await pipeline.drain(timeout=2)
            report["checks"]["pipeline"] = "passed"

            stage = "deepgram_imports"
            from .streaming_asr import create_streaming_asr
            from deepgram.listen.v1.types import ListenV1Finalize

            # An explicit inert key prevents loading any user's environment key.
            # Construction and lazy client access do not call setup/connect.
            service = create_streaming_asr({
                "api_key": "offline-diagnostic-unused", "model": "nova-3", "language": "zh"})
            sdk_v1 = service._client.listen.v1
            if not callable(sdk_v1.connect) or ListenV1Finalize(type="Finalize").type != "Finalize":
                raise RuntimeError("diagnostic_sdk_shape_changed")
            report["checks"]["deepgram_imports"] = "passed"
            report.update(ok=True, code="voice_runtime_ready")
    except (ImportError, PackageNotFoundError):
        report["code"] = "voice_runtime_dependency_unavailable"
    except TimeoutError:
        report["code"] = "voice_runtime_check_timeout"
    except Exception:
        report["code"] = "voice_runtime_check_failed"
    finally:
        report["stage"] = stage
        try:
            async with asyncio.timeout(2):
                if service is not None:
                    await service.cleanup()
                if pipeline is not None:
                    await pipeline.close()
            report["checks"]["cleanup"] = "passed"
        except Exception:
            report.update(ok=False, code="voice_runtime_cleanup_failed", stage="cleanup")
            report["checks"]["cleanup"] = "failed"
    if report["ok"]:
        report["stage"] = "complete"
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return report
