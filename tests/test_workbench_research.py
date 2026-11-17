"""Daily document loop through the real outer graph, with synthetic delegation."""
import asyncio
import importlib
import json

import pytest

from greatsage.research import ResearchWorkflow
from greatsage.source_selection import SourceSelection
from test_research import approval, delegate, runtime, seed, settled, start, until


async def decline(runtime, task):
    call = await approval(runtime, task["id"])
    await runtime.tasks.resolve(call["id"], False, "once", "click")
    await settled(runtime, task["id"])
    await until(lambda: task["id"] not in runtime.tasks.jobs)
    return call


async def test_revision_keeps_document_lineage_correction_and_separate_save(runtime, delegate, tmp_path):
    _, material, _ = seed(runtime, tmp_path)
    original = await start(runtime, material)
    first = await approval(runtime, original["id"])
    await runtime.tasks.resolve(first["id"], True, "once", "click")
    original_detail = await settled(runtime, original["id"])
    document = original_detail["artifacts"][0]["id"]
    original_file = tmp_path / "workspace" / "notes" / "research.md"
    previous = original_file.read_text(encoding="utf-8")
    correction = "SYNTHETIC 用户纠正：发布时间仍待确认。"
    revision = await ResearchWorkflow(runtime.tasks).revise(document, correction, expected_version=1)
    second = await approval(runtime, revision["id"])
    assert second["id"] != first["id"] and second["arguments"]["version"] == 2
    assert second["arguments"]["path"] == "notes/research.v2.md"
    artifact = runtime.memory.artifact(document)
    assert artifact["task_id"] == original["id"] and artifact["generating_task_id"] == revision["id"]
    assert artifact["parent_version"] == 1 and artifact["version"] == 2
    source = runtime.memory.record(artifact["correction_source_id"])
    assert source["role"] == "user" and source["text"] == correction
    assert correction not in json.dumps(runtime.memory.task(revision["id"])["params"], ensure_ascii=False)
    assert source["id"] in {row["id"] for row in delegate.calls[-1]}
    review = runtime.memory.artifact_review(document)
    assert review["diff"]["from_version"] == 1 and review["diff"]["to_version"] == 2
    assert not review["semantic_verification"]
    with pytest.raises(ValueError):
        await runtime.tasks.resolve(first["id"], True, "once", "click")
    await runtime.tasks.resolve(second["id"], False, "once", "click")
    assert (await settled(runtime, revision["id"]))["status"] == "succeeded"
    assert original_file.read_text(encoding="utf-8") == previous
    assert not (tmp_path / "workspace" / "notes" / "research.v2.md").exists()
    assert len(runtime.memory.artifacts()) == 1
    runtime.memory.delete_message(source["id"])
    assert runtime.memory.artifact(document)["status"] == "stale"
    assert not runtime.memory.artifact_review(document)["correction_source_valid"]


async def test_changed_scope_requires_explicit_update_and_old_task_is_unchanged(runtime, delegate, tmp_path):
    _, material, path = seed(runtime, tmp_path)
    task = await start(runtime, material)
    await decline(runtime, task)
    original = runtime.memory.task(task["id"])
    before = SourceSelection(runtime.tasks).inspect(original)
    assert before["selection_status"] == "selected" and before["selected_count"] == 2
    path.write_text("# Orion\nSYNTHETIC latest owner is Lin Lan.\n", encoding="utf-8")
    runtime.memory.rescan_materials()
    stale = SourceSelection(runtime.tasks).inspect(runtime.memory.task(task["id"]))
    assert stale["selection_status"] == "stale" and any(not item["available"] for item in stale["candidates"])
    workflow = ResearchWorkflow(runtime.tasks)
    with pytest.raises(ValueError, match="更新来源"):
        await workflow.reselect(task["id"], {})
    renewed = await workflow.reselect(task["id"], {}, update_sources=True)
    await decline(runtime, renewed)
    assert renewed["id"] != task["id"]
    assert runtime.memory.task(renewed["id"])["params"]["parent_task_id"] == task["id"]
    assert runtime.memory.task(task["id"])["params"] == original["params"]
    assert any("latest owner" in row["text"] for row in delegate.calls[-1])


async def test_full_coverage_failure_can_be_reselected_without_running_partial_task(runtime, delegate, tmp_path):
    path = tmp_path / "long.md"
    path.write_text("".join(f"# Section{i}\nSYNTHETIC release item{i}\n" for i in range(12)), encoding="utf-8")
    mid = runtime.memory.import_materials(str(path))["materials"][0]["id"]
    task = await start(runtime, mid, use_session=False, selection_mode="full")
    assert (await settled(runtime, task["id"]))["status"] == "failed"
    assert delegate.calls == []
    successor = await ResearchWorkflow(runtime.tasks).reselect(task["id"], {"selection_mode": "relevant"})
    await decline(runtime, successor)
    selected = SourceSelection(runtime.tasks).inspect(runtime.memory.task(successor["id"]))
    assert selected["selected_count"] == 8 and selected["total_sources"] == 12
    assert not selected["all_selected"]


async def test_concurrent_edit_prevents_revision_commit(runtime, delegate, tmp_path):
    _, material, _ = seed(runtime, tmp_path)
    task = await start(runtime, material)
    await decline(runtime, task)
    artifact = runtime.memory.task(task["id"])["artifacts"][0]
    delegate.started.clear()
    delegate.release = asyncio.Event()
    revision = await ResearchWorkflow(runtime.tasks).revise(artifact["id"], "SYNTHETIC correction", 1)
    await asyncio.wait_for(delegate.started.wait(), 3)
    runtime.memory.add_artifact_version(artifact["id"], "# Manual edit kept\n", expected_version=1)
    delegate.release.set()
    result = await settled(runtime, revision["id"])
    assert result["status"] == "failed" and not result["tool_calls"]
    current = runtime.memory.artifact(artifact["id"])
    assert current["version"] == 2 and current["content"] == "# Manual edit kept\n"
    with pytest.raises(ValueError):
        runtime.memory.add_artifact_version(artifact["id"], "stale edit", expected_version=1)


async def test_manual_edit_is_only_a_draft_and_keeps_last_revision_scope(runtime, delegate, tmp_path, monkeypatch):
    _, material, _ = seed(runtime, tmp_path)
    task = await start(runtime, material)
    await decline(runtime, task)
    document = runtime.memory.task(task["id"])["artifacts"][0]["id"]
    previous = await runtime.tasks.regenerate(document, "SYNTHETIC earlier correction", expected_version=1)
    await decline(runtime, previous)
    earlier_source = runtime.memory.artifact(document)["correction_source_id"]
    manual = "# SYNTHETIC manual edit is not evidence\nUnsupported invented figure 999999.\n"
    runtime.memory.add_artifact_version(document, manual, expected_version=2)
    instructions = []

    async def capture(*args, **kwargs):
        instructions.append(kwargs["instruction"])
        return await delegate(*args, **kwargs)

    monkeypatch.setattr(importlib.import_module("greatsage.research"), "run_delegation", capture)
    revision = await runtime.tasks.regenerate(document, "请保留有证据支持的内容", expected_version=3)
    await decline(runtime, revision)
    assert earlier_source in {row["id"] for row in delegate.calls[-1]}
    assert "<untrusted_draft>" in instructions[0] and "999999" in instructions[0]
    assert not any("999999" in row["text"] for row in delegate.calls[-1])
    assert runtime.memory.artifact(document)["version"] == 4


async def test_late_research_callback_after_close_does_not_touch_closed_store(runtime, delegate, tmp_path, monkeypatch):
    _, material, _ = seed(runtime, tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def late(service, config, sources, counter, **callbacks):
        snapshot = await callbacks["before_request"]("planner", [{"role": "user", "content": "synthetic"}], config,
                                                       [row["id"] for row in sources])
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        await callbacks["on_request_end"](snapshot, "stale")
        return await delegate(service, config, sources, counter, **callbacks)

    monkeypatch.setattr(importlib.import_module("greatsage.research"), "run_delegation", late)
    task = await start(runtime, material)
    await asyncio.wait_for(started.wait(), 3)
    job = runtime.tasks.jobs[task["id"]]
    await runtime.tasks.close()
    real_memory = runtime.memory
    real_db, late_access = real_memory._db, []

    class ClosedStore:
        def __getattr__(self, key):
            late_access.append(key)
            raise AssertionError("late callback accessed closed SQLite: " + key)

    runtime.memory = ClosedStore()
    real_memory._db = ClosedStore()
    try:
        release.set()
        result = await asyncio.wait_for(asyncio.gather(job, return_exceptions=True), 3)
        assert not any(isinstance(item, AssertionError) for item in result)
        assert not late_access
    finally:
        real_memory._db = real_db
        runtime.memory = real_memory
    assert real_memory.task(task["id"])["status"] == "interrupted"
