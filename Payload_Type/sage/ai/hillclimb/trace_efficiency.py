"""Offline trace-efficiency scorer for archived Phoenix windows.

Step 0a of the control-state reprioritization (Plans/SAGE_HILLCLIMB_REPRIORITIZATION_2026-06-22.md):
score archived traces for DELIBERATION WASTE without touching the live gauge or `verifier_hash`. The capability
gauge is blind to looping/repetition (a 461K-token supervisor↔worker loop scores the same as a clean run, and
Phoenix `status_code=OK` hides operational failure). This module makes that waste a number so terminal-blocker
propagation can be tested against a measurable target BEFORE any expensive live run.

Design: the scoring core (`score_spans`) is PURE over a list of `Span` records so it is unit-testable without a
Phoenix DB or a live range. `score_phoenix_db` is the only sqlite-touching wrapper. Nothing here imports Sage
runtime or mutates state. The key discriminator is the post-last-endpoint TAIL: LLM tokens and supervisor
transfers that occur AFTER the last real Mythic endpoint action are pure deliberation with no new endpoint work
— exactly the `1116` loop signature.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict

ENDPOINT_TOOL = "issue_task_and_waitfor_task_output"   # the only real "did something on the endpoint" action
POLL_TOOL = "get_all_task_output_by_task_id"           # async polling read
TRANSFER_PREFIX = "transfer_to_"                        # inter-agent handoff (e.g. transfer_to_Mythic_Operator)


@dataclass(frozen=True)
class Span:
    kind: str                 # 'TOOL' | 'LLM' | 'CHAIN'
    name: str
    start: str                # sortable timestamp string (Phoenix 'YYYY-MM-DD HH:MM:SS.ffffff')
    tool_input: str = ""      # normalized serialized tool input (for exact-repeat + poll keys)
    prompt_tokens: int = 0
    completion_tokens: int = 0


def score_spans(spans: list[Span]) -> dict:
    """Pure efficiency scoring over a single trace window's spans. Higher `waste_score` == worse.

    Metrics:
      - exact_repeat_rate: fraction of TOOL spans that are exact (name, input) duplicates of an earlier one.
      - unchanged_poll_reads: repeated polls of the SAME task id (the LLM used as a scheduler).
      - transfers_after_last_endpoint: inter-agent handoffs with NO new endpoint action after them (loop tail).
      - tail_tokens / tail_token_fraction: LLM tokens spent AFTER the last endpoint action (deliberation waste).
      - waste_score: a single scalar so windows can be ordered; weights the three loop signals.
    """
    tool = [s for s in spans if s.kind == "TOOL"]
    llm = [s for s in spans if s.kind == "LLM"]

    # exact tool repeats (same name + identical normalized input occurring more than once in the window)
    counts: dict[tuple, int] = {}
    for s in tool:
        counts[(s.name, s.tool_input)] = counts.get((s.name, s.tool_input), 0) + 1
    exact_repeat_count = sum(c - 1 for c in counts.values() if c > 1)
    exact_repeat_rate = (exact_repeat_count / len(tool)) if tool else 0.0

    # last real endpoint action — everything after it with no further endpoint is tail
    endpoint_starts = [s.start for s in tool if s.name == ENDPOINT_TOOL]
    last_endpoint = max(endpoint_starts) if endpoint_starts else None

    def after_tail(s: Span) -> bool:
        return last_endpoint is not None and s.start > last_endpoint

    transfers = [s for s in tool if s.name.startswith(TRANSFER_PREFIX)]
    transfers_after_last_endpoint = sum(1 for s in transfers if after_tail(s))

    # unchanged polling: same task id read more than once
    poll_counts: dict[str, int] = {}
    for s in tool:
        if s.name == POLL_TOOL:
            poll_counts[s.tool_input] = poll_counts.get(s.tool_input, 0) + 1
    poll_reads = sum(poll_counts.values())
    unchanged_poll_reads = sum(c - 1 for c in poll_counts.values() if c > 1)

    total_tokens = sum(s.prompt_tokens + s.completion_tokens for s in llm)
    tail_tokens = sum(s.prompt_tokens + s.completion_tokens for s in llm if after_tail(s))
    tail_token_fraction = (tail_tokens / total_tokens) if total_tokens else 0.0
    # A clean trace ends with ~1 LLM call to read the final result and stop; a loop emits MANY post-endpoint
    # LLM calls + transfers with no new endpoint. Score on those COUNTS (size-robust), not the token fraction
    # (which a small clean trace inflates). tail_tokens stays reported as the headline magnitude (the ~461K).
    tail_llm_calls = sum(1 for s in llm if after_tail(s))

    waste_score = round(
        0.4 * exact_repeat_rate
        + 0.3 * min(1.0, transfers_after_last_endpoint / 20.0)
        + 0.3 * min(1.0, tail_llm_calls / 20.0),
        4,
    )

    return {
        "tool_calls": len(tool),
        "llm_calls": len(llm),
        "endpoint_tasks": len(endpoint_starts),
        "exact_repeat_count": exact_repeat_count,
        "exact_repeat_rate": round(exact_repeat_rate, 4),
        "transfers": len(transfers),
        "transfers_after_last_endpoint": transfers_after_last_endpoint,
        "poll_reads": poll_reads,
        "unchanged_poll_reads": unchanged_poll_reads,
        "total_tokens": total_tokens,
        "tail_tokens": tail_tokens,
        "tail_token_fraction": round(tail_token_fraction, 4),
        "tail_llm_calls": tail_llm_calls,
        "waste_score": waste_score,
    }


def _normalize_tool_input(attributes: dict) -> str:
    """Stable serialization of a tool span's input for exact-repeat / poll keys. Phoenix stores input either as
    a JSON value or a python-repr string; we key on its trimmed text either way (semantic-repeat detection is a
    later refinement — this is the exact-repeat lower bound)."""
    val = attributes.get("input")
    if isinstance(val, dict):
        val = val.get("value", val)
    try:
        return json.dumps(val, sort_keys=True)[:4000] if not isinstance(val, str) else val.strip()[:4000]
    except Exception:
        return str(val)[:4000]


def _tool_name(attributes: dict, span_name: str) -> str:
    tool = attributes.get("tool")
    if isinstance(tool, dict) and tool.get("name"):
        return str(tool["name"])
    return span_name


def score_phoenix_db(db_path: str) -> dict:
    """Read a Phoenix sqlite DB into `Span` records and score it. The ONLY sqlite-touching entry point."""
    con = sqlite3.connect(db_path)
    spans: list[Span] = []
    rows = con.execute(
        "SELECT span_kind, name, start_time, attributes, "
        "COALESCE(llm_token_count_prompt,0), COALESCE(llm_token_count_completion,0) FROM spans"
    ).fetchall()
    for kind, name, start, attrs, pt, ct in rows:
        try:
            ad = json.loads(attrs) if isinstance(attrs, (str, bytes)) else (attrs or {})
        except Exception:
            ad = {}
        tool_input = _normalize_tool_input(ad) if kind == "TOOL" else ""
        spans.append(Span(
            kind=str(kind or ""), name=_tool_name(ad, str(name or "")),
            start=str(start or ""), tool_input=tool_input,
            prompt_tokens=int(pt or 0), completion_tokens=int(ct or 0),
        ))
    con.close()
    result = score_spans(spans)
    result["db"] = db_path
    return result


def score_windows(db_paths: list[str]) -> list[dict]:
    """Score several windows and return them sorted worst-first by waste_score."""
    scored = [score_phoenix_db(p) for p in db_paths]
    return sorted(scored, key=lambda r: r["waste_score"], reverse=True)
