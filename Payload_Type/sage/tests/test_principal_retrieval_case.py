"""Offline checks for the principal retrieval fixture boundary."""

import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import time

from langchain_core.messages import AIMessage
import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-focused-capability-tests/scripts/run_principal_retrieval_case.py"


def runner():
    spec = importlib.util.spec_from_file_location("principal_retrieval_case", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    return {"schema": "sage-principal-fixture-v1", "id": "principal_case_1",
        "prompt": "Review the retained principal evidence and BloodHound graph. Do not execute tasks.",
        "callbacks": [{"id": 7, "host": "LAB-WS01"}],
        "mythic_responses": [{"tool": "list_callbacks", "arguments": {},
            "result": [{"id": 7, "host": "LAB-WS01"}]}],
        "mcp_tools": [{"name": "user_info", "description": "Search BloodHound principals",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                "limit": {"type": "integer", "default": 10}}, "required": ["query"]}}],
        "mcp_responses": [{"tool": "user_info", "arguments": {"query": "alice"},
            "result": {"nodes": [{"name": "ALICE@LAB", "objectid": "S-1-5-21-1"}]}}],
        "expected": {"principal": "ALICE@LAB"}}


def test_exact_response_matching_and_default_normalization(tmp_path):
    mod = runner()
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture()))
    source = mod.Fixture(mod.load_fixture(path))
    assert json.loads(source.answer("mcp", "user_info", {"query": "alice", "limit": 10}))["nodes"][0]["objectid"] == "S-1-5-21-1"
    assert json.loads(source.answer("mythic", "list_callbacks", {}))[0]["id"] == 7
    with pytest.raises(mod.matrix.FixtureGap):
        source.answer("mcp", "user_info", {"query": "bob"})
    with pytest.raises(mod.matrix.FixtureGap):
        source.answer("mcp", "user_info", {"query": "alice", "limit": 11})
    with pytest.raises(mod.matrix.FixtureGap):
        source.answer("mcp", "user_info", {"query": "alice", "extra": "x"})


def test_mythic_registered_defaults_match_omitted_and_explicit_empty_filters(tmp_path, monkeypatch):
    mod = runner()
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("socket")))
    data = fixture()
    data["mythic_responses"].append({"tool": "read_credentials", "arguments": {},
        "result": {"credentials": [], "source": "synthetic empty store"}})
    wrapped = mod.Fixture(data)
    sink = mod.matrix.MythicTools(agent_task_id="offline", operation_id=1, channel_id=1)
    wrapped.bind_mythic_schemas(sink)
    registered = next(tool for tool in mod.matrix.MythicTools.get_tools(sink, ["read_credentials"])
                      if tool.name == "read_credentials")
    defaults = registered.args_schema.model_validate({}).model_dump()
    assert defaults == {"realm": "", "account": ""}
    expected = {"credentials": [], "source": "synthetic empty store"}
    assert json.loads(wrapped.answer("mythic", "read_credentials", {})) == expected
    assert json.loads(wrapped.answer("mythic", "read_credentials", defaults)) == expected
    with pytest.raises(mod.matrix.FixtureGap):
        wrapped.answer("mythic", "read_credentials", {"account": "river", "realm": ""})


def test_inspect_builds_actual_graph_without_socket_or_provider(tmp_path, monkeypatch, capsys):
    mod = runner()
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture()))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("socket")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("socket")))
    assert mod.main(["--fixture", str(path)]) == 0
    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert output["mode"] == "scripted_graph_build_only"
    assert {"Supervisor", "BloodHound"} <= set(output["graph_nodes"])
    assert output["mcp_tool_names"] == ["user_info"]
    assert output["prompt_sha256"]["bloodhound.md"]
    assert output["rendered_prompt_sha256"]["bloodhound_messages"]
    assert output["mcp_schema_sha256"]["user_info"]


def test_built_mcp_schema_preserves_declared_property_types(tmp_path):
    mod = runner()
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture()))
    source = mod.Fixture(mod.load_fixture(path))
    built = source.mcp_tools()[0].args_schema.model_json_schema()
    assert built["properties"]["query"]["type"] == "string"
    assert built["properties"]["limit"]["type"] == "integer"
    assert built["properties"]["limit"]["default"] == 10
    assert built["required"] == ["query"]


def test_native_tool_path_and_unexpected_query_stop(tmp_path, monkeypatch):
    mod = runner()
    from sage_chat import session
    original_marker = session._CHANNEL_MARKER_DB_PATH
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("socket")))
    source = fixture()
    source["fixture_sha256"] = "a" * 64
    wrapped = mod.Fixture(source)
    seen = []
    marker_paths = []

    def bh_reply(messages):
        seen.append(list(messages))
        marker_paths.append(session._CHANNEL_MARKER_DB_PATH)
        return mod.matrix.CASE.call("user_info", {"query": "alice"}, 3)

    fake = mod.matrix.CASE.ScriptedModel(messages=iter([
        mod.matrix.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "retained callback", "handoff_instruction": "Read callback inventory"}, 1),
        mod.matrix.CASE.call("list_callbacks", {}, 2),
        AIMessage(content="Callback 7 is LAB-WS01 in the retained inventory."),
        mod.matrix.CASE.call("transfer_to_BloodHound", {
            "handoff_title": "principal", "handoff_instruction": "Find alice"}, 3),
        bh_reply,
        AIMessage(content="BloodHound returned ALICE@LAB with objectid S-1-5-21-1."),
        AIMessage(content="Graph result identifies ALICE@LAB; no new task was run."),
    ]))
    monkeypatch.setattr(mod.matrix.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test",
        "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 8, "max_calls": 8,
        "max_case_tokens": 131072, "max_tokens": 131072, "output_tokens": 2048,
        "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(mod.execute(wrapped, profile, tmp_path, limits, {"127.0.0.1"}, None))
    assert result["status"] == "observed", result.get("error")
    assert wrapped.events[0]["source"] == "mythic"
    assert wrapped.events[0]["result"][0]["host"] == "LAB-WS01"
    assert wrapped.events[1]["source"] == "mcp"
    assert wrapped.events[1]["result"]["nodes"][0]["objectid"] == "S-1-5-21-1"
    assert result["effects"] == []
    assert seen
    assert marker_paths and Path(marker_paths[0]).name == "sage.db"
    assert Path(marker_paths[0]).parent != Path(session.database_path()).parent
    assert not Path(marker_paths[0]).exists()
    assert session._CHANNEL_MARKER_DB_PATH == original_marker


def test_execute_rejects_missing_limits_and_overwrite_before_network(tmp_path, monkeypatch, capsys):
    mod = runner()
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture()))
    profile = tmp_path / "profile"
    profile.write_text("provider=openai\nmodel=synthetic-test\nAPI_ENDPOINT=https://127.0.0.1:7443/v1\nAPI_KEY=offline\n")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("socket")))
    assert mod.main(["--fixture", str(path), "--execute", "--config", str(profile),
                     "--output-dir", str(out)]) == 2
    assert "limits" in capsys.readouterr().out
    (out / "case_principal_case_1.json").write_text("{}")
    import hashlib
    endpoint_hash = hashlib.sha256(b"https://127.0.0.1:7443/v1").hexdigest()
    assert mod.main(["--fixture", str(path), "--execute", "--config", str(profile),
        "--output-dir", str(out), "--expected-model", "synthetic-test",
        "--expected-endpoint-sha256", endpoint_hash, "--max-calls", "1", "--max-tokens", "10000",
        "--max-case-calls", "1",
        "--max-wall", "30", "--output-tokens", "512", "--request-seconds", "5",
        "--deadline-epoch", str(time.time() + 30)]) == 2
    assert "already exists" in capsys.readouterr().out


def test_native_unexpected_bloodhound_query_stops_without_fallback(tmp_path, monkeypatch):
    mod = runner()
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("socket")))
    source = fixture()
    source["fixture_sha256"] = "a" * 64
    wrapped = mod.Fixture(source)
    fake = mod.matrix.CASE.ScriptedModel(messages=iter([
        mod.matrix.CASE.call("transfer_to_BloodHound", {
            "handoff_title": "principal", "handoff_instruction": "Find bob"}, 1),
        mod.matrix.CASE.call("user_info", {"query": "bob"}, 2),
        AIMessage(content="must not be consumed"),
    ]))
    monkeypatch.setattr(mod.matrix.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test",
        "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 8, "max_calls": 8,
        "max_case_tokens": 131072, "max_tokens": 131072, "output_tokens": 2048,
        "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(mod.execute(wrapped, profile, tmp_path, limits, {"127.0.0.1"}, None))
    assert result["status"] == "harness_gap", result.get("error")
    assert not result["effects"] and not result["provider_requests"]
    assert wrapped.events == []


def test_retained_task_provenance_reaches_bloodhound_handoff(tmp_path, monkeypatch):
    mod = runner()
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("socket")))
    source = fixture()
    source["fixture_sha256"] = "a" * 64
    source["mythic_responses"].append({"tool": "get_all_task_output_by_task_id",
        "arguments": {"task_id": 701}, "result": {"task_output": [{"task_id": 701,
            "response_text": "Current principal SID S-1-5-21-10101-20202-30303-1101"}]}})
    wrapped = mod.Fixture(source)
    bh_inputs = []

    def supervisor_to_bh(messages):
        return mod.matrix.CASE.call("transfer_to_BloodHound", {
            "handoff_title": "graph principal", "handoff_instruction": "Check the retained principal"}, 3)

    def bh_answer(messages):
        bh_inputs.append(list(messages))
        return AIMessage(content="Received retained task evidence.")

    fake = mod.matrix.CASE.ScriptedModel(messages=iter([
        mod.matrix.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "retained identity", "handoff_instruction": "Read task 701"}, 1),
        mod.matrix.CASE.call("get_all_task_output_by_task_id", {"task_id": 701}, 2),
        AIMessage(content="The retained output names a principal SID."),
        supervisor_to_bh,
        bh_answer,
        AIMessage(content="Further verification is needed."),
    ]))
    monkeypatch.setattr(mod.matrix.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test",
        "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 8, "max_calls": 8,
        "max_case_tokens": 131072, "max_tokens": 131072, "output_tokens": 2048,
        "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(mod.execute(wrapped, profile, tmp_path, limits, {"127.0.0.1"}, None))
    assert result["status"] == "observed", result.get("error")
    assert result["tool_events"][0]["tool"] == "get_all_task_output_by_task_id"
    provenance = json.loads(result["tool_events"][0]["result"])["provenance"]
    assert provenance["task_id"] == 701 and provenance["origin"] == "historical_or_external"
    assert bh_inputs
    joined = "\n".join(str(message.content) for message in bh_inputs[0])
    assert any(message.additional_kwargs.get("_worker_task_evidence") is True
               for message in bh_inputs[0])
    assert "<untrusted_mythic_task_observations>" in joined
    assert "S-1-5-21-10101-20202-30303-1101" in joined


def test_declared_graph_search_inventory_semantics():
    mod = runner()
    source = fixture()
    source["expected"] = {"secret_verdict": "must never enter tool result"}
    source["mcp_tools"] = [
        {"name": "graph_analysis", "description": "Synthetic graph search",
         "parameters": {"type": "object", "properties": {
             "info_type": {"type": "string"}, "query": {"type": "string"},
             "search_type": {"type": "string", "default": "fuzzy"},
             "limit": {"type": "integer", "default": 100},
             "skip": {"type": "integer", "default": 0}},
             "required": ["info_type"]}},
        {"name": "domain_info", "description": "Synthetic domain search",
         "parameters": {"type": "object", "properties": {
             "info_type": {"type": "string", "default": "list"},
             "query": {"type": "string", "default": None},
             "object_type": {"type": "string", "default": None},
             "limit": {"type": "integer", "default": 100},
             "skip": {"type": "integer", "default": 0}}}},
    ]
    source["mcp_responses"] = [{"tool": "graph_analysis", "arguments": {
        "info_type": "search", "query": "CEDAR\\RIVER"},
        "result": {"canned": "must not supersede complete inventory"}}]
    source["search_nodes"] = [
        {"name": "CEDAR\\River", "objectID": "S-1-5-21-1-1101", "label": "User"},
        {"name": "CEDAR\\Riverton", "objectID": "S-1-5-21-1-1102", "label": "User"},
        {"name": "CEDAR-WS", "objectID": "S-1-5-21-1-2101", "label": "Computer"},
    ]
    wrapped = mod.Fixture(source)
    fuzzy = json.loads(wrapped.answer("mcp", "graph_analysis", {
        "info_type": "search", "query": "cEdAr\\RiV"}))
    assert fuzzy == {"info_type": "search", "data": {"count": 2, "limit": 100,
        "skip": 0, "data": source["search_nodes"][:2]}}
    assert "canned" not in json.loads(wrapped.answer("mcp", "graph_analysis", {
        "info_type": "search", "query": "CEDAR\\RIVER"}))
    exact = json.loads(wrapped.answer("mcp", "graph_analysis", {
        "info_type": "search", "query": "CEDAR\\RIVER", "search_type": "exact"}))
    assert exact["data"]["count"] == 1 and exact["data"]["data"][0]["objectID"] == "S-1-5-21-1-1101"
    by_id = json.loads(wrapped.answer("mcp", "graph_analysis", {
        "info_type": "search", "query": "s-1-5-21-1-1102", "search_type": "exact"}))
    assert by_id["data"]["data"][0]["name"] == "CEDAR\\Riverton"
    paged = json.loads(wrapped.answer("mcp", "domain_info", {
        "info_type": "search", "query": "CEDAR", "object_type": "user",
        "skip": 1, "limit": 1}))
    assert paged["data"]["count"] == 2
    assert [item["objectID"] for item in paged["data"]["data"]] == ["S-1-5-21-1-1102"]
    empty = json.loads(wrapped.answer("mcp", "graph_analysis", {
        "info_type": "search", "query": "absent"}))
    assert empty["data"] == {"count": 0, "limit": 100, "skip": 0, "data": []}
    assert wrapped.events[-1]["result"] == empty
    assert "secret_verdict" not in json.dumps(wrapped.events)
    for args in ({"info_type": "paths", "query": "CEDAR"},
                 {"info_type": "search", "query": ""},
                 {"info_type": "search", "query": "CEDAR", "search_type": "regex"},
                 {"info_type": "search", "query": "CEDAR", "skip": -1},
                 {"info_type": "search", "query": "CEDAR", "extra": "x"}):
        with pytest.raises(mod.matrix.FixtureGap):
            wrapped.answer("mcp", "graph_analysis", args)


def test_component_seed_accepts_only_bounded_system_insertion(tmp_path):
    mod = runner()
    contract = mod.build_request_contract(request_id="chat:1:request:1", channel_id=1,
        operation_id=1, mode="supervised", autonomous_solve=False)
    authority = mod.TurnAuthority(mode="supervised_action", request_id="chat:1:request:1",
        turn_id="chat:1:request:1", prompt_fingerprint=contract.digest,
        request_contract_digest=contract.digest)
    system = ("BloodHound baseline.\n\nSage runtime context. The following turn-authority block "
        "is generated by Sage, not supplied by the operator.\n" + authority.render_ephemeral() +
        "\n[supervised-runtime] retained projection")
    retained = {"model_calls": [{}, {}, {}, {}, {}, {"input_messages": [[
        {"type": "system", "content": system, "additional_kwargs": {}},
        {"type": "ai", "content": "evidence", "additional_kwargs": {"_worker_task_evidence": True}},
        {"type": "human", "content": "analyze", "additional_kwargs": {}}]]}],
        "provider_requests": [{}, {}, {}, {}, {}, {"request": {"model": "synthetic-test",
            "messages": [{"role": "system", "content": system}], "tools": [{}] * 9}}]}
    retained_path = tmp_path / "retained.json"
    retained_path.write_text(json.dumps(retained))
    baseline = mod.component_seed(retained_path)
    assert baseline["messages"][0]["content"] == "BloodHound baseline."
    assert baseline["messages"][1]["additional_kwargs"]["_worker_task_evidence"] is True
    assert baseline["authority"].render_ephemeral() == authority.render_ephemeral()
    candidate_path = tmp_path / "candidate.txt"
    candidate_path.write_text(system.replace("BloodHound baseline.",
        "BloodHound baseline. Read the retained principal first."))
    candidate = mod.component_seed(retained_path, candidate_path)
    assert candidate["messages"][0]["content"] == "BloodHound baseline. Read the retained principal first."
    assert mod._wire_difference(candidate["expected_wire"], baseline["expected_wire"]) == "wire.messages[0].content"
    candidate_path.write_text(system.replace("evidence", "success"))
    with pytest.raises(ValueError, match="one insertion"):
        mod.component_seed(retained_path, candidate_path)


def test_component_graph_uses_real_bloodhound_tool_and_hands_back(tmp_path, monkeypatch):
    mod = runner()
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("socket")))
    source = fixture()
    source["fixture_sha256"] = "a" * 64
    wrapped = mod.Fixture(source)
    text_and_tool = mod.matrix.CASE.call("user_info", {"query": "alice"}, 1)
    text_and_tool.content = "I'll verify the graph principal."
    fake = mod.matrix.CASE.ScriptedModel(messages=iter([
        text_and_tool,
        mod.matrix.CASE.call("summarize_and_handback", {
            "progress_summary": "Read the graph result", "tasks_remaining": "None",
            "key_findings": "ALICE@LAB is in the retained graph"}, 2),
    ]))
    monkeypatch.setattr(mod.matrix.model_module, "init_chat_model", lambda *a, **kw: fake)
    rpc_attempts = []
    async def forbid_task_rpc(*args, **kwargs):
        rpc_attempts.append((args, kwargs))
        raise AssertionError("task RPC must not be attempted")
    monkeypatch.setattr(mod.matrix.model_module, "SendMythicRPCResponseCreate", forbid_task_rpc)
    contract = mod.build_request_contract(request_id="chat:1:request:1", channel_id=1,
        operation_id=1, mode="supervised", autonomous_solve=False)
    component = {"messages": [
        {"type": "system", "content": "BloodHound synthetic seed", "additional_kwargs": {}},
        {"type": "ai", "content": "trusted evidence", "additional_kwargs": {"_worker_task_evidence": True}},
        {"type": "human", "content": "analyze alice", "additional_kwargs": {}}],
        "authority": mod.TurnAuthority(mode="supervised_action", request_id="chat:1:request:1",
            turn_id="chat:1:request:1", prompt_fingerprint=contract.digest,
            request_contract_digest=contract.digest),
        "request_contract": contract,
        "supervised_projection": "[supervised-runtime] synthetic",
        "expected_wire": {"model": "synthetic-test", "tools": [{}] * 9},
        "retained_sha256": "a" * 64, "insertion_sha256": None}
    profile = {"provider": "openai", "model": "synthetic-test",
        "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 8, "max_calls": 8,
        "max_case_tokens": 131072, "max_tokens": 131072, "output_tokens": 2048,
        "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(mod.execute(wrapped, profile, tmp_path, limits, {"127.0.0.1"}, None,
        component=component))
    assert result["status"] == "component_observed", result.get("error")
    assert result["component_next"] == ["Supervisor"]
    assert any("ALICE@LAB is in the retained graph" in str(row["content"])
               for row in result["component_supervisor_messages"])
    assert result["component_supervised_projection"]["harness_replay"] is True
    assert any("I'll verify the graph principal." in str(row.get("content"))
               for row in result["component_emissions"])
    assert not rpc_attempts
    assert wrapped.events and wrapped.events[0]["source"] == "mcp"
    assert wrapped.events[0]["result"]["nodes"][0]["name"] == "ALICE@LAB"
    assert not result["effects"] and not result["provider_requests"]
    assert result["user_visible_proposal"] is False
