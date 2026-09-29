import asyncio
import json

import httpx
import pytest

from greatsage.memory import MemoryStore
from greatsage.providers import ProviderError, Providers
from greatsage.semantic import chunks, fingerprint, text_hash
from greatsage.runtime import Runtime
from greatsage.budget import TokenCounter, encoding_name


def test_vector_retrieval_versions_revision_and_deletion(tmp_path):
    store = MemoryStore(tmp_path)
    try:
        first = store.add_message("user", "我最喜欢的水果是苹果。")
        other = store.add_message("user", "下周要整理车库。")
        for row, vector in [(first, [1, 0]), (other, [0, 1])]:
            assert store.save_vectors(row["id"], "model-a", text_hash(row["text"]), [vector])
        assert store.semantic_search([.99, .01], "model-a")[0]["id"] == first["id"]
        assert store.semantic_search([1, 0], "model-b") == []
        revised = store.revise_message(first["id"], "我现在最喜欢芒果。")
        assert not store.save_vectors(first["id"], "model-a", text_hash(first["text"]), [[1, 0]])
        assert all(row["id"] != first["id"] for row in store.semantic_search([1, 0], "model-a"))
        assert revised["id"] in {row["id"] for row in store.pending_vectors("model-a")}
        with pytest.raises(ValueError, match="dimensions"):
            store.save_vectors(revised["id"], "model-a", text_hash(revised["text"]), [[1, 0, 0]])
        store.clear_history()
        assert store.vector_status("model-a")["chunks"] == 0
    finally:
        store.close()


def test_chunking_covers_unicode_and_model_identity_ignores_credentials():
    text = "中文🙂abcdefgh" * 1000
    parts = chunks(text)
    assert all(len(part.encode("utf-8")) <= 4000 for part in parts)
    assert parts[0].startswith(text[:80]) and parts[-1].endswith(text[-80:])
    assert fingerprint({"model": "a", "api_key": "one"}) == fingerprint({"model": "a", "api_key": "two"})
    assert fingerprint({"model": "a"}) != fingerprint({"model": "b"})


@pytest.mark.parametrize("provider", ["openrouter", "ollama"])
async def test_embedding_protocol_orders_vectors_and_disallows_silent_truncation(provider):
    def handler(request):
        body = json.loads(request.content)
        assert body["input"] == ["甲", "乙"]
        if provider == "ollama":
            assert request.url.path == "/api/embed" and body["truncate"] is False
            return httpx.Response(200, json={"embeddings": [[1, 0], [0, 1]], "prompt_eval_count": 2})
        assert request.url.path.endswith("/embeddings")
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}], "usage": {"prompt_tokens": 2}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Providers(client).embed({"provider": provider, "model": "fixture", "api_key": "fixture"}, ["甲", "乙"])
        assert result["vectors"] == [[1, 0], [0, 1]] and result["usage"]["prompt_tokens"] == 2


@pytest.mark.parametrize("vectors", [[[0, 0]], [[1, 0], [1]], [[float("nan"), 0]]])
async def test_embedding_rejects_invalid_vectors(vectors):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"embeddings": vectors}))) as client:
        with pytest.raises(ProviderError):
            await Providers(client).embed({"provider": "ollama", "model": "fixture"}, ["测试"])


def test_budget_unknown_model_never_downloads_and_preserves_bytes(tmp_path):
    counter = TokenCounter({"model": "unknown"}, tmp_path)
    messages = [{"role": "user", "content": "你好🙂"}]
    assert counter.method == "utf8_upper_bound"
    assert counter.count(messages) == len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode())
    assert encoding_name({"model": "openai/gpt-4o-mini"}) == "o200k_base"


async def test_background_index_cannot_commit_after_delete_and_query_failure_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    class Fake:
        async def embed(self, config, texts):
            runtime.memory.delete_message(record["id"])
            return {"vectors": [[1, 0]], "usage": {}, "model": "fixture"}
        async def close(self):
            pass
    runtime = Runtime(tmp_path, providers=Fake())
    try:
        runtime.settings.update({"embedding": {"enabled": True}})
        record = runtime.memory.add_message("user", "过期内容")
        await runtime._index_pending()
        assert runtime.memory.vector_status(fingerprint(runtime.settings.raw()["embedding"]))["indexed"] == 0
    finally:
        await runtime.close()
