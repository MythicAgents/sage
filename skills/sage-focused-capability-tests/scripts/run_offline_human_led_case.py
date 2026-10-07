"""Scripted, offline native-chat approval slice. No provider or Mythic connection.

The scripted messages select graph actions; the driver only answers the exact
prescribed proposal. This checks transport and execution wiring, not model judgment.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
from contextlib import redirect_stdout
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type" / "sage"))

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from mythic_container.ChatBase import ChatInputResponse

from ai.langgraph.model import Model, _current_assisted_operator_input
from ai.langgraph.mythic_tools import MythicTools
from sage_chat.headless import HeadlessSageChat, build_chat_request
from sage_chat.hitl import approval_response_matches
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session, get_channel_session, put_channel_session

PROMPT = "In supervised mode, run whoami on callback 7 and report its output."
EXPECTED = {"name": "issue_task_and_waitfor_task_output", "args": {
    "command": "whoami", "parameters": "", "callback_display_id": 7,
}}
REDIRECT_TEXT = (
    "Change of plan. Do not run the proposed task or use any more tools. "
    "Using only evidence already available, say what is established about this "
    "callback's identity and recommend one check for later. Stop without asking "
    "for approval or taking another action."
)
REDIRECT_ANSWER = (
    "This offline case has not verified the callback's identity. A later, separately "
    "approved whoami task could check it."
)


def fixture_base_arguments(arguments: object) -> dict | None:
    """Validate and remove only the deterministic internal preparation fields."""
    if not isinstance(arguments, dict):
        return None
    extra = set(arguments) - set(EXPECTED["args"])
    if extra and extra != {"timeout", "token_id", "visibility_context"}:
        return None
    if not set(EXPECTED["args"]).issubset(arguments):
        return None
    if extra:
        binding = arguments.get("visibility_context")
        if (arguments.get("timeout") != 300 or arguments.get("token_id") is not None
                or not isinstance(binding, dict)
                or binding.get("_sage_prepared_action") is not True
                or set(binding) != {"_sage_prepared_action", "_sage_prepared_binding"}
                or binding["_sage_prepared_binding"] != {
                    "schema_digest": hashlib.sha256(b"[]").hexdigest(),
                    "group": None, "file": None,
                }):
            return None
        arguments = {key: arguments[key] for key in EXPECTED["args"]}
    if set(arguments) != set(EXPECTED["args"]):
        return None
    return dict(arguments)


def accepted_effective_arguments(arguments: object) -> dict | None:
    """The two scoped identity actions this human-led case permits."""
    arguments = fixture_base_arguments(arguments)
    if arguments is None:
        return None
    if type(arguments["callback_display_id"]) is not int or arguments["callback_display_id"] != 7:
        return None
    values = dict(arguments)
    if values["command"] == "shell":
        parameters = values["parameters"]
        if isinstance(parameters, str) and parameters != "whoami":
            if not (parameters.strip().startswith("{") and parameters.strip().endswith("}")):
                return None
            try:
                # Preserve object pairs so duplicate keys cannot disappear during parsing.
                parameters = json.loads(parameters, object_pairs_hook=list)
            except (TypeError, ValueError):
                return None
        if parameters not in ("whoami", {"command": "whoami"}, [("command", "whoami")]):
            return None
        values["parameters"] = {"command": "whoami"}
    elif values["command"] != "whoami":
        return None
    effective = MythicTools(agent_task_id="offline-human-led")._effective_request_action_arguments(
        EXPECTED["name"], values
    )
    expected_parameters = "whoami" if values["command"] == "shell" else ""
    if effective["parameters"] != expected_parameters:
        return None
    return effective


def install_inert_supervised_preparation(sink, *, commands=("whoami", "shell", "hostname"), callback_id=7):
    """Give a fixture sink real deterministic preparation backed by known empty schemas."""
    preparer = MythicTools(agent_task_id="offline-preparation")
    preparer.client = object()
    async def live_command(command, callback):
        if callback != callback_id or command not in commands:
            return {"status": "missing", "payload_type": "apollo"}
        return {"status": "available", "command": command}
    async def known_empty_schema(command, callback):
        return [] if callback == callback_id and command in commands else None
    preparer._authenticate_live_command = live_command
    preparer._fetch_command_schema = known_empty_schema
    sink._prepare_supervised_issue_action = preparer._prepare_supervised_issue_action


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        scripted = next(self.messages)
        if callable(scripted):
            scripted = scripted(messages)
        return ChatResult(generations=[ChatGeneration(message=scripted)])


def call(name, args, index):
    return AIMessage(content="", tool_calls=[{
        "name": name, "args": args, "id": f"call_{index}", "type": "tool_call",
    }])


def input_response(action: str, card: dict, *, response: str = "") -> ChatInputResponse:
    """Echo the emitted native card as Mythic would after an operator decision."""
    resolved = {"input_request_message_id": 71, "resolved_by_operator_id": 19,
                "resolved_by": "offline operator", "resolved_at": "2026-01-01T00:00:00Z"}
    payload = {**resolved, "action": action}
    if response:
        payload["response"] = response
    request = copy.deepcopy(card)
    # Mythic persists an empty choice catalog even for approval cards.
    request.setdefault("choices", [])
    request.update({"status": {"accept": "accepted", "reject": "rejected",
                               "respond": "responded"}[action],
                    "response": payload, **{key: resolved[key] for key in
                                           ("resolved_by_operator_id", "resolved_by", "resolved_at")}})
    return ChatInputResponse(action=action, response=response, choice={}, input_request_message_id=71,
                             input_request=request, resolved_by_operator_id=19,
                             resolved_by="offline operator", resolved_at="2026-01-01T00:00:00Z")


class DriverChat(HeadlessSageChat):
    def __init__(self, model):
        super().__init__()
        self.model = model

    async def _get_or_create_model(self, request):
        existing = await get_channel_session(request)
        if existing is not None:
            return existing, True
        await put_channel_session(request, self.model)
        return self.model, False


async def _run_case(*, decision: str, proposed_command: str) -> dict:
    started = perf_counter()
    effects: list[dict] = []
    authenticated_steer_seen = False

    @tool
    async def issue_task_and_waitfor_task_output(command: str, parameters: str,
                                                  callback_display_id: int) -> str:
        """Issue a task on a callback and wait for its output."""
        effects.append({"command": command, "parameters": parameters,
                        "callback_display_id": callback_display_id})
        return "offline mock output: example-user"

    proposal = {"name": EXPECTED["name"], "args": {
        **EXPECTED["args"], "command": proposed_command,
        "parameters": '{"command": "whoami"}' if proposed_command == "shell" else "",
    }}
    model = None

    def finish_redirect_only_from_current_steer(messages):
        nonlocal authenticated_steer_seen
        authenticated_steer_seen = (
            model is not None
            and _current_assisted_operator_input(model.state) == REDIRECT_TEXT
            and any(
                isinstance(message, HumanMessage)
                and message.content == REDIRECT_TEXT
                and message.additional_kwargs.get("_operator_input") is True
                and message.additional_kwargs.get("_operator_steer") is True
                and str(message.additional_kwargs.get("_request_id") or "")
                == str(model.state.get("_request_id") or "")
                for message in messages
            )
        )
        if authenticated_steer_seen:
            return AIMessage(content=REDIRECT_ANSWER)
        return call("transfer_to_Mythic_Operator", {
            "handoff_title": "retry original identity task", "handoff_instruction": PROMPT,
        }, 4)

    script = [
        call("transfer_to_Mythic_Operator", {"handoff_title": "run scoped command",
             "handoff_instruction": PROMPT}, 1),
        call(proposal["name"], proposal["args"], 2),
        AIMessage(content="The guarded task was handled."),
        (finish_redirect_only_from_current_steer if decision == "redirect"
         else AIMessage(content="The requested command is complete.")),
        # A stale original-request rebind attempts the old action and must fail the case.
        call(proposal["name"], proposal["args"], 5),
    ]
    model = Model(provider="openai", model="gpt-4o-mini", system_prompt="offline case",
                  config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
                  task_id=0, agent_task_id="offline-human-led", mode="supervised",
                  operation_id=1, channel_id=1, apitoken_id=0)
    sink = MagicMock()
    install_inert_supervised_preparation(sink)
    sink.get_tools.side_effect = lambda names: ([issue_task_and_waitfor_task_output]
                                                if EXPECTED["name"] in names else [])
    model.mythic_client = sink
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = ScriptedModel(messages=iter(script))
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = DriverChat(model)
    first = build_chat_request(PROMPT, channel_id=1, operation_id=1, request_id=1,
                               config={"mode": "supervised"})
    bind_channel_thread_id(first, model)
    await chat.chat(first)
    chat._used = True
    def cards_and_actions():
        cards = [e for e in chat.emissions
                 if (e.get("metadata") or {}).get("special_type") == "input_requested"]
        actions = [action for card in cards for action in
                   card["metadata"]["input_requested"].get("data", {}).get("actions", [])]
        return cards, actions

    cards, observed = cards_and_actions()
    exact = len(cards) == 1 and len(observed) == 1 and (
        observed[0].get("tool_name") == EXPECTED["name"]
        and accepted_effective_arguments(observed[0].get("arguments")) is not None)
    paused = await model._hitl_interrupt_pending(model._session_thread_id())
    card_released = len(cards) == 1 and cards[0].get("complete_request") is False
    first_terminal_count = len(chat.terminal_emissions)
    pre_decision_effect_calls = list(effects)
    actual_decision = decision if exact else "reject"
    native_action = {"accept": "accept", "reject": "reject", "redirect": "respond"}[actual_decision]
    response_matches = False
    if cards:
        second = build_chat_request("", channel_id=1, operation_id=1, request_id=2,
                                    config={"mode": "supervised"})
        second.InputResponse = input_response(
            native_action, cards[0]["metadata"]["input_requested"],
            response=REDIRECT_TEXT if native_action == "respond" else "",
        )
        response_matches = approval_response_matches(second, model._pending_approval_context)
        await chat.chat(second)
    cards, observed = cards_and_actions()
    resumed = not await model._hitl_interrupt_pending(model._session_thread_id())
    terminal_answer = "".join(
        str(emission.get("content") or "")
        for emission in chat.emissions
        if emission.get("kind") in {"text", "delta", "response"}
        and emission.get("complete_request") is not False
        and emission.get("content")
    ).strip()
    passed = (exact and paused and card_released and first_terminal_count == 0
              and not pre_decision_effect_calls
              and response_matches and resumed and len(cards) == 1
              and len(chat.terminal_emissions) == 1
              and [accepted_effective_arguments(effect) for effect in effects] ==
              ([accepted_effective_arguments(observed[0]["arguments"])]
               if decision == "accept" else [])
              and (decision != "redirect" or
                   (native_action == "respond" and authenticated_steer_seen
                    and REDIRECT_ANSWER in terminal_answer)))
    return {"tier": "offline_scripted", "prompt": PROMPT,
            "operator_steer_text": REDIRECT_TEXT if native_action == "respond" else None,
            "requested_decision": decision,
            "applied_decision": actual_decision if cards else None,
            "requested_native_action": {"accept": "accept", "reject": "reject",
                                        "redirect": "respond"}[decision],
            "applied_native_action": native_action if cards else None,
            "exact_proposal": exact, "proposals": observed, "effect_calls": effects,
            "pre_decision_effect_calls": pre_decision_effect_calls,
            "card_count": len(cards), "card_released": card_released,
            "paused_on_checkpoint": paused, "resumed_checkpoint": resumed,
            "response_matches_pending": response_matches,
            "first_terminal_count": first_terminal_count,
            "terminal_count": len(chat.terminal_emissions),
            "terminal_answer_text": terminal_answer,
            "authenticated_steer_seen": authenticated_steer_seen,
            "elapsed_seconds": round(perf_counter() - started, 6),
            "token_usage": {"status": "unavailable", "reason": "scripted fake model"},
            "status": "passed" if passed else ("unexpected_proposal" if not exact else "failed")}


async def run_case(*, decision: str, proposed_command: str) -> dict:
    try:
        return await _run_case(decision=decision, proposed_command=proposed_command)
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision", choices=("accept", "reject", "redirect"), default="accept")
    parser.add_argument("--proposed-command", choices=("whoami", "shell", "hostname"), default="whoami")
    args = parser.parse_args()
    # Sage's runtime logger writes to stdout; keep this CLI's stdout one JSON record.
    # The script executes one synthetic case per process. Give its native channel
    # marker the same disposable scope as its in-memory checkpoint.
    with tempfile.TemporaryDirectory(prefix="sage-offline-human-led-") as scratch, redirect_stdout(sys.stderr), patch(
        "socket.socket.connect", side_effect=RuntimeError("offline case blocked network connect")
    ), patch(
        "socket.socket.connect_ex", side_effect=RuntimeError("offline case blocked network connect")
    ):
        from sage_chat import session as chat_session

        previous_marker_path = chat_session._CHANNEL_MARKER_DB_PATH
        try:
            chat_session._CHANNEL_MARKER_DB_PATH = str(Path(scratch) / "sage.db")
            result = asyncio.run(run_case(decision=args.decision, proposed_command=args.proposed_command))
        finally:
            chat_session._CHANNEL_MARKER_DB_PATH = previous_marker_path
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
