"""Offline checks for the provider error log fields live check's pass rules (no live Mythic, no provider).

Inputs, labelled:
- Real: case O's fields line is written by Sage's own handler (``ProviderErrorLogHandler``) through the mythic logger
  with the container's log format, from an error built with the installed OpenAI SDK's own error class.
- Synthetic: case A's fields line is the literal Sage's offline suite asserts for Anthropic's documented 401
  (``Payload_Type/sage/tests/test_provider_error_log_fields.py``; skills do not import the Anthropic SDK); the
  error bodies, the graph-error and model debug lines (in the shapes Sage's ``model.py`` writes them) and the
  mutations labelled in each test.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_provider_error_log_fields.py"
SPEC = importlib.util.spec_from_file_location("check_provider_error_log_fields", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))

ORACLES = {"O": {"status": 401, "type": "invalid_request_error", "code": "invalid_api_key", "param": None},
           "A": {"status": 401, "type": "authentication_error", "code": None, "param": None}}
OPENAI_401 = {"message": "Incorrect API key provided: sage-inv*****-key.", "type": "invalid_request_error",
              "param": None, "code": "invalid_api_key"}
ANTHROPIC_LINE = ("ERROR 2026-11-28 10:00:01,000 on_llm_error 173  : "
                  "Provider request failed: sdk=anthropic status=401 type=authentication_error code=- param=-")
CONTAINER_FORMAT = "%(levelname) -4s %(asctime)s %(funcName) -3s %(lineno) -5d: %(message)s"  # logging_fix.py


def _handler_line(case: str) -> str:
    """The line Sage's handler writes for this case's 401, formatted as the container log shows it."""
    if case == "A":
        return ANTHROPIC_LINE
    import httpx2
    import openai
    from mythic_container.logging import logger

    from ai.langgraph.operator_error import ProviderErrorLogHandler

    spec = check.CASES[case]
    response = httpx2.Response(401, request=httpx2.Request("POST", spec["endpoint"] + "/messages"))
    error = openai.AuthenticationError("Error code: 401 - " + OPENAI_401["message"], response=response,
                                       body=OPENAI_401)
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(logging.Formatter(CONTAINER_FORMAT).format(record))

    capture = _Capture(level=logging.DEBUG)
    logger.addHandler(capture)
    try:
        ProviderErrorLogHandler().on_llm_error(error)
    finally:
        logger.removeHandler(capture)
    assert len(records) == 1, records
    return records[0]


def _window(case: str, *, fields: list[str] | None = None, graph: str | None = None) -> list[str]:
    spec = check.CASES[case]
    debug = (f"DEBUG 2026-11-28 10:00:00,000 _get_base_chat_model 10003: Initializing model with "
             f"provider={spec['provider']}, model={spec['model']}, api_key=configured, base_url={spec['endpoint']}, "
             "openai_api=n/a, access_program=none, safety_identifier=none")
    graph_line = graph if graph is not None else (
        "ERROR 2026-11-28 10:00:01,000 invoke 19015: Error during graph execution: Error code: 401 - provider text")
    return [debug, *(fields if fields is not None else [_handler_line(case)]), graph_line]


def _outcomes(o: list[str], a: list[str]) -> dict:
    return {"O": check.window_outcome("O", o, ORACLES["O"]), "A": check.window_outcome("A", a, ORACLES["A"])}


def test_constants_match_the_plan():
    assert check.PLACEHOLDER == "sage-invalid-placeholder-key"
    assert check.PROMPT == "Reply with OK."
    assert {case: (s["provider"], s["model"], s["endpoint"], s["openai_api"]) for case, s in check.CASES.items()} == {
        "O": ("openai", "gpt-5.6-sol", "https://api.openai.com/v1", "responses"),
        "A": ("anthropic", "claude-sonnet-5-5", "https://api.anthropic.com", ""),
    }
    assert check.LIMITS == {"minutes": 15, "channels": 2, "provider_requests": 4, "target_tasks": 0}


def test_each_channel_carries_the_placeholder_key_and_its_own_route():
    for case, spec in check.CASES.items():
        config = check.channel_metadata(case)["config"]
        assert config["API_KEY"] == check.PLACEHOLDER
        assert (config["provider"], config["model"], config["API_ENDPOINT"]) == (
            spec["provider"], spec["model"], spec["endpoint"])
        assert config["mode"] == "conversation"
        assert config.get("SAGE_OPENAI_API", "") == spec["openai_api"]


def test_lines_from_sages_handler_pass_every_rule():
    result = check.verdict(_outcomes(_window("O"), _window("A")))
    assert result == {"verdict": "pass", "criteria": {"L1": True, "L2": True, "L3": True, "L4": True},
                      "not_run": []}


def test_no_fields_line_fails_its_case():
    """Synthetic: the window as a deployment without the handler would log it."""
    result = check.verdict(_outcomes(_window("O", fields=[]), _window("A")))
    assert result["verdict"] == "fail" and result["criteria"]["L1"] is False and result["criteria"]["L2"] is True


def test_two_lines_for_one_case_fail():
    line = _handler_line("A")
    result = check.verdict(_outcomes(_window("O"), _window("A", fields=[line, line])))
    assert result["criteria"]["L2"] is False


def test_a_field_that_differs_from_the_oracle_fails():
    """Synthetic: the handler line with another code."""
    line = _handler_line("O").replace("code=invalid_api_key", "code=invalid_key")
    result = check.verdict(_outcomes(_window("O", fields=[line]), _window("A")))
    assert result["criteria"]["L1"] is False


def test_another_sdks_line_does_not_count_for_this_case():
    """Synthetic: only an Anthropic line in the OpenAI case's window."""
    line = _handler_line("A")
    outcomes = _outcomes(_window("O", fields=[line]), _window("A"))
    assert check.verdict(outcomes)["criteria"]["L1"] is False
    assert outcomes["O"]["detail"]["other_fields_lines"] == [check._fields_text(line)]  # kept as an observation


@pytest.mark.parametrize("extra", [" " + "sage-invalid-placeholder-key", " message=Incorrect API key provided",
                                   " Reply with OK."])
def test_a_line_carrying_the_key_prompt_or_message_fails(extra):
    """Synthetic: the handler line with text appended."""
    line = _handler_line("O") + extra
    outcome = _outcomes(_window("O", fields=[line]), _window("A"))
    result = check.verdict(outcome)
    assert result["verdict"] == "fail"
    assert result["criteria"]["L1"] is False  # the line no longer matches the fields grammar exactly


def test_the_placeholder_inside_a_field_fails_l3():
    """Synthetic: a token-shaped value that is the placeholder itself."""
    line = _handler_line("O").replace("param=-", f"param={check.PLACEHOLDER}")
    oracles = {**ORACLES["O"], "param": check.PLACEHOLDER}
    outcome = check.window_outcome("O", _window("O", fields=[line]), oracles)
    assert outcome["fields"] is True and outcome["clean"] is False


def test_a_window_without_the_graph_error_line_did_not_run():
    result = check.verdict(_outcomes(_window("O"), _window("A", graph="")))
    assert result["verdict"] == "not_run" and result["not_run"] == ["A"] and result["criteria"]["L4"] is False


def test_a_graph_error_that_is_not_a_401_did_not_run():
    graph = "ERROR 2026-11-28 10:00:01,000 invoke 19015: Error during graph execution: Connection error."
    result = check.verdict(_outcomes(_window("O", graph=graph), _window("A")))
    assert result["verdict"] == "not_run" and result["not_run"] == ["O"]


def test_readiness_requires_a_401_with_an_error_type_at_each_endpoint():
    assert check.oracle_blockers(ORACLES) == []
    assert check.oracle_blockers({"O": ORACLES["O"]}) == [
        "case A: GET /v1/models with the placeholder returned None (no error), not 401"]
    assert check.oracle_blockers({**ORACLES, "O": {"status": 200}})[0].startswith("case O: GET /v1/models")
    assert check.oracle_blockers({**ORACLES, "A": {"status": 401, "type": None}}) == [
        "case A: the 401 body names no error type"]


def test_the_model_debug_line_is_recorded_as_an_observation():
    outcome = check.window_outcome("A", _window("A"), ORACLES["A"])
    assert outcome["detail"]["model_debug"] == [{"provider": "anthropic", "base_url": "https://api.anthropic.com"}]


def test_the_container_probe_compiles_and_records_no_message():
    compile(check._CONTAINER_PROBE, "container_probe", "exec")
    assert 'entry.update(type=error.get("type"), code=error.get("code"), param=error.get("param"))' in (
        check._CONTAINER_PROBE)
    assert "message" not in check._CONTAINER_PROBE
