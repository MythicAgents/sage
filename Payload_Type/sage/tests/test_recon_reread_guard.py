"""Recon re-read guard.

Regression for the 2026-06-07 essos run that hit the 250-step recursion limit while issuing only ~2 Mythic
commands: the budget was burned on redundant recon — get_task_history_for_callback 98×, list_callbacks 29×.
The guard returns a "stop re-reading, act" nudge after repeated identical reads within one task epoch, and
resets when a command is issued (epoch bump) so a legitimate post-action re-read is always allowed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools  # noqa: E402


def _mt():
    return mythic_tools.MythicTools(agent_task_id="recon-test")


def test_third_reread_warns():
    mt = _mt()
    assert mt._recon_reread_guard("get_task_history_for_callback", 50) is None   # 1st
    assert mt._recon_reread_guard("get_task_history_for_callback", 50) is None   # 2nd
    w = mt._recon_reread_guard("get_task_history_for_callback", 50)              # 3rd
    assert w is not None and "STOP" in w


def test_epoch_bump_resets_guard():
    mt = _mt()
    mt._recon_reread_guard("get_task_history_for_callback", 50)
    mt._recon_reread_guard("get_task_history_for_callback", 50)
    assert mt._recon_reread_guard("get_task_history_for_callback", 50) is not None  # 3rd warns
    mt._recon_epoch += 1  # a command was issued → new state
    assert mt._recon_reread_guard("get_task_history_for_callback", 50) is None      # fresh epoch allows a read


def test_distinct_targets_independent():
    mt = _mt()
    # Different callbacks and different tools are tracked separately; none trips on a single read each.
    assert mt._recon_reread_guard("get_task_history_for_callback", 50) is None
    assert mt._recon_reread_guard("get_task_history_for_callback", 51) is None
    assert mt._recon_reread_guard("list_callbacks", "all") is None


def test_guard_never_raises_on_bad_state():
    mt = _mt()
    mt._recon_call_log = None  # corrupt — guard must fail-open to None, never throw
    assert mt._recon_reread_guard("list_callbacks", "all") is None
