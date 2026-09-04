import struct

import pytest

from greatsage.segmentation import Segmenter


VOICE = struct.pack("<h", 1000) * 480
QUIET = b"\0" * 960


class Detector:
    def is_speech(self, frame, rate):
        assert len(frame) == 960 and rate == 16000
        return True


def feed_frames(segmenter, frames, start=1000.0):
    return segmenter.feed(b"".join(frames), start + len(frames) * .03)


def test_speech_start_interrupt_signal_arrives_after_three_frames():
    segmenter = Segmenter(detector=Detector())
    assert not feed_frames(segmenter, [VOICE] * 2)
    events = feed_frames(segmenter, [VOICE], 1000.06)
    assert [event.kind for event in events] == ["start"]


def test_short_noise_is_discarded_and_minimum_speech_is_not_rounded_down():
    segmenter = Segmenter(min_speech_ms=250, detector=Detector())
    events = feed_frames(segmenter, [VOICE] * 8 + [QUIET] * 19)
    assert [event.kind for event in events] == ["start", "discard"]
    assert not segmenter.active


def test_final_event_tracks_actual_speech_end_and_limits_silence_tail():
    segmenter = Segmenter(silence_ms=550, min_speech_ms=250, detector=Detector())
    events = feed_frames(segmenter, [VOICE] * 9 + [QUIET] * 19)
    final = next(event for event in events if event.kind == "final")
    assert final.speech_end == pytest.approx(1000.27)
    assert final.pcm == VOICE * 9 + QUIET * 3
    assert not final.continued


def test_partial_and_maximum_duration_keep_continuous_speech_bounded():
    segmenter = Segmenter(silence_ms=90, min_speech_ms=90, max_seconds=.6, partial_seconds=.3, detector=Detector())
    events = feed_frames(segmenter, [VOICE] * 45 + [QUIET] * 3)
    finals = [event for event in events if event.kind == "final"]
    assert [event.continued for event in finals] == [True, True, False]
    assert all(len(event.pcm) <= 20 * 960 for event in finals)
    assert any(event.kind == "partial" for event in events)
    assert len([event for event in events if event.kind == "start"]) == 1
    assert not segmenter.active


def test_pcm_split_across_arbitrary_chunk_boundaries_preserves_frames():
    segmenter = Segmenter(silence_ms=90, min_speech_ms=90, detector=Detector())
    pcm = VOICE * 5 + QUIET * 3
    offset = 0
    events = []
    for length in (311, 2000, 17, len(pcm)):
        chunk = pcm[offset:offset + length]
        offset += len(chunk)
        events.extend(segmenter.feed(chunk, 1000 + offset / 32000))
    final = next(event for event in events if event.kind == "final")
    assert final.pcm == pcm
    assert final.speech_end == pytest.approx(1000.15)


@pytest.mark.parametrize("kwargs", [{"silence_ms": 0}, {"partial_seconds": float("nan")}, {"max_seconds": .01, "min_speech_ms": 500}])
def test_invalid_durations_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Segmenter(detector=Detector(), **kwargs)
