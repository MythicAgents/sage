"""Approval/effect parity for the configured omitted Mythic task timeout."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph import mythic_tools as module  # noqa: E402
from ai.langgraph.mythic_tools import MythicTools  # noqa: E402
from ai.langgraph.request_contract import action_spec_from_tool_call, build_request_contract  # noqa: E402
from ai.langgraph.turn_authority import authority_from_request_contract  # noqa: E402
from sage_chat.hitl import (  # noqa: E402
    approval_action_digest,
    approval_action_fingerprint,
    approval_proposal_digest,
    approval_selection_digest,
)


TOOL = "issue_task_and_waitfor_task_output"
_OMITTED = object()


def _args(
    *,
    command: str = "sleep",
    parameters=None,
    callback_display_id: int = 1,
    timeout=None,
):
    if parameters is None:
        parameters = {"interval": 120}
    values = {
        "command": command,
        "parameters": parameters,
        "callback_display_id": callback_display_id,
    }
    if timeout is not _OMITTED:
        values["timeout"] = timeout
    return values


def _bare_tools() -> MythicTools:
    tools = object.__new__(MythicTools)
    tools._turn_authority = None
    tools._active_approval_claim = None
    tools._request_contract = None
    return tools


def _install_exact_claim(tools: MythicTools, approved_args: dict) -> None:
    action = {"name": TOOL, "args": approved_args}
    contract = build_request_contract(
        request_id="request-timeout-parity",
        channel_id="channel-timeout-parity",
        operation_id="operation-timeout-parity",
        mode="supervised",
        autonomous_solve=False,
        requested_actions=(action_spec_from_tool_call(action),),
    )
    tools.set_request_contract(contract)
    tools.set_turn_authority(authority_from_request_contract(contract))
    digest = approval_action_digest([action])
    action_id = approval_action_fingerprint(action)
    tools.set_approval_claim({
        "approval_id": "approval-timeout-parity",
        "request_id": contract.request_id,
        "request_contract_digest": contract.digest,
        "tool_name": TOOL,
        "selection_mode": "single",
        "actions": [action],
        "approved_actions": [action],
        "approved_action_ids": [action_id],
        "action_digest": digest,
        "proposal_digest": approval_proposal_digest(contract.digest, digest),
        "selection_digest": approval_selection_digest(contract.digest, digest, [action_id]),
    })


def _effective(tools: MythicTools, arguments: dict) -> dict:
    return tools._effective_request_action_arguments(TOOL, arguments)


@pytest.mark.parametrize("configured_timeout", [20, 45, 300])
def test_omitted_and_runtime_derived_timeout_follow_the_same_configured_default(
    monkeypatch: pytest.MonkeyPatch,
    configured_timeout: int,
):
    tools = _bare_tools()
    monkeypatch.setattr(module, "SAGE_MYTHIC_TASK_TIMEOUT", configured_timeout)

    approved = _args(timeout=_OMITTED)
    executed = _args(timeout=configured_timeout)

    assert _effective(tools, approved) == _effective(tools, executed)
    assert _effective(tools, approved)["timeout"] == configured_timeout


@pytest.mark.parametrize("configured_timeout", [20, 45, 300])
def test_two_check_issue_blocker_accepts_exact_omitted_then_derived_action(
    monkeypatch: pytest.MonkeyPatch,
    configured_timeout: int,
):
    tools = _bare_tools()
    monkeypatch.setattr(module, "SAGE_MYTHIC_TASK_TIMEOUT", configured_timeout)
    approved = _args(timeout=_OMITTED)
    _install_exact_claim(tools, approved)

    assert tools._turn_authority_issue_blocker(**approved) == ""
    assert tools._turn_authority_issue_blocker(
        **_args(timeout=configured_timeout),
        recheck=True,
    ) == ""


@pytest.mark.parametrize("approved_timeout,executed_timeout", [(20, 20), (45, 45), (300, 300)])
def test_explicit_identical_timeout_remains_covered(
    approved_timeout: int,
    executed_timeout: int,
):
    tools = _bare_tools()
    approved = _args(timeout=approved_timeout)
    _install_exact_claim(tools, approved)

    assert tools._turn_authority_issue_blocker(
        **_args(timeout=executed_timeout),
        recheck=True,
    ) == ""


def test_resolver_added_sleep_jitter_default_stays_covered_when_interval_is_unchanged():
    tools = _bare_tools()
    approved_parameters = {"interval": 120}
    resolved_parameters = {"interval": 120, "jitter": 0}
    tools._last_argres_binding = {
        "command": "sleep",
        "callback_display_id": 1,
        "original": approved_parameters,
        "resolved": resolved_parameters,
    }
    approved = _args(parameters=approved_parameters, timeout=20)
    _install_exact_claim(tools, approved)

    assert tools._turn_authority_issue_blocker(
        **_args(parameters=resolved_parameters, timeout=20),
        recheck=True,
    ) == ""


@pytest.mark.parametrize(
    "mutated",
    [
        _args(timeout=21),
        _args(parameters={"interval": 121}, timeout=20),
        _args(callback_display_id=2, timeout=20),
        _args(command="shell", parameters="whoami", timeout=20),
    ],
)
def test_timeout_or_other_effect_change_remains_denied(mutated: dict):
    tools = _bare_tools()
    approved = _args(timeout=20)
    _install_exact_claim(tools, approved)

    assert "approved proposal does not cover this exact effect" in (
        tools._turn_authority_issue_blocker(**mutated, recheck=True)
    )


@pytest.mark.parametrize("claim", [None, {}, {"request_id": "stale-request"}])
def test_missing_or_stale_claim_remains_denied(claim):
    tools = _bare_tools()
    approved = _args(timeout=20)
    _install_exact_claim(tools, approved)
    tools._active_approval_claim = claim

    assert "lacks an exact active approval claim" in tools._turn_authority_issue_blocker(**approved)
