#!/usr/bin/env python3
"""Per-request delegation metrics for a retained supervised Sage run, read from Phoenix spans (read-only).

A delegation runs from a ``transfer_to_<worker>`` tool span to the Supervisor's next model call, or to the end of
the request when control never returns. For each delegation this reports the brief the worker received, how long
the worker ran (model calls, tool calls, issued Mythic tasks, wall time), repeated tool calls, and whether the task
ids the worker issued were visible to the Supervisor when it next ran. Request-level figures add the delegation
distribution, control-return intervals, approval interrupts, and token totals.

Inputs are a Phoenix SQLite database and a Mythic export holding the requests' prompts and time windows (and,
optionally, task identities). Task parameters and output are never needed. Every metric names its method in the
output, because several of them are proxies; for example, the brief fields are the ones the handoff tool had at the
time, not the structured brief the supervised orchestration plan introduces later.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

ISSUED_TASK_ID = re.compile(r"task_display_id=(\d+)\b")
# How a handback can name a task to the Supervisor: the issuance header, a task-observation envelope's JSON
# "task_id" (equal to the display id in Mythic v4), or prose such as "task 47" / "Task #47". The pattern also
# accepts the escaped quotes of JSON nested in a message string.
CITED_TASK_ID = re.compile(r'(?:task_display_id=|task_id\\*"?\s*:\s*|\b[Tt]ask #?)(\d+)\b')
WORD = re.compile(r"[a-z0-9]+")
HANDOFF_PREFIX = "transfer_to_"
TASK_TOOL = "issue_task_and_waitfor_task_output"
SUPERVISOR = "Supervisor"
METHODS = {
    "delegation_window": "transfer_to_* tool span start -> next Supervisor model call start, else request end",
    "title_only": "handoff input carries no handoff_instruction (the worker then receives only the operator input)",
    "prompt_coverage": "share of the request prompt's distinct words present in the instruction; reported, never a gate",
    "issued_tasks": f"task_display_id values in {TASK_TOOL} tool outputs inside the window",
    "repeat_tool_calls": "tool spans in the window whose name+input hash was already seen in the same window",
    "handback_visibility": "issued task ids named (task_display_id=N, JSON task_id, 'task N', 'Task #N') in the "
                           "Supervisor's next model input",
    "approval_cards": "Mythic chat messages carrying input_requested, by status; latency = resolved_at - created_at",
    "approval_interrupts": "Phoenix cross-check: HumanInTheLoopMiddleware.after_model spans with a non-null output",
}


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _phoenix_time(value: str) -> str:
    return _utc(value).strftime("%Y-%m-%d %H:%M:%S.%f")


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def load_spans(db: Path, start: str, end: str) -> list[dict[str, Any]]:
    """Spans whose start falls in [start, end), oldest first. Opens the database read-only."""
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(spans)")}
        attr = "attributesJSONB" if "attributesJSONB" in cols else "attributes"
        rows = conn.execute(
            f"SELECT name, span_kind, start_time, end_time, status_code, llm_token_count_prompt, "
            f"llm_token_count_completion, {attr} FROM spans WHERE start_time >= ? AND start_time < ? "
            "ORDER BY start_time, id", (_phoenix_time(start), _phoenix_time(end))).fetchall()
    finally:
        conn.close()
    spans = []
    for name, kind, began, finished, status, prompt, completion, raw in rows:
        attrs = _json(raw) if raw is not None else {}
        attrs = attrs if isinstance(attrs, dict) else {}
        llm = attrs.get("llm") if isinstance(attrs.get("llm"), dict) else {}
        spans.append({
            "name": str(name or ""), "kind": str(kind or ""), "start": str(began), "end": str(finished or began),
            "error": status == "ERROR",
            "agent": (attrs.get("metadata") or {}).get("lc_agent_name") if isinstance(attrs.get("metadata"), dict) else None,
            "input": (attrs.get("input") or {}).get("value") if isinstance(attrs.get("input"), dict) else None,
            "output": (attrs.get("output") or {}).get("value") if isinstance(attrs.get("output"), dict) else None,
            "llm_input": llm.get("input_messages"),
            "prompt_tokens": prompt or 0, "completion_tokens": completion or 0,
        })
    return spans


def prompt_coverage(instruction: str, prompt: str) -> float:
    words, covered = set(WORD.findall(prompt.lower())), set(WORD.findall(instruction.lower()))
    return round(len(words & covered) / len(words), 3) if words else 0.0


def brief_of(handoff: dict[str, Any], prompt: str) -> dict[str, Any]:
    payload = _json(handoff["input"])
    if isinstance(payload, dict):
        title = str(payload.get("handoff_title") or "")
        instruction = str(payload.get("handoff_instruction") or "")
    else:
        title, instruction = str(payload or ""), ""
    return {
        "target": handoff["name"][len(HANDOFF_PREFIX):],
        "title": title,
        "instruction_chars": len(instruction),
        "title_only": not instruction.strip(),
        "prompt_coverage": prompt_coverage(instruction, prompt) if instruction else None,
    }


def _seconds(start: str, end: str) -> float:
    return round((_utc(end) - _utc(start)).total_seconds(), 1)


def delegation_metrics(spans: list[dict[str, Any]], prompt: str, request_end: str) -> list[dict[str, Any]]:
    supervisor_calls = [s for s in spans if s["kind"] == "LLM" and s["agent"] == SUPERVISOR]
    handoffs = [s for s in spans if s["kind"] == "TOOL" and s["name"].startswith(HANDOFF_PREFIX)]
    results = []
    for handoff in handoffs:
        returned = next((s for s in supervisor_calls if s["start"] > handoff["start"]), None)
        end = returned["start"] if returned else _phoenix_time(request_end)
        window = [s for s in spans if handoff["start"] < s["start"] < end]
        tools = [s for s in window if s["kind"] == "TOOL" and not s["name"].startswith(HANDOFF_PREFIX)]
        issued = sorted({int(m) for s in tools if s["name"] == TASK_TOOL
                         for m in ISSUED_TASK_ID.findall(str(s["output"] or ""))})
        seen: set[str] = set()
        repeats = 0
        for tool in tools:
            key = hashlib.sha256(f"{tool['name']}\0{tool['input']}".encode()).hexdigest()
            repeats += key in seen
            seen.add(key)
        visible = None
        if returned is not None:
            text = json.dumps(returned["llm_input"], ensure_ascii=False) if returned["llm_input"] is not None else ""
            shown = {int(m) for m in CITED_TASK_ID.findall(text)}
            visible = sorted(set(issued) & shown)
        results.append({
            **brief_of(handoff, prompt),
            "started_at": handoff["start"],
            "returned": returned is not None,
            "wall_seconds": _seconds(handoff["start"], end),
            "worker_model_calls": sum(1 for s in window if s["kind"] == "LLM" and s["agent"] != SUPERVISOR),
            "tool_calls": len(tools),
            "tool_errors": sum(1 for s in tools if s["error"]),
            "repeat_tool_calls": repeats,
            "issued_task_ids": issued,
            "handback_visible_task_ids": visible,
        })
    return results


def card_metrics(cards: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = sorted(_seconds(str(c["created_at"]), str(c["resolved_at"])) for c in cards if c.get("resolved_at"))
    return {
        "approval_cards": len(cards),
        "approval_cards_by_status": dict(Counter(str(c.get("status")) for c in cards)),
        "median_decision_seconds": latencies[len(latencies) // 2] if latencies else None,
    }


def request_metrics(spans: list[dict[str, Any]], request: dict[str, Any],
                    cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    prompt = str(request.get("prompt") or "")
    delegations = delegation_metrics(spans, prompt, str(request["completed_at"]))
    supervisor_starts = [s["start"] for s in spans if s["kind"] == "LLM" and s["agent"] == SUPERVISOR]
    gaps = [_seconds(a, b) for a, b in zip(supervisor_starts, supervisor_starts[1:])]
    issued = sum(len(d["issued_task_ids"]) for d in delegations)
    visible = sum(len(d["handback_visible_task_ids"] or []) for d in delegations)
    return {
        "request_id": request.get("id"),
        "prompt": prompt,
        "wall_seconds": _seconds(str(request["created_at"]), str(request["completed_at"])),
        "supervisor_model_calls": len(supervisor_starts),
        "max_seconds_between_supervisor_calls": max(gaps) if gaps else None,
        "delegations": len(delegations),
        "delegation_targets": dict(Counter(d["target"] for d in delegations)),
        "returns_to_supervisor": sum(d["returned"] for d in delegations),
        "issued_tasks": issued,
        "handback_task_visibility": round(visible / issued, 3) if issued else None,
        **(card_metrics(cards) if cards is not None else {}),
        "approval_interrupts": sum(1 for s in spans if s["name"] == "HumanInTheLoopMiddleware.after_model"
                                   and s["output"] not in (None, "null")),
        "prompt_tokens": sum(s["prompt_tokens"] for s in spans if s["kind"] == "LLM"),
        "completion_tokens": sum(s["completion_tokens"] for s in spans if s["kind"] == "LLM"),
        "delegation_detail": delegations,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["| Request | Prompt | Wall | Delegations (targets) | Returns | Max gap between Supervisor calls | "
             "Issued tasks | Handback task visibility | Approval cards (status) | Median decision | Interrupts |",
             "|" + "---|" * 11]
    for r in report["requests"]:
        statuses = ", ".join(f"{k} {v}" for k, v in (r.get("approval_cards_by_status") or {}).items())
        lines.append(
            f"| {r['request_id']} | {r['prompt'][:60]} | {r['wall_seconds'] / 60:.1f} min | {r['delegations']} "
            f"({', '.join(f'{k} {v}' for k, v in r['delegation_targets'].items())}) | {r['returns_to_supervisor']} | "
            f"{(r['max_seconds_between_supervisor_calls'] or 0) / 60:.1f} min | {r['issued_tasks']} | "
            f"{r['handback_task_visibility']} | {r.get('approval_cards', '—')} ({statuses}) | "
            f"{r.get('median_decision_seconds', '—')} s | {r['approval_interrupts']} |")
    lines += ["", "| Request | Delegation | Title | Title only | Prompt coverage | Worker model calls | Tool calls | "
              "Repeats | Errors | Wall | Issued | Visible at return |", "|" + "---|" * 12]
    for r in report["requests"]:
        for i, d in enumerate(r["delegation_detail"], 1):
            lines.append(
                f"| {r['request_id']} | {i} → {d['target']} | {d['title'][:40]} | {d['title_only']} | "
                f"{d['prompt_coverage']} | {d['worker_model_calls']} | {d['tool_calls']} | "
                f"{d['repeat_tool_calls']} | {d['tool_errors']} | {d['wall_seconds'] / 60:.1f} min | "
                f"{len(d['issued_task_ids'])} | "
                f"{'—' if d['handback_visible_task_ids'] is None else len(d['handback_visible_task_ids'])} |")
    return "\n".join(lines) + "\n"


def build_report(phoenix: Path, export: dict[str, Any], request_ids: list[int] | None) -> dict[str, Any]:
    requests = [r for r in export["requests"] if not request_ids or r["id"] in request_ids]
    cards = export.get("cards")
    return {
        "methods": METHODS,
        "requests": [request_metrics(load_spans(phoenix, str(r["created_at"]), str(r["completed_at"])), r,
                                     None if cards is None else [c for c in cards if c["request_id"] == r["id"]])
                     for r in requests],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phoenix", type=Path, required=True, help="Phoenix SQLite database (opened read-only).")
    parser.add_argument("--mythic-export", type=Path, required=True,
                        help='JSON with "requests": [{id, prompt, created_at, completed_at}] (UTC).')
    parser.add_argument("--request", type=int, action="append", help="Request id to include; repeatable.")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args(argv)
    report = build_report(args.phoenix, json.loads(args.mythic_export.read_text(encoding="utf-8")), args.request)
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = render_markdown(report)
    if args.markdown_out:
        args.markdown_out.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
