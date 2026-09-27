"""Offline guards for the narrow recorded Supervisor completion replay."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

import httpx
from langchain_core.load import dumpd
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills" / "sage-focused-capability-tests" / "scripts" / "replay_supervisor_completion.py"
SPEC = importlib.util.spec_from_file_location("replay_supervisor_completion", SCRIPT)
assert SPEC and SPEC.loader
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def test_candidate_changes_only_streaming_paragraph():
    old = (replay.HEADER + "            The user has ALREADY SEEN the answer.\n"
           "            Therefore:\n" + replay.NEXT_BULLET + " use it.\n")
    original = [SystemMessage(content="prefix\n" + old + "suffix\n"),
                AIMessage(content="specialist finding", name="Mythic_Operator"),
                HumanMessage(content="Internal conversation continuation.")]
    revised = replay.candidate_messages(original, ROOT / "Payload_Type/sage/prompts/supervisor.md")
    assert len(revised) == len(original)
    assert revised[0].content.startswith("prefix\n" + replay.HEADER)
    assert revised[0].content.endswith(replay.NEXT_BULLET + " use it.\nsuffix\n")
    assert "ALREADY SEEN" not in revised[0].content
    assert "visibility does not complete your final-report step" in revised[0].content
    assert [dumpd(m) for m in revised[1:]] == [dumpd(m) for m in original[1:]]
    assert original[0].content == "prefix\n" + old + "suffix\n"


def test_supervisor_continuation_changes_only_tagged_final_message(monkeypatch):
    monkeypatch.setattr(replay.model_module, "_provider_continuation_text",
                        lambda role: "Finish the pending Supervisor report." if role == "Supervisor" else "",
                        raising=False)
    original = [SystemMessage(content="prompt"), AIMessage(content="specialist answer"),
                HumanMessage(content=replay.RECORDED_CONTINUATION,
                             additional_kwargs={"_synthetic_nudge": "provider_requires_user_turn",
                                                "_hide_from_stream": True})]
    revised = replay.supervisor_continuation_messages(original)
    assert [dumpd(m) for m in revised[:-1]] == [dumpd(m) for m in original[:-1]]
    assert revised[-1].content == "Finish the pending Supervisor report."
    assert revised[-1].additional_kwargs == original[-1].additional_kwargs
    assert original[-1].content == replay.RECORDED_CONTINUATION


@pytest.mark.parametrize("last", [
    HumanMessage(content=replay.RECORDED_CONTINUATION,
                 additional_kwargs={"_synthetic_nudge": "provider_requires_user_turn",
                                    "_hide_from_stream": True, "_operator_input": True}),
    HumanMessage(content=replay.RECORDED_CONTINUATION),
    HumanMessage(content="different continuation",
                 additional_kwargs={"_synthetic_nudge": "provider_requires_user_turn",
                                    "_hide_from_stream": True}),
])
def test_supervisor_continuation_rejects_untrusted_last_message(monkeypatch, last):
    monkeypatch.setattr(replay.model_module, "_provider_continuation_text", lambda role: "replacement",
                        raising=False)
    with pytest.raises(ValueError, match="exact runtime-tagged continuation"):
        replay.supervisor_continuation_messages([SystemMessage(content="prompt"), last])


def test_supervisor_continuation_flag_rejects_baseline(monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--evidence", "missing.json",
                                  "--arm", "baseline", "--supervisor-continuation"])
    with pytest.raises(SystemExit):
        replay.main()


def test_transport_rejects_wrong_endpoint_cap_and_second_call():
    async def probe():
        transport = replay.OneRequestTransport("https://example.test/v1/chat/completions")
        transport.inner.handle_async_request = AsyncMock(return_value=httpx.Response(200))
        def request(url: str, cap: int):
            return httpx.Request("POST", url, json={"model": "example", "max_tokens": cap,
                                                      "tools": [{"type": "function"}]})
        good_url = "https://example.test/v1/chat/completions"
        with pytest.raises(RuntimeError, match="unexpected provider endpoint"):
            await transport.handle_async_request(request("https://other.test/v1/chat/completions", 1024))
        with pytest.raises(RuntimeError, match="missing exact output cap"):
            await transport.handle_async_request(request(good_url, 2048))
        assert transport.count == 0
        await transport.handle_async_request(request(good_url, 1024))
        assert transport.count == 1 and transport.wire_body_sha256
        assert transport.wire_tool_schema_sha256 == replay.digest([{"type": "function"}])
        with pytest.raises(RuntimeError, match="one provider request"):
            await transport.handle_async_request(request(good_url, 1024))
        assert transport.inner.handle_async_request.await_count == 1
        await transport.aclose()
    asyncio.run(probe())


def test_existing_output_path_prevents_provider_call(tmp_path: Path, monkeypatch):
    evidence = tmp_path / "evidence.json"
    recorded = [dumpd(SystemMessage(content="recorded Supervisor prompt")),
                dumpd(HumanMessage(content="request"))]
    evidence.write_text(json.dumps({"trace_id": "offline-test", "llm_calls": [
        {"input": {"value": json.dumps({"messages": [recorded]})}}]}))
    profile = tmp_path / "profile"
    profile.write_text("provider=openai\nmodel=example\nAPI_ENDPOINT=http://127.0.0.1:9/v1\nAPI_KEY=dummy\n")
    output = tmp_path / "existing.json"
    output.write_text("preserve me")

    async def forbidden(*args, **kwargs):
        raise AssertionError("provider called before output path preflight")

    monkeypatch.setattr(replay, "run_once", forbidden)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--evidence", str(evidence),
                                  "--arm", "baseline", "--profile", str(profile),
                                  "--run", "--output", str(output)])
    with pytest.raises(FileExistsError):
        replay.main()
    assert output.read_text() == "preserve me"
