"""Structural brief for supervised delegations: checked before dispatch, never judged on wording.

Unit cases exercise the validator against what Sage already holds; assembled cases drive the real graph through
the scripted harness (test_scripted_handoff.py) so the gate is proven at the handoff tool, not in isolation.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage, HumanMessage
import pytest

_HARNESS_PATH = Path(__file__).resolve().with_name("test_scripted_handoff.py")
_SPEC = importlib.util.spec_from_file_location("scripted_handoff_harness_brief", _HARNESS_PATH)
_harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_harness)

from ai.langgraph import handoff_brief as hb  # noqa: E402
from ai.langgraph import model as model_module  # noqa: E402
from ai.langgraph.request_contract import build_request_contract  # noqa: E402

PROMPT = "Enumerate local administrators on meereen.essos.local from callback 3"
TASK_TOOL = ["issue_task_and_waitfor_task_output"]
CALLBACKS = [{"id": 3, "host": "MEEREEN", "user": "ESSOS\\daenerys"}]


def _footholds() -> list[Any]:
    return [SimpleNamespace(callback_id="3", host="MEEREEN", forest="essos.local", identity="ESSOS\\daenerys")]


def _client(**extra: Any) -> SimpleNamespace:
    values = {"_recon_snapshot_cache": {("list_callbacks", "all"): CALLBACKS}, "_engagement_footholds": _footholds()}
    values.update(extra)
    return SimpleNamespace(**values)


def _scope(operator_input: str = PROMPT, bound: Any = "", **extra: Any) -> hb.KnownScope:
    return hb.known_scope(_client(**extra), operator_input=operator_input, bound_callback=bound)


def _brief(**fields: Any) -> hb.HandoffBrief:
    base = {"callback_display_id": 3, "targets": ["meereen.essos.local"], "expected_result": "Local admin list."}
    if "allowed_capabilities" not in fields:
        base["allowed_tools"] = TASK_TOOL
    base.update(fields)
    return hb.HandoffBrief.from_args(**base)


def _errors(brief: hb.HandoffBrief, scope: hb.KnownScope | None = None, worker: str = "Mythic_Operator",
            require_scope: bool = True) -> tuple[str, ...]:
    return hb.validate_brief(brief, worker=worker, require_scope=require_scope, scope=scope or _scope())


# --- validator -------------------------------------------------------------------------------------------------

def test_a_complete_consistent_brief_passes():
    assert _errors(_brief()) == ()


@pytest.mark.parametrize("missing", ["callback_display_id", "targets", "expected_result"])
def test_each_missing_required_field_is_named(missing):
    errors = _errors(_brief(**{missing: None}))
    assert len(errors) == 1 and missing in errors[0]


def test_a_control_plane_step_needs_only_an_expected_result():
    assert _errors(hb.HandoffBrief.from_args(allowed_capabilities=["control_plane"], expected_result="List.")) == ()
    errors = _errors(hb.HandoffBrief.from_args(allowed_capabilities=["control_plane"]))
    assert errors and "expected_result" in errors[0] and "callback_display_id" not in errors[0]


def test_control_plane_cannot_be_mixed_with_target_facing_classes():
    errors = _errors(_brief(allowed_capabilities=["control_plane", "collect-graph"]))
    assert any("cannot be combined" in error for error in errors)


@pytest.mark.parametrize("capabilities, allowed, recorded", [
    (["collect-graph"], ("collect-graph",), ()),
    (["off_catalog"], ("off_catalog",), ("off_catalog",)),
    (["collect-graph", "off_catalog"], ("collect-graph", "off_catalog"), ("off_catalog",)),
    (["shell"], ("off_catalog",), ("shell",)),                      # a command name is off-catalog work
    (["collect-graphs", "whoami"], ("off_catalog",), ("collect-graphs", "whoami")),
])
def test_names_outside_the_catalog_become_off_catalog_and_are_kept(capabilities, allowed, recorded):
    brief = _brief(allowed_capabilities=capabilities)
    assert brief.allowed_capabilities == allowed
    assert brief.off_catalog_names == recorded
    assert _errors(brief) == ()


def test_capability_choices_follow_the_catalog(monkeypatch):
    from ai.langgraph import capabilities

    monkeypatch.setattr(capabilities, "_CAPABILITY_CATALOG", (("new-procedure", "A newly cataloged procedure."),))
    assert hb.capability_choices() == ("new-procedure", "off_catalog", "control_plane")
    assert _brief(allowed_capabilities=["new-procedure"]).allowed_capabilities == ("new-procedure",)
    tool = model_module._create_handoff_tool(agent_name="Mythic_Operator", brief_check=lambda *args: None)
    schema = tool.tool_call_schema.model_json_schema()["properties"]["allowed_capabilities"]
    items = next(branch["items"] for branch in schema["anyOf"] if branch.get("type") == "array")
    assert items["enum"] == ["new-procedure", "off_catalog", "control_plane"]


@pytest.mark.parametrize("target, ok", [
    ("meereen.essos.local", True),       # named in the request and observed
    ("MEEREEN", True),                   # observed callback host
    ("essos", True),                     # bare label resolving to one observed domain
    ("essos.local", True),               # observed forest
    ("ESSOS\\daenerys", True),           # observed identity
    ("daenerys@essos.local", True),      # same principal, other form
    ("meereen.essos.locals", False),     # one character off a known name
    ("braavos.essos.local", False),      # neither requested nor observed
    ("sevenkingdoms.local", False),
    ("ESSOS\\viserys", False),
])
def test_targets_must_be_requested_or_observed(target, ok):
    assert (_errors(_brief(targets=[target])) == ()) is ok


def test_a_target_named_only_in_the_request_passes_without_engagement_state():
    scope = hb.known_scope(SimpleNamespace(_recon_snapshot_cache={("list_callbacks", "all"): CALLBACKS}),
                           operator_input="Check shares on braavos.essos.local.")
    assert _errors(_brief(targets=["braavos.essos.local"]), scope) == ()
    assert _errors(_brief(targets=["braavos.essos.loca"]), scope) != ()


@pytest.mark.parametrize("callback, bound, ok", [
    ("1", 1, True),          # string vs int of the bound callback
    (1, "1", True),
    ("#1", "1", True),
    (2, "1", False),         # a different callback than the request is bound to
])
def test_the_bound_callback_is_the_only_admissible_callback(callback, bound, ok):
    scope = hb.known_scope(_client(_recon_snapshot_cache={("list_callbacks", "all"): [{"id": 1}, {"id": 2}]}),
                           operator_input=PROMPT, bound_callback=bound)
    assert (_errors(_brief(callback_display_id=callback), scope) == ()) is ok


def test_an_unobserved_callback_is_rejected_and_points_to_a_control_plane_read():
    errors = _errors(_brief(callback_display_id=9))
    assert len(errors) == 1 and "control_plane" in errors[0]
    empty = hb.known_scope(SimpleNamespace(), operator_input=PROMPT)
    assert _errors(_brief(), empty)


@pytest.mark.parametrize("reference, ok", [
    ("task:47", True),
    ("cred:12", True),
    ("@cred:12", True),
    ("task:48", False),
    ("cred:13", False),
    ("task 47", False),
    ("output of the last task", False),
])
def test_references_resolve_only_against_records_sage_holds(reference, ok):
    scope = _scope(_task_source_request_ids={47: "chat:1:request:1"}, _cred_cache=[{"id": 12}])
    assert (_errors(_brief(inputs_by_reference=[reference]), scope) == ()) is ok


def test_a_task_the_operator_names_is_a_resolvable_reference():
    scope = _scope(operator_input=PROMPT + " using the output of task 51")
    assert _errors(_brief(inputs_by_reference=["task:51"]), scope) == ()


@pytest.mark.parametrize("worker", ["Generalist", "BloodHound", "Mythic_Payload", "MCP_Manager"])
def test_other_workers_and_unscoped_lanes_need_no_fields(worker):
    empty = hb.HandoffBrief()
    assert _errors(empty, worker=worker) == ()
    assert _errors(empty, require_scope=False) == ()


def test_fields_given_to_other_workers_are_still_checked():
    assert _errors(_brief(targets=["braavos.essos.local"]), worker="BloodHound")


def test_restatement_is_measured_not_judged():
    assert hb.restatement("Objective: " + PROMPT, PROMPT) > 0.8
    assert hb.restatement("Summarize the stored output.", PROMPT) == 0.0
    assert _errors(_brief()) == ()  # verbatim restatement does not enter validation


def test_rejection_count_is_consecutive():
    events = [SimpleNamespace(phase=phase) for phase in ("rejected", "accepted", "rejected", "rejected")]
    assert hb.rejection_count(events) == 2
    assert hb.rejection_count(events[:2]) == 0


# --- assembled graph -------------------------------------------------------------------------------------------

def _handoff(args: dict, call_id: str = "call_handoff_1", worker: str = "Mythic_Operator") -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": f"transfer_to_{worker}", "args": args, "id": call_id, "type": "tool_call"}])


def _run(script: list[AIMessage], *, prompt: str = PROMPT, mode: str = "supervised", recursion_limit: int = 12):
    harness = _harness._Harness(script)
    original_build = harness._build

    def build(script):
        model = original_build(script)
        model.mythic_client._recon_snapshot_cache = {("list_callbacks", "all"): CALLBACKS}
        model.mythic_client._engagement_footholds = _footholds()
        return model

    harness._build = build
    original_run = harness.run

    async def run(prompt_text: str, *, assisted: bool = False, stream: bool = False):
        if mode == "supervised":
            return await original_run(prompt_text, assisted=True)
        real_build = _harness.build_request_contract
        _harness.build_request_contract = lambda **kw: real_build(**{**kw, "mode": mode})
        try:
            return await original_run(prompt_text, assisted=True)
        finally:
            _harness.build_request_contract = real_build

    limit = _harness._RECURSION_LIMIT
    _harness._RECURSION_LIMIT = recursion_limit
    try:
        result = asyncio.run(run(prompt))
    finally:
        _harness._RECURSION_LIMIT = limit
    return harness, result


def _delegated(result: dict, channel: str = "mythic_operator_messages") -> list[HumanMessage]:
    return [m for m in result.get(channel, []) if isinstance(m, HumanMessage) and m.additional_kwargs.get("_delegated_to")]


def _brief_events(harness) -> list[Any]:
    return list(harness.model._request_event_ledger.actual_events(kind="brief"))


def test_complete_brief_dispatches_the_exact_operator_input_with_the_brief_attached():
    args = {"handoff_title": "Enumerate admins", "handoff_instruction": "Objective: " + PROMPT,
            "callback_display_id": "3", "targets": ["meereen.essos.local"], "expected_result": "Admin list.", "allowed_tools": TASK_TOOL}
    harness, result = _run([_handoff(args), AIMessage(content="worker done"), AIMessage(content="final")])

    delegated = _delegated(result)
    assert [m.content for m in delegated] == [PROMPT]
    assert delegated[0].additional_kwargs["_handoff_brief"]["callback_display_id"] == "3"
    assert [e.phase for e in _brief_events(harness)] == ["accepted"]
    assert float(dict(_brief_events(harness)[0].metadata)["restatement"]) > 0.8


@pytest.mark.parametrize("args", [
    {"handoff_title": "Enumerate admins"},
    {"handoff_title": "Enumerate admins", "callback_display_id": 3, "targets": ["braavos.essos.local"],
     "expected_result": "Admin list.", "allowed_tools": TASK_TOOL},
    {"handoff_title": "Enumerate admins", "callback_display_id": 4, "targets": ["meereen.essos.local"],
     "expected_result": "Admin list.", "allowed_tools": TASK_TOOL},
    {"handoff_title": "Enumerate admins", "callback_display_id": 3, "targets": ["meereen.essos.local"],
     "expected_result": "Admin list.", "allowed_tools": TASK_TOOL, "inputs_by_reference": ["task:99"]},
], ids=["no-scope", "unrequested-target", "unobserved-callback", "unresolved-reference"])
def test_inadmissible_brief_reaches_no_worker_and_tells_the_supervisor_why(args):
    harness, result = _run([_handoff(args), AIMessage(content="final")])

    assert _delegated(result) == []
    supervisor_view = [str(m.content) for m in result.get("supervisor_messages", [])]
    assert any("was not dispatched" in text for text in supervisor_view)
    assert [e.phase for e in _brief_events(harness)] == ["rejected"]


def test_two_rejections_end_the_step_and_tell_the_operator():
    bad = {"handoff_title": "Enumerate admins"}
    script = [_handoff(bad, "first"), _handoff(bad, "second"), _handoff(bad, "third"), AIMessage(content="never")]
    harness, result = _run(script)

    assert _delegated(result) == []
    assert [e.phase for e in _brief_events(harness)] == ["rejected", "rejected"]
    final = [m for m in result.get("supervisor_messages", []) if m.additional_kwargs.get("_is_final_report")]
    assert final and "could not form an admissible delegation" in str(final[-1].content)
    assert "callback_display_id" in str(final[-1].content)


def test_a_corrected_brief_after_one_rejection_dispatches():
    good = {"handoff_title": "Enumerate admins", "callback_display_id": 3, "targets": ["meereen.essos.local"],
            "expected_result": "Admin list.", "allowed_tools": TASK_TOOL}
    script = [_handoff({"handoff_title": "Enumerate admins"}, "first"), _handoff(good, "second"),
              AIMessage(content="worker done"), AIMessage(content="final")]
    harness, result = _run(script)

    assert [m.content for m in _delegated(result)] == [PROMPT]
    assert [e.phase for e in _brief_events(harness)] == ["rejected", "accepted"]


def test_other_workers_dispatch_without_fields():
    harness, result = _run([_handoff({"handoff_title": "Explain"}, worker="Generalist"),
                            AIMessage(content="worker done"), AIMessage(content="final")])
    assert [m.content for m in _delegated(result, "generalist_messages")] == [PROMPT]


def test_conversation_lane_dispatches_operator_without_fields():
    prompt = "Which callbacks are alive?"
    harness, result = _run([_handoff({"handoff_title": "Check callbacks"}),
                            AIMessage(content="worker done"), AIMessage(content="final")],
                           prompt=prompt, mode="conversation")
    assert [m.content for m in _delegated(result)] == [prompt]


@pytest.mark.parametrize("mode, gated", [("auto", False), ("supervised", True), ("conversation", True)])
def test_autonomous_handoff_tools_keep_their_schema(monkeypatch, mode, gated):
    seen: dict[str, Any] = {}
    original = model_module._create_handoff_tool

    def spy(**kwargs):
        tool = original(**kwargs)
        seen[kwargs["agent_name"]] = (kwargs.get("brief_check") is not None, set(tool.args))
        return tool

    monkeypatch.setattr(model_module, "_create_handoff_tool", spy)

    async def build():
        model = model_module.Model(
            provider="openai", model="gpt-4o-mini", system_prompt="brief schema", task_id=0,
            agent_task_id="brief-schema", channel_id=1, mode=mode,
            config={"configurable": {"api_key": "sk-not-a-real-key", "API_ENDPOINT": _harness._DEAD_ENDPOINT}},
        )
        model.mythic_client = MagicMock()
        model.mythic_client.get_payload_names = AsyncMock(return_value=[])
        model._payload_names = []
        model._rebuild_graph()

    asyncio.run(build())
    has_check, args = seen["Mythic_Operator"]
    assert has_check is gated
    assert ("targets" in args) is gated


@pytest.mark.parametrize("lists_first", [True, False], ids=["lists-first", "no-listing"])
def test_supervisor_lists_callbacks_itself_then_briefs_from_what_it_saw(monkeypatch, lists_first):
    """The Supervisor's own read-only listing is what makes the brief's callback known; nothing else seeds it."""
    from ai.langgraph import mythic_tools

    async def query(_client, _query, *args, **kwargs):
        return {"callback": [{"display_id": 3, "host": "MEEREEN", "user": "daenerys", "integrity_level": 2,
                              "last_checkin": None, "payload": {"payloadtype": {"name": "test-agent"}},
                              "c2profileparametersinstances": []}]}

    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", query)
    reader = mythic_tools.MythicTools(agent_task_id="brief-listing", operation_id=1, channel_id=1)
    reader.client = object()
    listing = AIMessage(content="", tool_calls=[{"name": "list_callbacks", "args": {}, "id": "list", "type": "tool_call"}])
    brief = {"handoff_title": "Enumerate admins", "callback_display_id": 3, "targets": ["MEEREEN"],
             "expected_result": "Admin list.", "allowed_tools": TASK_TOOL}
    script = [listing] if lists_first else []
    script += [_handoff(brief), AIMessage(content="worker done"), AIMessage(content="final")]
    harness = _harness._Harness(script)
    original_build = harness._build

    def build(script):
        model = original_build(script)
        model.mythic_client.get_tools.side_effect = (
            lambda names: reader.get_tools(names) if names == ["list_callbacks"] else [])
        model.mythic_client._recon_snapshot_cache = reader._recon_snapshot_cache
        return model

    harness._build = build
    limit = _harness._RECURSION_LIMIT
    _harness._RECURSION_LIMIT = 12
    try:
        result = asyncio.run(harness.run(PROMPT, assisted=True))
    finally:
        _harness._RECURSION_LIMIT = limit

    phases = [e.phase for e in _brief_events(harness)]
    if lists_first:
        assert reader._recon_snapshot_cache[("list_callbacks", "all")][0]["id"] == 3
        assert phases == ["accepted"] and [m.content for m in _delegated(result)] == [PROMPT]
    else:
        assert phases[0] == "rejected" and _delegated(result) == []


@pytest.mark.parametrize("mode, listed", [("auto", False), ("supervised", True), ("conversation", True)])
def test_only_assisted_supervisors_get_the_callback_listing(monkeypatch, mode, listed):
    from langchain_core.tools import tool

    @tool
    def list_callbacks() -> str:
        """Test stand-in for the read-only callback listing."""
        return "[]"

    captured: dict[str, Any] = {}
    original = model_module.create_agent

    def spy(*args, **kwargs):
        captured.setdefault(kwargs.get("name"), [getattr(t, "name", "") for t in kwargs.get("tools", [])])
        return original(*args, **kwargs)

    monkeypatch.setattr(model_module, "create_agent", spy)

    async def build():
        model = model_module.Model(
            provider="openai", model="gpt-4o-mini", system_prompt="listing", task_id=0,
            agent_task_id="listing", channel_id=1, mode=mode,
            config={"configurable": {"api_key": "sk-not-a-real-key", "API_ENDPOINT": _harness._DEAD_ENDPOINT}},
        )
        model.mythic_client = MagicMock()
        model.mythic_client.get_payload_names = AsyncMock(return_value=[])
        model.mythic_client.get_tools.side_effect = lambda names: [list_callbacks] if names == ["list_callbacks"] else []
        model._payload_names = []
        model._rebuild_graph()

    asyncio.run(build())
    assert ("list_callbacks" in captured["Supervisor"]) is listed


def test_an_off_catalog_delegation_is_recorded_for_retrospectives():
    args = {"handoff_title": "Enumerate admins", "handoff_instruction": "List local administrators.",
            "callback_display_id": 3, "targets": ["meereen.essos.local"], "expected_result": "Admin list.",
            "allowed_capabilities": ["off_catalog"]}
    harness, result = _run([_handoff(args), AIMessage(content="worker done"), AIMessage(content="final")])

    assert [m.content for m in _delegated(result)] == [PROMPT]
    records = harness.model._request_event_ledger.actual_events(kind="off_catalog")
    assert len(records) == 1 and records[0].content == "Admin list."
    metadata = dict(records[0].metadata)
    assert metadata["title"] == "Enumerate admins" and metadata["assignment"] == "List local administrators."
    assert metadata["worker"] == "Mythic_Operator"


def test_a_catalog_delegation_is_not_recorded_as_off_catalog():
    args = {"handoff_title": "Collect graph", "callback_display_id": 3, "targets": ["meereen.essos.local"],
            "expected_result": "Graph collected.", "allowed_capabilities": ["collect-graph"]}
    harness, _result = _run([_handoff(args), AIMessage(content="worker done"), AIMessage(content="final")])
    assert harness.model._request_event_ledger.actual_events(kind="off_catalog") == ()


def test_a_name_outside_the_offered_list_is_refused_by_the_schema_with_the_choices():
    args = {"handoff_title": "Enumerate admins", "callback_display_id": 3, "targets": ["meereen.essos.local"],
            "expected_result": "Admin list.", "allowed_capabilities": ["shell"]}
    harness, result = _run([_handoff(args), AIMessage(content="final")])

    assert _delegated(result) == []
    errors = [str(m.content) for m in result.get("supervisor_messages", []) if type(m).__name__ == "ToolMessage"]
    assert any("off_catalog" in text and "collect-graph" in text for text in errors)
