"""CLI for the Sage eval gauge (Phase 0): gate-experiment | report.

  python -m ai.hillclimb gate-experiment --dry-run        # runnable now; synthetic runner
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
    from . import reliability
    from .scenarios import goad_scenarios
    from .range_state import Milestone
except Exception:  # script / sys.path import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gate_experiment as gx  # type: ignore
    import gate_live  # type: ignore
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
