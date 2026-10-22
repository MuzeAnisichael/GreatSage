"""Voice inspector metadata boundaries, without devices or model requests."""
import json
from types import SimpleNamespace

from greatsage.inspector import voice_snapshot


class RecentEvents:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.limits = []

    def events(self, limit):
        self.limits.append(limit)
        return self.rows[:limit]


def runtime(rows=(), **values):
    return SimpleNamespace(
        memory=RecentEvents(rows), session_id="current",
        voice_pipelines=values.get("voice_pipelines", {}),
        voice_disabled=values.get("voice_disabled", set()),
        voice_errors=values.get("voice_errors", {}),
        streaming_turns={"private": {"text": "PRIVATE_TRANSCRIPT"}},
        settings={"api_key": "PRIVATE_CREDENTIAL"},
    )


def metric(component, **data):
    return {"kind": "metrics", "created_at": "2026-10-21T08:00:00+00:00",
            "data": {"component": component, "text": "PRIVATE_TRANSCRIPT",
                     "api_key": "PRIVATE_CREDENTIAL", **data}}


def test_source_metadata_does_not_expose_sdk_payloads_or_private_turns():
    active = SimpleNamespace(state="running", streaming=True, pending_frames=3,
                             api_key="PRIVATE_CREDENTIAL", transcript="PRIVATE_TRANSCRIPT")
    starting = SimpleNamespace(state="starting", streaming=False, pending_frames=0)
    subject = runtime(
        voice_pipelines={"microphone:2": active, "system": starting},
        voice_disabled={"process:91", "microphone:9"},
        voice_errors={"process:91": "streaming_asr_transport_failed",
                      "microphone:9": "PRIVATE_CREDENTIAL PRIVATE_TRANSCRIPT"},
    )
    result = voice_snapshot(subject)
    sources = {row["source"]: row for row in result["sources"]}
    assert sources["microphone:2"] == {
        "source": "microphone:2", "state": "running", "streaming": True,
        "pending_frames": 3, "error_code": None, "recovery_action": None}
    assert sources["system"]["state"] == "starting"
    assert sources["system"]["pending_frames"] == 0
    assert sources["process:91"]["pending_frames"] is None
    assert sources["process:91"]["error_code"] == "streaming_asr_transport_failed"
    assert sources["microphone:9"]["error_code"] == "voice_pipeline_failed"
    assert "PRIVATE_" not in json.dumps(result)
    assert all(value is None for value in result["metrics"].values())


def test_metric_tail_is_bounded_and_durations_keep_missing_distinct_from_zero():
    rows = [
        metric("playback", measurement="client_playing_ack_received",
               first_audio_ms=1234, client_event_to_playing_ms=None),
        metric("playback", measurement="client_playing_ack_received",
               first_audio_ms=999, client_event_to_playing_ms=0),
        metric("playback", measurement="client_pause_ack_received",
               interrupt_to_stop_ack_ms=78, had_audio=False),
        metric("tts", audio_ready_ms=876),
    ]
    subject = runtime(rows)
    result = voice_snapshot(subject)
    assert subject.memory.limits == [80]
    assert {key: value["value_ms"] for key, value in result["metrics"].items()} == {
        "first_audio_ms": 1234, "client_event_to_playing_ms": 0,
        "interrupt_to_stop_ack_ms": 78, "audio_ready_ms": 876}
    assert result["metrics"]["interrupt_to_stop_ack_ms"]["had_audio"] is False
    assert "PRIVATE_" not in json.dumps(result)
    # An old useful sample is deliberately not found through an unbounded scan.
    subject.memory.rows = [{"kind": "ignored", "data": {}}] * 80 + rows
    assert all(value is None for value in voice_snapshot(subject)["metrics"].values())


def test_bad_or_old_measurements_do_not_look_like_current_playback_evidence():
    subject = runtime([
        metric("playback", measurement="client_playing_ack_received",
               first_audio_ms=float("nan"), client_event_to_playing_ms=True),
        metric("playback", measurement="client_pause_ack_received",
               interrupt_to_stop_ack_ms=-1),
        metric("tts", audio_ready_ms=float("inf")),
        metric("tts", first_audio_ms=100),  # Historical ready-time estimates are not receipts.
        metric("playback", first_audio_ms=100),
        metric("playback", measurement="client_playing_ack_received",
               first_audio_ms=500, session_id="previous"),
        {"kind": "session", "data": {"session_id": "current"}},
        metric("tts", audio_ready_ms=200),
    ])
    assert all(value is None for value in voice_snapshot(subject)["metrics"].values())
