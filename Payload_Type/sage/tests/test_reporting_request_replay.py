"""Offline checks for exact captured reporting request replay."""

import asyncio
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def runner():
    path = (Path(__file__).resolve().parents[3] /
            "skills/sage-focused-capability-tests/scripts/replay_reporting_request.py")
    spec = importlib.util.spec_from_file_location("reporting_request_replay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path, module, *, handback_tool=True):
    root = tmp_path
    source_path = root / ".sage_history" / "source.json"
    source_path.parent.mkdir()
    phrases = [("single account asserted", "single identity reported"),
               ("paired accounts asserted", "paired identities reported"),
               ("map to accounts", "map to identities")]
    original = {"model": "test-model", "stream": False, "max_completion_tokens": 2048,
                "messages": [{"role": "system", "content": "instructions"},
                             {"role": "user", "content": "first question"},
                             {"role": "assistant", "content": "tool request", "tool_calls": []},
                             *({"role": "tool", "content": f"tool {i}",
                                "tool_call_id": f"call-{i}"} for i in range(4)),
                             {"role": "assistant", "content": " / ".join(old for old, _ in phrases)},
                             {"role": "user", "content": "final recommendation?"}],
                "tools": [{"type": "function", "function": {"name": f"tool{i}"}}
                          for i in range(24)] + [{"type": "function", "function": {
                              "name": "handback_to_supervisor" if handback_tool else "tool24",
                              "parameters": {"type": "object", "required":
                                             ["reason", "summary", "outcome"]}}}]}
    source = {"provider_requests": [{"status": "returned", "http_status": 200,
                                     "request": original}]}
    source_path.write_text(json.dumps(source), encoding="utf-8")
    edited = copy.deepcopy(original)
    for old, new in phrases:
        edited["messages"][7]["content"] = edited["messages"][7]["content"].replace(old, new)
    endpoint = "https://provider.test/v1"
    packet = {"source": {"path": ".sage_history/source.json",
                         "file_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                         "request_pointer": "/provider_requests/0/request",
                         "request_sha256": module.digest(original)},
              "route": {"provider": "openai", "model": "test-model",
                        "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest()},
              "arms": {"original": original, "qualified_prior_prose": edited},
              "changed_field": "/messages/7/content",
              "replacements": [{"from": old, "to": new, "expected_count": 1}
                               for old, new in phrases],
              "order": list(module.ORDER), "limits": module.LIMITS.copy()}
    profile = {"provider": "openai", "model": "test-model",
               "API_ENDPOINT": endpoint, "API_KEY": "synthetic-test-key"}
    return packet, profile


def with_no_tools_override(packet):
    packet["common_request_override"] = {"tool_choice": "none"}
    for body in packet["arms"].values():
        body["tool_choice"] = "none"
    return packet


def with_handback_override(packet, module):
    packet["common_request_override"] = copy.deepcopy(module.HANDBACK_CHOICE)
    for body in packet["arms"].values():
        body.update(copy.deepcopy(module.HANDBACK_CHOICE))
    return packet


def test_offline_validation_never_loads_provider_or_writes(tmp_path, monkeypatch, capsys):
    module = runner()
    packet, _ = fixture(tmp_path, module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "load_helpers", lambda: pytest.fail("provider setup"))
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet))
    assert module.main(["--packet", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["calls"] == 4
    assert list(tmp_path.glob("*.json")) == [path]


def test_common_tool_choice_none_is_exactly_added_to_both_arms(tmp_path, monkeypatch):
    module = runner()
    packet, profile = fixture(tmp_path, module)
    source_original = copy.deepcopy(packet["arms"]["original"])
    with_no_tools_override(packet)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    module.validate_packet(packet)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "test-model",
            "usage": {"total_tokens": 40},
            "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]})

    output = tmp_path / "override.jsonl"
    result = asyncio.run(module.run(packet, profile, output,
                         transport=httpx.MockTransport(handler)))
    assert result["status"] == "complete"
    assert len(sent) == 4
    for body, arm in zip(sent, module.ORDER):
        assert body == packet["arms"][arm]
        assert body["tool_choice"] == "none"
        assert {key: value for key, value in body.items() if key != "tool_choice"} == (
            source_original if arm == "original" else
            {key: value for key, value in packet["arms"][arm].items()
             if key != "tool_choice"})
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["common_request_override"] == {"tool_choice": "none"}
    assert all(row["returned_tool_calls"] is False for row in rows
               if row["event"] == "response")


def test_plural_common_override_key_rejected(tmp_path):
    module = runner()
    packet, _ = fixture(tmp_path, module)
    packet["common_request_overrides"] = {"tool_choice": "none"}
    with pytest.raises(ValueError, match="unsupported common"):
        module.validate_packet(packet, root=tmp_path)


@pytest.mark.parametrize("mutation", ["unsupported", "original_only", "qualified_only",
                                      "extra_override", "tool_schema", "message", "settings"])
def test_rejects_noncommon_or_broader_overrides(tmp_path, mutation):
    module = runner()
    packet, _ = fixture(tmp_path, module)
    with_no_tools_override(packet)
    if mutation == "unsupported":
        packet["common_request_override"] = {"tool_choice": "auto"}
    elif mutation == "original_only":
        packet["arms"]["qualified_prior_prose"].pop("tool_choice")
    elif mutation == "qualified_only":
        packet["arms"]["original"].pop("tool_choice")
    elif mutation == "extra_override":
        packet["common_request_override"]["temperature"] = 0
    elif mutation == "tool_schema":
        packet["arms"]["qualified_prior_prose"]["tools"][0]["function"]["name"] = "other"
    elif mutation == "message":
        packet["arms"]["original"]["messages"][8]["content"] = "changed"
    else:
        packet["arms"]["qualified_prior_prose"]["max_completion_tokens"] = 1024
    with pytest.raises(ValueError):
        module.validate_packet(packet, root=tmp_path)


def test_returned_tool_call_with_none_stops_and_retains_response(tmp_path, monkeypatch):
    module = runner()
    packet, profile = fixture(tmp_path, module)
    with_no_tools_override(packet)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model": "test-model",
            "usage": {"total_tokens": 73},
            "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
                {"id": "unexecuted", "function": {"name": "tool1", "arguments": "{}"}}]}}]})

    output = tmp_path / "violation.jsonl"
    result = asyncio.run(module.run(packet, profile, output,
                         transport=httpx.MockTransport(handler)))
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert result == {"status": "stopped_tool_choice_violation",
                      "provider_requests": 1, "observed_tokens": 73}
    assert len(calls) == 1
    assert rows[0]["common_request_override"] == {"tool_choice": "none"}
    assert rows[-2]["returned_tool_calls"] is True
    assert rows[-2]["usage_status"] == "reported"
    assert rows[-2]["response_body_base64"]
    assert rows[-1]["status"] == "stopped_tool_choice_violation"


def test_named_handback_override_delivers_both_arms_without_dispatch(tmp_path, monkeypatch):
    module = runner()
    packet, profile = fixture(tmp_path, module)
    source_original = copy.deepcopy(packet["arms"]["original"])
    with_handback_override(packet, module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    module.validate_packet(packet)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "test-model",
            "usage": {"total_tokens": 91}, "choices": [{"finish_reason": "tool_calls",
            "message": {"content": "Report preface", "tool_calls": [{"type": "function",
                "function": {"name": "handback_to_supervisor",
                             "arguments": json.dumps({"reason": "Done reviewing",
                                "summary": "Observed result with caveats", "outcome": "complete"})}}]}}]})

    output = tmp_path / "named-handback.jsonl"
    result = asyncio.run(module.run(packet, profile, output,
                         transport=httpx.MockTransport(handler)))
    assert result == {"status": "complete", "provider_requests": 4,
                      "observed_tokens": 364}
    assert len(sent) == 4
    for body, arm in zip(sent, module.ORDER):
        assert body == packet["arms"][arm]
        assert body["tool_choice"] == module.HANDBACK_CHOICE["tool_choice"]
        assert body["tools"] == source_original["tools"]
        assert body["messages"][3:7] == source_original["messages"][3:7]
        assert body["messages"][8] == source_original["messages"][8]
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["common_request_override"] == module.HANDBACK_CHOICE
    assert all(row["handback_status"] == "valid" and row["returned_tool_calls"]
               for row in rows if row["event"] == "response")
    assert all(row["response_body_base64"] for row in rows if row["event"] == "response")


@pytest.mark.parametrize("mutation", ["missing_capture_tool", "one_arm", "wrong_name",
                                      "extra_override", "edited_schema", "edited_message"])
def test_named_handback_packet_rejects_scope_change(tmp_path, mutation):
    module = runner()
    packet, _ = fixture(tmp_path, module, handback_tool=mutation != "missing_capture_tool")
    with_handback_override(packet, module)
    if mutation == "one_arm":
        packet["arms"]["qualified_prior_prose"].pop("tool_choice")
    elif mutation == "wrong_name":
        packet["common_request_override"]["tool_choice"]["function"]["name"] = "tool1"
    elif mutation == "extra_override":
        packet["common_request_override"]["temperature"] = 0
    elif mutation == "edited_schema":
        packet["arms"]["original"]["tools"][24]["function"]["name"] = "other"
    elif mutation == "edited_message":
        packet["arms"]["qualified_prior_prose"]["messages"][3]["content"] = "changed"
    with pytest.raises(ValueError):
        module.validate_packet(packet, root=tmp_path)


@pytest.mark.parametrize("tool_calls,expected_status", [
    ([], "missing_or_multiple_calls"),
    ([{"function": {"name": "tool1", "arguments": "{}"}}], "wrong_tool"),
    ([{"function": {"name": "handback_to_supervisor", "arguments": "not-json"}}],
     "invalid_arguments"),
    ([{"function": {"name": "handback_to_supervisor",
                    "arguments": '{"reason":"x","summary":"y","outcome":"other"}'}}],
     "invalid_arguments"),
    ([{"function": {"name": "handback_to_supervisor",
                    "arguments": '{"reason":"x","outcome":"complete"}'}}],
     "invalid_arguments"),
    ([{"function": {"name": "handback_to_supervisor",
                    "arguments": '{"reason":"x","summary":"y","outcome":"complete"}'}},
      {"function": {"name": "handback_to_supervisor",
                    "arguments": '{"reason":"x","summary":"y","outcome":"complete"}'}}],
     "missing_or_multiple_calls"),
])
def test_named_handback_report_format_violation_stops(tmp_path, monkeypatch,
                                                       tool_calls, expected_status):
    module = runner()
    packet, profile = fixture(tmp_path, module)
    with_handback_override(packet, module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model": "test-model",
            "usage": {"total_tokens": 61}, "choices": [{"finish_reason": "tool_calls",
            "message": {"content": "Readable preface", "tool_calls": tool_calls}}]})

    output = tmp_path / "format-violation.jsonl"
    result = asyncio.run(module.run(packet, profile, output,
                         transport=httpx.MockTransport(handler)))
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert result == {"status": "stopped_report_format_violation",
                      "provider_requests": 1, "observed_tokens": 61}
    assert len(calls) == 1
    assert rows[-2]["handback_status"] == expected_status
    assert rows[-2]["response_body_base64"]
    assert rows[-2]["usage_status"] == "reported"
    assert rows[-1]["status"] == "stopped_report_format_violation"


@pytest.mark.parametrize("mutation", ["raw_tool", "final_user", "extra_assistant",
                                      "original", "replacement", "source", "route", "limit"])
def test_rejects_unreviewed_mutations(tmp_path, mutation):
    module = runner()
    packet, _ = fixture(tmp_path, module)
    if mutation == "raw_tool":
        packet["arms"]["qualified_prior_prose"]["messages"][3]["content"] = "different"
    elif mutation == "final_user":
        packet["arms"]["qualified_prior_prose"]["messages"][8]["content"] = "different"
    elif mutation == "extra_assistant":
        packet["arms"]["qualified_prior_prose"]["messages"][7]["content"] += " extra"
    elif mutation == "original":
        packet["arms"]["original"]["messages"][3]["content"] = "different"
    elif mutation == "replacement":
        packet["replacements"][0]["to"] = "unreviewed text"
    elif mutation == "source":
        (tmp_path / ".sage_history/source.json").write_text("{}")
    elif mutation == "route":
        packet["route"]["model"] = "other-model"
    else:
        packet["limits"]["max_provider_calls"] = 5
    with pytest.raises(ValueError):
        module.validate_packet(packet, root=tmp_path)


def test_mock_transport_keeps_tool_calls_and_append_only_attempts(tmp_path, monkeypatch):
    module = runner()
    packet, profile = fixture(tmp_path, module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"model": "test-model",
            "usage": {"total_tokens": 100},
            "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
                {"id": "returned-only", "function": {"name": "tool1", "arguments": "{}"}}]}}]},
            headers={"x-litellm-response-cost": "0.01"})

    output = tmp_path / "attempts.jsonl"
    result = asyncio.run(module.run(packet, profile, output,
                         transport=httpx.MockTransport(handler)))
    assert result == {"status": "complete", "provider_requests": 4,
                      "observed_tokens": 400}
    assert [body["messages"][7]["content"] for body in seen] == [
        packet["arms"][arm]["messages"][7]["content"] for arm in module.ORDER]
    assert all(body["messages"][3:7] == packet["arms"]["original"]["messages"][3:7]
               and body["messages"][8] == packet["arms"]["original"]["messages"][8]
               and body["tools"] == packet["arms"]["original"]["tools"] for body in seen)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [r["event"] for r in rows] == [
        "start", "attempt", "response", "attempt", "response", "attempt", "response",
        "attempt", "response", "finish"]
    assert all(r["returned_tool_calls"] for r in rows if r["event"] == "response")
    assert all(r["reported_response_cost_usd"] == "0.01" for r in rows
               if r["event"] == "response")
    with pytest.raises(FileExistsError):
        asyncio.run(module.run(packet, profile, output,
                    transport=httpx.MockTransport(handler)))
    assert len(seen) == 4


@pytest.mark.parametrize("kind", ["missing_usage", "model_mismatch", "malformed_choices"])
def test_stops_after_first_unknown_usage_or_model_mismatch(tmp_path, monkeypatch, kind):
    module = runner()
    packet, profile = fixture(tmp_path, module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        payload = {"model": "other-model" if kind == "model_mismatch" else "test-model",
                   "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]}
        if kind in {"model_mismatch", "malformed_choices"}:
            payload["usage"] = {"total_tokens": 5}
        if kind == "malformed_choices":
            payload["choices"] = [{"finish_reason": "stop", "message": {}}]
        return httpx.Response(200, json=payload)

    output = tmp_path / "attempts.jsonl"
    result = asyncio.run(module.run(packet, profile, output,
                         transport=httpx.MockTransport(handler)))
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert result["provider_requests"] == len(calls) == 1
    assert result["status"] == "stopped_provider_or_usage_or_route"
    assert rows[-2]["event"] == "response"
    assert rows[-2]["response_body_base64"]
    assert rows[-2]["usage_status"] == ("unknown" if kind == "missing_usage" else "reported")
    if kind == "malformed_choices":
        assert rows[-2]["answer_status"] == "malformed_choices"


def test_profile_route_rejected_before_output_or_request(tmp_path, monkeypatch):
    module = runner()
    packet, profile = fixture(tmp_path, module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    profile["API_ENDPOINT"] = "https://other.test/v1"
    output = tmp_path / "attempts.jsonl"
    with pytest.raises(ValueError, match="route mismatch"):
        asyncio.run(module.run(packet, profile, output,
                    transport=httpx.MockTransport(lambda _: pytest.fail("network"))))
    assert not output.exists()
