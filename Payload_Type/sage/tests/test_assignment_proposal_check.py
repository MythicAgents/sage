"""A prepared raw task proposal is checked against its delegated assignment before native approval.

The episodes drive the real native chat service, Supervisor handoff, Mythic_Operator worker, turn-authority
middleware, task preparer and approval card. Only the chat model is scripted; the checker is the same configured
model, so its verdicts are scripted turns. These prove wiring and enforcement, not model judgment.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from tests import test_scripted_handoff as harness_module

DISCOVERY = "Report which domain controller serves the callback's domain."


def call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def verdict(value: str, reason: str = "scripted verdict") -> AIMessage:
    return AIMessage(content=json.dumps({"verdict": value, "reason": reason}))


def delegate(instruction: str = DISCOVERY) -> AIMessage:
    return call("transfer_to_Mythic_Operator", {
        "handoff_title": "Selected read", "handoff_instruction": instruction, "callback_display_id": 7,
        "targets": ["WORKSTATION-7"], "expected_result": "The requested read's result.",
        "allowed_tools": ["issue_task_and_waitfor_task_output"]}, "route-op")


def propose(command: str, parameters: str = "", call_id: str = "proposed", callback: int = 7) -> AIMessage:
    return call("issue_task_and_waitfor_task_output",
                {"callback_display_id": callback, "command": command, "parameters": parameters}, call_id)


def episode(monkeypatch, tmp_path, script, *, prompt="Find the domain controller with one read on callback 7.",
            accept=False, after_accept=None, graph_tools=None):
    from ai.mcp import MCPManager
    from ai.langgraph.model import Model
    from ai.langgraph.mythic_tools import MythicTools
    from sage_chat import session
    from sage_chat.headless import build_chat_request
    from sage_chat.service import bind_channel_thread_id
    from tests.test_supervised_objective_native import driver

    seen = []
    original = harness_module._ScriptedModel._generate

    def generate(self, messages, *args, **kwargs):
        seen.append(list(messages))
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(harness_module._ScriptedModel, "_generate", generate)
    monkeypatch.setattr(harness_module, "_RECURSION_LIMIT", 32)
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    if graph_tools:
        monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
        monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _server: list(graph_tools)))
        monkeypatch.setattr(Model, "_bloodhound_server_is_locally_pinned",
                            lambda self, server: server == "offline-graph")
        from ai.langgraph import mcp_tool_policy
        from ai.langgraph.mythic_tools import TOOL_SAFETY_READ_ONLY

        classify = mcp_tool_policy.classify_mcp_tool
        monkeypatch.setattr(mcp_tool_policy, "classify_mcp_tool", lambda server, tool: (
            TOOL_SAFETY_READ_ONLY if server == "offline-graph" else classify(server, tool)))
    else:
        monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: []))
    effects = []
    sink = MythicTools(agent_task_id="offline-assignment-check", operation_id=1, channel_id=1)

    async def available_command(command, _callback):
        return {"status": "available", "command": command}

    async def empty_schema(_command, _callback):
        return []

    sink._authenticate_live_command = available_command
    sink._fetch_command_schema = empty_schema

    @tool
    async def issue_task_and_waitfor_task_output(
        command: str, parameters: str, callback_display_id: int,
        token_id: int | None = None, timeout: int | None = None,
        visibility_context: dict | None = None,
    ) -> str:
        """Inert guarded command with the production request and approval check."""
        blocker = sink._turn_authority_issue_blocker(
            command, callback_display_id, parameters=parameters,
            token_id=token_id, timeout=timeout, visibility_context=visibility_context,
        )
        if blocker:
            return blocker
        effects.append((command, callback_display_id))
        return "inert output"

    sink._recon_snapshot_cache[("list_callbacks", "all")] = [{"id": 7, "host": "WORKSTATION-7"}]
    sink.get_tools = lambda names: ([issue_task_and_waitfor_task_output]
                                    if "issue_task_and_waitfor_task_output" in names else [])
    harness = harness_module._Harness(script)
    outcome = {}

    async def run():
        model = harness._build(script)
        model.mode = "supervised"
        model.operation_id = 1
        model.mythic_client = sink
        model._payload_names = []
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        model._rebuild_graph()
        chat = driver.DriverChat(model)
        request = build_chat_request(prompt, channel_id=1, operation_id=1, request_id=1,
                                     config={"mode": "supervised", "autonomous_solve": False})
        bind_channel_thread_id(request, model)
        try:
            await chat.chat(request)
            outcome["cards"] = [row["metadata"]["input_requested"] for row in chat.emissions
                                if (row.get("metadata") or {}).get("special_type") == "input_requested"]
            if accept and outcome["cards"]:
                from tests.test_hitl_steer_current_intent import _input_response
                if after_accept is not None:
                    after_accept(model)
                second = build_chat_request("", channel_id=1, operation_id=1, request_id=2,
                                            config={"mode": "supervised", "autonomous_solve": False})
                second.InputResponse = _input_response("accept", outcome["cards"][0])
                await chat.chat(second)
            outcome["model"] = model
            snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
            outcome["state"] = dict(getattr(snapshot, "values", {}) or {})
            outcome["text"] = "\n".join(str(row.get("content") or "") for row in chat.emissions)
        finally:
            await session.drop_channel_session(request)

    asyncio.run(run())
    outcome["effects"] = effects
    outcome["seen"] = seen
    return outcome


def _events(model, kind):
    return [event for event in model._request_event_ledger.events if event.kind == kind and not event.projected]


def test_an_unrelated_proposal_never_cards_and_one_correction_reaches_the_assigned_read(monkeypatch, tmp_path):
    out = episode(monkeypatch, tmp_path, [
        delegate(),
        propose("klist", call_id="ticket-cache"),
        verdict("inconsistent", "A local ticket-cache listing does not identify a domain controller."),
        propose("nltest", "/dsgetdc:lab.local", call_id="dc-read"),
        verdict("consistent", "nltest /dsgetdc reports the domain controller."),
    ])
    assert [card["data"]["arguments"]["command"] for card in out["cards"]] == ["nltest"]
    assert out["effects"] == []
    corrections = _events(out["model"], "assignment_check_correction")
    assert len(corrections) == 1
    assert '"verdict"' not in out["text"]  # checker output is never published as worker prose
    checks = _events(out["model"], "assignment_check")
    assert [json.loads(event.content)["verdict"] for event in checks] == ["inconsistent", "consistent"]
    # The correction names the mismatch and the current assignment to the same worker.
    correction_turn = out["seen"][-2]
    assert any("ticket-cache listing" in str(m.content) and DISCOVERY in str(m.content) for m in correction_turn)
    # The checker saw the prepared action and assignment, never the worker's reasoning or tools.
    checker_input = out["seen"][2]
    assert len(checker_input) == 2 and '"command": "klist"' in str(checker_input[1].content)
    assert DISCOVERY in str(checker_input[1].content)


@pytest.mark.parametrize("command,parameters", [
    ("whoami", ""),                                  # Apollo-style
    ("run", '{"path": "whoami", "args": ""}'),       # Merlin-style
    ("zz_identity_probe_v9", ""),                    # previously unseen payload command
])
def test_a_consistent_proposal_reaches_its_exact_card_whatever_the_payload_command(
        command, parameters, monkeypatch, tmp_path):
    out = episode(monkeypatch, tmp_path, [
        delegate("Report callback 7's current process identity."),
        propose(command, parameters),
        verdict("consistent"),
    ], prompt="Report callback 7's current process identity with one read.")
    assert [card["data"]["arguments"]["command"] for card in out["cards"]] == [command]
    assert out["effects"] == []
    assert _events(out["model"], "assignment_check_correction") == []


@pytest.mark.parametrize("response", [
    verdict("unknown", "The command's semantics are unavailable."),
    AIMessage(content="Looks fine to me."),
    AIMessage(content='{"verdict": "consistent", "verdict": "inconsistent", "reason": "x"}'),
    AIMessage(content='{"verdict": "approve", "reason": "x"}'),
    AIMessage(content='{"verdict": "consistent", "reason": "x", "approved": true}'),
])
def test_unknown_or_malformed_checker_output_ends_the_request_without_a_card_or_task(
        response, monkeypatch, tmp_path):
    out = episode(monkeypatch, tmp_path, [delegate(), propose("nltest", "/dsgetdc:lab.local"), response])
    assert out["cards"] == [] and out["effects"] == []
    assert out["model"]._stop_requested and out["model"]._stop_reason == "assignment_check"
    assert "no task was issued" in out["text"]
    assert _events(out["model"], "assignment_check_correction") == []


def test_a_second_inconsistency_ends_the_request_instead_of_looping(monkeypatch, tmp_path):
    out = episode(monkeypatch, tmp_path, [
        delegate(),
        propose("klist", call_id="first"), verdict("inconsistent", "ticket cache is not discovery"),
        propose("ps", call_id="second"), verdict("inconsistent", "a process list is not discovery"),
    ])
    assert out["cards"] == [] and out["effects"] == []
    assert len(_events(out["model"], "assignment_check_correction")) == 1
    assert out["model"]._stop_reason == "assignment_check"


def test_a_callback_outside_the_brief_is_rejected_in_code_before_any_checker_call(monkeypatch, tmp_path):
    out = episode(monkeypatch, tmp_path, [
        delegate(),
        propose("nltest", "/dsgetdc:lab.local", call_id="wrong-callback", callback=9),
        propose("nltest", "/dsgetdc:lab.local", call_id="right-callback"),
        verdict("consistent"),
    ])
    assert [card["data"]["arguments"]["callback_display_id"] for card in out["cards"]] == [7]
    checks = [json.loads(event.content) for event in _events(out["model"], "assignment_check")]
    assert [check["verdict"] for check in checks] == ["inconsistent", "consistent"]
    assert checks[0]["source"] == "code"


@pytest.mark.parametrize("change", ["none", "changed_action", "operator_steer", "other_delegation", "no_check"])
def test_the_pre_execution_boundary_runs_only_the_exact_checked_action(change):
    from langchain_core.messages import HumanMessage
    from sage_chat.hitl import approval_action_fingerprint
    from tests.test_native_chat_multiturn_state import _install_contract_without_graph_rebuild, _new_model

    request_id = "chat:channel-isolation:request:1"

    async def run() -> str:
        model = _new_model()
        _install_contract_without_graph_rebuild(model, request_id)
        model.mode = "supervised"
        model.state["supervisor_messages"] = [HumanMessage(content="Find the DC.", additional_kwargs={
            "_request_id": request_id, "_operator_input": True, "_operator_input_revision": 0, "_seq": 1})]
        delegated = HumanMessage(content="Find the DC.", additional_kwargs={
            "_delegated_to": "Mythic_Operator", "_handoff_request_id": request_id, "_seq": 2,
            "_assisted_assignment": DISCOVERY, "_handoff_brief": {"callback_display_id": "7"}})
        messages = [delegated]
        checked = {"name": "issue_task_and_waitfor_task_output",
                   "args": {"command": "nltest", "parameters": "/dsgetdc:lab.local", "callback_display_id": 7}}
        scope = model._assignment_check_scope("Mythic_Operator", messages)
        if change != "no_check":
            from ai.langgraph import assignment_check
            bound = assignment_check.binding(
                request_id=request_id, operator_revision=0, delegation=scope["delegation"],
                assignment_digest=scope["assignment_digest"],
                action_fingerprint=approval_action_fingerprint(checked))
            model._record_assignment_check(scope, bound, verdict="consistent", reason="ok", source="checker")
        executed = dict(checked, args=dict(checked["args"]))
        if change == "changed_action":
            executed["args"]["parameters"] = "/dsgetdc:other.local"
        elif change == "operator_steer":
            model.state["supervisor_messages"].append(HumanMessage(content="Only list callbacks.", additional_kwargs={
                "_request_id": request_id, "_operator_input": True, "_operator_input_revision": 1,
                "_operator_steer": True, "_seq": 3}))
        elif change == "other_delegation":
            messages = [HumanMessage(content="Find the DC.",
                                     additional_kwargs={**delegated.additional_kwargs, "_seq": 9})]
        return model._assignment_check_admission(
            "Mythic_Operator", executed, approval_action_fingerprint(executed), messages)

    reason = asyncio.run(run())
    assert (reason == "") is (change == "none")


@pytest.mark.parametrize("tamper", [False, True])
def test_an_accepted_card_executes_only_under_its_recorded_consistent_check(tamper, monkeypatch, tmp_path):
    def forget_check(model):
        # The approved action loses its consistent binding (as a mutated or replayed action would).
        ledger = model._request_event_ledger
        ledger._events[:] = [event for event in ledger._events if event.kind != "assignment_check"]

    out = episode(monkeypatch, tmp_path, [
        delegate("Report callback 7's current process identity."),
        propose("whoami"),
        verdict("consistent"),
        AIMessage(content="Identity reported."),
        AIMessage(content="Callback 7 identity reported."),
    ], prompt="Report callback 7's current process identity with one read.", accept=True,
        after_accept=forget_check if tamper else None)
    assert [card["data"]["arguments"]["command"] for card in out["cards"]] == ["whoami"]
    assert out["effects"] == ([] if tamper else [("whoami", 7)])
    if not tamper:
        # The approval pause and resume neither re-ran the check nor produced a second card.
        assert len(_events(out["model"], "assignment_check")) == 1


def test_the_bound_explicit_collection_route_reaches_its_card_without_a_checker_call(monkeypatch):
    from ai.langgraph.model import Model
    from tests import test_supervised_request_closure as closure

    def no_check(self):
        pytest.fail("the bound collection procedure must not call the assignment checker")

    monkeypatch.setattr(Model, "_assignment_checker_model", no_check)
    closure.test_an_explicit_request_reaches_the_bound_procedure_before_any_supervisor_inference()


def test_a_checker_timeout_ends_the_request_without_a_card(monkeypatch, tmp_path):
    from ai.langgraph import assignment_check
    from ai.langgraph.model import Model

    class Slow:
        def bind(self, **_kwargs):
            return self

        async def ainvoke(self, _messages, config=None):
            await asyncio.sleep(5)

    monkeypatch.setattr(assignment_check, "CHECK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(Model, "_assignment_checker_model", lambda self: Slow())
    out = episode(monkeypatch, tmp_path, [delegate(), propose("nltest", "/dsgetdc:lab.local")])
    assert out["cards"] == [] and out["effects"] == []
    [check] = [json.loads(event.content) for event in _events(out["model"], "assignment_check")]
    assert (check["verdict"], check["reason"]) == ("unavailable", "the checker timed out")


def test_an_essential_packet_over_the_bound_is_unavailable_not_truncated():
    from ai.langgraph import assignment_check

    common = dict(request_id="r", operator_revision=0, preceding_answer=None, assignment="Find the DC.",
                  brief=None, action={"command": "nltest", "parameters": "", "callback_display_id": 7}, schema=[])
    assert assignment_check.proposal_packet(operator_input="x" * 70_000, **common) is None
    text, essential = assignment_check.proposal_packet(
        operator_input="Find the DC.", observations=["o" * 6000] * 20, **common)
    packet = json.loads(text)
    assert len(text) <= assignment_check.CHECK_PACKET_MAX_CHARS
    assert packet["operator_input"] == "Find the DC." and packet["source_observations_omitted"] > 0
    assert len(packet["source_observations"]) + packet["source_observations_omitted"] == 20


def test_the_checked_action_carries_no_secret_values():
    from ai.langgraph import assignment_check

    schema = [{"name": "credential", "type": "Credential"}, {"name": "hash", "type": "String"},
              {"name": "ref", "type": "Credential"}]
    projected = assignment_check.action_projection({
        "command": "pth", "callback_display_id": 7,
        "parameters": {"credential": "Summer2026!", "ref": "@cred:12",
                       "hash": "aad3b435b51404eeaad3b435b51404ee", "note": "password=hunter2"},
    }, schema)
    text = json.dumps(projected)
    assert "Summer2026!" not in text and "aad3b435b51404eeaad3b435b51404ee" not in text and "hunter2" not in text
    assert projected["parameters"]["credential"] == "<credential value omitted>"
    assert projected["parameters"]["ref"] == "@cred:12"


@pytest.mark.parametrize("text", [
    '{"verdict": "consistent", "reason": "fine"}',
    '```json\n{"verdict": "inconsistent", "reason": "not the assignment"}\n```',
])
def test_the_strict_parser_accepts_only_the_two_field_shape(text):
    from ai.langgraph import assignment_check

    assert set(assignment_check.parse_proposal_verdict(text)) == {"verdict", "reason"}


# ── Cold-review reproductions (2026-11-10): kept as regression probes ─────────────────────────────
from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402




SECRET = "Winter2026!Sup3r"


def test_proposal_packet_carries_no_raw_credential_from_a_prior_read():
    from tests.test_native_chat_multiturn_state import _install_contract_without_graph_rebuild, _new_model

    request_id = "chat:channel-isolation:request:1"
    captured = []

    async def run():
        model = _new_model()
        _install_contract_without_graph_rebuild(model, request_id)
        model.mode = "supervised"
        assert model._assignment_check_scope("Mythic_Operator", []) is not None

        async def checker(instruction, text):
            captured.append(text)
            return '{"verdict": "consistent", "reason": "ok"}', None

        model._invoke_assignment_checker = checker
        operator = HumanMessage(content="Use samwell's stored password to run make_token on callback 7.",
                                additional_kwargs={"_request_id": request_id, "_operator_input": True,
                                                   "_operator_input_revision": 0, "_seq": 1})
        model.state["supervisor_messages"] = [operator]
        messages = [
            HumanMessage(content=operator.content, additional_kwargs={
                "_delegated_to": "Mythic_Operator", "_handoff_request_id": request_id, "_seq": 2,
                "_assisted_assignment": "Create a logon token for samwell on callback 7."}),
            AIMessage(content="", tool_calls=[{"name": "read_credentials", "args": {"account": "samwell"},
                                               "id": "rc-1", "type": "tool_call"}]),
            # The production read_credentials report format (mythic_tools.read_credentials).
            ToolMessage(content=("1 credential(s) in the Mythic store:\n- account=samwell.tarly realm=NORTH "
                                 f"type=plaintext credential={SECRET}"), name="read_credentials",
                        tool_call_id="rc-1"),
        ]
        call = {"name": "issue_task_and_waitfor_task_output", "id": "p-1", "type": "tool_call",
                "args": {"callback_display_id": 7, "command": "make_token",
                         "parameters": '{"credential": "@cred:12"}'}}
        return await model._check_prepared_proposal("Mythic_Operator", call, messages)

    record = asyncio.run(run())
    assert captured
    assert not any(SECRET in text for text in captured), "raw credential copied into the checker packet"
