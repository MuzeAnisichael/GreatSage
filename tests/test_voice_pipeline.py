"""Real Pipecat lifecycle checks; no hardware, service, or recorded speech."""
import asyncio
import os
import struct

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame, InterimTranscriptionFrame, TranscriptionFrame,
    VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from greatsage.audio import AudioChunk
from greatsage.voice_pipeline import SourceVoicePipeline, VoiceIdentity


VOICE = struct.pack("<h", 1200) * 480
QUIET = b"\0" * 960
CONFIG = {"endpoint_silence_ms": 90, "min_speech_ms": 90,
          "max_utterance_seconds": 2, "partial_interval_seconds": 1}


class DeterministicVAD:
    def is_speech(self, pcm, sample_rate):
        return pcm != QUIET


async def noop(*args):
    pass


class RecordingSTT(FrameProcessor):
    """Exercise native STT placement and lifecycle using real SDK frames."""

    def __init__(self, blocked=None):
        super().__init__()
        self.blocked = blocked
        self.received = asyncio.Event()
        self.cleaned = False
        self.frames = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (InputAudioRawFrame, VADUserStartedSpeakingFrame,
                              VADUserStoppedSpeakingFrame)):
            self.frames.append(type(frame).__name__)
        if isinstance(frame, InputAudioRawFrame):
            self.received.set()
            if self.blocked:
                await self.blocked.wait()
            # An STT may consume the input; ordered acknowledgements must still
            # reach the sink so queued PCM remains bounded across this stage.
            return
        await self.push_frame(frame, direction)

    async def cleanup(self):
        self.cleaned = True
        await super().cleanup()


@pytest.mark.asyncio
async def test_real_pipeline_segments_each_source_with_immutable_identity(tmp_path, monkeypatch):
    marker = tmp_path / "unexpected-hook.txt"
    hook = tmp_path / "setup-hook.py"
    hook.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n")
    monkeypatch.setenv("PIPECAT_SETUP_FILES", str(hook))
    events = []

    async def capture(identity, event):
        events.append((identity, event.kind, event.pcm))

    microphone = VoiceIdentity("microphone:4", "session-a", 3, 7)
    desktop = VoiceIdentity("process:42", "session-a", 3, 2)
    first = SourceVoicePipeline(microphone, CONFIG, capture)
    second = SourceVoicePipeline(desktop, CONFIG, capture)
    first.segmenter.vad = second.segmenter.vad = DeterministicVAD()
    try:
        await asyncio.gather(first.start(), second.start())
        assert "PIPECAT_SETUP_FILES" not in os.environ
        assert not marker.exists()
        await first.feed(AudioChunk(microphone.source, VOICE * 4, timestamp=10))
        await second.feed(AudioChunk(desktop.source, VOICE * 3, timestamp=20))
        await first.feed(AudioChunk(microphone.source, QUIET * 3, timestamp=10.1))
        await asyncio.gather(first.drain(), second.drain())
        assert [(scope, kind) for scope, kind, _ in events if scope == microphone] == [
            (microphone, "start"), (microphone, "final")]
        assert [(scope, kind) for scope, kind, _ in events if scope == desktop] == [
            (desktop, "start")]
        assert microphone.role == "user"
        assert desktop.role == "observation"
        assert not first.segmenter.active and second.segmenter.active
        with pytest.raises(ValueError, match="source"):
            await first.feed(AudioChunk(desktop.source, VOICE))
    finally:
        await asyncio.gather(first.close(), second.close())


@pytest.mark.asyncio
async def test_streaming_endpoint_flags_and_continued_segments_keep_the_turn_open():
    transcripts = []
    delivered = asyncio.Event()
    identity = VoiceIdentity("system", "session-b", 9)
    stt = RecordingSTT()

    async def capture(scope, text, final, timestamp, extra):
        transcripts.append((scope, text, final, timestamp, extra))
        if len(transcripts) == 2:
            delivered.set()

    pipeline = SourceVoicePipeline(
        identity, {**CONFIG, "max_utterance_seconds": .12}, noop,
        stt_processor=stt, on_transcript=capture)
    pipeline.segmenter.vad = DeterministicVAD()
    try:
        await pipeline.start()
        await pipeline.feed(AudioChunk("system", VOICE * 4, timestamp=20))
        await pipeline.drain()
        assert pipeline.segmenter.active
        assert stt.frames == ["VADUserStartedSpeakingFrame", "InputAudioRawFrame"]
        await stt.push_frame(InterimTranscriptionFrame(
            "部分", "untrusted-remote-user", "t1", result={"is_final": False}),
            FrameDirection.DOWNSTREAM)
        await stt.push_frame(TranscriptionFrame(
            "完整片段", "untrusted-remote-user", "t2",
            result={"is_final": True, "speech_final": False, "from_finalize": True,
                    "start": 0.0, "duration": .12, "upstream_secret": "must-not-escape"}),
            FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(delivered.wait(), 2)
        assert transcripts[0][:4] == (identity, "部分", False, "t1")
        assert transcripts[1][:4] == (identity, "完整片段", True, "t2")
        assert transcripts[1][4] == {
            "is_final": True, "speech_final": False, "from_finalize": True,
            "start": 0.0, "duration": .12, "finalized": False}
        await pipeline.feed(AudioChunk("system", QUIET * 3, timestamp=20.1))
        await pipeline.drain()
        assert stt.frames[-2:] == ["InputAudioRawFrame", "VADUserStoppedSpeakingFrame"]
    finally:
        await pipeline.close()
    assert stt.cleaned


@pytest.mark.asyncio
async def test_backpressure_covers_stt_and_close_rejects_late_results():
    transcripts = []
    stt = RecordingSTT(blocked=asyncio.Event())

    async def capture(*args):
        transcripts.append(args)

    pipeline = SourceVoicePipeline(
        VoiceIdentity("microphone:0", "old-session", 1), CONFIG, noop,
        stt_processor=stt, on_transcript=capture, max_pending_frames=2)
    try:
        await pipeline.start()
        assert await pipeline.feed(AudioChunk("microphone:0", QUIET))
        await asyncio.wait_for(stt.received.wait(), 2)
        assert await pipeline.feed(AudioChunk("microphone:0", QUIET))
        assert not await pipeline.feed(AudioChunk("microphone:0", QUIET))
        assert pipeline.pending_frames == 2
        await pipeline.close()
        await stt.push_frame(TranscriptionFrame("迟到", "", "old"), FrameDirection.DOWNSTREAM)
        assert pipeline.closed and pipeline.pending_frames == 0
        assert not await pipeline.feed(AudioChunk("microphone:0", QUIET))
        assert not transcripts and stt.cleaned
        with pytest.raises(RuntimeError, match="closed"):
            await pipeline.start()
    finally:
        await pipeline.close()
