"""A target the operator named earlier in the same native chat generation can be named by a later brief.

Assembled cases drive the native chat driver and `Model.invoke`: the real graph, handoff tool, brief validator,
checkpoint and approval cards, with only the chat model scripted and the guarded executors replaced by inert
recorders. Network is disabled. No case seeds the historical-name source: it comes from the operator inputs the
transport tagged on earlier requests. Provenance cases call the production brief check on a real Model whose
Supervisor history holds the exact message shapes under test.

The earlier mention is reference memory only. It never proves a host exists, never resolves a callback, task or
credential, never changes the worker's instruction, and never stands in for the approval card.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import aiosqlite
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
import pytest

_HARNESS_PATH = Path(__file__).resolve().with_name("test_scripted_handoff.py")
_SPEC = importlib.util.spec_from_file_location("scripted_handoff_harness_target_history", _HARNESS_PATH)
_harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_harness)

from ai.langgraph import handoff_brief as hb  # noqa: E402
from ai.langgraph import model as model_module  # noqa: E402
from ai.langgraph.mythic_tools import MythicTools  # noqa: E402
from ai.langgraph.request_contract import build_request_contract  # noqa: E402
from sage_chat.headless import build_chat_request  # noqa: E402
from sage_chat.service import bind_channel_thread_id  # noqa: E402
from sage_chat.session import drop_channel_session  # noqa: E402
from tests.test_hitl_steer_current_intent import (  # noqa: E402
    _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response,
)

DOMAIN = "branch.example.test"
HOST = "dc01.branch.example.test"
DISCUSS = (f"Can you run a SharpHound collection for {DOMAIN} from callback 5? "
           "Discuss only; do not start or propose a guarded action.")
FOLLOW_UP = "Yes, run it"
COLLECT_BRIEF = {"callback_display_id": 5, "targets": [DOMAIN], "allowed_capabilities": ["collect-graph"],
                 "expected_result": f"BloodHound holds a collected attack graph for {DOMAIN}."}
BOUND_ACTION = {"callback_id": "5", "capability": "collect-graph", "domain": DOMAIN}
ISSUE = "issue_task_and_waitfor_task_output"
FILLER = ["Noted; nothing further ran."] * 6


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # the Model's production saver opens a relative sage.db

    def refuse(*_args, **_kwargs):
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _reply(text: str, index: int) -> AIMessage:
    return _call("respond_to_user", {"final_response": text}, index)


def _handoff(index: int, brief: dict | None = None, title: str = "Collect the attack graph") -> AIMessage:
    return _call("transfer_to_Mythic_Operator", {
        "handoff_title": title,
        "handoff_instruction": f"Run the SharpHound collection for {DOMAIN} on callback 5.",
        **(brief or COLLECT_BRIEF),
    }, index)


def _build(script: list, *, mode: str, autonomous_solve: bool, sqlite: bool, channel_id: int = 1):
    effects = SimpleNamespace(ran=[], issued=[])

    @tool
    async def execute_capability(action: dict | str, inputs: dict | str | None = None) -> str:
        """Run one catalog capability."""
        effects.ran.append({"action": action, "inputs": inputs})
        return '{"ok": true, "status": "ingested"}'

    @tool
    async def issue_task_and_waitfor_task_output(command: str, parameters: str | dict, callback_display_id: int) -> str:
        """Issue one callback task and wait for its output."""
        effects.issued.append({"command": command, "callback_display_id": callback_display_id})
        return "offline effect"

    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline target history",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="target-history", mode=mode, autonomous_solve=autonomous_solve,
        operation_id=1, channel_id=channel_id, apitoken_id=0,
    )
    sink = MagicMock()
    # The real supervised issue preparation, with the live-command and schema lookups answered offline.
    preparer = MythicTools(agent_task_id="offline-target-history")
    preparer.client = object()

    async def live_command(command, callback):
        return {"status": "available", "command": command}

    async def known_schema(command, callback):
        return []

    preparer._authenticate_live_command = live_command
    preparer._fetch_command_schema = known_schema
    sink._prepare_supervised_issue_action = preparer._prepare_supervised_issue_action
    sink.get_tools.side_effect = lambda names: [
        *([execute_capability] if "execute_capability" in names else []),
        *([issue_task_and_waitfor_task_output] if ISSUE in names else []),
    ]
    # Observed: one callback and its host. The domain is in no callback, foothold, graph fact or ledger row.
    sink._recon_snapshot_cache = {("list_callbacks", "all"): [{"id": 5, "host": "WORKSTATION-5"}]}
    sink._engagement_footholds = []
    sink._engagement_hops = []
    sink._engagement_graph_facts = []
    sink._ledger_view = lambda: SimpleNamespace(hops=[], graph_facts=[], corrected_hops=[], snapshot=None)
    sink._consult_attack_graph = None
    model.mythic_client = sink
    model._payload_names = []
    if not sqlite:
        model.memory = InMemorySaver()
    fake = _NativeScriptedModel(messages=iter(script))
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    return model, fake, effects


def _run(script: list, steps: list, *, mode: str = "supervised", autonomous_solve: bool = False,
         sqlite: bool = False, after=None, channel_id: int = 1) -> SimpleNamespace:
    """Send each step as its own native request on one channel, in order, on one live Model.

    A step is a prompt, or a dict: {"prompt", "operation_id"} or {"decision", "response"} (answers the latest card).
    `after(model)` runs inside the loop before the session is dropped and its result is returned as `.after`."""

    async def exercise():
        model, fake, effects = _build(script, mode=mode, autonomous_solve=autonomous_solve, sqlite=sqlite,
                                      channel_id=channel_id)
        chat = _NativeDriverChat(model)
        request = None
        try:
            for number, step in enumerate(steps, start=1):
                step = step if isinstance(step, dict) else {"prompt": step}
                request = build_chat_request(step.get("prompt", ""), channel_id=channel_id,
                                             operation_id=step.get("operation_id", 1), request_id=number,
                                             config={"mode": mode})
                bind_channel_thread_id(request, model)
                if "decision" in step:
                    request.InputResponse = _input_response(step["decision"], _cards(chat)[-1],
                                                            response=step.get("response", ""))
                await chat.chat(request)
            extra = await after(model) if after is not None else None
            return SimpleNamespace(model=model, fake=fake, chat=chat, ran=effects.ran, issued=effects.issued,
                                   after=extra)
        finally:
            if request is not None:
                await drop_channel_session(request, expected_model=model)
            if sqlite:
                await model.memory.conn.close()

    return asyncio.run(exercise())


def _briefs(model) -> list[Any]:
    return list(model._request_event_ledger.actual_events(kind="brief"))


def _sources(event: Any) -> list[dict] | None:
    raw = dict(getattr(event, "metadata", ()) or ()).get("operator_target_sources")
    return json.loads(raw) if raw is not None else None


def _actions(card: dict) -> list[dict]:
    return [action["arguments"]["action"] for action in card["data"]["actions"]]


def _delegated(model) -> list[str]:
    return [m.content for m in model.state.get("mythic_operator_messages", [])
            if isinstance(m, HumanMessage) and m.additional_kwargs.get("_delegated_to") == "Mythic_Operator"]


def _observed(model) -> hb.KnownScope:
    return hb.known_scope(model.mythic_client)


# --- the #182 reproduction ------------------------------------------------------------------------------------

@pytest.mark.parametrize("history", [True, False], ids=["with-history", "history-removed"])
def test_an_elliptical_follow_up_collects_the_domain_the_operator_named_earlier(monkeypatch, history):
    if not history:
        monkeypatch.setattr(model_module.Model, "_assisted_operator_target_inputs", lambda self: ())
    run = _run([_reply("I can collect that graph once you confirm; nothing has run.", 1), _handoff(2), *FILLER],
               [DISCUSS, FOLLOW_UP])

    events = _briefs(run.model)
    assert run.ran == [] and run.issued == []
    if not history:
        # The fix removed: the earlier mention no longer admits the domain, exactly as observed in #182.
        assert events[0].phase == "rejected" and f"targets: {DOMAIN} named neither" in events[0].content
        assert _cards(run.chat) == []
        return
    assert [event.phase for event in events] == ["accepted"]
    assert _sources(events[0]) == [{"target": DOMAIN, "request_id": "chat:1:request:1", "revision": 0}]
    assert DOMAIN not in _observed(run.model).domains | _observed(run.model).hosts
    # The worker received the exact current input, not R1 or an accumulation of both.
    assert _delegated(run.model) == [FOLLOW_UP]
    # Sage's bound proposal paused on the native approval card for exactly that domain and callback.
    [card] = _cards(run.chat)
    assert card["status"] == "pending" and _actions(card) == [BOUND_ACTION]


def test_exact_host_and_domain_named_earlier_admit_a_two_target_brief():
    brief = {"callback_display_id": 5, "targets": [HOST, DOMAIN], "allowed_tools": [ISSUE],
             "expected_result": "Local administrators."}
    run = _run([_reply("Understood; waiting for your go-ahead.", 1), _handoff(2, brief, "Enumerate admins"),
                AIMessage(content="No task was needed."), *FILLER],
               [f"Discuss enumerating local admins on {HOST} in {DOMAIN} from callback 5; do not act yet.",
                "Go ahead"])

    [event] = _briefs(run.model)
    assert event.phase == "accepted"
    assert _sources(event) == [{"target": HOST, "request_id": "chat:1:request:1", "revision": 0},
                               {"target": DOMAIN, "request_id": "chat:1:request:1", "revision": 0}]
    observed = _observed(run.model)
    assert not {HOST, DOMAIN} & (observed.hosts | observed.domains)
    assert _delegated(run.model) == ["Go ahead"]


def test_an_fqdn_named_two_requests_back_survives_a_short_label_discussion():
    run = _run([_reply("Ready when you are.", 1), _reply("It is the first label of the domain.", 2),
                _handoff(3), *FILLER],
               [DISCUSS, "What does branch refer to there?", FOLLOW_UP])

    [event] = _briefs(run.model)
    assert event.phase == "accepted"
    assert _sources(event) == [{"target": DOMAIN, "request_id": "chat:1:request:1", "revision": 0}]
    [card] = _cards(run.chat)
    assert _actions(card) == [BOUND_ACTION] and run.ran == []


@pytest.mark.parametrize("prompts", [
    [DISCUSS],
    ["Remember dc01 for later; do not act.", f"And the domain is {DOMAIN}; still do not act."],
], ids=["domain-only", "label-and-domain-apart"])
def test_an_unobserved_host_is_not_named_by_its_domain_or_by_its_parts(prompts):
    brief = {"callback_display_id": 5, "targets": [HOST], "allowed_tools": [ISSUE],
             "expected_result": "Local administrators."}
    script = [*[_reply("Noted.", i) for i in range(1, len(prompts) + 1)], _handoff(9, brief, "Enumerate admins"),
              *FILLER]
    run = _run(script, [*prompts, "Go ahead"])

    [event, *_] = _briefs(run.model)
    assert event.phase == "rejected" and f"targets: {HOST} named neither" in event.content
    assert _sources(event) is None
    if prompts == [DISCUSS]:
        # The guidance recognises the earlier domain to explain the rejection; it does not admit the host.
        assert "lies in a known domain but no observation names it" in event.content
    assert run.ran == [] and run.issued == [] and _cards(run.chat) == []


def test_a_name_only_the_assistant_said_is_not_operator_evidence():
    run = _run([_reply(f"I could collect {DOMAIN} from callback 5 if you want.", 1), _handoff(2), *FILLER],
               ["Discuss a SharpHound collection from callback 5; do not act.", FOLLOW_UP])

    [event, *_] = _briefs(run.model)
    assert event.phase == "rejected" and f"targets: {DOMAIN} named neither" in event.content
    assert run.ran == [] and _cards(run.chat) == []


def test_a_steer_names_a_target_for_a_later_follow_up_while_the_worker_gets_only_the_latest_input():
    whoami = {"callback_display_id": 5, "targets": ["WORKSTATION-5"], "allowed_tools": [ISSUE],
              "expected_result": "The callback identity."}
    steer = f"Do not run that. Later I want a collection for {DOMAIN}; for now just stop."
    run = _run([
        _handoff(1, whoami, "Check identity"),
        _call(ISSUE, {"callback_display_id": 5, "command": "whoami", "parameters": ""}, 2),
        AIMessage(content="Stopped; no task ran."),
        AIMessage(content="Stopped as asked."),
        _handoff(5), *FILLER,
    ], ["Check the identity of callback 5.", {"decision": "respond", "response": steer}, FOLLOW_UP])

    [event] = _briefs(run.model)  # the follow-up request's own brief
    assert event.phase == "accepted"
    # The steer is revision 1 of request 1; request 1's opening text never named the domain.
    assert _sources(event) == [{"target": DOMAIN, "request_id": "chat:1:request:1", "revision": 1}]
    assert _delegated(run.model) == ["Check the identity of callback 5.", FOLLOW_UP]
    assert run.issued == [] and run.ran == []
    assert _actions(_cards(run.chat)[-1]) == [BOUND_ACTION]


# --- approval stays the operator's -----------------------------------------------------------------------------

def test_rejecting_the_card_runs_nothing_and_a_replayed_approval_does_not_revive_it():
    run = _run([_reply("Ready.", 1), _handoff(2), *FILLER],
               [DISCUSS, FOLLOW_UP, {"decision": "reject"}, {"decision": "accept"}])

    assert run.ran == [] and run.issued == []
    assert len({json.dumps(_actions(card)) for card in _cards(run.chat)}) == 1


def test_a_plain_yes_while_the_card_is_pending_does_not_approve_it():
    run = _run([_reply("Ready.", 1), _handoff(2), *FILLER], [DISCUSS, FOLLOW_UP, "Yes"])

    assert run.ran == [] and run.issued == []


def test_the_exact_typed_approval_runs_the_bound_collection_once():
    run = _run([_reply("Ready.", 1), _handoff(2), *FILLER], [DISCUSS, FOLLOW_UP, {"decision": "accept"}])

    assert run.ran == [{"action": BOUND_ACTION, "inputs": None}]
    assert run.issued == []


# --- lanes and generations -------------------------------------------------------------------------------------

def test_conversation_admits_the_reference_but_has_no_guarded_execution():
    run = _run([_reply("Here is how it would work.", 1), _handoff(2), *FILLER], [DISCUSS, FOLLOW_UP],
               mode="conversation")

    [event, *_] = _briefs(run.model)
    assert event.phase == "accepted"
    assert _sources(event) == [{"target": DOMAIN, "request_id": "chat:1:request:1", "revision": 0}]
    assert run.ran == [] and run.issued == [] and _cards(run.chat) == []


def test_supervised_autonomous_solve_turns_stay_with_the_controller_and_never_read_mentions(monkeypatch):
    """Autonomous Solve hands each supervised chat turn to the controller, whose own approval seams are unchanged; no
    Supervisor handoff or brief runs there, so the earlier mention is never consulted."""
    consulted: list[int] = []
    original = model_module.Model._assisted_operator_target_inputs
    monkeypatch.setattr(model_module.Model, "_assisted_operator_target_inputs",
                        lambda self: consulted.append(1) or original(self))
    run = _run([_reply("Ready.", 1), _handoff(2), *FILLER], [DISCUSS, FOLLOW_UP], autonomous_solve=True)

    assert consulted == [] and run.fake.inputs == []
    assert not list(run.model._request_event_ledger.actual_events(kind="brief"))
    assert run.ran == [] and run.issued == [] and _cards(run.chat) == []


def test_a_replacement_model_on_the_same_channel_inherits_no_mentions_and_is_refused():
    first = _run([_reply("Ready.", 1), *FILLER], [DISCUSS])
    assert first.ran == []
    replacement = _run([_handoff(1), *FILLER], [FOLLOW_UP])

    # The existing restart/replacement refusal still answers before any model call or brief.
    assert replacement.fake.inputs == [] and replacement.model._request_event_ledger is None
    assert any("session was replaced" in str(emission.get("content")) for emission in replacement.chat.emissions)
    assert replacement.model._assisted_operator_target_inputs() == ()
    assert replacement.ran == [] and _cards(replacement.chat) == []


def test_a_new_channel_inherits_no_mentions_from_another():
    first = _run([_reply("Ready.", 1), *FILLER], [DISCUSS])
    assert first.ran == []
    other = _run([_handoff(1), *FILLER], [FOLLOW_UP], channel_id=2)

    [event, *_] = _briefs(other.model)
    assert event.phase == "rejected" and f"targets: {DOMAIN} named neither" in event.content
    assert other.ran == [] and _cards(other.chat) == []


def test_a_request_for_another_operation_does_not_use_this_operations_mentions():
    run = _run([_reply("Ready.", 1), _handoff(2), *FILLER], [DISCUSS, {"prompt": FOLLOW_UP, "operation_id": 2}])

    events = _briefs(run.model)
    assert events and events[0].phase == "rejected"
    assert run.ran == [] and _cards(run.chat) == []


# --- same-generation checkpoint --------------------------------------------------------------------------------

def test_mentions_survive_the_sqlite_checkpoint_once_each_and_outlive_a_summarized_model_view(monkeypatch, tmp_path):
    """Retention within one generation through the production saver; reopening the file is not reattachment."""
    summarized: list[int] = []
    original = model_module._LoggingSummarizationMiddleware.abefore_model

    async def summarize_supervisor_before_follow_up(self, state, runtime):
        messages = list(state.get("messages", []))
        human = [m for m in messages if isinstance(m, HumanMessage)]
        if human and human[-1].content == FOLLOW_UP and any(m.content == DISCUSS for m in human):
            # The Supervisor's model view loses every earlier turn, as a fired summarization would.
            summarized.append(len(messages))
            from langchain_core.messages import RemoveMessage
            from langgraph.graph.message import REMOVE_ALL_MESSAGES
            return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES),
                                 HumanMessage(content="Summary: earlier discussion about graph collection."),
                                 human[-1]]}
        return await original(self, state, runtime)

    monkeypatch.setattr(model_module._LoggingSummarizationMiddleware, "abefore_model",
                        summarize_supervisor_before_follow_up)

    async def reopen(model):
        await model.memory.conn.close()
        conn = aiosqlite.connect(tmp_path / "sage.db", check_same_thread=False)
        try:
            saver = AsyncSqliteSaver(await conn.__aenter__())
            saved = await saver.aget_tuple(model._graph_run_config(model._session_thread_id()))
            return saved.checkpoint["channel_values"]
        finally:
            await conn.close()
            model.memory.conn = SimpleNamespace(close=_noop)

    run = _run([_reply("Ready.", 1), _reply("It is the first label.", 2), _handoff(3), *FILLER],
               [DISCUSS, "What does branch refer to there?", FOLLOW_UP], sqlite=True, after=reopen)

    assert summarized, "the Supervisor's model view was not summarized"
    follow_up_view = next(view for view in run.fake.inputs
                          if any(getattr(m, "content", "") == FOLLOW_UP for m in view))
    assert not any(getattr(m, "content", "") == DISCUSS for m in follow_up_view)
    [event] = _briefs(run.model)
    assert event.phase == "accepted"
    assert _sources(event) == [{"target": DOMAIN, "request_id": "chat:1:request:1", "revision": 0}]
    for messages in (run.model.state["supervisor_messages"], run.after["supervisor_messages"]):
        inputs = [m for m in messages if isinstance(m, HumanMessage) and m.additional_kwargs.get("_operator_input")]
        assert [(m.additional_kwargs["_request_id"], m.content) for m in inputs] == [
            ("chat:1:request:1", DISCUSS), ("chat:1:request:2", "What does branch refer to there?"),
            ("chat:1:request:3", FOLLOW_UP)]


async def _noop() -> None:
    return None


# --- provenance at the brief check ------------------------------------------------------------------------------

CURRENT_ID = "chat:1:request:3"


def _operator(text: str, request_id: str = "chat:1:request:1", seq: int = 1, **extra: Any) -> HumanMessage:
    return HumanMessage(content=text, additional_kwargs={
        "_request_id": request_id, "_operator_input": True, "_operator_input_revision": 0, "_seq": seq, **extra})


def _seam(history: list, *, mode: str = "supervised", channel: str = "1", operation: str = "1",
          current: str = FOLLOW_UP, model_channel: int | None = 1, model_operation: int | None = 1):
    """A real Model with a native contract installed and `history` before the current request's input."""

    async def build():
        model = model_module.Model(
            provider="openai", model="gpt-4o-mini", system_prompt="offline provenance",
            config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
            task_id=0, agent_task_id="target-history-seam", mode=mode, operation_id=model_operation,
            channel_id=model_channel, apitoken_id=0,
        )
        await model.memory.conn.close()
        model.memory = InMemorySaver()
        model.mythic_client = SimpleNamespace(
            _recon_snapshot_cache={("list_callbacks", "all"): [{"id": 5, "host": "WORKSTATION-5"}]})
        contract = build_request_contract(request_id=f"chat:{channel}:request:3", channel_id=channel,
                                          operation_id=operation, mode=mode, autonomous_solve=False)
        model.install_request_contract(contract)
        model.state["supervisor_messages"] = [
            SystemMessage(content="supervisor"), *history, _operator(current, contract.request_id, seq=50)]
        return model

    return asyncio.run(build())


def _check(model, **brief: Any):
    fields = {"callback_display_id": 5, "targets": [DOMAIN], "allowed_capabilities": ["collect-graph"],
              "expected_result": "Graph collected.", **brief}
    return model._check_handoff_brief("Mythic_Operator", hb.HandoffBrief.from_args(**fields), "instruction")


def test_the_seam_admits_an_eligible_earlier_input():
    assert _check(_seam([_operator(DISCUSS)])) is None


@pytest.mark.parametrize("message", [
    AIMessage(content=DISCUSS),
    ToolMessage(content=DISCUSS, tool_call_id="t1", name="list_callbacks"),
    SystemMessage(content=f"Summary: the operator asked about {DOMAIN}."),
    HumanMessage(content=DISCUSS, additional_kwargs={"_request_id": "chat:1:request:1", "_seq": 1}),
    _operator(DISCUSS, _hide_from_stream=True),
    _operator(DISCUSS, _synthetic_nudge="knowledge_source_consult"),
    _operator(DISCUSS, _delegated_to="Mythic_Operator"),
    _operator(DISCUSS, _handoff_request_id="chat:1:request:1"),
    _operator(DISCUSS, _carried_context="command_reference"),
    _operator(DISCUSS, request_id="chat:2:request:1"),
    _operator(DISCUSS, request_id="chat:11:request:1"),
    _operator(DISCUSS, request_id="session:1:request:1"),
    _operator(DISCUSS, request_id="chat:1:request:"),
], ids=["assistant", "tool-output", "summary", "untagged", "hidden", "synthetic", "delegated", "handoff",
        "carried", "foreign-channel", "channel-prefix", "legacy-session", "no-request-number"])
def test_only_transport_tagged_inputs_of_this_channel_name_targets(message):
    errors, _stop = _check(_seam([message]))
    assert any(f"targets: {DOMAIN} named neither" in error for error in errors)


@pytest.mark.parametrize("history", [
    [_operator(DISCUSS, _operator_input_revision=-1)],
    [_operator(DISCUSS, _operator_input_revision="0")],
    [_operator(DISCUSS, _operator_input_revision=True)],
    [_operator(DISCUSS, _operator_steer=True)],
    [_operator("Opening.", seq=1), _operator(DISCUSS, seq=2, _operator_input_revision=1)],
    [_operator(DISCUSS, seq=2, _operator_input_revision=1, _operator_steer=True)],
    [_operator(DISCUSS, seq=1), _operator("A different opening.", seq=2)],
    [_operator(DISCUSS, seq=1), _operator(DISCUSS, seq=2)],
    [_operator(DISCUSS, _seq=None)],
    [_operator(DISCUSS, _seq="7")],
], ids=["negative", "string", "bool", "steer-at-opening", "unauthenticated-revision", "no-opening",
        "conflicting-opening", "two-records-one-revision", "seq-none", "seq-string"])
def test_malformed_or_conflicting_provenance_adds_no_names(history):
    errors, _stop = _check(_seam(history))
    assert any(f"targets: {DOMAIN} named neither" in error for error in errors)


def test_an_identical_retained_message_is_one_record():
    message = _operator(DISCUSS)
    model = _seam([message, message])
    assert model._assisted_operator_target_inputs() == (
        hb.OperatorTargetInput("chat:1:request:1", 0, DISCUSS), hb.OperatorTargetInput(CURRENT_ID, 0, FOLLOW_UP))
    assert _check(model) is None


def test_a_bad_request_does_not_poison_another_requests_names():
    model = _seam([_operator("Opening.", "chat:1:request:1", seq=1, _operator_input_revision=1),
                   _operator(DISCUSS, "chat:1:request:2", seq=2)])
    assert [item.request_id for item in model._assisted_operator_target_inputs()] == ["chat:1:request:2", CURRENT_ID]
    assert _check(model) is None


def test_earlier_mentions_resolve_no_callback_task_or_credential():
    model = _seam([_operator(f"Use callback 9 with task 77 and cred:12 against {DOMAIN}; do not act.")])

    errors, _stop = _check(model, callback_display_id=9)
    assert any("callback 9 is not one Sage has observed" in error for error in errors)
    for reference in ("task:77", "cred:12"):
        errors, _stop = _check(model, inputs_by_reference=[reference])
        assert any(f"`{reference}` does not resolve" in error for error in errors)
    assert model._turn_authority.bounded_callback_id == ""


@pytest.mark.parametrize("seam", [
    {"mode": "auto"},
    {"channel": "2"},
    {"operation": "2"},
    {"model_channel": None},
    {"model_operation": None},
], ids=["auto", "other-channel-contract", "other-operation-contract", "no-channel", "no-operation"])
def test_no_history_outside_this_native_channel_and_operation(seam):
    history = [_operator(DISCUSS, request_id=f"chat:{seam.get('channel', '1')}:request:1")]
    assert _seam(history, **seam)._assisted_operator_target_inputs() == ()


def test_a_malformed_current_input_disables_history():
    model = _seam([_operator(DISCUSS)])
    model.state["supervisor_messages"].append(_operator("Another opening.", CURRENT_ID, seq=51))
    errors, _stop = _check(model)
    assert any(f"targets: {DOMAIN} named neither" in error for error in errors)
