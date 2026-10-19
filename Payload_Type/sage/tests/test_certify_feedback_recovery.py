"""Inert checks for the fault-injected Certify feedback measurement boundary."""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-focused-capability-tests/scripts/run_certify_feedback_recovery.py"


def module():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("certify_feedback_recovery", SCRIPT)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def request(*, stream=False, operator=True):
    tools = ([{"type": "function", "function": {"name": "issue_task_and_waitfor_task_output",
               "parameters": {"type": "object"}}}] if operator else [])
    return httpx.Request("POST", "https://127.0.0.1:7443/v1/chat/completions", json={
        "model": "synthetic-test", "max_tokens": 2048, "stream": stream, "tools": tools,
        "messages": [{"role": "user", "content": "inert"}],
    })


@pytest.mark.parametrize("stream", [False, True])
def test_seed_is_local_and_preserves_invalid_proposal(stream, monkeypatch):
    runner = module()
    class NoNetwork(httpx.AsyncBaseTransport):
        async def handle_async_request(self, _request):
            raise AssertionError("network transport reached")
    class Ledger:
        def reserve(self, *_args, **_kwargs):
            raise AssertionError("seed must not reserve a paid request")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: NoNetwork())
    runner.SeededMeteredTransport.configured_ledger = Ledger()
    transport = runner.SeededMeteredTransport("https://127.0.0.1:7443/v1", "synthetic-test",
        max_requests=8, output_tokens=2048, persist=lambda: None)
    response = asyncio.run(transport.handle_async_request(request(stream=stream)))
    body = response.text
    assert "issue_task_and_waitfor_task_output" in body
    assert "forge" in body
    assert transport.seeded and transport.seed_event["kind"] == "local_fault_injection"
    assert transport.requests == []
    asyncio.run(transport.aclose())


def test_fixture_real_preparation_denies_unenrolled_verb_and_prepares_valid(tmp_path, monkeypatch):
    runner = module()
    descriptor = json.loads((runner.SAGE / "ttps/contracts/certify.json").read_text())
    import hashlib
    descriptor["binary"]["sha1"] = hashlib.sha1(runner.ARTIFACT).hexdigest()
    contract_root = tmp_path / "contracts"
    contract_root.mkdir()
    (contract_root / "certify.json").write_text(json.dumps(descriptor))
    monkeypatch.setattr(runner.mythic_tools, "_EXECUTABLE_CONTRACT_ROOT", contract_root)
    sink = runner.MythicTools(agent_task_id="inert-certify")
    runner.fixture_bindings(sink)

    async def exercise():
        with pytest.raises(ValueError, match="not enrolled"):
            await sink._prepare_supervised_issue_action(runner.SEED_ARGS)
        result = await sink._prepare_supervised_issue_action({
            "command": "execute_assembly", "callback_display_id": 7,
            "parameters": {"Assembly": "Certify.exe", "Arguments": {
                "verb": "enum-templates", "options": {"filter-vulnerable": True}}},
        })
        return result

    prepared = asyncio.run(exercise())
    assert prepared["parameters"]["Arguments"] == "enum-templates --filter-vulnerable"


def test_guidance_fixture_reads_checked_in_certify_only_on_matching_request(monkeypatch):
    runner = module()
    sink = runner.MythicTools(agent_task_id="inert-certify-guidance")
    runner.fixture_bindings(sink)
    guide = runner.SAGE / "ttps/certify.md"
    original = guide.read_text(encoding="utf-8")
    expected_frontmatter, expected_body = runner.ttp_library.parse_markdown(original)
    observed_reads = []
    original_load = runner.ttp_library.load_ttp

    def observed_load(slug):
        observed_reads.append(slug)
        return original_load(slug)

    monkeypatch.setattr(runner.ttp_library, "load_ttp", observed_load)
    assert observed_reads == []
    args = {"goal": "enumerate ADCS certificate templates with Certify.exe",
            "callback_display_id": 7}
    result = json.loads(asyncio.run(runner.fixture_dispatch(
        "get_ttp_guidance", args, sink, None)))
    assert observed_reads == ["certify"]
    assert result["slug"] == "certify"
    assert result["frontmatter"] == expected_frontmatter
    assert result["guidance"] == runner.ttp_library.guidance_body(expected_body)
    assert result["frontmatter"]["binary_sha1"] == expected_frontmatter["binary_sha1"]
    assert result["payload_type"] == "apollo"
    with pytest.raises(runner.matrix.FixtureGap):
        asyncio.run(runner.fixture_dispatch("get_ttp_guidance", {
            "goal": "unrelated goal", "callback_display_id": 7}, sink, None))
    assert observed_reads == ["certify"]


def test_classification_needs_actual_feedback_and_pending_valid_card():
    runner = module()
    card = {"metadata": {"input_requested": {"data": {"actions": [{
        "tool_name": "issue_task_and_waitfor_task_output", "arguments": {
            "command": "execute_assembly", "callback_display_id": 7,
            "parameters": {"Assembly": "Certify.exe",
                           "Arguments": "enum-templates --filter-vulnerable"}}}]}}}}
    result = {"initial_cards": [card], "effects": [], "provider_requests": [{
        "status": "returned", "request": {"messages": [{"content":
        "[task preparation] Approval was not requested: executable verb 'forge' is not enrolled; "
        "enrolled verbs for this artifact: enum-templates"}]}}]}
    classified = runner.classify(result, {"kind": "local_fault_injection"})
    assert classified["seeded"] and classified["actual_preparation_feedback_seen"]
    assert classified["operator_provider_request_count"] == 0
    assert not classified["operator_preparation_feedback_seen"]
    assert classified["enrolled_alternatives_seen"]
    assert classified["valid_enum_templates_pending_card"]
    assert classified["effect_count"] == 0
    result["provider_requests"][0]["request"]["messages"] = []
    assert not runner.classify(result, {"kind": "local_fault_injection"})[
        "actual_preparation_feedback_seen"]


def test_cli_dry_run_and_missing_shared_ledger_fail_before_network(tmp_path, monkeypatch, capsys):
    runner = module()
    config = tmp_path / "provider.conf"
    config.write_text("provider=openai\nmodel=synthetic-test\n"
                      "API_ENDPOINT=https://127.0.0.1:7443/v1\nAPI_KEY=inert\n")
    endpoint = "https://127.0.0.1:7443/v1"
    args = ["--config", str(config), "--output-dir", str(tmp_path / "output"),
            "--expected-model", "synthetic-test",
            "--expected-endpoint-sha256", hashlib.sha256(endpoint.encode()).hexdigest(),
            "--expected-source-sha256", runner.sha256(
                runner.SAGE / "ai/langgraph/executable_contracts.py")]
    monkeypatch.setattr(runner.matrix.REAL, "resolved_endpoint_ips",
        lambda *_: (_ for _ in ()).throw(AssertionError("DNS must not occur")))
    assert runner.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run_ready"
    for name in ("SAGE_RESOURCE_BUDGET_CONFIG", "SAGE_RESOURCE_BUDGET_LEDGER",
                 "SAGE_RESOURCE_EPISODE_ID", "SAGE_RESOURCE_BUDGET_PHASE"):
        monkeypatch.delenv(name, raising=False)
    assert runner.main(["--run", *args]) == 2
    assert "required" in json.loads(capsys.readouterr().out)["reason"]
    assert not (tmp_path / "output").exists()


def test_assembled_graph_consumes_real_preparation_feedback_without_effect(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))

    class Ledger:
        def __init__(self):
            self.reservations = []
            self.settlements = []

        def reserve(self, amounts, operation):
            self.reservations.append((amounts, operation))
            return "inert-reservation-" + str(len(self.reservations))

        def settle_provider(self, reservation, actual):
            self.settlements.append((reservation, actual))

    class FakeProvider(httpx.AsyncBaseTransport):
        def __init__(self):
            self.operator_calls = 0
            self.supervisor_calls = 0
            self.typed_schema_seen = False

        async def handle_async_request(self, request):
            body = json.loads(request.content)
            role = runner.matrix.REAL._role_hint(body)
            if role.startswith("Supervisor"):
                self.supervisor_calls += 1
                name = "transfer_to_Mythic_Operator"
                args = {"handoff_title": "prepare Certify",
                        "handoff_instruction": "Prepare the synthetic Certify template enumeration."}
            elif role.startswith("Mythic_Operator"):
                self.operator_calls += 1
                if self.operator_calls == 1:
                    name = "get_all_command_args_for_payloadtype"
                    args = {"payload": "apollo", "command": "execute_assembly",
                            "callback_display_id": 7, "executable_filename": "Certify.exe",
                            "executable_verb": "enum-templates"}
                else:
                    name = "issue_task_and_waitfor_task_output"
                    args = {"command": "execute_assembly", "callback_display_id": 7,
                            "parameters": {"Assembly": "Certify.exe", "Arguments": {
                                "verb": "enum-templates", "options": {"filter-vulnerable": True}}}}
                    issue = next(tool["function"]["parameters"] for tool in body["tools"]
                                 if tool.get("function", {}).get("name") == name)
                    verb_schema = issue["properties"]["parameters"]["properties"][
                        "Arguments"]["properties"]["verb"]
                    self.typed_schema_seen = verb_schema.get("const", verb_schema.get("enum")) in (
                        "enum-templates", ["enum-templates"])
            else:
                raise AssertionError("unexpected model role")
            tool_call = {"id": f"fake-{self.supervisor_calls}-{self.operator_calls}",
                         "type": "function", "function": {"name": name,
                         "arguments": json.dumps(args)}}
            if body.get("stream") is True:
                chunks = [
                    {"id": "inert-provider", "object": "chat.completion.chunk", "created": 0,
                     "model": "synthetic-test", "choices": [{"index": 0,
                         "delta": {"role": "assistant", "tool_calls": [{"index": 0, **tool_call}]},
                         "finish_reason": None}]},
                    {"id": "inert-provider", "object": "chat.completion.chunk", "created": 0,
                     "model": "synthetic-test", "choices": [{"index": 0, "delta": {},
                         "finish_reason": "tool_calls"}]},
                    {"id": "inert-provider", "object": "chat.completion.chunk", "created": 0,
                     "model": "synthetic-test", "choices": [],
                     "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}},
                ]
                content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                return httpx.Response(200, request=request, content=(content + "data: [DONE]\n\n").encode(),
                    headers={"content-type": "text/event-stream"})
            return httpx.Response(200, request=request, json={
                "id": "inert-provider", "object": "chat.completion", "created": 0,
                "model": "synthetic-test", "choices": [{"index": 0,
                    "finish_reason": "tool_calls", "message": {"role": "assistant",
                    "content": None, "tool_calls": [tool_call]}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})

    provider = FakeProvider()
    ledger = Ledger()
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: provider)
    profile = {"provider": "openai", "model": "synthetic-test",
               "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "inert"}
    limits = SimpleNamespace(max_case_calls=8, max_calls=8, max_case_tokens=250000,
        max_tokens=250000, output_tokens=2048, request_seconds=5, max_wall=30)
    result = asyncio.run(runner.run_case(profile, tmp_path, ledger, limits, None,
        {"127.0.0.1"}, tmp_path / "contracts"))
    assert result["injected_proposal"] is not None
    assert result["classification"]["actual_preparation_feedback_seen"], result.get("error")
    assert result["classification"]["operator_provider_request_count"] >= 2, result.get("error")
    assert result["classification"]["operator_preparation_feedback_seen"], result.get("error")
    assert provider.typed_schema_seen, result.get("error")
    assert result["classification"]["typed_enum_templates_schema_on_wire"], result.get("error")
    assert result["classification"]["valid_enum_templates_pending_card"], result.get("error")
    assert result["classification"]["effect_count"] == 0
    assert provider.supervisor_calls == 1
    assert len(ledger.reservations) == len(ledger.settlements)
    assert len(ledger.reservations) == result["classification"]["paid_provider_request_count"]
