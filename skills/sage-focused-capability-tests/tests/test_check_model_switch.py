"""Offline checks for the model-switch live-check tool's local logic (no Mythic, no provider)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_model_switch.py"
SPEC = importlib.util.spec_from_file_location("check_model_switch", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

A, B = "gpt-6-cyber", "gpt-5.6-sol"
GOOD = (f"API_ENDPOINT=https://api.openai.com/v1\nmodel={A}\nmodel_b={B}\n"
        "SAGE_OPENAI_SAFETY_IDENTIFIER=user_a17f\n")


def _route(tmp_path, body: str) -> Path:
    path = tmp_path / "route.env"
    path.write_text(body)
    path.chmod(0o600)
    return path


def test_route_file_needs_model_b_and_distinct_names(tmp_path):
    assert check.read_route(_route(tmp_path, GOOD))["model_b"] == B
    with pytest.raises(SystemExit, match="model_b"):
        check.read_route(_route(tmp_path, GOOD.replace(f"model_b={B}\n", "")))
    with pytest.raises(SystemExit, match="must all differ"):
        check.read_route(_route(tmp_path, GOOD.replace(f"model_b={B}", f"model_b={A}")))


def test_first_settings_are_an_invalid_route_with_the_wrong_model():
    config = check.channel_config({"API_ENDPOINT": "https://api.openai.com/v1",
                                   "SAGE_OPENAI_SAFETY_IDENTIFIER": "user_a17f"})["config"]
    assert config["model"] == check.WRONG_A
    assert (config["SAGE_OPENAI_API"], config["SAGE_OPENAI_ACCESS_PROGRAM"]) == ("chat_completions", "daybreak_red")
    assert "API_KEY" not in config


def test_span_names_a_model_or_its_dated_snapshot_only():
    assert check.names("gpt-6-cyber", A) and check.names("gpt-6-cyber-2026-11-01", A)
    assert check.names("gpt-6-cyber", "gpt-6")  # a dash-suffixed name counts; the route forbids such overlaps
    assert not check.names(B, A) and not check.names("nonexistent-model-b", A)


def test_declared_model_command_and_usable_keys():
    sage = {"name": "Sage", "metadata": {"slash_commands": [{"name": "mode"}, {"name": "model"}]}}
    assert check.declares_model_command([json.dumps(sage)])
    assert not check.declares_model_command([json.dumps({"name": "Sage", "metadata": {"slash_commands": []}})])
    assert check.usable_key([{"id": 4, "scopes": ["*"]}])["id"] == 4
    assert check.usable_key([{"id": 5, "scopes": ["apitoken.write", "chat-ai.write"]}]) is None
    assert check.usable_key([{"id": 6, "scopes": sorted(check.REQUIRED_KEY_SCOPES)}])["id"] == 6


def _request(status, *texts):
    return {"status": status, "messages": [{"message": text} for text in texts]}


def _passing():
    run = {"readiness": {"route_models": {"a": A, "b": B}}, "task_baseline": 9, "started_at": 0, "finished_at": 600,
           "steps": {f"S{i}": {"tasks_after": 9} for i in range(1, 9)},
           "stored_after_s3": {"model": A, "chip": {"key": "cfg_model", "click": "/model"}}}
    rows = {
        "S1": [_request("error", "OpenAI Daybreak (SAGE_OPENAI_ACCESS_PROGRAM=daybreak_red) requires OpenAI API = Responses")],
        "S2": [_request("error", f"The model provider rejected this request (HTTP 404): The model `{check.WRONG_A}` does "
                                 "not exist. To fix it, change **Model** (or run `/model <name>`)")],
        "S3": [_request("complete", f"This chat's model is `{check.WRONG_A}` (ui-config)."),
               _request("complete", f"Model set to `{A}` in this chat's settings.")],
        "S4": [_request("complete", f"This chat now uses model `{A}` (was `{check.WRONG_A}`).", "Noted amber-seven.")],
        "S5": [_request("complete", "One callback.")],
        "S6": [_request("complete", "Your label is amber-seven.")],
        "S7": [_request("complete", f"Model set to `{check.WRONG_B}`"),
               _request("error", f"... model `{check.WRONG_B}` ... rejected ...")],
        "S8": [_request("complete", f"Model set to `{A}`"), _request("complete", "amber-seven")],
    }
    span = lambda model: {"model": model, "prompt_tokens": 100, "completion_tokens": 10}  # noqa: E731
    spans = {"S1": [], "S2": [span(check.WRONG_A)], "S3": [], "S4": [span(A)], "S5": [span(A), span(A)],
             "S6": [span(B)], "S7": [span(check.WRONG_B)], "S8": [span(A)]}
    return run, rows, spans, {"passed": True, "round_trip_calls": ["call_1"]}


def test_every_rule_passes_on_a_passing_run():
    criteria = check.criteria_for(*_passing())
    assert all(criteria.values()), criteria


@pytest.mark.parametrize("mutate,rule", [
    (lambda run, rows, spans: spans["S1"].append({"model": check.WRONG_A, "prompt_tokens": 1, "completion_tokens": 1}), "S1"),
    (lambda run, rows, spans: rows["S2"][0]["messages"][0].update(message="Sage restarted or this channel's session was replaced"), "S2"),
    (lambda run, rows, spans: run["stored_after_s3"].update(model=check.WRONG_A), "S3"),
    (lambda run, rows, spans: spans["S4"].append({"model": B, "prompt_tokens": 1, "completion_tokens": 1}), "S4"),
    (lambda run, rows, spans: rows["S6"][0]["messages"][0].update(message="I do not know."), "S6"),
    (lambda run, rows, spans: spans["S7"].append({"model": A, "prompt_tokens": 1, "completion_tokens": 1}), "S7"),
    (lambda run, rows, spans: spans["S8"].__setitem__(0, {"model": B, "prompt_tokens": 1, "completion_tokens": 1}), "S8"),
    (lambda run, rows, spans: rows["S5"][0].update(status="streaming"), "terminal"),
    (lambda run, rows, spans: run["steps"]["S5"].update(tasks_after=10), "tasks_and_ceilings"),
    (lambda run, rows, spans: run.update(finished_at=21 * 60), "tasks_and_ceilings"),
])
def test_each_rule_fails_on_its_own_breach(mutate, rule):
    run, rows, spans, tool = _passing()
    mutate(run, rows, spans)
    assert not check.criteria_for(run, rows, spans, tool)[rule]
