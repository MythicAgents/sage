"""Answered requests must not be re-delegated until the no-progress stop.

Two supervised-run failures ended an answered request with a no-progress stop:

- A forged-ticket request for one domain, with a distinct `source_domain`, was keyed to the source domain's
  already-proven effect, so it returned "already achieved" for the wrong domain. Every re-delegation then
  ended before its model call because the earlier terminal capability result still sat in the worker channel.
- A BloodHound answer reached the Supervisor framed as unverified with no graph observations to compare it
  with, so the Supervisor kept re-delegating a finished analysis.

The scripted-graph checks prove transport, not that a real model then calls respond_to_user.
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from ai.langgraph import capabilities
from ai.langgraph import model as model_module
from ai.langgraph import mythic_tools
from tests import test_scripted_handoff as harness_module


# ── an earlier delegation's terminal capability result does not end a re-delegation ─────


def _seq(message, seq):
    message.additional_kwargs["_seq"] = seq
    return message


def _delegation(seq, text="Forge a ticket for the target domain."):
    return _seq(HumanMessage(content=text, additional_kwargs={"_delegated_to": "Mythic_Operator"}), seq)


def _capability_call(call_id="cap-1"):
    return AIMessage(content="", tool_calls=[{
        "name": "execute_capability", "args": {"action": "forge-golden-ticket"},
        "id": call_id, "type": "tool_call",
    }])


def _capability_result(call_id="cap-1"):
    return ToolMessage(
        content=json.dumps({"ok": True, "verdict": "achieved", "capability": "forge-golden-ticket",
                            "stopped_after": "already_achieved"}),
        name="execute_capability", tool_call_id=call_id,
    )


def _middleware_update(messages):
    middleware = model_module._BoundedExecuteCapabilityStopMiddleware(model=object())
    return middleware._before_model_update({"messages": messages})


def test_terminal_result_in_this_delegation_still_ends_the_loop():
    # Created inside the current react loop, so still untagged.
    assert _middleware_update([_delegation(5), _capability_call(), _capability_result()]) == {"jump_to": "end"}


def test_earlier_delegation_terminal_result_does_not_end_a_redelegation():
    messages = [
        _delegation(5), _seq(_capability_call(), 6), _seq(_capability_result(), 7),
        _seq(AIMessage(content="Already achieved."), 8),
        _delegation(9),
    ]
    assert _middleware_update(messages) is None


def test_reordered_invocation_copy_is_bounded_by_sequence_not_position():
    # The supervised path can move earlier returns after the repeated operator input.
    messages = [
        _delegation(9), _seq(_capability_call(), 6), _seq(_capability_result(), 7),
    ]
    assert _middleware_update(messages) is None


def test_redelegation_that_reaches_a_new_terminal_result_ends_again():
    messages = [
        _delegation(5), _seq(_capability_call("cap-1"), 6), _seq(_capability_result("cap-1"), 7),
        _delegation(9), _capability_call("cap-2"), _capability_result("cap-2"),
    ]
    assert _middleware_update(messages) == {"jump_to": "end"}


def test_follow_on_tool_block_uses_the_same_delegation_boundary():
    middleware = model_module._BoundedExecuteCapabilityStopMiddleware(model=object())
    stale = [_delegation(5), _seq(_capability_call(), 6), _seq(_capability_result(), 7), _delegation(9),
             AIMessage(content="", tool_calls=[{"name": "list_callbacks", "args": {}, "id": "cb-1",
                                                "type": "tool_call"}])]
    request = type("Request", (), {"state": {"messages": stale},
                                   "tool_call": {"name": "list_callbacks", "id": "cb-1", "args": {}}})()
    assert middleware._pre_tool_block_reason(request) is None


# ── a forged ticket for a distinct target domain is keyed to that target ──────────────────


def _resolve(action, inputs):
    tools = mythic_tools.MythicTools(agent_task_id="offline-forge-domains")
    return tools._capability_tool_action(action, tools._capability_tool_inputs(inputs), capabilities)


# The supervised run's exact argument shapes, with key material replaced.
_RUN_ACTION = json.dumps({"capability": "forge-golden-ticket", "domain": "essos.local",
                          "account": "Administrator", "callback_id": "1"})
_RUN_INPUTS = json.dumps({"source_domain": "sevenkingdoms.local", "domain_sid": "S-1-5-21-1-2-3",
                          "aes256": "0" * 64, "extra_sids": "S-1-5-21-4-5-6-519"})


def test_distinct_domain_with_source_domain_targets_the_requested_domain():
    action = _resolve(_RUN_ACTION, _RUN_INPUTS)
    assert action.effects == ["da:essos.local"]
    assert action.target == "domain=sevenkingdoms.local;target_domain=essos.local"


def test_requested_domain_effect_is_not_satisfied_by_the_source_domain_proof():
    tools = mythic_tools.MythicTools(agent_task_id="offline-forge-achieved")
    action = tools._capability_tool_action(_RUN_ACTION, tools._capability_tool_inputs(_RUN_INPUTS),
                                           capabilities)
    assert not tools._capability_action_effects_achieved(action, achieved_effects={"da:sevenkingdoms.local"})
    assert tools._capability_action_effects_achieved(action, achieved_effects={"da:essos.local"})


@pytest.mark.parametrize("action, inputs, effects, target", [
    # Same-domain forge: domain alone names the krbtgt domain.
    ({"capability": "forge-golden-ticket", "domain": "corp.local"}, {},
     ["da:corp.local"], "domain=corp.local"),
    # source_domain equal to domain is not a cross-domain request.
    ({"capability": "forge-golden-ticket", "domain": "corp.local"}, {"source_domain": "corp.local"},
     ["da:corp.local"], "domain=corp.local"),
    # An explicit target_domain still wins.
    ({"capability": "forge-golden-ticket", "domain": "child.corp.local", "target_domain": "corp.local"}, {},
     ["da:corp.local"], "domain=child.corp.local;target_domain=corp.local"),
    # source_domain alone keeps its domain.
    ({"capability": "forge-golden-ticket"}, {"source_domain": "corp.local"},
     ["da:corp.local"], "domain=corp.local"),
])
def test_forge_domain_near_matches_keep_their_meaning(action, inputs, effects, target):
    resolved = _resolve(json.dumps(action), json.dumps(inputs))
    assert resolved.effects == effects
    assert resolved.target == target


def test_plan_uses_source_domain_for_the_ticket_and_requires_extra_sids_for_the_target():
    action = _resolve(_RUN_ACTION, _RUN_INPUTS)
    plan = capabilities._build_forge_golden_ticket_execution_plan(action, json.loads(_RUN_INPUTS))
    assert plan.ok
    forge = next(step for step in plan.steps if step.operation == "kerberos-ticket-forge")
    parameters = forge.parameters
    assert parameters["domain"] == "sevenkingdoms.local"
    assert parameters["target_domain"] == "essos.local"
    without_sids = dict(json.loads(_RUN_INPUTS))
    without_sids.pop("extra_sids")
    blocked = capabilities._build_forge_golden_ticket_execution_plan(
        _resolve(_RUN_ACTION, json.dumps(without_sids)), without_sids,
    )
    assert not blocked.ok and "extra_sids" in blocked.missing


# ── BloodHound graph observations reach the Supervisor with the answer ───────────────────


def _graph_call(call_id="graph-1", name="graph_analysis"):
    return AIMessage(content="", tool_calls=[{
        "name": name, "args": {"query": "shortest path to Domain Admins"}, "id": call_id, "type": "tool_call",
    }])


def _graph_result(call_id="graph-1", name="graph_analysis", content="PATH: USER-A -[MemberOf]-> GROUP-B"):
    return ToolMessage(content=content, name=name, tool_call_id=call_id)


def _graph_project(messages):
    return model_module._worker_graph_evidence_message(
        messages, request_id="chat:1:request:1", worker="BloodHound",
    )


def test_graph_projection_pairs_calls_with_results():
    evidence = _graph_project([_graph_call(), _graph_result(), AIMessage(content="Options: …")])
    assert evidence is not None
    assert evidence.additional_kwargs == {"_worker_graph_evidence": True, "_request_id": "chat:1:request:1"}
    assert evidence.content.startswith("<untrusted_bloodhound_query_observations>\n")
    record = json.loads(evidence.content.splitlines()[-2])[0]
    assert record["source_tool"] == "graph_analysis"
    assert record["source_tool_call_id"] == "graph-1"
    assert "USER-A -[MemberOf]-> GROUP-B" in record["result_excerpt"]
    assert "not proof that any effect was achieved" in evidence.content


@pytest.mark.parametrize("messages", [
    [AIMessage(content="I already have the analysis; no need to re-query.")],          # prose only
    [_graph_call(), AIMessage(content="answer")],                                        # call, no result
    [_graph_result()],                                                                   # result, no call
    [_graph_call(name="issue_task"), _graph_result(name="issue_task")],                  # not a graph tool
])
def test_graph_projection_needs_a_paired_graph_query(messages):
    assert _graph_project(messages) is None


def test_graph_projection_is_bounded():
    messages = []
    for index in range(20):
        messages += [_graph_call(f"g-{index}"), _graph_result(f"g-{index}", content="x" * 5000)]
    evidence = _graph_project(messages)
    records = json.loads(evidence.content.splitlines()[-2])
    assert len(records) <= model_module._GRAPH_EVIDENCE_MAX_RECORDS
    assert sum(len(r["result_excerpt"]) for r in records) <= model_module._GRAPH_EVIDENCE_TOTAL_CHARS
    assert records[-1]["source_tool_call_id"] == "g-19" and all(r["truncated"] for r in records)


def _capture(monkeypatch):
    seen = []
    original = harness_module._ScriptedModel._generate

    def generate(self, messages, *args, **kwargs):
        seen.append(list(messages))
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(harness_module._ScriptedModel, "_generate", generate)
    monkeypatch.setattr(harness_module, "_RECURSION_LIMIT", 24)
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))
    return seen


def _run_bloodhound(monkeypatch, bloodhound_script):
    seen = _capture(monkeypatch)
    from ai.mcp import MCPManager
    from ai.langgraph.model import Model

    @tool
    def graph_analysis(query: str) -> str:
        """Run an inert offline graph query."""
        return "PATH: USER-A -[MemberOf]-> GROUP-B -[GenericAll]-> DC-1"

    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _server: [graph_analysis]))
    monkeypatch.setattr(Model, "_bloodhound_server_is_locally_pinned", lambda self, server: server == "offline-graph")
    script = [
        AIMessage(content="", tool_calls=[{"name": "transfer_to_BloodHound", "args": {
            "handoff_title": "Find paths", "handoff_instruction": "Find a path to Domain Admins and list options."},
            "id": "route-1", "type": "tool_call"}]),
        *bloodhound_script,
        AIMessage(content="", tool_calls=[{"name": "respond_to_user", "args": {
            "final_response": "Options are listed above.", "outcome": "completed"},
            "id": "final-1", "type": "tool_call"}]),
    ]
    harness = harness_module._Harness(script)
    result = asyncio.run(harness.run("Use BloodHound to find a path and tell me our options.", assisted=True))
    return seen, result


def test_assembled_bloodhound_return_carries_graph_observations_to_supervisor(monkeypatch):
    seen, result = _run_bloodhound(monkeypatch, [
        _graph_call(), AIMessage(content="Option 1: GenericAll on DC-1 via GROUP-B."),
    ])
    supervisor_after_return = seen[-1]
    evidence = [m for m in supervisor_after_return if m.additional_kwargs.get("_worker_graph_evidence")]
    assert len(evidence) == 1
    assert "GROUP-B -[GenericAll]-> DC-1" in evidence[0].content
    assert '"source_tool_call_id": "graph-1"' in evidence[0].content
    assert "Option 1: GenericAll on DC-1" in "\n".join(str(m.content) for m in supervisor_after_return)
    final = [m for m in result["supervisor_messages"] if m.additional_kwargs.get("_is_final_report")]
    assert [m.content for m in final] == ["Options are listed above."]


def test_assembled_bloodhound_prose_only_return_carries_no_graph_observations(monkeypatch):
    seen, _ = _run_bloodhound(monkeypatch, [AIMessage(content="Option 1 from memory.")])
    assert not [m for m in seen[-1] if m.additional_kwargs.get("_worker_graph_evidence")]
