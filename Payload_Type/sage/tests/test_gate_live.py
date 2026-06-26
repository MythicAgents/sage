"""Headless live Gate Experiment orchestration (gate_live.py) — unit-tested with injected reset + invoke,
so the wiring (reset-per-config, harness-per-scenario, fed into run_gate_experiment) is verified without
Mythic/GOAD.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
import gate_live  # noqa: E402
from live_runner import LiveConfig  # noqa: E402
from scenarios import goad_scenarios  # noqa: E402


def test_load_gate_configs_accepts_env_and_bare_forms(tmp_path):
    p = tmp_path / "configs.json"
    p.write_text(json.dumps({
        "prod": {"env": {"SAGE_MODEL": "strong"}},
        "weak": {"env": {"SAGE_MODEL": "weak"}},
        "bare": {"SAGE_MODEL": "x"},  # bare form (no explicit 'env' key)
    }))
    configs = gate_live.load_gate_configs(str(p))
    assert {c.name for c in configs} == {"prod", "weak", "bare"}
    assert next(c for c in configs if c.name == "prod").env == {"SAGE_MODEL": "strong"}
    assert next(c for c in configs if c.name == "bare").env == {"SAGE_MODEL": "x"}


def test_run_live_gate_experiment_resets_per_config_and_feeds_gate(tmp_path):
    scenarios = [s for s in goad_scenarios() if s.name == "child-da"]
    configs = [LiveConfig(f"c{i}", env={"SAGE_MODEL": str(i)}) for i in range(3)]
    seeds = 2
    resets, invokes = [], []

    def fake_reset(config, token):
        resets.append((str(config), token))
        return 7  # fresh sage_cb returned by the reset

    def fake_invoke(argv, env, out_dir):
        invokes.append(env.get("SAGE_ENGAGEMENT_ID"))
        case_id = scenarios[0].name
        return {"cases": [{"id": case_id, "seeds": [{"score": 0.5, "status": "done"} for _ in range(seeds)]}]}

    report = gate_live.run_live_gate_experiment(
        configs, scenarios, fake_reset, seeds=seeds, invoke=fake_invoke,
        results_dir=str(tmp_path), write_record=True, settle_timeout=0,
    )

    # Exactly one clean reset + one harness invocation per (config, scenario) — a fair gate.
    assert len(resets) == len(configs) * len(scenarios) == 3
    assert len(invokes) == 3
    # The SAME run token is minted once and threaded to BOTH the reset and the harness (the B/C fix: the
    # reader's ledger key only matches Sage's if the reset restarts Sage with this exact token).
    reset_tokens = {t for _, t in resets}
    assert all(reset_tokens) and len(reset_tokens) == 3      # fresh, non-empty token per batch
    assert reset_tokens == set(invokes)                       # reset token == harness SAGE_ENGAGEMENT_ID
    assert report.total_runs == len(configs) * len(scenarios) * seeds
    assert len(report.per_config) == len(configs)
    assert report.record_written is True
    assert (tmp_path / "gate_experiment.jsonl").exists()


def test_shipped_example_configs_load():
    # The ready-to-use gate config ladder must stay valid and have enough spread for the gate.
    p = Path(__file__).resolve().parents[3] / "skills" / "sage-eval-gauge" / "gate_configs.example.json"
    configs = gate_live.load_gate_configs(str(p))
    assert len(configs) >= 3
    assert all(c.env for c in configs)  # each carries an env overlay


def test_uniformly_empty_ground_truth_is_invalid(tmp_path):
    # Offline (no live Sage) every run reads an empty ledger -> zero ground-truth capability. The gate MUST
    # fail LOUD (INVALID), never emit a confident PASS/FAIL on no data (the Forge B/C catastrophe guard).
    scenarios = [s for s in goad_scenarios() if s.name == "child-da"]
    configs = [LiveConfig(f"c{i}", env={}) for i in range(3)]

    def fake_reset(config, token):
        return 7

    def fake_invoke(argv, env, out_dir):
        case_id = scenarios[0].name
        return {"cases": [{"id": case_id, "seeds": [{"score": 0.9, "status": "done"}]}]}  # high substring, no GT

    report = gate_live.run_live_gate_experiment(
        configs, scenarios, fake_reset, seeds=1, invoke=fake_invoke,
        results_dir=str(tmp_path), write_record=True, settle_timeout=0,
        # HERMETIC: stub the Option-A capture so it does not reach LIVE Mythic/LDAP. fake_reset doesn't wipe
        # the lab, so a real capture would read leftover krbtgt creds (KRBTGT_DUMPED=true, cap 0.444) and break
        # the "empty ground truth" premise this test exercises. The guard itself is verified by the empty result.
        capture_probes_fn=lambda scn, eid: {},
    )
    assert report.verdict == "INVALID"
    assert "ground truth" in report.note.lower()
