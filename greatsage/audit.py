"""Bounded, immutable request evidence. Bodies require explicit opt-in."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone


def scrub(value):
    if isinstance(value, dict):
        return {key: '[redacted]' if key.lower() in {'api_key', 'authorization', 'token', 'password', 'secret', 'credential'}
                else scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'\bsk-[\w-]{8,}', '[redacted]', value)
        return re.sub(r'Bearer\s+[^\s"\']+', 'Bearer [redacted]', value, flags=re.I)
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


class AuditMixin:
    def _init_audit(self):
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS request_snapshots (
                id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, purpose TEXT NOT NULL,
                created_at TEXT NOT NULL, metadata TEXT NOT NULL, content TEXT,
                source_ids TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS snapshot_trace ON request_snapshots(trace_id);
        ''')

    def save_snapshot(self, trace_id, purpose, config, messages, source_ids, skills=None, retain_content=False):
        config, messages, skills = scrub(config), scrub(messages), scrub(skills or [])
        body = {'config': config, 'messages': messages, 'skills': skills}
        serialized = encode(body)
        allowed = retain_content and len(serialized.encode('utf-8')) <= 256_000
        meta = {'schema_version': 1, 'config_sha256': digest(config), 'request_sha256': digest(messages),
                'skills': skills, 'request_bytes': len(encode(messages).encode('utf-8')),
                'provider': config.get('llm', config).get('provider'),
                'model': config.get('llm', config).get('model'), 'outcome': 'started',
                'content_status': 'saved' if allowed else 'size_limit' if retain_content else 'disabled'}
        id = uuid.uuid4().hex
        with self._lock, self._db:
            self._validate_sources(source_ids, allow_memories=True)
            self._db.execute('INSERT INTO request_snapshots VALUES (?,?,?,?,?,?,?)',
                             (id, trace_id, purpose, datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                              encode(meta), serialized if allowed else None, encode(list(dict.fromkeys(source_ids)))))
        return id

    def finish_snapshot(self, id, outcome):
        with self._lock, self._db:
            row = self._db.execute('SELECT metadata FROM request_snapshots WHERE id=?', (id,)).fetchone()
            if row:
                meta = json.loads(row[0]); meta['outcome'] = outcome
                self._db.execute('UPDATE request_snapshots SET metadata=? WHERE id=?', (encode(meta), id))

    def recover_snapshots(self):
        with self._lock, self._db:
            for row in self._db.execute('SELECT id,metadata FROM request_snapshots').fetchall():
                meta = json.loads(row['metadata'])
                if meta.get('outcome') == 'started':
                    meta['outcome'] = 'interrupted_restart'
                    self._db.execute('UPDATE request_snapshots SET metadata=? WHERE id=?', (encode(meta), row['id']))

    @staticmethod
    def _snapshot_row(row, include_content=False):
        result = dict(row)
        for field in ('metadata', 'source_ids'):
            result[field] = json.loads(result[field])
        content = result.pop('content', None)
        if include_content:
            result['content'] = json.loads(content) if content else None
        return result

    def snapshots(self, trace_id=None, limit=100):
        with self._lock:
            where, args = (' WHERE trace_id=?', [trace_id]) if trace_id else ('', [])
            rows = self._db.execute('SELECT id,trace_id,purpose,created_at,metadata,source_ids FROM request_snapshots' + where + ' ORDER BY rowid DESC LIMIT ?',
                                    [*args, max(1, min(limit, 200))])
            return [self._snapshot_row(row) for row in rows]

    def snapshot(self, id, include_content=False):
        with self._lock:
            row = self._db.execute('SELECT * FROM request_snapshots WHERE id=?', (id,)).fetchone()
            if not row:
                raise KeyError(id)
            return self._snapshot_row(row, include_content)

    def _redact_snapshots(self, ids, texts, traces):
        for row in self._db.execute('SELECT id,trace_id,source_ids,content,metadata FROM request_snapshots').fetchall():
            if (row['trace_id'] in traces or ids.intersection(json.loads(row['source_ids']))
                    or row['content'] and any(encode(text)[1:-1] in row['content'] for text in texts if text)):
                meta = json.loads(row['metadata'])
                meta['content_status'] = 'source_deleted'
                self._db.execute('UPDATE request_snapshots SET content=NULL,metadata=? WHERE id=?', (encode(meta), row['id']))
