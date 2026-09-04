"""Windows process-tree loopback capture using the native WASAPI activation API.

No device driver, virtual cable or compiler is needed. The ABI and activation
sequence follow Microsoft's ApplicationLoopback sample:
https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/ApplicationLoopback
This captures render streams for a PID *and its descendants*, across endpoints.
It is not acoustic echo cancellation and cannot capture protected audio.
"""

from __future__ import annotations

import ctypes as C
import sys
import threading
import time
import uuid
from collections.abc import Callable


HRESULT = C.c_int32
DWORD = C.c_uint32
UINT64 = C.c_uint64
PTR = C.c_void_p
WINFUNCTYPE = getattr(C, "WINFUNCTYPE", C.CFUNCTYPE)


class GUID(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]

    @classmethod
    def parse(cls, value: str) -> "GUID":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


IID_AUDIO_CLIENT = GUID.parse("1cb9ad4c-dbfa-4c32-b178-c2f568a703b2")
IID_CAPTURE_CLIENT = GUID.parse("c8adbd64-e71e-48a0-a4de-185c395cd317")
_HANDLER_IIDS = {
    uuid.UUID(value).bytes_le
    for value in (
        "00000000-0000-0000-c000-000000000046",  # IUnknown
        "41d949ab-9862-444a-80f6-c261334da5eb",  # completion handler
        "94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90",  # IAgileObject (marker)
    )
}


class ActivationParams(C.Structure):
    _fields_ = [("activation_type", DWORD), ("process_id", DWORD), ("mode", DWORD)]


class Blob(C.Structure):
    _fields_ = [("size", DWORD), ("data", PTR)]


class VariantValue(C.Union):
    _fields_ = [("blob", Blob), ("padding", C.c_ubyte * 16)]


class PropVariant(C.Structure):
    _fields_ = [("vt", C.c_uint16), ("reserved", C.c_uint16 * 3), ("value", VariantValue)]


class WaveFormat(C.Structure):
    _pack_ = 1
    _fields_ = [
        ("tag", C.c_uint16), ("channels", C.c_uint16), ("rate", DWORD),
        ("bytes_per_sec", DWORD), ("block_align", C.c_uint16),
        ("bits", C.c_uint16), ("extra_size", C.c_uint16),
    ]


def available() -> bool:
    """OS/API capability, not a promise that a specific audio stream is readable."""
    return sys.platform == "win32" and sys.getwindowsversion().build >= 20348


def _check(hr: int, action: str) -> None:
    if hr < 0:
        raise OSError(f"{action} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def _method(pointer: PTR, slot: int, *argtypes: object, restype: object = HRESULT):
    vtable = C.cast(pointer, C.POINTER(C.POINTER(PTR))).contents
    return WINFUNCTYPE(restype, PTR, *argtypes)(vtable[slot])


def _release(pointer: PTR) -> None:
    if pointer:
        _method(pointer, 2, restype=DWORD)(pointer)


# Windows may call back after start has timed out. Keep the Python COM object
# alive until Windows releases its reference; dropping a ctypes callback crashes.
_handlers: dict[int, "_ActivationHandler"] = {}
_handler_lock = threading.RLock()


class _ActivationHandler:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result = PTR()
        self.error: Exception | None = None
        self.cancelled = False
        self.refs = 1
        self.lock = threading.RLock()
        self._callbacks = (
            WINFUNCTYPE(HRESULT, PTR, PTR, C.POINTER(PTR))(self._query),
            WINFUNCTYPE(DWORD, PTR)(self._addref),
            WINFUNCTYPE(DWORD, PTR)(self._release),
            WINFUNCTYPE(HRESULT, PTR, PTR)(self._complete),
        )
        self._vtable = (PTR * 4)(*(C.cast(fn, PTR).value for fn in self._callbacks))
        self._object = PTR(C.addressof(self._vtable))
        self.pointer = C.cast(C.pointer(self._object), PTR)
        with _handler_lock:
            _handlers[self.pointer.value] = self

    def _query(self, this, iid, out) -> int:
        if not out:
            return -2147467261  # E_POINTER
        out[0] = None
        if C.string_at(iid, 16) not in _HANDLER_IIDS:
            return -2147467262  # E_NOINTERFACE
        out[0] = self.pointer.value
        self._addref(this)
        return 0

    def _addref(self, _this) -> int:
        with self.lock:
            self.refs += 1
            return self.refs

    def _release(self, _this) -> int:
        with self.lock:
            self.refs -= 1
            remaining = self.refs
        if remaining == 0:
            with _handler_lock:
                _handlers.pop(self.pointer.value, None)
        return remaining

    def _complete(self, _this, operation) -> int:
        interface = PTR()
        try:
            status = HRESULT()
            _check(_method(operation, 3, C.POINTER(HRESULT), C.POINTER(PTR))(
                operation, C.byref(status), C.byref(interface)), "GetActivateResult")
            _check(status.value, "Activate process audio")
            with self.lock:
                if self.cancelled:
                    _release(interface)
                else:
                    self.result = interface
        except Exception as exc:
            _release(interface)
            self.error = exc
        finally:
            self.done.set()
        return 0

    def take_result(self) -> PTR:
        with self.lock:
            pointer, self.result = self.result, PTR()
            return pointer

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            pointer, self.result = self.result, PTR()
        _release(pointer)


def capture_process(
    process_id: int,
    include_tree: bool,
    stop_event: threading.Event,
    on_pcm: Callable[[bytes], None],
    on_warning: Callable[[str], None] | None = None,
) -> None:
    """Blocking worker entry point; delivers little-endian PCM16, 16 kHz, mono.

    With include_tree=False, all render audio except that PID tree is captured.
    Callback work should be short (enqueue the chunk for an async consumer).
    All stream COM objects are owned and released on this worker's MTA thread.
    """
    if not available():
        raise OSError("Process audio requires Windows build 20348 or newer")
    if not 0 < process_id <= 0xFFFFFFFF:
        raise ValueError("A positive Windows process ID is required")

    ole = C.WinDLL("ole32", use_last_error=True)
    mm = C.WinDLL("Mmdevapi", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    ole.CoInitializeEx.argtypes = [PTR, DWORD]
    ole.CoInitializeEx.restype = HRESULT
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    mm.ActivateAudioInterfaceAsync.argtypes = [
        C.c_wchar_p, C.POINTER(GUID), C.POINTER(PropVariant), PTR, C.POINTER(PTR),
    ]
    mm.ActivateAudioInterfaceAsync.restype = HRESULT
    kernel.CreateEventW.argtypes = [PTR, C.c_int, C.c_int, C.c_wchar_p]
    kernel.CreateEventW.restype = PTR
    kernel.WaitForSingleObject.argtypes = [PTR, DWORD]
    kernel.WaitForSingleObject.restype = DWORD
    kernel.CloseHandle.argtypes = [PTR]
    kernel.CloseHandle.restype = C.c_int

    _check(ole.CoInitializeEx(None, 0), "Initialize audio COM apartment")
    handler = _ActivationHandler()
    operation, client, capture = PTR(), PTR(), PTR()
    ready = None
    started = False
    try:
        params = ActivationParams(1, process_id, 0 if include_tree else 1)
        variant = PropVariant()
        variant.vt = 65  # VT_BLOB
        variant.value.blob = Blob(C.sizeof(params), C.cast(C.pointer(params), PTR))
        # Retain the pointed-to activation memory even when the worker cancels
        # before Windows completes its asynchronous activation.
        handler.activation_memory = (params, variant)
        _check(mm.ActivateAudioInterfaceAsync(
            "VAD\\Process_Loopback", C.byref(IID_AUDIO_CLIENT), C.byref(variant),
            handler.pointer, C.byref(operation)), "ActivateAudioInterfaceAsync")
        deadline = time.monotonic() + 8
        while not handler.done.wait(0.05):
            if stop_event.is_set():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows process-audio activation timed out")
        if handler.error:
            raise handler.error
        client = handler.take_result()
        if not client:
            raise OSError("Windows returned no process-audio interface")
        if stop_event.is_set():
            return

        # Ask the shared Windows engine to perform channel and sample conversion.
        # Flags: LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY.
        fmt = WaveFormat(1, 1, 16000, 32000, 2, 16, 0)
        _check(_method(client, 3, C.c_int, DWORD, C.c_int64, C.c_int64,
                       C.POINTER(WaveFormat), PTR)(
            client, 0, 0x88060000, 0, 0, C.byref(fmt), None), "Initialize process audio")
        ready = kernel.CreateEventW(None, False, False, None)
        if not ready:
            raise C.WinError(C.get_last_error())
        _check(_method(client, 13, PTR)(client, ready), "Set audio-ready event")
        _check(_method(client, 14, C.POINTER(GUID), C.POINTER(PTR))(
            client, C.byref(IID_CAPTURE_CLIENT), C.byref(capture)), "Get audio capture client")
        _check(_method(client, 10)(client), "Start process audio")
        started = True

        get_size = _method(capture, 5, C.POINTER(DWORD))
        get_buffer = _method(capture, 3, C.POINTER(PTR), C.POINTER(DWORD),
                             C.POINTER(DWORD), C.POINTER(UINT64), C.POINTER(UINT64))
        release_buffer = _method(capture, 4, DWORD)
        first_packet = True
        while not stop_event.is_set():
            waited = kernel.WaitForSingleObject(ready, 50)
            if waited == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
            if waited == 258:  # WAIT_TIMEOUT
                continue
            while not stop_event.is_set():
                count = DWORD()
                _check(get_size(capture, C.byref(count)), "Read audio packet size")
                if not count.value:
                    break
                data, flags = PTR(), DWORD()
                position, qpc = UINT64(), UINT64()
                _check(get_buffer(capture, C.byref(data), C.byref(count), C.byref(flags),
                                  C.byref(position), C.byref(qpc)), "Read process audio")
                try:
                    payload = (bytes(count.value * 2) if flags.value & 2
                               else C.string_at(data, count.value * 2))
                finally:
                    _check(release_buffer(capture, count), "Release audio packet")
                if flags.value & 1 and not first_packet and on_warning:
                    on_warning("WASAPI audio discontinuity: one or more packets were lost")
                first_packet = False
                if payload and not stop_event.is_set():
                    on_pcm(payload)
    finally:
        if started:
            _method(client, 11)(client)
        _release(capture)
        _release(client)
        if ready:
            kernel.CloseHandle(ready)
        handler.cancel()
        _release(operation)
        handler._release(handler.pointer)
        ole.CoUninitialize()
