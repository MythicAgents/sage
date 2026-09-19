"""Assembled offline reporting CLIs refuse unlabeled performance claims."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type" / "sage"))

from ai.hillclimb import gate_experiment as gx  # noqa: E402


def _identity(arm: str, seed: int) -> dict:
    return {
        "protocol_id": "m1-reporting", "pair_id": "pair-1", "logical_seed": seed,
        "policy_arm": arm, "model_provider": "test-provider", "model_id": "test-model",
        "model_version": "v1", "backend_route": "offline", "generation_config": {"temperature": 0},
        "wrapper_id": "sage", "wrapper_revision": "r1", "authority_mode": "auto",
        "execution_path": "policy_selected_controller", "source_version": "source-v1",
    }


def _card(capability: float, *, identity: dict | None) -> dict:
    return {
        "scenario": "reporting-scenario", "verifier_hash": "sha256:reporting",
        "furthest_milestone": "FOOTHOLD", "capability": capability,
        "milestones_reached": 1, "milestones": {"FOOTHOLD": True},
        "substring_score": capability, "metric_capability_gap": 0.0,
        "tool_calls": 1, "model_calls": 1, "productive_action_ratio": capability,
        "turn_efficiency": capability, "status": "pass", "recursion_deaths": 0,
        "error_count": 0, "total_tokens": 1, "wall_seconds": 1.0,
        "measurement_identity": identity,
    }


def _run(*args: str):
    return subprocess.run(
        [sys.executable, "-m", "ai.hillclimb", *args], cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "Payload_Type" / "sage")}, text=True, capture_output=True,
        check=False,
    )


def test_noise_floor_cli_refuses_unlabeled_cards_without_printing_performance_numbers(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps({"side": "harness", "scenario": "reporting-scenario", "card": _card(v, identity=None)})
                              for v in (0.0, 1.0)) + "\n")

    completed = _run("noise-floor", "--scenario", "reporting-scenario", "--results", str(path))

    assert completed.returncode == 2
    assert "performance assertion refused" in completed.stderr
    assert "MDE=" not in completed.stdout
    assert "capability mean=" not in completed.stdout


def test_labeled_retained_gate_and_noise_floor_clis_emit_offline_reports(tmp_path):
    results = tmp_path / "results.jsonl"
    rows = [
        {"side": "harness", "scenario": "reporting-scenario", "card": _card(0.2, identity=_identity("a", 0))},
        {"side": "harness", "scenario": "reporting-scenario", "card": _card(0.8, identity=_identity("a", 1))},
    ]
    results.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    noise = _run("noise-floor", "--scenario", "reporting-scenario", "--results", str(results))
    assert noise.returncode == 0
    assert "MDE=" in noise.stdout
    assert '"performance_assertion_allowed": true' in noise.stdout

    retained = tmp_path / "gate.jsonl"
    gate_rows = [
        {"config": name, "card": _card(value, identity=_identity(name, 0))}
        for name, value in (("low", 0.2), ("mid", 0.5), ("high", 0.9))
    ]
    retained.write_text("\n".join(json.dumps(row) for row in gate_rows) + "\n")
    gate = _run("gate-experiment", "--scorecards", str(retained), "--seeds", "1")
    assert gate.returncode == 0
    assert '"performance_assertion_allowed": true' in gate.stdout
    assert "VERDICT: PASS" in gate.stdout


def test_retained_gate_cli_refuses_unlabeled_cards(tmp_path):
    retained = tmp_path / "gate.jsonl"
    retained.write_text(json.dumps({"config": "legacy", "card": _card(0.5, identity=None)}) + "\n")
    completed = _run("gate-experiment", "--scorecards", str(retained))
    assert completed.returncode == 2
    assert "performance assertion refused" in completed.stderr
    assert "rho=" not in completed.stdout


def test_verify_claims_cli_uses_safe_offline_failure_for_a_named_manifest(tmp_path):
    completed = _run("verify-claims", str(tmp_path / "missing-manifest.json"))
    assert completed.returncode == 2
    assert completed.stdout == (
        '{"verified": false, "error": "claim reproduction failed; check labels, source/evaluator bindings, dependencies and asserted output"}\n'
    )


def test_pure_gate_aggregate_never_writes_a_default_record(monkeypatch):
    def fail_write(*_args, **_kwargs):
        raise AssertionError("pure aggregate attempted a durable write")

    monkeypatch.setattr(gx, "write_gate_record", fail_write)
    cards = {
        name: [gx.ScoreCard(**_card(value, identity=_identity(name, 0)))]
        for name, value in (("low", 0.2), ("mid", 0.5), ("high", 0.9))
    }
    report = gx.aggregate_gate_experiment(cards, seeds=1)
    assert report.record_written is False
    assert report.measurement["performance_assertion_allowed"] is True
