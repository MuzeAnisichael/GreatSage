"""Read-only SKILL.md registration and bounded instruction/reference loading."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import unquote

import yaml

from .memory import _relevance, _terms


MAX_SKILL_BYTES = 128 * 1024
MAX_RESOURCE_BYTES = 64 * 1024
TEXT_SUFFIXES = {".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".csv"}


class SkillsManager:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.data_dir / "skills.json"
        self._lock = threading.RLock()
        self._registry: dict[str, dict] = {}
        if self._path.exists():
            saved = json.loads(self._path.read_text(encoding="utf-8"))
            self._registry = {item["id"]: item for item in saved}

    def _persist(self) -> None:
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(list(self._registry.values()), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self._path)

    @staticmethod
    def _read(path: Path, root: Path, max_bytes: int) -> tuple[str, bytes]:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve(strict=True)):
            raise ValueError("Reference leaves its skill directory")
        if not resolved.is_file() or resolved.stat().st_size > max_bytes:
            raise ValueError("Skill/reference is not a supported-size regular file")
        raw = resolved.read_bytes()
        if len(raw) > max_bytes:
            raise ValueError("Skill/reference grew beyond the size limit")
        return raw.decode("utf-8-sig"), raw

    @staticmethod
    def _parse(text: str, default_name: str) -> tuple[dict, str]:
        metadata: dict = {}
        body = text
        match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", text, re.S)
        if match:
            loaded = yaml.safe_load(match.group(1))
            if loaded is not None and not isinstance(loaded, dict):
                raise ValueError("SKILL.md frontmatter must be a YAML mapping")
            metadata = loaded or {}
            body = text[match.end():].strip()
        name = metadata.get("name")
        if not isinstance(name, str) or not name.strip():
            heading = re.search(r"^#\s+(.+)$", body, re.M)
            name = heading.group(1).strip() if heading else default_name
        description = metadata.get("description")
        if not isinstance(description, str):
            description = next((line.strip() for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")), "")
        return {**metadata, "name": name[:200], "description": description.strip()[:3000]}, body

    @staticmethod
    def _references(body: str) -> list[str]:
        refs = re.findall(r"\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)", body)
        paths = [left or right for left, right in refs]
        paths.extend(re.findall(r"`([^`\r\n]+\.(?:md|txt|rst|json|ya?ml|csv))`", body, re.I))
        return list(dict.fromkeys(unquote(path.split("#", 1)[0]) for path in paths if path and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", path)))[:64]

    def _load(self, registered: dict) -> tuple[dict, str, list[str]]:
        path, root = Path(registered["path"]), Path(registered["root"])
        text, raw = self._read(path, root, MAX_SKILL_BYTES)
        metadata, body = self._parse(text, root.name)
        limitations = []
        if metadata.get("allowed-tools") or metadata.get("tools") or re.search(r"\b(?:mcp__|functions\.|tools\.|subprocess|execute|bash|powershell)\b|执行脚本|调用工具|scripts[/\\]", body, re.I):
            limitations.append("此技能包含工具或脚本依赖；首版仅加载指令和文本资料，不执行工具或脚本。")
        dependencies = metadata.get("dependencies") or metadata.get("requirements")
        if dependencies:
            limitations.append("此技能声明了额外依赖；请确认相应能力可用。")
        item = {key: value for key, value in registered.items() if key != "root"}
        item.update(name=metadata["name"], description=metadata["description"],
                    version=hashlib.sha256(raw).hexdigest(), limitations=limitations, available=True)
        return item, body, self._references(body)

    def import_path(self, path: str) -> list[dict]:
        """Register one file, a skill directory, or a container (up to 200 skills)."""
        selected = Path(path).expanduser().resolve(strict=True)
        if selected.is_file():
            if selected.name.casefold() != "skill.md":
                raise ValueError("Select a SKILL.md file or a directory containing skills")
            candidates = [selected]
        elif (selected / "SKILL.md").is_file():
            candidates = [selected / "SKILL.md"]
        else:
            candidates = []
            for directory, dirs, files in os.walk(selected, followlinks=False):
                current = Path(directory)
                depth = len(current.relative_to(selected).parts)
                dirs[:] = [name for name in dirs if not name.startswith(".") and not (current / name).is_symlink()] if depth < 5 else []
                for filename in files:
                    if filename.casefold() == "skill.md":
                        candidates.append(current / filename)
                if len(candidates) > 200:
                    raise ValueError("Import is limited to 200 skills at a time")
        if not candidates:
            raise ValueError("No SKILL.md files found")
        with self._lock:
            pending, results = {}, []
            for candidate in candidates:
                candidate = candidate.resolve(strict=True)
                if not candidate.is_relative_to(selected if selected.is_dir() else selected.parent):
                    raise ValueError("Skill file leaves the selected directory")
                skill_id = hashlib.sha256(str(candidate).casefold().encode("utf-8")).hexdigest()[:24]
                registered = dict(id=skill_id, path=str(candidate), root=str(candidate.parent),
                                  enabled=self._registry.get(skill_id, {}).get("enabled", True))
                item, _, _ = self._load(registered)
                pending[skill_id] = registered
                results.append(item)
            self._registry.update(pending)
            self._persist()
            return results

    def list(self) -> list[dict]:
        with self._lock:
            result = []
            for registered in self._registry.values():
                try:
                    item, _, _ = self._load(registered)
                except (OSError, ValueError, UnicodeError, yaml.YAMLError) as error:
                    item = {key: value for key, value in registered.items() if key != "root"}
                    item.update(name=Path(item["path"]).parent.name, description="", version="",
                                available=False, limitations=[f"技能暂不可读取：{type(error).__name__}"])
                result.append(item)
            return sorted(result, key=lambda item: item["name"].casefold())

    def set_enabled(self, id: str, enabled: bool) -> dict:
        with self._lock:
            if id not in self._registry:
                raise KeyError(id)
            self._registry[id]["enabled"] = bool(enabled)
            self._persist()
            return next(item for item in self.list() if item["id"] == id)

    def select(self, query: str, max_chars: int = 5000) -> list[dict]:
        """Select matching skills; explicit $name/name references rank first.

        Only relevant, local text references are read, at most three per skill.
        `version` identifies SKILL.md bytes; resource versions identify the exact
        referenced content. All selected text is data under higher-level policy.
        """
        if not query.strip() or max_chars <= 0:
            return []
        with self._lock:
            candidates = []
            for registered in self._registry.values():
                if not registered["enabled"]:
                    continue
                try:
                    item, body, refs = self._load(registered)
                except (OSError, ValueError, UnicodeError, yaml.YAMLError):
                    continue
                score = _relevance(query, item["name"] + " " + item["description"])
                if item["name"].casefold() in query.casefold():
                    score += 100
                if score > 0:
                    candidates.append((score, item, body, refs, Path(registered["root"])))
            candidates.sort(key=lambda item: item[0], reverse=True)
            selected, remaining = [], int(max_chars)
            for _, item, body, refs, root in candidates[:3]:
                if remaining <= 0:
                    break
                text = body[:remaining]
                remaining -= len(text)
                resources = []
                ranked_refs = sorted(refs, key=lambda ref: _relevance(query, ref.replace("/", " ").replace("_", " ")), reverse=True)
                for ref in ranked_refs:
                    if remaining <= 0 or len(resources) >= 3:
                        break
                    resource_path = Path(ref)
                    if resource_path.is_absolute() or resource_path.suffix.casefold() not in TEXT_SUFFIXES:
                        continue
                    # References are read on demand when their path is relevant.
                    if not any(term in ref.casefold() for term in _terms(query)):
                        continue
                    try:
                        content, raw = self._read(root / resource_path, root, MAX_RESOURCE_BYTES)
                    except (OSError, ValueError, UnicodeError):
                        continue
                    chunk = content[:remaining]
                    resources.append({"path": resource_path.as_posix(), "text": chunk,
                                      "version": hashlib.sha256(raw).hexdigest(), "truncated": len(chunk) < len(content)})
                    remaining -= len(chunk)
                selected.append({**item, "text": text, "resources": resources, "truncated": len(text) < len(body)})
            return selected

    def remove(self, id: str) -> None:
        with self._lock:
            self._registry.pop(id, None)
            self._persist()
