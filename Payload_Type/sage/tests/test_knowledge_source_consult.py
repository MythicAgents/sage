"""The attack graph is consulted, with a typed outcome, before an objective-shaped request is routed.

Supervised run, channel 3 (2026-10-30): "From your current foothold, obtain administrative control of essos"
sent all 30 delegations to the execution worker and never consulted the graph. The path-shape classifier
did not recognise "administrative control", and the bare name "essos" did not resolve to a domain.
"""

import asyncio

import pytest

from ai.langgraph import knowledge_sources as ks
from ai.langgraph.knowledge_sources import ConsultOutcome

CHANNEL_3_PROMPT = "From your current foothold, obtain administrative control of essos"
KNOWN = ["north.sevenkingdoms.local", "sevenkingdoms.local", "essos.local"]


@pytest.mark.parametrize("prompt", [
    CHANNEL_3_PROMPT,
    "obtain administrative control of essos.local",
    "take over essos",
    "become DA in essos.local",
    "get domain admin on essos.local",
    "find an attack path to essos.local",
    "escalate to enterprise admin",
])
def test_objective_shaped_requests_are_classified_as_objective(prompt):
    assert ks.classify_turn(prompt) == ks.TURN_OBJECTIVE


@pytest.mark.parametrize("prompt", [
    "list the callbacks",
    "what callbacks are alive?",
    "Check the results of that task and see if the intended result has happened.",
    "k, do it",
    "is there a way to continue on?",
    "do not escalate, just enumerate users",
    "summarize the findings so far",
])
def test_lookups_follow_ups_and_negated_asks_are_not_objectives(prompt):
    assert ks.classify_turn(prompt) == ks.TURN_OTHER


def test_bare_domain_label_resolves_only_against_observed_domains():
    assert ks.resolve_objective_domains(CHANNEL_3_PROMPT, KNOWN) == ["essos.local"]
    assert ks.resolve_objective_domains(CHANNEL_3_PROMPT, []) == []
    # Ambiguous first label: never guess.
    assert ks.resolve_objective_domains("take over corp", ["corp.local", "corp.example"]) == []
    assert ks.resolve_objective_domains("obtain administrative control of essos.local", []) == ["essos.local"]


def _consult(prompt, *, available=True, collected=(), predicates=(), known=(), fail=False):
    probed = []

    async def collected_domains():
        if fail:
            raise RuntimeError("mcp down")
        return list(collected)

    async def probe_paths(domains):
        probed.append(list(domains))
        return list(predicates)

    result = asyncio.run(ks.consult_attack_graph(
        prompt, available=available, collected_domains=collected_domains, probe_paths=probe_paths,
        other_known_domains=known,
    ))
    return result, probed


def test_channel_3_graph_without_the_objective_domain_is_partial():
    result, probed = _consult(
        CHANNEL_3_PROMPT, collected=["north.sevenkingdoms.local", "sevenkingdoms.local"], known=["essos.local"],
    )
    assert result.outcome == ConsultOutcome.CONSULTED_PARTIAL
    assert result.missing_domains == ["essos.local"]
    assert probed == []  # nothing covered to probe
    text = ks.render_consult(result)
    assert "consulted_partial" in text and "Not collected: essos.local" in text
    assert "by hand" in text  # a declined collection degrades to manual enumeration


def test_covered_objective_probes_the_path_and_reports_it():
    result, probed = _consult(
        "obtain administrative control of essos.local", collected=KNOWN,
        predicates=["objective-path-found:essos.local"],
    )
    assert result.outcome == ConsultOutcome.CONSULTED_OK
    assert probed == [["essos.local"]]
    assert result.path_found == ["essos.local"]
    assert "Path exists from controlled principals to: essos.local" in ks.render_consult(result)


def test_no_graph_is_absent_and_unreachable_bloodhound_is_unavailable():
    assert _consult(CHANNEL_3_PROMPT, collected=[])[0].outcome == ConsultOutcome.ABSENT
    assert _consult(CHANNEL_3_PROMPT, available=False)[0].outcome == ConsultOutcome.UNAVAILABLE
    assert _consult(CHANNEL_3_PROMPT, fail=True)[0].outcome == ConsultOutcome.UNAVAILABLE


def test_non_objective_turn_makes_no_graph_call_and_renders_nothing():
    calls = []

    async def collected_domains():
        calls.append("collected")
        return KNOWN

    async def probe_paths(domains):
        calls.append("probe")
        return []

    result = asyncio.run(ks.consult_attack_graph(
        "list the callbacks", available=True, collected_domains=collected_domains, probe_paths=probe_paths,
    ))
    assert result.outcome == ConsultOutcome.NOT_REQUIRED
    assert calls == []
    assert ks.render_consult(result) == ""


# ── the assembled path: real MythicTools consult, real Model hook ─────────────────────────


class _Tool:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self._handler(args)


def _bloodhound(monkeypatch, collected):
    import json

    from ai import mcp as mcp_module

    def domain_info(_args):
        rows = [{"name": d.upper(), "collected": True} for d in collected]
        return json.dumps({"data": rows})

    def cypher(args):
        query = args.get("query", "")
        if "shortestPath" in query:
            return json.dumps({"data": {"literals": [{"key": "name", "value": "ESSOS.LOCAL"}]}})
        return json.dumps({"data": {"literals": []}})

    tools = [_Tool("domain_info", domain_info), _Tool("cypher_query", cypher)]
    monkeypatch.setattr(mcp_module.MCPManager, "get_connected_servers", staticmethod(lambda: ["bloodhound"]))
    monkeypatch.setattr(mcp_module.MCPManager, "is_bloodhound_server", staticmethod(lambda s: s == "bloodhound"))
    monkeypatch.setattr(mcp_module.MCPManager, "get_tools_by_server", staticmethod(lambda s: tools))
    return tools


def _mythic_tools():
    from ai.langgraph.mythic_tools import MythicTools

    mt = object.__new__(MythicTools)
    mt._engagement_hops = []
    mt._engagement_graph_facts = []
    mt._engagement_footholds = []
    mt._eng_key = lambda: "eng-consult"
    return mt


def test_mythic_tools_consult_reports_channel_3_coverage_gap(monkeypatch):
    tools = _bloodhound(monkeypatch, ["north.sevenkingdoms.local", "sevenkingdoms.local"])
    mt = _mythic_tools()
    mt._engagement_graph_facts = [type("F", (), {"predicate": "trust-reachable:essos.local"})()]
    result = asyncio.run(mt._consult_attack_graph(CHANNEL_3_PROMPT))
    assert result.outcome == ConsultOutcome.CONSULTED_PARTIAL
    assert result.objective_domains == ["essos.local"] and result.missing_domains == ["essos.local"]
    assert not any("shortestPath" in str(c) for c in tools[1].calls)


def test_mythic_tools_consult_without_bloodhound_is_unavailable(monkeypatch):
    from ai import mcp as mcp_module

    monkeypatch.setattr(mcp_module.MCPManager, "get_connected_servers", staticmethod(lambda: []))
    result = asyncio.run(_mythic_tools()._consult_attack_graph(CHANNEL_3_PROMPT))
    assert result.outcome == ConsultOutcome.UNAVAILABLE


def _supervised_model(consult_fn):
    from ai.langgraph import model as graph_model
    from ai.langgraph.request_contract import build_request_contract
    from ai.langgraph.request_events import RequestEventLedger

    m = object.__new__(graph_model.Model)
    m.mythic_client = type("C", (), {"_consult_attack_graph": staticmethod(consult_fn)})()
    m._request_event_ledger = RequestEventLedger("chat:3:request:1")
    contract = build_request_contract(
        request_id="chat:3:request:1", channel_id="3", operation_id="op", mode="supervised",
        autonomous_solve=False,
    )
    return m, contract


def test_supervised_objective_turn_gets_a_hidden_consult_message_and_event(monkeypatch):
    tools = _bloodhound(monkeypatch, ["north.sevenkingdoms.local", "sevenkingdoms.local"])
    mt = _mythic_tools()
    mt._engagement_graph_facts = [type("F", (), {"predicate": "trust-reachable:essos.local"})()]
    m, contract = _supervised_model(mt._consult_attack_graph)

    msg = asyncio.run(m._attack_graph_consult_message(CHANNEL_3_PROMPT, contract))
    assert msg is not None
    assert msg.additional_kwargs["_hide_from_stream"] is True
    assert msg.additional_kwargs["_knowledge_source_consult"]["outcome"] == "consulted_partial"
    assert "Not collected: essos.local" in msg.content
    events = [e for e in m._request_event_ledger._events if e.kind == "knowledge_consult"]
    assert len(events) == 1 and events[0].phase == "consulted_partial"
    assert tools[0].calls  # the graph was actually read


def test_non_objective_and_auto_turns_get_no_consult():
    from ai.langgraph.request_contract import build_request_contract

    calls = []

    async def consult(prompt):
        calls.append(prompt)
        raise AssertionError("must not be called")

    m, contract = _supervised_model(consult)
    assert asyncio.run(m._attack_graph_consult_message("list the callbacks", contract)) is None
    auto = build_request_contract(
        request_id="chat:3:request:2", channel_id="3", operation_id="op", mode="auto", autonomous_solve=True,
    )
    assert asyncio.run(m._attack_graph_consult_message(CHANNEL_3_PROMPT, auto)) is None
    assert calls == []


def test_consult_failure_fails_open_as_unavailable():
    async def consult(prompt):
        raise RuntimeError("bloodhound exploded")

    m, contract = _supervised_model(consult)
    msg = asyncio.run(m._attack_graph_consult_message(CHANNEL_3_PROMPT, contract))
    assert msg.additional_kwargs["_knowledge_source_consult"]["outcome"] == "unavailable"


def test_invoke_puts_the_consult_before_the_operator_message_in_the_supervisors_input():
    """The graph's first input already carries the consult, ahead of the operator's request."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_operator_ledger_view import _inventory_model

    m, streamed = _inventory_model(31)

    async def consult(prompt):
        return ks.AttackGraphConsult(ks.ConsultOutcome.ABSENT, objective_domains=["essos.local"])

    m.mythic_client._consult_attack_graph = consult
    m._refresh_graph_for_turn = lambda *a, **k: None
    m.verbose = False
    captured = []

    class _StopGraph(Exception):
        pass

    class _Graph:
        def astream(self, state, *args, **kwargs):
            captured.append(list(state["supervisor_messages"]))
            raise _StopGraph()

    m.graph = _Graph()
    try:
        asyncio.run(m.invoke(CHANNEL_3_PROMPT, is_interactive=True))
    except Exception:
        pass  # only the graph's input matters here
    assert captured, "the graph never ran"
    humans = [msg for msg in captured[0] if type(msg).__name__ == "HumanMessage"]
    assert [msg.additional_kwargs.get("_synthetic_nudge") for msg in humans[-2:]] == [
        "knowledge_source_consult", None,
    ]
    assert humans[-1].content == CHANNEL_3_PROMPT
    assert "Outcome: absent" in humans[-2].content
    assert not any("knowledge_source_consult" in str(text) for text in streamed)
