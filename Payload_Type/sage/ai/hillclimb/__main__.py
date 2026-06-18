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
from pathlib import Path

try:  # package import
    from . import gate_experiment as gx
    from .scenarios import goad_scenarios
    from .range_state import Milestone
except Exception:  # script / sys.path import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gate_experiment as gx  # type: ignore
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


def _cmd_gate_experiment(args: argparse.Namespace) -> int:
    scenarios = goad_scenarios(args.engagement_id)
    if args.dry_run:
        report = gx.run_gate_experiment(
            list(_DEMO_PROFILES.keys()), scenarios,
            gx.synthetic_runner(_DEMO_PROFILES),
            seeds=args.seeds, results_dir=args.results_dir, write_record=bool(args.results_dir),
        )
        _print_report(report)
        return 0 if report.verdict == "PASS" else 1

    print(
        "LIVE gate-experiment is not wired for headless execution.\n"
        "It requires the GOAD lab: run each config through evals/harness.py, then build a ScoreCard\n"
        "with gate_experiment.build_scorecard_from_run(harness_record, scenario) and pass that runner\n"
        "to gate_experiment.run_gate_experiment(...). Use --dry-run to exercise the orchestration.",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai.hillclimb", description="Sage eval gauge (Phase 0)")
    sub = parser.add_subparsers(dest="command", required=True)

    ge = sub.add_parser("gate-experiment", help="run the Gate Experiment (validate the gauge)")
    ge.add_argument("--dry-run", action="store_true", help="use the synthetic runner (no lab)")
    ge.add_argument("--seeds", type=int, default=5)
    ge.add_argument("--engagement-id", default="Operation_GOAD")
    ge.add_argument("--results-dir", default=None)
    ge.set_defaults(func=_cmd_gate_experiment)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
