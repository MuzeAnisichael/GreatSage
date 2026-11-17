"""Scope/budget decisions use the real store and no model or network."""
import json
from types import SimpleNamespace

import pytest

from greatsage.memory import MemoryStore
from greatsage.source_selection import SourceSelection, selection_options
from greatsage.tasks import TaskManager


class Manager:
    _messages, _records = TaskManager._messages, TaskManager._records

    def __init__(self, directory):
        self.memory = MemoryStore(directory)
        self.runtime = SimpleNamespace(data_dir=directory)

    def _llm(self, output):
        return {"context_tokens": 131072, "max_tokens": output, "model": "synthetic"}


@pytest.fixture
def manager(tmp_path):
    value = Manager(tmp_path / "data")
    yield value
    value.memory.close()


def scope(manager, tmp_path, text, **options):
    path = tmp_path / "selected.md"
    path.write_text(text, encoding="utf-8")
    material = manager.memory.import_materials(str(path))["materials"][0]
    params = {"material_ids": [material["id"]], "session_ids": [],
              "instructions": "红杉项目负责人", **selection_options(options)}
    selector = SourceSelection(manager)
    params["source_scope"] = selector.freeze(params)
    return selector, {"title": "资料整理", "params": params}, path


def test_question_selection_stays_in_scope_and_supports_pin_exclude(manager, tmp_path):
    selector, task, _ = scope(manager, tmp_path, "".join(f"# 章节{i}\n无关采购记录{i}\n" for i in range(10))
                              + "# 红杉\n红杉项目负责人是林蓝。\n")
    outside = manager.memory.add_message("user", "红杉项目负责人是错误的库外来源")
    selected = selector.select(task)
    assert not selected["error"] and len(selected["sources"]) == 1
    assert outside["id"] not in {ref["id"] for ref in selected["sources"]}
    first = task["params"]["source_scope"]["sources"][0]["id"]
    chosen = selected["sources"][0]["id"]
    task["params"].update(pinned_source_ids=[first], excluded_source_ids=[chosen])
    selected = selector.select(task)
    assert [ref["id"] for ref in selected["sources"]] == [first]
    assert selected["reasons"][first] == "pinned" and selected["reasons"][chosen] == "excluded"
    assert "林蓝" not in json.dumps(selected, ensure_ascii=False), "selection metadata contains no excerpts"
    task["params"]["pinned_source_ids"] = [outside["id"]]
    assert "范围" in selector.select(task)["error"]


@pytest.mark.parametrize("instruction,mode", [("请逐段核对全文", "relevant"), ("红杉", "full")])
def test_full_coverage_never_silently_reduces_scope(manager, tmp_path, instruction, mode):
    selector, task, _ = scope(manager, tmp_path, "".join(f"# 红杉{i}\n负责人是林蓝{i}\n" for i in range(10)), selection_mode=mode)
    task["params"]["instructions"] = instruction
    result = selector.select(task)
    assert result["mode"] == "full" and result["selection_status"] == "needs_attention"
    assert "全文" in result["error"] and not result["all_selected"]


def test_pinned_source_over_budget_is_error_not_truncation(manager, tmp_path):
    selector, task, _ = scope(manager, tmp_path, "# 唯一来源\n" + "synthetic text " * 400)
    sid = task["params"]["source_scope"]["sources"][0]["id"]
    task["params"]["pinned_source_ids"] = [sid]
    manager._llm = lambda _: {"context_tokens": 3000, "max_tokens": 1000, "model": "synthetic"}
    result = selector.select(task)
    assert result["error"] and result["sources"] == []
    original = manager.memory.material_chunks(ids=[sid])[0]["text"]
    assert selector.source(task, sid)["text"] == original and len(original) > 2000


def test_snapshot_does_not_adopt_new_messages_or_changed_material(manager, tmp_path):
    selector, task, path = scope(manager, tmp_path, "# 红杉\n原始私人来源\n")
    sid = manager.memory.new_session()
    message = manager.memory.add_message("user", "原始用户输入", session_id=sid)
    task["params"]["session_ids"] = [sid]
    task["params"]["source_scope"] = selector.freeze(task["params"])
    manager.memory.add_message("user", "迟到的其他输入", session_id=sid)
    assert len(selector.select(task)["sources"]) == 2
    old_chunk = task["params"]["source_scope"]["sources"][-1]["id"]
    path.write_text("# 红杉\n已经变更\n", encoding="utf-8")
    manager.memory.rescan_materials()
    assert selector.select(task)["selection_status"] == "stale"
    with pytest.raises(KeyError):
        selector.source(task, old_chunk)
    manager.memory.delete_message(message["id"])
    assert selector.select(task)["selection_status"] == "stale"


def test_scoped_search_filters_before_global_candidate_limits(manager, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("".join(f"# irrelevant{i}\nneedle\n" for i in range(320)), encoding="utf-8")
    manager.memory.import_materials(str(outside))
    _, task, _ = scope(manager, tmp_path, "# chosen\nneedle uniquely inside selected scope\n")
    results = manager.memory.search_chunks("needle", 8, task["params"]["material_ids"])
    assert len(results) == 1 and "uniquely" in results[0]["text"]
    assert manager.memory.search_chunks("needle", 8, []) == []


def test_new_correction_terms_are_not_crowded_out_by_original_instruction(manager, tmp_path):
    selector, task, _ = scope(manager, tmp_path, "".join(f"# Old{i}\n旧采购记录{i}\n" for i in range(10))
                              + "# 青鸟\n青鸟项目经理是林蓝\n")
    task["params"]["instructions"] = " ".join(f"oldterm{i}" for i in range(50))
    correction = manager.memory.add_message("user", "请补充青鸟的项目经理", "artifact_correction")
    task["params"]["correction_source_id"] = correction["id"]
    task["params"]["source_scope"] = selector.freeze(task["params"])
    selected = selector.select(task)
    texts = [selector.source(task, ref["id"])["text"] for ref in selected["sources"]]
    assert not selected["error"] and any("经理是林蓝" in text for text in texts)
