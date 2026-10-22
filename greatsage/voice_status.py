"""Shared metadata-only voice status and explicit recovery guidance."""
import re


VOICE_ERRORS = {
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
    "streaming_asr_turn_overlap", "streaming_asr_turn_too_large", "streaming_asr_turn_queue_full",
}
CONFIGURATION_ERRORS = {
    "streaming_asr_configuration_invalid", "streaming_asr_credentials_required",
    "streaming_asr_endpoint_not_allowed", "streaming_asr_language_not_supported",
    "streaming_asr_model_not_supported",
}
RESTART_ERRORS = {
    "voice_source_stopped", "voice_pipeline_cleanup_failed", "voice_pipeline_cleanup_timeout",
    "streaming_asr_cleanup_failed", "streaming_asr_dependency_unavailable",
}


def valid_source(source):
    return isinstance(source, str) and bool(re.fullmatch(r"(?:microphone(?::(?:default|\d+))?|system|process:\d+)", source))


def safe_error(code):
    return code if isinstance(code, str) and code in VOICE_ERRORS else "voice_pipeline_failed" if code else None


def audio_status(runtime):
    """Do not traverse SDK objects, settings, transcript state or event bodies."""
    pipelines = getattr(runtime, "voice_pipelines", {}) or {}
    disabled = getattr(runtime, "voice_disabled", set()) or set()
    errors = getattr(runtime, "voice_errors", {}) or {}
    captures = getattr(runtime, "capture_sources", {}) or {}
    retries = getattr(runtime, "voice_retries", {}) or {}
    listening = bool(getattr(runtime, "listening", False))
    sources = []
    for source in sorted(item for item in set(pipelines) | set(disabled) | set(captures) | set(errors)
                         if valid_source(item)):
        pipeline = pipelines.get(source)
        capture = captures.get(source)
        code = safe_error(errors.get(source))
        state = "failed" if source in disabled else getattr(pipeline, "state", "starting")
        if capture == "stopped":
            state = "stopped"
        elif source in retries:
            state = "starting"
        elif capture is not None and not listening and not code:
            state = "closed"
        if state not in ("starting", "running", "closed", "failed", "stopped"):
            state = "starting"
        action = None
        if code in CONFIGURATION_ERRORS:
            action = "configure_asr"
        elif capture == "stopped" or code in RESTART_ERRORS:
            action = "restart_listening"
        elif source in disabled:
            action = "retry_source" if listening and capture == "running" else "restart_listening"
        if source in retries:
            action = None
        streaming = getattr(pipeline, "streaming", None)
        pending = getattr(pipeline, "pending_frames", None)
        activity = getattr(runtime, "voice_activity", None)
        detail = activity(source) if callable(activity) else {}
        phase, turns = detail.get("speech_phase"), detail.get("pending_turns")
        sources.append({"source": source, "state": state,
                        "streaming": streaming if isinstance(streaming, bool) else None,
                        "pending_frames": pending if type(pending) is int and pending >= 0 else None,
                        "error_code": code, "recovery_action": action,
                        "speech_phase": phase if phase in ("speaking", "finalizing", "idle") else None,
                        "pending_turns": turns if type(turns) is int and turns >= 0 else None})
    return {"listening": listening, "sources": sources}
