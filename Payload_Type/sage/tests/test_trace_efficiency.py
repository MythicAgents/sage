"""Offline tests for the archived-trace efficiency scorer (control-state reprioritization, step 0a).

Pure-function tests over synthetic spans — no Phoenix DB, no live range. The load-bearing assertion is that a
loop trace (the `1116` signature: a long post-endpoint tail of supervisor transfers + LLM tokens with no new
endpoint action) scores DISTINCTLY worse than a clean trace that did the same endpoint work and stopped.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import trace_efficiency as te  # noqa: E402
from trace_efficiency import Span  # noqa: E402


def _clean_trace():
    # foothold -> issue a task -> read it once -> stop. No tail, no repeats.
    return [
        Span("LLM", "supervisor", "00:00:01", prompt_tokens=2000, completion_tokens=200),
        Span("TOOL", "transfer_to_Mythic_Operator", "00:00:02"),
        Span("TOOL", te.ENDPOINT_TOOL, "00:00:03", tool_input='{"command":"dcsync","p":1}'),
        Span("LLM", "mythic_operator", "00:00:04", prompt_tokens=3000, completion_tokens=300),
        Span("TOOL", te.POLL_TOOL, "00:00:05", tool_input="task:7"),
    ]


def _loop_trace():
    # reaches the same endpoint, then loops: 25 supervisor transfers + 25 LLM calls AFTER the last endpoint,
    # plus the same poll re-read 6x. This is the 1116 deliberation-tail shape.
    spans = [
        Span("TOOL", te.ENDPOINT_TOOL, "00:00:03", tool_input='{"command":"adcs-certificate-auth"}'),
    ]
    for i in range(25):
        t = f"00:10:{i:02d}"
        spans.append(Span("TOOL", "transfer_to_Mythic_Operator", t))
        spans.append(Span("LLM", "supervisor", t, prompt_tokens=18000, completion_tokens=400))
    for i in range(6):
        spans.append(Span("TOOL", te.POLL_TOOL, f"00:20:{i:02d}", tool_input="task:9"))
    return spans


def test_clean_trace_has_low_waste():
    r = te.score_spans(_clean_trace())
    assert r["transfers_after_last_endpoint"] == 0
    assert r["tail_llm_calls"] <= 1           # one normal post-endpoint wind-down call, not a loop
    assert r["exact_repeat_count"] == 0
    assert r["waste_score"] < 0.1


def test_loop_trace_scores_distinctly_worse_than_clean():
    clean = te.score_spans(_clean_trace())
    loop = te.score_spans(_loop_trace())
    # the 1116 tail must be visible and dominate the score
    assert loop["transfers_after_last_endpoint"] == 25
    assert loop["tail_llm_calls"] == 25
    assert loop["tail_tokens"] > 400_000           # 25 * (18000+400) = 460,000 — the ~461K tail
    assert loop["waste_score"] > clean["waste_score"] + 0.3   # distinctly worse, not a tie


def test_exact_repeat_rate_counts_identical_tool_inputs():
    spans = [
        Span("TOOL", "cypher_query", "1", tool_input="Q1"),
        Span("TOOL", "cypher_query", "2", tool_input="Q1"),   # repeat
        Span("TOOL", "cypher_query", "3", tool_input="Q1"),   # repeat
        Span("TOOL", "cypher_query", "4", tool_input="Q2"),   # distinct
    ]
    r = te.score_spans(spans)
    assert r["exact_repeat_count"] == 2          # 3 of Q1 -> 2 repeats
    assert r["exact_repeat_rate"] == 0.5         # 2 / 4 tool calls


def test_unchanged_poll_reads_counts_same_task_rereads():
    spans = [Span("TOOL", te.POLL_TOOL, str(i), tool_input="task:42") for i in range(5)]
    spans.append(Span("TOOL", te.POLL_TOOL, "9", tool_input="task:99"))
    r = te.score_spans(spans)
    assert r["poll_reads"] == 6
    assert r["unchanged_poll_reads"] == 4        # task:42 read 5x -> 4 redundant reads


def test_empty_trace_is_safe():
    r = te.score_spans([])
    assert r["waste_score"] == 0.0 and r["tool_calls"] == 0
