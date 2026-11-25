"""A completed collector that also printed diagnostics is retrieved; every collection exit carries a verdict.

Live shape (supervised GOAD run, 2026-11-13): the SharpHound task completed in Mythic and its output ended with
the canonical completion record, but earlier LDAP/HTTP diagnostic errors made the generic classifier return
`transient`. The collection stopped before listing, returned `task_failed` without a verdict, and the worker's
terminal consumer ignored it. These tests run the real approved client, alias resolution, issuer, classifier,
receipt, private transaction, collector, ingest verifier and persisted ledger. Only external Mythic and BloodHound
observations and effects are replaced (the fixtures of `test_private_collection_transaction.py`); sockets are
blocked. The output below is synthetic, with the recorded shape.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphInterrupt

from ai.langgraph import command_builder, model, mythic_tools as mt
from ai.langgraph.request_contract import action_spec_from_tool_call, build_request_contract
from ai.langgraph.turn_authority import authority_from_request_contract, compile_turn_authority
from tests.test_collection_capability import (
    ACTION, APPROVED_STEPS, _approve, _graph_hops, _real_recorder, _reloaded, _run, _streamed,
)
from tests.test_private_collection_transaction import (  # noqa: F401  (fixture)
    _collect, _install_apollo_alias_schema, collection,
)
from tests.test_request_contract import _approval_claim_for
from tests.test_turn_authority import (
    _install_collection_issue_test_path, _load_turn_authority, _resolve_collection_authority,
)

# --- synthetic collector output (fixture) ------------------------------------------------------------------
_STAMP = "2026-10-03T12:00:0{}.1000000-05:00"
COMPLETION_TEXT = "SharpHound Enumeration Completed at 12:00 PM on 10/3/2026! Happy Graphing!"
COMPLETION = f"{_STAMP.format(5)}|INFORMATION|{COMPLETION_TEXT}"
_HEAD = [
    f"{_STAMP.format(1)}|INFORMATION|This version of SharpHound is compatible with the 5.0.0 Release of BloodHound",
    f"{_STAMP.format(1)}|INFORMATION|Resolved Collection Methods: Group, LocalAdmin, Session, Trusts, ACL",
    f"{_STAMP.format(2)}|INFORMATION|Loaded cache with stats: 3 ID to type mappings.",
    " 2 name to SID mappings.",
    "",
    f"{_STAMP.format(3)}|ERROR|Unhandled LDAP InvalidCred error code during LDAP test: "
    "LdapNativeException: SASL bind failed. LDAP error code: 49.",
    "   at Example.Ntlm.LdapConnection.SaslBind(String distinguishedName, String mechanism)",
    "--- End of stack trace from previous location where exception was thrown ---",
    "   at Example.Processors.DCLdapProcessor.Authenticate(), Server error: 8009030C",
    f"{_STAMP.format(3)}|ERROR|Unauthorized exception checking NTLM accessibility for URL: "
    "http://ca.partner.test/certsrv/. Exception: 401 Unauthorized",
    f"{_STAMP.format(3)}|ERROR|HttpRequestException occurred checking NTLM accessibility for URL: "
    "http://ca.partner.test/service.svc. Exception: Response status code does not indicate success: "
    "404 (Not Found).",
    "Closing writers",
    f"{_STAMP.format(4)}|INFORMATION|Status: 371 objects finished (+371 92.75)/s -- Using 122 MB RAM",
    f"{_STAMP.format(4)}|INFORMATION|Enumeration finished in 00:00:04.6932663",
]
DIAGNOSTIC = "\n".join([*_HEAD, COMPLETION, ""])
OPSEC = (
    "[SAGE OPSEC] footprint total=3 axes={'disk_artifact': 1, 'new_beacon': 0, 'new_process': 1, "
    "'flagged_tool': 1, 'lateral_hop': 0, 'network_signature': 0, 'reversibility': 0}. This action was recorded "
    "to the artifact ledger \u2014 clean it up at sub-goal completion (list_open_artifacts)."
)
RECORD_FAULTS = {
    "absent": _HEAD,
    "echoed": [*_HEAD, f"{_STAMP.format(4)}|ERROR|Echo: {COMPLETION_TEXT}", COMPLETION],
    "malformed_time": [*_HEAD, COMPLETION.replace("12:00 PM", "1:00 PM")],
    "malformed_level": [*_HEAD, COMPLETION.replace("|INFORMATION|", "|WARNING|")],
    "duplicate": [*_HEAD, COMPLETION, COMPLETION],
    "not_final": [*_HEAD, COMPLETION, f"{_STAMP.format(6)}|INFORMATION|Saving cache with stats"],
}
RUNNER_STEPS = APPROVED_STEPS[:3]
NO_FILES = json.dumps({"files": []})
# --- end fixture ------------------------------------------------------------------------------------------


def _mythic(case, monkeypatch, *, runner=DIAGNOSTIC, rows=None, outputs=None, aliases=True):
    """Serve each task's Mythic row (`rows` overrides one command's row) and output (`outputs`, or `runner`)."""
    if aliases:
        _install_apollo_alias_schema(case, monkeypatch)
    monkeypatch.setattr(case.client, "_supervised_task_state",
                        mt.MythicTools._supervised_task_state.__get__(case.client))
    original = mt.mythic.waitfor_for_task_output

    async def task_query(_client, _query, *, variables):
        command = case.tasks[variables["id"] - 1]["command_name"]
        row = {"display_id": variables["id"], "command_name": command, "callback": {"display_id": 7},
               "completed": True, "status": "completed"}
        return {"task": [{**row, **(rows or {}).get(command, {})}]}

    async def output(*, task_display_id, **kwargs):
        command = case.tasks[task_display_id - 1]["command_name"]
        value = (outputs or {}).get(command, runner if command == "execute_assembly" else None)
        value = value(task_display_id) if callable(value) else value
        if isinstance(value, BaseException):
            raise value
        return value if value is not None else await original(task_display_id=task_display_id, **kwargs)

    monkeypatch.setattr(mt.mythic, "execute_custom_query", task_query)
    monkeypatch.setattr(mt.mythic, "waitfor_for_task_output", output)


def _commands(case):
    return [task["command_name"] for task in case.tasks]


def _report(result):
    payload = model._terminal_execute_capability_payload(
        [ToolMessage(content=json.dumps(result), name="execute_capability", tool_call_id="call-collect")]
    )
    assert payload is not None, result
    return model._terminal_execute_capability_report(payload)


def _assert_terminal(case, result, verdict, phase, commands):
    """One normal return: its verdict and phase, exactly the tasks issued, and no invented effect."""
    assert (result["verdict"], result["phase"]) == (verdict, phase), result
    assert _commands(case) == commands
    assert [(item["task_id"], item["command"]) for item in result["issued"]] == list(enumerate(commands, 1))
    report = _report(result)
    assert f"Executor verdict: `{verdict}`" in report and f"Phase: {phase}" in report, report
    task_line = next(line for line in report.splitlines() if line.startswith("Task IDs: "))
    for number, command in list(enumerate(commands, 1))[:8]:
        assert f"{number} `{command}`" in task_line, report
    if verdict != "achieved":
        assert result.get("recorded_effects", []) == [] and _graph_hops(case.client) == []
    return report


@pytest.mark.parametrize("runner", [DIAGNOSTIC, "Completed successfully"], ids=["diagnostic", "strict"])
@pytest.mark.parametrize("scope", ["corp.test", "partner.test"], ids=["forest", "cross-domain"])
def test_a_completed_collector_is_retrieved_ingested_and_verified_once(collection, monkeypatch, tmp_path,
                                                                        runner, scope):
    case = collection
    _real_recorder(case, monkeypatch, tmp_path)
    streamed = _streamed(case)
    _mythic(case, monkeypatch, runner=runner)
    monkeypatch.setattr(mt, "_bloodhound_collected_domains", AsyncMock(return_value=[scope]))
    case.ingest_tool.statuses = [(2, "Complete")]
    action = {**ACTION, "domain": scope}
    _approve(case, action)

    result = _run(case, action)

    assert (result["ok"], result["status"], result["graph_verified"]) == (True, "ingested", True), result
    report = _assert_terminal(case, result, "achieved", "ingest", APPROVED_STEPS)
    assert case.ingest_tool.uploads == 1 and [e for e in case.events if e[0] == "ingest"] == [
        ("ingest", "upload_bytes"), ("ingest", "status")]
    assert len(result["recorded_effects"]) == 1 and "graph-built:" in result["recorded_effects"][0]
    fresh, _state = _reloaded(case)
    hops = _graph_hops(fresh)
    assert len(hops) == 1 and hops[0].status == "achieved" and hops[0].proof_envelope["task_id"] == "5"
    generic = command_builder.classify_result("execute_assembly", runner, parameters=case.tasks[2]["parameters"])
    if runner == DIAGNOSTIC:
        # Generic classification is unchanged; the diagnostics stay attached to the collector task.
        assert generic == "transient"
        assert result["collector_diagnostics"] == {"task_id": 3, "output_class": "transient"}
        assert "Collector task 3 reported diagnostic errors" in report
        assert any("diagnostic errors" in message and "`3`" in message for message in streamed)
    else:
        assert generic == "success" and "collector_diagnostics" not in result
    assert streamed[-1].startswith("**Collection verified**")


@pytest.mark.parametrize("fault", [*RECORD_FAULTS, "failed_row", "nonterminal_row", "other_callback_row",
                                   "unbound_selector"])
def test_only_a_proved_bound_runner_with_one_final_completion_record_unlocks_retrieval(collection, monkeypatch,
                                                                                      fault):
    case = collection
    rows = {"failed_row": {"status": "error"}, "nonterminal_row": {"completed": False, "status": "processing"},
            "other_callback_row": {"callback": {"display_id": 8}}}
    _mythic(case, monkeypatch, runner="\n".join(RECORD_FAULTS.get(fault, DIAGNOSTIC.splitlines())),
            rows={"execute_assembly": rows[fault]} if fault in rows else None, aliases=fault != "unbound_selector")
    _approve(case, ACTION)

    result = _run(case, ACTION)

    _assert_terminal(case, result, "failed", "runner", RUNNER_STEPS)
    assert result["status"] in {"task_failed", "task_nonterminal"} and "collector_diagnostics" not in result
    assert not any(event[0] == "ingest" for event in case.events) and case.ingest_tool.uploads == 0


def test_the_eligibility_reads_only_the_bound_assembly_and_the_raw_final_record():
    params = {"Assembly": "SharpHound.exe", "Arguments": "--ZipFilename bloodhound_x.zip"}
    eligible = command_builder.sharphound_retrieval_eligible
    assert eligible("execute_assembly", DIAGNOSTIC, params)
    assert eligible("execute_assembly", f"{DIAGNOSTIC}\n{OPSEC}\n", params)
    assert not eligible("execute_assembly", f"{DIAGNOSTIC}\n{OPSEC}\n{OPSEC}", params)
    assert not eligible("execute_assembly", DIAGNOSTIC, {**params, "Assembly": "Other.exe"})
    assert not eligible("execute_assembly", DIAGNOSTIC, {**params, "assembly_name": "SharpHound.exe"})
    assert not eligible("shell", DIAGNOSTIC, params)
    for lines in RECORD_FAULTS.values():
        assert not eligible("execute_assembly", "\n".join(lines), params)


def _artifact_fault(case, monkeypatch, fault):
    if fault == "stale_zip":  # an unchanged listing holding another run's ZIP: all four listings run
        return {"ls": json.dumps({"files": [{"name": "20261003120000_bloodhound_0000000000000000.zip",
                                             "full_name": "C:\\Users\\Public\\20261003120000_bloodhound_0000000000000000.zip",
                                             "is_file": True}]})}
    if fault == "no_zip":  # a changing listing that never shows this run's ZIP: the four-listing bound holds
        return {"ls": lambda task_id: json.dumps({"files": [{"name": f"report{task_id}.txt", "is_file": True}]})}
    if fault == "empty_dir":  # an unchanged empty listing
        return {"ls": NO_FILES}
    original = case.client._latest_download_for_callback
    if fault == "no_filemeta":
        monkeypatch.setattr(case.client, "_latest_download_for_callback", AsyncMock(return_value=None))
    elif fault == "unbound_filename":
        async def renamed(*args):
            return {**await original(*args), "filename_utf8": "collection.zip"}
        monkeypatch.setattr(case.client, "_latest_download_for_callback", renamed)
    elif fault == "other_callback":
        async def moved(*args):
            row = await original(*args)
            return {**row, "task": {**row["task"], "callback": {"display_id": 8}}}
        monkeypatch.setattr(case.client, "_get_file_metadata", moved)
    elif fault == "invalid_zip":
        monkeypatch.setattr(mt.mythic, "download_file", AsyncMock(return_value=b"SharpHound usage: not a ZIP"))
    else:
        case.ingest_tool.statuses = {
            "job_failed": [(5, "Failed")], "job_pending": [(6, "Ingesting")],
            "job_mismatched": [[{"type": "text", "text": json.dumps({"info_type": "status", "data": {
                "job_id": 40, "status": 2, "status_message": "Complete"}})}]],
        }[fault]
    return None


@pytest.mark.parametrize("fault, status, verdict, phase", [
    ("no_zip", "no_collection_artifact", "failed", "listing"),
    # Changed expectation: this row pinned the defect where the unproductive-repeat guard cut the collector's
    # four-listing bound at three (`task_issue_failed`); the collector owns that bounded wait and its expiry outcome.
    ("stale_zip", "no_collection_artifact", "failed", "listing"),
    ("empty_dir", "no_collection_artifact", "failed", "listing"),
    ("no_filemeta", "no_fresh_collection", "failed", "artifact"),
    ("unbound_filename", "artifact_blocked", "blocked", "artifact"),
    ("other_callback", "blocked", "blocked", "ingest"),
    ("invalid_zip", "error", "failed", "ingest"),
    ("job_failed", "ingest_failed", "failed", "ingest"),
    ("job_mismatched", "error", "failed", "ingest"),
    ("job_pending", "uploaded_pending_ingest", "partial", "ingest"),
])
def test_missing_or_unbound_artifacts_and_unfinished_ingest_never_record_an_achieved_effect(
    collection, monkeypatch, tmp_path, fault, status, verdict, phase,
):
    case = collection
    _real_recorder(case, monkeypatch, tmp_path)
    streamed = _streamed(case)
    _mythic(case, monkeypatch, outputs=_artifact_fault(case, monkeypatch, fault))
    _approve(case, ACTION)

    result = _run(case, ACTION)

    commands = [*RUNNER_STEPS, *["ls"] * 4] if phase == "listing" else APPROVED_STEPS
    report = _assert_terminal(case, result, verdict, phase, commands)
    if phase == "listing":  # the collector's own bounded wait ends in its expiry outcome, never the loop guard
        assert result["reason"] == "SharpHound produced no collection ZIP carrying this run's token"
        assert "trajectory_repair" not in result
        text = "\n".join([json.dumps(result), report, *streamed])
        assert "unproductive" not in text.casefold() and "[SAGE LOOP-GUARD]" not in text
    assert (result["ok"], result["status"]) == (False, status), result
    assert result["collector_diagnostics"]["task_id"] == 3
    assert result["recorded_effects"] == [] and _graph_hops(_reloaded(case)[0]) == []
    if verdict == "partial":
        assert result["bloodhound_job_id"] == 41 and case.ingest_tool.uploads == 1


def _context_at_issue(monkeypatch):
    """Record, at the Mythic boundary, the ambient visibility context each task is created under."""
    issue, seen = mt.mythic.issue_task, []

    async def observed(**kwargs):
        seen.append((kwargs["command_name"], dict(mt._task_visibility_context.get() or {})))
        return await issue(**kwargs)

    monkeypatch.setattr(mt.mythic, "issue_task", observed)
    return seen


def test_three_unchanged_listings_then_this_runs_zip_at_the_fourth_is_retrieved_once(collection, monkeypatch,
                                                                                       tmp_path):
    case = collection
    _real_recorder(case, monkeypatch, tmp_path)
    seen = _context_at_issue(monkeypatch)
    stale = _artifact_fault(case, monkeypatch, "stale_zip")["ls"]
    # Tasks 4-6 list the stale directory; task 7 (the fourth listing) falls through to the fixture's token ZIP.
    _mythic(case, monkeypatch, outputs={"ls": lambda task_id: stale if task_id < 7 else None})
    case.ingest_tool.statuses = [(2, "Complete")]
    _approve(case, ACTION)

    result = _run(case, ACTION)

    commands = [*RUNNER_STEPS, *["ls"] * 4, "download"]
    _assert_terminal(case, result, "achieved", "ingest", commands)
    assert (result["ok"], result["status"], len(result["recorded_effects"])) == (True, "ingested", 1), result
    hops = _graph_hops(_reloaded(case)[0])
    assert len(hops) == 1 and hops[0].status == "achieved" and hops[0].proof_envelope["task_id"] == "8"
    # The private exception is present only for the collector's own listings, beside the collection's provenance.
    assert [(command, context.get("bounded_delayed_proof_poll") is True) for command, context in seen] == [
        (command, command == "ls") for command in commands]
    assert all(context["capability"] == "collect-graph" for _command, context in seen)
    assert mt._task_visibility_context.get() is None


def test_collector_listings_leave_the_guard_to_judge_other_repeats_and_the_envelope_still_bounds_them(
    collection, monkeypatch,
):
    case = collection
    client, ls_args = case.client, {"path": mt.SHARPHOUND_CANONICAL_OUTPUT_DIRECTORY}
    _mythic(case, monkeypatch, outputs=_artifact_fault(case, monkeypatch, "stale_zip"))
    _approve(case, ACTION)
    result = _run(case, ACTION)
    assert (result["status"], _commands(case)) == ("no_collection_artifact", [*RUNNER_STEPS, *["ls"] * 4]), result
    # The collector's listings were neither counted nor flagged: the runner's diagnostic (non-success) output ended
    # the streak, and the four identical listings did not start a new one.
    assert client._unproductive_tripped == set() and client._last_action_sig is None

    async def issue(context, timeout=300, **kwargs):
        token = mt._task_visibility_context.set(context)
        try:
            return await client.issue_task_and_waitfor_task_output("ls", ls_args, 7, timeout=timeout, **kwargs)
        finally:
            mt._task_visibility_context.reset(token)

    # A fifth listing under the collector's own exception is refused by the transaction envelope; no task.
    fifth = asyncio.run(issue({"capability": "collect-graph", "bounded_delayed_proof_poll": True}))
    assert fifth.startswith("STOP") and len(case.tasks) == 7, fifth
    assert client._private_collection_transaction.reserve_child(
        command="ls", parameters=ls_args, callback_display_id=7, token_id=None, timeout=300,
        visibility_context={"capability": "collect-graph"},
    ) == "collection child exceeded the bounded listing envelope"
    # The model's own identical `ls`, each under its own supervised request and Accept (the guard is per client),
    # trips the guard at the third, and the fourth is refused before a task.
    root = {"name": "issue_task_and_waitfor_task_output",
            "args": {"command": "ls", "parameters": ls_args, "callback_display_id": 7}}
    outputs = []
    for number in range(1, 5):
        contract = build_request_contract(
            request_id=f"request-model-ls-{number}", channel_id="channel-test", operation_id="operation-test",
            mode="supervised", autonomous_solve=False, requested_actions=(action_spec_from_tool_call(root),))
        client.set_request_contract(contract)
        client.set_turn_authority(authority_from_request_contract(contract))
        client.set_approval_claim(_approval_claim_for(contract, [root]))
        outputs.append(asyncio.run(issue(None, timeout=None)))
    assert ["[SAGE LOOP-GUARD]" in output for output in outputs[:3]] == [False, False, True], outputs
    assert "already flagged as an unproductive loop" in outputs[3] and len(case.tasks) == 10, outputs[3]


@pytest.mark.parametrize("control", [RuntimeError("subscription closed"), asyncio.CancelledError()],
                         ids=["error", "cancel"])
def test_the_listing_exception_is_removed_after_an_error_or_a_cancellation(collection, monkeypatch, control):
    case = collection
    seen = _context_at_issue(monkeypatch)
    _mythic(case, monkeypatch, outputs={"ls": control})
    ambient = {"capability": "collect-graph", "purpose": "controller collection"}

    async def exercise():
        token = mt._task_visibility_context.set(ambient)
        try:
            try:
                result = await case.owner._controller_collect(case.state, request=case.request)
            except asyncio.CancelledError:
                result = None
            return result, mt._task_visibility_context.get()
        finally:
            mt._task_visibility_context.reset(token)

    result, after = asyncio.run(exercise())
    assert after is ambient
    assert seen[-1] == ("ls", {**ambient, "bounded_delayed_proof_poll": True}), seen
    assert (result is None) is isinstance(control, asyncio.CancelledError)
    if result is not None:
        assert (result["ok"], result["phase"]) == (False, "listing"), result


def test_the_controller_spends_its_collection_retry_budget_on_an_unchanged_listing(collection, monkeypatch):
    from ai.langgraph import autonomous_controller as ac

    case = collection
    _mythic(case, monkeypatch, outputs=_artifact_fault(case, monkeypatch, "stale_zip"))
    attempts = []

    async def collect(_observed):  # each controller collection consumes its own Accept: a fresh claim per attempt
        case.client.set_approval_claim({**case.claim, "approval_id": f"approved-collection-{len(attempts) + 1}"})
        attempts.append(await _collect(case))
        return attempts[-1]

    controller = ac.AutonomousController(
        observe=lambda: case.state, execute=lambda _action: {"ok": True}, frontier_fn=lambda _state: [],
        needs_collection=lambda _state: {"collection_key": case.request.collection_key}, collect=collect,
        config=ac.ControllerConfig(max_collection_attempts_per_request=2),  # Sage's own setting
    )
    result = asyncio.run(controller.run())

    assert (result.status, result.reason) == (ac.STATUS_BLOCKED, "collection retry budget exhausted for one request")
    assert result.blocker["reason"] == "baseline: no_collection_artifact" and "trajectory_repair" not in result.blocker
    assert [attempt["status"] for attempt in attempts] == ["no_collection_artifact"] * 2
    assert _commands(case) == [*RUNNER_STEPS, *["ls"] * 4] * 2


@pytest.mark.parametrize("fault, verdict, phase, commands", [
    ("identity", "failed", "identity", ["whoami"]),
    ("restore", "failed", "restore", ["whoami", "ticket_cache_list", "rev2self"]),
    ("runner_wait", "failed", "runner", RUNNER_STEPS),
    ("listing", "failed", "listing", [*RUNNER_STEPS, "ls"]),
    ("download", "failed", "download", APPROVED_STEPS),
    ("ingest_exception", "failed", "ingest", APPROVED_STEPS),
    ("ingest_unready", "blocked", "preflight", []),
    ("no_target", "blocked", "preflight", []),
    ("no_runner", "blocked", "preflight", []),
])
def test_each_early_refusal_or_failure_names_its_phase_and_only_its_own_tasks(collection, monkeypatch,
                                                                             fault, verdict, phase, commands):
    case = collection
    rows = {"identity": {"whoami": {"status": "error"}}, "restore": {"rev2self": {"status": "error"}},
            "listing": {"ls": {"status": "error"}}, "download": {"download": {"status": "error"}}}.get(fault)
    outputs = {
        "restore": {"whoami": "Local Identity: workstation\\local\nImpersonation Identity: workstation\\local",
                    "ticket_cache_list": "[]"},
        "runner_wait": {"execute_assembly": RuntimeError("subscription closed")},
    }.get(fault)
    _mythic(case, monkeypatch, rows=rows, outputs=outputs)
    action = {**ACTION, "callback_id": "9"} if fault == "no_target" else ACTION
    _approve(case, action)
    if fault == "ingest_exception":
        monkeypatch.setattr(case.client, "ingest_collection", AsyncMock(side_effect=RuntimeError("mcp closed")))
    elif fault == "ingest_unready":
        case.ingest_tool.patched = False
    elif fault == "no_runner":
        case.client.set_collection_runner(None)

    result = _run(case, action)

    _assert_terminal(case, result, verdict, phase, commands)
    assert result["ok"] is False and result.get("reason"), result


@pytest.mark.parametrize("seam", ["identity", "runner_task", "ingest"])
@pytest.mark.parametrize("control", [lambda: GraphInterrupt(()), asyncio.CancelledError], ids=["interrupt", "cancel"])
def test_cancellation_and_graph_interrupts_propagate_instead_of_becoming_a_verdict(collection, monkeypatch,
                                                                                  seam, control):
    """The collector's own handlers re-raise control; each seam is a call the collector makes itself."""
    case = collection
    raised = control()
    _mythic(case, monkeypatch)
    _approve(case, ACTION)
    issue = case.client.issue_task_and_waitfor_task_output

    async def runner_task(command, *args, **kwargs):
        if command == "execute_assembly":
            raise raised
        return await issue(command, *args, **kwargs)

    seams = {"identity": ("probe_authentication_context", AsyncMock(side_effect=raised)),
             "runner_task": ("issue_task_and_waitfor_task_output", runner_task),
             "ingest": ("ingest_collection", AsyncMock(side_effect=raised))}
    monkeypatch.setattr(case.client, *seams[seam])
    with pytest.raises(type(raised)):
        _run(case, ACTION)


def test_supervised_tasking_still_requires_accept_and_conversation_still_denies(collection, monkeypatch):
    case = collection
    _mythic(case, monkeypatch)
    with pytest.raises(ValueError, match="conversational lane denies external actions"):
        _approve(case, ACTION, mode="conversation")
    case.client.clear_approval_claim()  # Reject: no claim is installed
    with pytest.raises(PermissionError):
        _run(case, ACTION)
    assert case.tasks == [] and case.ingest_tool.calls == []


@pytest.mark.parametrize("bounded", [True, False], ids=["bounded-one-action", "delegation-summary"])
def test_the_worker_reports_a_failed_collection_with_the_executor_report(collection, monkeypatch, bounded):
    case = collection
    _mythic(case, monkeypatch, outputs={"ls": NO_FILES})
    _approve(case, ACTION)
    raw = asyncio.run(case.client.execute_capability(ACTION, None))
    call = {"name": "execute_capability", "args": {"action": ACTION}, "id": "call-collect", "type": "tool_call"}
    prompt = ("Use execute_capability exactly once for collect-graph on callback 7, then stop." if bounded
              else "Collect the graph from callback 7 with execute_capability.")

    class RecordedWorker:  # returns the real ToolMessage; no model interprets the failure
        async def ainvoke(self, args, config=None):
            return {"messages": [*args["messages"], AIMessage(content="", tool_calls=[call], name="Mythic_Operator"),
                                 ToolMessage(content=raw, name="execute_capability", tool_call_id="call-collect")]}

    owner = model.Model.__new__(model.Model)
    owner._turn_authority = compile_turn_authority(prompt, objective_classifier=lambda _text: False)
    owner._message_seq, owner.state, owner.mythic_client = 1, {"_message_seq": 1}, None
    owner.llm = None  # a synthesis call would fail the test
    state = {"_message_seq": 1, "supervisor_messages": [], "mythic_operator_messages": [HumanMessage(content=prompt)]}
    wrapped = owner._wrap_create_agent(RecordedWorker(), "mythic_operator_messages", "Mythic_Operator")
    update = asyncio.run(wrapped(state, {})).update

    text = "\n".join(str(message.content) for message in update["supervisor_messages"])
    expected = model._terminal_execute_capability_report(
        model._terminal_execute_capability_payload([ToolMessage(content=raw, name="execute_capability",
                                                                tool_call_id="call-collect")]),
        bounded_one_action=bounded,
    )
    assert expected in text, text
    assert "Executor verdict: `failed` for `collect-graph`" in expected and "Phase: listing" in expected
    assert "1 `whoami`, 2 `ticket_cache_list`, 3 `execute_assembly`, 4 `ls`" in expected


@pytest.mark.parametrize("runner, verdict, phase", [(DIAGNOSTIC, "achieved", "ingest"),
                                                    ("\n".join(_HEAD), "failed", "runner")],
                         ids=["eligible", "no-completion-record"])
def test_the_controller_route_returns_the_same_verdict_phase_and_tasks(collection, monkeypatch,
                                                                       runner, verdict, phase):
    case = collection
    _mythic(case, monkeypatch, runner=runner)
    result = asyncio.run(_collect(case))  # the controller's own call (no execute_capability wrapper)
    assert (result["verdict"], result["phase"]) == (verdict, phase), result
    assert [item["command"] for item in result["issued"]] == (APPROVED_STEPS if verdict == "achieved"
                                                              else RUNNER_STEPS)


@pytest.mark.parametrize("runner, admitted", [(DIAGNOSTIC, True), ("\n".join(_HEAD), False)],
                         ids=["eligible", "no-completion-record"])
def test_the_objective_bound_collector_record_admits_listing_only_after_an_eligible_runner(monkeypatch,
                                                                                          runner, admitted):
    authority = _resolve_collection_authority(_load_turn_authority().compile_turn_authority(
        "Complete the objective.", objective_classifier=model.Model._looks_like_explicit_objective_prompt,
        stored_operator_objective="Collect and ingest the current graph.",
    ))
    contract = authority.objective_contract
    tools = mt.MythicTools(channel_id=10)
    tools.client = object()
    tools.set_turn_authority(authority)
    tools.begin_operator_turn(contract.objective_text, objective_contract=contract)
    _install_collection_issue_test_path(monkeypatch, tools, mt)
    monkeypatch.setattr(mt.mythic, "waitfor_for_task_output", AsyncMock(return_value=runner))
    monkeypatch.setattr(tools, "_supervised_task_state",
                        AsyncMock(return_value={"task_completed": True, "task_status": "success"}))  # Apollo's form
    parameters = {"Assembly": "SharpHound.exe", "Arguments": (
        "-c All --CollectAllProperties --SearchForest -o C:\\Users\\Public "
        f"--ZipFilename bloodhound_{contract.collection_token}.zip")}
    receipt = {}
    token = mt._task_issuance_receipt_context.set(receipt)  # what the collector's own task call installs
    try:
        asyncio.run(tools.issue_task_and_waitfor_task_output(contract.collection_profile.runner_command,
                                                             parameters, 7, timeout=5))
    finally:
        mt._task_issuance_receipt_context.reset(token)

    collector = tools._operator_collection_request["transaction"]["collector"]
    assert collector["terminal_success"] is False and receipt["output_success"] is False
    assert collector.get("retrieval_eligible", False) is admitted and receipt["retrieval_eligible"] is admitted
    listing = tools._reserve_contract_collection_attempt(
        contract.collection_profile.ls_command, 7, {"path": "C:\\Users\\Public"}, None)
    assert (listing == "") is admitted, listing
    assert "download terminal success" in tools._contract_ingest_preflight_blocker() if admitted else True
    terminal = tools.contract_progress_snapshot()["terminal_state"]
    assert (terminal is None) if admitted else terminal["kind"] == "collector_failed", terminal
