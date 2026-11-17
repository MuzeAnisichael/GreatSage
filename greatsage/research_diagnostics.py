"""Offline smoke check of the installed/frozen Deep Agents research runtime."""
from __future__ import annotations

import asyncio
from importlib.metadata import PackageNotFoundError, version
import json
import re
import sys
import time

from . import __version__


class _Counter:
    def count(self, values):
        return len(json.dumps(values, ensure_ascii=False).encode("utf-8"))


class _OfflineProvider:
    """Deterministic tool responses; deliberately has no real transport/client."""

    def __init__(self, finding, review):
        self.calls = 0
        self.closed = 0
        self.role_calls = {}
        self.scripts = {
            "planner": [
                ("write_todos", {"todos": [{"content": "Organize sources and verify evidence", "status": "in_progress"}]}),
                ("task", {"subagent_type": "source_researcher", "description": "Organize the synthetic launch source."}),
                ("task", {"subagent_type": "evidence_reviewer", "description": "Check every finding against the synthetic source."}),
                ("submit_report", {"confirmed": True}),
            ],
            "source_researcher": [
                ("read_source", {"source_id": "offline-source"}),
                ("submit_findings", {"findings": [finding]}),
            ],
            "evidence_reviewer": [
                ("read_source", {"source_id": "offline-source"}),
                ("submit_review", {"review": [review]}),
            ],
        }

    async def stream_chat(self, config, messages):
        role_match = re.search(r"GreatSage role: (\w+)", json.dumps(messages))
        if role_match is None or role_match[1] not in self.scripts:
            raise ValueError("unexpected_diagnostic_role")
        role = role_match[1]
        self.calls += 1
        self.role_calls[role] = self.role_calls.get(role, 0) + 1
        try:
            name, arguments = self.scripts[role].pop(0)
            yield {"tool_calls": [{"id": f"offline-call-{self.calls}", "name": name,
                                   "arguments": json.dumps(arguments)}]}
        finally:
            self.closed += 1


async def check_research_runtime() -> dict:
    """Run the real main/child graphs against one synthetic source, offline.

    Does not construct app settings, a provider client, a tokenizer cache or a
    database. Returns metadata and counts only, never source/model transcripts.
    """
    started = time.perf_counter()
    report = {
        "ok": False, "code": "research_runtime_check_failed", "stage": "metadata",
        "versions": {"greatsage": __version__, "python": ".".join(map(str, sys.version_info[:3]))},
        "frozen": bool(getattr(sys, "frozen", False)), "network": "not_used",
        "configuration": "not_read", "model_quality": "not_tested", "source_type": "synthetic",
    }
    provider = None
    try:
        async with asyncio.timeout(20):
            for name in ("deepagents", "langchain", "langchain-core", "langgraph", "wcmatch", "bracex"):
                report["versions"][name] = version(name)
            report["stage"] = "imports"
            from .delegation import run_delegation

            source = {"handle": "S1", "id": "offline-source", "kind": "material", "label": "Synthetic launch note",
                      "text": "The synthetic launch is Friday."}
            finding = {"text": source["text"], "evidence": [{"source_id": source["id"], "quote": source["text"]}]}
            review = {**finding, "status": "supported", "reason": "The selected synthetic original explicitly states this."}
            provider = _OfflineProvider(finding, review)
            report["stage"] = "graph"
            result = await run_delegation(provider, {"context_tokens": 32768, "max_tokens": 768}, [source],
                                          _Counter(), instruction="Summarize the synthetic launch date with evidence.")
            expected = {"planner": 4, "source_researcher": 2, "evidence_reviewer": 2}
            children = result.get("children", [])
            if (result.get("findings") != [finding] or result.get("review") != [review]
                    or result.get("calls") != 8 or provider.calls != 8 or provider.closed != 8
                    or provider.role_calls != expected or any(provider.scripts.values())
                    or {child["role"] for child in children} != {"source_researcher", "evidence_reviewer"}
                    or len(children) != 2 or any(child["status"] != "completed" for child in children)):
                raise ValueError("unexpected_offline_result")
            report.update(ok=True, code="research_runtime_ready", stage="complete",
                          children=[{"role": child["role"], "status": child["status"], "calls": child["calls"]}
                                    for child in children])
    except (ImportError, PackageNotFoundError) as error:
        report.update(code="research_runtime_dependency_unavailable", error_type=type(error).__name__)
    except TimeoutError:
        report["code"] = "research_runtime_check_timeout"
    except Exception as error:
        # Exception messages may contain SDK transcripts or machine paths.
        report["error_type"] = type(error).__name__
    report["model_calls"] = provider.calls if provider is not None else 0
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return report
