"""Background audio sources with explicit provenance and bounded buffering.

Audio is never written to disk here. Start/stop are thread-safe, and callbacks
run on a dedicated dispatch thread: callers should enqueue into their event loop
with call_soon_threadsafe rather than await or do model work inside a callback.
"""

from __future__ import annotations

import audioop
import importlib.util
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import winloopback


@dataclass(frozen=True)
class AudioChunk:
    source: str
    pcm: bytes
    sample_rate: int = 16000
    channels: int = 1
    timestamp: float = field(default_factory=time.time)


class _PCMConverter:
    """Stateful PCM16 stereo/mono conversion; preserves phase across chunks."""
    def __init__(self, sample_rate: int, channels: int) -> None:
        if channels not in (1, 2):
            raise ValueError("Capture supports mono or stereo PCM input")
        self.sample_rate = sample_rate
        self.channels = channels
        self.state = None

    def convert(self, pcm: bytes) -> bytes:
        if self.channels == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        if self.sample_rate != 16000:
            pcm, self.state = audioop.ratecv(
                pcm, 2, 1, self.sample_rate, 16000, self.state)
        return pcm


class AudioCaptureManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._dispatcher: threading.Thread | None = None
        self._queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=100)
        self._running = False
        self._last_overflow = 0.0

    def sources(self) -> dict:
        """Enumerate without starting capture; id values are current device IDs.

        process_available reports native OS support, not whether every listed
        process emits accessible audio. Process mode captures the selected tree.
        Device and PID choices must be refreshed after devices/processes change.
        """
        result: dict = {
            "microphones": [], "processes": [],
            "system_available": winloopback.available(),
            "process_available": winloopback.available(),
            "system_excludes_self": winloopback.available(),
            "errors": [],
        }
        if sys.platform != "win32":
            result["errors"].append("Windows audio capture is unavailable on this OS")
            return result
        try:
            import psutil

            for process in psutil.process_iter(["pid", "name"]):
                if process.info["pid"] > 0 and process.info["name"]:
                    result["processes"].append({
                        "id": process.info["pid"], "name": process.info["name"],
                    })
            result["processes"].sort(key=lambda p: (p["name"].casefold(), p["id"]))
        except Exception as exc:
            result["errors"].append(f"Could not list processes: {exc}")
        try:
            import pyaudiowpatch as pa

            with pa.PyAudio() as audio:
                wasapi = audio.get_host_api_info_by_type(pa.paWASAPI)
                for info in audio.get_device_info_generator():
                    if (info["hostApi"] == wasapi["index"]
                            and info["maxInputChannels"] > 0
                            and not info.get("isLoopbackDevice", False)):
                        result["microphones"].append({
                            "id": str(info["index"]), "name": info["name"],
                            "default": info["index"] == wasapi["defaultInputDevice"],
                        })
                # Older Windows can still capture the default render endpoint,
                # but only the native process-loopback path can exclude a PID.
                if not result["system_available"]:
                    try:
                        audio.get_default_wasapi_loopback()
                        result["system_available"] = True
                    except OSError:
                        pass
        except Exception as exc:
            result["errors"].append(f"Could not list audio devices: {exc}")
        return result

    def start(
        self,
        options: dict,
        on_chunk: Callable[[AudioChunk], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """Start requested sources, returning before devices are opened.

        Failures opening a source are delivered to on_error with source labels;
        another successfully opened source continues. Invalid options fail early.
        source labels: microphone:<device index>, system, process:<PID>.
        exclude_process_id can be the Electron host PID to omit host + renderers.
        """
        desktop = options.get("desktop_source", "none")
        if desktop not in ("none", "system", "process"):
            raise ValueError("desktop_source must be none, system or process")
        microphone = bool(options.get("microphone", False))
        if not microphone and desktop == "none":
            raise ValueError("Select at least one audio source")
        pid = options.get("desktop_process_id")
        if desktop == "process":
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise ValueError("Select a valid process ID for process capture")
            if not winloopback.available():
                raise OSError("Selected-process capture requires Windows build 20348 or newer")
            import psutil

            if not psutil.pid_exists(pid):
                raise ValueError(f"Process {pid} no longer exists; refresh the process list")
        if sys.platform != "win32":
            raise OSError("Audio capture currently requires Windows")
        if microphone and importlib.util.find_spec("pyaudiowpatch") is None:
            raise RuntimeError("Microphone capture requires the PyAudioWPatch package")
        exclude_pid = options.get("exclude_process_id") or os.getpid()
        if not isinstance(exclude_pid, int) or isinstance(exclude_pid, bool) or exclude_pid <= 0:
            raise ValueError("exclude_process_id must be a positive process ID")

        with self._lock:
            if self._running:
                raise RuntimeError("Audio capture is already running; stop it before restarting")
            if any(worker.is_alive() for worker in self._workers):
                raise RuntimeError("Previous audio sources are still stopping")
            self._running = True
            self._stop = threading.Event()
            self._queue = queue.Queue(maxsize=100)
            self._last_overflow = 0.0
            stop = self._stop
            chunks = self._queue

            def report(message: str) -> None:
                if on_error:
                    try:
                        on_error(message)
                    except Exception:
                        pass  # A UI/log callback must not crash a capture thread.

            def emit(source: str, pcm: bytes) -> None:
                if not pcm or stop.is_set():
                    return
                chunk = AudioChunk(source, pcm)
                try:
                    chunks.put_nowait(chunk)
                except queue.Full:
                    # Prefer recent audio. Inform audit consumers of the gap.
                    try:
                        chunks.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        chunks.put_nowait(chunk)
                    except queue.Full:
                        pass
                    now = time.monotonic()
                    if now - self._last_overflow > 1:
                        self._last_overflow = now
                        report("Audio dispatch queue overflow: oldest audio dropped")

            def dispatch() -> None:
                while not stop.is_set():
                    try:
                        chunk = chunks.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if not stop.is_set():
                        try:
                            on_chunk(chunk)
                        except Exception as exc:
                            report(f"Audio consumer failed: {exc}")

            def run(label: str, target, *args) -> None:
                try:
                    target(*args)
                except Exception as exc:
                    if not stop.is_set():
                        report(f"{label}: {exc}")

            self._workers = []
            if microphone:
                self._workers.append(threading.Thread(
                    target=run, args=("microphone", self._capture_device,
                                     options.get("microphone_device"), False, stop, emit, report),
                    name="greatsage-microphone", daemon=True))
            if desktop != "none":
                if winloopback.available():
                    source = f"process:{pid}" if desktop == "process" else "system"
                    capture_pid = pid if desktop == "process" else exclude_pid
                    self._workers.append(threading.Thread(
                        target=run, args=(source, winloopback.capture_process, capture_pid,
                                         desktop == "process", stop,
                                         lambda pcm, source=source: emit(source, pcm), report),
                        name="greatsage-desktop-audio", daemon=True))
                else:
                    report("system: endpoint loopback cannot exclude assistant playback on this Windows version")
                    self._workers.append(threading.Thread(
                        target=run, args=("system", self._capture_device, None, True, stop, emit, report),
                        name="greatsage-system-audio", daemon=True))
            self._dispatcher = threading.Thread(
                target=dispatch, name="greatsage-audio-dispatch", daemon=True)
            self._dispatcher.start()
            for worker in self._workers:
                worker.start()

    @staticmethod
    def _capture_device(device_id, loopback, stop, emit, report) -> None:
        import pyaudiowpatch as pa

        with pa.PyAudio() as audio:
            if loopback:
                info = audio.get_default_wasapi_loopback()
            elif device_id is None or device_id == "":
                host = audio.get_host_api_info_by_type(pa.paWASAPI)
                if host["defaultInputDevice"] < 0:
                    raise OSError("No default WASAPI microphone is available")
                info = audio.get_device_info_by_index(host["defaultInputDevice"])
            else:
                info = audio.get_device_info_by_index(int(device_id))
            if not loopback and info.get("isLoopbackDevice", False):
                raise ValueError("A render-loopback device cannot be selected as a microphone")
            if info["maxInputChannels"] < 1:
                raise OSError("Selected device has no input channels")
            rate = round(info["defaultSampleRate"])
            channels = min(2, int(info["maxInputChannels"]))
            converter = _PCMConverter(rate, channels)
            source = "system" if loopback else f"microphone:{info['index']}"
            block_frames = max(1, rate // 50)  # 20 ms, preserving streaming latency
            stream = audio.open(
                format=pa.paInt16, channels=channels, rate=rate,
                input=True, input_device_index=int(info["index"]),
                frames_per_buffer=block_frames,
            )
            try:
                while not stop.is_set():
                    # Poll availability so stopping never waits for a full read
                    # on a silent/unplugged device. Native errors are reported.
                    ready = stream.get_read_available()
                    if ready < block_frames:
                        if not stream.is_active():
                            raise OSError("Audio device stream stopped unexpectedly")
                        stop.wait(0.005)
                        continue
                    try:
                        pcm = stream.read(min(ready, block_frames * 5), exception_on_overflow=True)
                    except OSError as exc:
                        if getattr(exc, "errno", None) == pa.paInputOverflowed or (
                                exc.args and exc.args[0] == pa.paInputOverflowed):
                            report(f"{source}: microphone input overflow; an audio gap occurred")
                            continue
                        raise
                    emit(source, converter.convert(pcm))
            finally:
                try:
                    stream.stop_stream()
                finally:
                    stream.close()

    def stop(self) -> None:
        """Signal immediately, then release worker-owned devices (normally <100ms).

        A hung OS/consumer callback is bounded to two seconds; starting again is
        refused while old capture workers remain alive, preventing duplicate input.
        """
        with self._lock:
            self._stop.set()
            self._running = False
            workers = [*self._workers]
            if self._dispatcher:
                workers.append(self._dispatcher)
        deadline = time.monotonic() + 2
        current = threading.current_thread()
        for worker in workers:
            if worker is not current:
                worker.join(max(0.0, deadline - time.monotonic()))
