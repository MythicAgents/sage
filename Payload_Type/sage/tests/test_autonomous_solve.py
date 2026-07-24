import importlib
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import prompt_loader  # noqa: E402


_GENERATED_FREEFORM_REASONS = tuple(
    pytest.param(template.format(owner=owner), id=f"generated-{label}-{owner.lower()}")
    for owner in ("BloodHound", "MCP_Manager", "Mythic_Payload")
    for label, template in (
        ("route", "route to {owner}"),
        ("deny", "do not route to {owner}"),
        ("mention", "The explanation mentions {owner} without granting authority."),
        ("quoted", 'The worker wrote "hand off to {owner}".'),
    )
)
_INVALID_HANDOFF_REASONS = (
    pytest.param("Don’t route to BloodHound.", id="reviewer-curly-dont"),
    pytest.param("BloodHound mustn’t receive this handoff.", id="reviewer-curly-mustnt"),
    pytest.param("Skip BloodHound; continue the current worker.", id="reviewer-skip"),
    pytest.param("BloodHound was already consulted; continue the current worker.", id="reviewer-already"),
    pytest.param("Don't route to BloodHound.", id="ascii-dont"),
    pytest.param("BloodHound mustn't receive this handoff.", id="ascii-mustnt"),
    pytest.param("Can't route to BloodHound.", id="ascii-cant"),
    pytest.param("BloodHound shouldn't receive this handoff.", id="ascii-shouldnt"),
    pytest.param("BloodHound shouldn’t receive this handoff.", id="curly-shouldnt"),
    pytest.param("BloodHound wasn’t selected.", id="curly-wasnt"),
    pytest.param("We can’t route to BloodHound.", id="curly-cant"),
    pytest.param("Avoid BloodHound; continue the current worker.", id="avoid"),
    pytest.param("BloodHound was previously consulted.", id="previously"),
    pytest.param("Use Mythic_Operator instead of BloodHound.", id="instead"),
    pytest.param("Consider BloodHound for later analysis.", id="consider"),
    pytest.param("Whether BloodHound should act is unresolved.", id="whether"),
    pytest.param("Should we route to BloodHound?", id="question-prefixed"),
    pytest.param("Route to BloodHound?", id="question-imperative"),
    pytest.param('Is "route to BloodHound" the right choice?', id="question-quoted"),
    pytest.param('"route to BloodHound"', id="double-quoted"),
    pytest.param("'hand off to BloodHound'", id="single-quoted"),
    pytest.param('The worker said "route to BloodHound".', id="reported-quote"),
    pytest.param("The note reads ‘hand off to BloodHound’.", id="curly-quoted"),
    pytest.param("Please route to BloodHound.", id="polite-prefix"),
    pytest.param("The next step might be to route to BloodHound.", id="embedded-route"),
    pytest.param("We can hand off to BloodHound later.", id="embedded-handoff"),
    pytest.param("Context: route to BloodHound.", id="colon-not-boundary"),
    pytest.param("Context, route to BloodHound.", id="comma-not-boundary"),
    pytest.param("Route to BloodHound, but do not transfer control.", id="postposed-comma"),
    pytest.param("Route to BloodHound. Continue the current worker instead.", id="postposed-sentence"),
    pytest.param("Route to BloodHound; skip the handoff.", id="postposed-semicolon"),
    pytest.param(
        "Route to BloodHound to analyze the graph. Continue Mythic_Operator instead.",
        id="postposed-after-purpose",
    ),
    pytest.param("route to BloodHound to avoid using the graph", id="purpose-avoid"),
    pytest.param("route to BloodHound to not use the graph", id="purpose-not"),
    pytest.param("route to BloodHound to skip graph analysis", id="purpose-skip"),
    pytest.param("rоute to BloodHound to analyze the graph", id="unicode-directive-lookalike"),
    pytest.param("route to BloodHound to analyzе the graph", id="unicode-purpose-lookalike"),
    pytest.param("Route to BloodHound to analyze the graph…", id="unicode-purpose-punctuation"),
    pytest.param("route to BloodHoundExtra", id="owner-suffix"),
    pytest.param("route to bloodhound", id="owner-case"),
    pytest.param("route to UnknownOwner", id="unknown-owner"),
    pytest.param("hand-off to BloodHound", id="hyphenated-handoff"),
    pytest.param("route toward BloodHound", id="wrong-preposition"),
    pytest.param("route to BloodHound immediately", id="malformed-suffix"),
    pytest.param("route to BloodHound and MCP_Manager", id="multiple-owner-suffix"),
    pytest.param("route to BloodHound to consult MCP_Manager", id="multiple-owner-purpose"),
    pytest.param(
        "Context mentions Mythic_Operator; route to BloodHound to analyze the graph",
        id="multiple-owner-context",
    ),
    pytest.param("route to BloodHound; hand off to MCP_Manager", id="multiple-directive-owner"),
    pytest.param(
        "route to BloodHound to analyze, then route to MCP_Manager",
        id="multiple-directive-purpose",
    ),
    pytest.param(
        "Context; route to BloodHound. hand off to BloodHound",
        id="multiple-directive-same-owner",
    ),
    pytest.param("route to Mythic_Operator", id="same-owner"),
    pytest.param(
        (
            "BloodHound logon-session query is the next required step per operator steering; "
            "route to BloodHound to check for <target-user> sessions before any further credential acquisition."
        ),
        id="exact-real-fixture-without-owner",
    ),
    *_GENERATED_FREEFORM_REASONS,
)

_VALID_HANDOFF_REASONS = (
    pytest.param(
        "Mythic_Operator",
        "BloodHound",
        (
            "BloodHound logon-session query is the next required step per operator steering; "
            "route to BloodHound to check for <target-user> sessions before any further credential acquisition."
        ),
        id="exact-real-fixture",
    ),
    pytest.param("Mythic_Operator", "BloodHound", "route to BloodHound", id="simple-route"),
    pytest.param(
        "Mythic_Operator",
        "BloodHound",
        "Route to BloodHound to analyze the graph",
        id="route-purpose",
    ),
    pytest.param(
        "Mythic_Operator",
        "MCP_Manager",
        "Context is complete; hand off to MCP_Manager to query the connected service",
        id="handoff-purpose",
    ),
    pytest.param(
        "Mythic_Operator",
        "BloodHound",
        "Context is complete. Route to BloodHound to analyze the graph",
        id="period-boundary",
    ),
    pytest.param("BloodHound", "Mythic_Operator", "route to Mythic_Operator", id="other-source"),
)

_MALFORMED_TYPED_OWNERS = (
    pytest.param("bloodhound", id="case-folded"),
    pytest.param("BloodHoundExtra", id="suffixed"),
    pytest.param("BloodHound,MCP_Manager", id="multiple-string"),
    pytest.param(["BloodHound"], id="list"),
    pytest.param(("BloodHound",), id="tuple"),
    pytest.param({"owner": "BloodHound"}, id="mapping"),
    pytest.param(7, id="integer"),
    pytest.param(None, id="none"),
    pytest.param("Mythic_Operator", id="self"),
)


def _load_model_class():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        mod = importlib.import_module("ai.langgraph.model")
    except Exception as e:
        pytest.skip(f"model.py runtime unavailable: {e}")
    return mod.Model


def _load_model_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        return importlib.import_module("ai.langgraph.model")
    except Exception as e:
        pytest.skip(f"model.py runtime unavailable: {e}")


def _engagement_modules():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        return (
            importlib.import_module("ai.langgraph.engagement_state"),
            importlib.import_module("ai.langgraph.mythic_tools"),
        )
    except Exception as e:
        pytest.skip(f"engagement modules unavailable: {e}")


def _fake_hop(es, effect, evidence=None):
    return es.Hop(
        id=f"seed:{effect}",
        technique="seed",
        target="seed",
        effect=effect,
        status="achieved",
        evidence=evidence or {"provenance": "test"},
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp="2026-06-16T12:00:00Z",
    )


def _fake_fact(es, predicate):
    return es.GraphFact(
        predicate=predicate,
        source="bloodhound:test",
        timestamp="2026-06-16T12:00:00Z",
        ttl_seconds=600,
    )


def _fake_foothold(es, forest="north.sevenkingdoms.local", callback_id="3"):
    return es.Foothold(
        callback_id=callback_id,
        agent="apollo",
        host="CASTELBLACK",
        forest=forest,
        identity="NORTH\\samwell.tarly",
        integrity="medium",
        alive=True,
        source="test",
        timestamp="2026-06-16T12:00:00Z",
    )


@pytest.mark.parametrize("autonomous_solve", [False, True])
@pytest.mark.parametrize(
    "instruction",
    [
        (
            "Continue by executing exactly one next grounded capability action using "
            "the generic execute_capability tool. Retry at most once, then stop."
        ),
        (
            "Call execute_capability exactly once for ensure-kerberos-context on callback 2. "
            "Stop after the result."
        ),
        (
            "Call execute_capability exactly once for ensure-kerberos-context on callback 2; "
            "report the single result and stop."
        ),
    ],
)
def test_bounded_one_action_execute_capability_result_ends_graph(autonomous_solve, instruction):
    mod = _load_model_module()
    Model = mod.Model

    class FakeAgent:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, args, config=None):
            self.calls += 1
            payload = {
                "ok": False,
                "verdict": "failed",
                "capability": "ensure-account-kerberos-context",
                "reason": "account key material is missing",
                "issued": [],
                "recorded_effects": [],
            }
            return {
                "messages": list(args["messages"]) + [
                    ToolMessage(
                        content=json.dumps(payload, sort_keys=True),
                        name="execute_capability",
                        tool_call_id="call_1",
                    ),
                    AIMessage(content="Executor verdict: failed.", name="Mythic_Operator"),
                ]
            }

    m = Model.__new__(Model)
    m._autonomous_solve = autonomous_solve
    m._message_seq = 3
    m.state = {"_message_seq": 3}
    m.llm = None
    m.mythic_client = None

    channel = [HumanMessage(content=instruction)]
    state = {
        "_message_seq": 3,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": channel,
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
    }
    fake = FakeAgent()

    wrapped = m._wrap_create_agent(fake, "mythic_operator_messages", "Mythic_Operator")
    result = asyncio.run(wrapped(state, {}))

    assert fake.calls == 1
    assert getattr(result, "goto", None) == mod.END
    update = getattr(result, "update", {})
    assert "supervisor_messages" in update
    assert update["recursion_handback"] is True
    assert update["supervisor_messages"][0].additional_kwargs["_is_final_report"] is True
    assert "Executor verdict: `failed`" in update["supervisor_messages"][0].content
    assert "account key material is missing" in update["supervisor_messages"][0].content
    assert "This was a bounded one-action capability request" in update["supervisor_messages"][0].content


def test_worker_handoff_prefers_terminal_execute_capability_report_over_narration():
    mod = _load_model_module()
    Model = mod.Model

    class FakeAgent:
        async def ainvoke(self, args, config=None):
            payload = {
                "ok": True,
                "verdict": "achieved",
                "capability": "ensure-kerberos-context",
                "reason": "requested capability effect verified",
                "issued": [
                    {"task_id": 79, "command": "run"},
                    {"task_id": 80, "command": "ls"},
                ],
                "recorded_effects": ["kerberos-context:north.sevenkingdoms.local@callback:2"],
                "achieved_effects": ["kerberos-context:north.sevenkingdoms.local@callback:2"],
            }
            return {
                "messages": list(args["messages"]) + [
                    AIMessage(
                        content="Callback 2 is alive. I am using deterministic ensure-kerberos-context now.",
                        name="Mythic_Operator",
                        tool_calls=[{
                            "name": "execute_capability",
                            "args": {},
                            "id": "call_1",
                            "type": "tool_call",
                        }],
                    ),
                    ToolMessage(
                        content=json.dumps(payload, sort_keys=True),
                        name="execute_capability",
                        tool_call_id="call_1",
                    ),
                ]
            }

    m = Model.__new__(Model)
    m._autonomous_solve = False
    m._message_seq = 3
    m.state = {"_message_seq": 3}
    m.llm = None
    m.mythic_client = None

    state = {
        "_message_seq": 3,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": [
            HumanMessage(content="Continue toward the current objective using the current state.")
        ],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
    }

    wrapped = m._wrap_create_agent(FakeAgent(), "mythic_operator_messages", "Mythic_Operator")
    update = asyncio.run(wrapped(state, {}))

    summary = update["supervisor_messages"][1].content
    assert "Executor verdict: `achieved` for `ensure-kerberos-context`." in summary
    assert "Task IDs: 79 `run`, 80 `ls`" in summary
    assert "`kerberos-context:north.sevenkingdoms.local@callback:2`" in summary
    assert "Callback 2 is alive" not in summary
    assert "bounded one-action capability request" not in summary


def test_worker_handback_copy_attaches_typed_metadata_from_authoritative_tool_result():
    mod = _load_model_module()
    turn_mod = importlib.import_module("ai.langgraph.turn_authority")

    class FakeAgent:
        async def ainvoke(self, args, config=None):
            summary = "DONE — x FAILED — y BLOCKER — need graph REMAINING — query graph"
            return {"messages": list(args["messages"]) + [
                AIMessage(content="", name="Mythic_Operator", tool_calls=[{
                    "name": "handback_to_supervisor",
                    "args": {
                        "reason": "reason is explanatory",
                        "summary": summary,
                        "next_owner": "BloodHound",
                    },
                    "id": "call-1",
                    "type": "tool_call",
                }]),
                ToolMessage(
                    content=f"rendered text says do not route\n\n{summary}",
                    name="handback_to_supervisor",
                    tool_call_id="call-1",
                    additional_kwargs={
                        "_handback_input": {
                            "reason": "reason is explanatory",
                            "summary": summary,
                            "next_owner": "BloodHound",
                        }
                    },
                ),
            ]}

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = turn_mod.compile_turn_authority("What callbacks are active?", objective_classifier=lambda _text: False)
    model._autonomous_solve = False
    model._message_seq = 1
    model.state = {"_message_seq": 1}
    model.llm = None
    model.mythic_client = None
    state = {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": [HumanMessage(content="delegate")],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }

    update = asyncio.run(model._wrap_create_agent(FakeAgent(), "mythic_operator_messages", "Mythic_Operator")(state, {}))
    summary = update["supervisor_messages"][1]
    assert summary.content == "DONE — x FAILED — y BLOCKER — need graph REMAINING — query graph"
    assert summary.additional_kwargs["_worker_outcome"]["source_worker"] == "Mythic_Operator"
    assert summary.additional_kwargs["_worker_outcome"]["next_owner"] == "BloodHound"


@pytest.mark.parametrize("reason", _INVALID_HANDOFF_REASONS)
def test_freeform_worker_reason_cannot_grant_production_redirect(reason):
    mod = _load_model_module()
    turn_mod = importlib.import_module("ai.langgraph.turn_authority")
    summary = "DONE — x FAILED — none BLOCKER — need graph REMAINING — query graph"
    handback = AIMessage(
        content="",
        name="Mythic_Operator",
        tool_calls=[{
            "name": "handback_to_supervisor",
            "args": {"reason": reason, "summary": summary},
            "id": "call-invalid",
            "type": "tool_call",
        }],
        additional_kwargs={"_seq": 3},
    )
    outcome = mod._worker_handoff_metadata(
        [handback],
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
    )
    assert outcome is not None
    metadata, authoritative_summary = outcome
    assert metadata["outcome"] == "blocked"
    assert metadata["next_owner"] == ""
    assert authoritative_summary == summary

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = turn_mod.TurnAuthority(mode="observe", turn_id="turn-1")
    state = {
        "messages": [],
        "supervisor_messages": [
            HumanMessage(content="operator turn"),
            AIMessage(
                content=summary,
                name="Mythic_Operator",
                additional_kwargs={"_worker_outcome": metadata},
            ),
        ],
        "mythic_operator_messages": [],
        "generalist_messages": [],
    }
    assert model._latest_admitted_worker_handoff(state) == (metadata, summary)
    tool = mod._create_handoff_tool(
        agent_name="Generalist",
        worker_outcome_lookup=model._latest_admitted_worker_handoff,
    )
    command = tool.func(
        SimpleNamespace(state=state, tool_call_id="handoff-invalid"),
        "perform new Mythic work",
    )
    assert command.goto == "Generalist"
    assert command.goto != "BloodHound"


@pytest.mark.parametrize(
    "summary",
    (
        "DONE — x FAILED — none BLOCKER — none REMAINING — none",
        "DONE — none FAILED — failed action BLOCKER — none REMAINING — none",
        "DONE — none FAILED — none BLOCKER — none REMAINING — none",
    ),
)
def test_contradictory_worker_handback_cannot_reach_production_redirect(summary):
    mod = _load_model_module()
    turn_mod = importlib.import_module("ai.langgraph.turn_authority")
    handback = AIMessage(
        content="",
        name="Mythic_Operator",
        tool_calls=[{
            "name": "handback_to_supervisor",
            "args": {
                "reason": "reason is explanatory",
                "summary": summary,
                "next_owner": "BloodHound",
            },
            "id": "call-contradictory",
            "type": "tool_call",
        }],
        additional_kwargs={"_seq": 3},
    )
    assert mod._worker_handoff_metadata(
        [handback],
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
    ) is None

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = turn_mod.TurnAuthority(mode="observe", turn_id="turn-1")
    state = {
        "messages": [],
        "supervisor_messages": [
            HumanMessage(content="operator turn"),
            AIMessage(content=summary, name="Mythic_Operator"),
        ],
        "mythic_operator_messages": [],
        "bloodhound_messages": [],
    }
    assert model._latest_admitted_worker_handoff(state) is None
    command = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        worker_outcome_lookup=model._latest_admitted_worker_handoff,
    ).func(
        SimpleNamespace(state=state, tool_call_id="handoff-contradictory"),
        "perform new Mythic work",
    )
    assert command.goto == "Mythic_Operator"


@pytest.mark.parametrize(("source_worker", "next_owner", "reason"), _VALID_HANDOFF_REASONS)
def test_explicit_worker_handback_redirects_with_authoritative_summary(
    source_worker,
    next_owner,
    reason,
):
    mod = _load_model_module()
    turn_mod = importlib.import_module("ai.langgraph.turn_authority")
    summary = "DONE — reviewed state FAILED — none BLOCKER — need next owner REMAINING — perform next step"
    handback = AIMessage(
        content="",
        name=source_worker,
        tool_calls=[{
            "name": "handback_to_supervisor",
            "args": {
                "reason": reason,
                "summary": summary,
                "next_owner": next_owner,
            },
            "id": "call-valid",
            "type": "tool_call",
        }],
        additional_kwargs={"_seq": 3},
    )
    outcome = mod._worker_handoff_metadata(
        [handback],
        source_worker=source_worker,
        source_turn_id="turn-1",
    )
    assert outcome is not None
    metadata, authoritative_summary = outcome
    assert metadata["outcome"] == "handoff"
    assert metadata["next_owner"] == next_owner
    assert authoritative_summary == summary

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = turn_mod.TurnAuthority(mode="observe", turn_id="turn-1")
    state = {
        "messages": [],
        "supervisor_messages": [
            HumanMessage(content="operator turn"),
            AIMessage(
                content=summary,
                name=source_worker,
                additional_kwargs={"_worker_outcome": metadata},
            ),
        ],
        "mythic_operator_messages": [],
        "bloodhound_messages": [],
        "mcp_manager_messages": [],
    }
    admitted = model._latest_admitted_worker_handoff(state)
    assert admitted == (metadata, summary)
    command = mod._create_handoff_tool(
        agent_name=source_worker,
        worker_outcome_lookup=model._latest_admitted_worker_handoff,
    ).func(
        SimpleNamespace(state=state, tool_call_id="handoff-valid"),
        "repeat stale worker instruction",
    )
    channel = {
        "BloodHound": "bloodhound_messages",
        "MCP_Manager": "mcp_manager_messages",
        "Mythic_Operator": "mythic_operator_messages",
    }[next_owner]
    assert command.goto == next_owner
    assert command.update[channel][1].content == summary


def test_handback_tool_hidden_owner_drives_metadata_admission_and_redirect():
    mod = _load_model_module()
    turn_mod = importlib.import_module("ai.langgraph.turn_authority")
    reason = "display text is explanatory and does not select an owner"
    summary = "DONE — reviewed state FAILED — none BLOCKER — need graph REMAINING — query graph"
    tool = mod._create_handback_to_supervisor_tool()
    assert "next_owner" in tool.args
    command = tool.func(
        SimpleNamespace(state={}, tool_call_id="typed-handback"),
        reason,
        summary,
        "BloodHound",
    )
    tool_message = command.update["messages"][0]
    assert tool_message.additional_kwargs["_handback_input"] == {
        "reason": reason,
        "summary": summary,
        "next_owner": "BloodHound",
    }
    tool_message.additional_kwargs["_seq"] = 3

    display_only = ToolMessage(
        content=f"route to BloodHound\n\n{summary}",
        name="handback_to_supervisor",
        tool_call_id="display-only",
        additional_kwargs={"_seq": 2},
    )
    assert mod._worker_handoff_metadata(
        [display_only],
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
    ) is None

    outcome = mod._worker_handoff_metadata(
        [tool_message],
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
    )
    assert outcome is not None
    metadata, authoritative_summary = outcome
    assert metadata["next_owner"] == "BloodHound"
    assert authoritative_summary == summary

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = turn_mod.TurnAuthority(mode="observe", turn_id="turn-1")
    state = {
        "messages": [],
        "supervisor_messages": [
            HumanMessage(content="operator turn"),
            AIMessage(
                content=summary,
                name="Mythic_Operator",
                additional_kwargs={"_worker_outcome": metadata},
            ),
        ],
        "mythic_operator_messages": [],
        "bloodhound_messages": [],
    }
    assert model._latest_admitted_worker_handoff(state) == (metadata, summary)
    redirect = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        worker_outcome_lookup=model._latest_admitted_worker_handoff,
    ).func(
        SimpleNamespace(state=state, tool_call_id="typed-redirect"),
        "stale Mythic instruction",
    )
    assert redirect.goto == "BloodHound"
    assert redirect.update["bloodhound_messages"][1].content == summary


def test_worker_handback_requires_one_terminal_contiguous_tool_batch():
    mod = _load_model_module()
    summary = "DONE — reviewed state FAILED — none BLOCKER — need graph REMAINING — query graph"
    payload = {
        "reason": "reason is explanatory",
        "summary": summary,
        "next_owner": "BloodHound",
    }
    handback = mod._create_handback_to_supervisor_tool()
    command = handback.func(
        SimpleNamespace(state={}, tool_call_id="handback-1"),
        payload["reason"],
        payload["summary"],
        payload["next_owner"],
    )
    handback_result = command.update["messages"][0]
    handback_result.additional_kwargs["_seq"] = 4
    handback_call = AIMessage(
        content="",
        name="Mythic_Operator",
        tool_calls=[{
            "name": "handback_to_supervisor",
            "args": payload,
            "id": "handback-1",
            "type": "tool_call",
        }],
        additional_kwargs={"_seq": 3},
    )
    prior_call = AIMessage(
        content="",
        name="Mythic_Operator",
        tool_calls=[{
            "name": "get_callbacks",
            "args": {},
            "id": "prior-1",
            "type": "tool_call",
        }],
        additional_kwargs={"_seq": 1},
    )
    prior_result = ToolMessage(
        content="callback inventory",
        name="get_callbacks",
        tool_call_id="prior-1",
        additional_kwargs={"_seq": 2},
    )

    admitted = mod._worker_handoff_metadata(
        [prior_call, prior_result, handback_call, handback_result],
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
    )
    assert admitted is not None
    assert admitted[0]["next_owner"] == "BloodHound"
    assert admitted[1] == summary

    later_result = ToolMessage(
        content="objective is complete; graph handoff is stale",
        name="observe",
        tool_call_id="observe-1",
        additional_kwargs={"_seq": 5},
    )
    interposed_result = ToolMessage(
        content="intervening observation",
        name="observe",
        tool_call_id="observe-1",
        additional_kwargs={"_seq": 4},
    )
    mismatched_id = handback_result.model_copy(
        update={"tool_call_id": "wrong-handback"}
    )
    mismatched_payload = handback_result.model_copy(
        update={
            "additional_kwargs": {
                **handback_result.additional_kwargs,
                "_handback_input": {
                    **payload,
                    "next_owner": "MCP_Manager",
                },
            }
        }
    )
    rejected_batches = (
        [handback_call, handback_result, later_result],
        [handback_call, interposed_result, handback_result],
        [handback_call, mismatched_id],
        [handback_call, mismatched_payload],
        [prior_result, handback_result],
    )
    for messages in rejected_batches:
        assert mod._worker_handoff_metadata(
            messages,
            source_worker="Mythic_Operator",
            source_turn_id="turn-1",
        ) is None


def test_concurrent_toolnode_shaped_handbacks_fail_closed():
    mod = _load_model_module()
    summary = "DONE — reviewed state FAILED — none BLOCKER — need graph REMAINING — query graph"
    handback = mod._create_handback_to_supervisor_tool()
    first_payload = {
        "reason": "first typed route",
        "summary": summary,
        "next_owner": "BloodHound",
    }
    second_payload = {
        "reason": "second typed route",
        "summary": summary,
        "next_owner": "MCP_Manager",
    }
    first_result = handback.func(
        SimpleNamespace(state={}, tool_call_id="handback-1"),
        first_payload["reason"],
        first_payload["summary"],
        first_payload["next_owner"],
    ).update["messages"][0]
    second_result = handback.func(
        SimpleNamespace(state={}, tool_call_id="handback-2"),
        second_payload["reason"],
        second_payload["summary"],
        second_payload["next_owner"],
    ).update["messages"][0]
    sibling_batch = AIMessage(
        content="",
        name="Mythic_Operator",
        tool_calls=[
            {
                "name": "handback_to_supervisor",
                "args": first_payload,
                "id": "handback-1",
                "type": "tool_call",
            },
            {
                "name": "observe",
                "args": {},
                "id": "observe-1",
                "type": "tool_call",
            },
        ],
        additional_kwargs={"_seq": 1},
    )
    observation_result = ToolMessage(
        content="objective is complete",
        name="observe",
        tool_call_id="observe-1",
        additional_kwargs={"_seq": 3},
    )
    two_handbacks = AIMessage(
        content="",
        name="Mythic_Operator",
        tool_calls=[
            {
                "name": "handback_to_supervisor",
                "args": first_payload,
                "id": "handback-1",
                "type": "tool_call",
            },
            {
                "name": "handback_to_supervisor",
                "args": second_payload,
                "id": "handback-2",
                "type": "tool_call",
            },
        ],
        additional_kwargs={"_seq": 1},
    )
    for messages in (
        [sibling_batch, first_result, observation_result],
        [two_handbacks, first_result, second_result],
    ):
        assert mod._worker_handoff_metadata(
            messages,
            source_worker="Mythic_Operator",
            source_turn_id="turn-1",
        ) is None


@pytest.mark.parametrize("batch_kind", ("command-and-dict", "two-commands"))
def test_real_toolnode_concurrent_handback_batches_fail_closed(batch_kind):
    mod = _load_model_module()
    summary = "DONE — reviewed state FAILED — none BLOCKER — need graph REMAINING — query graph"
    handback = mod._create_handback_to_supervisor_tool()

    @tool
    def observe() -> dict:
        """Return a later observation from a concurrent sibling call."""
        return {"objective_complete": True}

    first = {
        "name": "handback_to_supervisor",
        "args": {
            "reason": "first typed route",
            "summary": summary,
            "next_owner": "BloodHound",
        },
        "id": "handback-1",
        "type": "tool_call",
    }
    if batch_kind == "command-and-dict":
        calls = [
            first,
            {
                "name": "observe",
                "args": {},
                "id": "observe-1",
                "type": "tool_call",
            },
        ]
        node = ToolNode([handback, observe])
        expected_types = ["Command", "dict"]
    else:
        calls = [
            first,
            {
                "name": "handback_to_supervisor",
                "args": {
                    "reason": "second typed route",
                    "summary": summary,
                    "next_owner": "MCP_Manager",
                },
                "id": "handback-2",
                "type": "tool_call",
            },
        ]
        node = ToolNode([handback])
        expected_types = ["Command", "Command"]

    request = AIMessage(
        content="",
        name="Mythic_Operator",
        tool_calls=calls,
        additional_kwargs={"_seq": 1},
    )
    outputs = asyncio.run(node._afunc({"messages": [request]}, {}, Runtime()))
    assert [type(output).__name__ for output in outputs] == expected_types

    captured = [request]
    for output in outputs:
        update = output if isinstance(output, dict) else output.update
        captured.extend(update["messages"])
    for index, message in enumerate(captured, start=1):
        message.additional_kwargs.setdefault("_seq", index)
    assert mod._worker_handoff_metadata(
        captured,
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
    ) is None


@pytest.mark.parametrize(
    "args",
    (
        {"summary": "DONE — x FAILED — none BLOCKER — need graph REMAINING — query graph", "next_owner": "BloodHound"},
        {"reason": "typed route", "next_owner": "BloodHound"},
        {"reason": 7, "summary": "DONE — x FAILED — none BLOCKER — need graph REMAINING — query graph", "next_owner": "BloodHound"},
        {"reason": "typed route", "summary": ["not", "a", "string"], "next_owner": "BloodHound"},
        {"reason": "typed route", "summary": "DONE — x FAILED — none BLOCKER — need graph REMAINING — query graph", "next_owner": "BloodHound", "extra": True},
    ),
)
def test_ai_only_handback_must_match_required_tool_argument_schema(args):
    mod = _load_model_module()
    message = AIMessage(
        content="",
        name="Mythic_Operator",
        tool_calls=[{
            "name": "handback_to_supervisor",
            "args": args,
            "id": "invalid-ai-only",
            "type": "tool_call",
        }],
        additional_kwargs={"_seq": 3},
    )
    assert mod._worker_handoff_metadata(
        [message],
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
    ) is None


@pytest.mark.parametrize("message_source", ("ai", "tool"))
@pytest.mark.parametrize("next_owner", _MALFORMED_TYPED_OWNERS)
def test_malformed_typed_owner_fails_closed_from_every_authoritative_source(
    message_source,
    next_owner,
):
    mod = _load_model_module()
    summary = "DONE — reviewed state FAILED — none BLOCKER — need graph REMAINING — query graph"
    payload = {
        "reason": "route to BloodHound",
        "summary": summary,
        "next_owner": next_owner,
    }
    if message_source == "ai":
        message = AIMessage(
            content="",
            name="Mythic_Operator",
            tool_calls=[{
                "name": "handback_to_supervisor",
                "args": payload,
                "id": "malformed-ai",
                "type": "tool_call",
            }],
            additional_kwargs={"_seq": 3},
        )
    else:
        message = ToolMessage(
            content=f"route to BloodHound\n\n{summary}",
            name="handback_to_supervisor",
            tool_call_id="malformed-tool",
            additional_kwargs={"_seq": 3, "_handback_input": payload},
        )
    assert mod._worker_handoff_metadata(
        [message],
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
    ) is None


def test_completed_worker_summary_hands_back_to_supervisor_without_emitting_final_report():
    mod = _load_model_module()
    Model = mod.Model

    class FakeAgent:
        async def ainvoke(self, args, config=None):
            return {
                "messages": list(args["messages"]) + [
                    AIMessage(
                        content=(
                            "DONE — Current callbacks are callback 1 on CASTELBLACK and callback 2 on BRAAVOS.\n"
                            "FAILED — none.\n"
                            "BLOCKER — none.\n"
                            "REMAINING — all done / no further action required."
                        ),
                        name="Mythic_Operator",
                    )
                ]
            }

    m = Model.__new__(Model)
    m._autonomous_solve = False
    m._message_seq = 3
    m.state = {"_message_seq": 3}
    m.llm = None
    m.mythic_client = None

    state = {
        "_message_seq": 3,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": [HumanMessage(content="List current callbacks")],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
    }

    wrapped = m._wrap_create_agent(FakeAgent(), "mythic_operator_messages", "Mythic_Operator")
    update = asyncio.run(wrapped(state, {}))

    assert isinstance(update, dict)
    assert getattr(update, "goto", None) is None
    assert len(update["supervisor_messages"]) == 2
    assert update["supervisor_messages"][0].additional_kwargs["_is_completion_header"] is True
    assert "_is_final_report" not in update["supervisor_messages"][1].additional_kwargs
    assert "REMAINING — all done / no further action required." in update["supervisor_messages"][1].content


def test_autonomous_handoff_redirects_stale_gpo_redelegation_to_bloodhound():
    mod = _load_model_module()
    state = {
        "supervisor_messages": [
            ToolMessage(
                content=(
                    "🔄 **Handback to Supervisor** — Need graph-supported next hop selection.\n\n"
                    "**DONE (do NOT repeat):** STARKWALLPAPER GPO hop completed; net group "
                    "\"Domain Admins\" /domain includes samwell.tarly; PAC refresh proven on callback:3.\n"
                    "**FAILED (do NOT blindly retry):** sevenkingdoms krbtgt DCSync failed twice with "
                    "0x20f7/8439.\n"
                    "**BLOCKER / MISSING CAPABILITY:** Need graph-supported next action for Essos path; "
                    "route to BloodHound."
                ),
                name="handback_to_supervisor",
                tool_call_id="h1",
            )
        ],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Execute the next graph-supported hop: use SharpGPOAbuse on STARKWALLPAPER GPO to add "
            "samwell.tarly to Domain Admins with net group."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "BloodHound"
    assert "callback 3" in instruction
    assert "Do not repeat the STARKWALLPAPER/GPO hop" in instruction
    assert "0x20f7/8439" in instruction


def test_autonomous_handoff_redirects_verified_collection_regression_to_current_failure():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content=json.dumps({
                    "ok": True,
                    "graph_verified": True,
                    "job_status": "Complete",
                    "domains": ["north.sevenkingdoms.local", "sevenkingdoms.local"],
                }),
                name="ingest_collection",
                tool_call_id="ingest_1",
            ),
            ToolMessage(
                content=json.dumps({
                    "ok": False,
                    "verdict": "failed",
                    "capability": "gpo-controlled-system-exec",
                    "reason": "PowerShell Put operation failed while updating the GPO object.",
                    "issued": [{"task_id": 6, "command": "powerpick"}],
                }),
                name="execute_capability",
                tool_call_id="cap_1",
            ),
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Continue the objective. BloodHound domain_info was empty and SharpHound was started, but "
            "completion and ZIP path are not yet confirmed. Do not rerun SharpHound. List C:\\Users\\Public, "
            "download the exact ZIP, run ingest_collection, verify graph_verified, then stop."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "graph_verified=true" in instruction
    assert "Do not perform SharpHound collection confirmation" in instruction
    assert "`gpo-controlled-system-exec` returned `failed`" in instruction
    assert "task(s) 6" in instruction
    assert "Do not regress to collection work" in instruction


def test_autonomous_handoff_redirects_verified_collection_regression_to_bloodhound_without_failure():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content='{"ok": true, "graph_verified": true, "job_status": "Complete"}',
                name="ingest_collection",
                tool_call_id="ingest_1",
            ),
        ],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        "SharpHound completion and ZIP path are not confirmed; download the ZIP and ingest_collection.",
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "BloodHound"
    assert redirect.title == "Analyze verified graph"
    assert "already shows `graph_verified=true`" in instruction
    assert "Analyze the verified BloodHound graph" in instruction


def test_post_ingest_handback_routes_directly_to_bloodhound_without_supervisor():
    mod = _load_model_module()
    es, _ = _engagement_modules()
    foothold = _fake_foothold(es, callback_id="2")
    state = es.EngagementState(
        objective="obtain verified administrative control of target.local",
        footholds=[foothold],
    )
    access_key = es.access_context_key(state, foothold)

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = [_fake_hop(
            es,
            f"graph-built:{access_key}",
            {
                "provenance": "test",
                "graph_verified": True,
                "covered_domains": ["north.sevenkingdoms.local"],
            },
        )]
        _engagement_graph_facts = []

        def _engagement_objective(self):
            return "obtain verified administrative control of target.local"

    tool = mod._create_handback_to_supervisor_tool(FakeMythic(), autonomous=True)
    runtime = SimpleNamespace(state={}, tool_call_id="post-ingest")

    command = tool.func(
        runtime,
        "Graph analysis is required next.",
        "Collection and ingest completed; determine the next graph-supported hop.",
    )

    assert command.goto == "BloodHound"
    assert command.update["next_owner"] == "BloodHound"
    assert command.update["_last_target_agent"] == "BloodHound"
    assert "AUTONOMOUS POST-INGEST ROUTER" in command.update["bloodhound_messages"][1].content
    assert "do not route back to Mythic_Operator" in command.update["bloodhound_messages"][1].content


def test_post_ingest_handback_stays_with_supervisor_when_collection_is_missing():
    mod = _load_model_module()
    es, _ = _engagement_modules()
    foothold = _fake_foothold(es, callback_id="2")

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = []
        _engagement_graph_facts = []

        def _engagement_objective(self):
            return "obtain verified administrative control of target.local"

    tool = mod._create_handback_to_supervisor_tool(FakeMythic(), autonomous=True)
    runtime = SimpleNamespace(state={}, tool_call_id="pre-ingest")

    command = tool.func(runtime, "Collection required.", "No verified graph exists.")

    assert command.goto == "Supervisor"
    assert command.update["next_owner"] == "Supervisor"


def test_post_ingest_handback_stays_with_supervisor_when_ingest_covered_other_domain():
    mod = _load_model_module()
    es, _ = _engagement_modules()
    foothold = _fake_foothold(es, forest="north.sevenkingdoms.local", callback_id="2")
    state = es.EngagementState(objective="x", footholds=[foothold])
    access_key = es.access_context_key(state, foothold)

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = [_fake_hop(
            es,
            f"graph-built:{access_key}",
            {
                "graph_verified": True,
                "covered_domains": ["essos.local"],
            },
        )]
        _engagement_graph_facts = []

        def _engagement_objective(self):
            return "obtain verified administrative control of target.local"

    tool = mod._create_handback_to_supervisor_tool(FakeMythic(), autonomous=True)
    runtime = SimpleNamespace(state={}, tool_call_id="wrong-domain-ingest")
    command = tool.func(runtime, "Collection required.", "The source domain is still missing.")

    assert command.goto == "Supervisor"
    assert command.update["next_owner"] == "Supervisor"


def test_post_ingest_handback_does_not_hide_grounded_capability():
    mod = _load_model_module()
    es, _ = _engagement_modules()
    foothold = _fake_foothold(es, callback_id="2")
    state = es.EngagementState(
        objective="obtain verified administrative control of target.local",
        footholds=[foothold],
    )
    access_key = es.access_context_key(state, foothold)

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = [_fake_hop(es, f"graph-built:{access_key}")]
        _engagement_graph_facts = [
            _fake_fact(es, "generic-write:gpo:controlled-policy"),
            _fake_fact(es, "gpo-domain:controlled-policy:north.sevenkingdoms.local"),
        ]

        def _engagement_objective(self):
            return "obtain verified administrative control of target.local"

    assert mod._deterministic_post_ingest_owner(FakeMythic()) is None


def test_autonomous_handoff_redirects_stale_kerberos_context_redelegation_to_dcsync():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content=json.dumps({
                    "ok": True,
                    "verdict": "achieved",
                    "capability": "ensure-kerberos-context",
                    "recorded_effects": ["kerberos-context:north.sevenkingdoms.local@callback:3"],
                    "issued": [
                        {"task_id": 31, "command": "shell", "parameters": "klist"},
                        {
                            "task_id": 32,
                            "command": "shell",
                            "parameters": r"dir \\winterfell.north.sevenkingdoms.local\C$",
                        },
                    ],
                }),
                name="execute_capability",
                tool_call_id="cap_context",
            ),
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Continue the autonomous GOAD solve toward essos.local from callback 3. Verify NORTH Domain "
            "Admins membership and current token/ticket state, refresh Kerberos/PAC if membership is present, "
            "then proceed to NORTH DCSync remotely from CASTELBLACK using NETBIOS-qualified NORTH\\krbtgt."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "already recorded `kerberos-context:north.sevenkingdoms.local@callback:3`" in instruction
    assert "Do not repeat Domain Admins membership checks" in instruction
    assert "DCSync `NORTH\\krbtgt`" in instruction
    assert "`krbtgt-hash:north.sevenkingdoms.local`" in instruction


def test_autonomous_handoff_redirects_stale_gpo_after_krbtgt_to_ticket_forge():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content=json.dumps({
                    "ok": True,
                    "verdict": "achieved",
                    "capability": "dcsync-account",
                    "achieved_effects": [
                        "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local",
                        "da:north.sevenkingdoms.local",
                        "kerberos-context:north.sevenkingdoms.local@callback:3",
                        "krbtgt-hash:north.sevenkingdoms.local",
                        "creds:administrator@north.sevenkingdoms.local",
                    ],
                    "recorded_effects": ["creds:administrator@north.sevenkingdoms.local"],
                }),
                name="execute_capability",
                tool_call_id="cap_admin",
            ),
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Continue the autonomous GOAD objective. Prior work only staged a proof-only GPP task for "
            "STARKWALLPAPER and did not add samwell.tarly to NORTH Domain Admins. Execute "
            "gpo-controlled-system-exec with command cmd.exe and arguments /c net group "
            "\"Domain Admins\" samwell.tarly /add /domain, then DCSync NORTH\\krbtgt."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "already prove the STARKWALLPAPER/GPO chain is past the GPO hop" in instruction
    assert "`krbtgt-hash:north.sevenkingdoms.local` is achieved" in instruction
    assert "`forge-golden-ticket`" in instruction
    assert "`domain=north.sevenkingdoms.local`" in instruction
    assert "`target_domain=sevenkingdoms.local`" in instruction
    assert "Do not repeat GPO abuse" in instruction


def test_autonomous_handoff_redirects_stale_gpo_after_task_history_dcsync_to_ticket_forge():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content=(
                    '[{"command_name": "execute_pe", "display_id": 36, "status": "success", '
                    '"display_params": "lsadump::dcsync /domain:north.sevenkingdoms.local '
                    '/dc:WINTERFELL.NORTH.SEVENKINGDOMS.LOCAL /user:NORTH\\\\krbtgt"}]'
                ),
                name="get_task_history_for_callback",
                tool_call_id="hist_1",
            ),
            HumanMessage(
                content=(
                    "=== ENGAGEMENT STATE ===\n"
                    "Achieved hops:\n"
                    "- capability:ensure-kerberos-context: kerberos-context:north.sevenkingdoms.local@callback:3\n"
                )
            ),
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Continue the autonomous GOAD objective. The prior proof-only STARKWALLPAPER task did not add "
            "samwell.tarly to Domain Admins; execute gpo-controlled-system-exec with net group "
            "\"Domain Admins\" samwell.tarly /add /domain, then DCSync NORTH\\krbtgt."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "`krbtgt-hash:north.sevenkingdoms.local` is achieved" in instruction
    assert "`forge-golden-ticket`" in instruction


def test_autonomous_step_gate_compiles_bloodhound_handoff_to_next_capability(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()

    class FakeMythic:
        _engagement_footholds = [_fake_foothold(es)]
        _engagement_hops = [
            _fake_hop(es, "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"),
            _fake_hop(es, "da:north.sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:north.sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:north.sevenkingdoms.local"),
        ]
        _engagement_graph_facts = [
            _fake_fact(es, "generic-write:gpo:starkwallpaper"),
            _fake_fact(es, "gpo-domain:starkwallpaper:north.sevenkingdoms.local"),
            _fake_fact(es, "credential-target:arya.stark@north.sevenkingdoms.local"),
        ]

        def _engagement_objective(self):
            return "compromise essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = FakeMythic()

    redirect = model._autonomous_handoff_step_redirect(
        "BloodHound",
        "Analyze the graph again and return the next STARKWALLPAPER/GPO hop.",
        {},
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Autonomous_Executor"
    assert "AUTONOMOUS STEP DRIVER" in instruction
    assert '"name": "forge-golden-ticket"' in instruction
    assert '"target_domain": "sevenkingdoms.local"' in instruction
    assert '"callback_id": "3"' in instruction
    assert "supersedes the delegated prose from BloodHound" in instruction
    assert "STARKWALLPAPER" in instruction
    assert mod._is_bounded_one_action_capability_request([HumanMessage(content=instruction)]) is False


def test_autonomous_step_gate_compiles_collection_needed_over_stale_gpo(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()

    foothold = _fake_foothold(es)
    old_access_key = es.access_context_key(es.EngagementState(objective="compromise essos.local", footholds=[foothold]), foothold)

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = [
            _fake_hop(es, f"graph-built:{old_access_key}"),
            _fake_hop(es, "da:north.sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:north.sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:north.sevenkingdoms.local"),
            _fake_hop(es, "da:sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:sevenkingdoms.local"),
        ]
        _engagement_graph_facts = [
            _fake_fact(es, "generic-write:computer:kingslanding"),
            _fake_fact(es, "write-dacl:domain:sevenkingdoms.local"),
        ]

        def _engagement_objective(self):
            return "obtain administrative control of essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = FakeMythic()

    redirect = model._autonomous_handoff_step_redirect(
        "Mythic_Operator",
        "Execute the STARKWALLPAPER GPO Domain Admins add again with SharpGPOAbuse.",
        {},
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "AUTONOMOUS COLLECTION DRIVER" in instruction
    assert "callback 3" in instruction
    assert "one NEW SharpHound collection" in instruction
    assert "Do not use task history, task 5" in instruction
    assert "callback_display_id=3" in instruction
    assert "Do not run GPO abuse" in instruction
    assert "STARKWALLPAPER" in instruction


def test_autonomous_step_gate_routes_blocked_mythic_handoff_to_bloodhound(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()

    foothold = _fake_foothold(es)
    state_with_control = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[
            _fake_hop(es, "da:sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:sevenkingdoms.local"),
        ],
    )
    current_key = es.access_context_key(state_with_control, foothold)

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = [
            *state_with_control.hops,
            _fake_hop(
                es,
                f"graph-built:{current_key}",
                {
                    "graph_verified": True,
                    "covered_domains": ["north.sevenkingdoms.local"],
                },
            ),
        ]
        _engagement_graph_facts = [_fake_fact(es, "domain-collected:sevenkingdoms.local")]

        def _engagement_objective(self):
            return "obtain administrative control of essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = FakeMythic()

    redirect = model._autonomous_handoff_step_redirect(
        "Mythic_Operator",
        "Execute STARKWALLPAPER again.",
        {"bloodhound_messages": []},
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "BloodHound"
    assert "AUTONOMOUS BLOCKED-STATE GRAPH ANALYSIS" in instruction
    assert "Do not ask Mythic_Operator to rerun GPO abuse" in instruction
    assert "Execute STARKWALLPAPER again" in instruction


def test_autonomous_step_gate_terminalizes_blocked_after_bloodhound_blocker(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()

    foothold = _fake_foothold(es)
    state_with_control = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[
            _fake_hop(es, "da:sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:sevenkingdoms.local"),
        ],
    )
    current_key = es.access_context_key(state_with_control, foothold)

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = [
            *state_with_control.hops,
            _fake_hop(
                es,
                f"graph-built:{current_key}",
                {
                    "graph_verified": True,
                    "covered_domains": ["north.sevenkingdoms.local"],
                },
            ),
        ]
        _engagement_graph_facts = [_fake_fact(es, "domain-collected:sevenkingdoms.local")]

        def _engagement_objective(self):
            return "obtain administrative control of essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = FakeMythic()
    runtime_state = {
        "bloodhound_messages": [
            AIMessage(content=(
                "BLOCKER / MISSING CAPABILITY: ESSOS.LOCAL collected=false; users: `0`; groups: `0`; "
                "computers: `0`; no modeled hop available."
            ))
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = model._autonomous_handoff_step_redirect(
        "Mythic_Operator",
        "Execute STARKWALLPAPER again.",
        runtime_state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "__terminal__"
    assert "AUTONOMOUS BLOCKED REPORT" in instruction
    assert "No reset is indicated" in instruction
    assert "Suppressed handoff text: Execute STARKWALLPAPER again." in instruction


def test_autonomous_executor_node_calls_execute_capability_once(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()

    class FakeMythic:
        _engagement_footholds = [_fake_foothold(es)]
        _engagement_hops = [
            _fake_hop(es, "da:sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:sevenkingdoms.local@callback:3"),
        ]
        _engagement_graph_facts = []

        def __init__(self):
            self.calls = []

        def _engagement_objective(self):
            return "compromise essos.local"

        async def execute_capability(self, action, inputs):
            self.calls.append((action, inputs))
            return json.dumps({
                "ok": True,
                "verdict": "achieved",
                "capability": action["name"],
                "issued": [{"task_id": 60, "command": "dcsync"}],
                "recorded_effects": ["krbtgt-hash:sevenkingdoms.local"],
                "achieved_effects": ["krbtgt-hash:sevenkingdoms.local"],
            }, sort_keys=True)

    fake_mythic = FakeMythic()
    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = fake_mythic
    model._message_seq = 0
    model.state = {"_message_seq": 0}

    action = {
        "name": "dcsync-krbtgt",
        "target": "domain=sevenkingdoms.local;account=krbtgt",
        "preconditions": ["kerberos-context:sevenkingdoms.local@callback:3"],
        "effects": ["krbtgt-hash:sevenkingdoms.local"],
        "intent": {"capability": "dcsync-krbtgt", "domain": "sevenkingdoms.local", "account": "krbtgt"},
        "verifier": {"achieved_any": ["krbtgt_hash_present"]},
        "reason": "test",
        "source_facts": ["kerberos-context:sevenkingdoms.local@callback:3"],
    }
    instruction = (
        "AUTONOMOUS STEP DRIVER: test\n"
        f"`action={json.dumps(action, sort_keys=True)}`\n"
        '`inputs={"callback_id": "3"}`'
    )
    state = {
        "messages": [],
        "supervisor_messages": [],
        "mythic_operator_messages": [],
        "bloodhound_messages": [],
        "autonomous_executor_messages": [HumanMessage(content=instruction)],
    }

    command = asyncio.run(model._autonomous_executor_node(state))

    assert command.goto == "Supervisor"
    assert len(fake_mythic.calls) == 1
    called_action, called_inputs = fake_mythic.calls[0]
    assert called_action["name"] == "dcsync-krbtgt"
    assert called_inputs == {"callback_id": "3"}
    update = command.update
    assert "mythic_operator_messages" not in update
    assert update["messages"][0].name == "execute_capability"
    assert "Executor verdict: `achieved` for `dcsync-krbtgt`" in update["supervisor_messages"][0].content


def test_autonomous_step_gate_is_inactive_outside_autonomous_mode(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()

    class FakeMythic:
        _engagement_footholds = [_fake_foothold(es)]
        _engagement_hops = [_fake_hop(es, "krbtgt-hash:north.sevenkingdoms.local")]
        _engagement_graph_facts = []

        def _engagement_objective(self):
            return "compromise essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = False
    model.mythic_client = FakeMythic()

    assert model._autonomous_handoff_step_redirect(
        "BloodHound",
        "Analyze the graph.",
        {},
    ) is None


def test_handoff_tool_uses_autonomous_redirect_target_channel():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(
        agent_name="BloodHound",
        autonomous_redirect=lambda agent, instruction, state: (
            "Autonomous_Executor",
            "compiled deterministic capability instruction",
        ),
    )
    old_supervisor_msg = HumanMessage(content="old supervisor context")
    runtime = SimpleNamespace(
        state={
            "messages": [old_supervisor_msg],
            "supervisor_messages": [old_supervisor_msg],
            "mythic_operator_messages": [],
            "generalist_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
            "autonomous_executor_messages": [],
        },
        tool_call_id="handoff-test",
    )

    command = tool.func(runtime, "stale BloodHound planner handoff")

    assert command.goto == "Autonomous_Executor"
    assert "autonomous_executor_messages" in command.update
    assert "bloodhound_messages" not in command.update
    assert command.update["autonomous_executor_messages"][1].content == "compiled deterministic capability instruction"


def test_handoff_tool_keeps_short_title_separate_from_full_instruction():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(agent_name="Mythic_Operator")
    runtime = SimpleNamespace(
        state={
            "messages": [],
            "supervisor_messages": [],
            "mythic_operator_messages": [],
            "generalist_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
        },
        tool_call_id="handoff-title",
    )

    command = tool.func(
        runtime,
        "List all active Mythic callbacks and report each host, user, and integrity level.",
        "List active callbacks",
    )

    delegated = command.update["mythic_operator_messages"][1]
    assert delegated.content == "List all active Mythic callbacks and report each host, user, and integrity level."
    assert delegated.additional_kwargs["_handoff_title"] == "List active callbacks"


def test_handoff_tool_schema_exposes_title_and_instruction_in_one_call():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(agent_name="BloodHound")

    assert set(tool.args) >= {"handoff_title", "handoff_instruction"}
    assert "input_payload" not in tool.args
    assert "input_type" not in tool.args
    assert "short operator-facing title" in tool.args["handoff_title"]["description"]
    assert "complete, self-contained instruction" in tool.args["handoff_instruction"]["description"]


def test_sandbox_handoff_schema_carries_inline_payload_to_worker():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(agent_name="Sandbox")
    runtime = SimpleNamespace(
        state={
            "messages": [],
            "supervisor_messages": [],
            "mythic_operator_messages": [],
            "generalist_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
            "sandbox_messages": [],
        },
        tool_call_id="sandbox-handoff",
    )
    payload = '[{"id": 41, "host": "CASTELBLACK"}]'

    command = tool.func(
        runtime,
        "Group the callbacks by host and identify duplicate IDs.",
        "Group callback JSON",
        payload,
        "json",
    )

    assert set(tool.args) >= {"handoff_title", "handoff_instruction", "input_payload", "input_type"}
    delegated = command.update["sandbox_messages"][1]
    assert delegated.additional_kwargs["_handoff_title"] == "Group callback JSON"
    assert delegated.content.startswith("Group the callbacks by host and identify duplicate IDs.")
    assert "Input payload (json):" in delegated.content
    assert "```json" in delegated.content
    assert payload in delegated.content


def test_handoff_redirect_replaces_stale_caller_title():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(
        agent_name="BloodHound",
        autonomous_redirect=lambda agent, instruction, state: mod._handoff_directive(
            "Autonomous_Executor",
            "AUTONOMOUS STEP DRIVER: execute the selected capability.",
            "Execute selected capability",
        ),
    )
    runtime = SimpleNamespace(
        state={
            "messages": [],
            "supervisor_messages": [],
            "mythic_operator_messages": [],
            "generalist_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
            "autonomous_executor_messages": [],
        },
        tool_call_id="handoff-redirect-title",
    )

    command = tool.func(runtime, "Analyze the graph.", "Analyze BloodHound graph")

    delegated = command.update["autonomous_executor_messages"][1]
    assert delegated.content == "AUTONOMOUS STEP DRIVER: execute the selected capability."
    assert delegated.additional_kwargs["_handoff_title"] == "Execute selected capability"


def test_handoff_tool_terminal_redirect_sets_recursion_handback():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        autonomous_redirect=lambda agent, instruction, state: (
            "__terminal__",
            "AUTONOMOUS BLOCKED REPORT: no modeled hop",
        ),
    )
    runtime = SimpleNamespace(
        state={
            "messages": [],
            "supervisor_messages": [],
            "mythic_operator_messages": [],
            "generalist_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
            "autonomous_executor_messages": [],
        },
        tool_call_id="handoff-terminal",
    )

    command = tool.func(runtime, "stale GPO")

    assert command.goto == "Supervisor"
    assert command.update["recursion_handback"] is True
    assert "supervisor_messages" in command.update
    assert command.update["supervisor_messages"][1].content == "AUTONOMOUS BLOCKED REPORT: no modeled hop"


def test_autonomous_handoff_redirect_does_not_block_collection_without_tool_evidence():
    mod = _load_model_module()
    state = {
        "supervisor_messages": [
            HumanMessage(content="The prompt mentioned graph_verified=true as a desired future state.")
        ],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        "SharpHound completion and ZIP path are not confirmed; download the ZIP and ingest_collection.",
        state,
    )

    assert redirect is None


def test_autonomous_handoff_redirect_ignores_new_mythic_work():
    mod = _load_model_module()
    state = {
        "supervisor_messages": [
            ToolMessage(
                content=(
                    "🔄 **Handback to Supervisor** — Need graph-supported next hop selection.\n\n"
                    "**DONE (do NOT repeat):** STARKWALLPAPER GPO hop completed.\n"
                    "**BLOCKER / MISSING CAPABILITY:** Need graph-supported next action for Essos path; "
                    "route to BloodHound."
                ),
                name="handback_to_supervisor",
                tool_call_id="h1",
            )
        ],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        "Execute the graph-selected LAPS read for BRAAVOS from callback 3.",
        state,
    )

    assert redirect is None


def test_handoff_tool_returns_delta_only_for_add_reducers():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(agent_name="Mythic_Operator")
    old_supervisor_msg = HumanMessage(content="old supervisor context")
    runtime = SimpleNamespace(
        state={
            "messages": [old_supervisor_msg],
            "supervisor_messages": [old_supervisor_msg],
            "mythic_operator_messages": [],
            "generalist_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
        },
        tool_call_id="handoff-test",
    )

    command = tool.func(runtime, "Run execute_capability dcsync-krbtgt once.")

    assert command.goto == "Mythic_Operator"
    assert "supervisor_messages" not in command.update
    assert command.update["messages"] == command.update["mythic_operator_messages"]
    assert len(command.update["messages"]) == 2
    assert all(msg is not old_supervisor_msg for msg in command.update["messages"])


def test_typed_handoff_redirects_same_worker_to_known_next_owner():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        worker_outcome_lookup=lambda _state: ({
            "source_worker": "Mythic_Operator",
            "outcome": "handoff",
            "next_owner": "BloodHound",
        }, "summary"),
    )
    runtime = SimpleNamespace(
        state={"messages": [], "supervisor_messages": [], "mythic_operator_messages": [], "bloodhound_messages": []},
        tool_call_id="handoff-typed",
    )

    command = tool.func(runtime, "repeat Mythic work")

    assert command.goto == "BloodHound"
    assert command.update["bloodhound_messages"][1].content == "summary"
    assert command.update["bloodhound_messages"][1].additional_kwargs["_delegated_to"] == "BloodHound"


@pytest.mark.parametrize(
    ("outcome", "tool_text", "final_text"),
    (
        ("blocked", "Worker outcome prevented repeated delegation: Mythic_Operator reported BLOCKED with no next owner.", "**Blocked**\n\nsummary"),
        ("complete", "Worker outcome prevented repeated delegation: Mythic_Operator reported COMPLETE.", "**Objective complete**\n\nsummary"),
    ),
)
def test_typed_worker_outcome_terminalizes_repeated_same_worker(outcome, tool_text, final_text):
    mod = _load_model_module()
    tool = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        worker_outcome_lookup=lambda _state: ({
            "source_worker": "Mythic_Operator",
            "outcome": outcome,
            "next_owner": "",
        }, "summary"),
    )
    runtime = SimpleNamespace(
        state={"messages": [], "supervisor_messages": [], "mythic_operator_messages": []},
        tool_call_id="handoff-terminal-typed",
    )

    command = tool.func(runtime, "repeat Mythic work")

    assert command.goto == "__end__"
    assert command.graph == mod.Command.PARENT
    assert command.update["messages"] == command.update["supervisor_messages"]
    assert [type(msg).__name__ for msg in command.update["messages"]] == ["ToolMessage", "AIMessage"]
    assert command.update["messages"][0].name == "transfer_to_Mythic_Operator"
    assert command.update["messages"][0].tool_call_id == "handoff-terminal-typed"
    assert command.update["messages"][0].content == tool_text
    assert command.update["messages"][1].content == final_text
    assert command.update["messages"][1].additional_kwargs["_is_final_report"] is True


def test_complete_terminalizes_different_requested_owner_but_blocked_does_not():
    mod = _load_model_module()
    runtime = SimpleNamespace(
        state={"messages": [], "supervisor_messages": [], "mythic_operator_messages": [], "bloodhound_messages": []},
        tool_call_id="handoff-cross-owner",
    )
    complete = mod._create_handoff_tool(
        agent_name="BloodHound",
        worker_outcome_lookup=lambda _state: ({"source_worker": "Mythic_Operator", "outcome": "complete", "next_owner": ""}, "summary"),
    ).func(runtime, "stale follow-up")
    assert complete.goto == "__end__"
    assert complete.update["messages"][0].content == "Worker outcome prevented repeated delegation: Mythic_Operator reported COMPLETE."
    blocked = mod._create_handoff_tool(
        agent_name="BloodHound",
        worker_outcome_lookup=lambda _state: ({"source_worker": "Mythic_Operator", "outcome": "blocked", "next_owner": ""}, "summary"),
    ).func(runtime, "fresh BloodHound work")
    assert blocked.goto == "BloodHound"


def test_terminal_execute_capability_report_includes_existing_proof_chain():
    mod = _load_model_module()

    report = mod._terminal_execute_capability_report({
        "ok": True,
        "verdict": "achieved",
        "capability": "ensure-kerberos-context",
        "reason": "requested capability effect is already achieved",
        "issued": [],
        "recorded_effects": [],
        "proof_chain": [
            {"effect": "kerberos-context:essos.local@callback:14", "task_id": "670", "callback_id": "14"},
            {"effect": "da:essos.local", "task_id": "655"},
        ],
    })

    assert "Proof chain:" in report
    assert "`kerberos-context:essos.local@callback:14` task=670 cb=14" in report
    assert "`da:essos.local` task=655" in report


def test_partial_execute_capability_result_is_terminal_for_bounded_action():
    mod = _load_model_module()
    payload = {
        "ok": False,
        "verdict": "partial",
        "capability": "dcsync-krbtgt",
        "reason": "precheck blocked actual tasking",
        "issued": [{"command": "dcsync", "task_id": None}],
        "recorded_effects": [],
    }

    terminal = mod._terminal_execute_capability_payload([
        ToolMessage(
            content=json.dumps(payload, sort_keys=True),
            name="execute_capability",
            tool_call_id="call_1",
        )
    ])

    assert terminal is not None
    assert terminal["verdict"] == "partial"
    assert "precheck blocked" in mod._terminal_execute_capability_report(terminal)


def test_bounded_execute_capability_middleware_ends_agent_loop_after_terminal_result():
    mod = _load_model_module()
    payload = {
        "ok": True,
        "verdict": "achieved",
        "capability": "dcsync-krbtgt",
        "recorded_effects": ["krbtgt-hash:essos.local"],
    }
    messages = [
        HumanMessage(
            content=(
                "Continue by executing exactly one next grounded capability action using "
                "the generic execute_capability tool. Retry at most once, then stop."
            )
        ),
        ToolMessage(
            content=json.dumps(payload, sort_keys=True),
            name="execute_capability",
            tool_call_id="call_1",
        ),
    ]
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())

    update = mw.before_model({"messages": messages}, runtime=None)

    assert update == {"jump_to": "end"}


def test_execute_capability_boundary_ends_agent_loop_without_bounded_prompt():
    mod = _load_model_module()
    payload = {
        "ok": True,
        "verdict": "achieved",
        "capability": "execute-as-local-admin",
        "recorded_effects": ["remote-exec:braavos@essos.local"],
    }
    messages = [
        HumanMessage(content="Continue toward ESSOS administrative control from the current state."),
        ToolMessage(
            content=json.dumps(payload, sort_keys=True),
            name="execute_capability",
            tool_call_id="call_1",
        ),
    ]
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())

    update = mw.before_model({"messages": messages}, runtime=None)

    assert update == {"jump_to": "end"}


def test_bounded_execute_capability_middleware_skips_sibling_tools_without_executing():
    mod = _load_model_module()
    messages = [
        HumanMessage(
            content=(
                "Continue by executing exactly one next grounded capability action using "
                "the generic execute_capability tool. Retry at most once, then stop."
            )
        ),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "execute_capability", "args": {"action": {"name": "dcsync-krbtgt"}}, "id": "call_exec"},
                {"name": "issue_task_and_waitfor_task_output", "args": {"command": "dcsync"}, "id": "call_extra"},
            ],
        ),
    ]
    request = ToolCallRequest(
        tool_call={"name": "issue_task_and_waitfor_task_output", "args": {}, "id": "call_extra"},
        tool=None,
        state={"messages": messages},
        runtime=None,
    )
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="side effect", name="issue_task_and_waitfor_task_output", tool_call_id="call_extra")

    result = mw.wrap_tool_call(request, handler)

    assert called is False
    assert isinstance(result, ToolMessage)
    assert result.name == "issue_task_and_waitfor_task_output"
    payload = json.loads(result.content)
    assert payload["verdict"] == "blocked"
    assert "skipped sibling tool" in payload["reason"]


def test_execute_capability_boundary_skips_sibling_tools_without_bounded_prompt():
    mod = _load_model_module()
    messages = [
        HumanMessage(content="Continue toward ESSOS administrative control from the current state."),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "execute_capability", "args": {"action": {"name": "execute-as-local-admin"}}, "id": "call_exec"},
                {"name": "issue_task_and_waitfor_task_output", "args": {"command": "dcsync"}, "id": "call_extra"},
            ],
        ),
    ]
    request = ToolCallRequest(
        tool_call={"name": "issue_task_and_waitfor_task_output", "args": {}, "id": "call_extra"},
        tool=None,
        state={"messages": messages},
        runtime=None,
    )
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="side effect", name="issue_task_and_waitfor_task_output", tool_call_id="call_extra")

    result = mw.wrap_tool_call(request, handler)

    assert called is False
    payload = json.loads(result.content)
    assert payload["capability"] == "execute-capability-boundary"
    assert "atomic transaction boundary" in payload["reason"]


def test_execute_capability_boundary_allows_regular_unbounded_tool_batches():
    mod = _load_model_module()
    messages = [
        HumanMessage(content="Continue toward ESSOS administrative control from the current state."),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "get_task_history_for_callback", "args": {"callback_id": "3"}, "id": "call_history"},
            ],
        ),
    ]
    request = ToolCallRequest(
        tool_call={"name": "get_task_history_for_callback", "args": {}, "id": "call_history"},
        tool=None,
        state={"messages": messages},
        runtime=None,
    )
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="ok", name="get_task_history_for_callback", tool_call_id="call_history")

    result = mw.wrap_tool_call(request, handler)

    assert called is True
    assert result.content == "ok"


def test_bounded_execute_capability_middleware_is_opt_in_for_operator_only():
    mod = _load_model_module()
    Model = mod.Model
    m = Model.__new__(Model)
    m.mode = "auto"
    m._get_base_chat_model = lambda: None

    default_names = [type(item).__name__ for item in m._context_middleware()]
    operator_names = [
        type(item).__name__
        for item in m._context_middleware(bounded_execute_stop=True)
    ]

    assert "_BoundedExecuteCapabilityStopMiddleware" not in default_names
    assert "_BoundedExecuteCapabilityStopMiddleware" in operator_names


def test_seed_autonomous_objective_seeds_and_resets_latch_on_new_prompt():
    mod = _load_model_module()
    Model = mod.Model

    class FakeClient:
        def __init__(self):
            self._autonomous_objective_seed = "prior mission"
            self._autonomous_objective_persisted = True
        def _engagement_objective(self):
            return "escalate to Domain Admin of north.sevenkingdoms.local"

    m = Model.__new__(Model)
    m._autonomous_solve = True
    fc = FakeClient()
    m.mythic_client = fc
    m._seed_autonomous_objective("escalate to Domain Admin of north.sevenkingdoms.local")
    # New (different) prompt -> seed updated AND persist latch reset so a reused client re-adopts.
    assert fc._autonomous_objective_seed == "escalate to Domain Admin of north.sevenkingdoms.local"
    assert fc._autonomous_objective_persisted is False


def test_seed_autonomous_objective_noop_when_not_autonomous_or_no_client():
    mod = _load_model_module()
    Model = mod.Model

    class FakeClient:
        _autonomous_objective_seed = "orig"
        _autonomous_objective_persisted = True
        def _engagement_objective(self):
            return "orig"

    # Not autonomous -> seed untouched.
    m = Model.__new__(Model)
    m._autonomous_solve = False
    fc = FakeClient()
    m.mythic_client = fc
    m._seed_autonomous_objective("anything")
    assert fc._autonomous_objective_seed == "orig"

    # No client -> must not raise.
    m2 = Model.__new__(Model)
    m2._autonomous_solve = True
    m2.mythic_client = None
    m2._seed_autonomous_objective("x")


def test_seed_autonomous_objective_loud_guard_warns_on_opaque(caplog):
    import logging
    mod = _load_model_module()
    Model = mod.Model

    class OpaqueClient:
        _autonomous_objective_seed = ""
        _autonomous_objective_persisted = False
        def _engagement_objective(self):
            return "sage-engagement:abc"   # still opaque -> completion-recognition unreachable

    m = Model.__new__(Model)
    m._autonomous_solve = True
    m.mythic_client = OpaqueClient()
    with caplog.at_level(logging.WARNING):
        m._seed_autonomous_objective("")   # blank prompt -> stays opaque -> must warn
    assert any("completion-recognition is UNREACHABLE" in r.message for r in caplog.records)
