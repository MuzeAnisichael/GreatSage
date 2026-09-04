from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

import uvicorn

from .server import create_app
from .settings import default_data_dir


def main():
    parser = argparse.ArgumentParser(description="GreatSage local desktop service")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--exclude-pid", type=int)
    parser.add_argument("--ui-dir", type=Path)
    args = parser.parse_args()
    token = os.environ.get("GREATSAGE_TOKEN") or secrets.token_urlsafe(32)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    connection = args.data_dir / "connection.json"
    connection.write_text(json.dumps({"baseUrl": f"http://127.0.0.1:{args.port}", "token": token}), encoding="utf-8")
    app = create_app(args.data_dir, token, args.exclude_pid, ui_dir=args.ui_dir)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
    finally:
        connection.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
