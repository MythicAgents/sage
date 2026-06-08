"""Anti-cycle guard for get_ttp_guidance.

Regression for the 2026-06-07 BRAAVOS-LAPS run, where the agent called get_ttp_guidance ~7× with near-identical
goals ("read LAPS password … controlled group context") instead of executing — burning the budget to the
deadline. The guard returns an escalating "stop planning, execute or handback" nudge on the 3rd near-identical
request. Distinct exploration goals must NOT trip it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools  # noqa: E402


def _mt():
    return mythic_tools.MythicTools(agent_task_id="guidance-cycle-test")


def test_third_near_identical_goal_warns():
    mt = _mt()
    assert mt._record_and_check_guidance_cycle(
        "read LAPS password over LDAP using controlled group context") is None          # 1st
    assert mt._record_and_check_guidance_cycle(
        "read LAPS password over LDAP using controlled group context now") is None       # 2nd (reworded)
    warning = mt._record_and_check_guidance_cycle(
        "read LAPS password with controlled group context please")                       # 3rd
    assert warning is not None
    assert "STOP planning" in warning


def test_real_loop_goals_trip_the_guard():
    # The actual goals logged during the stuck run (paraphrased core: read LAPS in a controlled group context).
    mt = _mt()
    goals = [
        "read LAPS password over LDAP using current Kerberos foreign group context and abuse ADCS",
        "forge kerberos ticket with Rubeus using krbtgt hash and read LAPS password",
        "read LAPS password from Active Directory using controlled group context",
        "read LAPS password from Active Directory with a controlled group context",
    ]
    warnings = [mt._record_and_check_guidance_cycle(g) for g in goals]
    assert any(w is not None for w in warnings), warnings


def test_distinct_goals_do_not_warn():
    mt = _mt()
    assert mt._record_and_check_guidance_cycle("dump LSASS on the foothold") is None
    assert mt._record_and_check_guidance_cycle("enumerate the domain trusts") is None
    assert mt._record_and_check_guidance_cycle("request an ADCS certificate via ESC1") is None


def test_empty_goal_is_safe():
    mt = _mt()
    assert mt._record_and_check_guidance_cycle("") is None
    assert mt._record_and_check_guidance_cycle("   ") is None
