"""Baseline probes: what the current supervised path does with the delegation shapes channel 3 exposed.

These are MEASUREMENTS, not desired behaviour. Each test asserts what the real assembled graph does today, so the
supervised orchestration plan's later slices flip them deliberately (a brief gate, per-step bounds) instead of
discovering the old behaviour by accident. Only the model is scripted; the graph, handoff tools, middleware and
state channels are real (see test_scripted_handoff.py for the harness and its stated seams).

The worker's tool is a neutral, test-only counter added to Mythic_Operator's tool list, so the probes measure
when control returns to the Supervisor without exercising any real Mythic or tradecraft tool.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
import pytest

_HARNESS_PATH = Path(__file__).resolve().with_name("test_scripted_handoff.py")
_SPEC = importlib.util.spec_from_file_location("scripted_handoff_harness", _HARNESS_PATH)
_harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_harness)

from ai.langgraph import model as model_module  # noqa: E402

PROMPT = "From your current foothold, obtain administrative control of the target domain"
PROBE_CALLS = 30


def _probe_tool(calls: list[int]):
    @tool
    def baseline_probe_step(step: int) -> str:
        """Test-only probe. Records one worker step and returns immediately."""
        calls.append(step)
        return f"probe step {step} recorded"
    return baseline_probe_step


@pytest.fixture
def probe_calls(monkeypatch):
    calls: list[int] = []
    original = model_module.filter_tools_by_frontmatter
    probe = _probe_tool(calls)

    def with_probe(agent: str, tools):
        selected = original(agent, tools)
        return [*selected, probe] if agent == "mythic_operator" else selected

    monkeypatch.setattr(model_module, "filter_tools_by_frontmatter", with_probe)
    return calls


def _handoff(args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "transfer_to_Mythic_Operator", "args": args, "id": "call_handoff_1", "type": "tool_call"}])


def _probe_step(step: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "baseline_probe_step", "args": {"step": step}, "id": f"probe_{step}", "type": "tool_call"}])


def _run(script: list[AIMessage], *, recursion_limit: int) -> dict:
    harness = _harness._Harness(script)
    original_limit = _harness._RECURSION_LIMIT
    _harness._RECURSION_LIMIT = recursion_limit
    try:
        return asyncio.run(harness.run(PROMPT, assisted=True))
    finally:
        _harness._RECURSION_LIMIT = original_limit


def _worker_received(result: dict) -> list[str]:
    return [str(m.content) for m in result.get("mythic_operator_messages", [])
            if type(m).__name__ == "HumanMessage"]


@pytest.mark.parametrize("args", [
    {"handoff_title": "List active callbacks"},                                            # title only
    {"handoff_title": "Take the domain", "handoff_instruction": "Objective: " + PROMPT},   # restates the objective
], ids=["title-only", "restates-objective"])
def test_baseline_any_brief_shape_is_dispatched_and_the_worker_gets_the_operator_input(args, probe_calls):
    result = _run([_handoff(args), AIMessage(content="worker done"), AIMessage(content="final")],
                  recursion_limit=8)

    assert PROMPT in _worker_received(result)  # dispatched: no brief check rejects it today


def test_baseline_a_worker_runs_every_step_it_chooses_before_control_returns(probe_calls):
    script = [_handoff({"handoff_title": "Work the objective", "handoff_instruction": "Objective: " + PROMPT}),
              *(_probe_step(step) for step in range(PROBE_CALLS)),
              AIMessage(content="worker done"),
              AIMessage(content="final")]

    _run(script, recursion_limit=250)

    # Today nothing bounds a delegation: all scripted worker steps run before the Supervisor sees control again.
    assert probe_calls == list(range(PROBE_CALLS))
