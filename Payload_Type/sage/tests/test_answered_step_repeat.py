"""A supervised step that already answered is not sent again unchanged.

Live supervised runs re-sent a step that had answered: a read the worker had fully answered, a worker's report that
it needed the operator to supply a tool, and an "already achieved" capability verdict. Each repeat returned the same
answer until the no-progress stop. Sage now refuses an unchanged repeat of the step that just answered: it hands the
answer back once, and a second repeat ends the request with that answer.

BloodHound analysis is one step whatever its title, instruction or scope: once BloodHound answered without effect,
the next BloodHound question naming no guarded tool is refused the same way, and the request ends `completed` when a
successful graph read backs the answer, `checkpoint` otherwise.

The scripted checks prove the handoff boundary, not that a real model then calls respond_to_user.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool, ToolException, tool
import pytest

_HARNESS_PATH = Path(__file__).resolve().with_name("test_scripted_handoff.py")
_SPEC = importlib.util.spec_from_file_location("scripted_handoff_harness_answered", _HARNESS_PATH)
_harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_harness)

from ai.langgraph import model as model_module  # noqa: E402
from ai.langgraph import step_bounds as sb  # noqa: E402
from tests import test_step_bounds as native  # noqa: E402

READ = {"handoff_title": "Fetch task 88 output", "handoff_instruction": "Fetch task 88 output.",
        "allowed_capabilities": ["control_plane"], "expected_result": "Task 88 output."}
ANSWER = "Task 88 failed: Rubeus.exe is not staged. Upload it to the tools drop zone to continue."


def _handoff(args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "transfer_to_Mythic_Operator", "args": args, "id": call_id, "type": "tool_call"}])


def _run(script: list[AIMessage]) -> dict:
    limit = _harness._RECURSION_LIMIT
    _harness._RECURSION_LIMIT = 60
    try:
        return asyncio.run(_harness._Harness(script).run("Check the results of task 88.", assisted=True))
    finally:
        _harness._RECURSION_LIMIT = limit


def _steps(messages: list) -> int:
    return sum("_step_outcome" in m.additional_kwargs for m in messages)


def _final(messages: list) -> AIMessage:
    """The Supervisor's last answer: its own final text, or a final report Sage wrote for it."""
    return next(m for m in reversed(messages) if isinstance(m, AIMessage) and m.name == "Supervisor"
                and str(m.content).strip() and not m.tool_calls)


def _stops(messages: list) -> list:
    return [m for m in messages if "Stopped" in str(m.content) or "Halted" in str(m.content)]


# --- assembled: the scripted graph ------------------------------------------------------------------------------

def test_an_answered_step_sent_again_hands_its_answer_back_without_dispatch():
    script = [_handoff(READ, "h1"), AIMessage(content=ANSWER), _handoff(READ, "h2"),
              AIMessage(content="Rubeus.exe is missing; upload it to continue.")]
    messages = _run(script)["supervisor_messages"]

    assert _steps(messages) == 1  # the repeat never reached the worker
    [handback] = [m for m in messages if getattr(m, "name", "") == "transfer_to_Mythic_Operator"
                  and "not dispatched" in str(m.content)]
    assert ANSWER in str(handback.content)
    assert str(_final(messages).content) == "Rubeus.exe is missing; upload it to continue."
    assert _stops(messages) == []


def test_a_second_unchanged_repeat_ends_the_request_with_the_answer():
    script = [_handoff(READ, "h1"), AIMessage(content=ANSWER), _handoff(READ, "h2"), _handoff(READ, "h3"),
              AIMessage(content="never reached")]
    messages = _run(script)["supervisor_messages"]

    assert _steps(messages) == 1
    final = _final(messages)
    assert str(final.content) == ANSWER
    assert final.additional_kwargs["_turn_outcome"] == "checkpoint"
    assert _stops(messages) == []


def test_a_repeat_that_only_rewords_its_expected_result_is_not_dispatched():
    """Live shape: the Supervisor re-sent one step with the same title and scope, rewording only the result it
    expected each time, and every rewording was dispatched until the request was blocked."""
    reworded = {**READ, "expected_result": "The status and output of task 88, returned."}
    script = [_handoff(READ, "h1"), AIMessage(content=ANSWER), _handoff(reworded, "h2"),
              AIMessage(content="Rubeus.exe is missing; upload it to continue.")]
    messages = _run(script)["supervisor_messages"]

    assert _steps(messages) == 1
    assert any("not dispatched" in str(m.content) for m in messages
               if getattr(m, "name", "") == "transfer_to_Mythic_Operator")
    assert _stops(messages) == []


@pytest.mark.parametrize("change", [
    {"handoff_title": "Fetch task 89 output"},
])
def test_a_changed_step_is_dispatched(change):
    script = [_handoff(READ, "h1"), AIMessage(content=ANSWER), _handoff({**READ, **change}, "h2"),
              AIMessage(content="Task 89 succeeded."), AIMessage(content="final")]
    messages = _run(script)["supervisor_messages"]

    assert _steps(messages) == 2
    assert str(_final(messages).content) == "final"


def test_the_record_is_only_the_step_that_just_answered():
    """A -> B -> A is planning, not a repeat: only a back-to-back resend is refused."""
    other = {**READ, "handoff_title": "List callbacks", "expected_result": "Live callbacks."}
    script = [_handoff(READ, "h1"), AIMessage(content=ANSWER), _handoff(other, "h2"), AIMessage(content="cb 5"),
              _handoff(READ, "h3"), AIMessage(content=ANSWER), AIMessage(content="final")]
    messages = _run(script)["supervisor_messages"]

    assert _steps(messages) == 3


def test_a_bounded_step_is_still_continued_with_the_same_step():
    """A step that reached its tool-call bound has not answered; the same step again continues it."""
    outcome = {"status": "budget_exhausted", "tasks_issued": [], "credentials_added": [], "guarded_rejected": 0}
    assert not sb.answered_without_effect(outcome)


# --- pure: what counts as answered, and what is compared --------------------------------------------------------

@pytest.mark.parametrize("fields, answered", [
    ({"status": "ended"}, True),
    ({"status": "action_executed"}, True),            # e.g. an "already achieved" capability verdict
    ({"status": "precondition_unsatisfiable"}, True),
    ({"status": "action_executed", "tasks_issued": ["41"]}, False),
    ({"status": "action_executed", "credentials_added": [3]}, False),
    ({"status": "rejected", "guarded_rejected": 1}, False),
    ({"status": "ended", "guarded_rejected": 1}, False),
    ({"status": "rejected_by_scope"}, False),
    ({"status": "budget_exhausted"}, False),
    ({"status": "task_pending", "tasks_issued": ["41"]}, False),
])
def test_answered_without_effect(fields, answered):
    outcome = {"tasks_issued": [], "credentials_added": [], "guarded_rejected": 0, **fields}
    assert sb.answered_without_effect(outcome) is answered


def test_a_delegation_without_a_brief_is_never_compared():
    # Specialist questions with no structural brief differ only in their prose. BloodHound is the exception, below.
    assert model_module._answered_step_key("Generalist", "Find a path", None) == ""
    assert model_module._answered_step_key("Generalist", "Find a path", {"targets": [], "expected_result": ""}) == ""
    assert model_module._answered_step_key("Generalist", "Find a path", {"expected_result": "a path"}) == ""
    brief = {"allowed_capabilities": ["control_plane"], "expected_result": "x"}
    assert model_module._answered_step_key("Mythic_Operator", "Read  TASK", brief) \
        == model_module._answered_step_key("Mythic_Operator", "read task", {**brief, "expected_result": "y"})
    # A change of scope is a different step.
    for change in ({"targets": ["north.test"]}, {"allowed_capabilities": ["collect-graph"]},
                   {"callback_display_id": "5"}, {"inputs_by_reference": ["task:41"]}):
        assert model_module._answered_step_key("Mythic_Operator", "read task", {**brief, **change}) \
            != model_module._answered_step_key("Mythic_Operator", "read task", brief)


# --- assembled: native chat, an "already achieved" capability verdict -------------------------------------------

ACHIEVED = {
    "ok": True, "verdict": "achieved", "capability": "collect-graph",
    "reason": "requested capability effect is already achieved in the engagement ledger; no Mythic tasks issued",
    "issued": [], "recorded_effects": [], "achieved_effects": ["graph-built:north.test"],
    "proof_chain": [{"effect": "graph-built:north.test", "task_id": "41", "callback_id": "5"}],
    "stopped_after": "already_achieved",
}


def test_an_already_achieved_verdict_is_not_delegated_again(monkeypatch):
    ran: list[dict] = []

    @tool
    async def execute_capability(action: dict | str, inputs: dict | str | None = None) -> str:
        """Run one catalog capability."""
        ran.append({"action": action})
        return json.dumps(ACHIEVED)

    original = model_module.Model._rebuild_graph

    def rebuild(self, *args, **kwargs):
        sink = self.mythic_client
        issue = sink.get_tools.side_effect
        sink.get_tools.side_effect = lambda names: [*issue(names),
                                                    *([execute_capability] if "execute_capability" in names else [])]
        sink._engagement_footholds = [SimpleNamespace(callback_id="5", host="WORKSTATION-5", forest="north.test",
                                                      identity="NORTH\\operator")]
        return original(self, *args, **kwargs)

    monkeypatch.setattr(model_module.Model, "_rebuild_graph", rebuild)
    captured: list = []
    monkeypatch.setattr(native, "_outcomes", lambda messages: captured.extend(messages) or [])
    collect = {"handoff_title": "collect graph", "handoff_instruction": native.ORIGINAL, **native.BRIEF,
               "allowed_capabilities": ["collect-graph"], "targets": ["north.test"]}
    # A terminal capability result ends the worker's turn before another model call.
    script = [native._call("transfer_to_Mythic_Operator", collect, 1),
              native._call("transfer_to_Mythic_Operator", collect, 2),
              native._call("transfer_to_Mythic_Operator", collect, 3),
              AIMessage(content="never reached")]
    asyncio.run(native._native(script, "accept"))

    assert len(ran) == 1  # the capability ran once; neither repeat reached the worker
    assert _steps(captured) == 1
    final = _final(captured)
    assert "Executor verdict: `achieved` for `collect-graph`" in str(final.content)
    assert "task=41" in str(final.content)  # the existing proof is named
    assert final.additional_kwargs["_turn_outcome"] == "checkpoint"
    assert _stops(captured) == []


# --- BloodHound: an answered analysis is not asked again --------------------------------------------------------
#
# Live supervised shape: BloodHound ran its graph queries and answered with the options, then the Supervisor sent
# BloodHound five more analysis delegations, each with a new title, no brief and an instruction asking for "RAW
# results only", until the neutral cap stopped the request. Sage now decides the request does not ask BloodHound
# the same thing again: the first re-ask gets the answer back and the second ends the request with it.
#
# Everything is real except the models. BloodHound's turns and the other agents' turns come from separate scripts,
# so a re-ask that is dispatched is answered as it was live instead of desynchronising one shared script. The
# scripted Supervisor proves wiring only; whether a real Supervisor re-asks is the live check's question.

PATHS_ANSWER = ("Four options to Domain Admin in partner.test: 1) GenericAll on DC-1 via GROUP-B; "
                "2) ADCS ESC1 on template USER-ESC; 3) DCSync rights held by SVC-SYNC; 4) a cross-forest trust.")
REASK_TITLES = (  # the live re-asks' titles, with the domain renamed
    "BloodHound cross-forest path analysis",
    "Query BloodHound for partner paths",
    "BloodHound paths to partner.test DA",
    "BloodHound path analysis to PARTNER.TEST",
    "BloodHound path query to PARTNER.TEST",
)
CONTROL_PLANE = {"allowed_capabilities": ["control_plane"]}


class _RoutedModel(_harness._ScriptedModel):
    """BloodHound speaks from its own script; every other agent from the main one."""

    bloodhound: Any = None
    seen: Any = None  # what every non-BloodHound model call was given, in order

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        speaker = any(isinstance(m, SystemMessage) and "You are the **BloodHound Agent**" in str(m.content)
                      for m in messages)
        if not speaker:
            self.seen.append(list(messages))
        message = next(self.bloodhound if speaker else self.messages)
        return ChatResult(generations=[ChatGeneration(message=message)])


class _BloodHoundHarness(_harness._Harness):
    def __init__(self, script: list[AIMessage], bloodhound: list[AIMessage]) -> None:
        super().__init__(script)
        self.bloodhound = bloodhound

    def _build(self, script):
        model = super()._build(script)
        self.fake = _RoutedModel(messages=iter(script), bloodhound=iter(self.bloodhound), seen=[])
        model._get_base_chat_model = lambda *a, **k: self.fake  # type: ignore[assignment]
        return model


def _bh_handoff(title: str, call_id: str, brief: dict | None = None, instruction: str = "") -> AIMessage:
    args = {"handoff_title": title,
            "handoff_instruction": instruction or "Query BloodHound for attack paths into partner.test. Perform the "
                                                  "tool calls and return the RAW results only.",
            **(brief or {})}
    return AIMessage(content="", tool_calls=[{"name": "transfer_to_BloodHound", "args": args, "id": call_id,
                                              "type": "tool_call"}])


def _graph_read(call_id: str, name: str = "graph_analysis") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": {"query": "shortest path to Domain Admins"},
                                              "id": call_id, "type": "tool_call"}])


def _respond(text: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "respond_to_user", "args": {
        "final_response": text, "outcome": "completed"}, "id": "final-1", "type": "tool_call"}])


def _first_ask(brief: dict | None = None) -> AIMessage:
    return _bh_handoff("Cross-forest path to partner.test DA", "h1", brief,
                       "Query BloodHound for attack paths that lead to Domain Admin in partner.test and list our "
                       "options.")


def _reasks(brief: dict | None = None) -> list[AIMessage]:
    return [_bh_handoff(title, f"h{index}", brief) for index, title in enumerate(REASK_TITLES, 2)]


def _answers(first: list[AIMessage]) -> list[AIMessage]:
    """BloodHound's first answer, then the same answer to every re-ask that reaches it."""
    again = [message for index in range(len(REASK_TITLES))
             for message in (_graph_read(f"graph-again-{index}"), AIMessage(content=PATHS_ANSWER))]
    return [*first, *again]


def _run_bh(monkeypatch, script: list[AIMessage], bloodhound: list[AIMessage]) -> tuple[dict, Any]:
    from ai.langgraph.model import Model
    from ai.mcp import MCPManager

    @tool
    def graph_analysis(query: str) -> str:
        """Run an inert offline graph query."""
        return "PATH: USER-A -[MemberOf]-> GROUP-B -[GenericAll]-> DC-1"

    def _fail(query: str) -> str:
        raise ToolException("graph query failed: database unavailable")

    domain_info = StructuredTool.from_function(_fail, name="domain_info", description="Inert failing graph read.",
                                               handle_tool_error=True)

    @tool
    def adcs_info(query: str) -> str:
        """Inert graph read failing the way the BloodHound MCP server reports an API failure: an ordinary result."""
        return json.dumps({"error": "Connection error: BloodHound CE API unreachable"})

    # Named as in production, so the shipped MCP tool policy treats these graph reads as read-only.
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["BloodHound"]))
    monkeypatch.setattr(MCPManager, "get_tools_by_server",
                        staticmethod(lambda _server: [graph_analysis, domain_info, adcs_info]))
    monkeypatch.setattr(Model, "_bloodhound_server_is_locally_pinned", lambda self, server: server == "BloodHound")
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))
    harness = _BloodHoundHarness(script, bloodhound)
    limit = _harness._RECURSION_LIMIT
    _harness._RECURSION_LIMIT = 80
    try:
        result = asyncio.run(harness.run(
            "Use BloodHound to find a path that would give us Domain Admin in partner.test and let me know what our "
            "options are.", assisted=True))
    finally:
        _harness._RECURSION_LIMIT = limit
    harness.model.supervisor_inputs = harness.fake.seen
    return result, harness.model


def _bh_dispatches(result: dict) -> int:
    return sum(1 for m in result.get("bloodhound_messages", [])
               if isinstance(m, HumanMessage) and m.additional_kwargs.get("_delegated_to") == "BloodHound")


def _assert_ended_on_the_answer(result: dict, model: Any, outcome: str) -> None:
    messages = result["supervisor_messages"]
    assert _bh_dispatches(result) == 1  # no re-ask reached BloodHound
    assert _steps(messages) == 1
    # The first re-ask's handback lives in the Supervisor's own loop, which the second re-ask ends; the Supervisor
    # model read it before sending that second re-ask.
    [handback] = [m for m in model.supervisor_inputs[-1] if isinstance(m, ToolMessage) and m.tool_call_id == "h2"]
    assert "not dispatched" in str(handback.content) and PATHS_ANSWER in str(handback.content)
    final = _final(messages)
    assert str(final.content) == PATHS_ANSWER
    assert final.additional_kwargs["_is_final_report"] is True
    assert final.additional_kwargs["_turn_outcome"] == outcome
    assert _stops(messages) == []
    assert getattr(model, "_stop_reason", "") != model_module.STOP_REASON_NO_PROGRESS


def test_a_bloodhound_answer_backed_by_a_graph_read_ends_the_request_as_completed(monkeypatch):
    """(a) The live shape: brief-less re-asks with new titles after an answer backed by a successful graph read."""
    result, model = _run_bh(monkeypatch, [_first_ask(), *_reasks(), _respond("never reached")],
                            _answers([_graph_read("graph-1"), AIMessage(content=PATHS_ANSWER)]))
    _assert_ended_on_the_answer(result, model, "completed")


def test_a_control_plane_scope_does_not_make_a_bloodhound_reask_a_new_step(monkeypatch):
    """(b) The same re-asks, each scoped to the control plane."""
    result, model = _run_bh(monkeypatch, [_first_ask(CONTROL_PLANE), *_reasks(CONTROL_PLANE),
                                          _respond("never reached")],
                            _answers([_graph_read("graph-1"), AIMessage(content=PATHS_ANSWER)]))
    _assert_ended_on_the_answer(result, model, "completed")


@pytest.mark.parametrize("first", [
    pytest.param([AIMessage(content=PATHS_ANSWER)], id="no-graph-read"),
    pytest.param([_graph_read("graph-1", "domain_info"), AIMessage(content=PATHS_ANSWER)], id="failed-graph-read"),
    pytest.param([_graph_read("graph-1", "adcs_info"), AIMessage(content=PATHS_ANSWER)], id="graph-api-error-result"),
])
def test_a_bloodhound_answer_without_a_successful_graph_read_ends_as_a_checkpoint(monkeypatch, first):
    """(e) Sage ends the request, but nothing in the graph backs the answer."""
    result, model = _run_bh(monkeypatch, [_first_ask(), *_reasks(), _respond("never reached")], _answers(first))
    _assert_ended_on_the_answer(result, model, "checkpoint")


def test_a_bloodhound_step_naming_a_guarded_tool_is_dispatched(monkeypatch):
    """(c) A BloodHound brief naming a guarded tool in allowed_tools is dispatched after an answer.

    Today BloodHound declares no guarded tool, so its handoff carries no brief and every BloodHound step is
    analysis. Declaring one gives its handoff the scoped signature (`_create_handoff_tool`), which is the only way a
    brief can name a guarded tool; this declares `file_upload` so the exemption is exercised at the real handoff.
    """
    declared = model_module._worker_guarded_tool_names
    monkeypatch.setattr(model_module, "_worker_guarded_tool_names",
                        lambda name: ("file_upload",) if name == "BloodHound" else declared(name))
    guarded = {"allowed_tools": ["file_upload"], "expected_result": "The collection is ingested."}
    script = [_first_ask(), _bh_handoff("Ingest the collection", "h2", guarded), _respond("Options are above.")]
    bloodhound = [_graph_read("graph-1"), AIMessage(content=PATHS_ANSWER), AIMessage(content="Nothing to ingest.")]
    result, model = _run_bh(monkeypatch, script, bloodhound)
    delegated = [m.additional_kwargs.get("_handoff_brief", {}).get("allowed_tools")
                 for m in result["bloodhound_messages"]
                 if isinstance(m, HumanMessage) and m.additional_kwargs.get("_delegated_to") == "BloodHound"]
    assert delegated == [[], ["file_upload"]]  # both dispatched; the second carries the guarded tool
    assert not [m for m in result["supervisor_messages"] if getattr(m, "name", "") == "transfer_to_BloodHound"
                and "not dispatched" in str(m.content)]
    assert str(_final(result["supervisor_messages"]).content) == "Options are above."


def test_a_bloodhound_reask_after_another_worker_is_dispatched(monkeypatch):
    """(d) BloodHound, then Mythic_Operator, then BloodHound: the record is only the step that just answered."""
    script = [_first_ask(), _handoff(READ, "op-1"), AIMessage(content="Callback 5 is alive."),
              _reasks()[0], _respond("Options are above.")]
    bloodhound = [_graph_read("graph-1"), AIMessage(content=PATHS_ANSWER),
                  _graph_read("graph-2"), AIMessage(content=PATHS_ANSWER)]
    result, model = _run_bh(monkeypatch, script, bloodhound)
    assert _bh_dispatches(result) == 2
    assert _steps(result["supervisor_messages"]) == 3
    assert str(_final(result["supervisor_messages"]).content) == "Options are above."
    assert _stops(result["supervisor_messages"]) == []


def test_bloodhound_analysis_steps_share_one_key():
    key = model_module._answered_step_key
    analysis = key("BloodHound", "Find a path", None)
    assert analysis
    assert key("BloodHound", "BloodHound path query to PARTNER.TEST", None) == analysis
    assert key("BloodHound", "Find a path", {"allowed_capabilities": ["control_plane"], "expected_result": "x"}) \
        == analysis
    assert key("BloodHound", "Find a path", {"targets": ["partner.test"], "expected_result": "x"}) == analysis
    # A step naming a guarded tool keeps the exact identity.
    guarded = key("BloodHound", "Ingest", {"allowed_tools": ["file_upload"], "expected_result": "x"})
    assert guarded and guarded != analysis
    # Other workers are unchanged.
    assert key("Mythic_Operator", "Find a path", None) == ""
