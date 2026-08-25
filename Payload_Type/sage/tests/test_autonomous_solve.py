import importlib
import asyncio
import inspect
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
                        "outcome": "handoff",
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
                            "outcome": "handoff",
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
    model.verbose = False
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
            "args": {"reason": reason, "summary": summary, "outcome": "blocked"},
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
    assert command.goto == "__end__"
    assert command.goto != "BloodHound"
    assert command.update["messages"][-1].content.startswith("**Blocked**")


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
                "outcome": "handoff",
                "next_owner": "BloodHound",
            },
            "id": "call-contradictory",
            "type": "tool_call",
        }],
        additional_kwargs={"_seq": 3},
    )
    typed = mod._worker_handoff_metadata(
        [handback],
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
    )
    assert typed is not None
    assert typed[0]["outcome"] == "handoff"
    assert typed[0]["next_owner"] == "BloodHound"

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
                "outcome": "handoff",
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
        "handoff",
        "BloodHound",
    )
    tool_message = command.update["messages"][0]
    assert tool_message.additional_kwargs["_handback_input"] == {
        "reason": reason,
        "summary": summary,
        "outcome": "handoff",
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
        "outcome": "handoff",
        "next_owner": "BloodHound",
    }
    handback = mod._create_handback_to_supervisor_tool()
    command = handback.func(
        SimpleNamespace(state={}, tool_call_id="handback-1"),
        payload["reason"],
        payload["summary"],
        payload["outcome"],
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
        "outcome": "handoff",
        "next_owner": "BloodHound",
    }
    second_payload = {
        "reason": "second typed route",
        "summary": summary,
        "outcome": "handoff",
        "next_owner": "MCP_Manager",
    }
    first_result = handback.func(
        SimpleNamespace(state={}, tool_call_id="handback-1"),
        first_payload["reason"],
        first_payload["summary"],
        first_payload["outcome"],
        first_payload["next_owner"],
    ).update["messages"][0]
    second_result = handback.func(
        SimpleNamespace(state={}, tool_call_id="handback-2"),
        second_payload["reason"],
        second_payload["summary"],
        second_payload["outcome"],
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
            "outcome": "handoff",
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
                    "outcome": "handoff",
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
        "outcome": "handoff",
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


@pytest.mark.parametrize("outcome", ("progress", "blocked", "complete"))
def test_post_ingest_handback_returns_to_typed_supervisor_boundary(outcome):
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
        outcome,
    )

    assert command.goto == "Supervisor"
    assert command.update["next_owner"] == "Supervisor"
    assert "_last_target_agent" not in command.update
    assert "bloodhound_messages" not in command.update


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

    command = tool.func(
        runtime,
        "Collection required.",
        "No verified graph exists.",
        "progress",
    )

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
    command = tool.func(
        runtime,
        "Collection required.",
        "The source domain is still missing.",
        "progress",
    )

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


def test_autonomous_step_gate_does_not_terminalize_blocked_after_bloodhound_blocker(monkeypatch):
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

    assert redirect is None


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


def _assisted_handoff_state(
    *,
    request_id="chat:59:request:2",
    lane="supervised_workflow",
    operator_messages=(),
):
    return {
        "messages": [],
        "supervisor_messages": list(operator_messages),
        "mythic_operator_messages": [],
        "generalist_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
        "_request_id": request_id,
        "_request_lane": lane,
        "_request_stop_condition": (
            "actions_complete" if lane == "supervised_workflow" else "response_emitted"
        ),
    }


def _operator_message(content, request_id, **extra):
    return HumanMessage(
        content=content,
        additional_kwargs={
            "_request_id": request_id,
            "_operator_input": True,
            **extra,
        },
    )


@pytest.mark.parametrize("agent_name", ["Generalist", "Mythic_Operator"])
def test_assisted_handoff_uses_only_exact_current_request_operator_input(agent_name):
    mod = _load_model_module()
    current_request = "chat:59:request:2"
    current_text = "List the Kerberos ticket cache on callback 7 only."
    state = _assisted_handoff_state(
        request_id=current_request,
        operator_messages=(
            _operator_message("PRIOR request: list callbacks.", "chat:59:request:1"),
            _operator_message(current_text, current_request),
        ),
    )
    tool = mod._create_handoff_tool(agent_name=agent_name)

    command = tool.func(
        SimpleNamespace(state=state, tool_call_id="fabricated-attribution"),
        'The operator has asked Sage to "decide your next action" and execute it.',
        "Decide next action",
    )

    channel = {
        "Generalist": "generalist_messages",
        "Mythic_Operator": "mythic_operator_messages",
    }[agent_name]
    delegated = command.update[channel][1]
    assert delegated.content == current_text
    assert "decide your next action" not in delegated.content
    assert "PRIOR request" not in delegated.content


def test_conversational_handoff_binds_exact_operator_authored_bytes():
    mod = _load_model_module()
    request_id = "chat:60:request:3"
    operator_text = 'Explain this quoted evidence exactly: "the operator asked X".'
    state = _assisted_handoff_state(
        request_id=request_id,
        lane="conversational",
        operator_messages=(_operator_message(operator_text, request_id),),
    )

    command = mod._create_handoff_tool(agent_name="Generalist").func(
        SimpleNamespace(state=state, tool_call_id="conversation-authority"),
        "A prior worker wondered whether Sage should do something else.",
        "Explain evidence",
    )

    assert command.update["generalist_messages"][1].content == operator_text


def test_assisted_typed_subgoal_route_cannot_replace_current_operator_input():
    mod = _load_model_module()
    request_id = "chat:60:request:typed-route"
    operator_text = "Inspect the current request evidence and report what it proves."
    state = _assisted_handoff_state(
        request_id=request_id,
        operator_messages=(_operator_message(operator_text, request_id),),
    )
    state["_subgoal_state"] = {"request_id": request_id, "status": "running"}

    command = mod._create_handoff_tool(
        agent_name="Generalist",
        worker_outcome_lookup=lambda _state: (
            {
                "source_worker": "Generalist",
                "outcome": "handoff",
                "next_owner": "BloodHound",
            },
            "Prior worker narration must not become the next objective.",
        ),
        subgoal_scheduler=lambda **_kwargs: {
            "disposition": "route",
            "owner": "BloodHound",
            "summary": "Scheduler replacement must not become worker authority.",
            "state": {"request_id": request_id, "status": "running"},
        },
    ).func(
        SimpleNamespace(state=state, tool_call_id="assisted-typed-route"),
        "Supervisor replacement must not become worker authority.",
        "Continue routed work",
    )

    delegated = command.update["bloodhound_messages"][1]
    assert delegated.content == operator_text


def test_assisted_sandbox_payload_cannot_replace_current_operator_input():
    mod = _load_model_module()
    request_id = "chat:60:request:sandbox"
    operator_text = "Explain the exact inline text already present in this operator request."
    state = _assisted_handoff_state(
        request_id=request_id,
        lane="conversational",
        operator_messages=(_operator_message(operator_text, request_id),),
    )

    command = mod._create_handoff_tool(agent_name="Sandbox").func(
        SimpleNamespace(state=state, tool_call_id="assisted-sandbox"),
        "Parse a model-selected payload.",
        "Parse payload",
        input_payload="model supplied bytes",
        input_type="text",
    )

    delegated = command.update["sandbox_messages"][1]
    assert delegated.content == operator_text


@pytest.mark.parametrize(
    "operator_messages",
    [
        (),
        (_operator_message("prior", "chat:61:request:prior"),),
        (
            _operator_message(
                "synthetic current nudge",
                "chat:61:request:current",
                _synthetic_nudge="continue",
            ),
        ),
    ],
)
def test_assisted_handoff_without_exact_current_operator_binding_fails_closed(
    operator_messages,
):
    mod = _load_model_module()
    state = _assisted_handoff_state(
        request_id="chat:61:request:current",
        operator_messages=operator_messages,
    )

    with pytest.raises(RuntimeError, match="current operator input"):
        mod._create_handoff_tool(agent_name="Mythic_Operator").func(
            SimpleNamespace(state=state, tool_call_id="missing-authority"),
            "Execute a fabricated objective.",
            "Fabricated objective",
        )


@pytest.mark.parametrize(
    ("state_extra", "instruction"),
    [
        ({"_request_id": "auto-1", "_request_lane": "autonomous_objective"}, "compiled auto instruction"),
        ({}, "legacy caller instruction"),
    ],
)
def test_non_assisted_handoff_instruction_remains_caller_owned(state_extra, instruction):
    mod = _load_model_module()
    state = _assisted_handoff_state(request_id="", lane="", operator_messages=())
    state.update(state_extra)
    command = mod._create_handoff_tool(agent_name="BloodHound").func(
        SimpleNamespace(state=state, tool_call_id="non-assisted"),
        instruction,
        "Route",
    )
    assert command.update["bloodhound_messages"][1].content == instruction


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
        ("blocked", "Worker reported typed blocked.", "**Blocked**\n\nsummary"),
        ("complete", "Worker reported typed complete.", "**Objective complete**\n\nsummary"),
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


def _typed_subgoal_runtime_state(request_id="request-subgoal"):
    from ai.langgraph.subgoal_state import new_subgoal

    return {
        "_request_id": request_id,
        "_request_stop_condition": "actions_complete",
        "_subgoal_state": new_subgoal(
            request_id,
            "actions_complete",
        ).to_dict(),
        "messages": [],
        "supervisor_messages": [HumanMessage(content="operator request")],
        "generalist_messages": [],
        "mythic_operator_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
        "autonomous_executor_messages": [],
    }


def _canonical_subgoal_model(mod, state):
    from ai.langgraph.subgoal_state import SubgoalState

    model = mod.Model.__new__(mod.Model)
    model._subgoal_authority_lock = mod.threading.Lock()
    model._subgoal_authority = SubgoalState.from_dict(state["_subgoal_state"])
    model._subgoal_evidence_records = set()
    return model


def _bind_matching_request_contract(model, state):
    from ai.langgraph.request_contract import build_request_contract

    contract = build_request_contract(
        request_id=state["_request_id"],
        channel_id="7",
        operation_id="9",
        mode="supervised",
        autonomous_solve=False,
    )
    model._request_contract = contract
    model._request_dynamic_proposals = False
    model._turn_authority = importlib.import_module(
        "ai.langgraph.turn_authority"
    ).authority_from_request_contract(contract)
    model.mythic_client = None
    return contract


def test_production_handoff_admits_one_exact_subgoal_execution_tuple():
    from ai.langgraph.subgoal_state import SubgoalState

    mod = _load_model_module()
    state = _typed_subgoal_runtime_state()
    model = _canonical_subgoal_model(mod, state)
    tool = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        subgoal_scheduler=model._schedule_subgoal_transition,
    )

    first = tool.func(
        SimpleNamespace(state=state, tool_call_id="first"),
        "Inspect callback state.",
    )
    running = SubgoalState.from_dict(first.update["_subgoal_state"])
    assert running.owner == "Mythic_Operator"
    assert len(running.admissions) == 1
    assert running.admissions[0].key == (
        running.request_id,
        running.subgoal_id,
        "Mythic_Operator",
        "transfer_to_Mythic_Operator",
        "0",
    )

    replay_state = {**state, **first.update}
    replay = tool.func(
        SimpleNamespace(state=replay_state, tool_call_id="replay"),
        "Same work with different prose.",
    )
    assert replay.goto == "__end__"
    assert "duplicate execution" in replay.update["messages"][0].content
    assert replay.update["_subgoal_state"]["status"] == "blocked"


@pytest.mark.parametrize(
    ("request_id", "instruction", "prior_summary"),
    (
        (
            "request-legacy-heading",
            "Use STARKWALLPAPER GPO with SharpGPOAbuse to add the user to Domain Admins.",
            "handback to supervisor; done (do not repeat): STARKWALLPAPER Domain Admins "
            "graph-supported BloodHound path to essos on callback 7",
        ),
        (
            "request-renamed-heading",
            "Use ORIONPOLICY policy control with the approved adapter to add the principal to Administrators.",
            "return to coordinator; complete: ORIONPOLICY Administrators graph-supported "
            "directory path to vega.example on callback 42",
        ),
        (
            "request-neutral-heading",
            "Inspect the typed callback state and report evidence.",
            "Arbitrary display prose that must not control ownership.",
        ),
    ),
)
def test_typed_handoff_owner_is_invariant_to_legacy_headings_and_entity_renaming(
    request_id,
    instruction,
    prior_summary,
):
    from ai.langgraph.subgoal_state import SubgoalState

    mod = _load_model_module()
    state = _typed_subgoal_runtime_state(request_id)
    state["supervisor_messages"] = [HumanMessage(content=prior_summary)]
    state["messages"] = [HumanMessage(content=prior_summary)]
    model = _canonical_subgoal_model(mod, state)
    tool = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        subgoal_scheduler=model._schedule_subgoal_transition,
    )

    command = tool.func(
        SimpleNamespace(state=state, tool_call_id=f"handoff-{request_id}"),
        instruction,
    )
    projected = SubgoalState.from_dict(command.update["_subgoal_state"])

    assert command.goto == "Mythic_Operator"
    assert projected.owner == "Mythic_Operator"
    assert "mythic_operator_messages" in command.update
    assert "bloodhound_messages" not in command.update
    assert "_autonomous_handoff_redirect" not in inspect.getsource(
        mod._create_handoff_tool
    )


@pytest.mark.parametrize("same_target", (True, False))
def test_concurrent_production_transfers_admit_exactly_one_prestate(same_target):
    from ai.langgraph.subgoal_state import SubgoalState

    mod = _load_model_module()
    state = _typed_subgoal_runtime_state("request-concurrent")
    model = _canonical_subgoal_model(mod, state)
    first = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        subgoal_scheduler=model._schedule_subgoal_transition,
    )
    second = (
        first
        if same_target
        else mod._create_handoff_tool(
            agent_name="BloodHound",
            subgoal_scheduler=model._schedule_subgoal_transition,
        )
    )
    calls = [
        {
            "name": first.name,
            "args": {"handoff_instruction": "Execute the first typed transfer."},
            "id": "transfer-1",
            "type": "tool_call",
        },
        {
            "name": second.name,
            "args": {"handoff_instruction": "Execute the concurrent typed transfer."},
            "id": "transfer-2",
            "type": "tool_call",
        },
    ]
    request = AIMessage(
        content="",
        name="Supervisor",
        tool_calls=calls,
        additional_kwargs={"_seq": 1},
    )
    state["messages"] = [request]
    outputs = asyncio.run(
        ToolNode([first] if same_target else [first, second])._afunc(
            state,
            {},
            Runtime(),
        )
    )

    winners = [
        output
        for output in outputs
        if output.goto in {"Mythic_Operator", "BloodHound"}
    ]
    losers = [output for output in outputs if output.goto == "__end__"]
    assert len(winners) == 1
    assert len(losers) == 1
    assert "_subgoal_state" not in losers[0].update
    admitted = SubgoalState.from_dict(winners[0].update["_subgoal_state"])
    assert admitted.owner == winners[0].goto
    assert len(admitted.admissions) == 1
    assert model._subgoal_authority == admitted


@pytest.mark.parametrize(
    ("first_owner", "second_owner"),
    (
        ("Mythic_Operator", "BloodHound"),
        ("BloodHound", "Mythic_Operator"),
    ),
)
def test_gate_k_in_place_arbiter_reaches_only_first_control_handler(
    first_owner,
    second_owner,
):
    from ai.langgraph.subgoal_state import SubgoalState

    mod = _load_model_module()
    state = _typed_subgoal_runtime_state("request-gate-k-arbiter")
    model = _canonical_subgoal_model(mod, state)
    _bind_matching_request_contract(model, state)
    tools = {
        owner: mod._create_handoff_tool(
            agent_name=owner,
            subgoal_scheduler=model._schedule_subgoal_transition,
        )
        for owner in {first_owner, second_owner}
    }
    calls = [
        {
            "name": tools[first_owner].name,
            "args": {"handoff_instruction": f"Route to {first_owner}."},
            "id": "first-control",
            "type": "tool_call",
        },
        {
            "name": tools[second_owner].name,
            "args": {"handoff_instruction": f"Route to {second_owner}."},
            "id": "second-control",
            "type": "tool_call",
        },
    ]
    message = AIMessage(
        content=[
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["name"],
                "input": call["args"],
            }
            for call in calls
        ],
        name="Supervisor",
        tool_calls=calls,
        additional_kwargs={
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(
                                call["args"],
                                sort_keys=True,
                            ),
                        },
                    }
                    for call in calls
                ],
                "_seq": 1,
            },
    )
    callback_reference = message

    assert mod._TurnAuthorityToolMiddleware(model).after_model(
        {"messages": [message]},
        None,
    ) is None
    assert callback_reference.tool_calls == [calls[0]]
    assert not [
        block for block in message.content
        if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_call"}
    ]
    assert "tool_calls" not in message.additional_kwargs
    assert "function_call" not in message.additional_kwargs

    state["messages"] = [message]
    outputs = asyncio.run(
        ToolNode(list(tools.values()))._afunc(state, {}, Runtime())
    )

    assert len(outputs) == 1
    assert outputs[0].goto == first_owner
    admitted = SubgoalState.from_dict(outputs[0].update["_subgoal_state"])
    assert admitted.owner == first_owner
    assert len(admitted.admissions) == 1
    assert model._subgoal_authority == admitted


@pytest.mark.parametrize(
    ("first_owner", "second_owner"),
    (
        ("BloodHound", "Generalist"),
        ("Generalist", "BloodHound"),
    ),
)
def test_gate_k_terminal_control_orders_emit_one_canonical_final(
    first_owner,
    second_owner,
):
    from ai.langgraph import worker_outcome
    from ai.langgraph.subgoal_state import (
        SubgoalState,
        assign_and_admit,
        new_subgoal,
    )

    mod = _load_model_module()
    initial = assign_and_admit(
        new_subgoal("request-gate-k-terminal", "actions_complete"),
        owner="Mythic_Operator",
        method="transfer_to_Mythic_Operator",
    )
    state = {
        **_typed_subgoal_runtime_state("request-gate-k-terminal"),
        "_subgoal_state": initial.to_dict(),
    }
    model = _canonical_subgoal_model(mod, state)
    _bind_matching_request_contract(model, state)
    metadata = worker_outcome.build_handoff_metadata(
        source_worker="Mythic_Operator",
        source_turn_id="turn-gate-k-terminal",
        source_seq=3,
        reason="typed blocker",
        summary="No admissible next action exists.",
        outcome="blocked",
    )
    tools = {
        owner: mod._create_handoff_tool(
            agent_name=owner,
            worker_outcome_lookup=lambda _state: (
                metadata,
                "No admissible next action exists.",
            ),
            subgoal_scheduler=model._schedule_subgoal_transition,
        )
        for owner in {first_owner, second_owner}
    }
    calls = [
        {
            "name": tools[first_owner].name,
            "args": {"handoff_instruction": f"Route to {first_owner}."},
            "id": "terminal-control-1",
            "type": "tool_call",
        },
        {
            "name": tools[second_owner].name,
            "args": {"handoff_instruction": f"Route to {second_owner}."},
            "id": "terminal-control-2",
            "type": "tool_call",
        },
    ]
    message = AIMessage(content="", name="Supervisor", tool_calls=calls)

    assert mod._TurnAuthorityToolMiddleware(model).after_model(
        {"messages": [message]},
        None,
    ) is None
    state["messages"] = [message]
    outputs = asyncio.run(
        ToolNode(list(tools.values()))._afunc(state, {}, Runtime())
    )

    assert len(outputs) == 1
    assert outputs[0].goto == "__end__"
    final_reports = [
        item
        for item in outputs[0].update["messages"]
        if isinstance(item, AIMessage)
        and item.additional_kwargs.get("_is_final_report") is True
    ]
    assert len(final_reports) == 1
    terminal = SubgoalState.from_dict(outputs[0].update["_subgoal_state"])
    assert terminal.status.value == "blocked"
    assert terminal.owner == ""
    assert len(terminal.admissions) == 1
    assert model._subgoal_authority == terminal


@pytest.mark.parametrize(
    ("projection_kind", "should_execute"),
    (
        ("absent", True),
        ("exact", True),
        ("empty", False),
        ("stale", False),
        ("malformed", False),
    ),
)
@pytest.mark.parametrize("async_wrapper", (False, True))
def test_gate_k_control_wrapper_binds_only_exact_canonical_projection(
    projection_kind,
    should_execute,
    async_wrapper,
):
    from ai.langgraph.subgoal_state import new_subgoal

    mod = _load_model_module()
    state = _typed_subgoal_runtime_state("request-gate-k-wrapper")
    model = _canonical_subgoal_model(mod, state)
    _bind_matching_request_contract(model, state)
    middleware = mod._TurnAuthorityToolMiddleware(model)
    tool_call = {
        "name": "transfer_to_Mythic_Operator",
        "args": {"handoff_instruction": "Inspect the callback."},
        "id": f"control-{projection_kind}",
        "type": "tool_call",
    }
    request_state = {
        "messages": [AIMessage(content="", tool_calls=[tool_call])],
    }
    if projection_kind == "exact":
        request_state["_subgoal_state"] = dict(state["_subgoal_state"])
    elif projection_kind == "empty":
        request_state["_subgoal_state"] = {}
    elif projection_kind == "stale":
        request_state["_subgoal_state"] = new_subgoal(
            "stale-request",
            "actions_complete",
        ).to_dict()
    elif projection_kind == "malformed":
        request_state["_subgoal_state"] = ["not", "a", "projection"]
    request = ToolCallRequest(
        tool_call=tool_call,
        tool=None,
        state=request_state,
        runtime=Runtime(),
    )
    observed = []

    if async_wrapper:
        async def async_handler(bound_request):
            observed.append(bound_request.state["_subgoal_state"])
            return "executed"

        result = asyncio.run(
            middleware.awrap_tool_call(request, async_handler)
        )
    else:
        def handler(bound_request):
            observed.append(bound_request.state["_subgoal_state"])
            return "executed"

        result = middleware.wrap_tool_call(request, handler)

    if should_execute:
        assert result == "executed"
        assert observed == [state["_subgoal_state"]]
    else:
        assert observed == []
        assert isinstance(result, ToolMessage)
        assert "malformed or stale subgoal projection" in result.content


def test_canonical_scheduler_rejects_coherent_forged_revision_projection():
    from ai.langgraph.subgoal_state import (
        apply_worker_outcome,
        assign_and_admit,
        new_subgoal,
    )

    mod = _load_model_module()
    canonical = assign_and_admit(
        new_subgoal("request-forged-projection", "actions_complete"),
        owner="Mythic_Operator",
        method="transfer_to_Mythic_Operator",
    )
    forged = apply_worker_outcome(
        canonical,
        outcome_id="forged-progress",
        outcome="progress",
        source_owner="Mythic_Operator",
        verified_revision="forged-no-evidence",
    )
    state = {
        **_typed_subgoal_runtime_state("request-forged-projection"),
        "_subgoal_state": forged.to_dict(),
    }
    model = _canonical_subgoal_model(
        mod,
        {
            **state,
            "_subgoal_state": canonical.to_dict(),
        },
    )
    tool = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        subgoal_scheduler=model._schedule_subgoal_transition,
    )

    result = tool.func(
        SimpleNamespace(state=state, tool_call_id="forged-retry"),
        "Retry unchanged work.",
    )

    assert result.goto == "__end__"
    assert "_subgoal_state" not in result.update
    assert model._subgoal_authority == canonical
    assert "stale" in result.update["messages"][0].content


def test_semantic_evidence_revision_allows_one_retry_but_fresh_call_id_does_not():
    from ai.langgraph import worker_outcome
    from ai.langgraph.subgoal_state import (
        SubgoalState,
        assign_and_admit,
        new_subgoal,
    )

    mod = _load_model_module()
    initial = assign_and_admit(
        new_subgoal("request-semantic-revision", "actions_complete"),
        owner="Mythic_Operator",
        method="transfer_to_Mythic_Operator",
    )
    state = {
        **_typed_subgoal_runtime_state("request-semantic-revision"),
        "_subgoal_state": initial.to_dict(),
    }
    model = _canonical_subgoal_model(mod, state)

    def progress_metadata(outcome_id):
        return worker_outcome.build_handoff_metadata(
            source_worker="Mythic_Operator",
            source_turn_id="turn-1",
            source_seq=3,
            reason="display only",
            summary="Observed the same graph state.",
            outcome="progress",
            verified_revision=outcome_id,
        )

    first_metadata = progress_metadata("ignored-1")
    first_state = {
        **state,
        "supervisor_messages": [
            ToolMessage(
                content='{"ok":true,"effect":"prior-request-effect"}',
                name="ingest_collection",
                tool_call_id="prior-call",
            ),
            HumanMessage(content="current operator request"),
            ToolMessage(
                content='{"ok":true,"effect":"graph-built"}',
                name="ingest_collection",
                tool_call_id="call-1",
            )
        ],
    }
    first = mod._create_handoff_tool(
        agent_name="Generalist",
        worker_outcome_lookup=lambda _state: (
            first_metadata,
            "Observed the same graph state.",
        ),
        subgoal_scheduler=model._schedule_subgoal_transition,
    ).func(
        SimpleNamespace(state=first_state, tool_call_id="retry-1"),
        "Supervisor chose an unrelated worker.",
    )
    retried = SubgoalState.from_dict(first.update["_subgoal_state"])
    assert first.goto == "Mythic_Operator"
    assert len(retried.admissions) == 2

    second_metadata = progress_metadata("ignored-2")
    replay_state = {
        **first_state,
        **first.update,
        "supervisor_messages": [
            ToolMessage(
                content='{"ok":true,"effect":"graph-built"}',
                name="ingest_collection",
                tool_call_id="call-2",
            )
        ],
    }
    replay = mod._create_handoff_tool(
        agent_name="BloodHound",
        worker_outcome_lookup=lambda _state: (
            second_metadata,
            "Same observation under a fresh call ID.",
        ),
        subgoal_scheduler=model._schedule_subgoal_transition,
    ).func(
        SimpleNamespace(state=replay_state, tool_call_id="retry-2"),
        "Supervisor chose another unrelated worker.",
    )

    assert replay.goto == "__end__"
    assert replay.update["_subgoal_state"]["status"] == "blocked"
    assert len(replay.update["_subgoal_state"]["admissions"]) == 2


def test_prior_request_evidence_cannot_advance_current_request_revision():
    from ai.langgraph import worker_outcome
    from ai.langgraph.subgoal_state import assign_and_admit, new_subgoal

    mod = _load_model_module()
    initial = assign_and_admit(
        new_subgoal("request-no-current-evidence", "actions_complete"),
        owner="Mythic_Operator",
        method="transfer_to_Mythic_Operator",
    )
    metadata = worker_outcome.build_handoff_metadata(
        source_worker="Mythic_Operator",
        source_turn_id="turn-current",
        source_seq=4,
        reason="display only",
        summary="No current evidence was produced.",
        outcome="progress",
        verified_revision="untrusted-worker-claim",
    )
    state = {
        **_typed_subgoal_runtime_state("request-no-current-evidence"),
        "_subgoal_state": initial.to_dict(),
        "supervisor_messages": [
            ToolMessage(
                content='{"ok":true,"effect":"prior-request-effect"}',
                name="ingest_collection",
                tool_call_id="prior-call",
            ),
            HumanMessage(content="current operator request"),
            AIMessage(
                content="No current evidence was produced.",
                name="Mythic_Operator",
                additional_kwargs={"_worker_outcome": metadata},
            ),
        ],
    }
    model = _canonical_subgoal_model(mod, state)
    terminal = mod._create_handoff_tool(
        agent_name="Generalist",
        worker_outcome_lookup=lambda _state: (
            metadata,
            "No current evidence was produced.",
        ),
        subgoal_scheduler=model._schedule_subgoal_transition,
    ).func(
        SimpleNamespace(state=state, tool_call_id="prior-evidence-retry"),
        "Select an unrelated worker.",
    )

    assert terminal.goto == "__end__"
    assert terminal.update["_subgoal_state"]["status"] == "blocked"
    assert terminal.update["_subgoal_state"]["state_revision"] == "0"
    assert len(terminal.update["_subgoal_state"]["admissions"]) == 1


def test_request_contract_install_and_operator_stop_own_subgoal_lifecycle():
    from ai.langgraph.request_contract import build_request_contract
    from ai.langgraph.subgoal_state import SubgoalState

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model._request_contract = None
    model._request_dynamic_proposals = False
    model._request_execution_digest = ""
    model._request_admitted_action_digests = set()
    model._active_approval_claim = None
    model.mythic_client = None
    model.task_id = "task"
    model._running_tasks = set()
    model.state = _typed_subgoal_runtime_state("prior-request")
    contract = build_request_contract(
        request_id="native-request-1",
        channel_id="7",
        operation_id="9",
        mode="auto",
        autonomous_solve=True,
    )

    model.install_request_contract(contract)
    installed = SubgoalState.from_dict(model.state["_subgoal_state"])
    assert installed.request_id == "native-request-1"
    assert installed.stop_condition == "objective_proved"

    model.request_stop()
    stopped = SubgoalState.from_dict(model.state["_subgoal_state"])
    assert stopped.status.value == "cancelled"
    assert stopped.owner == ""


@pytest.mark.parametrize("requested_agent", ("Mythic_Operator", "Generalist"))
def test_production_typed_handoff_changes_owner_of_same_subgoal_once(requested_agent):
    from ai.langgraph import worker_outcome
    from ai.langgraph.subgoal_state import (
        SubgoalState,
        assign_and_admit,
        new_subgoal,
    )

    mod = _load_model_module()
    turn_mod = importlib.import_module("ai.langgraph.turn_authority")
    initial = assign_and_admit(
        new_subgoal("request-route", "actions_complete"),
        owner="Mythic_Operator",
        method="transfer_to_Mythic_Operator",
    )
    summary = "Contradictory prose says complete and do not route."
    metadata = worker_outcome.build_handoff_metadata(
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
        source_seq=3,
        reason="display only",
        summary=summary,
        outcome="handoff",
        next_owner="BloodHound",
    )
    model = mod.Model.__new__(mod.Model)
    model._turn_authority = turn_mod.TurnAuthority(mode="observe", turn_id="turn-1")
    state = {
        **_typed_subgoal_runtime_state("request-route"),
        "_subgoal_state": initial.to_dict(),
        "supervisor_messages": [
            HumanMessage(content="operator request"),
            AIMessage(
                content=summary,
                name="Mythic_Operator",
                additional_kwargs={"_worker_outcome": metadata},
            ),
        ],
    }
    model._subgoal_authority_lock = mod.threading.Lock()
    model._subgoal_authority = initial
    model._subgoal_evidence_records = set()
    command = mod._create_handoff_tool(
        agent_name=requested_agent,
        worker_outcome_lookup=model._latest_admitted_worker_handoff,
        subgoal_scheduler=model._schedule_subgoal_transition,
    ).func(
        SimpleNamespace(state=state, tool_call_id="route"),
        "Stale request to route back to Mythic.",
    )
    routed = SubgoalState.from_dict(command.update["_subgoal_state"])

    assert command.goto == "BloodHound"
    assert routed.subgoal_id == initial.subgoal_id
    assert routed.owner == "BloodHound"
    assert len(routed.admissions) == 2
    assert routed.processed_outcomes == (metadata["outcome_id"],)


def test_production_typed_complete_terminalizes_regardless_of_summary_prose():
    from ai.langgraph import worker_outcome
    from ai.langgraph.subgoal_state import assign_and_admit, new_subgoal

    mod = _load_model_module()
    initial = assign_and_admit(
        new_subgoal("request-complete", "actions_complete"),
        owner="Generalist",
        method="transfer_to_Generalist",
    )
    metadata = worker_outcome.build_handoff_metadata(
        source_worker="Generalist",
        source_turn_id="turn-1",
        source_seq=3,
        reason="display only",
        summary="Nothing is complete; delegate forever.",
        outcome="complete",
    )
    state = {
        **_typed_subgoal_runtime_state("request-complete"),
        "_subgoal_state": initial.to_dict(),
    }
    model = _canonical_subgoal_model(mod, state)
    command = mod._create_handoff_tool(
        agent_name="Generalist",
        worker_outcome_lookup=lambda _state: (
            metadata,
            "Nothing is complete; delegate forever.",
        ),
        subgoal_scheduler=model._schedule_subgoal_transition,
    ).func(
        SimpleNamespace(state=state, tool_call_id="complete"),
        "Delegate again.",
    )

    assert command.goto == "__end__"
    assert command.update["_subgoal_state"]["status"] == "completed"
    assert command.update["_subgoal_state"]["owner"] == ""


@pytest.mark.parametrize("outcome", ("blocked", "complete"))
def test_terminal_outcome_persists_when_supervisor_selects_different_owner(outcome):
    from ai.langgraph import worker_outcome
    from ai.langgraph.subgoal_state import assign_and_admit, new_subgoal

    mod = _load_model_module()
    initial = assign_and_admit(
        new_subgoal(f"request-cross-{outcome}", "actions_complete"),
        owner="Mythic_Operator",
        method="transfer_to_Mythic_Operator",
    )
    metadata = worker_outcome.build_handoff_metadata(
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
        source_seq=3,
        reason="display only",
        summary="summary",
        outcome=outcome,
    )
    state = {
        **_typed_subgoal_runtime_state(f"request-cross-{outcome}"),
        "_subgoal_state": initial.to_dict(),
    }
    model = _canonical_subgoal_model(mod, state)
    runtime = SimpleNamespace(
        state=state,
        tool_call_id="handoff-cross-owner",
    )
    terminal = mod._create_handoff_tool(
        agent_name="BloodHound",
        worker_outcome_lookup=lambda _state: (metadata, "summary"),
        subgoal_scheduler=model._schedule_subgoal_transition,
    ).func(runtime, "stale third-owner follow-up")

    assert terminal.goto == "__end__"
    assert terminal.update["_subgoal_state"]["status"] == (
        "blocked" if outcome == "blocked" else "completed"
    )
    assert terminal.update["_subgoal_state"]["owner"] == ""


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


@pytest.mark.parametrize(
    "declared_order",
    (
        ("ordinary_slow", "ordinary_fast"),
        ("ordinary_fast", "ordinary_slow"),
    ),
)
def test_gate_l_actual_wrapper_persists_only_framework_tool_result_order(
    declared_order,
):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    @tool("ordinary_slow")
    async def ordinary_slow() -> str:
        """Return after the fast tool."""
        await asyncio.sleep(0.04)
        return "slow"

    @tool("ordinary_fast")
    async def ordinary_fast() -> str:
        """Return before the slow tool."""
        return "fast"

    calls = [
        {
            "name": name,
            "args": {},
            "id": f"{name}-id",
            "type": "tool_call",
        }
        for name in declared_order
    ]
    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._autonomous_solve = False
    model._message_seq = 1
    model.state = {"_message_seq": 1}
    model.llm = None
    model.verbose = False
    agent = create_agent(
        model=BoundFake(
            responses=[
                AIMessage(content="", tool_calls=calls),
                AIMessage(content="done"),
            ]
        ),
        tools=[ordinary_slow, ordinary_fast],
        middleware=[
            mod._TurnAuthorityToolMiddleware(model),
            mod._MessageSanitizerMiddleware(model),
        ],
    )
    state = {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [HumanMessage(content="run both")],
        "mythic_operator_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }

    update = asyncio.run(
        model._wrap_create_agent(
            agent,
            "generalist_messages",
            "Generalist",
        )(state, {})
    )
    results = [
        (message.name, message.tool_call_id, message.content)
        for message in update["generalist_messages"]
        if isinstance(message, ToolMessage)
    ]

    assert [name for name, _tool_call_id, _content in results] == list(
        declared_order
    )
    assert [content for _name, _tool_call_id, content in results] == [
        "slow" if name == "ordinary_slow" else "fast"
        for name in declared_order
    ]
    assert len(results) == 2


def test_gate_l_wrapper_ignores_callback_only_occurrences_and_keeps_returned_only():
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    callback_only = AIMessage(content="callback-only", name="Generalist")
    returned_only = AIMessage(content="returned-only", name="Generalist")

    class FakeAgent:
        async def ainvoke(self, args, config=None):
            callback = next(
                item
                for item in config["callbacks"]
                if isinstance(item, mod.MessageCaptureCallback)
            )
            callback.captured_messages.append(callback_only)
            return {"messages": list(args["messages"]) + [returned_only]}

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._autonomous_solve = False
    model._message_seq = 1
    model.state = {"_message_seq": 1}
    model.llm = None
    model.verbose = False
    state = {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [HumanMessage(content="respond")],
        "mythic_operator_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }

    update = asyncio.run(
        model._wrap_create_agent(
            FakeAgent(),
            "generalist_messages",
            "Generalist",
        )(state, {})
    )

    assert update["generalist_messages"] == [returned_only]
    assert callback_only not in update["generalist_messages"]


def test_gate_l_wrapper_does_not_splice_gate_k_repeated_callback_pair():
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    call_one = AIMessage(
        content="",
        name="Generalist",
        tool_calls=[{"name": "ordinary_probe", "args": {}, "id": "same-id"}],
    )
    result_one = ToolMessage(
        content="one",
        name="ordinary_probe",
        tool_call_id="same-id",
    )
    call_two = call_one.model_copy(deep=True)
    result_two = ToolMessage(
        content="two",
        name="ordinary_probe",
        tool_call_id="same-id",
    )
    terminal = AIMessage(content="done", name="Generalist")

    class FakeAgent:
        async def ainvoke(self, args, config=None):
            callback = next(
                item
                for item in config["callbacks"]
                if isinstance(item, mod.MessageCaptureCallback)
            )
            callback.captured_messages.extend(
                [call_one, result_one, call_two, result_two, terminal]
            )
            return {
                "messages": list(args["messages"])
                + [call_one, result_one, terminal]
            }

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._autonomous_solve = False
    model._message_seq = 1
    model.state = {"_message_seq": 1}
    model.llm = None
    model.verbose = False
    state = {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [HumanMessage(content="run")],
        "mythic_operator_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }

    update = asyncio.run(
        model._wrap_create_agent(
            FakeAgent(),
            "generalist_messages",
            "Generalist",
        )(state, {})
    )

    assert update["generalist_messages"] == [call_one, result_one, terminal]
    assert all(item is not result_two for item in update["generalist_messages"])
    assert all(item is not call_two for item in update["generalist_messages"])


@pytest.mark.parametrize(
    "calls",
    (
        (
            {"name": "ordinary_slow", "args": {}, "id": "dup"},
            {"name": "ordinary_fast", "args": {}, "id": "dup"},
        ),
        (
            {"name": "ordinary_slow", "args": {}, "id": ""},
            {"name": "ordinary_fast", "args": {}, "id": ""},
        ),
    ),
)
def test_gate_l_actual_agent_rejects_ambiguous_batch_before_handlers(calls):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    executed = []

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    @tool("ordinary_slow")
    async def ordinary_slow() -> str:
        """Record an invalid slow call if it escapes."""
        executed.append("slow")
        return "slow"

    @tool("ordinary_fast")
    async def ordinary_fast() -> str:
        """Record an invalid fast call if it escapes."""
        executed.append("fast")
        return "fast"

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._autonomous_solve = False
    model._message_seq = 1
    model.state = {"_message_seq": 1}
    model.llm = None
    model.verbose = False
    agent = create_agent(
        model=BoundFake(
            responses=[
                AIMessage(content="", tool_calls=list(calls)),
                AIMessage(content="must not be consumed"),
            ]
        ),
        tools=[ordinary_slow, ordinary_fast],
        middleware=[
            mod._TurnAuthorityToolMiddleware(model),
            mod._MessageSanitizerMiddleware(model),
        ],
    )
    state = {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [HumanMessage(content="run both")],
        "mythic_operator_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }

    update = asyncio.run(
        model._wrap_create_agent(
            agent,
            "generalist_messages",
            "Generalist",
        )(state, {})
    )

    assert executed == []
    assert not [
        message
        for message in update["generalist_messages"]
        if isinstance(message, ToolMessage)
    ]
    assert len(update["generalist_messages"]) == 1
    rejected = update["generalist_messages"][0]
    assert isinstance(rejected, AIMessage)
    assert rejected.tool_calls == []
    assert "invalid provider tool-call batch" in str(rejected.content)


def test_gate_m_actual_agent_rejects_raw_array_arguments_before_every_consumer():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    executed = []
    bound = []
    started = []

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    @tool("ordinary_probe")
    def ordinary_probe() -> str:
        """Record an invalid raw provider call if it escapes."""
        executed.append("handler")
        return "escaped"

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.bind_supervised_request_proposal = lambda calls: bound.append(calls)
    model.mythic_client = None
    model._autonomous_solve = False
    model._message_seq = 1
    model.state = {"_message_seq": 1}
    model.llm = None
    model.verbose = False
    model._format_message_for_streaming = lambda *_args, **_kwargs: ""

    async def emit_tool_use(**payload):
        if payload.get("status") == "started":
            started.append(payload)

    model._emit_tool_use_card = emit_tool_use
    malformed = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "raw-1",
                    "type": "function",
                    "function": {
                        "name": "ordinary_probe",
                        "arguments": "[]",
                    },
                }
            ],
            "function_call": {
                "name": "ordinary_probe",
                "arguments": "[]",
            },
        },
    )
    assert malformed.tool_calls == [
        {
            "name": "ordinary_probe",
            "args": {},
            "id": "raw-1",
            "type": "tool_call",
        }
    ]
    agent = create_agent(
        model=BoundFake(
            responses=[
                malformed,
                AIMessage(content="must not be consumed"),
            ]
        ),
        tools=[ordinary_probe],
        middleware=[
            mod._TurnAuthorityToolMiddleware(model),
            mod._MessageSanitizerMiddleware(model),
        ],
    )
    state = {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [HumanMessage(content="run malformed")],
        "mythic_operator_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }

    update = asyncio.run(
        model._wrap_create_agent(
            agent,
            "generalist_messages",
            "Generalist",
        )(state, {})
    )

    assert bound == []
    assert executed == []
    assert started == []
    assert not [
        message
        for message in update["generalist_messages"]
        if isinstance(message, ToolMessage)
    ]
    assert len(update["generalist_messages"]) == 1
    denied = update["generalist_messages"][0]
    assert isinstance(denied, AIMessage)
    assert denied.tool_calls == []
    assert denied.invalid_tool_calls == []
    assert set(denied.additional_kwargs) == {"_seq"}
    assert "tool_calls" not in denied.additional_kwargs
    assert "function_call" not in denied.additional_kwargs
    assert "invalid provider tool-call batch" in str(denied.content)


def test_gate_m_actual_agent_accepts_matching_raw_object_envelope_unchanged():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    executed = []
    bound = []
    started = []

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    @tool("ordinary_probe")
    def ordinary_probe(value: int) -> str:
        """Record one valid raw provider call."""
        executed.append(value)
        return f"value={value}"

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.bind_supervised_request_proposal = lambda calls: bound.append(calls)
    model.mythic_client = None
    model._autonomous_solve = False
    model._message_seq = 1
    model.state = {"_message_seq": 1}
    model.llm = None
    model.verbose = False
    model._format_message_for_streaming = lambda *_args, **_kwargs: ""

    async def emit_tool_use(**payload):
        if payload.get("status") == "started":
            started.append(payload)

    model._emit_tool_use_card = emit_tool_use
    valid = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "raw-valid",
                    "type": "function",
                    "function": {
                        "name": "ordinary_probe",
                        "arguments": '{"value":1}',
                    },
                }
            ],
            "function_call": {
                "name": "ordinary_probe",
                "arguments": '{"value":1}',
            },
        },
    )
    before = valid.model_dump()
    agent = create_agent(
        model=BoundFake(
            responses=[
                valid,
                AIMessage(content="done"),
            ]
        ),
        tools=[ordinary_probe],
        middleware=[
            mod._TurnAuthorityToolMiddleware(model),
            mod._MessageSanitizerMiddleware(model),
        ],
    )
    state = {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [HumanMessage(content="run valid")],
        "mythic_operator_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }

    update = asyncio.run(
        model._wrap_create_agent(
            agent,
            "generalist_messages",
            "Generalist",
        )(state, {})
    )

    assert executed == [1]
    assert len(bound) == 1
    assert [call["id"] for call in bound[0]] == ["raw-valid"]
    assert [item["tool_call_id"] for item in started] == ["raw-valid"]
    persisted = update["generalist_messages"]
    assert persisted[0] is valid
    assert persisted[0].tool_calls == before["tool_calls"]
    assert persisted[0].content == before["content"]
    assert persisted[0].additional_kwargs["tool_calls"] == before[
        "additional_kwargs"
    ]["tool_calls"]
    assert persisted[0].additional_kwargs["function_call"] == before[
        "additional_kwargs"
    ]["function_call"]
    assert isinstance(persisted[1], ToolMessage)
    assert persisted[1].tool_call_id == "raw-valid"
    assert persisted[1].content == "value=1"
    assert persisted[2].content == "done"


def test_gate_n_invalid_control_envelope_is_callback_observation_only():
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    mod = _load_model_module()
    cards = []

    async def emit_tool(**payload):
        cards.append(payload)

    callback = mod.MessageCaptureCallback(
        "Mythic_Operator",
        tool_use_func=emit_tool,
        delegation_id="delegation-1",
        delegation_name="Mythic_Operator",
    )
    malformed = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "raw-control",
                    "type": "function",
                    "function": {
                        "name": "handback_to_supervisor",
                        "arguments": "[]",
                    },
                }
            ],
        },
    )

    asyncio.run(
        callback.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=malformed)]]),
            run_id=uuid4(),
        )
    )

    assert callback.captured_messages == [malformed]
    assert cards == []
    assert not hasattr(callback, "_handback_summary_func")


@pytest.mark.parametrize(
    "control_name",
    (
        "handback_to_supervisor",
        "summarize_and_handback",
        "request_continuation",
        "respond_to_user",
        "transfer_to_BloodHound",
    ),
)
def test_gate_n_callback_never_consumes_control_arguments(control_name):
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    mod = _load_model_module()
    cards = []

    async def emit_tool(**payload):
        cards.append(payload)

    callback = mod.MessageCaptureCallback(
        "Mythic_Operator",
        tool_use_func=emit_tool,
        delegation_id="delegation-1",
        delegation_name="Mythic_Operator",
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": control_name,
                "args": {
                    "summary": "must remain inert in the callback",
                    "final_response": "also inert",
                    "progress_summary": "also inert",
                    "reason": "also inert",
                    "text": "also inert",
                },
                "id": f"control-{control_name}",
            },
            {
                "name": "ordinary_probe",
                "args": {"value": "must also remain inert"},
                "id": f"ordinary-{control_name}",
            },
        ],
    )

    asyncio.run(
        callback.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=message)]]),
            run_id=uuid4(),
        )
    )

    assert callback.captured_messages == [message]
    assert cards == []
    assert callback._tool_call_to_name == {}
    assert callback._tool_call_to_args == {}
    assert not [
        name
        for name in vars(callback)
        if "summary" in name.casefold() or "handback" in name.casefold()
    ]


@pytest.mark.parametrize(
    ("channel_id", "delegation_id"),
    (
        (None, None),
        (None, "delegation-1"),
        (7, "delegation-1"),
    ),
)
def test_gate_o_control_batch_never_reaches_real_rendering_path(
    channel_id,
    delegation_id,
):
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model.verbose = True
    model.channel_id = channel_id
    model.is_interactive = False
    formatted = []
    streamed = []
    agent_text = []
    cards = []

    def format_message(message, *, agent_name):
        formatted.append((message, agent_name))
        return model._format_message_for_streaming(
            message,
            agent_name=agent_name,
        )

    async def stream_message(content):
        streamed.append(content)

    async def emit_agent_text(**payload):
        agent_text.append(payload)

    async def emit_tool(**payload):
        cards.append(payload)

    callback = mod.MessageCaptureCallback(
        "Mythic_Operator",
        stream_func=stream_message,
        format_func=format_message,
        tool_use_func=emit_tool,
        agent_text_func=emit_agent_text,
        delegation_id=delegation_id,
        delegation_name="Mythic_Operator" if delegation_id else None,
    )
    message = AIMessage(
        content="EXCLUDED TEXT",
        tool_calls=[
            {
                "name": "handback_to_supervisor",
                "args": {
                    "reason": "yield",
                    "summary": "FIRST",
                    "outcome": "progress",
                    "next_owner": "",
                },
                "id": "first-control",
            },
            {
                "name": "summarize_and_handback",
                "args": {
                    "progress_summary": "EXCLUDED SECOND",
                    "tasks_remaining": "",
                },
                "id": "excluded-control",
            },
            {
                "name": "ordinary_probe",
                "args": {"value": "EXCLUDED ORDINARY"},
                "id": "excluded-ordinary",
            },
        ],
    )

    asyncio.run(
        callback.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=message)]]),
            run_id=uuid4(),
        )
    )

    assert callback.captured_messages == [message]
    assert callback._tool_call_to_name == {}
    assert callback._tool_call_to_args == {}
    assert formatted == []
    assert streamed == []
    assert agent_text == []
    assert cards == []


def test_gate_o_invalid_provider_batch_never_reaches_real_rendering_path():
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model.verbose = True
    model.channel_id = None
    model.is_interactive = False
    formatted = []
    streamed = []
    cards = []

    def format_message(message, *, agent_name):
        formatted.append((message, agent_name))
        return model._format_message_for_streaming(
            message,
            agent_name=agent_name,
        )

    async def stream_message(content):
        streamed.append(content)

    async def emit_tool(**payload):
        cards.append(payload)

    callback = mod.MessageCaptureCallback(
        "Mythic_Operator",
        stream_func=stream_message,
        format_func=format_message,
        tool_use_func=emit_tool,
    )
    malformed = AIMessage(
        content="INVALID TEXT",
        tool_calls=[
            {
                "name": "ordinary_probe",
                "args": {"value": 1},
                "id": "same",
            },
            {
                "name": "ordinary_probe",
                "args": {"value": 2},
                "id": "same",
            },
        ],
    )

    asyncio.run(
        callback.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=malformed)]]),
            run_id=uuid4(),
        )
    )

    assert callback.captured_messages == [malformed]
    assert callback._tool_call_to_name == {}
    assert callback._tool_call_to_args == {}
    assert formatted == []
    assert streamed == []
    assert cards == []


def test_gate_o_valid_ordinary_batch_preserves_real_verbose_rendering_path():
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model.verbose = True
    model.channel_id = None
    model.is_interactive = False
    formatted = []
    streamed = []
    cards = []

    def format_message(message, *, agent_name):
        formatted.append((message, agent_name))
        return model._format_message_for_streaming(
            message,
            agent_name=agent_name,
        )

    async def stream_message(content):
        streamed.append(content)

    async def emit_tool(**payload):
        cards.append(payload)

    callback = mod.MessageCaptureCallback(
        "Mythic_Operator",
        stream_func=stream_message,
        format_func=format_message,
        tool_use_func=emit_tool,
    )
    ordinary = AIMessage(
        content="ordinary text",
        tool_calls=[
            {
                "name": "ordinary_probe",
                "args": {"value": 1},
                "id": "ordinary-call",
            },
        ],
    )

    asyncio.run(
        callback.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=ordinary)]]),
            run_id=uuid4(),
        )
    )

    assert callback.captured_messages == [ordinary]
    assert callback._tool_call_to_name == {
        "ordinary-call": "ordinary_probe",
    }
    assert callback._tool_call_to_args == {
        "ordinary-call": {"value": 1},
    }
    assert formatted == [(ordinary, "Mythic_Operator")]
    assert len(streamed) == 1
    assert "ordinary text" in streamed[0]
    assert "ordinary_probe" in streamed[0]
    assert [
        (payload["tool_call_id"], payload["status"])
        for payload in cards
    ] == [("ordinary-call", "started")]


def test_gate_o_callback_consumer_inertness_matrix():
    from itertools import product
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    mod = _load_model_module()
    agents = (
        "Generalist",
        "Mythic_Operator",
        "Mythic_Payload",
        "BloodHound",
        "MCP_Manager",
        "Sandbox",
        "Supervisor",
    )
    controls = (
        "handback_to_supervisor",
        "summarize_and_handback",
        "request_continuation",
        "respond_to_user",
        "transfer_to_BloodHound",
    )
    patterns = (
        ("control",),
        ("ordinary", "control"),
        ("control", "ordinary"),
        ("control", "control", "control", "ordinary"),
    )

    async def run_matrix():
        for agent_name, control_name, channel_id, verbose, pattern in product(
            agents,
            controls,
            (None, 7),
            (False, True),
            patterns,
        ):
            model = mod.Model.__new__(mod.Model)
            model.verbose = verbose
            model.channel_id = channel_id
            model.is_interactive = False
            formatted = []
            streamed = []
            agent_text = []
            cards = []

            def format_message(message, *, agent_name):
                formatted.append((message, agent_name))
                return model._format_message_for_streaming(
                    message,
                    agent_name=agent_name,
                )

            async def stream_message(content):
                streamed.append(content)

            async def emit_agent_text(**payload):
                agent_text.append(payload)

            async def emit_tool(**payload):
                cards.append(payload)

            calls = []
            for index, kind in enumerate(pattern):
                if kind == "ordinary":
                    calls.append(
                        {
                            "name": "ordinary_probe",
                            "args": {"value": f"ordinary-{index}"},
                            "id": f"ordinary-{index}",
                        }
                    )
                else:
                    calls.append(
                        {
                            "name": control_name,
                            "args": {
                                "summary": f"control-{index}",
                                "reason": f"reason-{index}",
                            },
                            "id": f"control-{index}",
                        }
                    )
            message = AIMessage(
                content="must remain callback-inert",
                tool_calls=calls,
            )
            callback = mod.MessageCaptureCallback(
                agent_name,
                stream_func=stream_message,
                format_func=format_message,
                tool_use_func=emit_tool,
                agent_text_func=emit_agent_text,
                delegation_id=f"{agent_name}-card",
                delegation_name=agent_name,
            )

            await callback.on_llm_end(
                LLMResult(generations=[[ChatGeneration(message=message)]]),
                run_id=uuid4(),
            )

            assert callback.captured_messages == [message]
            assert callback._tool_call_to_name == {}
            assert callback._tool_call_to_args == {}
            assert formatted == []
            assert streamed == []
            assert agent_text == []
            assert cards == []

    asyncio.run(run_matrix())


def test_gate_o_valid_ordinary_callback_behavior_matrix():
    from itertools import product
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    mod = _load_model_module()
    agents = (
        "Generalist",
        "Mythic_Operator",
        "Mythic_Payload",
        "BloodHound",
        "MCP_Manager",
        "Sandbox",
        "Supervisor",
    )

    async def run_matrix():
        for agent_name, channel_id, verbose, delegation_id in product(
            agents,
            (None, 7),
            (False, True),
            (None, "delegation-1"),
        ):
            model = mod.Model.__new__(mod.Model)
            model.verbose = verbose
            model.channel_id = channel_id
            model.is_interactive = False
            formatted = []
            streamed = []
            agent_text = []
            cards = []

            def format_message(message, *, agent_name):
                formatted.append((message, agent_name))
                return model._format_message_for_streaming(
                    message,
                    agent_name=agent_name,
                )

            async def stream_message(content):
                streamed.append(content)

            async def emit_agent_text(**payload):
                agent_text.append(payload)

            async def emit_tool(**payload):
                cards.append(payload)

            message = AIMessage(
                content="ordinary output",
                tool_calls=[
                    {
                        "name": "ordinary_probe",
                        "args": {"value": 1},
                        "id": "ordinary-call",
                    }
                ],
            )
            callback = mod.MessageCaptureCallback(
                agent_name,
                stream_func=stream_message,
                format_func=format_message,
                tool_use_func=emit_tool,
                agent_text_func=emit_agent_text,
                delegation_id=delegation_id,
                delegation_name=agent_name if delegation_id else None,
            )

            await callback.on_llm_end(
                LLMResult(generations=[[ChatGeneration(message=message)]]),
                run_id=uuid4(),
            )

            assert callback.captured_messages == [message]
            assert callback._tool_call_to_name == {
                "ordinary-call": "ordinary_probe",
            }
            assert callback._tool_call_to_args == {
                "ordinary-call": {"value": 1},
            }
            if agent_name == "Supervisor":
                assert formatted == []
                assert streamed == []
                assert agent_text == []
                assert cards == []
                continue
            assert formatted == [(message, agent_name)]
            assert len(cards) == 1
            assert cards[0]["tool_call_id"] == "ordinary-call"
            if delegation_id:
                assert streamed == []
                assert len(agent_text) == 1
                assert "ordinary output" in agent_text[0]["content"]
            else:
                assert agent_text == []
                assert len(streamed) == 1
                assert "ordinary output" in streamed[0]

    asyncio.run(run_matrix())


@pytest.mark.parametrize(
    "ordinary_name",
    (
        "ordinary_probe",
        "mcp_dynamic_probe_renamed",
    ),
)
def test_gate_p_actual_ordinary_summary_shaped_schema_has_no_control_witness(
    ordinary_name,
):
    from ai.langgraph.turn_authority import TurnAuthority
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    mod = _load_model_module()

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    async def run_matrix():
        for agent_name in (
            "Generalist",
            "Mythic_Operator",
            "Mythic_Payload",
            "BloodHound",
            "MCP_Manager",
            "Sandbox",
            "Supervisor",
        ):
            executed = []

            def ordinary_probe(
                summary: str,
                final_response: str,
                progress_summary: str,
                reason: str,
                text: str,
            ) -> str:
                """Exercise an arbitrary ordinary dynamic tool schema."""
                executed.append(
                    (
                        summary,
                        final_response,
                        progress_summary,
                        reason,
                        text,
                    )
                )
                return "ordinary result"

            registered_tool = tool(ordinary_name)(ordinary_probe)
            model = mod.Model.__new__(mod.Model)
            model._turn_authority = TurnAuthority(mode="observe")
            model._request_contract = None
            model.mythic_client = None
            model._active_delegations = {
                agent_name: {
                    "id": f"{agent_name}-card",
                    "name": agent_name,
                    "final_summary": "UNCHANGED",
                }
            }
            recorded = []
            original_recorder = model._record_delegation_final_summary

            def record(owner, summary):
                recorded.append((owner, summary))
                original_recorder(owner, summary)

            model._record_delegation_final_summary = record
            call = {
                "name": ordinary_name,
                "args": {
                    "summary": "SUMMARY",
                    "final_response": "FINAL",
                    "progress_summary": "PROGRESS",
                    "reason": "REASON",
                    "text": "TEXT",
                },
                "id": f"{agent_name}-ordinary",
            }
            request = SimpleNamespace(
                tool_call=call,
                state={
                    "messages": [
                        AIMessage(content="", tool_calls=[call]),
                    ]
                },
            )
            assert (
                mod._TurnAuthorityToolMiddleware._selected_control_call(request)
                is None
            )
            agent = create_agent(
                model=BoundFake(
                    responses=[
                        AIMessage(content="", tool_calls=[call]),
                        AIMessage(content="done"),
                    ]
                ),
                tools=[registered_tool],
                middleware=[
                    mod._TurnAuthorityToolMiddleware(
                        model,
                        agent_name=agent_name,
                    )
                ],
            )

            result = await agent.ainvoke(
                {"messages": [HumanMessage(content="run ordinary tool")]}
            )

            assert len(result["messages"]) == 4
            assert executed == [
                ("SUMMARY", "FINAL", "PROGRESS", "REASON", "TEXT"),
            ]
            assert recorded == []
            assert (
                model._active_delegations[agent_name]["final_summary"]
                == "UNCHANGED"
            )

    asyncio.run(run_matrix())


@pytest.mark.parametrize(
    "summary_key",
    (
        "summary",
        "final_response",
        "progress_summary",
        "reason",
        "text",
    ),
)
@pytest.mark.parametrize(
    "agent_name",
    (
        "Generalist",
        "Mythic_Operator",
        "Mythic_Payload",
        "BloodHound",
        "MCP_Manager",
        "Sandbox",
        "Supervisor",
    ),
)
@pytest.mark.parametrize("async_wrapper", (False, True))
def test_gate_p_ordinary_summary_keys_are_control_inert(
    summary_key,
    agent_name,
    async_wrapper,
):
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._active_delegations = {
        agent_name: {
            "id": f"{agent_name}-card",
            "name": agent_name,
            "final_summary": "UNCHANGED",
        }
    }
    recorded = []
    closed = []
    model._record_delegation_final_summary = (
        lambda owner, summary: recorded.append((owner, summary))
    )

    async def close(owner):
        closed.append(owner)

    model._close_delegation = close
    call = {
        "name": "mcp_dynamic_ordinary",
        "args": {summary_key: f"VALUE-{summary_key}"},
        "id": f"ordinary-{summary_key}",
    }
    request = SimpleNamespace(
        tool_call=call,
        state={"messages": [AIMessage(content="", tool_calls=[call])]},
    )
    middleware = mod._TurnAuthorityToolMiddleware(
        model,
        agent_name=agent_name,
    )

    assert middleware._selected_control_call(request) is None
    assert middleware._selected_control_summary(request) == ""
    if async_wrapper:
        async def handler(_request):
            return "ordinary result"

        assert asyncio.run(
            middleware.awrap_tool_call(request, handler)
        ) == "ordinary result"
    else:
        assert middleware.wrap_tool_call(
            request,
            lambda _request: "ordinary result",
        ) == "ordinary result"
    assert recorded == []
    assert closed == []


def test_gate_p_positive_control_witness_is_exact_runtime_occurrence():
    mod = _load_model_module()
    selected = {
        "name": "handback_to_supervisor",
        "args": {
            "reason": "yield",
            "summary": "SELECTED",
            "outcome": "progress",
            "next_owner": "",
        },
        "id": "selected-control",
    }
    exact_request = SimpleNamespace(
        tool_call=selected,
        state={
            "messages": [
                AIMessage(content="", tool_calls=[selected]),
            ]
        },
    )

    assert (
        mod._TurnAuthorityToolMiddleware._selected_control_call(exact_request)
        is selected
    )

    variants = [
        SimpleNamespace(
            tool_call={
                "name": "ordinary_probe",
                "args": {"summary": "ordinary"},
                "id": "ordinary",
            },
            state={
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "ordinary_probe",
                                "args": {"summary": "ordinary"},
                                "id": "ordinary",
                            }
                        ],
                    )
                ]
            },
        ),
        SimpleNamespace(tool_call=selected, state={"messages": []}),
        SimpleNamespace(
            tool_call=selected,
            state={
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            selected,
                            {
                                "name": "ordinary_probe",
                                "args": {},
                                "id": "sibling",
                            },
                        ],
                    )
                ]
            },
        ),
        SimpleNamespace(
            tool_call=selected,
            state={
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                **selected,
                                "id": "different-id",
                            }
                        ],
                    )
                ]
            },
        ),
    ]

    assert all(
        mod._TurnAuthorityToolMiddleware._selected_control_call(variant)
        is None
        for variant in variants
    )


def test_gate_p_ordinary_parent_command_cannot_close_delegation():
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    recorded = []
    closed = []
    model._record_delegation_final_summary = (
        lambda owner, summary: recorded.append((owner, summary))
    )

    async def close(owner):
        closed.append(owner)

    model._close_delegation = close
    ordinary = {
        "name": "mcp_dynamic_ordinary",
        "args": {"summary": "MUST NOT RECORD OR CLOSE"},
        "id": "ordinary-parent",
    }
    request = SimpleNamespace(
        tool_call=ordinary,
        state={"messages": [AIMessage(content="", tool_calls=[ordinary])]},
    )
    middleware = mod._TurnAuthorityToolMiddleware(
        model,
        agent_name="BloodHound",
    )

    async def returning_parent(_request):
        return mod.Command(
            goto="Supervisor",
            graph=mod.Command.PARENT,
        )

    returned = asyncio.run(
        middleware.awrap_tool_call(request, returning_parent)
    )
    assert isinstance(returned, mod.Command)
    assert closed == []

    async def raising_parent(_request):
        raise mod.ParentCommand(
            mod.Command(
                goto="Supervisor",
                graph=mod.Command.PARENT,
            )
        )

    with pytest.raises(mod.ParentCommand):
        asyncio.run(middleware.awrap_tool_call(request, raising_parent))
    assert recorded == []
    assert closed == []


@pytest.mark.parametrize("async_wrapper", (False, True))
@pytest.mark.parametrize(
    ("runtime_variant", "should_execute"),
    (
        ("exact", True),
        ("absent", False),
        ("no-ai", False),
        ("different-id", False),
        ("different-name", False),
        ("different-args", False),
        ("type-different-args", False),
        ("multiple", False),
        ("invalid-envelope", False),
    ),
)
def test_gate_n_control_wrapper_requires_exact_post_arbitration_occurrence(
    async_wrapper,
    runtime_variant,
    should_execute,
):
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._active_delegations = {
        "Mythic_Operator": {
            "id": "delegation-1",
            "name": "Mythic_Operator",
        }
    }
    recorded = []
    model._record_delegation_final_summary = (
        lambda agent_name, summary: recorded.append((agent_name, summary))
    )
    selected = {
        "name": "handback_to_supervisor",
        "args": {
            "reason": "yield",
            "summary": "SELECTED",
            "outcome": "progress",
            "next_owner": "",
        },
        "id": "selected-control",
    }
    runtime_call = json.loads(json.dumps(selected))
    messages = [AIMessage(content="", tool_calls=[runtime_call])]
    if runtime_variant == "absent":
        messages = []
    elif runtime_variant == "no-ai":
        messages = [HumanMessage(content="not an AI batch")]
    elif runtime_variant == "different-id":
        messages[0].tool_calls[0]["id"] = "other-control"
    elif runtime_variant == "different-name":
        messages[0].tool_calls[0]["name"] = "summarize_and_handback"
    elif runtime_variant == "different-args":
        messages[0].tool_calls[0]["args"]["summary"] = "OTHER"
    elif runtime_variant == "type-different-args":
        messages[0].tool_calls[0]["args"]["next_owner"] = False
    elif runtime_variant == "multiple":
        messages[0].tool_calls.append(
            {
                "name": "ordinary_probe",
                "args": {},
                "id": "ordinary-sibling",
            }
        )
    elif runtime_variant == "invalid-envelope":
        messages[0].additional_kwargs["tool_calls"] = [
            {
                "id": "selected-control",
                "type": "function",
                "function": {
                    "name": "handback_to_supervisor",
                    "arguments": "[]",
                },
            }
        ]
    request = SimpleNamespace(
        tool_call=selected,
        state={"messages": messages},
    )
    handled = []
    middleware = mod._TurnAuthorityToolMiddleware(
        model,
        agent_name="Mythic_Operator",
    )

    if async_wrapper:
        async def handler(bound_request):
            handled.append(bound_request.tool_call["id"])
            return "executed"

        result = asyncio.run(middleware.awrap_tool_call(request, handler))
    else:
        def handler(bound_request):
            handled.append(bound_request.tool_call["id"])
            return "executed"

        result = middleware.wrap_tool_call(request, handler)

    if should_execute:
        assert result == "executed"
        assert handled == ["selected-control"]
        assert recorded == [("Mythic_Operator", "SELECTED")]
    else:
        assert isinstance(result, ToolMessage)
        assert handled == []
        assert recorded == []


@pytest.mark.parametrize(
    ("args", "expected"),
    (
        (
            {
                "summary": " summary ",
                "final_response": "final",
                "progress_summary": "progress",
                "reason": "reason",
                "text": "text",
            },
            "summary",
        ),
        ({"summary": " ", "final_response": " final "}, "final"),
        ({"progress_summary": " progress "}, "progress"),
        ({"reason": " reason "}, "reason"),
        ({"text": " text "}, "text"),
        ({"summary": 7, "reason": ""}, ""),
    ),
)
def test_gate_n_selected_summary_field_priority_is_deterministic(args, expected):
    mod = _load_model_module()
    tool_call = {
        "name": "handback_to_supervisor",
        "args": args,
        "id": "selected",
    }
    request = SimpleNamespace(
        tool_call=tool_call,
        state={"messages": [AIMessage(content="", tool_calls=[tool_call])]},
    )

    assert (
        mod._TurnAuthorityToolMiddleware._selected_control_summary(request)
        == expected
    )


@pytest.mark.parametrize(
    ("identity_fields", "valid"),
    (
        ({"id": "same", "tool_use_id": "same"}, True),
        ({"id": "only-id"}, True),
        ({"tool_use_id": "only-tool-use-id"}, True),
        ({"id": "one", "tool_use_id": "two"}, False),
        ({"id": "", "tool_use_id": ""}, False),
        ({"id": " padded ", "tool_use_id": " padded "}, False),
        ({"id": 7, "tool_use_id": 7}, False),
        ({"id": "same", "tool_use_id": 7}, False),
    ),
)
def test_gate_n_content_identity_aliases_must_be_exact(identity_fields, valid):
    mod = _load_model_module()
    record, error = mod._provider_content_call_record(
        {
            "type": "tool_use",
            "name": "ordinary_probe",
            "input": {},
            **identity_fields,
        }
    )

    assert (error == "") is valid
    assert (record is not None) is valid


@pytest.mark.parametrize(
    "excluded_summaries",
    (
        (),
        ("SECOND",),
        ("SECOND", "THIRD", "FOURTH"),
    ),
)
def test_gate_n_actual_parent_command_records_only_post_arbitration_summary(
    excluded_summaries,
):
    from ai.langgraph.turn_authority import TurnAuthority
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    mod = _load_model_module()

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._active_delegations = {
        "Mythic_Operator": {
            "id": "delegation-1",
            "name": "Mythic_Operator",
        }
    }
    recorded = []
    original_recorder = model._record_delegation_final_summary

    def record(agent_name, summary):
        recorded.append((agent_name, summary))
        original_recorder(agent_name, summary)

    model._record_delegation_final_summary = record
    callback = mod.MessageCaptureCallback(
        "Mythic_Operator",
        delegation_id="delegation-1",
        delegation_name="Mythic_Operator",
    )
    calls = [{
        "name": "handback_to_supervisor",
        "args": {
            "reason": "first reason",
            "summary": "FIRST",
            "outcome": "progress",
            "next_owner": "",
        },
        "id": "first-handback",
    }]
    calls.extend(
        {
            "name": "handback_to_supervisor",
            "args": {
                "reason": f"excluded reason {index}",
                "summary": summary,
                "outcome": "progress",
                "next_owner": "",
            },
            "id": f"excluded-handback-{index}",
        }
        for index, summary in enumerate(excluded_summaries, start=2)
    )
    agent = create_agent(
        model=BoundFake(
            responses=[AIMessage(content="", tool_calls=calls)]
        ),
        tools=[mod._create_handback_to_supervisor_tool()],
        middleware=[
            mod._TurnAuthorityToolMiddleware(
                model,
                agent_name="Mythic_Operator",
            )
        ],
    )

    async def invoke():
        try:
            await agent.ainvoke(
                {
                    "messages": [HumanMessage(content="yield")],
                    "supervisor_messages": [],
                },
                {"callbacks": [callback]},
            )
        except BaseException as exc:
            return exc
        raise AssertionError("expected ParentCommand")

    error = asyncio.run(invoke())
    assert type(error).__name__ == "ParentCommand"
    command = getattr(error, "command", None)
    if command is None and getattr(error, "args", None):
        command = error.args[0]

    assert [call["id"] for call in callback.captured_messages[0].tool_calls] == [
        "first-handback"
    ]
    assert recorded == [("Mythic_Operator", "FIRST")]
    assert "Mythic_Operator" not in model._active_delegations
    assert len(command.update["messages"]) == 1
    assert "FIRST" in command.update["messages"][0].content
    assert all(
        summary not in command.update["messages"][0].content
        for summary in excluded_summaries
    )


def test_gate_n_actual_recursion_handback_records_only_selected_progress():
    from ai.langgraph.turn_authority import TurnAuthority
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    mod = _load_model_module()

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._active_delegations = {
        "Mythic_Operator": {
            "id": "delegation-1",
            "name": "Mythic_Operator",
        }
    }
    recorded = []
    model._record_delegation_final_summary = (
        lambda agent_name, summary: recorded.append((agent_name, summary))
    )
    callback = mod.MessageCaptureCallback(
        "Mythic_Operator",
        delegation_id="delegation-1",
        delegation_name="Mythic_Operator",
    )
    calls = [
        {
            "name": "summarize_and_handback",
            "args": {
                "progress_summary": "SELECTED PROGRESS",
                "tasks_remaining": "one task",
                "key_findings": "one finding",
            },
            "id": "first-recursion-handback",
        },
        {
            "name": "summarize_and_handback",
            "args": {
                "progress_summary": "EXCLUDED PROGRESS",
                "tasks_remaining": "other task",
                "key_findings": "other finding",
            },
            "id": "second-recursion-handback",
        },
    ]
    agent = create_agent(
        model=BoundFake(responses=[AIMessage(content="", tool_calls=calls)]),
        tools=[mod._create_summarize_handback_tool()],
        middleware=[
            mod._TurnAuthorityToolMiddleware(
                model,
                agent_name="Mythic_Operator",
            )
        ],
    )

    async def invoke():
        try:
            await agent.ainvoke(
                {
                    "messages": [HumanMessage(content="summarize")],
                    "supervisor_messages": [],
                },
                {"callbacks": [callback]},
            )
        except BaseException as exc:
            return exc
        raise AssertionError("expected ParentCommand")

    error = asyncio.run(invoke())
    assert type(error).__name__ == "ParentCommand"
    command = getattr(error, "command", None)
    if command is None and getattr(error, "args", None):
        command = error.args[0]

    assert [call["id"] for call in callback.captured_messages[0].tool_calls] == [
        "first-recursion-handback"
    ]
    assert recorded == [("Mythic_Operator", "SELECTED PROGRESS")]
    assert len(command.update["messages"]) == 1
    assert "SELECTED PROGRESS" in command.update["messages"][0].content
    assert "EXCLUDED PROGRESS" not in command.update["messages"][0].content


def test_gate_o_summary_uses_static_agent_owner_with_overlapping_cards():
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._active_delegations = {
        "Mythic_Operator": {
            "id": "mythic-card",
            "name": "Mythic_Operator",
            "final_summary": "UNCHANGED",
        },
        "BloodHound": {
            "id": "bloodhound-card",
            "name": "BloodHound",
            "final_summary": "",
        },
    }
    selected = {
        "name": "handback_to_supervisor",
        "args": {
            "reason": "yield",
            "summary": "BLOODHOUND SUMMARY",
            "outcome": "progress",
            "next_owner": "",
        },
        "id": "bloodhound-handback",
    }
    request = SimpleNamespace(
        tool_call=selected,
        state={"messages": [AIMessage(content="", tool_calls=[selected])]},
    )
    middleware = mod._TurnAuthorityToolMiddleware(
        model,
        agent_name="BloodHound",
    )

    assert middleware.wrap_tool_call(request, lambda _request: "executed") == "executed"
    assert (
        model._active_delegations["BloodHound"]["final_summary"]
        == "BLOODHOUND SUMMARY"
    )
    assert (
        model._active_delegations["Mythic_Operator"]["final_summary"]
        == "UNCHANGED"
    )

    unbound = mod._TurnAuthorityToolMiddleware(model)
    assert unbound.wrap_tool_call(request, lambda _request: "unbound") == "unbound"
    supervisor = mod._TurnAuthorityToolMiddleware(
        model,
        agent_name="Supervisor",
    )
    assert supervisor.wrap_tool_call(
        request,
        lambda _request: "supervisor",
    ) == "supervisor"
    assert (
        model._active_delegations["BloodHound"]["final_summary"]
        == "BLOODHOUND SUMMARY"
    )


def test_gate_o_actual_direct_transfer_closes_only_source_delegation():
    from ai.langgraph.turn_authority import TurnAuthority
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    mod = _load_model_module()

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._active_delegations = {
        "Mythic_Operator": {
            "id": "source-card",
            "name": "Mythic_Operator",
        },
        "BloodHound": {
            "id": "target-card",
            "name": "BloodHound",
        },
        "Execution": {
            "id": "execution-card",
            "name": "Execution",
        },
        "Collection": {
            "id": "collection-card",
            "name": "Collection",
        },
    }
    transfer_call = {
        "name": "transfer_to_BloodHound",
        "args": {
            "handoff_title": "Analyze graph",
            "handoff_instruction": "Analyze the current graph.",
        },
        "id": "direct-transfer",
    }
    agent = create_agent(
        model=BoundFake(
            responses=[AIMessage(content="", tool_calls=[transfer_call])]
        ),
        tools=[mod._create_handoff_tool(agent_name="BloodHound")],
        middleware=[
            mod._TurnAuthorityToolMiddleware(
                model,
                agent_name="Mythic_Operator",
            )
        ],
    )

    async def invoke():
        try:
            await agent.ainvoke(
                {
                    "messages": [HumanMessage(content="transfer")],
                    "supervisor_messages": [],
                    "bloodhound_messages": [],
                }
            )
        except BaseException as exc:
            return exc
        raise AssertionError("expected ParentCommand")

    error = asyncio.run(invoke())
    assert type(error).__name__ == "ParentCommand"
    assert set(model._active_delegations) == {
        "BloodHound",
        "Execution",
        "Collection",
    }
    assert model._active_delegations["BloodHound"]["id"] == "target-card"
    assert model._active_delegations["Execution"]["id"] == "execution-card"
    assert model._active_delegations["Collection"]["id"] == "collection-card"


def test_gate_o_direct_transfer_then_handback_preserves_selected_target_summary():
    from ai.langgraph.turn_authority import TurnAuthority
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    mod = _load_model_module()

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._active_delegations = {
        "Mythic_Operator": {
            "id": "source-card",
            "name": "Mythic_Operator",
            "final_summary": "",
        },
        "BloodHound": {
            "id": "target-card",
            "name": "BloodHound",
            "final_summary": "",
        },
    }
    closed = []

    async def close(agent_name, content="", status="finished"):
        delegation = model._active_delegations.pop(agent_name)
        closed.append(
            (
                agent_name,
                delegation.get("final_summary", ""),
                content,
                status,
            )
        )

    model._close_delegation = close
    transfer_call = {
        "name": "transfer_to_BloodHound",
        "args": {
            "handoff_title": "Analyze graph",
            "handoff_instruction": "Analyze the current graph.",
        },
        "id": "direct-transfer",
    }
    source_agent = create_agent(
        model=BoundFake(
            responses=[AIMessage(content="", tool_calls=[transfer_call])]
        ),
        tools=[mod._create_handoff_tool(agent_name="BloodHound")],
        middleware=[
            mod._TurnAuthorityToolMiddleware(
                model,
                agent_name="Mythic_Operator",
            )
        ],
    )

    async def invoke(agent, state):
        try:
            await agent.ainvoke(state)
        except BaseException as exc:
            return exc
        raise AssertionError("expected ParentCommand")

    transfer_error = asyncio.run(
        invoke(
            source_agent,
            {
                "messages": [HumanMessage(content="transfer")],
                "supervisor_messages": [],
                "bloodhound_messages": [],
            },
        )
    )
    assert type(transfer_error).__name__ == "ParentCommand"
    assert closed == [
        ("Mythic_Operator", "", "", "finished"),
    ]
    assert set(model._active_delegations) == {"BloodHound"}

    handback_call = {
        "name": "handback_to_supervisor",
        "args": {
            "reason": "analysis complete",
            "summary": "SELECTED BLOODHOUND SUMMARY",
            "outcome": "progress",
            "next_owner": "",
        },
        "id": "bloodhound-handback",
    }
    target_agent = create_agent(
        model=BoundFake(
            responses=[AIMessage(content="", tool_calls=[handback_call])]
        ),
        tools=[mod._create_handback_to_supervisor_tool()],
        middleware=[
            mod._TurnAuthorityToolMiddleware(
                model,
                agent_name="BloodHound",
            )
        ],
    )
    handback_error = asyncio.run(
        invoke(
            target_agent,
            {
                "messages": [HumanMessage(content="hand back")],
                "supervisor_messages": [],
            },
        )
    )

    assert type(handback_error).__name__ == "ParentCommand"
    assert closed == [
        ("Mythic_Operator", "", "", "finished"),
        (
            "BloodHound",
            "SELECTED BLOODHOUND SUMMARY",
            "",
            "finished",
        ),
    ]
    assert model._active_delegations == {}


def test_gate_o_async_control_cleanup_preserves_normal_return_and_error():
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._active_delegations = {
        "BloodHound": {
            "id": "bloodhound-card",
            "name": "BloodHound",
        }
    }
    closed = []

    async def close(agent_name):
        closed.append(agent_name)

    model._close_delegation = close
    selected = {
        "name": "handback_to_supervisor",
        "args": {
            "reason": "yield",
            "summary": "summary",
            "outcome": "progress",
            "next_owner": "",
        },
        "id": "selected-control",
    }
    request = SimpleNamespace(
        tool_call=selected,
        state={"messages": [AIMessage(content="", tool_calls=[selected])]},
    )
    middleware = mod._TurnAuthorityToolMiddleware(
        model,
        agent_name="BloodHound",
    )

    async def normal_handler(_request):
        return "ordinary return"

    assert asyncio.run(
        middleware.awrap_tool_call(request, normal_handler)
    ) == "ordinary return"
    assert closed == []

    async def failing_handler(_request):
        raise ValueError("ordinary failure")

    with pytest.raises(ValueError, match="ordinary failure"):
        asyncio.run(middleware.awrap_tool_call(request, failing_handler))
    assert closed == []

    async def parent_handler(_request):
        raise mod.ParentCommand(
            mod.Command(
                goto="Supervisor",
                graph=mod.Command.PARENT,
            )
        )

    with pytest.raises(mod.ParentCommand):
        asyncio.run(middleware.awrap_tool_call(request, parent_handler))
    assert closed == ["BloodHound"]


def test_gate_o_context_middleware_binds_exact_static_agent_owner():
    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model.mode = "auto"
    model._get_base_chat_model = lambda: None

    for agent_name in (
        "Generalist",
        "Mythic_Operator",
        "Mythic_Payload",
        "BloodHound",
        "MCP_Manager",
        "Sandbox",
        "Supervisor",
    ):
        middleware = model._context_middleware(agent_name=agent_name)
        authority = [
            item
            for item in middleware
            if isinstance(item, mod._TurnAuthorityToolMiddleware)
        ]
        assert len(authority) == 1
        assert authority[0]._agent_name == agent_name


def test_gate_l_actual_agent_shared_message_survives_orphan_input_cleanup():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()
    executed = []

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    @tool("transfer_to_Alpha")
    def transfer() -> str:
        """Perform one test control transition."""
        executed.append("control")
        return "controlled"

    @tool("ordinary_probe")
    def ordinary() -> str:
        """Perform one ordinary test action."""
        executed.append("ordinary")
        return "ordinary"

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    model._autonomous_solve = False
    model._message_seq = 1
    model.state = {"_message_seq": 1}
    model.llm = None
    agent = create_agent(
        model=BoundFake(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "ordinary_probe",
                            "args": {},
                            "id": "ordinary-call",
                            "type": "tool_call",
                        },
                        {
                            "name": "transfer_to_Alpha",
                            "args": {},
                            "id": "control-call",
                            "type": "tool_call",
                        },
                    ],
                ),
                AIMessage(content="done"),
            ]
        ),
        tools=[transfer, ordinary],
        middleware=[mod._TurnAuthorityToolMiddleware(model)],
    )
    state = {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [
            HumanMessage(content="go"),
            ToolMessage(
                content="orphan",
                name="old_tool",
                tool_call_id="missing-call",
            ),
        ],
        "mythic_operator_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }

    update = asyncio.run(
        model._wrap_create_agent(
            agent,
            "generalist_messages",
            "Generalist",
        )(state, {})
    )
    persisted = update["generalist_messages"]

    assert executed == ["control"]
    assert [type(message).__name__ for message in persisted] == [
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert [call["id"] for call in persisted[0].tool_calls] == ["control-call"]
    assert persisted[1].tool_call_id == "control-call"
    assert persisted[2].content == "done"


def test_gate_l_actual_parent_command_uses_only_in_place_selected_final():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from ai.langgraph.turn_authority import TurnAuthority

    mod = _load_model_module()

    class BoundFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    callback = mod.MessageCaptureCallback("Supervisor")
    agent = create_agent(
        model=BoundFake(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "respond_to_user",
                            "args": {"final_response": "first"},
                            "id": "first-final",
                            "type": "tool_call",
                        },
                        {
                            "name": "respond_to_user",
                            "args": {"final_response": "second"},
                            "id": "second-final",
                            "type": "tool_call",
                        },
                    ],
                )
            ]
        ),
        tools=[mod._create_respond_to_user_tool()],
        middleware=[mod._TurnAuthorityToolMiddleware(model)],
    )

    async def invoke():
        try:
            await agent.ainvoke(
                {
                    "messages": [HumanMessage(content="finish")],
                    "supervisor_messages": [],
                },
                {"callbacks": [callback]},
            )
        except BaseException as exc:
            return exc
        raise AssertionError("expected ParentCommand")

    error = asyncio.run(invoke())
    assert type(error).__name__ == "ParentCommand"
    command = getattr(error, "command", None)
    if command is None and getattr(error, "args", None):
        command = error.args[0]

    assert [call["id"] for call in callback.captured_messages[0].tool_calls] == [
        "first-final"
    ]
    assert [message.content for message in command.update["messages"]] == [
        "first"
    ]


def test_gate_l_callback_suppresses_start_card_for_control_excluded_sibling():
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    mod = _load_model_module()
    started = []

    async def emit_tool(**payload):
        started.append(payload)

    callback = mod.MessageCaptureCallback(
        "Mythic_Operator",
        tool_use_func=emit_tool,
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ordinary_probe",
                "args": {},
                "id": "ordinary-call",
                "type": "tool_call",
            },
            {
                "name": "transfer_to_Alpha",
                "args": {},
                "id": "control-call",
                "type": "tool_call",
            },
        ],
    )

    asyncio.run(
        callback.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=message)]]),
            run_id=uuid4(),
        )
    )

    assert callback.captured_messages == [message]
    assert [call["id"] for call in message.tool_calls] == [
        "ordinary-call",
        "control-call",
    ]
    assert started == []


@pytest.mark.parametrize(
    "calls",
    (
        (
            {"name": "ordinary_probe", "args": {}, "id": "dup"},
            {"name": "ordinary_probe", "args": {}, "id": "dup"},
        ),
        (
            {"name": "ordinary_probe", "args": {}, "id": ""},
            {"name": "ordinary_probe", "args": {}, "id": "other"},
        ),
    ),
)
def test_gate_l_callback_suppresses_start_cards_for_invalid_batch(calls):
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    mod = _load_model_module()
    started = []

    async def emit_tool(**payload):
        started.append(payload)

    callback = mod.MessageCaptureCallback(
        "Mythic_Operator",
        tool_use_func=emit_tool,
    )
    message = AIMessage(content="", tool_calls=list(calls))

    asyncio.run(
        callback.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=message)]]),
            run_id=uuid4(),
        )
    )

    assert callback.captured_messages == [message]
    assert started == []


def test_gate_l_same_request_stop_replacement_resets_canonical_subgoal():
    from ai.langgraph.request_contract import (
        StopCondition,
        build_request_contract,
    )
    from ai.langgraph.subgoal_state import (
        SubgoalState,
        assign_and_admit,
    )

    mod = _load_model_module()
    model = mod.Model.__new__(mod.Model)
    model._request_contract = None
    model._request_dynamic_proposals = False
    model._request_execution_digest = ""
    model._request_admitted_action_digests = set()
    model._active_approval_claim = None
    model.mythic_client = None
    model.state = _typed_subgoal_runtime_state("prior-request")
    contract = build_request_contract(
        request_id="native-request-stop-replacement",
        channel_id="7",
        operation_id="9",
        mode="supervised",
        autonomous_solve=False,
    )

    model.install_request_contract(contract)
    admitted = assign_and_admit(
        model._subgoal_authority,
        owner="Mythic_Operator",
        method="transfer_to_Mythic_Operator",
    )
    model._subgoal_authority = admitted
    model.state["_subgoal_state"] = admitted.to_dict()
    same_stop = contract.amend()
    model.install_request_contract(same_stop)
    assert model._subgoal_authority == admitted

    changed_value = same_stop.amend(
        stop_condition=StopCondition(
            kind=same_stop.stop_condition.kind,
            value="one-specific-effect",
        )
    )
    model.install_request_contract(changed_value)
    value_replaced = SubgoalState.from_dict(model.state["_subgoal_state"])
    assert value_replaced.stop_condition == "actions_complete"
    assert value_replaced.subgoal_id == admitted.subgoal_id
    assert value_replaced.owner == ""
    assert value_replaced.method == ""
    assert value_replaced.status.value == "proposed"
    assert value_replaced.admissions == ()
    assert value_replaced.transitions == ()

    model.install_request_contract(changed_value.stop())
    replaced = SubgoalState.from_dict(model.state["_subgoal_state"])
    assert replaced.request_id == contract.request_id
    assert replaced.stop_condition == "operator_stop"
    assert replaced.subgoal_id != value_replaced.subgoal_id
    assert replaced.admissions == ()
    assert replaced.status.value == "proposed"


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
