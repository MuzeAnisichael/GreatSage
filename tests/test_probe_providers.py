"""Probe errors remain diagnosable without leaking credentials or losing successes."""
import json
from types import SimpleNamespace

import httpx
import pytest

from scripts import probe_providers as probe
from greatsage.providers import ProviderError


def arguments(tmp_path, **overrides):
    values = dict(data_dir=str(tmp_path), components="llm,tts", cloud=True, ollama=False,
                  local_speech=False, catalog=False, local_model="gemma3:4b", whisper_model="base",
                  tts_model=None, voice=None, asr_model=None, output=str(tmp_path / "probe.json"))
    return SimpleNamespace(**(values | overrides))


class Store:
    def __init__(self, _):
        pass

    def provider(self, component):
        return {"provider": "openrouter", "model": "test-model", "voice": "test-voice",
                "base_url": "https://openrouter.ai/api/v1", "api_key": "never-output-secret"}


@pytest.mark.asyncio
async def test_probe_preserves_completed_stages_and_safe_failure(tmp_path, monkeypatch, capsys):
    class Providers:
        async def stream_chat(self, config, messages):
            yield {"text": "Two."}

        async def synthesize(self, *args):
            raise ProviderError("openrouter", "upstream_unavailable", 503)

        async def close(self):
            pass

    monkeypatch.setattr(probe, "SettingsStore", Store)
    monkeypatch.setattr(probe, "Providers", Providers)
    assert await probe.run(arguments(tmp_path)) is False
    records = json.loads((tmp_path / "probe.json").read_text(encoding="utf-8"))
    assert records[0]["status"] == "ok"
    assert records[0]["text"] == "Two."
    assert records[1]["error"]["code"] == "upstream_unavailable"
    assert records[1]["error"]["http_status"] == 503
    assert "never-output-secret" not in capsys.readouterr().out
    assert "never-output-secret" not in json.dumps(records)


@pytest.mark.asyncio
async def test_catalog_queries_speech_and_lists_supported_voices(tmp_path, monkeypatch):
    seen = []

    def respond(request):
        seen.append(request.url.params["output_modalities"])
        return httpx.Response(200, json={"data": [{"id": "test-model", "supported_voices": ["voice"]}]})

    monkeypatch.setattr(probe, "SettingsStore", Store)
    monkeypatch.setattr(probe, "_desktop_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    assert await probe.run(arguments(tmp_path, cloud=False, catalog=True)) is True
    assert seen == ["transcription", "speech"]
    records = json.loads((tmp_path / "probe.json").read_text(encoding="utf-8"))
    assert records[1]["models"][0]["supported_voices"] == ["voice"]
