"""A capability plan that still needs an input no step supplies is refused before its first Mythic task.

`execute_capability` builds the capability's command plan before it issues anything. When a command it would issue
still holds an input placeholder that no earlier step fills at run time (here the service-proof target
`{{kerberos_service_resource}}`, left unfilled when the request names no `proof_host`/`proof_resource` and
BloodHound knows no domain controller for the domain), the executor issues nothing and its report names the missing
input and what supplies it. `{{kerberos_ticket_base64}}`, which the forge's own output fills, is never such an input.
A step that can reach `achieved` before the unfilled placeholder (the current-context preflight with a target only
it reads, `proof_unc`) keeps the request from being refused early.

Fixtures run the production executor, binder, builder, adapter, proved-key ledger, worker handback, supervised card
flow and autonomous executor node; only Mythic's issue/wait seam is inert, and every domain, SID and key is
synthetic (`lab.local`, `north.lab.local`).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_dcsync_summary_truth import _handback_summary  # noqa: E402
from test_engagement_wiring import _split_issue  # noqa: E402
from test_verified_capability_input_continuity import (  # noqa: E402
    DCSYNC_OK, KRBTGT_KEY, _krbtgt_tools, _prove_krbtgt,
)

model = importlib.import_module("ai.langgraph.model")

CHILD, PARENT = "north.lab.local", "lab.local"
ENSURE = {"capability": "ensure-kerberos-context", "callback_id": "13"}
CASES = {
    "same-domain": {"domain": PARENT, "source_domain": PARENT, "target_domain": PARENT},
    "child-to-parent": {"domain": PARENT, "source_domain": CHILD, "target_domain": PARENT},
}
# What the executor issues for the same-domain context once the proof target resolves (the readiness probe's
# control): preflight inventory and service proof, the forge, the logon context, the ticket import and list, and
# the post-import service proof.
CONTROL_SEQUENCE = ["shell", "shell", "execute_assembly", "make_token", "ticket_store_add", "ticket_store_list",
                    "shell"]


def _client(monkeypatch, tmp_path, case: str):
    """A client whose krbtgt for the case's source domain was proved by the production DCSync executor."""
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    source = CASES[case]["source_domain"]
    if source == PARENT:
        _prove_krbtgt(mt, store)
    else:
        sids = {CHILD: "S-1-5-21-1-2-3", PARENT: "S-1-5-21-9-9-9"}
        mt._resolve_domain_sid = lambda domain: asyncio.sleep(0, result=sids.get(str(domain).casefold(), ""))
        output = DCSYNC_OK.replace("'lab.local'", f"'{CHILD}'")
        store["task_outputs"][24] = output
        with _split_issue(lambda: output, {}, display_id=lambda: 24):
            proved = json.loads(asyncio.run(mt.execute_capability(
                {"capability": "dcsync-krbtgt", "domain": CHILD, "callback_id": "13"}, {"timeout": 5})))
        assert f"krbtgt-hash:{CHILD}" in proved.get("recorded_effects", []), proved
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="")  # BloodHound knows no DC
    return mt, store


def _run(mt, inputs: dict) -> tuple[str, dict]:
    calls: dict = {}
    with _split_issue(lambda: "ok", calls, display_id=lambda: 30 + len(calls.get("issued", []))):
        raw = asyncio.run(mt.execute_capability(dict(ENSURE), {**inputs, "timeout": 5}))
    return raw, calls


def _issued(calls: dict) -> list[str]:
    return [issue["command_name"] for issue in calls.get("issued", [])]


def _assert_refused(result: dict, domain: str) -> None:
    assert result["verdict"] == "failed" and result["issued"] == [], result
    assert result["missing"] == ["kerberos_service_resource"], result
    assert result["recorded_effects"] == [] and result["stopped_after"] == "unresolved_inputs", result
    reason = result["reason"]
    assert "kerberos_service_resource" in reason and domain in reason, reason
    assert "proof_host" in reason and "proof_resource" in reason, reason
    assert reason.endswith("No task was issued."), reason


# --- the refusal ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(CASES))
def test_an_unresolvable_proof_target_issues_no_task_and_names_the_input(monkeypatch, tmp_path, case):
    mt, store = _client(monkeypatch, tmp_path, case)
    writes_before = list(store["writes"])

    raw, calls = _run(mt, CASES[case])

    assert _issued(calls) == [], _issued(calls)
    _assert_refused(json.loads(raw), PARENT)
    assert store["writes"] == writes_before
    assert KRBTGT_KEY not in raw


def test_the_refusal_reaches_the_supervisor_as_a_report_naming_the_input(monkeypatch, tmp_path):
    mt, _ = _client(monkeypatch, tmp_path, "same-domain")
    calls: dict = {}

    async def run_tool():
        with _split_issue(lambda: "ok", calls, display_id=lambda: 30 + len(calls.get("issued", []))):
            return await mt.execute_capability(dict(ENSURE), {**CASES["same-domain"], "timeout": 5})

    summary = _handback_summary(run_tool)

    assert _issued(calls) == []
    assert "Executor verdict: `failed` for `ensure-kerberos-context`." in summary, summary
    assert "kerberos_service_resource" in summary and "proof_host" in summary, summary
    assert "Task IDs: none issued" in summary.splitlines(), summary


# --- controls: a resolvable or run-time-filled input is not refused ---------------------------------------------------


@pytest.mark.parametrize("source", ["proof_host", "bloodhound"])
def test_a_resolvable_proof_target_issues_the_unchanged_sequence(monkeypatch, tmp_path, source):
    mt, _ = _client(monkeypatch, tmp_path, "same-domain")
    inputs = dict(CASES["same-domain"])
    if source == "proof_host":
        inputs["proof_host"] = "dc01.lab.local"
    else:
        mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.lab.local")

    raw, calls = _run(mt, inputs)

    assert _issued(calls) == CONTROL_SEQUENCE, _issued(calls)
    assert json.loads(raw).get("stopped_after") != "unresolved_inputs"


def test_a_ticket_filled_by_the_forge_is_not_an_unresolved_input(monkeypatch, tmp_path):
    mt, _ = _client(monkeypatch, tmp_path, "same-domain")
    inputs = {**CASES["same-domain"], "proof_host": "dc01.lab.local"}
    plan = json.loads(asyncio.run(mt.build_capability_commands(dict(ENSURE), dict(inputs))))
    placeholders = {name for command in plan["commands"]
                    for name in mt._capability_executor_placeholders(command.get("parameters"))}
    assert placeholders == {"kerberos_ticket_base64"}, placeholders

    raw, calls = _run(mt, inputs)

    assert "execute_assembly" in _issued(calls) and json.loads(raw).get("stopped_after") != "unresolved_inputs"


def test_a_preflight_that_can_prove_access_first_is_not_refused(monkeypatch, tmp_path):
    # `proof_unc` reaches only the current-context preflight, whose service proof can achieve before the plan's
    # unfilled post-import proof is reached; the request runs as before and the backstop still guards the plan.
    mt, _ = _client(monkeypatch, tmp_path, "same-domain")

    raw, calls = _run(mt, {**CASES["same-domain"], "proof_unc": "\\\\dc01.lab.local\\C$"})

    assert _issued(calls)[:2] == ["shell", "shell"], _issued(calls)
    assert json.loads(raw).get("stopped_after") != "unresolved_inputs", raw


# --- lanes ---------------------------------------------------------------------------------------------------------


def _supervised(monkeypatch, tmp_path, *, mode: str = "supervised", decision: str = "accept"):
    from test_hitl_steer_current_intent import _call
    from test_step_bounds import _proved_krbtgt_client, _rescope_run

    mt, split_issue = _proved_krbtgt_client(monkeypatch, tmp_path)
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="")
    brief = {"handoff_title": "establish the Kerberos context", "allowed_capabilities": ["ensure-kerberos-context"],
             "handoff_instruction": "Establish a Kerberos context for lab.local on callback 13 and verify it.",
             "callback_display_id": 13, "targets": [PARENT], "expected_result": "A verified Kerberos context."}
    call = {"action": {**ENSURE, "domain": PARENT}, "inputs": {"source_domain": PARENT, "target_domain": PARENT}}
    script = [_call("transfer_to_Mythic_Operator", brief, 1), _call("execute_capability", call, 2)]
    script += [AIMessage(content="Final answer.")] * 4
    return asyncio.run(_rescope_run(mt, split_issue, script, mode=mode, decision=decision))


def test_supervised_accept_issues_nothing_and_reports_the_missing_input(monkeypatch, tmp_path):
    run = _supervised(monkeypatch, tmp_path)

    assert run.cards and run.issued_before_decision == []
    assert run.issued == [], [issue["command_name"] for issue in run.issued]
    emitted = json.dumps(run.emissions, default=str)
    assert "kerberos_service_resource" in emitted and "none issued" in emitted


def test_supervised_reject_issues_nothing(monkeypatch, tmp_path):
    run = _supervised(monkeypatch, tmp_path, decision="reject")

    assert run.cards and run.issued == []


def test_conversation_still_denies(monkeypatch, tmp_path):
    run = _supervised(monkeypatch, tmp_path, mode="conversation")

    assert run.cards == [] and run.issued == []


def test_the_autonomous_executor_entry_gets_the_same_refusal(monkeypatch, tmp_path):
    mt, _ = _client(monkeypatch, tmp_path, "same-domain")
    m = model.Model.__new__(model.Model)
    m._autonomous_solve = True
    m.mythic_client = mt
    m._message_seq = 0
    m.state = {"_message_seq": 0}
    action = {"name": "ensure-kerberos-context", "target": f"domain={PARENT}", "effects": [],
              "intent": {"capability": "ensure-kerberos-context", "domain": PARENT}}
    inputs = {"callback_id": "13", "source_domain": PARENT, "target_domain": PARENT, "timeout": 5}
    instruction = ("AUTONOMOUS STEP DRIVER: test\n"
                   f"`action={json.dumps(action, sort_keys=True)}`\n`inputs={json.dumps(inputs, sort_keys=True)}`")
    state = {"messages": [], "supervisor_messages": [], "mythic_operator_messages": [], "bloodhound_messages": [],
             "autonomous_executor_messages": [HumanMessage(content=instruction)]}
    calls: dict = {}

    with _split_issue(lambda: "ok", calls, display_id=lambda: 30 + len(calls.get("issued", []))):
        command = asyncio.run(m._autonomous_executor_node(state))

    assert _issued(calls) == []
    _assert_refused(json.loads(command.update["messages"][0].content), PARENT)
    report = command.update["supervisor_messages"][0].content
    assert "kerberos_service_resource" in report and "Task IDs: none issued" in report, report
