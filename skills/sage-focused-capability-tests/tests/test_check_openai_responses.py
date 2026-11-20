"""Offline checks for the OpenAI Responses live-check tool's local logic (no Mythic, no provider)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_openai_responses.py"
SPEC = importlib.util.spec_from_file_location("check_openai_responses", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def _route(tmp_path, body: str, mode: int = 0o600) -> Path:
    path = tmp_path / "route.env"
    path.write_text(body)
    path.chmod(mode)
    return path


GOOD = "API_ENDPOINT=https://api.openai.com/v1\nmodel=gpt-6-cyber\nSAGE_OPENAI_SAFETY_IDENTIFIER=user_a17f\n"


def test_route_file_is_owner_only_keyless_and_complete(tmp_path):
    route = check.read_route(_route(tmp_path, GOOD))
    assert check.route_summary(route) == {"endpoint": "https://api.openai.com/v1", "model": "gpt-6-cyber",
                                          "safety_identifier": "configured", "direct_openai": True}
    with pytest.raises(SystemExit, match="owner-only"):
        check.read_route(_route(tmp_path, GOOD, 0o644))
    with pytest.raises(SystemExit, match="must not contain an API key"):
        check.read_route(_route(tmp_path, GOOD + "API_KEY=sk-placeholder\n"))
    with pytest.raises(SystemExit, match="SAGE_OPENAI_SAFETY_IDENTIFIER"):
        check.read_route(_route(tmp_path, "API_ENDPOINT=https://api.openai.com/v1\nmodel=gpt-6-cyber\n"))


def test_channel_configs_carry_the_route_but_never_a_key(tmp_path):
    route = check.read_route(_route(tmp_path, GOOD))
    first = check.channel_config(route, api="chat_completions")["config"]
    corrected = check.channel_config(route, api="responses", access_program="daybreak_red")["config"]
    assert first["SAGE_OPENAI_API"] == "chat_completions" and "SAGE_OPENAI_ACCESS_PROGRAM" not in first
    assert corrected["SAGE_OPENAI_API"] == "responses"
    assert corrected["SAGE_OPENAI_ACCESS_PROGRAM"] == "daybreak_red"
    for config in (first, corrected):
        assert config["mode"] == "conversation" and config["autonomous_solve"] is False
        assert config["max_steps"] == check.MAX_STEPS
        assert not any("KEY" in key for key in config)


def _phoenix(tmp_path) -> Path:
    db = tmp_path / "phoenix.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY, span_kind TEXT, attributes TEXT, "
                 "llm_token_count_prompt INTEGER, llm_token_count_completion INTEGER)")
    call = {"llm": {"model_name": "gpt-6-cyber", "output_messages": [{"message.role": "assistant",
            "message.tool_calls": [{"tool_call.id": "call_a", "tool_call.function.name": "list_callbacks"}]}]}}
    result = {"llm": {"model_name": "gpt-6-cyber", "input_messages": [
        {"message.role": "user", "message.content": "status"},
        {"message.role": "tool", "message.tool_call_id": "call_a", "message.content": "[]"}]}}
    rows = [(1, "LLM", json.dumps(call), 100, 20), (2, "TOOL", "{}", 0, 0), (3, "LLM", json.dumps(result), 120, 30)]
    conn.executemany("INSERT INTO spans VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def test_model_spans_pair_a_produced_call_with_its_consumed_result(tmp_path):
    db = _phoenix(tmp_path)
    assert check.phoenix_mark(db) == 3
    spans = check.model_spans(db, 0)
    assert [s["id"] for s in spans] == [1, 3]
    assert spans[0]["produced_calls"] == ["call_a"] and spans[1]["consumed_results"] == ["call_a"]
    assert check.model_spans(db, 1, 2) == []


def test_ceilings_stop_on_calls_tokens_or_output():
    span = {"prompt_tokens": 10, "completion_tokens": 10}
    assert check._over_ceiling([span] * 16, check.time.monotonic()) == ""
    assert check._over_ceiling([span] * 17, check.time.monotonic()) == "provider call ceiling"
    assert check._over_ceiling([{"prompt_tokens": 1, "completion_tokens": 5000}],
                               check.time.monotonic()) == "output token ceiling"
    assert check._over_ceiling([span], check.time.monotonic() - 16 * 60) == "wall-time ceiling"
