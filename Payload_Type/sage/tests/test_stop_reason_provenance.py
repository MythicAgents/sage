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

import re

import pytest

from ai.langgraph.model import (
    STOP_REASON_NO_PROGRESS,
    STOP_REASON_OPERATOR,
    STOP_REASON_RESUME_REFUSED,
    STOP_REASON_RUNTIME_ERROR,
    STOP_REASON_SESSION_ROTATED,
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
    for reason in (
        STOP_REASON_OPERATOR,
        STOP_REASON_NO_PROGRESS,
        STOP_REASON_TERMINAL_BLOCKER,
        STOP_REASON_SESSION_ROTATED,
        STOP_REASON_RESUME_REFUSED,
        STOP_REASON_RUNTIME_ERROR,
        "",
    ):
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


def test_session_rotation_does_not_blame_the_operator():
    """`service.py` rotates a session when the Mythic token, operation, or config changes."""
    notice = stop_notice_for(STOP_REASON_SESSION_ROTATED)
    assert OPERATOR_PHRASE not in notice
    assert "replaced" in notice.lower()


def test_refused_resume_does_not_blame_the_operator():
    """A fresh prompt over a pending approval drops the session; the operator cancelled nothing."""
    notice = stop_notice_for(STOP_REASON_RESUME_REFUSED)
    assert OPERATOR_PHRASE not in notice
    assert "pending" in notice.lower()


def test_request_stop_defaults_to_operator_but_accepts_a_reason():
    """`request_stop` is both the operator's kill switch AND the service's cleanup entry point.

    Assuming the former was wrong: `_stop_and_close_request_lifecycles` calls it during rotation and
    refused resumes, so a bare call must keep operator wording while a labelled call must not.
    """
    def _stoppable():
        model = _bare_model()
        model.task_id = 0            # request_stop logs it
        model._running_tasks = set()  # and cancels registered invoke tasks
        model._subgoal_authority_lock = None
        model._subgoal_authority = None
        return model

    model = _stoppable()
    model.request_stop()
    assert model._stop_reason == STOP_REASON_OPERATOR

    model2 = _stoppable()
    model2.request_stop(STOP_REASON_SESSION_ROTATED)
    assert model2._stop_reason == STOP_REASON_SESSION_ROTATED
    assert OPERATOR_PHRASE not in stop_notice_for(model2._stop_reason)


def test_service_lifecycle_call_sites_state_a_reason():
    """Every `_stop_and_close_request_lifecycles` call must state a known reason.

    Not "none may claim operator": one of the five sites — the `asyncio.CancelledError` handler — IS a
    genuine operator cancel and correctly says so. What must not happen is a site staying silent and
    inheriting a default, or naming a reason that has no wording behind it.

    Source-level on purpose: constructing the chat service to exercise rotation is far more machinery
    than the property needs, and the failure mode being guarded is regression by omission — a new
    call site added without a reason. The equivalent source guard on the flag-copy loop found a second
    unguarded site I had missed, so this is a pattern that has already paid for itself here.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "sage_chat" / "service.py"
    ).read_text()

    known = {
        STOP_REASON_OPERATOR,
        STOP_REASON_SESSION_ROTATED,
        STOP_REASON_RESUME_REFUSED,
        STOP_REASON_RUNTIME_ERROR,
    }
    # `self.`-prefixed so the `async def` signature is not parsed as one of its own call sites.
    call = "self._stop_and_close_request_lifecycles("
    sites = source.count(call)
    assert sites >= 5, (
        f"expected at least 5 call sites, found {sites}; this guard is looking at the wrong thing. "
        "The original sweep assumed 3 and this assertion is what revealed the other two."
    )

    for chunk in source.split(call)[1:]:
        invocation = chunk[: chunk.index(")")]
        assert "reason=" in invocation, (
            f"a lifecycle teardown does not state its reason and will fall back silently: {invocation!r}"
        )
        match = re.search(r'reason=["\']([a-z_]+)["\']', invocation)
        assert match, f"lifecycle teardown's reason is not a plain literal: {invocation!r}"
        assert match.group(1) in known, (
            f"lifecycle teardown uses an unknown stop reason {match.group(1)!r}"
        )


def test_every_known_reason_has_distinct_wording():
    reasons = [
        STOP_REASON_OPERATOR,
        STOP_REASON_NO_PROGRESS,
        STOP_REASON_TERMINAL_BLOCKER,
        STOP_REASON_SESSION_ROTATED,
        STOP_REASON_RESUME_REFUSED,
    ]
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
