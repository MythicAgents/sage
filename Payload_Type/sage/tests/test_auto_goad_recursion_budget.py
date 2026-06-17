import importlib
import sys
from pathlib import Path

import pytest


def _load_model_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        return importlib.import_module("ai.langgraph.model")
    except Exception as e:
        pytest.skip(f"model.py runtime unavailable: {e}")


def _model_stub(mod, *, autonomous_solve: bool, max_steps: int):
    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = autonomous_solve
    model._max_steps = max_steps
    return model


def test_autonomous_zero_max_steps_maps_to_positive_graph_budget():
    mod = _load_model_module()
    model = _model_stub(mod, autonomous_solve=True, max_steps=0)

    assert model._graph_recursion_limit() == mod._AUTONOMOUS_UNBOUNDED_GRAPH_RECURSION_LIMIT
    assert model._graph_recursion_limit() >= 1
    assert model._graph_run_config("thread-1") == {
        "configurable": {"thread_id": "thread-1"},
        "recursion_limit": mod._AUTONOMOUS_UNBOUNDED_GRAPH_RECURSION_LIMIT,
    }


@pytest.mark.parametrize(
    ("autonomous_solve", "max_steps"),
    [
        (False, 0),
        (False, 300),
        (True, 300),
    ],
)
def test_graph_budget_stays_bounded_without_autonomous_zero(autonomous_solve, max_steps):
    mod = _load_model_module()
    model = _model_stub(mod, autonomous_solve=autonomous_solve, max_steps=max_steps)

    assert model._graph_recursion_limit() == mod._DEFAULT_GRAPH_RECURSION_LIMIT


def test_guided_goad_runner_requests_unbounded_auto_steps():
    repo_root = Path(__file__).resolve().parents[3]
    runner = repo_root / "skills" / "sage-live-runner" / "scripts" / "run_essos_da.py"
    source = runner.read_text(encoding="utf-8")

    assert "AUTO_GOAD_MAX_STEPS = 0" in source
    assert '"max_steps": AUTO_GOAD_MAX_STEPS' in source
