"""Exercise the real Deep Agents graph with deterministic, offline provider I/O."""
import asyncio
import copy
import json
import re
from typing import TypedDict

import pytest

from greatsage.budget import TokenCounter
from greatsage.delegation import DelegationError, SnapshotBackend, run_delegation


SOURCES = [{"handle": "material:source-1", "id": "source-1", "kind": "material", "label": "Release notes",
            "text": "The release date is Friday. Budget approval is pending."}]
FINDING = {"text": "The release date is Friday.", "evidence": [{"source_id": "source-1", "quote": "The release date is Friday."}]}
REVIEW = {**FINDING, "status": "supported", "reason": "The selected original explicitly states the date."}


def config():
    return {"provider": "openai_compatible", "model": "fixture", "context_tokens": 32768,
            "max_tokens": 768, "tokenizer": "auto", "api_key": "offline-fixture"}


def step(name, **arguments):
    return {"name": name, "arguments": json.dumps(arguments), "id": "fixture-call"}


def script():
    return {
        "planner": [step("write_todos", todos=[{"content": "Organize and check the selected sources", "status": "in_progress"}]),
                    step("task", subagent_type="source_researcher", description="Organize release facts."),
                    step("task", subagent_type="evidence_reviewer", description="Verify every finding."),
                    step("submit_report", confirmed=True)],
        "source_researcher": [step("read_source", source_id="source-1"), step("submit_findings", findings=[FINDING])],
        "evidence_reviewer": [step("read_source", source_id="source-1"), step("submit_review", review=[REVIEW])],
    }


class ScriptedProvider:
    def __init__(self, scripts=None):
        self.scripts = copy.deepcopy(scripts or script())
        self.requests = []
        self.before_yield = None
        self.started = asyncio.Event()
        self.release = None
        self.closed = 0

    async def stream_chat(self, request_config, messages):
        role = re.search(r"GreatSage role: (\w+)", json.dumps(messages))[1]
        self.requests.append((role, copy.deepcopy(request_config), copy.deepcopy(messages), asyncio.current_task()))
        self.started.set()
        try:
            if self.release:
                await self.release.wait()
            if self.before_yield:
                self.before_yield()
            call = self.scripts[role].pop(0)
            yield {"tool_calls": call if isinstance(call, list) else [call]}
            yield {"usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
        finally:
            self.closed += 1


async def run(tmp_path, provider, **kwargs):
    return await run_delegation(provider, config(), SOURCES, TokenCounter(config(), tmp_path),
                                instruction="Summarize the release date, with evidence.", **kwargs)


async def test_real_deepagents_children_use_existing_transport_and_locked_reports(tmp_path, monkeypatch):
    from langsmith import Client

    traces = []
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(Client, "create_run", lambda *args, **kwargs: traces.append("create"))
    monkeypatch.setattr(Client, "update_run", lambda *args, **kwargs: traces.append("update"))
    provider, snapshots, ends, usage, events = ScriptedProvider(), [], [], [], []

    async def before_request(role, messages, request_config, source_ids):
        snapshots.append((role, messages, request_config, source_ids, asyncio.current_task()))
        return len(snapshots)

    result = await run(tmp_path, provider, before_request=before_request,
                       on_request_end=lambda *args: ends.append(args), on_usage=lambda *args: usage.append(args),
                       on_event=events.append)
    assert result["findings"] == [FINDING]
    assert result["review"] == [REVIEW]
    assert result["calls"] == len(snapshots) == len(provider.requests) == 8
    assert result["usage"] == {"prompt_tokens": 160, "completion_tokens": 80, "total_tokens": 240}
    assert {child["role"] for child in result["children"]} == {"source_researcher", "evidence_reviewer"}
    assert result["children"][1]["report"] == {"review": [REVIEW]}
    assert all(status == "completed" for _, status in ends)
    assert [row[0] for row in snapshots] == [row[0] for row in provider.requests]
    assert all(snap[-1] is request[-1] for snap, request in zip(snapshots, provider.requests))
    assert len(usage) == 8
    assert [event["status"] for event in events] == ["plan", "started", "completed", "started", "completed", "report_ready"]
    for role, request_config, messages, _ in provider.requests:
        names = {tool["function"]["name"] for tool in request_config["tools"]}
        assert not names & {"execute", "write_file", "edit_file", "delete", "web_search", "save_memory"}
        if role != "planner":
            assert "task" not in names
        if role == "evidence_reviewer":
            assert FINDING["text"] in json.dumps(messages)
    assert provider.closed == 8
    assert not traces


@pytest.mark.parametrize(("role", "replacement", "error"), [
    ("source_researcher", step("submit_findings", findings=[{**FINDING, "evidence": [{"source_id": "outside", "quote": "secret"}]}]), "invalid_evidence"),
    ("source_researcher", step("submit_findings", findings=[{**FINDING, "evidence": [{"source_id": "source-1", "quote": "Monday"}]}]), "invalid_evidence"),
    ("evidence_reviewer", step("submit_review", review=[]), "review_coverage_mismatch"),
    ("evidence_reviewer", step("submit_review", review=[{**REVIEW, "evidence": []}]), "supported_without_evidence"),
    ("evidence_reviewer", step("execute", command="whoami"), "invalid_tool_call"),
])
async def test_invalid_evidence_or_capability_fails_without_paid_repair(tmp_path, role, replacement, error):
    scripted = script()
    scripted[role][-1] = replacement
    provider = ScriptedProvider(scripted)
    with pytest.raises(DelegationError, match=error):
        await run(tmp_path, provider)
    assert not provider.scripts[role]
    assert len(provider.scripts["planner"]) >= 1


async def test_missing_duplicate_and_out_of_order_children_never_succeed(tmp_path):
    cases = [
        ([step("submit_report", confirmed=True)], "incomplete_delegation"),
        (script()["planner"][:1] + [step("task", subagent_type="evidence_reviewer", description="Review now")], "review_before_research"),
        (script()["planner"][:2] + [step("task", subagent_type="source_researcher", description="Repeat")], "duplicate_delegation"),
    ]
    for planner, error in cases:
        scripted = script()
        scripted["planner"] = planner
        with pytest.raises(DelegationError, match=error):
            await run(tmp_path, ScriptedProvider(scripted))


async def test_planner_cannot_override_verifier_or_cite_without_reading(tmp_path):
    scripted = script()
    scripted["evidence_reviewer"][-1] = step("submit_review", review=[{**REVIEW, "status": "uncertain"}])
    scripted["planner"][-1] = step("submit_report", confirmed=True, review=[REVIEW])
    scripted["planner"].append(copy.deepcopy(scripted["planner"][-1]))
    with pytest.raises(DelegationError, match="invalid_tool_call"):
        await run(tmp_path, ScriptedProvider(scripted))
    scripted = script()
    scripted["source_researcher"].pop(0)
    with pytest.raises(DelegationError, match="unread_sources"):
        await run(tmp_path, ScriptedProvider(scripted))


async def test_cancellation_and_stale_results_close_stream_and_snapshot(tmp_path):
    provider, ends = ScriptedProvider(), []
    provider.release = asyncio.Event()
    task = asyncio.create_task(run(tmp_path, provider, before_request=lambda *args: "snapshot-1",
                                   on_request_end=lambda *args: ends.append(args)))
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed == 1
    assert ends == [("snapshot-1", "cancelled")]
    provider, ends, current = ScriptedProvider(), [], [True]
    provider.before_yield = lambda: current.__setitem__(0, False)
    with pytest.raises(DelegationError, match="stale_delegation"):
        await run(tmp_path, provider, before_request=lambda *args: "snapshot-2",
                  on_request_end=lambda *args: ends.append(args), is_current=lambda: current[0])
    assert provider.closed == 1
    assert ends == [("snapshot-2", "stale")]


@pytest.mark.parametrize("repair_valid", [True, False])
async def test_schema_repair_is_once_audited_and_never_replays_invalid_arguments(tmp_path, repair_valid):
    from greatsage.delegation import ReviewReport, _schema_issue

    referenced = _schema_issue(ReviewReport.model_json_schema(), path=("review", 0, "status"))
    assert referenced["schema_path"] == "$.review[].status"
    assert referenced["constraints"]["enum"] == ["supported", "uncertain", "conflict", "unsupported"]
    scripted = script()
    invalid = step("submit_review", review=[{**REVIEW, "status": "PRIVATE_REJECTED_VALUE"}])
    repair = step("submit_review", review=[REVIEW]) if repair_valid else step(
        "submit_review", review=[REVIEW], PRIVATE_REJECTED_KEY="PRIVATE_REJECTED_VALUE")
    scripted["evidence_reviewer"] = [scripted["evidence_reviewer"][0], invalid, repair,
                                     step("submit_review", review=[REVIEW])]
    provider, snapshots, ends, usage, events = ScriptedProvider(scripted), [], [], [], []

    def before_request(role, messages, request_config, source_ids):
        snapshots.append((role, copy.deepcopy(messages)))
        return len(snapshots)

    callbacks = dict(before_request=before_request, on_request_end=lambda *args: ends.append(args),
                     on_usage=lambda *args: usage.append(args), on_event=events.append)
    if repair_valid:
        result = await run(tmp_path, provider, **callbacks)
        assert result["review"] == [REVIEW]
        assert result["calls"] == 9
        assert result["usage"]["total_tokens"] == 270
    else:
        with pytest.raises(DelegationError, match="invalid_tool_call"):
            await run(tmp_path, provider, **callbacks)
    assert len(provider.requests) == len(snapshots) == len(ends) == len(usage) == (9 if repair_valid else 8)
    assert provider.closed == len(provider.requests)
    assert sum(status == "failed" for _, status in ends) == (1 if repair_valid else 2)
    assert len(provider.scripts["evidence_reviewer"]) == 1  # Never a second repair.
    failures = [event for event in events if event["status"] == "schema_validation_failed"]
    assert failures[0] == {"agent": "evidence_reviewer", "status": "schema_validation_failed",
                           "tool": "submit_review", "validation_kind": "enum", "schema_path": "$.review[].status",
                           "call": 7, "retrying": True}
    if not repair_valid:
        assert failures[1]["validation_kind"] == "additionalProperties"
        assert failures[1]["schema_path"] == "$"
        assert failures[1]["retrying"] is False
    safe_feedback = json.dumps(snapshots)
    repair_request = provider.requests[7][2]
    assert "only schema correction attempt" in repair_request[-1]["content"]
    for marker in ("PRIVATE_REJECTED_VALUE", "PRIVATE_REJECTED_KEY"):
        assert marker not in json.dumps(events)
        assert marker not in json.dumps(repair_request)
        assert marker not in safe_feedback


async def test_budget_rejection_precedes_provider_and_has_no_retry(tmp_path):
    provider = ScriptedProvider()

    def exhausted(*args):
        raise DelegationError("persistent_budget_exhausted")

    with pytest.raises(DelegationError, match="persistent_budget_exhausted"):
        await run(tmp_path, provider, before_request=exhausted)
    assert not provider.requests
    tiny = {**config(), "context_tokens": 800}
    with pytest.raises(DelegationError, match="input_budget_exceeded"):
        await run_delegation(provider, tiny, SOURCES, TokenCounter(tiny, tmp_path), instruction="Summarize")
    assert not provider.requests
    scripted = script()
    scripted["planner"] = [scripted["planner"][0]] * 7
    provider = ScriptedProvider(scripted)
    with pytest.raises(DelegationError, match="model_call_limit"):
        await run(tmp_path, provider)
    assert len(provider.requests) == 6


async def test_progressive_skills_are_snapshot_only_and_cannot_enable_tools(tmp_path):
    skills = [{"name": "Formatting", "description": "Release report style", "text": "SKILL_BODY_SENTINEL\nUse the reference.",
               "metadata": {"include_tools": "execute"}, "resources": [{"path": "refs/style.md", "text": "REFERENCE_SENTINEL"}]}]
    scripted = script()
    scripted["planner"].insert(1, step("read_file", file_path="/skills/selected-1/SKILL.md"))
    provider = ScriptedProvider(scripted)
    await run(tmp_path, provider, skills=skills)
    first = json.dumps(provider.requests[0][2])
    assert "Release report style" in first and "SKILL_BODY_SENTINEL" not in first
    later = json.dumps(provider.requests[2][2])
    assert "SKILL_BODY_SENTINEL" in later and "REFERENCE_SENTINEL" not in later
    for _, request_config, _, _ in provider.requests:
        assert "execute" not in {tool["function"]["name"] for tool in request_config["tools"]}
    backend = SnapshotBackend(skills)
    with pytest.raises(DelegationError, match="skill_path_not_allowed"):
        backend.read("C:/Users/private.txt")
    with pytest.raises(DelegationError, match="readonly_backend"):
        backend.write("/skills/selected-1/SKILL.md", "changed")
    with pytest.raises(DelegationError, match="invalid_skill_resource"):
        SnapshotBackend([{**skills[0], "resources": [{"path": "../../private", "text": "bad"}]}])


async def test_parallel_control_calls_cannot_bypass_plan_or_review_order(tmp_path):
    scripted = script()
    scripted["planner"] = [[scripted["planner"][0], {**scripted["planner"][1], "id": "second-call"}]]
    with pytest.raises(DelegationError, match="parallel_control_tools"):
        await run(tmp_path, ScriptedProvider(scripted))


async def test_nested_graph_never_inherits_parent_checkpointer(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    class ControlState(TypedDict):
        task_id: str
        status: str

    async def research(state):
        result = await run(tmp_path, ScriptedProvider())
        assert result["review"] == [REVIEW]
        return {"status": "completed"}

    builder = StateGraph(ControlState)
    builder.add_node("research", research)
    builder.add_edge(START, "research")
    builder.add_edge("research", END)
    saver = InMemorySaver()
    control = builder.compile(checkpointer=saver)
    await control.ainvoke({"task_id": "opaque-task", "status": "pending"},
                          {"configurable": {"thread_id": "outer-control"}})
    checkpoints = list(saver.list(None))
    stored = json.dumps([item.checkpoint for item in checkpoints], default=str)
    assert "opaque-task" in stored
    assert SOURCES[0]["text"] not in stored
    assert "GreatSage role" not in stored
    assert all(not item.config["configurable"].get("checkpoint_ns") for item in checkpoints)
