"""Bounded cross-day acceptance with synthetic text. Real OpenRouter calls require --cloud.

Reports and synthetic documents stay in .runtime. Corrections represent submitted
text. Microphone and ASR acceptance use a separate device probe.
"""
import argparse
import asyncio
from datetime import date, datetime, time, timedelta, timezone
import json
from time import monotonic
from uuid import uuid4

from probe_research import ROOT, TERMINAL, MeasuredProviders, aggregate_requests, wait_for, write_json
from greatsage.continuation import preview_scope
from greatsage.runtime import Runtime
from greatsage.settings import read_env


SOURCES = ("已经决定首批只开放 Windows。", "顾言负责整理兼容清单。", "周五发布只是候选，发布日期仍待确认。")
CORRECTIONS = ("兼容清单负责人由顾言改为林岚。", "周五不是截止日期，发布日期待确认。")


def seed(runtime):
    day = date.today() - timedelta(days=1)
    point = datetime.combine(day, time(15)).astimezone(timezone.utc)
    memory = runtime.memory
    for index, sentence in enumerate(SOURCES):
        row = memory.add_message("user", sentence, "text", runtime.session_id)
        stamp = (point + timedelta(minutes=index)).isoformat(timespec="milliseconds")
        with memory._lock, memory._db:
            memory._db.execute("UPDATE messages SET created_at=? WHERE id=?", (stamp, row["id"]))
    memory.add_message("user", "今天另议 Linux 灰度。", "text", runtime.session_id)
    return day


async def run(model):
    key = read_env("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("OpenRouter credential unavailable")
    directory = ROOT / ".runtime" / "continuation-probe" / uuid4().hex
    directory.mkdir(parents=True)
    provider = MeasuredProviders(key)
    runtime = Runtime(directory, providers=provider)
    report = {"model": model, "stages": {}, "synthetic": True}
    try:
        runtime.settings.update({"voice_enabled": False, "memory_extraction_enabled": False,
            "llm": {"provider": "openrouter", "model": model, "context_tokens": 32768,
                    "temperature": 0, "max_tokens": 1600}})
        day = seed(runtime)
        session = runtime.session_id
        report["yesterday_count"] = preview_scope(runtime.memory, session, instructions="昨天")["message_count"]
        report["empty_day_count"] = preview_scope(runtime.memory, session, str(day - timedelta(days=1)))["message_count"]
        document = None
        for index, stage in enumerate(("brief", "owner", "date")):
            started = monotonic()
            provider.begin(stage, 2)
            if index == 0:
                task = await runtime.tasks.start_research({"profile": "continuation", "session_ids": [session],
                    "instructions": "昨天首轮给哪些用户开放？接下来谁来准备，排期敲定了吗？",
                    "save_path": "notes/brief.md"}, "console", session)
            else:
                task = await runtime.tasks.regenerate(document, CORRECTIONS[index - 1], expected_version=index)
            result = await wait_for(runtime, task["id"], TERMINAL | {"waiting_approval"}, 120)
            metrics = aggregate_requests(provider.stage_requests(stage), 2)
            report["stages"][stage] = {**metrics, "elapsed_ms": round((monotonic() - started) * 1000),
                                       "status": result["status"]}
            if result["status"] != "waiting_approval":
                report["stages"][stage]["error"] = result["error"]
                break
            call = next(c for c in runtime.tasks.approvals_pending() if c["task_id"] == task["id"])
            document = call["arguments"]["artifact_id"]
            item = runtime.memory.artifact(document)
            (directory / f"v{item['version']}.md").write_text(item["content"], encoding="utf-8")
            report["stages"][stage].update(version=item["version"], valid_sources=sum(c["valid"] for c in item["citations"]))
            await runtime.tasks.resolve(call["id"], stage == "date", via="click")
            await wait_for(runtime, task["id"], TERMINAL, 20)
            path = directory / "workspace" / call["arguments"]["path"]
            report["stages"][stage]["file_exists"] = path.exists()
            if path.exists():
                report["stages"][stage]["matches_export"] = path.read_text(encoding="utf-8") == runtime.memory.export_markdown(document)
    finally:
        await runtime.close()
        write_json(directory / "result.json", report)
        print(directory.relative_to(ROOT))
        print(json.dumps(report, ensure_ascii=True))
    return len(report["stages"]) == 3 and report["stages"]["date"].get("matches_export", False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", action="store_true")
    parser.add_argument("--model", default="openai/gpt-4.1")
    args = parser.parse_args()
    if args.cloud:
        raise SystemExit(0 if asyncio.run(run(args.model)) else 1)
    print("Offline plan: yesterday and empty-day scopes, owner correction, date negation, version-bound export.")
