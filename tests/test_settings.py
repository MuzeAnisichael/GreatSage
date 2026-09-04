import json
import os
import sys
import types

import pytest

from greatsage import settings
from greatsage.settings import SettingsStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ENVIRONMENT_TEST_CREDENTIAL")
    monkeypatch.setattr(settings, "_protect_bytes", lambda data: b"protected:" + bytes(value ^ 0xA5 for value in data))
    monkeypatch.setattr(settings, "_unprotect_bytes", lambda data: bytes(value ^ 0xA5 for value in data.removeprefix(b"protected:")))
    return SettingsStore(tmp_path)


def test_defaults_follow_confirmed_first_release_policy(store):
    public = store.get()
    assert public["mode"] == "conversation"
    assert public["microphone"] and not public["voice_enabled"]
    assert not public["record_audio"]
    assert public["log_retention_days"] == 30 and public["recording_retention_days"] == 7
    assert public["llm"]["key_configured"]
    assert "ENVIRONMENT_TEST_CREDENTIAL" not in json.dumps(public)
    assert all("api_key" not in provider for provider in (public["asr"], public["llm"], public["tts"]))


def test_secrets_never_enter_config_raw_get_or_version_and_survive_restart(store):
    original_version = store.version()
    returned = store.update({"llm": {"api_key": "STORED_TEST_CREDENTIAL"}})
    assert store.version() == original_version
    assert returned["llm"]["key_configured"]
    assert "api_key" not in store.raw()["llm"]
    assert store.provider("llm")["api_key"] == "STORED_TEST_CREDENTIAL"
    assert "STORED_TEST_CREDENTIAL" not in json.dumps(returned)
    assert "secret_file" not in json.dumps(returned)
    for file in store.data_dir.iterdir():
        assert b"STORED_TEST_CREDENTIAL" not in file.read_bytes()
        assert b"ENVIRONMENT_TEST_CREDENTIAL" not in file.read_bytes()
    restored = SettingsStore(store.data_dir)
    assert restored.provider("llm")["api_key"] == "STORED_TEST_CREDENTIAL"
    restored.update({"llm": {"api_key": "", "key_configured": True}})
    assert restored.provider("llm")["api_key"] == "STORED_TEST_CREDENTIAL"
    restored.update({"llm": {"clear_api_key": True}})
    assert restored.provider("llm")["api_key"] == "ENVIRONMENT_TEST_CREDENTIAL"
    assert not list(store.data_dir.glob("secrets-*.bin"))


def test_ui_can_roundtrip_redacted_settings_and_partial_updates(store):
    displayed = store.get()
    displayed["global_prompt"] = "使用简短中文答复。"
    store.update(displayed)
    before = store.version()
    store.update({"asr": {"provider": "faster_whisper", "model": "small", "device": "cpu", "cache_dir": "D:/Models"}})
    assert store.raw()["asr"]["compute_type"] == "int8"
    assert store.raw()["asr"]["cache_dir"] == "D:/Models"
    assert store.raw()["global_prompt"] == "使用简短中文答复。"
    assert store.version() != before
    mutable = store.raw()
    mutable["llm"]["model"] = "changed outside store"
    assert store.raw()["llm"]["model"] != mutable["llm"]["model"]


def test_failed_atomic_commit_preserves_both_old_settings_and_credentials(store, monkeypatch):
    store.update({"llm": {"api_key": "ORIGINAL_TEST_CREDENTIAL"}})
    old_settings = store.raw()
    old_file = (store.data_dir / "settings.json").read_bytes()
    old_secrets = list(store.data_dir.glob("secrets-*.bin"))
    replace = settings.os.replace

    def fail_settings_replace(source, target):
        if target.name == "settings.json":
            raise OSError("simulated commit failure")
        return replace(source, target)

    monkeypatch.setattr(settings.os, "replace", fail_settings_replace)
    with pytest.raises(OSError):
        store.update({"output_language": "en", "llm": {"api_key": "UNCOMMITTED_TEST_CREDENTIAL"}})
    assert store.raw() == old_settings
    assert (store.data_dir / "settings.json").read_bytes() == old_file
    assert list(store.data_dir.glob("secrets-*.bin")) == old_secrets
    assert not list(store.data_dir.glob("settings-*.tmp"))
    assert SettingsStore(store.data_dir).provider("llm")["api_key"] == "ORIGINAL_TEST_CREDENTIAL"


@pytest.mark.parametrize("patch", [
    {"mode": "unknown"}, {"mode": []}, {"unrecognized_option": True},
    {"voice_enabled": "false"}, {"cooldown_seconds": -1}, {"cooldown_seconds": float("nan")},
    {"cooldown_seconds": 10 ** 1000}, {"recording_retention_days": 0},
    {"log_retention_days": 2.5}, {"desktop_source": "process"},
    {"desktop_process_id": -5}, {"microphone_device": True},
    {"global_prompt": "x" * 32001}, {"output_language": "<script>"},
    {"llm": {"context_tokens": 1024, "max_tokens": 1024}},
    {"llm": {"temperature": 10}}, {"llm": {"api_key_env": "KEY;print(secret)"}},
    {"llm": {"provider": "arbitrary_code"}}, {"llm": {"provider": []}},
    {"asr": {"compute_type": "arbitrary"}}, {"asr": {"device": "shell"}},
    {"tts": {"unknown_nested": "data"}}, {"tts": {"api_key": "secret\nInjected"}},
])
def test_invalid_updates_are_rejected_without_any_disk_change(store, patch):
    old = (store.data_dir / "settings.json").read_bytes()
    with pytest.raises(ValueError):
        store.update(patch)
    assert (store.data_dir / "settings.json").read_bytes() == old


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "https://user:password@example.com/v1",
    "https://example.com/v1?api_key=secret", "https://example.com/v1#secret",
    "https://example.com:bad/v1", "http://", "http://example.com\\other",
    "https://example.com/\nsecret",
])
def test_url_validation_rejects_credentials_unsafe_schemes_and_ambiguous_addresses(store, url):
    with pytest.raises(ValueError):
        store.update({"llm": {"base_url": url}})


def test_local_and_custom_cloud_urls_are_supported(store):
    store.update({"llm": {"provider": "ollama", "base_url": "http://localhost:11434/", "model": "qwen3:4b", "api_key_env": ""}})
    assert store.raw()["llm"]["base_url"] == "http://localhost:11434"
    store.update({"llm": {"provider": "openai", "base_url": "https://model.example/v1"}})
    assert store.raw()["llm"]["base_url"] == "https://model.example/v1"


def test_read_env_queries_only_requested_registry_value(monkeypatch):
    requested = []

    class RegistryKey:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    fake_registry = types.SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_READ=2, REG_SZ=1, REG_EXPAND_SZ=2,
        OpenKey=lambda *args: RegistryKey(),
        QueryValueEx=lambda key, name: (requested.append(name) or "USER_REGISTRY_TEST_VALUE", 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_registry)
    monkeypatch.setattr(settings, "_IS_WINDOWS", True)
    monkeypatch.delenv("GREATSAGE_REGISTRY_TEST_KEY", raising=False)
    assert settings.read_env("GREATSAGE_REGISTRY_TEST_KEY") == "USER_REGISTRY_TEST_VALUE"
    assert requested == ["GREATSAGE_REGISTRY_TEST_KEY"]
    monkeypatch.setenv("GREATSAGE_REGISTRY_TEST_KEY", "PROCESS_TEST_VALUE")
    assert settings.read_env("GREATSAGE_REGISTRY_TEST_KEY") == "PROCESS_TEST_VALUE"
    assert requested == ["GREATSAGE_REGISTRY_TEST_KEY"]
    with pytest.raises(ValueError):
        settings.read_env("BAD;NAME")


def test_non_windows_never_saves_plaintext_as_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_IS_WINDOWS", False)
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    instance = SettingsStore(tmp_path)
    old = (tmp_path / "settings.json").read_bytes()
    with pytest.raises(RuntimeError, match="requires Windows DPAPI"):
        instance.update({"llm": {"api_key": "NEVER_SAVE_THIS_AS_PLAINTEXT"}})
    assert (tmp_path / "settings.json").read_bytes() == old
    assert not list(tmp_path.glob("secrets-*.bin"))


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows API")
def test_native_dpapi_roundtrip_encrypts_for_current_user():
    plaintext = b"GreatSage random fixture credential, not a real API key"
    protected = settings._protect_bytes(plaintext)
    assert plaintext not in protected
    assert settings._unprotect_bytes(protected) == plaintext
