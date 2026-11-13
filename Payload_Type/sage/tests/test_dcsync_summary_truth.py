"""A refused DCSync reads as access denied, and the executor summary lists only tasks Mythic ran.

Mimikatz resolves the DCSync `/user` with DRSCrackNames before it calls GetNCChanges, and a failed lookup prints
`CrackNames (name status)` and stops. So when mimikatz echoes a domain-qualified account (`DOMAIN\\sam`, a DN or a
SID) and GetNCChanges then returns 8439, the name was already resolved: the DC refused the replication request.
The probe must report `replication access denied`, not `bad dn`. An unqualified echo, a CrackNames failure, a real
8453 and a successful dump keep their current readings.

The executor summary the operator and the Supervisor read must list only tasks Mythic created. A step Sage refused
before issue (here, its repeat-failure circuit breaker) is named as not issued, never as a task, and no other
capability's task id appears in it.

Fixtures run the production probe, verifier, executor, issue path, worker handback and summary renderer; only the
Mythic API seam is inert, and every value is synthetic (`lab.local`, `LAB`).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capabilities  # noqa: E402
from test_engagement_wiring import _split_issue  # noqa: E402
from test_verified_capability_input_continuity import DCSYNC, _dcsync_tools  # noqa: E402

model = importlib.import_module("ai.langgraph.model")


def _mimikatz_dcsync(user: str, result: str) -> str:
    """Mimikatz `lsadump::dcsync` output as Apollo returns it, with the account echo mimikatz prints."""
    return (
        f"mimikatz(commandline) # lsadump::dcsync /domain:lab.local /dc:dc01.lab.local /user:{user}\r\n"
        "[DC] 'lab.local' will be the domain\r\n"
        "[DC] 'dc01.lab.local' will be the DC server\r\n"
        f"[DC] '{user}' will be the user account\r\n"
        "[rpc] Service  : ldap\r\n"
        "[rpc] AuthnSvc : GSS_NEGOTIATE (9)\r\n"
        f"{result}\r\n"
    )


GETNCCHANGES_8439 = "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x000020f7 (8439)"
GETNCCHANGES_8453 = "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x00002105 (8453)"
CRACKNAMES_FAILED = "ERROR kull_m_rpc_drsr_CrackName ; CrackNames (name status): 0x00000002 (2)"
DUMPED = (
    "Object RDN           : krbtgt\r\n\r\n** SAM ACCOUNT **\r\n\r\nSAM Username         : krbtgt\r\n"
    f"Credentials:\r\n  Hash NTLM: {'d' * 32}\r\n"
)
QUALIFIED_8439 = _mimikatz_dcsync("LAB\\krbtgt", GETNCCHANGES_8439)
STANDIN_DENIED = "[+] Set object access rules\r\n[!] Failed to set object access properties..\r\n    |_ Access is denied.\r\n"
GRANT = {"capability": "grant-directory-rights", "domain": "lab.local", "callback_id": "13"}
GRANT_INPUTS = {"principal": "user1", "execution_method": "direct", "timeout": 5}


# --- the probe: a qualified GetNCChanges 8439 is a replication refusal --------------------------------------


@pytest.mark.parametrize("user", [
    "LAB\\krbtgt",
    "CN=krbtgt,CN=Users,DC=lab,DC=local",
    "S-1-5-21-1111111111-2222222222-3333333333-502",
])
def test_a_qualified_getncchanges_8439_is_replication_access_denied_not_bad_dn(user):
    probe = capabilities.extract_dcsync_secret_probe(_mimikatz_dcsync(user, GETNCCHANGES_8439))
    verification = capabilities.verify_dcsync_secret(probe)

    assert probe["replication_access_denied"] is True
    assert probe["bad_dn"] is False
    assert (verification.verdict, verification.reason) == ("blocked", "replication access denied")


@pytest.mark.parametrize("output, flags, verdict, reason", [
    # A bare name can resolve outside the DC's domain, so 8439 keeps its invalid-DN reading.
    (_mimikatz_dcsync("krbtgt", GETNCCHANGES_8439),
     {"bad_dn": True, "replication_access_denied": False}, "blocked", "bad dn"),
    # The name lookup failed, so GetNCChanges never ran: not a replication refusal.
    (_mimikatz_dcsync("LAB\\nosuchuser", CRACKNAMES_FAILED),
     {"bad_dn": False, "replication_access_denied": False}, "partial",
     "DCSync ran or connected, but no usable secret was verified"),
    (_mimikatz_dcsync("LAB\\krbtgt", GETNCCHANGES_8453),
     {"bad_dn": False, "replication_access_denied": True}, "blocked", "replication access denied"),
    (_mimikatz_dcsync("LAB\\krbtgt", DUMPED),
     {"bad_dn": False, "replication_access_denied": False}, "achieved", "DCSync secret material verified"),
], ids=["unqualified-8439", "cracknames-failure", "real-8453", "successful-dump"])
def test_near_misses_keep_their_current_reading(output, flags, verdict, reason):
    probe = capabilities.extract_dcsync_secret_probe(output)
    verification = capabilities.verify_dcsync_secret(probe)

    assert {key: probe[key] for key in flags} == flags
    assert (verification.verdict, verification.reason) == (verdict, reason)


# --- the assembled handback: what the Supervisor reads ------------------------------------------------------


def _handback_summary(run_tool) -> str:
    """Run the production worker handback (`_wrap_create_agent`) around one `execute_capability` call.

    `run_tool` is awaited inside the worker's turn and returns the tool's JSON text. The worker is scripted only to
    make that call; the ToolMessage and everything after it are production code.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    class Worker:
        async def ainvoke(self, args, config=None):
            raw = await run_tool()
            return {"messages": list(args["messages"]) + [
                AIMessage(content="Running the DCSync capability.", name="Mythic_Operator", tool_calls=[{
                    "name": "execute_capability", "args": {}, "id": "call-1", "type": "tool_call",
                }]),
                ToolMessage(content=raw, name="execute_capability", tool_call_id="call-1"),
            ]}

    m = model.Model.__new__(model.Model)
    m._autonomous_solve = False
    m._message_seq = 3
    m.state = {"_message_seq": 3}
    m.llm = None
    m.mythic_client = None
    state = {
        "_message_seq": 3,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": [HumanMessage(content="Continue toward the current objective.")],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
    }
    result = asyncio.run(m._wrap_create_agent(Worker(), "mythic_operator_messages", "Mythic_Operator")(state, {}))
    assert isinstance(result, model.Command) and result.goto == "Supervisor", result
    return result.update["supervisor_messages"][1].content


def _execute(mt, action: dict, inputs: dict, output: str, task_ids, calls: dict):
    async def run() -> str:
        with _split_issue(lambda: output, calls, display_id=lambda: next(task_ids)):
            return await mt.execute_capability(dict(action), dict(inputs))
    return run


def _task_line(summary: str) -> str:
    return next(line for line in summary.splitlines() if line.startswith("Task IDs: "))


def test_the_supervisor_reads_replication_access_denied_for_a_qualified_8439(monkeypatch, tmp_path):
    mt = _dcsync_tools(monkeypatch, tmp_path)
    calls: dict = {}

    summary = _handback_summary(_execute(mt, DCSYNC, {"timeout": 5}, QUALIFIED_8439, iter([26]), calls))

    assert [issue["command_name"] for issue in calls["issued"]] == ["dcsync"]
    assert "Executor verdict: `blocked` for `dcsync-krbtgt`." in summary, summary
    assert "Reason: replication access denied" in summary, summary
    assert _task_line(summary) == "Task IDs: 26 `dcsync`", summary
    assert "bad dn" not in summary.casefold(), summary


# --- two capabilities in one request: each summary names only its own tasks --------------------------------


def test_each_summary_names_only_its_own_task_and_a_refused_step_is_named_as_not_issued(monkeypatch, tmp_path):
    mt = _dcsync_tools(monkeypatch, tmp_path)
    task_ids = iter([26, 27, 28])

    first_calls: dict = {}
    first = _handback_summary(_execute(mt, DCSYNC, {"timeout": 5}, QUALIFIED_8439, task_ids, first_calls))
    grant_calls: dict = {}
    grant = _handback_summary(_execute(mt, GRANT, GRANT_INPUTS, STANDIN_DENIED, task_ids, grant_calls))
    refused_calls: dict = {}
    refused = _handback_summary(_execute(mt, DCSYNC, {"timeout": 5}, QUALIFIED_8439, task_ids, refused_calls))

    assert [issue["command_name"] for issue in first_calls["issued"]] == ["dcsync"]
    assert [issue["command_name"] for issue in grant_calls["issued"]] == ["execute_assembly"]
    assert refused_calls.get("issued", []) == [], "precondition: Sage's circuit breaker refuses the repeat DCSync"

    assert _task_line(first) == "Task IDs: 26 `dcsync`", first
    assert "Reason: replication access denied" in first, first
    assert _task_line(grant) == "Task IDs: 27 `execute_assembly`", grant

    assert "Executor verdict: `failed` for `dcsync-krbtgt`." in refused, refused
    assert _task_line(refused) == "Task IDs: none issued", refused
    assert "Not issued: `dcsync`" in refused, refused
    assert "27" not in refused and "26" not in refused, refused


def test_a_fresh_dcsync_after_another_capability_names_only_its_own_task(monkeypatch, tmp_path):
    mt = _dcsync_tools(monkeypatch, tmp_path)
    task_ids = iter([27, 28])

    grant_calls: dict = {}
    grant = _handback_summary(_execute(mt, GRANT, GRANT_INPUTS, STANDIN_DENIED, task_ids, grant_calls))
    dcsync_calls: dict = {}
    dcsync = _handback_summary(_execute(mt, DCSYNC, {"timeout": 5}, QUALIFIED_8439, task_ids, dcsync_calls))

    assert [issue["command_name"] for issue in grant_calls["issued"]] == ["execute_assembly"]
    assert [issue["command_name"] for issue in dcsync_calls["issued"]] == ["dcsync"]
    assert _task_line(grant) == "Task IDs: 27 `execute_assembly`", grant
    assert _task_line(dcsync) == "Task IDs: 28 `dcsync`", dcsync
    assert "Reason: replication access denied" in dcsync, dcsync
    assert "27" not in dcsync, dcsync


# --- the renderer: an unissued step never enters the Task IDs line -----------------------------------------


def test_the_renderer_keeps_an_unissued_step_out_of_task_ids_and_names_it():
    summary = model._terminal_execute_capability_report({
        "capability": "dcsync-krbtgt",
        "verdict": "failed",
        "reason": "no Mythic task was issued for `dcsync`",
        "issued": [
            {"command": "klist", "task_id": 25, "task_issued": True},
            {"command": "dcsync", "task_id": None, "task_issued": False},
        ],
        "recorded_effects": [],
    })

    assert _task_line(summary) == "Task IDs: 25 `klist`", summary
    assert "Not issued: `dcsync`" in summary, summary
