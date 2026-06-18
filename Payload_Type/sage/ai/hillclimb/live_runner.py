"""Live runner — drives real GOAD runs through evals/harness.py and scores them with the gauge.

Bridges the gate-experiment orchestration to the lab: for each (config, scenario), it runs
`evals.harness run --only <case> --seeds N`, reads the report JSON, and builds one ScoreCard
per seed via C1/C1b/C2 (`gate_experiment.build_scorecard_from_run`).

Testability: the only thing that needs the lab is `_invoke` (the subprocess + report read). It is
injectable, so command construction, report parsing, and ScoreCard composition are all unit-tested
here without Mythic/GOAD. The real `_invoke` runs the harness and reads the newest `eval-*.json`.

Lab-verification notes (cannot be validated without the range — verify on first run):
  * `scenario.engagement_id` MUST equal the ledger key the agent writes (the Mythic OPERATION
    name; `mythic_tools._ensure_engagement_key` resolves it). We set SAGE_ENGAGEMENT_ID as a hint,
    but the operation name wins — confirm the produced `state_<key>.json` matches.
  * A "config" is an ENV overlay (LiveConfig.env). For it to change agent behavior, the keys must be
    ones the harness/model already honors. New prompt-overlay knobs are Phase-1 work (touch model.py
    -> architecture-governor gated) and are out of scope for this runner.
  * Per-batch trajectory run_id: all seeds in one harness invocation share it, so C1b tradecraft is
    batch-granular, not per-seed (acceptable for Phase 0; diagnostic only).
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:  # package import
    from .gate_experiment import build_scorecard_from_run
    from .fitness import GAUGE_VERSION, ScoreCard
except Exception:  # script / sys.path import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gate_experiment import build_scorecard_from_run  # type: ignore
    from fitness import GAUGE_VERSION, ScoreCard  # type: ignore

SAGE_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class LiveConfig:
    """A candidate config = a name + an env overlay applied to the harness subprocess."""
    name: str
    env: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.name


def parse_report(report: dict, case_id: str) -> list[dict]:
    """Extract the list of per-seed raw run records for a case from a harness v2 report."""
    for case in report.get("cases", []):
        if str(case.get("id")) == str(case_id):
            return list(case.get("seeds", []))
    raise KeyError(f"case {case_id!r} not found in harness report")


class LiveRunner:
    def __init__(
        self,
        sage_cb: int | None,
        *,
        db: str | None = None,
        out_dir: str | os.PathLike | None = None,
        python_exe: str | None = None,
        case_id_for: Callable[[object], str] | None = None,
        invoke: Callable[[list, dict, str], dict] | None = None,
        gauge_version: str = GAUGE_VERSION,
    ):
        self.sage_cb = sage_cb
        self.db = db
        self.out_dir = str(out_dir) if out_dir else str(SAGE_ROOT / ".hillclimb" / "harness-out")
        self.python_exe = python_exe or "python3"
        self.case_id_for = case_id_for or (lambda scn: getattr(scn, "name", str(scn)))
        self.gauge_version = gauge_version
        self._invoke = invoke or self._default_invoke

    def build_command(self, config, scenario, seeds: int) -> tuple[list[str], dict, str]:
        case_id = self.case_id_for(scenario)
        argv = [self.python_exe, "-m", "evals.harness", "run",
                "--only", case_id, "--seeds", str(seeds), "--out", self.out_dir]
        if self.sage_cb is not None:
            argv += ["--sage-cb", str(self.sage_cb)]
        if self.db:
            argv += ["--db", self.db]
        run_id = f"gauge-{getattr(config, 'name', config)}-{getattr(scenario, 'name', 'scn')}-{uuid.uuid4().hex[:8]}"
        env = dict(os.environ)
        env.update(getattr(config, "env", {}) or {})
        env["SAGE_ENGAGEMENT_ID"] = getattr(scenario, "engagement_id", "")
        env["SAGE_TRAJECTORY_RUN_ID"] = run_id
        env["SAGE_TRAJECTORY_ENABLED"] = "1"
        return argv, env, run_id

    def _default_invoke(self, argv: list[str], env: dict, out_dir: str) -> dict:
        """Run the harness and return the newest eval-*.json. Lab-only path."""
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        subprocess.run(argv, env=env, cwd=str(SAGE_ROOT), check=True)
        reports = sorted(Path(out_dir).glob("eval-*.json"), key=lambda p: p.stat().st_mtime)
        if not reports:
            raise FileNotFoundError(f"no eval-*.json produced in {out_dir}")
        return json.loads(reports[-1].read_text(encoding="utf-8"))

    def run_batch(self, config, scenario, seeds: int = 5) -> list[ScoreCard]:
        """One harness invocation (N seeds) -> N ScoreCards scored by the gauge."""
        argv, env, run_id = self.build_command(config, scenario, seeds)
        report = self._invoke(argv, env, self.out_dir)
        records = parse_report(report, self.case_id_for(scenario))
        return [
            build_scorecard_from_run(rec, scenario, trajectory_run_id=run_id, gauge_version=self.gauge_version)
            for rec in records
        ]

    def as_run_fn(self, seeds: int) -> Callable[[object, object, int], ScoreCard]:
        """A memoized run_fn(config, scenario, seed) for run_gate_experiment: the batch runs once
        per (config, scenario); per-seed calls read from the cached batch."""
        cache: dict[tuple, list[ScoreCard]] = {}

        def run_fn(config, scenario, seed):
            key = (str(config), getattr(scenario, "name", str(scenario)))
            if key not in cache:
                cache[key] = self.run_batch(config, scenario, seeds)
            batch = cache[key]
            return batch[seed % len(batch)] if batch else None

        return run_fn
