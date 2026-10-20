"""Read-only local runtime inspection. No prompts, tool arguments or credentials."""
from datetime import datetime, timezone
import json
import math
import re


_VOICE_EVENT_LIMIT = 80
_VOICE_METRICS = {
    "first_audio_ms": ("playback", "client_playing_ack_received"),
    "client_event_to_playing_ms": ("playback", "client_playing_ack_received"),
    "interrupt_to_stop_ack_ms": ("playback", "client_pause_ack_received"),
    "audio_ready_ms": ("tts", None),
}
_VOICE_ERRORS = {
    "voice_pipeline_cleanup_failed", "voice_pipeline_cleanup_timeout", "voice_pipeline_failed",
    "voice_pipeline_processor_error", "voice_pipeline_start_failed", "voice_pipeline_start_or_feed_failed",
    "voice_source_audio_gap", "voice_source_backpressure", "voice_source_stopped",
    "streaming_asr_cleanup_failed", "streaming_asr_commit_failed", "streaming_asr_configuration_invalid",
    "streaming_asr_connection_closed", "streaming_asr_connection_failed", "streaming_asr_connection_timeout",
    "streaming_asr_connection_unavailable", "streaming_asr_credentials_required",
    "streaming_asr_dependency_unavailable", "streaming_asr_endpoint_not_allowed",
    "streaming_asr_failed", "streaming_asr_finalize_timeout", "streaming_asr_language_not_supported",
    "streaming_asr_model_not_supported", "streaming_asr_processing_failed", "streaming_asr_restart_required",
    "streaming_asr_segment_overlap", "streaming_asr_timestamp_missing", "streaming_asr_transport_failed",
    "streaming_asr_turn_overlap", "streaming_asr_turn_too_large",
}


def _duration(value):
    if (isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            and (not isinstance(value, float) or math.isfinite(value))):
        return value
    return None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def voice_snapshot(runtime):
    """Whitelist live source metadata and recent measured receipts, never PCM/text.

    Read a bounded tail before filtering, so refreshing cannot scan historical
    transcript/event bodies indefinitely. Metrics are independent latest samples
    within that tail, not a single response or a latency percentile.
    """
    pipelines = getattr(runtime, "voice_pipelines", {}) or {}
    disabled = getattr(runtime, "voice_disabled", set()) or set()
    errors = getattr(runtime, "voice_errors", {}) or {}
    sources = []
    for source in sorted(item for item in set(pipelines) | set(disabled) if isinstance(item, str)):
        if not re.fullmatch(r"(?:microphone(?::(?:default|\d+))?|system|process:\d+)", source):
            continue
        pipeline = pipelines.get(source)
        state = "failed" if source in disabled else getattr(pipeline, "state", "starting")
        if state not in ("starting", "running", "closed", "failed"):
            state = "starting"
        streaming = getattr(pipeline, "streaming", None)
        pending = _duration(getattr(pipeline, "pending_frames", None))
        code = errors.get(source)
        sources.append({
            "source": source, "state": state,
            "streaming": streaming if isinstance(streaming, bool) else None,
            "pending_frames": pending if isinstance(pending, int) else None,
            "error_code": code if isinstance(code, str) and code in _VOICE_ERRORS else "voice_pipeline_failed" if code else None,
        })
    metrics = {name: None for name in _VOICE_METRICS}
    for event in runtime.memory.events(limit=_VOICE_EVENT_LIMIT):
        data = event.get("data", {})
        if not isinstance(data, dict):
            continue
        if event.get("kind") in ("session", "started"):
            break  # Earlier observations belong to a preceding session/runtime.
        if event.get("kind") != "metrics" or data.get("session_id", runtime.session_id) != runtime.session_id:
            continue
        for name, (component, measurement) in _VOICE_METRICS.items():
            value = _duration(data.get(name))
            if (metrics[name] is not None or value is None or data.get("component") != component
                    or (measurement and data.get("measurement") != measurement)):
                continue
            metrics[name] = {"value_ms": value, "created_at": _timestamp(event.get("created_at"))}
            if name == "interrupt_to_stop_ack_ms" and isinstance(data.get("had_audio"), bool):
                metrics[name]["had_audio"] = data["had_audio"]
    return {"sources": sources, "metrics": metrics, "recent_event_limit": _VOICE_EVENT_LIMIT}


async def inspect_runtime(runtime):
    memory = runtime.memory
    rows = memory.tasks(30)
    tasks = []
    for row in rows:
        try:
            task = memory.task(row["id"])
        except KeyError:
            continue
        graph = await runtime.tasks.graphs.inspect(task)
        tasks.append({**row, "graph": graph})
    with memory._lock:
        counts = {"history_messages": memory._db.execute("SELECT count(*) FROM messages").fetchone()[0],
                  "active_memories": memory._db.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0],
                  "summaries": memory._db.execute("SELECT count(*) FROM summaries").fetchone()[0],
                  "conflicts": memory._db.execute("SELECT count(*) FROM memory_conflicts").fetchone()[0]}
        active_tasks = memory._db.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running','waiting_approval')").fetchone()[0]
        events = memory._db.execute("SELECT id,kind,created_at,data FROM events WHERE kind IN ('memory_updated','compression_done') ORDER BY rowid DESC LIMIT 12").fetchall()
        changes = []
        for event in events:
            data = json.loads(event["data"])
            action = data.get("action") if data.get("action") in ("revise", "delete", "resolve_conflict") else (
                "clear" if data.get("cleared") else "delete" if data.get("deleted_id") else "update")
            changes.append({"id": event["id"], "kind": event["kind"], "created_at": event["created_at"], "action": action})
    settings = runtime.settings.raw()
    return {"at": datetime.now(timezone.utc).isoformat(), "session_id": runtime.session_id,
            "state": runtime.state, "listening": runtime.listening, "engine": "langgraph",
            "tasks": tasks, "active_tasks": active_tasks, "requests": memory.snapshots(limit=20), "memory": {**counts, "changes": changes},
            "background": runtime.background.snapshot(), "audit_content": settings["audit_content"],
            "model": {"provider": settings["llm"]["provider"], "name": settings["llm"]["model"]},
            "mcp_tools": len(runtime.tasks.mcp.specs), "voice": voice_snapshot(runtime),
            "entity_memory": "planned", "multi_agent": "planned"}
