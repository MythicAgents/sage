"""Check paired evidence and inert provider-only reporting replay boundaries."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


def runner():
    path = Path(__file__).resolve().parents[3] / "skills/sage-focused-capability-tests/scripts/compare_reporting_context.py"
    spec = importlib.util.spec_from_file_location("report_context", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan():
    packet = {"role": "user", "content": "Task 1 reports Domain: example.test."}
    return {"cases": [{"id": "known", "arms": {
        "full": [{"role": "system", "content": "Report the supplied evidence."}, packet],
        "minimal": [packet.copy()],
    }}]}


def test_dry_run_never_loads_provider(tmp_path, monkeypatch, capsys):
    module = runner()
    source = tmp_path / "plan.json"
    source.write_text(json.dumps(plan()))
    monkeypatch.setattr(module, "load_helpers", lambda: pytest.fail("provider setup"))
    assert module.main(["--plan", str(source), "--output", str(tmp_path / "out.json")]) == 0
    assert json.loads(capsys.readouterr().out)["calls"] == 4
    assert not (tmp_path / "out.json").exists()


def test_mismatched_evidence_and_tool_messages_rejected():
    module = runner()
    data = plan()
    data["cases"][0]["arms"]["minimal"][-1]["content"] = "different evidence"
    with pytest.raises(ValueError, match="share"):
        module.validate_plan(data)
    data = plan()
    data["cases"][0]["arms"]["full"].insert(0, {"role": "tool", "content": "x"})
    with pytest.raises(ValueError, match="plain-text"):
        module.validate_plan(data)


@pytest.mark.parametrize("known_usage,expected_calls", [(True, 4), (False, 1)])
def test_provider_only_replay_retains_usage_and_stops_on_unknown(tmp_path, monkeypatch, known_usage, expected_calls):
    module = runner()
    calls = []
    def respond(request):
        body = json.loads(request.content)
        assert str(request.url) == "https://example.test/v1/chat/completions"
        assert "tools" not in body and body["max_completion_tokens"] == 100
        calls.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Observed"}}],
            "usage": {"total_tokens": 12} if known_usage else {}})
    monkeypatch.setattr(module.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    profile = {"provider": "openai", "model": "test", "API_ENDPOINT": "https://example.test/v1", "API_KEY": "secret"}
    args = SimpleNamespace(expected_model="test", expected_endpoint_sha256=module.hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest(),
        output=tmp_path / "out.json", repeats=2, max_calls=4, max_tokens=1000, max_wall=20, output_tokens=100)
    result = asyncio.run(module.run(plan(), args, SimpleNamespace(sage_tls_context=lambda: None), profile))
    assert len(calls) == expected_calls
    assert result["observed_tokens"] == (48 if known_usage else 0)
    assert result["status"] == ("complete" if known_usage else "stopped_unknown_usage_or_provider_error")
    assert "secret" not in args.output.read_text()
    assert [r["arm"] for r in result["attempts"]] == (["full", "minimal", "minimal", "full"] if known_usage else ["full"])
