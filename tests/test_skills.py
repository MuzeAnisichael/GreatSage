import os

import pytest

from greatsage.skills import SkillsManager


def write_skill(path, text):
    path.mkdir(parents=True, exist_ok=True)
    skill = path / "SKILL.md"
    skill.write_text(text, encoding="utf-8")
    return skill


def test_import_existing_frontmatter_is_read_only_and_persistent(tmp_path):
    original = "---\nname: meeting-secretary\ndescription: 总结会议内容和后续事项\n---\n# 秘书\n保留时间和来源。\n"
    skill = write_skill(tmp_path / "original", original)
    manager = SkillsManager(tmp_path / "data")
    entry = manager.import_path(str(skill))[0]
    assert entry["enabled"] and entry["name"] == "meeting-secretary"
    assert skill.read_text(encoding="utf-8") == original
    assert manager.select("请总结会议内容")[0]["id"] == entry["id"]
    manager.set_enabled(entry["id"], False)
    restored = SkillsManager(tmp_path / "data")
    assert not restored.select("会议内容")
    assert not restored.list()[0]["enabled"]
    restored.remove(entry["id"])
    assert not restored.list() and skill.exists()


def test_plain_markdown_skill_and_container_import(tmp_path):
    root = tmp_path / "skills"
    write_skill(root / "translation", "# 翻译助手\n帮助翻译中英日文本。\n保持专有名词。")
    write_skill(root / "notes", "# 笔记助手\n帮助整理课堂笔记。")
    manager = SkillsManager(tmp_path / "data")
    assert len(manager.import_path(str(root))) == 2
    assert manager.select("翻译中英日文本")[0]["name"] == "翻译助手"
    assert manager.select("无关天气查询") == []


def test_reference_loading_rejects_traversal_scripts_and_is_budgeted(tmp_path):
    root = tmp_path / "skill"
    write_skill(root, "---\nname: research\ndescription: reference secret example research\nallowed-tools: [Bash]\n---\n"
                "Read [reference](reference.md), [secret](../secret.md), [example](example.py).\n")
    (root / "reference.md").write_text("文档参考资料" * 300, encoding="utf-8")
    (tmp_path / "secret.md").write_text("OUTSIDE_SECRET", encoding="utf-8")
    (root / "example.py").write_text("raise RuntimeError('SCRIPT_NOT_EXECUTED')", encoding="utf-8")
    manager = SkillsManager(tmp_path / "data")
    entry = manager.import_path(str(root))[0]
    assert entry["limitations"]
    selected = manager.select("research reference secret example", max_chars=350)
    assert [resource["path"] for resource in selected[0]["resources"]] == ["reference.md"]
    assert selected[0]["resources"][0]["truncated"]
    assert len(selected[0]["text"]) + sum(len(resource["text"]) for resource in selected[0]["resources"]) <= 350
    assert "OUTSIDE_SECRET" not in repr(selected)


def test_symlink_reference_cannot_escape_skill_root(tmp_path):
    root = tmp_path / "skill"
    write_skill(root, "# references\nreference documentation\n[reference](reference.md)")
    secret = tmp_path / "secret.md"
    secret.write_text("SENSITIVE", encoding="utf-8")
    try:
        os.symlink(secret, root / "reference.md")
    except OSError:
        pytest.skip("Windows symlink creation needs Developer Mode or privilege")
    manager = SkillsManager(tmp_path / "data")
    manager.import_path(str(root))
    assert manager.select("reference")[0]["resources"] == []


def test_versions_follow_source_edits_and_unavailable_skills_are_visible(tmp_path):
    skill = write_skill(tmp_path / "skill", "# meeting\nmeeting notes\nfirst instructions")
    manager = SkillsManager(tmp_path / "data")
    first = manager.import_path(str(skill))[0]
    skill.write_text("# meeting\nmeeting notes\nupdated instructions", encoding="utf-8")
    assert manager.list()[0]["version"] != first["version"]
    assert "updated instructions" in manager.select("meeting")[0]["text"]
    skill.unlink()
    assert not manager.list()[0]["available"]
    assert manager.select("meeting") == []


def test_skill_size_and_yaml_mapping_validation(tmp_path):
    manager = SkillsManager(tmp_path / "data")
    malformed = write_skill(tmp_path / "malformed", "---\n- not\n- a mapping\n---\nbody")
    with pytest.raises(ValueError, match="YAML mapping"):
        manager.import_path(str(malformed))
    oversized = write_skill(tmp_path / "oversized", "x" * (128 * 1024 + 1))
    with pytest.raises(ValueError, match="supported-size"):
        manager.import_path(str(oversized))
    assert not manager.list()
