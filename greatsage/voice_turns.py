"""Small source-local turn records around Pipecat/SDK endpoints.

The provider owns recognition; these records only retain GreatSage's local
boundaries, provenance and bounded pending work. No network or model calls.
"""

MAX_PENDING_TURNS = 4


def turn_id(identity, version):
    return f"{identity.session_id}:{identity.generation}:{identity.source_generation}:{identity.source}:{version}"


def records(state):
    if not state:
        return []
    return [*state.get("pending", []), *([state["turn"]] if state.get("turn") else [])]


def make_turn(identity, version, settled_end, event):
    return {"id": turn_id(identity, version), "text": [], "chars": 0,
            "end_offset": settled_end, "local_end": False, "remote_end": False,
            "discard": False, "remote_end_offset": -1.0, "speech_end": 0,
            "pcm": bytearray(), "recording_overflow": False,
            "version": version, "has_segment": False, "timeout": None,
            "media_start": event.media_start, "media_end": None, "voice_end": None}


def activity(state):
    turns = records(state)
    speaking = any(not turn["local_end"] for turn in turns)
    pending = sum(turn["local_end"] for turn in turns) + (state or {}).get("committing", 0)
    return {"speech_phase": "speaking" if speaking else "finalizing" if pending else "idle",
            "pending_turns": pending}
