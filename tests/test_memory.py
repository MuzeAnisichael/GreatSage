import json
import threading

import pytest

from greatsage.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    memory = MemoryStore(tmp_path)
    yield memory
    memory.close()


def test_restart_preserves_sessions_explicit_memories_and_source_identity(tmp_path):
    first = MemoryStore(tmp_path)
    session = first.new_session()
    message = first.add_message("user", "我喜欢绿茶", "microphone", session, "trace-1")
    assistant = first.add_message("assistant", "我猜你喜欢红茶", "model", session)
    first.add_memory("用户明确偏好绿茶", [message["id"]])
    first.close()
    second = MemoryStore(tmp_path)
    try:
        assert second.sessions()[0]["message_count"] == 2
        assert [item["role"] for item in second.history(session_id=session)] == ["user", "assistant"]
        assert second.search("喜欢绿茶")[0]["id"] == message["id"]
        assert second.search("红茶")[0]["id"] == assistant["id"]
        assert len(second.list_memories()) == 1
        assert second.list_memories()[0]["origin"] == "user_explicit"
    finally:
        second.close()


def test_chinese_and_english_search_handles_long_questions_and_fts_punctuation(store):
    session = store.new_session()
    a = store.add_message("user", "项目明天下午三点在杭州开会", session_id=session)
    b = store.add_message("user", "The release checkpoint is Friday", session_id=session)
    assert store.search("杭州会议什么时候开始")[0]["id"] == a["id"]
    assert store.search('"Friday" OR * ???')[0]["id"] == b["id"]
    assert store.search("!!!") == []


def test_delete_removes_transitive_derivatives_and_audit_body_copies(store):
    session = store.new_session()
    a = store.add_message("user", "秘密项目编号七七九", session_id=session, trace_id="secret-trace")
    b = store.add_message("user", "保留这一句话", session_id=session)
    segment = store.save_summary("七七九项目待跟进", [a["id"]], model="test")
    session_summary = store.save_summary("本次会话提及七七九", [segment["id"]])
    store.add_memory("项目编号七七九", [session_summary["id"]])
    retained = store.add_memory("我的名字是小林")
    store.add_event("context", {"prompt": [a, segment, session_summary]}, "different-trace")
    store.add_event("model", {"output": "derived private output"}, "secret-trace")
    store.delete_message(a["id"])
    assert store.search("七七九") == []
    assert store.summaries() == []
    assert [memory["id"] for memory in store.list_memories()] == [retained["id"]]
    assert [message["id"] for message in store.history()] == [b["id"]]
    assert all(event["data"].get("redacted") for event in store.events())
    with pytest.raises(ValueError, match="no longer exists"):
        store.save_summary("不能恢复内容", [a["id"]])


def test_background_summary_cannot_commit_after_source_deletion(store):
    a = store.add_message("user", "稍后删除的转写")
    ready, resume = threading.Event(), threading.Event()
    errors = []

    def compressor():
        ready.set()
        assert resume.wait(timeout=5)
        try:
            store.save_summary("过时的压缩结果", [a["id"]])
        except ValueError as error:
            errors.append(error)

    thread = threading.Thread(target=compressor)
    thread.start()
    assert ready.wait(timeout=5)
    store.delete_message(a["id"])
    resume.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors and not store.summaries()


def test_deletion_removes_derived_assistant_outputs_and_blocks_late_replies(store):
    original = store.add_message("user", "我的项目名叫海鸥")
    preference = store.add_memory("使用中文答复")
    response = store.add_message("assistant", "海鸥项目已记录", trace_id="response-trace",
                                 metadata={"source_ids": [original["id"], preference["id"]]})
    store.add_event("model.completed", {"reply": "响应的其他私密资料"}, "response-trace")
    store.delete_message(original["id"])
    assert not store.history()
    assert not store.search("海鸥")
    assert store.events()[0]["data"]["redacted"]
    assert store.list_memories()[0]["id"] == preference["id"]
    with pytest.raises(ValueError, match="no longer exists"):
        store.add_message("assistant", "过时回答", metadata={"source_ids": [original["id"]]})
    with pytest.raises(ValueError, match="no longer exists"):
        store.save_summary("过时摘要", [response["id"]])


def test_revision_replaces_id_and_invalidates_old_summaries(store):
    a = store.add_message("user", "会议三点开始")
    store.save_summary("三点开会", [a["id"]])
    b = store.revise_message(a["id"], "会议四点开始")
    assert b["id"] != a["id"]
    assert b["metadata"]["revision_of"] == a["id"]
    assert b["metadata"]["version"] == 2
    assert not store.summaries()
    assert not store.search("三点")
    with pytest.raises(ValueError):
        store.save_summary("生成过程中旧值", [a["id"]])
    memory = store.add_memory("姓名小林", [b["id"]])
    revision = store.revise_memory(memory["id"], "姓名小琳")
    assert revision["version"] == 2
    assert revision["revision_of"] == memory["id"]
    assert store.list_memories()[0]["text"] == "姓名小琳"


def test_failed_revision_rolls_back_original_and_derived_records(store, monkeypatch):
    original = store.add_message("user", "原来的记录")
    summary = store.save_summary("原来的摘要", [original["id"]])

    def fail(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(store, "_insert_message", fail)
    with pytest.raises(RuntimeError):
        store.revise_message(original["id"], "未成功写入的修正")
    assert store.history()[0]["id"] == original["id"]
    assert store.summaries()[0]["id"] == summary["id"]
    assert store.search("原来的记录")[0]["id"] == original["id"]


def test_context_deduplicates_layered_summaries(store):
    earlier_session = store.new_session()
    original = store.add_message("user", "长期保留的一次谈话", session_id=earlier_session)
    segment = store.save_summary("较早片段", [original["id"]])
    store.save_summary("较早会话", [segment["id"]])
    context = store.context("无关的新主题", store.new_session())
    assert len(context["summaries"]) == 1


def test_compression_keeps_recent_and_is_idempotent(store):
    session = store.new_session()
    messages = [store.add_message("user", f"语音片段 {i}", session_id=session) for i in range(20)]
    candidates = store.compression_candidates(session)
    assert len(candidates) == 8
    ids = [item["id"] for item in candidates]
    first = store.save_summary("早期内容摘要", ids, "local-model", "v2")
    assert first["source_chars"] == sum(len(item["text"]) for item in candidates)
    assert first["prompt_version"] == "v2"
    assert first == store.save_summary("重复任务返回现有摘要", ids)
    assert not store.compression_candidates(session)
    assert len(store.compression_candidates(session, keep_recent=0)) == len(messages) - 8


def test_context_fits_serialized_budget_and_retrieves_earlier_sessions(store):
    old = store.new_session()
    message = store.add_message("user", "杭州会议在六月十九日召开", session_id=old)
    store.add_memory("用户偏好使用中文")
    current = store.new_session()
    for i in range(15):
        store.add_message("user", f"当前转写 {i} " + "长内容" * 500, session_id=current)
    context = store.context("杭州会议何时", current, max_chars=5000)
    assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) <= 5000
    assert context["recent"]
    assert any(item["id"] == message["id"] for item in context["retrieved"])
    ids = [item["id"] for key in ("recent", "retrieved") for item in context[key]]
    assert len(ids) == len(set(ids))
    for budget in (100, 250, 500):
        assert len(json.dumps(store.context("杭州", current, budget), ensure_ascii=False, separators=(",", ":"))) <= budget


def test_clear_history_does_not_revive_derived_memories_after_restart(tmp_path):
    memory = MemoryStore(tmp_path)
    original = memory.add_message("user", "需要遗忘的会话")
    memory.add_memory("派生事实", [original["id"]])
    explicit = memory.add_memory("独立保存的偏好")
    memory.save_summary("旧会话摘要", [original["id"]])
    memory.add_event("history", {"text": original["text"]})
    memory.clear_history()
    memory.close()
    memory = MemoryStore(tmp_path)
    try:
        assert not memory.history() and not memory.search("遗忘")
        assert not memory.events() and not memory.summaries()
        assert memory.list_memories()[0]["id"] == explicit["id"]
    finally:
        memory.close()
