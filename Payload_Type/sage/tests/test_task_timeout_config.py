"""Operator-configurable timeouts (ISA 9A, ISC-33/ISC-34).

The defaults Sage shipped were lab numbers. A callback on a range answers in seconds, so the
hardcoded 300s Mythic budget was never exercised; an agent sleeping for OPSEC reasons on a real
engagement cannot answer inside it at all, so every task would time out rather than the occasional one.

Two knobs, deliberately asymmetric:

* the Mythic task budget adapts per call, raised when the liveness assessment established that this
  callback sleeps longer than the configured base; and
* the graph node idle timeout cannot adapt — it is fixed when the graph is built — so it is a plain
  operator setting that defaults to following the Mythic base.

The property worth defending hardest is that the derivation only ever RAISES. Sage's sleep detection
is Apollo-shaped (it matches the literal command name `sleep` and an `interval` parameter), so a wrong
reading must stay harmless in the dangerous direction.

Mirrors the repo's no-pytest-asyncio convention.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SAGE_ROOT))

from ai.langgraph import model as model_module  # noqa: E402
from ai.langgraph.mythic_tools import (  # noqa: E402
    SAGE_MYTHIC_SLEEP_TIMEOUT_MULTIPLIER,
    SAGE_MYTHIC_TASK_TIMEOUT,
    MythicTools,
    _env_positive_int,
)


def _tools() -> MythicTools:
    """A MythicTools shell: the timeout helpers touch no client and no constructor state."""
    return MythicTools.__new__(MythicTools)


# --------------------------------------------------------------------------------------------------
# _env_positive_int
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 300),          # unset
        ("", 300),            # empty
        ("   ", 300),         # whitespace only
        ("600", 600),         # plain
        (" 600 ", 600),       # padded
        ("600.9", 600),       # float truncates
        ("0", 300),           # zero is not a usable timeout
        ("-5", 300),          # negative is not a usable timeout
        ("abc", 300),         # junk in an operator-edited .env must not stop startup
        ("6e2", 600),         # scientific notation still parses
    ],
)
def test_env_positive_int_falls_back_on_anything_unusable(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SAGE_TEST_TIMEOUT_KNOB", raising=False)
    else:
        monkeypatch.setenv("SAGE_TEST_TIMEOUT_KNOB", raw)
    assert _env_positive_int("SAGE_TEST_TIMEOUT_KNOB", 300) == expected


# --------------------------------------------------------------------------------------------------
# _derive_task_timeout
# --------------------------------------------------------------------------------------------------


def test_unknown_sleep_leaves_the_base_untouched():
    """No cached sleep means no opinion, which must reproduce pre-9A behaviour exactly."""
    assert _tools()._derive_task_timeout(7) == SAGE_MYTHIC_TASK_TIMEOUT


def test_short_sleep_never_lowers_the_operator_base():
    """The derivation is a floor-raiser. A fast callback must not shorten the configured budget."""
    tools = _tools()
    tools._record_callback_sleep(7, {"effective_sleep_seconds": 5, "sleep_source": "sleep_task"})

    assert tools._derive_task_timeout(7) == SAGE_MYTHIC_TASK_TIMEOUT


def test_long_sleep_raises_the_budget():
    """A callback sleeping past the base gets a budget covering several cycles plus round trip."""
    tools = _tools()
    tools._record_callback_sleep(7, {"effective_sleep_seconds": 3600, "sleep_source": "sleep_task"})

    expected = 3600 * SAGE_MYTHIC_SLEEP_TIMEOUT_MULTIPLIER + 60
    assert tools._derive_task_timeout(7) == expected
    assert expected > SAGE_MYTHIC_TASK_TIMEOUT


def test_derivation_is_per_callback():
    """Two callbacks with different sleeps must not share a budget."""
    tools = _tools()
    tools._record_callback_sleep(1, {"effective_sleep_seconds": 3600, "sleep_source": "sleep_task"})
    tools._record_callback_sleep(2, {"effective_sleep_seconds": 5, "sleep_source": "sleep_task"})

    assert tools._derive_task_timeout(1) > tools._derive_task_timeout(2)
    assert tools._derive_task_timeout(2) == SAGE_MYTHIC_TASK_TIMEOUT


@pytest.mark.parametrize(
    "liveness",
    [
        {"effective_sleep_seconds": None, "sleep_source": "unknown"},
        {"effective_sleep_seconds": 0, "sleep_source": "c2_profile"},
        {"effective_sleep_seconds": -30, "sleep_source": "sleep_task"},
        {"effective_sleep_seconds": "3600", "sleep_source": "sleep_task"},  # str, not a number
        {},
    ],
    ids=["none", "zero", "negative", "string", "empty"],
)
def test_unusable_sleep_readings_are_not_cached(liveness):
    """An unestablished sleep must read as unknown, never as a confident number."""
    tools = _tools()
    tools._record_callback_sleep(7, liveness)

    assert tools._derive_task_timeout(7) == SAGE_MYTHIC_TASK_TIMEOUT


# --------------------------------------------------------------------------------------------------
# Graph node idle timeout
# --------------------------------------------------------------------------------------------------


def test_node_idle_timeout_is_independent_of_the_mythic_budget():
    """The idle timeout is a stall detector and must NOT track how long a wait is allowed to be.

    An earlier revision set it to `SAGE_MYTHIC_TASK_TIMEOUT + 120`. That looked safe and was not: the
    Mythic budget is raised per call for a slow-sleeping callback, so a callback sleeping past roughly
    87 seconds produced a budget the fixed node timeout could no longer cover, and the graph cancelled
    the node while the wait was still legitimate. Coupling them the other way is worse — a six-hour
    sleeper would buy a six-hour blindness window on every node in the session.

    `_graph_heartbeat` is what makes independence correct: a deliberate wait announces itself, so
    silence past this window means stalled rather than patient.
    """
    assert model_module.SAGE_NODE_IDLE_TIMEOUT != SAGE_MYTHIC_TASK_TIMEOUT + 120
    assert model_module.SAGE_NODE_IDLE_TIMEOUT == 300


def test_raising_the_mythic_budget_does_not_move_the_idle_timeout(monkeypatch):
    """Sleep length belongs to the Mythic budget alone. Reloading with a huge base must not drag the
    stall detector along with it."""
    import importlib

    monkeypatch.setenv("SAGE_MYTHIC_TASK_TIMEOUT", "21600")  # six hours
    reloaded_tools = importlib.reload(sys.modules["ai.langgraph.mythic_tools"])
    try:
        assert reloaded_tools.SAGE_MYTHIC_TASK_TIMEOUT == 21600
        reloaded_model = importlib.reload(sys.modules["ai.langgraph.model"])
        try:
            assert reloaded_model.SAGE_NODE_IDLE_TIMEOUT == 300
        finally:
            monkeypatch.delenv("SAGE_MYTHIC_TASK_TIMEOUT", raising=False)
            importlib.reload(sys.modules["ai.langgraph.model"])
    finally:
        monkeypatch.delenv("SAGE_MYTHIC_TASK_TIMEOUT", raising=False)
        importlib.reload(sys.modules["ai.langgraph.mythic_tools"])
        importlib.reload(sys.modules["ai.langgraph.model"])


def test_graph_build_applies_the_timeout_policy():
    """`_rebuild_graph` must actually install the policy, not merely compute the constant."""
    source = (SAGE_ROOT / "ai" / "langgraph" / "model.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    rebuild = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_rebuild_graph"
    )
    body = ast.dump(rebuild)

    assert "set_node_defaults" in body, "_rebuild_graph no longer sets node defaults"
    assert "TimeoutPolicy" in body, "_rebuild_graph no longer installs a TimeoutPolicy"
    assert "SAGE_NODE_IDLE_TIMEOUT" in body, "the policy is no longer driven by the operator knob"
    assert "run_timeout" not in body, (
        "run_timeout is never refreshed by progress signals; it would cap legitimate long work"
    )
