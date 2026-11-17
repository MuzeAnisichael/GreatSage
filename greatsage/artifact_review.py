"""Bounded presentation helpers; citation presence is not semantic verification."""
from __future__ import annotations

import difflib
import re

MAX_DIFF_INPUT_CHARS = 40_000
MAX_DIFF_INPUT_LINES = 1000
MAX_DIFF_OUTPUT_CHARS = 40_000
MAX_PASSAGES = 200
MAX_PASSAGE_CHARS = 2000
MAX_REVIEW_CHARS = 80_000
_MARKER = re.compile(r"\[S(\d{1,4})\]")


def bounded_diff(before: str, after: str, from_version: int, to_version: int) -> dict:
    """Bound both SequenceMatcher input and the returned unified diff."""
    def limited(text):
        lines = text[:MAX_DIFF_INPUT_CHARS].splitlines()
        return lines[:MAX_DIFF_INPUT_LINES], len(text) > MAX_DIFF_INPUT_CHARS or len(lines) > MAX_DIFF_INPUT_LINES

    old, old_cut = limited(before)
    new, new_cut = limited(after)
    output, remaining, output_cut = [], MAX_DIFF_OUTPUT_CHARS, False
    for line in difflib.unified_diff(old, new, fromfile=f"v{from_version}", tofile=f"v{to_version}", n=3, lineterm=""):
        entry = line + "\n"
        if len(entry) > remaining:
            output.append(entry[:remaining])
            output_cut = True
            break
        output.append(entry)
        remaining -= len(entry)
    return {"from_version": from_version, "to_version": to_version, "text": "".join(output),
            "truncated": old_cut or new_cut or output_cut, "input_truncated": old_cut or new_cut,
            "output_truncated": output_cut}


def review_passages(content: str, citations: list[dict]) -> tuple[list[dict], bool]:
    """Map visible Markdown body lines to citation markers, without judging claims."""
    known = {item["marker"]: item for item in citations}
    rows, total, fence = [], 0, ""
    truncated = len(content) > MAX_REVIEW_CHARS
    for number, line in enumerate(content[:MAX_REVIEW_CHARS].splitlines(), 1):
        text = line.strip()
        opening = re.match(r"(`{3,}|~{3,})", text)
        if opening:
            if not fence:
                fence = opening[1][0]
            elif opening[1][0] == fence:
                fence = ""
            continue
        if fence or not text or text.startswith("#") or text in {"---", "***", "___"}:
            continue
        if len(rows) >= MAX_PASSAGES or total + min(len(text), MAX_PASSAGE_CHARS) > MAX_REVIEW_CHARS:
            truncated = True
            break
        markers = list(dict.fromkeys("S" + item for item in _MARKER.findall(text)))
        unknown = [marker for marker in markers if marker not in known]
        invalid = [marker for marker in markers if marker in known and not known[marker]["valid"]]
        status = "unknown" if unknown else "invalid" if invalid else "cited" if markers else "uncited"
        cut = len(text) > MAX_PASSAGE_CHARS
        rows.append({"line_number": number, "text": text[:MAX_PASSAGE_CHARS], "text_truncated": cut,
                     "markers": markers, "invalid_markers": invalid, "unknown_markers": unknown,
                     "citation_status": status})
        total += min(len(text), MAX_PASSAGE_CHARS)
        truncated = truncated or cut
    return rows, truncated
