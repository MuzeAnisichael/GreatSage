"""Verify desktop shutdown/parent EOF against a real isolated backend process.

No microphones, model requests or user data. Can also check a frozen backend.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


def run(executable=None):
    directory = ROOT / ".runtime" / "shutdown-smoke" / secrets.token_hex(8)
    directory.mkdir(parents=True)
    opener = build_opener(ProxyHandler({}))
    results = []
    for method in ("shutdown", "eof"):
        data = directory / method
        data.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_urlsafe(24)
        command = [str(executable)] if executable else [sys.executable, "-m", "greatsage"]
        command += ["--data-dir", str(data), "--port", str(port), "--shutdown-stdin"]
        with (data / "process.log").open("w", encoding="utf-8") as log:
            child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                                     stdout=log, stderr=log, text=True,
                                     env={**os.environ, "GREATSAGE_TOKEN": token})
            try:
                deadline = time.monotonic() + 40
                while True:
                    if child.poll() is not None:
                        raise RuntimeError("Backend exited before ready; inspect isolated process.log")
                    try:
                        request = Request(f"http://127.0.0.1:{port}/api/status",
                                          headers={"Authorization": f"Bearer {token}"})
                        with opener.open(request, timeout=.5) as response:
                            assert response.status == 200
                        break
                    except (URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Backend readiness timeout") from None
                        time.sleep(.1)
                started = time.monotonic()
                if method == "shutdown":
                    child.stdin.write("shutdown\n")
                    child.stdin.flush()
                child.stdin.close()
                code = child.wait(timeout=5)
                assert code == 0, f"{method}: backend exit was not clean"
                assert not (data / "connection.json").exists(), "Lifespan did not finish"
                connection = sqlite3.connect(data / "memory.sqlite3")
                try:
                    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                finally:
                    connection.close()
                results.append({"method": method, "ok": True,
                                "exit_ms": round((time.monotonic() - started) * 1000)})
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
    report = {"checks": results, "data_dir": str(directory)}
    (directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path)
    run(parser.parse_args().executable)
