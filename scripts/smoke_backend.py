"""Black-box source/frozen backend smoke with loopback-only synthetic services.

Does not read real history, record audio, contact a model provider, execute a
command tool or write outside its fresh .runtime/backend-smoke directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


class Fixture(BaseHTTPRequestHandler):
    tool_calls = 0
    model_calls = 0

    def log_message(self, *args):
        pass

    def send(self, value=None, status=200, content_type="application/json"):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode() if value is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.send(status=405)

    def do_DELETE(self):
        self.send(status=204)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/mcp":
            if "id" not in payload:
                return self.send(status=202)
            method = payload["method"]
            if method == "initialize":
                result = {"protocolVersion": payload["params"]["protocolVersion"], "capabilities": {"tools": {}},
                          "serverInfo": {"name": "greatsage-synthetic-fixture", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "fixture_echo", "description": "Return a synthetic integer.",
                    "inputSchema": {"type": "object", "properties": {"value": {"type": "integer"}},
                                    "required": ["value"], "additionalProperties": False},
                    "annotations": {"readOnlyHint": True}}]}
            elif method == "tools/call":
                type(self).tool_calls += 1
                result = {"content": [{"type": "text", "text": "SYNTHETIC TOOL RESULT"}],
                          "structuredContent": {"value": payload["params"]["arguments"]["value"]}, "isError": False}
            else:
                return self.send({"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "Unknown method"}})
            return self.send({"jsonrpc": "2.0", "id": payload["id"], "result": result})
        if self.path != "/v1/chat/completions":
            return self.send(status=404)
        type(self).model_calls += 1
        messages = payload["messages"]
        system = messages[0]["content"]
        reason = "stop"
        if "meeting minutes" in system:
            records = json.loads(messages[1]["content"])["records"]
            extraction = {"summary": "Synthetic meeting summary.",
                          "points": [{"text": "Synthetic source checked.", "sources": [records[0]["handle"]]}],
                          "decisions": [], "todos": [], "questions": []}
            delta = {"content": json.dumps(extraction)}
        elif "document draft" in system:
            delta = {"content": "# Synthetic document\nSource checked [S1]."}
        elif any(message["role"] == "tool" for message in messages):
            delta = {"content": "Synthetic tool completed once."}
        else:
            name = next(tool["function"]["name"] for tool in payload["tools"] if tool["function"]["name"].startswith("mcp_"))
            delta = {"tool_calls": [{"index": 0, "id": "fixture-call", "type": "function",
                                    "function": {"name": name, "arguments": '{"value":7}'}}]}
            reason = "tool_calls"
        def chunk(value, finish=None):
            return {"id": "fixture-chat", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                    "choices": [{"index": 0, "delta": value, "finish_reason": finish}]}
        stream = [chunk({"role": "assistant", **delta}), chunk({}, reason)]
        body = "".join("data: " + json.dumps(item) + "\n\n" for item in stream) + "data: [DONE]\n\n"
        self.send(body.encode(), content_type="text/event-stream")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, help="Frozen backend; omit to use this Python interpreter.")
    args = parser.parse_args()
    destination = ROOT / ".runtime" / "backend-smoke" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3))
    destination.mkdir(parents=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fixture_url = f"http://127.0.0.1:{server.server_port}"
    token, port = secrets.token_urlsafe(32), free_port()
    opener = build_opener(ProxyHandler({}))
    command = [str(args.executable.resolve())] if args.executable else [sys.executable, "-m", "greatsage"]
    command += ["--data-dir", str(destination / "data"), "--port", str(port)]
    env = {**os.environ, "GREATSAGE_TOKEN": token, "OPENROUTER_API_KEY": "", "OPENAI_API_KEY": "",
           "OPENROUTER_BASE_URL": fixture_url + "/v1", "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost", "HF_HOME": str(destination / "hf-cache")}
    checks, process, log = [], None, None

    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    def request(path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    def until(callback, label, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("Backend exited: " + label)
            try:
                value = callback()
                if value:
                    return value
            except (URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(.1)
        raise AssertionError("Timed out: " + label)

    def start():
        nonlocal process, log
        log = (destination / "backend.log").open("ab")
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        until(lambda: request("/health").get("ok"), "startup")

    def stop():
        nonlocal process, log
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log:
            log.close()

    result = {"mode": "frozen" if args.executable else "source", "success": False, "checks": checks}
    try:
        start()
        request("/api/settings", "PUT", {"llm": {"provider": "openai", "base_url": fixture_url + "/v1", "model": "fixture", "api_key_env": ""},
            "voice_enabled": False, "semantic_decisions": False, "embedding": {"enabled": False}, "audit_content": False,
            "mcp_servers": [{"id": "fixture", "url": fixture_url + "/mcp", "token_env": ""}]})
        check(not request("/api/status")["listening"], "Audio capture remains off")
        check(request("/api/mcp/servers")[0]["tools"] == [], "No implicit MCP discovery")
        discovered = request("/api/mcp/servers/fixture/discover", "POST")
        check(len(discovered[0]["tools"]) == 1, "Real HTTP MCP discovery through SDK")
        request("/api/chat", "POST", {"text": "SYNTHETIC: call fixture_echo with value 7."})
        approval = until(lambda: request("/api/approvals"), "first approval")[0]
        task_id = approval["task_id"]
        check(Fixture.tool_calls == 0, "No tool execution before a click")
        inspection = request("/api/inspector")
        graph = next(row["graph"] for row in inspection["tasks"] if row["id"] == task_id)
        check(graph["checkpoint_id"] and graph["next"], "Approval checkpoint persisted")
        # Simulate process interruption while waiting, then use only public APIs.
        stop()
        start()
        check(request("/api/approvals") == [], "Restart revokes pending authorization")
        check(request("/api/mcp/servers")[0]["tools"] == [], "Restart requires explicit MCP rediscovery")
        request("/api/mcp/servers/fixture/discover", "POST")
        request(f"/api/tasks/{task_id}/resume", "POST")
        approval = until(lambda: request("/api/approvals"), "renewed approval")[0]
        check(Fixture.tool_calls == 0, "Resume waits for a fresh click")
        request(f"/api/tool-calls/{approval['id']}/approve", "POST", {"scope": "once"})
        def finished(id):
            task = request(f"/api/tasks/{id}")
            return task if task["status"] in ("succeeded", "failed") else None
        action = until(lambda: finished(task_id), "tool task completion")
        check(action["status"] == "succeeded" and Fixture.tool_calls == 1, "Resumed tool runs exactly once and model replies")
        minutes = request("/api/tasks", "POST", {"title": "Synthetic minutes", "use_session": True,
            "session_ids": [action["session_id"]], "document": True})
        completed = until(lambda: finished(minutes["id"]), "minutes completion")
        check(completed["status"] == "succeeded", "Structured extraction and document graph complete")
        check({item["kind"] for item in completed["artifacts"]} == {"minutes", "todos", "document"}, "Three artifacts persisted")
        inspection = request("/api/inspector")
        check(inspection["requests"] and all("content" not in row for row in inspection["requests"]), "Inspector keeps request bodies private")
        check(any(row["metadata"].get("message_manifest") for row in inspection["requests"]), "Message and budget metadata recorded")
        check(not inspection["listening"] and Fixture.tool_calls == 1, "No microphone or duplicate side effects")
        result["success"] = True
    except Exception as error:
        # Only fixtures are used, but avoid echoing HTTP bodies or connection data.
        result["error"] = str(error) if isinstance(error, AssertionError) else type(error).__name__
    finally:
        stop()
        server.shutdown()
        server.server_close()
        result["model_calls"] = Fixture.model_calls
        result["tool_calls"] = Fixture.tool_calls
        (destination / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**result, "result_file": str(destination / "result.json")}, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
