"""Graph observations carry one reusable source identity from the BloodHound worker to later delegations.

One pure builder serves the step references, the Supervisor handback and a fresh worker's context. A record is
usable support only when its call and result pair unambiguously; an error stays an error.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

import tests.test_scripted_handoff as harness_module
from ai.langgraph import model as model_module
from ai.langgraph import step_references as sr

REQUEST = "chat:1:request:1"
PATH = "PATH: USER-A -[MemberOf]-> GROUP-B -[GenericAll]-> DC-1"


def _call(call_id="graph-1", name="graph_analysis", query="shortest path to Domain Admins"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {"query": query}, "id": call_id,
                                              "type": "tool_call"}])


def _result(call_id="graph-1", name="graph_analysis", content=PATH, status="success"):
    return ToolMessage(content=content, name=name, tool_call_id=call_id, status=status)


def _records(messages, **kwargs):
    return sr.graph_observation_records(messages, {"graph_analysis"}, request_id=REQUEST,
                                        is_error=model_module._tool_result_is_error, **kwargs)


def test_a_paired_successful_query_gets_a_citation_digest_and_disposition():
    [record] = _records([_call(), _result()], generation="channel:generation:abc")
    assert record["citation"] == f"graph:{REQUEST}:graph-1"
    assert record["usable"] is True and record["pairing"] == "paired"
    assert record["disposition"] == "success"
    assert record["generation"] == "channel:generation:abc"
    assert record["result_sha256"] == hashlib.sha256(PATH.encode("utf-8")).hexdigest()
    arguments = json.dumps({"query": "shortest path to Domain Admins"}, ensure_ascii=False, sort_keys=True)
    assert record["arguments_sha256"] == hashlib.sha256(arguments.encode("utf-8")).hexdigest()
    assert record["result_excerpt"] == PATH and record["result_truncated"] is False


@pytest.mark.parametrize("result", [
    _result(status="error", content="Neo4j unavailable"),
    _result(content='{"status": "error", "error": "query rejected"}'),
    _result(content="Error: syntax error in cypher"),
])
def test_an_error_result_stays_an_error_not_a_successful_observation(result):
    [record] = _records([_call(), result])
    assert record["disposition"] == "error"
    assert record["usable"] is True  # an error is still a citable observation of an error


@pytest.mark.parametrize("case", ["duplicate_call", "duplicate_result", "tool_name_mismatch"])
def test_ambiguous_pairing_is_visible_but_never_usable_support(case):
    messages = [_call(), _result()]
    if case == "duplicate_call":
        messages.insert(1, _call(query="a different query"))
    elif case == "duplicate_result":
        messages.append(_result(content="PATH: USER-A -[AdminTo]-> DC-1"))
    else:
        messages[1] = _result(name="other_graph_tool")
    records = _records(messages)
    assert records and all(record["usable"] is False and "citation" not in record for record in records)
    assert all(record["pairing"] == case for record in records)
    assert all("result_excerpt" not in record for record in records)


def test_an_unpaired_result_or_unanswered_call_produces_no_record():
    assert _records([_result()]) == []
    assert _records([_call()]) == []


def test_truncation_is_explicit_and_the_digest_covers_the_full_result():
    text = "x" * 5000
    [record] = _records([_call(), _result(content=text)])
    assert record["result_truncated"] is True and len(record["result_excerpt"]) < len(text)
    assert record["result_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_step_references_and_the_handback_share_one_builder():
    messages = [_call(), _result(), _call("graph-2"), _result("graph-2", status="error", content="boom")]
    references = sr.graph_read_references(messages, {"graph_analysis"},
                                          is_error=model_module._tool_result_is_error)
    records = _records(messages)
    assert [(r["tool_call_id"], r["args_sha256"], r["result_sha256"], r["disposition"]) for r in references] == [
        (r["source_tool_call_id"], r["arguments_sha256"], r["result_sha256"], r["disposition"]) for r in records]
    evidence = model_module._worker_graph_evidence_message(messages, request_id=REQUEST, worker="BloodHound")
    projected = json.loads(evidence.content.splitlines()[-2])
    assert [r["citation"] for r in projected] == [f"graph:{REQUEST}:graph-1", f"graph:{REQUEST}:graph-2"]
    assert [r["disposition"] for r in projected] == ["success", "error"]


def test_a_fresh_worker_selects_graph_envelopes_with_their_collection_identity():
    evidence = model_module._worker_graph_evidence_message([_call(), _result()], request_id=REQUEST,
                                                           worker="BloodHound")
    supervisor = [evidence]
    delegated = HumanMessage(content="Restate it.", additional_kwargs={"_delegated_to": "BloodHound"})
    selected = model_module._assisted_worker_observation_context(supervisor, [delegated])
    assert selected == [evidence]
    assert model_module._assisted_worker_observation_context(supervisor, [evidence, delegated]) == []


def test_assembled_fresh_bloodhound_delegation_reuses_the_cited_result_without_its_transcript(monkeypatch):
    seen = []
    original = harness_module._ScriptedModel._generate

    def generate(self, messages, *args, **kwargs):
        seen.append(list(messages))
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(harness_module._ScriptedModel, "_generate", generate)
    monkeypatch.setattr(harness_module, "_RECURSION_LIMIT", 30)
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    from ai.mcp import MCPManager
    from ai.langgraph.model import Model

    queries = []

    @tool
    def graph_analysis(query: str) -> str:
        """Run an inert offline graph query."""
        queries.append(query)
        return PATH

    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _server: [graph_analysis]))
    monkeypatch.setattr(Model, "_bloodhound_server_is_locally_pinned", lambda self, server: server == "offline-graph")

    def route(call_id, title):
        return AIMessage(content="", tool_calls=[{"name": "transfer_to_BloodHound", "args": {
            "handoff_title": title, "handoff_instruction": title}, "id": call_id, "type": "tool_call"}])

    script = [
        route("route-1", "Find a path to Domain Admins"),
        _call(), AIMessage(content="USER-A reaches DC-1 through GROUP-B."),
        route("route-2", "Restate the cited result without querying again"),
        AIMessage(content="Restated from the earlier result."),
        AIMessage(content="", tool_calls=[{"name": "respond_to_user", "args": {
            "final_response": "Restated.", "outcome": "completed"}, "id": "final-1", "type": "tool_call"}]),
    ]
    harness = harness_module._Harness(script)
    asyncio.run(harness.run("Use BloodHound to find a path, then restate it.", assisted=True))
    assert queries == ["shortest path to Domain Admins"]
    workers = [messages for messages in seen
               if any(isinstance(m, HumanMessage) and m.additional_kwargs.get("_delegated_to") == "BloodHound"
                      for m in messages)]
    restating = workers[-1]
    graph = [m for m in restating if m.additional_kwargs.get("_worker_graph_evidence")]
    assert len(graph) == 1
    assert '"citation": "graph:' in graph[0].content and ':graph-1"' in graph[0].content
    # The earlier worker's tool transcript and prose are not replayed into the fresh delegation.
    assert not any(isinstance(m, ToolMessage) for m in restating)
    assert not any("USER-A reaches DC-1" in str(m.content) for m in restating)
