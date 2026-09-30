"""Markdown materials: locators, versions, and what deletion does downstream."""
import pytest

from greatsage.materials import HARD_CHARS, split_markdown
from greatsage.memory import MemoryStore


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_split_keeps_heading_paths_line_ranges_and_code_fences():
    text = "前言段落。\n\n# 项目\n\n## 进度\n第一行\n第二行\n\n```\n# 不是标题\n```\n\n## 风险\n预算不足\n"
    chunks = split_markdown(text)
    assert [chunk["heading"] for chunk in chunks] == ["", "项目 / 进度", "项目 / 风险"]
    assert chunks[1]["line_start"] == 5 and "# 不是标题" in chunks[1]["text"]
    assert chunks[2]["text"].endswith("预算不足") and chunks[2]["line_end"] == 14
    long_line = "字" * (HARD_CHARS * 2 + 5)
    pieces = split_markdown(long_line)
    assert len(pieces) == 3 and all(piece["line_start"] == 1 for piece in pieces)


def test_import_only_markdown_with_readable_errors_and_versioning(tmp_path):
    store = MemoryStore(tmp_path / "data")
    folder = tmp_path / "notes"
    first = write(folder / "a.md", "# 周会\n小林负责测试。\n")
    write(folder / "skip.txt", "不是 markdown")
    write(folder / ".hidden" / "b.md", "# 隐藏\n")
    (folder / "bad.md").write_bytes("编码".encode("gbk"))
    result = store.import_materials(str(folder))
    assert [item["name"] for item in result["materials"]] == ["a.md"]
    assert result["errors"][0]["path"].endswith("bad.md")
    with pytest.raises(ValueError, match=".md"):
        store.import_materials(str(folder / "skip.txt"))
    with pytest.raises(ValueError, match="完整路径"):
        store.import_materials("notes/a.md")
    assert store.import_materials(str(first))["materials"][0]["action"] == "unchanged"
    write(first, "# 周会\n小林负责测试，周五前完成。\n")
    changed = store.rescan_materials()["materials"][0]
    assert changed["action"] == "updated" and changed["version"] == 2
    assert "周五前" in store.search_chunks("周五前完成")[0]["text"]
    first.unlink()
    assert store.rescan_materials()["status_changes"] == [{"id": changed["id"], "status": "missing"}]
    assert store.material(changed["id"])["chunks"], "a missing file keeps its last imported snapshot"


def test_deleting_or_revising_material_cascades_answers_and_fails_artifact_citations(tmp_path):
    store = MemoryStore(tmp_path / "data")
    path = write(tmp_path / "plan.md", "# 发布\n发布日期是十月八日。\n")
    material = store.import_materials(str(path))["materials"][0]
    chunk = store.material_chunks(material_ids=[material["id"]])[0]
    question = store.add_message("user", "什么时候发布？")
    answer = store.add_message("assistant", "十月八日。", "assistant", metadata={"source_ids": [question["id"], chunk["id"]]})
    sources = {"S1": {"kind": "chunk", "id": chunk["id"], "label": "plan.md", "excerpt": chunk["text"]}}
    artifact = store.create_artifact("minutes", "纪要", "- 发布日期是十月八日 [S1]\n", sources, "task")
    assert artifact["citations"][0]["valid"] and artifact["status"] == "active"
    write(path, "# 发布\n发布日期改为十月十日。\n")
    store.rescan_materials()
    assert store.record(answer["id"]) is None, "answers derived from an old version follow the deletion cascade"
    assert store.record(question["id"]), "the user's own question is not derived from the material"
    stale = store.artifact(artifact["id"])
    assert stale["status"] == "stale" and not stale["citations"][0]["valid"] and stale["citations"][0]["excerpt"] is None
    assert "发布日期是十月八日" in stale["content"], "the user's artifact text is kept"
    assert "来源已删除或已变更" in store.export_markdown(artifact["id"])

    fresh = store.material_chunks(material_ids=[material["id"]])[0]
    later = store.create_artifact("document", "文档", "十月十日发布 [S1]\n",
                                  {"S1": {"kind": "chunk", "id": fresh["id"], "label": "plan.md", "excerpt": fresh["text"]}})
    store.clear_history()  # history is not material: the artifact keeps its material link
    store.delete_material(material["id"])
    assert store.artifact(later["id"])["status"] == "stale"
    assert store.materials() == [] and store.search_chunks("十月十日") == []


def test_materials_join_the_chat_context_with_locators(tmp_path):
    store = MemoryStore(tmp_path / "data")
    store.import_materials(str(write(tmp_path / "spec.md", "# 需求\n## 登录\n登录需要短信验证码。\n")))
    session = store.new_session()
    context = store.context("登录需要什么验证", session, 4000)
    assert context["materials"][0]["role"] == "material"
    assert "spec.md › 需求 / 登录" in context["materials"][0]["source"]
    reply = store.add_message("assistant", "需要短信验证码。", "assistant", session,
                              metadata={"source_ids": [context["materials"][0]["id"]]})
    assert store.record(reply["id"])
