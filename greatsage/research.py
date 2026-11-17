"""Source-scoped research tasks; model delegation never owns storage or saving.

Only the outer LangGraph checkpoints references. Intermediate reports live in
the existing source-deletable task store; reviewed documents remain artifacts.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import re
import time
import uuid
from pathlib import PurePosixPath

from .budget import TokenCounter
from .delegation import DelegationError, run_delegation
from .tools import TRUSTED_ORIGINS, ToolError


MAX_CALLS = 24
MAX_RESERVED_TOKENS = 160_000
MAX_ACTIVE_SECONDS = 600
ATTEMPT_SECONDS = 180
MAX_SOURCES = 8


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_report(report: dict, records: list[dict]) -> dict:
    """Validate provenance independently of the model/framework's tool schema."""
    source_map = {item["id"]: item for item in records}
    if not isinstance(report, dict):
        raise ValueError("研究报告格式无效。")
    findings, reviews, plan = report.get("findings"), report.get("review"), report.get("plan")
    if (not isinstance(findings, list) or len(findings) > 40 or not isinstance(reviews, list)
            or len(reviews) != len(findings) or not isinstance(plan, list) or not 1 <= len(plan) <= 12
            or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in plan)):
        raise ValueError("研究报告缺少计划或完整核对结果。")
    texts = []
    for item in findings + reviews:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not 1 <= len(item["text"].strip()) <= 3000:
            raise ValueError("研究条目格式无效。")
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 12:
            raise ValueError("证据格式无效。")
        for cite in evidence:
            if not isinstance(cite, dict):
                raise ValueError("证据格式无效。")
            sid, quote = cite.get("source_id"), cite.get("quote")
            if (not isinstance(sid, str) or sid not in source_map or not isinstance(quote, str)
                    or not quote.strip() or len(quote) > 4000 or quote not in source_map[sid]["text"]):
                raise ValueError("研究证据与选定原文不匹配。")
    for item in findings:
        texts.append(item["text"])
    if len(set(texts)) != len(texts) or set(texts) != {item["text"] for item in reviews}:
        raise ValueError("核对结果没有逐项覆盖整理结论。")
    for item in reviews:
        if (item.get("status") not in {"supported", "uncertain", "conflict", "unsupported"}
                or not isinstance(item.get("reason"), str) or len(item["reason"]) > 3000
                or (item["status"] == "supported" and not item.get("evidence"))):
            raise ValueError("核对结论或证据无效。")
    return report


def render_report(title: str, report: dict, records: list[dict]) -> str:
    sources = {item["id"]: item for item in records}
    labels = {"uncertain": "待确认", "conflict": "来源有分歧", "unsupported": "缺少支持"}
    # Only evidence mappings may mint citation markers, including in quotations.
    def display(text):
        return re.sub(r"\[S(\d{1,4})\]", r"［S\1］", " ".join(text.split()))

    lines = [f"# {display(title)}", "", "> 基于选定资料与会话生成，模型核对不等于事实认证；请审阅后使用。", "", "## 整理结论", ""]
    supported = [item for item in report["review"] if item["status"] == "supported"]
    uncertain = [item for item in report["review"] if item["status"] != "supported"]
    for item in supported:
        handles = list(dict.fromkeys(sources[cite["source_id"]]["handle"] for cite in item["evidence"]))
        lines.append(f"- {display(item['text'])} " + "".join(f"[{handle}]" for handle in handles))
    if not supported:
        lines.append("当前来源没有形成可支持的结论。")
    lines += ["", "## 待确认与分歧", ""]
    for item in uncertain:
        handles = list(dict.fromkeys(sources[cite["source_id"]]["handle"] for cite in item["evidence"]))
        lines += [f"- **{labels[item['status']]}**：{display(item['text'])} " + "".join(f"[{handle}]" for handle in handles),
                  f"  核对说明：{display(item['reason'])}"]
    if not uncertain:
        lines.append("本轮核对未标记分歧；这不保证没有遗漏。")
    lines += ["", "## 核对依据", ""]
    for item in report["review"]:
        for cite in item["evidence"]:
            quote = display(cite["quote"])
            lines.append(f"- “{quote}” [{sources[cite['source_id']]['handle']}]")
    return "\n".join(lines) + "\n"


class ResearchWorkflow:
    def __init__(self, manager):
        self.manager = manager
        self.runtime = manager.runtime
        self.memory = manager.memory

    async def start(self, params, origin, session_id, trace_id=""):
        if origin not in TRUSTED_ORIGINS:
            raise ValueError("只有用户请求才能创建资料文档任务。")
        params = dict(params)
        materials = params.get("material_ids", [])
        sessions = params.get("session_ids", [session_id] if params.get("use_session", True) else [])
        for values, maximum in ((materials, 50), (sessions, 20)):
            if not isinstance(values, list) or len(values) > maximum or any(not isinstance(item, str) for item in values):
                raise ValueError("所选来源列表无效。")
        if set(materials) - {item["id"] for item in self.memory.materials()}:
            raise ValueError("所选资料不存在或已删除。")
        if set(sessions) - {item["id"] for item in self.memory.sessions()}:
            raise ValueError("所选会话不存在。")
        if not materials and not sessions:
            raise ValueError("请至少选择一份资料或一个会话。")
        title = str(params.get("title") or "资料整理与核对").strip()[:80]
        path = str(params.get("save_path") or "").strip().replace("\\", "/")
        if path and (PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                     or ":" in path or not path.lower().endswith(".md") or len(path) > 240):
            raise ValueError("保存位置必须是工作目录内的相对 .md 路径。")
        if type(params.get("overwrite", False)) is not bool:
            raise ValueError("overwrite 必须是布尔值。")
        clean = {"material_ids": list(dict.fromkeys(materials)), "session_ids": list(dict.fromkeys(sessions)),
                 "instructions": str(params.get("instructions") or "")[:4000], "save_path": path,
                 "overwrite": params.get("overwrite", False)}
        id = self.memory.create_task("research", title, origin, session_id, trace_id or uuid.uuid4().hex, clean,
                                    [("collect", "收集来源"), ("research", "规划、整理与证据核对"),
                                     ("compose", "生成可审阅文档"), ("save", "审批并保存文档")])
        if not path:
            clean["save_path"] = f"documents/{id}.md"
            self.memory.update_task(id, params=clean)
        await self.manager.changed(id)
        self.manager._schedule(id)
        return self.memory.task_summary(id)

    def guard(self, task_id):
        token = self.manager.run_tokens.get(task_id)
        identity = (self.runtime.data_epoch, self.runtime.session_id, self.runtime.settings.version())

        def current():
            return (token is not None and token is self.manager.run_tokens.get(task_id)
                    and identity == (self.runtime.data_epoch, self.runtime.session_id, self.runtime.settings.version())
                    and self.memory.task(task_id)["status"] in {"running", "queued", "waiting_approval"})
        return current

    def records(self, collected):
        if not collected or not collected.get("sources"):
            raise ValueError("任务来源已清除，请重新创建任务。")
        records = self.manager._records(collected["sources"])
        hashes = {ref["id"]: ref["content_hash"] for ref in collected["sources"]}
        if len(records) != len(hashes) or any(hashes.get(item["id"]) != digest(item["text"]) for item in records):
            raise ValueError("选定来源已修改或删除，请重新创建任务。")
        return records

    async def collect(self, task):
        collected = await self.manager._collect(task)
        records = self.manager._records(collected["sources"])
        if (len(records) > MAX_SOURCES or sum(len(item["text"]) for item in records) > 64_000
                or any(len(item["text"]) > 32_000 for item in records)):
            raise ValueError("研究任务最多使用 8 段、共 64000 字符来源（单段不超过 32000），请缩小资料范围。")
        hashes = {item["id"]: digest(item["text"]) for item in records}
        for ref in collected["sources"]:
            ref["content_hash"] = hashes[ref["id"]]
        self.memory.bind_research_sources(task["id"], list(hashes))
        return collected

    async def research(self, task):
        collected = self.memory.step(task["id"], "collect")["result"]
        records = self.records(collected)
        current = self.guard(task["id"])
        config = self.manager._llm(2048)
        counter = TokenCounter(config, self.runtime.data_dir)
        skills = self.runtime.skills.select(task["title"] + " " + task["params"]["instructions"], max_chars=5000)
        skill_audit = [
            {key: item[key] for key in ("id", "version", "truncated") if key in item} | {
                "resources": [
                    {key: resource[key] for key in ("path", "version", "truncated") if key in resource}
                    for resource in item.get("resources", [])
                ]
            }
            for item in skills
        ]
        pending = {}
        started = time.monotonic()
        state = self.memory.task(task["id"])["state"]
        budget = dict(state.get("research_budget", {"calls": 0, "reserved_tokens": 0, "active_seconds": 0}))
        if budget["active_seconds"] >= MAX_ACTIVE_SECONDS:
            raise ValueError("任务累计运行预算已用完，请缩小范围后新建任务。")

        def check():
            if not current():
                raise ValueError("运行状态已改变，已丢弃过期研究结果。")
            self.records(collected)

        async def before_request(role, messages, request_config, source_ids):
            check()
            reserve = counter.count(messages + [{"tools": request_config.get("tools", [])}]) + request_config["max_tokens"]
            if budget["calls"] >= MAX_CALLS or budget["reserved_tokens"] + reserve > MAX_RESERVED_TOKENS:
                raise ValueError("任务调用或上下文累计预算已用完，请缩小范围后新建任务。")
            budget["calls"] += 1
            budget["reserved_tokens"] += reserve
            state["research_budget"] = budget
            self.memory.update_task(task["id"], state=state)
            snapshot = await self.runtime.snapshot_request(task["trace_id"], "research_" + role, messages,
                                                           source_ids, skill_audit, provider=request_config)
            pending[snapshot] = {"purpose": "research_" + role, "agent": role, "started": time.monotonic(), "usage": {}}
            check()
            return snapshot

        async def on_usage(role, usage):
            check()
            for entry in reversed(list(pending.values())):
                if entry["agent"] == role:
                    entry["usage"] = dict(usage)
                    break
            await self.runtime.emit("usage", {"component": "llm", "purpose": "research_" + role,
                                              "task_id": task["id"], "usage": usage,
                                              "source_ids": [item["id"] for item in records]}, task["trace_id"])

        async def on_request_end(snapshot, status):
            self.memory.finish_snapshot(snapshot, "cancelled" if status == "stale" else status)
            entry = pending.pop(snapshot, None)
            if not entry or not current():
                return
            entry["latency_ms"] = round((time.monotonic() - entry.pop("started")) * 1000)
            entry["status"] = status
            detail = self.memory.step(task["id"], "research")["detail"]
            detail["calls"] = detail.get("calls", []) + [entry]
            detail["budget"] = dict(budget)
            self.memory.update_step(task["id"], "research", detail=detail)
            await self.manager.changed(task["id"])

        async def on_event(event):
            check()
            detail = self.memory.step(task["id"], "research")["detail"]
            if event.get("status") == "schema_validation_failed":
                # The adapter supplies only names/paths from our own schemas,
                # never rejected argument values or arbitrary model keys.
                metadata = {key: event[key] for key in (
                    "tool", "validation_kind", "schema_path", "call", "retrying") if key in event}
                for entry in reversed(list(pending.values())):
                    if entry["agent"] == event.get("agent"):
                        entry["validation"] = metadata
                        break
                else:
                    # Schema repair is announced after the failed request's
                    # finally callback has recorded its usage and closed it.
                    calls = detail.get("calls", [])
                    if calls and calls[-1].get("agent") == event.get("agent"):
                        calls[-1]["validation"] = metadata
                await self.runtime.emit("research_validation", {
                    "task_id": task["id"], "agent": event.get("agent"), **metadata}, task["trace_id"])
                detail["progress"] = ("模型报告格式未通过，正在进行一次修正。" if event.get("retrying")
                                      else "模型报告格式仍未通过，已停止研究。")
            if isinstance(event.get("plan"), list):
                detail["plan"] = event["plan"]
            role = event.get("role", event.get("agent"))
            if role:
                detail["agent"] = role
                children = {item["name"]: item for item in detail.get("children", [])}
                status = event.get("status", event.get("type", "running"))
                if status == "schema_validation_failed":
                    status = "running" if event.get("retrying") else "failed"
                children[role] = {"name": role, "status": status}
                detail["children"] = list(children.values())
            self.memory.update_step(task["id"], "research", detail=detail)
            await self.manager.changed(task["id"])

        try:
            instruction = (f"Output language: {self.runtime.settings.raw()['output_language']}.\n"
                           + self.runtime.settings.raw()["global_prompt"] + "\nTask: " + task["title"]
                           + "\n" + task["params"]["instructions"])
            async with asyncio.timeout(min(ATTEMPT_SECONDS, MAX_ACTIVE_SECONDS - budget["active_seconds"])):
                # The research SDK builds its own temporary Pregel graph. Run it
                # outside the durable outer task graph's context so LangGraph
                # does not mistake the inner graph for an outer checkpointed
                # subgraph. Only this boundary is detached; the research graph's
                # own child-agent context and cancellation remain intact.
                delegation = asyncio.create_task(
                    run_delegation(self.runtime.providers, config, records, counter, instruction=instruction,
                                   skills=skills, before_request=before_request, on_usage=on_usage,
                                   on_event=on_event, is_current=current, on_request_end=on_request_end),
                    context=contextvars.Context())
                report = await delegation
            check()
            report = validate_report(report, records)
            reviews = report["review"]
            await self.manager._progress(task["id"], "research", plan=report["plan"],
                                         children=[{"name": item.get("role", item.get("name", "")),
                                                    "status": item.get("status", "completed")} for item in report.get("children", [])],
                                         review_summary=f"核对 {len(reviews)} 项，其中 {sum(item['status'] == 'supported' for item in reviews)} 项有来源支持。",
                                         uncertainties=[item["text"] for item in reviews if item["status"] != "supported"])
            check()
            return {key: report[key] for key in ("plan", "findings", "review")}
        except TimeoutError:
            raise ValueError("研究阶段超出时间预算，已停止；可缩小资料范围重试。") from None
        except DelegationError as exc:
            code = str(exc)
            hints = {"input_budget_exceeded": "选定资料与工具定义超出模型上下文，请减少资料或增大上下文配置。",
                     "model_call_limit": "研究代理达到调用上限，请缩小任务范围。",
                     "invalid_tool_call": "模型返回的工具参数不符合结构，请更换支持结构化工具调用的模型。",
                     "missing_structured_result": "模型没有返回所需工具调用，请更换兼容模型。",
                     "stale_delegation": "运行状态已改变，已丢弃过期研究结果。"}
            raise ValueError(hints.get(code, "研究未完成，校验代码：" + (code if re.fullmatch(r"[a-z_]{1,80}", code) else "invalid_result"))) from None
        finally:
            for snapshot in pending:
                self.memory.finish_snapshot(snapshot, "cancelled")
            budget["active_seconds"] += round(time.monotonic() - started, 3)
            # Counters contain no source text, and survive cancellation/resume.
            fresh = self.memory.task(task["id"])["state"]
            fresh["research_budget"] = budget
            self.memory.update_task(task["id"], state=fresh)

    async def compose(self, task):
        records = self.records(self.memory.step(task["id"], "collect")["result"])
        report = validate_report(self.memory.step(task["id"], "research")["result"], records)
        content = render_report(task["title"], report, records)
        existing = self.memory.artifacts(task_id=task["id"])
        if existing:
            artifact = self.memory.artifact(existing[0]["id"])
        else:
            artifact = self.memory.create_artifact("document", task["title"], content, self.manager._source_map(records),
                                                   task["id"], note="资料整理与证据核对；模型核对不代替人工审阅。")
        return {"artifact_ids": [artifact["id"]]}

    async def save(self, task):
        current = self.guard(task["id"])
        self.records(self.memory.step(task["id"], "collect")["result"])
        result = self.memory.step(task["id"], "compose")["result"]
        artifact = self.memory.artifact(result["artifact_ids"][0])
        content_hash = digest(self.memory.export_markdown(artifact["id"]))
        args = {"artifact_id": artifact["id"], "version": artifact["version"], "content_hash": content_hash,
                "path": task["params"]["save_path"], "overwrite": task["params"]["overwrite"]}
        identity = {"session_id": self.runtime.session_id, "settings": self.runtime.settings.version(),
                    "data_epoch": self.runtime.data_epoch}
        state = self.memory.task(task["id"])["state"]
        approved = any(call["status"] == "approved" for call in self.memory.task(task["id"])["tool_calls"])
        if approved and state.get("save_identity") != identity:
            raise ToolError("保存审批期间运行状态已改变，请继续任务并重新确认。")
        state["save_identity"] = identity
        self.memory.update_task(task["id"], state=state)

        def check():
            if not current():
                raise ToolError("任务状态已改变，请重新审阅后保存。")
            self.records(self.memory.step(task["id"], "collect")["result"])

        key = f"research-save:{artifact['version']}:{content_hash}"
        outcome = await self.manager._call(task, key, {"name": "export_artifact", "arguments": args},
                                            force_click=True, pre_execute=check)
        if outcome["status"] not in {"succeeded", "denied"}:
            raise ToolError("保存未完成：" + str(outcome.get("error", outcome["status"])))
        saved = outcome["status"] == "succeeded"
        await self.manager._progress(task["id"], "save", saved=saved,
                                     progress="已保存到工作目录" if saved else "未保存：文档保留在产物中")
        return {"saved": saved, "artifact_ids": [artifact["id"]], "outcome": outcome}
