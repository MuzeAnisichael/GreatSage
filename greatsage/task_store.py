"""SQLite task, step and tool-call persistence for MemoryStore.

This mixin shares its owner's database, lock and artifact access. It does not
import model providers, graph orchestration or tool execution.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

ACTIVE = ("queued", "running", "waiting_approval")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class TaskStoreMixin:
    def _init_tasks(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                origin TEXT NOT NULL, session_id TEXT NOT NULL, trace_id TEXT NOT NULL, params TEXT NOT NULL,
                state TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_steps (
                task_id TEXT NOT NULL, ordinal INTEGER NOT NULL, name TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL, result TEXT, started_at TEXT, finished_at TEXT,
                PRIMARY KEY(task_id, ordinal));
            CREATE TABLE IF NOT EXISTS tool_calls (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, call_key TEXT NOT NULL, tool TEXT NOT NULL,
                arguments TEXT NOT NULL, effect TEXT NOT NULL, target TEXT NOT NULL, decision TEXT NOT NULL,
                reason TEXT NOT NULL, status TEXT NOT NULL, approval TEXT, result TEXT, undo TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(task_id, call_key));
        """)

    @staticmethod
    def _decode(row, *fields) -> dict:
        value = dict(row)
        for field in fields:
            if value.get(field) is not None:
                value[field] = json.loads(value[field])
        return value

    def create_task(self, kind: str, title: str, origin: str, session_id: str, trace_id: str, params: dict,
                    steps: list[tuple[str, str]], state: dict | None = None) -> str:
        id, stamp = uuid.uuid4().hex, _now()
        with self._lock, self._db:
            self._db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (id, kind, title[:120], "queued", origin, session_id, trace_id, _json(params),
                              _json(state or {}), None, stamp, stamp))
            self._db.executemany("INSERT INTO task_steps VALUES (?,?,?,?,?,?,?,?,?)",
                                 [(id, ordinal, name, step_title, "pending", "{}", None, None, None)
                                  for ordinal, (name, step_title) in enumerate(steps)])
        return id

    def update_task(self, id: str, **fields) -> None:
        for key in ("params", "state"):
            if key in fields:
                fields[key] = _json(fields[key])
        with self._lock, self._db:
            assignments = ",".join(f"{key}=?" for key in fields)
            self._db.execute(f"UPDATE tasks SET {assignments},updated_at=? WHERE id=?", [*fields.values(), _now(), id])

    def update_step(self, task_id: str, name: str, **fields) -> None:
        for key in ("detail", "result"):
            if key in fields and fields[key] is not None:
                fields[key] = _json(fields[key])
        with self._lock, self._db:
            assignments = ",".join(f"{key}=?" for key in fields)
            self._db.execute(f"UPDATE task_steps SET {assignments} WHERE task_id=? AND name=?", [*fields.values(), task_id, name])

    def step(self, task_id: str, name: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM task_steps WHERE task_id=? AND name=?", (task_id, name)).fetchone()
            if not row:
                raise KeyError(f"{task_id}:{name}")
            return self._decode(row, "detail", "result")

    def task(self, id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (id,)).fetchone()
            if not row:
                raise KeyError(id)
            steps = [self._decode(item, "detail") for item in self._db.execute(
                "SELECT task_id,ordinal,name,title,status,detail,started_at,finished_at FROM task_steps WHERE task_id=? ORDER BY ordinal", (id,))]
            calls = [self.tool_call(item[0]) for item in self._db.execute(
                "SELECT id FROM tool_calls WHERE task_id=? ORDER BY created_at", (id,))]
        return {**self._decode(row, "params", "state"), "steps": steps, "tool_calls": calls,
                "artifacts": self.artifacts(task_id=id)}

    def task_summary(self, id: str) -> dict:
        task = self.task(id)
        current = next((step for step in task["steps"] if step["status"] in ("running", "pending")), None)
        done = sum(step["status"] == "succeeded" for step in task["steps"])
        pending = sum(call["status"] == "awaiting_approval" for call in task["tool_calls"])
        return {key: task[key] for key in ("id", "kind", "title", "status", "origin", "session_id", "trace_id", "error",
                                           "created_at", "updated_at")} | {
            "steps_done": done, "steps_total": len(task["steps"]), "pending_approvals": pending,
            "current_step": current["title"] if current else "", "progress": (current or {}).get("detail", {}).get("progress", ""),
            "artifact_ids": [item["id"] for item in task["artifacts"]]}

    def tasks(self, limit: int = 50) -> list[dict]:
        with self._lock:
            ids = [row[0] for row in self._db.execute("SELECT id FROM tasks ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),))]
        return [self.task_summary(id) for id in ids]

    def finish_task(self, id: str, status: str, error: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE task_steps SET status=?,finished_at=? WHERE task_id=? AND status='running'",
                             (status if status != "succeeded" else "succeeded", _now(), id))
            self._db.execute("UPDATE tasks SET status=?,error=?,updated_at=? WHERE id=?", (status, error, _now(), id))

    def reset_unfinished_steps(self, id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE task_steps SET status='pending',detail='{}',started_at=NULL,finished_at=NULL WHERE task_id=? AND status!='succeeded'", (id,))
            self._db.execute("UPDATE tasks SET status='queued',error=NULL,updated_at=? WHERE id=?", (_now(), id))

    def recover_tasks(self) -> list[str]:
        """After a restart nothing is running: mark work interrupted, never replay it."""
        with self._lock, self._db:
            ids = [row[0] for row in self._db.execute(f"SELECT id FROM tasks WHERE status IN {ACTIVE}")]
            self._db.execute("UPDATE task_steps SET status='interrupted' WHERE status='running'")
            self._db.execute("UPDATE tool_calls SET status='unknown',error='应用在执行期间关闭，结果未知，不会自动重试。',updated_at=? WHERE status='running'", (_now(),))
            self._db.execute("UPDATE tool_calls SET status='expired',approval=NULL,updated_at=? WHERE status IN ('awaiting_approval','proposed','approved')", (_now(),))
            self._db.execute(f"UPDATE tasks SET status='interrupted',updated_at=? WHERE status IN {ACTIVE}", (_now(),))
        return ids

    def purge_task_bodies(self, id: str | None = None) -> None:
        """Drop intermediate copies of source text; artifacts and call metadata remain."""
        where, args = ("WHERE task_id=?", [id]) if id else ("", [])
        with self._lock, self._db:
            self._db.execute(f"UPDATE task_steps SET result=NULL {where} {'AND' if id else 'WHERE'} name IN ('collect','extract','act')", args)
            if id:
                self._db.execute("UPDATE tasks SET state='{}' WHERE id=?", (id,))
            else:
                self._db.execute(f"UPDATE tasks SET state='{{}}' WHERE status NOT IN {ACTIVE}")

    def _clear_task_history(self) -> None:
        self._db.execute("UPDATE task_steps SET result=NULL WHERE name IN ('collect','extract','act')")
        self._db.execute("UPDATE tasks SET state='{}'")
        self._db.execute("""UPDATE tool_calls SET arguments='{"redacted":true}',result=NULL
                            WHERE tool IN ('write_file','create_minutes_task','search_materials','run_command') OR tool LIKE 'mcp_%'""")

    def delete_task(self, id: str) -> None:
        with self._lock, self._db:
            row = self._db.execute("SELECT status FROM tasks WHERE id=?", (id,)).fetchone()
            if not row:
                raise KeyError(id)
            if row[0] in ACTIVE:
                raise ValueError("请先取消正在进行的任务。")
            for table in ("task_steps", "tool_calls"):
                self._db.execute(f"DELETE FROM {table} WHERE task_id=?", (id,))
            self._db.execute("DELETE FROM tasks WHERE id=?", (id,))

    def add_tool_call(self, task_id: str, call_key: str, tool: str, arguments: dict, effect: str, target: str,
                      decision: str, reason: str, status: str, error: str | None = None) -> dict:
        id, stamp = uuid.uuid4().hex, _now()
        with self._lock, self._db:
            self._db.execute("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (id, task_id, call_key, tool, _json(arguments), effect, target[:1000], decision, reason,
                              status, None, None, None, error, stamp, stamp))
        return self.tool_call(id)

    def update_tool_call(self, id: str, **fields) -> dict:
        for key in ("approval", "result", "undo"):
            if key in fields and fields[key] is not None:
                fields[key] = _json(fields[key])
        with self._lock, self._db:
            assignments = ",".join(f"{key}=?" for key in fields)
            self._db.execute(f"UPDATE tool_calls SET {assignments},updated_at=? WHERE id=?", [*fields.values(), _now(), id])
        return self.tool_call(id)

    def tool_call(self, id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tool_calls WHERE id=?", (id,)).fetchone()
            return self._decode(row, "arguments", "approval", "result", "undo") if row else None

    def tool_call_by_key(self, task_id: str, call_key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT id FROM tool_calls WHERE task_id=? AND call_key=?", (task_id, call_key)).fetchone()
        return self.tool_call(row[0]) if row else None

    def pending_tool_calls(self) -> list[dict]:
        with self._lock:
            ids = [row[0] for row in self._db.execute("SELECT id FROM tool_calls WHERE status='awaiting_approval' ORDER BY created_at")]
        return [self.tool_call(id) for id in ids]
