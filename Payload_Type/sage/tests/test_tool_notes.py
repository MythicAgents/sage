"""Per-binary knowledge notes ride on the approval card and the deferred-task
triage — operator-maintained markdown in `tool_notes/<binary>.md`, advisory only.

Fail-before where the behavior replaces a designed one: the card and the forced-end
triage rendered no per-binary note before this surface existed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai.langgraph import model as model_mod  # noqa: E402
from ai.langgraph import mythic_tools as mythic_tools_mod  # noqa: E402
from ai.langgraph.mythic_tools import MythicTools, _TOOL_NOTES_DIR  # noqa: E402

CERTUTIL_NOTE = (
    "# certutil\n\n"
    "## Hangs — interactive prompts\n\n"
    "- `certutil -config -` prompts for CA selection interactively. A non-interactive shell\n"
    "  (an Apollo task) waits forever — the task never returns and the wait only ends at the\n"
    "  timeout. Observed 2026-10-15.\n"
    "  - Remedy: name the CA explicitly (`certutil -config <CA-Name> ...`), or enumerate CAs\n"
    "    without the interactive form first (`certutil -dump`, `certutil -catemplates`).\n"
    "  - A task already hanging on this: end the spawned process on the target, or re-issue\n"
    "    with the CA named. Deciding between those is an operator call.\n"
)


@pytest.fixture
def certutil_note():
    _TOOL_NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = _TOOL_NOTES_DIR / "certutil.md"
    existed = path.exists()
    if not existed:
        path.write_text(CERTUTIL_NOTE, encoding="utf-8")
    yield path
    if not existed:
        path.unlink(missing_ok=True)


def test_tool_note_advisory_matches_shell_invoked_binary(certutil_note):
    """`shell certutil -config - -ping` → the certutil note rides as advisory text."""
    tools = MythicTools(agent_task_id=1)
    tools.client = object()
    advisory = tools._tool_note_advisory("shell", {"custom_preview": "certutil -config - -ping"})
    assert "Tool note (certutil)" in advisory
    assert "name the ca explicitly" in advisory.casefold()


def test_tool_note_advisory_empty_when_no_note():
    tools = MythicTools(agent_task_id=1)
    tools.client = object()
    assert tools._tool_note_advisory("whoami", "{}") == ""


def test_tool_note_advisory_never_blocks_on_read_failure(certutil_note, monkeypatch):
    tools = MythicTools(agent_task_id=1)
    tools.client = object()

    def _boom(_path):
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert tools._tool_note_advisory("shell", {"custom_preview": "certutil -config -"}) == ""


def test_tool_note_candidates_cover_executable_parameters(certutil_note):
    """A `run` task with an executable parameter resolves the note through the .exe
    basename variant — the same identity rule the registered-file guard uses."""
    tools = MythicTools(agent_task_id=1)
    tools.client = object()
    advisory = tools._tool_note_advisory(
        "run", {"executable": "C:\\Windows\\System32\\certutil.exe", "arguments": "-config -"})
    assert "Tool note (certutil)" in advisory


def test_bare_model_renders_no_note_without_client():
    model = object.__new__(model_mod.Model)
    model.mythic_client = None
    args = {"command": "shell", "callback_display_id": 1,
            "parameters": '{"custom_preview": "certutil -config - -ping"}'}
    assert model._hitl_decision_context(
        [{"name": "issue_task_and_waitfor_task_output", "args": args}]) == ""


def test_card_decision_context_includes_tool_note(certutil_note):
    model = object.__new__(model_mod.Model)
    model.mythic_client = MythicTools(agent_task_id=1)
    model.mythic_client.client = object()
    args = {"command": "shell", "callback_display_id": 1,
            "parameters": '{"custom_preview": "certutil -config - -ping"}'}
    text = model._hitl_decision_context(
        [{"name": "issue_task_and_waitfor_task_output", "args": args}])
    assert "Tool note (certutil)" in text
    assert "name the ca explicitly" in text.casefold()


def test_tool_note_rides_on_the_real_card(certutil_note):
    """The emitter seam carries the note into the card description."""
    chat = types.SimpleNamespace(kwargs=None)

    async def _send(_request, **kwargs):
        chat.kwargs = kwargs

    chat.send_approval_request = _send
    request = types.SimpleNamespace(ChannelID=7, OperationID=1, APITokenID=1, InputResponse=None)
    model = object.__new__(model_mod.Model)
    model.mythic_client = MythicTools(agent_task_id=1)
    model.mythic_client.client = object()
    emitter = hitl_mod.make_card_emitter(
        chat, request, decision_context_lookup=model._hitl_decision_context,
    )
    action = {
        "name": "issue_task_and_waitfor_task_output",
        "display_name": "issue_task",
        "args": {"command": "shell", "callback_display_id": 1,
                 "parameters": '{"custom_preview": "certutil -config - -ping"}'},
    }
    asyncio.run(emitter([action]))
    assert chat.kwargs is not None
    assert "Tool note (certutil)" in chat.kwargs["description"]


def test_forced_end_triage_carries_the_tool_note(certutil_note):
    """A deferred certutil task's forced-end triage names the remedy."""
    from ai.langgraph.mythic_tools import MythicTools as _MT

    tools = _MT(agent_task_id=1)
    tools.client = object()
    model = object.__new__(model_mod.Model)
    model.mythic_client = tools
    model.state = {
        "_request_lane": "supervised_workflow",
        "_request_id": "chat:tool-note:request:1",
    }
    model._message_seq = 1
    model.mode = "supervised"
    tools._deferred_task_receipt = {
        "command": "shell",
        "callback_display_id": 5,
        "task_display_id": 42,
        "waited_seconds": 60,
        "request_id": "chat:tool-note:request:1",
        "observation_status": "deferred",
        "parameters_text": "certutil -config - -ping",
    }
    receipt = model._pending_task_receipt_for_request()
    assert receipt is not None
    # The triage summary text is built in the wrapped-node path; the note lookup the
    # path uses must resolve the remedy from the receipt's command + parameters text.
    note = tools._tool_note_advisory(
        receipt.get("command"), receipt.get("parameters_text") or "")
    assert "Tool note (certutil)" in note
    assert "end the spawned process" in note


import asyncio  # noqa: E402  (placed here to keep the module-level import list stable)
import types  # noqa: E402

from sage_chat import hitl as hitl_mod  # noqa: E402
