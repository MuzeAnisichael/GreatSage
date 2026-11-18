"""Offline v0.9 dictation probe. A real microphone is opened only with --microphone."""
import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from greatsage.audio import AudioCaptureManager, AudioChunk
from greatsage.correction_voice import CorrectionVoice
from greatsage.echo import decode_reference
from greatsage.providers import Providers


class SyntheticCapture:
    def __init__(self, pcm):
        self.pcm = pcm

    def start(self, options, chunk, error):
        chunk(AudioChunk("microphone:fixture", self.pcm))

    def stop(self):
        pass


async def run(args):
    config = {"provider": "faster_whisper", "model": args.model, "device": "cpu", "compute_type": "int8",
              "language": "zh", "cache_dir": str(args.cache.resolve())}
    settings = SimpleNamespace(raw=lambda: {"asr": config}, version=lambda: "probe")
    artifact = {"kind": "document", "profile": "continuation", "session_id": "demo", "current_version": 1}
    runtime = SimpleNamespace(settings=settings, memory=SimpleNamespace(artifact=lambda _: artifact),
                              _provider=lambda _: config, _session_switching=False, listening=False,
                              session_id="demo", data_epoch=0)
    if args.microphone:
        manager = AudioCaptureManager()
        count = len(manager.sources()["microphones"])
        microphone = CorrectionVoice(runtime, manager)
        try:
            recording = await microphone.start("brief", "demo", 1)
            await microphone.cancel(recording["capture_id"])
            print(json.dumps({"probe": "microphone", "devices": count, "first_frame": True,
                              "released": not microphone.busy, "transcribed": False}))
        finally:
            await microphone.cancel()

    runtime.providers = Providers()
    try:
        expected = "周五不是截止日期，发布日期待确认。"
        speech = await runtime.providers.synthesize({"provider": "system"}, expected, "zh-CN")
        pcm = decode_reference(speech["audio"], speech["mime"])
        synthetic = CorrectionVoice(runtime, SyntheticCapture(pcm))
        recording = await synthetic.start("brief", "demo", 1)
        result = await synthetic.finish(recording["capture_id"])
        text = result["text"]
        matched = ("五" in text and "不是" in text and "截止" in text
                   and ("待确认" in text or "待確認" in text))
        print(json.dumps({"probe": "synthetic_speech", "source": "Windows SAPI", "asr": args.model,
                          "duration_s": round(len(pcm) / 32000, 2), "expected": expected,
                          "actual": text, "matched_negation_and_uncertainty": matched}, ensure_ascii=True))
        return matched
    finally:
        await runtime.providers.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--microphone", action="store_true", help="Read one real frame and immediately discard it.")
    parser.add_argument("--model", default="base", help="Cached faster-whisper model or a local model directory.")
    parser.add_argument("--cache", type=Path, default=Path(".runtime/models"))
    raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)
