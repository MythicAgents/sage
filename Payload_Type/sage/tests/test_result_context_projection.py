"""Earlier execution reports reach an ordinary supervised model call as attributed user-role data.

Sage copies worker returns, task observations, step outcomes and its own carried context into the Supervisor's
history. Carried in the assistant role, those reserved report forms read to the model as its own way of reporting a
finished step, and recorded zero-step closings reproduced them. On every model call of an ordinary supervised
request, the turn-authority middleware now projects each such carrier into the user role with a Sage attribution,
leaving operator input, tool-call pairs and benign replies alone. The recorded carriers come from two retained
native supervised checkpoints (`fixtures/model_input_boundaries/reserved_result_contexts.json`, redacted); the real
boundary under test is `_TurnAuthorityInjectionMiddleware._augment` → `_MessageSanitizerMiddleware._sanitize` → the
OpenAI and Bedrock serializers. Scripted input proves what Sage sends a model, not what a model would write.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import re
import socket

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain.agents.middleware.types import ModelRequest
from langchain_openai.chat_models.base import _convert_message_to_dict
from langchain_aws.chat_models import bedrock_converse
from langchain_aws.chat_models.bedrock_converse import _messages_to_bedrock

from langchain_aws.chat_models.bedrock import _format_anthropic_messages

from ai.langgraph import model as model_module
from ai.langgraph.model import (
    Model, _MessageSanitizerMiddleware, _TurnAuthorityInjectionMiddleware, _earlier_steps_context,
    _message_content_as_text, _previous_reply_context,
)
from ai.langgraph.request_contract import build_request_contract
from ai.langgraph.step_bounds import RESERVED_RESULT_MARKERS
from ai.langgraph.turn_authority import TurnAuthority
from tests import test_partial_verdict_report as partial_module
from tests.test_hitl_steer_current_intent import _NativeScriptedModel, _call
from tests.test_partial_verdict_report import LEAD, NARRATIVES, _dcsync_client, _requests
from tests.test_supervised_catalog_step_binding import _run, _select

SEGMENTS = {segment["id"]: segment for segment in json.loads(
    (Path(__file__).parent / "fixtures/model_input_boundaries/reserved_result_contexts.json").read_text())["segments"]}
IDS = sorted(SEGMENTS)
RECORD_HEADER = "[Sage record"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def _message(row: dict):
    cls = {"ai": AIMessage, "human": HumanMessage}[row["type"]]
    fields = {"content": row["content"], "additional_kwargs": dict(row.get("additional_kwargs") or {})}
    if row.get("name"):
        fields["name"] = row["name"]
    return cls(**fields)


def _messages(segment_id: str) -> list:
    return [_message(row) for row in SEGMENTS[segment_id]["messages"]]


def _recorded_assistant_marker_count(segment_id: str) -> int:
    return sum(row["type"] == "ai" and any(marker in row["content"] for marker in RESERVED_RESULT_MARKERS)
               for row in SEGMENTS[segment_id]["messages"])


def _model(request_id: str, *, mode: str = "supervised", autonomous_solve: bool = False) -> Model:
    model = object.__new__(Model)
    model._turn_authority = TurnAuthority(mode="observe", request_id=request_id)
    model._objective_contract_progress = lambda: {}
    model._supervised_execution_context = lambda: ""
    model._supervised_step_budget_line = lambda *args: ""
    model._request_contract = build_request_contract(
        request_id=request_id, channel_id=1, operation_id=1, mode=mode, autonomous_solve=autonomous_solve)
    return model


def _dict_text(serialized: dict) -> str:
    content = serialized.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content or "")


def _bedrock_text(message: dict) -> str:
    parts: list[str] = []
    for block in message.get("content", []):
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            parts.append(block["text"])
        result = block.get("toolResult")
        if isinstance(result, dict):
            for inner in result.get("content", []):
                if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                    parts.append(inner["text"])
    return "\n".join(parts)


def _system_text(message) -> str:
    if message is None:
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))


def _outgoing(messages: list, *, mode: str = "supervised", request_id: str = "chat:1:request:1",
              autonomous_solve: bool = False, model: Model | None = None, agent_name: str = "Supervisor"):
    model = model or _model(request_id, mode=mode, autonomous_solve=autonomous_solve)
    injection = _TurnAuthorityInjectionMiddleware(model, agent_name=agent_name)
    sanitizer = _MessageSanitizerMiddleware(model)
    request = ModelRequest(
        model=FakeListChatModel(responses=["unused"]),
        messages=model._sanitize_messages(messages),
        system_message=SystemMessage(content="Trusted supervised instructions"),
    )
    return sanitizer._sanitize(injection._augment(request))


def _assistant_reports(messages: list) -> list:
    """Assistant messages without tool calls that carry a reserved report form."""
    return [message for message in messages if isinstance(message, AIMessage) and not message.tool_calls and any(
        marker in _message_content_as_text(message.content) for marker in RESERVED_RESULT_MARKERS)]


def _records(messages: list) -> list:
    return [message for message in messages
            if isinstance(message, HumanMessage) and message.additional_kwargs.get("_sage_record")]


def _header_token(message) -> str:
    match = re.match(r"\[Sage record ([0-9a-f]{16}) · ", _message_content_as_text(message.content))
    assert match, _message_content_as_text(message.content)[:120]
    return match.group(1)


def _assistant_markers_openai(outgoing) -> int:
    count = 0
    for message in outgoing.messages:
        serialized = _convert_message_to_dict(message)
        if serialized.get("role") == "assistant" and any(
                marker in _dict_text(serialized) for marker in RESERVED_RESULT_MARKERS):
            count += 1
    return count


def _assistant_markers_bedrock(outgoing) -> int:
    bedrock_messages, _ = _messages_to_bedrock(list(outgoing.messages), None)
    return sum(message["role"] == "assistant" and any(
        marker in _bedrock_text(message) for marker in RESERVED_RESULT_MARKERS)
        for message in bedrock_messages)


# --- (a) the reserved forms leave the assistant role at the real provider boundary ---------------------------------

@pytest.mark.parametrize("segment", IDS)
def test_no_reserved_form_reaches_the_model_in_the_assistant_role_openai(segment):
    assert _recorded_assistant_marker_count(segment) > 0           # the exposure this line removes exists
    assert _assistant_markers_openai(_outgoing(_messages(segment))) == 0


@pytest.mark.parametrize("segment", IDS)
def test_no_reserved_form_reaches_the_model_in_the_assistant_role_bedrock(segment):
    """Bedrock merges consecutive user runs, so a projected carrier joins the user turn around it, never an
    assistant one."""
    assert _assistant_markers_bedrock(_outgoing(_messages(segment))) == 0


# --- (b) every record survives, attributed, and the attribution reaches trusted system context --------------------

@pytest.mark.parametrize("segment", IDS)
def test_every_record_survives_and_is_attributed(segment):
    outgoing = _outgoing(_messages(segment))
    blob = "\n".join(_dict_text(_convert_message_to_dict(message)) for message in outgoing.messages)
    for row in SEGMENTS[segment]["messages"]:
        assert row["content"] in blob, f"record {row['index']} lost its text"
    assert RECORD_HEADER in blob                                   # projected carriers wear a Sage record header
    assert RECORD_HEADER in _system_text(outgoing.system_message)  # the attribution paragraph explains that header


def test_a_benign_supervisor_reply_keeps_the_assistant_role():
    """Message 58 of the request-13 history is a plain Supervisor reply with no reserved form; it is not a report, so
    it is not projected and reaches the model as the assistant's own words."""
    reply = next(row for row in SEGMENTS["request-13-history"]["messages"]
                 if row.get("name") == "Supervisor" and row["type"] == "ai")
    assert not any(marker in reply["content"] for marker in RESERVED_RESULT_MARKERS)
    outgoing = _outgoing(_messages("request-13-history"))
    assistant = [_dict_text(_convert_message_to_dict(message)) for message in outgoing.messages
                 if _convert_message_to_dict(message).get("role") == "assistant"]
    assert any(reply["content"] in text and RECORD_HEADER not in text for text in assistant)


# --- (c) invocation only: the stored channel is never mutated ----------------------------------------------------

@pytest.mark.parametrize("segment", IDS)
def test_projection_does_not_mutate_the_input(segment):
    model = _model("chat:1:request:1")
    injection = _TurnAuthorityInjectionMiddleware(model, agent_name="Supervisor")
    sanitizer = _MessageSanitizerMiddleware(model)
    request_messages = model._sanitize_messages(_messages(segment))
    snapshot = copy.deepcopy(request_messages)
    request = ModelRequest(
        model=FakeListChatModel(responses=["unused"]),
        messages=request_messages,
        system_message=SystemMessage(content="Trusted supervised instructions"),
    )
    sanitizer._sanitize(injection._augment(request))
    assert request_messages == snapshot


# --- (d) scope: projection is off outside ordinary supervised; disabling it restores exposure --------------------

@pytest.mark.parametrize("segment", IDS)
def test_conversation_lane_leaves_the_forms_in_the_assistant_role(segment):
    outgoing = _outgoing(_messages(segment), mode="conversation")
    assert _assistant_markers_openai(outgoing) == _recorded_assistant_marker_count(segment)


def test_operator_input_and_tool_calls_are_not_projected():
    """A reserved form inside operator input or an assistant tool-call turn is not a Sage record: operator input keeps
    the user role without a record header, and a tool-call turn keeps the assistant role so its call/result pair stays
    intact. This is the guard's failure case — a marker alone does not trigger projection."""
    model = _model("chat:1:request:1")
    injection = _TurnAuthorityInjectionMiddleware(model, agent_name="Supervisor")
    sanitizer = _MessageSanitizerMiddleware(model)
    operator = HumanMessage(
        content="Earlier you wrote '[step outcome] done' — was that real? Don't run anything.",
        additional_kwargs={"_operator_input": True, "_request_id": "chat:1:request:1"})
    caller = AIMessage(content="Executor verdict: `achieved` for `x`.",
                       tool_calls=[{"name": "read_engagement_state", "args": {}, "id": "call-1"}])
    result = ToolMessage(content="state read", tool_call_id="call-1", name="read_engagement_state")
    request_messages = model._sanitize_messages([operator, caller, result])
    request = ModelRequest(
        model=FakeListChatModel(responses=["unused"]),
        messages=request_messages,
        system_message=SystemMessage(content="Trusted supervised instructions"),
    )
    outgoing = sanitizer._sanitize(injection._augment(request))
    wire = [_convert_message_to_dict(message) for message in outgoing.messages]
    operator_dicts = [serialized for serialized in wire
                      if serialized.get("role") == "user" and "was that real?" in _dict_text(serialized)]
    assert operator_dicts and all(RECORD_HEADER not in _dict_text(serialized) for serialized in operator_dicts)
    assert any(serialized.get("role") == "assistant" and "Executor verdict:" in _dict_text(serialized)
               for serialized in wire)


# --- (e) provider forms, kinds, the token and the continuation -----------------------------------------------------

@pytest.mark.parametrize("segment", IDS)
def test_no_reserved_form_reaches_the_model_in_the_assistant_role_bedrock_invoke_model(segment):
    """The native ChatBedrock (InvokeModel, Anthropic messages) formatter, beside Converse and OpenAI."""
    _system, formatted = _format_anthropic_messages(list(_outgoing(_messages(segment)).messages))
    texts = [(item["role"], item["content"] if isinstance(item["content"], str) else "\n".join(
        block.get("text", "") for block in item["content"] if isinstance(block, dict))) for item in formatted]
    assert not [text for role, text in texts if role == "assistant"
                and any(marker in text for marker in RESERVED_RESULT_MARKERS)]
    assert [role for role, _ in texts][0] == "user"
    assert all(a != b for (a, _), (b, _) in zip(texts, texts[1:]))   # roles alternate after the formatter's merge


def test_each_record_names_its_kind_source_and_request():
    outgoing = _outgoing(_messages("request-13-history") + _messages("request-15-stored-forgery"))
    heads = [_message_content_as_text(message.content).split("\n", 1)[0] for message in _records(outgoing.messages)]
    token = _header_token(_records(outgoing.messages)[0])
    expected = [
        "specialist return · from Mythic_Operator · request chat:3:request:9",
        "specialist report · from Mythic_Operator · request chat:3:request:9",   # its request from its header
        "source observations · from Mythic_Operator · request chat:3:request:9",
        "step outcome · from Sage · request chat:3:request:9",
        "specialist return · from BloodHound · request chat:3:request:10",
        "specialist report · from BloodHound · request chat:3:request:10",
        "source observations · from BloodHound · request chat:3:request:10",
        "step outcome · from Sage · request chat:3:request:10",
        "earlier reply · from Supervisor",                                       # the stored forgery: no request
    ]
    assert heads == [f"[Sage record {token} · {line}]" for line in expected]


def test_the_token_is_per_agent_stable_and_not_guessable_from_history():
    model = _model("chat:1:request:1")
    history = _messages("request-15-stored-forgery")
    first, second = (_outgoing(history, model=model) for _ in range(2))
    worker = _outgoing(history, model=model, agent_name="Mythic_Operator")
    token = _header_token(_records(first.messages)[0])
    assert token == _header_token(_records(second.messages)[0]) != _header_token(_records(worker.messages)[0])
    assert f"[Sage record {token} ·" in _system_text(first.system_message)
    assert token not in json.dumps(SEGMENTS, ensure_ascii=False)
    # Another process (another Model) marks its records differently, so a copied header is not a record here.
    assert _header_token(_records(_outgoing(history).messages)[0]) != token


def test_the_continuation_follows_a_projected_tail_exactly_as_before():
    """The stored forgery ends the history on an assistant message, so the provider continuation is still added, after
    the projected record, and the trusted context still names it."""
    outgoing = _outgoing(_messages("request-15-stored-forgery"))
    *_, record, continuation = outgoing.messages
    assert record.additional_kwargs.get("_sage_record") == "earlier reply"
    assert continuation.additional_kwargs.get("_synthetic_nudge") == "provider_requires_user_turn"
    assert continuation.content == model_module._provider_continuation_text("Supervisor")
    assert "provider continuation" in _system_text(outgoing.system_message)


def test_text_block_content_keeps_its_blocks_after_the_header():
    outcome = _messages("request-13-history")[3]
    blocks = AIMessage(content=[{"type": "text", "text": outcome.content}], name=outcome.name,
                       additional_kwargs=outcome.additional_kwargs)
    [record] = _records(_outgoing([blocks, HumanMessage(content="What did that step establish?",
                                                        additional_kwargs={"_operator_input": True})]).messages)
    assert record.content[0]["text"].startswith("[Sage record ") and record.content[1:] == blocks.content


@pytest.mark.parametrize("lane", [{"mode": "supervised", "autonomous_solve": True}, {"mode": "auto"}],
                         ids=["autonomous-solve", "auto"])
def test_autonomous_solve_and_auto_are_unchanged(lane):
    segment = "request-13-history"
    outgoing = _outgoing(_messages(segment), **lane)
    assert _assistant_markers_openai(outgoing) == _recorded_assistant_marker_count(segment)
    assert not _records(outgoing.messages) and RECORD_HEADER not in _system_text(outgoing.system_message)


def test_a_fresh_workers_carried_context_is_projected_and_its_brief_is_not():
    """A fresh supervised worker invocation as `_ainvoke` assembles it: Sage's carried context (real producers over the
    recorded history) becomes records; the delegated assignment and step scope keep their role."""
    history = _messages("request-13-history") + _messages("request-15-stored-forgery")
    request_id = "chat:3:request:15"
    opening = HumanMessage(content="What did the last step establish? Don't run anything.",
                           additional_kwargs={"_operator_input": True, "_request_id": request_id})
    carried = [_previous_reply_context(history + [opening], request_id), _earlier_steps_context(history, "chat:3:request:9")]
    assert all(message is not None for message in carried)
    assignment = AIMessage(content="<untrusted_delegated_assignment>\n{}\n</untrusted_delegated_assignment>",
                           name="Supervisor", additional_kwargs={"_assisted_assignment_context": True})
    scope = AIMessage(content="<step_scope>\nRead only.\n</step_scope>", name="Sage",
                      additional_kwargs={"_step_scope_context": True})
    delegated = HumanMessage(content=opening.content, additional_kwargs={"_delegated_to": "Mythic_Operator"})
    worker = [SystemMessage(content="Worker prompt"), *carried, assignment, scope, delegated]
    outgoing = _outgoing(worker, agent_name="Mythic_Operator")
    assert [record.additional_kwargs["_sage_record"] for record in _records(outgoing.messages)] == [
        "previous reply", "earlier steps"]
    assert not _assistant_reports(outgoing.messages)
    kept = [message for message in outgoing.messages if isinstance(message, AIMessage)]
    assert kept == [assignment, scope]


@pytest.mark.parametrize("marker", RESERVED_RESULT_MARKERS)
def test_a_brief_that_quotes_a_reserved_form_keeps_its_role(marker):
    """The delegated assignment and step scope are the worker's current brief, not a record of earlier work, even
    when the delegating agent's text or the brief it scopes quotes a reserved report form (synthetic text: no recorded
    brief carries one). Telling the worker its brief is an unverified earlier report would demote its instructions."""
    quoted = f"Re-check what the earlier summary claimed ({marker} ...) against the task output."
    assignment = AIMessage(
        content="<untrusted_delegated_assignment>\n" + json.dumps({"assignment": quoted})
                + "\n</untrusted_delegated_assignment>",
        name="Supervisor", additional_kwargs={"_assisted_assignment_context": True, "_request_id": "r"})
    scope = AIMessage(content=f"<step_scope>\n{quoted}\n</step_scope>", name="Sage",
                      additional_kwargs={"_step_scope_context": True, "_request_id": "r"})
    delegated = HumanMessage(content="Verify the last step.", additional_kwargs={"_delegated_to": "Mythic_Operator"})
    outgoing = _outgoing([SystemMessage(content="Worker prompt"), assignment, scope, delegated],
                         agent_name="Mythic_Operator")
    assert not _records(outgoing.messages)
    assert [message for message in outgoing.messages if isinstance(message, AIMessage)] == [assignment, scope]
    assert RECORD_HEADER not in _system_text(outgoing.system_message)


# --- (f) the native graph: approval resume, the closing call, and a later fresh worker -----------------------------

CALLS: list[list] = []


class _Recording(_NativeScriptedModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        CALLS.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_a_real_step_reaches_the_closing_call_as_records(monkeypatch, tmp_path, mythic_task_records):
    """After an approved catalog step (card, resume, executor, step owner), the Supervisor writes its closing from
    records, not assistant-voice reports; the stored channel and the operator's closing are unchanged."""
    mt, split = _dcsync_client(monkeypatch, tmp_path / "state", mythic_task_records)
    narrative = NARRATIVES["run5-request8"]
    run = asyncio.run(_run(mt, split, [_select("dcsync-krbtgt"), AIMessage(content=narrative)]))
    assert len(run.cards) == 1 and [row["command_name"] for row in run.issued] == ["dcsync"]
    closing_call = next(messages for messages in reversed(run.fake.inputs)
                        if any("[step outcome]" in _message_content_as_text(m.content) for m in messages))
    assert not _assistant_reports(closing_call)
    records = _records(closing_call)
    assert {"specialist return", "specialist report", "source observations", "step outcome"} <= {
        record.additional_kwargs["_sage_record"] for record in records}
    assert "Executor verdict: `partial` for `dcsync-krbtgt`" in "\n".join(str(record.content) for record in records)
    system = next(message for message in closing_call if isinstance(message, SystemMessage))
    assert f"[Sage record {_header_token(records[0])} ·" in _system_text(system)
    stored = run.values["supervisor_messages"]
    assert any(isinstance(m, AIMessage) and m.additional_kwargs.get("_step_outcome") for m in stored)
    assert not any((m.additional_kwargs or {}).get("_sage_record") for m in stored)
    terminal = "\n".join(str(e.get("content") or "") for e in run.terminals).strip()
    assert terminal.startswith(LEAD) and narrative.strip() in terminal    # the partial line still leads, unchanged


def test_a_later_requests_fresh_worker_gets_the_previous_reply_as_a_record(monkeypatch, tmp_path, mythic_task_records):
    """Request 2 sends a fresh read step: its worker call carries Sage's previous reply (request 1's closing) as a
    record it can refer to, while the step scope stays Sage's assistant-role brief."""
    CALLS.clear()
    monkeypatch.setattr(partial_module, "_NativeScriptedModel", _Recording)
    mt, split = _dcsync_client(monkeypatch, tmp_path / "state", mythic_task_records)
    narrative = NARRATIVES["run5-request8"]
    second = "Read the live callbacks again. Don't change anything."
    brief = {"handoff_title": "read callbacks", "handoff_instruction": second,
             "allowed_capabilities": ["control_plane"], "expected_result": "The live callbacks."}
    run = asyncio.run(_requests(mt, split, [
        _select("dcsync-krbtgt"), AIMessage(content=narrative),
        _call("transfer_to_Mythic_Operator", brief, 5), _call("list_callbacks", {}, 6),
        AIMessage(content="Callbacks 13 and 14 are live."), AIMessage(content="Both callbacks are live.")],
        prompts=[second]))
    worker_call = next(messages for messages in CALLS
                       if any("<previous_reply>" in _message_content_as_text(m.content) for m in messages))
    [previous] = [m for m in worker_call if "<previous_reply>" in _message_content_as_text(m.content)]
    assert isinstance(previous, HumanMessage) and previous.additional_kwargs["_sage_record"] == "previous reply"
    assert narrative.strip()[-200:] in _message_content_as_text(previous.content)
    scope = [m for m in worker_call if "<step_scope>" in _message_content_as_text(m.content)]
    assert scope and all(isinstance(m, AIMessage) for m in scope)
    assert not _assistant_reports(worker_call)
    later = "\n".join(str(e.get("content") or "") for e in run.emissions[run.marks[0]:])
    assert "Both callbacks are live." in later


# --- (g) Bedrock Converse serializes the same history: Sage's metadata stays out of its run merge ----------------
#
# Converse (`_messages_to_bedrock`) merges consecutive same-role messages with `merge_message_runs`, which merges their
# `additional_kwargs`; two recorded step outcomes (`active_seconds` 0.0 and 25.1) in one run raised a TypeError there.
# Sage's adapter patch (`_apply_converse_metadata_patch`, applied with every model Sage builds) hands that serializer
# copies without Sage's metadata; the outgoing request, callbacks and the stored messages keep it.

OUTCOME_ROWS = [row for row in SEGMENTS["request-13-history"]["messages"]
                if (row.get("additional_kwargs") or {}).get("_step_outcome")]
OPERATOR_ROWS = [row for row in SEGMENTS["request-13-history"]["messages"]
                 if (row.get("additional_kwargs") or {}).get("_operator_input")]
SAGE_KEY = re.compile(r"_(?!_)")          # Sage's metadata keys: one leading underscore (providers own `__x__`)


def _sage_keys(message) -> list[str]:
    keys = [key for key in (message.additional_kwargs or {}) if SAGE_KEY.match(key)]
    return keys + (["sage_model"] if "sage_model" in (message.response_metadata or {}) else [])


def _converse(outgoing, monkeypatch) -> tuple[list, list[dict]]:
    """The messages Converse's run merge receives, and the Converse messages it produces, through the serializer as
    `ChatBedrockConverse` calls it once Sage has built a model (the patched module attribute)."""
    model_module._apply_converse_metadata_patch()
    seen: list[list] = []
    real = bedrock_converse.merge_message_runs

    def spy(messages, *args, **kwargs):
        seen.append(list(messages))
        return real(messages, *args, **kwargs)

    monkeypatch.setattr(bedrock_converse, "merge_message_runs", spy)
    wire, _system = bedrock_converse._messages_to_bedrock(list(outgoing.messages), None)
    return seen[-1], wire


def _history(shape: str) -> list:
    first, second = (_message(row) for row in OUTCOME_ROWS)
    opening, later = (_message(row) for row in OPERATOR_ROWS)
    return [opening, first, second] if shape == "adjacent" else [opening, first, later, second]


def _in_order(wire: list[dict], texts: list[tuple[str, str]]) -> None:
    """Each (role, text) appears in a Converse message of that role, after the previous one."""
    position = (-1, -1)
    for role, text in texts:
        found = next(((index, offset) for index, message in enumerate(wire)
                      for offset in [_bedrock_text(message).find(text)]
                      if offset >= 0 and message["role"] == role and (index, offset) > position), None)
        assert found, (role, text[:80])
        position = found


def test_the_serializer_patch_is_applied_with_every_model_sage_builds():
    """`_get_base_chat_model` passes every model through `_patch_model_for_bedrock`; that installs the wrapper once."""
    model_module._patch_model_for_bedrock(FakeListChatModel(responses=["unused"]))
    first = bedrock_converse._messages_to_bedrock
    model_module._patch_model_for_bedrock(FakeListChatModel(responses=["unused"]))
    assert getattr(first, "_sage_metadata_removed", False) and bedrock_converse._messages_to_bedrock is first
    assert not getattr(first.__wrapped__, "_sage_metadata_removed", False)


@pytest.mark.parametrize("mode", ["conversation", "supervised"])
@pytest.mark.parametrize("shape", ["adjacent", "operator-separated"])
def test_converse_serializes_recorded_step_outcomes(monkeypatch, shape, mode):
    """The recorded pair: adjacent outcomes form one assistant run (conversation) or one projected user run
    (supervised); separated by an operator turn they stay apart unprojected and form one user run when projected.
    Both outcomes and the operator turns arrive in order, in their roles, and the stored messages are unchanged."""
    assert [row["additional_kwargs"]["_step_outcome"]["active_seconds"] for row in OUTCOME_ROWS] == [0.0, 25.1]
    model = _model("chat:3:request:13", mode=mode)
    stored = model._sanitize_messages(_history(shape))
    snapshot = copy.deepcopy(stored)
    injection = _TurnAuthorityInjectionMiddleware(model, agent_name="Supervisor")
    request = ModelRequest(model=FakeListChatModel(responses=["unused"]), messages=stored,
                           system_message=SystemMessage(content="Trusted supervised instructions"))
    outgoing = _MessageSanitizerMiddleware(model)._sanitize(injection._augment(request))
    merged_input, wire = _converse(outgoing, monkeypatch)

    assert not [key for message in merged_input for key in _sage_keys(message)]
    # Only the serializer's input is a copy: the request the model and its callbacks receive keeps Sage's metadata.
    assert sum("_step_outcome" in message.additional_kwargs for message in outgoing.messages) == 2
    report_role = "user" if mode == "supervised" else "assistant"
    expected = [("user", OPERATOR_ROWS[0]["content"]), (report_role, OUTCOME_ROWS[0]["content"])]
    if shape == "operator-separated":
        expected.append(("user", OPERATOR_ROWS[1]["content"]))
    expected.append((report_role, OUTCOME_ROWS[1]["content"]))
    _in_order(wire, expected)
    if mode == "supervised":
        assert all(RECORD_HEADER in _bedrock_text(message) for message in wire
                   if OUTCOME_ROWS[0]["content"] in _bedrock_text(message)
                   or OUTCOME_ROWS[1]["content"] in _bedrock_text(message))
    assert stored == snapshot and stored[1].additional_kwargs["_step_outcome"]["active_seconds"] == 0.0


def test_converse_serializes_a_second_incompatible_sage_field(monkeypatch):
    """Synthetic control: `_deferred_task_reconciled` is written as `True` (the pending summary) and as a dict (the
    recorded reconciliation); in one assistant run the two types collide in the merge. Not only `active_seconds`."""
    recorded = _message(next(row for row in SEGMENTS["request-13-history"]["messages"]
                             if isinstance((row.get("additional_kwargs") or {}).get("_deferred_task_reconciled"), dict)))
    pending = AIMessage(content="Synthetic pending summary.", name="Supervisor",
                        additional_kwargs={"_deferred_task_reconciled": True, "_request_id": "chat:3:request:9"})
    operator = _message(OPERATOR_ROWS[0])
    stored = [operator, pending, recorded]
    snapshot = copy.deepcopy(stored)
    merged_input, wire = _converse(_outgoing(stored, mode="conversation"), monkeypatch)
    assert not [key for message in merged_input for key in _sage_keys(message)]
    _in_order(wire, [("user", operator.content), ("assistant", pending.content), ("assistant", recorded.content)])
    assert stored == snapshot


def test_the_serializer_copy_keeps_content_provider_fields_reasoning_and_tool_ids(monkeypatch):
    """What the copy must keep, at each consumer: text blocks, the trusted attribution header, provider-owned kwargs
    (`refusal`, `__openai_role__`), same-model reasoning, and tool-call/result IDs. A message without Sage metadata
    reaches the merge as the same object. Synthetic shapes beside the recorded rows."""
    model = _model("chat:3:request:13")
    model.model = "anthropic.claude-test"
    outcome = _message(OUTCOME_ROWS[0])
    blocks = AIMessage(content=[{"type": "text", "text": outcome.content}], name="Sage",
                       additional_kwargs=copy.deepcopy(outcome.additional_kwargs))
    reply = _message(next(row for row in SEGMENTS["request-13-history"]["messages"] if "refusal" in row["additional_kwargs"]))
    reasoning = AIMessage(
        content=[{"type": "reasoning_content", "reasoning_content": {"text": "same-model thought", "signature": "sig"}},
                 {"type": "text", "text": "Checking callbacks."}],
        tool_calls=[{"name": "list_callbacks", "args": {}, "id": "call-1"}],
        additional_kwargs={"_seq": 7, "_request_id": "chat:3:request:13"},
        response_metadata={"sage_model": "anthropic.claude-test", "model_name": "anthropic.claude-test"})
    result = ToolMessage(content="callbacks 13 and 14", tool_call_id="call-1", name="list_callbacks",
                         additional_kwargs={"_seq": 8, "_task_output_collection_request_id": "chat:3:request:13"})
    operator = _message(OPERATOR_ROWS[1])
    developer = SystemMessage(content="Developer note.", additional_kwargs={"__openai_role__": "developer"})
    stored = [developer, operator, blocks, reply, reasoning, result]
    snapshot = copy.deepcopy(stored)
    outgoing = _outgoing(stored, model=model)
    sent = outgoing.messages
    merged, wire = _converse(outgoing, monkeypatch)

    assert not [key for message in merged for key in _sage_keys(message)]
    assert merged[0] is developer and _convert_message_to_dict(sent[0])["role"] == "developer"
    record = next(message for message in merged if isinstance(message, HumanMessage)
                  and RECORD_HEADER in _message_content_as_text(message.content))
    assert record.content[0]["text"].startswith(f"[Sage record {_header_token(record)} · step outcome")
    assert f"[Sage record {_header_token(record)} ·" in _system_text(outgoing.system_message)
    assert [block for block in record.content if block.get("text") == outcome.content] == blocks.content
    assert any(message is reply for message in merged) and reply.additional_kwargs == {"refusal": None}
    [kept_reasoning] = [message for message in merged if isinstance(message, AIMessage) and message.tool_calls]
    assert kept_reasoning.content == reasoning.content and kept_reasoning.tool_calls == reasoning.tool_calls
    assert kept_reasoning.response_metadata == {"model_name": "anthropic.claude-test"}

    blocks_out = [block for message in wire for block in message["content"]]
    assert any("reasoningContent" in block for block in blocks_out)
    assert [block["toolUse"]["toolUseId"] for block in blocks_out if "toolUse" in block] == ["call-1"]
    assert [block["toolResult"]["toolUseId"] for block in blocks_out if "toolResult" in block] == ["call-1"]
    openai = [_convert_message_to_dict(message) for message in sent]
    assert [call["id"] for item in openai for call in item.get("tool_calls", [])] == ["call-1"]
    assert [item["tool_call_id"] for item in openai if item["role"] == "tool"] == ["call-1"]
    assert stored == snapshot
    assert stored[4].response_metadata["sage_model"] == "anthropic.claude-test"
    assert stored[5].additional_kwargs["_task_output_collection_request_id"] == "chat:3:request:13"
