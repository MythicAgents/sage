"""Offline checks for the reversible native-report projection experiment."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage, ToolMessage


SCRIPT = (Path(__file__).resolve().parents[3] / "skills" /
          "sage-focused-capability-tests" / "scripts" / "report_projection_candidate.py")


def module():
    spec = importlib.util.spec_from_file_location("report_projection_candidate", SCRIPT)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_exact_observation_provenance_positive_811_and_unknown():
    candidate = module().ReportProjectionCandidate()
    callback = SimpleNamespace(agent_name="Mythic_Operator", _tool_call_to_name={"call-1": "get_all_task_output_by_task_id"})
    candidate.request_id = "request-A"
    candidate.collect(callback, ToolMessage(tool_call_id="call-1", name="get_all_task_output_by_task_id",
        content=json.dumps({"task_output": [{"task_id": 811, "response_text":
            "Host: LAB-WS01\nPartOfDomain: True\nDomain: example.test\nTrust inventory: not collected"}]})))
    candidate.request_id = "request-B"
    result = candidate.project('{"proposals":[]}', "service_final")
    assert "PartOfDomain: True" in result
    assert "Trust inventory: not collected" in result
    assert "collected_request=request-A" in result
    assert "No next action proposed" in result
    assert "domain trusts exist" not in result
    assert candidate.project(result, "service_final") == result


def test_missing_near_match_and_control_observations_do_not_make_facts():
    candidate = module().ReportProjectionCandidate()
    callback = SimpleNamespace(agent_name="worker", _tool_call_to_name={"good": "get_all_task_output_by_task_id",
                                                                   "ctl": "respond_to_user"})
    candidate.collect(callback, ToolMessage(tool_call_id="unpaired", name="get_all_task_output_by_task_id",
                                            content="PartOfDomain: True"))
    candidate.collect(callback, ToolMessage(tool_call_id="ctl", name="respond_to_user",
                                            content="PartOfDomain: True"))
    candidate.collect(callback, ToolMessage(tool_call_id="good", name="get_all_task_output_by_task_id",
                                            content="PartOfDomain: Unknown"))
    result = candidate.project('{"proposals":[]}', "worker")
    assert len(candidate.observations) == 1
    assert "PartOfDomain: Unknown" in result
    assert "PartOfDomain: True" not in result


def test_malformed_schema_falls_back_to_sources_and_records_failure():
    candidate = module().ReportProjectionCandidate()
    bad = ('{"summary":"domain account", "proposals":'
           '[{"verify":["check"],"if_verified":"continue"}]}')
    result = candidate.project(bad, "worker")
    assert "domain account" not in result
    assert "Candidate format failure" in result
    assert len(candidate.candidate_format_failures) == 1
    assert candidate.events[0]["format_valid"] is False


def test_conditional_presupposition_is_structurally_admitted_for_grader():
    candidate = module().ReportProjectionCandidate()
    raw = json.dumps({"proposals": [{"verify": ["the account is a domain account"],
                                      "if_verified": "query the directory"}]})
    result = candidate.project(raw, "worker")
    assert "Before acting, verify the account is a domain account" in result
    assert candidate.events[0]["format_valid"] is True
    assert candidate.candidate_format_failures == []


def test_source_and_record_caps_are_honest():
    candidate = module().ReportProjectionCandidate()
    callback = SimpleNamespace(agent_name="worker", _tool_call_to_name={str(i): "read" for i in range(30)})
    for i in range(30):
        candidate.collect(callback, ToolMessage(tool_call_id=str(i), name="read", content="X" * 5000))
    result = candidate.project('{"proposals":[]}', "service_final")
    assert "source truncated" in result
    assert "source list capped" in result
    assert len(result) < 20000


def test_decorated_and_reused_final_keeps_projected_proposals():
    candidate = module().ReportProjectionCandidate()
    candidate.request_id = "one"
    raw = '{"proposals":[{"verify":["membership is observed"],"if_verified":"inspect trust data"}]}'
    displayed = candidate.project("📊 **Final Report**\n" + raw, "supervisor")
    assert "Before acting, verify membership is observed" in displayed
    assert candidate.project("🤖[Supervisor]> " + displayed, "service_final") == displayed
    assert candidate.candidate_format_failures == []
    candidate.request_id = "two"
    later = candidate.project("🤖[Supervisor]> " + displayed, "service_final")
    assert "Candidate format failure" in later


def test_scope_arguments_exact_reuse_and_late_observation():
    candidate = module().ReportProjectionCandidate()
    candidate.request_id = "current"
    callback = SimpleNamespace(agent_name="worker", _tool_call_to_name={"a": "read_credentials",
                                                             "b": "read_credentials"},
                               _tool_call_to_args={"a": {"realm": "A", "account": "alice"},
                                                   "b": {"realm": "B", "account": "bob"}})
    candidate.collect(callback, ToolMessage(tool_call_id="a", name="read_credentials",
                                            content="No credentials matching the given filters."))
    raw = '{"proposals":[]}'
    first = candidate.project(raw, "worker")
    assert 'arguments={"account": "alice", "realm": "A"}' in first
    # Extra text is never silently dropped under the guise of idempotence.
    altered = candidate.project(first + "\nUnverified assertion", "service_final")
    assert "Candidate format failure" in altered
    candidate.collect(callback, ToolMessage(tool_call_id="b", name="read_credentials",
                                            content="No credentials matching the given filters."))
    refreshed = candidate.project(first, "service_final")
    assert 'arguments={"account": "bob", "realm": "B"}' in refreshed
    assert refreshed != first
    assert "collected_request=current" in refreshed


def test_close_and_service_fallback_sanitize_all_narrative_fields():
    mod = module()
    from ai.langgraph import model as model_module
    from sage_chat import service
    seen = {}
    async def original_close(self, agent_name, content="", status="finished"):
        seen["close"] = (content, dict(self._active_delegations[agent_name]))
    def original_service(model, content):
        seen["service"] = content
        return content
    class Transport:
        async def handle_async_request(self, request):
            return httpx.Response(200)
    matrix = SimpleNamespace(MatrixTransport=Transport)
    candidate = mod.ReportProjectionCandidate()
    bad = "Invented domain administrator status"
    model = object.__new__(model_module.Model)
    model._active_delegations = {"worker": {"final_summary": bad,
                                            "last_text": bad,
                                            "streamed_text_chunks": [bad]}}
    with (patch.object(model_module.Model, "_close_delegation", original_close),
          patch.object(service, "_with_task_provenance", original_service)):
        with mod.install(candidate, matrix_module=matrix):
            asyncio.run(model._close_delegation("worker", content=bad))
            service._with_task_provenance(model, bad)
    assert bad not in seen["close"][0]
    assert bad not in seen["close"][1]["final_summary"]
    assert bad not in seen["close"][1]["last_text"]
    assert bad not in seen["close"][1]["streamed_text_chunks"][0]
    assert bad not in seen["service"]
    assert len(candidate.candidate_format_failures) >= 2
    json.dumps(candidate.audit())


def test_generated_delegation_title_is_replaced_before_native_open():
    mod = module()
    from ai.langgraph import model as model_module
    seen = {}
    async def original_open(self, agent_name, instruction, source_seq, title=""):
        seen.update(agent_name=agent_name, instruction=instruction,
                    source_seq=source_seq, title=title)
    class Transport:
        async def handle_async_request(self, request):
            return httpx.Response(200)
    candidate = mod.ReportProjectionCandidate()
    with patch.object(model_module.Model, "_open_delegation", original_open):
        with mod.install(candidate, matrix_module=SimpleNamespace(MatrixTransport=Transport)):
            model = object.__new__(model_module.Model)
            asyncio.run(model._open_delegation("Mythic_Operator", "Check callback 7", 2,
                                             title="Known domain administrator account"))
    assert seen == {"agent_name": "Mythic_Operator", "instruction": "Check callback 7",
                    "source_seq": 2, "title": "Review with Mythic_Operator"}
    assert candidate.events[-1]["raw"] == "Known domain administrator account"
    assert candidate.events[-1]["surface"] == "delegation_title"


def test_install_patches_native_surfaces_and_restores_them():
    mod = module()
    from ai.langgraph import model as model_module
    from sage_chat import service
    originals = (model_module.Model._format_message_for_streaming,
                 model_module.Model._close_delegation,
                 model_module.MessageCaptureCallback.on_tool_end,
                 service._with_task_provenance)

    class Transport:
        async def handle_async_request(self, request):
            self.seen = json.loads(request.content)
            return httpx.Response(200)

    matrix = SimpleNamespace(MatrixTransport=Transport)
    candidate = mod.ReportProjectionCandidate()
    with mod.install(candidate, matrix_module=matrix):
        body = {"model": "test", "messages": [{"role": "system", "content": "base"},
                                               {"role": "user", "content": "report"}],
                "tools": [{"type": "function"}], "tool_choice": "auto"}
        transport = Transport()
        request = httpx.Request("POST", "https://example.invalid/chat/completions", json=body)
        asyncio.run(transport.handle_async_request(request))
        assert mod.PROTOCOL in transport.seen["messages"][0]["content"]
        assert transport.seen["tools"] == body["tools"]
        assert transport.seen["tool_choice"] == "auto"
        assert candidate.protocol_requests[0]["body"] == transport.seen

        model = object.__new__(model_module.Model)
        model.channel_id = 1
        model.verbose = False
        callback = model_module.MessageCaptureCallback("worker")
        callback._tool_call_to_name["observed"] = "get_all_task_output_by_task_id"
        asyncio.run(callback.on_tool_end(ToolMessage(tool_call_id="observed",
            name="get_all_task_output_by_task_id", content="PartOfDomain: True"), run_id=uuid4()))
        assert len(candidate.observations) == 1
        formatted = model._format_message_for_streaming(AIMessage(content='{"proposals":[]}'), "worker")
        assert "PartOfDomain: True" in formatted
        assert '{"proposals":[]}' not in formatted
    assert (model_module.Model._format_message_for_streaming,
            model_module.Model._close_delegation,
            model_module.MessageCaptureCallback.on_tool_end,
            service._with_task_provenance) == originals
