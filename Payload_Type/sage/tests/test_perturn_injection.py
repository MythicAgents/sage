"""Per-call engagement-ledger injection is ephemeral.

The autonomous-only observed-state injection (``_EngagementStateMiddleware`` and
``Model._render_engagement_state_for_injection``) is retired. ``_EngagementLedgerViewMiddleware`` adds one fresh
engagement-ledger view to every Supervisor and Mythic_Operator model call, through ``request.override`` only, so
the block never accumulates in graph state. The assembled behaviour (actual model inputs across lanes, freshness,
no accumulation) is covered in ``test_engagement_ledger_phase5_fresh_awareness.py``.
"""

import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph import engagement_ledger  # noqa: E402
from ai.langgraph import model as model_module  # noqa: E402


class _FakeRequest:
    def __init__(self, messages, tools=()):
        self.messages = list(messages)
        self.tools = list(tools)

    def override(self, **kw):
        return _FakeRequest(kw.get("messages", self.messages), self.tools)


class _StubModel:
    def _engagement_ledger_view_for_call(self, agent_name, request):
        return f"[Sage engagement ledger — fresh view for this model call] role={agent_name}", 7


def test_the_retired_autonomous_only_injection_is_gone():
    assert not hasattr(model_module, "_EngagementStateMiddleware")
    assert not hasattr(model_module.Model, "_render_engagement_state_for_injection")
    assert not hasattr(model_module, "_ENGAGEMENT_STATE_DIRECTIVE")


def test_the_view_is_appended_ephemerally_and_tagged():
    mw = model_module._EngagementLedgerViewMiddleware(_StubModel(), agent_name="Supervisor")
    request = _FakeRequest([HumanMessage(content="hello")])
    augmented = mw._augment(request)
    assert len(request.messages) == 1  # the original request (graph state) is untouched
    assert len(augmented.messages) == 2
    view, last = augmented.messages
    assert last.content == "hello"  # the operator's message stays the final turn
    assert view.content.endswith("role=Supervisor")
    assert view.additional_kwargs[engagement_ledger.MODEL_VIEW_TAG] == {"role": "Supervisor", "revision": 7}


def test_placement_keeps_the_final_turn_and_never_splits_a_tool_result():
    from langchain_core.messages import AIMessage, ToolMessage

    mw = model_module._EngagementLedgerViewMiddleware(_StubModel(), agent_name="Supervisor")
    call = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1", "type": "tool_call"}])
    result = ToolMessage(content="r", tool_call_id="c1")
    after_tool = mw._augment(_FakeRequest([HumanMessage(content="go"), call, result])).messages
    assert after_tool[1:3] == [call, result] and engagement_ledger.MODEL_VIEW_TAG in after_tool[3].additional_kwargs
    answer = AIMessage(content="worker answer")
    continuation = HumanMessage(content="continue", additional_kwargs={"_synthetic_nudge": "provider_requires_user_turn"})
    outcome = AIMessage(content="[step outcome] ...", name="Sage")
    tail = mw._augment(_FakeRequest([HumanMessage(content="go"), answer, outcome, continuation])).messages
    assert engagement_ledger.MODEL_VIEW_TAG in tail[1].additional_kwargs and tail[2:] == [answer, outcome, continuation]
    scope = AIMessage(content="<step_scope>", name="Sage", additional_kwargs={"_step_scope_context": True})
    delegated = HumanMessage(content="do the step")
    framed = mw._augment(_FakeRequest([HumanMessage(content="go"), call, result, scope, delegated])).messages
    assert framed[3:4][0].additional_kwargs.get(engagement_ledger.MODEL_VIEW_TAG) and framed[4:] == [scope, delegated]


def test_an_earlier_generated_view_is_replaced_not_stacked():
    mw = model_module._EngagementLedgerViewMiddleware(_StubModel(), agent_name="Mythic_Operator")
    first = mw._augment(_FakeRequest([HumanMessage(content="go")]))
    second = mw._augment(_FakeRequest(first.messages))
    views = [m for m in second.messages if engagement_ledger.MODEL_VIEW_TAG in m.additional_kwargs]
    assert len(views) == 1 and len(second.messages) == 2
