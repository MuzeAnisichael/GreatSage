"""Offline request budgeting; tokenizer resources download only on explicit prepare."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

_HASHES = {
    "cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
}
_ENCODINGS = {}
_LOCK = threading.RLock()


def encoding_name(config):
    selected = config.get("tokenizer", "auto")
    if selected != "auto":
        return selected if selected in _HASHES else None
    model = config.get("model", "").removeprefix("openai/")
    if model.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4")):
        return "o200k_base"
    if model.startswith(("gpt-4", "gpt-3.5-turbo")):
        return "cl100k_base"
    return None


def _resource(name, data_dir):
    url = f"https://openaipublic.blob.core.windows.net/encodings/{name}.tiktoken"
    cache = Path(data_dir) / "tokenizers"
    return url, cache / hashlib.sha1(url.encode()).hexdigest()


def load_cached(name, data_dir):
    if not name:
        return None
    with _LOCK:
        if name in _ENCODINGS:
            return _ENCODINGS[name]
        _, path = _resource(name, data_dir)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != _HASHES[name]:
            return None
        import tiktoken
        previous = os.environ.get("TIKTOKEN_CACHE_DIR")
        try:
            os.environ["TIKTOKEN_CACHE_DIR"] = str(path.parent)
            _ENCODINGS[name] = tiktoken.get_encoding(name)
            return _ENCODINGS[name]
        finally:
            if previous is None:
                os.environ.pop("TIKTOKEN_CACHE_DIR", None)
            else:
                os.environ["TIKTOKEN_CACHE_DIR"] = previous


async def prepare(config, data_dir, client):
    name = encoding_name(config)
    if not name:
        return {"ready": False, "method": "utf8_upper_bound", "detail": "当前模型没有已知编码，继续使用保守字节预算。"}
    url, path = _resource(name, data_dir)
    if not load_cached(name, data_dir):
        response = await client.get(url, timeout=25)
        response.raise_for_status()
        raw = response.content
        if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != _HASHES[name]:
            raise ValueError("Tokenizer resource integrity check failed")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    load_cached(name, data_dir)
    return {"ready": True, "method": f"{name}:json_estimate", "detail": "编码已准备；预算包含请求封装估算和输出预留。"}


class TokenCounter:
    def __init__(self, config, data_dir):
        self.name = encoding_name(config)
        self.encoder = load_cached(self.name, data_dir)
        self.method = f"{self.name}:json_estimate" if self.encoder else "utf8_upper_bound"

    def count(self, messages):
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        if self.encoder:
            return len(self.encoder.encode(serialized, disallowed_special=())) + 8 * len(messages)
        return len(serialized.encode("utf-8"))
