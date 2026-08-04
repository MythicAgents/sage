"""A halt must say why it happened, and must never blame the operator for Sage's own decision.

Found live 2026-08-03: a repeated prompt tripped the Supervisor's no-progress backstop, which set
`_stop_requested` internally, and the operator was told "Session stopped by operator" for a halt he had
no part in. The backstop had already composed an honest explanation; the generic operator wording
buried it.

The load-bearing assertion here is the *negative* one — `test_no_internal_reason_blames_the_operator`
— because any future halt path that forgets to label itself must land on neutral wording rather than
silently reintroducing the accusation.
"""

from __future__ import annotations

import pytest

from ai.langgraph.model import (
    STOP_REASON_NO_PROGRESS,
    STOP_REASON_OPERATOR,
    STOP_REASON_TERMINAL_BLOCKER,
    stop_notice_for,
)

OPERATOR_PHRASE = "stopped by operator"


def test_operator_stop_keeps_its_wording():
    """The near-match control: a real operator stop must still say so."""
    assert OPERATOR_PHRASE in stop_notice_for(STOP_REASON_OPERATOR)


def test_no_progress_halt_does_not_blame_the_operator():
    notice = stop_notice_for(STOP_REASON_NO_PROGRESS)
    assert OPERATOR_PHRASE not in notice
    assert "no progress" in notice.lower()


def test_halt_notices_are_self_contained():
    """No notice may defer to a message that the halt itself prevented from being sent.

    Regression for a live miss: the first no-progress notice said "see the explanation above" while
    the fuller explanation never reached the chat, because the halt ends the graph before it streams.
    Pointing an operator at absent text is worse than terse text.
    """
    for reason in (STOP_REASON_OPERATOR, STOP_REASON_NO_PROGRESS, STOP_REASON_TERMINAL_BLOCKER, ""):
        notice = stop_notice_for(reason).lower()
        for dangling in ("above", "below", "see the explanation", "as described"):
            assert dangling not in notice, (
                f"halt notice for {reason!r} defers to other text ({dangling!r}) that may not exist"
            )


def test_no_progress_notice_explains_what_counts_as_progress():
    """The operator's first question is 'why does it think nothing happened?' — answer it in place."""
    notice = stop_notice_for(STOP_REASON_NO_PROGRESS).lower()
    assert "task" in notice, "the notice must say that progress means an issued task"


def test_terminal_blocker_halt_does_not_blame_the_operator():
    notice = stop_notice_for(STOP_REASON_TERMINAL_BLOCKER)
    assert OPERATOR_PHRASE not in notice
    assert "blocker" in notice.lower()


@pytest.mark.parametrize("reason", ["", None, "something_new_nobody_labelled", "NO_PROGRESS"])
def test_no_internal_reason_blames_the_operator(reason):
    """An unlabelled halt must be neutral, never attributed to the operator.

    This is the guard against regression by omission: a future halt path that sets `_stop_requested`
    without a reason gets neutral wording instead of quietly resurrecting the bug. Note `NO_PROGRESS`
    in the cases — wrong case is an unknown key, and must also fall through to neutral rather than
    matching by accident.
    """
    assert OPERATOR_PHRASE not in stop_notice_for(reason)


def test_every_known_reason_has_distinct_wording():
    reasons = [STOP_REASON_OPERATOR, STOP_REASON_NO_PROGRESS, STOP_REASON_TERMINAL_BLOCKER]
    notices = [stop_notice_for(r) for r in reasons]
    assert len(set(notices)) == len(reasons), f"halt reasons share wording: {notices}"
    assert all(n.strip() for n in notices), "a halt must always say something"


# --- a stopped Model must not poison the next request ----------------------------------------------

def _bare_model():
    """Minimal Model for contract installation, matching the pattern in test_request_contract.py."""
    from ai.langgraph.model import Model

    model = Model.__new__(Model)
    model._request_contract = None
    model._request_execution_digest = ""
    model._request_admitted_action_digests = set()
    model._request_dynamic_proposals = False
    model._active_approval_claim = None
    model.mythic_client = None
    model._stop_requested = False
    model._stop_reason = ""
    return model


def _request_contract(request_id):
    from ai.langgraph.request_contract import build_request_contract

    return build_request_contract(
        request_id=request_id,
        channel_id="channel-9",
        operation_id="operation-4",
        mode="supervised",
        autonomous_solve=False,
    )


def test_a_new_request_clears_a_stale_halt():
    """The cycle Russel hit: a halt on one turn silently disabled every later turn on that channel.

    `_stop_requested` was set to False in exactly one place — `__init__` — so a per-channel Model that
    halted once stayed halted forever, and the next request either crashed on the unassigned `result`
    or (once guarded) did nothing at all.
    """
    model = _bare_model()
    model.install_request_contract(_request_contract("request-1"))

    model._stop_requested = True
    model._stop_reason = "no_progress"

    model.install_request_contract(_request_contract("request-2"))
    assert model._stop_requested is False, "a new request inherited the previous request's halt"
    assert model._stop_reason == ""


def test_reinstalling_the_same_request_does_not_undo_a_stop():
    """The control that keeps the kill switch honest.

    An operator stop landing mid-request must survive a re-install of that same contract; only a
    genuinely new request counts as consent to proceed. If this ever passes by clearing anyway, the
    reset is in the wrong place and the fix should move to rotating the Model instead.
    """
    model = _bare_model()
    contract = _request_contract("request-1")
    model.install_request_contract(contract)

    model._stop_requested = True
    model._stop_reason = "operator"

    model.install_request_contract(contract)
    assert model._stop_requested is True, "re-installing the same request undid an operator stop"
    assert model._stop_reason == "operator"


# --- the None crash at the Supervisor node ---------------------------------------------------------

def _flag_copy(result):
    """The exact shape of the production loop at model.py:6814, including its guard.

    Kept as a local mirror rather than importing, because the original is a closure inside a node
    factory that needs a fully built Model. If the production guard is removed, the live path raises
    and `test_none_result_is_survivable_in_production` catches it.
    """
    update: dict = {}
    if isinstance(result, dict):
        for flag in ("recursion_summary_requested", "recursion_handback"):
            if flag in result:
                update[flag] = result[flag]
    return update


@pytest.mark.parametrize("result", [None, {}, {"recursion_handback": True}, {"unrelated": 1}])
def test_flag_copy_survives_any_node_result(result):
    out = _flag_copy(result)
    assert isinstance(out, dict)
    if isinstance(result, dict) and "recursion_handback" in result:
        assert out["recursion_handback"] is True


def test_none_result_is_survivable_in_production():
    """Pin the guard in the real file: an unguarded `flag in result` raises on None.

    Reading the source is deliberate. The crash lived inside a closure that requires a constructed
    Model with a live graph, so a behavioural test would cost more than it proves; what must not
    regress is that the membership test is guarded at all.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "ai" / "langgraph" / "model.py"
    text = source.read_text()
    anchor = 'for flag in ("recursion_summary_requested", "recursion_handback"):'
    parts = text.split(anchor)
    sites = len(parts) - 1
    assert sites >= 2, (
        f"expected at least 2 flag-copy sites, found {sites}; the loop moved and this guard is "
        "no longer inspecting what it thinks it is"
    )
    for index, before in enumerate(parts[:-1]):
        assert "isinstance(result, dict)" in before[-400:], (
            f"flag-copy site {index + 1} of {sites} is not guarded against a None node result — "
            "this is the TypeError observed live on 2026-08-03. Both sites need the guard; the "
            "second one was found only because this test checked every occurrence, not the first."
        )
