"""Explicit microphone dictation for an editable artifact correction."""
from __future__ import annotations

import asyncio
import audioop
from dataclasses import dataclass, field
from uuid import uuid4

from .audio import AudioCaptureManager, AudioChunk


@dataclass
class _Recording:
    artifact_id: str
    session_id: str
    expected_version: int
    data_epoch: int
    settings_version: str
    ready: asyncio.Event
    id: str = field(default_factory=lambda: uuid4().hex)
    pcm: bytearray = field(default_factory=bytearray)
    error: str = ""
    cancelled: bool = False
    finishing: bool = False
    deadline: asyncio.TimerHandle | None = None
    stopping: asyncio.Task | None = None
    transcription: asyncio.Task | None = None


class CorrectionVoice:
    """Own the microphone until stop/cancel. The only output is a draft."""

    max_seconds = 45
    open_timeout = 8
    asr_timeout = 120
    sample_rate = 16000

    def __init__(self, runtime, capture=None):
        self.runtime = runtime
        self.capture = capture if capture is not None else AudioCaptureManager()
        self._active: _Recording | None = None

    @property
    def busy(self):
        return self._active is not None

    def _check_target(self, recording):
        runtime = self.runtime
        if (recording.cancelled or runtime._session_switching or runtime.listening
                or runtime.session_id != recording.session_id
                or runtime.data_epoch != recording.data_epoch
                or runtime.settings.version() != recording.settings_version):
            raise ValueError("录音对应的会话或设置已更改，请重新录制。")
        artifact = runtime.memory.artifact(recording.artifact_id)
        if (artifact.get("profile") != "continuation" or artifact.get("session_id") != recording.session_id
                or artifact["kind"] != "document" or artifact["current_version"] != recording.expected_version):
            raise ValueError("当前会话中的文档或版本已更改，请重新选择后录制。")

    async def start(self, artifact_id, session_id, expected_version):
        if self.busy:
            raise ValueError("已有语音纠正正在使用麦克风或转写，请先完成或取消。")
        if self.runtime.listening:
            raise ValueError("请先停止常规监听，再录制语音纠正。")
        provider = self.runtime.settings.raw()["asr"]["provider"]
        if provider not in ("openrouter", "openai", "openai_compatible", "faster_whisper"):
            raise ValueError("当前识别服务只支持流式监听。请使用文字纠正，或在设置中选择可转写录音的服务。")
        recording = _Recording(artifact_id, session_id, expected_version, self.runtime.data_epoch,
                               self.runtime.settings.version(), asyncio.Event())
        self._check_target(recording)
        loop = asyncio.get_running_loop()
        self._active = recording
        try:
            self.capture.start(
                {"microphone": True, "microphone_device": self.runtime.settings.raw().get("microphone_device"),
                 "desktop_source": "none"},
                lambda chunk: loop.call_soon_threadsafe(self._append, recording, chunk),
                lambda message: loop.call_soon_threadsafe(self._capture_error, recording, message),
            )
            await asyncio.wait_for(recording.ready.wait(), self.open_timeout)
            if recording.error:
                raise ValueError(recording.error)
            self._check_target(recording)
            recording.deadline = loop.call_later(self.max_seconds, lambda: asyncio.create_task(self.cancel(recording.id)))
            return {"capture_id": recording.id, "max_seconds": self.max_seconds, "provider": provider}
        except TimeoutError:
            await self.cancel(recording.id)
            raise ValueError("麦克风未及时返回音频，请检查设备和 Windows 麦克风权限。") from None
        except (Exception, asyncio.CancelledError) as exc:
            await self.cancel(recording.id)
            if isinstance(exc, (OSError, RuntimeError)):
                raise ValueError(f"无法启动麦克风：{exc}") from None
            raise

    def _append(self, recording, chunk: AudioChunk):
        if (self._active is not recording or recording.cancelled or recording.finishing
                or not chunk.source.startswith("microphone:") or not chunk.pcm):
            return
        remaining = self.max_seconds * self.sample_rate * 2 - len(recording.pcm)
        if remaining > 0:
            recording.pcm.extend(chunk.pcm[:remaining])
        recording.ready.set()

    def _capture_error(self, recording, message):
        if self._active is recording and not recording.finishing:
            recording.error = f"麦克风录音中断，请检查设备后重新录制。{message}"
            recording.ready.set()

    async def _stop_device(self, recording):
        if recording.deadline:
            recording.deadline.cancel()
        if recording.stopping is None:
            recording.stopping = asyncio.create_task(asyncio.to_thread(self.capture.stop))
        await asyncio.shield(recording.stopping)

    async def cancel(self, capture_id=None):
        recording = self._active
        if recording and (capture_id is None or recording.id == capture_id):
            recording.cancelled = True
            recording.ready.set()
            recording.pcm.clear()
            if recording.transcription and not recording.transcription.done():
                recording.transcription.cancel()
            await self._stop_device(recording)
            if self._active is recording:
                self._active = None
        return {"ok": True}

    async def cancel_for_artifact(self, artifact_id):
        if self._active and self._active.artifact_id == artifact_id:
            await self.cancel(self._active.id)

    async def finish(self, capture_id):
        recording = self._active
        if not recording or recording.id != capture_id or recording.cancelled or recording.finishing:
            raise ValueError("录音已结束、取消或超时，请重新录制。")
        recording.finishing = True
        try:
            await self._stop_device(recording)
            self._check_target(recording)
            if recording.error:
                raise ValueError(recording.error)
            if not recording.pcm or audioop.max(recording.pcm, 2) < 8:
                raise ValueError("未检测到麦克风信号，请检查输入设备后重新录制。")
            config = self.runtime._provider("asr")
            recording.transcription = asyncio.create_task(self.runtime.providers.transcribe(
                config, bytes(recording.pcm), self.sample_rate))
            recording.pcm.clear()
            try:
                result = await asyncio.wait_for(recording.transcription, self.asr_timeout)
            except asyncio.CancelledError:
                if recording.cancelled:
                    raise ValueError("录音已取消。") from None
                raise
            except TimeoutError:
                raise ValueError("语音转写超时，请重试或输入文字纠正。") from None
            self._check_target(recording)
            text = result.get("text", "").strip()
            if not text:
                raise ValueError("未识别到语音，请重新录制或输入文字纠正。")
            return {"artifact_id": recording.artifact_id, "session_id": recording.session_id,
                    "expected_version": recording.expected_version, "text": text}
        finally:
            await self.cancel(recording.id)
