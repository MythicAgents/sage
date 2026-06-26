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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:  # package import
    from . import live_seams, probes as probes_mod, run_gauge_live
    from .gate_experiment import build_scorecard_from_run
    from .fitness import GAUGE_VERSION, ScoreCard
    from .range_state import Milestone, PROBEABLE_MILESTONES
    from ..langgraph import engagement_ledger
except Exception:  # script / sys.path import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import live_seams, probes as probes_mod, run_gauge_live  # type: ignore
    from gate_experiment import build_scorecard_from_run  # type: ignore
    from fitness import GAUGE_VERSION, ScoreCard  # type: ignore
    from range_state import Milestone, PROBEABLE_MILESTONES  # type: ignore
    import engagement_ledger  # type: ignore

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


def _scored_milestones(scenario) -> tuple[Milestone, ...]:
    spec = scenario.spec() if hasattr(scenario, "spec") else {}
    return tuple(getattr(scenario, "milestone_subset", None) or (
        m for m in Milestone if m == Milestone.FOOTHOLD or m in spec
    ))


def _captureable_probe_milestones(scenario) -> tuple[Milestone, ...]:
    """Probe-able scored milestones worth capturing for later hermetic replay.

    `build_probes` may construct helper probes outside a scenario's scored subset; those are not ground truth
    for this run. Restricting capture to declared/scored probe-able milestones prevents unprobed default False
    values from overwriting legitimate ledger effects during offline re-score.
    """
    direct = set(getattr(scenario, "direct_probes", {}) or {})
    recorded = set(getattr(scenario, "recorded_probe_milestones", frozenset()) or frozenset())
    exempt = set(getattr(scenario, "self_report_exempt", frozenset()) or frozenset())
    declared = direct | recorded | exempt
    return tuple(m for m in _scored_milestones(scenario) if m in PROBEABLE_MILESTONES and m in declared)


def _persist_ground_truth_probe_results(
    engagement_id: str,
    milestones: dict,
    *,
    recorded_milestones: set[Milestone],
    verifier_hash: str | None = None,
) -> dict:
    """Add the captured live-probe vector to the engagement ledger.

    The write is deliberately additive: existing hop/evidence keys are loaded, preserved, and saved through
    engagement_ledger. Only milestones backed by a probe in this capture are written, using Milestone.name so
    the JSON stays stable and inspectable across enum value changes.
    """
    values: dict[str, bool] = {}
    for milestone, value in milestones.items():
        if milestone in recorded_milestones:
            values[milestone.name] = bool(value)
    if not values:
        return {}
    record: dict[str, object] = {
        **values,
        "_captured_at": datetime.now(timezone.utc).isoformat(),
        "_source": "probe",
    }
    if verifier_hash:
        record["_verifier_hash"] = verifier_hash
    data = engagement_ledger.load(engagement_id)
    data["ground_truth_probes"] = record
    engagement_ledger.save(data, engagement_id)
    return record


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
        reset_fn: Callable[[object, str], int | None] | None = None,
        capture_probes_fn: Callable[[object, str], dict | None] | None = None,
        gauge_version: str = GAUGE_VERSION,
        settle_timeout: int = 300,
        settle_interval: int = 20,
    ):
        self.sage_cb = sage_cb
        self.db = db
        self.out_dir = str(out_dir) if out_dir else str(SAGE_ROOT / ".hillclimb" / "harness-out")
        self.python_exe = python_exe or "python3"
        self.case_id_for = case_id_for or (lambda scn: getattr(scn, "name", str(scn)))
        self.gauge_version = gauge_version
        # DA/OBJECTIVE membership (GPO/SYSTEM-on-DC) propagates with delay; the post-solve capture must POLL
        # for it like run_side does (default 300s), or a still-propagating DA win is recorded as a
        # false-negative ground truth and then looks like drift on re-score.
        self._capture_settle_timeout = settle_timeout
        self._capture_settle_interval = settle_interval
        self._invoke = invoke or self._default_invoke
        # Capture hook: called once after the harness finishes while the range still reflects this batch's
        # terminal state, and before scorecard construction. It is injected for offline unit tests; the default
        # implementation uses the live referee readers and persists their vector into the run ledger.
        self._capture_probes_fn = capture_probes_fn or self._default_capture_probes
        # Reset hook: called as reset_fn(config, token) BEFORE each batch so a fair gate measures every config
        # from the SAME clean range. It receives BOTH the config AND the run's engagement token so the operator
        # can restart Sage with the per-config settings (the prompt/model-overlay knob) AND with
        # SAGE_ENGAGEMENT_ID=token — required, because the harness subprocess env never reaches the persistent
        # Sage process (Forge 2026-06-20). If it returns a callback id, that becomes the new sage_cb. None = no
        # reset (dry/test, or caller resets externally).
        self.reset_fn = reset_fn

    def _mint_token(self, config, scenario) -> str:
        return f"gauge-{getattr(config, 'name', config)}-{getattr(scenario, 'name', 'scn')}-{uuid.uuid4().hex[:8]}"

    def build_command(self, config, scenario, seeds: int, token: str | None = None) -> tuple[list[str], dict, str]:
        case_id = self.case_id_for(scenario)
        argv = [self.python_exe, "-m", "evals.harness", "run",
                "--only", case_id, "--seeds", str(seeds), "--out", self.out_dir]
        if self.sage_cb is not None:
            argv += ["--sage-cb", str(self.sage_cb)]
        if self.db:
            argv += ["--db", self.db]
        token = token or self._mint_token(config, scenario)
        env = dict(os.environ)
        env.update(getattr(config, "env", {}) or {})
        # The token keys this run's ledger + trajectory store. CRITICAL (Forge 2026-06-20): these env vars sit
        # on the HARNESS subprocess, which is only a Mythic CLIENT — they do NOT reach the persistent Sage
        # process that actually writes the ledger (Sage freezes its own SAGE_ENGAGEMENT_ID at its startup). For
        # C1 ground truth to read THIS run's ledger, the RESET must restart Sage with SAGE_ENGAGEMENT_ID=token
        # (and the config) — which is why run_batch mints the token first and hands it to reset_fn. Absent that,
        # the ledger read is empty and the gate's empty-ground-truth guard fails the run INVALID.
        env["SAGE_ENGAGEMENT_ID"] = token
        env["SAGE_TRAJECTORY_RUN_ID"] = token
        env["SAGE_TRAJECTORY_ENABLED"] = "1"
        return argv, env, token

    def _default_invoke(self, argv: list[str], env: dict, out_dir: str) -> dict:
        """Run the harness and return the newest eval-*.json. Lab-only path."""
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        subprocess.run(argv, env=env, cwd=str(SAGE_ROOT), check=True)
        reports = sorted(Path(out_dir).glob("eval-*.json"), key=lambda p: p.stat().st_mtime)
        if not reports:
            raise FileNotFoundError(f"no eval-*.json produced in {out_dir}")
        return json.loads(reports[-1].read_text(encoding="utf-8"))

    def _default_capture_probes(self, scenario, engagement_id: str) -> dict:
        """Run the live referee probes once and persist the replayable ground-truth vector.

        This mirrors run_gauge_live.run_side's reader/baseline/probe assembly so the gate path and
        bare-vs-harness path share the same referee definition AND the same DA settling window
        (self._capture_settle_timeout, default 300s) — so a GPO/SYSTEM-on-DC DA win that is still
        propagating at capture time is polled for, not recorded as a false-negative.
        """
        wanted = set(_captureable_probe_milestones(scenario))
        if not wanted:
            return {}
        needed = run_gauge_live._scored_referee_domains(scenario)
        reader = live_seams.make_referee_reader() if needed else (lambda _d: set())
        baseline = {d: reader(d) for d in needed}
        probes = run_gauge_live.build_probes(
            reader, baseline, scenario,
            settle_timeout=self._capture_settle_timeout,
            settle_interval=self._capture_settle_interval,
        )
        recordable = wanted & set(probes)
        if not recordable:
            return {}
        gt = probes_mod.read_ground_truth_from_probes(scenario, probes)
        return _persist_ground_truth_probe_results(
            engagement_id,
            gt.milestones,
            recorded_milestones=recordable,
        )

    def run_batch(self, config, scenario, seeds: int = 5) -> list[ScoreCard]:
        """One harness invocation (N seeds) -> N ScoreCards scored by the gauge.

        Resets the lab to a clean state first when a reset_fn is configured (fair gate: every config measured
        from the same start). The run token is minted FIRST and handed to reset_fn(config, token) so the reset
        can restart Sage with that engagement-id (and the config) — the only way the per-run ledger the gauge
        reads is the ledger Sage actually writes. A fresh callback id returned by reset_fn becomes sage_cb."""
        token = self._mint_token(config, scenario)
        if self.reset_fn is not None:
            fresh_cb = self.reset_fn(config, token)
            if fresh_cb is not None:
                self.sage_cb = fresh_cb
        argv, env, token = self.build_command(config, scenario, seeds, token=token)
        report = self._invoke(argv, env, self.out_dir)
        records = parse_report(report, self.case_id_for(scenario))
        if records and _captureable_probe_milestones(scenario):
            try:
                captured = self._capture_probes_fn(scenario, token)
                if not captured:
                    print(f"[live-runner] no ground-truth probe results captured for {token}", flush=True)
            except Exception as e:
                print(f"[live-runner] ground-truth probe capture failed for {token}: {e}", flush=True)
        return [
            build_scorecard_from_run(rec, scenario, engagement_id=token, trajectory_run_id=token,
                                     gauge_version=self.gauge_version, run_live_probes=True)
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
