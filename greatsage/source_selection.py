"""Offline evidence selection inside an explicit, versioned scope.

Reuse the store's lexical ranking (including Chinese bigrams). Persist only
source IDs/hashes and selection reasons; excerpts are resolved live so deleting
a source does not leave a second private corpus in task metadata.
"""
from __future__ import annotations

import hashlib
import re

from .budget import TokenCounter
from .memory import _relevance

MAX_SOURCES, MAX_CHARS, MAX_SOURCE_CHARS, MAX_SCOPE = 8, 64_000, 32_000, 1500


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def selection_options(params):
    mode = params.get("selection_mode", "relevant")
    if mode not in {"relevant", "full"}:
        raise ValueError("资料选择方式只能是 relevant 或 full。")
    result = {"selection_mode": mode}
    for key in ("pinned_source_ids", "excluded_source_ids"):
        values = params.get(key, [])
        if (not isinstance(values, list) or len(values) > MAX_SCOPE
                or any(not isinstance(item, str) or len(item) > 128 for item in values)):
            raise ValueError("固定或排除的来源列表无效。")
        result[key] = list(dict.fromkeys(values))
    if set(result["pinned_source_ids"]) & set(result["excluded_source_ids"]):
        raise ValueError("同一来源不能同时固定和排除。")
    return result


class SourceSelection:
    def __init__(self, manager):
        self.manager, self.memory = manager, manager.memory

    def freeze(self, params):
        """Freeze the user-chosen scope, not the entire searchable library."""
        refs, materials = [], []
        for sid in params.get("session_ids", []):
            rows = self.memory.history(MAX_SCOPE + 1, sid)
            if len(rows) > MAX_SCOPE:
                raise ValueError("所选会话超过 1500 条消息，请缩小来源范围；没有截取部分会话。")
            refs += [{"kind": "message", "id": row["id"], "content_hash": digest(row["text"])}
                     for row in rows if row["role"] in {"user", "observation"} and row["text"].strip()]
        for mid in params.get("material_ids", []):
            material = self.memory.material(mid)
            materials.append({"id": mid, "version": material["version"]})
            refs += [{"kind": "chunk", "id": row["id"], "content_hash": digest(row["text"])}
                     for row in material["chunks"]]
        correction = params.get("correction_source_id")
        if correction and correction not in {ref["id"] for ref in refs}:
            rows = self.manager._messages([correction])
            if not rows or rows[0]["role"] != "user":
                raise ValueError("用户纠正来源已删除。")
            refs.append({"kind": "message", "id": correction, "content_hash": digest(rows[0]["text"])})
        if len(refs) > MAX_SCOPE:
            raise ValueError("所选来源超过 1500 条，请缩小范围；没有静默截取来源。")
        return {"materials": materials, "sources": refs}

    def records(self, params):
        manifest = params.get("source_scope") or self.freeze(params)
        refs = [{**ref, "handle": f"S{index}"} for index, ref in enumerate(manifest["sources"], 1)]
        records = self.manager._records(refs)
        known = {item["id"]: item for item in records}
        stale = any(ref["id"] not in known or digest(known[ref["id"]]["text"]) != ref["content_hash"] for ref in refs)
        versions = {item["id"]: item["version"] for item in self.memory.materials()}
        stale = stale or any(versions.get(item["id"]) != item["version"] for item in manifest["materials"])
        return manifest, records, stale

    def select(self, task):
        params = task["params"]
        options = selection_options(params)
        manifest, records, stale = self.records(params)
        by_id = {item["id"]: item for item in records}
        known = {ref["id"] for ref in manifest["sources"]}
        pins, excluded = set(options["pinned_source_ids"]), set(options["excluded_source_ids"])
        if params.get("correction_source_id"):
            pins.add(params["correction_source_id"])
        query = params.get("instructions", "").strip() or task["title"]
        correction = by_id.get(params.get("correction_source_id"))
        if correction:
            # The shared term extractor is capped. Prioritize the current edit
            # request so a long original instruction cannot crowd it out.
            query = correction["text"] + " " + query
        full = options["selection_mode"] == "full" or bool(re.search(
            r"全文|通读|逐段|逐条|完整(?:覆盖|整理|总结|梳理)|(?:全部|所有)(?:资料|内容|来源|段落)|\b(?:full[- ]text|every (?:source|section)|all sources)\b", query, re.I))
        mode = "full" if full else "relevant"
        error = None
        if stale:
            error = "来源已修改或删除，请明确更新来源后重新选择；旧任务不会自动采用新内容。"
        elif (pins | excluded) - known:
            error = "固定或排除的来源不在本任务范围中，请重新选择。"
        elif pins & excluded:
            error = "用户纠正来源必须参与修订，不能排除。"
        elif not records:
            error = "没有可用的来源，请选择有原文的会话或资料。"
        ranked = sorted(records, key=lambda item: (
            item["id"] in pins, _relevance(query, item["label"] + " " + item["text"])), reverse=True)
        eligible = [item for item in ranked if item["id"] not in excluded]
        config = self.manager._llm(2048)
        counter = TokenCounter(config, self.manager.runtime.data_dir)
        # This preselection allowance is conservative, not an exact model-token
        # guarantee. Delegation additionally checks the actual request + tools.
        source_budget = max(0, config.get("context_tokens", 8192) - config["max_tokens"] - 2048)
        chosen, reasons = [], {}
        small_scope = len(eligible) <= MAX_SOURCES
        for item in ranked:
            sid = item["id"]
            reason = "excluded" if sid in excluded else "not_relevant"
            if sid not in excluded:
                candidate = chosen + [item]
                fits = (len(candidate) <= MAX_SOURCES and len(item["text"]) <= MAX_SOURCE_CHARS
                        and sum(len(row["text"]) for row in candidate) <= MAX_CHARS
                        and counter.count([{"id": row["id"], "text": row["text"]} for row in candidate]) <= source_budget)
                relevant = full or small_scope or sid in pins or _relevance(query, item["label"] + " " + item["text"]) > 0
                if relevant and fits:
                    chosen.append(item)
                    reason = "pinned" if sid in pins else "full" if full else "small_scope" if small_scope else "relevant"
                elif relevant:
                    reason = "budget"
            reasons[sid] = reason
        if not error and (pins - {row["id"] for row in chosen}):
            error = "固定来源超出段数、长度或上下文预算，请调整选择或模型上下文。"
        if not error and full and len(chosen) != len(eligible):
            error = "全文范围无法放入本轮预算（最多 8 段、64000 字符），请缩小范围或改为按问题选择；没有执行部分全文任务。"
        if not error and not chosen:
            error = "未选出可用片段：请具体描述问题、固定来源，或调整上下文预算。"
        selected = [{"kind": row["source_kind"], "id": row["id"], "handle": f"S{index}",
                     "content_hash": digest(row["text"])} for index, row in enumerate(chosen, 1)]
        return {"mode": mode, "sources": selected, "reasons": reasons,
                "total_sources": len(manifest["sources"]), "selected_chars": sum(len(row["text"]) for row in chosen),
                "all_selected": len(chosen) == len(manifest["sources"]),
                "selection_status": "stale" if stale else "needs_attention" if error else "selected", "error": error}

    def inspect(self, task):
        """Present actual executed selection, resolving source bodies only now."""
        params = task["params"]
        manifest, records, stale = self.records(params)
        detail = self.memory.step(task["id"], "collect")["detail"].get("selection")
        detail = detail or self.select(task)
        if "source_scope" not in params:
            detail = {**detail, "sources": [], "selected_chars": 0, "all_selected": False,
                      "selection_status": "stale", "error": "旧任务未保存范围快照；下方仅为当前候选，请明确更新来源后重新选择。"}
        selected = {ref["id"]: ref for ref in detail["sources"]}
        pins, excluded = set(params.get("pinned_source_ids", [])), set(params.get("excluded_source_ids", []))
        if params.get("correction_source_id"):
            pins.add(params["correction_source_id"])
        by_id = {row["id"]: row for row in records}
        refs = sorted(manifest["sources"], key=lambda ref: ref["id"] not in selected)
        candidates = []
        for ref in refs[:100]:
            sid, row = ref["id"], by_id.get(ref["id"])
            available = row is not None and digest(row["text"]) == ref["content_hash"]
            candidates.append({"id": sid, "kind": ref["kind"], "label": row["label"] if available else "来源已失效",
                               "selected": sid in selected, "pinned": sid in pins, "excluded": sid in excluded,
                               "reason": detail["reasons"].get(sid, "not_selected"),
                               "chars": len(row["text"]) if available else 0, "content_hash": ref["content_hash"],
                               "available": available, "excerpt": row["text"][:400] if available else "",
                               "handle": selected.get(sid, {}).get("handle")})
        return {"task_id": task["id"], "mode": detail["mode"],
                "scope": {key: params.get(key, []) for key in ("material_ids", "session_ids")},
                "total_sources": detail["total_sources"], "selected_count": len(selected),
                "selected_chars": detail["selected_chars"], "max_sources": MAX_SOURCES, "max_chars": MAX_CHARS,
                "pinned_source_ids": list(pins), "excluded_source_ids": list(excluded), "all_selected": detail["all_selected"],
                "selection_status": "stale" if stale else detail["selection_status"],
                "error": "来源已变化，请明确更新来源后重新选择。" if stale else detail["error"],
                "candidates_limited": len(refs) > 100, "candidates": candidates}

    def source(self, task, source_id):
        manifest, records, _ = self.records(task["params"])
        ref = next((ref for ref in manifest["sources"] if ref["id"] == source_id), None)
        row = next((row for row in records if row["id"] == source_id), None)
        if not ref or not row or digest(row["text"]) != ref["content_hash"]:
            raise KeyError(source_id)
        return {"id": source_id, "label": row["label"], "text": row["text"],
                "content_hash": ref["content_hash"], "available": True}
