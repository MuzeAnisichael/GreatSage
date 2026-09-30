"""Controlled tools with frontier-agent style permissions.

Every tool declares an effect class. In-app work and reads run directly;
writes, launches and deletions ask the user each time; commands stay disabled
until enabled in settings and then ask every time. Rules match a call's target
with wildcards and resolve deny > ask > allow; deletions and commands never get
a standing allow. Only tasks started by the user's own text, voice or console
action may call tools: observed audio, materials, Skills and tool results are
data and cannot start or approve anything. File tools stay inside the
workspace, and writes and deletions keep what they need to be undone.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

EFFECTS = ("internal", "read", "write", "launch", "destructive", "execute")
EFFECT_LABELS = {"internal": "应用内", "read": "只读", "write": "写入", "launch": "打开",
                 "destructive": "删除", "execute": "命令"}
DEFAULT_ACTIONS = {"internal": "allow", "read": "allow", "write": "ask", "launch": "ask",
                   "destructive": "ask", "execute": "ask"}
NO_STANDING_ALLOW = {"destructive", "execute"}
VOICE_APPROVABLE = {"write", "launch"}
TRUSTED_ORIGINS = {"user_text", "user_voice", "console"}
TEXT_SUFFIXES = {".md", ".txt"}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_APPROVE = {"确认执行", "確認執行", "同意执行", "同意執行", "批准执行", "批准執行", "确认操作", "確認操作"}
_DENY = {"取消执行", "取消執行", "拒绝执行", "拒絕執行", "不要执行", "不要執行", "取消操作"}


class ToolError(ValueError):
    """A safe, user-facing tool failure."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    effect: str
    description: str
    parameters: dict


def _object(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_STR, _BOOL = {"type": "string"}, {"type": "boolean"}
SPECS = {spec.name: spec for spec in (
    ToolSpec("create_minutes_task", "整理纪要", "internal",
             "Start a background task that writes cited meeting minutes, editable todos and optionally a document "
             "draft from the current conversation and imported Markdown materials.",
             _object({"title": _STR, "use_session": _BOOL, "material_query": _STR,
                      "material_ids": {"type": "array", "items": _STR}, "document": _BOOL, "instructions": _STR})),
    ToolSpec("search_materials", "检索资料", "read", "Search imported Markdown materials and return cited excerpts.",
             _object({"query": _STR, "limit": {"type": "integer"}}, ["query"])),
    ToolSpec("list_files", "列出文件", "read", "List a folder inside the workspace. Paths are relative to the workspace.",
             _object({"path": _STR})),
    ToolSpec("read_file", "读取文件", "read", "Read a .md or .txt file inside the workspace.", _object({"path": _STR}, ["path"])),
    ToolSpec("write_file", "写入文件", "write", "Create, or with overwrite=true replace, a .md or .txt file inside the workspace.",
             _object({"path": _STR, "content": _STR, "overwrite": _BOOL}, ["path", "content"])),
    ToolSpec("export_artifact", "导出产物", "write", "Save a GreatSage artifact as a Markdown file inside the workspace.",
             _object({"artifact_id": _STR, "path": _STR, "overwrite": _BOOL}, ["artifact_id", "path"])),
    ToolSpec("open_path", "打开文件或文件夹", "launch", "Open a workspace file or folder with its default application.",
             _object({"path": _STR}, ["path"])),
    ToolSpec("open_url", "打开网址", "launch", "Open an http(s) URL in the default browser.", _object({"url": _STR}, ["url"])),
    ToolSpec("launch_app", "启动程序", "launch", "Start a program the user registered in settings, by its registered name.",
             _object({"name": _STR}, ["name"])),
    ToolSpec("delete_file", "移入回收区", "destructive", "Move a workspace file into GreatSage's recoverable trash.",
             _object({"path": _STR}, ["path"])),
    ToolSpec("run_command", "执行命令", "execute", "Run a PowerShell command in the workspace folder and return its output.",
             _object({"command": _STR}, ["command"])),
)}
TOOL_EFFECTS = {name: spec.effect for name, spec in SPECS.items()}
_FILE_TOOLS = {"list_files", "read_file", "write_file", "export_artifact", "open_path", "delete_file"}


def validate_arguments(spec: ToolSpec, raw) -> dict:
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        raise ToolError("工具参数不是有效的 JSON。") from None
    arguments = {} if arguments is None else arguments
    if not isinstance(arguments, dict):
        raise ToolError("工具参数必须是对象。")
    properties = spec.parameters["properties"]
    if set(arguments) - set(properties):
        raise ToolError("工具参数包含未知字段：" + "、".join(sorted(set(arguments) - set(properties))))
    for name in spec.parameters["required"]:
        if name not in arguments:
            raise ToolError(f"缺少参数：{name}")
    for name, value in arguments.items():
        kind = properties[name]["type"]
        valid = {"string": isinstance(value, str), "boolean": type(value) is bool, "integer": type(value) is int,
                 "array": isinstance(value, list) and len(value) <= 50 and all(isinstance(item, str) for item in value)}[kind]
        limit = 400_000 if name == "content" else 4000
        if not valid or (isinstance(value, str) and (len(value) > limit or "\x00" in value)):
            raise ToolError(f"参数 {name} 无效。")
    return arguments


def validate_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid or len(value) > 2048 or any(ord(character) < 33 for character in value.strip()):
        raise ToolError("只能打开不含账号信息的 http(s) 网址。")
    return value.strip()


def voice_intent(text: str) -> str | None:
    normalized = re.sub(r"[\W_]+", "", text or "")
    for prefix in ("大贤者", "大賢者", "请", "請"):
        normalized = normalized.removeprefix(prefix)
    return "approve" if normalized in _APPROVE else "deny" if normalized in _DENY else None


def decide(spec: ToolSpec, target: str, origin: str, settings: dict, granted: set[str] = frozenset()) -> tuple[str, str]:
    """Return allow/ask/deny with a reason. Pure: no I/O, no side effects."""
    if origin not in TRUSTED_ORIGINS:
        return "deny", "只有你的文字、语音或控制台操作才能发起工具调用。"
    if not settings.get("tools_enabled", True):
        return "deny", "工具已在设置中关闭。"
    if spec.effect == "execute" and not settings.get("command_tool"):
        return "deny", "命令工具未启用。"
    matched = {rule["action"] for rule in settings.get("tool_rules", [])
               if rule.get("tool") in (spec.name, "*")
               and fnmatch.fnmatchcase(target.casefold(), (rule.get("match") or "*").casefold())}
    if "deny" in matched:
        return "deny", "匹配“拒绝”规则。"
    if "ask" in matched:
        return "ask", "匹配“询问”规则。"
    if spec.effect not in NO_STANDING_ALLOW:
        if "allow" in matched:
            return "allow", "匹配“允许”规则。"
        if spec.name in granted:
            return "allow", "本任务内已允许。"
    action = DEFAULT_ACTIONS[spec.effect]
    return action, "默认允许：不改变电脑状态。" if action == "allow" else "默认需要你确认。"


def definitions(settings: dict) -> list[dict]:
    """Tool schemas offered to the model; unavailable tools are not offered at all."""
    hidden = set() if settings.get("command_tool") else {"run_command"}
    if not settings.get("app_launchers"):
        hidden.add("launch_app")
    return [{"type": "function", "function": {"name": spec.name, "description": spec.description,
                                              "parameters": spec.parameters}}
            for spec in SPECS.values() if spec.name not in hidden]


class Workspace:
    def __init__(self, data_dir: Path, extra: list[str] = ()):
        self.primary = Path(data_dir) / "workspace"
        self.roots = [self.primary, *(Path(item) for item in extra)]

    def ensure(self) -> Path:
        self.primary.mkdir(parents=True, exist_ok=True)
        return self.primary.resolve()

    @staticmethod
    def _check_names(parts) -> None:
        for part in parts:
            if part not in (".", "..") and (":" in part or part.split(".")[0].upper() in _RESERVED or part.endswith((" ", "."))):
                raise ToolError("路径包含 Windows 不允许的名称。")

    def resolve(self, value: str) -> Path:
        self.ensure()
        raw = (value or "").strip()
        candidate = Path(raw) if raw else Path(".")
        # Windows normalization silently rewrites device names and trailing dots, so check the input as written.
        self._check_names(candidate.parts[1:] if candidate.anchor else candidate.parts)
        if not candidate.is_absolute():
            candidate = self.primary / candidate
        # resolve() follows links, so a link inside the workspace cannot point outside it.
        resolved = candidate.resolve(strict=False)
        for root in self.roots:
            base = root.resolve(strict=False)
            if resolved == base or resolved.is_relative_to(base):
                self._check_names(resolved.relative_to(base).parts)
                return resolved
        raise ToolError("路径不在工作目录内。")

    def label(self, path: Path) -> str:
        for index, root in enumerate(self.roots):
            base = root.resolve(strict=False)
            if path == base or path.is_relative_to(base):
                relative = path.relative_to(base).as_posix()
                return relative if index == 0 else f"{base.as_posix()}/{relative}".removesuffix("/.")
        return path.as_posix()


def target_for(spec: ToolSpec, arguments: dict, workspace: Workspace) -> str:
    if spec.name in _FILE_TOOLS:
        return workspace.label(workspace.resolve(arguments.get("path", "")))
    if spec.name == "open_url":
        return validate_url(arguments["url"])
    if spec.name == "launch_app":
        return arguments["name"].strip()
    if spec.name == "run_command":
        return arguments["command"].strip()
    return ""


def _open(target: str) -> None:
    if sys.platform != "win32":
        raise ToolError("打开操作目前只支持 Windows。")
    os.startfile(target)  # noqa: S606 - the permission layer approved this exact target


def _launch(path: Path) -> None:
    subprocess.Popen([str(path)], cwd=str(path.parent), close_fds=True)  # noqa: S603


def _powershell(command: str, cwd: Path, timeout: float) -> dict:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + command],
        cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=flags)
        stdout, stderr = process.communicate()
    text = lambda data: data.decode("utf-8", errors="replace")[-20000:]
    return {"exit_code": process.returncode, "stdout": text(stdout), "stderr": text(stderr), "timed_out": timed_out}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ToolRunner:
    """Executes approved calls. Blocking handlers run in a worker thread."""

    def __init__(self, data_dir: Path, memory, settings):
        self.data_dir, self.memory, self.settings = Path(data_dir), memory, settings

    def workspace(self) -> Workspace:
        return Workspace(self.data_dir, self.settings().get("workspace_dirs", []))

    def target(self, spec: ToolSpec, arguments: dict) -> str:
        return target_for(spec, arguments, self.workspace())

    def run(self, call_id: str, spec: ToolSpec, arguments: dict) -> tuple[dict, dict | None]:
        return getattr(self, "_" + spec.name)(call_id, arguments)

    def _search_materials(self, call_id, arguments):
        rows = self.memory.search_chunks(arguments["query"], max(1, min(arguments.get("limit", 5), 10)))
        return {"results": [{"chunk_id": row["id"], "material_id": row["material_id"], "material": row["name"],
                             "heading": row["heading"], "lines": f"{row['line_start']}-{row['line_end']}",
                             "excerpt": row["text"][:600]} for row in rows]}, None

    def _list_files(self, call_id, arguments):
        workspace = self.workspace()
        folder = workspace.resolve(arguments.get("path", ""))
        if not folder.is_dir():
            raise ToolError("文件夹不存在。")
        children = sorted(folder.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        entries = [{"name": child.name, "type": "folder" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None} for child in children[:200]]
        return {"path": workspace.label(folder), "entries": entries, "truncated": len(children) > 200}, None

    def _read_file(self, call_id, arguments):
        workspace = self.workspace()
        path = workspace.resolve(arguments["path"])
        if not path.is_file() or path.suffix.casefold() not in TEXT_SUFFIXES:
            raise ToolError("只能读取工作目录中已存在的 .md 或 .txt 文件。")
        if path.stat().st_size > 512 * 1024:
            raise ToolError("文件超过 512 KB。")
        text = path.read_bytes().decode("utf-8", errors="replace")
        return {"path": workspace.label(path), "content": text[:20000], "truncated": len(text) > 20000}, None

    def _write(self, call_id: str, path_value: str, content: str, overwrite: bool):
        workspace = self.workspace()
        path = workspace.resolve(path_value)
        if path.suffix.casefold() not in TEXT_SUFFIXES:
            raise ToolError("只能写入 .md 或 .txt 文件。")
        if path.is_dir():
            raise ToolError("目标是文件夹。")
        existed = path.exists()
        if existed and not overwrite:
            raise ToolError("文件已存在；需要覆盖时请明确指定 overwrite。")
        path.parent.mkdir(parents=True, exist_ok=True)
        undo = {"action": "remove", "path": str(path)}
        if existed:
            backup = self.data_dir / "tool-backups" / call_id / path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            undo = {"action": "restore", "path": str(path), "backup": str(backup)}
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        undo["sha256"] = _digest(path)
        return {"path": workspace.label(path), "bytes": len(content.encode("utf-8")), "overwritten": existed}, undo

    def _write_file(self, call_id, arguments):
        return self._write(call_id, arguments["path"], arguments["content"], arguments.get("overwrite", False))

    def _export_artifact(self, call_id, arguments):
        try:
            content = self.memory.export_markdown(arguments["artifact_id"])
        except KeyError:
            raise ToolError("产物不存在或已删除。") from None
        return self._write(call_id, arguments["path"], content, arguments.get("overwrite", False))

    def _open_path(self, call_id, arguments):
        workspace = self.workspace()
        path = workspace.resolve(arguments["path"])
        if not path.exists():
            raise ToolError("文件或文件夹不存在。")
        _open(str(path))
        return {"opened": workspace.label(path)}, None

    def _open_url(self, call_id, arguments):
        url = validate_url(arguments["url"])
        _open(url)
        return {"opened": url}, None

    def _launch_app(self, call_id, arguments):
        name = arguments["name"].strip().casefold()
        entry = next((item for item in self.settings().get("app_launchers", []) if item["name"].casefold() == name), None)
        if not entry:
            raise ToolError("设置中没有登记这个程序。")
        path = Path(entry["path"])
        if not path.is_file():
            raise ToolError("登记的程序路径不存在。")
        _launch(path)
        return {"started": entry["name"]}, None

    def _delete_file(self, call_id, arguments):
        workspace = self.workspace()
        path = workspace.resolve(arguments["path"])
        if not path.is_file():
            raise ToolError("只能把工作目录中的文件移入回收区。")
        trash = self.data_dir / "tool-trash" / call_id / path.name
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(trash))
        return {"moved": workspace.label(path)}, {"action": "restore_trash", "path": str(path), "trash": str(trash)}

    def _run_command(self, call_id, arguments):
        if not self.settings().get("command_tool"):
            raise ToolError("命令工具未启用。")
        return _powershell(arguments["command"], self.workspace().ensure(), 60), None

    def undo(self, undo: dict) -> dict:
        path = Path(undo["path"])
        if undo["action"] == "remove":
            if path.exists() and _digest(path) != undo.get("sha256"):
                raise ToolError("文件在写入后又被修改过，不能安全撤销。")
            path.unlink(missing_ok=True)
            return {"removed": path.name}
        if undo["action"] == "restore":
            if path.exists() and _digest(path) != undo.get("sha256"):
                raise ToolError("文件在写入后又被修改过，不能安全撤销。")
            shutil.copy2(undo["backup"], path)
            return {"restored": path.name}
        if undo["action"] == "restore_trash":
            if path.exists():
                raise ToolError("原位置已有同名文件，不能恢复。")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(undo["trash"], str(path))
            return {"restored": path.name}
        raise ToolError("这项操作不能撤销。")
