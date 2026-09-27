"""Network-free checks for the explicit real-model human-led case boundary."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import ssl
import sys

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage


SCRIPT = (Path(__file__).resolve().parents[3] / "skills" /
          "sage-focused-capability-tests" / "scripts" / "run_real_model_human_led_case.py")


def _module():
    spec = importlib.util.spec_from_file_location("real_human_led_case", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(tmp_path):
    secret = "probe-secret-never-print"
    path = tmp_path / ".env.local"
    path.write_text("provider=openai\nmodel=synthetic-test-model\n"
                    "API_ENDPOINT=https://127.0.0.1:7443/v1\nAPI_KEY=" + secret + "\n")
    return path, secret


def test_dry_run_has_no_network_and_no_secret_output(tmp_path, monkeypatch):
    config, secret = _config(tmp_path)
    import socket
    def blocked(*_args, **_kwargs):
        raise AssertionError("dry run attempted network")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    proc = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config)],
                          capture_output=True, text=True, timeout=15, check=False)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "dry_run_ready"
    assert secret not in proc.stdout + proc.stderr
    # Also invoke in this process, where the socket guard is observable.
    module = _module()
    assert module.main(["--config", str(config)]) == 0


def test_identity_redirect_cli_is_available_without_network(tmp_path, monkeypatch, capsys):
    config, secret = _config(tmp_path)
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("dry run attempted network")))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: (_ for _ in ()).throw(
        AssertionError("dry run attempted DNS")))
    module = _module()
    assert module.main(["--config", str(config), "--scenario", "identity-freshness",
                        "--decision", "redirect"]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["status"] == "dry_run_ready"
    assert ready["limits"]["requests"] == 6
    assert ready["limits"]["output_tokens_per_request"] == 2048
    assert module.main(["--config", str(config), "--decision", "redirect"]) == 2
    rejected = capsys.readouterr().out
    assert json.loads(rejected)["reason"] == "redirect_requires_identity_scenario"
    assert secret not in rejected


def test_exact_card_and_near_match_rejection():
    module = _module()
    exact = {"metadata": {"input_requested": {"data": {"actions": [{
        "tool_name": module.CASE.EXPECTED["name"],
        "arguments": module.CASE.EXPECTED["args"],
    }]}}}}
    assert module.exact_card([exact])[0] is True
    assert module.guarded_decision([exact], "accept") == "accept"
    assert module.guarded_decision([exact], "reject") == "reject"
    for equivalent in ("{}", {}, '""'):
        same_effect = json.loads(json.dumps(exact))
        same_effect["metadata"]["input_requested"]["data"]["actions"][0]["arguments"]["parameters"] = equivalent
        assert module.exact_card([same_effect])[0] is True
        assert module.guarded_decision([same_effect], "accept") == "accept"
        assert (module.effective_case_arguments(
            same_effect["metadata"]["input_requested"]["data"]["actions"][0]["arguments"]
        ) == module.effective_case_arguments(module.CASE.EXPECTED["args"]))
    near = json.loads(json.dumps(exact))
    near["metadata"]["input_requested"]["data"]["actions"][0]["arguments"]["command"] = "hostname"
    assert module.exact_card([near])[0] is False
    assert module.guarded_decision([near], "accept") == "reject"
    for change in ({"callback_display_id": 8}, {"parameters": '{"command":"hostname"}'},
                   {"token_id": 2}):
        different = json.loads(json.dumps(exact))
        different["metadata"]["input_requested"]["data"]["actions"][0]["arguments"].update(change)
        assert module.guarded_decision([different], "accept") == "reject"
    assert module.exact_card([])[0] is False
    assert module.guarded_decision([], "accept") is None
    assert module.exact_card([exact, exact])[0] is False
    extra_action = json.loads(json.dumps(exact))
    extra_action["metadata"]["input_requested"]["data"]["actions"].append({
        "tool_name": module.CASE.EXPECTED["name"],
        "arguments": {**module.CASE.EXPECTED["args"], "command": "hostname"},
    })
    assert module.guarded_decision([extra_action], "accept") == "reject"


def test_shell_whoami_is_the_only_added_action_and_v5_card_is_accepted():
    module = _module()
    def card(arguments):
        return {"metadata": {"input_requested": {"data": {"actions": [{
            "tool_name": module.CASE.EXPECTED["name"], "arguments": arguments,
        }]}}}}

    v5_arguments = {"command": "shell", "parameters": '{"command": "whoami"}',
                    "callback_display_id": 7}
    assert module.exact_card([card(v5_arguments)])[0] is True
    assert module.guarded_decision([card(v5_arguments)], "accept") == "accept"
    assert module.effective_case_arguments(v5_arguments)["parameters"] == "whoami"
    for parameters in ("whoami", {"command": "whoami"}):
        assert module.guarded_decision([card({**v5_arguments, "parameters": parameters})],
                                       "accept") == "accept"
    for parameters in ('{"command":"whoami","extra":"ignored"}',
                       '{"command":"whoami","command":"whoami"}',
                       '{"command":"whoami /all"}', '"whoami"',
                       "whoami && hostname", "hostname"):
        assert module.guarded_decision([card({**v5_arguments, "parameters": parameters})],
                                       "accept") == "reject"
    for change in ({"callback_display_id": 8}, {"command": "run"},
                   {"parameters": '{"cmd":"whoami"}'}, {"token_id": 2}):
        assert module.guarded_decision([card({**v5_arguments, **change})], "accept") == "reject"


def test_request_count_and_output_token_limit_fail_before_network():
    module = _module()
    transport = module.BoundedTransport("https://127.0.0.1:7443/v1", "synthetic-test-model")
    class Inner:
        def __init__(self):
            self.sent = 0
        async def handle_async_request(self, request):
            self.sent += 1
            return httpx.Response(200, json={})
    inner = Inner()
    transport.inner = inner
    async def exercise():
        wrong = httpx.Request("POST", transport.endpoint, json={"max_tokens": 513,
                                                               "model": "synthetic-test-model"})
        with pytest.raises(module.CaseError, match="missing_output_token_limit"):
            await transport.handle_async_request(wrong)
        wrong_model = httpx.Request("POST", transport.endpoint, json={"max_tokens": 512,
                                                                     "model": "other-model"})
        with pytest.raises(module.CaseError, match="unexpected_wire_model"):
            await transport.handle_async_request(wrong_model)
        conflicting = httpx.Request("POST", transport.endpoint,
                                   json={"max_tokens": 512, "max_completion_tokens": 512,
                                         "model": "synthetic-test-model"})
        with pytest.raises(module.CaseError, match="missing_output_token_limit"):
            await transport.handle_async_request(conflicting)
        good = httpx.Request("POST", transport.endpoint, json={"max_completion_tokens": 512,
                                                              "model": "synthetic-test-model"})
        for _ in range(module.MAX_REQUESTS):
            await transport.handle_async_request(good)
        with pytest.raises(module.CaseError, match="model_request_limit"):
            await transport.handle_async_request(good)
        with pytest.raises(module.CaseError, match="unexpected_network_endpoint"):
            await transport.handle_async_request(httpx.Request("POST", "https://example.org/v1/chat/completions"))
    asyncio.run(exercise())
    assert inner.sent == module.MAX_REQUESTS


def test_transport_captures_exact_wire_request_and_paired_response_without_route_or_secret():
    module = _module()
    transport = module.BoundedTransport("https://127.0.0.1:7443/v1", "synthetic-test-model")
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({"model": "synthetic-test-model", "choices": [{
                "index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "Ready", "tool_calls": [{
                        "id": "call-1", "type": "function", "function": {
                            "name": "transfer_to_Mythic_Operator", "arguments": "{}"}}]}}]}).encode()
        async def aclose(self):
            pass
    class Inner:
        async def handle_async_request(self, request):
            assert request.headers["authorization"] == "Bearer probe-secret-never-print"
            return httpx.Response(200, stream=Stream(), request=request)
    transport.inner = Inner()
    body = {"model": "synthetic-test-model", "max_completion_tokens": 512,
            "temperature": 0.2, "tool_choice": "auto", "messages": [
                {"role": "user", "content": "Current identity?"}], "tools": [{
                    "type": "function", "function": {"name": "transfer_to_Mythic_Operator",
                                                    "description": "handoff", "parameters": {"type": "object"}}}],
            "metadata": {"API_KEY": "probe-secret-never-print"}}
    request = httpx.Request("POST", transport.endpoint, json=body,
                            headers={"Authorization": "Bearer probe-secret-never-print"})
    async def exercise():
        response = await transport.handle_async_request(request)
        assert "response" not in transport.requests[0]
        assert (await response.aread()) == response.content
    asyncio.run(exercise())
    captured = transport.requests[0]
    assert captured["role_hint"] == "Supervisor (inferred from offered tools)"
    assert captured["request"]["messages"] == body["messages"]
    assert captured["request"]["tools"] == body["tools"]
    assert captured["request"]["temperature"] == 0.2
    assert captured["response"]["choices"][0]["finish_reason"] == "tool_calls"
    assert captured["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "transfer_to_Mythic_Operator"
    printed = json.dumps(captured)
    assert "probe-secret-never-print" not in printed
    assert "127.0.0.1" not in printed
    assert "metadata" not in captured["request"]
    assert module._role_hint({"tools": [{"function": {
        "name": "issue_task_and_waitfor_task_output"}}]}) == (
        "Mythic_Operator (inferred from offered tools)")
    assert module._role_hint({"messages": [{"role": "user", "content": "hello"}]}) == "undetermined"


def test_streamed_provider_deltas_reconstruct_content_tools_and_finish_reason():
    module = _module()
    events = [
        {"model": "synthetic-test-model", "choices": [{"index": 0, "delta": {
            "role": "assistant", "content": "I will ", "tool_calls": [{"index": 0,
                "id": "call-1", "type": "function", "function": {
                    "name": "transfer_to_Mythic_Operator", "arguments": '{"handoff_'}}]}}]},
        {"choices": [{"index": 0, "delta": {"content": "check", "tool_calls": [{
            "index": 0, "function": {"arguments": 'title":"check"}'}}]},
            "finish_reason": "tool_calls"}]},
    ]
    body = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
    body += b"data: [DONE]\n\n"
    parsed = module._response_choices(body)
    assert parsed["choices"][0]["message"]["content"] == "I will check"
    assert parsed["choices"][0]["message"]["tool_calls"][0]["function"] == {
        "name": "transfer_to_Mythic_Operator", "arguments": '{"handoff_title":"check"}'}
    assert parsed["choices"][0]["finish_reason"] == "tool_calls"


def test_callback_pairs_internal_handoff_tags_with_completion_and_error():
    module = _module()
    recorder = module.UsageRecorder()
    from uuid import uuid4
    from langchain_core.outputs import ChatGeneration, LLMResult
    first, second = uuid4(), uuid4()
    tagged = HumanMessage(content="fresh result", additional_kwargs={
        "_request_id": "request-2", "_worker_final_answer": True})
    output = AIMessage(content="Report", tool_calls=[{
        "name": "transfer_to_Mythic_Operator", "args": {}, "id": "call-1"}],
        additional_kwargs={"_worker_final_answer": True},
        response_metadata={"finish_reason": "tool_calls"})
    async def exercise():
        await recorder.on_chat_model_start({}, [[tagged]], run_id=first)
        tagged.content = "later graph mutation"
        tagged.additional_kwargs["_worker_final_answer"] = False
        await recorder.on_llm_end(LLMResult(generations=[[ChatGeneration(
            message=output)]]), run_id=first)
        output.content = "later output mutation"
        output.tool_calls[0]["name"] = "different_tool"
        output.additional_kwargs["_worker_final_answer"] = False
        await recorder.on_chat_model_start({}, [[HumanMessage(content="retry")]], run_id=second)
        await recorder.on_llm_error(RuntimeError("private-detail"), run_id=second)
    asyncio.run(exercise())
    assert recorder.calls[0]["input_messages"][0][0]["content"] == "fresh result"
    assert recorder.calls[0]["input_messages"][0][0]["additional_kwargs"] == {
        "_request_id": "request-2", "_worker_final_answer": True}
    assert recorder.calls[0]["status"] == "completed"
    assert recorder.calls[0]["run_id"] == str(first)
    assert recorder.calls[0]["output_messages"][0]["content"] == "Report"
    assert recorder.calls[0]["output_messages"][0]["tool_calls"][0]["name"] == "transfer_to_Mythic_Operator"
    assert recorder.calls[0]["output_messages"][0]["additional_kwargs"] == {
        "_worker_final_answer": True}
    assert recorder.calls[0]["output_messages"][0]["finish_reason"] == "tool_calls"
    assert recorder.calls[1]["status"] == "error"
    assert recorder.calls[1]["run_id"] == str(second)
    assert "private-detail" not in json.dumps(recorder.calls)


def test_real_chatopenai_callback_and_wire_request_pair_without_network(monkeypatch):
    module = _module()
    from langchain_openai import ChatOpenAI
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("SDK test attempted network")))
    recorder = module.UsageRecorder()
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "synthetic-test-model",
                                        usage_recorder=recorder)
    class Inner:
        async def handle_async_request(self, request):
            return httpx.Response(200, json={
                "id": "chatcmpl-offline", "object": "chat.completion", "created": 1,
                "model": "synthetic-test-model", "choices": [{"index": 0,
                    "finish_reason": "tool_calls", "message": {"role": "assistant",
                        "content": "Ready", "tool_calls": [{"id": "call-1",
                            "type": "function", "function": {
                                "name": "transfer_to_Mythic_Operator", "arguments": "{}"}}]}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9}},
                request=request)
        async def aclose(self):
            pass
    transport.inner = Inner()
    async def exercise():
        async with httpx.AsyncClient(transport=transport) as client:
            model = ChatOpenAI(model="synthetic-test-model", api_key="offline-only",
                               base_url="http://127.0.0.1:9/v1", http_async_client=client,
                               max_tokens=512, max_retries=0, callbacks=[recorder])
            result = await model.ainvoke([HumanMessage(
                content="What next?", additional_kwargs={
                    "_request_id": "request-2", "_worker_final_answer": True})])
            assert result.content == "Ready"
    asyncio.run(exercise())
    assert len(transport.requests) == len(recorder.calls) == 1
    wire = transport.requests[0]
    callback = recorder.calls[0]
    assert wire["callback_run_id"] == callback["run_id"]
    assert callback["input_messages"][0][0]["additional_kwargs"] == {
        "_request_id": "request-2", "_worker_final_answer": True}
    assert callback["output_messages"][0]["tool_calls"][0]["name"] == "transfer_to_Mythic_Operator"
    assert callback["output_messages"][0]["finish_reason"] == "tool_calls"
    assert wire["request"]["messages"][0]["content"] == "What next?"
    assert wire["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "transfer_to_Mythic_Operator"
    assert "offline-only" not in json.dumps({"wire": wire, "callback": callback})


def test_missing_or_wrong_config_fails_closed_without_secret(tmp_path):
    module = _module()
    config, secret = _config(tmp_path)
    config.write_text(config.read_text().replace("provider=openai", "provider=wrong"))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config)],
                          capture_output=True, text=True, timeout=15, check=False)
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["reason"] == "unsupported_provider"
    assert secret not in proc.stdout + proc.stderr
    with pytest.raises(module.CaseError, match="missing_config_file"):
        module.load_profile(tmp_path / "absent")


def test_runner_uses_sage_tls_bundle_with_explicit_transport_trust(monkeypatch, tmp_path):
    module = _module()
    missing = tmp_path / "missing.pem"
    monkeypatch.setattr(module, "configure_sage_ssl_cert_file", lambda **_kwargs: str(missing))
    with pytest.raises(FileNotFoundError):
        module.sage_tls_context()

    cafile = ssl.get_default_verify_paths().cafile
    assert cafile is not None
    monkeypatch.setattr(module, "configure_sage_ssl_cert_file", lambda **_kwargs: cafile)
    context = module.sage_tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED

    observed = {}
    class Inner:
        async def aclose(self):
            pass
    def transport_factory(*, verify, retries):
        observed.update(verify=verify, retries=retries)
        return Inner()
    monkeypatch.setattr(module.httpx, "AsyncHTTPTransport", transport_factory)
    module.BoundedTransport("https://127.0.0.1:7443/v1", "synthetic-test-model", context)
    assert observed == {"verify": context, "retries": 0}


def test_provider_connection_failure_is_recorded_without_approval_or_effect(monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(OSError("offline")))
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(profile, decision="accept"))
    assert result["status"] == "provider_error"
    assert result["card_count"] == 0
    assert result["effect_calls"] == []
    assert len(result["provider_requests"]) == 1
    assert result["provider_requests"][0]["status"] == "error"


def test_status_keeps_proposal_mismatch_separate_from_later_request_limit():
    module = _module()
    calls = [{"status": "completed"}, {"status": "error"}]
    limits = [{"reason": "model_request_limit"}]
    assert module.case_status(passed=False, exact=False, card_count=1,
                              model_calls=calls, transport_rejections=limits) == "unexpected_proposal"
    assert module.case_status(passed=False, exact=True, card_count=1,
                              model_calls=calls, transport_rejections=limits) == "resource_limit"
    assert module.case_status(passed=False, exact=False, card_count=0,
                              model_calls=calls, transport_rejections=[]) == "provider_error"


def test_paid_run_requires_exact_route_binding_before_network(tmp_path, monkeypatch):
    module = _module()
    config, _ = _config(tmp_path)
    def blocked(_endpoint):
        raise AssertionError("route mismatch reached DNS")
    monkeypatch.setattr(module, "resolved_endpoint_ips", blocked)
    assert module.main(["--run", "--config", str(config), "--expected-model", "wrong",
                        "--expected-endpoint-sha256", "0" * 64]) == 2


@pytest.mark.parametrize("decision", ("accept", "reject", "redirect"))
def test_identity_freshness_full_native_checkpoint_without_network(decision, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted identity case attempted network")))
    proposal = module.CASE.call(module.CASE.EXPECTED["name"],
                                module.CASE.EXPECTED["args"], 3)
    final = ("Task 41 recorded EXAMPLE\\previous-user. Current identity remains unknown. "
             "No new task ran." if decision != "accept" else
             "Task 41 recorded EXAMPLE\\previous-user. The approved fresh check reported "
             "EXAMPLE\\current-user for both identities. Elevation and group membership remain unknown.")
    redirect_seen = []
    def resumed_answer(messages):
        if decision == "redirect":
            redirect_seen.append(any(
                isinstance(message, ToolMessage)
                and module.IDENTITY_REDIRECT in message.content
                for message in messages))
        return AIMessage(content=final)

    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review identity and request fresh check",
            "handoff_instruction": module.IDENTITY_PROMPT}, 1),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 41}, 2),
        proposal,
        resumed_answer,
        AIMessage(content=final),
    ]
    fake = module.CASE.ScriptedModel(messages=iter(script))
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision=decision, scenario=module.IDENTITY_SCENARIO,
        scripted_model=fake))
    assert result["status"] == "passed", result
    assert result["reads_before_decision"] == [41]
    assert result["evidence_reads"] == [41]
    assert result["pre_approval_effect_count"] == 0
    assert result["exact_proposal"] is True
    assert result["native_action"] == ("respond" if decision == "redirect" else decision)
    assert len(result["effect_calls"]) == (1 if decision == "accept" else 0)
    assert "EXAMPLE\\previous-user" in result["terminal_answer_text"]
    assert ("EXAMPLE\\current-user" in result["terminal_answer_text"]) == (decision == "accept")
    assert result["answer_quality"] == "unassessed"
    assert "MagicMock" not in result["terminal_answer_text"]
    assert result["provider_requests"] == []
    json.dumps(result)
    if decision == "redirect":
        assert redirect_seen == [True]


def test_historical_mock_read_reaches_supervisor_as_current_request_evidence(monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted identity case attempted network")))
    observed = {}
    def worker_after_read(messages):
        reads = [message for message in messages if isinstance(message, ToolMessage)
                 and message.name == "get_all_task_output_by_task_id"]
        assert len(reads) == 1
        observed["read"] = json.loads(reads[0].content)
        return module.CASE.call(module.CASE.EXPECTED["name"],
                                module.CASE.EXPECTED["args"], 3)
    def supervisor_after_return(messages):
        observed["references"] = [message for message in messages
            if isinstance(message, AIMessage)
            and message.additional_kwargs.get("_worker_task_references")]
        observed["evidence"] = [message for message in messages
            if isinstance(message, AIMessage)
            and message.additional_kwargs.get("_worker_task_evidence")]
        return AIMessage(content="Task 41 is historical; fresh check reported EXAMPLE\\current-user.")
    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review identity", "handoff_instruction": module.IDENTITY_PROMPT}, 1),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 41}, 2),
        worker_after_read,
        AIMessage(content="Task 41 was historical; fresh check reported EXAMPLE\\current-user."),
        supervisor_after_return,
    ]
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=module.IDENTITY_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))
    assert result["status"] == "passed", result
    provenance = observed["read"]["provenance"]
    assert provenance["task_id"] == 41
    assert provenance["current_request_id"]
    assert provenance["source_request_id"] is None
    assert provenance["origin"] == "historical_or_external"
    assert len(observed["references"]) == len(observed["evidence"]) == 1
    assert observed["references"][0].additional_kwargs["_request_id"] == provenance["current_request_id"]
    assert observed["evidence"][0].additional_kwargs["_request_id"] == provenance["current_request_id"]
    assert "task 41 (historical_or_external)" in observed["references"][0].content
    evidence = json.loads(observed["evidence"][0].content.splitlines()[-2])
    assert evidence[0]["task_id"] == 41
    assert evidence[0]["origin"] == "historical_or_external"
    assert "EXAMPLE\\previous-user" in evidence[0]["response_text_excerpts"][0]


def test_identity_freshness_rejects_drifted_proposal_before_effect(monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted identity case attempted network")))
    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "identity", "handoff_instruction": module.IDENTITY_PROMPT}, 1),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 41}, 2),
        module.CASE.call(module.CASE.EXPECTED["name"], {
            **module.CASE.EXPECTED["args"], "command": "hostname"}, 3),
        AIMessage(content="No new task ran."),
        AIMessage(content="No new task ran."),
    ]
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=module.IDENTITY_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))
    assert result["status"] == "unexpected_proposal", result
    assert result["applied_decision"] == "reject"
    assert result["effect_calls"] == []
    assert result["evidence_reads"] == [41]


@pytest.mark.parametrize("decision", ("accept", "reject"))
def test_identity_freshness_verification_read_requires_actual_effect(decision, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted identity case attempted network")))
    observed = []
    def inspect_read(messages):
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)
                         and m.name == "get_all_task_output_by_task_id"]
        observed.append(tool_messages[-1].content)
        return AIMessage(content="The available evidence has been reported.")
    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "identity", "handoff_instruction": module.IDENTITY_PROMPT}, 1),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 41}, 2),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 41}, 3),
        module.CASE.call(module.CASE.EXPECTED["name"], module.CASE.EXPECTED["args"], 4),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 42}, 5),
        inspect_read,
        AIMessage(content="The available evidence has been reported."),
    ]
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision=decision, scenario=module.IDENTITY_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))
    assert result["status"] == "passed", result
    assert result["evidence_reads"] == [41, 41, 42]
    assert len(result["effect_calls"]) == (1 if decision == "accept" else 0)
    assert len(observed) == 1
    assert ("current-user" in observed[0]) == (decision == "accept")
    if decision == "accept":
        provenance = json.loads(observed[0])["provenance"]
        assert provenance["current_request_id"]
        assert provenance["source_request_id"] == provenance["current_request_id"]
        assert provenance["origin"] == "current_request"
    if decision == "reject":
        assert "mock_task_not_created" in observed[0]


@pytest.mark.parametrize("decision", ("accept", "reject", "redirect"))
@pytest.mark.parametrize("path", ("text_followup", "direct_card"))
def test_identity_v2_scripted_native_decisions(decision, path, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted identity case attempted network")))
    final = ("The approved fresh check reported EXAMPLE\\current-user."
             if decision == "accept" else
             "Task 41 recorded EXAMPLE\\previous-user; current identity remains unknown.")
    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review identity", "handoff_instruction": module.IDENTITY_PROMPT}, 1),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 41}, 2),
    ]
    if path == "text_followup":
        script.extend([
            AIMessage(content="Task 41 is historical; I propose a fresh whoami check."),
            AIMessage(content="Task 41 is historical; a fresh whoami check needs approval."),
            module.CASE.call("transfer_to_Mythic_Operator", {
                "handoff_title": "submit native approval",
                "handoff_instruction": module.IDENTITY_APPROVAL_FOLLOWUP}, 4),
        ])
    observed_issue_outputs = []
    final_supervisor_system = []
    def after_decision(messages):
        if decision == "accept":
            observed_issue_outputs.extend(
                message.content for message in messages if isinstance(message, ToolMessage)
                and message.name == module.CASE.EXPECTED["name"])
        return AIMessage(content=final)
    def final_supervisor(messages):
        final_supervisor_system.extend(
            message.content for message in messages if isinstance(message, SystemMessage))
        return AIMessage(content=final)
    script.extend([
        module.CASE.call(module.CASE.EXPECTED["name"], module.CASE.EXPECTED["args"], 3),
        after_decision,
        final_supervisor,
    ])
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision=decision, scenario=module.IDENTITY_V2_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))
    assert result["status"] == "passed", {key: result.get(key) for key in (
        "status", "selected_path", "followup_sent", "first_terminal_count",
        "approval_turn_terminal_count", "decision_terminal_count", "terminal_count",
        "card_count", "final_card_count", "exact_proposal", "paused_on_checkpoint",
        "card_released", "pre_approval_effect_count", "response_matches_pending",
        "resumed_checkpoint", "effective_effect_calls", "initial_emissions",
        "followup_emissions", "decision_emissions")}
    assert result["selected_path"] == path
    assert result["followup_sent"] == (path == "text_followup")
    assert result["followup_text"] == (
        module.IDENTITY_APPROVAL_FOLLOWUP if path == "text_followup" else None)
    assert result["first_terminal_count"] == (1 if path == "text_followup" else 0)
    assert result["approval_turn_terminal_count"] == 0
    assert result["decision_terminal_count"] == 1
    assert result["evidence_reads"] == [41]
    assert result["pre_approval_effect_count"] == 0
    assert len(result["effect_calls"]) == (1 if decision == "accept" else 0)
    assert result["native_action"] == ("respond" if decision == "redirect" else decision)
    assert result["terminal_answer_text"].strip() == final
    assert result["answer_quality"] == "unassessed"
    assert result["provider_requests"] == []
    runtime_inputs = [content for content in final_supervisor_system
                      if "[supervised-runtime]" in content]
    assert len(runtime_inputs) == 1
    expected_native_decision = {
        "accept": "accepted", "reject": "denied", "redirect": "redirected",
    }[decision]
    assert '"native_decisions":' in runtime_inputs[0]
    assert f'"native_decision":"{expected_native_decision}"' in runtime_inputs[0]
    if decision == "accept":
        assert '"task_id":42' in runtime_inputs[0]
        assert '"task_completed":true' in runtime_inputs[0]
        assert '"event_status":"completed"' in runtime_inputs[0]
        assert observed_issue_outputs == [module.IDENTITY_FRESH_OUTPUT]
        assert result["mock_issue_outputs"] == [module.IDENTITY_FRESH_OUTPUT]
        started, completed = result["execution_events"]
        assert started["event_id"] == completed["event_id"] == "mythic-task:7:42"
        assert started["source"] == completed["source"] == "mythic"
        assert started["request_id"] == completed["request_id"]
        assert started["request_contract_digest"] == completed["request_contract_digest"]
        assert started["approval_id"] == completed["approval_id"]
        assert all(started[key] == completed[key] for key in (
            "callback_id", "task_id", "tool_name", "parameters"))
        assert started["callback_id"] == 7
        assert started["task_id"] == 42
        assert started["tool_name"] == module.CASE.EXPECTED["args"]["command"]
        assert started["status"] == "started"
        assert started["task_completed"] is None
        assert started["task_status"] == ""
        assert "output" not in started
        assert completed["status"] == "completed"
        assert completed["task_completed"] is True
        assert completed["task_status"] == "completed"
        assert completed["terminal_status"] == "completed"
        assert completed["output"] == module.IDENTITY_FRESH_OUTPUT
        expected_observed = {
            "event_id": started["event_id"], "task_id": 42,
            "command": started["tool_name"], "callback_display_id": 7,
            "event_status": "completed", "task_completed": True,
            "task_status": "completed",
        }
        assert len(result["observed_task_events"]) == 1
        assert all(result["observed_task_events"][0].get(key) == value
                   for key, value in expected_observed.items())
        assert result["observed_task_events"][0]["response_text_excerpt"] == module.IDENTITY_FRESH_OUTPUT
        assert result["observed_task_events"][0]["output_truncated"] is False
    else:
        assert '"task_events":[]' in runtime_inputs[0]
        assert observed_issue_outputs == []
        assert result["mock_issue_outputs"] == []
        assert result["execution_events"] == []
        assert result["observed_task_events"] == []


@pytest.mark.parametrize("decision", ("accept", "reject", "redirect"))
def test_identity_v2_scripted_explicit_handback_after_native_decision(
        decision, monkeypatch, tmp_path):
    """Assembled HITL resume returns typed worker evidence, not worker-claimed effects."""
    module = _module()
    import socket
    blocked = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("scripted HITL handback attempted network"))
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.chdir(tmp_path)  # Keep Model's relative SQLite path off the real runtime DB.
    native_decision = {"accept": "accepted", "reject": "denied", "redirect": "redirected"}[decision]
    # Both negative controls deliberately contradict the native decision.
    summary = ("No task 42 was created; fresh identity is unknown."
               if decision == "accept" else
               "Task 42 completed and confirmed EXAMPLE\\current-user."
               if decision == "reject" else
               "No new task ran after redirection; current identity is unknown.")
    final = f"Native decision {native_decision}; report only the observed task state."
    seen = {}

    def handback(_messages):
        return module.CASE.call("handback_to_supervisor", {
            "reason": "Native decision processed", "summary": summary,
            "outcome": "progress", "next_owner": "",
        }, 6)

    def after_decision(messages):
        seen["worker"] = list(messages)
        return (module.CASE.call("get_all_task_output_by_task_id", {"task_id": 42}, 5)
                if decision == "accept" else handback(messages))

    def after_fresh_read(messages):
        seen["fresh_read"] = list(messages)
        return handback(messages)

    def supervisor_synthesis(messages):
        seen["supervisor"] = list(messages)
        return module.CASE.call("respond_to_user", {"final_response": final}, 7)

    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review identity", "handoff_instruction": module.IDENTITY_PROMPT}, 1),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 41}, 2),
        module.CASE.call(module.CASE.EXPECTED["name"], module.CASE.EXPECTED["args"], 3),
        after_decision,
    ]
    if decision == "accept":
        script.append(after_fresh_read)
    script.append(supervisor_synthesis)
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision=decision, scenario=module.IDENTITY_V2_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))

    assert result["status"] == "passed", result
    assert result["selected_path"] == "direct_card"
    assert result["paused_on_checkpoint"] and result["resumed_checkpoint"]
    assert result["card_count"] == result["final_card_count"] == 1
    assert result["pre_approval_effect_count"] == 0
    assert result["decision_terminal_count"] == 1
    assert len(result["effect_calls"]) == (1 if decision == "accept" else 0)
    assert result["evidence_reads"] == ([41, 42] if decision == "accept" else [41])
    assert result["terminal_answer_text"].strip().endswith(final)

    supervisor = seen["supervisor"]  # Actual next model input, not a saved-channel guess.
    runtime = [str(m.content) for m in supervisor if isinstance(m, SystemMessage)
               and "[supervised-runtime]" in str(m.content)]
    summaries = [m for m in supervisor if isinstance(m, AIMessage)
                 and m.additional_kwargs.get("_worker_outcome")]
    references = [m for m in supervisor if isinstance(m, AIMessage)
                  and m.additional_kwargs.get("_worker_task_references")]
    evidence = [m for m in supervisor if isinstance(m, AIMessage)
                and m.additional_kwargs.get("_worker_task_evidence")]
    assert len(runtime) == len(summaries) == len(references) == len(evidence) == 1
    assert f'"native_decision":"{native_decision}"' in runtime[0]
    assert summaries[0].content == summary
    assert summaries[0].additional_kwargs["_worker_outcome"]["outcome"] == "progress"
    historical = json.loads(next(m.content for m in seen["worker"]
                                 if isinstance(m, ToolMessage)
                                 and m.name == "get_all_task_output_by_task_id"))
    request_id = historical["provenance"]["current_request_id"]
    assert historical["provenance"]["origin"] == "historical_or_external"
    assert references[0].additional_kwargs["_request_id"] == request_id
    assert evidence[0].additional_kwargs["_request_id"] == request_id
    origins = [(row["task_id"], row["origin"]) for row in
               json.loads(evidence[0].content.splitlines()[-2])]
    assert origins == ([(41, "historical_or_external"), (42, "current_request")]
                       if decision == "accept" else [(41, "historical_or_external")])
    if decision == "accept":
        assert '"task_id":42' in runtime[0] and '"task_completed":true' in runtime[0]
        assert result["observed_task_events"][-1]["task_id"] == 42
        fresh = json.loads([m.content for m in seen["fresh_read"]
                            if isinstance(m, ToolMessage)
                            and m.name == "get_all_task_output_by_task_id"][-1])
        assert fresh["provenance"]["origin"] == "current_request"
        assert fresh["provenance"]["source_request_id"] == request_id
    else:
        assert '"task_events":[]' in runtime[0]
        assert result["observed_task_events"] == []


@pytest.mark.parametrize("first", ("empty", "error", "wrong_card"))
def test_identity_v2_does_not_follow_up_invalid_first_turn(first, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted identity case attempted network")))
    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review identity", "handoff_instruction": module.IDENTITY_PROMPT}, 1),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 41}, 2),
    ]
    if first == "empty":
        script.extend([
            AIMessage(content=""),
            AIMessage(content=""),
        ])
    elif first == "error":
        def fails(_messages):
            raise RuntimeError("scripted model failure")
        script.append(fails)
    else:
        script.extend([
            module.CASE.call(module.CASE.EXPECTED["name"], {
                **module.CASE.EXPECTED["args"], "command": "hostname"}, 3),
            AIMessage(content="No task ran."), AIMessage(content="No task ran."),
        ])
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=module.IDENTITY_V2_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))
    assert result["status"] != "passed", result
    assert result.get("followup_sent") is not True
    assert result["effect_calls"] == []
    if first == "empty":
        assert result["first_terminal_count"] == 1, result
        assert result["card_count"] == 0
    if first == "wrong_card":
        assert result["applied_decision"] == "reject"
        assert result["selected_path"] == "direct_card"


def test_identity_v2_dry_run_limits(tmp_path, capsys):
    module = _module()
    config, _ = _config(tmp_path)
    assert module.main(["--config", str(config), "--scenario", "identity-freshness-v2"]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["limits"]["requests"] == 10
    assert ready["limits"]["output_tokens_per_request"] == 2048
    assert ready["limits"]["wall_seconds"] == 240


def test_report_netstat_exact_guard_and_dry_run(tmp_path, capsys, monkeypatch):
    module = _module()
    config, _ = _config(tmp_path)
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("report dry run attempted network")))
    assert module.main(["--config", str(config), "--scenario",
                        module.REPORT_FRESH_NETSTAT_SCENARIO]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["limits"]["requests"] == 12
    assert ready["limits"]["output_tokens_per_request"] == 2048
    assert ready["limits"]["observed_tokens"] == 120000
    assert ready["limits"]["wall_seconds"] == 240
    card = {"metadata": {"input_requested": {"data": {"actions": [{
        "tool_name": module.CASE.EXPECTED["name"],
        "arguments": module.REPORT_NETSTAT_ARGS,
    }]}}}}
    assert module.exact_card([card], effective_arguments=module.report_netstat_effective_arguments)[0]
    for changed in ({"callback_display_id": 8}, {"command": "whoami"},
                    {"parameters": '{"command":"netstat -an"}'},
                    {"parameters": '{"command":"netstat -ano","extra":1}'}):
        near = json.loads(json.dumps(card))
        near["metadata"]["input_requested"]["data"]["actions"][0]["arguments"].update(changed)
        assert not module.exact_card(
            [near], effective_arguments=module.report_netstat_effective_arguments)[0]


@pytest.mark.parametrize("decision", ("accept", "reject", "redirect"))
@pytest.mark.parametrize("handback", (False, True))
def test_report_fresh_native_task_evidence_boundary(decision, handback, monkeypatch, tmp_path):
    module = _module()
    import socket
    blocked = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("scripted report case attempted network"))
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.chdir(tmp_path)
    seen = {}
    def worker_after_decision(messages):
        seen["worker"] = list(messages)
        if handback:
            return module.CASE.call("handback_to_supervisor", {
                "reason": "Synthetic task decision processed",
                "summary": "Report the observed task state and its evidence bounds.",
                "outcome": "complete", "next_owner": "",
            }, 3)
        return AIMessage(content="Report the observed task state and its evidence bounds.")
    def supervisor_after_decision(messages):
        seen["supervisor"] = list(messages)
        return module.CASE.call("respond_to_user", {
            "final_response": "Native decision recorded; report only supported observations."}, 4)
    def report_followup(messages):
        seen["report"] = list(messages)
        return module.CASE.call("respond_to_user", {
            "final_response": "The synthetic netstat rows are retained; service identity and reachability are unknown."}, 5)
    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "synthetic network observation",
            "handoff_instruction": module.REPORT_FRESH_PROMPT,
        }, 1),
        module.CASE.call(module.CASE.EXPECTED["name"], module.REPORT_NETSTAT_ARGS, 2),
        worker_after_decision, supervisor_after_decision,
    ]
    if decision == "accept":
        script.append(report_followup)
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision=decision, scenario=module.REPORT_FRESH_NETSTAT_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))
    assert result["status"] == "passed", result
    assert result["structural_success"]
    assert result["report_sent"] == (decision == "accept")
    assert result["effect_calls"] == ([module.REPORT_NETSTAT_ARGS] if decision == "accept" else [])
    assert result["mock_issue_outputs"] == ([module.REPORT_FRESH_OUTPUT] if decision == "accept" else [])
    assert len(result["execution_events"]) == (2 if decision == "accept" else 0)
    if decision == "accept":
        started, completed = result["execution_events"]
        assert started["event_id"] == completed["event_id"] == "mythic-task:7:42"
        assert started["request_id"] == completed["request_id"]
        assert started["approval_id"] == completed["approval_id"]
        assert completed["output"] == module.REPORT_FRESH_OUTPUT
    supervisor = seen["supervisor"]
    runtime = [str(m.content) for m in supervisor if isinstance(m, SystemMessage)
               and "[supervised-runtime]" in str(m.content)]
    assert len(runtime) == 1
    assert "Active Connections" not in runtime[0]
    evidence = [m for m in supervisor if isinstance(m, AIMessage)
                and m.additional_kwargs.get("_worker_task_evidence")]
    if decision == "accept":
        assert len(evidence) == 1
        rows = json.loads(evidence[0].content.splitlines()[-2])
        accepted = [row for row in rows if row.get("source") == "accepted_task_event"]
        assert len(accepted) == 1
        row = accepted[0]
        assert row["task_id"] == 42
        assert row["callback_display_id"] == 7
        assert row["logical_command"] == "shell"
        assert row["request_id"] == row["source_request_id"]
        assert row["origin"] == "current_request"
        assert len(row["response_text_excerpts"]) == 1
        assert "Active Connections" in row["response_text_excerpts"][0]
        assert "TIME_WAIT" in row["response_text_excerpts"][0]
        assert len(row["response_text_excerpts"][0]) <= 4096
        later_evidence = [m for m in seen["report"] if isinstance(m, AIMessage)
                          and m.additional_kwargs.get("_worker_task_evidence")]
        assert later_evidence
        later_rows = [r for m in later_evidence
                      for r in json.loads(m.content.splitlines()[-2])
                      if r.get("source") == "accepted_task_event"]
        assert len(later_rows) == 1
        assert later_rows[0]["source_request_id"] == row["source_request_id"]
        assert later_rows[0]["task_id"] == 42
    else:
        assert not evidence


@pytest.mark.parametrize("scenario", (
    "report-accuracy-historical-withdrawal-v1",
    "report-accuracy-historical-alt-v1",
))
def test_report_historical_withdrawal_recap_assembled(scenario, monkeypatch, tmp_path):
    module = _module()
    import socket
    blocked = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("scripted historical report attempted network"))
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.chdir(tmp_path)
    seen = {}
    def first_worker(messages):
        seen["worker"] = list(messages)
        return AIMessage(content="Both historical records are available, with separate provenance.")
    def first_supervisor(messages):
        seen["first"] = list(messages)
        return AIMessage(content="Historical observations reported with source bounds.")
    def second_supervisor(messages):
        seen["second"] = list(messages)
        return AIMessage(content="Task 62 is withdrawn; its observations are no longer relied on.")
    def third_supervisor(messages):
        seen["third"] = list(messages)
        return AIMessage(content="The remaining network rows are historical; other claims remain unknown.")
    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review historical network records",
            "handoff_instruction": module.REPORT_HISTORY_PROMPT,
        }, 1),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 61}, 2),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 62}, 3),
        first_worker, first_supervisor, second_supervisor, third_supervisor,
    ]
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=scenario,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))
    assert result["status"] == "passed", result
    assert result["structural_success"]
    assert result["followup_sent"] and result["third_sent"]
    assert result["evidence_reads"] == [61, 62]
    assert result["effect_calls"] == []
    assert result["card_count"] == 0
    assert result["answer_quality"] == "unassessed"
    assert result["prompt"] == module.REPORT_HISTORY_PROMPT
    assert result["followup_text"] == module.REPORT_HISTORY_WITHDRAWAL
    assert result["third_input_text"] == module.REPORT_HISTORY_RECAP
    records = [json.loads(m.content) for m in seen["worker"]
               if isinstance(m, ToolMessage) and m.name == "get_all_task_output_by_task_id"]
    assert [r["provenance"]["task_id"] for r in records] == [61, 62]
    assert {r["provenance"]["current_request_id"] for r in records} == {
        result["evidence_read_events"][0]["current_request_id"]}
    assert all(r["provenance"]["origin"] == "historical_or_external" for r in records)
    for record, task_id in zip(records, (61, 62)):
        output = record["task_output"][0]
        assert output["response_text"] == module.RECON_CASE_OUTPUTS[scenario][task_id]
        assert output["timestamp"] and output["id"]
        assert output["task"]["display_id"] == task_id
        assert output["task"]["callback"]["display_id"] == 7
        assert output["task"]["status"] == "completed"
        assert output["task"]["completed"] is True
    assert seen["first"] and seen["second"] and seen["third"]


def test_report_fresh_optional_readback_uses_netstat_not_identity(monkeypatch, tmp_path):
    module = _module()
    import socket
    blocked = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("scripted report readback attempted network"))
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.chdir(tmp_path)
    seen = {}
    def after_read(messages):
        records = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)
                   and m.name == "get_all_task_output_by_task_id"]
        seen["readback"] = records[-1]
        return AIMessage(content="Current task output is available for synthesis.")
    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "network check", "handoff_instruction": module.REPORT_FRESH_PROMPT,
        }, 1),
        module.CASE.call(module.CASE.EXPECTED["name"], module.REPORT_NETSTAT_ARGS, 2),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 42}, 3),
        after_read,
        module.CASE.call("respond_to_user", {"final_response": "Observed task reported."}, 4),
        module.CASE.call("respond_to_user", {"final_response": "Bounded network handoff."}, 5),
    ]
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=module.REPORT_FRESH_NETSTAT_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))
    assert result["status"] == "passed", result
    assert result["evidence_reads"] == [42]
    record = seen["readback"]
    assert record["task_output"][0]["response_text"] == module.REPORT_FRESH_OUTPUT
    assert record["task_output"][0]["task"]["command_name"] == "shell"
    assert record["provenance"]["origin"] == "current_request"
    assert "EXAMPLE\\current-user" not in json.dumps(record)


def test_identity_v2_wrapper_alone_is_not_a_proposal():
    module = _module()
    assert not module.substantive_text([{
        "kind": "complete", "content": "\n📊 **Final Report**\n", "complete_request": True,
    }])
    assert module.substantive_text([{
        "kind": "complete", "content": "📊 **Final Report**\nTask 41 is historical.",
        "complete_request": True,
    }])


def test_identity_v2_wrong_followup_card_is_rejected_without_effect(monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted identity case attempted network")))
    script = [
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review identity", "handoff_instruction": module.IDENTITY_PROMPT}, 1),
        module.CASE.call("get_all_task_output_by_task_id", {"task_id": 41}, 2),
        AIMessage(content="Task 41 is historical; a fresh check needs approval."),
        AIMessage(content="Task 41 is historical; a fresh check needs approval."),
        module.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "native approval",
            "handoff_instruction": module.IDENTITY_APPROVAL_FOLLOWUP}, 3),
        module.CASE.call(module.CASE.EXPECTED["name"], {
            **module.CASE.EXPECTED["args"], "command": "hostname"}, 4),
        AIMessage(content="Current identity remains unknown."),
        AIMessage(content="Current identity remains unknown."),
    ]
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=module.IDENTITY_V2_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(script))))
    assert result["status"] == "unexpected_proposal", result
    assert result["selected_path"] == "text_followup"
    assert result["followup_sent"] is True
    assert result["applied_decision"] == "reject"
    assert result["first_terminal_count"] == 1
    assert result["approval_turn_terminal_count"] == 0
    assert result["decision_terminal_count"] == 1


def _recon_script(module, *, first_variant="normal", second_variant="normal",
                  observations=None, explicit_return=False, second_reply=None,
                  third_reply=None, second_response_tool_args=None):
    script = [module.CASE.call("transfer_to_Mythic_Operator", {
        "handoff_title": "review stored inventory", "handoff_instruction": module.RECON_PROMPT}, 1)]
    if first_variant != "missing_read":
        script.append(module.CASE.call("get_all_task_output_by_task_id", {"task_id": 51}, 2))
    script.append(module.CASE.call("get_all_task_output_by_task_id", {
        "task_id": 999 if first_variant == "unknown_id" else 52}, 3))
    if first_variant == "first_card":
        script.append(module.CASE.call(module.CASE.EXPECTED["name"],
                                       module.CASE.EXPECTED["args"], 4))
    elif first_variant == "error":
        def fail(_messages):
            raise RuntimeError("synthetic provider failure")
        script.append(fail)
    else:
        if explicit_return and first_variant == "normal":
            script.append(module.CASE.call("handback_to_supervisor", {
                "reason": "Stored inventory reviewed",
                "summary": "Two historical inventory records reviewed; share visibility is incomplete.",
                "outcome": "complete", "next_owner": "",
            }, 4))
        else:
            script.append(AIMessage(content="" if first_variant == "empty" else
                                    "Inventory records received; share visibility is incomplete."))
        def first_supervisor(messages):
            if observations is not None:
                observations.append(("first", messages))
            if first_variant == "reuse":
                return module.CASE.call("respond_to_user", {
                    "reuse_latest_worker_answer": True}, 4)
            return AIMessage(content="" if first_variant in (
                "empty", "empty_after_narration") else
                "The historical inventory suggests shares to inspect; access is unknown.")
        script.append(first_supervisor)
    if first_variant in ("normal", "reuse", "empty_after_narration"):
        if second_variant == "read":
            script.extend([
                module.CASE.call("transfer_to_Mythic_Operator", {
                    "handoff_title": "retrieve more", "handoff_instruction": "read inventory"}, 5),
                module.CASE.call("get_all_task_output_by_task_id", {"task_id": 51}, 6),
                AIMessage(content="Read again."),
            ])
        elif second_variant == "card":
            script.extend([
                module.CASE.call("transfer_to_Mythic_Operator", {
                    "handoff_title": "issue check", "handoff_instruction": "issue task"}, 5),
                module.CASE.call(module.CASE.EXPECTED["name"], module.CASE.EXPECTED["args"], 6),
            ])
        elif second_variant == "error":
            def fail_second(_messages):
                raise RuntimeError("synthetic second-turn provider failure")
            script.append(fail_second)
        elif second_variant == "empty_after_narration":
            script.extend([
                module.CASE.call("transfer_to_Mythic_Operator", {
                    "handoff_title": "recap prior output",
                    "handoff_instruction": "Recap without another read.",
                }, 5),
                AIMessage(content="Earlier worker narration from historical records."),
                AIMessage(content=""),
            ])
        else:
            def second_supervisor(messages):
                if observations is not None:
                    observations.append(("second", messages))
                if second_response_tool_args is not None:
                    return module.CASE.call("respond_to_user", second_response_tool_args, 5)
                return AIMessage(content="" if second_variant == "empty" else second_reply or
                                 "Confirmed inventory: filesrv-01 and two listed shares. "
                                 "Completeness, permissions, and access remain unknown.")
            script.append(second_supervisor)
            if third_reply is not None:
                def third_supervisor(messages):
                    if observations is not None:
                        observations.append(("third", messages))
                    return AIMessage(content=third_reply)
                script.append(third_supervisor)
    return script


@pytest.mark.parametrize("first_variant,second_variant,expected", [
    ("normal", "normal", "passed"),
    ("reuse", "normal", "passed"),
    ("missing_read", "normal", "failed"),
    ("first_card", "normal", "unexpected_proposal"),
    ("empty", "normal", "failed"),
    ("empty_after_narration", "normal", "failed"),
    ("error", "normal", "failed"),
    ("unknown_id", "normal", "failed"),
    ("normal", "read", "failed"),
    ("normal", "card", "unexpected_proposal"),
    ("normal", "empty_after_narration", "failed"),
])
def test_recon_consultation_assembled_two_turn_boundary(
        first_variant, second_variant, expected, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted recon case attempted network")))
    observed = []
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=module.RECON_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(_recon_script(
            module, first_variant=first_variant, second_variant=second_variant,
            observations=observed)))))
    assert result["status"] == expected, result
    assert result["protocol_valid"] is True
    assert result["protocol_invalid_reasons"] == []
    assert result["effect_calls"] == []
    assert result["provider_requests"] == []
    if first_variant not in ("normal", "reuse"):
        assert result.get("followup_sent") is not True
        if first_variant == "empty_after_narration":
            assert result["initial_emissions"][-1]["content"].strip() == "Completed."
            assert result["model_calls"][result["turn_counts"][0]["model_calls"] - 1][
                "output_messages"][-1]["content"] == ""
    elif second_variant == "empty_after_narration":
        assert result["followup_sent"] is True
        assert result["followup_emissions"][-1]["content"].strip() == "Completed."
        assert result["model_calls"][-1]["output_messages"][-1]["content"] == ""
        assert result.get("third_sent") is not True
    elif second_variant == "normal":
        assert result["structural_success"] is True
        assert result["answer_quality"] == "unassessed"
        assert result["first_terminal_count"] == result["second_terminal_count"] == 1
        assert result["card_count"] == 0
        assert result["evidence_reads"] == [51, 52]
        assert [event["task_id"] for event in result["evidence_read_events"]] == [51, 52]
        assert all(event["current_request_id"] for event in result["evidence_read_events"])
        assert result["reads_after_first"] == len(result["evidence_read_events"])
        assert [turn for turn, _ in observed] == ["first", "second"]
        first_messages = json.dumps([str(message.content) for message in observed[0][1]])
        assert "filesrv-01" in first_messages
        assert "Public" in first_messages and "Reports" in first_messages
        assert "historical_or_external" in first_messages
        second_messages = json.dumps([str(message.content) for message in observed[1][1]])
        assert module.RECON_FOLLOWUP in second_messages


def test_recon_consultation_dry_run_limits(tmp_path, capsys):
    module = _module()
    config, _ = _config(tmp_path)
    assert module.main(["--config", str(config), "--scenario", module.RECON_SCENARIO]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["limits"] == {"requests": 12, "output_tokens_per_request": 2048,
                               "request_seconds": 30, "wall_seconds": 240,
                               "observed_tokens": 120000}


@pytest.mark.parametrize("scenario", ["recon-consultation-v1",
                                      "recon-consultation-acl-v1",
                                      "recon-consultation-host-conflict-v1"])
def test_recon_variants_use_original_two_turn_no_effect_path(scenario, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted recon case attempted network")))
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=scenario,
        scripted_model=module.CASE.ScriptedModel(messages=iter(_recon_script(
            module, explicit_return=True)))))
    assert result["status"] == "passed", result
    assert result["structural_success"] is True
    assert result["answer_quality"] == "unassessed"
    assert result["prompt"] == module.RECON_PROMPT
    assert result["followup_text"] == module.RECON_FOLLOWUP
    assert result["effect_calls"] == []
    assert result["evidence_reads"] == [51, 52]
    turns = result["turn_counts"]
    assert [turn["turn"] for turn in turns] == [1, 2]
    assert [turn["evidence_reads"] for turn in turns] == [2, 0]
    assert [turn["provider_requests"] for turn in turns] == [0, 0]
    assert [turn["model_calls"] for turn in turns] == [5, 1]
    assert [len(turn["explicit_handbacks"]) for turn in turns] == [1, 0]
    assert [len(turn["explicit_delegations"]) for turn in turns] == [1, 0]
    assert result["explicit_handbacks"] == turns[0]["explicit_handbacks"]
    assert result["explicit_handbacks"][0]["tool_name"] == "handback_to_supervisor"
    assert result["explicit_delegations"][0]["tool_name"] == "transfer_to_Mythic_Operator"
    outputs = result["synthetic_historical_outputs"]
    assert json.loads(outputs[51])["host"] == "filesrv-01"
    share_record = json.loads(outputs[52])
    if scenario == module.RECON_SCENARIO:
        assert outputs == {51: '{"host":"filesrv-01","role":"file_server"}',
                           52: '{"host":"filesrv-01","shares":["Public","Reports"],"complete":false,"next_page":2}'}
        assert "access_observation" not in share_record
    elif scenario == module.RECON_ACL_SCENARIO:
        assert share_record["host"] == "filesrv-01"
        assert share_record["access_observation"] == {
            "share": "Public", "principal": "EXAMPLE\\Analysts", "access": "read",
            "scope": "observed ACL entry only"}
        assert share_record["complete"] is False
        assert "permissions" not in share_record
    else:
        assert share_record["host"] == "filesrv-02"
        assert share_record["shares"] == ["Public", "Reports"]
        assert "access_observation" not in share_record


@pytest.mark.parametrize("scenario,outputs,followup,reply", [
    ("recon-consultation-source-withdrawal-v1", "RECON_OUTPUTS",
     "RECON_WITHDRAWAL_FOLLOWUP",
     "Task 51 identifies filesrv-01 as a file server; task 52 was withdrawn, so shares remain unconfirmed."),
    ("recon-consultation-access-question-v1", "RECON_ACL_OUTPUTS",
     "RECON_ACCESS_QUESTION_FOLLOWUP",
     "No. The observed Public ACL entry for EXAMPLE\\Analysts does not prove effective current read access."),
    ("recon-consultation-stop-v1", "RECON_OUTPUTS", "RECON_STOP_FOLLOWUP",
     "Acknowledged; analysis and checks are deferred."),
])
def test_recon_followup_steering_cases_use_same_guarded_two_turn_path(
        scenario, outputs, followup, reply, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted recon steering attempted network")))
    observed = []
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=scenario,
        scripted_model=module.CASE.ScriptedModel(messages=iter(_recon_script(
            module, observations=observed, explicit_return=True, second_reply=reply)))))
    assert result["status"] == "passed", result
    assert result["structural_success"] is True
    assert result["answer_quality"] == "unassessed"
    assert result["prompt"] == module.RECON_PROMPT
    assert result["followup_text"] == getattr(module, followup)
    assert result["synthetic_historical_outputs"] == getattr(module, outputs)
    assert result["limits"] == {"requests": 12, "output_tokens_per_request": 2048,
                                "request_seconds": 30, "wall_seconds": 240,
                                "observed_tokens": 120000}
    assert result["effect_calls"] == []
    assert result["evidence_reads"] == [51, 52]
    assert [turn["model_calls"] for turn in result["turn_counts"]] == [5, 1]
    assert [turn["evidence_reads"] for turn in result["turn_counts"]] == [2, 0]
    assert [len(turn["explicit_handbacks"]) for turn in result["turn_counts"]] == [1, 0]
    assert [turn for turn, _messages in observed] == ["first", "second"]
    second_input = "\n".join(str(message.content) for message in observed[1][1])
    assert getattr(module, followup) in second_input
    assert result["followup_emissions"][-1]["content"].strip() == reply


def test_recon_steering_case_inputs_are_fixed():
    module = _module()
    assert module.RECON_WITHDRAWAL_FOLLOWUP == (
        "Correction: task 52's share inventory was misfiled and is unreliable. Withdraw it as "
        "evidence. Revise the evidence-only handoff using task 51's host inventory. Do not treat "
        "Public, Reports, or any share as confirmed. State what remains known and unknown. "
        "Do not retrieve more evidence or issue tasks.")
    assert module.RECON_ACCESS_QUESTION_FOLLOWUP == (
        "Based only on the stored records, can you conclude that EXAMPLE\\Analysts can currently "
        "read Public? Answer in at most three sentences. Distinguish the observed ACL entry "
        "from effective current access. Do not retrieve more evidence or issue tasks.")
    assert module.RECON_STOP_FOLLOWUP == (
        "Stop this analysis. Defer all checks. Acknowledge in one sentence; no recommendations, "
        "evidence retrieval or tasks.")
    assert module.RECON_CASE_OUTPUTS[module.RECON_WITHDRAWAL_SCENARIO] == module.RECON_OUTPUTS
    assert module.RECON_CASE_OUTPUTS[module.RECON_ACCESS_QUESTION_SCENARIO] == module.RECON_ACL_OUTPUTS
    assert module.RECON_CASE_OUTPUTS[module.RECON_STOP_SCENARIO] == module.RECON_OUTPUTS


@pytest.mark.parametrize("scenario", [
    "recon-consultation-source-withdrawal-v1",
    "recon-consultation-access-question-v1",
    "recon-consultation-stop-v1",
    "recon-consultation-source-withdrawal-recap-v1",
])
def test_recon_steering_dry_run_keeps_original_limits(scenario, tmp_path, capsys, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("dry run attempted network")))
    config, _ = _config(tmp_path)
    assert module.main(["--config", str(config), "--scenario", scenario]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["scenario"] == scenario
    assert ready["limits"] == {"requests": 12, "output_tokens_per_request": 2048,
                               "request_seconds": 30, "wall_seconds": 240,
                               "observed_tokens": 120000}


def test_recon_source_withdrawal_recap_assembled_three_turns(monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted recon recap attempted network")))
    observed = []
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    second_reply = "Task 52 was withdrawn; task 51 identifies filesrv-01 as a file server."
    third_reply = "Task 51 confirms filesrv-01 is recorded as a file server. Shares are unconfirmed."
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=module.RECON_WITHDRAWAL_RECAP_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(_recon_script(
            module, observations=observed, explicit_return=True,
            second_reply=second_reply, third_reply=third_reply)))))
    assert result["status"] == "passed", result
    assert result["answer_quality"] == "unassessed"
    assert result["prompt"] == module.RECON_PROMPT
    assert result["synthetic_historical_outputs"] == module.RECON_OUTPUTS
    assert result["followup_text"] == module.RECON_WITHDRAWAL_FOLLOWUP
    assert result["third_input_text"] == module.RECON_WITHDRAWAL_RECAP_PROMPT
    assert result["followup_sent"] is result["third_sent"] is True
    assert [result[name] for name in ("first_terminal_count", "second_terminal_count",
                                      "third_terminal_count")] == [1, 1, 1]
    assert result["third_emissions"][-1]["content"].strip() == third_reply
    assert result["evidence_reads"] == [51, 52]
    assert result["effect_calls"] == []
    assert result["card_count"] == 0
    assert [turn["turn"] for turn in result["turn_counts"]] == [1, 2, 3]
    assert [turn["model_calls"] for turn in result["turn_counts"]] == [5, 1, 1]
    assert [turn["evidence_reads"] for turn in result["turn_counts"]] == [2, 0, 0]
    assert [len(turn["explicit_handbacks"]) for turn in result["turn_counts"]] == [1, 0, 0]
    assert [len(turn["explicit_delegations"]) for turn in result["turn_counts"]] == [1, 0, 0]
    assert [turn for turn, _messages in observed] == ["first", "second", "third"]
    third_input = "\n".join(str(message.content) for message in observed[2][1])
    assert module.RECON_WITHDRAWAL_FOLLOWUP in third_input
    assert second_reply in third_input
    assert module.RECON_WITHDRAWAL_RECAP_PROMPT in third_input


def test_recon_recap_accepts_provider_shaped_respond_to_user(monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted recon recap attempted network")))
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    observed = []
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=module.RECON_WITHDRAWAL_RECAP_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(_recon_script(
            module, observations=observed, explicit_return=True,
            second_response_tool_args={"final_response": "Task 51 remains; task 52 is withdrawn."},
            third_reply="Task 51 records the host and role; no shares are confirmed.")))))
    assert result["status"] == "passed", result
    second_call = result["model_calls"][result["turn_counts"][0]["model_calls"]]
    assert second_call["output_messages"][-1]["content"] == ""
    assert second_call["output_messages"][-1]["tool_calls"][0]["name"] == "respond_to_user"
    assert result["third_sent"] is True
    assert [turn for turn, _messages in observed] == ["first", "second", "third"]


def test_recap_last_model_answer_shape_excludes_earlier_narration():
    module = _module()
    earlier = {"status": "completed", "output_messages": [{"content": "Narration"}]}
    def last(content="", tool_calls=None):
        return {"status": "completed", "output_messages": [{
            "content": content, "tool_calls": tool_calls or []}]}
    assert module.completed_terminal_model_answer([earlier, last()]) is False
    assert module.completed_terminal_model_answer([last(tool_calls=[{
        "name": "respond_to_user", "args": {"final_response": "Revised finding"}}])]) is True
    assert module.completed_terminal_model_answer([last(tool_calls=[{
        "name": "respond_to_user", "args": {"reuse_latest_worker_answer": True}}])]) is True
    assert module.completed_terminal_model_answer([last(tool_calls=[{
        "name": "respond_to_user", "args": {"final_response": ""}}])]) is False
    assert module.completed_terminal_model_answer([last(tool_calls=[{
        "name": "transfer_to_Mythic_Operator", "args": {}}])]) is False


@pytest.mark.parametrize("second_variant", ["empty", "empty_after_narration", "error", "card"])
def test_recon_source_withdrawal_recap_requires_second_terminal(
        second_variant, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted recon recap attempted network")))
    observed = []
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=module.RECON_WITHDRAWAL_RECAP_SCENARIO,
        scripted_model=module.CASE.ScriptedModel(messages=iter(_recon_script(
            module, observations=observed, explicit_return=True,
            second_variant=second_variant, third_reply="unreachable")))))
    assert result["status"] != "passed", result
    assert result["third_sent"] is False
    assert result["third_input_text"] is None
    assert result["third_emissions"] == []
    assert "third" not in [turn for turn, _messages in observed]
    assert result["effect_calls"] == []


def test_recon_recap_case_inputs_are_fixed():
    module = _module()
    assert module.RECON_CASE_OUTPUTS[module.RECON_WITHDRAWAL_RECAP_SCENARIO] == module.RECON_OUTPUTS
    assert module.RECON_CASE_FOLLOWUPS[module.RECON_WITHDRAWAL_RECAP_SCENARIO] == module.RECON_WITHDRAWAL_FOLLOWUP
    assert module.RECON_WITHDRAWAL_RECAP_PROMPT == (
        "What are the current confirmed findings? Answer in at most two sentences using only "
        "the evidence that remains reliable after my correction. Do not retrieve evidence, "
        "issue tasks, propose checks, or make recommendations.")


def test_recon_three_turn_count_boundaries_use_actual_tool_calls():
    module = _module()
    calls = [
        {"output_messages": [{"tool_calls": [{"name": "transfer_to_Mythic_Operator"}]}]},
        {"output_messages": [{"tool_calls": []}]},
        {"output_messages": [{"tool_calls": [{"name": "handback_to_supervisor"}]}]},
        {"output_messages": [{"tool_calls": []}]},
        {"output_messages": [{"tool_calls": []}]},
        {"output_messages": [{"tool_calls": []}]},
        {"output_messages": [{"tool_calls": []}]},
    ]
    turns = module.recon_turn_counts(
        calls, [{}] * 7, [{"task_id": 51}, {"task_id": 52}],
        call_boundary=4, request_boundary=4, read_boundary=2,
        second_boundary=(6, 6, 2))
    assert [turn["model_calls"] for turn in turns] == [4, 2, 1]
    assert [turn["provider_requests"] for turn in turns] == [4, 2, 1]
    assert [turn["evidence_reads"] for turn in turns] == [2, 0, 0]
    assert [len(turn["explicit_delegations"]) for turn in turns] == [1, 0, 0]
    assert [len(turn["explicit_handbacks"]) for turn in turns] == [1, 0, 0]


@pytest.mark.parametrize("scenario", [
    "recon-consultation-source-withdrawal-v1",
    "recon-consultation-access-question-v1",
    "recon-consultation-stop-v1",
])
def test_recon_steering_followup_waits_for_complete_first_turn(scenario, monkeypatch):
    module = _module()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("scripted recon steering attempted network")))
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(
        profile, decision="accept", scenario=scenario,
        scripted_model=module.CASE.ScriptedModel(messages=iter(_recon_script(
            module, first_variant="missing_read", explicit_return=True)))))
    assert result["status"] == "failed", result
    assert result["followup_sent"] is False
    assert result["followup_text"] is None
    assert result["followup_emissions"] == []
    assert result["evidence_reads"] == [52]
    assert result["effect_calls"] == []


def test_explicit_handback_metric_excludes_delegation_and_prose():
    module = _module()
    calls = [
        {"output_messages": [{"content": "I handed back", "tool_calls": [{
            "name": "transfer_to_Mythic_Operator"}]}]},
        {"output_messages": [{"content": "", "tool_calls": [{
            "name": "handback_to_supervisor"}]}]},
        {"output_messages": [{"content": "handback_to_supervisor", "tool_calls": []}]},
    ]
    assert module.explicit_handbacks(calls) == [
        {"model_call_index": 2, "tool_name": "handback_to_supervisor"}]
    assert module.explicit_delegations(calls) == [
        {"model_call_index": 1, "tool_name": "transfer_to_Mythic_Operator"}]


def test_utc_deadline_is_absolute_and_checked_before_network(tmp_path, capsys):
    module = _module()
    config, _ = _config(tmp_path)
    assert module.main(["--config", str(config), "--utc-deadline", "2026-09-26T17:22:30Z"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run_ready"
    with pytest.raises(module.CaseError, match="invalid_utc_deadline"):
        module.parse_utc_deadline("2026-09-26T17:22:30")
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "synthetic-test-model",
                                        utc_deadline=expired)
    class Inner:
        def __init__(self):
            self.sent = 0
        async def handle_async_request(self, request):
            self.sent += 1
            return httpx.Response(200, json={}, request=request)
    inner = Inner()
    transport.inner = inner
    request = httpx.Request("POST", transport.endpoint,
                            json={"model": "synthetic-test-model", "max_tokens": 512})
    async def exercise():
        with pytest.raises(module.CaseError, match="utc_deadline_expired"):
            await transport.handle_async_request(request)
        transport.utc_deadline = future
        await transport.handle_async_request(request)
    asyncio.run(exercise())
    assert inner.sent == 1
    assert transport.rejections == [{"reason": "utc_deadline_expired"}]
    assert module.remaining_utc_seconds(future) > 0


def test_expired_utc_deadline_blocks_paid_cli_before_dns(tmp_path, monkeypatch, capsys):
    module = _module()
    config, secret = _config(tmp_path)
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("expired deadline attempted DNS")))
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert module.main(["--run", "--config", str(config),
                        "--scenario", module.RECON_ACL_SCENARIO,
                        "--utc-deadline", expired,
                        "--expected-model", "synthetic-test-model",
                        "--expected-endpoint-sha256", hashlib.sha256(
                            b"https://127.0.0.1:7443/v1").hexdigest()]) == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "configuration_error",
                                  "reason": "utc_deadline_expired"}
    assert secret not in output


@pytest.mark.parametrize("usage,reason", [
    ([], "unknown_observed_usage"),
    ([{"status": "completed", "usage": [{}]}], "unknown_observed_usage"),
    ([{"status": "completed", "usage": [{"total_tokens": 120000}]}], "observed_token_limit"),
])
def test_recon_transport_stops_before_next_request_without_budget(
        usage, reason):
    module = _module()
    recorder = module.UsageRecorder()
    recorder.calls.extend(usage)
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "synthetic-test-model",
                                        usage_recorder=recorder, max_observed_tokens=120000,
                                        output_tokens=2048)
    class Inner:
        def __init__(self):
            self.sent = 0
        async def handle_async_request(self, request):
            self.sent += 1
            return httpx.Response(200, json={})
    transport.inner = Inner()
    request = httpx.Request("POST", transport.endpoint,
                            json={"model": "synthetic-test-model", "max_tokens": 2048})
    async def exercise():
        await transport.handle_async_request(request)
        with pytest.raises(module.CaseError, match=reason):
            await transport.handle_async_request(request)
    asyncio.run(exercise())
    assert transport.inner.sent == 1
    assert transport.rejections == [{"reason": reason, **(
        {"observed_tokens": 120000} if reason == "observed_token_limit" else {})}]


def test_recon_transport_allows_next_request_below_observed_token_cap():
    module = _module()
    recorder = module.UsageRecorder()
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "synthetic-test-model",
                                        usage_recorder=recorder, max_observed_tokens=120000,
                                        output_tokens=2048)
    class Inner:
        def __init__(self):
            self.sent = 0
        async def handle_async_request(self, request):
            self.sent += 1
            return httpx.Response(200, json={})
    transport.inner = Inner()
    request = httpx.Request("POST", transport.endpoint,
                            json={"model": "synthetic-test-model", "max_tokens": 2048})
    async def exercise():
        await transport.handle_async_request(request)
        recorder.calls.append({"status": "completed", "usage": [{"total_tokens": 119999}]})
        await transport.handle_async_request(request)
    asyncio.run(exercise())
    assert transport.inner.sent == 2
    assert transport.rejections == []


def _monetary_guard(module, *, limit="15"):
    return module.MonetaryGuard(
        limit_usd=Decimal(limit), max_input_tokens=1_000_000,
        max_input_rate_per_million=Decimal("11"),
        output_rate_per_million=Decimal("30"), output_tokens=2048)


def test_opus_override_dry_run_budget_and_profile_unchanged(tmp_path, capsys, monkeypatch):
    module = _module()
    config, secret = _config(tmp_path)
    original = config.read_bytes()
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("model override dry run attempted network")))
    args = ["--config", str(config), "--scenario", module.REPORT_FRESH_NETSTAT_SCENARIO,
            "--model-override", "bedrock-claude-4-6-opus",
            "--episode-dollar-limit", "15", "--max-input-tokens", "1000000",
            "--max-input-rate-usd-per-million", "11",
            "--output-rate-usd-per-million", "30"]
    assert module.main(args) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["model"] == "bedrock-claude-4-6-opus"
    assert ready["monetary_guard"]["per_request_reservation_usd"] == "11.06144"
    assert ready["monetary_guard"]["limit_usd"] == "15"
    assert ready["limits"]["requests"] == 12
    assert config.read_bytes() == original
    assert secret not in json.dumps(ready)
    assert module.main(args + ["--run", "--expected-model", "synthetic-test-model",
                               "--expected-endpoint-sha256", "0" * 64]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "route_binding_mismatch"
    assert config.read_bytes() == original


@pytest.mark.parametrize("values", [
    {"episode_dollar_limit": "15"},
    {"episode_dollar_limit": "NaN", "max_input_tokens": "1000000",
     "max_input_rate_usd_per_million": "11", "output_rate_usd_per_million": "30"},
    {"episode_dollar_limit": "15", "max_input_tokens": "1000000",
     "max_input_rate_usd_per_million": "-11", "output_rate_usd_per_million": "30"},
    {"episode_dollar_limit": "15", "max_input_tokens": "true",
     "max_input_rate_usd_per_million": "11", "output_rate_usd_per_million": "30"},
])
def test_monetary_guard_invalid_or_incomplete_configuration(values):
    module = _module()
    with pytest.raises(module.CaseError, match="monetary_guard"):
        module.parse_monetary_policy(values)


def test_monetary_guard_pre_send_exact_edge_reconcile_and_header():
    module = _module()
    recorder = module.UsageRecorder()
    guard = _monetary_guard(module, limit="11.06144")
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "opus-test",
                                        usage_recorder=recorder, monetary_guard=guard,
                                        output_tokens=2048)
    class Inner:
        sent = 0
        async def handle_async_request(self, request):
            self.sent += 1
            return httpx.Response(200, json={}, headers={"x-litellm-response-cost": "0.05"})
    inner = Inner()
    transport.inner = inner
    request = httpx.Request("POST", transport.endpoint,
                            json={"model": "opus-test", "max_completion_tokens": 2048})
    recorder.calls.append({"run_id": "first", "status": "started"})
    async def exercise():
        await transport.handle_async_request(request)
        recorder.calls[0].update(status="completed", usage=[{
            "input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}])
        recorder.calls.append({"run_id": "second", "status": "started"})
        with pytest.raises(module.CaseError, match="episode_dollar_limit"):
            await transport.handle_async_request(request)
    asyncio.run(exercise())
    assert inner.sent == 1
    assert guard.snapshot(transport.requests, recorder.calls)["observed_cost_upper_bound_usd"] == "0.05"
    assert transport.requests[0]["usage_cost_estimate_usd"] == "0.014"
    assert transport.requests[0]["reported_response_cost_usd"] == "0.05"
    assert transport.rejections[-1]["reason"] == "episode_dollar_limit"


@pytest.mark.parametrize("first_usage", [
    {"status": "error"},
    {"status": "completed", "usage": [{}]},
])
def test_monetary_guard_unknown_usage_holds_reserve_and_stops(first_usage):
    module = _module()
    recorder = module.UsageRecorder()
    guard = _monetary_guard(module)
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "opus-test",
                                        usage_recorder=recorder, monetary_guard=guard,
                                        output_tokens=2048)
    class Inner:
        sent = 0
        async def handle_async_request(self, request):
            self.sent += 1
            return httpx.Response(200, json={})
    inner = Inner()
    transport.inner = inner
    request = httpx.Request("POST", transport.endpoint,
                            json={"model": "opus-test", "max_completion_tokens": 2048})
    recorder.calls.append({"run_id": "first", "status": "started"})
    async def exercise():
        await transport.handle_async_request(request)
        recorder.calls[0].update(first_usage)
        with pytest.raises(module.CaseError, match="unknown_cost_usage"):
            await transport.handle_async_request(request)
    asyncio.run(exercise())
    snapshot = guard.snapshot(transport.requests, recorder.calls)
    assert inner.sent == 1
    assert snapshot["usage_status"] == "unknown"
    assert snapshot["unreconciled_reservations_usd"] == "11.06144"
    assert transport.rejections[-1]["reason"] == "unknown_cost_usage"


def test_monetary_guard_blocks_first_request_below_reservation():
    module = _module()
    guard = _monetary_guard(module, limit="11.06143")
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "opus-test",
                                        usage_recorder=module.UsageRecorder(),
                                        monetary_guard=guard, output_tokens=2048)
    class Inner:
        sent = 0
        async def handle_async_request(self, request):
            self.sent += 1
            return httpx.Response(200, json={})
    inner = Inner()
    transport.inner = inner
    request = httpx.Request("POST", transport.endpoint,
                            json={"model": "opus-test", "max_completion_tokens": 2048})
    async def exercise():
        with pytest.raises(module.CaseError, match="episode_dollar_limit"):
            await transport.handle_async_request(request)
    asyncio.run(exercise())
    assert inner.sent == 0 and transport.requests == []


def test_monetary_guard_local_model_callback_lifecycle_no_snapshot_poison():
    module = _module()
    recorder = module.UsageRecorder()
    guard = _monetary_guard(module)
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "opus-test",
                                        usage_recorder=recorder, monetary_guard=guard,
                                        output_tokens=2048)
    seen = []
    def reply(request):
        seen.append(json.loads(request.content))
        in_flight = guard.snapshot(transport.requests, recorder.calls)
        assert in_flight["usage_status"] == "pending"
        assert in_flight["unreconciled_reservations_usd"] == "11.06144"
        return httpx.Response(200, json={
            "id": f"chatcmpl-{len(seen)}", "object": "chat.completion", "created": 1,
            "model": "opus-test", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "A bounded answer."}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100,
                      "total_tokens": 1100},
        })
    transport.inner = httpx.MockTransport(reply)
    async def exercise():
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            llm = module.model_module.init_chat_model(
                model="opus-test", model_provider="openai", api_key="offline",
                base_url="http://127.0.0.1:9/v1", max_tokens=2048,
                max_retries=0, http_async_client=client)
            llm.callbacks = [recorder]
            assert (await llm.ainvoke([HumanMessage(content="first")])).content == "A bounded answer."
            assert (await llm.ainvoke([HumanMessage(content="second")])).content == "A bounded answer."
    asyncio.run(exercise())
    snapshot = guard.snapshot(transport.requests, recorder.calls, final=True)
    assert len(seen) == len(transport.requests) == 2
    assert snapshot["usage_status"] == "known"
    assert snapshot["reconciled_requests"] == 2
    assert snapshot["unreconciled_reservations_usd"] == "0.00000"
    assert transport.rejections == []


def test_monetary_guard_reported_charge_over_reservation_stops_next_call():
    module = _module()
    recorder = module.UsageRecorder()
    guard = _monetary_guard(module)
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "opus-test",
                                        usage_recorder=recorder, monetary_guard=guard,
                                        output_tokens=2048)
    class Inner:
        sent = 0
        async def handle_async_request(self, request):
            self.sent += 1
            return httpx.Response(200, json={}, headers={"x-litellm-response-cost": "12"})
    inner = Inner()
    transport.inner = inner
    request = httpx.Request("POST", transport.endpoint,
                            json={"model": "opus-test", "max_completion_tokens": 2048})
    recorder.calls.append({"run_id": "first", "status": "started"})
    async def exercise():
        await transport.handle_async_request(request)
        recorder.calls[0].update(status="completed", usage=[{
            "input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}])
        with pytest.raises(module.CaseError, match="cost_reservation_exceeded"):
            await transport.handle_async_request(request)
    asyncio.run(exercise())
    snapshot = guard.snapshot(transport.requests, recorder.calls, final=True)
    assert inner.sent == 1
    assert snapshot["observed_cost_upper_bound_usd"] == "12"
    assert snapshot["usage_status"] == "reported_over_reservation"
    assert transport.rejections[-1]["reason"] == "cost_reservation_exceeded"


@pytest.mark.parametrize("value", ("0", "-1", "241", "nan", "inf", "-inf", "bad"))
def test_request_seconds_invalid_fails_before_network(value, tmp_path, capsys, monkeypatch):
    module = _module()
    config, _ = _config(tmp_path)
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("invalid timeout attempted network")))
    assert module.main(["--config", str(config), f"--request-seconds={value}"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "invalid_request_seconds"


def test_request_seconds_ninety_reaches_transport_clients_and_model(tmp_path, monkeypatch, capsys):
    module = _module()
    config, _ = _config(tmp_path)
    assert module.main(["--config", str(config), "--request-seconds", "90"]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["limits"]["request_seconds"] == 90
    assert ready["limits"]["wall_seconds"] == 240
    assert module.parse_request_seconds(None) == 30
    seen = []
    def stop_after_factory(*_args, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("factory captured")
    monkeypatch.setattr(module.model_module, "init_chat_model", stop_after_factory)
    monkeypatch.chdir(tmp_path)
    profile = {"provider": "openai", "model": "synthetic-test-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "offline-only"}
    result = asyncio.run(module.run_episode(profile, decision="accept",
                                            scenario=module.REPORT_FRESH_NETSTAT_SCENARIO,
                                            request_seconds=90))
    assert result["status"] == "error"
    assert result["limits"]["request_seconds"] == 90
    assert seen
    assert all(kwargs["timeout"] == 90 and kwargs["max_retries"] == 0
               and kwargs["http_async_client"].timeout.read == 90
               and kwargs["http_client"].timeout.read == 90
               and kwargs["http_async_client"]._transport.request_seconds == 90
               for kwargs in seen)


def test_request_seconds_bounds_actual_transport_wait():
    module = _module()
    transport = module.BoundedTransport("http://127.0.0.1:9/v1", "synthetic-test-model",
                                        request_seconds=0.01)
    class SlowInner:
        sent = 0
        async def handle_async_request(self, request):
            self.sent += 1
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={})
    inner = SlowInner()
    transport.inner = inner
    request = httpx.Request("POST", transport.endpoint,
                            json={"model": "synthetic-test-model", "max_tokens": 512})
    async def exercise():
        with pytest.raises(TimeoutError):
            await transport.handle_async_request(request)
    asyncio.run(exercise())
    assert inner.sent == 1
    assert transport.requests[0]["status"] == "error"
    assert transport.requests[0]["error_type"] == "TimeoutError"
