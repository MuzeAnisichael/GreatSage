"""Versioned local vector index. The containing MemoryStore owns transactions."""
from __future__ import annotations

import hashlib
import json
import math
import numpy as np


def fingerprint(config: dict) -> str:
    value = {key: config.get(key, "") for key in ("provider", "base_url", "model")}
    value["chunk_version"] = "utf8-4000-overlap80-v1"
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunks(text: str) -> list[str]:
    result, offset = [], 0
    while offset < len(text):
        part = text[offset:].encode("utf-8")[:4000].decode("utf-8", errors="ignore")
        if part.strip():
            result.append(part)
        if offset + len(part) >= len(text):
            break
        offset += max(1, len(part) - 80)
    return result


def fuse(lexical: list[dict], semantic: list[dict], limit: int = 100) -> list[dict]:
    scores, records = {}, {}
    for ranking in (lexical, semantic):
        for rank, item in enumerate(ranking):
            records[item["id"]] = item
            scores[item["id"]] = scores.get(item["id"], 0) + 1 / (20 + rank)
    return [records[id] for id in sorted(scores, key=lambda id: scores[id], reverse=True)[:limit]]


class VectorIndexMixin:
    def _init_vectors(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS vector_records (
                record_id TEXT NOT NULL, fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL,
                PRIMARY KEY(record_id,fingerprint));
            CREATE TABLE IF NOT EXISTS vectors (
                record_id TEXT NOT NULL, fingerprint TEXT NOT NULL, part INTEGER NOT NULL,
                dimensions INTEGER NOT NULL, vector BLOB NOT NULL,
                PRIMARY KEY(record_id,fingerprint,part));
            CREATE INDEX IF NOT EXISTS vector_model ON vectors(fingerprint);
        """)

    def record(self, id: str) -> dict | None:
        with self._lock:
            for kind, table in (("message", "messages"), ("memory", "memories"), ("summary", "summaries")):
                row = self._row(self._db.execute(f"SELECT * FROM {table} WHERE id=?", (id,)).fetchone())
                if row:
                    return {**row, "kind": kind}
            return None

    def pending_vectors(self, version: str, limit: int = 4) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""
                SELECT * FROM (
                  SELECT id,text,'memory' AS kind,created_at FROM memories
                  UNION ALL SELECT id,text,'message',created_at FROM messages
                  UNION ALL SELECT id,text,'summary',created_at FROM summaries
                ) r WHERE NOT EXISTS (SELECT 1 FROM vector_records v WHERE v.record_id=r.id AND v.fingerprint=?)
                ORDER BY created_at LIMIT ?
            """, (version, max(1, min(limit, 32)))).fetchall()
            return [dict(row) for row in rows]

    def save_vectors(self, id: str, version: str, content_hash: str, vectors: list[list[float]]) -> bool:
        matrix = np.asarray(vectors, dtype=np.float64)
        if matrix.ndim != 2 or not 1 <= matrix.shape[1] <= 16384 or not matrix.shape[0] or not np.isfinite(matrix).all():
            raise ValueError("Invalid embedding vectors")
        norms = np.linalg.norm(matrix, axis=1)
        if (norms <= 0).any() or not np.isfinite(norms).all():
            raise ValueError("Invalid embedding norms")
        matrix = (matrix / norms[:, None]).astype("<f4")
        with self._lock, self._db:
            row = self.record(id)
            if not row or text_hash(row["text"]) != content_hash:
                return False  # Source was deleted/revised while the model ran.
            dimensions = self._db.execute("SELECT dimensions FROM vectors WHERE fingerprint=? LIMIT 1", (version,)).fetchone()
            if dimensions and dimensions[0] != matrix.shape[1]:
                raise ValueError("Embedding dimensions changed; rebuild the index")
            self._db.execute("DELETE FROM vectors WHERE record_id=? AND fingerprint=?", (id, version))
            self._db.executemany("INSERT INTO vectors VALUES (?,?,?,?,?)",
                                 [(id, version, i, len(vector), vector.tobytes()) for i, vector in enumerate(matrix)])
            self._db.execute("INSERT OR REPLACE INTO vector_records VALUES (?,?,?)", (id, version, content_hash))
            return True

    def semantic_search(self, vector: list[float], version: str, limit: int = 20) -> list[dict]:
        query = np.asarray(vector, dtype=np.float64)
        if query.ndim != 1 or not query.size or not np.isfinite(query).all():
            raise ValueError("Invalid query vector")
        norm = np.linalg.norm(query)
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("Invalid query vector norm")
        query = query / norm
        with self._lock:
            scores = {}
            cursor = self._db.execute("SELECT record_id,vector FROM vectors WHERE fingerprint=? AND dimensions=?", (version, len(query)))
            while batch := cursor.fetchmany(256):
                matrix = np.stack([np.frombuffer(row["vector"], dtype="<f4") for row in batch])
                for row, score in zip(batch, matrix @ query):
                    if score >= .25:
                        scores[row["record_id"]] = max(scores.get(row["record_id"], -1), float(score))
            result = []
            for id in sorted(scores, key=scores.get, reverse=True)[:max(0, min(limit, 100))]:
                row = self.record(id)
                if row:
                    result.append({**row, "semantic_score": round(scores[id], 6)})
            return result

    def vector_status(self, version: str) -> dict:
        with self._lock:
            total = sum(self._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("messages", "memories", "summaries"))
            indexed = self._db.execute("SELECT count(*) FROM vector_records WHERE fingerprint=?", (version,)).fetchone()[0]
            parts = self._db.execute("SELECT count(*) FROM vectors WHERE fingerprint=?", (version,)).fetchone()[0]
            return {"total": total, "indexed": indexed, "pending": max(0, total - indexed), "chunks": parts,
                    "fingerprint": version}

    def clear_vectors(self):
        with self._lock, self._db:
            self._db.execute("DELETE FROM vectors")
            self._db.execute("DELETE FROM vector_records")

    def _drop_vectors(self, ids):
        for id in ids:
            self._db.execute("DELETE FROM vectors WHERE record_id=?", (id,))
            self._db.execute("DELETE FROM vector_records WHERE record_id=?", (id,))
