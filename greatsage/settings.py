"""Validated local configuration with current-user Windows DPAPI credentials."""
from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import math
import os
import re
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit


_IS_WINDOWS = sys.platform == "win32"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SECRET_NAME = re.compile(r"secrets-[0-9a-f]{32}\.bin\Z")
_COMPONENTS = ("asr", "llm", "tts")


def read_env(name: str, default: str = "") -> str:
    """Read only the requested name, including a Windows user's saved value."""
    if not name:
        return default
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        raise ValueError("Invalid environment variable name")
    value = os.getenv(name)
    if value is not None:
        return value
    if _IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
                value, kind = winreg.QueryValueEx(key, name)
            if isinstance(value, str) and kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                return value
        except (ImportError, OSError):
            pass
    return default


def default_data_dir() -> Path:
    if _IS_WINDOWS:
        base = read_env("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "GreatSage"
    return Path(read_env("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "greatsage"


def _defaults() -> dict:
    url = read_env("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    return {
        "mode": "conversation",
        "global_prompt": "你是 GreatSage（大贤者），一位简洁、准确的桌面秘书。",
        "output_language": "zh-CN", "voice_language": "zh-CN", "voice_enabled": False,
        "allow_proactive": False, "cooldown_seconds": 15,
        "microphone": True, "microphone_device": None,
        "desktop_source": "none", "desktop_process_id": None,
        "asr": {"provider": "openrouter", "base_url": url, "model": "openai/whisper-1",
                "api_key_env": "OPENROUTER_API_KEY", "language": "zh",
                "device": "cpu", "compute_type": "int8", "cache_dir": ""},
        "llm": {"provider": "openrouter", "base_url": url, "model": "google/gemini-2.5-flash-lite",
                "api_key_env": "OPENROUTER_API_KEY", "context_tokens": 8192, "max_tokens": 768},
        "tts": {"provider": "openrouter", "base_url": url, "model": "openai/tts-1",
                "voice": "alloy", "api_key_env": "OPENROUTER_API_KEY"},
        # Confirmed v0.1 policy: text stays until deletion, logs 30 days,
        # opt-in recordings 7 days. Retention is configurable in the UI.
        "record_audio": False, "recording_retention_days": 7, "log_retention_days": 30,
        "partial_interval_seconds": 2.5, "endpoint_silence_ms": 550,
        "min_speech_ms": 250, "max_utterance_seconds": 20,
    }


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    if not _IS_WINDOWS:
        raise RuntimeError("Saving API keys requires Windows DPAPI; configure an environment variable on this platform")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(raw: bytes):
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        return Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    incoming, incoming_buffer = blob(data)
    entropy, entropy_buffer = blob(b"GreatSage.credentials.v1")
    outgoing = Blob()
    if encrypt:
        success = crypt32.CryptProtectData(ctypes.byref(incoming), "GreatSage credentials",
                                          ctypes.byref(entropy), None, None, 1, ctypes.byref(outgoing))
    else:
        success = crypt32.CryptUnprotectData(ctypes.byref(incoming), None,
                                            ctypes.byref(entropy), None, None, 1, ctypes.byref(outgoing))
    if not success:
        raise RuntimeError("Windows could not protect or unlock the API credentials for this user")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(outgoing.pbData)
        # Keep input buffers alive until the native call returns.
        del incoming_buffer, entropy_buffer


def _protect_bytes(data: bytes) -> bytes:
    return _dpapi(data, True)


def _unprotect_bytes(data: bytes) -> bytes:
    return _dpapi(data, False)


def _string(value, label: str, maximum: int, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value or (not empty and not value.strip()):
        raise ValueError(f"Invalid {label}: expected {'nonempty ' if not empty else ''}text up to {maximum} characters")
    return value


def _number(value, label: str, low: float, high: float, integer: bool = False):
    expected = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"Invalid {label}: expected {'integer ' if integer else ''}between {low} and {high}")
    return value


def _url(value: str, label: str, allow_empty: bool) -> str:
    _string(value, label, 2048, empty=allow_empty)
    if not value and allow_empty:
        return value
    if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        raise ValueError(f"Invalid {label}: whitespace and backslashes are not allowed")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"Invalid {label}: use an HTTP(S) base URL without credentials, query, or fragment")
    return value.rstrip("/")


def _validate(config: dict) -> dict:
    allowed = set(_defaults())
    unknown = set(config) - allowed
    if unknown:
        raise ValueError("Unknown top-level setting")
    if config["mode"] not in ("conversation", "listen", "proactive"):
        raise ValueError("Invalid mode")
    if config["desktop_source"] not in ("none", "system", "process"):
        raise ValueError("Invalid desktop_source")
    for field in ("voice_enabled", "allow_proactive", "microphone", "record_audio"):
        if type(config[field]) is not bool:
            raise ValueError(f"Invalid {field}: expected a boolean")
    _string(config["global_prompt"], "global_prompt", 32000)
    for field in ("output_language", "voice_language"):
        if not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}", _string(config[field], field, 24, empty=False)):
            raise ValueError(f"Invalid {field}: expected a language tag")
    for field, low, high, integer in (
        ("cooldown_seconds", 0, 3600, False), ("recording_retention_days", 1, 3650, True),
        ("log_retention_days", 1, 3650, True), ("partial_interval_seconds", .5, 30, False),
        ("endpoint_silence_ms", 150, 5000, True), ("min_speech_ms", 50, 2000, True),
        ("max_utterance_seconds", 3, 120, False),
    ):
        _number(config[field], field, low, high, integer)
    device = config["microphone_device"]
    if device is not None:
        if type(device) is int:
            _number(device, "microphone_device", 0, 65535, True)
        else:
            _string(device, "microphone_device", 512, empty=False)
    process = config["desktop_process_id"]
    if process is not None:
        _number(process, "desktop_process_id", 1, 4294967295, True)
    if config["desktop_source"] == "process" and process is None:
        raise ValueError("desktop_process_id is required for a process audio source")
    common = {"provider", "base_url", "model", "api_key_env", "timeout_seconds"}
    extra = {
        "asr": {"language", "device", "compute_type", "cache_dir", "beam_size", "cpu_threads", "device_index"},
        "llm": {"context_tokens", "max_tokens", "temperature"},
        "tts": {"voice", "rate", "speed"},
    }
    providers = {"asr": ("openai", "openrouter", "faster_whisper"),
                 "llm": ("openai", "openrouter", "ollama"),
                 "tts": ("openai", "openrouter", "system")}
    for component in _COMPONENTS:
        provider = config[component]
        if not isinstance(provider, dict) or set(provider) - common - extra[component]:
            raise ValueError(f"Unknown or invalid {component} setting")
        if provider.get("provider") not in providers[component]:
            raise ValueError(f"Invalid {component}.provider")
        local = provider["provider"] in {"faster_whisper", "system"}
        provider["base_url"] = _url(provider.get("base_url", ""), f"{component}.base_url", local)
        _string(provider.get("model", ""), f"{component}.model", 256, empty=component == "tts" and local)
        name = _string(provider.get("api_key_env", ""), f"{component}.api_key_env", 128)
        if name and not _ENV_NAME.fullmatch(name):
            raise ValueError(f"Invalid {component}.api_key_env")
        if "timeout_seconds" in provider:
            _number(provider["timeout_seconds"], f"{component}.timeout_seconds", 1, 300)
        if component == "asr":
            language = _string(provider.get("language", ""), "asr.language", 24)
            if language and not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}", language):
                raise ValueError("Invalid asr.language")
            if provider.get("device", "cpu") not in ("cpu", "cuda", "auto"):
                raise ValueError("Invalid asr.device")
            if provider.get("compute_type", "int8") not in ("default", "auto", "int8", "int8_float16", "int8_float32", "int8_bfloat16", "int16", "float16", "float32", "bfloat16"):
                raise ValueError("Invalid asr.compute_type")
            _string(provider.get("cache_dir", ""), "asr.cache_dir", 2048)
            for field, low, high in (("beam_size", 1, 10), ("cpu_threads", 0, 64), ("device_index", 0, 32)):
                if field in provider:
                    _number(provider[field], f"asr.{field}", low, high, True)
        elif component == "llm":
            _number(provider["context_tokens"], "llm.context_tokens", 1024, 2000000, True)
            _number(provider["max_tokens"], "llm.max_tokens", 64, 128000, True)
            if provider["max_tokens"] + 256 >= provider["context_tokens"]:
                raise ValueError("llm.context_tokens must leave room for both prompt and output")
            if "temperature" in provider:
                _number(provider["temperature"], "llm.temperature", 0, 2)
        else:
            _string(provider.get("voice", ""), "tts.voice", 256)
            if "rate" in provider:
                _number(provider["rate"], "tts.rate", -10, 10, True)
            if "speed" in provider:
                _number(provider["speed"], "tts.speed", .25, 4)
    return config


class SettingsStore:
    """Commit configuration + credential references as one atomic snapshot.

    Immutable encrypted credential generations are written before settings.json;
    replacing settings.json is the commit point. A failed commit retains both
    the prior settings and its credential file. No plaintext credential is ever
    part of get(), raw(), version(), or the serialized configuration document.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.data_dir / "settings.json"
        self._lock = threading.RLock()
        self._secret_file: str | None = None
        self._state = _defaults()
        if self._path.exists():
            envelope = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or envelope.get("schema_version") != 1:
                raise ValueError("Unsupported settings file format")
            saved = envelope.get("settings")
            if not isinstance(saved, dict):
                raise ValueError("Invalid settings file")
            self._state = self._merge(saved)
            self._secret_file = envelope.get("secret_file")
            if self._secret_file is not None and (not isinstance(self._secret_file, str) or not _SECRET_NAME.fullmatch(self._secret_file)):
                raise ValueError("Invalid credential file reference")
            _validate(self._state)
        else:
            _validate(self._state)
            self._commit(self._state, None)

    def _merge(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("Settings update must be an object")
        merged = copy.deepcopy(self._state)
        for key, value in patch.items():
            if key in _COMPONENTS and isinstance(value, dict):
                merged[key].update(copy.deepcopy(value))
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def _commit(self, config: dict, secret_file: str | None) -> None:
        envelope = {"schema_version": 1, "settings": config, "secret_file": secret_file}
        temporary = self.data_dir / f"settings-{uuid.uuid4().hex}.tmp"
        try:
            self._write_file(temporary, json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8"))
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def _secrets(self) -> dict[str, str]:
        if not self._secret_file:
            return {}
        path = self.data_dir / self._secret_file
        try:
            if path.stat().st_size > 128 * 1024:
                raise ValueError("Credential file is too large")
            secrets = json.loads(_unprotect_bytes(path.read_bytes()).decode("utf-8"))
            if not isinstance(secrets, dict) or set(secrets) - set(_COMPONENTS) or any(not isinstance(value, str) for value in secrets.values()):
                raise ValueError("Invalid credential payload")
            return secrets
        except (OSError, UnicodeError, ValueError) as error:
            raise RuntimeError("Stored credentials cannot be read; reconfigure them under the original Windows user") from None

    def raw(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def get(self) -> dict:
        with self._lock:
            state = self.raw()
            secrets = self._secrets()
            for component in _COMPONENTS:
                state[component]["key_configured"] = bool(secrets.get(component) or read_env(state[component].get("api_key_env", "")))
            return state

    def provider(self, component: str) -> dict:
        if component not in _COMPONENTS:
            raise ValueError("Unknown provider component")
        with self._lock:
            result = copy.deepcopy(self._state[component])
            # An explicitly entered key overrides the environment until cleared.
            result["api_key"] = self._secrets().get(component) or read_env(result.get("api_key_env", ""))
            return result

    def version(self) -> str:
        with self._lock:
            # Credential values and random encrypted generations are excluded.
            raw = json.dumps(self._state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def update(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("Settings update must be an object")
        with self._lock:
            clean = copy.deepcopy(patch)
            secret_changes: dict[str, str | None] = {}
            for component in _COMPONENTS:
                part = clean.get(component)
                if not isinstance(part, dict):
                    continue
                part.pop("key_configured", None)
                clear = part.pop("clear_api_key", False)
                if type(clear) is not bool:
                    raise ValueError(f"Invalid {component}.clear_api_key")
                key = part.pop("api_key", "")
                _string(key, f"{component}.api_key", 16384)
                if any(ord(character) < 32 for character in key):
                    raise ValueError(f"Invalid {component}.api_key: control characters are not allowed")
                if clear and key.strip():
                    raise ValueError(f"Cannot set and clear {component}.api_key together")
                if clear:
                    secret_changes[component] = None
                elif key.strip():
                    secret_changes[component] = key.strip()
            next_state = _validate(self._merge(clean))
            old_file, next_file = self._secret_file, self._secret_file
            created: Path | None = None
            try:
                if secret_changes:
                    secrets = self._secrets()
                    for component, key in secret_changes.items():
                        if key is None:
                            secrets.pop(component, None)
                        else:
                            secrets[component] = key
                    if secrets:
                        encrypted = _protect_bytes(json.dumps(secrets, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                        next_file = f"secrets-{uuid.uuid4().hex}.bin"
                        created = self.data_dir / next_file
                        self._write_file(created, encrypted)
                    else:
                        next_file = None
                self._commit(next_state, next_file)
            except Exception:
                if created is not None:
                    created.unlink(missing_ok=True)
                raise
            self._state, self._secret_file = next_state, next_file
            if old_file and old_file != next_file:
                try:
                    (self.data_dir / old_file).unlink(missing_ok=True)
                except OSError:
                    # An old DPAPI ciphertext may remain, but the committed
                    # snapshot never points to it and it contains no clear text.
                    pass
            return self.get()
