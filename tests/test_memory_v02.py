import pytest

from greatsage.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    db = MemoryStore(tmp_path)
    yield db
    db.close()


@pytest.mark.parametrize('resolution,count,new_active', [('replace', 1, True), ('keep_existing', 1, False), ('keep_both', 2, True)])
def test_conflict_is_quarantined_until_user_decides(store, resolution, count, new_active):
    old = store.add_memory('我的当前称呼是小林')
    answer = store.add_message('assistant', '称呼小林', metadata={'source_ids': [old['id']]})
    source = store.add_message('user', '请记住我的称呼是小琳')
    new = store.add_memory('我的当前称呼是小琳', [source['id']], [old['id']])
    assert new['status'] == 'pending'
    assert new['id'] not in {r['id'] for r in store.list_memories()}
    assert new['id'] not in {r['id'] for r in store.pending_vectors('test')}
    assert store.conflicts()[0]['existing'][0]['id'] == old['id']
    store.resolve_conflict(new['id'], resolution)
    assert len(store.list_memories()) == count
    assert (new['id'] in {r['id'] for r in store.list_memories()}) == new_active
    assert bool(store.record(answer['id'])) == (resolution != 'replace')
    assert store.record(source['id'])
    assert not store.conflicts()
    with pytest.raises(KeyError):
        store.resolve_conflict(new['id'], resolution)


def test_candidate_source_deletion_and_revision_invalidate_pending(store):
    old = store.add_memory('名字甲')
    source = store.add_message('user', '名字乙')
    candidate = store.add_memory('名字乙', [source['id']], [old['id']])
    store.revise_message(source['id'], '不是我的名字')
    assert store.record(candidate['id']) is None
    assert not store.conflicts()


def test_old_sessions_resume_into_layered_summaries_and_source_navigation(store, tmp_path):
    originals, segments = [], []
    for index in range(4):
        session = store.new_session()
        originals.extend(store.add_message('user', f'会话 {index} 原文 {j}', session_id=session) for j in range(2))
    current = store.new_session()
    for index in range(4):
        batch = store.compression_batch(current)
        assert len(batch) == 2
        segments.append(store.save_summary(f'片段 {index}', [r['id'] for r in batch]))
    batch = store.compression_batch(current)
    assert {r['id'] for r in batch} == {r['id'] for r in segments}
    upper = store.save_summary('跨会话摘要', [r['id'] for r in batch])
    assert upper['level'] == 2 and upper['session_id'] == '*'
    assert len(store.record_details(upper['id'])['sources']) == 4
    assert len(store.record_details(segments[0]['id'])['sources']) == 2
    store.delete_message(originals[0]['id'])
    assert not store.record(upper['id'])
    assert len(store.summaries()) == 3
    assert not store.compression_batch(current)


def test_history_cursor_has_no_duplicates_across_equal_timestamps(store):
    first, second = store.new_session(), store.new_session()
    expected = [store.add_message('user', str(i), session_id=first)['id'] for i in range(45)]
    store.add_message('user', 'other session', session_id=second)
    seen, cursor = [], None
    while True:
        page = store.history_page(cursor, first, 7)
        seen.extend(row['id'] for row in page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert seen == list(reversed(expected))
