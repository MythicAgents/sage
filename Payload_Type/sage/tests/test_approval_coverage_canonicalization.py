"""Regression tests for the ISC-49R S4-01 approval-coverage defect.

An approved guarded shell action could never execute. The approval claim captured the model's
structured proposal (``parameters={"command": "whoami"}``) while the effect path passed the
flattened shell string (``parameters="whoami"``). ``_approval_effect_blocker`` compares the two
sides for exact equality, so the shape difference read as a scope change: the effect was denied,
the agent re-proposed, the operator approved again, and the request livelocked without ever
issuing a Mythic task.

Live evidence: channel 40 / request 55 — three byte-identical approvals (one ``action_digest``),
zero Mythic tasks, request never terminalised. Fixed by canonicalizing ``parameters`` through the
product's existing ``_shell_parameter_text`` on BOTH sides of the comparison.

These tests pin the canonicalization *and* its limits: the fix must not make genuinely different
effects compare equal, and must not touch commands whose parameters are legitimately structured.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph.mythic_tools import MythicTools  # noqa: E402

TOOL = "issue_task_and_waitfor_task_output"


@pytest.fixture
def tools() -> MythicTools:
    """A bare instance: the method under test only needs the class's own signatures."""
    return object.__new__(MythicTools)


def _effective(tools: MythicTools, **arguments):
    return tools._effective_request_action_arguments(TOOL, dict(arguments))


def test_structured_and_flattened_shell_parameters_canonicalize_identically(tools):
    """The exact S4-01 failure: the approved proposal's shape must not be a scope change."""
    approved = _effective(
        tools, callback_display_id=3, command="shell", parameters={"command": "whoami"}
    )
    executed = _effective(tools, callback_display_id=3, command="shell", parameters="whoami")
    assert approved == executed
    assert approved["parameters"] == "whoami"


@pytest.mark.parametrize("key", ["command", "cmd", "shell", "arguments", "args"])
def test_every_recognized_shell_parameter_key_canonicalizes(tools, key):
    structured = _effective(tools, callback_display_id=3, command="shell", parameters={key: "whoami"})
    assert structured["parameters"] == "whoami"


def test_a_different_shell_command_still_differs(tools):
    """Canonicalization must not collapse distinct effects into one another."""
    whoami = _effective(tools, callback_display_id=3, command="shell", parameters={"command": "whoami"})
    other = _effective(tools, callback_display_id=3, command="shell", parameters="id")
    assert whoami != other


def test_a_different_callback_still_differs(tools):
    """The target is part of the effect; canonicalizing parameters must not blur it."""
    on_three = _effective(tools, callback_display_id=3, command="shell", parameters="whoami")
    on_four = _effective(tools, callback_display_id=4, command="shell", parameters="whoami")
    assert on_three != on_four


def test_non_shell_parameters_are_left_structured(tools):
    """Only shell flattens. A command with genuinely structured parameters keeps them."""
    effective = _effective(
        tools, callback_display_id=3, command="make_token", parameters={"username": "bob"}
    )
    assert effective["parameters"] == {"username": "bob"}


def test_empty_parameters_still_normalize_to_the_empty_string(tools):
    """The pre-existing empty-parameter contract is unchanged by the shell canonicalization."""
    for empty in (None, {}, "", "{}"):
        effective = _effective(tools, callback_display_id=3, command="shell", parameters=empty)
        assert effective["parameters"] == ""


def test_unrecognized_shell_parameter_shape_is_not_silently_flattened(tools):
    """A shape the canonicalizer does not recognize must survive, not become a wrong string."""
    effective = _effective(
        tools, callback_display_id=3, command="shell", parameters={"unexpected": "whoami"}
    )
    assert effective["parameters"] == {"unexpected": "whoami"}
