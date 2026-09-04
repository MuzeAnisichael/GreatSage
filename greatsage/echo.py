"""Conservative playback-reference echo gate, not acoustic echo cancellation.

Only nearly identical, gain-scaled reference audio is silenced. Room reverberation,
speaker distortion, clock drift and weak double talk can defeat this classifier.
In particular, a user's speech below the residual noise threshold cannot reliably
be distinguished from noise. Headphones remain the dependable echo-free setup.

No microphone buffering delay is introduced: insufficient evidence is passed
through immediately. The first 60 ms after a reset is deliberately preserved.
"""
from __future__ import annotations

import io
import math
import threading
import wave

import numpy as np


SAMPLE_RATE = 16000
MAX_REFERENCE_SECONDS = 120
MAX_REFERENCE_SAMPLES = SAMPLE_RATE * MAX_REFERENCE_SECONDS


def decode_reference(audio: bytes, mime: str) -> bytes:
    """Decode up to 120 seconds of generated audio to PCM16 16 kHz mono.

    PCM WAV at the target format uses stdlib only. Other supported formats need
    PyAV (`av` must be a base dependency for cloud MP3 TTS). Demuxer selection is
    explicit; arbitrary playlists and external media URLs are not opened.
    """
    if len(audio) > 32 * 1024 * 1024:
        raise ValueError("Audio reference exceeds the 32 MiB limit")
    if not audio:
        return b""
    media_type = mime.split(";")[0].strip().lower()
    formats = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
               "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "ogg",
               "audio/opus": "ogg", "audio/flac": "flac", "audio/aac": "aac"}
    if media_type not in formats:
        raise ValueError("Unsupported playback reference MIME type")
    if formats[media_type] == "wav":
        try:
            with wave.open(io.BytesIO(audio), "rb") as source:
                if (source.getnchannels() == 1 and source.getsampwidth() == 2
                        and source.getframerate() == SAMPLE_RATE and source.getcomptype() == "NONE"):
                    return source.readframes(MAX_REFERENCE_SAMPLES)
        except (wave.Error, EOFError):
            pass  # PyAV also handles extensible WAV that older Python cannot.
    try:
        import av
    except ImportError:
        raise RuntimeError("Decoding this playback reference requires the av package") from None
    parts: list[bytes] = []
    remaining = MAX_REFERENCE_SAMPLES
    try:
        with av.open(io.BytesIO(audio), format=formats[media_type], mode="r") as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

            def append(frames):
                nonlocal remaining
                for frame in frames:
                    if remaining <= 0:
                        break
                    values = frame.to_ndarray().reshape(-1)[:remaining]
                    parts.append(np.asarray(values, dtype="<i2").tobytes())
                    remaining -= len(values)

            for frame in container.decode(audio=0):
                append(resampler.resample(frame))
                if remaining <= 0:
                    break
            if remaining > 0:
                append(resampler.resample(None))
    except Exception:
        raise ValueError("Unsupported or damaged playback reference audio") from None
    return b"".join(parts)


class EchoGuard:
    """Gate only high-confidence playback copies while preserving uncertain input.

    `started_at` is actual playback start, not synthesis/request start. `timestamp`
    is wall-clock time at the END of the microphone PCM block. Both must use the
    same clock (time.time with AudioChunk). Call clear() when playback is stopped.

    max_residual_rms is measured in signed PCM16 counts, default 18 (~-65 dBFS).
    This intentionally rejects many real acoustic echoes rather than increasing
    the risk of suppressing the user's voice. It is a tunable noise threshold,
    not a reliable double-talk detector. No output frame is delayed or shortened.
    """
    def __init__(self, *, max_delay_seconds: float = 0.5,
                 min_correlation: float = 0.995, max_residual_rms: float = 18.0) -> None:
        if not 0 <= max_delay_seconds <= 1:
            raise ValueError("Echo delay window must be between 0 and 1 second")
        if not 0.98 <= min_correlation <= 1:
            raise ValueError("Echo correlation threshold must be between 0.98 and 1")
        if not 0 <= max_residual_rms <= 100:
            raise ValueError("Residual threshold must be between 0 and 100 PCM16 counts")
        self.max_delay_seconds = max_delay_seconds
        self.min_correlation = min_correlation
        self.max_residual_rms = max_residual_rms
        self._lock = threading.RLock()
        self._reference = np.empty(0, dtype=np.float32)
        self._coarse = np.empty(0, dtype=np.float32)
        self._sums = self._squares = np.zeros(1, dtype=np.float64)
        self._history = np.empty(0, dtype=np.float32)
        self._started_at = 0.0
        self._last_timestamp: float | None = None
        self.last_suppressed = False
        self.last_decision: dict = {"reason": "no_reference"}
        self.reference_truncated = False

    def set_reference(self, pcm16mono16000: bytes, started_at: float) -> None:
        if len(pcm16mono16000) % 2:
            raise ValueError("Reference must contain aligned PCM16 mono samples")
        if not math.isfinite(started_at):
            raise ValueError("Reference start time must be finite")
        with self._lock:
            self.clear()
            self.reference_truncated = len(pcm16mono16000) > MAX_REFERENCE_SAMPLES * 2
            self._reference = np.frombuffer(pcm16mono16000[:MAX_REFERENCE_SAMPLES * 2],
                                            dtype="<i2").astype(np.float32)
            # Four-sample averaging provides a cheap coarse correlation search.
            # Every accepted match is subsequently checked at full sample rate.
            count = len(self._reference) // 4 * 4
            self._coarse = self._reference[:count].reshape(-1, 4).mean(axis=1)
            self._sums = np.concatenate(([0.0], np.cumsum(self._coarse, dtype=np.float64)))
            self._squares = np.concatenate(([0.0], np.cumsum(
                self._coarse.astype(np.float64) ** 2, dtype=np.float64)))
            self._started_at = float(started_at)
            self.last_decision = {"reason": "reference_ready" if count else "no_reference"}

    def clear(self) -> None:
        with self._lock:
            self._reference = np.empty(0, dtype=np.float32)
            self._coarse = np.empty(0, dtype=np.float32)
            self._sums = self._squares = np.zeros(1, dtype=np.float64)
            self._history = np.empty(0, dtype=np.float32)
            self._last_timestamp = None
            self.last_suppressed = False
            self.reference_truncated = False
            self.last_decision = {"reason": "no_reference"}

    def filter(self, pcm: bytes, timestamp: float) -> bytes:
        """Return the original block, or exactly equal-length zero PCM on a match."""
        if len(pcm) % 2:
            raise ValueError("Microphone block must contain aligned PCM16 samples")
        if not math.isfinite(timestamp):
            raise ValueError("Microphone timestamp must be finite")
        with self._lock:
            self.last_suppressed = False
            self.last_decision = {"reason": "no_reference"}
            if not pcm or not self._reference.size:
                return pcm
            elapsed = timestamp - self._started_at
            if elapsed < 0:
                self.last_decision = {"reason": "playback_not_started"}
                return pcm
            if elapsed > len(self._reference) / SAMPLE_RATE + self.max_delay_seconds + 0.08:
                self.clear()
                self.last_decision = {"reason": "reference_expired"}
                return pcm
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            duration = len(samples) / SAMPLE_RATE
            if duration > 0.08:
                # Long batched input would need per-frame output decisions. Keep
                # it intact rather than mute possible speech outside our window.
                self._history = np.empty(0, dtype=np.float32)
                self._last_timestamp = timestamp
                self.last_decision = {"reason": "block_too_long"}
                return pcm
            if self._last_timestamp is not None and (
                    timestamp <= self._last_timestamp
                    or abs(timestamp - self._last_timestamp - duration) > 0.08):
                self._history = np.empty(0, dtype=np.float32)
            self._last_timestamp = timestamp
            self._history = np.concatenate((self._history, samples))[-1280:]  # 80 ms maximum
            if len(self._history) < 960:  # 60 ms evidence, never wait or buffer output
                self.last_decision = {"reason": "insufficient_history"}
                return pcm
            # Trim the oldest samples to align the coarse analysis to four samples.
            count = len(self._history) // 4 * 4
            if count < len(samples):
                self.last_decision = {"reason": "insufficient_aligned_history"}
                return pcm
            mic = self._history[-count:]
            centered = mic - mic.mean()
            energy = float(np.dot(centered, centered))
            if energy < count * 50 ** 2 or np.max(np.abs(mic)) >= 32760:
                self.last_decision = {"reason": "quiet_or_clipped_input"}
                return pcm

            end_index = elapsed * SAMPLE_RATE
            # 20 ms clock/delivery tolerance avoids missing reference starts when
            # the renderer's playing event is delivered a little after playback.
            low = max(0, math.floor(end_index - count - self.max_delay_seconds * SAMPLE_RATE - 320))
            high = min(len(self._reference) - count, math.ceil(end_index - count + 320))
            first = (low + 3) // 4
            last = high // 4
            coarse_mic = mic.reshape(-1, 4).mean(axis=1)
            coarse_mic = coarse_mic - coarse_mic.mean()
            width = len(coarse_mic)
            last = min(last, len(self._coarse) - width)
            if first > last:
                self.last_decision = {"reason": "outside_reference_window"}
                return pcm
            norm_mic = float(np.dot(coarse_mic, coarse_mic))
            if norm_mic <= 1:
                self.last_decision = {"reason": "insufficient_signal_variation"}
                return pcm
            sums = self._sums[first + width:last + width + 1] - self._sums[first:last + 1]
            squares = self._squares[first + width:last + width + 1] - self._squares[first:last + 1]
            norms = np.maximum(squares - sums * sums / width, 1)
            products = np.correlate(self._coarse[first:last + width], coarse_mic, "valid")
            correlations = np.abs(products) / np.sqrt(norms * norm_mic)
            best_coarse = int(np.argmax(correlations))
            if correlations[best_coarse] < self.min_correlation - 0.02:
                self.last_decision = {"reason": "low_correlation"}
                return pcm

            coarse_offset = (first + best_coarse) * 4
            best = None
            for offset in range(max(low, coarse_offset - 4), min(high, coarse_offset + 4) + 1):
                reference = self._reference[offset:offset + count]
                ref_centered = reference - reference.mean()
                norm_ref = float(np.dot(ref_centered, ref_centered))
                if norm_ref < count * 50 ** 2:
                    continue
                product = float(np.dot(centered, ref_centered))
                correlation = abs(product) / math.sqrt(energy * norm_ref)
                if best is None or correlation > best[0]:
                    best = (correlation, product / norm_ref, offset, ref_centered)
            if best is None or best[0] < self.min_correlation:
                self.last_decision = {"reason": "low_full_rate_correlation"}
                return pcm
            correlation, gain, offset, ref_centered = best
            residual = centered - gain * ref_centered
            residual_rms = math.sqrt(float(np.mean(residual * residual)))
            # Inspect this very block independently so an onset of user speech
            # cannot be hidden by the preceding 60 ms of clean reference audio.
            current_residual = residual[-len(samples):]
            current_rms = math.sqrt(float(np.mean(current_residual * current_residual)))
            peak = float(np.max(np.abs(current_residual)))
            self.last_decision = {
                "reason": "residual_or_gain_mismatch", "correlation": round(correlation, 6),
                "gain": round(gain, 4), "residual_rms": round(residual_rms, 2),
                "current_residual_rms": round(current_rms, 2),
                "delay_ms": round((end_index - count - offset) / SAMPLE_RATE * 1000, 1),
            }
            if (not 0.01 <= abs(gain) <= 4 or residual_rms > self.max_residual_rms
                    or current_rms > self.max_residual_rms
                    or peak > max(20, self.max_residual_rms * 6)):
                return pcm
            self.last_suppressed = True
            self.last_decision["reason"] = "high_confidence_reference_copy"
            return bytes(len(pcm))
