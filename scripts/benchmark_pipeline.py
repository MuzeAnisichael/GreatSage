"""Synthetic, paced Runtime benchmark. No hardware capture or speaker playback.

Only the fixed fixture, its transcript, and allowlisted measurements are printed. Runtime data is
isolated in a new run directory; source settings and credentials are read-only.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from greatsage.audio import AudioChunk
from greatsage.echo import decode_reference
from greatsage.providers import ProviderError, Providers
from greatsage.runtime import Runtime
from greatsage.segmentation import Segmenter
from greatsage.settings import SettingsStore


FIXTURE_TEXT = "请用一句话回答一加一等于几？"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def safe_error(error: BaseException) -> dict:
    if isinstance(error, ProviderError):
        return {"type": "ProviderError", "provider": error.provider, "code": error.code,
                "http_status": error.status}
    return {"type": type(error).__name__, "code": "timeout" if isinstance(error, TimeoutError) else "benchmark_error"}


def normalized(text: str) -> str:
    return re.sub(r"\W", "", text).casefold()


def persist_result(path: Path, result: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class PacedCapture:
    """Capture-compatible synthetic source, scheduled against a monotonic clock."""

    def __init__(self, pcm: bytes, packet_ms: int, tail_ms: int):
        self.pcm = pcm + b"\0" * (tail_ms * 32)
        self.packet_bytes = packet_ms * 32
        self.stopped = threading.Event()
        self.callback = None
        self.started_wall = self.started_perf = None
        self.max_schedule_lag_ms = 0.0
        self.packet_count = 0

    def start(self, config, on_chunk, on_error):
        self.callback = on_chunk
        self.stopped.clear()

    def stop(self):
        self.stopped.set()

    async def feed(self):
        self.started_perf, self.started_wall = time.perf_counter(), time.time()
        for offset in range(0, len(self.pcm), self.packet_bytes):
            if self.stopped.is_set():
                break
            packet = self.pcm[offset:offset + self.packet_bytes]
            end_offset = (offset + len(packet)) / 32000
            target = self.started_perf + end_offset
            # Windows asyncio timers may wake before a perf_counter deadline.
            # Recheck so scheduled speech timestamps never run ahead of delivery.
            while (remaining := target - time.perf_counter()) > 0:
                await asyncio.sleep(remaining)
            self.max_schedule_lag_ms = max(self.max_schedule_lag_ms, (time.perf_counter() - target) * 1000)
            self.callback(AudioChunk("microphone:synthetic", packet, timestamp=self.started_wall + end_offset))
            self.packet_count += 1


class TimedSegmenter:
    def __init__(self, config: dict, factory=Segmenter):
        self.segmenter = factory(config["endpoint_silence_ms"], config["min_speech_ms"],
                                 config["max_utterance_seconds"], config["partial_interval_seconds"])
        self.endpoints = []

    @property
    def active(self):
        return self.segmenter.active

    def feed(self, pcm, timestamp):
        events = self.segmenter.feed(pcm, timestamp)
        detected = time.perf_counter()
        for event in events:
            if event.kind == "final":
                self.endpoints.append({"speech_end_wall": event.speech_end, "detected_perf": detected,
                                       "continued": event.continued,
                                       "pcm_hash": hashlib.sha256(event.pcm).hexdigest()})
        return events


class ObservedProviders:
    """Record boundary times without changing or inspecting provider payloads."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.asr_calls = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def transcribe(self, config, pcm, sample_rate=16000):
        call = {"start_perf": time.perf_counter(), "pcm_hash": hashlib.sha256(pcm).hexdigest(),
                "audio_seconds": len(pcm) / (sample_rate * 2), "status": "running"}
        self.asr_calls.append(call)
        try:
            result = await self.delegate.transcribe(config, pcm, sample_rate)
            call["status"] = "ok"
            return result
        except asyncio.CancelledError:
            call["status"] = "cancelled"
            raise
        except Exception as error:
            call.update(status="error", error=safe_error(error))
            raise
        finally:
            call["end_perf"] = time.perf_counter()


class CredentialSettings:
    """Use isolated configuration but resolve saved source credentials in memory."""

    def __init__(self, isolated: SettingsStore, source: SettingsStore | None, changed_envs: set[str]):
        self.isolated, self.source, self.changed_envs = isolated, source, changed_envs

    def __getattr__(self, name):
        return getattr(self.isolated, name)

    def provider(self, component):
        config = self.isolated.provider(component)
        if self.source is not None and component not in self.changed_envs:
            original = self.source.raw()[component]
            if (config["provider"] == original["provider"]
                    and config.get("api_key_env") == original.get("api_key_env")):
                config["api_key"] = self.source.provider(component)["api_key"]
        return config


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings-dir", type=Path, default=PROJECT_ROOT / ".runtime")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / ".runtime" / "benchmark")
    parser.add_argument("--packet-ms", type=int, choices=(20, 30), default=30)
    parser.add_argument("--timeout-seconds", type=float, default=90)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--fixture-voice", default="")
    parser.add_argument("--partial-interval", type=float)
    parser.add_argument("--dry-run", action="store_true", help="Validate and write the plan without model, capture or speech calls")
    choices = {"asr": ("openai", "openrouter", "faster_whisper"),
               "llm": ("openai", "openrouter", "ollama"), "tts": ("openai", "openrouter", "system")}
    for component in ("asr", "llm", "tts"):
        parser.add_argument(f"--{component}-provider", choices=choices[component])
        parser.add_argument(f"--{component}-model")
        parser.add_argument(f"--{component}-base-url")
        parser.add_argument(f"--{component}-key-env")
    parser.add_argument("--tts-voice")
    return parser


def prepare(args):
    destination, source_dir = args.data_dir.resolve(), args.settings_dir.resolve()
    if destination == source_dir or source_dir.is_relative_to(destination):
        raise ValueError("Benchmark data directory must be separate from source settings")
    if (destination / "memory.sqlite3").exists() or (destination / "settings.json").exists():
        raise ValueError("Benchmark root cannot be an application runtime directory")
    if not math.isfinite(args.timeout_seconds) or not 5 <= args.timeout_seconds <= 180:
        raise ValueError("Benchmark timeout must be between 5 and 180 seconds")
    destination.mkdir(parents=True, exist_ok=True)
    run_dir = destination / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    run_dir.mkdir()
    isolated = SettingsStore(run_dir)
    source = SettingsStore(source_dir) if (source_dir / "settings.json").exists() else None
    config = source.raw() if source else isolated.raw()
    config.update(mode="conversation", global_prompt="你是 GreatSage 合成基准助手。仅用一句简短中文回答测试问题。",
                  output_language="zh-CN", voice_language="zh-CN", voice_enabled=True,
                  allow_proactive=False, microphone=True, microphone_device=None,
                  desktop_source="none", desktop_process_id=None, record_audio=False)
    config["llm"]["max_tokens"] = args.max_tokens
    if args.partial_interval is not None:
        config["partial_interval_seconds"] = args.partial_interval
    changed_envs = set()
    for component in ("asr", "llm", "tts"):
        for field in ("provider", "model", "base_url", "api_key_env"):
            attribute = f"{component}_key_env" if field == "api_key_env" else f"{component}_{field}"
            value = getattr(args, attribute)
            if value is not None:
                config[component][field] = value
                if field == "api_key_env":
                    changed_envs.add(component)
        if config[component]["provider"] == "ollama" and args.llm_provider == "ollama":
            config[component]["base_url"] = args.llm_base_url or "http://127.0.0.1:11434"
            if not args.llm_model:
                raise ValueError("Switching to Ollama requires an explicit installed model name")
    if config["tts"]["provider"] == "system":
        config["tts"].update(model="", voice=args.tts_voice or "", api_key_env="")
    elif args.tts_voice is not None:
        config["tts"]["voice"] = args.tts_voice
    if config["asr"]["provider"] == "faster_whisper" and not config["asr"].get("cache_dir"):
        config["asr"]["cache_dir"] = str(source_dir / "models")
    isolated.update(config)
    return destination, run_dir, CredentialSettings(isolated, source, changed_envs)


async def run(args, *, providers=None, segmenter_factory=Segmenter) -> dict:
    destination, run_dir, settings = prepare(args)
    config = settings.raw()
    result = {
        "schema_version": 1, "status": "planned" if args.dry_run else "running",
        "started_at": datetime.now(timezone.utc).isoformat(), "run_directory": run_dir.name,
        "fixture": {"text": FIXTURE_TEXT, "source": "Windows system TTS", "language": "zh-CN",
                    "played_to_speakers": False},
        "configuration": {name: {"provider": config[name]["provider"], "model": config[name]["model"]}
                          for name in ("asr", "llm", "tts")},
        "parameters": {"packet_ms": args.packet_ms, "max_tokens": config["llm"]["max_tokens"],
                       "context_tokens": config["llm"]["context_tokens"],
                       "partial_interval_seconds": config["partial_interval_seconds"],
                       "configured_endpoint_silence_ms": config["endpoint_silence_ms"],
                       "effective_endpoint_silence_ms": math.ceil(config["endpoint_silence_ms"] / 30) * 30,
                       "timeout_seconds": args.timeout_seconds},
        "scope": {"synthetic_input": True, "hardware_capture": False, "websocket_transport": False,
                  "actual_playback_measured": False, "human_asr_accuracy_measured": False,
                  "timing_origin": "last PCM frame classified as speech by VAD, using the monotonic feed clock",
                  "first_text_boundary": "first nonempty Runtime response_delta received by an in-process subscriber",
                  "audio_ready_boundary": "first Runtime audio event received; synthesis and reference decoding complete, no playback"},
        "metrics": {}, "errors": [],
    }
    result_file = destination / "results.json"
    persist_result(result_file, result)
    if args.dry_run:
        persist_result(run_dir / "results.json", result)
        return result
    base = providers or Providers()
    observed = ObservedProviders(base)
    runtime = None
    monitor_task = None
    try:
        async with asyncio.timeout(args.timeout_seconds):
            preparation_start = time.perf_counter()
            audio = await base.synthesize({"provider": "system", "voice": args.fixture_voice}, FIXTURE_TEXT, "zh-CN")
            fixture_pcm = decode_reference(audio["audio"], audio["mime"])
            if not fixture_pcm or len(fixture_pcm) > 32000 * 30:
                raise ValueError("Synthetic fixture must contain up to 30 seconds of audio")
            (run_dir / "fixture.wav").write_bytes(audio["audio"])
            result["fixture"].update(duration_seconds=round(len(fixture_pcm) / 32000, 4),
                                     pcm_sha256=hashlib.sha256(fixture_pcm).hexdigest(),
                                     synthesis_ms=round((time.perf_counter() - preparation_start) * 1000, 2))
            tail_ms = math.ceil(config["endpoint_silence_ms"] / 30) * 30 + 600
            capture = PacedCapture(fixture_pcm, args.packet_ms, tail_ms)
            runtime = Runtime(run_dir, providers=observed)
            runtime.settings = settings
            runtime.capture = capture
            queue = asyncio.Queue(maxsize=2000)
            runtime.subscribers.add(queue)
            tracker = TimedSegmenter(config, segmenter_factory)
            traces = {}
            changed = asyncio.Event()

            async def monitor():
                while True:
                    event = await queue.get()
                    at, data = time.perf_counter(), event.get("data", {})
                    trace = event.get("trace_id", "")
                    record = traces.setdefault(trace, {})
                    kind = event["kind"]
                    if kind == "user_message":
                        record["speech_end_wall"] = data.get("metadata", {}).get("speech_end")
                        record["transcript"] = str(data.get("text", ""))[:500]
                        record["transcript_normalized_match"] = normalized(data.get("text", "")) == normalized(FIXTURE_TEXT)
                        record["transcript_ready_perf"] = at
                    elif kind == "response_delta" and data.get("text"):
                        record.setdefault("first_text_perf", at)
                    elif kind == "audio":
                        record.setdefault("first_audio_ready_perf", at)
                    elif kind == "response_done":
                        record["complete"] = bool(data.get("complete"))
                        record["done_perf"] = at
                    elif kind == "metrics":
                        component = data.get("component")
                        if component == "asr":
                            record["asr_pipeline_ms"] = data.get("latency_ms")
                        elif component == "llm":
                            record["model_first_text_ms"] = data.get("model_first_text_ms")
                        elif component == "tts":
                            record.setdefault("first_tts_synthesis_ms", data.get("synthesis_ms"))
                    elif kind == "error":
                        # Upstream error text and arbitrary response data stay out.
                        result["errors"].append({"component": str(data.get("component", "unknown")),
                                                  "code": "runtime_stage_error"})
                    changed.set()

            monitor_task = asyncio.create_task(monitor())
            await runtime.start()
            load_start = time.perf_counter()
            await runtime.set_listening(True)
            result["metrics"]["listening_setup_ms"] = round((time.perf_counter() - load_start) * 1000, 2)
            runtime.segments["microphone:synthetic"] = tracker
            await capture.feed()
            target, final_call, record = None, None, None
            while True:
                endpoints = [endpoint for endpoint in tracker.endpoints if not endpoint["continued"]]
                target = endpoints[-1] if endpoints else None
                if target:
                    record = next((value for value in traces.values() if value.get("speech_end_wall") == target["speech_end_wall"]), None)
                    final_call = next((call for call in observed.asr_calls
                                       if call["pcm_hash"] == target["pcm_hash"]
                                       and call["start_perf"] >= target["detected_perf"]
                                       and call["status"] != "cancelled"), None)
                    if record and "done_perf" in record:
                        break
                    if final_call and final_call["status"] == "error":
                        result["errors"].append({"component": "asr", **final_call["error"]})
                        break
                changed.clear()
                try:
                    await asyncio.wait_for(changed.wait(), .2)
                except TimeoutError:
                    pass
            speech_end = capture.started_perf + target["speech_end_wall"] - capture.started_wall
            metrics = result["metrics"]
            metrics.update(vad_detected_utterances=len(tracker.endpoints),
                           vad_speech_end_offset_ms=round((speech_end - capture.started_perf) * 1000, 2),
                           endpoint_detection_ms=round((target["detected_perf"] - speech_end) * 1000, 2),
                           injection_max_schedule_lag_ms=round(capture.max_schedule_lag_ms, 2),
                           injected_packets=capture.packet_count,
                           asr_requests=len(observed.asr_calls),
                           asr_cancelled_requests=sum(call["status"] == "cancelled" for call in observed.asr_calls))
            if final_call:
                metrics["final_asr_queue_ms"] = round((final_call["start_perf"] - target["detected_perf"]) * 1000, 2)
                metrics["final_asr_provider_ms"] = round((final_call["end_perf"] - final_call["start_perf"]) * 1000, 2)
            if record:
                result["recognition"] = {
                    "transcript": record.get("transcript", ""),
                    "punctuation_insensitive_match": record.get("transcript_normalized_match", False),
                    "comparison_note": "Only punctuation and spacing are ignored; inspect script, numeral and lexical differences manually.",
                }
                for field, name in (("transcript_ready_perf", "speech_end_to_transcript_ms"),
                                    ("first_text_perf", "speech_end_to_first_text_ms"),
                                    ("first_audio_ready_perf", "speech_end_to_audio_ready_ms"),
                                    ("done_perf", "speech_end_to_completion_ms")):
                    if field in record:
                        metrics[name] = round((record[field] - speech_end) * 1000, 2)
                for field in ("asr_pipeline_ms", "model_first_text_ms", "first_tts_synthesis_ms", "transcript_normalized_match"):
                    if field in record:
                        metrics[field] = record[field]
            result["status"] = "ok" if record and record.get("complete") and "first_text_perf" in record and "first_audio_ready_perf" in record else "error"
    except Exception as error:
        result["status"] = "error"
        result["errors"].append(safe_error(error))
    finally:
        if monitor_task:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        try:
            if runtime:
                await runtime.close()
            else:
                await base.close()
        except Exception as error:
            result["errors"].append({"component": "cleanup", **safe_error(error)})
            result["status"] = "error"
        persist_result(run_dir / "results.json", result)
        persist_result(result_file, result)
    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    arguments = make_parser().parse_args()
    try:
        output = asyncio.run(run(arguments))
        print(json.dumps(output, ensure_ascii=False, indent=2))
        raise SystemExit(0 if output["status"] in {"planned", "ok"} else 1)
    except Exception as error:
        print(json.dumps({"status": "error", "error": safe_error(error)}, ensure_ascii=False))
        raise SystemExit(1)
