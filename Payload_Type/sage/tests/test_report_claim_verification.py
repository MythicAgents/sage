"""Offline checks for the inert claim verification experiment."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


def runner():
    path = (Path(__file__).resolve().parents[3] /
            "skills/sage-focused-capability-tests/scripts/verify_report_claims.py")
    spec = importlib.util.spec_from_file_location("claim_verification", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan():
    return {"sources": [
        {"id": "task:1", "text": "Host: NODE-A\nDomain: example.test\nUser: LAB\\analyst",
         "subject": {"task_id": 1, "callback_id": 7}, "observed_at": "2026-01-01T00:00:00Z"},
        {"id": "task:2", "text": "Host: NODE-B\nDomain: other.test",
         "subject": {"task_id": 2, "callback_id": 8}, "withdrawn": True}],
        "claims": [{"id": "domain", "text": "NODE-A reports example.test as its domain.",
                    "source_ids": ["task:1"], "subject": {"task_id": 1},
                    "as_of": "2026-01-01T00:00:00Z"}],
        "reports": [{"id": "report", "operator_request": "Summarize NODE-A.",
                     "draft": "NODE-A reports example.test and has directory rights.",
                     "claim_ids": ["domain"], "source_ids": ["task:1"]}]}


def answer(verdict="supported", evidence=None):
    return json.dumps({"verdict": verdict,
                       "evidence": [{"source_id": "task:1", "quote": "Domain: example.test"}]
                       if evidence is None else evidence, "reason": "Source states the field."})


def test_offline_plan_validation_has_no_provider_or_output(tmp_path, monkeypatch, capsys):
    module = runner()
    source = tmp_path / "plan.json"
    source.write_text(json.dumps(plan()))
    monkeypatch.setattr(module, "load_helpers", lambda: pytest.fail("provider setup"))
    output = tmp_path / "result.json"
    assert module.main(["--plan", str(source), "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["calls"] == 4
    assert not output.exists()


@pytest.mark.parametrize("response,changes,expected_error", [
    ("nonsense", {}, "invalid_json"),
    ("```json\n" + answer() + "\n```", {}, "invalid_json"),
    (answer(evidence=[{"source_id": "task:1", "quote": "Invented rights"}]),
     {}, "quote_not_in_source"),
    (answer(evidence=[{"source_id": "task:2", "quote": "Domain: other.test"}]),
     {}, "unknown_or_disallowed_source"),
    (answer(), {"subject": {"callback_id": 8}}, "subject_mismatch"),
    (answer(), {"as_of": "2027-01-01T00:00:00Z"}, "time_mismatch"),
    (answer(evidence=[]), {}, "missing_support_reference"),
])
def test_rejects_invalid_or_misattributed_citations(response, changes, expected_error):
    module = runner()
    data = plan()
    data["claims"][0].update(changes)
    if expected_error == "unknown_or_disallowed_source":
        data["claims"][0]["source_ids"].append("task:2")
        # A declared task ID mismatch also exists, but withdrawal alone must reject it.
        assert "withdrawn_source" in module.mechanical_check(
            response, data["claims"][0], {s["id"]: s for s in data["sources"]})["errors"]
        data["claims"][0]["source_ids"].remove("task:2")
    checked = module.mechanical_check(response, data["claims"][0],
                                      {s["id"]: s for s in data["sources"]})
    assert not checked["valid"]
    assert expected_error in checked["errors"]


def test_withdrawn_source_rejected_even_with_real_quote():
    module = runner()
    data = plan()
    data["claims"][0]["source_ids"] = ["task:2"]
    data["claims"][0].pop("subject")
    data["claims"][0].pop("as_of")
    checked = module.mechanical_check(
        answer(evidence=[{"source_id": "task:2", "quote": "Domain: other.test"}]),
        data["claims"][0], {s["id"]: s for s in data["sources"]})
    assert checked["errors"] == ["withdrawn_source"]


def test_real_but_irrelevant_quote_is_only_mechanically_valid():
    module = runner()
    data = plan()
    checked = module.mechanical_check(
        answer(evidence=[{"source_id": "task:1", "quote": "User: LAB\\analyst"}]),
        data["claims"][0], {s["id"]: s for s in data["sources"]})
    assert checked["valid"]
    assert checked["parsed"]["verdict"] == "supported"
    # The cited user string does not semantically establish a domain claim.


def test_claim_prompt_excludes_draft_and_repair_includes_judgments():
    module = runner()
    data = plan()
    claim = data["claims"][0]
    source_map = {s["id"]: s for s in data["sources"]}
    claim_messages = module.check_messages(claim, source_map)
    assert "directory rights" not in json.dumps(claim_messages)
    assert "expected" not in json.dumps(claim_messages).lower()
    assert "task:2" not in json.dumps(claim_messages)
    repair = module.repair_messages(data["reports"][0], {claim["id"]: claim},
                                    source_map, {"domain": json.loads(answer())})
    assert "directory rights" in json.dumps(repair)
    assert "Domain: example.test" in json.dumps(repair)
    assert json.loads(repair[1]["content"])["claims"][0]["judgment"]["verdict"] == "supported"


def _args(module, output):
    endpoint = "https://example.test/v1"
    profile = {"provider": "openai", "model": "test-model", "API_ENDPOINT": endpoint,
               "API_KEY": "secret"}
    args = SimpleNamespace(expected_model="test-model",
        expected_endpoint_sha256=module.hashlib.sha256(endpoint.encode()).hexdigest(),
        output=output, repeats=2, max_calls=4, max_tokens=1000, max_wall=20,
        output_tokens=128)
    return args, profile


@pytest.mark.parametrize("first_content,first_usage,expected_calls,expected_status", [
    (answer(), True, 4, "complete"),
    (answer(), False, 1, "stopped_unknown_usage_or_provider_error"),
    ("invalid JSON", True, 3, "complete_with_incomplete_reports"),
])
def test_serial_provider_pipeline_retains_attempts_and_skips_invalid_repairs(
        tmp_path, monkeypatch, first_content, first_usage, expected_calls, expected_status):
    module = runner()
    output = tmp_path / "result.json"
    args, profile = _args(module, output)
    seen = []

    def respond(request):
        body = json.loads(request.content)
        retained = json.loads(output.read_text())
        assert retained["provider_requests"] == len(seen) + 1
        assert retained["attempts"][-1]["status"] == "started"
        assert body == retained["attempts"][-1]["request"]
        assert "tools" not in body and body["max_completion_tokens"] == 128
        assert str(request.url) == "https://example.test/v1/chat/completions"
        seen.append(body)
        content = first_content if len(seen) == 1 else (
            "NODE-A reported example.test [task:1]. Rights remain unknown."
            if len(seen) == 2 and first_content != "invalid JSON" else answer())
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                        "usage": {"total_tokens": 12} if first_usage else {}})

    monkeypatch.setattr(module.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.MockTransport(respond))
    result = asyncio.run(module.run(plan(), args,
                        SimpleNamespace(sage_tls_context=lambda: None), profile))
    assert len(seen) == expected_calls
    assert result["status"] == expected_status
    assert "secret" not in output.read_text()
    assert result["observed_tokens"] == (12 * expected_calls if first_usage else 0)
    if first_content == "invalid JSON":
        assert result["attempts"][0]["mechanical_check"]["errors"] == ["invalid_json"]
        assert result["attempts"][1]["status"] == "incomplete_invalid_claim_check"
        assert result["attempts"][1]["invalid_claim_ids"] == ["domain"]


def test_route_mismatch_fails_before_creating_output(tmp_path):
    module = runner()
    args, profile = _args(module, tmp_path / "out.json")
    args.expected_model = "wrong"
    with pytest.raises(ValueError, match="route binding"):
        asyncio.run(module.run(plan(), args,
                    SimpleNamespace(sage_tls_context=lambda: None), profile))
    assert not args.output.exists()
