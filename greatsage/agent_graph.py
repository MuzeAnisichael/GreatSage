"""LangGraph orchestration with reference-only local checkpoints.

The task database remains authoritative for source text, step results and the
tool execution ledger. Checkpoints contain control state, never message bodies.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from langsmith import tracing_context

from .tasks import MAX_ROUNDS, TaskError, _now, assistant_calls


class ControlState(TypedDict):
    task_id: str
    route: str


class MetadataCheckpointer(AsyncSqliteSaver):
    async def aput_writes(self, config, writes, task_id, task_path=""):
        # LangGraph ordinarily checkpoints the exception itself, whose message
        # may contain upstream output or deleted source text.
        safe = [(channel, type(value).__name__ if channel == "__error__" else value)
                for channel, value in writes]
        await super().aput_writes(config, safe, task_id, task_path)


class AgentGraphs:
    def __init__(self, manager):
        self.manager = manager
        self.stack = AsyncExitStack()
        self.lock = asyncio.Lock()
        self.saver = None
        self.graphs = {}
        self.nodes: dict[str, str] = {}

    async def ensure(self):
        async with self.lock:
            if self.saver is None:
                path = self.manager.runtime.data_dir / "agent-checkpoints.sqlite3"
                self.saver = await self.stack.enter_async_context(MetadataCheckpointer.from_conn_string(str(path)))
                await self.saver.setup()
                self.graphs = {"minutes": self._minutes(), "action": self._action()}

    async def close(self):
        await self.stack.aclose()

    @staticmethod
    def config(task_id):
        return {"configurable": {"thread_id": task_id}, "callbacks": [], "recursion_limit": 100}

    def _node(self, name, work):
        async def run(state):
            task = self.manager.memory.task(state["task_id"])
            self.nodes[task["id"]] = name
            await self.manager.runtime.emit("agent_node", {"task_id": task["id"], "node": name,
                                            "engine": "langgraph"}, task["trace_id"])
            route = await work(task)
            return {"route": route or ""}
        return run

    def _minutes(self):
        graph = StateGraph(ControlState)
        for name in ("collect", "extract", "compose", "document"):
            async def work(task, step=name):
                async def execute():
                    if step == "collect":
                        return await self.manager._collect(task)
                    collected = self.manager.memory.step(task["id"], "collect")["result"]
                    if collected is None:
                        raise TaskError("任务来源上下文已清除，请重新发起任务。")
                    if step == "extract":
                        return await self.manager._extract(task, collected)
                    extraction = self.manager.memory.step(task["id"], "extract")["result"]
                    if extraction is None:
                        raise TaskError("任务抽取上下文已清除，请重新发起任务。")
                    method = self.manager._compose if step == "compose" else self.manager._document
                    return await method(task, collected, extraction)
                await self.manager._step(task["id"], step, execute)
                return "document" if task["params"].get("document") else "end"
            graph.add_node(name, self._node(name, work))
        graph.add_edge(START, "collect")
        graph.add_edge("collect", "extract")
        graph.add_edge("extract", "compose")
        graph.add_conditional_edges("compose", lambda state: state["route"], {"document": "document", "end": END})
        graph.add_edge("document", END)
        return graph.compile(checkpointer=self.saver, name="greatsage_minutes_v1")

    def _action(self):
        graph = StateGraph(ControlState)
        for name, work in (("tools", self._tool), ("model", self._model), ("reply", self._reply)):
            graph.add_node(name, self._node(name, work))
        graph.add_edge(START, "tools")
        for name in ("tools", "model"):
            graph.add_conditional_edges(name, lambda state: state["route"], {key: key for key in ("tools", "model", "reply")})
        graph.add_edge("reply", END)
        return graph.compile(checkpointer=self.saver, name="greatsage_action_v1")

    async def _tool(self, task):
        manager, state = self.manager, task["state"]
        if manager.memory.step(task["id"], "act")["status"] == "succeeded":
            return "reply"
        if not state.get("messages"):
            raise TaskError("这项操作的上下文已清除，请重新发起请求。")
        if state.get("phase") == "model":
            return "model"
        round_index, index = state.get("round", 0), state.get("call_index", 0)
        if round_index >= MAX_ROUNDS:
            raise TaskError(f"连续操作超过 {MAX_ROUNDS} 轮，已停止。")
        if manager.memory.step(task["id"], "act")["status"] != "running":
            manager.memory.update_step(task["id"], "act", status="running", started_at=_now(), finished_at=None)
            await manager.changed(task["id"])
        call = state["calls"][index]
        outcome = await manager._call(task, f"{round_index}:{call['id']}", call)
        state["messages"].append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                  "content": json.dumps(outcome, ensure_ascii=False)[:8000]})
        state["call_index"] = index + 1
        state["phase"] = "tools" if state["call_index"] < len(state["calls"]) else "model"
        manager.memory.update_task(task["id"], state=state)
        return state["phase"]

    async def _model(self, task):
        manager, state = self.manager, task["state"]
        if manager.memory.step(task["id"], "act")["status"] == "succeeded":
            return "reply"
        config = manager.runtime._provider("llm")
        settings = manager.runtime.settings.raw()
        uncertain = any(call["status"] == "unknown" for call in task["tool_calls"])
        if settings.get("tools_enabled", True) and not uncertain:
            config["tools"] = manager.tool_definitions()
        reply_to = task["params"].get("reply_to")
        sources = [reply_to] if reply_to and manager.memory.record(reply_to) else []
        text, calls = await manager._model(task, "act", "task_action", state["messages"], config, sources)
        if calls and uncertain:
            raise TaskError("上一项操作结果未知，已停止后续工具调用；请先检查结果。")
        if not calls:
            manager.memory.update_step(task["id"], "act", status="succeeded", finished_at=_now(), result={"text": text})
            await manager.changed(task["id"])
            return "reply"
        state.update(messages=state["messages"] + [assistant_calls(text, calls)], calls=calls,
                     round=state.get("round", 0) + 1, call_index=0, phase="tools")
        manager.memory.update_task(task["id"], state=state)
        return "tools"

    async def _reply(self, task):
        outcome = self.manager.memory.step(task["id"], "act")["result"]
        await self.manager._step(task["id"], "reply", lambda: self.manager._reply(task, outcome))

    async def run(self, task, approval=None):
        await self.ensure()
        graph, config = self.graphs[task["kind"]], self.config(task["id"])
        with tracing_context(enabled=False):
            saved = await graph.aget_state(config)
            value = Command(resume=approval) if approval is not None else (
                None if saved.next else {"task_id": task["id"], "route": ""})
            result = await graph.ainvoke(value, config, durability="sync")
            return bool(result.get("__interrupt__"))

    async def delete(self, task_id):
        await self.ensure()
        await self.saver.adelete_thread(task_id)
        self.nodes.pop(task_id, None)

    async def inspect(self, task):
        await self.ensure()
        with tracing_context(enabled=False):
            state = await self.graphs[task["kind"]].aget_state(self.config(task["id"]))
        return {"engine": "langgraph", "graph_version": 1, "task_id": task["id"],
                "node": self.nodes.get(task["id"]), "next": list(state.next),
                "checkpoint_id": (state.config or {}).get("configurable", {}).get("checkpoint_id"),
                "checkpoint_at": state.created_at, "checkpoint_content": "control_only",
                "pending_calls": [item.value["call_id"] for item in state.interrupts
                                  if isinstance(item.value, dict) and "call_id" in item.value]}
