"""Control exceptions raised inside the task issuer propagate; ordinary failures keep their task error.

`MythicTools.issue_task_and_waitfor_task_output` turns an ordinary exception in its issue/wait path into an
operator-visible task error and advances the failure breaker. LangGraph's `GraphBubbleUp` (interrupts,
`ParentCommand`) and Sage's `_OperatorStopRequested` also subclass `Exception`. Observed offline: a `GraphInterrupt`
raised from the Mythic SDK wait for the collector task came back as a failed collection instead of propagating out of
`execute_capability`.

These tests run the real issuer through a package import, so it reads the control set Sage's graph owns
(`model._COLLECTION_CONTROL_EXCEPTIONS`). Replaced, and labelled synthetic: the Mythic SDK calls `mythic.issue_task`
and `mythic.waitfor_for_task_output`; the existing stand-ins for Mythic reads that the collection fixture already
uses (live command surface, callback liveness, footprint); and, for the pre-issue rows, the one Sage helper each issuer
handler wraps, because those helpers absorb SDK errors themselves before the issuer sees them. No production code
raises a control exception inside the issuer today, so every control exception here is synthetic. The collection rows
reuse the real approved client, collector and private transaction of `test_collection_terminal_outcome.py`.
"""

import asyncio
import socket
from unittest.mock import AsyncMock

import pytest
from langgraph.errors import GraphInterrupt, ParentCommand
from langgraph.types import Command, Interrupt

from ai.langgraph import model, mythic_tools as mt
from tests.test_collection_capability import ACTION, _approve, _run
from tests.test_collection_terminal_outcome import _commands, _mythic
from tests.test_private_collection_transaction import collection  # noqa: F401  (fixture)

# --- synthetic control exceptions and SDK output (fixture) --------------------------------------------------------
CONTROL = {
    "interrupt": lambda: GraphInterrupt((Interrupt(value={"action_requests": []}),)),
    "parent_command": lambda: ParentCommand(Command(graph=Command.PARENT, goto="Supervisor")),
    "operator_stop": lambda: model._OperatorStopRequested("operator_stop"),
}
OUTPUT = "Local Identity: corp\\operator"
CREDENTIAL_REJECTION = "credential parameters require @cred:<id> task references"
LS_SCHEMA = [{"name": "path", "cli_name": "path", "type": "String", "required": False,
              "parameter_group_name": "Default"}]
# --- end fixture --------------------------------------------------------------------------------------------------


class _SDK:
    """Synthetic Mythic SDK: task creation and the output wait each return a value or raise the given exception."""

    def __init__(self, monkeypatch, *, create=None, wait=OUTPUT):
        self.created, self.create, self.wait = [], create, wait
        monkeypatch.setattr(mt.mythic, "issue_task", self._issue)
        monkeypatch.setattr(mt.mythic, "waitfor_for_task_output", self._wait)

    async def _issue(self, **kwargs):
        self.created.append(kwargs["command_name"])
        if isinstance(self.create, BaseException):
            raise self.create
        return {"display_id": 4242}

    async def _wait(self, **_kwargs):
        if isinstance(self.wait, BaseException):
            raise self.wait
        return self.wait


@pytest.fixture
def issuer(monkeypatch):
    """A worker's Mythic client with no Mythic server: only the stand-ins named in the module docstring."""
    def forbidden(*_args, **_kwargs):
        pytest.fail("the issuer attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    client = mt.MythicTools(agent_task_id="test-task")
    client.client = object()
    monkeypatch.setattr(client, "_authenticate_live_command", AsyncMock(return_value={"status": "available"}))
    monkeypatch.setattr(client, "_callback_tasking_liveness_blocker", AsyncMock(return_value=""))
    monkeypatch.setattr(client, "_action_footprint", AsyncMock(return_value=None))
    return client


def _breaker(client):
    return dict(client._task_failure_counts), dict(client._request_shape_counts())


def _issue(client, command="whoami", parameters="", **kwargs):
    return asyncio.run(client.issue_task_and_waitfor_task_output(command, parameters, 11, timeout=5, **kwargs))


def test_the_synthetic_control_exceptions_are_the_ones_sage_names():
    for factory in CONTROL.values():
        assert isinstance(factory(), model._COLLECTION_CONTROL_EXCEPTIONS)
    assert mt._issuer_control_exceptions() is model._COLLECTION_CONTROL_EXCEPTIONS


@pytest.mark.parametrize("control", CONTROL)
@pytest.mark.parametrize("seam", ["wait", "create"])
def test_a_control_exception_from_the_mythic_sdk_propagates_out_of_the_issuer(issuer, monkeypatch, seam, control):
    raised = CONTROL[control]()
    sdk = _SDK(monkeypatch, **{seam: raised})

    with pytest.raises(type(raised)) as caught:
        _issue(issuer)

    assert caught.value is raised
    assert sdk.created == ["whoami"]
    assert _breaker(issuer) == ({}, {})


@pytest.mark.parametrize("control", CONTROL)
def test_a_control_exception_from_the_wait_propagates_out_of_the_registered_worker_tool(issuer, monkeypatch,
                                                                                       control):
    raised = CONTROL[control]()
    _SDK(monkeypatch, wait=raised)
    tool = issuer.get_tools(["issue_task_and_waitfor_task_output"])[0]

    with pytest.raises(type(raised)) as caught:
        asyncio.run(tool.ainvoke({"command": "whoami", "parameters": "", "callback_display_id": 11, "timeout": 5}))

    assert caught.value is raised
    assert _breaker(issuer) == ({}, {})


# Each pre-issue handler of the issuer, by the helper it wraps: (helper, command, parameters, issue kwargs,
# the outcome an ordinary exception from that helper keeps).
PRE_ISSUE = {
    "dcsync_coercion": ("_coerce_native_dcsync_to_working_form", "whoami", "", {}, OUTPUT),
    "engagement_hook": ("_engagement_issue_hook", "whoami", "", {}, OUTPUT),
    "shell_coercion": ("_coerce_shell_parameters_from_schema", "whoami", "", {}, OUTPUT),
    "live_command": ("_authenticate_live_command", "whoami", "", {}, OUTPUT),
    "prepared_binding": ("_fetch_enrolled_executable_schema", "whoami", "",
                         {"visibility_context": {"_sage_prepared_action": True, "_sage_prepared_binding": {}}},
                         "STOP — prepared task binding could not be revalidated: RuntimeError"),
    "registered_file": ("_ensure_registered_file_available", "whoami", "", {},
                        f"{mt._REGISTERED_FILE_PREFLIGHT_PREFIX} could not verify registered-file prerequisites "
                        "for command 'whoami': RuntimeError: helper failed"),
    "argument_resolution": ("_bind_mythic_credential_parameters", "ls", {"path": "C:\\Users"}, {}, OUTPUT),
    "empty_parameter_schema": ("_fetch_command_schema", "whoami", "", {}, OUTPUT),
}


def _pre_issue(client, monkeypatch, seam, raised):
    helper, command, parameters, kwargs, ordinary = PRE_ISSUE[seam]
    if seam == "argument_resolution":
        monkeypatch.setattr(client, "_fetch_command_schema", AsyncMock(return_value=LS_SCHEMA))
    monkeypatch.setattr(client, helper, AsyncMock(side_effect=raised))
    return command, parameters, kwargs, ordinary


@pytest.mark.parametrize("control", CONTROL)
@pytest.mark.parametrize("seam", PRE_ISSUE)
def test_a_control_exception_from_a_pre_issue_step_propagates_before_any_task(issuer, monkeypatch, seam, control):
    raised = CONTROL[control]()
    sdk = _SDK(monkeypatch)
    command, parameters, kwargs, _ordinary = _pre_issue(issuer, monkeypatch, seam, raised)

    with pytest.raises(type(raised)) as caught:
        _issue(issuer, command, parameters, **kwargs)

    assert caught.value is raised
    assert sdk.created == []
    assert _breaker(issuer) == ({}, {})


@pytest.mark.parametrize("seam", PRE_ISSUE)
def test_an_ordinary_exception_from_a_pre_issue_step_keeps_its_fail_open_or_denial(issuer, monkeypatch, seam):
    sdk = _SDK(monkeypatch)
    command, parameters, kwargs, ordinary = _pre_issue(issuer, monkeypatch, seam, RuntimeError("helper failed"))

    result = _issue(issuer, command, parameters, **kwargs)

    assert result == ordinary
    assert sdk.created == ([command] if ordinary == OUTPUT else [])


def test_a_control_exception_carrying_the_credential_rejection_text_is_not_repaired(issuer, monkeypatch):
    """The repair path matches Mythic's rejection by its text; a control exception must never enter it."""
    raised = GraphInterrupt((Interrupt(value=CREDENTIAL_REJECTION),))
    assert issuer._is_mythic_credential_reference_rejection(raised)  # the near-match matches the text check
    sdk = _SDK(monkeypatch, create=raised)
    repairs = []
    bind = issuer._bind_mythic_credential_parameters

    async def binder(*args, **kwargs):
        repairs.append(kwargs.get("force_refresh", False))
        return await bind(*args, **kwargs)

    monkeypatch.setattr(issuer, "_bind_mythic_credential_parameters", binder)

    with pytest.raises(GraphInterrupt) as caught:
        _issue(issuer)

    assert caught.value is raised
    assert sdk.created == ["whoami"] and repairs == []
    assert _breaker(issuer) == ({}, {})


def test_an_ordinary_credential_rejection_still_enters_the_repair_and_fails_closed(issuer, monkeypatch):
    sdk = _SDK(monkeypatch, create=RuntimeError(CREDENTIAL_REJECTION))
    repairs = []
    bind = issuer._bind_mythic_credential_parameters

    async def binder(*args, **kwargs):
        repairs.append(kwargs.get("force_refresh", False))
        return await bind(*args, **kwargs)

    monkeypatch.setattr(issuer, "_bind_mythic_credential_parameters", binder)

    result = _issue(issuer)

    assert repairs == [True] and sdk.created == ["whoami"]
    assert result == f"Error issuing command 'whoami' to agent 11: {CREDENTIAL_REJECTION}"
    assert _breaker(issuer) == ({("whoami", 11, ""): 1}, {("whoami", 11): 1})


@pytest.mark.parametrize("seam,message,expected", [
    ("wait", "subscription closed", "Error issuing command 'whoami' to agent 11: subscription closed"),
    ("create", "subscription closed", "Error issuing command 'whoami' to agent 11: subscription closed"),
    ("wait", "GraphInterrupt: interrupted by the operator",
     "Error issuing command 'whoami' to agent 11: GraphInterrupt: interrupted by the operator"),
])
def test_an_ordinary_exception_keeps_its_error_text_and_advances_the_breaker(issuer, monkeypatch, seam, message,
                                                                            expected):
    sdk = _SDK(monkeypatch, **{seam: RuntimeError(message)})

    assert _issue(issuer) == expected
    assert sdk.created == ["whoami"]
    assert _breaker(issuer) == ({("whoami", 11, ""): 1}, {("whoami", 11): 1})


def test_an_ordinary_wait_failure_through_the_worker_tool_names_the_issued_task(issuer, monkeypatch):
    _SDK(monkeypatch, wait=RuntimeError("subscription closed"))
    tool = issuer.get_tools(["issue_task_and_waitfor_task_output"])[0]

    result = asyncio.run(tool.ainvoke({"command": "whoami", "parameters": "", "callback_display_id": 11,
                                       "timeout": 5}))

    assert result.endswith(
        "Error observing output for issued task 4242 ('whoami' on callback 11): subscription closed")
    assert "observation=wait_failed" in result
    assert _breaker(issuer) == ({("whoami", 11, ""): 1}, {("whoami", 11): 1})


def test_an_ordinary_genuine_failure_still_reaches_the_breakers_stop_decision(issuer, monkeypatch):
    _SDK(monkeypatch, create=RuntimeError("Access is denied"))

    result = _issue(issuer)

    assert result == "genuine failure — not retrying: Error issuing command 'whoami' to agent 11: Access is denied"
    assert _breaker(issuer) == ({("whoami", 11, ""): 2}, {("whoami", 11): 1})


@pytest.mark.parametrize("seam", ["wait", "create"])
def test_cancellation_still_propagates_from_the_issuer(issuer, monkeypatch, seam):
    _SDK(monkeypatch, **{seam: asyncio.CancelledError()})

    with pytest.raises(asyncio.CancelledError):
        _issue(issuer)

    assert _breaker(issuer) == ({}, {})


# --- collection: through execute_capability, the collector's own handlers and the real issuer ---------------------

def _collection_seam(case, monkeypatch, seam, raised):
    """Raise `raised` at one point of the collector runner's own task (execute_assembly)."""
    if seam == "wait":
        _mythic(case, monkeypatch, outputs={"execute_assembly": raised})
        return
    _mythic(case, monkeypatch)
    if seam == "create":
        fixture_issue = mt.mythic.issue_task

        async def issue(**kwargs):
            if kwargs["command_name"] == "execute_assembly":
                raise raised
            return await fixture_issue(**kwargs)

        monkeypatch.setattr(mt.mythic, "issue_task", issue)
    else:  # the live command-surface read, the fixture's stand-in for that Mythic query
        async def live(command, _callback):
            if command == "execute_assembly":
                raise raised
            return {"status": "available"}

        monkeypatch.setattr(case.client, "_authenticate_live_command", live)


@pytest.mark.parametrize("control", CONTROL)
@pytest.mark.parametrize("seam", ["wait", "create", "live_command"])
def test_a_control_exception_inside_the_collector_task_propagates_out_of_execute_capability(collection, monkeypatch,
                                                                                         seam, control):
    case = collection
    raised = CONTROL[control]()
    _collection_seam(case, monkeypatch, seam, raised)
    _approve(case, ACTION)

    with pytest.raises(type(raised)) as caught:
        _run(case, ACTION)

    assert caught.value is raised
    issued = ["whoami", "ticket_cache_list"] + (["execute_assembly"] if seam == "wait" else [])
    assert _commands(case) == issued
    assert ("ingest", "upload_bytes") not in case.events
    assert _breaker(case.client) == ({}, {})
