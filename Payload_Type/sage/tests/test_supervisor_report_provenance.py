"""An ordinary supervised request with no Sage-recorded step cannot show the operator a Supervisor model closing.

When the current request has no step outcome from Sage's step owner, every model-written closing (direct content or
`respond_to_user`, whatever outcome it declares) is replaced by one fixed notice, recorded as a `blocked` turn. A
real step keeps its closing, whatever the closing says: provenance comes from the step owner, never from the text.

Real: the native chat driver, graph, request contract, `respond_to_user`, catalog binding, approval card and resume,
MythicTools' executor, the step owner, the closing stream and the service's terminal. Synthetic: the ledger state (the
catalog-binding fixtures), Mythic I/O and the scripted model choices. The zero-step closings are recorded Supervisor
outputs (`fixtures/model_input_boundaries/zero_step_closings.json`); scripted replay proves what Sage does with them,
not what a model would write.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket

import pytest
from langchain_core.messages import AIMessage

from tests.test_hitl_steer_current_intent import _call
from tests.test_partial_verdict_report import LEAD, _dcsync_client, _requests
from tests.test_supervised_catalog_step_binding import PROMPT, _isolated, _run, _select

ROWS = {row["id"]: row for row in json.loads(
    (Path(__file__).parent / "fixtures/model_input_boundaries/zero_step_closings.json").read_text())["rows"]}
NOTICE = "Sage has no completed step recorded for this request, so it cannot report an execution result."
READ_BRIEF = {"handoff_title": "read callbacks", "handoff_instruction": PROMPT,
              "allowed_capabilities": ["control_plane"], "expected_result": "The live callbacks."}
STEP_ANSWER = "Callbacks 13 and 14 are live."
CLOSING = "Both callbacks are live, as the step read them from Mythic."


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def _recorded(row_id: str) -> AIMessage:
    return AIMessage(**ROWS[row_id]["message"])


def _respond(text: str, outcome: str | None):
    return _call("respond_to_user", {"final_response": text, **({"outcome": outcome} if outcome else {})}, 9)


def _text(message: AIMessage) -> str:
    """Every model-written string in a proposed closing: its content and any `respond_to_user` argument."""
    content = message.content if isinstance(message.content, str) else "".join(
        block.get("text", "") for block in message.content)
    return "\n".join([content, *(call["args"].get("final_response", "") for call in message.tool_calls)])


R13 = ROWS["request-13"]["message"]["content"]
ZERO_STEP = {
    "request-13": _recorded("request-13"),
    "request-15": _recorded("request-15"),
    "request-25": _recorded("request-25"),
    "request-13-text-blocks": AIMessage(content=[{"type": "text", "text": R13}]),
    "request-13-respond-completed": _respond(R13, "completed"),
    "request-13-respond-omitted": _respond(R13, None),
    "request-13-respond-blocked": _respond(R13, "blocked"),
    "request-13-respond-checkpoint": _respond(R13, "checkpoint"),
    "proposals-json": _respond(json.dumps({"proposals": [{"capability": "forge-golden-ticket"}]}), None),
    "plain-claim": AIMessage(content="I ran DCSync against DC01 and recovered the krbtgt key for lab.local."),
    "quoted-result": AIMessage(content="Task output:\n```\nSAM Username : krbtgt\nHash NTLM: 0123456789abcdef\n```"),
    # The intentional compatibility change: an innocent zero-step answer gets the same notice. No classifier.
    "greeting": AIMessage(content="Hello. I can explain the available workflows."),
}


def _fragments(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if len(line.strip()) >= 12] or [text.strip()]


def _leaks(run, proposed: str) -> list[str]:
    emitted = "\n".join(str(e.get("content") or "") for e in run.emissions)
    return [fragment for fragment in _fragments(proposed) if fragment in emitted]


def _terminal(run) -> str:
    [terminal] = run.terminals
    return str(terminal.get("content") or "").strip()


def _final_response(run) -> str:
    [event] = run.model._request_event_ledger.actual_events(kind="final_response")
    return event.content.strip()


# --- (a) every zero-step closing form gives the same notice, and none of its bytes reach the operator --------------

@pytest.mark.parametrize("case", sorted(ZERO_STEP))
def test_a_zero_step_closing_is_replaced_by_the_notice(monkeypatch, tmp_path, case):
    mt, split = _isolated(monkeypatch, tmp_path / "state")
    run = asyncio.run(_run(mt, split, [ZERO_STEP[case]]))
    assert run.model._request_contract.lane.value == "supervised_workflow"
    assert _terminal(run) == NOTICE and _final_response(run) == NOTICE
    assert run.terminals[0]["metadata"]["turn_outcome"] == "blocked"     # never the model's declared outcome
    assert _leaks(run, _text(ZERO_STEP[case])) == []
    assert len(run.fake.inputs) == 1 and not run.all_cards and not run.issued and run.outcomes == []


@pytest.mark.parametrize("brief", ["refused", "admitted"])
def test_a_transfer_preamble_is_not_streamed(monkeypatch, tmp_path, brief):
    """A refused transfer leaves its text in the Supervisor's node update beside the closing (the streamed case); an
    admitted one leaves the node through the handoff, and its step keeps the closing."""
    preamble = ROWS["request-13"]["message"]["content"]
    args = READ_BRIEF if brief == "admitted" else {"handoff_title": "read callbacks"}
    handoff = _call("transfer_to_Mythic_Operator", args, 1).model_copy(update={"content": preamble})
    script = [handoff, *([AIMessage(content=STEP_ANSWER)] if brief == "admitted" else []), AIMessage(content=CLOSING)]
    mt, split = _isolated(monkeypatch, tmp_path / "state")
    run = asyncio.run(_run(mt, split, script))
    assert _leaks(run, preamble) == []
    assert len(run.outcomes) == (brief == "admitted")
    assert _terminal(run) == (CLOSING if brief == "admitted" else NOTICE)


# --- (b) a real step keeps its closing, whatever the closing says --------------------------------------------------

@pytest.mark.parametrize("ending", ["plain", "handback"])
def test_a_real_read_only_step_keeps_its_closing(monkeypatch, tmp_path, ending):
    worker_end = AIMessage(content=STEP_ANSWER) if ending == "plain" else _call(
        "handback_to_supervisor", {"reason": "read done", "summary": STEP_ANSWER, "outcome": "progress"}, 3)
    mt, split = _isolated(monkeypatch, tmp_path / "state")
    run = asyncio.run(_run(mt, split, [_call("transfer_to_Mythic_Operator", READ_BRIEF, 1),
                                       _call("list_callbacks", {}, 2), worker_end, AIMessage(content=CLOSING)]))
    [outcome] = run.outcomes
    assert outcome["worker"] == "Mythic_Operator" and outcome["tool_calls"] == 1
    # The Supervisor wrote its closing after reading Sage's record of the step.
    assert "[step outcome] Mythic_Operator step ended" in "\n".join(str(m.content) for m in run.fake.inputs[-1])
    assert _terminal(run) == CLOSING and NOTICE not in str(run.emissions)


def test_a_real_catalog_step_keeps_even_a_recorded_fabrication(monkeypatch, tmp_path, mythic_task_records):
    """After a real, approved catalog step (no worker-model call), the same recorded text passes unchanged: the
    boundary is the step record, not the wording."""
    mt, split = _dcsync_client(monkeypatch, tmp_path / "state", mythic_task_records, completion_error=False)
    run = asyncio.run(_run(mt, split, [_select("dcsync-krbtgt"), _recorded("request-15")]))
    assert len(run.cards) == 1 and [row["command_name"] for row in run.issued] == ["dcsync"]
    assert run.model._request_contract.request_id == "chat:1:request:1"   # the resume kept its request
    assert [o["worker"] for o in run.outcomes] == ["Mythic_Operator"]
    assert ROWS["request-15"]["message"]["content"].strip() in _terminal(run) and NOTICE not in _terminal(run)


def test_a_rejected_step_keeps_its_closing(monkeypatch, tmp_path, mythic_task_records):
    mt, split = _dcsync_client(monkeypatch, tmp_path / "state", mythic_task_records)
    closing = "The operator rejected the DCSync; nothing ran."
    # After the rejection the worker answers once (a model call), then the Supervisor closes.
    run = asyncio.run(_run(mt, split, [_select("dcsync-krbtgt"), AIMessage(content="The card was rejected."),
                                       AIMessage(content=closing)], decision="reject"))
    assert run.issued == [] and len(run.outcomes) == 1
    assert _terminal(run).endswith(closing) and NOTICE not in _terminal(run)


# --- (c) request binding, cold restoration, other lanes and Sage-authored finals -----------------------------------

def test_a_later_request_cannot_borrow_an_earlier_step(monkeypatch, tmp_path, mythic_task_records):
    mt, split = _dcsync_client(monkeypatch, tmp_path / "state", mythic_task_records)
    later = "Callback 14 is still live."
    run = asyncio.run(_requests(mt, split, [_select("dcsync-krbtgt"), AIMessage(content="The DCSync ran."),
                                            AIMessage(content=later)], prompts=["Is callback 14 still live?"]))
    first = "\n".join(str(e.get("content") or "") for e in run.emissions[:run.marks[0]])
    second = "\n".join(str(e.get("content") or "") for e in run.emissions[run.marks[0]:])
    assert "The DCSync ran." in first and NOTICE not in first
    assert NOTICE in second and later not in second and LEAD not in second


def _emit(model, message: AIMessage) -> str:
    emitted: list[str] = []

    async def emit(text):
        emitted.append(text)
        return True

    model._response_emitter = emit
    asyncio.run(model._process_stream_event({"Supervisor": {"supervisor_messages": [message]}}))
    return "".join(emitted).strip()


def test_cold_restoration_reads_the_checkpoint_or_stays_unverified(monkeypatch, tmp_path):
    """Synthetic loss of the in-memory record after a real step: the checkpoint's Sage outcome for this request still
    counts; an unreadable checkpoint leaves the closing unverified."""
    mt, split = _isolated(monkeypatch, tmp_path / "state")
    run = asyncio.run(_run(mt, split, [_call("transfer_to_Mythic_Operator", READ_BRIEF, 1),
                                       AIMessage(content=STEP_ANSWER), AIMessage(content=CLOSING)]))
    assert len(run.outcomes) == 1
    run.model._request_step_outcomes = []
    assert _emit(run.model, AIMessage(content="Restored closing.", name="Supervisor")) == "Restored closing."

    async def unreadable(*_args, **_kwargs):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(run.model.graph, "aget_state", unreadable)
    assert _emit(run.model, AIMessage(content="Restored closing.", name="Supervisor")) == NOTICE


def test_sage_authored_finals_keep_their_facts(monkeypatch, tmp_path):
    mt, split = _isolated(monkeypatch, tmp_path / "state")
    run = asyncio.run(_run(mt, split, [ZERO_STEP["greeting"]]))
    blocker = AIMessage(content="Sage stopped this request.", name="Supervisor",
                        additional_kwargs={"_is_final_report": True, "_turn_outcome": "blocked"})
    emitted = _emit(run.model, blocker)
    assert emitted.startswith("⛔ **Blocked**\nSage stopped this request.") and NOTICE not in emitted


@pytest.mark.parametrize("lane", ["conversation", "autonomous_solve"])
def test_other_lanes_are_unchanged(monkeypatch, tmp_path, lane):
    mt, split = _isolated(monkeypatch, tmp_path / "state")
    kwargs = {"mode": "conversation"} if lane == "conversation" else {"autonomous_solve": True}
    run = asyncio.run(_run(mt, split, [ZERO_STEP["greeting"]], **kwargs))
    assert NOTICE not in str(run.emissions)
    if lane == "conversation":
        assert _terminal(run) == "Hello. I can explain the available workflows."
