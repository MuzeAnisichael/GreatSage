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
from contextvars import ContextVar
from datetime import datetime

from langgraph.types import interrupt

from .budget import TokenCounter
from .materials import chunk_label
from .providers import ProviderError
# Re-export the store and timestamp helper for existing task/graph imports.
from .task_store import ACTIVE, TaskStoreMixin, _now
from .tools import (EFFECT_LABELS, NO_STANDING_ALLOW, SPECS, VOICE_APPROVABLE, ToolError, ToolOutcomeUnknown, ToolRunner, decide,
                    definitions, validate_arguments, voice_intent)
from .workflows import (EXTRACT_SCHEMA, SUMMARY_SCHEMA, clean_extraction, clean_markers, document_messages,
                        extract_messages, keep_sources, language_name, merge_extractions, render_minutes,
                        summary_messages)
from .artifacts import todo_line

FINISHED_CALLS = ("succeeded", "failed", "denied", "unknown", "undone")
MAX_ROUNDS = 4
# A model call has no side effects, so a transient provider failure is retried
# after a short backoff. A client-side timeout is not retried: it has already
# waited the full timeout and a retry would hold the single task slot for minutes.
RETRY_DELAYS = (1.0, 3.0)
RETRYABLE = {"upstream_error", "upstream_stream_error", "upstream_unavailable", "rate_limited",
             "stream_ended_before_completion", "connection_failed"}
CANCEL_WAIT_SECONDS = .25


class TaskError(RuntimeError):
    """A safe, user-facing task failure."""


def assistant_calls(text: str, calls: list[dict]) -> dict:
    return {"role": "assistant", "content": text or "",
            "tool_calls": [{"id": call["id"], "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]}} for call in calls]}


class TaskManager:
    def __init__(self, runtime):
        from .agent_graph import AgentGraphs
        from .mcp_tools import MCPTools
        self.runtime = runtime
        self.jobs: dict[str, asyncio.Task] = {}
        self.run_tokens: dict[str, object] = {}
        # LangGraph child coroutines inherit this scope. Reading the latest token
        # alone would accidentally authorize an old coroutine after a resume.
        self._run_scope = ContextVar("greatsage_task_run", default=None)
        self._graph_cleanup: asyncio.Task | None = None
        self.approvals: set[str] = set()
        self.approval_lock = asyncio.Lock()
        self.grants: dict[str, set[str]] = {}
        self.slot = asyncio.Semaphore(1)
        self.closing = False
        self.runner = ToolRunner(runtime.data_dir, runtime.memory, runtime.settings.raw)
        self.graphs = AgentGraphs(self)
        self.mcp = MCPTools(runtime.settings.raw)

    def specs(self):
        return SPECS | self.mcp.specs

    def tool_definitions(self):
        return definitions(self.runtime.settings.raw(), self.mcp.specs.values())

    @property
    def memory(self):
        return self.runtime.memory

    # ---- lifecycle -------------------------------------------------------
    def recover(self) -> list[str]:
        return self.memory.recover_tasks()

    async def close(self) -> None:
        self.closing = True
        if self._graph_cleanup is None:
            _, jobs = self._stop_active("interrupted")

            async def close_graphs():
                # A provider may ignore cancellation. Keep its checkpoint
                # connection until the graph really exits, without blocking UI.
                await asyncio.gather(*jobs, return_exceptions=True)
                await self.graphs.close()

            self._graph_cleanup = asyncio.create_task(close_graphs())
        await asyncio.wait({self._graph_cleanup}, timeout=CANCEL_WAIT_SECONDS)

    def _identity(self):
        return (self.runtime.session_id, self.runtime.data_epoch, self.runtime.settings.version())

    def _check_control_identity(self, identity):
        if self.closing:
            raise ValueError("应用正在关闭，请稍后继续任务。")
        if getattr(self.runtime, "_session_switching", False) or identity != self._identity():
            raise ValueError("运行状态已改变，请重新继续任务或确认操作。")

    def _abandon_control(self, task_id):
        # A settings change can invalidate a resume/approval without the session
        # cleanup doing it for us. Leave an explicitly resumable state, not a
        # queued task with a stale approval and no scheduled worker.
        if not self.closing:
            with self.memory._lock:
                row = self.memory._db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row is not None and row[0] in ACTIVE:
                    self._expire_approvals(task_id)
                    self.memory.finish_task(task_id, "interrupted", None)

    def _guard(self, task_id: str):
        scope = self._run_scope.get()
        if scope is None or scope[0] != task_id:
            # Also supports direct step/tool calls used by adapters and tests.
            scope = (task_id, self.run_tokens.get(task_id), self._identity())

        def check():
            if (self.closing or getattr(self.runtime, "_session_switching", False)
                    or scope[1] is not self.run_tokens.get(task_id) or scope[2] != self._identity()):
                raise TaskError("任务运行状态已改变，请继续任务后重试。")
            with self.memory._lock:
                row = self.memory._db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row[0] not in ACTIVE:
                raise TaskError("任务已中断或结束，已丢弃迟到结果。")

        return check

    def _stop_active(self, status: str, task_id: str | None = None):
        # Everything through job.cancel() is synchronous: no old result can
        # regain authority while session switching waits for cleanup.
        if task_id is None:
            self.run_tokens.clear()
        else:
            self.run_tokens.pop(task_id, None)
        with self.memory._lock:
            ids = [row[0] for row in self.memory._db.execute(
                f"SELECT id FROM tasks WHERE status IN {ACTIVE}" + (" AND id=?" if task_id else ""),
                (task_id,) if task_id else ())]
            for id in ids:
                self.memory.finish_task(id, status, None)
                self._expire_approvals(id)
                for call in self.memory.task(id)["tool_calls"]:
                    if call["status"] == "running":
                        self.memory.update_tool_call(call["id"], status="unknown", error="任务在执行期间中断，结果未知，不会自动重试。")
        if task_id is None:
            self.approvals.clear()
            self.grants.clear()
        jobs = [job for id, job in self.jobs.items() if task_id is None or id == task_id]
        for job in jobs:
            if not job.done():
                job.cancel()
        return ids, jobs

    @staticmethod
    async def _cancel_and_wait(jobs):
        if jobs:
            # wait_for(gather(...)) still waits indefinitely when cancellation
            # is swallowed. wait() leaves unfinished jobs tracked by _schedule.
            await asyncio.wait(jobs, timeout=CANCEL_WAIT_SECONDS)

    async def suspend_for_session(self) -> None:
        ids, jobs = self._stop_active("interrupted")
        await self._cancel_and_wait(jobs)
        for id in ids:
            await self.changed(id)

    async def changed(self, id: str) -> None:
        summary = self.memory.task_summary(id)
        await self.runtime.emit("task_updated", summary, summary["trace_id"], persist=False)

    def _schedule(self, id: str, approval=None) -> None:
        if self.closing or self.memory.task(id)["status"] != "queued":
            return
        if id in self.jobs and not self.jobs[id].done():
            raise ValueError("上一次运行仍在退出，请稍后继续任务。")
        job = asyncio.create_task(self._run(id, approval))
        self.jobs[id] = job
        def completed(done):
            if self.jobs.get(id) is done:
                self.jobs.pop(id, None)
        job.add_done_callback(completed)

    def _expire_approvals(self, id: str) -> None:
        for call in self.memory.task(id)["tool_calls"]:
            self.approvals.discard(call["id"])
            if call["status"] in ("proposed", "awaiting_approval", "approved"):
                self.memory.update_tool_call(call["id"], status="expired", approval=None)
        self.grants.pop(id, None)

    async def invalidate(self) -> None:
        ids, jobs = self._stop_active("cancelled")
        await self._cancel_and_wait(jobs)
        for id in ids:
            await self.changed(id)

    async def cancel(self, id: str) -> dict:
        task = self.memory.task(id)
        if task["status"] not in ACTIVE:
            raise ValueError("任务不在进行中。")
        _, jobs = self._stop_active("cancelled", id)
        await self._cancel_and_wait(jobs)
        await self.changed(id)
        return self.memory.task_summary(id)

    async def resume(self, id: str) -> dict:
        identity = self._identity()
        self._check_control_identity(identity)
        # Serialize checkpoint deletion with other resume/approval requests.
        # Session suspension deliberately does not wait for this lock.
        async with self.approval_lock:
            self._check_control_identity(identity)
            try:
                return await self._resume(id, identity)
            except ValueError:
                if self.closing or getattr(self.runtime, "_session_switching", False) or identity != self._identity():
                    self._abandon_control(id)
                raise

    async def _resume(self, id: str, identity) -> dict:
        if id in self.jobs and not self.jobs[id].done():
            raise ValueError("上一次运行仍在退出，请稍后继续任务。")
        task = self.memory.task(id)
        if task["status"] not in ("interrupted", "failed", "cancelled"):
            raise ValueError("只有中断、失败或已取消的任务可以继续。")
        if task["kind"] == "action" and not task["state"].get("messages"):
            raise ValueError("这项操作的上下文已清除，请重新发起请求。")
        if any(call["status"] == "expired" for call in task["tool_calls"]):
            # Drop obsolete resume values after cancellation/restart. Canonical
            # task cursors and the execution ledger retain completed work.
            await self.graphs.delete(id)
        self._check_control_identity(identity)
        self.memory.reset_unfinished_steps(id)
        await self.changed(id)
        self._check_control_identity(identity)
        self._schedule(id)
        return self.memory.task_summary(id)

    async def _run(self, id: str, approval=None) -> None:
        status, error, waiting = "succeeded", None, False
        token, context = None, None
        try:
            async with self.slot:
                task = self.memory.task(id)
                if task["status"] != "queued":
                    return
                self.memory.update_task(id, status="running", error=None)
                token = self.run_tokens[id] = object()
                context = self._run_scope.set((id, token, self._identity()))
                check = self._guard(id)
                check()
                await self.changed(id)
                check()
                waiting = await self.graphs.run(task, approval)
                check()
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
            try:
                if token is not None and self.run_tokens.get(id) is token:
                    self.run_tokens.pop(id, None)
                    if not waiting:
                        self._expire_approvals(id)
                    if not waiting and self.memory.task(id)["status"] in ACTIVE:
                        self.memory.finish_task(id, status, error)
                        if status == "succeeded":
                            self.memory.purge_task_bodies(id)
                        trace_id = self.memory.task(id)["trace_id"]
                        await self.changed(id)
                        # close() may have returned and closed MemoryStore while
                        # an event subscriber ignored cancellation. Do not read
                        # the task again or persist a late terminal event.
                        if not self.closing and self._run_scope.get()[2] == self._identity():
                            await self.runtime.emit("task_finished", {"task_id": id, "status": status, "error": error},
                                                    trace_id)
            finally:
                if context is not None:
                    self._run_scope.reset(context)

    async def _step(self, task_id: str, name: str, work):
        check = self._guard(task_id)
        check()
        step = self.memory.step(task_id, name)
        if step["status"] == "succeeded":
            return step["result"]
        detail = {key: step["detail"][key] for key in ("calls", "budget") if key in step["detail"]} if name == "research" else {}
        self.memory.update_step(task_id, name, status="running", started_at=_now(), finished_at=None, detail=detail)
        await self.changed(task_id)
        check()
        result = await work()
        check()
        detail = self.memory.step(task_id, name)["detail"]
        self.memory.update_step(task_id, name, status="succeeded", finished_at=_now(), result=result, detail=detail)
        await self.changed(task_id)
        check()
        return result

    async def _progress(self, task_id: str, name: str, **values) -> None:
        check = self._guard(task_id)
        check()
        detail = self.memory.step(task_id, name)["detail"]
        detail.update(values)
        self.memory.update_step(task_id, name, detail=detail)
        await self.changed(task_id)
        check()

    # ---- model calls -----------------------------------------------------
    def _llm(self, output_tokens: int) -> dict:
        llm = self.runtime._provider("llm")
        llm["max_tokens"] = max(256, min(output_tokens, llm.get("context_tokens", 8192) // 3))
        return llm

    def _budget(self, llm: dict) -> int:
        return llm.get("context_tokens", 8192) - llm["max_tokens"] - 256

    async def _model(self, task: dict, step: str, purpose: str, messages: list, config: dict,
                     source_ids: list[str]) -> tuple[str, list[dict]]:
        check = self._guard(task["id"])
        for attempt, delay in enumerate(RETRY_DELAYS + (None,), 1):
            check()
            try:
                return await self._model_once(task, step, purpose, messages, config, source_ids, attempt)
            except ProviderError as exc:
                check()
                if delay is None or not (exc.code in RETRYABLE or exc.status == 408):
                    raise
                detail = self.memory.step(task["id"], step)["detail"]
                retries = detail.get("retries", []) + [{"purpose": purpose, "code": exc.code, "attempt": attempt}]
                await self._progress(task["id"], step, retries=retries, retrying=f"{attempt}/{len(RETRY_DELAYS)}")
                await asyncio.sleep(delay)
                check()

    async def _model_once(self, task: dict, step: str, purpose: str, messages: list, config: dict,
                          source_ids: list[str], attempt: int) -> tuple[str, list[dict]]:
        check = self._guard(task["id"])
        check()
        trace = task["trace_id"]
        snapshot = await self.runtime.snapshot_request(trace, purpose, messages, source_ids, provider=config)
        started, first, text, calls, usage = time.monotonic(), None, "", [], {}
        try:
            check()
            async for item in self.runtime.providers.stream_chat(config, messages):
                check()
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
                    check()
            check()
            self.memory.finish_snapshot(snapshot, "completed")
        except asyncio.CancelledError:
            if not self.closing:
                self.memory.finish_snapshot(snapshot, "cancelled")
            raise
        except Exception:
            if not self.closing:
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
        check()
        return text, calls

    async def _model_json(self, task, step, purpose, messages, llm, schema, source_ids) -> dict:
        text, _ = await self._model(task, step, purpose, messages, {**llm, "json_schema": schema, "temperature": 0}, source_ids)
        try:
            return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
        except json.JSONDecodeError:
            raise TaskError("模型没有返回有效的结构化结果。") from None

    # ---- minutes ---------------------------------------------------------
    async def start_research(self, params: dict, origin: str, session_id: str, trace_id: str = "") -> dict:
        from .research import ResearchWorkflow
        return await ResearchWorkflow(self).start(params, origin, session_id, trace_id)

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

    async def regenerate(self, artifact_id: str, instructions: str, *, expected_version=None, update_sources=False) -> dict:
        artifact = self.memory.artifact(artifact_id)
        if expected_version is not None and artifact["version"] != expected_version:
            raise ValueError("文档已有新版本，请刷新后再修订。")
        original = self.memory.task(artifact["task_id"])
        if original["kind"] == "research":
            from .research import ResearchWorkflow
            return await ResearchWorkflow(self).revise(artifact_id, instructions, expected_version, update_sources)
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
                source = ("用户纠正" if row["source"] == "artifact_correction" else
                          "麦克风" if row["source"].startswith("microphone") else "文字" if row["source"] == "text" else "桌面声音")
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
        self._guard(task["id"])()
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
        check = self._guard(task["id"])
        check()
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
        check()
        content, unknown = clean_markers(text, handles)
        note = f"模型写入了不存在的引用：{'、'.join(unknown)}" if unknown else ""
        revise = task["params"].get("revise", {})
        if revise.get("document"):
            artifact = self.memory.add_artifact_version(revise["document"], content, "regenerated", note, self._source_map(records))
        else:
            artifact = self.memory.create_artifact("document", f"{task['title']} · 文档草稿", content, self._source_map(records),
                                                   task["id"], note=note)
        return {"artifact_ids": [artifact["id"]], "unknown_markers": unknown}

    # ---- tool actions ----------------------------------------------------
    async def start_action(self, message: dict, messages: list, text: str, calls: list[dict], trace_id: str) -> dict:
        origin = "user_voice" if message["source"].startswith("microphone") else "user_text"
        specs = self.specs()
        names = "、".join(specs[call["name"]].title if call["name"] in specs else call["name"] for call in calls)
        state = {"messages": messages + [assistant_calls(text, calls)], "calls": calls, "round": 0}
        id = self.memory.create_task("action", f"操作：{names}"[:80], origin, message["session_id"], trace_id,
                                     {"reply_to": message["id"]}, [("act", "执行操作"), ("reply", "回复结果")], state)
        await self.changed(id)
        self._schedule(id)
        return self.memory.task_summary(id)

    def _outcome(self, call: dict) -> dict:
        if call["status"] == "succeeded":
            return {"status": "succeeded", "result": call["result"]}
        return {"status": call["status"], "error": call["error"] or call["reason"]}

    async def _call(self, task: dict, key: str, call: dict, *, force_click=False, pre_execute=None) -> dict:
        check = self._guard(task["id"])
        check()
        row = self.memory.tool_call_by_key(task["id"], key)
        if row and row["status"] in FINISHED_CALLS:
            return self._outcome(row)
        settings = self.runtime.settings.raw()
        spec = self.specs().get(call["name"])
        try:
            if not spec:
                raise ToolError("未知工具。")
            arguments = validate_arguments(spec, call["arguments"] if row is None else row["arguments"])
            target = self.mcp.target(spec.name) if spec.effect == "external" else self.runner.target(spec, arguments)
            if row and spec.effect == "external" and row["target"] != target:
                raise ToolError("MCP 工具定义或服务配置已改变，请发起新请求。")
        except ToolError as exc:
            row = row or self.memory.add_tool_call(task["id"], key, call["name"][:64], {}, spec.effect if spec else "unknown",
                                                   "", "deny", "参数无效。", "failed")
            row = self.memory.update_tool_call(row["id"], status="failed", error=str(exc))
            await self._call_changed(row)
            check()
            return self._outcome(row)
        if any(item["status"] == "unknown" for item in self.memory.task(task["id"])["tool_calls"]):
            # The model may have requested several tools in one reply. Removing
            # tools from the next model call cannot protect this existing batch.
            decision, reason = "deny", "上一项操作结果未知，已停止后续工具；请先检查结果。"
        else:
            decision, reason = decide(spec, target, task["origin"], settings, self.grants.get(task["id"], set()))
        if force_click and decision != "deny":
            decision, reason = "ask", "请审阅当前文档版本，并点击确认保存。"
        if row is None:
            row = self.memory.add_tool_call(task["id"], key, spec.name, arguments, spec.effect, target, decision, reason, "proposed")
        else:
            row = self.memory.update_tool_call(row["id"], decision=decision, reason=reason, status="proposed")
        if decision == "deny":
            row = self.memory.update_tool_call(row["id"], status="denied", error=reason)
            await self._call_changed(row)
            check()
            return self._outcome(row)
        if decision == "ask":
            answer = await self._ask(task, row)
            check()
            if not answer["approved"]:
                row = self.memory.update_tool_call(row["id"], status="denied", error="你拒绝了这项操作。")
                await self._call_changed(row)
                check()
                return self._outcome(row)
            if answer["scope"] == "task":
                self.grants.setdefault(task["id"], set()).add(spec.name)
        def execute_check():
            check()
            if pre_execute:
                pre_execute()

        execute_check()
        row = self.memory.update_tool_call(row["id"], status="running")
        await self._call_changed(row)
        try:
            execute_check()
            if spec.effect == "internal":
                result, undo = await self._internal(task, spec, arguments), None
            elif spec.effect == "external":
                result, undo = await self.mcp.call(spec.name, arguments), None
            else:
                def execute_local():
                    if pre_execute:
                        # Recheck after thread-pool queuing, at the write boundary.
                        with self.memory._lock:
                            execute_check()
                            return self.runner.run(row["id"], spec, arguments)
                    # Already-started OS operations cannot be recalled. Do not
                    # hold the memory lock throughout a potentially long command.
                    execute_check()
                    return self.runner.run(row["id"], spec, arguments)
                result, undo = await asyncio.to_thread(execute_local)
            check()
            row = self.memory.update_tool_call(row["id"], status="succeeded", result=result, undo=undo)
        except asyncio.CancelledError:
            if not self.closing and self.memory.tool_call(row["id"])["status"] == "running":
                self.memory.update_tool_call(row["id"], status="unknown", error="任务在执行期间被取消，结果未知。")
            raise
        except TaskError:
            if not self.closing and self.memory.tool_call(row["id"])["status"] == "running":
                self.memory.update_tool_call(row["id"], status="unknown", error="任务身份已改变，执行结果未知。")
            raise
        except ToolOutcomeUnknown as exc:
            check()
            row = self.memory.update_tool_call(row["id"], status="unknown", error=str(exc))
        except (ToolError, ValueError, KeyError) as exc:
            check()
            row = self.memory.update_tool_call(row["id"], status="failed", error=str(exc)[:500])
        except OSError as exc:
            check()
            row = self.memory.update_tool_call(row["id"], status="failed", error=f"系统操作失败：{exc.strerror or type(exc).__name__}")
        await self._call_changed(row)
        check()
        return self._outcome(row)

    async def _internal(self, task: dict, spec, arguments: dict) -> dict:
        start = self.start_research if spec.name == "create_research_task" else self.start_minutes
        child = await start(arguments, task["origin"], task["session_id"])
        return {"task_id": child["id"], "title": child["title"], "status": "started"}

    def approval_view(self, call: dict) -> dict:
        arguments = dict(call["arguments"])
        if isinstance(arguments.get("content"), str) and len(arguments["content"]) > 2000:
            arguments["content"] = arguments["content"][:2000] + "…"
        task = self.memory.task(call["task_id"])
        settings = self.runtime.settings.raw()
        return {"id": call["id"], "task_id": call["task_id"], "task_title": task["title"], "tool": call["tool"],
                "title": self.specs()[call["tool"]].title if call["tool"] in self.specs() else call["tool"], "effect": call["effect"],
                "effect_label": EFFECT_LABELS.get(call["effect"], call["effect"]), "target": call["target"],
                "arguments": arguments, "reason": call["reason"], "origin": task["origin"], "created_at": call["created_at"],
                "task_scope": task["kind"] != "research" and call["effect"] not in NO_STANDING_ALLOW,
                "voice": task["kind"] != "research" and bool(settings.get("voice_approval")) and call["effect"] in VOICE_APPROVABLE}

    def approvals_pending(self) -> list[dict]:
        return [self.approval_view(call) for call in self.memory.pending_tool_calls() if call["id"] in self.approvals]

    async def _ask(self, task: dict, row: dict) -> dict:
        check = self._guard(task["id"])
        check()
        self.approvals.add(row["id"])
        row = self.memory.update_tool_call(row["id"], status="awaiting_approval")
        self.memory.update_task(task["id"], status="waiting_approval")
        await self.changed(task["id"])
        check()
        await self.runtime.emit("approval_requested", self.approval_view(row), task["trace_id"])
        check()
        # Exactly one tool per graph node keeps interrupt ordering stable.
        # Only identifiers enter the checkpoint; approval previews remain local.
        answer = interrupt({"call_id": row["id"]})
        saved = self.memory.tool_call(row["id"])["approval"]
        if not isinstance(answer, dict) or answer.get("call_id") != row["id"] or saved != answer:
            raise TaskError("工具授权已失效，请继续任务并重新确认。")
        self.approvals.discard(row["id"])
        self.memory.update_task(task["id"], status="running")
        await self.changed(task["id"])
        check()
        return answer

    async def resolve(self, call_id: str, approved: bool, scope: str = "once", via: str = "click") -> dict:
        identity = self._identity()
        self._check_control_identity(identity)
        # A click may arrive before the interrupted node's checkpoint is written.
        call = self.memory.tool_call(call_id)
        job = self.jobs.get(call["task_id"]) if call else None
        if job:
            await asyncio.wait({job}, timeout=CANCEL_WAIT_SECONDS)
            if not job.done():
                raise ValueError("任务仍在保存检查点或退出，请稍后重试。")
        async with self.approval_lock:
            self._check_control_identity(identity)
            try:
                return await self._resolve(call_id, approved, scope, via)
            except TaskError as exc:
                if call:
                    self._abandon_control(call["task_id"])
                raise ValueError(str(exc)) from None

    async def _resolve(self, call_id: str, approved: bool, scope: str, via: str) -> dict:
        call = self.memory.tool_call(call_id)
        if not call or call["status"] != "awaiting_approval" or call_id not in self.approvals:
            raise ValueError("这项操作已经处理过，或者任务已中断。")
        task = self.memory.task(call["task_id"])
        check = self._guard(task["id"])
        check()
        if task["kind"] == "research":
            if scope != "once" or via != "click":
                raise ValueError("文档保存需要在控制台逐次点击确认。")
            try:
                identity = {"session_id": self.runtime.session_id, "settings": self.runtime.settings.version(),
                            "data_epoch": self.runtime.data_epoch}
                if task["state"].get("save_identity") != identity:
                    raise ToolError("保存审批的运行状态已改变。")
                self.runner.validate_artifact_export(call["arguments"])
            except (ToolError, KeyError, ValueError):
                self._expire_approvals(task["id"])
                self.memory.finish_task(task["id"], "failed", "文档或来源已改变；请继续任务并重新审阅确认。")
                await self.changed(task["id"])
                raise ValueError("文档或来源已改变；请继续任务并重新审阅确认。") from None
        saved = await self.graphs.inspect(task)
        check()
        if task["status"] != "waiting_approval" or call_id not in saved["pending_calls"]:
            raise ValueError("审批状态已改变，请刷新后重试。")
        if scope not in ("once", "task") or (scope == "task" and call["effect"] in NO_STANDING_ALLOW):
            raise ValueError("删除、命令和外部工具只能逐次确认。")
        if via == "voice" and (not self.runtime.settings.raw().get("voice_approval") or call["effect"] not in VOICE_APPROVABLE):
            raise ValueError("这项操作需要在控制台点击确认。")
        approval = {"call_id": call_id, "approved": bool(approved), "scope": scope, "via": via, "at": _now()}
        self.memory.update_tool_call(call_id, status="approved" if approved else "denied", approval=approval)
        self.approvals.discard(call_id)
        self.memory.update_task(call["task_id"], status="queued")
        await self.runtime.emit("approval_resolved", {"id": call_id, "task_id": call["task_id"], **approval},
                                self.memory.task(call["task_id"])["trace_id"])
        check()
        self._schedule(call["task_id"], approval)
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
        elif self.memory.task(pending[0]["task_id"])["kind"] == "research":
            hint = "文档保存需要在控制台审阅并点击确认。"
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
        check = self._guard(task["id"])
        check()
        text = (outcome.get("text") or "").strip() or "操作已处理，详细结果见任务页。"
        reply_to = task["params"].get("reply_to")
        sources = [reply_to] if reply_to and self.memory.record(reply_to) else []
        saved = self.memory.add_message("assistant", text, "assistant", task["session_id"], task["trace_id"],
                                        {"source_ids": sources, "task_id": task["id"], "complete": True})
        if task["session_id"] == self.runtime.session_id:
            await self.runtime.emit("response_start", {}, task["trace_id"], False)
            check()
            await self.runtime.emit("response_delta", {"text": text}, task["trace_id"], False)
            check()
            await self.runtime.emit("response_done", {"text": text, "id": saved["id"], "complete": True,
                                                      "source_ids": sources}, task["trace_id"])
            check()
        return {"message_id": saved["id"]}
