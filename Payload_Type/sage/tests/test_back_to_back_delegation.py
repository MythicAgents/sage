"""A supervised Supervisor may not re-delegate to the agent that just executed.

Channel 78: the operator asked for one SharpHound collection. `Mythic_Operator` ran it end to end
— execute_assembly, ls, download, ingest_collection, eleven cypher queries — and reported "## DONE".
The Supervisor immediately handed the same objective back to `Mythic_Operator`, which started the
whole job again from scratch. The operator cancelled it.

Nothing caught it, for three independent reasons:

* the delegation cap counts specialist RETURNS, not their identity, and one duplicate fits inside it;
* every no-progress backstop resets when a delegation issues real Mythic tasks, which the repeat did;
* `assign_and_admit`'s duplicate key includes `state_revision`, and evidence advances the revision —
  so completing the work is exactly what makes repeating it look new.

The two handoff instructions were *paraphrases* sharing almost no wording, so an instruction-text
digest would not have caught it either. The check is therefore typed: requested owner against the
owner of the most recent admitted execution.
"""

import pytest

from ai.langgraph import model as model_module
from ai.langgraph.model import Model, _SUPERVISED_REPEAT_REFUSAL_CAP
from ai.langgraph.request_contract import RequestLane
from ai.langgraph.subgoal_state import apply_worker_outcome, assign_and_admit, new_subgoal


class _Contract:
    def __init__(self, lane):
        self.lane = lane
        self.request_id = "chat:78:request:87"


class _Model:
    """Only the surface `_supervised_back_to_back_refusal` reads."""

    _supervised_back_to_back_refusal = Model._supervised_back_to_back_refusal

    def __init__(self, lane=RequestLane.SUPERVISED_WORKFLOW, prior_result=""):
        self._request_contract = _Contract(lane)
        self.state = {"supervisor_messages": []}
        if prior_result:
            from langchain_core.messages import AIMessage

            self.state["supervisor_messages"].append(
                AIMessage(
                    content=prior_result,
                    name="Mythic_Operator",
                    additional_kwargs={"_is_completion_header": True},
                )
            )


def _subgoal_after(*owners):
    """A canonical subgoal that really executed `owners` in order, then handed back.

    Built through the same typed transitions production uses — `assign_and_admit` refuses a bare
    owner change, so each step goes through a worker handoff — otherwise the fixture would not
    exercise the transition history the refusal reads.
    """

    state = new_subgoal("chat:78:request:87", "actions_complete")
    for index, owner in enumerate(owners):
        state = assign_and_admit(state, owner=owner, method=f"transfer_to_{owner}")
        nxt = owners[index + 1] if index + 1 < len(owners) else "Supervisor"
        state = apply_worker_outcome(
            state,
            outcome_id=f"outcome-{index}",
            outcome="handoff",
            source_owner=owner,
            next_owner=nxt,
        )
    return state


# --------------------------------------------------------------------------------------
# The reported defect and its near-match control.
# --------------------------------------------------------------------------------------


def test_back_to_back_redelegation_is_refused():
    model = _Model(prior_result="## DONE\n\nSharpHound collected and ingested.")
    subgoal = _subgoal_after("Mythic_Operator")

    refusal = model._supervised_back_to_back_refusal(subgoal, "Mythic_Operator")

    assert refusal is not None, "channel 78's A->A shape was admitted again"
    assert "SharpHound collected and ingested" in refusal


def test_a_to_b_to_a_is_still_allowed():
    """The operator's rule: repeats are fine, consecutive repeats are not."""

    model = _Model(prior_result="## DONE")
    subgoal = _subgoal_after("Mythic_Operator", "Generalist")

    assert model._supervised_back_to_back_refusal(subgoal, "Mythic_Operator") is None


def test_a_different_specialist_is_always_allowed():
    model = _Model(prior_result="## DONE")
    subgoal = _subgoal_after("Mythic_Operator")

    assert model._supervised_back_to_back_refusal(subgoal, "Generalist") is None


def test_the_first_delegation_of_a_request_is_allowed():
    model = _Model()
    subgoal = new_subgoal("chat:78:request:87", "actions_complete")

    assert model._supervised_back_to_back_refusal(subgoal, "Mythic_Operator") is None


# --------------------------------------------------------------------------------------
# Scope: autonomous must keep its recovery path.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lane",
    [lane for lane in RequestLane if lane != RequestLane.SUPERVISED_WORKFLOW],
)
def test_non_supervised_lanes_are_untouched(lane):
    """Auto re-delegates to the same specialist to recover after a blocker. Do not break that."""

    model = _Model(lane=lane, prior_result="## DONE")
    subgoal = _subgoal_after("Mythic_Operator")

    assert model._supervised_back_to_back_refusal(subgoal, "Mythic_Operator") is None


def test_an_unreadable_contract_allows_rather_than_blocks():
    model = _Model()
    model._request_contract = None
    subgoal = _subgoal_after("Mythic_Operator")

    assert model._supervised_back_to_back_refusal(subgoal, "Mythic_Operator") is None


# --------------------------------------------------------------------------------------
# Paraphrase independence — the property a text digest could not have.
# --------------------------------------------------------------------------------------


def test_refusal_reads_no_instruction_text():
    """Channel 78's two instructions shared almost no wording. The decision must not depend on
    wording at all, so the same (owner, subgoal) pair decides identically regardless."""

    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(Model._supervised_back_to_back_refusal)))
    fn = tree.body[0]
    if ast.get_docstring(fn) is not None:
        fn.body = fn.body[1:]  # prose about instructions is not consulting them
    code = ast.unparse(fn)
    assert "last_admitted" in code, "did not capture the real refusal body"
    for forbidden in ("instruction", "prompt", "title", "content.lower", "casefold"):
        assert forbidden not in code, (
            f"refusal consults {forbidden!r}; paraphrase would defeat it"
        )

    model = _Model(prior_result="## DONE")
    subgoal = _subgoal_after("Mythic_Operator")
    assert (
        model._supervised_back_to_back_refusal(subgoal, "Mythic_Operator")
        == model._supervised_back_to_back_refusal(subgoal, "Mythic_Operator")
    )


# --------------------------------------------------------------------------------------
# The hand-back contract, and the bound that stops it becoming a loop.
# --------------------------------------------------------------------------------------


def test_handback_does_not_terminate_and_terminates_only_at_the_cap():
    """ISC-2 and ISC-6 read straight off the production branch.

    Below the cap the tool returns a plain `Command(update=...)` — no goto, no PARENT, no
    `_is_final_report` — so the Supervisor keeps control. At the cap it closes the request with
    the result it already had.
    """

    import inspect

    source = inspect.getsource(model_module._create_handoff_tool)
    start = source.index('if disposition == "repeat":')
    end = source.index('if disposition == "route":', start)
    branch = source[start:end]

    assert "_SUPERVISED_REPEAT_REFUSAL_CAP" in branch, "the hand-back is unbounded"
    below, at_cap = branch.split("if refusals >= _SUPERVISED_REPEAT_REFUSAL_CAP:", 1)
    at_cap, handback = at_cap.split("handback = ToolMessage(", 1)

    # At the cap: terminate with the prior result.
    assert "_is_final_report" in at_cap and 'goto="__end__"' in at_cap

    # Below the cap: hand back only.
    assert "_is_final_report" not in handback
    assert "goto=" not in handback
    assert "Command.PARENT" not in handback
    assert "return Command(update=repeat_update)" in handback


def test_the_refusal_cap_is_small_enough_to_matter():
    assert 1 <= _SUPERVISED_REPEAT_REFUSAL_CAP <= 3
