from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
from pathlib import Path

import uvicorn

from .server import create_app
from .settings import default_data_dir


def _watch_desktop_pipe(server, stream):
    """Only the owning desktop's private pipe can request this shutdown path."""
    try:
        while True:
            line = stream.readline(128)
            if not line or line.rstrip("\r\n") == "shutdown":
                server.should_exit = True
                return
    except (OSError, ValueError):
        server.should_exit = True


def main():
    parser = argparse.ArgumentParser(description="GreatSage local desktop service")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--exclude-pid", type=int)
    parser.add_argument("--ui-dir", type=Path)
    parser.add_argument("--shutdown-stdin", action="store_true",
                        help="Exit gracefully when the owning desktop closes stdin or sends shutdown.")
    args = parser.parse_args()
    token = os.environ.get("GREATSAGE_TOKEN") or secrets.token_urlsafe(32)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    connection = args.data_dir / "connection.json"
    connection.write_text(json.dumps({"baseUrl": f"http://127.0.0.1:{args.port}", "token": token}), encoding="utf-8")
    app = create_app(args.data_dir, token, args.exclude_pid, ui_dir=args.ui_dir)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning",
        timeout_graceful_shutdown=3))
    if args.shutdown_stdin:
        threading.Thread(target=_watch_desktop_pipe, args=(server, sys.stdin),
                         name="desktop-lifetime", daemon=True).start()
    try:
        server.run()
    finally:
        connection.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
