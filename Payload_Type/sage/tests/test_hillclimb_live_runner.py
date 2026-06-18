"""Live-runner tests (eval gauge Phase 0).

The lab subprocess (`_invoke`) is injected, so command construction, report parsing, ScoreCard
composition, and batch memoization are all tested WITHOUT Mythic/GOAD. The real `_invoke` (harness
subprocess + report read) is the operator's lab step and is intentionally not exercised here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import live_runner as lr  # noqa: E402
import gate_experiment as gx  # noqa: E402
from scenarios import goad_scenarios  # noqa: E402


def _synthetic_report(case_id, scores):
    return {"schema_version": 2, "sage_cb": 13, "cases": [{
        "id": case_id,
        "seeds": [{"score": s, "status": "pass", "tool_calls": 10, "model_calls": 20,
                   "recursion_deaths": 0, "total_tokens": 500, "wall_seconds": 3.0,
                   "errors": [], "per_agent_tokens": {}} for s in scores],
    }]}


def _hermetic(monkeypatch, tmp_path):
    # No real ledger/trajectory: C1/C1b read empty -> capability 0, but the plumbing is exercised.
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "eng"))
    monkeypatch.setenv("SAGE_TRAJECTORY_STORE", str(tmp_path / "nope.jsonl"))


def test_build_command_has_harness_args_and_env(monkeypatch, tmp_path):
    _hermetic(monkeypatch, tmp_path)
    runner = lr.LiveRunner(sage_cb=13, out_dir=str(tmp_path), db="/x/phoenix.db")
    scn = goad_scenarios("Operation_Test")[0]  # name == "single-hop-system"
    argv, env, token = runner.build_command(lr.LiveConfig("weak", {"SAGE_PROMPT_VARIANT": "weak"}), scn, seeds=3)
    assert argv[:4] == [runner.python_exe, "-m", "evals.harness", "run"]
    assert "--only" in argv and "single-hop-system" in argv
    assert "--seeds" in argv and "3" in argv
    assert "--sage-cb" in argv and "13" in argv
    assert env["SAGE_PROMPT_VARIANT"] == "weak"          # config env overlay applied
    assert token.startswith("gauge-weak-single-hop-system-")
    assert env["SAGE_ENGAGEMENT_ID"] == token            # FRESH per-run id, not the static scenario id
    assert env["SAGE_TRAJECTORY_RUN_ID"] == token        # same token keys the trajectory store


def test_parse_report_extracts_seed_records():
    recs = lr.parse_report(_synthetic_report("single-hop-system", [0.8, 0.6]), "single-hop-system")
    assert [r["score"] for r in recs] == [0.8, 0.6]
    try:
        lr.parse_report(_synthetic_report("x", [0.1]), "missing")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_run_batch_builds_one_scorecard_per_seed(monkeypatch, tmp_path):
    _hermetic(monkeypatch, tmp_path)
    scn = goad_scenarios("Op")[0]
    fake = lambda argv, env, out: _synthetic_report("single-hop-system", [0.8, 0.6, 0.9])
    runner = lr.LiveRunner(sage_cb=13, out_dir=str(tmp_path), invoke=fake)
    cards = runner.run_batch(lr.LiveConfig("prod"), scn, seeds=3)
    assert len(cards) == 3
    assert [round(c.substring_score, 2) for c in cards] == [0.8, 0.6, 0.9]
    assert all(c.verifier_hash.startswith("sha256:") for c in cards)


def test_as_run_fn_memoizes_one_batch_per_config_scenario(monkeypatch, tmp_path):
    _hermetic(monkeypatch, tmp_path)
    scn = goad_scenarios("Op")[0]
    calls = []

    def fake(argv, env, out):
        calls.append(1)
        return _synthetic_report("single-hop-system", [0.8, 0.6])

    runner = lr.LiveRunner(sage_cb=13, out_dir=str(tmp_path), invoke=fake)
    rep = gx.run_gate_experiment([lr.LiveConfig("a")], [scn], runner.as_run_fn(2),
                                 seeds=2, write_record=False)
    assert rep.total_runs == 2
    assert len(calls) == 1  # one harness invocation for the (config, scenario) batch, not per-seed
