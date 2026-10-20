"""Opt-in real model/tool acceptance using isolated, non-personal data.

Run --cloud --mcp-learn for one public Microsoft Learn search, and/or --ollama
for one read of a freshly generated fixture. Each case allows at most two model
requests and one approved tool. The harness calls the application's click
approval entry point; it does not simulate a physical mouse or audio input.
Credentials are read from existing settings/environment, never copied to disk.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
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
from greatsage.tools import SPECS  # noqa: E402
from probe_providers import safe_error  # noqa: E402

LEARN_URL = "https://learn.microsoft.com/api/mcp"
LEARN_QUERY = "PowerShell Get-Process command documentation"


class MeasuredProviders(Providers):
    """Count actual adapter calls, retain usage, and stop unexpected extra turns."""

    def __init__(self, credentials: dict):
        super().__init__()
        self.credentials, self.requests = credentials, []

    async def stream_chat(self, config, messages):
        if len(self.requests) >= 2:
            raise ProviderError("probe", "request_limit")
        entry = {"usage": {}, "first_text_ms": None, "tool_result_in_input": any(
            item.get("role") == "tool" for item in messages)}
        self.requests.append(entry)
        started = time.perf_counter()
        try:
            config = {**config, "api_key": self.credentials.get("api_key", "")}
            async for item in super().stream_chat(config, messages):
                if item.get("text") and entry["first_text_ms"] is None:
                    entry["first_text_ms"] = round((time.perf_counter() - started) * 1000)
                entry["usage"].update(item.get("usage", {}))
                if item.get("tool_calls"):
                    entry["native_tool_calls"] = [
                        {"name": call["name"], "arguments": call["arguments"]}
                        for call in item["tool_calls"]]
                yield item
        except Exception as exc:
            entry["error"] = safe_error(exc)
            raise
        finally:
            entry["latency_ms"] = round((time.perf_counter() - started) * 1000)


async def run_case(kind: str, args, directory: Path) -> dict:
    source = SettingsStore(Path(args.settings_dir)).provider("llm") if kind == "cloud" else {
        "provider": "ollama", "base_url": "http://127.0.0.1:11434",
        "model": args.local_model, "api_key_env": ""}
    if kind == "cloud" and source["provider"] != "openrouter":
        raise ValueError("This bounded cloud probe requires configured OpenRouter")
    if kind == "cloud" and args.cloud_model:
        source["model"] = args.cloud_model
    provider = MeasuredProviders(source)
    runtime = Runtime(directory, providers=provider)
    # No background extraction, recordings, speech or unrelated paid requests.
    runtime._schedule_compression = lambda: None
    result = {"kind": kind, "provider": source["provider"], "model": source["model"],
              "status": "failed", "approval": "application_click_entry_point",
              "requests": provider.requests, "fixture": "synthetic_file",
              "max_tokens": args.max_tokens, "context_tokens": args.context_tokens}
    task_id, approved = None, []
    started = time.perf_counter()
    try:
        runtime.settings.update({
            "voice_enabled": False, "semantic_decisions": False,
            "embedding": {"enabled": False}, "audit_content": False,
            "llm": {**{key: source[key] for key in ("provider", "base_url", "model", "api_key_env")},
                    "max_tokens": args.max_tokens, "context_tokens": args.context_tokens,
                    "temperature": 0, "timeout_seconds": 90},
        })
        marker = "GS-" + uuid.uuid4().hex[:12]
        expected_name, expected_arguments = "read_file", {"path": "fixture.md"}
        fixture = directory / "workspace" / "fixture.md"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(f"# Fictional acceptance fixture\nVerification code: {marker}\n", encoding="utf-8")
        prompt = ("Use read_file exactly once to read fixture.md. After its result, answer only "
                  "with the verification code found in the file. Do not guess or call other tools. /no_think")
        if kind == "cloud" and args.mcp_learn:
            runtime.settings.update({"mcp_servers": [{"id": "learn", "url": LEARN_URL, "token_env": ""}]})
            discovery_started = time.perf_counter()
            await runtime.tasks.mcp.discover("learn")
            entries = runtime.tasks.mcp.entries
            result["mcp"] = {
                "url": LEARN_URL, "discovery_ms": round((time.perf_counter() - discovery_started) * 1000),
                "tools": [{"remote_name": entry["remote_name"], "version": entry["version"],
                           "schema": entry["spec"].parameters} for entry in entries.values()],
            }
            expected_name = next(name for name, entry in entries.items()
                                 if entry["remote_name"] == "microsoft_docs_search")
            expected_arguments = {"query": LEARN_QUERY}
            prompt = (f"Use {expected_name} exactly once with query {json.dumps(LEARN_QUERY)}. "
                      "After its result, answer briefly in Chinese what Get-Process does and cite one "
                      "returned learn.microsoft.com URL. Do not call any other tool or run commands.")
            result["fixture"] = "public_microsoft_learn_query"
        allowed = {expected_name}
        available = set(SPECS) | set(runtime.tasks.mcp.specs)
        runtime.settings.update({"tool_rules": [
            {"tool": name, "match": "*", "action": "ask" if name in allowed else "deny"}
            for name in available]})
        submitted = time.perf_counter()
        await runtime.chat(prompt)
        deadline = time.perf_counter() + args.timeout
        while time.perf_counter() < deadline:
            for pending in runtime.tasks.approvals_pending():
                call = runtime.memory.tool_call(pending["id"])
                if approved or call["tool"] != expected_name or call["arguments"] != expected_arguments:
                    await runtime.tasks.resolve(call["id"], False, "once", "click")
                    raise RuntimeError("Unexpected proposal; denied without execution")
                if call["status"] != "awaiting_approval" or call["result"] is not None:
                    raise RuntimeError("Tool did not pause before execution")
                result["proposal_ms"] = round((time.perf_counter() - submitted) * 1000)
                result["approval_wait_observed"] = True
                task_id = call["task_id"]
                approved.append(call["id"])
                await runtime.tasks.resolve(call["id"], True, "once", "click")
            tasks = runtime.memory.tasks()
            if tasks:
                task_id = tasks[0]["id"]
                task = runtime.memory.task(task_id)
                if task["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
                    break
            elif runtime.reply_task is None:
                raise RuntimeError("Model did not propose a native tool")
            await asyncio.sleep(.03)
        else:
            raise TimeoutError("Tool acceptance timed out")
        task = runtime.memory.task(task_id)
        calls = task["tool_calls"]
        # Successful tasks purge intermediate bodies; the durable reply carries
        # task_id and source links in message metadata.
        replies = [message for message in runtime.memory.history(session_id=runtime.session_id)
                   if message["role"] == "assistant" and message["metadata"].get("task_id") == task_id]
        answer = replies[-1]["text"] if replies else ""
        valid = ("Get-Process" in answer and "learn.microsoft.com" in answer) if (
            kind == "cloud" and args.mcp_learn) else marker in answer
        result.update(task_status=task["status"], answer=answer[:1000], answer_valid=valid,
                      tool_calls=[{"tool": call["tool"], "status": call["status"],
                                   "approval": call["approval"], "target": call["target"]} for call in calls])
        if len(calls) == 1 and calls[0].get("result") is not None:
            raw = json.dumps(calls[0]["result"], ensure_ascii=False).encode("utf-8")
            result["tool_result"] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                                     "has_learn_url": b"learn.microsoft.com" in raw}
        if not (task["status"] == "succeeded" and valid and len(approved) == len(calls) == 1
                and calls[0]["status"] == "succeeded" and len(provider.requests) == 2
                and provider.requests[1]["tool_result_in_input"]):
            raise RuntimeError("Tool acceptance assertions failed")
        result["status"] = "passed"
    except Exception as exc:
        result["error"] = safe_error(exc)
    finally:
        if task_id:
            task = runtime.memory.task(task_id)
            if task["status"] not in ("succeeded", "failed", "cancelled"):
                await runtime.tasks.cancel(task_id)
        await runtime.close()
        result["total_ms"] = round((time.perf_counter() - started) * 1000)
        costs = [entry["usage"].get("cost") for entry in provider.requests]
        result["reported_cost_usd"] = sum(costs) if costs and all(
            isinstance(value, (int, float)) for value in costs) else None
        (directory / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


async def main(args) -> bool:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    root = ROOT / ".runtime" / "probe-agent-tools" / run_id
    report = {"date": datetime.now(timezone.utc).isoformat(), "versions": {
        package: importlib.metadata.version(package)
        for package in ("langchain-openai", "langchain-ollama", "langgraph", "mcp", "httpx2")},
        "cases": []}
    for kind in ("cloud", "ollama"):
        if getattr(args, kind):
            report["cases"].append(await run_case(kind, args, root / kind))
            (root / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **report}, ensure_ascii=False, indent=2))
    return all(case["status"] == "passed" for case in report["cases"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", action="store_true")
    parser.add_argument("--ollama", action="store_true")
    parser.add_argument("--mcp-learn", action="store_true", help="Cloud case uses one public Microsoft Learn search")
    parser.add_argument("--settings-dir", default=str(ROOT / ".runtime"))
    parser.add_argument("--cloud-model")
    parser.add_argument("--local-model", default="qwen3:8b")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--context-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=240)
    arguments = parser.parse_args()
    if not (arguments.cloud or arguments.ollama):
        parser.error("Select --cloud and/or --ollama; real inference is opt-in")
    if arguments.mcp_learn and not arguments.cloud:
        parser.error("--mcp-learn requires --cloud")
    try:
        sys.exit(0 if asyncio.run(main(arguments)) else 1)
    except Exception as exc:
        print(json.dumps({"probe_error": safe_error(exc)}), file=sys.stderr)
        sys.exit(1)
