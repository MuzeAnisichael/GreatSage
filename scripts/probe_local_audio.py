"""Explicit local ASR download and synthetic Chinese/English speech smoke test."""
import argparse
import asyncio
import json
import time
from pathlib import Path

from greatsage.echo import decode_reference
from greatsage.providers import ProviderError, Providers


async def run(args):
    provider = Providers()
    results = []
    config = {"provider": "faster_whisper", "model": args.model, "device": "cpu",
              "compute_type": "int8", "cpu_threads": 4, "beam_size": 3,
              "cache_dir": str(args.data_dir / "models")}
    try:
        started = time.perf_counter()
        await provider.warmup(config)
        results.append({"kind": "model_prepare", "model": args.model,
                        "elapsed_ms": round((time.perf_counter()-started)*1000)})
        for language, text in [("zh-CN", "请记住，我喜欢简短的中文回答。"),
                               ("en", "Please remember that I prefer short answers.")]:
            voice = await provider.synthesize({"provider": "system", "voice": ""}, text, language)
            pcm = decode_reference(voice["audio"], voice["mime"])
            started = time.perf_counter()
            result = await provider.transcribe({**config, "language": language}, pcm)
            results.append({"kind": "synthetic_speech", "language": language, "expected": text,
                            "actual": result["text"], "audio_seconds": len(pcm)/32000,
                            "asr_ms": round((time.perf_counter()-started)*1000)})
    except ProviderError as error:
        results.append({"kind": "error", "provider": error.provider, "code": error.code,
                        "status": error.status})
        raise
    finally:
        await provider.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(results, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="base")
    parser.add_argument("--data-dir", type=Path, default=Path(".runtime"))
    parser.add_argument("--output", type=Path, default=Path(".runtime/local-audio-probe.json"))
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments))
    except ProviderError:
        raise SystemExit(1)
