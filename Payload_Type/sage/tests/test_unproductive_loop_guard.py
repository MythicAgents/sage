"""Phase-1 loop-guard backstop: a SUCCESSFUL-but-unproductive repeated command (e.g. `shell klist` returning
the same tickets, with only drifting timestamps) must be caught. The existing failure circuit breaker cannot
catch it because SUCCESS resets the breaker; this guard tracks a consecutive-identical-action streak instead.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import command_builder  # noqa: E402
import mythic_tools  # noqa: E402

SUCCESS = command_builder.ResultClass.SUCCESS.value
GENUINE = command_builder.ResultClass.GENUINE.value


def _guard():
    """A bare MythicTools carrying only the loop-guard state (avoids the heavy live-client constructor)."""
    g = object.__new__(mythic_tools.MythicTools)
    g._unproductive_repeat_limit = 3
    g._last_action_sig = None
    g._action_repeat_count = 0
    g._unproductive_tripped = set()
    g._volatile_output_patterns = mythic_tools._VOLATILE_OUTPUT_PATTERNS
    return g


def _klist(ts):
    return f"Cached Tickets: (2)\n#0> Client: samwell @ NORTH\n    valid starting: {ts}  expires: {ts}"


def test_three_identical_successes_trip_loop_guard():
    g = _guard()
    assert g._unproductive_repeat_nudge("shell klist", 14, "", _klist("6/19/2026 10:00:01"), SUCCESS) is None
    assert g._unproductive_repeat_nudge("shell klist", 14, "", _klist("6/19/2026 10:00:05"), SUCCESS) is None
    nudge = g._unproductive_repeat_nudge("shell klist", 14, "", _klist("6/19/2026 10:00:09"), SUCCESS)
    assert nudge is not None and "STOP" in nudge
    # The action is now flagged so the pre-issue guard refuses further repeats.
    assert g._unproductive_action_key("shell klist", 14, "") in g._unproductive_tripped


def test_drifting_timestamps_are_normalized_before_hashing():
    # Same klist with wholly different times/dates must still be treated as identical (else the guard no-ops).
    g = _guard()
    a = g._normalize_volatile_output(_klist("6/19/2026 10:00:01"))
    b = g._normalize_volatile_output(_klist("12/31/2026 23:59:59"))
    assert a and a == b


def test_guid_and_ipport_drift_is_normalized():
    # Outputs that drift only in GUIDs or ephemeral IP:port pairs must normalize identically (Forge D-i).
    g = _guard()
    a = g._normalize_volatile_output("session 1A2B3C4D-1111-2222-3333-444455556666 from 10.4.10.22:51514")
    b = g._normalize_volatile_output("session 9F8E7D6C-9999-8888-7777-666655554444 from 10.4.10.22:60001")
    assert a and a == b


def test_interleaved_different_command_resets_streak():
    g = _guard()
    out = _klist("6/19/2026 10:00:01")
    assert g._unproductive_repeat_nudge("shell klist", 14, "", out, SUCCESS) is None
    assert g._unproductive_repeat_nudge("shell klist", 14, "", out, SUCCESS) is None
    # A different successful action breaks the streak (this is real work between checks).
    assert g._unproductive_repeat_nudge("shell whoami", 14, "", "NORTH\\samwell", SUCCESS) is None
    # klist again should NOT immediately trip — the streak restarted.
    assert g._unproductive_repeat_nudge("shell klist", 14, "", out, SUCCESS) is None
    assert not g._unproductive_tripped


def test_failure_result_does_not_count_toward_loop_guard():
    g = _guard()
    out = _klist("6/19/2026 10:00:01")
    assert g._unproductive_repeat_nudge("shell klist", 14, "", out, SUCCESS) is None
    # A failing result breaks the streak entirely (failures are the circuit breaker's job).
    assert g._unproductive_repeat_nudge("shell klist", 14, "", "error: access denied", GENUINE) is None
    assert g._action_repeat_count == 0
    assert g._unproductive_repeat_nudge("shell klist", 14, "", out, SUCCESS) is None
    assert g._action_repeat_count == 1


def test_new_output_clears_trip_and_allows_rerun():
    g = _guard()
    for _ in range(3):
        g._unproductive_repeat_nudge("shell klist", 14, "", _klist("6/19/2026 10:00:01"), SUCCESS)
    assert g._unproductive_action_key("shell klist", 14, "") in g._unproductive_tripped
    # A genuinely different klist (e.g. a freshly forged ticket now present) clears the trip.
    nudge = g._unproductive_repeat_nudge("shell klist", 14, "", "Cached Tickets: (3) new golden ticket", SUCCESS)
    assert nudge is None
    assert not g._unproductive_tripped


def test_loop_guard_is_independent_of_failure_breaker():
    g = _guard()
    g._task_failure_counts = {}
    for _ in range(3):
        g._unproductive_repeat_nudge("shell klist", 14, "", _klist("6/19/2026 10:00:01"), SUCCESS)
    # The unproductive-success guard must NOT ride the failure-breaker counters.
    assert g._task_failure_counts == {}


def test_distinct_callbacks_do_not_share_a_streak():
    g = _guard()
    out = _klist("6/19/2026 10:00:01")
    assert g._unproductive_repeat_nudge("shell klist", 14, "", out, SUCCESS) is None
    assert g._unproductive_repeat_nudge("shell klist", 99, "", out, SUCCESS) is None
    assert g._unproductive_repeat_nudge("shell klist", 14, "", out, SUCCESS) is None
    # No single (command, callback, params) signature hit the limit consecutively.
    assert not g._unproductive_tripped
