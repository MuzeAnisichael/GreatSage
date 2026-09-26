"""Repeatable, isolated v0.2 evaluations. Default runs never call models or devices."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from greatsage import __version__
from greatsage.evaluation import classification, compare_reports
from greatsage.memory import MemoryStore
from greatsage.runtime import explicit_request


def load_corpus(name):
    raw = (ROOT / "evals" / f"{name}.json").read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["decisions", "memory", "all"], default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path, help="Compare against a previously written report for the same corpus")
    args = parser.parse_args()
    suites = ["decisions", "memory"] if args.suite == "all" else [args.suite]
    reports = {suite: offline(suite) for suite in suites}
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
