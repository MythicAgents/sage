"""ISC-53 round 10 — the zero-return instrument reports message IDENTITY, not just lengths.

Round 9 refuted the history-rewrite attribution for the channel-56/57 zero-return, leaving the
mechanism unknown. Lengths alone cannot distinguish the leading hypothesis (an `add_messages`
id collision, which REPLACES in place and so leaves len(updated) == len(input)) from a genuine
drop. These tests pin the three quantities that can:

    new     — ids returned that were not sent
    dropped — ids sent that did not come back
    mutated — ids on both sides whose CONTENT changed  <-- the collision tell

`new=0 dropped=0 mutated>0` is replace-in-place and confirms the hypothesis on the next live
occurrence; `mutated=0` kills it. The instrument is diagnostic only and must never raise.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages

from ai.langgraph.model import _zero_return_id_forensics


def _channel(n=20):
    msgs = [SystemMessage(content="sys", id="m0")]
    msgs += [HumanMessage(content=f"h{i}", id=f"m{i}") for i in range(1, n)]
    return msgs


def test_collision_replace_is_named_as_the_confirming_verdict():
    """The channel-57 shape: 20 in, 20 out, model produced 3 — via id collision."""
    agent_input = _channel(20)
    colliding = [
        AIMessage(content="I'll check callbacks", id="m17"),
        ToolMessage(content="tool result", tool_call_id="tc1", id="m18"),
        AIMessage(content="Here are the results", id="m19"),
    ]
    updated = add_messages(agent_input, colliding)

    # Precondition: this really is the zero-return signature, or the test proves nothing.
    assert len(updated) == len(agent_input) == 20

    out = _zero_return_id_forensics(updated, agent_input)
    assert "n_in=20 n_out=20" in out
    assert "new=0" in out
    assert "dropped=0" in out
    assert "mutated=3" in out
    assert "COLLISION-REPLACE" in out
    assert "CONFIRMED" in out


def test_clean_append_is_not_reported_as_collision():
    agent_input = _channel(20)
    fresh = [
        AIMessage(content="I'll check callbacks", id="n1"),
        ToolMessage(content="tool result", tool_call_id="tc1", id="n2"),
        AIMessage(content="Here are the results", id="n3"),
    ]
    updated = add_messages(agent_input, fresh)

    out = _zero_return_id_forensics(updated, agent_input)
    assert "new=3" in out
    assert "mutated=0" in out
    assert "COLLISION-REPLACE" not in out
    assert "does NOT explain this one" in out


def test_genuine_drop_is_distinguished_from_collision():
    """A real shortening rewrite — the round-8 theory — reports dropped>0, not mutated."""
    agent_input = _channel(20)
    updated = list(agent_input[3:])  # three removed off the front, nothing added

    out = _zero_return_id_forensics(updated, agent_input)
    assert "dropped=3" in out
    assert "new=0" in out
    assert "mutated=0" in out
    assert "COLLISION-REPLACE" not in out


def test_messages_without_ids_are_counted_not_crashed_on():
    agent_input = [HumanMessage(content="no id"), HumanMessage(content="h1", id="m1")]
    updated = [HumanMessage(content="no id"), HumanMessage(content="h1", id="m1")]

    out = _zero_return_id_forensics(updated, agent_input)
    assert "untracked_no_id=2" in out
    assert "forensics-unavailable" not in out


def test_instrument_never_raises():
    """Diagnostics must never break the node — a bad input degrades to a marker string."""

    class Exploding:
        @property
        def id(self):
            raise RuntimeError("boom")

    out = _zero_return_id_forensics([Exploding()], [Exploding()])
    assert "forensics-unavailable" in out


def test_id_samples_are_bounded():
    """Ids are uuid-length; an unbounded dump would flood the log on a large channel."""
    agent_input = _channel(60)
    updated = list(agent_input) + [
        AIMessage(content=f"extra{i}", id=f"z{i}") for i in range(30)
    ]
    out = _zero_return_id_forensics(updated, agent_input)
    assert "new=30" in out
    # sample lists cap at 5 entries
    sample = out.split("new_sample=")[1].split("]")[0] + "]"
    assert sample.count("'") == 10
