"""Bounded comparison probe for minutes and research tasks.

The default mode is an offline plan: it does not read credentials, create a
runtime directory, or contact a provider.  Real inference requires the
explicit ``--cloud`` flag and uses the already configured OpenRouter key only
in memory.  All inputs are synthetic and every generated body remains under a
fresh ``.runtime/research-probe/<stamp>`` directory.

The research save approval is resolved through GreatSage's application click
entry point.  That is a programmatic acceptance check, not a human click and
not evidence that a person reviewed the generated document.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from greatsage.providers import ProviderError, Providers  # noqa: E402
from greatsage.runtime import Runtime  # noqa: E402
from greatsage.settings import SettingsStore  # noqa: E402
from probe_providers import safe_error  # noqa: E402


MODEL_DEFAULT = "openai/gpt-4.1-mini"
CONTEXT_DEFAULT = 32_768
MINUTES_REQUEST_LIMIT = 3  # One request plus the task's two bounded retries.
RESEARCH_REQUEST_LIMIT = 16  # Match one product attempt; three sources may be read serially.
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cost")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return value
    return None


class MeasuredProviders(Providers):
    """Inject one in-memory key, count real requests and retain safe usage only."""

    def __init__(self, api_key: str):
        super().__init__()
        self._api_key = api_key
        self.stage = ""
        self.limit = 0
        self.requests: list[dict] = []
        self.tool_responses: list[dict] = []

    def begin(self, stage: str, limit: int) -> None:
        self.stage, self.limit = stage, limit

    def stage_requests(self, stage: str) -> list[dict]:
        return [entry for entry in self.requests if entry["stage"] == stage]

    async def stream_chat(self, config, messages):
        if not self.stage or self.limit < 1:
            raise ProviderError("probe", "stage_not_started")
        existing = self.stage_requests(self.stage)
        if len(existing) >= self.limit:
            raise ProviderError("probe", "request_limit")
        entry = {
            "stage": self.stage,
            "request_index": len(existing) + 1,
            "usage": {},
            "first_text_ms": None,
        }
        self.requests.append(entry)
        started = time.perf_counter()
        try:
            # Never persist the credential in the isolated SettingsStore.
            request_config = {**config, "api_key": self._api_key}
            async for event in super().stream_chat(request_config, messages):
                if event.get("tool_calls"):
                    # Synthetic-only diagnostic, kept in the isolated ignored
                    # directory. Never copy config, headers or credentials.
                    self.tool_responses.append({"stage": self.stage, "request_index": entry["request_index"],
                                                "tool_calls": event["tool_calls"]})
                if event.get("text") and entry["first_text_ms"] is None:
                    entry["first_text_ms"] = round((time.perf_counter() - started) * 1000)
                usage = event.get("usage") or {}
                for key in USAGE_KEYS:
                    value = safe_number(usage.get(key))
                    if value is not None:
                        entry["usage"][key] = value
                yield event
        except Exception as exc:
            entry["error"] = safe_error(exc)
            raise
        finally:
            entry["latency_ms"] = round((time.perf_counter() - started) * 1000)


def aggregate_requests(requests: list[dict], limit: int) -> dict:
    usage, coverage = {}, {}
    for key in USAGE_KEYS:
        values = [entry.get("usage", {}).get(key) for entry in requests]
        reported = [value for value in values if safe_number(value) is not None]
        usage[key] = sum(reported) if reported else None
        coverage[key] = {"reported": len(reported), "requests": len(requests)}
    # Partial cost is not presented as total cost. This prevents unknown cost
    # from being rendered as zero or as an apparently complete bill.
    cost = usage["cost"] if requests and coverage["cost"]["reported"] == len(requests) else None
    return {
        "actual_request_count": len(requests),
        "request_limit": limit,
        "actual_usage": {
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "cost_usd": cost,
            "coverage": coverage,
        },
        "requests": requests,
    }


def task_summary(task: dict, elapsed_ms: int, measured: dict) -> dict:
    steps = []
    for step in task.get("steps", []):
        calls = step.get("detail", {}).get("calls", [])
        steps.append({
            "name": step.get("name"),
            "status": step.get("status"),
            "model_call_records": len(calls) if isinstance(calls, list) else 0,
        })
    return {
        "id": task.get("id"),
        "kind": task.get("kind"),
        "status": task.get("status"),
        "elapsed_ms": elapsed_ms,
        "has_error": bool(task.get("error")),
        "steps": steps,
        **measured,
    }


def line_positions(text: str) -> dict:
    lines = text.splitlines()

    def containing(*needles: str) -> list[int]:
        lowered = tuple(needle.casefold() for needle in needles)
        return [index for index, line in enumerate(lines, 1)
                if any(needle in line.casefold() for needle in lowered)]

    return {
        "citation_marker_lines": [index for index, line in enumerate(lines, 1)
                                  if re.search(r"\[S\d{1,4}\]", line)],
        "date_2032_06_14_lines": containing("2032-06-14", "2032年6月14日"),
        "date_2032_06_21_lines": containing("2032-06-21", "2032年6月21日"),
        "owner_lin_lan_lines": containing("林岚"),
        "conflict_or_uncertainty_lines": containing(
            "分歧", "冲突", "不一致", "尚未确认", "待确认", "来源有分歧",
            "conflict", "uncertain", "unconfirmed", "inconsistent"),
    }


def artifact_summary(artifact: dict, exported: Path, run_root: Path) -> dict:
    citations = artifact.get("citations", [])
    text = exported.read_text(encoding="utf-8") if exported.exists() else ""
    return {
        "kind": artifact.get("kind"),
        "version": artifact.get("version"),
        "path": exported.relative_to(run_root).as_posix(),
        "citation_count": len(citations),
        "valid_citation_count": sum(bool(item.get("valid")) for item in citations),
        "unknown_marker_count": len(artifact.get("unknown_markers", [])),
        "manual_review_lines": line_positions(text),
    }


async def wait_for(runtime: Runtime, task_id: str, statuses: set[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = runtime.memory.task(task_id)
        if task["status"] in statuses:
            return task
        await asyncio.sleep(0.05)
    raise TimeoutError("bounded task wait timed out")


def create_fixtures(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    plan = directory / "firefly-plan.md"
    update = directory / "firefly-update.md"
    plan.write_text(
        "# 虚构项目：萤火计划\n\n"
        "## 初始里程碑\n\n"
        "仅供验收的虚构记录：计划发布日期为 2032-06-14。\n\n"
        "## 文档责任\n\n"
        "仅供验收的虚构记录：文档负责人是林岚。\n",
        encoding="utf-8",
    )
    update.write_text(
        "# 虚构项目：萤火计划\n\n"
        "## 修订说明\n\n"
        "仅供验收的虚构记录：计划发布日期为 2032-06-21，日期尚未确认。\n",
        encoding="utf-8",
    )
    return [plan, update]


def dry_run_plan(args) -> dict:
    return {
        "mode": "dry_run",
        "network": False,
        "credentials_read": False,
        "directories_created": False,
        "provider": "openrouter",
        "model": args.model,
        "context_tokens": args.context_tokens,
        "synthetic_sources": {
            "markdown_files": 2,
            "expected_chunks": "2-4",
            "facts": ["conflicting fictional dates", "one fictional document owner"],
        },
        "sequence": [
            {"kind": "minutes", "document": False, "request_limit": MINUTES_REQUEST_LIMIT},
            {"kind": "research", "request_limit": RESEARCH_REQUEST_LIMIT,
             "save_approval": "programmatic application click entry point; not a human click"},
        ],
        "isolation": ".runtime/research-probe/<UTC stamp>-<random>",
        "retry_policy": "The probe never reruns a paid task; existing bounded task retries remain enabled.",
        "next_step": "Pass --cloud to run once with the configured OpenRouter credential.",
    }


async def run_cloud(args) -> tuple[bool, dict, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    run_root = ROOT / ".runtime" / "research-probe" / stamp
    report_path = run_root / "report.json"
    report = {
        "schema": 1,
        "started_at": utc_now(),
        "mode": "cloud",
        "isolation": str(run_root),
        "provider": "openrouter",
        "model": args.model,
        "context_tokens": args.context_tokens,
        "synthetic_only": True,
        "probe_retry_policy": "none; each task is started once",
        "application_task_retries": "existing bounded retries are unchanged",
        "tasks": {},
        "status": "running",
    }
    run_root.mkdir(parents=True, exist_ok=False)
    write_json(report_path, report)

    runtime = None
    active_ids: list[str] = []
    provider = None
    try:
        source = SettingsStore(Path(args.settings_dir)).provider("llm")
        if source.get("provider") != "openrouter":
            raise ValueError("bounded cloud probe requires configured OpenRouter")
        api_key = source.pop("api_key", "")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("configured OpenRouter credential is unavailable")

        provider = MeasuredProviders(api_key.strip())
        runtime = Runtime(run_root, providers=provider)
        runtime._schedule_compression = lambda: None
        runtime.settings.update({
            "global_prompt": (
                "你正在执行隔离的 GreatSage 验收。只处理选定的虚构萤火计划资料；"
                "保持来源日期分歧，不得自行调和，不得引入外部事实。"
            ),
            "output_language": "zh-CN",
            "voice_enabled": False,
            "semantic_decisions": False,
            "audit_content": False,
            "memory_extraction_enabled": False,
            "embedding": {"enabled": False},
            "llm": {
                "provider": "openrouter",
                "base_url": source.get("base_url", "https://openrouter.ai/api/v1"),
                "model": args.model,
                "api_key_env": "",
                "context_tokens": args.context_tokens,
                "max_tokens": args.max_tokens,
                "temperature": 0,
                "timeout_seconds": min(300, max(1, args.request_timeout)),
                "tokenizer": "auto",
            },
        })

        fixtures = create_fixtures(run_root / "fixtures")
        imported = runtime.memory.import_materials(str(run_root / "fixtures"))
        if imported["errors"] or len(imported["materials"]) != len(fixtures):
            raise RuntimeError("synthetic fixture import failed")
        material_ids = [item["id"] for item in imported["materials"]]
        chunks = runtime.memory.material_chunks(material_ids=material_ids)
        if not 2 <= len(chunks) <= 4:
            raise RuntimeError("synthetic fixture chunk count is outside the bounded range")
        report["fixtures"] = {
            "files": [path.relative_to(run_root).as_posix() for path in fixtures],
            "material_count": len(material_ids),
            "chunk_count": len(chunks),
            "contains_personal_history": False,
        }

        instructions = (
            "仅依据选定的虚构资料，列出两个互相冲突的发布日期和文档负责人。"
            "不要把日期差异静默合并；应明确标为分歧或待确认，并保留可核对的来源引用。"
        )

        provider.begin("minutes", MINUTES_REQUEST_LIMIT)
        started = time.perf_counter()
        minutes = await runtime.tasks.start_minutes({
            "title": "虚构萤火计划纪要对照",
            "instructions": instructions,
            "material_ids": material_ids,
            "session_ids": [],
            "use_session": False,
            "document": False,
        }, "console", runtime.session_id)
        active_ids.append(minutes["id"])
        minutes_task = await wait_for(runtime, minutes["id"], TERMINAL, args.timeout)
        minutes_elapsed = round((time.perf_counter() - started) * 1000)
        minute_requests = provider.stage_requests("minutes")
        report["tasks"]["minutes"] = task_summary(
            minutes_task,
            minutes_elapsed,
            aggregate_requests(minute_requests, MINUTES_REQUEST_LIMIT),
        )
        if minutes_task["status"] == "succeeded":
            minute_rows = runtime.memory.artifacts(task_id=minutes["id"])
            minute_row = next((item for item in minute_rows if item["kind"] == "minutes"), None)
            if minute_row is None:
                raise RuntimeError("minutes artifact is missing")
            minute_artifact = runtime.memory.artifact(minute_row["id"])
            minute_export = run_root / "review" / "minutes.md"
            minute_export.parent.mkdir(parents=True, exist_ok=True)
            minute_export.write_text(runtime.memory.export_markdown(minute_row["id"]), encoding="utf-8")
            report["tasks"]["minutes"]["artifact"] = artifact_summary(
                minute_artifact, minute_export, run_root)
        write_json(report_path, report)

        provider.begin("research", RESEARCH_REQUEST_LIMIT)
        started = time.perf_counter()
        research = await runtime.tasks.start_research({
            "title": "虚构萤火计划资料核对",
            "instructions": instructions,
            "material_ids": material_ids,
            "session_ids": [],
            "use_session": False,
            "save_path": "review/research.md",
            "overwrite": False,
        }, "console", runtime.session_id)
        active_ids.append(research["id"])
        research_wait = await wait_for(
            runtime, research["id"], TERMINAL | {"waiting_approval"}, args.timeout)
        approval_record = {
            "observed_waiting_approval": research_wait["status"] == "waiting_approval",
            "method": "Runtime.tasks.resolve(call_id, True, 'once', 'click')",
            "programmatic": True,
            "human_click": False,
            "resolved_count": 0,
        }
        if research_wait["status"] == "waiting_approval":
            pending = [item for item in runtime.tasks.approvals_pending()
                       if item["task_id"] == research["id"]]
            if len(pending) != 1:
                raise RuntimeError("research did not expose exactly one save approval")
            view = pending[0]
            ledger = runtime.memory.tool_call(view["id"])
            if (view.get("tool") != "export_artifact" or view.get("voice") is not False
                    or view.get("task_scope") is not False or ledger.get("status") != "awaiting_approval"
                    or ledger.get("result") is not None):
                raise RuntimeError("research approval boundary is invalid")
            target = run_root / "workspace" / "review" / "research.md"
            if target.exists():
                raise RuntimeError("research document was saved before approval")
            await runtime.tasks.resolve(view["id"], True, "once", "click")
            approval_record.update(call_id=view["id"], resolved_count=1)

        research_task = await wait_for(runtime, research["id"], TERMINAL, args.timeout)
        research_elapsed = round((time.perf_counter() - started) * 1000)
        research_requests = provider.stage_requests("research")
        report["tasks"]["research"] = task_summary(
            research_task,
            research_elapsed,
            aggregate_requests(research_requests, RESEARCH_REQUEST_LIMIT),
        )
        final_save_calls = [call for call in research_task.get("tool_calls", [])
                            if call.get("tool") == "export_artifact"]
        approval_record["ledger"] = {
            "save_call_count": len(final_save_calls),
            "status": final_save_calls[0].get("status") if len(final_save_calls) == 1 else None,
            "scope": (final_save_calls[0].get("approval") or {}).get("scope")
            if len(final_save_calls) == 1 else None,
            "via": (final_save_calls[0].get("approval") or {}).get("via")
            if len(final_save_calls) == 1 else None,
        }
        report["tasks"]["research"]["save_approval"] = approval_record

        if research_task["artifacts"]:
            document_row = next((item for item in research_task["artifacts"] if item["kind"] == "document"), None)
            if document_row is not None:
                document = runtime.memory.artifact(document_row["id"])
                saved = run_root / "workspace" / "review" / "research.md"
                if saved.exists():
                    report["tasks"]["research"]["artifact"] = artifact_summary(
                        document, saved, run_root)

        minutes_ok = report["tasks"]["minutes"]["status"] == "succeeded"
        research_ok = report["tasks"]["research"]["status"] == "succeeded"
        research_artifact = report["tasks"]["research"].get("artifact", {})
        structural_ok = (
            minutes_ok
            and research_ok
            and approval_record["resolved_count"] == 1
            and approval_record["ledger"] == {
                "save_call_count": 1, "status": "succeeded", "scope": "once", "via": "click"}
            and research_artifact.get("citation_count", 0) > 0
            and research_artifact.get("citation_count") == research_artifact.get("valid_citation_count")
            and research_artifact.get("unknown_marker_count") == 0
        )
        report["manual_review"] = {
            "required": True,
            "note": "模型核对不等于事实认证；程序审批也不等于人工审阅。",
            "inspect": [
                "review/minutes.md：核对引用、两个冲突日期、林岚负责人和未静默调和的措辞。",
                "workspace/review/research.md：核对来源表、分歧/待确认段、负责人归属和引用有效性。",
            ],
            "positions": {
                name: task.get("artifact", {}).get("manual_review_lines", {})
                for name, task in report["tasks"].items()
            },
        }
        report["status"] = "completed_manual_review_required" if structural_ok else "failed"
        report["structural_checks_passed"] = structural_ok
        return structural_ok, report, report_path
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = safe_error(exc)
        return False, report, report_path
    finally:
        if runtime is not None:
            for task_id in active_ids:
                try:
                    if runtime.memory.task(task_id)["status"] not in TERMINAL:
                        await runtime.tasks.cancel(task_id)
                except (KeyError, ValueError):
                    pass
            await runtime.close()
        report["finished_at"] = utc_now()
        if provider is not None:
            write_json(run_root / "tool-responses.json", {"synthetic_only": True, "responses": provider.tool_responses})
        if provider is not None:
            report["actual_request_count"] = len(provider.requests)
        write_json(report_path, report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cloud", action="store_true",
                      help="opt in to one bounded OpenRouter comparison run")
    mode.add_argument("--dry-run", action="store_true",
                      help="print the offline plan (this is also the default)")
    parser.add_argument("--settings-dir", default=str(ROOT / ".runtime"),
                        help="existing GreatSage settings used only to read the OpenRouter credential")
    parser.add_argument("--model", default=MODEL_DEFAULT)
    parser.add_argument("--context-tokens", type=int, default=CONTEXT_DEFAULT)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--request-timeout", type=float, default=180,
                        help="per provider request timeout in seconds (1-300)")
    parser.add_argument("--timeout", type=float, default=600,
                        help="maximum wait for each task or approval state")
    return parser


def validate_args(parser: argparse.ArgumentParser, args) -> None:
    if not args.model.strip() or len(args.model) > 256:
        parser.error("--model must be 1-256 characters")
    if not 1024 <= args.context_tokens <= 2_000_000:
        parser.error("--context-tokens must be between 1024 and 2000000")
    if not 64 <= args.max_tokens < args.context_tokens - 256:
        parser.error("--max-tokens must leave at least 256 context tokens")
    if not 1 <= args.request_timeout <= 300:
        parser.error("--request-timeout must be between 1 and 300 seconds")
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout must be between 1 and 3600 seconds")


if __name__ == "__main__":
    argument_parser = build_parser()
    arguments = argument_parser.parse_args()
    validate_args(argument_parser, arguments)
    if not arguments.cloud:
        print(json.dumps(dry_run_plan(arguments), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    try:
        passed, final_report, output = asyncio.run(run_cloud(arguments))
        # The stdout summary contains no source/model text, URL, environment
        # name or credential. Full generated documents stay in the isolated run.
        print(json.dumps({
            "status": final_report.get("status"),
            "report": str(output),
            "model": final_report.get("model"),
            "actual_request_count": final_report.get("actual_request_count"),
            "manual_review_required": True,
        }, ensure_ascii=False, indent=2))
        raise SystemExit(0 if passed else 1)
    except Exception as exc:
        print(json.dumps({"probe_error": safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
