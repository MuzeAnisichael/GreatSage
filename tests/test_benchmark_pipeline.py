"""Exercise the real Runtime/segmentation path without devices or paid requests."""
import asyncio
import io
import json
import struct
import wave

import pytest

from greatsage.memory import MemoryStore
from greatsage.providers import ProviderError
from greatsage.segmentation import Segmenter
from greatsage.settings import SettingsStore
from scripts.benchmark_pipeline import FIXTURE_TEXT, make_parser, prepare, run


def fixture_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        # 360 ms above the RMS threshold, followed by 90 ms silence.
        audio.writeframes(struct.pack("<h", 1500) * 5760 + b"\0" * 2880)
    return output.getvalue()


class DeterministicVad:
    def is_speech(self, frame, sample_rate):
        # Segmenter still performs its own RMS silence rejection.
        return True


def deterministic_segmenter(*args):
    return Segmenter(*args, detector=DeterministicVad())


class MockProviders:
    def __init__(self, *, asr_failure=False, tts_failure=False, asr_delay=.02):
        self.asr_failure = asr_failure
        self.tts_failure = tts_failure
        self.asr_delay = asr_delay
        self.synthesis_count = 0
        self.asr_count = 0
        self.chat_count = 0
        self.closed = False

    async def synthesize(self, config, text, language):
        self.synthesis_count += 1
        assert config["provider"] == "system"
        assert language == "zh-CN"
        if self.synthesis_count == 1:
            assert text == FIXTURE_TEXT
        elif self.tts_failure:
            raise RuntimeError("TEST_PRIVATE_KEY should not appear in the report")
        await asyncio.sleep(.01)
        return {"audio": fixture_wav(), "mime": "audio/wav", "usage": {}}

    async def transcribe(self, config, pcm, sample_rate=16000):
        self.asr_count += 1
        assert sample_rate == 16000
        assert len(pcm) > 32000 * .3
        await asyncio.sleep(self.asr_delay)
        if self.asr_failure:
            raise ProviderError("openrouter", "network_error")
        return {"text": FIXTURE_TEXT, "usage": {}}

    async def stream_chat(self, config, messages):
        self.chat_count += 1
        assert messages[-1]["content"] == FIXTURE_TEXT
        assert "unrelated private history" not in json.dumps(messages)
        await asyncio.sleep(.015)
        yield {"text": "一加一"}
        await asyncio.sleep(.01)
        yield {"text": "等于二。"}

    async def close(self):
        self.closed = True


def arguments(tmp_path, *extra):
    return make_parser().parse_args([
        "--settings-dir", str(tmp_path / "source"),
        "--data-dir", str(tmp_path / "benchmark"),
        "--tts-provider", "system", "--timeout-seconds", "5", *extra,
    ])


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    # Never load an actual user key into the mock providers.
    monkeypatch.setenv("OPENROUTER_API_KEY", "TEST_PRIVATE_KEY")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


async def test_dry_run_has_no_provider_calls_and_keeps_source_unchanged(tmp_path):
    source = SettingsStore(tmp_path / "source")
    source.update({"global_prompt": "unrelated private prompt", "mode": "proactive"})
    original = (tmp_path / "source" / "settings.json").read_bytes()
    providers = MockProviders()
    result = await run(arguments(tmp_path, "--dry-run"), providers=providers)
    assert result["status"] == "planned"
    assert providers.synthesis_count == providers.asr_count == providers.chat_count == 0
    assert (tmp_path / "source" / "settings.json").read_bytes() == original
    serialized = json.dumps(result)
    assert "TEST_PRIVATE_KEY" not in serialized
    assert "unrelated private prompt" not in serialized
    assert result["parameters"]["effective_endpoint_silence_ms"] == 570
    assert result["configuration"]["tts"] == {"provider": "system", "model": ""}
    assert json.loads((tmp_path / "benchmark" / "results.json").read_text("utf-8")) == result
    assert (tmp_path / "benchmark" / result["run_directory"] / "results.json").exists()


@pytest.mark.parametrize("packet_ms", [20, 30])
async def test_paced_pipeline_times_real_runtime_boundaries_in_isolation(tmp_path, packet_ms):
    SettingsStore(tmp_path / "source")
    original_memory = MemoryStore(tmp_path / "source")
    session = original_memory.new_session()
    original_memory.add_message("user", "unrelated private history", session_id=session)
    original_memory.close()
    providers = MockProviders()
    result = await run(arguments(tmp_path, "--packet-ms", str(packet_ms)), providers=providers,
                       segmenter_factory=deterministic_segmenter)
    assert result["status"] == "ok", result
    metrics = result["metrics"]
    assert metrics["vad_detected_utterances"] == 1
    assert metrics["vad_speech_end_offset_ms"] == pytest.approx(360, abs=1)
    # Scheduling overhead is measured separately; this is not a hardware SLA.
    assert 550 <= metrics["endpoint_detection_ms"] < 1500
    assert 0 < metrics["final_asr_provider_ms"] < metrics["speech_end_to_transcript_ms"]
    assert metrics["speech_end_to_transcript_ms"] >= metrics["endpoint_detection_ms"]
    assert metrics["speech_end_to_first_text_ms"] >= metrics["speech_end_to_transcript_ms"]
    assert metrics["speech_end_to_audio_ready_ms"] >= metrics["speech_end_to_first_text_ms"]
    assert metrics["speech_end_to_completion_ms"] >= metrics["speech_end_to_audio_ready_ms"]
    assert metrics["transcript_normalized_match"] is True
    assert result["recognition"]["transcript"] == FIXTURE_TEXT
    assert result["scope"]["actual_playback_measured"] is False
    assert result["scope"]["hardware_capture"] is False
    assert result["scope"]["human_asr_accuracy_measured"] is False
    assert providers.asr_count == providers.chat_count == 1
    assert providers.synthesis_count == 2 and providers.closed
    assert "TEST_PRIVATE_KEY" not in json.dumps(result)
    original_memory = MemoryStore(tmp_path / "source")
    assert [message["text"] for message in original_memory.history()] == ["unrelated private history"]
    original_memory.close()
    run_memory = MemoryStore(tmp_path / "benchmark" / result["run_directory"])
    assert [message["role"] for message in run_memory.history()] == ["user", "assistant"]
    assert not any(event["kind"] == "playback" for event in run_memory.events())
    run_memory.close()


async def test_asr_failure_reports_endpoint_without_fabricated_response_times(tmp_path):
    providers = MockProviders(asr_failure=True)
    result = await run(arguments(tmp_path), providers=providers, segmenter_factory=deterministic_segmenter)
    assert result["status"] == "error"
    assert any(error.get("code") == "network_error" for error in result["errors"])
    assert result["metrics"]["endpoint_detection_ms"] >= 550
    assert "speech_end_to_first_text_ms" not in result["metrics"]
    assert "speech_end_to_audio_ready_ms" not in result["metrics"]
    assert providers.chat_count == 0 and providers.closed


async def test_partial_asr_cancellation_does_not_count_as_final_latency(tmp_path):
    providers = MockProviders(asr_delay=.8)
    result = await run(arguments(tmp_path, "--partial-interval", ".5"), providers=providers,
                       segmenter_factory=deterministic_segmenter)
    assert result["status"] == "ok", result
    metrics = result["metrics"]
    assert metrics["asr_requests"] == 2
    assert metrics["asr_cancelled_requests"] == 1
    assert metrics["final_asr_queue_ms"] >= 0
    assert 0 < metrics["final_asr_provider_ms"] < metrics["speech_end_to_transcript_ms"]
    assert providers.chat_count == 1


async def test_tts_failure_preserves_text_metric_and_omits_upstream_error_body(tmp_path):
    providers = MockProviders(tts_failure=True)
    result = await run(arguments(tmp_path), providers=providers, segmenter_factory=deterministic_segmenter)
    assert result["status"] == "error"
    assert result["metrics"]["speech_end_to_first_text_ms"] >= 550
    assert "speech_end_to_audio_ready_ms" not in result["metrics"]
    assert any(error.get("component") == "tts" for error in result["errors"])
    assert "TEST_PRIVATE_KEY" not in json.dumps(result)
    assert providers.closed


def test_rejects_user_runtime_destination_and_implicit_ollama_model(tmp_path):
    args = arguments(tmp_path)
    args.data_dir = args.settings_dir
    with pytest.raises(ValueError, match="separate"):
        prepare(args)
    with pytest.raises(ValueError, match="explicit installed model"):
        prepare(arguments(tmp_path, "--llm-provider", "ollama"))
