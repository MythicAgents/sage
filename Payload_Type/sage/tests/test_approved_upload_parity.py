"""An accepted upload reaches its one child task, and approval still binds every material value.

The operator accepts `upload_file_by_file_uuid`; the helper then issues the `upload` task itself. Between the two,
Sage's schema resolver may rename the model's keys (`file`/`remote_path` -> `File`/`Path`) or add declared defaults,
and the task path fills an omitted or null timeout. The child check must compare the approved root and the child
under the same effective-argument rules, so those differences admit the child while any material change to the
command, callback, file, destination, token or explicit timeout still issues nothing.

Everything on the path is real except Mythic's network I/O: the registered helper, its request-contract check and
file metadata guard, the task method with every authority recheck, the schema resolver and its recorded binding,
and (in the assembled case) the approval card, its acceptance and the native chat service.
"""

from __future__ import annotations

import asyncio
import copy
import socket
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph import model as model_module
from ai.langgraph import mythic_tools as mythic_tools_module
from ai.langgraph.mythic_tools import MythicTools, SAGE_MYTHIC_TASK_TIMEOUT
from ai.langgraph.request_contract import action_spec_from_tool_call, build_request_contract
from ai.langgraph.turn_authority import authority_from_request_contract
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session
from tests.test_engagement_wiring import _split_issue
from tests.test_hitl_steer_current_intent import (
    _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response,
)
from tests.test_request_contract import _approval_claim_for

UPLOAD = "upload_file_by_file_uuid"
ISSUE = "issue_task_and_waitfor_task_output"
CALLBACK = 7
FILE_UUID = "d9c43269-2cb5-4aa3-b893-219294b88b17"
OTHER_FILE_UUID = "a9c43269-2cb5-4aa3-b893-219294b88b17"
DESTINATION = "C:\\Temp\\synthetic-apollo.exe"
WRONG_DESTINATION = "C:\\Temp\\wrong.exe"
HOST = "WORKSTATION-7"
UPLOAD_OUTPUT = "Uploaded synthetic-apollo.exe to C:\\Temp\\synthetic-apollo.exe"
ALIAS = {"file": FILE_UUID, "remote_path": DESTINATION}
CANONICAL = {"File": FILE_UUID, "Path": DESTINATION}
# Apollo's upload schema: the model's `file`/`remote_path` names map to the `File`/`Path` cli names.
UPLOAD_SCHEMA = [
    {"name": "remote_path", "cli_name": "Path", "type": "String", "choices": [],
     "parameter_group_name": "Default", "required": False},
    {"name": "remote_path", "cli_name": "Path", "type": "String", "choices": [],
     "parameter_group_name": "ExistingFile", "required": False},
    {"name": "file", "cli_name": "File", "type": "File", "choices": [],
     "parameter_group_name": "Default", "required": False},
    {"name": "filename", "cli_name": "fileName", "type": "ChooseOne", "choices": [],
     "parameter_group_name": "ExistingFile", "required": True},
]
# The same schema with a declared default the resolver adds when the proposal omits it.
DEFAULTED_SCHEMA = [*UPLOAD_SCHEMA, {
    "name": "overwrite", "cli_name": "Overwrite", "type": "Boolean", "choices": [],
    "parameter_group_name": "Default", "required": False, "default_value": True,
}]
_OMITTED = object()


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("unexpected network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def _upload_args(parameters=None, *, timeout=_OMITTED, token_id=_OMITTED, **changes) -> dict:
    args = {"command": "upload", "parameters": copy.deepcopy(ALIAS if parameters is None else parameters),
            "file_uuid": FILE_UUID, "callback_display_id": CALLBACK}
    if timeout is not _OMITTED:
        args["timeout"] = timeout
    if token_id is not _OMITTED:
        args["token_id"] = token_id
    args.update(changes)
    return args


def _inert_mythic_io(monkeypatch, tools: MythicTools, schema=UPLOAD_SCHEMA) -> None:
    """Answer Mythic's control-plane reads locally; task issue and wait are inert through `_split_issue`."""
    async def file_metadata(file_uuid):
        return ({"agent_file_id": FILE_UUID, "filename_utf8": "synthetic-apollo.exe", "complete": True,
                 "deleted": False} if file_uuid in {FILE_UUID, OTHER_FILE_UUID} else None)

    async def live_command(command, callback_display_id):
        return ({"status": "available", "command": command} if int(callback_display_id) == CALLBACK
                else {"status": "missing", "payload_type": "apollo"})

    async def command_schema(command, callback_display_id, **_kwargs):
        if command == "upload" and int(callback_display_id) == CALLBACK:
            return copy.deepcopy(schema)
        return []

    async def liveness(client, display_id, **_kwargs):
        return {"status": "active", "reason": "synthetic check-in"}

    async def task_state(*_args, **_kwargs):
        return {"task_completed": True, "task_status": "completed"}

    monkeypatch.setattr(tools, "_get_file_metadata", file_metadata)
    monkeypatch.setattr(tools, "_authenticate_live_command", live_command)
    monkeypatch.setattr(tools, "_resolve_payload_type", lambda callback_id: asyncio.sleep(0, result="apollo"))
    monkeypatch.setattr(tools, "_fetch_command_schema", command_schema)
    monkeypatch.setattr(tools, "_fetch_enrolled_executable_schema", command_schema)
    monkeypatch.setattr(tools, "_supervised_task_state", task_state)
    monkeypatch.setattr(mythic_tools_module, "assess_callback_liveness", liveness)
    tools._recon_snapshot_cache[("list_callbacks", "all")] = [{"id": CALLBACK, "display_id": CALLBACK,
                                                              "host": HOST}]


def _tools(monkeypatch, schema=UPLOAD_SCHEMA) -> MythicTools:
    tools = MythicTools(agent_task_id="approved-upload-parity")
    tools.client = object()
    _inert_mythic_io(monkeypatch, tools, schema)
    return tools


def _accept(tools: MythicTools, args: dict, *, request_id: str = "chat:upload:request:1") -> dict:
    """Install the supervised request and the operator's exact acceptance of this upload."""
    action = {"name": UPLOAD, "args": copy.deepcopy(args)}
    contract = build_request_contract(
        request_id=request_id, channel_id="1", operation_id="1", mode="supervised",
        autonomous_solve=False, requested_actions=(action_spec_from_tool_call(action),))
    tools.set_request_contract(contract)
    tools.set_turn_authority(authority_from_request_contract(contract))
    claim = _approval_claim_for(contract, [action])
    tools.set_approval_claim(claim)
    return claim


def _mutating_child(tools: MythicTools, change):
    """Change the helper's child task arguments after the root was accepted, then call the real task method."""
    real = tools.issue_task_and_waitfor_task_output

    async def child(**kwargs):
        kwargs = change(dict(kwargs))
        return await real(**kwargs)

    tools.issue_task_and_waitfor_task_output = child


def _run_upload(tools: MythicTools, args: dict) -> SimpleNamespace:
    calls: dict = {}
    error = None
    result = None
    with _split_issue(UPLOAD_OUTPUT, calls, display_id=61):
        try:
            result = asyncio.run(tools.upload_file_by_file_uuid(**copy.deepcopy(args)))
        except PermissionError as exc:
            error = str(exc)
    return SimpleNamespace(result=result, error=error, issued=calls.get("issued", []),
                           denial=getattr(tools, "_last_effect_denial", None))


def _assert_one_upload(run, parameters=CANONICAL) -> None:
    assert run.error is None, run.error
    assert run.denial is None, run.denial
    assert [(row["command_name"], row["parameters"], row["callback_display_id"]) for row in run.issued] == [
        ("upload", parameters, CALLBACK)]
    assert UPLOAD_OUTPUT in str(run.result)
    assert "exact effect" not in str(run.result)


def _assert_nothing_issued(run) -> None:
    assert run.issued == []
    assert UPLOAD_OUTPUT not in str(run.result or "")


# --- the accepted upload reaches its one child task ---------------------------------------------------------------

@pytest.mark.parametrize("parameters", [ALIAS, CANONICAL], ids=["schema-alias-keys", "canonical-keys"])
@pytest.mark.parametrize("timeout", [_OMITTED, None, 120], ids=["timeout-omitted", "timeout-null", "timeout-120"])
@pytest.mark.parametrize("token_id", [_OMITTED, None], ids=["token-omitted", "token-null"])
def test_accepted_upload_issues_exactly_one_child_task(monkeypatch, no_network, parameters, timeout, token_id):
    tools = _tools(monkeypatch)
    args = _upload_args(parameters, timeout=timeout, token_id=token_id)
    _accept(tools, args)

    run = _run_upload(tools, args)

    _assert_one_upload(run)
    assert tools._last_issued_task_display_id == 61


def test_resolver_declared_default_stays_covered(monkeypatch, no_network):
    tools = _tools(monkeypatch, DEFAULTED_SCHEMA)
    args = _upload_args(ALIAS, timeout=120)
    _accept(tools, args)

    run = _run_upload(tools, args)

    _assert_one_upload(run, {**CANONICAL, "Overwrite": True})


# --- a material change at the child boundary issues nothing -------------------------------------------------------

def _set_parameter(key, value):
    def change(kwargs):
        kwargs["parameters"] = {**kwargs["parameters"], key: value}
        return kwargs
    return change


def _set(key, value):
    def change(kwargs):
        kwargs[key] = value
        return kwargs
    return change


def _canonicalize_before_resolver(kwargs):
    # Same values under the resolver's output names, but transformed outside the resolver, so no binding records it.
    kwargs["parameters"] = {"File": kwargs["parameters"]["file"], "Path": kwargs["parameters"]["remote_path"]}
    return kwargs


CHILD_CHANGES = {
    "file": (_set_parameter("file", OTHER_FILE_UUID), 120),
    "destination": (_set_parameter("remote_path", WRONG_DESTINATION), 120),
    "command": (_set("command", "download"), 120),
    "token": (_set("token_id", 3), 120),
    "explicit-timeout": (_set("timeout", 121), 120),
    "omitted-root-timeout": (_set("timeout", 999), _OMITTED),
    "unrecorded-transformation": (_canonicalize_before_resolver, 120),
}


@pytest.mark.parametrize("name", sorted(CHILD_CHANGES))
def test_child_change_after_acceptance_issues_nothing(monkeypatch, no_network, name):
    change, timeout = CHILD_CHANGES[name]
    tools = _tools(monkeypatch)
    args = _upload_args(ALIAS, timeout=timeout)
    _accept(tools, args)
    _mutating_child(tools, change)

    run = _run_upload(tools, args)

    _assert_nothing_issued(run)
    assert "approved proposal does not cover this exact effect" in str(run.result)
    assert "approved proposal does not cover this exact effect" in run.denial["reason"]


def test_child_on_another_callback_issues_nothing(monkeypatch, no_network):
    tools = _tools(monkeypatch)
    args = _upload_args(ALIAS, timeout=120)
    _accept(tools, args)
    _mutating_child(tools, _set("callback_display_id", 8))

    run = _run_upload(tools, args)

    _assert_nothing_issued(run)
    assert "approved proposal does not cover this exact effect" in str(run.result)


@pytest.mark.parametrize("command,callback", [("download", CALLBACK), ("upload", 8)])
def test_resolver_binding_for_another_command_or_callback_does_not_cover_the_child(
        monkeypatch, no_network, command, callback):
    """A recorded resolution whose output is the child's arguments, for another command or callback, admits nothing."""
    tools = _tools(monkeypatch)
    args = _upload_args(ALIAS, timeout=120)
    _accept(tools, args)
    wrong = {"File": FILE_UUID, "Path": WRONG_DESTINATION}
    tools._last_argres_binding = {"command": command, "callback_display_id": callback,
                                  "original": dict(ALIAS), "resolved": dict(wrong)}
    _mutating_child(tools, _set("parameters", dict(wrong)))

    run = _run_upload(tools, args)

    _assert_nothing_issued(run)
    assert "approved proposal does not cover this exact effect" in str(run.result)


def test_unrelated_task_under_an_accepted_upload_issues_nothing(monkeypatch, no_network):
    tools = _tools(monkeypatch)
    _accept(tools, _upload_args(ALIAS, timeout=120))
    calls: dict = {}

    with _split_issue("synthetic whoami", calls, display_id=62):
        result = asyncio.run(tools.issue_task_and_waitfor_task_output(
            command="whoami", parameters="", callback_display_id=CALLBACK))

    assert calls.get("issued", []) == []
    assert "approved proposal does not cover this exact effect" in result


def test_undeclared_child_tool_is_not_covered_by_an_accepted_upload(monkeypatch):
    tools = _tools(monkeypatch)
    _accept(tools, _upload_args(ALIAS, timeout=120))

    assert tools._approval_effect_blocker("sandbox_exec", {"command": "whoami"}) == (
        "approved proposal does not cover this exact effect")


@pytest.mark.parametrize("claim", ["missing", "another-request"])
def test_missing_or_stale_claim_issues_nothing(monkeypatch, no_network, claim):
    tools = _tools(monkeypatch)
    args = _upload_args(ALIAS, timeout=120)
    accepted = _accept(tools, args)
    if claim == "missing":
        tools.clear_approval_claim()
    else:
        tools._active_approval_claim = {**accepted, "request_id": "chat:upload:request:0"}

    run = _run_upload(tools, args)

    assert run.issued == []
    assert run.error is not None and "lacks an exact active approval claim" in run.error


# --- the controls above are sensitive: name/callback-only child admission lets them through ------------------------

def _name_and_callback_only(monkeypatch):
    real = MythicTools._approved_workflow_child

    def weakened(self, root_name, root_args, tool_name, actual_args, visibility_context, *, recheck=False):
        if root_name == UPLOAD and tool_name == ISSUE:
            return actual_args.get("callback_display_id") == root_args.get("callback_display_id")
        return real(self, root_name, root_args, tool_name, actual_args, visibility_context, recheck=recheck)

    monkeypatch.setattr(MythicTools, "_approved_workflow_child", weakened)


@pytest.mark.parametrize("name", sorted(CHILD_CHANGES))
def test_child_controls_fail_under_name_and_callback_only_admission(monkeypatch, no_network, name):
    _name_and_callback_only(monkeypatch)
    change, timeout = CHILD_CHANGES[name]
    tools = _tools(monkeypatch)
    args = _upload_args(ALIAS, timeout=timeout)
    _accept(tools, args)
    _mutating_child(tools, change)

    run = _run_upload(tools, args)

    assert len(run.issued) == 1, (name, run.result)


def test_unrelated_task_control_fails_under_name_and_callback_only_admission(monkeypatch, no_network):
    _name_and_callback_only(monkeypatch)
    tools = _tools(monkeypatch)
    _accept(tools, _upload_args(ALIAS, timeout=120))
    calls: dict = {}

    with _split_issue("synthetic whoami", calls, display_id=62):
        asyncio.run(tools.issue_task_and_waitfor_task_output(
            command="whoami", parameters="", callback_display_id=CALLBACK))

    assert len(calls.get("issued", [])) == 1


def test_null_and_omitted_timeout_bind_the_configured_task_timeout(monkeypatch):
    """Both sides of the child comparison read an omitted or null timeout as the one configured task timeout."""
    tools = _tools(monkeypatch)
    _accept(tools, _upload_args(ALIAS, timeout=None))
    child = {"command": "upload", "parameters": dict(ALIAS), "callback_display_id": CALLBACK,
             "token_id": None, "visibility_context": None}

    assert tools._approval_effect_blocker(ISSUE, {**child, "timeout": SAGE_MYTHIC_TASK_TIMEOUT}) == ""
    assert tools._approval_effect_blocker(ISSUE, {**child, "timeout": None}) == ""
    assert tools._approval_effect_blocker(ISSUE, {**child, "timeout": SAGE_MYTHIC_TASK_TIMEOUT + 1}) != ""


# --- assembled: the accepted upload in a later supervised step reaches the worker through native chat -------------

OPERATOR = "Mythic_Operator"
HANDOFF = f"transfer_to_{OPERATOR}"
WHOAMI_OUTPUT = "LAB\\svc-synthetic"
PROMPT = (f"In supervised mode on callback {CALLBACK}: first run whoami, then upload Mythic file {FILE_UUID} "
          f"to {DESTINATION}.")
STEP_BRIEF = {"callback_display_id": CALLBACK, "targets": [HOST]}
WHOAMI_STEP = {"handoff_title": "identify the callback user",
               "handoff_instruction": f"Run whoami on callback {CALLBACK}.", **STEP_BRIEF,
               "allowed_tools": [ISSUE], "expected_result": "The callback's user."}
UPLOAD_STEP = {"handoff_title": "upload the payload file",
               "handoff_instruction": f"Upload Mythic file {FILE_UUID} to {DESTINATION} on callback {CALLBACK}.",
               **STEP_BRIEF, "allowed_tools": [UPLOAD], "expected_result": "The payload file on the host."}
WHOAMI_ACTION = {"command": "whoami", "parameters": "", "callback_display_id": CALLBACK}
# The observed native proposal shape: the schema's `file`/`remote_path` names and an explicit timeout.
UPLOAD_ACTION = _upload_args(ALIAS, timeout=120)
FINAL = "Callback 7 runs as LAB\\svc-synthetic and the payload file is uploaded."


class _StepModel(_NativeScriptedModel):
    """Answer the Mythic Operator and every other agent from functions of what each was actually given."""

    worker_fn: object = None
    supervisor_fn: object = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.inputs.append(list(messages))
        worker = any(isinstance(m, SystemMessage) and "You are the **Mythic Operator**" in str(m.content)
                     for m in messages)
        message = (self.worker_fn if worker else self.supervisor_fn)(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])


def _current_step(messages):
    """The latest delegated step's title and every tool result the worker has seen since it."""
    start = max((i for i, m in enumerate(messages)
                 if m.additional_kwargs.get("_delegated_to") == OPERATOR), default=-1)
    title = messages[start].additional_kwargs.get("_handoff_title") if start >= 0 else ""
    return title, [m for m in messages[start + 1:] if isinstance(m, ToolMessage)]


async def _assembled_run(tools: MythicTools, proposed: list) -> SimpleNamespace:
    counter = iter(range(100, 200))

    def worker(messages):
        title, results = _current_step(messages)
        if title == UPLOAD_STEP["handoff_title"]:
            if not results or len([call for call in proposed if call == UPLOAD]) < 2:
                proposed.append(UPLOAD)  # the second proposal is a scripted same-step resend
                return _call(UPLOAD, copy.deepcopy(UPLOAD_ACTION), next(counter))
            return AIMessage(content=f"Uploaded the payload file to {DESTINATION}.")
        if not results:
            proposed.append(ISSUE)
            return _call(ISSUE, dict(WHOAMI_ACTION), next(counter))
        return AIMessage(content=f"Callback {CALLBACK} runs as {WHOAMI_OUTPUT}.")

    def supervisor(messages):
        # The Supervisor is given Sage's record of each ended worker step.
        done = len([m for m in messages if isinstance(m, AIMessage) and m.additional_kwargs.get("_step_outcome")])
        if done == 0:
            return _call(HANDOFF, dict(WHOAMI_STEP), next(counter))
        if done == 1:
            return _call(HANDOFF, dict(UPLOAD_STEP), next(counter))
        return AIMessage(content=FINAL)

    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline approved upload parity",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="approved-upload", mode="supervised", operation_id=1, channel_id=1,
        apitoken_id=0,
    )
    model.mythic_client = tools
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = _StepModel(messages=iter(()), worker_fn=worker, supervisor_fn=supervisor)
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    calls: dict = {}

    def output():
        return UPLOAD_OUTPUT if calls["issued"][-1]["command_name"] == "upload" else WHOAMI_OUTPUT

    request_id = handled = 0
    try:
        with _split_issue(output, calls, display_id=lambda: 70 + len(calls.get("issued", []))):
            request_id += 1
            request = build_chat_request(PROMPT, channel_id=1, operation_id=1, request_id=request_id,
                                         config={"mode": "supervised"})
            bind_channel_thread_id(request, model)
            await chat.chat(request)
            while True:  # accept each card raised since the last one handled, as the operator would
                raised = [card for card in _cards(chat)[handled:]
                          if card.get("status") not in {"accepted", "rejected"}]
                handled = len(_cards(chat))
                if not raised:
                    break
                request_id += 1
                accept = build_chat_request("", channel_id=1, operation_id=1, request_id=request_id,
                                            config={"mode": "supervised"})
                accept.InputResponse = _input_response("accept", raised[-1])
                await chat.chat(accept)
        snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
        return SimpleNamespace(model=model, chat=chat, emissions=chat.emissions, issued=calls.get("issued", []),
                               values=snapshot.values, next=snapshot.next, fake=fake)
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


def _card_tools(run) -> list[str]:
    return [action["tool_name"] for card in _cards(run.chat) for action in card["data"]["actions"]]


def _terminals(run) -> list[dict]:
    return [e for e in run.emissions if e.get("complete_request")
            and not (isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "input_requested")]


def _assert_lifecycle_clean(run) -> None:
    ledger = run.model._request_event_ledger
    assert ledger.open_lifecycles() == ()
    final = ledger.reconcile(require_final=True)
    assert final["ok"], final["errors"]


def test_accepted_upload_in_a_later_step_issues_one_task_and_reaches_the_worker(monkeypatch, no_network):
    tools = _tools(monkeypatch)
    proposed: list = []

    run = asyncio.run(_assembled_run(tools, proposed))

    # One card per step: the prior step's consumed approval is not reused for the upload.
    assert _card_tools(run) == [ISSUE, UPLOAD]
    # The worker proposed the upload twice in its step; the resend issued nothing.
    assert proposed == [ISSUE, UPLOAD, UPLOAD]
    assert [(row["command_name"], row["parameters"], row["callback_display_id"]) for row in run.issued] == [
        ("whoami", "", CALLBACK), ("upload", CANONICAL, CALLBACK)]
    # The upload task's output went back to the worker as the helper's result.
    upload_results = [m for m in run.values.get("mythic_operator_messages", [])
                      if isinstance(m, ToolMessage) and m.name == UPLOAD]
    assert len(upload_results) == 1 and UPLOAD_OUTPUT in str(upload_results[0].content), upload_results
    assert "exact effect" not in str(upload_results[0].content)
    # The upload step handed back as an executed step, and the Supervisor answered from both steps.
    supervisor_inputs = [msgs for msgs in run.fake.inputs if not any(
        isinstance(m, SystemMessage) and "You are the **Mythic Operator**" in str(m.content) for m in msgs)]
    outcomes = [str(m.content) for m in supervisor_inputs[-1]
                if isinstance(m, AIMessage) and m.additional_kwargs.get("_step_outcome")]
    assert len(outcomes) == 2 and "1 executed" in outcomes[-1], outcomes
    text = "\n".join(str(e.get("content") or "") for e in run.emissions)
    assert "exact effect" not in text and "live target" not in text and "Blocked" not in text
    assert tools._last_effect_denial is None
    [terminal] = _terminals(run)
    assert terminal["kind"] == "complete" and FINAL in terminal["content"], terminal
    assert run.next == ()
    _assert_lifecycle_clean(run)
