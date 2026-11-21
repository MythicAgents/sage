"""Offline checks for the BloodHound status live-check tool's local rules (no Mythic, no provider, no Docker)."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_bloodhound_quiet_status.py"
SPEC = importlib.util.spec_from_file_location("check_bloodhound_quiet_status", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

C_ID = 61


def _msg(text: str, key: str = "assistant:1", sender: str = "Sage") -> dict:
    return {"message": text, "chat_response_key": key, "sender_display_name": sender}


def _req(*messages: dict, status: str = "complete") -> dict:
    return {"status": status, "messages": list(messages)}


def _sent(channel: str, chip: str, tooltip: str = "") -> dict:
    return {"channel": channel, "chip": {"value": chip, "tooltip": tooltip}, "tasks_after": 5}


NOT_CONFIGURED_GUARD = _msg("BloodHound is not configured for this chat, so I did not read any graph data for this "
                            "request.", "subagent_final:bloodhound:1", "BloodHound")
REFUSED_GUARD = _msg("BloodHound is connected, but BloodHound CE did not answer a read just now, so I did not read any "
                     "graph data for this request.\n\nNothing accepted a connection at that address.",
                     "subagent_final:bloodhound:2", "BloodHound")
# Live attempt 1 (2026-11-21), channel B, recorded in its run file: turn 1 named the failed start, turns 2 and 3 read
# the failed-credential skip, which named no failure, and so did the guard row.
FAILED_TOOLTIP = "BloodHound: Failed to connect to BloodHound MCP: McpError: Connection closed Click to run /bloodhound."
SKIP_TOOLTIP = ("BloodHound: BloodHound MCP connect skipped: this exact credential set already failed and nothing about "
                "it has changed. Update the configuration to trigger a fresh attempt. Click to run /bloodhound.")
SKIP = ("BloodHound MCP connect skipped: this exact credential set already failed and nothing about it has changed. "
        "Update the configuration to trigger a fresh attempt.")
ATTEMPT_1_GUARD = _msg(f"BloodHound is configured but not connected, so I did not read any graph data for this "
                       f"request.\n\n{SKIP}", "subagent_final:bloodhound:3", "BloodHound")
# The same turns once the skip names its last failure (synthetic: the fixed outcome's text).
LAST = "Last failure: Failed to connect to BloodHound MCP: McpError: Connection closed."
NAMED_TOOLTIP = f"BloodHound: {SKIP} {LAST} Click to run /bloodhound."
CONNECT_FAILED_GUARD = _msg(f"BloodHound is configured but not connected, so I did not read any graph data for this "
                            f"request.\n\n{SKIP} {LAST}", "subagent_final:bloodhound:3", "BloodHound")
LOST = _msg("BloodHound is no longer available in this chat.", f"bloodhound_lost:{C_ID}")


def _passing():
    run = {"task_baseline": 5, "channels": {"A": 59, "B": 60, "C": C_ID, "E": 62}, "steps": {
        "L1": {"requests": [_sent("A", "not_configured")] * 3},
        "L2": {"requests": [_sent("A", "not_configured")]},
        "L3": {"requests": [_sent("B", "unavailable", FAILED_TOOLTIP)] + [_sent("B", "unavailable", NAMED_TOOLTIP)] * 2},
        "L4": {"requests": [_sent("C", "unavailable"), _sent("C", "connected")]},
        "L5": {"requests": [_sent("C", "unavailable"), _sent("C", "unavailable"), _sent("E", "unavailable"),
                            _sent("C", "unavailable")]},
        "L6": {"requests": [_sent("C", "connected")]},
    }}
    rows = {
        "L1": [_req(_msg("4"))] * 3,
        "L2": [_req(NOT_CONFIGURED_GUARD, _msg("BloodHound is not set up yet."))],
        "L3": [_req(_msg("4")), _req(_msg("4")), _req(CONNECT_FAILED_GUARD, _msg("It could not be reached."))],
        "L4": [_req(_msg("**Connection:** Reconnected to BloodHound MCP (13 tools), verified by a live domain read."
                         "\n\n**Ingest:** Ready. The connected BloodHound MCP answered.")), _req(_msg("4"))],
        "L5": [_req(LOST, _msg("4")), _req(_msg("4")), _req(_msg("4")), _req(REFUSED_GUARD, _msg("Down."))],
        "L6": [_req(_msg("4"))],
    }
    tools = {"L1": [[]] * 3, "L2": [["transfer_to_BloodHound"]], "L3": [[], [], ["transfer_to_BloodHound"]],
             "L4": [[], []], "L5": [[], [], [], ["transfer_to_BloodHound"]], "L6": [[]]}
    return run, rows, tools


def test_the_rules_pass_on_the_expected_shape():
    criteria = check.criteria_for(*_passing())
    assert criteria == {key: True for key in criteria}, criteria


@pytest.mark.parametrize("mutate, failing", [
    (lambda run, rows, tools: rows.__setitem__(
        "L1", [_req(_msg("4")), _req(_msg("notice", "bloodhound_degraded:59")), _req(_msg("4"))]), "L1"),
    (lambda run, rows, tools: tools.__setitem__("L2", [["respond_to_user"]]), "L2"),
    (lambda run, rows, tools: tools.__setitem__("L2", [["transfer_to_BloodHound", "domain_info"]]), "L2"),
    (lambda run, rows, tools: rows.__setitem__("L3", rows["L3"][:2] + [_req(NOT_CONFIGURED_GUARD)]), "L3"),
    (lambda run, rows, tools: run["steps"]["L3"].__setitem__("requests", [_sent("B", "unavailable", FAILED_TOOLTIP),
                                                                          _sent("B", "degraded", NAMED_TOOLTIP),
                                                                          _sent("B", "unavailable", NAMED_TOOLTIP)]),
     "L3"),
    # Attempt 1's recorded shapes: the skip tooltip on turns 2 and 3, and its guard row.
    (lambda run, rows, tools: run["steps"]["L3"].__setitem__("requests", [_sent("B", "unavailable", FAILED_TOOLTIP)]
                                                             + [_sent("B", "unavailable", SKIP_TOOLTIP)] * 2), "L3"),
    (lambda run, rows, tools: rows["L3"].__setitem__(2, _req(ATTEMPT_1_GUARD, _msg("It could not be reached."))),
     "L3"),
    # F1's shape, which the deployed server cannot produce from a configured URL, no longer passes L3.
    (lambda run, rows, tools: rows["L3"].__setitem__(2, _req(REFUSED_GUARD, _msg("It could not be reached."))), "L3"),
    (lambda run, rows, tools: rows["L4"].__setitem__(0, _req(_msg("**Ingest:** NOT ready"))), "L4"),
    (lambda run, rows, tools: rows["L5"].__setitem__(1, _req(LOST)), "L5"),
    (lambda run, rows, tools: rows["L5"].__setitem__(2, _req(_msg("x", "bloodhound_lost:62"))), "L5"),
    (lambda run, rows, tools: rows["L5"].__setitem__(0, _req(_msg("4"))), "L5"),
    (lambda run, rows, tools: rows.__setitem__("L6", [_req(_msg("4"), status="streaming")]), "terminal"),
    (lambda run, rows, tools: run["steps"]["L6"]["requests"][0].__setitem__("tasks_after", 6), "tasks_and_ceilings"),
])
def test_each_rule_can_fail(mutate, failing):
    run, rows, tools = copy.deepcopy(_passing())
    mutate(run, rows, tools)
    assert check.criteria_for(run, rows, tools)[failing] is False


def test_only_the_two_token_values_are_read_from_the_mcp_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BLOODHOUND_DOMAIN=localhost\nBLOODHOUND_TOKEN_ID=id-value\nexport BLOODHOUND_TOKEN_KEY='key-value'\n"
                   "OTHER=ignored\nBLOODHOUND_PORT=\n")
    assert check.read_mcp_tokens(env) == {"BLOODHOUND_TOKEN_ID": "id-value", "BLOODHOUND_TOKEN_KEY": "key-value"}


def test_the_chip_is_read_from_stored_channel_metadata():
    metadata = {"channel_metadata": {"items": [{"key": "cfg_model", "value": "x"},
                                               {"key": "bloodhound", "value": "unavailable", "color": "warning",
                                                "tooltip": "BloodHound: CE did not answer."}]}}
    assert check.bloodhound_chip(metadata) == {"value": "unavailable", "color": "warning",
                                               "tooltip": "BloodHound: CE did not answer."}
    assert check.bloodhound_chip({}) == {"value": None, "color": None, "tooltip": None}


def test_lane_channels_use_the_lane_model_and_only_given_bloodhound_settings():
    route = {"API_ENDPOINT": "https://api.openai.com/v1", "model": "gpt-6-cyber", "model_b": "gpt-5.6-sol",
             "SAGE_OPENAI_SAFETY_IDENTIFIER": "user_a17f"}
    plain = check.channel_config(route, lane=True)["config"]
    assert plain["model"] == "gpt-5.6-sol" and plain["SAGE_OPENAI_ACCESS_PROGRAM"] == "standard"
    assert not any(key.startswith("BLOODHOUND_") for key in plain)
    first = check.channel_config(route, lane=False)["config"]
    assert first["model"] == "gpt-6-cyber" and first["SAGE_OPENAI_ACCESS_PROGRAM"] == "daybreak_red"
