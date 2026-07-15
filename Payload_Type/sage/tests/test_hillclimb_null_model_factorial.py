import json
import subprocess
from pathlib import Path

from ai.hillclimb.null_model_factorial import run_null_model_factorial
from ai.hillclimb import headless_solver, live_seams


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT.parents[1] / ".venv" / "bin" / "python"
ORCHESTRATE = ROOT.parents[1] / "skills" / "sage-eval-gauge" / "scripts" / "orchestrate.py"


def test_null_model_factorial_passes_all_policy_and_telemetry_invariants():
    report = run_null_model_factorial()

    assert report["verdict"] == "PASS"
    assert report["failures"] == {}
    rows = {row["policy_mode"]: row for row in report["rows"]}

    assert rows["symbolic"]["executed_capabilities"] == ["test-capability"]
    assert rows["symbolic"]["telemetry"]["semantic_transaction_count"] == 1
    assert rows["symbolic"]["scorecard"]["objective_proven"] is True
    assert rows["symbolic"]["scorecard"]["objective_clean_stop"] is True

    hybrid = rows["hybrid"]
    assert hybrid["executed_capabilities"] == ["test-capability"]
    assert hybrid["telemetry"]["semantic_transaction_count"] == 1
    assert hybrid["telemetry"]["decisions"][0]["decision_owner"] == "kernel_singleton"
    assert hybrid["scorecard"]["objective_proven"] is True
    assert hybrid["scorecard"]["objective_clean_stop"] is True

    llm = rows["llm"]
    assert llm["executed_capabilities"] == []
    assert llm["telemetry"]["semantic_transaction_count"] == 0
    assert llm["telemetry"]["policy_switches"] == []
    assert llm["scorecard"]["objective_proven"] is False
    assert llm["scorecard"]["clean_stop"] is True
    assert llm["scorecard"]["objective_clean_stop"] is False


def test_null_model_factorial_cli_is_reproducible_and_offline():
    result = subprocess.run(
        [str(PY), "-m", "ai.hillclimb", "null-model-factorial"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout.split("\nVERDICT:", 1)[0])
    assert payload["kind"] == "null_model_factorial"
    assert payload["verdict"] == "PASS"


def test_headless_null_model_ablation_removes_policy_seam(monkeypatch):
    captured = {}

    class FakeModel:
        def __init__(self, **_kwargs):
            self.llm = object()

        async def initialize(self):
            return None

        async def invoke(self, _objective):
            captured["llm"] = self.llm

        def controller_runtime_telemetry(self):
            return {"model_calls": 0}

    monkeypatch.setattr("ai.langgraph.model.Model", FakeModel)
    result = __import__("asyncio").run(headless_solver.run_headless_solve(
        "objective",
        client=object(),
        operation_id=1,
        engagement_id="factorial",
        null_model=True,
        return_details=True,
    ))

    assert captured["llm"] is None
    assert result["runtime_telemetry"]["model_calls"] == 0


def test_make_headless_solver_forwards_null_model(monkeypatch):
    captured = {}

    async def fake_run(_objective, **kwargs):
        captured.update(kwargs)
        return {"status": "completed", "runtime_telemetry": {}}

    monkeypatch.setattr(headless_solver, "run_headless_solve", fake_run)
    solve = live_seams.make_headless_solver(
        object(),
        engagement_id="factorial",
        policy_mode="hybrid",
        null_model=True,
    )

    assert solve("objective") == "completed"
    assert captured["policy_mode"] == "hybrid"
    assert captured["null_model"] is True


def test_orchestrator_null_model_factorial_plans_all_three_policies():
    result = subprocess.run(
        [
            str(PY),
            str(ORCHESTRATE),
            "--scenario",
            "cross-forest-objective",
            "--side",
            "harness",
            "--null-model-factorial",
        ],
        cwd=ROOT.parents[1],
        capture_output=True,
        text=True,
        check=True,
    )

    for mode in ("symbolic", "llm", "hybrid"):
        assert f"--policy-mode {mode}" in result.stdout
    assert result.stdout.count("--null-model") >= 3
