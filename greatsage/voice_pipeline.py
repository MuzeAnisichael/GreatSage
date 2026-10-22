"""Per-source Pipecat audio pipelines with immutable GreatSage provenance.

Windows capture, echo filtering and gap detection stay in Runtime. Pipecat owns
the queued PCM -> VAD/segments -> optional streaming STT -> result lifecycle.
No LLM, tool, remote tracing or conversation authority is delegated here.
"""
from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

# Framework TRACE frames and upstream errors can include transcript bodies.
# Only safe, scoped callbacks below enter GreatSage's existing audit stream.
logger.disable("pipecat")

from pipecat.frames.frames import (
    Frame, InputAudioRawFrame, InterimTranscriptionFrame, SystemFrame,
    TranscriptionFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from .audio import AudioChunk
from .segmentation import Segmenter, SpeechEvent


@dataclass(frozen=True)
class VoiceIdentity:
    """Scope captured before enqueue; invalidation creates a new pipeline."""

    source: str
    session_id: str
    generation: int
    source_generation: int = 0

    @property
    def role(self) -> str:
        return "user" if self.source.startswith("microphone") else "observation"


SegmentCallback = Callable[[VoiceIdentity, SpeechEvent], Awaitable[None]]
TranscriptCallback = Callable[[VoiceIdentity, str, bool, str, dict], Awaitable[None]]
ErrorCallback = Callable[[VoiceIdentity, str], Awaitable[None]]


@dataclass
class _ConsumedFrame(SystemFrame):
    """Same FIFO system tier as InputAudioRawFrame, including through STT.

    A control/data acknowledgement would run in a separate processing task and
    could pass blocked system PCM. This marker must share the PCM system tier.
    """

    audio_id: int


def _transcript_metadata(frame: TranscriptionFrame | InterimTranscriptionFrame) -> dict:
    """Keep endpoint flags, never arbitrary upstream payloads or credentials."""
    result = frame.result
    values: dict[str, Any] = {}
    for name in ("is_final", "speech_final", "from_finalize", "start", "duration"):
        value = result.get(name) if isinstance(result, dict) else getattr(result, name, None)
        if isinstance(value, (bool, int, float)) and math.isfinite(value):
            values[name] = value
    values["finalized"] = bool(getattr(frame, "finalized", False))
    return values


class _SegmentProcessor(FrameProcessor):
    def __init__(self, owner: SourceVoicePipeline) -> None:
        super().__init__(name="GreatSageVoiceSegments")
        self.owner = owner
        self.media_seconds = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, InputAudioRawFrame) or direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if self.owner.closed:
            return
        # Send streaming PCM in VAD-sized pieces, so Finalize follows the exact
        # local boundary even if one capture packet contains several turns.
        if self.owner.streaming:
            for offset in range(0, len(frame.audio), 960):
                audio = frame.audio[offset:offset + 960]
                part = InputAudioRawFrame(audio, frame.sample_rate, frame.num_channels)
                part.metadata.update(frame.metadata)
                part.metadata["capture_timestamp"] -= (len(frame.audio) - offset - len(audio)) / 32000
                part.transport_source = frame.transport_source
                await self._audio(part, direction)
                if self.owner.closed:
                    return
        else:
            await self._audio(frame, direction)

    async def _audio(self, frame, direction):
        timestamp = frame.metadata["capture_timestamp"]
        self.media_seconds += len(frame.audio) / 32000
        # VAD and provider boundaries share submitted PCM time. Capture callback
        # jitter only affects display/latency timestamps, never turn ownership.
        clock = self.media_seconds if self.owner.streaming else timestamp
        events = self.owner.segmenter.feed(frame.audio, clock)
        for event in events:
            event.media_end = self.media_seconds
            if self.owner.streaming:
                event.media_start = event.speech_start if event.kind == "start" else None
                event.media_voice_end = self.owner.segmenter.last_voice
                if event.speech_end:
                    event.speech_end = timestamp - (self.media_seconds - event.speech_end)
            else:
                event.media_start = self.media_seconds - (timestamp - event.speech_start) if event.speech_start else None
                event.media_voice_end = self.media_seconds - (timestamp - event.speech_end) if event.speech_end else None
        # VAD start precedes this chunk. VAD stop follows its PCM, so a streaming
        # service's finalize cannot overtake the last samples of the utterance.
        for event in events:
            if event.kind == "start" and self.owner.streaming:
                await self.push_frame(VADUserStartedSpeakingFrame(
                    start_secs=.09, timestamp=timestamp), direction)
            if self.owner.closed:
                return
            await self.owner._on_segment(self.owner.identity, event)
        if self.owner.closed:
            return
        await self.push_frame(frame, direction)
        if self.owner.streaming:
            for event in events:
                if event.kind == "discard" or (event.kind == "final" and not event.continued):
                    await self.push_frame(VADUserStoppedSpeakingFrame(
                        stop_secs=self.owner.segmenter.silence_frames * .03,
                        timestamp=timestamp), direction)


class _ResultProcessor(FrameProcessor):
    def __init__(self, owner: SourceVoicePipeline) -> None:
        super().__init__(name="GreatSageVoiceResults")
        self.owner = owner

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, _ConsumedFrame):
            self.owner._pending.discard(frame.audio_id)
            if not self.owner._pending:
                self.owner._drained.set()
            return
        if (isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame))
                and direction == FrameDirection.DOWNSTREAM):
            if not self.owner.closed and self.owner._on_transcript is not None:
                await self.owner._on_transcript(
                    self.owner.identity, frame.text, isinstance(frame, TranscriptionFrame),
                    frame.timestamp, _transcript_metadata(frame))
            return
        await self.push_frame(frame, direction)


class SourceVoicePipeline:
    """One source and one session/generation, bounded across all processors.

    ``feed`` returning False means the caller must audit a gap and invalidate
    this source; audio is never silently replaced. For local/chunked ASR,
    ``on_segment`` schedules the existing provider call. An optional official
    STT processor instead receives live PCM and VAD boundaries; its stable
    transcription chunks are not necessarily complete user turns.

    Callbacks must be short and must not await ``close`` on their own pipeline.
    The receiver must recheck identity after any further asynchronous work.
    """

    def __init__(
        self,
        identity: VoiceIdentity,
        config: dict,
        on_segment: SegmentCallback,
        *,
        stt_processor: FrameProcessor | None = None,
        on_transcript: TranscriptCallback | None = None,
        on_error: ErrorCallback | None = None,
        max_pending_frames: int = 100,
    ) -> None:
        if (not isinstance(max_pending_frames, int) or isinstance(max_pending_frames, bool)
                or max_pending_frames < 1):
            raise ValueError("max_pending_frames must be a positive integer")
        if stt_processor is not None and on_transcript is None:
            raise ValueError("A streaming STT processor requires on_transcript")
        self.identity = identity
        self.segmenter = Segmenter(
            config.get("endpoint_silence_ms", 550), config.get("min_speech_ms", 250),
            config.get("max_utterance_seconds", 20), config.get("partial_interval_seconds", 2.5))
        self.streaming = stt_processor is not None
        self._on_segment = on_segment
        self._on_transcript = on_transcript
        self._on_error = on_error
        self._max_pending = max_pending_frames
        self._pending: set[int] = set()
        self._drained = asyncio.Event()
        self._drained.set()
        self._ready = asyncio.Event()
        self._started = False
        self._closed = False
        self._close_task: asyncio.Task | None = None
        self._runner_task: asyncio.Task | None = None
        processors = [_SegmentProcessor(self)]
        if stt_processor is not None:
            processors.append(stt_processor)
        processors.append(_ResultProcessor(self))
        self.worker = PipelineWorker(
            Pipeline(processors),
            params=PipelineParams(
                audio_in_sample_rate=16000, enable_metrics=False,
                enable_usage_metrics=False, send_initial_empty_metrics=False),
            enable_rtvi=False, enable_turn_tracking=False, enable_tracing=False,
            idle_timeout_secs=None, cancel_on_idle_timeout=False,
            processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
            setup_timeout_secs=10, start_timeout_secs=10, cancel_timeout_secs=2,
        )
        self._runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)

        @self.worker.event_handler("on_pipeline_started")
        async def started(_worker, _frame):
            self._started = True
            self._ready.set()

        @self.worker.event_handler("on_pipeline_error")
        async def failed(_worker, _frame):
            if not self.closed and self._on_error is not None:
                await self._on_error(self.identity, "voice_pipeline_processor_error")

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending_frames(self) -> int:
        return len(self._pending)

    @property
    def state(self) -> str:
        """Metadata-only lifecycle state; never expose an SDK exception."""
        if self._closed:
            return "closed"
        if self._runner_task is not None and self._runner_task.done():
            return "failed"
        return "running" if self._started else "starting"

    async def start(self) -> None:
        # Upstream debugger hooks execute arbitrary files from this environment
        # variable. GreatSage only loads Skills as text and never opts into hooks.
        os.environ.pop("PIPECAT_SETUP_FILES", None)
        if self._closed:
            raise RuntimeError("Cannot restart a closed voice pipeline")
        if self._runner_task is None:
            await self._runner.add_workers(self.worker)
            self._runner_task = asyncio.create_task(
                self._runner.run(), name=f"voice:{self.identity.source}")
            self._runner_task.add_done_callback(lambda _: self._ready.set())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=25)
            if not self._started or self._closed or self._runner_task.done():
                raise RuntimeError("voice_pipeline_start_failed")
        except BaseException:
            await self.close()
            raise

    async def feed(self, chunk: AudioChunk) -> bool:
        """Enqueue already-filtered PCM16/16 kHz mono; preserve capture time."""
        if chunk.source != self.identity.source:
            raise ValueError("Audio source does not match pipeline identity")
        if chunk.sample_rate != 16000 or chunk.channels != 1 or len(chunk.pcm) % 2:
            raise ValueError("Voice pipeline requires PCM16/16 kHz mono")
        if (self._closed or not self._started or self._runner_task is None
                or self._runner_task.done() or len(self._pending) >= self._max_pending):
            return False
        frame = InputAudioRawFrame(chunk.pcm, chunk.sample_rate, chunk.channels)
        frame.transport_source = self.identity.source
        frame.metadata.update(
            voice_identity=self.identity, capture_timestamp=chunk.timestamp,
            capture_sequence=chunk.sequence)
        self._pending.add(frame.id)
        self._drained.clear()
        await self.worker.queue_frames([frame, _ConsumedFrame(frame.id)])
        return True

    async def drain(self, timeout: float = 5) -> None:
        """Wait for accepted PCM to reach the sink, not remote ASR completion."""
        await asyncio.wait_for(self._drained.wait(), timeout)

    async def close(self) -> None:
        """Invalidate immediately; all callers observe the same cleanup result.

        Runtime schedules close outside processor callbacks. A cancelled waiter
        must not cancel cleanup or make another caller mistake ``closed`` for
        released resources, including when ``start`` is already cleaning up.
        """
        if self._close_task is None:
            self._closed = True
            self._ready.set()
            self._pending.clear()
            self._drained.set()
            self._close_task = asyncio.create_task(self._cleanup(), name=f"voice-close:{self.identity.source}")
            # A timeout may cancel every waiter; still retrieve the safe error.
            self._close_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        await asyncio.shield(self._close_task)

    async def _cleanup(self) -> None:
        if self._runner_task is None:
            return
        try:
            # Cancellation itself can wait on SDK/processor cleanup. Bound that
            # phase too, then force the runner task down if it cannot complete.
            await asyncio.wait_for(self._runner.cancel(reason="GreatSage voice scope invalidated"), timeout=2)
            await asyncio.wait_for(asyncio.shield(self._runner_task), timeout=5)
        except TimeoutError:
            self._runner_task.cancel()
            done, _ = await asyncio.wait({self._runner_task}, timeout=2)
            if not done:
                raise RuntimeError("voice_pipeline_cleanup_timeout") from None
        except Exception:
            raise RuntimeError("voice_pipeline_cleanup_failed") from None
        if self._runner_task.done() and not self._runner_task.cancelled():
            try:
                self._runner_task.result()
            except Exception:
                raise RuntimeError("voice_pipeline_cleanup_failed") from None
