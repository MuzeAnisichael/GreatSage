"""Small, explicit metrics shared by repeatable evaluation commands."""
from __future__ import annotations

import math
import re
import unicodedata


def normalized(text: str) -> str:
    # Do not normalize homophones, numbers or simplified/traditional characters.
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


def distance(reference, actual) -> int:
    previous = list(range(len(actual) + 1))
    for i, wanted in enumerate(reference, 1):
        current = [i]
        for j, received in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (wanted != received)))
        previous = current
    return previous[-1]


def recognition(reference: str, actual: str) -> dict:
    wanted, received = normalized(reference), normalized(actual)
    words = lambda text: re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())
    ref_words, got_words = words(reference), words(actual)
    return {"cer": distance(wanted, received) / max(1, len(wanted)),
            "wer": distance(ref_words, got_words) / max(1, len(ref_words)),
            "reference_characters": len(wanted), "exact_normalized": wanted == received}


def distribution(values) -> dict:
    data = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not data:
        return {"count": 0, "p50": None, "p95": None}

    def percentile(fraction):
        position = (len(data) - 1) * fraction
        low = int(position)
        return round(data[low] + (data[min(low + 1, len(data) - 1)] - data[low]) * (position - low), 3)

    return {"count": len(data), "p50": percentile(.5), "p95": percentile(.95)}


def classification(rows: list[dict]) -> dict:
    tp = sum(bool(row["actual"]) and bool(row["expected"]) for row in rows)
    fp = sum(bool(row["actual"]) and not row["expected"] for row in rows)
    fn = sum(not row["actual"] and bool(row["expected"]) for row in rows)
    tn = len(rows) - tp - fp - fn
    return {"cases": len(rows), "true_positive": tp, "false_positive": fp,
            "false_negative": fn, "true_negative": tn,
            "accuracy": (tp + tn) / max(1, len(rows)),
            "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn)}


def compare_reports(baseline: dict, current: dict) -> dict:
    if baseline.get("suite") != current.get("suite") or baseline.get("corpus_sha256") != current.get("corpus_sha256"):
        raise ValueError("Only reports for the same suite and corpus version can be compared")
    differences = {}
    for name, value in current.get("metrics", {}).items():
        before = baseline.get("metrics", {}).get(name)
        if isinstance(value, (int, float)) and isinstance(before, (int, float)):
            differences[name] = {"baseline": before, "current": value, "delta": value - before}
    return {"suite": current["suite"], "baseline_version": baseline.get("version"),
            "current_version": current.get("version"), "metrics": differences,
            "configuration_changed": baseline.get("configuration") != current.get("configuration")}
