"""Conversation metadata and a single durable active-session pointer."""
from __future__ import annotations


class SessionsMixin:
    def _init_sessions(self):
        self._db.execute("""CREATE TABLE IF NOT EXISTS session_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            active_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL)""")

    def session(self, session_id: str) -> dict | None:
        """Return metadata only; history stays in canonical message rows."""
        with self._lock:
            row = self._db.execute("""
                SELECT s.id,s.created_at,count(m.id) AS message_count,max(m.created_at) AS last_message_at,
                       CASE WHEN s.id=(SELECT active_session_id FROM session_state WHERE singleton=1)
                            THEN 1 ELSE 0 END AS active
                FROM sessions s LEFT JOIN messages m ON m.session_id=s.id
                WHERE s.id=? GROUP BY s.id
            """, (session_id,)).fetchone()
            if row is None:
                return None
            return {**dict(row), "active": bool(row["active"])}

    def activate_session(self, session_id: str) -> str:
        with self._lock, self._db:
            if not isinstance(session_id, str) or not self._db.execute(
                    "SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError("会话不存在或已删除。")
            self._db.execute("""INSERT INTO session_state(singleton,active_session_id) VALUES(1,?)
                ON CONFLICT(singleton) DO UPDATE SET active_session_id=excluded.active_session_id""", (session_id,))
        return session_id

    def active_session(self) -> str:
        """Restore the selected session; migrate old databases to the latest one."""
        with self._lock, self._db:
            row = self._db.execute("""SELECT s.id FROM session_state a
                JOIN sessions s ON s.id=a.active_session_id WHERE a.singleton=1""").fetchone()
            if row is not None:
                return row["id"]
            latest = self._db.execute("""SELECT s.id FROM sessions s
                LEFT JOIN messages m ON m.session_id=s.id GROUP BY s.id
                ORDER BY coalesce(max(m.created_at),s.created_at) DESC,s.rowid DESC LIMIT 1""").fetchone()
            session_id = latest["id"] if latest is not None else self.new_session()
            return self.activate_session(session_id)

    def sessions(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""
                SELECT s.id,s.created_at,count(m.id) AS message_count,max(m.created_at) AS last_message_at,
                       CASE WHEN s.id=(SELECT active_session_id FROM session_state WHERE singleton=1)
                            THEN 1 ELSE 0 END AS active
                FROM sessions s LEFT JOIN messages m ON m.session_id=s.id GROUP BY s.id
                ORDER BY active DESC,coalesce(max(m.created_at),s.created_at) DESC,s.rowid DESC
            """)
            return [{**dict(row), "active": bool(row["active"])} for row in rows]
