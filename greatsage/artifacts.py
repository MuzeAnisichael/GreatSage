"""Reviewable Markdown artifacts whose citations visibly fail when sources change.

Artifacts are user work products: deleting or revising a cited source keeps the
artifact text but invalidates the citation, drops its stored excerpt and marks
the artifact stale. Only an explicit artifact deletion removes the text.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

MARKER = re.compile(r"\[S(\d{1,4})\]")
KINDS = ("minutes", "todos", "document")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def markers(text: str) -> list[str]:
    return list(dict.fromkeys(f"S{number}" for number in MARKER.findall(text or "")))


def todo_line(todo: dict) -> str:
    owner = todo["owner"] or "未确认"
    due = todo["due"] or "未确认"
    cited = "".join(f"[{marker}]" for marker in todo.get("markers", []))
    return f"- [{'x' if todo['done'] else ' '}] {todo['text']}（负责人：{owner}；期限：{due}）{cited}"


class ArtifactsMixin:
    def _init_artifacts(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, current_version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifact_versions (
                id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL,
                origin TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(artifact_id, version));
            CREATE TABLE IF NOT EXISTS citations (
                version_id TEXT NOT NULL, marker TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
                label TEXT NOT NULL, excerpt TEXT, valid INTEGER NOT NULL, PRIMARY KEY(version_id, marker));
            CREATE INDEX IF NOT EXISTS citation_source ON citations(source_id);
            CREATE TABLE IF NOT EXISTS todos (
                id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL,
                owner TEXT NOT NULL, due TEXT NOT NULL, done INTEGER NOT NULL, markers TEXT NOT NULL,
                edited INTEGER NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS todo_artifact ON todos(artifact_id, ordinal);
            CREATE TABLE IF NOT EXISTS artifact_revision_links (
                version_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, version INTEGER NOT NULL,
                generating_task_id TEXT NOT NULL, parent_version INTEGER, correction_source_id TEXT NOT NULL,
                UNIQUE(artifact_id, version));
            CREATE UNIQUE INDEX IF NOT EXISTS artifact_revision_task
                ON artifact_revision_links(artifact_id, generating_task_id) WHERE generating_task_id != '';
            INSERT OR IGNORE INTO artifact_revision_links
                SELECT v.id,v.artifact_id,v.version,CASE WHEN v.version=1 THEN a.task_id ELSE '' END,
                    CASE WHEN v.version>1 THEN v.version-1 ELSE NULL END,''
                FROM artifact_versions v JOIN artifacts a ON a.id=v.artifact_id;
        """)

    def _source_exists(self, kind: str, id: str) -> bool:
        table = {"message": "messages", "chunk": "material_chunks"}.get(kind)
        return bool(table and self._db.execute(f"SELECT 1 FROM {table} WHERE id=?", (id,)).fetchone())

    def _insert_version(self, artifact_id: str, number: int, content: str, origin: str, note: str,
                        sources: dict, used: set[str] | None = None, *, task_id: str = "",
                        correction_source_id: str = "") -> str:
        version_id = uuid.uuid4().hex
        self._db.execute("INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?)",
                         (version_id, artifact_id, number, content, origin, note[:2000], _now()))
        self._db.execute("INSERT INTO artifact_revision_links VALUES (?,?,?,?,?,?)",
                         (version_id, artifact_id, number, task_id, number - 1 if number > 1 else None,
                          correction_source_id))
        for marker in sorted(set(markers(content)) | set(used or ()), key=lambda item: int(item[1:])):
            source = sources.get(marker)
            if not source:
                continue
            valid = self._source_exists(source["kind"], source["id"]) and source.get("valid", True)
            self._db.execute("INSERT INTO citations VALUES (?,?,?,?,?,?,?)",
                             (version_id, marker, source["kind"], source["id"], str(source.get("label", ""))[:500],
                              str(source.get("excerpt", ""))[:600] if valid else None, int(valid)))
            if valid:
                self._db.execute("INSERT OR IGNORE INTO dependencies VALUES ('artifact',?,?)", (artifact_id, source["id"]))
        return version_id

    def create_artifact(self, kind: str, title: str, content: str, sources: dict, task_id: str = "",
                        todos: list[dict] | None = None, origin: str = "generated", note: str = "") -> dict:
        if kind not in KINDS:
            raise ValueError("Unsupported artifact kind")
        stamp, id = _now(), uuid.uuid4().hex
        with self._lock, self._db:
            self._db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)",
                             (id, task_id, kind, title[:200], "active", 1, stamp, stamp))
            used = {marker for todo in todos or [] for marker in todo.get("markers", [])}
            version_id = self._insert_version(id, 1, content, origin, note, sources, used, task_id=task_id)
            self._insert_todos(id, todos or [])
            self._mark_stale_if_invalid(id, version_id)
        return self.artifact(id)

    def _insert_todos(self, artifact_id: str, todos: list[dict]) -> None:
        stamp = _now()
        for ordinal, todo in enumerate(todos):
            self._db.execute("INSERT INTO todos VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (uuid.uuid4().hex, artifact_id, ordinal, todo["text"][:500], todo.get("owner", "")[:120],
                              todo.get("due", "")[:120], int(bool(todo.get("done"))),
                              json.dumps(todo.get("markers", [])), 0, stamp))

    def _mark_stale_if_invalid(self, artifact_id: str, version_id: str) -> None:
        if self._db.execute("SELECT 1 FROM citations WHERE version_id=? AND valid=0", (version_id,)).fetchone():
            self._db.execute("UPDATE artifacts SET status='stale' WHERE id=?", (artifact_id,))

    def replace_todos(self, id: str, todos: list[dict], content: str, sources: dict, note: str = "") -> dict:
        """Regeneration replaces the checklist; the previous list stays in version history."""
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM artifacts WHERE id=? AND kind='todos'", (id,)).fetchone()
            if not row:
                raise KeyError(id)
            number = row["current_version"] + 1
            used = {marker for todo in todos for marker in todo.get("markers", [])}
            version_id = self._insert_version(id, number, content, "regenerated", note, sources, used)
            self._db.execute("DELETE FROM todos WHERE artifact_id=?", (id,))
            self._insert_todos(id, todos)
            self._db.execute("UPDATE artifacts SET current_version=?,status='active',updated_at=? WHERE id=?", (number, _now(), id))
            self._mark_stale_if_invalid(id, version_id)
        return self.artifact(id)

    def _citation_sources(self, version_id: str) -> dict:
        return {row["marker"]: {"kind": row["source_kind"], "id": row["source_id"], "label": row["label"],
                                "excerpt": row["excerpt"] or "", "valid": bool(row["valid"])}
                for row in self._db.execute("SELECT * FROM citations WHERE version_id=?", (version_id,))}

    def add_artifact_version(self, id: str, content: str, origin: str = "edited", note: str = "",
                             sources: dict | None = None, expected_version: int | None = None) -> dict:
        """Edits keep the previous citation map; regeneration supplies a new one."""
        if not isinstance(content, str) or not content.strip() or len(content) > 400_000:
            raise ValueError("产物内容需要 1–400000 个字符。")
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM artifacts WHERE id=?", (id,)).fetchone()
            if not row:
                raise KeyError(id)
            if expected_version is not None and row["current_version"] != expected_version:
                raise ValueError("文档已有新版本，请刷新后再编辑。")
            previous = self._db.execute("SELECT id FROM artifact_versions WHERE artifact_id=? AND version=?",
                                        (id, row["current_version"])).fetchone()
            if sources is None:
                sources = self._citation_sources(previous["id"]) if previous else {}
            number = row["current_version"] + 1
            self._insert_version(id, number, content, origin, note, sources)
            invalid = self._db.execute("""SELECT count(*) FROM citations c JOIN artifact_versions v ON v.id=c.version_id
                                          WHERE v.artifact_id=? AND v.version=? AND c.valid=0""", (id, number)).fetchone()[0]
            self._db.execute("UPDATE artifacts SET current_version=?,status=?,updated_at=? WHERE id=?",
                             (number, "stale" if invalid else "active", _now(), id))
        return self.artifact(id)

    def commit_revision(self, artifact_id: str, expected_version: int, task_id: str, content: str,
                        sources: dict, correction_source_id: str = "", note: str = "") -> dict:
        """Commit one research document revision atomically; a task can commit only once.

        Retrying a committed task returns its own historical version, even if a
        later task or manual edit has advanced current_version. Corrections are
        source IDs only; the caller owns their creation and execution identity.
        """
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version 必须是正整数。")
        if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 128:
            raise ValueError("修订必须关联一个有效任务 ID。")
        if not isinstance(correction_source_id, str) or len(correction_source_id) > 128:
            raise ValueError("纠正来源 ID 无效。")
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if not row:
                raise KeyError(artifact_id)
            if row["kind"] != "document":
                raise ValueError("研究修订只支持文档产物。")
            existing = self._db.execute("""SELECT version FROM artifact_revision_links
                WHERE artifact_id=? AND generating_task_id=?""", (artifact_id, task_id)).fetchone()
            if existing:
                return self.artifact(artifact_id, existing["version"])
            if row["current_version"] != expected_version:
                raise ValueError("文档版本已改变，请重新审阅后发起修订。")
            if not isinstance(content, str) or not content.strip() or len(content) > 400_000:
                raise ValueError("产物内容需要 1–400000 个字符。")
            if not isinstance(sources, dict) or not isinstance(note, str):
                raise ValueError("修订来源或说明无效。")
            if correction_source_id and not self._db.execute(
                    "SELECT 1 FROM messages WHERE id=? AND role='user'", (correction_source_id,)).fetchone():
                raise ValueError("用户纠正来源不存在或已删除。")
            for marker in markers(content):
                source = sources.get(marker)
                if (not isinstance(source, dict) or not source.get("valid", True)
                        or not self._source_exists(source.get("kind"), source.get("id"))):
                    raise ValueError("修订引用的来源不存在或已失效。")
            number = expected_version + 1
            self._insert_version(artifact_id, number, content, "regenerated", note, sources,
                                 task_id=task_id, correction_source_id=correction_source_id)
            if correction_source_id:
                self._db.execute("INSERT OR IGNORE INTO dependencies VALUES ('artifact',?,?)",
                                 (artifact_id, correction_source_id))
            self._db.execute("UPDATE artifacts SET current_version=?,status='active',updated_at=? WHERE id=?",
                             (number, _now(), artifact_id))
            return self.artifact(artifact_id, number)

    def artifacts(self, task_id: str | None = None, session_id: str | None = None) -> list[dict]:
        clauses, args = [], []
        if task_id:
            clauses.append("""(a.task_id=? OR EXISTS (SELECT 1 FROM artifact_revision_links r
                WHERE r.artifact_id=a.id AND r.generating_task_id=?))""")
            args += [task_id, task_id]
        if session_id:
            clauses.append("""EXISTS (SELECT 1 FROM tasks t WHERE t.session_id=? AND
                (t.id=a.task_id OR EXISTS (SELECT 1 FROM artifact_revision_links r
                    WHERE r.artifact_id=a.id AND r.generating_task_id=t.id)))""")
            args.append(session_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = [dict(row) for row in self._db.execute("SELECT a.* FROM artifacts a" + where + " ORDER BY a.created_at DESC", args)]
            if task_id:
                for row in rows:
                    row["task_versions"] = [item[0] for item in self._db.execute("""SELECT version
                        FROM artifact_revision_links WHERE artifact_id=? AND generating_task_id=? ORDER BY version""",
                        (row["id"], task_id))]
            return rows

    def _version_lineage(self, artifact_id: str) -> list[dict]:
        rows = self._db.execute("""SELECT r.version,r.generating_task_id,r.parent_version,r.correction_source_id,
            CASE WHEN r.correction_source_id='' THEN NULL ELSE EXISTS
                (SELECT 1 FROM messages m WHERE m.id=r.correction_source_id AND m.role='user') END AS correction_source_valid
            FROM artifact_revision_links r WHERE r.artifact_id=? ORDER BY r.version""", (artifact_id,))
        return [dict(row) | {"correction_source_valid": None if row["correction_source_valid"] is None
                            else bool(row["correction_source_valid"])} for row in rows]

    def todos(self, artifact_id: str) -> list[dict]:
        with self._lock:
            return [{**dict(row), "done": bool(row["done"]), "edited": bool(row["edited"]), "markers": json.loads(row["markers"])}
                    for row in self._db.execute("SELECT * FROM todos WHERE artifact_id=? ORDER BY ordinal", (artifact_id,))]

    def artifact(self, id: str, version: int | None = None) -> dict:
        with self._lock:
            row = self._db.execute("""SELECT a.*, COALESCE(t.session_id, '') AS session_id,
                COALESCE(json_extract(t.params, '$.profile'), '') AS profile
                FROM artifacts a LEFT JOIN tasks t ON t.id=a.task_id WHERE a.id=?""", (id,)).fetchone()
            if not row:
                raise KeyError(id)
            number = version or row["current_version"]
            selected = self._db.execute("SELECT * FROM artifact_versions WHERE artifact_id=? AND version=?", (id, number)).fetchone()
            if not selected:
                raise KeyError(f"{id}@{number}")
            citations = [dict(item) | {"valid": bool(item["valid"])} for item in self._db.execute(
                "SELECT marker,source_kind,source_id,label,excerpt,valid FROM citations WHERE version_id=?", (selected["id"],))]
            citations.sort(key=lambda item: int(item["marker"][1:]))
            lineage = self._version_lineage(id)
            by_version = {item["version"]: item for item in lineage}
            result = {**dict(row), "version": number, "content": selected["content"], "origin": selected["origin"],
                      "note": selected["note"], "citations": citations, "lineage": lineage,
                      **by_version[number],
                      "versions": [dict(item) | by_version[item["version"]] for item in self._db.execute(
                          "SELECT version,origin,note,created_at FROM artifact_versions WHERE artifact_id=? ORDER BY version DESC", (id,))]}
        if row["kind"] == "todos":
            result["todos"] = self.todos(id)
            result["content"] = "\n".join([f"# {row['title']}", ""] + [todo_line(todo) for todo in result["todos"]]) + "\n"
        known = {item["marker"] for item in citations}
        used = set(markers(result["content"]))
        result["unknown_markers"] = sorted(used - known, key=lambda item: int(item[1:]))
        result["invalid_markers"] = [item["marker"] for item in citations if not item["valid"]]
        return result

    def artifact_review(self, id: str, version: int | None = None, compare_to: int | None = None) -> dict:
        """Return bounded text differences and source locators, never truth scores."""
        from .artifact_review import bounded_diff, review_passages

        if version is not None and (type(version) is not int or version < 1):
            raise ValueError("查看版本必须是正整数。")
        with self._lock:
            artifact = self.artifact(id, version)
            if compare_to is not None and (type(compare_to) is not int or compare_to < 1):
                raise ValueError("对比版本必须是正整数。")
            previous = compare_to if compare_to is not None else artifact["parent_version"]
            before = self.artifact(id, previous)["content"] if previous is not None else None
            citations = []
            for citation in artifact["citations"]:
                item = dict(citation)
                locator = None
                if item["valid"] and item["source_kind"] == "chunk":
                    source = self._db.execute("""SELECT c.material_id,c.version,c.heading,c.line_start,c.line_end,m.name
                        FROM material_chunks c JOIN materials m ON m.id=c.material_id WHERE c.id=?""",
                        (item["source_id"],)).fetchone()
                    if source:
                        locator = {"kind": "chunk", "source_id": item["source_id"], **dict(source)}
                elif item["valid"] and item["source_kind"] == "message":
                    source = self._db.execute("SELECT session_id,created_at FROM messages WHERE id=?", (item["source_id"],)).fetchone()
                    if source:
                        locator = {"kind": "message", "source_id": item["source_id"], **dict(source)}
                item["valid"] = item["valid"] and locator is not None
                item["locator"] = locator
                if not item["valid"]:
                    item["excerpt"] = None
                citations.append(item)
        passages, truncated = review_passages(artifact["content"], citations)
        return {"artifact_id": id, **{key: artifact[key] for key in (
                    "version", "current_version", "generating_task_id", "parent_version", "correction_source_id",
                    "correction_source_valid", "lineage", "unknown_markers")},
                "citations": citations, "passages": passages, "passages_truncated": truncated,
                "invalid_markers": [item["marker"] for item in citations if not item["valid"]],
                "diff": bounded_diff(before, artifact["content"], previous, artifact["version"]) if before is not None else None,
                "semantic_verification": False}

    def update_todo(self, todo_id: str, fields: dict) -> dict:
        allowed = {"text": 500, "owner": 120, "due": 120}
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
            if not row:
                raise KeyError(todo_id)
            values = dict(row)
            for key, value in fields.items():
                if key == "done":
                    if type(value) is not bool:
                        raise ValueError("done 必须是布尔值。")
                    values["done"] = int(value)
                elif key in allowed:
                    if not isinstance(value, str) or len(value) > allowed[key] or (key == "text" and not value.strip()):
                        raise ValueError(f"{key} 内容无效。")
                    values[key] = value.strip()
                else:
                    raise ValueError("只能修改待办内容、负责人、期限和完成状态。")
            self._db.execute("UPDATE todos SET text=?,owner=?,due=?,done=?,edited=1,updated_at=? WHERE id=?",
                             (values["text"], values["owner"], values["due"], values["done"], _now(), todo_id))
            self._db.execute("UPDATE artifacts SET updated_at=? WHERE id=?", (_now(), row["artifact_id"]))
        return next(todo for todo in self.todos(row["artifact_id"]) if todo["id"] == todo_id)

    def export_markdown(self, id: str, version: int | None = None) -> str:
        artifact = self.artifact(id, version)
        used = set(markers(artifact["content"]))
        lines = [artifact["content"].rstrip(), ""]
        cited = [item for item in artifact["citations"] if item["marker"] in used]
        if cited or artifact["unknown_markers"]:
            lines += ["---", "", "## 来源", ""]
            for item in cited:
                if item["valid"]:
                    excerpt = " ".join((item["excerpt"] or "").split())
                    lines.append(f"- [{item['marker']}] {item['label']}" + (f" —— “{excerpt[:160]}”" if excerpt else ""))
                else:
                    lines.append(f"- [{item['marker']}] {item['label']}（来源已删除或已变更）")
            lines += [f"- [{marker}]（没有对应来源）" for marker in artifact["unknown_markers"]]
            lines.append("")
        return "\n".join(lines)

    def delete_artifact(self, id: str) -> None:
        with self._lock:
            with self._db:
                if not self._db.execute("SELECT 1 FROM artifacts WHERE id=?", (id,)).fetchone():
                    raise KeyError(id)
                self._db.execute("DELETE FROM citations WHERE version_id IN (SELECT id FROM artifact_versions WHERE artifact_id=?)", (id,))
                for table in ("artifact_revision_links", "artifact_versions", "todos"):
                    self._db.execute(f"DELETE FROM {table} WHERE artifact_id=?", (id,))
                self._db.execute("DELETE FROM dependencies WHERE owner_type='artifact' AND owner_id=?", (id,))
                self._db.execute("DELETE FROM artifacts WHERE id=?", (id,))
            self._checkpoint()

    def _stale_artifact(self, artifact_id: str, source_ids: set[str]) -> None:
        marks = ",".join("?" * len(source_ids))
        self._db.execute(f"""UPDATE citations SET valid=0,excerpt=NULL WHERE source_id IN ({marks})
                             AND version_id IN (SELECT id FROM artifact_versions WHERE artifact_id=?)""",
                         [*source_ids, artifact_id])
        self._db.execute("UPDATE artifacts SET status='stale',updated_at=? WHERE id=?", (_now(), artifact_id))

    def _stale_message_citations(self) -> None:
        """History was cleared: every conversation citation loses its excerpt."""
        affected = [row[0] for row in self._db.execute("""SELECT DISTINCT v.artifact_id FROM citations c
                    JOIN artifact_versions v ON v.id=c.version_id WHERE c.source_kind='message'""")]
        affected.extend(row[0] for row in self._db.execute(
            "SELECT DISTINCT artifact_id FROM artifact_revision_links WHERE correction_source_id!=''"))
        self._db.execute("UPDATE citations SET valid=0,excerpt=NULL WHERE source_kind='message'")
        self._db.executemany("UPDATE artifacts SET status='stale',updated_at=? WHERE id=?", [(_now(), id) for id in affected])
        self._db.execute("DELETE FROM dependencies WHERE owner_type='artifact' AND source_id NOT IN (SELECT id FROM material_chunks)")
