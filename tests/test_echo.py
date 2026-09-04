"""Synthetic-only echo gate checks; never open microphones or speakers."""
import io
import time
import wave

import numpy as np
import pytest

from greatsage.echo import EchoGuard, MAX_REFERENCE_SAMPLES, decode_reference


def pcm(values):
    return np.clip(np.rint(values), -32768, 32767).astype("<i2").tobytes()


def signal(seconds=2, seed=91):
    rng = np.random.default_rng(seed)
    values = rng.normal(size=int(seconds * 16000))
    values = np.convolve(values, np.hanning(9), mode="same")
    return values / values.std() * 3000


def feed(guard, values, delay=0.18, start=100.0, frame_sizes=(320,)):
    position, index, results = 0, 0, []
    while position < len(values):
        size = frame_sizes[index % len(frame_sizes)]
        frame = values[position:position + size]
        position += len(frame)
        raw = pcm(frame)
        filtered = guard.filter(raw, start + delay + position / 16000)
        results.append((raw, filtered, guard.last_suppressed))
        index += 1
    return results


def test_no_reference_and_explicit_clear_preserve_original_bytes():
    guard = EchoGuard()
    raw = pcm(signal(0.02))
    assert guard.filter(raw, 100.02) is raw
    guard.set_reference(pcm(signal()), 100)
    guard.clear()
    assert guard.filter(raw, 100.02) is raw
    assert not guard.last_suppressed


@pytest.mark.parametrize("delay,gain", [(0.0, 0.6), (0.18, 0.35), (0.48, -0.7)])
def test_delayed_scaled_reference_with_small_noise_is_gated(delay, gain):
    reference = np.frombuffer(pcm(signal()), dtype="<i2").astype(float)
    microphone = reference * gain + np.random.default_rng(6).normal(0, 4, len(reference))
    guard = EchoGuard()
    guard.set_reference(pcm(reference), 100)
    results = feed(guard, microphone, delay=delay)
    assert sum(suppressed for _, _, suppressed in results[4:]) >= len(results[4:]) * 0.98
    assert all(len(raw) == len(filtered) for raw, filtered, _ in results)
    assert all(filtered == bytes(len(raw)) for raw, filtered, suppressed in results if suppressed)
    assert all(raw == filtered for raw, filtered, suppressed in results if not suppressed)


def test_double_talk_is_preserved_even_with_dominant_reference_audio():
    reference = np.frombuffer(pcm(signal()), dtype="<i2").astype(float)
    user = signal(seed=17) * 0.08  # ~240 PCM RMS; reference is much louder.
    guard = EchoGuard()
    guard.set_reference(pcm(reference), 100)
    results = feed(guard, reference + user)
    assert not any(suppressed for _, _, suppressed in results)
    assert all(raw == filtered for raw, filtered, _ in results)


def test_new_user_onset_is_preserved_on_its_first_frame():
    reference = np.frombuffer(pcm(signal()), dtype="<i2").astype(float)
    mixed = reference.copy()
    mixed[1600:1920] += signal(0.02, seed=81) * 0.03  # user ~90 RMS, echo ~3000 RMS
    guard = EchoGuard()
    guard.set_reference(pcm(reference), 100)
    results = feed(guard, mixed)
    assert results[4][2]  # prior clean echo is gated
    assert not results[5][2]  # first 20 ms of the added user voice is passed
    assert results[5][0] == results[5][1]


def test_unrelated_speech_is_preserved_with_active_reference():
    guard = EchoGuard()
    guard.set_reference(pcm(signal()), 100)
    results = feed(guard, signal(seed=424))
    assert not any(suppressed for _, _, suppressed in results)


def test_expired_or_future_reference_does_not_gate_audio():
    reference = pcm(signal(0.2))
    guard = EchoGuard()
    guard.set_reference(reference, 100)
    raw = reference[:640]
    assert guard.filter(raw, 99.9) is raw
    assert guard.last_decision["reason"] == "playback_not_started"
    assert guard.filter(raw, 101) is raw
    assert guard.last_decision["reason"] == "reference_expired"
    assert not guard._reference.size


def test_irregular_small_frames_keep_state_without_delaying_output():
    reference = np.frombuffer(pcm(signal()), dtype="<i2").astype(float)
    guard = EchoGuard()
    guard.set_reference(pcm(reference), 100)
    results = feed(guard, reference * 0.45, frame_sizes=(80, 320, 160, 640))
    assert sum(suppressed for _, _, suppressed in results[8:]) >= len(results[8:]) * 0.98
    assert all(len(raw) == len(filtered) for raw, filtered, _ in results)
    assert results[0][0] == results[0][1]  # not enough evidence: preserve immediately


def test_timestamp_gap_and_new_reference_reset_analysis_history():
    reference = pcm(signal())
    guard = EchoGuard()
    guard.set_reference(reference, 100)
    for index in range(4):
        guard.filter(reference[index * 640:(index + 1) * 640], 100.18 + (index + 1) * 0.02)
    assert guard.last_suppressed
    frame = reference[12800:13440]
    assert guard.filter(frame, 101.0) is frame
    assert guard.last_decision["reason"] == "insufficient_history"
    guard.set_reference(reference, 200)
    assert guard.filter(reference[:640], 200.2) == reference[:640]
    assert guard.last_decision["reason"] == "insufficient_history"


def test_long_batched_input_is_not_muted_from_a_short_tail_match():
    reference = pcm(signal())
    guard = EchoGuard()
    guard.set_reference(reference, 100)
    frame = reference[:6400]
    assert guard.filter(frame, 100.38) is frame
    assert guard.last_decision["reason"] == "block_too_long"


def test_100ms_batch_with_user_onset_outside_analysis_tail_is_preserved():
    reference = np.frombuffer(pcm(signal()), dtype="<i2").astype(float)
    guard = EchoGuard()
    guard.set_reference(pcm(reference), 100)
    feed(guard, reference[:1600])
    frame = reference[1600:3200].copy()
    frame[:320] += signal(0.02, seed=77) * 0.2
    raw = pcm(frame)
    assert guard.filter(raw, 100.38) is raw
    assert not guard.last_suppressed


def test_alignment_never_leaves_part_of_the_current_block_unchecked():
    reference = pcm(signal())
    guard = EchoGuard()
    guard.set_reference(reference, 100)
    frame = reference[:1279 * 2]
    assert guard.filter(frame, 100.18 + 1279 / 16000) is frame
    assert guard.last_decision["reason"] == "insufficient_aligned_history"


def test_reference_size_is_bounded_and_truncation_is_visible():
    guard = EchoGuard()
    guard.set_reference(bytes((MAX_REFERENCE_SAMPLES + 100) * 2), 100)
    assert len(guard._reference) == MAX_REFERENCE_SAMPLES
    assert guard.reference_truncated


def make_wav(values, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(pcm(values))
    return output.getvalue()


def test_decode_native_wav_preserves_pcm_exactly():
    values = signal(0.1)
    assert decode_reference(make_wav(values), "audio/wav") == pcm(values)


def test_decode_stereo_48khz_wav_resamples_to_16khz_mono():
    pytest.importorskip("av")
    samples = np.arange(4800)
    tone = np.sin(2 * np.pi * 500 * samples / 48000) * 2000
    stereo = np.column_stack((tone, tone)).reshape(-1)
    decoded = decode_reference(make_wav(stereo, rate=48000, channels=2), "audio/wav")
    assert abs(len(decoded) - 3200) <= 4
    assert np.frombuffer(decoded, dtype="<i2").std() > 1000


def test_decode_mp3_generated_in_memory_preserves_duration():
    av = pytest.importorskip("av")
    if "libmp3lame" not in av.codecs_available:
        pytest.skip("This PyAV build has no MP3 encoder for the synthetic fixture")
    output = io.BytesIO()
    with av.open(output, mode="w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=24000)
        stream.layout = "mono"
        values = (np.sin(2 * np.pi * 500 * np.arange(4800) / 24000) * 0.1).astype(np.float32)
        frame = av.AudioFrame.from_ndarray(values.reshape(1, -1), format="fltp", layout="mono")
        frame.sample_rate = 24000
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    decoded = decode_reference(output.getvalue(), "audio/mpeg")
    assert abs(len(decoded) - 6400) <= 4
    assert np.frombuffer(decoded, dtype="<i2").std() > 1000


def test_decode_rejects_unrecognized_media_and_damaged_wav():
    with pytest.raises(ValueError, match="MIME"):
        decode_reference(b"#EXTM3U", "application/x-mpegURL")
    with pytest.raises(ValueError, match="damaged"):
        decode_reference(b"not a WAV", "audio/wav")


def test_synthetic_processing_cost_is_reported_not_a_latency_guarantee():
    reference = signal(4)
    guard = EchoGuard()
    guard.set_reference(pcm(reference), 100)
    began = time.perf_counter()
    frames = feed(guard, reference * 0.5)
    elapsed = time.perf_counter() - began
    print({"synthetic_frames": len(frames), "total_ms": round(elapsed * 1000, 2),
           "mean_filter_ms": round(elapsed * 1000 / len(frames), 3)})
    assert sum(suppressed for _, _, suppressed in frames[4:]) > 180
