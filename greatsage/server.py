"""Loopback-only authenticated HTTP/WebSocket interface for the desktop UI."""
from __future__ import annotations

import asyncio
import anyio
import audioop
import contextlib
import io
import secrets
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .runtime import Runtime, redact
from .providers import ProviderError
from .budget import prepare as prepare_tokenizer, TokenCounter
from .semantic import fingerprint


def create_app(data_dir: Path, token: str, exclude_pid=None, runtime=None, ui_dir=None):
    runtime = runtime or Runtime(data_dir, exclude_pid)
    ui_dir = Path(ui_dir or Path(__file__).resolve().parent.parent / "ui")

    @asynccontextmanager
    async def lifespan(app):
        await runtime.start()
        yield
        await runtime.close()

    app = FastAPI(title="GreatSage", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    async def authorized(request: Request):
        auth = request.headers.get("authorization", "")
        if not secrets.compare_digest(auth.encode("utf-8"), f"Bearer {token}".encode("utf-8")):
            raise HTTPException(401, "需要有效的本地连接凭据。")

    @app.middleware("http")
    async def headers_and_limits(request, call_next):
        try:
            length = int(request.headers.get("content-length", 0))
        except ValueError:
            return JSONResponse({"detail": "无效请求长度。"}, status_code=400)
        if length > 2_000_000:
            return JSONResponse({"detail": "请求过大。"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self' ws://127.0.0.1:*; "
            "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid_input(request, exc):
        return JSONResponse({"detail": redact(str(exc))}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing_record(request, exc):
        return JSONResponse({"detail": "记录不存在或已删除。"}, status_code=404)

    @app.exception_handler(ProviderError)
    async def provider_failed(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/health")
    async def health():
        return {"ok": True}

    api = [Depends(authorized)]

    @app.get("/api/status", dependencies=api)
    async def status():
        return runtime.status()

    @app.get("/api/settings", dependencies=api)
    async def settings():
        return runtime.settings.get()

    @app.put("/api/settings", dependencies=api)
    async def update_settings(patch: dict):
        was_listening = runtime.listening
        # Validate first, then stop/reconfigure the active pipeline.
        updated = runtime.settings.update(patch)
        await runtime.interrupt("settings_changed")
        await runtime.pause_background()
        runtime.background.retry()
        if was_listening:
            await runtime.set_listening(False)
            await runtime.set_listening(True)
        await runtime.emit("settings_updated", {"version": runtime.settings.version()})
        runtime._schedule_compression()
        return updated

    @app.get("/api/memory/index", dependencies=api)
    async def index_status():
        config = runtime.settings.raw()["embedding"]
        return {**runtime.memory.vector_status(fingerprint(config)), "enabled": config["enabled"],
                "background": runtime.background.snapshot()}

    @app.post("/api/memory/reindex", dependencies=api)
    async def reindex():
        await runtime.pause_background()
        runtime.memory.clear_vectors()
        runtime.background.retry()
        runtime._schedule_compression()
        await runtime.emit("index_reset", {"reason": "user_requested"})
        return {"ok": True}

    @app.post("/api/background/retry", dependencies=api)
    async def retry_background():
        runtime.background.retry()
        runtime._schedule_compression()
        return {"ok": True}

    @app.get("/api/tokenizer", dependencies=api)
    async def tokenizer_status():
        counter = TokenCounter(runtime.settings.raw()["llm"], runtime.data_dir)
        return {"method": counter.method, "requested": counter.name, "ready": counter.encoder is not None}

    @app.post("/api/tokenizer/prepare", dependencies=api)
    async def tokenizer_prepare():
        result = await prepare_tokenizer(runtime.settings.raw()["llm"], runtime.data_dir, runtime.providers._client)
        await runtime.emit("tokenizer_prepared", result)
        return result

    @app.get("/api/audio/sources", dependencies=api)
    async def sources():
        return await asyncio.to_thread(runtime.capture.sources)

    @app.post("/api/listening", dependencies=api)
    async def listening(body: dict):
        if not isinstance(body.get("enabled"), bool):
            raise ValueError("enabled 必须是布尔值。")
        return await runtime.set_listening(body["enabled"])

    @app.post("/api/chat", dependencies=api)
    async def chat(body: dict):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 24000:
            raise ValueError("请输入 1–24000 个字符的内容。")
        return await runtime.chat(text.strip())

    @app.post("/api/interrupt", dependencies=api)
    async def interrupt():
        await runtime.interrupt()
        return {"ok": True}

    @app.post("/api/playback", dependencies=api)
    async def playback(body: dict):
        if not isinstance(body.get("playing"), bool):
            raise ValueError("playing 必须是布尔值。")
        await runtime.playback(body["playing"], str(body.get("text", ""))[:10000],
                               str(body.get("trace_id", ""))[:128])
        return {"ok": True}

    @app.get("/api/audio/{key}", dependencies=api)
    async def audio(key: str):
        entry = runtime.audio_cache.get(key)
        if not entry:
            raise HTTPException(404, "语音已播放完、过期或被打断。")
        return Response(entry[1], media_type=entry[2])

    @app.get("/api/recordings/{message_id}", dependencies=api)
    async def recording(message_id: str):
        if len(message_id) != 32 or any(c not in "0123456789abcdef" for c in message_id):
            raise HTTPException(404)
        path = runtime.data_dir / "recordings" / f"{message_id}.wav"
        if not path.is_file():
            raise HTTPException(404, "未开启录音保存，或录音已删除／过期。")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/history", dependencies=api)
    async def history(session_id: str | None = None, limit: int = 100):
        return runtime.memory.history(min(max(limit, 1), 500), session_id)

    @app.get("/api/sessions", dependencies=api)
    async def sessions():
        return runtime.memory.sessions()

    @app.post("/api/sessions", dependencies=api)
    async def new_session():
        return await runtime.reset_session()

    @app.get("/api/events", dependencies=api)
    async def events(limit: int = 200):
        return redact(runtime.memory.events(min(max(limit, 1), 1000)))

    @app.get('/api/audit', dependencies=api)
    async def audit_list(trace_id: str | None = None, limit: int = 100):
        return runtime.memory.snapshots(trace_id, limit)

    @app.get('/api/audit/{snapshot_id}', dependencies=api)
    async def audit_detail(snapshot_id: str, include_content: bool = False):
        return runtime.memory.snapshot(snapshot_id, include_content)

    @app.post('/api/audit/{snapshot_id}/export', dependencies=api)
    async def audit_export(snapshot_id: str, body: dict):
        if not isinstance(body.get('include_content', False), bool):
            raise ValueError('include_content must be a boolean')
        record = runtime.memory.snapshot(snapshot_id, body.get('include_content', False))
        await runtime.emit('audit_export', {'snapshot_id': snapshot_id, 'include_content': body.get('include_content', False)}, record['trace_id'])
        return JSONResponse({'format': 'greatsage-request-audit-v1', 'snapshot': record},
                            headers={'Content-Disposition': f'attachment; filename="greatsage-audit-{snapshot_id}.json"'})

    @app.get("/api/memories", dependencies=api)
    async def memories():
        return runtime.memory.list_memories()

    @app.post("/api/memories", dependencies=api)
    async def add_memory(body: dict):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise ValueError("记忆内容需要 1–12000 个字符。")
        await runtime.pause_background()
        return await runtime.add_memory_checked(text.strip())

    @app.get("/api/memory/conflicts", dependencies=api)
    async def memory_conflicts():
        return runtime.memory.conflicts()

    @app.post("/api/memory/conflicts/{candidate_id}", dependencies=api)
    async def resolve_conflict(candidate_id: str, body: dict):
        if body.get("resolution") not in ("replace", "keep_existing", "keep_both"):
            raise ValueError("请选择替换、保留旧记忆或同时保留。")
        await runtime.before_delete()
        result = runtime.memory.resolve_conflict(candidate_id, body["resolution"])
        await runtime.emit("memory_updated", {**result, "action": "resolve_conflict"})
        runtime._schedule_compression()
        return result

    @app.get("/api/records/{record_id}", dependencies=api)
    async def record_details(record_id: str):
        return runtime.memory.record_details(record_id)

    @app.get("/api/history/page", dependencies=api)
    async def history_page(cursor: int | None = None, session_id: str | None = None, limit: int = 30):
        return runtime.memory.history_page(cursor, session_id, limit)

    @app.get("/api/memory/search", dependencies=api)
    async def search(q: str):
        return runtime.memory.search(q[:2000])

    @app.get("/api/summaries", dependencies=api)
    async def summaries():
        return runtime.memory.summaries(100)

    @app.put("/api/memories/{record_id}", dependencies=api)
    async def revise_memory(record_id: str, body: dict):
        await runtime.before_delete()
        result = runtime.memory.revise_memory(record_id, str(body.get("text", "")))
        await runtime.emit("memory_updated", {"id": result["id"], "action": "revise"})
        return result

    @app.delete("/api/memories/{record_id}", dependencies=api)
    async def delete_memory(record_id: str):
        await runtime.before_delete()
        runtime.memory.delete_memory(record_id)
        await runtime.emit("memory_updated", {"deleted_id": record_id})
        return {"ok": True}

    @app.put("/api/history/{record_id}", dependencies=api)
    async def revise_history(record_id: str, body: dict):
        await runtime.before_delete()
        result = runtime.memory.revise_message(record_id, str(body.get("text", "")))
        if len(record_id) == 32 and all(c in "0123456789abcdef" for c in record_id):
            (runtime.data_dir / "recordings" / f"{record_id}.wav").unlink(missing_ok=True)
        await runtime.emit("memory_updated", {"id": result["id"], "action": "revise"})
        return result

    @app.delete("/api/history/{record_id}", dependencies=api)
    async def delete_history(record_id: str):
        await runtime.before_delete()
        runtime.memory.delete_message(record_id)
        # ID validation prevents a deletion path escaping recordings/.
        if len(record_id) == 32 and all(c in "0123456789abcdef" for c in record_id):
            (runtime.data_dir / "recordings" / f"{record_id}.wav").unlink(missing_ok=True)
        await runtime.emit("memory_updated", {"deleted_id": record_id})
        return {"ok": True}

    @app.post("/api/history/clear", dependencies=api)
    async def clear_history(body: dict):
        if body.get("confirmation") != "DELETE":
            raise ValueError("清空历史需要明确确认。")
        await runtime.before_delete()
        runtime.memory.clear_history()
        for path in (runtime.data_dir / "recordings").glob("*.wav"):
            path.unlink(missing_ok=True)
        await runtime.emit("memory_updated", {"cleared": True})
        return {"ok": True}

    @app.get("/api/skills", dependencies=api)
    async def skills():
        return runtime.skills.list()

    @app.post("/api/skills/import", dependencies=api)
    async def import_skills(body: dict):
        path = body.get("path")
        if not isinstance(path, str) or not path.strip() or len(path) > 2000:
            raise ValueError("请选择有效的本地技能目录。")
        result = await asyncio.to_thread(runtime.skills.import_path, path)
        await runtime.emit("skills_updated", {"count": len(result)})
        return result

    @app.put("/api/skills/{skill_id}", dependencies=api)
    async def enable_skill(skill_id: str, body: dict):
        if not isinstance(body.get("enabled"), bool):
            raise ValueError("enabled 必须是布尔值。")
        result = runtime.skills.set_enabled(skill_id, body["enabled"])
        await runtime.emit("skills_updated", {"id": skill_id})
        return result

    @app.delete("/api/skills/{skill_id}", dependencies=api)
    async def remove_skill(skill_id: str):
        runtime.skills.remove(skill_id)
        await runtime.emit("skills_updated", {"removed_id": skill_id})
        return {"ok": True}

    @app.get("/api/voices", dependencies=api)
    async def voices():
        return await runtime.providers.voices(runtime._provider("tts"))

    @app.post("/api/providers/warmup", dependencies=api)
    async def warmup():
        return await runtime.providers.warmup(runtime._provider("asr"))

    @app.post("/api/providers/test", dependencies=api)
    async def test_provider(body: dict):
        component = body.get("component")
        if component not in ("llm", "asr", "tts", "embedding"):
            raise ValueError("无效的服务类型。")
        started = time.time()
        config = runtime._provider(component)
        try:
            if component == "llm":
                config["max_tokens"] = 32
                answer = ""
                async for item in runtime.providers.stream_chat(config, [{"role": "user", "content": "Reply only OK."}]):
                    answer += item.get("text", "")
                detail = answer
            elif component == "embedding":
                result = await runtime.providers.embed(config, ["GreatSage 语义检索测试。"])
                detail = f"已生成 {result['dimensions']} 维向量。"
            elif component == "tts":
                result = await runtime.providers.synthesize(config, "Great Sage is ready.", "en")
                detail = f"已生成 {len(result['audio'])} 字节语音（未自动播放）。"
            else:
                sample = await runtime.providers.synthesize({"provider": "system", "voice": "", "cache_dir": str(runtime.data_dir / "models")}, "Great Sage is ready.", "en")
                with wave.open(io.BytesIO(sample["audio"]), "rb") as wav:
                    pcm = wav.readframes(wav.getnframes())
                    if wav.getnchannels() == 2:
                        pcm = audioop.tomono(pcm, wav.getsampwidth(), .5, .5)
                    if wav.getsampwidth() != 2:
                        pcm = audioop.lin2lin(pcm, wav.getsampwidth(), 2)
                    sample_rate = wav.getframerate()
                result = await runtime.providers.transcribe({**config, "language": "en"}, pcm, sample_rate)
                detail = result["text"]
            metrics = {"ok": True, "component": component, "detail": detail,
                       "provider": config["provider"], "model": config["model"],
                       "latency_ms": round((time.time()-started)*1000)}
            await runtime.emit("provider_test", metrics)
            return metrics
        except Exception as exc:
            await runtime.emit("error", {"message": str(exc), "component": component})
            raise HTTPException(502, redact(str(exc)))

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        if not secrets.compare_digest(ws.query_params.get("token", "").encode("utf-8"), token.encode("utf-8")):
            await ws.close(code=1008)
            return
        origin = ws.headers.get("origin")
        if origin and origin not in {f"http://{ws.headers.get('host')}", "http://testserver"}:
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = asyncio.Queue(maxsize=500)
        runtime.subscribers.add(queue)
        await ws.send_json({"kind": "state", "data": runtime.status(), "created_at": time.time()})

        async def sender():
            while True:
                await ws.send_json(await queue.get())

        async def receiver():
            while True:
                await ws.receive_text()  # Also detects disconnects during quiet periods.

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            runtime.subscribers.discard(queue)
            for task in tasks:
                task.cancel()
            # Disconnect cancellation can race with sender shutdown. Finish
            # draining both local tasks even under an ASGI cancellation scope.
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)

    if ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
    return app
