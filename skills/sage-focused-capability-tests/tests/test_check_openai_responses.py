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


# --------------------------------------------------------------------------------------------------
# Revision 2: P3 grades the change's own effect; calling agent and cards are observations. P7 reads the
# deployed controls from Mythic's stored model definition.
# --------------------------------------------------------------------------------------------------

def _span(produced=(), consumed=(), agent="Supervisor"):
    return {"model": "gpt-6-cyber", "prompt_tokens": 1, "completion_tokens": 1, "produced_calls": list(produced),
            "consumed_results": list(consumed), "calling_agents": [agent] if produced else []}


def _row(text, **metadata):
    return {"id": 1, "author_type": "ai", "message": text, "metadata": metadata}


NOTICE = "⚠️ **BloodHound is not connected**, so attack-graph analysis is unavailable."
FINAL = "📊 **Final Report** The label is amber-seven. One callback: CASTELBLACK."


def test_p3_passes_on_a_round_trip_and_one_answer_row_with_no_card():
    rows = {"status": "complete", "messages": [_row(NOTICE), _row(FINAL, turn_outcome="answered")]}
    result = check.tool_step_result([_span(produced=["call_a"]), _span(consumed=["call_a"])], rows)
    assert result["passed"] is True
    assert result["calling_agents"] == ["Supervisor"] and result["card_rows"] == 0


def test_p3_passes_with_worker_cards_and_records_them():
    card = _row("", tool_use={"name": "list_callbacks", "status": "completed"})
    rows = {"status": "complete", "messages": [card, _row(FINAL)]}
    result = check.tool_step_result([_span(produced=["call_a"], agent="Mythic_Operator"),
                                     _span(consumed=["call_a"])], rows)
    assert result["passed"] is True
    assert result["calling_agents"] == ["Mythic_Operator"] and result["card_rows"] == 1


@pytest.mark.parametrize("case", ["unconsumed", "no_call", "duplicate_answer", "empty_answer", "not_complete"])
def test_p3_fails_when_the_change_effect_is_missing(case):
    spans = [_span(produced=["call_a"]), _span(consumed=["call_a"])]
    messages = [_row(FINAL)]
    status = "complete"
    if case == "unconsumed":
        spans = [_span(produced=["call_a"]), _span(consumed=["call_other"])]
    elif case == "no_call":
        spans = [_span(), _span()]
    elif case == "duplicate_answer":
        messages = [_row(FINAL), _row(FINAL)]
    elif case == "empty_answer":
        messages = [_row(NOTICE), _row("   ")]
    else:
        status = "error"
    assert check.tool_step_result(spans, {"status": status, "messages": messages})["passed"] is False


def _subscriptions(options):
    sage = {"name": "Sage", "metadata": {"configuration_options": options}}
    watcher = {"name": "Sage Watcher", "metadata": {"configuration_options": []}}
    return [json.dumps(sage), json.dumps(watcher)]


def _deployed_options():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"))
    from sage_chat.models import SAGE_MODELS

    return [option.to_json() for option in SAGE_MODELS[0].Metadata.ConfigurationOptions]


def test_p7_accepts_the_specified_controls_after_provider_and_model():
    assert check.deployed_controls_error(_subscriptions(_deployed_options())) == ""


@pytest.mark.parametrize("mutate", ["missing", "reordered", "relabelled", "value", "default", "moved"])
def test_p7_rejects_a_changed_control(mutate):
    options = _deployed_options()
    index = {option["name"]: i for i, option in enumerate(options)}
    daybreak = options[index["SAGE_OPENAI_ACCESS_PROGRAM"]]
    if mutate == "missing":
        options.pop(index["SAGE_OPENAI_SAFETY_IDENTIFIER"])
    elif mutate == "reordered":
        a, b = index["SAGE_OPENAI_API"], index["SAGE_OPENAI_ACCESS_PROGRAM"]
        options[a], options[b] = options[b], options[a]
    elif mutate == "relabelled":
        daybreak["display_name"] = "Daybreak access"
    elif mutate == "value":
        daybreak["choices"][1]["value"] = "off"
    elif mutate == "default":
        daybreak["default_value"] = "standard"
    else:
        options.append(options.pop(index["SAGE_OPENAI_API"]))
    assert check.deployed_controls_error(_subscriptions(options))
