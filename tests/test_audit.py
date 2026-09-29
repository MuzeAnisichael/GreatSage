import json

from greatsage.memory import MemoryStore


def test_snapshots_are_opt_in_scrubbed_immutable_and_invalidated(tmp_path):
    store = MemoryStore(tmp_path)
    try:
        source = store.add_message('user', '保密项目甲', trace_id='user-trace')
        config = {'llm': {'model': 'fixture', 'api_key': 'a-secret-not-in-sk-format'}, 'global_prompt': '我的全局指令'}
        messages = [{'role': 'system', 'content': 'Skill revision 1'}, {'role': 'user', 'content': source['text']}]
        meta_id = store.save_snapshot('trace', 'response', config, messages, [source['id']])
        body_id = store.save_snapshot('trace', 'response', config, messages, [source['id']], retain_content=True)
        messages[0]['content'] = 'Skill revision 2'
        meta = store.snapshot(meta_id, True)
        body = store.snapshot(body_id, True)
        assert meta['content'] is None and meta['metadata']['content_status'] == 'disabled'
        assert '我的全局指令' not in json.dumps(meta, ensure_ascii=False)
        assert 'a-secret-not-in-sk-format' not in json.dumps(body)
        assert body['content']['messages'][0]['content'] == 'Skill revision 1'
        assert 'content' not in store.snapshot(body_id)
        assert len(store.snapshots('trace')) == 2
        store.delete_message(source['id'])
        assert store.snapshot(body_id, True)['content'] is None
        assert store.snapshot(body_id)['metadata']['content_status'] == 'source_deleted'
        store.clear_history()
        assert store.snapshots() == []
    finally:
        store.close()


def test_body_limit_and_retention_apply_to_audit(tmp_path):
    store = MemoryStore(tmp_path)
    try:
        id = store.save_snapshot('trace', 'response', {}, [{'content': 'x' * 270_000}], [], retain_content=True)
        assert store.snapshot(id, True)['metadata']['content_status'] == 'size_limit'
        assert store.snapshot(id, True)['content'] is None
        store._db.execute("UPDATE request_snapshots SET created_at='2000-01-01T00:00:00+00:00'")
        store._db.commit()
        store.cleanup(30)
        assert not store.snapshots()
    finally:
        store.close()
