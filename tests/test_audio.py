"""Audio invariants. Hardware tests require GREATSAGE_AUDIO_HARDWARE_TESTS=1.

The opt-in process test briefly plays a generated tone and keeps captured audio
in memory only. Ordinary tests neither open devices nor capture user audio.
"""
import ctypes
import math
import os
import struct
import subprocess
import sys
import threading
import time

import pytest

from greatsage.audio import AudioCaptureManager, AudioChunk, _PCMConverter
from greatsage import winloopback


def test_streaming_resample_preserves_samples_across_irregular_boundaries():
    pcm = b"".join(struct.pack("<hh", int(9000 * math.sin(i / 13)),
                               int(3000 * math.sin(i / 13))) for i in range(44100))
    full = _PCMConverter(44100, 2).convert(pcm)
    converter = _PCMConverter(44100, 2)
    streamed = b"".join(converter.convert(pcm[i:i + 404])
                        for i in range(0, len(pcm), 404))
    assert streamed == full
    assert abs(len(streamed) - 32000) <= 2


def test_loopback_abi_layout_matches_windows_sdk():
    assert ctypes.sizeof(winloopback.ActivationParams) == 12
    assert ctypes.sizeof(winloopback.WaveFormat) == 18
    assert winloopback.PropVariant.value.offset == 8
    assert ctypes.sizeof(winloopback.PropVariant) == 24
    if sys.platform == "win32":
        handler = winloopback._ActivationHandler()
        out = winloopback.PTR()
        try:
            iid = winloopback.GUID.parse("00000000-0000-0000-c000-000000000046")
            query = winloopback._method(handler.pointer, 0, winloopback.PTR,
                                       ctypes.POINTER(winloopback.PTR))
            assert query(handler.pointer, ctypes.byref(iid), ctypes.byref(out)) == 0
            assert out.value == handler.pointer.value
            winloopback._release(out)
        finally:
            handler._release(handler.pointer)
        assert handler.pointer.value not in winloopback._handlers


def test_empty_or_invalid_source_does_not_start_a_thread():
    manager = AudioCaptureManager()
    with pytest.raises(ValueError, match="at least one"):
        manager.start({}, lambda _: None)
    with pytest.raises(ValueError, match="desktop_source"):
        manager.start({"desktop_source": "fake"}, lambda _: None)
    with pytest.raises(ValueError, match="process ID"):
        manager.start({"desktop_source": "process", "desktop_process_id": -1}, lambda _: None)
    assert manager._workers == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows capture routing")
def test_requested_process_is_never_replaced_with_system_audio(monkeypatch):
    calls, chunks, errors = [], [], []
    emitted = threading.Event()

    def fake_capture(pid, include, stop, on_pcm, on_warning):
        calls.append((pid, include))
        on_pcm(b"\x01\x00" * 320)
        stop.wait(1)

    monkeypatch.setattr(winloopback, "available", lambda: True)
    monkeypatch.setattr(winloopback, "capture_process", fake_capture)
    manager = AudioCaptureManager()

    def receive(chunk):
        chunks.append(chunk)
        emitted.set()

    manager.start({"desktop_source": "process", "desktop_process_id": os.getpid()},
                  receive, errors.append)
    assert emitted.wait(1)
    manager.stop()
    assert calls == [(os.getpid(), True)]
    assert chunks[0].source == f"process:{os.getpid()}"
    assert chunks[0].sample_rate == 16000
    assert chunks[0].channels == 1
    assert not errors
    count = len(chunks)
    time.sleep(0.03)
    assert len(chunks) == count


@pytest.mark.skipif(sys.platform != "win32", reason="Windows capture routing")
def test_system_audio_excludes_configured_host_tree(monkeypatch):
    calls = []
    ready = threading.Event()

    def fake_capture(pid, include, stop, on_pcm, on_warning):
        calls.append((pid, include))
        ready.set()
        stop.wait(1)

    monkeypatch.setattr(winloopback, "available", lambda: True)
    monkeypatch.setattr(winloopback, "capture_process", fake_capture)
    manager = AudioCaptureManager()
    manager.start({"desktop_source": "system", "exclude_process_id": 12345}, lambda _: None)
    assert ready.wait(1)
    manager.stop()
    assert calls == [(12345, False)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows capture routing")
def test_open_failure_reports_source_without_falling_back(monkeypatch):
    errors = []
    failed = threading.Event()

    def fail(*args):
        raise OSError("simulated inaccessible process")

    monkeypatch.setattr(winloopback, "available", lambda: True)
    monkeypatch.setattr(winloopback, "capture_process", fail)
    manager = AudioCaptureManager()

    def error(message):
        errors.append(message)
        failed.set()

    manager.start({"desktop_source": "process", "desktop_process_id": os.getpid()},
                  lambda _: pytest.fail("Should not emit audio"), error)
    assert failed.wait(1)
    manager.stop()
    assert errors == [f"process:{os.getpid()}: simulated inaccessible process"]


def _frequency_amplitude(pcm, frequency=660):
    values = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    if not values:
        return 0.0
    omega = 2 * math.pi * frequency / 16000
    real = sum(value * math.cos(index * omega) for index, value in enumerate(values))
    imag = sum(value * math.sin(index * omega) for index, value in enumerate(values))
    return 2 * math.hypot(real, imag) / len(values)


@pytest.mark.skipif(os.getenv("GREATSAGE_AUDIO_HARDWARE_TESTS") != "1"
                    or not winloopback.available(), reason="explicit hardware test opt-in required")
def test_native_process_include_and_exclude_isolate_generated_tone():
    helper = """
import io, math, struct, sys, wave, winsound
buffer = io.BytesIO()
with wave.open(buffer, 'wb') as wav:
    wav.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
    wav.writeframes(b''.join(struct.pack('<h', int(5000 * math.sin(2 * math.pi * 660 * i / 48000))) for i in range(57600)))
print('ready', flush=True)
sys.stdin.readline()
winsound.PlaySound(buffer.getvalue(), winsound.SND_MEMORY)
print('played', flush=True)
sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, "-u", "-c", helper],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    included, excluded, errors = [], [], []
    include_manager, exclude_manager = AudioCaptureManager(), AudioCaptureManager()
    try:
        assert child.stdout.readline().strip() == "ready"
        include_manager.start({"desktop_source": "process", "desktop_process_id": child.pid},
                              included.append, errors.append)
        exclude_manager.start({"desktop_source": "system", "exclude_process_id": child.pid},
                              excluded.append, errors.append)
        time.sleep(0.35)
        child.stdin.write("play\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "played"
        time.sleep(0.15)
    finally:
        began = time.monotonic()
        include_manager.stop()
        exclude_manager.stop()
        stopped_seconds = time.monotonic() - began
        if child.poll() is None:
            child.stdin.write("exit\n")
            child.stdin.flush()
        child.communicate(timeout=3)
    include_amplitude = _frequency_amplitude(b"".join(c.pcm for c in included))
    exclude_amplitude = _frequency_amplitude(b"".join(c.pcm for c in excluded))
    print({"include_chunks": len(included), "exclude_chunks": len(excluded),
           "tone_include_amplitude": round(include_amplitude, 1),
           "tone_exclude_amplitude": round(exclude_amplitude, 1),
           "stop_seconds": round(stopped_seconds, 3), "errors": errors})
    assert not errors
    assert include_amplitude > 100
    assert exclude_amplitude < include_amplitude * 0.05
    assert stopped_seconds < 1
