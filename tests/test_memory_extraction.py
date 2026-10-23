"""Real LangMem and LangChain protocol tests; the network is always local fake I/O."""
import asyncio
import json

import httpx
import pytest

from greatsage.budget import TokenCounter
from greatsage.memory_extraction import (
    ExtractionError, MemoryFact, NoMemory, ProviderMemoryModel, extract_memory_proposals, select_sources,
)
from greatsage.providers import Providers


def proposal(source_id="source-1"):
    return {"text": "用户喜欢中文回答", "kind": "preference", "subject": "user", "subject_type": "person",
            "predicate": "reply_language", "value": "中文", "object_type": "literal",
            "evidence": [{"source_id": source_id, "quote": "我喜欢中文回答。"}], "review_reasons": []}


def config():
    return {"provider": "openai_compatible", "base_url": "http://127.0.0.1:1234/v1", "model": "fixture",
            "api_key": "test-only", "context_tokens": 8192, "max_tokens": 768, "tokenizer": "auto"}


class ProposalProvider:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = None
        self.on_call = None
        self.failure = None
        self.closed = False

    async def stream_chat(self, config, messages):
        self.calls.append((config, messages))
        self.started.set()
        if self.on_call:
            await self.on_call()
        if self.release:
            await self.release.wait()
        if self.failure:
            raise self.failure
        # LangMem wraps input into a transcript, so take the supplied ID from it.
        import re
        match = re.search(r'"source_id"\s*:\s*"([^"]+)"', messages[-1]["content"])
        yield {"tool_calls": [{"id": "call-fixture", "name": MemoryFact.__name__,
                               "arguments": json.dumps(proposal(match[1]))}]}
        yield {"usage": {"prompt_tokens": 20, "completion_tokens": 30}}

    async def close(self):
        self.closed = True


async def test_real_langmem_uses_existing_provider_transport_and_only_extracts(tmp_path, monkeypatch):
    from langsmith import Client

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    requests, audit, usages = [], [], []
    traces = []
    monkeypatch.setattr(Client, "create_run", lambda *args, **kwargs: traces.append("create"))
    monkeypatch.setattr(Client, "update_run", lambda *args, **kwargs: traces.append("update"))

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == "http://127.0.0.1:1234/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-only"
        assert body["tools"][0]["function"]["name"] == MemoryFact.__name__
        chunks = [
            {"id": "response", "object": "chat.completion.chunk", "model": "fixture", "choices": [{"index": 0,
             "delta": {"tool_calls": [{"index": 0, "id": "call-1", "type": "function", "function": {
                 "name": MemoryFact.__name__, "arguments": json.dumps(proposal())}}]}, "finish_reason": None}]},
            {"id": "response", "object": "chat.completion.chunk", "model": "fixture", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n")

    async def before_request(messages, provider):
        audit.append((messages, provider))

    async def on_usage(usage):
        usages.append(usage)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = Providers(client=client)
        sources = [{"id": "source-1", "text": "我喜欢中文回答。", "metadata": {"secret": "never-send-this"}}]
        result = await extract_memory_proposals(service, config(), sources, TokenCounter(config(), tmp_path),
                                               before_request=before_request, on_usage=on_usage)
        await service.close()
    assert result == [proposal()]
    assert len(requests) == len(audit) == 1
    assert "never-send-this" not in json.dumps(requests)
    assert "user" in json.dumps(requests)
    assert usages[0]["total_tokens"] == 50
    assert not traces


async def test_adapter_rejects_invalid_output_without_second_paid_attempt(tmp_path):
    service = ProposalProvider()

    async def invalid(config, messages):
        service.calls.append((config, messages))
        yield {"tool_calls": [{"id": "invalid", "name": "MemoryFact", "arguments": '{"text":"private raw response"}'}]}

    service.stream_chat = invalid
    with pytest.raises(ExtractionError, match="invalid_extraction_schema"):
        await extract_memory_proposals(service, config(), [{"id": "source-1", "text": "我喜欢中文回答。"}],
                                       TokenCounter(config(), tmp_path))
    assert len(service.calls) == 1


async def test_adapter_has_independent_call_cap_and_closes_cancelled_stream(tmp_path):
    service = ProposalProvider()
    service.release = asyncio.Event()
    model = ProviderMemoryModel(service=service, provider_config=config(), counter=TokenCounter(config(), tmp_path))
    task = asyncio.create_task(model.ainvoke([("user", '{"source_id":"source-1"}')]))
    await service.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ExtractionError, match="model_call_limit"):
        await model.ainvoke([("user", "another request")])
    assert len(service.calls) == 1


def test_budget_never_slices_originals_or_sends_metadata(tmp_path):
    counter = TokenCounter(config(), tmp_path)
    small = {"id": "small", "text": "我喜欢中文回答。", "metadata": {"unexpected": "X" * 10000}}
    huge = {"id": "huge", "text": "原文" * 100000}
    assert select_sources([huge, small], counter, config()) == [small]
    with pytest.raises(ExtractionError, match="input_budget_exceeded"):
        select_sources([huge], counter, config())


async def test_output_text_limit_is_enforced_before_framework_can_retry(tmp_path):
    class Oversized:
        async def stream_chat(self, config, messages):
            yield {"text": "x" * 64001}

    with pytest.raises(ExtractionError, match="output_limit_exceeded"):
        await extract_memory_proposals(Oversized(), config(), [{"id": "source-1", "text": "我喜欢中文回答。"}],
                                       TokenCounter(config(), tmp_path))


@pytest.mark.parametrize("text", ["", "I cannot use tools", '{"items":[]}'])
async def test_plain_response_is_never_treated_as_valid_empty_extraction(tmp_path, text):
    class Plain:
        async def stream_chat(self, config, messages):
            yield {"text": text}

    with pytest.raises(ExtractionError, match="missing_extraction_result"):
        await extract_memory_proposals(Plain(), config(), [{"id": "source-1", "text": "你好"}],
                                       TokenCounter(config(), tmp_path))


async def test_explicit_empty_schema_is_the_only_successful_no_memory_result(tmp_path):
    class Empty:
        async def stream_chat(self, config, messages):
            yield {"tool_calls": [{"id": "empty", "name": NoMemory.__name__, "arguments": '{"no_memory":true}'}]}

    assert await extract_memory_proposals(Empty(), config(), [{"id": "source-1", "text": "你好"}],
                                          TokenCounter(config(), tmp_path)) == []


async def test_ollama_wire_schema_inlines_evidence_for_local_tool_templates(tmp_path):
    requests = []
    local = {**config(), "provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "fixture"}

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        fact = next(tool["function"] for tool in body["tools"] if tool["function"]["name"] == "MemoryFact")
        assert "$ref" not in json.dumps(fact["parameters"])
        evidence = fact["parameters"]["properties"]["evidence"]["items"]
        assert set(evidence["properties"]) == {"source_id", "quote"}
        assert set(evidence["required"]) == {"source_id", "quote"}
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, text=json.dumps({
            "model": "fixture", "message": {"role": "assistant", "content": "", "tool_calls": [{
                "function": {"name": "NoMemory", "arguments": {"no_memory": True}}}]},
            "done": True, "done_reason": "stop"}) + "\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = Providers(client=client)
        assert await extract_memory_proposals(service, local, [{"id": "source-1", "text": "你好"}],
                                              TokenCounter(local, tmp_path)) == []
        await service.close()
    assert len(requests) == 1
