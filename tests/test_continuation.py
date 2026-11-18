"""Regression coverage for the source window and bounded edits of a continuation brief."""
from datetime import date, datetime, time, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from greatsage import continuation
from greatsage.memory import MemoryStore
from greatsage.research import ResearchWorkflow
from greatsage.source_selection import SourceSelection
from greatsage.tasks import TaskManager


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


async def test_explicit_refresh_replays_surviving_corrections_after_freezing_new_sources(tmp_path):
    store = MemoryStore(tmp_path / "data")
    sid = store.new_session()
    try:
        manager = SimpleNamespace(memory=store, runtime=SimpleNamespace(data_dir=tmp_path, session_id=sid,
            _session_switching=False), _schedule=lambda _: None, changed=AsyncMock(), _progress=AsyncMock(),
            _llm=lambda _: {"max_tokens": 1536, "context_tokens": 65536}, _budget=lambda _: 63000)
        manager._records = TaskManager._records.__get__(manager)
        manager._messages = TaskManager._messages.__get__(manager)
        workflow = ResearchWorkflow(manager)
        original = store.add_message("user", "顾言负责清单。周五暂定。", "text", sid)
        yesterday = date.today() - timedelta(days=1)
        stamp = datetime.combine(yesterday, time(10)).astimezone(timezone.utc).isoformat()
        with store._db:
            store._db.execute("UPDATE messages SET created_at=? WHERE id=?", (stamp, original["id"]))
        path = tmp_path / "selected.md"
        path.write_text("第一批。", encoding="utf-8")
        material = store.import_materials(str(path))["materials"][0]
        initial = await workflow.start({"profile": "continuation", "material_ids": [material["id"]],
            "local_date": yesterday.isoformat()}, "console", sid)
        sources = {"S1": {"id": original["id"], "kind": "message", "label": "原文", "excerpt": "顾言负责清单。"}}
        draft = store.create_artifact("document", "简报", "## 下一步\n- 顾言负责清单。 [S1]\n", sources, initial["id"])
        first = await workflow.revise(draft["id"], "负责人改为林岚。", 1)
        correction = store.task(first["id"])["params"]["correction_source_id"]
        store.commit_revision(draft["id"], 1, first["id"], "## 下一步\n- 林岚负责清单。 [S1] [S2]\n",
            {**sources, "S2": {"id": correction, "kind": "message", "label": "用户纠正", "excerpt": "负责人改为林岚。"}},
            correction)
        path.write_text("第二批。", encoding="utf-8")
        store.rescan_materials()
        with pytest.raises(ValueError, match="来源已变化"):
            await workflow.revise(draft["id"], "周五不是截止日期。", 2)
        next_task = await workflow.revise(draft["id"], "周五不是截止日期。", 2, update_sources=True)
        task = store.task(next_task["id"])
        new = task["params"]["correction_source_id"]
        manifest = task["params"]["source_scope"]
        assert manifest["time_range"]["message_count"] == 1 and manifest["correction_ids"] == [correction, new]
        collected = await workflow.collect(task)
        assert [r["id"] for r in collected["retained_sources"]] == [correction, new]
        requests = []
        async def model_json(task, step, purpose, messages, config, definition, ids):
            payload = json.loads(messages[-1]["content"])
            requests.append((purpose, ids))
            if purpose == "continuation_brief":
                assert [i["text"] for i in payload["units"]] == ["顾言负责清单。", "周五暂定。", "第二批。"]
                return {"items": [{"unit": 1, "section": "next"}, {"unit": 2, "section": "open"}]}
            target, old, value, section = (1, "顾言", "林岚", "next") if len(requests) == 2 else (
                2, "周五暂定。", "周五不是截止日期。", "open")
            return {"items": [{"target": target, "old": old, "new": value, "section": section}]}
        manager._model_json = model_json
        text = await continuation.generate(manager, task, workflow.records(collected), store.artifact(draft["id"]), 24)
        assert "林岚负责清单。" in text and "周五不是截止日期。" in text
        assert set(requests[-1][1]) == {original["id"], correction, new}
        store.delete_message(original["id"])
        with pytest.raises(ValueError, match="冻结的来源已变化"):
            workflow.check_brief_scope(task)
    finally:
        store.close()
