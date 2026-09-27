"""Network-free checks for one captured provider-wire replay."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import stat

import httpx
import pytest


SCRIPT = (Path(__file__).resolve().parents[3] / "skills" /
          "sage-focused-capability-tests" / "scripts" / "replay_captured_wire.py")


def _module():
    spec = importlib.util.spec_from_file_location("captured_wire_replay", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files(tmp_path, *, scenario="report-accuracy-historical-withdrawal-v1",
           request_index=3, transform="none"):
    module = _module()
    profile = tmp_path / "profile.env"
    profile.write_text("provider=openai\nmodel=synthetic-model\n"
                       "API_ENDPOINT=http://127.0.0.1:7443/v1\nAPI_KEY=private-test-key\n")
    body = {"model": "synthetic-model", "max_completion_tokens": 2048,
            "stream": False, "messages": [{"role": "system", "content": "Instructions"},
                                           {"role": "user", "content": "Report only the data"}],
            "tools": [{"type": "function", "function": {"name": "respond_to_user",
                                                       "parameters": {"type": "object"}}}]}
    source = {"scenario": scenario, "provider": "openai", "model": "synthetic-model",
              "protocol_valid": True,
              "provider_requests": [{"request": body, "status": "returned", "http_status": 200}
                                    for _ in range(request_index + 1)]}
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source))
    case = {"source_path": str(source_path),
            "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "source_request_sha256": module.digest(body),
            "frozen_request_sha256": module.digest(body),
            "request_index": request_index, "scenario": scenario,
            "endpoint_sha256": hashlib.sha256(b"http://127.0.0.1:7443/v1").hexdigest(),
            "transform": transform, "request": body}
    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps(case))
    return module, profile, source_path, case_path, body


def test_exact_captured_body_one_request_and_no_tool_dispatch(tmp_path):
    module, profile_path, _, case_path, body = _files(tmp_path)
    profile = module.read_profile(profile_path)
    frozen, identity = module.read_case(case_path, profile)
    assert frozen == body and identity["max_requests"] == 1
    seen = []
    def reply(request):
        seen.append(request)
        assert json.loads(request.content) == body
        assert request.url == "http://127.0.0.1:7443/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer private-test-key"
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{
            "function": {"name": "issue_task_and_waitfor_task_output", "arguments": "{}"}
        }]}}], "usage": {"total_tokens": 123}})
    response = asyncio.run(module.replay_once(frozen, profile, transport=httpx.MockTransport(reply)))
    assert len(seen) == 1
    assert response["status"] == "returned"
    assert response["provider_requests"] == 1
    assert response["usage"] == {"total_tokens": 123}
    assert response["usage_status"] == "reported"
    assert b"issue_task_and_waitfor_task_output" in base64.b64decode(response["response_body_base64"])


def test_stream_response_bytes_and_unknown_usage(tmp_path):
    module, profile_path, _, case_path, body = _files(tmp_path)
    body = json.loads(json.dumps(body))
    body["stream"] = True
    case = json.loads(case_path.read_text())
    case["request"] = body
    source = json.loads(Path(case["source_path"]).read_text())
    source["provider_requests"][3]["request"]["stream"] = True
    source_path = Path(case["source_path"])
    source_path.write_text(json.dumps(source))
    case["source_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    case["source_request_sha256"] = module.digest(body)
    case["frozen_request_sha256"] = module.digest(body)
    case_path.write_text(json.dumps(case))
    frozen, _ = module.read_case(case_path, module.read_profile(profile_path))
    raw = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=raw))
    response = asyncio.run(module.replay_once(frozen, module.read_profile(profile_path),
                                              transport=transport))
    assert base64.b64decode(response["response_body_base64"]) == raw
    assert response["usage"] is None and response["usage_status"] == "unknown"


def test_changed_body_or_provenance_fails_before_network(tmp_path, monkeypatch):
    module, profile_path, source_path, case_path, body = _files(tmp_path)
    profile = module.read_profile(profile_path)
    case = json.loads(case_path.read_text())
    case["request"]["messages"][1]["content"] = "Altered user request"
    case["frozen_request_sha256"] = module.digest(case["request"])
    case_path.write_text(json.dumps(case))
    with pytest.raises(ValueError, match="baseline request differs"):
        module.read_case(case_path, profile)
    case["request"] = body
    case["source_sha256"] = "0" * 64
    case_path.write_text(json.dumps(case))
    with pytest.raises(ValueError, match="source artifact hash mismatch"):
        module.read_case(case_path, profile)
    case["source_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    case["endpoint_sha256"] = "0" * 64
    case_path.write_text(json.dumps(case))
    with pytest.raises(ValueError, match="frozen request or route"):
        module.read_case(case_path, profile)
    monkeypatch.setattr(module, "replay_once", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("dry run attempted provider request")))
    case["endpoint_sha256"] = hashlib.sha256(b"http://127.0.0.1:7443/v1").hexdigest()
    case["frozen_request_sha256"] = module.digest(body)
    case_path.write_text(json.dumps(case))
    assert module.main(["--case", str(case_path), "--profile", str(profile_path)]) == 0


def test_exclusive_private_artifact_and_no_retry(tmp_path, monkeypatch):
    module, profile_path, _, case_path, body = _files(tmp_path)
    calls = []
    async def fake_once(request_body, _profile):
        calls.append(request_body)
        return {"status": "error", "provider_requests": 1,
                "usage": None, "usage_status": "unknown", "error_type": "ConnectError"}
    monkeypatch.setattr(module, "replay_once", fake_once)
    artifact = tmp_path / "response.json"
    assert module.main(["--case", str(case_path), "--profile", str(profile_path),
                        "--output", str(artifact), "--run"]) == 1
    assert calls == [body]
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert json.loads(artifact.read_text())["response"]["usage_status"] == "unknown"
    with pytest.raises(FileExistsError):
        module.main(["--case", str(case_path), "--profile", str(profile_path),
                     "--output", str(artifact), "--run"])
    assert calls == [body]


def test_freeze_only_compact_is_exact_subtraction(tmp_path):
    module, profile_path, source_path, _, body = _files(tmp_path, request_index=2)
    original = ("Opening\n" + module.COMPACT_PREAMBLE + "Rules one through three.\n" +
                module.COMPACT_START + "Execution strategy removed.\n" +
                module.COMPACT_END + " Keep handback and authority.\n")
    source = json.loads(source_path.read_text())
    source["provider_requests"][2]["request"]["messages"][0]["content"] = original
    source_path.write_text(json.dumps(source))
    case = module.freeze_case(source_path, 2, "compact_specialist_system",
                              module.read_profile(profile_path))
    candidate = case["request"]["messages"][0]["content"]
    assert candidate == "Opening\nRules one through three.\n" + module.COMPACT_END + " Keep handback and authority.\n"
    assert case["request"]["messages"][1:] == body["messages"][1:]
    assert case["request"]["tools"] == body["tools"]
    case["request"]["messages"][1]["content"] = "changed user instruction"
    case["frozen_request_sha256"] = module.digest(case["request"])
    with pytest.raises(ValueError, match="specialist candidate changed more"):
        module._check_case(case, source_path.read_bytes(), module.read_profile(profile_path))


def test_freeze_only_duplicate_removal_requires_exact_source_shape(tmp_path):
    module, profile_path, source_path, _, _ = _files(tmp_path, request_index=4)
    source = json.loads(source_path.read_text())
    body = source["provider_requests"][4]["request"]
    body["messages"] = [
        {"role": "system", "content": "system"},
        {"role": "system", "content": "runtime"},
        {"role": "user", "content": "original"},
        {"role": "assistant", "content": "header"},
        {"role": "assistant", "name": "Mythic_Operator", "content": "same answer"},
        {"role": "assistant", "content": "references"},
        {"role": "assistant", "content": "raw evidence"},
        {"role": "assistant", "name": "Supervisor", "content": "same answer"},
        {"role": "user", "content": "withdraw task62"},
    ]
    source_path.write_text(json.dumps(source))
    case = module.freeze_case(source_path, 4, "omit_duplicate_supervisor_answer",
                              module.read_profile(profile_path))
    assert case["request"]["messages"] == body["messages"][:7] + body["messages"][8:]
    source["provider_requests"][4]["request"]["messages"][7]["content"] = "different"
    source_path.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="exact duplicate removal"):
        module.freeze_case(source_path, 4, "omit_duplicate_supervisor_answer",
                           module.read_profile(profile_path))
