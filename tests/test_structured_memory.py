import json

import pytest

from greatsage.memory import MemoryStore
from greatsage.semantic import text_hash


@pytest.fixture
def store(tmp_path):
    memory = MemoryStore(tmp_path)
    yield memory
    memory.close()


def fact(source, value="绿茶", **changes):
    return {"text": "用户偏好" + value, "kind": "preference", "subject": "用户", "subject_type": "person",
            "predicate": "偏好饮品", "value": value, "object_type": "literal",
            "evidence": [{"source_id": source["id"], "quote": source["text"]}], "review_reasons": [], **changes}


@pytest.mark.parametrize("policy,reasons,status", [
    ("auto", [], "active"), ("auto", ["sensitive"], "active"),
    ("necessary", [], "active"), ("necessary", ["uncertain"], "pending"),
    ("necessary", ["ambiguous_entity"], "pending"), ("mostly", [], "pending"),
])
def test_policies_quarantine_only_the_required_extractions(store, policy, reasons, status):
    source = store.add_message("user", "我喜欢绿茶")
    snapshots = store.extraction_sources()
    result = store.save_extraction([fact(source, review_reasons=reasons)], snapshots, policy, "test")
    row = result["items"][0]
    assert row["status"] == status and result[status] == 1
    assert bool(store.list_memories()) == (status == "active")
    assert bool(store.memory_candidates_list()) == (status == "pending")
    indexed = {item["id"] for item in store.pending_vectors("test")}
    assert (row["id"] in indexed) == (status == "active")
    context_ids = {item["id"] for item in store.context("饮品", "new")["memories"]}
    assert (row["id"] in context_ids) == (status == "active")
    assert store.record_details(row["id"])["structured"]["evidence"][0]["quote"] == source["text"]
    repeated = store.save_extraction([fact(source)], snapshots, "auto")
    assert repeated["saved"] == 0 and repeated["skipped"] == 1
    assert store.record(row["id"])["status"] == status  # Policy changes do not approve an old candidate.


def test_source_selection_is_bounded_and_excludes_non_user_and_ambient(store):
    for role, source in [("assistant", "text"), ("tool", "text"), ("system", "text"),
                         ("observation", "microphone"), ("user", "system"), ("user", "process")]:
        store.add_message(role, "不能作为用户事实", source)
    selected = [store.add_message("user", f"原文 {index}", "microphone", "selected") for index in range(10)]
    store.add_message("user", "别的会话", session_id="other")
    rows = store.extraction_sources(100, "selected")
    assert len(rows) == 8 and {row["id"] for row in rows} == {row["id"] for row in selected[:8]}
    assert all(row["content_hash"] == text_hash(row["text"]) and row["version"] == 1 for row in rows)
    store.save_extraction([], rows, "auto")
    assert len(store.extraction_sources(8, "selected")) == 2
    assert store.extraction_status()["processed_sources"] == 8


@pytest.mark.parametrize("tamper", ["quote", "source_id", "hash", "text", "version", "role", "source", "session_id"])
def test_forged_evidence_or_changed_snapshot_does_not_advance_progress(store, tamper):
    source = store.add_message("user", "我喜欢绿茶")
    snapshots = store.extraction_sources()
    item = fact(source)
    if tamper in {"quote", "source_id"}:
        item["evidence"][0][tamper] = "模型捏造"
    else:
        snapshots[0]["content_hash" if tamper == "hash" else tamper] = "不匹配"
    with pytest.raises(ValueError):
        store.save_extraction([item], snapshots, "auto")
    assert not store.list_memories() and store.extraction_status()["processed_sources"] == 0


def test_non_user_source_cannot_be_submitted_by_bypassing_selection(store):
    source = store.add_message("assistant", "我喜欢绿茶", "text")
    snapshot = {**source, "content_hash": text_hash(source["text"]), "version": 1}
    with pytest.raises(ValueError, match="user original"):
        store.save_extraction([fact(source)], [snapshot], "auto")
    assert not store.list_memories()


def test_batch_limit_and_write_failure_roll_back_facts_and_checkpoint(store):
    source = store.add_message("user", "我喜欢绿茶，也曾喝红茶")
    snapshots = store.extraction_sources()
    with pytest.raises(ValueError, match="24"):
        store.save_extraction([fact(source)] * 25, snapshots, "auto")
    with store._db:
        store._db.execute("""CREATE TEMP TRIGGER extraction_failure BEFORE INSERT ON structured_memories
            WHEN NEW.value='红茶' BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
    with pytest.raises(Exception, match="simulated storage failure"):
        store.save_extraction([fact(source), fact(source, "红茶")], snapshots, "auto")
    assert not store.list_memories() and store.extraction_status()["processed_sources"] == 0
    assert store._db.execute("SELECT count(*) FROM structured_memories").fetchone()[0] == 0


def test_candidate_accept_rechecks_conflicts_and_uses_existing_conflict_resolution(store):
    source = store.add_message("user", "以前喝绿茶，现在喝红茶")
    result = store.save_extraction([fact(source), fact(source, "红茶")], store.extraction_sources(), "mostly")
    old, new = result["items"]
    store.resolve_memory_candidate(old["id"], "accept")
    with pytest.raises(ValueError, match="conflict"):
        store.resolve_memory_candidate(new["id"], "accept")
    assert store.record(new["id"])["has_conflict"]
    assert store.record(old["id"])["status"] == "active"
    store.resolve_conflict(new["id"], "replace")
    assert store.record(old["id"]) is None
    assert store.record(new["id"])["status"] == "active"
    assert not store.conflicts()


def test_automatic_extraction_conflict_cannot_replace_existing_memory(store):
    first = store.add_message("user", "我喜欢绿茶")
    old = store.save_extraction([fact(first)], store.extraction_sources(), "auto")["items"][0]
    second = store.add_message("user", "我喜欢红茶")
    new = store.save_extraction([fact(second, "红茶")], store.extraction_sources(), "auto")["items"][0]
    assert new["status"] == "pending" and new["conflict_ids"] == [old["id"]]
    assert [row["id"] for row in store.list_memories()] == [old["id"]]
    store.resolve_conflict(new["id"], "keep_existing")
    assert store.record(new["id"]) is None


@pytest.mark.parametrize("action", ["reject", "delete", "revise"])
def test_forgetting_survives_restart_and_suppresses_paraphrases_of_old_source(tmp_path, action):
    memory = MemoryStore(tmp_path)
    source = memory.add_message("user", "我喜欢绿茶")
    snapshots = memory.extraction_sources()
    row = memory.save_extraction([fact(source)], snapshots, "mostly")["items"][0]
    if action == "reject":
        memory.resolve_memory_candidate(row["id"], "reject")
    elif action == "delete":
        memory.delete_memory(row["id"])
    else:
        corrected = memory.revise_memory(row["id"], "我改为喝清水")
        assert memory.record_details(corrected["id"])["structured"] is None
    assert not memory.memory_entities()
    memory.close()
    memory = MemoryStore(tmp_path)
    try:
        assert not memory.extraction_sources()
        replay = memory.save_extraction([fact(source, text="另一种措辞：偏好绿茶")], snapshots, "auto")
        assert replay["saved"] == 0 and replay["skipped"] == 1
        assert memory.record(source["id"])  # Forgetting a memory deliberately keeps its original.
        assert memory._db.execute("SELECT count(*) FROM structured_memories").fetchone()[0] == 0
        assert "绿茶" not in json.dumps([dict(row) for row in memory._db.execute("SELECT * FROM memory_extraction_blocks")], ensure_ascii=False)
        newer = memory.add_message("user", "今天我重新确认喜欢绿茶")
        assert memory.save_extraction([fact(newer)], memory.extraction_sources(), "auto")["saved"] == 1
        assert memory.add_memory("用户明确重新添加的偏好", [source["id"]])["status"] == "active"
    finally:
        memory.close()


def test_legacy_explicit_forgetting_is_migrated_and_blocks_late_extraction(tmp_path):
    memory = MemoryStore(tmp_path)
    source = memory.add_message("user", "我喜欢绿茶")
    snapshots = memory.extraction_sources()
    summary = memory.save_summary("绿茶偏好", [source["id"]])
    explicit = memory.add_memory("绿茶偏好", [summary["id"]])
    memory.delete_memory(explicit["id"])
    with memory._db:
        memory._db.execute("DELETE FROM memory_extraction_blocks")  # Simulate pre-v0.6 schema data.
    memory.close()
    memory = MemoryStore(tmp_path)
    try:
        assert not memory.extraction_sources()
        assert memory.save_extraction([fact(source)], snapshots, "auto")["saved"] == 0
    finally:
        memory.close()


def test_source_revision_deletes_structures_vectors_and_derived_replies(store):
    source = store.add_message("user", "我在海鸥项目工作")
    item = fact(source, "海鸥", kind="relationship", predicate="参与项目", object_type="project")
    row = store.save_extraction([item], store.extraction_sources(), "auto")["items"][0]
    snapshots = [store._source_snapshot(source)]
    store.save_vectors(row["id"], "test", text_hash(row["text"]), [[1, 0]])
    reply = store.add_message("assistant", "海鸥项目已记录", metadata={"source_ids": [row["id"]]})
    entities = store.memory_entities()
    project = next(entity for entity in entities if entity["type"] == "project")
    assert store.memory_entity(project["id"])["relationships"][0]["id"] == row["id"]
    revised = store.revise_message(source["id"], "我在白鹭项目工作")
    assert not store.memory_entities() and store.record(reply["id"]) is None
    assert store.semantic_search([1, 0], "test") == []
    with pytest.raises(ValueError, match="no longer exists"):
        store.save_extraction([item], snapshots, "auto")
    assert store.extraction_sources()[0]["id"] == revised["id"]
    assert store.extraction_sources()[0]["version"] == 2


def test_clear_history_invalidates_pending_results_and_preserves_manual_memories(store):
    source = store.add_message("user", "我喜欢绿茶")
    snapshots = store.extraction_sources()
    store.save_extraction([fact(source)], snapshots, "mostly")
    manual = store.add_memory("独立手动添加")
    store.clear_history()
    assert not store.memory_candidates_list() and not store.memory_entities()
    assert store.extraction_status()["processed_sources"] == 0
    assert [row["id"] for row in store.list_memories()] == [manual["id"]]
    with pytest.raises(ValueError):
        store.save_extraction([fact(source)], snapshots, "auto")


@pytest.mark.parametrize("operation", ["revision", "clear"])
def test_failed_source_mutation_rolls_back_structures_and_extraction_progress(store, monkeypatch, operation):
    source = store.add_message("user", "我喜欢绿茶")
    row = store.save_extraction([fact(source)], store.extraction_sources(), "auto")["items"][0]
    before = store.extraction_status()

    def fail(*args, **kwargs):
        raise RuntimeError("simulated mutation failure")

    monkeypatch.setattr(store, "_insert_message" if operation == "revision" else "_clear_task_history", fail)
    with pytest.raises(RuntimeError, match="simulated mutation failure"):
        if operation == "revision":
            store.revise_message(source["id"], "未提交的改动")
        else:
            store.clear_history()
    assert store.record(row["id"])["structured"]["value"] == "绿茶"
    assert store.record(source["id"])["text"] == source["text"]
    assert store.extraction_status() == before
