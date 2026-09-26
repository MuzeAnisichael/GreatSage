"""Repeatable, isolated v0.2 evaluations. Default runs never call models or devices."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from greatsage import __version__
from greatsage.evaluation import classification, compare_reports, distribution
from greatsage.memory import MemoryStore
from greatsage.runtime import explicit_request
from greatsage.providers import Providers
from greatsage.settings import SettingsStore
from greatsage.semantic import fingerprint, text_hash, fuse


def load_corpus(name):
    corpus = json.loads((ROOT / "evals" / f"{name}.json").read_text(encoding="utf-8"))
    canonical = json.dumps(corpus, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return corpus, hashlib.sha256(canonical).hexdigest()


def offline(name):
    corpus, digest = load_corpus(name)
    report = {"schema_version": 1, "suite": name, "version": __version__, "corpus_sha256": digest,
              "configuration": {"mode": "offline", "model_calls": False}, "cases": []}
    if name == "decisions":
        report["cases"] = [{"id": row["id"], "expected": row["respond"], "actual": explicit_request(row["text"])}
                           for row in corpus["cases"]]
        report["metrics"] = classification(report["cases"])
    else:
        temporary_root = ROOT / ".runtime" / "eval"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="memory-", dir=temporary_root) as directory:
            store = MemoryStore(Path(directory))
            try:
                mapping = {}
                for record in corpus["records"]:
                    row = store.add_message("user", record["text"], session_id=store.new_session())
                    mapping[row["id"]] = record["id"]
                for query in corpus["queries"]:
                    results = [mapping[row["id"]] for row in store.search(query["text"], limit=3)]
                    expected = set(query["expected"])
                    report["cases"].append({"id": query["id"], "retrieved": results,
                                            "expected": sorted(expected),
                                            "recall_at_3": len(expected & set(results)) / len(expected)})
                report["metrics"] = {"cases": len(report["cases"]),
                                     "recall_at_3": sum(row["recall_at_3"] for row in report["cases"]) / len(report["cases"])}
            finally:
                store.close()
    return report


async def live_memory(args):
    corpus, digest = load_corpus("memory")
    settings = SettingsStore(args.settings_dir)
    config = settings.provider("embedding")
    if args.embedding_provider:
        config.update(provider=args.embedding_provider, api_key="", api_key_env="")
        config["base_url"] = "http://127.0.0.1:11434" if args.embedding_provider == "ollama" else "https://openrouter.ai/api/v1"
        if args.embedding_provider == "openrouter":
            config["api_key"] = settings.provider("embedding")["api_key"]
    if args.embedding_model:
        config["model"] = args.embedding_model
    version = fingerprint(config)
    report = {"schema_version": 1, "suite": "memory", "version": __version__, "corpus_sha256": digest,
              "configuration": {"mode": "live", "provider": config["provider"], "model": config["model"], "model_calls": True}, "cases": [], "usage": []}
    temporary_root = ROOT / ".runtime" / "eval"
    temporary_root.mkdir(parents=True, exist_ok=True)
    providers = Providers()
    try:
        with tempfile.TemporaryDirectory(prefix="live-memory-", dir=temporary_root) as directory:
            store = MemoryStore(Path(directory))
            try:
                mapping = {}
                originals = [store.add_message("user", row["text"], session_id=store.new_session()) for row in corpus["records"]]
                indexed = await providers.embed(config, [row["text"] for row in originals])
                report["usage"].append(indexed["usage"])
                for fixture, row, vector in zip(corpus["records"], originals, indexed["vectors"]):
                    mapping[row["id"]] = fixture["id"]
                    store.save_vectors(row["id"], version, text_hash(row["text"]), [vector])
                for query in corpus["queries"]:
                    started = time.monotonic()
                    result = await providers.embed(config, [query["text"]])
                    hits = fuse(store.search(query["text"], 12), store.semantic_search(result["vectors"][0], version), limit=3)
                    retrieved = [mapping[row["id"]] for row in hits]
                    report["usage"].append(result["usage"])
                    report["cases"].append({"id": query["id"], "retrieved": retrieved, "expected": query["expected"],
                                            "recall_at_3": len(set(query["expected"]) & set(retrieved)) / len(query["expected"]),
                                            "latency_ms": (time.monotonic() - started) * 1000})
                    report["cases"][-1]["within_runtime_wait_cap"] = report["cases"][-1]["latency_ms"] <= config.get("query_timeout_seconds", 1.5) * 1000
                report["metrics"] = {"cases": len(report["cases"]), "recall_at_3": sum(row["recall_at_3"] for row in report["cases"]) / len(report["cases"])}
                report["latency_ms"] = distribution(row["latency_ms"] for row in report["cases"])
                report["configuration"]["runtime_query_wait_cap_seconds"] = config.get("query_timeout_seconds", 1.5)
                report["metrics"]["queries_within_wait_cap"] = sum(row["within_runtime_wait_cap"] for row in report["cases"])
            finally:
                store.close()
    finally:
        await providers.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["decisions", "memory", "all"], default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path, help="Compare against a previously written report for the same corpus")
    parser.add_argument("--live", action="store_true", help="Call the configured embedding service, using only fictional fixtures")
    parser.add_argument("--settings-dir", type=Path, default=ROOT / ".runtime")
    parser.add_argument("--embedding-provider", choices=["openrouter", "ollama"])
    parser.add_argument("--embedding-model")
    args = parser.parse_args()
    suites = ["decisions", "memory"] if args.suite == "all" else [args.suite]
    reports = {suite: asyncio.run(live_memory(args)) if args.live and suite == "memory" else offline(suite) for suite in suites}
    output = {"reports": reports}
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))["reports"]
        output["comparison"] = {name: compare_reports(baseline[name], report) for name, report in reports.items()}
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
