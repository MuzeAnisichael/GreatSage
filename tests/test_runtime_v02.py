import asyncio
import time

import pytest

from greatsage.audio import AudioChunk
from greatsage.runtime import Runtime
from greatsage.semantic import fingerprint, text_hash


class FixtureProviders:
    def __init__(self):
        self.answer = '{"respond":true,"reason":"direct_request"}'
        self.calls = 0
        self.on_call = lambda: None

    async def stream_chat(self, config, messages):
        self.calls += 1
        self.on_call()
        yield {'text': self.answer, 'usage': {'prompt_tokens': 10}}

    async def transcribe(self, config, pcm):
        return {'text': '半句识别', 'usage': {'seconds': .3}}

    async def embed(self, config, texts):
        raise TimeoutError('fixture service failure')

    async def close(self):
        pass


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', '')
    instance = Runtime(tmp_path, providers=FixtureProviders())
    yield instance
    await instance.close()


def message(runtime, text):
    return runtime.memory.add_message('user', text, 'microphone:fixture', runtime.session_id, 'fixture-trace')


async def test_listen_fast_path_semantic_and_invalid_json(runtime):
    config = runtime.settings.raw()
    assert await runtime._should_reply(message(runtime, '大贤者，解释这段。'), config)
    assert runtime.providers.calls == 0
    assert not await runtime._should_reply(message(runtime, '先别回答，为什么会这样？'), config)
    assert await runtime._should_reply(message(runtime, '这一行是什么意思？'), config)
    assert runtime.memory.snapshots()[0]['purpose'] == 'listen_decision'
    runtime.providers.answer = '{"respond":"yes","reason":"direct_request"}'
    assert not await runtime._should_reply(message(runtime, '她问我这是什么？'), config)
    assert runtime.memory.snapshots()[0]['metadata']['outcome'] == 'failed'


async def test_obsolete_decision_does_not_reply(runtime):
    runtime.providers.on_call = lambda: setattr(runtime, 'generation', runtime.generation + 1)
    assert not await runtime._should_reply(message(runtime, '这是为什么？'), runtime.settings.raw())


async def test_model_suggested_conflicts_cannot_overwrite_or_invent_ids(runtime):
    old = runtime.memory.add_memory('当前称呼是甲')
    runtime.providers.answer = '{"conflict_ids":["' + old['id'] + '","nonexistent"]}'
    new = await runtime.add_memory_checked('当前称呼是乙')
    assert new['status'] == 'pending'
    assert runtime.memory.list_memories()[0]['id'] == old['id']
    runtime.providers.on_call = lambda: setattr(runtime, 'data_epoch', runtime.data_epoch + 1)
    with pytest.raises(ValueError, match='changed'):
        await runtime.add_memory_checked('不应提交的过期内容')
    assert len(runtime.memory.conflicts()) == 1


async def test_partial_asr_usage_and_cancellation_are_audited(runtime):
    runtime.listening = True
    await runtime._transcribe('microphone:fixture', b'\0' * 9600, False, time.time(), 0)
    assert any(e['kind'] == 'metrics' and e['data'].get('final') is False for e in runtime.memory.events())
    started = asyncio.Event()
    async def blocked(*args):
        started.set(); await asyncio.Event().wait()
    runtime.providers.transcribe = blocked
    task = asyncio.create_task(runtime._transcribe('microphone:fixture', b'\0' * 9600, False, time.time(), 0))
    await started.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert any(e['data'].get('remote_usage') == 'unknown_after_cancellation' for e in runtime.memory.events())


async def test_audio_gap_invalidates_inflight_asr(runtime):
    runtime.listening = True
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args):
        started.set(); await release.wait()
        return {'text': '丢帧前的过时结果', 'usage': {}}
    runtime.providers.transcribe = delayed
    task = asyncio.create_task(runtime._transcribe('microphone:fixture', b'\0'*640, True, time.time(), 0))
    await started.wait()
    runtime.audio_queue = asyncio.Queue(maxsize=1)
    runtime._enqueue(AudioChunk('microphone:fixture', b'\0'*640))
    runtime._enqueue(AudioChunk('microphone:fixture', b'\0'*640))
    release.set(); await task
    assert not runtime.memory.history()
    runtime.consumer_task = asyncio.create_task(runtime._audio_consumer())
    for _ in range(10):
        if not runtime.audio_queue.qsize(): break
        await asyncio.sleep(.01)
    assert any(e['kind'] == 'audio_gap' for e in runtime.memory.events())


async def test_all_source_failure_stops_listening_but_stale_callback_does_not(runtime):
    runtime.listening = True; runtime.capture_generation = 5
    await runtime._capture_state({'kind': 'all_sources_stopped', 'source': 'fixture'}, 4)
    assert runtime.listening
    await runtime._capture_state({'kind': 'all_sources_stopped', 'source': 'fixture'}, 5)
    assert not runtime.listening


async def test_query_failure_falls_back_and_background_backoff_is_bounded(runtime):
    runtime.settings.update({'embedding': {'enabled': True}})
    config = runtime.settings.raw(); version = fingerprint(config['embedding'])
    record = runtime.memory.add_memory('过往事实')
    runtime.memory.save_vectors(record['id'], version, text_hash(record['text']), [[1, 0]])
    current = message(runtime, '过往事实是什么')
    assert await runtime._semantic_context(current, config) == []
    assert not runtime.background.ready('retrieval')
    delays = [runtime.background.failure('index') for _ in range(10)]
    assert delays[:3] == [5, 10, 20] and max(delays) == 300
    runtime.background.retry(); assert runtime.background.ready('index')


async def test_proactive_identical_observation_is_not_repeated(runtime):
    runtime.settings.update({'mode': 'proactive', 'allow_proactive': True})
    runtime.proactive_seen.append((time.time()-30, '重复观察'))
    observation = runtime.memory.add_message('observation', '重复观察。', 'system', runtime.session_id)
    await runtime._consider_proactive(observation, time.time())
    assert runtime.reply_task is None and runtime.providers.calls == 0
    assert runtime.memory.events()[0]['data']['reason'] == 'repeat_observation'


async def test_start_marks_abandoned_snapshots_without_replaying_requests(runtime):
    id = runtime.memory.save_snapshot('trace', 'response', {}, [], [])
    await runtime.start()
    assert runtime.memory.snapshot(id)['metadata']['outcome'] == 'interrupted_restart'
    assert runtime.providers.calls == 0
