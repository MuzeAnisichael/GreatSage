"""Bounded voice activity segmentation, independent of network latency."""
from __future__ import annotations

import audioop
import math
from collections import deque
from dataclasses import dataclass

import webrtcvad


@dataclass
class SpeechEvent:
    kind: str
    pcm: bytes = b""
    speech_end: float = 0.0
    continued: bool = False


class Segmenter:
    FRAME_BYTES = 960  # 30 ms, 16 kHz, signed PCM16 mono

    def __init__(self, silence_ms=550, min_speech_ms=250, max_seconds=20,
                 partial_seconds=2.5, detector=None):
        self.vad = detector or webrtcvad.Vad(2)
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0
               for value in (silence_ms, min_speech_ms, max_seconds, partial_seconds)):
            raise ValueError("Segmentation durations must be positive finite numbers")
        self.silence_frames = max(3, math.ceil(silence_ms / 30))
        self.min_frames = max(3, math.ceil(min_speech_ms / 30))
        self.max_frames = max(3, math.floor(max_seconds / .03))
        if self.max_frames < self.min_frames:
            raise ValueError("Maximum utterance duration must exceed minimum speech duration")
        self.partial_frames = max(1, math.ceil(partial_seconds / .03))
        self.pending = bytearray()
        self.preroll = deque(maxlen=8)
        self.frames: list[bytes] = []
        self.voiced = self.quiet = self.since_partial = self.start_hits = 0
        self.active = False
        self.last_voice = 0.0

    def feed(self, pcm: bytes, timestamp: float) -> list[SpeechEvent]:
        self.pending.extend(pcm)
        events = []
        while len(self.pending) >= self.FRAME_BYTES:
            frame = bytes(self.pending[:self.FRAME_BYTES])
            del self.pending[:self.FRAME_BYTES]
            frame_time = timestamp - len(self.pending) / 32000
            voice = audioop.rms(frame, 2) > 120 and self.vad.is_speech(frame, 16000)
            if not self.active:
                self.preroll.append(frame)
                self.start_hits = self.start_hits + 1 if voice else 0
                if self.start_hits < 3:
                    continue
                self.active = True
                self.frames = list(self.preroll)
                self.voiced = self.start_hits
                self.since_partial = len(self.frames)
                self.quiet = 0
                self.last_voice = frame_time
                events.append(SpeechEvent("start"))
                continue
            self.frames.append(frame)
            self.since_partial += 1
            if voice:
                self.last_voice = frame_time
                self.voiced += 1
                self.quiet = 0
            else:
                self.quiet += 1
            if self.quiet >= self.silence_frames or len(self.frames) >= self.max_frames:
                continued = self.quiet < self.silence_frames
                if self.voiced >= self.min_frames:
                    end = len(self.frames) - max(0, self.quiet - 3)
                    events.append(SpeechEvent("final", b"".join(self.frames[:end]),
                                              self.last_voice, continued))
                else:
                    events.append(SpeechEvent("discard"))
                self.frames = []
                self.voiced = self.quiet = self.since_partial = self.start_hits = 0
                self.active = continued
                self.preroll.clear()
            elif self.since_partial >= self.partial_frames and self.voiced >= self.min_frames:
                events.append(SpeechEvent("partial", b"".join(self.frames), self.last_voice))
                self.since_partial = 0
        return events
