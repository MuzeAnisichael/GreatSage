"""Audio invariants. Hardware tests require GREATSAGE_AUDIO_HARDWARE_TESTS=1.

The opt-in process test briefly plays a generated tone and keeps captured audio
in memory only. Ordinary tests neither open devices nor capture user audio.
"""
import ctypes
import importlib.machinery
import json
import math
import os
import struct
import subprocess
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from greatsage.audio import AudioCaptureManager, AudioChunk, _PCMConverter
from greatsage import audio as audio_module, winloopback


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


@pytest.fixture
def fake_portaudio(monkeypatch):
    """Detect native-call overlap, thread changes, and termination of live streams."""
    state = types.SimpleNamespace(
        active=0, threads=set(), events=[], streams=set(), initialized=False,
        open_count=0, fail_enumeration=False, fail_open=False,
    )

    @contextmanager
    def native(label):
        assert state.active == 0, "PortAudio calls overlapped"
        state.active += 1
        state.threads.add(threading.get_ident())
        state.events.append(label)
        try:
            time.sleep(0.001)  # Release GIL; independent unsynchronized instances race.
            yield
        finally:
            state.active -= 1

    info = {"hostApi": 0, "index": 0, "maxInputChannels": 1,
            "defaultSampleRate": 16000, "name": "Synthetic device"}

    class Stream:
        def get_read_available(self):
            with native("available"):
                assert self in state.streams and state.initialized
                return 320

        def read(self, *args, **kwargs):
            with native("read"):
                assert self in state.streams and state.initialized
                return b"\x01\x00" * 320

        def stop_stream(self):
            with native("stop"):
                assert self in state.streams

        def close(self):
            with native("close"):
                state.streams.remove(self)

    class PyAudio:
        def __init__(self):
            with native("initialize"):
                assert not state.initialized
                state.initialized = True

        def terminate(self):
            with native("terminate"):
                assert not state.streams, "Terminated PortAudio while another manager was capturing"
                assert state.initialized
                state.initialized = False

        def get_host_api_info_by_type(self, kind):
            with native("host_info"):
                return {"index": 0, "defaultInputDevice": 0}

        def get_device_info_generator(self):
            with native("enumerate"):
                if state.fail_enumeration:
                    raise OSError("simulated enumeration failure")
                yield dict(info)

        def get_device_info_by_index(self, index):
            with native("device_info"):
                return dict(info)

        def open(self, **kwargs):
            with native("open"):
                if state.fail_open:
                    raise OSError("simulated stream open failure")
                stream = Stream()
                state.streams.add(stream)
                state.open_count += 1
                return stream

    module = types.ModuleType("pyaudiowpatch")
    module.__spec__ = importlib.machinery.ModuleSpec("pyaudiowpatch", loader=None)
    module.PyAudio = PyAudio
    module.paWASAPI, module.paInt16, module.paInputOverflowed = 13, 8, -9981
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", module)
    monkeypatch.setattr(winloopback, "available", lambda: True)
    import psutil

    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [])
    host = audio_module._PortAudioHost()
    monkeypatch.setattr(audio_module, "_PORTAUDIO", host)
    yield state
    assert not state.streams
    assert not state.initialized
    assert host._leases == 0
    if host._worker:
        host._requests.put(None)
        host._worker.join(1)
        assert not host._worker.is_alive()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows source enumeration")
def test_concurrent_managers_serialize_all_portaudio_calls(fake_portaudio):
    barrier = threading.Barrier(2)

    def enumerate_repeatedly():
        manager = AudioCaptureManager()
        barrier.wait(timeout=2)
        return [manager.sources() for _ in range(4)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(enumerate_repeatedly) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert all(not result["errors"] and len(result["microphones"]) == 1
               for group in results for result in group)
    assert len(fake_portaudio.threads) == 1  # Setup, enumeration and teardown same thread.
    assert fake_portaudio.events.count("initialize") == fake_portaudio.events.count("terminate")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows microphone lifecycle")
def test_enumeration_and_stopping_one_manager_preserve_other_capture(fake_portaudio):
    managers = [AudioCaptureManager(), AudioCaptureManager()]
    received = [threading.Event(), threading.Event()]
    errors = []
    try:
        for manager, event in zip(managers, received):
            manager.start({"microphone": True}, lambda chunk, event=event: event.set(), errors.append)
            assert event.wait(2)
        assert AudioCaptureManager().sources()["errors"] == []
        assert fake_portaudio.events.count("initialize") == 1
        assert fake_portaudio.events.count("terminate") == 0
        managers[0].stop()
        received[1].clear()
        assert received[1].wait(2)
        assert len(fake_portaudio.streams) == 1
        assert fake_portaudio.events.count("terminate") == 0
        # Race the final release against a request from an unrelated manager.
        barrier = threading.Barrier(2)

        def enumerate_while_stopping():
            barrier.wait(timeout=2)
            return AudioCaptureManager().sources()

        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(enumerate_while_stopping)
            barrier.wait(timeout=2)
            managers[1].stop()
            assert result.result(timeout=3)["errors"] == []
    finally:
        for manager in managers:
            manager.stop()
    assert not errors
    assert len(fake_portaudio.threads) == 1
    assert fake_portaudio.events.count("initialize") == fake_portaudio.events.count("terminate")
    assert fake_portaudio.events.count("close") == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows source failures")
def test_portaudio_leases_release_after_enumeration_and_open_failures(fake_portaudio):
    fake_portaudio.fail_enumeration = True
    assert "simulated enumeration failure" in AudioCaptureManager().sources()["errors"][0]
    fake_portaudio.fail_enumeration = False
    assert AudioCaptureManager().sources()["errors"] == []
    fake_portaudio.fail_open = True
    manager, error = AudioCaptureManager(), threading.Event()
    manager.start({"microphone": True}, lambda _: None, lambda _: error.set())
    try:
        assert error.wait(2)
    finally:
        manager.stop()
    assert not fake_portaudio.initialized
    assert fake_portaudio.events.count("initialize") == fake_portaudio.events.count("terminate")


@pytest.mark.skipif(os.getenv("GREATSAGE_AUDIO_ENUMERATION_TESTS") != "1"
                    or sys.platform != "win32", reason="explicit enumeration test opt-in required")
def test_real_concurrent_device_enumeration_without_recording():
    # Isolate native crashes from the test runner. This opens no recording stream.
    helper = """
import json, threading
from concurrent.futures import ThreadPoolExecutor
from greatsage.audio import AudioCaptureManager
barrier = threading.Barrier(2)
def probe():
    manager = AudioCaptureManager()
    barrier.wait(timeout=5)
    results = [manager.sources() for _ in range(4)]
    return [{"microphones": len(item["microphones"]), "errors": item["errors"]} for item in results]
with ThreadPoolExecutor(max_workers=2) as executor:
    futures = [executor.submit(probe) for _ in range(2)]
    print(json.dumps([future.result(timeout=25) for future in futures]))
"""
    result = subprocess.run([sys.executable, "-c", helper], capture_output=True, text=True,
                            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, f"Enumeration subprocess exit: {result.returncode}; {result.stderr}"
    groups = json.loads(result.stdout)
    assert len(groups) == 2 and all(len(group) == 4 for group in groups)
    assert all(not result["errors"] for group in groups for result in group)
    print({"concurrent_requests": 2, "enumeration_calls": 8,
           "microphones": groups[0][0]["microphones"], "recording_started": False})


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
