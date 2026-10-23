"""Local structured memory, review and extraction checkpoints; no model imports."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from .semantic import text_hash


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _entity_id(name: str, kind: str) -> str:
    # Exact names and types only: aliases and similar spellings are not merged.
    return hashlib.sha256(_json([kind, name]).encode("utf-8")).hexdigest()[:32]


class StructuredMemoryMixin:
    def _init_structured_memory(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS structured_memories (
                memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, subject TEXT NOT NULL, subject_type TEXT NOT NULL,
                predicate TEXT NOT NULL, value TEXT NOT NULL, object_type TEXT NOT NULL,
                evidence TEXT NOT NULL, review_reasons TEXT NOT NULL, model TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE);
            CREATE INDEX IF NOT EXISTS structured_memory_key
                ON structured_memories(subject,subject_type,predicate);
            CREATE TABLE IF NOT EXISTS memory_extraction_progress (
                source_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                content_hash TEXT NOT NULL, processed_at TEXT NOT NULL, model TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_extraction_blocks (
                source_id TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(source_id,content_hash));
        """)
        # Migrate existing explicit forget decisions without retaining their text.
        for row in self._db.execute("SELECT source_ids FROM forgotten_memories").fetchall():
            self._block_extraction_sources(json.loads(row["source_ids"]))

    def _block_extraction_sources(self, source_ids: list[str]) -> None:
        """A forget decision suppresses the old source version, including paraphrases."""
        for source_id in self._leaf_sources(source_ids):
            row = self._db.execute("SELECT text FROM messages WHERE id=?", (source_id,)).fetchone()
            if row:
                self._db.execute("INSERT OR IGNORE INTO memory_extraction_blocks VALUES (?,?,?)",
                                 (source_id, text_hash(row["text"]), _now()))

    @staticmethod
    def _eligible_extraction_source(row: dict) -> bool:
        return row.get("role") == "user" and row.get("source") in {"text", "microphone"}

    def _source_snapshot(self, row: dict) -> dict:
        return {**row, "content_hash": text_hash(row["text"]),
                "version": row.get("metadata", {}).get("version", 1)}

    def extraction_sources(self, limit: int = 8, session_id: str | None = None) -> list[dict]:
        """Return at most eight unprocessed, unsuppressed stable user originals."""
        with self._lock:
            session, args = (" AND m.session_id=?", [session_id]) if session_id is not None else ("", [])
            rows = self._db.execute("""
                SELECT m.* FROM messages m WHERE m.role='user' AND m.source IN ('text','microphone')
                AND NOT EXISTS (SELECT 1 FROM memory_extraction_progress p WHERE p.source_id=m.id)
                AND NOT EXISTS (SELECT 1 FROM memory_extraction_blocks b WHERE b.source_id=m.id)
            """ + session + " ORDER BY m.rowid LIMIT ?", [*args, max(0, min(int(limit), 8))])
            return [self._source_snapshot(self._row(row)) for row in rows]

    def _validate_extraction_sources(self, sources: list[dict]) -> dict[str, dict]:
        if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
            raise ValueError("Extraction needs one to eight source snapshots")
        validated = {}
        for snapshot in sources:
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str):
                raise ValueError("Invalid extraction source snapshot")
            row = self._row(self._db.execute("SELECT * FROM messages WHERE id=?", (snapshot["id"],)).fetchone())
            if not row or not self._eligible_extraction_source(row):
                raise ValueError("Extraction source no longer exists or is not a user original")
            current = self._source_snapshot(row)
            if any(snapshot.get(key) != current[key]
                   for key in ("id", "text", "content_hash", "version", "role", "source", "session_id")):
                raise ValueError("Extraction source snapshot changed")
            if row["id"] in validated:
                raise ValueError("Duplicate extraction source snapshot")
            validated[row["id"]] = current
        return validated

    def _validate_extraction_item(self, item: dict, sources: dict[str, dict]) -> dict:
        if not isinstance(item, dict):
            raise ValueError("Invalid extraction item")
        clean = {}
        for field, limit in (("text", 2000), ("subject", 200), ("predicate", 200), ("value", 2000)):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError("Invalid extracted " + field)
            clean[field] = value.strip()
        choices = {"kind": {"fact", "preference", "relationship"},
                   "subject_type": {"person", "project", "organization", "other"},
                   "object_type": {"literal", "person", "project", "organization", "other"}}
        for field, allowed in choices.items():
            if not isinstance(item.get(field), str) or item[field] not in allowed:
                raise ValueError("Invalid extracted " + field)
            clean[field] = item[field]
        reasons = item.get("review_reasons", [])
        if (not isinstance(reasons, list) or any(not isinstance(reason, str) or reason not in
                {"uncertain", "sensitive", "ambiguous_entity"} for reason in reasons)):
            raise ValueError("Invalid extraction review reasons")
        clean["review_reasons"] = list(dict.fromkeys(reasons))
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
            raise ValueError("Extracted memory needs source evidence")
        clean["evidence"] = []
        for citation in evidence:
            if not isinstance(citation, dict):
                raise ValueError("Invalid extraction evidence")
            source_id, quote = citation.get("source_id"), citation.get("quote")
            if (not isinstance(source_id, str) or source_id not in sources or not isinstance(quote, str)
                    or not quote.strip() or len(quote) > 4000 or quote not in sources[source_id]["text"]):
                raise ValueError("Extraction evidence must quote the supplied user original")
            source = sources[source_id]
            clean["evidence"].append({"source_id": source_id, "quote": quote,
                                      "content_hash": source["content_hash"], "version": source["version"]})
        return clean

    def _structured_conflicts(self, item: dict, exclude_id: str = "") -> list[str]:
        return [row["memory_id"] for row in self._db.execute("""
            SELECT s.memory_id FROM structured_memories s JOIN memories m ON m.id=s.memory_id
            WHERE m.status='active' AND s.subject=? AND s.subject_type=? AND s.predicate=?
              AND (s.value!=? OR s.object_type!=?) AND s.memory_id!=?
        """, (item["subject"], item["subject_type"], item["predicate"], item["value"],
              item["object_type"], exclude_id))]

    def _structured_memory_row(self, row: dict) -> dict:
        detail = self._db.execute("SELECT * FROM structured_memories WHERE memory_id=?", (row["id"],)).fetchone()
        if not detail:
            return row
        structured = {key: detail[key] for key in
                      ("kind", "subject", "subject_type", "predicate", "value", "object_type", "model")}
        structured["evidence"] = json.loads(detail["evidence"])
        structured["review_reasons"] = json.loads(detail["review_reasons"])
        conflict = self._db.execute("SELECT existing_ids FROM memory_conflicts WHERE candidate_id=?", (row["id"],)).fetchone()
        ids = [id for id in json.loads(conflict[0]) if self._db.execute(
            "SELECT 1 FROM memories WHERE id=? AND status='active'", (id,)).fetchone()] if conflict else []
        return {**row, "structured": structured, "memory_kind": structured["kind"],
                "review_reasons": structured["review_reasons"], "conflict_ids": ids, "has_conflict": bool(ids)}

    def record(self, id: str) -> dict | None:
        with self._lock:
            row = super().record(id)
            return self._structured_memory_row(row) if row and row["kind"] == "memory" else row

    def save_extraction(self, items: list[dict], sources: list[dict], policy: str, model: str = "") -> dict:
        """Validate snapshots/evidence and commit facts with progress in one transaction."""
        if policy not in {"auto", "necessary", "mostly"}:
            raise ValueError("Invalid memory confirmation policy")
        if not isinstance(items, list) or len(items) > 24:
            raise ValueError("Extraction is limited to 24 items")
        if not isinstance(model, str) or len(model) > 300:
            raise ValueError("Invalid extraction model")
        result = {"saved": 0, "active": 0, "pending": 0, "conflicts": 0,
                  "skipped": 0, "processed_sources": 0, "items": []}
        with self._lock, self._db:
            snapshots = self._validate_extraction_sources(sources)
            validated = [self._validate_extraction_item(item, snapshots) for item in items]
            marks = ",".join("?" for _ in snapshots)
            consumed = {row[0] for row in self._db.execute(
                f"SELECT source_id FROM memory_extraction_progress WHERE source_id IN ({marks})", list(snapshots))}
            blocked = {row[0] for row in self._db.execute(
                f"SELECT source_id FROM memory_extraction_blocks WHERE source_id IN ({marks})", list(snapshots))}
            for item in validated:
                source_ids = list(dict.fromkeys(e["source_id"] for e in item["evidence"]))
                identity = {key: item[key] for key in
                            ("kind", "subject", "subject_type", "predicate", "value", "object_type")}
                identity["sources"] = sorted((sid, snapshots[sid]["content_hash"]) for sid in source_ids)
                fingerprint = text_hash(_json(identity))
                if (blocked.intersection(source_ids) or set(source_ids).issubset(consumed) or
                        self._db.execute("SELECT 1 FROM structured_memories WHERE fingerprint=?", (fingerprint,)).fetchone()):
                    result["skipped"] += 1
                    continue
                conflicts = self._structured_conflicts(item)
                reasons = list(item["review_reasons"])
                if policy == "mostly":
                    reasons.append("confirmation_policy")
                if conflicts:
                    reasons.append("conflict")
                pending = bool(conflicts or policy == "mostly" or (policy == "necessary" and reasons))
                row = {"id": uuid.uuid4().hex, "text": item["text"], "source_ids": source_ids,
                       "created_at": _now(), "version": 1, "origin": "extracted", "revision_of": None,
                       "status": "pending" if pending else "active"}
                self._db.execute("INSERT INTO memories (id,text,source_ids,created_at,version,origin,revision_of,status) VALUES (?,?,?,?,?,?,?,?)",
                                 (row["id"], row["text"], _json(source_ids), row["created_at"], 1, "extracted", None, row["status"]))
                self._db.executemany("INSERT INTO dependencies VALUES ('memory',?,?)", [(row["id"], sid) for sid in source_ids])
                self._db.execute("INSERT INTO structured_memories VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (row["id"], item["kind"], item["subject"], item["subject_type"], item["predicate"],
                                  item["value"], item["object_type"], _json(item["evidence"]), _json(reasons), model, fingerprint))
                if conflicts:
                    self._db.execute("INSERT INTO memory_conflicts VALUES (?,?,?,?)",
                                     (row["id"], _json(conflicts), "structured_contradiction", row["created_at"]))
                result["items"].append(self._structured_memory_row(row))
                result["saved"] += 1
                result[row["status"]] += 1
                result["conflicts"] += bool(conflicts)
            for sid, source in snapshots.items():
                if sid not in consumed and sid not in blocked:
                    self._db.execute("INSERT INTO memory_extraction_progress VALUES (?,?,?,?)",
                                     (sid, source["content_hash"], _now(), model))
                    result["processed_sources"] += 1
        return result

    def memory_candidates_list(self) -> list[dict]:
        with self._lock:
            return [self._structured_memory_row(self._row(row)) for row in self._db.execute("""
                SELECT m.* FROM memories m JOIN structured_memories s ON s.memory_id=m.id
                WHERE m.status='pending' ORDER BY m.rowid DESC
            """)]

    def resolve_memory_candidate(self, id: str, action: str) -> dict:
        if action not in {"accept", "reject"}:
            raise ValueError("Invalid memory candidate action")
        conflict_ids = []
        with self._lock:
            with self._db:
                row = self.record(id)
                if not row or row.get("status") != "pending" or "structured" not in row:
                    raise KeyError(id)
                if action == "reject":
                    self._delete_record("memory", id)
                else:
                    conflict_ids = list(dict.fromkeys(row["conflict_ids"] + self._structured_conflicts(row["structured"], id)))
                    if conflict_ids:
                        self._db.execute("INSERT OR REPLACE INTO memory_conflicts VALUES (?,?,?,?)",
                                         (id, _json(conflict_ids), "structured_contradiction", _now()))
                    else:
                        self._db.execute("UPDATE memories SET status='active' WHERE id=?", (id,))
                        self._db.execute("DELETE FROM memory_conflicts WHERE candidate_id=?", (id,))
            if action == "reject":
                self._checkpoint()
        if conflict_ids:
            raise ValueError("Candidate has a conflict; use the conflict resolution action")
        return {"candidate_id": id, "action": action, "status": "active" if action == "accept" else "rejected"}

    def _entity_records(self) -> tuple[dict, list[dict]]:
        entities, facts = {}, []
        for row in self._db.execute("""SELECT m.* FROM memories m JOIN structured_memories s
                                       ON s.memory_id=m.id ORDER BY m.rowid DESC"""):
            fact = self._structured_memory_row(self._row(row))
            facts.append(fact)
            item = fact["structured"]
            names = [(item["subject"], item["subject_type"])]
            if item["object_type"] != "literal":
                names.append((item["value"], item["object_type"]))
            for name, kind in set(names):
                id = _entity_id(name, kind)
                entity = entities.setdefault(id, {"id": id, "name": name, "type": kind,
                                                  "fact_count": 0, "pending_count": 0})
                entity["fact_count" if fact["status"] == "active" else "pending_count"] += 1
        return entities, facts

    def memory_entities(self, query: str = "") -> list[dict]:
        with self._lock:
            entities, _ = self._entity_records()
            return sorted((item for item in entities.values() if query.casefold() in item["name"].casefold()),
                          key=lambda item: (item["name"], item["type"]))

    def memory_entity(self, id: str) -> dict:
        with self._lock:
            entities, facts = self._entity_records()
            if id not in entities:
                raise KeyError(id)
            matching = [fact for fact in facts if id in {
                _entity_id(fact["structured"]["subject"], fact["structured"]["subject_type"]),
                _entity_id(fact["structured"]["value"], fact["structured"]["object_type"])
                if fact["structured"]["object_type"] != "literal" else ""}]
            return {"entity": entities[id], "facts": matching,
                    "relationships": [fact for fact in matching if fact["structured"]["kind"] == "relationship"]}

    def extraction_status(self) -> dict:
        with self._lock:
            eligible = self._db.execute("SELECT count(*) FROM messages WHERE role='user' AND source IN ('text','microphone')").fetchone()[0]
            processed = self._db.execute("SELECT count(*),max(processed_at) FROM memory_extraction_progress").fetchone()
            blocked = self._db.execute("SELECT count(*) FROM messages m WHERE EXISTS (SELECT 1 FROM memory_extraction_blocks b WHERE b.source_id=m.id)").fetchone()[0]
            pending = self._db.execute("""SELECT count(*) FROM messages m WHERE m.role='user' AND m.source IN ('text','microphone')
                AND NOT EXISTS (SELECT 1 FROM memory_extraction_progress p WHERE p.source_id=m.id)
                AND NOT EXISTS (SELECT 1 FROM memory_extraction_blocks b WHERE b.source_id=m.id)""").fetchone()[0]
            counts = {row[0]: row[1] for row in self._db.execute("""SELECT m.status,count(*) FROM memories m
                JOIN structured_memories s ON s.memory_id=m.id GROUP BY m.status""")}
            conflicts = self._db.execute("SELECT count(*) FROM memory_conflicts c JOIN structured_memories s ON s.memory_id=c.candidate_id").fetchone()[0]
            return {"eligible_sources": eligible, "processed_sources": processed[0], "pending_sources": pending,
                    "blocked_sources": blocked, "active": counts.get("active", 0), "pending": counts.get("pending", 0),
                    "conflicts": conflicts, "last_processed_at": processed[1]}
