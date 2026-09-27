"""Provider filtering through the real graph, Model.invoke and native service."""

import asyncio

import pytest
from langchain_core.messages import AIMessage

import sage_chat.session as chat_session
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from .test_sage_chat_scaffold import _DriverChat
from .test_scripted_handoff import _Harness, _ScriptedModel, _handoff_call


@pytest.fixture(autouse=True)
def _isolated_native_channel_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))


@pytest.mark.parametrize("filtered_agent", ["Supervisor", "Mythic_Operator"])
@pytest.mark.parametrize("filtered_content", ["", "Filtered text must not be delivered as an answer."])
def test_native_filtered_response_is_one_error_without_redelegation(monkeypatch, filtered_agent, filtered_content):
    calls = []
    original = _ScriptedModel._generate

    def generate(self, messages, *args, **kwargs):
        calls.append(messages)
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(_ScriptedModel, "_generate", generate)

    async def run():
        filtered = AIMessage(content=filtered_content, response_metadata={"finish_reason": "content_filter"})
        script = ([filtered] if filtered_agent == "Supervisor" else [_handoff_call(), filtered])
        harness = _Harness(script)
        model = harness._build(script)
        model.command_name = "chat"
        model._rebuild_graph()
        chat = _DriverChat(model)
        request = build_chat_request("Report the stored observation.", channel_id=1,
                                     operation_id=1, request_id=91, config={"mode": "conversation"})
        bind_channel_thread_id(request, model)
        await chat.chat(request)
        return model, harness, chat

    model, harness, chat = asyncio.run(run())
    assert len(calls) == (1 if filtered_agent == "Supervisor" else 2)
    assert len(chat.terminal_emissions) == 1
    terminal = chat.terminal_emissions[0]
    assert terminal["kind"] == "error"
    assert terminal["complete_request"] is True
    assert "content_filter" in terminal["error"]
    rendered = str(chat.emissions)
    assert "Completed without a textual summary" not in rendered
    assert "Try again with modified parameters" not in rendered
    assert "different model" not in rendered
    if filtered_content:
        assert filtered_content not in rendered
    assert not harness.mythic.issue_task.called
    assert not model._active_delegations
    ledger = model._request_event_ledger
    assert ledger.reconcile(require_final=True)["ok"] is True
    terminals = [r for r in ledger.reconstruct_transcript() if r["kind"] == "control_transition"
                 and r["phase"] == "request_terminal"]
    assert len(terminals) == 1 and terminals[0]["content"] == "error"
