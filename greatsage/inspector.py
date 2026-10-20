"""Read-only local runtime inspection. No prompts, tool arguments or credentials."""
from datetime import datetime, timezone
import json


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
            "mcp_tools": len(runtime.tasks.mcp.specs), "entity_memory": "planned", "multi_agent": "planned"}
