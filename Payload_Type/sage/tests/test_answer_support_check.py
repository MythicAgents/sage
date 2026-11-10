"""Returned answers are checked against their source observations before publication.

The assembled episodes drive the real native chat service, Supervisor, BloodHound and Mythic_Operator workers,
graph tool wrapper, answer check and final-report path. Only the chat model is scripted, including the checker's
verdicts. These prove wiring and enforcement, not model judgment.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from tests import test_scripted_handoff as harness_module
from tests.test_assignment_proposal_check import call, delegate, episode, propose, verdict

REQUEST = "chat:1:request:1"
PATH = "PATH: SAMWELL.TARLY -[MemberOf]-> GROUP-B -[GenericAll]-> DOMAIN ADMINS"
TOKEN = f"graph:{REQUEST}:graph-1"
ANSWER = f"SAMWELL.TARLY reaches DOMAIN ADMINS through GROUP-B ({TOKEN})."
FABRICATED = f"SAMWELL.TARLY is AdminTo DC01 ({TOKEN})."


def support(verdict_value="supported", assignment="satisfied", citations=(), reason="scripted"):
    return AIMessage(content=json.dumps({"verdict": verdict_value, "assignment": assignment, "reason": reason,
                                         "citations": list(citations)}))


GOOD_CITATION = {"claim": "SAMWELL.TARLY reaches DOMAIN ADMINS through GROUP-B", "source_id": TOKEN,
                 "quote": "SAMWELL.TARLY -[MemberOf]-> GROUP-B -[GenericAll]-> DOMAIN ADMINS"}


def route(call_id="route-bh", title="Find a path to Domain Admins"):
    return call("transfer_to_BloodHound", {"handoff_title": title, "handoff_instruction": title}, call_id)


def graph_call(call_id="graph-1"):
    return call("graph_analysis", {"query": "shortest path SAMWELL.TARLY to DOMAIN ADMINS"}, call_id)


def final(text="", reuse=False, outcome="completed", call_id="final-1"):
    args = {"outcome": outcome}
    if reuse:
        args["reuse_latest_worker_answer"] = True
    else:
        args["final_response"] = text
    return call("respond_to_user", args, call_id)


def bh_episode(monkeypatch, tmp_path, script, prompt="Using only the existing graph, report a path to Domain Admins."):
    queries = []

    @tool
    def graph_analysis(query: str) -> str:
        """Run an inert offline graph query."""
        queries.append(query)
        return PATH

    result = episode(monkeypatch, tmp_path, script, prompt=prompt, graph_tools=[graph_analysis])
    result["queries"] = queries
    return result


def _events(model, kind):
    return [event for event in model._request_event_ledger.events if event.kind == kind and not event.projected]


def _checks(model, kind="answer_check"):
    return [json.loads(event.content) for event in _events(model, kind)]


def test_a_supported_cited_graph_answer_is_published_and_its_exact_reuse_needs_no_second_check(
        monkeypatch, tmp_path):
    out = bh_episode(monkeypatch, tmp_path, [
        route(), graph_call(), AIMessage(content=ANSWER),
        support(citations=[GOOD_CITATION]),
        final(reuse=True),
    ])
    checks = _checks(out["model"])
    assert [(c["verdict"], c["binding"]["worker"]) for c in checks] == [("supported", "BloodHound")]
    assert out["text"].count("reaches DOMAIN ADMINS through GROUP-B") >= 1
    finals = [m for m in out["state"]["supervisor_messages"] if m.additional_kwargs.get("_is_final_report")]
    assert [m.content for m in finals] == [ANSWER]


def test_a_fabricated_edge_on_a_real_call_gets_one_correction_then_a_non_completion_report(monkeypatch, tmp_path):
    out = bh_episode(monkeypatch, tmp_path, [
        route(), graph_call(), AIMessage(content=FABRICATED),
        support("unsupported", "unknown", reason="no source shows AdminTo DC01"),
        AIMessage(content=FABRICATED),
        AIMessage(content=json.dumps({"verdict": "supported", "assignment": "satisfied", "reason": "x",
                                      "citations": [{"claim": "SAMWELL.TARLY is AdminTo DC01", "source_id": TOKEN,
                                                     "quote": "SAMWELL.TARLY -[AdminTo]-> DC01"}]})),
        final("Sage could not confirm a path."),
        support("supported", "blocked", citations=[]),
    ])
    checks = _checks(out["model"])
    assert [c["verdict"] for c in checks][:2] == ["unsupported", "unavailable"]
    assert len(_events(out["model"], "assignment_check_correction")) == 1
    # The rejected draft never reaches the operator or the Supervisor as an accepted answer.
    assert "AdminTo DC01" not in out["text"]
    returns = [m for m in out["state"]["supervisor_messages"]
               if m.name == "BloodHound" and m.additional_kwargs.get("_answer_check")]
    assert returns and "did not complete this step with a supported answer" in returns[-1].content
    assert not any(m.additional_kwargs.get("_worker_final_answer") for m in returns)


def test_a_handback_summary_is_the_checked_candidate():
    from langchain_core.messages import ToolMessage
    from ai.langgraph.model import _worker_return_candidate

    summary = "Task 1 reported the identity of callback 7 (task 1)."
    messages = [
        AIMessage(content="Interim prose.", tool_calls=[{"name": "handback_to_supervisor", "id": "hb-1",
                  "type": "tool_call", "args": {"reason": "done", "summary": summary, "outcome": "complete",
                                                "next_owner": ""}}]),
        ToolMessage(content="Handed back to Supervisor.", name="handback_to_supervisor", tool_call_id="hb-1",
                    additional_kwargs={"_handback_input": {"reason": "done", "summary": summary,
                                                           "outcome": "complete", "next_owner": ""}}),
    ]
    for seq, message in enumerate(messages, start=5):
        message.additional_kwargs["_seq"] = seq
    assert _worker_return_candidate(messages, "Mythic_Operator", "turn-1") == summary
    assert _worker_return_candidate([AIMessage(content="Final prose.")], "BloodHound", "") == "Final prose."


def test_an_unsupported_final_report_gets_one_correction_then_is_replaced(monkeypatch, tmp_path):
    out = bh_episode(monkeypatch, tmp_path, [
        route(), graph_call(), AIMessage(content=ANSWER), support(citations=[GOOD_CITATION]),
        final("SAMWELL.TARLY already controls the domain."),
        support("unsupported", "unknown", reason="control is not observed"),
        final("SAMWELL.TARLY still controls the domain.", call_id="final-2"),
        support("unsupported", "unknown", reason="control is not observed"),
    ])
    supervisor_checks = [c for c in _checks(out["model"]) if c["binding"]["worker"] == "Supervisor"]
    assert [c["verdict"] for c in supervisor_checks] == ["unsupported", "unsupported"]
    finals = [m for m in out["state"]["supervisor_messages"] if m.additional_kwargs.get("_is_final_report")]
    assert len(finals) == 1 and "controls the domain" not in finals[0].content
    assert finals[0].additional_kwargs["_turn_outcome"] == "blocked"
    assert "controls the domain" not in out["text"]


def test_a_direct_supervisor_answer_after_a_checked_return_is_checked(monkeypatch, tmp_path):
    out = bh_episode(monkeypatch, tmp_path, [
        route(), graph_call(), AIMessage(content=ANSWER), support(citations=[GOOD_CITATION]),
        AIMessage(content="The domain is fully compromised."),
        support("unsupported", "unknown", reason="no compromise is observed"),
    ])
    assert "fully compromised" not in out["text"]
    assert [c["binding"]["worker"] for c in _checks(out["model"])] == ["BloodHound", "Supervisor"]


def test_an_executed_read_whose_expected_result_is_absent_is_not_completed_credit(monkeypatch, tmp_path):
    out = episode(monkeypatch, tmp_path, [
        delegate("Report callback 7's current process identity."),
        propose("whoami"), verdict("consistent"),
        AIMessage(content="The task ran."),
        support("supported", "partial", citations=[], reason="the identity is not in the output"),
        final("The identity read ran; its identity value was not observed.", outcome="blocked"),
        support("supported", "blocked", citations=[]),
    ], prompt="Report callback 7's current process identity with one read.", accept=True)
    returns = [m for m in out["state"]["supervisor_messages"]
               if m.name == "Mythic_Operator" and m.additional_kwargs.get("_answer_check")]
    assert returns and returns[-1].additional_kwargs["_answer_check"]["assignment"] == "partial"
    assert not returns[-1].additional_kwargs.get("_worker_final_answer")


def test_a_worker_without_a_checked_proposal_keeps_its_prose_and_makes_no_check(monkeypatch, tmp_path):
    out = episode(monkeypatch, tmp_path, [
        call("transfer_to_Mythic_Operator", {"handoff_title": "Explain", "handoff_instruction": "Explain callbacks.",
                                             "allowed_capabilities": ["control_plane"],
                                             "expected_result": "An explanation."}, "route-explain"),
        AIMessage(content="Callbacks are agent sessions."),
        final("Callbacks are agent sessions."),
    ], prompt="Explain what a callback is.")
    assert _checks(out["model"]) == []
    assert "Callbacks are agent sessions." in out["text"]


SOURCES = {TOKEN: f"graph_analysis {{}} (success; collection request {REQUEST})\n{PATH}"}


@pytest.mark.parametrize("citation,reason", [
    ({"claim": "not in the answer", "source_id": TOKEN, "quote": "GROUP-B"}, "claim"),
    ({"claim": "SAMWELL.TARLY reaches", "source_id": "graph:invented:1", "quote": "GROUP-B"}, "source"),
    ({"claim": "SAMWELL.TARLY reaches", "source_id": TOKEN, "quote": "-[AdminTo]-> DC01"}, "quote"),
])
def test_the_strict_support_parser_rejects_invented_provenance(citation, reason):
    from ai.langgraph import assignment_check

    text = json.dumps({"verdict": "supported", "assignment": "satisfied", "reason": "r", "citations": [citation]})
    with pytest.raises(ValueError, match=reason):
        assignment_check.parse_support_verdict(text, ANSWER, SOURCES)


@pytest.mark.parametrize("text", [
    '{"verdict": "supported", "assignment": "satisfied", "reason": "r", "citations": []}',
    '{"verdict": "supported", "verdict": "supported", "assignment": "partial", "reason": "r", "citations": []}',
    '{"verdict": "supported", "assignment": "done", "reason": "r", "citations": []}',
    '{"verdict": "supported", "assignment": "partial", "reason": "r"}',
])
def test_the_strict_support_parser_rejects_malformed_shapes(text):
    from ai.langgraph import assignment_check

    with pytest.raises(ValueError):
        assignment_check.parse_support_verdict(text, ANSWER, SOURCES)


@pytest.mark.parametrize("checked_proposal", [False, True])
def test_held_prose_is_released_on_an_approval_pause_only_without_a_checked_proposal(checked_proposal):
    from langchain_core.messages import HumanMessage
    from langchain_core.outputs import ChatGeneration, LLMResult
    from langgraph.errors import GraphInterrupt
    from tests.test_native_chat_multiturn_state import _install_contract_without_graph_rebuild, _new_model

    request_id = "chat:channel-isolation:request:1"
    published = []

    async def run():
        model = _new_model()
        _install_contract_without_graph_rebuild(model, request_id)
        model.mode = "supervised"

        async def record(text):
            published.append(text)
            return True

        model._stream_message_to_mythic = record
        operator = HumanMessage(content="Do it.", additional_kwargs={
            "_request_id": request_id, "_operator_input": True, "_operator_input_revision": 0, "_seq": 1})
        delegated = HumanMessage(content="Do it.", additional_kwargs={
            "_delegated_to": "Mythic_Operator", "_handoff_request_id": request_id, "_seq": 2,
            "_assisted_assignment": "Do it."})
        model.state["supervisor_messages"] = [operator]
        if checked_proposal:
            scope = model._assignment_check_scope("Mythic_Operator", [delegated])
            model._record_assignment_check(scope, {"delegation": scope["delegation"]}, verdict="consistent",
                                           reason="ok", source="checker")

        class Agent:
            async def ainvoke(self, payload, config=None):
                capture = config["callbacks"][-1]
                await capture.on_llm_end(LLMResult(generations=[[ChatGeneration(
                    message=AIMessage(content="Interim worker prose."))]]), run_id=__import__("uuid").uuid4())
                raise GraphInterrupt(())

        state = {"_request_id": request_id, "_request_lane": "supervised_workflow",
                 "supervisor_messages": [operator], "mythic_operator_messages": [delegated], "messages": []}
        wrapped = model._wrap_create_agent(Agent(), "mythic_operator_messages", "Mythic_Operator")
        with pytest.raises(GraphInterrupt):
            await wrapped(state, {})

    asyncio.run(run())
    released = any("Interim worker prose." in text for text in published)
    assert released is (not checked_proposal)


# ── Cold-review reproductions (2026-11-10): returns through handback control tools, partial finals ──

HANDBACK_FABRICATED = "Callback 7 runs as NORTH\\eddard.stark, a Domain Admin (task 1)."


def test_a_handback_to_supervisor_return_is_checked_corrected_once_then_replaced(monkeypatch, tmp_path):
    handback = lambda call_id: call("handback_to_supervisor", {  # noqa: E731
        "reason": "done", "summary": HANDBACK_FABRICATED, "outcome": "complete", "next_owner": ""}, call_id)
    out = episode(monkeypatch, tmp_path, [
        delegate("Report callback 7's current process identity."),
        propose("whoami"), verdict("consistent"),
        handback("hb-1"), support("unsupported", "unknown", reason="no source shows the identity"),
        handback("hb-2"),  # identical resubmission: the earlier rejection is reused, no second checker call
        final("Sage could not confirm callback 7's identity.", outcome="blocked"),
        support("supported", "blocked", citations=[]),
    ], prompt="Report callback 7's current process identity with one read.", accept=True)
    worker_checks = [c for c in _checks(out["model"]) if c["binding"]["worker"] == "Mythic_Operator"]
    assert [c["verdict"] for c in worker_checks] == ["unsupported"]
    assert len(_events(out["model"], "assignment_check_correction")) == 1
    assert any("did not complete this step with a supported answer" in str(m.content)
               for m in out["state"]["supervisor_messages"])
    assert "eddard.stark" not in out["text"]
    assert not any("eddard.stark" in str(m.content) for m in out["state"]["supervisor_messages"]
                   if m.name != "Mythic_Operator" or m.additional_kwargs.get("_worker_outcome"))


def test_a_bloodhound_summarize_and_handback_return_is_checked(monkeypatch, tmp_path):
    progress = f"SAMWELL.TARLY is AdminTo DC01 ({TOKEN})."
    handback = call("summarize_and_handback", {"progress_summary": progress, "tasks_remaining": "none"}, "sh-1")
    out = bh_episode(monkeypatch, tmp_path, [
        route(), graph_call(), handback, support("unsupported", "unknown", reason="no AdminTo edge observed"),
        call("summarize_and_handback", {"progress_summary": progress, "tasks_remaining": "none"}, "sh-2"),
    ])
    assert [c["verdict"] for c in _checks(out["model"])][:1] == ["unsupported"]
    assert len(_events(out["model"], "assignment_check_correction")) == 1
    assert "AdminTo DC01" not in out["text"]


def test_a_supported_partial_final_is_not_reported_completed(monkeypatch, tmp_path):
    out = episode(monkeypatch, tmp_path, [
        delegate("Report callback 7's current process identity."),
        propose("whoami"), verdict("consistent"),
        AIMessage(content="The task ran."),
        support("supported", "partial", citations=[], reason="the identity is not in the output"),
        final("The identity read ran.", outcome="completed"),
        support("supported", "partial", citations=[], reason="the identity value is still not observed"),
    ], prompt="Report callback 7's current process identity with one read.", accept=True)
    assert out["model"].request_turn_outcome() != "completed"
    [final_message] = [m for m in out["state"]["supervisor_messages"] if m.additional_kwargs.get("_is_final_report")]
    assert final_message.additional_kwargs["_answer_check"]["assignment"] == "partial"
