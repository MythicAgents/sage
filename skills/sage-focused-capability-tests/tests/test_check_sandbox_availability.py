"""Offline checks for the Sandbox availability live check's pass rules (no live Mythic, no provider).

Inputs, labelled where used:
- Recorded: ``fixtures/sandbox_request125_rows.json``, request 125's Mythic chat rows, reduced (see its
  ``provenance``). That run used the Docker executor, so its result names no executor and M2 must fail on it.
- Real: C1's reply is rendered by Sage's own `/sandbox` renderer from a real run of the confined local executor.
- Synthetic: mutations of those rows, labelled in each test, prove each rule rejects its falsifier.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_sandbox_availability.py"
SPEC = importlib.util.spec_from_file_location("check_sandbox_availability", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))
RECORDED = ROOT / "skills/sage-focused-capability-tests/tests/fixtures/sandbox_request125_rows.json"
GUARDED = frozenset({"sandbox_exec", "issue_task_and_waitfor_task_output", "add_credential"})


def _recorded() -> dict:
    return json.loads(RECORDED.read_text())


def _with_local_executor(rows: dict) -> dict:
    """Synthetic: the recorded sandbox_exec result with `executor: local` added, as this branch returns it."""
    rows = copy.deepcopy(rows)
    for row in rows["messages"]:
        tool_use = row["metadata"].get("tool_use")
        if tool_use and tool_use.get("tool_name") == "sandbox_exec":
            result = json.loads(tool_use["result_preview"])
            tool_use["result_preview"] = json.dumps({**result, "executor": "local"}, sort_keys=True)
    return rows


def test_constants_match_the_plan():
    assert check.fibonacci(25) == 75025
    recorded = next(row["metadata"]["tool_use"]["result_preview"] for row in _recorded()["messages"]
                    if row["metadata"].get("tool_use"))
    assert check.DIGEST in json.loads(recorded)["stdout"]  # the recorded run computed the same digest
    assert check.LIMITS == {"provider_calls": 15, "total_tokens": 150_000, "minutes": 15, "target_tasks": 0}


def test_recorded_docker_run_meets_m1_but_not_m2():
    outcome = check.supervised_outcome(_recorded(), [], GUARDED)
    assert outcome["M1"] is True
    assert outcome["M2"] is False  # recorded on the Docker executor: no `executor: local`
    assert outcome["M3"] is True
    assert outcome["closing_states_both"] is True


def test_local_executor_result_meets_m2():
    outcome = check.supervised_outcome(_with_local_executor(_recorded()), [], GUARDED)
    assert (outcome["M1"], outcome["M2"], outcome["M3"]) == (True, True, True)


@pytest.mark.parametrize("mutation", ["rejected", "shell", "other_agent"])
def test_m1_rejects_a_card_that_was_not_the_seam(mutation):
    rows = _with_local_executor(_recorded())  # synthetic mutations of the recorded card
    for row in rows["messages"]:
        card = row["metadata"].get("input_requested")
        if card:
            if mutation == "rejected":
                card["status"] = "rejected"
            elif mutation == "shell":
                card["data"]["arguments"]["language"] = "shell"
            else:
                row["metadata"]["delegation_name"] = "Mythic_Operator"
    outcome = check.supervised_outcome(rows, [], GUARDED)
    assert outcome["M1"] is False and outcome["M2"] is False


def test_m2_rejects_a_wrong_digest_and_another_delegation():
    rows = _with_local_executor(_recorded())
    wrong = copy.deepcopy(rows)  # synthetic: stdout without the digest
    for row in wrong["messages"]:
        tool_use = row["metadata"].get("tool_use")
        if tool_use:
            result = json.loads(tool_use["result_preview"])
            tool_use["result_preview"] = json.dumps({**result, "stdout": "F(25): 75025\n"})
    assert check.supervised_outcome(wrong, [], GUARDED)["M2"] is False
    elsewhere = copy.deepcopy(rows)  # synthetic: the result row belongs to another delegation
    for row in elsewhere["messages"]:
        if row["metadata"].get("tool_use"):
            row["metadata"]["delegation_id"] = "sandbox:other"
    assert check.supervised_outcome(elsewhere, [], GUARDED)["M2"] is False


def test_m3_rejects_another_guarded_tool_or_a_task():
    rows = _with_local_executor(_recorded())
    other = copy.deepcopy(rows)  # synthetic: a completed task tool row in the same request
    other["messages"].append({"id": 9999, "author_type": "ai", "message": "", "status": "complete", "tool_output": None,
                              "metadata": {"special_type": "tool_use", "tool_use": {
                                  "tool_name": "issue_task_and_waitfor_task_output", "status": "completed"}}})
    assert check.supervised_outcome(other, [], GUARDED)["M3"] is False
    assert check.supervised_outcome(rows, [{"display_id": 7}], GUARDED)["M3"] is False


def _slash_rows(code: str) -> dict:
    """Real: Sage's `/sandbox` renderer over a real confined-executor run (skipped where it cannot run)."""
    from ai.langgraph import sandbox_executor
    from sage_chat.slash import _render_sandbox_result

    if sandbox_executor.landlock_abi() < sandbox_executor.REQUIRED_LANDLOCK_ABI:
        pytest.skip("host lacks Landlock ABI 6")
    result = {**sandbox_executor.run_local("python", code, 20), "status": "ok", "executor": "local"}
    return {"messages": [{"id": 1, "author_type": "ai", "metadata": {},
                          "message": _render_sandbox_result("python", json.dumps(result))}]}


def test_c1_reads_the_rendered_reply_from_the_real_executor(tmp_path):
    code = check.SLASH_CODE.replace("/Mythic/.env.local", str(tmp_path / "stand-in.env"))
    (tmp_path / "stand-in.env").write_text("stand-in")
    outcome = check.slash_outcome(_slash_rows(code))
    assert outcome["detail"]["probes"] == check.SLASH_PROBES
    assert outcome["C1"] is True


def test_c1_rejects_an_allowed_probe_and_another_executor():
    reply = ("**Sandbox result**\n\n| Field | Value |\n|---|---|\n| Status | `ok` |\n| Language | `python` |\n"
             "| Executor | `container` |\n\n```stdout\n4\n" + json.dumps(check.SLASH_PROBES, sort_keys=True) + "\n```")
    rows = {"messages": [{"id": 1, "message": reply}]}  # synthetic reply
    assert check.slash_outcome(rows)["C1"] is False
    allowed = dict(check.SLASH_PROBES, proc="allowed")
    rows = {"messages": [{"id": 1, "message": reply.replace("`container`", "`local`").replace(
        json.dumps(check.SLASH_PROBES, sort_keys=True), json.dumps(allowed, sort_keys=True))}]}
    assert check.slash_outcome(rows)["C1"] is False


def test_d_requires_the_same_single_mount_and_no_privilege():
    before = {"mounts": [{"Destination": "/Mythic", "Source": "/srv/sage", "RW": True}], "privileged": False,
              "cap_add": [], "devices": [], "security_opt": [], "network_mode": "host"}
    assert check.docker_holds(before, copy.deepcopy(before)) is True
    socket_mount = copy.deepcopy(before)
    socket_mount["mounts"].append({"Destination": "/var/run/docker.sock", "Source": "/var/run/docker.sock", "RW": True})
    assert check.docker_holds(before, socket_mount) is False
    assert check.docker_holds(before, {**before, "privileged": True}) is False
    assert check.docker_holds({}, before) is False
