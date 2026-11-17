"""Revision lineage, transactional publication and bounded source review."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from greatsage.artifact_review import MAX_DIFF_OUTPUT_CHARS, MAX_PASSAGES, bounded_diff, review_passages
from greatsage.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    instance = MemoryStore(tmp_path / "data")
    yield instance
    instance.close()


def document(store, tmp_path):
    path = tmp_path / "selected.md"
    path.write_text("# SYNTHETIC plan\nThe owner is Alice.\n", encoding="utf-8")
    material = store.import_materials(str(path))["materials"][0]
    chunk = store.material_chunks(material_ids=[material["id"]])[0]
    sources = {"S1": {"kind": "chunk", "id": chunk["id"], "label": "selected.md", "excerpt": chunk["text"]}}
    first_session, later_session = store.new_session(), store.new_session()
    original_task = store.create_task("research", "original", "console", first_session, "", {}, [])
    revision_task = store.create_task("research", "revision", "console", later_session, "", {}, [])
    artifact = store.create_artifact("document", "SYNTHETIC", "# SYNTHETIC\n- Owner: Alice [S1]\n", sources, original_task)
    correction = store.add_message("user", "SYNTHETIC private correction: add the source context.", "text", later_session)
    return artifact, sources, correction, original_task, revision_task, first_session, later_session, path


def commit(store, fixture, **changes):
    artifact, sources, correction, _, task, *_ = fixture
    args = {"artifact_id": artifact["id"], "expected_version": 1, "task_id": task,
            "content": "# SYNTHETIC\n- The selected plan names Alice as owner. [S1]\n", "sources": sources,
            "correction_source_id": correction["id"], "note": "user-requested revision"}
    return store.commit_revision(**(args | changes))


def test_revision_preserves_history_and_links_both_task_sessions(store, tmp_path):
    fixture = document(store, tmp_path)
    original, _, correction, initial_task, revision_task, first_session, later_session, _ = fixture
    revised = commit(store, fixture)
    assert revised["id"] == original["id"] and revised["task_id"] == initial_task
    assert revised["version"] == revised["current_version"] == 2
    assert revised["generating_task_id"] == revision_task and revised["parent_version"] == 1
    assert revised["correction_source_id"] == correction["id"] and revised["correction_source_valid"] is True
    assert store.artifact(original["id"], 1)["content"] == original["content"]
    assert [row["generating_task_id"] for row in revised["lineage"]] == [initial_task, revision_task]
    assert [row["parent_version"] for row in revised["versions"]] == [1, None]
    assert correction["text"] not in json.dumps(revised["lineage"])
    assert store.artifacts(task_id=initial_task)[0]["task_versions"] == [1]
    assert store.artifacts(task_id=revision_task)[0]["task_versions"] == [2]
    assert store.task(revision_task)["artifacts"][0]["id"] == original["id"]
    assert [row["id"] for row in store.artifacts(session_id=first_session)] == [original["id"]]
    assert [row["id"] for row in store.artifacts(session_id=later_session)] == [original["id"]]
    assert not store.artifacts(session_id=store.new_session())


def test_retry_returns_its_historical_version_after_another_edit_and_restart(store, tmp_path):
    fixture = document(store, tmp_path)
    committed = commit(store, fixture)
    store.add_artifact_version(committed["id"], "# SYNTHETIC\n- Later manual change [S1]\n")
    repeated = commit(store, fixture, content="must not replace committed content")
    assert repeated["version"] == 2 and repeated["current_version"] == 3
    assert repeated["content"] == committed["content"]
    reopened = MemoryStore(tmp_path / "data")
    try:
        again = commit(reopened, fixture)
        assert again["version"] == 2 and again["current_version"] == 3
        assert len(again["versions"]) == 3
    finally:
        reopened.close()


def test_concurrent_revisions_cannot_publish_over_the_same_parent(store, tmp_path):
    fixture = document(store, tmp_path)
    barrier = threading.Barrier(2)

    def publish(task):
        barrier.wait()
        try:
            return commit(store, fixture, task_id=task)["version"]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(publish, ["revision-a", "revision-b"]))
    assert sorted(outcomes, key=str) == [2, "conflict"]
    assert store.artifact(fixture[0]["id"])["current_version"] == 2
    assert store._db.execute("SELECT count(*) FROM artifact_versions").fetchone()[0] == 2
    assert store._db.execute("SELECT count(*) FROM artifact_revision_links").fetchone()[0] == 2


def test_failed_publish_rolls_back_version_citations_lineage_and_dependencies(store, tmp_path, monkeypatch):
    fixture = document(store, tmp_path)
    tables = ("artifact_versions", "citations", "artifact_revision_links", "dependencies")
    counts = {table: store._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    insert = store._insert_version

    def fail_after_insert(*args, **kwargs):
        insert(*args, **kwargs)
        raise RuntimeError("SYNTHETIC transaction failure")

    monkeypatch.setattr(store, "_insert_version", fail_after_insert)
    with pytest.raises(RuntimeError, match="transaction failure"):
        commit(store, fixture)
    assert store.artifact(fixture[0]["id"])["version"] == 1
    assert counts == {table: store._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


@pytest.mark.parametrize("removal", ["delete", "clear_history"])
def test_uncited_correction_still_invalidates_artifact_without_copying_text(store, tmp_path, removal):
    fixture = document(store, tmp_path)
    revised = commit(store, fixture)
    assert all(cite["source_kind"] == "chunk" for cite in revised["citations"])
    if removal == "delete":
        store.delete_message(fixture[2]["id"])
    else:
        store.clear_history()
    stale = store.artifact(revised["id"])
    assert stale["status"] == "stale" and stale["content"] == revised["content"]
    assert stale["correction_source_valid"] is False
    assert all(cite["valid"] for cite in stale["citations"]), "material evidence remains separate from a deleted correction"
    review = store.artifact_review(revised["id"])
    assert review["correction_source_valid"] is False


def test_rescan_invalidates_citations_and_locators_in_every_revision(store, tmp_path):
    fixture = document(store, tmp_path)
    revised = commit(store, fixture)
    fixture[-1].write_text("# SYNTHETIC plan\nThe owner is Bob.\n", encoding="utf-8")
    store.rescan_materials()
    for number in (1, 2):
        review = store.artifact_review(revised["id"], number)
        assert review["invalid_markers"] == ["S1"]
        assert review["citations"][0]["locator"] is None
        assert review["citations"][0]["excerpt"] is None
        assert review["semantic_verification"] is False
    assert store.artifact(revised["id"], 1)["content"] == fixture[0]["content"]


def test_review_maps_text_to_real_chunk_and_message_locators_and_unknown_markers(store, tmp_path):
    fixture = document(store, tmp_path)
    original, sources, message, *_ = fixture
    sources = sources | {"S2": {"kind": "message", "id": message["id"], "label": "user correction", "excerpt": message["text"]}}
    content = "# SYNTHETIC\n- Evidence [S1]\n- User input [S2]\n- Unmapped [S99]\n```\ncode [S1]\n```\n"
    edited = store.add_artifact_version(original["id"], content, sources=sources)
    review = store.artifact_review(original["id"])
    assert review["version"] == 2 and review["parent_version"] == 1
    assert review["diff"]["from_version"] == 1 and review["diff"]["to_version"] == 2
    assert "+- Unmapped [S99]" in review["diff"]["text"] and not review["diff"]["truncated"]
    assert [row["line_number"] for row in review["passages"]] == [2, 3, 4]
    assert [row["citation_status"] for row in review["passages"]] == ["cited", "cited", "unknown"]
    assert review["unknown_markers"] == ["S99"] and review["invalid_markers"] == []
    chunk, chat = review["citations"]
    assert chunk["locator"] == {"kind": "chunk", "source_id": sources["S1"]["id"],
                                "material_id": store.materials()[0]["id"], "name": "selected.md", "version": 1,
                                "heading": "SYNTHETIC plan", "line_start": 1, "line_end": 2}
    assert chat["locator"]["source_id"] == message["id"] and chat["locator"]["session_id"] == message["session_id"]
    assert store.artifact_review(original["id"], 1)["diff"] is None
    with pytest.raises(KeyError):
        store.artifact_review(edited["id"], compare_to=99)


def test_diff_and_passages_have_explicit_bounded_output():
    diff = bounded_diff("old line\n" * 100_000, "new line\n" * 100_000, 1, 2)
    assert diff["truncated"] and diff["input_truncated"]
    assert len(diff["text"]) <= MAX_DIFF_OUTPUT_CHARS
    passages, cut = review_passages("- evidence [S99]\n" * 1000, [])
    assert cut and len(passages) == MAX_PASSAGES
    assert all(row["citation_status"] == "unknown" for row in passages)


def test_deleting_artifact_cleans_revision_links_but_keeps_sources(store, tmp_path):
    fixture = document(store, tmp_path)
    commit(store, fixture)
    artifact_id = fixture[0]["id"]
    store.delete_artifact(artifact_id)
    for table in ("artifacts", "artifact_versions", "artifact_revision_links", "citations"):
        assert store._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert not store._db.execute("SELECT 1 FROM dependencies WHERE owner_type='artifact'").fetchone()
    assert store.record(fixture[2]["id"]) and store.record(fixture[1]["S1"]["id"])
    assert not store.artifacts(task_id=fixture[4])


def test_legacy_versions_migrate_without_inventing_later_generation_tasks(store, tmp_path):
    fixture = document(store, tmp_path)
    store.add_artifact_version(fixture[0]["id"], "# SYNTHETIC\n- Existing manual edit [S1]\n")
    with store._db:
        store._db.execute("DROP TABLE artifact_revision_links")
    reopened = MemoryStore(tmp_path / "data")
    try:
        artifact = reopened.artifact(fixture[0]["id"])
        assert artifact["version"] == 2
        assert artifact["lineage"][0]["generating_task_id"] == fixture[3]
        assert artifact["lineage"][1]["generating_task_id"] == ""
        assert artifact["lineage"][1]["parent_version"] == 1
        assert artifact["citations"][0]["valid"]
    finally:
        reopened.close()


@pytest.mark.parametrize("invalid", ["assistant", "deleted", "stale_version", "unknown_citation"])
def test_invalid_revision_identity_or_source_never_creates_half_version(store, tmp_path, invalid):
    fixture = document(store, tmp_path)
    changes = {}
    if invalid == "assistant":
        changes["correction_source_id"] = store.add_message("assistant", "SYNTHETIC suggestion")["id"]
    elif invalid == "deleted":
        store.delete_message(fixture[2]["id"])
    elif invalid == "stale_version":
        changes["expected_version"] = 2
    else:
        changes["content"] = "# SYNTHETIC\n- Unknown citation [S9]\n"
    with pytest.raises(ValueError):
        commit(store, fixture, **changes)
    assert store.artifact(fixture[0]["id"])["version"] == 1
    assert store._db.execute("SELECT count(*) FROM artifact_revision_links").fetchone()[0] == 1
