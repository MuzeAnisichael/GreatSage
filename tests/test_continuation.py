"""Regression coverage for the source window and bounded edits of a continuation brief."""
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from greatsage import continuation
from greatsage.memory import MemoryStore
from greatsage.source_selection import SourceSelection


def test_local_calendar_and_empty_day_with_selected_markdown(tmp_path):
    store = MemoryStore(tmp_path / "data")
    try:
        session = store.new_session()
        other = store.new_session()
        yesterday = date.today() - timedelta(days=1)
        midnight = datetime.combine(yesterday, time.min)
        def insert(sid, words, when, role="user", source="text"):
            row = store.add_message(role, words, source, sid)
            with store._lock, store._db:
                stamp = when.astimezone(timezone.utc).isoformat(timespec="milliseconds")
                store._db.execute("UPDATE messages SET created_at=? WHERE id=?", (stamp, row["id"]))
            return row["id"]
        first = insert(session, "首批只开放 Windows。", midnight)
        last = insert(session, "顾言负责整理兼容清单。", midnight + timedelta(days=1, milliseconds=-1))
        insert(session, "今天已超出范围。", midnight + timedelta(days=1))
        insert(other, "其他会话。", midnight)
        insert(session, "模型生成内容。", midnight, role="assistant")
        insert(session, "其他文档的纠正。", midnight, source="artifact_correction")
        view = continuation.preview_scope(store, session, instructions="昨天敲定了什么？")
        assert view["local_date"] == yesterday.isoformat() and view["message_count"] == 2
        assert datetime.fromisoformat(view["first_at"]).hour == 0
        assert datetime.fromisoformat(view["last_at"]).hour == 23
        path = tmp_path / "release.md"
        path.write_text("## 发布\n术语 RC 指发布候选版。", encoding="utf-8")
        mid = store.import_materials(str(path))["materials"][0]["id"]
        selector = SourceSelection(SimpleNamespace(memory=store))
        params = {"profile": "continuation", "session_ids": [session], "material_ids": [mid],
                  "local_date": yesterday.isoformat()}
        frozen = selector.freeze(params)
        assert [row["id"] for row in frozen["sources"] if row["kind"] == "message"] == [first, last]
        assert frozen["materials"][0]["id"] == mid
        params["local_date"] = (yesterday - timedelta(days=1)).isoformat()
        assert continuation.preview_scope(store, session, params["local_date"])["message_count"] == 0
        with pytest.raises(ValueError, match="没有会话原文"):
            selector.freeze(params)
    finally:
        store.close()


def test_original_sentences_classification_citations_and_two_local_corrections():
    original = "首批只开放 Windows；顾言负责整理兼容清单；争取周五发布，尚待确认。建议先核对 RC 清单。"
    items = continuation.units([{"text": original, "handle": "S3"}])
    choices = {"items": [{"unit": 1, "section": "decision"}, {"unit": 2, "section": "next"},
                         {"unit": 3, "section": "decision"}, {"unit": 4, "section": "next"}]}
    scope = {"first_at": "2026-11-17T09:00:00+08:00", "last_at": "2026-11-17T10:00:00+08:00",
             "message_count": 1}
    first = continuation.render("接续", scope, choices, items)
    rows = continuation.positions(first)
    assert [row["section"] for row in rows] == ["decision", "next", "next", "open"]
    assert all("[S3]" in row["text"] for row in rows)
    assert "完成情况待确认" in rows[1]["text"] and rows[2]["text"].startswith("建议：")
    second = continuation.apply_patch(first, {"items": [{"target": 2, "old": "顾言", "new": "林岚",
        "section": "next"}]}, {"text": "兼容清单负责人改为林岚。", "handle": "S4"})
    patch = {"target": 4, "old": "争取周五发布，尚待确认。", "new": "周五不是截止日期，发布日期待确认。", "section": "open"}
    correction = {"text": "周五不是截止日期，发布日期待确认。", "handle": "S5"}
    third = continuation.apply_patch(second, {"items": [patch]}, correction)
    latest = continuation.positions(third)
    assert latest[0]["text"] == rows[0]["text"] and latest[2]["text"] == rows[2]["text"]
    assert "林岚" in latest[1]["text"] and "[S3] [S4]" in latest[1]["text"] and "顾言" not in third
    assert "周五不是截止日期" in latest[3]["text"] and "[S3] [S5]" in latest[3]["text"]
    with pytest.raises(ValueError, match="否定"):
        continuation.apply_patch(second, {"items": [{**patch, "new": "周五"}]}, correction)
    with pytest.raises(ValueError, match="明确替换文字"):
        continuation.apply_patch(second, {"items": [{**patch, "new": "星期四敲定"}]}, correction)
