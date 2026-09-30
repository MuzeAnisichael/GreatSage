"""Tool permission policy, workspace sandbox and reversible execution."""
import pytest

from greatsage import tools
from greatsage.memory import MemoryStore
from greatsage.settings import SettingsStore
from greatsage.tools import SPECS, ToolError, ToolRunner, Workspace, decide, validate_arguments, voice_intent


def settings(**overrides):
    return {"tools_enabled": True, "command_tool": False, "tool_rules": [], "workspace_dirs": [], "app_launchers": [],
            "voice_approval": False, **overrides}


def test_defaults_follow_effect_classes_and_untrusted_origins_are_denied():
    base = settings()
    assert decide(SPECS["read_file"], "a.md", "user_text", base)[0] == "allow"
    assert decide(SPECS["create_minutes_task"], "", "user_voice", base)[0] == "allow"
    for name in ("write_file", "open_url", "launch_app", "delete_file"):
        assert decide(SPECS[name], "x", "user_text", base)[0] == "ask"
    assert decide(SPECS["run_command"], "dir", "user_text", base) == ("deny", "命令工具未启用。")
    assert decide(SPECS["run_command"], "dir", "user_text", settings(command_tool=True))[0] == "ask"
    for origin in ("observation", "material", "tool_result", "system"):
        assert decide(SPECS["read_file"], "a.md", origin, base)[0] == "deny"
    assert decide(SPECS["read_file"], "a.md", "user_text", settings(tools_enabled=False))[0] == "deny"


def test_rules_resolve_deny_over_ask_over_allow_and_never_standing_allow_destruction():
    rules = [{"tool": "write_file", "match": "notes/*.md", "action": "allow"},
             {"tool": "write_file", "match": "notes/secret*", "action": "ask"},
             {"tool": "*", "match": "*.env*", "action": "deny"},
             {"tool": "delete_file", "match": "*", "action": "allow"}]
    config = settings(tool_rules=rules)
    assert decide(SPECS["write_file"], "Notes/A.MD", "user_text", config)[0] == "allow"
    assert decide(SPECS["write_file"], "notes/secret.md", "user_text", config)[0] == "ask"
    assert decide(SPECS["read_file"], "config/.env.local", "user_text", config)[0] == "deny"
    assert decide(SPECS["delete_file"], "a.md", "user_text", config)[0] == "ask"
    assert decide(SPECS["write_file"], "b.md", "user_text", settings(), {"write_file"})[0] == "allow"
    assert decide(SPECS["delete_file"], "b.md", "user_text", settings(), {"delete_file"})[0] == "ask"


def test_workspace_rejects_escapes_links_and_windows_device_names(tmp_path):
    extra = tmp_path / "docs"
    extra.mkdir()
    workspace = Workspace(tmp_path / "data", [str(extra)])
    assert workspace.label(workspace.resolve("sub/a.md")) == "sub/a.md"
    assert workspace.label(workspace.resolve(str(extra / "b.md"))).endswith("docs/b.md")
    for value in ("../escape.md", str(tmp_path / "outside.md"), "con.md", "a.md:stream", "trailing. "):
        with pytest.raises(ToolError):
            workspace.resolve(value)
    link = workspace.ensure() / "link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation needs extra Windows permissions")
    with pytest.raises(ToolError, match="不在工作目录内"):
        workspace.resolve("link/outside.md")


def test_arguments_are_schema_checked():
    spec = SPECS["write_file"]
    assert validate_arguments(spec, '{"path":"a.md","content":"x"}') == {"path": "a.md", "content": "x"}
    for raw in ("not json", "[]", '{"path":"a.md"}', '{"path":"a.md","content":1}',
                '{"path":"a.md","content":"x","mode":"append"}', {"path": "a" * 5000, "content": ""}):
        with pytest.raises(ToolError):
            validate_arguments(spec, raw)


def test_write_overwrite_delete_and_undo_are_reversible(tmp_path):
    memory = MemoryStore(tmp_path)
    runner = ToolRunner(tmp_path, memory, settings)
    result, undo = runner.run("c1", SPECS["write_file"], {"path": "notes/a.md", "content": "第一版"})
    target = tmp_path / "workspace" / "notes" / "a.md"
    assert result["overwritten"] is False and target.read_text(encoding="utf-8") == "第一版"
    with pytest.raises(ToolError, match="overwrite"):
        runner.run("c2", SPECS["write_file"], {"path": "notes/a.md", "content": "第二版"})
    _, restore = runner.run("c3", SPECS["write_file"], {"path": "notes/a.md", "content": "第二版", "overwrite": True})
    runner.undo(restore)
    assert target.read_text(encoding="utf-8") == "第一版"
    with pytest.raises(ToolError, match="只能写入"):
        runner.run("c4", SPECS["write_file"], {"path": "run.ps1", "content": "x"})
    target.write_text("用户后来改过", encoding="utf-8")
    with pytest.raises(ToolError, match="又被修改过"):
        runner.undo(undo)
    _, trash = runner.run("c5", SPECS["delete_file"], {"path": "notes/a.md"})
    assert not target.exists()
    runner.undo(trash)
    assert target.read_text(encoding="utf-8") == "用户后来改过"
    listing, _ = runner.run("c6", SPECS["list_files"], {"path": "notes"})
    assert listing["entries"] == [{"name": "a.md", "type": "file", "size": target.stat().st_size}]
    assert runner.run("c7", SPECS["read_file"], {"path": "notes/a.md"})[0]["content"] == "用户后来改过"


def test_launch_tools_only_open_validated_targets(tmp_path, monkeypatch):
    opened, started = [], []
    monkeypatch.setattr(tools, "_open", opened.append)
    monkeypatch.setattr(tools, "_launch", started.append)
    app = tmp_path / "editor.exe"
    app.write_bytes(b"")
    runner = ToolRunner(tmp_path, MemoryStore(tmp_path), lambda: settings(app_launchers=[{"name": "Editor", "path": str(app)}]))
    runner.run("u1", SPECS["open_url"], {"url": "https://example.com/docs"})
    with pytest.raises(ToolError):
        runner.run("u2", SPECS["open_url"], {"url": "file:///C:/Windows"})
    with pytest.raises(ToolError):
        runner.run("u3", SPECS["open_url"], {"url": "https://user:pass@example.com"})
    runner.run("a1", SPECS["launch_app"], {"name": "editor"})
    with pytest.raises(ToolError, match="没有登记"):
        runner.run("a2", SPECS["launch_app"], {"name": "cmd"})
    with pytest.raises(ToolError, match="不存在"):
        runner.run("p1", SPECS["open_path"], {"path": "missing.md"})
    assert opened == ["https://example.com/docs"] and started == [app]


def test_voice_phrases_must_be_explicit():
    assert voice_intent("确认执行。") == voice_intent("大贤者，确认执行") == "approve"
    assert voice_intent("取消执行") == "deny"
    for text in ("确认", "好的", "我确认执行这件事了吗", "执行吧"):
        assert voice_intent(text) is None


def test_settings_reject_unsafe_tool_configuration(tmp_path):
    store = SettingsStore(tmp_path)
    for patch in ({"tool_rules": [{"tool": "delete_file", "match": "*", "action": "allow"}]},
                  {"tool_rules": [{"tool": "*", "match": "*", "action": "allow"}]},
                  {"tool_rules": [{"tool": "format_disk", "match": "*", "action": "deny"}]},
                  {"workspace_dirs": ["relative/folder"]},
                  {"app_launchers": [{"name": "A", "path": "C:/a.exe"}, {"name": "a", "path": "C:/b.exe"}]},
                  {"voice_approval": "yes"}):
        with pytest.raises(ValueError):
            store.update(patch)
    updated = store.update({"tool_rules": [{"tool": "write_file", "match": "notes/*", "action": "allow"}],
                            "workspace_dirs": [str(tmp_path)], "voice_approval": True})
    assert updated["tool_rules"][0]["action"] == "allow" and updated["voice_approval"] is True
