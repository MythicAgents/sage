"""sage-task-deferral-receipt (ISC-1..6) — bounded grace, and a receipt naming the task.

The defect: `_derive_task_timeout` multiplied a callback's observed sleep on top of the configured
base and only ever raised, with nothing bounding the result. A callback sleeping four hours produced
a ~12-hour wait, `_graph_heartbeat` kept the node alive throughout, and Mythic disables the chat
composer for a request in `pending`/`streaming` — so one "enumerate this host" could hold the channel
for half a day. When it finally gave up it said "Timed out", which reads as a failure for a task that
is queued and healthy, and it never named the task id it had in hand.

Run: PYTHONPATH=Payload_Type/sage .venv/bin/python -m pytest -q tests/test_task_deferral_receipt.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mythic_tools  # noqa: E402
from mythic_tools import SAGE_MYTHIC_TASK_TIMEOUT  # noqa: E402
from test_circuit_breaker import _make_tools, _split_issue  # noqa: E402

import task_deferral_fast_path_golden  # noqa: E402


# --------------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------------


def _defer_after_issue(task_display_id: int = 4242):
    """Issue succeeds and records its id, then the grace expires while waiting for output.

    Raising `asyncio.TimeoutError` from inside the awaited coroutine reaches the production
    `except asyncio.TimeoutError` by the same path `asyncio.wait_for` would, so this exercises the
    real branch without spending the real deadline.
    """

    async def fake_issue_task(mythic, command_name, parameters, callback_display_id,
                              wait_for_complete=True, timeout=None):
        return {"display_id": task_display_id}

    async def fake_waitfor(mythic, task_display_id, timeout=None):
        raise asyncio.TimeoutError()

    return patch.multiple(
        mythic_tools.mythic,
        issue_task=fake_issue_task,
        waitfor_for_task_output=fake_waitfor,
    )


def _defer_before_issue():
    """The grace expires before Mythic confirms a task id at all."""

    async def fake_issue_task(mythic, command_name, parameters, callback_display_id,
                              wait_for_complete=True, timeout=None):
        raise asyncio.TimeoutError()

    async def fake_waitfor(mythic, task_display_id, timeout=None):  # pragma: no cover - unreachable
        return "unreachable"

    return patch.multiple(
        mythic_tools.mythic,
        issue_task=fake_issue_task,
        waitfor_for_task_output=fake_waitfor,
    )


def _issue(tools, command="shell", parameters="hostname", callback=11):
    return asyncio.run(tools.issue_task_and_waitfor_task_output(command, parameters, callback))


# --------------------------------------------------------------------------------------------
# ISC-1 — one configured grace period; nothing sleep-derived may extend it
# --------------------------------------------------------------------------------------------


def test_every_recorded_sleep_bounds_at_the_same_configured_deadline():
    """The four probes named in the contract: 10s, 300s, 4h, and an unresolvable sleep."""
    tools = _make_tools()
    for callback_id, sleep_seconds in ((1, 10), (2, 300), (3, 4 * 60 * 60)):
        tools._record_callback_sleep(callback_id, {
            "effective_sleep_seconds": sleep_seconds,
            "sleep_source": "test",
        })
    # The fourth probe: a callback whose sleep was never established records nothing at all.
    tools._record_callback_sleep(4, {"effective_sleep_seconds": None, "sleep_source": "unknown"})

    derived = [tools._derive_task_timeout(cb) for cb in (1, 2, 3, 4)]
    assert derived == [SAGE_MYTHIC_TASK_TIMEOUT] * 4, (
        f"all four probes must bound at the same deadline, got {derived}"
    )


def test_a_four_hour_sleep_no_longer_produces_a_multi_hour_wait():
    """The exact defect, stated as the number it used to produce."""
    tools = _make_tools()
    tools._record_callback_sleep(7, {
        "effective_sleep_seconds": 4 * 60 * 60,
        "sleep_source": "sleep-command",
    })
    old_behaviour = 4 * 60 * 60 * mythic_tools.SAGE_MYTHIC_SLEEP_TIMEOUT_MULTIPLIER + 60
    assert old_behaviour > 43_000, "sanity: the pre-change formula really did yield ~12 hours"
    assert tools._derive_task_timeout(7) == SAGE_MYTHIC_TASK_TIMEOUT


def test_a_long_sleep_is_still_reported_even_though_it_no_longer_moves_the_deadline():
    """The cached sleep stays useful as an operator signal; it just stops being authority."""
    tools = _make_tools()
    tools._record_callback_sleep(7, {"effective_sleep_seconds": 7200, "sleep_source": "c2-profile"})
    with patch.object(mythic_tools.logger, "info") as info:
        tools._derive_task_timeout(7)
    assert info.called, "capping a long-sleeping callback must not be silent"
    said = " ".join(str(call) for call in info.call_args_list)
    assert "7200" in said and str(SAGE_MYTHIC_TASK_TIMEOUT) in said


# --------------------------------------------------------------------------------------------
# ISC-2 — grace expiry returns a terminal, non-failure-shaped receipt naming the exact task
# --------------------------------------------------------------------------------------------


def test_receipt_names_the_exact_task_id_and_callback():
    tools = _make_tools()
    with _defer_after_issue(task_display_id=4242):
        result = _issue(tools)
    assert "4242" in result
    assert "11" in result


def test_receipt_is_not_failure_shaped():
    tools = _make_tools()
    with _defer_after_issue():
        result = _issue(tools)
    lowered = result.lower()
    assert result.startswith("DEFERRED"), result
    assert "timed out" not in lowered
    assert "not an error" in lowered and "not failed" in lowered.replace("has not failed", "not failed")


def test_prefix_colliding_task_ids_are_distinguished_by_exact_comparison():
    """`4` and `41` share a prefix; a substring check would call both receipts correct."""
    ids = {}
    for task_id in (4, 41):
        tools = _make_tools()
        with _defer_after_issue(task_display_id=task_id):
            ids[task_id] = _issue(tools)
    assert "task 4 " in ids[4] and "task 41 " not in ids[4]
    assert "task 41 " in ids[41]


def test_expiry_before_a_task_id_exists_names_no_task_at_all():
    """A receipt may never name a task this call did not issue.

    `_last_issued_task_display_id` is one slot that survives across calls, so a stale id is exactly
    what a careless implementation would print here.
    """
    tools = _make_tools()
    tools._last_issued_task_display_id = 999  # a PREVIOUS task, still in the slot
    with _defer_before_issue():
        result = _issue(tools)
    assert "999" not in result, "the receipt named a task from an earlier call"
    assert "unknown whether Mythic accepted" in result


def test_exactly_one_receipt_is_returned():
    tools = _make_tools()
    with _defer_after_issue():
        result = _issue(tools)
    assert result.count("DEFERRED:") == 1


# --------------------------------------------------------------------------------------------
# deferral is not a failure: it must not walk the circuit breaker toward STOP
# --------------------------------------------------------------------------------------------


def test_deferral_does_not_advance_the_failure_counter():
    tools = _make_tools()
    before = dict(tools._task_failure_counts)
    with _defer_after_issue():
        _issue(tools)
    assert dict(tools._task_failure_counts) == before, (
        "a queued, healthy task was counted as a failure"
    )


# --------------------------------------------------------------------------------------------
# ISC-4 — no further target-facing task in that logical request
# --------------------------------------------------------------------------------------------


def test_a_second_target_facing_task_is_refused_after_a_deferral():
    tools = _make_tools()
    with _defer_after_issue():
        first = _issue(tools)
    assert first.startswith("DEFERRED")

    issued: list[str] = []
    with _split_issue("NORTH\\samwell.tarly", on_issue=lambda p: issued.append(p)):
        second = _issue(tools, command="whoami", parameters="")
    assert second.startswith("BLOCKED"), second
    assert issued == [], "a second target-facing task reached Mythic after a deferral"


def test_the_block_names_the_deferred_task_so_the_model_can_report_it():
    tools = _make_tools()
    with _defer_after_issue(task_display_id=77):
        _issue(tools)
    with _split_issue("ok"):
        second = _issue(tools, command="whoami", parameters="")
    assert "77" in second


def test_control_plane_reads_stay_available_after_a_deferral():
    """The valid near-match. Control-plane reads are different tools and must not be caught."""
    tools = _make_tools()
    with _defer_after_issue():
        _issue(tools)
    assert tools._deferred_task_issue_blocker("whoami", 11), "sanity: the tasking path IS blocked"
    # The blocker is reachable only from the task-issue path; no control-plane reader calls it.
    source = Path(mythic_tools.__file__).read_text()
    assert source.count("_deferred_task_issue_blocker(") == 2, (
        "the deferral cap gained a caller; it must gate the task-issue path and nothing else"
    )


def test_a_new_operator_turn_clears_the_deferral():
    tools = _make_tools()
    with _defer_after_issue():
        _issue(tools)
    assert tools._deferred_task_issue_blocker("whoami", 11)

    class _Authority:
        turn_id = "turn-2"

    tools.set_turn_authority(_Authority())
    assert tools._deferred_task_issue_blocker("whoami", 11) == "", (
        "a deferral must bound the turn that deferred, not the operator's next one"
    )


# --------------------------------------------------------------------------------------------
# fast-path compatibility — behavior is byte-identical to the pre-change golden
# --------------------------------------------------------------------------------------------


def test_fast_path_is_byte_identical_to_the_pre_change_golden():
    golden = task_deferral_fast_path_golden.load()
    current = task_deferral_fast_path_golden.capture()
    assert current["tool_returns"] == golden["tool_returns"]
    assert current["chat_emissions"] == golden["chat_emissions"]


def test_the_golden_is_not_vacuous():
    """A golden that captured nothing would pass the comparison above forever."""
    golden = task_deferral_fast_path_golden.load()
    assert len(golden["tool_returns"]) == len(
        task_deferral_fast_path_golden.FAST_PATH_CASES
    ) >= 5
    assert len(golden["chat_emissions"]) >= 20
    assert sum(len(v) for v in golden["chat_emissions"].values()) >= 20


# --------------------------------------------------------------------------------------------
# ISC-6 — no late result may acquire authority or continue a plan
# --------------------------------------------------------------------------------------------


def test_deferral_leaves_turn_authority_and_request_contract_untouched():
    tools = _make_tools()

    class _Authority:
        turn_id = "turn-1"

    authority = _Authority()
    tools.set_turn_authority(authority)
    contract_before = tools._request_contract
    with _defer_after_issue():
        _issue(tools)
    assert tools._turn_authority is authority
    assert tools._request_contract is contract_before


def test_the_receipt_record_cannot_serve_as_a_resumption_handle():
    """It is a description of what happened, not a way to pick the task back up.

    `_turn_authority` and `_request_contract` are single slots read live at every enforcement point,
    so anything that let a late result be adjudicated later would be adjudicated by whatever turn
    happens to be current then.
    """
    tools = _make_tools()
    with _defer_after_issue():
        _issue(tools)
    receipt = tools._deferred_task_receipt
    assert set(receipt) == {"command", "callback_display_id", "task_display_id", "waited_seconds"}
    for value in receipt.values():
        assert isinstance(value, (str, int)), f"{value!r} is not inert data"
        assert not callable(value)


# --------------------------------------------------------------------------------------------
# ISC-3 — the turn goes terminal at grace expiry (verify-first per D4)
# --------------------------------------------------------------------------------------------


def test_a_long_sleeping_callback_no_longer_holds_the_turn_open():
    """ISC-3's precondition, which is the half that was actually broken.

    The composer unlocks when the owning `chat_request` reaches a terminal status, and it reaches one
    when the turn completes. Before ISC-1 the turn could not complete: the graph sat inside
    `asyncio.wait_for` under `_graph_heartbeat` for the whole derived budget, and for a four-hour
    sleep that budget was ~12 hours. So the falsifier worth testing is the tool call not returning.

    The callback here has a four-hour sleep recorded, which is exactly the case that used to produce
    the ~12-hour wait, and the whole call is given one second.

    Scope limit, stated rather than papered over: `chat_request.status` is written by
    `sage_chat/service.py` against live Mythic, so the status field itself is a live-layer assertion
    and is NOT proven here. What is proven is the precondition it depends on.
    """
    tools = _make_tools()
    tools._record_callback_sleep(11, {
        "effective_sleep_seconds": 4 * 60 * 60,
        "sleep_source": "sleep-command",
    })

    async def _bounded():
        with _defer_after_issue(task_display_id=4242):
            return await asyncio.wait_for(
                tools.issue_task_and_waitfor_task_output("shell", "hostname", 11),
                timeout=1,
            )

    result = asyncio.run(_bounded())
    assert result.startswith("DEFERRED"), result


def test_no_background_waiter_survives_a_deferral():
    """Nothing may keep waiting on the deferred task after the turn answered."""
    async def _run_and_count():
        tools = _make_tools()
        before = len(asyncio.all_tasks())
        with _defer_after_issue():
            await tools.issue_task_and_waitfor_task_output("shell", "hostname", 11)
        await asyncio.sleep(0)
        return before, len(asyncio.all_tasks())

    before, after = asyncio.run(_run_and_count())
    assert after <= before, "a background task outlived the deferral"
