"""CLI for the Sage eval gauge (Phase 0) and additive offline benchmarks.

  python -m ai.hillclimb gate-experiment --dry-run        # runnable now; synthetic runner
  python -m ai.hillclimb null-model-factorial             # no lab or model calls
  python -m ai.hillclimb decision-benchmark run --dry-run  # no lab or model calls
  python -m ai.hillclimb operator-replay run --dry-run     # no lab or model calls
  python -m ai.hillclimb policy-replay-selector-experiment # no lab or model calls
  python -m ai.hillclimb policy-replay-unseen-candidate-evaluate # no lab or model calls
  python -m ai.hillclimb policy-replay-hillclimb-iteration # no lab or model calls
  python -m ai.hillclimb policy-replay-promotion-gate # no lab or model calls
  python -m ai.hillclimb phase4-readiness-report      # no lab or model calls
  python -m ai.hillclimb phase5-full-frontier-t3      # no lab; add --run-model-matrix for weak/strong calls
  python -m ai.hillclimb target-disambiguation-contract-audit # no lab or model calls
  python -m ai.hillclimb target-value-census             # no lab or model calls
  python -m ai.hillclimb target-value-proofability-screen # no lab or model calls
  python -m ai.hillclimb target-value-runtime-decision    # no lab or model calls
  python -m ai.hillclimb gpo-dc-scope-late-blocker-contract-validate # no lab or model calls
  python -m ai.hillclimb gpo-dc-scope-late-blocker-authorization-audit # no lab or model calls
  python -m ai.hillclimb gpo-dc-scope-live-surface-validate --evidence <json> # no model calls
  python -m ai.hillclimb gpo-dc-scope-canary-validate --results <jsonl> # no model calls
  python -m ai.hillclimb gpo-dc-scope-matrix-validate --results <jsonl> # no model calls
  python -m ai.hillclimb laps-family-transfer-holdout-validate # no lab or model calls
  python -m ai.hillclimb laps-family-transfer-live-surface-validate --evidence <json> # no model calls
  python -m ai.hillclimb laps-family-transfer-canary-validate --results <jsonl> # no model calls
  python -m ai.hillclimb laps-family-transfer-matrix-validate --forced-results <jsonl> --policy-results <jsonl> # no model calls
  python -m ai.hillclimb trust-context-corroboration-validate # no lab or model calls
  python -m ai.hillclimb trust-context-corroboration-control-validate --evidence <json> # no model calls
  python -m ai.hillclimb trust-context-corroboration-live-validate --results <jsonl> --control-report <json> # no model calls
  python -m ai.hillclimb phase8-goad-regression-validate --results <jsonl> # no lab or model calls
  python ai/hillclimb/__main__.py gate-experiment --dry-run

A LIVE gate-experiment needs the GOAD lab (each config run through evals/harness.py, scored by
C1/C1b/C2 via gate_experiment.build_scorecard_from_run). That seam is intentionally not faked.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

try:  # package import
    from . import gate_experiment as gx
    from . import gate_live
    from . import decision_benchmark
    from . import null_model_factorial
    from . import operator_replay_benchmark
    from . import policy_replay_calibration
    from . import policy_replay_corpus
    from . import policy_replay_selector_experiment
    from . import policy_replay_unseen_candidate_evaluator
    from . import policy_replay_hillclimb_iteration
    from . import policy_replay_promotion_gate
    from . import full_frontier_t3
    from . import target_disambiguation_contract
    from . import target_value_census
    from . import target_value_proofability
    from . import target_value_runtime_decision
    from . import gpo_dc_scope_late_blocker_contract
    from . import gpo_dc_scope_late_blocker_authorization
    from . import gpo_dc_scope_live_surface
    from . import gpo_dc_scope_canary
    from . import gpo_dc_scope_matrix
    from . import evaluation_foundation
    from . import frontier_census
    from . import purpose_range
    from . import replication_purpose_range
    from . import laps_family_transfer_holdout
    from . import laps_family_transfer_live_surface
    from . import laps_family_transfer_canary
    from . import laps_family_transfer_matrix
    from . import trust_context_corroboration
    from . import phase8_goad_regression
    from . import replanning_benchmark
    from . import reliability
    from .scenarios import goad_scenarios
    from .range_state import Milestone
except Exception:  # script / sys.path import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gate_experiment as gx  # type: ignore
    import gate_live  # type: ignore
    import decision_benchmark  # type: ignore
    import null_model_factorial  # type: ignore
    import operator_replay_benchmark  # type: ignore
    import policy_replay_calibration  # type: ignore
    import policy_replay_corpus  # type: ignore
    import policy_replay_selector_experiment  # type: ignore
    import policy_replay_unseen_candidate_evaluator  # type: ignore
    import policy_replay_hillclimb_iteration  # type: ignore
    import policy_replay_promotion_gate  # type: ignore
    import full_frontier_t3  # type: ignore
    import target_disambiguation_contract  # type: ignore
    import target_value_census  # type: ignore
    import target_value_proofability  # type: ignore
    import target_value_runtime_decision  # type: ignore
    import gpo_dc_scope_late_blocker_contract  # type: ignore
    import gpo_dc_scope_late_blocker_authorization  # type: ignore
    import gpo_dc_scope_live_surface  # type: ignore
    import gpo_dc_scope_canary  # type: ignore
    import gpo_dc_scope_matrix  # type: ignore
    import evaluation_foundation  # type: ignore
    import frontier_census  # type: ignore
    import purpose_range  # type: ignore
    import replication_purpose_range  # type: ignore
    import laps_family_transfer_holdout  # type: ignore
    import laps_family_transfer_live_surface  # type: ignore
    import laps_family_transfer_canary  # type: ignore
    import laps_family_transfer_matrix  # type: ignore
    import trust_context_corroboration  # type: ignore
    import phase8_goad_regression  # type: ignore
    import replanning_benchmark  # type: ignore
    import reliability  # type: ignore
    from scenarios import goad_scenarios  # type: ignore
    from range_state import Milestone  # type: ignore


# Demo configs for --dry-run: an aligned gauge (substring tracks ground truth) -> PASS.
_DEMO_PROFILES = {
    "prod": {"substring": 0.90, "furthest": Milestone.OBJECTIVE},
    "good": {"substring": 0.70, "furthest": Milestone.DCSYNC_PARENT},
    "mid": {"substring": 0.55, "furthest": Milestone.DA_CHILD},
    "weak": {"substring": 0.35, "furthest": Milestone.GRAPH_COLLECTED},
    "inert": {"substring": 0.15, "furthest": Milestone.FOOTHOLD},
}


def _print_report(report: gx.GateExperimentReport) -> None:
    print(json.dumps({"kind": "gate_experiment", **report.__dict__}, indent=2, default=str))
    rho = "n/a" if report.spearman_rho is None else f"{report.spearman_rho:.3f}"
    print(
        f"\nVERDICT: {report.verdict}  (rho={rho}, "
        f"high-eval/low-truth={report.high_eval_low_truth_count}, runs={report.total_runs})\n"
        f"{report.note}",
        flush=True,
    )


def _orchestrate_reset_fn():
    """Return a reset_fn(config, token)->sage_cb backed by the sage-eval-gauge orchestrator's full clean reset.
    Lazily imports the operator skill (outside the package) so the package stays decoupled; raises a clear,
    actionable error if it isn't importable.

    TOKEN/CONFIG SEAM (wired 2026-06-20): the reset restarts Sage with SAGE_ENGAGEMENT_ID=<token> AND the
    config's env, so (a) Sage writes its ledger under the token the gauge reads (closing the empty-ground-truth
    hole), and (b) per-config settings actually reach Sage. Which config keys change behavior depends on what
    Sage reads at startup; SAGE_ENGAGEMENT_ID always takes effect."""
    scripts = Path(__file__).resolve().parents[4] / "skills" / "sage-eval-gauge" / "scripts"
    if not (scripts / "orchestrate.py").exists():
        raise SystemExit(f"reset orchestrator not found at {scripts}/orchestrate.py — cannot reset between configs")
    sys.path.insert(0, str(scripts))
    import orchestrate  # type: ignore

    def reset_fn(config, token):
        restart_env = {"SAGE_ENGAGEMENT_ID": token}
        restart_env.update(getattr(config, "env", {}) or {})  # per-config Sage settings reach the relaunch
        sage_cb, _apollo_cb = orchestrate.full_reset_and_ready(restart_env=restart_env)
        return sage_cb

    return reset_fn


def _cmd_gate_experiment(args: argparse.Namespace) -> int:
    scenarios = goad_scenarios(args.engagement_id)
    if args.scenario:
        scenarios = [s for s in scenarios if s.name == args.scenario]
        if not scenarios:
            print(f"no scenario named {args.scenario!r}", file=sys.stderr)
            return 2

    if args.dry_run:
        report = gx.run_gate_experiment(
            list(_DEMO_PROFILES.keys()), scenarios,
            gx.synthetic_runner(_DEMO_PROFILES),
            seeds=args.seeds, results_dir=args.results_dir, write_record=bool(args.results_dir),
        )
        _print_report(report)
        return 0 if report.verdict == "PASS" else 1

    if not args.live:
        print(
            "Specify --dry-run (synthetic, no lab) or --live (real GOAD run).\n"
            "  --live needs: --configs <json {name:{env:{...}}}> with 5-8 known-different-quality configs,\n"
            "  the GOAD lab reachable (resets between configs via the sage-eval-gauge orchestrator), and a\n"
            "  Sage callback. WARNING: a full live gate experiment is many hours (one clean reset per config).",
            file=sys.stderr,
        )
        return 2

    if not args.configs:
        print("--live requires --configs <path to JSON {name:{env:{...}}}> (>=3, ideally 5-8 configs)",
              file=sys.stderr)
        return 2
    configs = gate_live.load_gate_configs(args.configs)
    reset_fn = _orchestrate_reset_fn()
    report = gate_live.run_live_gate_experiment(
        configs, scenarios, reset_fn,
        seeds=args.seeds, sage_cb=args.sage_cb, db=args.db, out_dir=args.out,
        results_dir=args.results_dir, rho_threshold=args.rho_threshold,
    )
    _print_report(report)
    return 0 if report.verdict == "PASS" else 1


def _cmd_noise_floor(args: argparse.Namespace) -> int:
    """Compute the noise floor + MDE from already-recorded gauge runs (the seeds `orchestrate.py --seeds N`
    produced). No lab needed."""
    try:
        report = reliability.noise_floor_from_results(
            args.results, scenario=args.scenario, side=args.side, n=args.n,
        )
    except Exception as e:
        print(f"noise-floor: {e}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(report), indent=2, default=str))
    print(
        f"\nNOISE FLOOR ({report.scenario}, n={report.repeats}, hash={report.verifier_hash}):\n"
        f"  capability mean={report.capability_mean:.3f} stdev={report.capability_stdev:.3f} "
        f"[{report.capability_min:.3f}..{report.capability_max:.3f}]\n"
        f"  MDE={report.min_detectable_effect:.3f}  (a real config-vs-config gain must EXCEED this)\n"
        f"  least-stable milestone={report.least_stable_milestone} @ agreement {report.least_stable_agreement:.2f}",
        flush=True,
    )
    return 0


def _cmd_null_model_factorial(_args: argparse.Namespace) -> int:
    report = null_model_factorial.run_null_model_factorial()
    print(json.dumps(report, indent=2, default=str))
    print(f"\nVERDICT: {report['verdict']}", flush=True)
    return 0 if report["verdict"] == "PASS" else 1


def _cmd_frontier_census(args: argparse.Namespace) -> int:
    starts = frontier_census.candidate_starts()
    if args.start:
        wanted = set(args.start)
        starts = [item for item in starts if item.name in wanted]
        missing = sorted(wanted - {item.name for item in starts})
        if missing:
            print(f"unknown frontier census start(s): {', '.join(missing)}", file=sys.stderr)
            return 2
    report = frontier_census.run_live_frontier_census(
        starts=starts,
        ttl_seconds=args.ttl_seconds,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
    )
    rendered = json.dumps(report, indent=2, default=str)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(recommended_discriminator={report['recommended_discriminator']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1


def _cmd_purpose_range_validate(args: argparse.Namespace) -> int:
    report = purpose_range.validate_purpose_range()
    rendered = json.dumps(report, indent=2, default=str)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(range_source={report['spec']['source_dir']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1


def _cmd_replication_purpose_range_validate(args: argparse.Namespace) -> int:
    report = replication_purpose_range.validate_replication_purpose_range()
    rendered = json.dumps(report, indent=2, default=str)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(range_source={report['spec']['source_dir']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1


def _cmd_laps_family_transfer_holdout_validate(args: argparse.Namespace) -> int:
    report = laps_family_transfer_holdout.validate_laps_family_transfer_holdout()
    rendered = json.dumps(report, indent=2, default=str)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(manifest_hash={report['manifest']['manifest_hash']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1


def _cmd_replanning_benchmark_validate(args: argparse.Namespace) -> int:
    report = replanning_benchmark.validate_replanning_benchmark()
    rendered = json.dumps(report, indent=2, default=str)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(scenario={report['spec']['scenario']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1


def _cmd_phase4_readiness_report(args: argparse.Namespace) -> int:
    report = evaluation_foundation.build_phase4_provisional_report()
    rendered = json.dumps(report, indent=2, default=str)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    decision = ((report.get("readiness") or {}).get("readiness_decision") or "")
    print(f"\nREADINESS: {decision}", flush=True)
    return 0 if decision in {"auto_harness_not_ready", "eligible_for_supervised_artifact_campaign"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai.hillclimb", description="Sage eval gauge (Phase 0)")
    sub = parser.add_subparsers(dest="command", required=True)

    ge = sub.add_parser("gate-experiment", help="run the Gate Experiment (validate the gauge)")
    ge.add_argument("--dry-run", action="store_true", help="use the synthetic runner (no lab)")
    ge.add_argument("--live", action="store_true",
                    help="real GOAD run: resets the lab per config, runs evals/harness.py, scores via C1/C1b/C2")
    ge.add_argument("--configs", default=None,
                    help="JSON {name:{env:{...}}} of 5-8 known-different-quality configs (required for --live)")
    ge.add_argument("--scenario", default=None, help="restrict to one scenario by name (e.g. cross-forest-objective)")
    ge.add_argument("--sage-cb", type=int, default=None, help="initial Sage callback id (reset_fn refreshes it)")
    ge.add_argument("--db", default=None)
    ge.add_argument("--out", default=None, help="harness output dir for eval-*.json")
    ge.add_argument("--rho-threshold", type=float, default=0.7)
    ge.add_argument("--seeds", type=int, default=5)
    ge.add_argument("--engagement-id", default="Operation_GOAD")
    ge.add_argument("--results-dir", default=None)
    ge.set_defaults(func=_cmd_gate_experiment)

    nf = sub.add_parser("noise-floor", help="compute noise floor + MDE from already-recorded gauge runs (no lab)")
    nf.add_argument("--scenario", required=True, help="scenario name (e.g. cross-forest-objective, child-da)")
    nf.add_argument("--side", default="harness", choices=["harness", "bare"])
    nf.add_argument("--n", type=int, default=None, help="use only the last N recorded runs (default: all)")
    nf.add_argument("--results", default=None, help="path to bare_vs_harness.jsonl (default: .hillclimb/results/)")
    nf.set_defaults(func=_cmd_noise_floor)

    nm = sub.add_parser(
        "null-model-factorial",
        help="run the offline symbolic/llm/hybrid null-model policy factorial",
    )
    nm.set_defaults(func=_cmd_null_model_factorial)

    fc = sub.add_parser(
        "frontier-census",
        help="run the read-only GOAD frontier census against the current BloodHound graph",
    )
    fc.add_argument("--ttl-seconds", type=int, default=frontier_census.DEFAULT_TTL_SECONDS)
    fc.add_argument("--max-depth", type=int, default=frontier_census.DEFAULT_MAX_DEPTH)
    fc.add_argument("--max-nodes", type=int, default=frontier_census.DEFAULT_MAX_NODES)
    fc.add_argument("--start", action="append", default=None, help="restrict to one named start; repeatable")
    fc.add_argument("--output", default=None, help="optional JSON output path")
    fc.set_defaults(func=_cmd_frontier_census)

    pr = sub.add_parser(
        "purpose-range-validate",
        help="validate the minimal two-lane purpose-range manifest against the current capability frontier",
    )
    pr.add_argument("--output", default=None, help="optional JSON report path")
    pr.set_defaults(func=_cmd_purpose_range_validate)

    rpr = sub.add_parser(
        "replication-purpose-range-validate",
        help="validate the GPO-vs-direct-replication second purpose-range manifest",
    )
    rpr.add_argument("--output", default=None, help="optional JSON report path")
    rpr.set_defaults(func=_cmd_replication_purpose_range_validate)

    lft = sub.add_parser(
        "laps-family-transfer-holdout-validate",
        help="validate the sealed Phase 6 cross-domain LAPS family-transfer holdout contract",
    )
    lft.add_argument("--output", default=None, help="optional JSON report path")
    lft.set_defaults(func=_cmd_laps_family_transfer_holdout_validate)

    rb = sub.add_parser(
        "replanning-benchmark-validate",
        help="validate the shared-lane late-blocker recovery benchmark contract",
    )
    rb.add_argument("--output", default=None, help="optional JSON report path")
    rb.set_defaults(func=_cmd_replanning_benchmark_validate)

    p4 = sub.add_parser(
        "phase4-readiness-report",
        help="emit the current fail-closed Phase 4 auto-harness-improvement readiness report",
    )
    p4.add_argument("--output", default=None, help="optional JSON report path")
    p4.set_defaults(func=_cmd_phase4_readiness_report)

    decision_benchmark.add_cli(sub)
    operator_replay_benchmark.add_cli(sub)
    policy_replay_calibration.add_cli(sub)
    policy_replay_corpus.add_cli(sub)
    policy_replay_selector_experiment.add_cli(sub)
    policy_replay_unseen_candidate_evaluator.add_cli(sub)
    policy_replay_hillclimb_iteration.add_cli(sub)
    policy_replay_promotion_gate.add_cli(sub)
    full_frontier_t3.add_cli(sub)
    target_disambiguation_contract.add_cli(sub)
    target_value_census.add_cli(sub)
    target_value_proofability.add_cli(sub)
    target_value_runtime_decision.add_cli(sub)
    gpo_dc_scope_late_blocker_contract.add_cli(sub)
    gpo_dc_scope_late_blocker_authorization.add_cli(sub)
    gpo_dc_scope_live_surface.add_cli(sub)
    gpo_dc_scope_canary.add_cli(sub)
    gpo_dc_scope_matrix.add_cli(sub)
    laps_family_transfer_live_surface.add_cli(sub)
    laps_family_transfer_canary.add_cli(sub)
    laps_family_transfer_matrix.add_cli(sub)
    trust_context_corroboration.add_cli(sub)
    phase8_goad_regression.add_cli(sub)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
