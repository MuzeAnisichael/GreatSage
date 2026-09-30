"""Persistent background tasks that run beside the conversation.

A task and its steps are stored before work starts. Completed steps keep their
results, so a restarted task continues from its first unfinished step. A tool
call that was running when the app stopped is marked unknown and is never
replayed. New speech interrupts a conversational reply, but not a task.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone

from .budget import TokenCounter
from .materials import chunk_label
from .providers import ProviderError
from .tools import (EFFECT_LABELS, NO_STANDING_ALLOW, SPECS, VOICE_APPROVABLE, ToolError, ToolRunner, decide,
                    definitions, validate_arguments, voice_intent)
from .workflows import (EXTRACT_SCHEMA, SUMMARY_SCHEMA, clean_extraction, clean_markers, document_messages,
                        extract_messages, keep_sources, language_name, merge_extractions, render_minutes,
                        summary_messages)
from .artifacts import todo_line

ACTIVE = ("queued", "running", "waiting_approval")
FINISHED_CALLS = ("succeeded", "failed", "denied", "unknown", "undone")
MAX_ROUNDS = 4
# A model call has no side effects, so a transient provider failure is retried
# after a short backoff. A client-side timeout is not retried: it has already
# waited the full timeout and a retry would hold the single task slot for minutes.
RETRY_DELAYS = (1.0, 3.0)
RETRYABLE = {"upstream_error", "upstream_stream_error", "upstream_unavailable", "rate_limited",
             "stream_ended_before_completion", "connection_failed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class TaskError(RuntimeError):
    """A safe, user-facing task failure."""


def assistant_calls(text: str, calls: list[dict]) -> dict:
    return {"role": "assistant", "content": text or "",
            "tool_calls": [{"id": call["id"], "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]}} for call in calls]}


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
            self._db.execute("UPDATE tool_calls SET status='expired',updated_at=? WHERE status IN ('awaiting_approval','proposed','approved')", (_now(),))
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
                            WHERE tool IN ('write_file','create_minutes_task','search_materials','run_command')""")

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


class TaskManager:
    def __init__(self, runtime):
        self.runtime = runtime
        self.jobs: dict[str, asyncio.Task] = {}
        self.approvals: dict[str, asyncio.Future] = {}
        self.grants: dict[str, set[str]] = {}
        self.slot = asyncio.Semaphore(1)
        self.closing = False
        self.runner = ToolRunner(runtime.data_dir, runtime.memory, runtime.settings.raw)

    @property
    def memory(self):
        return self.runtime.memory

    # ---- lifecycle -------------------------------------------------------
    def recover(self) -> list[str]:
        return self.memory.recover_tasks()

    async def close(self) -> None:
        self.closing = True
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def changed(self, id: str) -> None:
        summary = self.memory.task_summary(id)
        await self.runtime.emit("task_updated", summary, summary["trace_id"], persist=False)

    def _schedule(self, id: str) -> None:
        job = asyncio.create_task(self._run(id))
        self.jobs[id] = job
        job.add_done_callback(lambda _: self.jobs.pop(id, None))

    async def cancel(self, id: str) -> dict:
        task = self.memory.task(id)
        if task["status"] not in ACTIVE:
            raise ValueError("任务不在进行中。")
        job = self.jobs.get(id)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        else:
            self.memory.finish_task(id, "cancelled", None)
            await self.changed(id)
        return self.memory.task_summary(id)

    async def resume(self, id: str) -> dict:
        task = self.memory.task(id)
        if task["status"] not in ("interrupted", "failed", "cancelled"):
            raise ValueError("只有中断、失败或已取消的任务可以继续。")
        if task["kind"] == "action" and not task["state"].get("messages"):
            raise ValueError("这项操作的上下文已清除，请重新发起请求。")
        self.memory.reset_unfinished_steps(id)
        await self.changed(id)
        self._schedule(id)
        return self.memory.task_summary(id)

    async def _run(self, id: str) -> None:
        status, error = "succeeded", None
        try:
            async with self.slot:
                task = self.memory.task(id)
                if task["status"] != "queued":
                    return
                self.memory.update_task(id, status="running", error=None)
                await self.changed(id)
                await (self._run_minutes if task["kind"] == "minutes" else self._run_action)(task)
        except asyncio.CancelledError:
            status = "interrupted" if self.closing else "cancelled"
            raise
        except (TaskError, ToolError, ValueError, KeyError) as exc:
            status, error = "failed", str(exc)[:500]
        except ProviderError as exc:
            status, error = "failed", f"模型服务失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - reported as a task failure, never swallowed silently
            status, error = "failed", f"任务失败：{type(exc).__name__}"
        finally:
            for call_id in [key for key, future in self.approvals.items() if not future.done()]:
                call = self.memory.tool_call(call_id)
                if call and call["task_id"] == id:
                    self.approvals.pop(call_id).cancel()
                    self.memory.update_tool_call(call_id, status="expired")
            self.grants.pop(id, None)
            if self.memory.task(id)["status"] in ACTIVE:
                self.memory.finish_task(id, status, error)
                if status == "succeeded":
                    self.memory.purge_task_bodies(id)
                await self.changed(id)
                await self.runtime.emit("task_finished", {"task_id": id, "status": status, "error": error},
                                        self.memory.task(id)["trace_id"])

    async def _step(self, task_id: str, name: str, work):
        step = self.memory.step(task_id, name)
        if step["status"] == "succeeded":
            return step["result"]
        self.memory.update_step(task_id, name, status="running", started_at=_now(), finished_at=None, detail={})
        await self.changed(task_id)
        result = await work()
        detail = self.memory.step(task_id, name)["detail"]
        self.memory.update_step(task_id, name, status="succeeded", finished_at=_now(), result=result, detail=detail)
        await self.changed(task_id)
        return result

    async def _progress(self, task_id: str, name: str, **values) -> None:
        detail = self.memory.step(task_id, name)["detail"]
        detail.update(values)
        self.memory.update_step(task_id, name, detail=detail)
        await self.changed(task_id)

    # ---- model calls -----------------------------------------------------
    def _llm(self, output_tokens: int) -> dict:
        llm = self.runtime._provider("llm")
        llm["max_tokens"] = max(256, min(output_tokens, llm.get("context_tokens", 8192) // 3))
        return llm

    def _budget(self, llm: dict) -> int:
        return llm.get("context_tokens", 8192) - llm["max_tokens"] - 256

    async def _model(self, task: dict, step: str, purpose: str, messages: list, config: dict,
                     source_ids: list[str]) -> tuple[str, list[dict]]:
        for attempt, delay in enumerate(RETRY_DELAYS + (None,), 1):
            try:
                return await self._model_once(task, step, purpose, messages, config, source_ids, attempt)
            except ProviderError as exc:
                if delay is None or not (exc.code in RETRYABLE or exc.status == 408):
                    raise
                detail = self.memory.step(task["id"], step)["detail"]
                retries = detail.get("retries", []) + [{"purpose": purpose, "code": exc.code, "attempt": attempt}]
                await self._progress(task["id"], step, retries=retries, retrying=f"{attempt}/{len(RETRY_DELAYS)}")
                await asyncio.sleep(delay)

    async def _model_once(self, task: dict, step: str, purpose: str, messages: list, config: dict,
                          source_ids: list[str], attempt: int) -> tuple[str, list[dict]]:
        trace = task["trace_id"]
        snapshot = await self.runtime.snapshot_request(trace, purpose, messages, source_ids, provider=config)
        started, first, text, calls, usage = time.monotonic(), None, "", [], {}
        try:
            async for item in self.runtime.providers.stream_chat(config, messages):
                if item.get("text"):
                    first = first or time.monotonic()
                    text += item["text"]
                    if len(text) > 400_000:
                        raise TaskError("模型输出过长。")
                if item.get("tool_calls"):
                    calls = item["tool_calls"]
                if item.get("usage"):
                    usage = item["usage"]
                    await self.runtime.emit("usage", {"component": "llm", "purpose": purpose, "usage": usage,
                                                      "task_id": task["id"], "source_ids": source_ids}, trace)
            self.memory.finish_snapshot(snapshot, "completed")
        except asyncio.CancelledError:
            self.memory.finish_snapshot(snapshot, "cancelled")
            raise
        except Exception:
            self.memory.finish_snapshot(snapshot, "failed")
            raise
        timing = {"purpose": purpose, "latency_ms": round((time.monotonic() - started) * 1000),
                  "first_token_ms": round((first - started) * 1000) if first else None, "usage": usage,
                  "attempt": attempt}
        detail = self.memory.step(task["id"], step)["detail"]
        detail["calls"] = detail.get("calls", []) + [timing]
        detail.pop("retrying", None)
        self.memory.update_step(task["id"], step, detail=detail)
        await self.runtime.emit("metrics", {"component": "task", "task_id": task["id"], **timing}, trace)
        return text, calls

    async def _model_json(self, task, step, purpose, messages, llm, schema, source_ids) -> dict:
        text, _ = await self._model(task, step, purpose, messages, {**llm, "json_schema": schema, "temperature": 0}, source_ids)
        try:
            return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
        except json.JSONDecodeError:
            raise TaskError("模型没有返回有效的结构化结果。") from None

    # ---- minutes ---------------------------------------------------------
    async def start_minutes(self, params: dict, origin: str, session_id: str, trace_id: str = "") -> dict:
        params = dict(params)
        material_ids = [str(item) for item in params.get("material_ids", [])][:50]
        if params.get("material_query"):
            for chunk in self.memory.search_chunks(str(params["material_query"])[:500], 12):
                if chunk["material_id"] not in material_ids and len(material_ids) < 5:
                    material_ids.append(chunk["material_id"])
        known = {item["id"] for item in self.memory.materials()}
        if set(material_ids) - known:
            raise ValueError("所选资料不存在或已删除。")
        sessions = params.get("session_ids")
        if sessions is None:
            sessions = [session_id] if params.get("use_session", True) else []
        title = str(params.get("title") or "").strip()[:80] or "会议纪要 " + datetime.now().strftime("%m-%d %H:%M")
        clean = {"session_ids": [str(item) for item in sessions][:20], "material_ids": material_ids,
                 "instructions": str(params.get("instructions") or "")[:4000], "document": bool(params.get("document")),
                 "revise": params.get("revise") or {}}
        steps = [("collect", "收集来源"), ("extract", "抽取要点与待办"), ("compose", "生成纪要与待办")]
        if clean["document"]:
            steps.append(("document", "撰写文档草稿"))
        id = self.memory.create_task("minutes", title, origin, session_id, trace_id or uuid.uuid4().hex, clean, steps)
        await self.changed(id)
        self._schedule(id)
        return self.memory.task_summary(id)

    async def regenerate(self, artifact_id: str, instructions: str) -> dict:
        artifact = self.memory.artifact(artifact_id)
        original = self.memory.task(artifact["task_id"])
        params = dict(original["params"])
        note = str(instructions or "").strip()[:4000]
        if note:
            params["instructions"] = (params.get("instructions", "") + "\n用户纠正：" + note).strip()
        params["revise"] = {item["kind"]: item["id"] for item in original["artifacts"]}
        params["document"] = params.get("document") or "document" in params["revise"]
        return await self.start_minutes({**params, "title": original["title"]}, "console", original["session_id"])

    def _records(self, refs: list[dict]) -> list[dict]:
        messages = {row["id"]: row for row in self._messages([ref["id"] for ref in refs if ref["kind"] == "message"])}
        chunks = {row["id"]: row for row in self.memory.material_chunks(ids=[ref["id"] for ref in refs if ref["kind"] == "chunk"])}
        records = []
        for ref in refs:
            if ref["kind"] == "message" and ref["id"] in messages:
                row = messages[ref["id"]]
                when = datetime.fromisoformat(row["created_at"]).astimezone().strftime("%m-%d %H:%M")
                speaker = "你" if row["role"] == "user" else "旁听"
                source = "麦克风" if row["source"].startswith("microphone") else "文字" if row["source"] == "text" else "桌面声音"
                records.append({"handle": ref["handle"], "kind": "speech" if row["role"] == "user" else "observation",
                                "source_kind": "message", "id": row["id"], "label": f"会话原文 · {speaker} · {source} · {when}",
                                "text": row["text"]})
            elif ref["kind"] == "chunk" and ref["id"] in chunks:
                row = chunks[ref["id"]]
                records.append({"handle": ref["handle"], "kind": "material", "source_kind": "chunk", "id": row["id"],
                                "label": "资料 " + chunk_label(row), "text": row["text"]})
        return records

    def _messages(self, ids: list[str]) -> list[dict]:
        rows = []
        for offset in range(0, len(ids), 500):
            part = ids[offset:offset + 500]
            with self.memory._lock:
                rows += [dict(row) for row in self.memory._db.execute(
                    "SELECT id,role,source,text,created_at FROM messages WHERE id IN (%s)" % ",".join("?" * len(part)), part)]
        return rows

    async def _collect(self, task: dict) -> dict:
        refs, messages = [], 0
        for session_id in task["params"]["session_ids"]:
            for row in self.memory.history(5000, session_id):
                if row["role"] in ("user", "observation") and row["text"].strip():
                    refs.append({"kind": "message", "id": row["id"]})
                    messages += 1
        ids = task["params"]["material_ids"]
        refs += [{"kind": "chunk", "id": row["id"]} for row in (self.memory.material_chunks(material_ids=ids) if ids else [])]
        if not refs:
            raise TaskError("没有可用的来源：所选会话还没有原文，也没有选择资料。")
        if len(refs) > 1500:
            raise TaskError("来源超过 1500 条，请减少会话或资料。")
        for index, ref in enumerate(refs, 1):
            ref["handle"] = f"S{index}"
        return {"sources": refs, "messages": messages, "chunks": len(refs) - messages}

    def _pack(self, records: list[dict], build, llm: dict) -> list[list[dict]]:
        counter, budget = TokenCounter(llm, self.runtime.data_dir), self._budget(llm)
        batches, current = [], []
        for record in records:
            if counter.count(build(current + [record])) <= budget:
                current.append(record)
                continue
            if current:
                batches.append(current)
            record = dict(record)
            while counter.count(build([record])) > budget and len(record["text"]) > 200:
                record["text"] = record["text"][:int(len(record["text"]) * .8)] + "…（已截断）"
            if counter.count(build([record])) > budget:
                raise TaskError("模型上下文预算太小，放不下单条来源。请在设置中调大上下文预算。")
            current = [record]
        return batches + ([current] if current else [])

    async def _extract(self, task: dict, collected: dict) -> dict:
        records = self._records(collected["sources"])
        if not records:
            raise TaskError("所有来源都已删除。")
        llm = self._llm(2048)
        language = language_name(self.runtime.settings.raw()["output_language"])
        instructions = task["params"]["instructions"]
        batches = self._pack(records, lambda batch: extract_messages(batch, instructions, language), llm)
        parts = []
        for index, batch in enumerate(batches, 1):
            await self._progress(task["id"], "extract", progress=f"{index}/{len(batches)}")
            data = await self._model_json(task, "extract", "task_extract", extract_messages(batch, instructions, language),
                                          llm, EXTRACT_SCHEMA, [item["id"] for item in batch])
            parts.append(clean_extraction(data, {item["handle"] for item in batch}))
        merged = merge_extractions(parts)
        if len(parts) > 1 and merged["summary"]:
            data = await self._model_json(task, "extract", "task_summary", summary_messages([part["summary"] for part in parts], language),
                                          self._llm(512), SUMMARY_SCHEMA, [])
            merged["summary"] = str(data.get("summary", merged["summary"]))[:1200]
        return {**merged, "batches": len(batches)}

    def _source_map(self, records: list[dict]) -> dict:
        return {item["handle"]: {"kind": item["source_kind"], "id": item["id"], "label": item["label"],
                                 "excerpt": item["text"][:300]} for item in records}

    async def _compose(self, task: dict, collected: dict, extraction: dict) -> dict:
        records = self._records(collected["sources"])
        extraction = keep_sources(extraction, {item["handle"] for item in records})
        sources, revise = self._source_map(records), task["params"].get("revise", {})
        stats = {"messages": sum(item["source_kind"] == "message" for item in records),
                 "chunks": sum(item["source_kind"] == "chunk" for item in records)}
        content = render_minutes(task["title"], extraction, stats)
        todos = [{"text": item["task"], "owner": item["owner"], "due": item["due"], "done": False, "markers": item["sources"]}
                 for item in extraction["todos"]]
        todo_content = "\n".join([f"# {task['title']} · 待办", ""] + [todo_line(todo) for todo in todos]) + "\n"
        note = task["params"]["instructions"][-500:]
        if revise.get("minutes"):
            minutes = self.memory.add_artifact_version(revise["minutes"], content, "regenerated", note, sources)
        else:
            minutes = self.memory.create_artifact("minutes", task["title"], content, sources, task["id"])
        if revise.get("todos"):
            todo = self.memory.replace_todos(revise["todos"], todos, todo_content, sources, note)
        else:
            todo = self.memory.create_artifact("todos", f"{task['title']} · 待办", todo_content, sources, task["id"], todos=todos)
        return {"artifact_ids": [minutes["id"], todo["id"]], "items": {key: len(extraction[key]) for key in ("points", "decisions", "todos", "questions")}}

    async def _document(self, task: dict, collected: dict, extraction: dict) -> dict:
        records = self._records(collected["sources"])
        handles = {item["handle"] for item in records}
        extraction = keep_sources(extraction, handles)
        llm = self._llm(3072)
        language = language_name(self.runtime.settings.raw()["output_language"])
        counter, budget = TokenCounter(llm, self.runtime.data_dir), self._budget(llm)
        build = lambda items: document_messages(task["title"], extraction, items, task["params"]["instructions"], language)
        selected = list(records)
        while selected and counter.count(build(selected)) > budget:
            selected = selected[:-max(1, len(selected) // 4)]
        if counter.count(build(selected)) > budget:
            raise TaskError("纪要内容超出模型上下文预算，请在设置中调大上下文预算后继续。")
        text, _ = await self._model(task, "document", "task_document", build(selected), llm, [item["id"] for item in selected])
        content, unknown = clean_markers(text, handles)
        note = f"模型写入了不存在的引用：{'、'.join(unknown)}" if unknown else ""
        revise = task["params"].get("revise", {})
        if revise.get("document"):
            artifact = self.memory.add_artifact_version(revise["document"], content, "regenerated", note, self._source_map(records))
        else:
            artifact = self.memory.create_artifact("document", f"{task['title']} · 文档草稿", content, self._source_map(records),
                                                   task["id"], note=note)
        return {"artifact_ids": [artifact["id"]], "unknown_markers": unknown}

    async def _run_minutes(self, task: dict) -> None:
        collected = await self._step(task["id"], "collect", lambda: self._collect(task))
        extraction = await self._step(task["id"], "extract", lambda: self._extract(task, collected))
        await self._step(task["id"], "compose", lambda: self._compose(task, collected, extraction))
        if task["params"].get("document"):
            await self._step(task["id"], "document", lambda: self._document(task, collected, extraction))

    # ---- tool actions ----------------------------------------------------
    async def start_action(self, message: dict, messages: list, text: str, calls: list[dict], trace_id: str) -> dict:
        origin = "user_voice" if message["source"].startswith("microphone") else "user_text"
        names = "、".join(SPECS[call["name"]].title if call["name"] in SPECS else call["name"] for call in calls)
        state = {"messages": messages + [assistant_calls(text, calls)], "calls": calls, "round": 0}
        id = self.memory.create_task("action", f"操作：{names}"[:80], origin, message["session_id"], trace_id,
                                     {"reply_to": message["id"]}, [("act", "执行操作"), ("reply", "回复结果")], state)
        await self.changed(id)
        self._schedule(id)
        return self.memory.task_summary(id)

    async def _run_action(self, task: dict) -> None:
        if not task["state"].get("messages"):
            raise TaskError("这项操作的上下文已清除，请重新发起请求。")
        outcome = await self._step(task["id"], "act", lambda: self._act(task))
        await self._step(task["id"], "reply", lambda: self._reply(task, outcome))

    async def _act(self, task: dict) -> dict:
        state = self.memory.task(task["id"])["state"]
        messages, calls = state["messages"], state["calls"]
        for round_index in range(state.get("round", 0), MAX_ROUNDS):
            results = []
            for call in calls:
                outcome = await self._call(task, f"{round_index}:{call['id']}", call)
                results.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                "content": json.dumps(outcome, ensure_ascii=False)[:8000]})
            messages = messages + results
            config = self.runtime._provider("llm")
            settings = self.runtime.settings.raw()
            if settings.get("tools_enabled", True):
                config["tools"] = definitions(settings)
            reply_to = task["params"].get("reply_to")
            sources = [reply_to] if reply_to and self.memory.record(reply_to) else []
            text, calls = await self._model(task, "act", "task_action", messages, config, sources)
            if not calls:
                return {"text": text}
            messages = messages + [assistant_calls(text, calls)]
            self.memory.update_task(task["id"], state={"messages": messages, "calls": calls, "round": round_index + 1})
        raise TaskError(f"连续操作超过 {MAX_ROUNDS} 轮，已停止。")

    def _outcome(self, call: dict) -> dict:
        if call["status"] == "succeeded":
            return {"status": "succeeded", "result": call["result"]}
        return {"status": call["status"], "error": call["error"] or call["reason"]}

    async def _call(self, task: dict, key: str, call: dict) -> dict:
        row = self.memory.tool_call_by_key(task["id"], key)
        if row and row["status"] in FINISHED_CALLS:
            return self._outcome(row)
        settings = self.runtime.settings.raw()
        spec = SPECS.get(call["name"])
        try:
            if not spec:
                raise ToolError("未知工具。")
            arguments = validate_arguments(spec, call["arguments"] if row is None else row["arguments"])
            target = self.runner.target(spec, arguments)
        except ToolError as exc:
            row = row or self.memory.add_tool_call(task["id"], key, call["name"][:64], {}, spec.effect if spec else "unknown",
                                                   "", "deny", "参数无效。", "failed")
            row = self.memory.update_tool_call(row["id"], status="failed", error=str(exc))
            await self._call_changed(row)
            return self._outcome(row)
        decision, reason = decide(spec, target, task["origin"], settings, self.grants.get(task["id"], set()))
        if row is None:
            row = self.memory.add_tool_call(task["id"], key, spec.name, arguments, spec.effect, target, decision, reason, "proposed")
        else:
            row = self.memory.update_tool_call(row["id"], decision=decision, reason=reason, status="proposed")
        if decision == "deny":
            row = self.memory.update_tool_call(row["id"], status="denied", error=reason)
            await self._call_changed(row)
            return self._outcome(row)
        if decision == "ask":
            answer = await self._ask(task, row)
            if not answer["approved"]:
                row = self.memory.update_tool_call(row["id"], status="denied", error="你拒绝了这项操作。")
                await self._call_changed(row)
                return self._outcome(row)
            if answer["scope"] == "task":
                self.grants.setdefault(task["id"], set()).add(spec.name)
        row = self.memory.update_tool_call(row["id"], status="running")
        await self._call_changed(row)
        try:
            if spec.effect == "internal":
                result, undo = await self._internal(task, spec, arguments), None
            else:
                result, undo = await asyncio.to_thread(self.runner.run, row["id"], spec, arguments)
            row = self.memory.update_tool_call(row["id"], status="succeeded", result=result, undo=undo)
        except asyncio.CancelledError:
            self.memory.update_tool_call(row["id"], status="unknown", error="任务在执行期间被取消，结果未知。")
            raise
        except (ToolError, ValueError, KeyError) as exc:
            row = self.memory.update_tool_call(row["id"], status="failed", error=str(exc)[:500])
        except OSError as exc:
            row = self.memory.update_tool_call(row["id"], status="failed", error=f"系统操作失败：{exc.strerror or type(exc).__name__}")
        await self._call_changed(row)
        return self._outcome(row)

    async def _internal(self, task: dict, spec, arguments: dict) -> dict:
        child = await self.start_minutes(arguments, task["origin"], task["session_id"])
        return {"task_id": child["id"], "title": child["title"], "status": "started"}

    def approval_view(self, call: dict) -> dict:
        arguments = dict(call["arguments"])
        if isinstance(arguments.get("content"), str) and len(arguments["content"]) > 2000:
            arguments["content"] = arguments["content"][:2000] + "…"
        task = self.memory.task(call["task_id"])
        settings = self.runtime.settings.raw()
        return {"id": call["id"], "task_id": call["task_id"], "task_title": task["title"], "tool": call["tool"],
                "title": SPECS[call["tool"]].title if call["tool"] in SPECS else call["tool"], "effect": call["effect"],
                "effect_label": EFFECT_LABELS.get(call["effect"], call["effect"]), "target": call["target"],
                "arguments": arguments, "reason": call["reason"], "origin": task["origin"], "created_at": call["created_at"],
                "task_scope": call["effect"] not in NO_STANDING_ALLOW,
                "voice": bool(settings.get("voice_approval")) and call["effect"] in VOICE_APPROVABLE}

    def approvals_pending(self) -> list[dict]:
        return [self.approval_view(call) for call in self.memory.pending_tool_calls() if call["id"] in self.approvals]

    async def _ask(self, task: dict, row: dict) -> dict:
        future = asyncio.get_running_loop().create_future()
        self.approvals[row["id"]] = future
        row = self.memory.update_tool_call(row["id"], status="awaiting_approval")
        self.memory.update_task(task["id"], status="waiting_approval")
        await self.changed(task["id"])
        await self.runtime.emit("approval_requested", self.approval_view(row), task["trace_id"])
        try:
            answer = await future
        finally:
            self.approvals.pop(row["id"], None)
        self.memory.update_task(task["id"], status="running")
        await self.changed(task["id"])
        return answer

    async def resolve(self, call_id: str, approved: bool, scope: str = "once", via: str = "click") -> dict:
        call = self.memory.tool_call(call_id)
        future = self.approvals.get(call_id)
        if not call or call["status"] != "awaiting_approval" or not future or future.done():
            raise ValueError("这项操作已经处理过，或者任务已中断。")
        if scope not in ("once", "task") or (scope == "task" and call["effect"] in NO_STANDING_ALLOW):
            raise ValueError("删除和命令类操作只能逐次确认。")
        if via == "voice" and (not self.runtime.settings.raw().get("voice_approval") or call["effect"] not in VOICE_APPROVABLE):
            raise ValueError("这项操作需要在控制台点击确认。")
        approval = {"approved": bool(approved), "scope": scope, "via": via, "at": _now()}
        self.memory.update_tool_call(call_id, status="approved" if approved else "denied", approval=approval)
        future.set_result(approval)
        await self.runtime.emit("approval_resolved", {"id": call_id, "task_id": call["task_id"], **approval},
                                self.memory.task(call["task_id"])["trace_id"])
        return approval

    async def handle_voice(self, message: dict) -> bool:
        """Consume a spoken approval phrase while a call awaits approval."""
        intent = voice_intent(message["text"])
        pending = [call for call in self.memory.pending_tool_calls() if call["id"] in self.approvals]
        if not intent or not pending:
            return False
        settings = self.runtime.settings.raw()
        hint = None
        if len(pending) > 1:
            hint = "有多项操作等待确认，请在控制台逐项处理。"
        elif not settings.get("voice_approval"):
            hint = "语音确认没有开启，请在控制台点击确认。"
        elif pending[0]["effect"] not in VOICE_APPROVABLE:
            hint = "删除和命令类操作需要在控制台点击确认。"
        elif self.runtime.playing:
            hint = "正在播报时不接受语音确认，请稍后再说。"
        if hint:
            await self.runtime.emit("approval_hint", {"message": hint, "source_ids": [message["id"]]}, message["trace_id"])
            return True
        await self.resolve(pending[0]["id"], intent == "approve", "once", "voice")
        return True

    async def undo(self, call_id: str) -> dict:
        call = self.memory.tool_call(call_id)
        if not call:
            raise KeyError(call_id)
        if call["status"] != "succeeded" or not call["undo"]:
            raise ValueError("这项操作不能撤销。")
        result = await asyncio.to_thread(self.runner.undo, call["undo"])
        call = self.memory.update_tool_call(call_id, status="undone", result={**(call["result"] or {}), "undo": result})
        await self._call_changed(call)
        return call

    async def _call_changed(self, call: dict) -> None:
        await self.runtime.emit("tool_call", {"id": call["id"], "task_id": call["task_id"], "tool": call["tool"],
                                              "effect": call["effect"], "target": call["target"], "status": call["status"],
                                              "decision": call["decision"], "error": call["error"]},
                                self.memory.task(call["task_id"])["trace_id"])
        await self.changed(call["task_id"])

    async def _reply(self, task: dict, outcome: dict) -> dict:
        text = (outcome.get("text") or "").strip() or "操作已处理，详细结果见任务页。"
        reply_to = task["params"].get("reply_to")
        sources = [reply_to] if reply_to and self.memory.record(reply_to) else []
        saved = self.memory.add_message("assistant", text, "assistant", task["session_id"], task["trace_id"],
                                        {"source_ids": sources, "task_id": task["id"], "complete": True})
        if task["session_id"] == self.runtime.session_id:
            await self.runtime.emit("response_start", {}, task["trace_id"], False)
            await self.runtime.emit("response_delta", {"text": text}, task["trace_id"], False)
            await self.runtime.emit("response_done", {"text": text, "id": saved["id"], "complete": True,
                                                      "source_ids": sources}, task["trace_id"])
        return {"message_id": saved["id"]}
