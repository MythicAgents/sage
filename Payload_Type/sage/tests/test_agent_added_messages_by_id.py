"""ISC-56 — the agent's output is identified by message id, never by list position.

`_ainvoke` used to slice `updated_channel[initial_agent_input_length:]`, which is sound only if the
returned list is (input + new). Identifying the agent's output by identity rather than by arithmetic
is unconditionally sounder, and these tests pin that property.

SCOPE (corrected 2026-07-28, round 9): these are HARDENING tests, not a regression for the
channel-56/57 zero-return. The original docstring claimed Sage's history-rewriting middleware
shortens the returned list; that is refuted for this runtime — ContextEditingMiddleware's
_DigestToolUsesEdit only does `messages[idx] = ...` (length invariant), and SummarizationMiddleware
has never fired (247 no-op before_model spans, zero LLM descendants, 150k trigger vs an all-time
peak of 80,839 prompt tokens). The shortened-list cases below are therefore SYNTHETIC: they prove
the function behaves correctly IF a rewrite ever shortens a list, not that one ever did. The real
mechanism of the live defect is unknown. Do not cite this file as evidence that it is fixed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from ai.langgraph.model import _messages_added_by_agent


def _m(cls, content, mid, **kw):
    msg = cls(content=content, **kw)
    msg.id = mid
    return msg


def _input():
    return [
        _m(SystemMessage, "ctx", "i1"),
        _m(HumanMessage, "go", "i2"),
        _m(AIMessage, "filler-1", "i3"),
        _m(AIMessage, "filler-2", "i4"),
    ]


def test_normal_append_is_unchanged():
    inp = _input()
    added = [_m(AIMessage, "", "n1"), _m(ToolMessage, "result", "n2", tool_call_id="t"),
             _m(AIMessage, "final", "n3")]
    out = _messages_added_by_agent(inp + added, inp, len(inp))
    assert [m.id for m in out] == ["n1", "n2", "n3"]


def test_history_rewrite_no_longer_hides_the_answer():
    """The channel-57 shape: rewrite drops 3 off the front, model adds 3 — net length unchanged.

    Positional slicing returns nothing here. That was the bug.
    """
    inp = _input()
    added = [_m(AIMessage, "", "n1"), _m(ToolMessage, "STOP — dead", "n2", tool_call_id="t"),
             _m(AIMessage, "callback 1 is dead", "n3")]
    rewritten = [inp[3]] + added                      # len == len(inp) == 4
    assert len(rewritten) == len(inp), "precondition: the positional slice would be empty"
    assert rewritten[len(inp):] == [], "precondition: this is exactly the observed failure"

    out = _messages_added_by_agent(rewritten, inp, len(inp))
    assert [m.id for m in out] == ["n1", "n2", "n3"], "the answer must be recovered"


def test_rewrite_shorter_than_input_still_recovers():
    inp = _input()
    added = [_m(AIMessage, "final", "n1")]
    rewritten = added                                  # everything prior summarized away
    out = _messages_added_by_agent(rewritten, inp, len(inp))
    assert [m.id for m in out] == ["n1"]


def test_missing_ids_fall_back_to_positional_slicing():
    """Byte-identical behaviour on transcripts this cannot reason about."""
    inp = _input()
    noid = AIMessage(content="no id")
    noid.id = None
    out = _messages_added_by_agent(inp + [noid], inp, len(inp))
    assert out == [noid]

    inp_missing = _input()
    inp_missing[0].id = None
    tail = _m(AIMessage, "x", "n9")
    out2 = _messages_added_by_agent(inp_missing + [tail], inp_missing, len(inp_missing))
    assert [m.id for m in out2] == ["n9"]


def test_agent_returning_nothing_still_reports_nothing():
    """A genuinely empty run must not be papered over."""
    inp = _input()
    assert _messages_added_by_agent(list(inp), inp, len(inp)) == []
