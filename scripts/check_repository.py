"""Reject accidentally tracked runtime data and locally configured credentials."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from greatsage.settings import read_env


def main():
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    credentials = [read_env(name).encode() for name in ('OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN') if read_env(name)]
    failures = []
    forbidden = {'.runtime', '.venv', 'node_modules', 'recordings', 'logs', 'data', 'release', 'dist'}
    for name in filter(None, paths):
        path = Path(name)
        if path.parts[0] in forbidden or path.suffix in {'.db', '.sqlite3', '.key', '.pem'} or (path.name.startswith('.env') and path.name != '.env.example'):
            failures.append(name + ': runtime or credential file must be ignored')
        content = subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)
        if any(secret in content for secret in credentials):
            failures.append(name + ': configured credential detected')
    for failure in failures:
        print(failure)
    print('Repository check:', 'FAILED' if failures else 'passed')
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
