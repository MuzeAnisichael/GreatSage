"""Opt-in, bounded checks using synthetic content; safe diagnostics persist on failure."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from greatsage.echo import decode_reference
from greatsage.providers import ProviderError, Providers, _desktop_http_client
from greatsage.settings import SettingsStore

FIXTURE_TEXT = "大贤者已经准备好了。今天是语音功能测试。"


def safe_error(exc):
    """Never expose upstream messages, response bodies, request URLs or headers."""
    if isinstance(exc, ProviderError):
        return {"type": "ProviderError", "provider": exc.provider, "code": exc.code,
                "http_status": exc.status}
    if isinstance(exc, httpx.HTTPStatusError):
        return {"type": "HTTPStatusError", "code": "http_error", "http_status": exc.response.status_code}
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return {"type": "TimeoutError", "code": "timeout", "http_status": None}
    if isinstance(exc, httpx.HTTPError):
        return {"type": "HTTPError", "code": "network_error", "http_status": None}
    return {"type": type(exc).__name__, "code": "local_probe_error", "http_status": None}


async def run(args):
    store = SettingsStore(Path(args.data_dir))
    provider = Providers()
    results = []
    components = set(args.components.split(","))
    if not components <= {"llm", "tts", "asr"}:
        raise ValueError("Unsupported probe component")

    def record(result):
        results.append(result)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    async def stage(kind, metadata, function):
        started = time.perf_counter()
        try:
            result = await function()
            details, value = result if isinstance(result, tuple) else (result, None)
            record({"kind": kind, "status": "ok", **metadata,
                    "latency_ms": round((time.perf_counter() - started) * 1000), **details})
            return value
        except Exception as exc:
            record({"kind": kind, "status": "error", **metadata,
                    "latency_ms": round((time.perf_counter() - started) * 1000), "error": safe_error(exc)})
            return None

    try:
        if args.catalog:
            config = store.provider("llm")
            async with _desktop_http_client() as client:
                for modality in ("transcription", "speech"):
                    async def catalog(modality=modality):
                        response = await client.get(config["base_url"].rstrip("/") + "/models",
                                                    params={"output_modalities": modality},
                                                    headers={"Authorization": "Bearer " + config["api_key"]},
                                                    timeout=60)
                        response.raise_for_status()
                        models = response.json()["data"]
                        return {"models": [{"id": model["id"],
                                            "architecture": model.get("architecture"),
                                            "supported_voices": model.get("supported_voices"),
                                            "pricing": model.get("pricing")}
                                           for model in models]}
                    await stage("catalog", {"modality": modality}, catalog)
        if (args.cloud or args.ollama) and "llm" in components:
            config = store.provider("llm") if args.cloud else {
                "provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": args.local_model}
            config["max_tokens"] = 64

            async def chat():
                started, first, text, usage = time.perf_counter(), None, "", {}
                async for item in provider.stream_chat(config, [
                    {"role": "user", "content": "用中文回答：一加一等于几？只用一句话。"}]):
                    if item.get("text"):
                        first = first or time.perf_counter()
                        text += item["text"]
                    usage.update(item.get("usage", {}))
                return {"text": text, "usage": usage,
                        "first_text_ms": round((first - started) * 1000) if first else None}

            await stage("llm", {"provider": config["provider"], "model": config["model"]}, chat)
        speech = None
        if (args.cloud or args.local_speech) and "tts" in components:
            config = store.provider("tts") if args.cloud else {"provider": "system", "voice": ""}
            if args.tts_model:
                config["model"] = args.tts_model
            if args.voice:
                config["voice"] = args.voice
            config["cache_dir"] = str(Path(args.data_dir) / "models")

            async def synthesize():
                result = await provider.synthesize(config, FIXTURE_TEXT, "zh-CN")
                pcm = decode_reference(result["audio"], result["mime"])
                return ({"bytes": len(result["audio"]), "mime": result["mime"],
                         "duration_seconds": len(pcm) / 32000, "usage": result.get("usage", {})}, pcm)

            speech = await stage("tts", {"provider": config["provider"], "model": config.get("model"),
                                          "voice": config.get("voice")}, synthesize)
        if (args.cloud or args.local_speech) and "asr" in components:
            fixture_source = "cloud_tts" if speech is not None and args.cloud else "system_tts"
            if speech is None:
                async def local_fixture():
                    result = await provider.synthesize({"provider": "system", "voice": ""}, FIXTURE_TEXT, "zh-CN")
                    return ({"bytes": len(result["audio"]), "mime": result["mime"]},
                            decode_reference(result["audio"], result["mime"]))
                speech = await stage("asr_fixture", {"provider": "system"}, local_fixture)
            config = store.provider("asr") if args.cloud else {
                "provider": "faster_whisper", "model": args.whisper_model,
                "device": "cpu", "compute_type": "int8", "language": "zh",
                "cache_dir": str(Path(args.data_dir) / "models")}
            if args.asr_model:
                config["model"] = args.asr_model
            if speech is None:
                record({"kind": "asr", "status": "skipped", "provider": config["provider"],
                        "model": config["model"], "reason": "synthetic_fixture_unavailable"})
            else:
                cases = [("final", config, speech)]
                if getattr(args, "compare_asr", False):
                    if config["provider"] != "openrouter":
                        raise ValueError("This bounded ASR comparison requires OpenRouter")
                    cases = [(phase, {**config, "model": model}, sample)
                             for model in ("openai/whisper-1", "openai/whisper-large-v3-turbo")
                             for phase, sample in (("snapshot", speech[:2 * 32000]), ("final", speech))]
                for phase, case_config, sample in cases:
                    async def transcribe(config=case_config, sample=sample, phase=phase):
                        if args.local_speech:
                            await provider.warmup(config)
                        result = await provider.transcribe(config, sample, 16000)
                        return {"text": result["text"], "expected_text": FIXTURE_TEXT if phase == "final" else None,
                                "fixture_source": fixture_source, "usage": result.get("usage", {})}
                    await stage("asr", {"provider": case_config["provider"], "model": case_config["model"],
                                        "phase": phase, "audio_seconds": len(sample) / 32000}, transcribe)
    finally:
        try:
            await provider.close()
        finally:
            print(json.dumps(results, ensure_ascii=False, indent=2))
            if args.output:
                Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return not any(result["status"] in ("error", "skipped") for result in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=".runtime")
    parser.add_argument("--catalog", action="store_true")
    parser.add_argument("--cloud", action="store_true")
    parser.add_argument("--ollama", action="store_true")
    parser.add_argument("--local-speech", action="store_true")
    parser.add_argument("--local-model", default="gemma3:4b")
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--tts-model")
    parser.add_argument("--asr-model")
    parser.add_argument("--compare-asr", action="store_true",
                        help="One bounded round: whisper-1 and large-v3-turbo, 2s snapshot plus full fixture")
    parser.add_argument("--voice")
    parser.add_argument("--components", default="llm,tts,asr", help="Comma-separated subset: llm,tts,asr")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        sys.exit(0 if asyncio.run(run(args)) else 1)
    except Exception as exc:
        print(json.dumps({"probe_error": safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

