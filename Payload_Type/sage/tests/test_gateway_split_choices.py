"""A reply the provider splits across Chat Completions choices reaches Sage as one message.

Some OpenAI-compatible gateways send one reply's text and its tool call as separate choices of one response, although
Sage never asks for more than one completion. LangChain keeps only the first choice, so the call was lost. Sage's
OpenAI adapter folds such a reply into one message; every other response shape stays as it was.

Evidence, labelled:

* **Recorded, rebuilt:** four gateway replies split as ``[text, call(s)]``, rebuilt from Phoenix spans (battery rows
  ``*_split_text_then_*`` in ``fixtures/model_input_boundaries/provider_wire_round_trips.json``). Each row's expected
  message is what the adapter makes of the same reply in one choice. They replay through both of Sage's model
  factories, the installed OpenAI SDK and ``ChatOpenAI``. Only the network transport is replaced.
* **Recorded:** single-choice replies from the gateway and from OpenAI (the ``*_single_choice_*`` rows).
* **Synthetic:** every other accepted form and near-match below, and the replay prompt. The assembled cases drive the
  real native-chat handler, ``Model``, graph, HITL middleware and card; the provider transport and the Mythic tool
  and client are stand-ins.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import langchain.chat_models
import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_openai.chat_models.base import BaseChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

import sage_chat.service as service
from ai import openai_api
from ai.langgraph import model as model_module
from ai.langgraph.model import Model
from ai.langgraph.mythic_tools import MythicTools
from sage_chat import session as chat_session
from sage_chat.config import ResolvedLLMProfile, init_chat_model_from_profile
from sage_chat.headless import HeadlessSageChat, build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session, get_channel_session, put_channel_session
from tests import openai_responses_harness as harness
from tests import provider_wire_replay as replay

FIXTURE = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/provider_wire_round_trips.json")
                     .read_text(encoding="utf-8"))
SPLIT_ROWS = [row for row in FIXTURE["rows"] if row.get("rebuilt")]
SINGLE_ROWS = [row for row in FIXTURE["rows"] if row.get("added_by") and not row.get("rebuilt")]
FAMILY = ("openai", "litellm", "openai_compatible")
CALLS = ("sync", "async")
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(autouse=True)
def _no_environment_proxy(monkeypatch):
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_the_evidence_is_the_four_rebuilt_split_replies_and_recorded_single_choice_replies():
    assert len(SPLIT_ROWS) == 4
    assert sorted(len(row["expected"]["message"]["tool_calls"]) for row in SPLIT_ROWS) == [1, 1, 1, 3]
    assert {row["model"] for row in SINGLE_ROWS} == {"recorded-model-c", "recorded-model-d"}
    for row in SINGLE_ROWS:
        assert len(row["body"]["choices"]) == 1 and row["expected"]["message"]["tool_calls"]


def _calls(message: AIMessage) -> list[dict]:
    return [{"name": call["name"], "args": call["args"], "id": call["id"]} for call in message.tool_calls]


def _invoke(llm, call: str) -> AIMessage | BaseException:
    if call == "sync":
        return replay.outcome(llm)
    try:
        return asyncio.run(llm.ainvoke(replay.REPLAY_PROMPT))
    except Exception as exc:  # noqa: BLE001 - the error itself is the outcome under test
        return exc


def _row(body: dict, row_id: str, *, model: str = "recorded-model-c") -> dict:
    return {"id": row_id, "model": model, "route": {"api": "chat_completions", "reasoning_effort": ""},
            "status": 200, "body": body}


def _replayed(body: dict, row_id: str, *, provider: str = "litellm", call: str = "sync", n: int | None = None):
    llm, _client, _requests = replay.build_model(_row(body, row_id), "default", provider=provider)
    if n is not None:
        llm.n = n
    return _invoke(llm, call)


def _unfolded(body: dict, row_id: str, *, n: int | None = None):
    """What the adapter alone makes of ``body``: the same replay with LangChain's own parser in place."""
    llm, _client, _requests = replay.build_model(_row(body, row_id), "default", provider="litellm")
    if n is not None:
        llm.n = n
    installed = BaseChatOpenAI._create_chat_result
    BaseChatOpenAI._create_chat_result = getattr(installed, "__wrapped__", installed)
    try:
        return replay.outcome(llm)
    finally:
        BaseChatOpenAI._create_chat_result = installed


# Recorded split replies through both real factories.

@pytest.mark.parametrize("path", replay.PATHS)
@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("provider", FAMILY)
@pytest.mark.parametrize("row", SPLIT_ROWS, ids=[row["id"][:3] for row in SPLIT_ROWS])
def test_a_recorded_split_reply_carries_every_call_and_its_text(row, provider, call, path):
    llm, _client, requests = replay.build_model(row, path, provider=provider)

    message = _invoke(llm, call)

    assert requests == ["/v1/chat/completions"]
    assert isinstance(message, AIMessage), repr(message)
    assert _calls(message) == row["expected"]["message"]["tool_calls"]
    assert message.content == row["expected"]["message"]["text"]
    assert message.response_metadata["finish_reason"] == "tool_calls"


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("row", SPLIT_ROWS, ids=[row["id"][:3] for row in SPLIT_ROWS])
def test_the_watcher_factory_receives_a_recorded_split_reply_whole(row, call):
    """``init_chat_model_from_profile`` builds the Watcher's and the operation reasoner's model."""
    profile = ResolvedLLMProfile(provider="openai_compatible", model=row["model"],
                                 api_endpoint=replay.base_url(row["id"], f"watcher-{call}"),
                                 api_key=replay.API_KEY, openai_api="chat_completions")
    llm = init_chat_model_from_profile(profile)
    payload = json.dumps(row["body"]).encode()
    for client in (llm.root_client._client, llm.root_async_client._client):
        library = replay._library_for(client)
        client._transport = library.MockTransport(
            lambda _request, library=library: library.Response(200, content=payload,
                                                               headers={"content-type": "application/json"}))

    message = _invoke(llm, call)

    assert _calls(message) == row["expected"]["message"]["tool_calls"]
    assert message.content == row["expected"]["message"]["text"]


# Accepted forms (synthetic): each reaches Sage as the same reply in one choice would.

def _envelope(choices: list[dict]) -> dict:
    return {"id": "resp_synthetic", "created": 0, "model": "recorded-model-c", "object": "chat.completion",
            "choices": choices, "usage": {"completion_tokens": 9, "prompt_tokens": 90, "total_tokens": 99}}


def _text(index: int, text: str, **message) -> dict:
    return {"finish_reason": "stop", "index": index, "message": {"role": "assistant", "content": text, **message},
            "logprobs": None}


def _tool_call(name: str, call_id: str, position: int = 0, **arguments) -> dict:
    return {"index": position, "function": {"arguments": json.dumps(arguments), "name": name}, "id": call_id,
            "type": "function"}


def _call_choice(index: int, *calls: dict, **message) -> dict:
    return {"finish_reason": "tool_calls", "index": index,
            "message": {"role": "assistant", "content": None, "tool_calls": list(calls), **message}, "logprobs": None}


def _one_choice(text: str | None, *calls: dict) -> dict:
    return _envelope([{"finish_reason": "tool_calls", "index": 0,
                       "message": {"role": "assistant", "content": text,
                                   "tool_calls": [{**c, "index": i} for i, c in enumerate(calls)]},
                       "logprobs": None}])


A = _tool_call("get_task_history_for_callback", "call_a", callback_display_id=2)
B = _tool_call("list_callbacks", "call_b")

ACCEPTED = {
    "choices_out_of_array_order": (
        _envelope([_call_choice(1, A), _text(0, "First the history.")]), _one_choice("First the history.", A)),
    "several_texts_then_a_call": (
        _envelope([_text(0, "One."), _text(1, "Two."), _call_choice(2, A)]), _one_choice("One.\n\nTwo.", A)),
    "calls_across_several_choices": (
        _envelope([_text(0, "Both."), _call_choice(1, A), _call_choice(2, B)]), _one_choice("Both.", A, B)),
    "reasoning_and_annotations_on_choices": (
        _envelope([_text(0, "Noted.", reasoning_content="think", annotations=[]),
                   _call_choice(1, A, reasoning_content="more")]), _one_choice("Noted.", A)),
    "a_call_with_no_text": (
        _envelope([_call_choice(0, A), _call_choice(1, B)]), _one_choice(None, A, B)),
}


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("name", ACCEPTED)
def test_an_accepted_split_form_reaches_sage_as_the_same_reply_in_one_choice(name, call):
    split, whole = ACCEPTED[name]

    folded = _replayed(split, f"accepted-{name}-{call}", call=call)
    expected = _replayed(whole, f"whole-{name}-{call}", call=call)

    assert replay.message_view(folded) == replay.message_view(expected)
    assert folded.tool_calls


def test_the_fold_keeps_reasoning_and_annotations_in_the_folded_choice():
    split, _whole = ACCEPTED["reasoning_and_annotations_on_choices"]
    split = copy.deepcopy(split)
    split["choices"][1]["message"]["annotations"] = [{"type": "url_citation"}]

    folded = openai_api.fold_split_choices(split, n=None)

    assert len(folded["choices"]) == 1
    message = folded["choices"][0]["message"]
    assert message["reasoning_content"] == "think\n\nmore"
    assert message["annotations"] == [{"type": "url_citation"}]
    assert folded["choices"][0]["finish_reason"] == "tool_calls"


# Near-matches (synthetic unless named): unchanged from what the adapter alone makes, with one content-free warning.

TEXT = _text(0, "A sentence that must not reach the log.")
NEAR_MATCHES = {
    "n_above_one": (_envelope([TEXT, _call_choice(1, A)]), 2),
    "a_repeated_call_id": (_envelope([TEXT, _call_choice(1, A), _call_choice(2, A)]), None),
    "a_choice_carrying_parsed": (_envelope([TEXT, _call_choice(1, A, parsed={"x": 1})]), None),
    "several_choices_and_no_call": (_envelope([TEXT, _text(1, "Another sentence.")]), None),
    "a_refusal": (_envelope([_text(0, "Text.", refusal="I can't help with that."), _call_choice(1, A)]), None),
    "a_truncated_choice": (_envelope([TEXT, {**_call_choice(1, A), "finish_reason": "length"}]), None),
    "a_content_filtered_choice": (_envelope([TEXT, {**_call_choice(1, A), "finish_reason": "content_filter"}]), None),
}
WARNING_CONTENT = ("must not reach the log", "call_a", "get_task_history_for_callback", "Another sentence", "can't help")


@pytest.mark.parametrize("name", NEAR_MATCHES)
def test_a_near_match_is_unchanged_and_logged_without_content(name, caplog):
    body, n = NEAR_MATCHES[name]
    caplog.set_level(logging.WARNING, logger=openai_api.__name__)

    result = _replayed(body, f"near-{name}", n=n)

    assert replay.message_view(result) == replay.message_view(_unfolded(body, f"near-today-{name}", n=n))
    warnings = [record.getMessage() for record in caplog.records
                if record.name == openai_api.__name__ and record.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert not any(fragment in warnings[0] for fragment in WARNING_CONTENT), warnings[0]


def test_a_non_assistant_choice_is_left_as_is(caplog):
    """The SDK parses such a body leniently, so it reaches the fold, which leaves it as it came (LangChain then
    rejects it exactly as before) and logs the reason without content. A dict body is returned as the same object."""
    body = _envelope([TEXT, {**_call_choice(1, A), "message": {**_call_choice(1, A)["message"], "role": "user"}}])
    caplog.set_level(logging.WARNING, logger=openai_api.__name__)

    through_sdk = _replayed(body, "near-role")
    today = _unfolded(body, "near-role-today")
    assert type(through_sdk) is type(today) and str(through_sdk) == str(today)
    assert isinstance(through_sdk, Exception)
    assert openai_api.fold_split_choices(body, n=None) is body
    warnings = [record.getMessage() for record in caplog.records if record.name == openai_api.__name__]
    assert warnings == ["Sage kept the first of 2 choices in a provider reply unfolded: a choice is not from the "
                        "assistant."] * 2


@pytest.mark.parametrize("row", SINGLE_ROWS, ids=[row["id"][:3] for row in SINGLE_ROWS])
def test_a_recorded_single_choice_reply_is_unchanged_and_not_logged(row, caplog):
    caplog.set_level(logging.WARNING, logger=openai_api.__name__)

    result = _replayed(row["body"], f"single-{row['id']}")

    assert replay.message_view(result) == row["expected"]["message"]
    assert not [record for record in caplog.records if record.name == openai_api.__name__]


@pytest.mark.parametrize(("body", "error"), [
    ({"error": {"message": "upstream failed"}, "choices": None, "object": "chat.completion"}, ValueError),
    ({"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": None}, TypeError),
])
def test_an_error_body_or_null_choices_still_raise_as_before(body, error):
    folded = _replayed(body, f"error-{error.__name__}")
    today = _unfolded(body, f"error-today-{error.__name__}")

    assert type(folded) is type(today) is error
    assert str(folded) == str(today)


def test_the_fold_is_installed_once_by_either_factory():
    replay.build_model(SPLIT_ROWS[0], "default")
    first = BaseChatOpenAI._create_chat_result
    init_chat_model_from_profile(ResolvedLLMProfile(provider="openai_compatible", model="m",
                                                    api_endpoint="https://once.invalid/v1", api_key=replay.API_KEY))
    assert BaseChatOpenAI._create_chat_result is first
    assert getattr(first, "__wrapped__", None) is not None


# Assembled: the native chat handler, real factory and graph, on Chat Completions.

def _wire(monkeypatch, answer):
    requests: list[dict] = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=answer(body))

    original = langchain.chat_models.init_chat_model
    monkeypatch.setattr(model_module, "init_chat_model", lambda **kwargs: original(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))))
    return requests


def _tool_names(body: dict) -> set[str]:
    return {item["function"]["name"] for item in body.get("tools") or []}


def _single(*, text: str | None = None, call: dict | None = None) -> dict:
    if call is None:
        return _envelope([_text(0, text or "")])
    return _envelope([_call_choice(0, call)])


class _NativeDriverChat(HeadlessSageChat):
    def __init__(self, model):
        super().__init__()
        self.model = model

    async def _get_or_create_model(self, request):
        existing = await get_channel_session(request)
        if existing is None:
            await put_channel_session(request, self.model)
        return self.model, existing is self.model


SPLIT_ISSUE = next(row for row in SPLIT_ROWS
                   if row["expected"]["message"]["tool_calls"][0]["name"] == "issue_task_and_waitfor_task_output")


async def _supervised_step(channel: int) -> tuple[Model, HeadlessSageChat, list[dict], dict]:
    effects: list[dict] = []

    @tool
    async def issue_task_and_waitfor_task_output(command: str, parameters: str | dict, callback_display_id: int) -> str:
        """Issue one callback task and wait for its output."""
        effects.append({"command": command, "parameters": parameters, "callback_display_id": callback_display_id})
        return "offline effect"

    model = Model(provider="litellm", model=SPLIT_ISSUE["model"], system_prompt="offline split-choice case",
                  config={"configurable": {"api_key": replay.API_KEY, "base_url": harness.ENDPOINT,
                                           "openai_api": "chat_completions"}},
                  task_id=0, agent_task_id="split-choice", mode="supervised", operation_id=1, channel_id=channel,
                  apitoken_id=0)
    sink = MagicMock()
    preparer = MythicTools(agent_task_id="offline-split-choice")
    preparer.client = object()

    async def live_command(command, callback):
        return ({"status": "available", "command": command} if (callback, command) == (2, "whoami")
                else {"status": "missing", "payload_type": "apollo"})

    async def known_schema(command, callback):
        return [] if (callback, command) == (2, "whoami") else None

    preparer._authenticate_live_command = live_command
    preparer._fetch_command_schema = known_schema
    sink._prepare_supervised_issue_action = preparer._prepare_supervised_issue_action
    sink.get_tools.side_effect = lambda names: (
        [issue_task_and_waitfor_task_output] if "issue_task_and_waitfor_task_output" in names else [])
    sink._recon_snapshot_cache = {("list_callbacks", "all"): [{"id": 2, "host": "WORKSTATION-A"}]}
    model.mythic_client = sink
    model._payload_names = []
    model.memory = InMemorySaver()
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    request = build_chat_request("Run whoami on callback 2.", channel_id=channel, operation_id=1, request_id=1,
                                 config={"mode": "supervised"})
    bind_channel_thread_id(request, model)
    try:
        await chat.chat(request)
        snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()), subgraphs=True)
    finally:
        await drop_channel_session(build_chat_request("", channel_id=channel, operation_id=1))
    return model, chat, effects, _checkpointed_messages(snapshot)


def _checkpointed_messages(snapshot) -> list:
    """Every message in the checkpoint, including the interrupted worker subgraph's own state."""
    found = [message for value in (snapshot.values or {}).values() if isinstance(value, list) for message in value]
    for task in snapshot.tasks or ():
        if getattr(task, "state", None) is not None and hasattr(task.state, "values"):
            found.extend(_checkpointed_messages(task.state))
    return found


def test_a_supervised_step_whose_call_came_split_raises_exactly_one_card_for_it(monkeypatch):
    transfer = _tool_call("transfer_to_Mythic_Operator", "call_transfer", handoff_title="Run whoami",
                          handoff_instruction="Run whoami on callback 2.", callback_display_id=2,
                          targets=["WORKSTATION-A"], expected_result="The callback's identity.",
                          allowed_capabilities=None, allowed_tools=["issue_task_and_waitfor_task_output"],
                          inputs_by_reference=None, candidate_ref="", proof_host="")

    def answer(body):
        tools = _tool_names(body)
        if "issue_task_and_waitfor_task_output" in tools:
            return SPLIT_ISSUE["body"] if not any(m.get("role") == "tool" for m in body["messages"]) else _single(
                text="Done.")
        if "transfer_to_Mythic_Operator" in tools and not any(m.get("role") == "tool" for m in body["messages"]):
            return _single(call=transfer)
        return _single(text="The step ended.")

    requests = _wire(monkeypatch, answer)
    model, chat, effects, checkpointed = asyncio.run(_supervised_step(channel=251))
    service._CHANNEL_TURN_LOCKS.clear()

    expected = SPLIT_ISSUE["expected"]["message"]["tool_calls"][0]
    cards = [emission["metadata"]["input_requested"] for emission in chat.emissions
             if isinstance(emission.get("metadata"), dict)
             and emission["metadata"].get("special_type") == "input_requested"]
    assert len(cards) == 1, [emission.get("content") for emission in chat.emissions]
    assert cards[0]["data"]["tool_name"] == expected["name"]
    assert cards[0]["data"]["arguments"]["command"] == expected["args"]["command"]
    assert cards[0]["data"]["arguments"]["callback_display_id"] == expected["args"]["callback_display_id"]
    assert model._hitl_card_pending is True
    assert effects == []
    # Supervised preparation adds its own fields to the prepared arguments; the call is the recorded one.
    calls = {(call["id"], call["args"]["command"], call["args"]["callback_display_id"]) for m in checkpointed
             if isinstance(m, AIMessage) for call in m.tool_calls if call["name"] == expected["name"]}
    assert calls == {(expected["id"], expected["args"]["command"], expected["args"]["callback_display_id"])}
    assert any("issue_task_and_waitfor_task_output" in _tool_names(body) for body in requests)


def test_a_conversation_turn_whose_read_call_came_split_runs_it_and_sends_its_result(monkeypatch):
    split = _envelope([_text(0, "I'll list the callbacks first."), _call_choice(1, B)])

    def answer(body):
        return split if not any(m.get("role") == "tool" for m in body["messages"]) else _single(
            text="One callback: WORKSTATION-5.")

    requests = _wire(monkeypatch, answer)
    channel = 252

    async def run():
        process = harness.Process()
        try:
            config = harness.route_config(provider="openai_compatible", model="recorded-model-c")
            await process.chat.chat(harness.request("List the callbacks Mythic has right now.", 1, config,
                                                    channel=channel))
            return process
        finally:
            await process.close()
            await chat_session.remove_session(str(channel))

    process = asyncio.run(run())
    service._CHANNEL_TURN_LOCKS.clear()

    assert process.list_callbacks_calls == 1
    results = [m for body in requests for m in body["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["call_b"]
    assert json.loads(results[0]["content"]) == harness.CALLBACK_ROWS
    assert [e["kind"] for e in process.chat.emissions if e in process.chat.terminal_emissions] == ["complete"]
