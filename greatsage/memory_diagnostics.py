"""Exercise the frozen LangMem import path without user data or network access."""
from __future__ import annotations

import asyncio
from importlib.metadata import version
import json
import time


async def check_memory_runtime() -> dict:
    started = time.perf_counter()
    report = {"ok": False, "code": "memory_runtime_check_failed", "versions": {},
              "network": "not_used", "model_quality": "not_tested"}
    try:
        async with asyncio.timeout(12):
            from .memory_extraction import extract_memory_proposals

            for name in ("langmem", "trustcall", "langchain", "langchain-core"):
                report["versions"][name] = version(name)
            source = {"id": "offline-source", "text": "I prefer green tea."}
            proposal = {"text": "User prefers green tea.", "kind": "preference", "subject": "user",
                        "subject_type": "person", "predicate": "preferred_drink", "value": "green tea",
                        "object_type": "literal", "evidence": [{"source_id": source["id"], "quote": source["text"]}],
                        "review_reasons": []}

            class Counter:
                def count(self, values):
                    return len(json.dumps(values, ensure_ascii=False))

            class OfflineProvider:
                calls = 0

                async def stream_chat(self, config, messages):
                    self.calls += 1
                    yield {"tool_calls": [{"id": "offline-call", "name": "MemoryFact",
                                           "arguments": json.dumps(proposal)}]}

            provider = OfflineProvider()
            result = await extract_memory_proposals(provider, {"context_tokens": 32000, "max_tokens": 1536},
                                                    [source], Counter())
            if result != [proposal] or provider.calls != 1:
                raise ValueError("unexpected_offline_result")
            report.update(ok=True, code="memory_runtime_ready", proposals=len(result), model_calls=provider.calls)
    except Exception as error:
        # SDK exception bodies can contain transcripts; return only a class name.
        report["error_type"] = type(error).__name__
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return report
