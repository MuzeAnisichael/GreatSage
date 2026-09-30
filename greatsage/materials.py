"""User-selected Markdown materials: versioned chunks with stable locators.

Material text is reference data. It never becomes an instruction; every chunk
keeps its file, heading path, line range and content version so answers and
artifacts can cite it, and edits or deletions can invalidate those citations.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 500
TARGET_CHARS = 1200
HARD_CHARS = 2400
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def split_markdown(text: str) -> list[dict]:
    """Split at headings and paragraph breaks, keeping 1-based line ranges."""
    chunks: list[dict] = []
    headings: list[tuple[int, str]] = []
    lines: list[tuple[int, str]] = []
    fence = ""

    def flush() -> None:
        body = "\n".join(line for _, line in lines).strip()
        # A heading directly followed by a sub-heading is carried by the heading path.
        if body and not all(_HEADING.match(line) for _, line in lines if line.strip()):
            chunks.append({"heading": " / ".join(title for _, title in headings),
                           "line_start": lines[0][0], "line_end": lines[-1][0], "text": body})
        lines.clear()

    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker and (not fence or marker.group(1).startswith(fence)):
            fence = "" if fence else marker.group(1)
        heading = None if fence or marker else _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            headings[:] = [item for item in headings if item[0] < level] + [(level, heading.group(2).strip())]
            lines.append((number, line))
            continue
        if len(line) > HARD_CHARS:
            flush()
            for offset in range(0, len(line), HARD_CHARS):
                lines.append((number, line[offset:offset + HARD_CHARS]))
                flush()
            continue
        lines.append((number, line))
        size = sum(len(item) + 1 for _, item in lines)
        if (not fence and not stripped and size >= TARGET_CHARS) or size >= HARD_CHARS:
            flush()
    flush()
    return chunks


def collect_markdown(path: str) -> list[Path]:
    """Expand one .md file or a directory tree without following links."""
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise ValueError("请选择资料的完整路径。")
    selected = selected.resolve(strict=True)
    if selected.is_file():
        if selected.suffix.casefold() != ".md":
            raise ValueError("首批资料只支持 .md 文件。")
        return [selected]
    files: list[Path] = []
    for directory, dirs, names in os.walk(selected, followlinks=False):
        current = Path(directory)
        depth = len(current.relative_to(selected).parts)
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and not (current / name).is_symlink()) if depth < 6 else []
        for name in sorted(names):
            candidate = current / name
            if name.casefold().endswith(".md") and not candidate.is_symlink():
                files.append(candidate)
                if len(files) > MAX_FILES:
                    raise ValueError(f"一次最多导入 {MAX_FILES} 个 .md 文件。")
    if not files:
        raise ValueError("所选目录中没有 .md 文件。")
    return files


def read_markdown(path: Path) -> tuple[str, str]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} 不是普通文件，或超过 2 MB。")
    raw = resolved.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} 超过 2 MB。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{path.name} 不是 UTF-8 编码。") from None
    return text.replace("\r\n", "\n").replace("\r", "\n"), hashlib.sha256(raw).hexdigest()


def chunk_label(chunk: dict) -> str:
    place = f"{chunk['name']}" + (f" › {chunk['heading']}" if chunk.get("heading") else "")
    return f"{place} · 第 {chunk['line_start']}–{chunk['line_end']} 行 · v{chunk['version']}"


class MaterialsMixin:
    def _init_materials(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS materials (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, path_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                content_hash TEXT NOT NULL, version INTEGER NOT NULL, size INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL, status TEXT NOT NULL, imported_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS material_chunks (
                id TEXT PRIMARY KEY, material_id TEXT NOT NULL, version INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                heading TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, text TEXT NOT NULL,
                content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS chunk_material ON material_chunks(material_id, ordinal);
        """)
        if self._fts:
            self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(id UNINDEXED,text,tokenize='unicode61')")
            self._db.execute("DELETE FROM chunk_fts")
            self._db.execute("INSERT INTO chunk_fts(id,text) SELECT id,text FROM material_chunks")

    def import_materials(self, path: str) -> dict:
        files = collect_markdown(path)
        loaded, errors = [], []
        for file in files:
            try:
                loaded.append((file, *read_markdown(file)))
            except (OSError, ValueError) as error:
                errors.append({"path": str(file), "error": str(error) if isinstance(error, ValueError) else "无法读取文件。"})
        with self._lock:
            with self._db:
                results = [self._upsert_material(file, text, digest) for file, text, digest in loaded]
            if any(item["action"] == "updated" for item in results):
                self._checkpoint()
        return {"materials": results, "errors": errors}

    def rescan_materials(self) -> dict:
        """Refresh versions from disk; missing files keep their last imported snapshot."""
        with self._lock:
            rows = [dict(row) for row in self._db.execute("SELECT id,path,content_hash,status FROM materials")]
        changes, loaded = [], []
        for row in rows:
            try:
                loaded.append((Path(row["path"]), *read_markdown(Path(row["path"]))))
            except OSError:
                if row["status"] != "missing":
                    changes.append((row["id"], "missing"))
            except ValueError:
                changes.append((row["id"], "unreadable"))
        with self._lock:
            with self._db:
                for id, status in changes:
                    self._db.execute("UPDATE materials SET status=?,updated_at=? WHERE id=?", (status, _now(), id))
                results = [self._upsert_material(file, text, digest) for file, text, digest in loaded]
            if any(item["action"] == "updated" for item in results):
                self._checkpoint()
        return {"materials": results, "status_changes": [{"id": id, "status": status} for id, status in changes]}

    def _upsert_material(self, file: Path, text: str, digest: str) -> dict:
        key = str(file).casefold()
        row = self._db.execute("SELECT * FROM materials WHERE path_key=?", (key,)).fetchone()
        if row and row["content_hash"] == digest:
            if row["status"] != "active":
                self._db.execute("UPDATE materials SET status='active',updated_at=? WHERE id=?", (_now(), row["id"]))
            return {"id": row["id"], "name": row["name"], "version": row["version"], "action": "unchanged"}
        chunks = split_markdown(text)
        stamp = _now()
        if row:
            id, version = row["id"], row["version"] + 1
            self._drop_chunks(id)
            self._db.execute("UPDATE materials SET content_hash=?,version=?,size=?,chunk_count=?,status='active',updated_at=? WHERE id=?",
                             (digest, version, len(text.encode("utf-8")), len(chunks), stamp, id))
            action = "updated"
        else:
            id, version, action = uuid.uuid4().hex, 1, "imported"
            self._db.execute("INSERT INTO materials VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (id, str(file), key, file.name, digest, version, len(text.encode("utf-8")),
                              len(chunks), "active", stamp, stamp))
        for ordinal, chunk in enumerate(chunks):
            chunk_id = uuid.uuid4().hex
            self._db.execute("INSERT INTO material_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (chunk_id, id, version, ordinal, chunk["heading"], chunk["line_start"], chunk["line_end"],
                              chunk["text"], hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest(), stamp))
            if self._fts:
                self._db.execute("INSERT INTO chunk_fts(id,text) VALUES (?,?)", (chunk_id, chunk["text"]))
        return {"id": id, "name": file.name, "version": version, "chunks": len(chunks), "action": action}

    def _drop_chunks(self, material_id: str) -> set[str]:
        ids = {row[0] for row in self._db.execute("SELECT id FROM material_chunks WHERE material_id=?", (material_id,))}
        if not ids:
            return ids
        # Derived chat answers follow the normal deletion cascade; artifacts are marked stale.
        texts, traces = set(), set()
        removed = self._delete_derived(set(ids), texts, traces)
        self._redact_events(removed, texts, traces)
        self._db.executemany("DELETE FROM material_chunks WHERE id=?", [(id,) for id in ids])
        if self._fts:
            self._db.executemany("DELETE FROM chunk_fts WHERE id=?", [(id,) for id in ids])
        return ids

    def delete_material(self, id: str) -> None:
        with self._lock:
            with self._db:
                if not self._db.execute("SELECT 1 FROM materials WHERE id=?", (id,)).fetchone():
                    raise KeyError(id)
                self._drop_chunks(id)
                self._db.execute("DELETE FROM materials WHERE id=?", (id,))
            self._checkpoint()

    def materials(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT id,path,name,version,size,chunk_count,status,imported_at,updated_at FROM materials ORDER BY name COLLATE NOCASE")]

    def material(self, id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT id,path,name,version,size,chunk_count,status,imported_at,updated_at FROM materials WHERE id=?", (id,)).fetchone()
            if not row:
                raise KeyError(id)
            return {**dict(row), "chunks": self.material_chunks(material_ids=[id])}

    def material_chunks(self, material_ids: list[str] | None = None, ids: list[str] | None = None) -> list[dict]:
        clauses, args = [], []
        if material_ids is not None:
            clauses.append("c.material_id IN (%s)" % ",".join("?" * len(material_ids)) if material_ids else "0")
            args += material_ids
        if ids is not None:
            clauses.append("c.id IN (%s)" % ",".join("?" * len(ids)) if ids else "0")
            args += ids
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._db.execute("SELECT c.*,m.name,m.path FROM material_chunks c JOIN materials m ON m.id=c.material_id"
                                    + where + " ORDER BY m.name COLLATE NOCASE,c.ordinal", args).fetchall()
            return [{**dict(row), "kind": "chunk"} for row in rows]

    def search_chunks(self, query: str, limit: int = 8, material_ids: list[str] | None = None) -> list[dict]:
        from .memory import _relevance, _terms
        terms = _terms(query)
        if not terms or limit <= 0:
            return []
        with self._lock:
            ids: set[str] = set()
            if self._fts:
                fts_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                ids.update(row[0] for row in self._db.execute(
                    "SELECT id FROM chunk_fts WHERE chunk_fts MATCH ? ORDER BY bm25(chunk_fts) LIMIT 100", (fts_query,)))
            conditions = " OR ".join("instr(lower(text),?)>0" for _ in terms)
            ids.update(row[0] for row in self._db.execute(
                "SELECT id FROM material_chunks WHERE " + conditions + " LIMIT 300", terms))
        chunks = self.material_chunks(material_ids=material_ids, ids=sorted(ids))
        chunks.sort(key=lambda item: _relevance(query, item["heading"] + " " + item["text"]), reverse=True)
        return chunks[:max(0, min(int(limit), 50))]
