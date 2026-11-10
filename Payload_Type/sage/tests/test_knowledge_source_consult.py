"""The attack graph is consulted, with a typed outcome, before an objective-shaped request is routed.

Supervised run, channel 3 (2026-10-30): "From your current foothold, obtain administrative control of essos"
sent all 30 delegations to the execution worker and never consulted the graph. The path-shape classifier
did not recognise "administrative control", and the bare name "essos" did not resolve to a domain.
"""

from tests.esl_fixtures import seed_ledger  # noqa: E402
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
    seed_ledger(mt, hops=[])
    seed_ledger(mt, graph_facts=[])
    mt._engagement_footholds = []
    mt._eng_key = lambda: "eng-consult"
    return mt


def test_mythic_tools_consult_reports_channel_3_coverage_gap(monkeypatch):
    tools = _bloodhound(monkeypatch, ["north.sevenkingdoms.local", "sevenkingdoms.local"])
    mt = _mythic_tools()
    seed_ledger(mt, graph_facts=[type("F", (), {"predicate": "trust-reachable:essos.local"})()])
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
    seed_ledger(mt, graph_facts=[type("F", (), {"predicate": "trust-reachable:essos.local"})()])
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


# ── partial coverage arms the first collection for one missing objective domain ──────────


def test_a_partial_consult_names_collect_graph_for_its_first_missing_domain():
    result, _ = _consult("take over west.test and take over south.test", collected=["north.test"])
    assert result.outcome == ConsultOutcome.CONSULTED_PARTIAL
    assert ks.first_missing_domain(result) == "south.test"
    text = ks.render_consult(result)
    assert "Not collected: south.test, west.test" in text
    assert "`collect-graph` on the observed callback, scoped to south.test" in text
    assert "by hand" in text


def test_mixed_coverage_keeps_the_covered_path_result_beside_the_missing_domain_proposal():
    result, probed = _consult("take over north.test and take over south.test", collected=["north.test"],
                              predicates=["objective-path-found:north.test"])
    assert probed == [["north.test"]] and result.path_found == ["north.test"]
    assert result.missing_domains == ["south.test"]
    text = ks.render_consult(result)
    assert "Path exists from controlled principals to: north.test" in text
    assert "scoped to south.test" in text


@pytest.mark.parametrize("missing, expected", [
    (["West.Test.", " south.test "], "south.test"),
    ([], ""),
    (["", "  "], ""),
])
def test_the_first_missing_domain_is_normalized_and_sorted(missing, expected):
    consult = ks.AttackGraphConsult(ConsultOutcome.CONSULTED_PARTIAL, missing_domains=missing)
    assert ks.first_missing_domain(consult) == expected


def test_covering_and_unavailable_consults_name_no_first_collection():
    covered, _ = _consult("take over north.test", collected=["north.test"])
    assert "Available first procedure" not in ks.render_consult(covered)
    assert "Available first procedure" not in ks.render_consult(ks.AttackGraphConsult(ConsultOutcome.UNAVAILABLE))


def _armed(consult, *, mode="supervised", request_id="chat:3:request:1"):
    from ai.langgraph.request_contract import build_request_contract

    async def consult_fn(prompt):
        return consult

    m, _ = _supervised_model(consult_fn)
    contract = build_request_contract(request_id=request_id, channel_id="3", operation_id="op", mode=mode,
                                      autonomous_solve=False)
    asyncio.run(m._attack_graph_consult_message(CHANNEL_3_PROMPT, contract))
    return m._graph_first_step


def test_partial_supervised_consults_arm_one_missing_domain_and_other_outcomes_do_not():
    partial = ks.AttackGraphConsult(ConsultOutcome.CONSULTED_PARTIAL, objective_domains=["essos.local", "west.test"],
                                    collected_domains=["north.test"], missing_domains=["west.test", "essos.local"])
    assert _armed(partial) == {"request_id": "chat:3:request:1", "partial": True, "domains": ["essos.local"],
                               "done": False}
    absent = ks.AttackGraphConsult(ConsultOutcome.ABSENT, objective_domains=["essos.local"])
    assert _armed(absent) == {"request_id": "chat:3:request:1", "partial": False, "domains": ["essos.local"],
                              "done": False}
    assert _armed(partial, mode="conversation") is None
    for outcome in (ConsultOutcome.CONSULTED_OK, ConsultOutcome.UNAVAILABLE):
        assert _armed(ks.AttackGraphConsult(outcome, objective_domains=["essos.local"])) is None
    # A partial outcome with nothing missing has nothing to collect.
    assert _armed(ks.AttackGraphConsult(ConsultOutcome.CONSULTED_PARTIAL, collected_domains=["north.test"])) is None


# --- an explicit supported collection request: a bounded, whole-string grammar ------------------------------------
#
# A live supervised request (2026-11-10), "Collect BloodHound data for north.sevenkingdoms.local through the
# current foothold; collection only.", reached the worker as a raw staging recipe. The parser recognizes only this
# deliberately bounded request language; anything else keeps ordinary routing.

REPRODUCED_COLLECTION = ("Collect BloodHound data for north.sevenkingdoms.local through the current foothold; "
                         "collection only.")


@pytest.mark.parametrize("prompt, domain, callback", [
    (REPRODUCED_COLLECTION, "north.sevenkingdoms.local", ""),
    ("collect bloodhound data for corp.example.test", "corp.example.test", ""),
    ("Recollect BloodHound graph data from corp.example.test.", "corp.example.test", ""),
    ("re-collect BloodHound graph in CORP.Example.Test", "corp.example.test", ""),
    ("Gather BloodHound collection for corp.example.test!", "corp.example.test", ""),
    ("Collect BloodHound data for corp.example.test.", "corp.example.test", ""),
    ("Please collect BloodHound data for corp.example.test through callback 7", "corp.example.test", "7"),
    ("Can you run SharpHound against corp.example.test on callback 12?", "corp.example.test", "12"),
    ("could you please rerun SharpHound for corp.example.test using callback 3, collection only",
     "corp.example.test", "3"),
    ("Can you please re-run SharpHound on corp.example.test through the current foothold.", "corp.example.test", ""),
    ("Please execute SharpHound for corp.example.test", "corp.example.test", ""),
    ("  Launch   SharpHound\tfor  corp.example.test  ", "corp.example.test", ""),
])
def test_explicit_collection_requests_parse_to_one_domain_and_an_optional_callback(prompt, domain, callback):
    parsed = ks.explicit_collection_request(prompt)
    assert parsed == ks.ExplicitCollectionRequest(domain=domain, callback_id=callback)
    assert parsed.source == "explicit_collection"
    # Never objective-shaped, so a direct request and an objective consult cannot both claim one request.
    assert ks.classify_turn(prompt) == ks.TURN_OTHER


@pytest.mark.parametrize("prompt", [
    "",
    "collect bloodhound data",
    "How do I collect BloodHound data for corp.example.test?",
    "Can you collect BloodHound data for corp.example.test, or should I?",
    '"Collect BloodHound data for corp.example.test"',
    "`Run SharpHound against corp.example.test`",
    "Don't collect BloodHound data for corp.example.test.",
    "Do not run SharpHound against corp.example.test",
    "If the graph is empty, collect BloodHound data for corp.example.test.",
    "Collect BloodHound data for corp.example.test if needed",
    "Collect BloodHound data for corp.example.test and south.example.test",
    "Collect BloodHound data for corp.example.test, south.example.test",
    "Collect BloodHound data for corp.example.test, then obtain administrative control of it",
    "Collect BloodHound data for corp.example.test and kerberoast",
    "Obtain administrative control of corp.example.test",
    "Collect AzureHound data for corp.example.test",
    "Collect BloodHound data for the corp.example.test Azure tenant",
    "Run RustHound against corp.example.test",
    "Run SharpHound against corp.example.test with -c DCOnly",
    "Run SharpHound against corp.example.test --CollectionMethods Session",
    "Collect BloodHound session data for corp.example.test",
    "Collect BloodHound data for corp.example.test and save it to C:\\Temp",
    "Collect BloodHound data for corp.example.test using execute_assembly",
    "Run SharpHound.exe with execute_assembly on callback 7",
    "Collect BloodHound data for corp",
    "Collect BloodHound data for corp..test",
    "Collect BloodHound data for corp.example.test through callback 7 through callback 8",
    "Collect BloodHound data for corp.example.test through the current foothold on callback 7",
    "Collect BloodHound data for corp.example.test through callback 0",
    "Collect BloodHound data for corp.example.test through callback 07",
    "Collect BloodHound data for corp.example.test through callback seven",
    # In the bounded grammar the qualifier follows a callback selector.
    "Collect BloodHound data for corp.example.test; collection only",
])
def test_near_matches_and_qualified_requests_are_not_rewritten(prompt):
    assert ks.explicit_collection_request(prompt) is None
