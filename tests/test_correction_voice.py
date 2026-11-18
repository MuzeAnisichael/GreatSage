import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from greatsage.audio import AudioChunk
from greatsage.correction_voice import CorrectionVoice


class Microphone:
    def __init__(self):
        self.chunk = AudioChunk("microphone:9", b"\x10\x01" * 320)
        self.calls = 0

    def start(self, options, on_chunk, on_error):
        assert options == {"microphone": True, "microphone_device": "9", "desktop_source": "none"}
        self.calls += 1
        on_chunk(AudioChunk("system", b"unrelated"))
        on_chunk(self.chunk)

    def stop(self):
        pass


@pytest.fixture
def voice():
    settings = Mock()
    settings.version.return_value = "settings-revision"
    settings.raw.return_value = {"asr": {"provider": "faster_whisper"}, "microphone_device": "9"}
    memory = Mock()
    memory.artifact.return_value = {"id": "doc", "kind": "document", "profile": "continuation",
                                    "session_id": "day-1", "current_version": 1}
    runtime = SimpleNamespace(session_id="day-1", _session_switching=False, listening=False,
                              data_epoch=0, memory=memory, settings=settings,
                              providers=SimpleNamespace(transcribe=AsyncMock(return_value={"text": "  周五前复核。  "})),
                              _provider=lambda name: settings.raw()["asr"])
    return CorrectionVoice(runtime, Microphone())


async def test_explicit_stop_returns_only_editable_transcript(voice):
    recording = await voice.start("doc", "day-1", 1)
    assert voice.busy
    voice.runtime.providers.transcribe.assert_not_called()
    with pytest.raises(ValueError, match="已有"):
        await voice.start("doc", "day-1", 1)
    result = await voice.finish(recording["capture_id"])
    assert result == {"session_id": "day-1", "artifact_id": "doc", "expected_version": 1, "text": "周五前复核。"}
    voice.runtime.providers.transcribe.assert_awaited_once_with(
        {"provider": "faster_whisper"}, voice.capture.chunk.pcm, 16000)
    assert {call[0] for call in voice.runtime.memory.method_calls} == {"artifact"}
    assert not voice.busy


@pytest.mark.parametrize("cause", ["normal-listener", "wrong-session", "old-version", "other-document", "streaming-only"])
async def test_rejected_start_never_opens_microphone(voice, cause):
    if cause == "normal-listener":
        voice.runtime.listening = True
    elif cause == "wrong-session":
        voice.runtime.session_id = "day-2"
    elif cause == "old-version":
        voice.runtime.memory.artifact.return_value["current_version"] = 2
    elif cause == "other-document":
        voice.runtime.memory.artifact.return_value["profile"] = "research"
    else:
        voice.runtime.settings.raw.return_value["asr"]["provider"] = "deepgram_streaming"
    with pytest.raises(ValueError):
        await voice.start("doc", "day-1", 1)
    assert not voice.busy and voice.capture.calls == 0


@pytest.mark.parametrize("cause", ["scope", "version", "settings", "delete"])
async def test_stale_asr_completion_cannot_fill_draft(voice, cause):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_provider(*args):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # Simulate a provider which still returns an obsolete result.
        return {"text": "old"}

    voice.runtime.providers.transcribe = slow_provider
    recording = await voice.start("doc", "day-1", 1)
    finishing = asyncio.create_task(voice.finish(recording["capture_id"]))
    await started.wait()
    if cause == "scope":
        voice.runtime.session_id = "day-2"
    elif cause == "version":
        voice.runtime.memory.artifact.return_value["current_version"] = 2
    elif cause == "settings":
        voice.runtime.settings.version.return_value = "new"
    else:
        await voice.cancel_for_artifact("unrelated")
        assert voice.busy
        await voice.cancel_for_artifact("doc")
    release.set()
    with pytest.raises(ValueError):
        await finishing
    assert not voice.busy


async def test_timeout_and_silent_input_discard_without_transcription(voice):
    voice.max_seconds = 1
    first = await voice.start("doc", "day-1", 1)
    await asyncio.sleep(1.05)
    with pytest.raises(ValueError, match="超时"):
        await voice.finish(first["capture_id"])
    voice.capture.chunk = AudioChunk("microphone:9", bytes(640))
    second = await voice.start("doc", "day-1", 1)
    await voice.cancel(first["capture_id"])
    with pytest.raises(ValueError, match="信号"):
        await voice.finish(second["capture_id"])
    voice.runtime.providers.transcribe.assert_not_called()
