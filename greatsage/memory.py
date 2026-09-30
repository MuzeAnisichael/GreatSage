"""Persistent, source-aware memory. No model calls or background workers live here."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .semantic import VectorIndexMixin, fuse
from .audit import AuditMixin
from .artifacts import ArtifactsMixin
from .materials import MaterialsMixin, chunk_label
from .tasks import TaskStoreMixin


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _id() -> str:
    return uuid.uuid4().hex


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _terms(text: str) -> list[str]:
    """Unicode61 does not segment Chinese, so also use Chinese phrase/bigrams."""
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.casefold())
    terms: list[str] = []
    for word in words:
        terms.append(word)
        if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 2:
            terms.extend(word[i:i + 2] for i in range(len(word) - 1))
    return list(dict.fromkeys(terms))[:40]


def _relevance(query: str, text: str) -> float:
    folded = text.casefold()
    return sum((2 + len(term)) / math.sqrt(1 + len(folded) / 500)
               for term in _terms(query) if term in folded)


class MemoryStore(VectorIndexMixin, AuditMixin, MaterialsMixin, ArtifactsMixin, TaskStoreMixin):
    """One WAL database, serialized transactions, and immutable source IDs.

    A revision creates a new ID, invalidating all derived records first. This
    makes an in-flight summary referencing the old ID impossible to commit.
    Only explicit calls to add_memory create durable user memories; assistant
    messages and ambient transcripts are never promoted automatically.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.data_dir / "memory.sqlite3", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL,
                source TEXT NOT NULL, session_id TEXT NOT NULL,
                trace_id TEXT NOT NULL, created_at TEXT NOT NULL, metadata TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS message_session ON messages(session_id,created_at);
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, text TEXT NOT NULL, source_ids TEXT NOT NULL,
                created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                origin TEXT NOT NULL DEFAULT 'user_explicit', revision_of TEXT);
            CREATE TABLE IF NOT EXISTS summaries (
                id TEXT PRIMARY KEY, text TEXT NOT NULL, source_ids TEXT NOT NULL,
                created_at TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, session_id TEXT NOT NULL,
                source_chars INTEGER NOT NULL, summary_chars INTEGER NOT NULL,
                time_start TEXT NOT NULL, time_end TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dependencies (
                owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, source_id TEXT NOT NULL,
                PRIMARY KEY(owner_type,owner_id,source_id));
            CREATE INDEX IF NOT EXISTS dependency_source ON dependencies(source_id);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
                trace_id TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_time ON events(created_at);
            CREATE TABLE IF NOT EXISTS forgotten_memories (
                id TEXT PRIMARY KEY, source_ids TEXT NOT NULL, deleted_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_conflicts (
                candidate_id TEXT PRIMARY KEY, existing_ids TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        for table, field, declaration in (("memories", "status", "TEXT NOT NULL DEFAULT 'active'"),
                                           ("summaries", "level", "INTEGER NOT NULL DEFAULT 1")):
            if field not in {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {field} {declaration}")
        self._init_vectors()
        self._init_audit()
        self._fts = True
        try:
            self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(id UNINDEXED,text,tokenize='unicode61')")
        except sqlite3.OperationalError:
            self._fts = False
        if self._fts:
            # Rebuild from canonical rows, so even recovery cannot resurrect data.
            self._db.execute("DELETE FROM message_fts")
            self._db.execute("INSERT INTO message_fts(id,text) SELECT id,text FROM messages")
        self._init_materials()
        self._init_artifacts()
        self._init_tasks()
        self._db.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        for field in ("metadata", "source_ids", "data"):
            if field in value:
                value[field] = json.loads(value[field])
        return value

    def new_session(self) -> str:
        session_id = _id()
        with self._lock, self._db:
            self._db.execute("INSERT INTO sessions VALUES (?,?)", (session_id, _now()))
        return session_id

    def add_message(self, role: str, text: str, source: str = "text", session_id: str = "",
                    trace_id: str = "", metadata: dict | None = None) -> dict:
        if role not in {"user", "assistant", "system", "tool", "observation"}:
            raise ValueError("Unsupported message role")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Message text cannot be empty")
        with self._lock, self._db:
            return self._insert_message(role, text, source, session_id, trace_id, metadata)

    def _insert_message(self, role: str, text: str, source: str, session_id: str,
                        trace_id: str, metadata: dict | None) -> dict:
        stamp = _now()
        session_id = session_id or _id()
        self._db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?)", (session_id, stamp))
        row = dict(id=_id(), role=role, text=text, source=source, session_id=session_id,
                   trace_id=trace_id, created_at=stamp, metadata=dict(metadata or {}))
        source_ids = row["metadata"].get("source_ids", [])
        if not isinstance(source_ids, list) or any(not isinstance(sid, str) for sid in source_ids):
            raise ValueError("metadata.source_ids must be a list of source IDs")
        source_ids = list(dict.fromkeys(source_ids))
        self._validate_sources(source_ids, allow_memories=True)
        self._db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                         tuple(_json(value) if key == "metadata" else value for key, value in row.items()))
        if self._fts:
            self._db.execute("INSERT INTO message_fts(id,text) VALUES (?,?)", (row["id"], text))
        self._db.executemany("INSERT INTO dependencies VALUES ('message',?,?)", [(row["id"], sid) for sid in source_ids])
        return row

    def history(self, limit: int = 100, session_id: str | None = None) -> list[dict]:
        with self._lock:
            clause, args = (" WHERE session_id=?", [session_id]) if session_id is not None else ("", [])
            rows = self._db.execute("SELECT * FROM messages" + clause + " ORDER BY rowid DESC LIMIT ?",
                                    [*args, max(0, min(int(limit), 100000))]).fetchall()
            return [self._row(row) for row in reversed(rows)]

    def sessions(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("""
                SELECT s.id,s.created_at,count(m.id) AS message_count,
                       max(m.created_at) AS last_message_at
                FROM sessions s LEFT JOIN messages m ON m.session_id=s.id
                GROUP BY s.id ORDER BY coalesce(max(m.created_at),s.created_at) DESC
            """)]

    def search(self, query: str, limit: int = 8) -> list[dict]:
        terms = _terms(query)
        if not terms or limit <= 0:
            return []
        with self._lock:
            candidates: dict[str, dict] = {}
            # Citation IDs may refer to originals, summaries or explicit memory.
            # Resolve derived IDs back to source originals for audit lookup even
            # when those messages are older than the UI's recent-history page.
            cited_ids = re.findall(r"\b[0-9a-f]{32}\b", query.lower())
            for cited_id in cited_ids[:20]:
                ids = [cited_id]
                memory = self._db.execute("SELECT source_ids FROM memories WHERE id=?", (cited_id,)).fetchone()
                if memory:
                    ids = json.loads(memory["source_ids"])
                for source_id in self._leaf_sources(ids):
                    row = self._db.execute("SELECT * FROM messages WHERE id=?", (source_id,)).fetchone()
                    if row:
                        candidates[row["id"]] = self._row(row)
            if candidates:
                return sorted(candidates.values(), key=lambda item: item["created_at"])[:min(int(limit), 100)]
            if self._fts:
                fts_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                for row in self._db.execute("""SELECT m.* FROM message_fts f
                    JOIN messages m ON m.id=f.id WHERE message_fts MATCH ?
                    ORDER BY bm25(message_fts) LIMIT 100""", (fts_query,)):
                    candidates[row["id"]] = self._row(row)
            conditions = " OR ".join("instr(lower(text),?)>0" for _ in terms)
            for row in self._db.execute("SELECT * FROM messages WHERE " + conditions + " ORDER BY rowid DESC LIMIT 500", terms):
                candidates[row["id"]] = self._row(row)
            ranked = sorted(candidates.values(), key=lambda item: (_relevance(query, item["text"]), item["created_at"]), reverse=True)
            return ranked[:max(0, min(int(limit), 100))]

    def _validate_sources(self, source_ids: list[str], allow_memories: bool = False) -> list[dict]:
        sources = []
        for source_id in source_ids:
            source = self._row(self._db.execute("SELECT * FROM messages WHERE id=?", (source_id,)).fetchone())
            if source is None:
                source = self._row(self._db.execute("SELECT * FROM summaries WHERE id=?", (source_id,)).fetchone())
            if source is None and allow_memories:
                source = self._row(self._db.execute("SELECT * FROM memories WHERE id=?", (source_id,)).fetchone())
            if source is None:
                source = self._row(self._db.execute("SELECT * FROM material_chunks WHERE id=?", (source_id,)).fetchone())
            if source is None:
                raise ValueError(f"Source no longer exists or has been revised: {source_id}")
            sources.append(source)
        return sources

    def add_memory(self, text: str, source_ids: list[str] | None = None, conflicts: list[str] | None = None) -> dict:
        if not text.strip():
            raise ValueError("Memory text cannot be empty")
        source_ids = list(dict.fromkeys(source_ids or []))
        with self._lock, self._db:
            self._validate_sources(source_ids)
            conflicts = list(dict.fromkeys(conflicts or []))
            for id in conflicts:
                if not self._db.execute("SELECT 1 FROM memories WHERE id=? AND status='active'", (id,)).fetchone():
                    raise ValueError("Conflicting memory no longer exists")
            row = dict(id=_id(), text=text, source_ids=source_ids, created_at=_now(),
                       version=1, origin="user_explicit", revision_of=None, status="pending" if conflicts else "active")
            self._db.execute("INSERT INTO memories (id,text,source_ids,created_at,version,origin,revision_of,status) VALUES (?,?,?,?,?,?,?,?)",
                             (row["id"], text, _json(source_ids), row["created_at"], 1, "user_explicit", None, row["status"]))
            self._db.executemany("INSERT INTO dependencies VALUES ('memory',?,?)", [(row["id"], sid) for sid in source_ids])
            if conflicts:
                self._db.execute("INSERT INTO memory_conflicts VALUES (?,?,?,?)", (row["id"], _json(conflicts), "possible_contradiction", row["created_at"]))
            return row

    def list_memories(self) -> list[dict]:
        with self._lock:
            return [self._row(row) for row in self._db.execute("SELECT * FROM memories WHERE status='active' ORDER BY rowid DESC")]

    def memory_candidates(self, text: str, limit: int = 24) -> list[dict]:
        return sorted(self.list_memories(), key=lambda item: (_relevance(text, item["text"]), item["created_at"]), reverse=True)[:limit]

    def conflicts(self) -> list[dict]:
        with self._lock:
            result = []
            for row in self._db.execute("SELECT * FROM memory_conflicts ORDER BY created_at DESC"):
                candidate = self.record(row["candidate_id"])
                if candidate:
                    existing = [item for id in json.loads(row["existing_ids"]) if (item := self.record(id))]
                    result.append({"candidate": candidate, "existing": existing, "reason": row["reason"]})
            return result

    def resolve_conflict(self, candidate_id: str, resolution: str) -> dict:
        if resolution not in ("replace", "keep_existing", "keep_both"):
            raise ValueError("Invalid memory conflict resolution")
        with self._lock:
            with self._db:
                conflict = self._db.execute("SELECT * FROM memory_conflicts WHERE candidate_id=?", (candidate_id,)).fetchone()
                if not conflict or not self.record(candidate_id):
                    raise KeyError(candidate_id)
                if resolution == "keep_existing":
                    self._delete_record("memory", candidate_id)
                else:
                    if resolution == "replace":
                        for id in json.loads(conflict["existing_ids"]):
                            self._delete_record("memory", id)
                    if not self.record(candidate_id):
                        raise ValueError("Candidate depended on a replaced record")
                    self._db.execute("UPDATE memories SET status='active' WHERE id=?", (candidate_id,))
                self._db.execute("DELETE FROM memory_conflicts WHERE candidate_id=?", (candidate_id,))
            self._checkpoint()
            return {"candidate_id": candidate_id, "resolution": resolution}

    def record_details(self, id: str) -> dict:
        with self._lock:
            record = self.record(id)
            if not record:
                raise KeyError(id)
            sources = record.get("source_ids", record.get("metadata", {}).get("source_ids", []))
            return {"record": record, "sources": [item for id in sources if (item := self.record(id))],
                    "dependents": [dict(row) for row in self._db.execute("SELECT owner_type,owner_id FROM dependencies WHERE source_id=? LIMIT 100", (id,))]}

    def history_page(self, cursor: int | None = None, session_id: str | None = None, limit: int = 30) -> dict:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            clauses, args = [], []
            if cursor is not None:
                clauses.append("rowid < ?"); args.append(int(cursor))
            if session_id:
                clauses.append("session_id=?"); args.append(session_id)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = self._db.execute("SELECT rowid AS cursor,* FROM messages" + where + " ORDER BY rowid DESC LIMIT ?", [*args, limit + 1]).fetchall()
            items = [self._row(row) for row in rows[:limit]]
            next_cursor = items[-1]["cursor"] if len(rows) > limit else None
            for item in items:
                item.pop("cursor")
            return {"items": items, "next_cursor": next_cursor}

    def _redact_events(self, ids: set[str], texts: set[str], traces: set[str]) -> None:
        self._redact_snapshots(ids, texts, traces)
        # Audit data is normally references, but callers may have logged bodies.
        # Redact the whole event if any deleted source is present anywhere in it.
        needles = [_json(text)[1:-1] for text in texts if text]
        for row in self._db.execute("SELECT id,data,trace_id FROM events").fetchall():
            serialized = row["data"]
            # IDs are 32-hex tokens: one scan per event instead of one per deleted ID.
            if (row["trace_id"] in traces or ids.intersection(re.findall(r"[0-9a-f]{32}", serialized))
                    or any(needle in serialized for needle in needles)):
                self._db.execute("UPDATE events SET data=? WHERE id=?", (_json({"redacted": True, "reason": "source_deleted"}), row["id"]))

    def _delete_derived(self, source_ids: set[str], texts: set[str], traces: set[str]) -> set[str]:
        queue, all_ids = list(source_ids), set(source_ids)
        while queue:
            source = queue.pop()
            for dep in self._db.execute("SELECT owner_type,owner_id FROM dependencies WHERE source_id=?", (source,)).fetchall():
                owner = dep["owner_id"]
                if dep["owner_type"] == "artifact":
                    # Artifacts are user work products: keep the text, fail the citation visibly.
                    self._stale_artifact(owner, {source})
                    continue
                if owner in all_ids:
                    continue
                all_ids.add(owner)
                queue.append(owner)
                table = {"memory": "memories", "summary": "summaries", "message": "messages"}[dep["owner_type"]]
                row = self._db.execute(f"SELECT * FROM {table} WHERE id=?", (owner,)).fetchone()
                if row:
                    texts.add(row["text"])
                    if table == "messages" and row["trace_id"]:
                        traces.add(row["trace_id"])
                self._db.execute(f"DELETE FROM {table} WHERE id=?", (owner,))
                if table == "messages" and self._fts:
                    self._db.execute("DELETE FROM message_fts WHERE id=?", (owner,))
        for source_id in all_ids:
            self._db.execute("DELETE FROM dependencies WHERE source_id=? OR owner_id=?", (source_id, source_id))
        self._drop_vectors(all_ids)
        for id in all_ids:
            self._db.execute("DELETE FROM memory_conflicts WHERE candidate_id=?", (id,))
        return all_ids

    def _checkpoint(self) -> None:
        # A completed delete should not leave recoverable bodies in an old WAL.
        if self._fts:
            with self._db:
                self._db.execute("INSERT INTO message_fts(message_fts) VALUES ('optimize')")
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _delete_record(self, kind: str, id: str) -> None:
        table = {"memory": "memories", "message": "messages"}[kind]
        row = self._db.execute(f"SELECT * FROM {table} WHERE id=?", (id,)).fetchone()
        if row is None:
            return
        if kind == "memory":
            self._db.execute("INSERT OR REPLACE INTO forgotten_memories VALUES (?,?,?)", (id, row["source_ids"], _now()))
        texts = {row["text"]}
        traces = {row["trace_id"]} if kind == "message" and row["trace_id"] else set()
        ids = self._delete_derived({id}, texts, traces)
        self._redact_events(ids, texts, traces)
        self._db.execute(f"DELETE FROM {table} WHERE id=?", (id,))
        if kind == "message" and self._fts:
            self._db.execute("DELETE FROM message_fts WHERE id=?", (id,))

    def delete_memory(self, id: str) -> None:
        with self._lock:
            with self._db:
                self._delete_record("memory", id)
            self._checkpoint()

    def delete_message(self, id: str) -> None:
        with self._lock:
            with self._db:
                self._delete_record("message", id)
            self._checkpoint()

    def revise_message(self, id: str, text: str) -> dict:
        if not text.strip():
            raise ValueError("Message text cannot be empty")
        with self._lock:
            old = self._row(self._db.execute("SELECT * FROM messages WHERE id=?", (id,)).fetchone())
            if not old:
                raise KeyError(id)
            # Replacement and invalidation commit together, including on crash.
            # No old body is retained in a revision snapshot.
            metadata = {**old["metadata"], "revision_of": id, "version": old["metadata"].get("version", 1) + 1}
            with self._db:
                self._delete_record("message", id)
                row = self._insert_message(old["role"], text, old["source"], old["session_id"], old["trace_id"], metadata)
            self._checkpoint()
            return row

    def revise_memory(self, id: str, text: str) -> dict:
        if not text.strip():
            raise ValueError("Memory text cannot be empty")
        with self._lock:
            old = self._row(self._db.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone())
            if not old:
                raise KeyError(id)
            with self._db:
                self._validate_sources(old["source_ids"])
                self._delete_record("memory", id)
                row = dict(id=_id(), text=text, source_ids=old["source_ids"], created_at=_now(),
                           version=old["version"] + 1, origin="user_explicit", revision_of=id, status="active")
                self._db.execute("INSERT INTO memories (id,text,source_ids,created_at,version,origin,revision_of,status) VALUES (?,?,?,?,?,?,?,?)",
                                 (row["id"], text, _json(row["source_ids"]), row["created_at"], row["version"], row["origin"], id, "active"))
                self._db.executemany("INSERT INTO dependencies VALUES ('memory',?,?)", [(row["id"], sid) for sid in row["source_ids"]])
            self._checkpoint()
            return row

    def clear_history(self) -> None:
        with self._lock:
            with self._db:
                self._db.execute("DELETE FROM memories WHERE source_ids != '[]'")
                for table in ("messages", "summaries", "sessions", "events", "forgotten_memories", "request_snapshots"):
                    self._db.execute(f"DELETE FROM {table}")
                # Imported materials are not history: artifacts keep their links to material chunks.
                self._db.execute("DELETE FROM dependencies WHERE NOT (owner_type='artifact' AND source_id IN (SELECT id FROM material_chunks))")
                self._db.execute("DELETE FROM memory_conflicts WHERE candidate_id NOT IN (SELECT id FROM memories)")
                kept = "(SELECT id FROM memories UNION SELECT id FROM material_chunks)"
                self._db.execute(f"DELETE FROM vectors WHERE record_id NOT IN {kept}")
                self._db.execute(f"DELETE FROM vector_records WHERE record_id NOT IN {kept}")
                # Artifacts stay until deleted explicitly; their conversation citations fail visibly.
                self._stale_message_citations()
                self._clear_task_history()
                if self._fts:
                    self._db.execute("DELETE FROM message_fts")
            self._checkpoint()

    def add_event(self, kind: str, data: dict, trace_id: str = "") -> dict:
        row = dict(id=_id(), kind=kind, data=data, trace_id=trace_id, created_at=_now())
        with self._lock, self._db:
            self._db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (row["id"], kind, _json(data), trace_id, row["created_at"]))
        return row

    def events(self, limit: int = 200) -> list[dict]:
        with self._lock:
            return [self._row(row) for row in self._db.execute("SELECT * FROM events ORDER BY rowid DESC LIMIT ?", (max(0, min(int(limit), 10000)),))]

    def compression_candidates(self, session_id: str, keep_recent: int = 12) -> list[dict]:
        with self._lock:
            rows = self.history(100000, session_id)
            candidates = rows[:-keep_recent] if keep_recent > 0 else rows
            covered = {row[0] for row in self._db.execute("SELECT source_id FROM dependencies WHERE owner_type='summary'")}
            return [row for row in candidates if row["id"] not in covered]

    def save_summary(self, text: str, source_ids: list[str], model: str = "", prompt_version: str = "v1") -> dict:
        if not text.strip() or not source_ids:
            raise ValueError("A summary needs text and source references")
        source_ids = list(dict.fromkeys(source_ids))
        with self._lock, self._db:
            sources = self._validate_sources(source_ids)
            session_ids = {source["session_id"] for source in sources}
            level = 1 + max(source.get("level", 0) for source in sources)
            if len(session_ids) != 1 and any("level" not in source for source in sources):
                raise ValueError("A segment summary must belong to one session")
            # Repeated jobs for the same segment return the committed record.
            for existing in self._db.execute("SELECT * FROM summaries WHERE source_ids=?", (_json(source_ids),)):
                return self._row(existing)
            row = dict(id=_id(), text=text, source_ids=source_ids, created_at=_now(), model=model,
                       prompt_version=prompt_version, version=1, session_id=next(iter(session_ids)) if len(session_ids) == 1 else "*",
                       source_chars=sum(len(source["text"]) for source in sources), summary_chars=len(text),
                       time_start=min(source.get("time_start", source["created_at"]) for source in sources),
                       time_end=max(source.get("time_end", source["created_at"]) for source in sources), level=level)
            self._db.execute("INSERT INTO summaries (id,text,source_ids,created_at,model,prompt_version,version,session_id,source_chars,summary_chars,time_start,time_end,level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(_json(value) if key == "source_ids" else value for key, value in row.items()))
            self._db.executemany("INSERT INTO dependencies VALUES ('summary',?,?)", [(row["id"], sid) for sid in source_ids])
            return row

    def compression_batch(self, active_session: str) -> list[dict]:
        """Resume old sessions first, then consolidate unparented summaries up to level 3."""
        with self._lock:
            for session in reversed(self.sessions()):
                current = session["id"] == active_session
                candidates = self.compression_candidates(session["id"], keep_recent=8 if current else 0)
                if len(candidates) >= (6 if current else 2):
                    return candidates[:16]
            for level in (1, 2):
                rows = [self._row(row) for row in self._db.execute("""
                    SELECT s.* FROM summaries s WHERE level=? AND NOT EXISTS (
                        SELECT 1 FROM dependencies d WHERE d.owner_type='summary' AND d.source_id=s.id)
                    ORDER BY time_start LIMIT 8
                """, (level,))]
                if len(rows) >= 4:
                    return rows
            return []

    def summaries(self, limit: int = 8) -> list[dict]:
        with self._lock:
            return [self._row(row) for row in self._db.execute("SELECT * FROM summaries ORDER BY rowid DESC LIMIT ?", (max(0, min(int(limit), 10000)),))]

    def _leaf_sources(self, source_ids: list[str]) -> set[str]:
        leaves, seen, queue = set(), set(), list(source_ids)
        while queue:
            source_id = queue.pop()
            if source_id in seen:
                continue
            seen.add(source_id)
            summary = self._db.execute("SELECT source_ids FROM summaries WHERE id=?", (source_id,)).fetchone()
            if summary:
                queue.extend(json.loads(summary["source_ids"]))
            else:
                leaves.add(source_id)
        return leaves

    def context(self, query: str, session_id: str, max_chars: int = 16000, semantic: list[dict] | None = None) -> dict:
        if max_chars < 100:
            raise ValueError("Context budget must be at least 100 characters")
        with self._lock:
            result = {"recent": [], "memories": [], "materials": [], "summaries": [], "retrieved": []}

            def take(group: str, entries: list[dict], allowance: int) -> None:
                used = 0
                for entry in entries:
                    item = dict(entry)
                    remaining = min(allowance - used, max_chars - len(_json(result)) - 1)
                    if remaining <= 0:
                        break
                    overhead = len(_json({**item, "text": ""}))
                    if overhead + 32 > remaining:
                        continue
                    if len(_json(item)) > remaining:
                        # JSON may escape characters; use actual serialized cost.
                        item["text"] = item["text"][:max(0, remaining - overhead - 16)]
                        item["truncated"] = True
                        while item["text"] and len(_json(item)) > remaining:
                            item["text"] = item["text"][:-max(1, len(_json(item)) - remaining)]
                    if len(_json(item)) <= remaining:
                        result[group].append(item)
                        used += len(_json(item)) + 1

            take("recent", list(reversed(self.history(30, session_id))), int(max_chars * .45))
            result["recent"].reverse()
            used_ids = {item["id"] for item in result["recent"]}
            memories = sorted(self.list_memories(), key=lambda item: (_relevance(query, item["text"]), item["created_at"]), reverse=True)
            semantic = semantic or []
            memories = fuse(memories, [item for item in semantic if item["kind"] == "memory"])
            take("memories", memories, int(max_chars * .15))
            # Imported materials are reference data with a locator, never instructions.
            materials = fuse(self.search_chunks(query, 8), [item for item in semantic if item["kind"] == "chunk"])
            take("materials", [{"id": item["id"], "role": "material", "source": chunk_label(item),
                                "created_at": item["created_at"], "text": item["text"]} for item in materials],
                 int(max_chars * .15))
            retrieved = fuse(self.search(query, 12), [item for item in semantic if item["kind"] == "message"])
            take("retrieved", [item for item in retrieved if item["id"] not in used_ids], int(max_chars * .12))
            used_ids.update(item["id"] for item in result["retrieved"])
            summaries = [item for item in self.summaries(100) if not used_ids.intersection(self._leaf_sources(item["source_ids"]))]
            summaries.sort(key=lambda item: (_relevance(query, item["text"]) + (2 if item["session_id"] == session_id else 0), item["created_at"]), reverse=True)
            eligible = {item["id"] for item in summaries}
            summaries = fuse(summaries, [item for item in semantic if item["id"] in eligible])
            for summary in summaries:
                leaves = self._leaf_sources(summary["source_ids"])
                if not used_ids.intersection(leaves):
                    before = len(result["summaries"])
                    take("summaries", [summary], max_chars - len(_json(result)))
                    if len(result["summaries"]) > before:
                        used_ids.update(leaves)
            return result

    def cleanup(self, log_days: int = 30) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, log_days))).isoformat(timespec="milliseconds")
        with self._lock:
            with self._db:
                self._db.execute("DELETE FROM events WHERE created_at<?", (cutoff,))
                self._db.execute("DELETE FROM request_snapshots WHERE created_at<?", (cutoff,))
            self._checkpoint()

    def close(self) -> None:
        with self._lock:
            self._db.close()
