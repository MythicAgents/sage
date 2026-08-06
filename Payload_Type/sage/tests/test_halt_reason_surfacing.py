"""A deliberate halt must tell the operator what stopped Sage.

A live request executed SharpHound, downloaded it, ingested it, and analysed the graph — then hit
its `max_steps` budget and reported exactly this to the operator:

    Sage request failed.

Nothing about a step limit, nothing about the work having succeeded, and indistinguishable from a
crash. `_StopCheckMiddleware` set `_stop_requested` directly instead of going through
`request_stop()`, so no reason was recorded, and the argless `_OperatorStopRequested` fell through
the service's generic error branch to its fallback string.

Sage already had the right machinery — `_STOP_NOTICE_BY_REASON` / `stop_notice_for`, built after an
earlier incident where halts were wrongly blamed on the operator. The step-limit path just never
joined it.
"""

import asyncio

import pytest

from ai.langgraph.model import (
    STOP_REASON_OPERATOR,
    STOP_REASON_STEP_LIMIT,
    STOP_REASON_UNSPECIFIED,
    _OperatorStopRequested,
    _StopCheckMiddleware,
    stop_notice_for,
)
from ai.langgraph.operator_error import operator_error_text


class _Model:
    """The stop-relevant surface of Model."""

    def __init__(self, *, max_steps=0, step_count=0, stop=False, reason="", limit_hit=False):
        self._max_steps = max_steps
        self._global_step_count = step_count
        self._stop_requested = stop
        self._stop_reason = reason
        self._global_step_limit_hit = limit_hit


def _render(exc: BaseException) -> str:
    """Mirror of service.py's error branch.

    This is a COPY of production logic, so on its own it proves only that the logic is right — not
    that service.py runs it. `test_service_error_branch_actually_renders_the_halt_reason` below
    pins the real call site so this mirror cannot drift away from it unnoticed.
    """

    halt_reason = str(getattr(exc, "stop_reason", "") or "")
    if halt_reason:
        return stop_notice_for(halt_reason, str(getattr(exc, "stop_detail", "") or "")).strip()
    return operator_error_text(exc) or "Sage request failed."


# --------------------------------------------------------------------------------------
# The reported defect.
# --------------------------------------------------------------------------------------


def test_step_limit_halt_explains_itself_instead_of_saying_failed():
    mw = _StopCheckMiddleware(_Model(max_steps=20, step_count=20))

    with pytest.raises(_OperatorStopRequested) as excinfo:
        mw.before_model(state={}, runtime=None)

    text = _render(excinfo.value)
    assert "Sage request failed." not in text
    assert "step budget" in text.lower()
    assert "not a crash" in text.lower()
    # The operator gets the actual numbers, not just the concept.
    assert "21 model steps used" in text
    assert "limit 20" in text


def test_step_limit_halt_records_its_reason_on_the_model():
    """Whoever inspects the model later must see the same truth the operator was told."""

    model = _Model(max_steps=3, step_count=3)
    mw = _StopCheckMiddleware(model)

    with pytest.raises(_OperatorStopRequested):
        mw.before_model(state={}, runtime=None)

    assert model._stop_reason == STOP_REASON_STEP_LIMIT
    assert model._global_step_limit_hit is True


# --------------------------------------------------------------------------------------
# Attribution, in both directions. Blaming the wrong actor is the defect stop_notice_for exists
# to prevent, and widening its use must not reintroduce it.
# --------------------------------------------------------------------------------------


def test_operator_stop_is_not_reported_as_a_step_limit():
    mw = _StopCheckMiddleware(_Model(stop=True, reason=STOP_REASON_OPERATOR))

    with pytest.raises(_OperatorStopRequested) as excinfo:
        mw.before_model(state={}, runtime=None)

    text = _render(excinfo.value)
    assert "stopped by operator" in text.lower()
    assert "step budget" not in text.lower()


def test_step_limit_is_not_reported_as_an_operator_stop():
    mw = _StopCheckMiddleware(_Model(max_steps=1, step_count=1))

    with pytest.raises(_OperatorStopRequested) as excinfo:
        mw.before_model(state={}, runtime=None)

    assert "operator" not in _render(excinfo.value).lower()


def test_a_halt_with_no_recorded_reason_stays_neutral():
    """Never attribute an unexplained halt to the operator."""

    mw = _StopCheckMiddleware(_Model(stop=True, reason=""))

    with pytest.raises(_OperatorStopRequested) as excinfo:
        mw.before_model(state={}, runtime=None)

    assert excinfo.value.stop_reason == STOP_REASON_UNSPECIFIED
    text = _render(excinfo.value)
    assert "session halted" in text.lower()
    assert "operator" not in text.lower()


def _halted_middleware():
    return _StopCheckMiddleware(
        _Model(stop=True, reason=STOP_REASON_STEP_LIMIT, limit_hit=True)
    )


def test_sync_tool_seam_carries_the_reason():
    """A halt caught at a tool boundary must explain itself the same way."""

    with pytest.raises(_OperatorStopRequested) as excinfo:
        _halted_middleware().wrap_tool_call(request=None, handler=lambda req: None)

    assert excinfo.value.stop_reason == STOP_REASON_STEP_LIMIT
    assert "step budget" in _render(excinfo.value).lower()


def test_async_tool_seam_carries_the_reason():
    """Awaited explicitly: a coroutine does not raise until it runs, so a bare call would pass
    this test without ever entering the function."""

    async def drive():
        await _halted_middleware().awrap_tool_call(request=None, handler=lambda req: None)

    with pytest.raises(_OperatorStopRequested) as excinfo:
        asyncio.run(drive())

    assert excinfo.value.stop_reason == STOP_REASON_STEP_LIMIT
    assert "step budget" in _render(excinfo.value).lower()


# --------------------------------------------------------------------------------------
# Controls: what must NOT change.
# --------------------------------------------------------------------------------------


def test_the_reason_token_never_leaks_as_raw_error_text():
    """str(exc) must stay empty so operator_error_text is untouched by this change."""

    exc = _OperatorStopRequested(STOP_REASON_STEP_LIMIT, detail="Step budget: 21 used, limit 20.")
    assert str(exc) == ""
    assert operator_error_text(exc) == ""
    assert "step_limit" not in _render(exc)


def test_a_genuine_crash_is_unaffected():
    """The near-match control: a real fault still renders its own message verbatim."""

    assert _render(RuntimeError("bedrock throttled the request")) == (
        "bedrock throttled the request"
    )


def test_existing_stop_notice_callers_are_unchanged_without_detail():
    """`detail` is optional; every pre-existing call site must behave exactly as before."""

    for reason in (STOP_REASON_OPERATOR, STOP_REASON_UNSPECIFIED, "no_progress", "made_up"):
        assert stop_notice_for(reason) == stop_notice_for(reason, "")


def test_detail_is_appended_not_substituted():
    base = stop_notice_for(STOP_REASON_STEP_LIMIT)
    with_detail = stop_notice_for(STOP_REASON_STEP_LIMIT, "Step budget: 21 used, limit 20.")

    assert base.strip() in with_detail
    assert with_detail.endswith("Step budget: 21 used, limit 20.\n")


def test_no_step_limit_no_halt():
    """The boundary: one step below the limit must not stop anything."""

    mw = _StopCheckMiddleware(_Model(max_steps=20, step_count=18))
    assert mw.before_model(state={}, runtime=None) is None


def test_service_error_branch_actually_renders_the_halt_reason():
    """The mirror above is only worth something if production runs the same branch."""

    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "sage_chat" / "service.py"
    ).read_text()

    assert 'getattr(error, "stop_reason", "")' in src, (
        "service.py no longer consults the halt reason; the mirror in this file is now fiction"
    )
    assert "stop_notice_for(" in src
    # The fallback must survive for genuine crashes — it just must not be the only path.
    assert 'operator_error_text(error) or "Sage request failed."' in src
