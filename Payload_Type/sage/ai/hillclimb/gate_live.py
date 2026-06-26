"""Headless LIVE Gate Experiment orchestration (Phase 0).

The Gate Experiment (`gate_experiment.py`) decides whether the gauge MEANS anything: run several
known-different-quality configs and check that the cheap substring eval RANKS them the way C1 ground
truth does (Spearman ρ ≥ threshold, zero high-eval/low-truth). `gate_experiment.run_gate_experiment`
is the pure scorer; this module is the LIVE driver that `__main__` was missing — it wires:

    for each config:                      # known-different-quality
        reset the lab to a clean range    # fair: every config from the same start (LiveRunner.reset_fn)
        run each scenario through evals/harness.py   # LiveRunner.run_batch -> N ScoreCards via C1/C1b/C2
    -> run_gate_experiment(...) -> Spearman ρ + danger-quadrant + verdict -> gate_experiment.jsonl

Everything lab-touching is injected (`reset_fn`, `invoke`) so the orchestration is unit-tested without
Mythic/GOAD. The CLI (`__main__ gate-experiment --live`) supplies the real reset + harness invoker.

IMPORTANT (honest seam): a config only changes Sage's behavior if `reset_fn` applies the config's settings
when it restarts Sage (the prompt/model-overlay knob). Env overlays on the harness subprocess alone do NOT
reach the already-running Sage callback. The orchestration passes the config to `reset_fn` for exactly this
reason; wiring the per-config Sage restart is the operator's responsibility (and is governor-gated where it
touches model.py).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Sequence

try:  # package import
    from . import gate_experiment as gx
    from .live_runner import LiveRunner, LiveConfig
    from .fitness import GAUGE_VERSION
except Exception:  # script / sys.path import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gate_experiment as gx  # type: ignore
    from live_runner import LiveRunner, LiveConfig  # type: ignore
    from fitness import GAUGE_VERSION  # type: ignore


def load_gate_configs(path: str) -> list[LiveConfig]:
    """Load operator-defined config variants from a JSON object `{name: {env: {...}}}` (or `{name: {...env}}`).

    These are the "5-8 known-different-quality configs" the Gate Experiment needs. Express each as the env
    overlay your `reset_fn` honors when it restarts Sage (e.g. a model/provider tier, a prompt-set selector).
    No lab literals; no source edits. <3 configs makes the gate INSUFFICIENT by design."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"gate configs must be a JSON object {{name: {{env...}}}}; got {type(raw).__name__}")
    configs: list[LiveConfig] = []
    for name, spec in raw.items():
        spec = spec or {}
        env = spec.get("env", spec) if isinstance(spec, dict) else {}
        configs.append(LiveConfig(name=str(name), env=dict(env)))
    return configs


def run_live_gate_experiment(
    configs: Sequence[LiveConfig],
    scenarios: Sequence,
    reset_fn: Callable[[object], int | None],
    *,
    seeds: int = 5,
    sage_cb: int | None = None,
    db: str | None = None,
    out_dir: str | None = None,
    results_dir: str | None = None,
    invoke: Callable[[list, dict, str], dict] | None = None,
    rho_threshold: float = 0.7,
    gauge_version: str = GAUGE_VERSION,
    settle_timeout: int = 300,
    capture_probes_fn: Callable[[object, str], dict | None] | None = None,
    write_record: bool = True,
) -> gx.GateExperimentReport:
    """Drive the live Gate Experiment and return its report. `reset_fn`/`invoke` are injected so this is
    unit-testable; the CLI supplies the real lab reset and harness subprocess invoker. `settle_timeout`
    (default 300s) is the post-solve DA-propagation poll for the ground-truth capture; tests pass 0."""
    runner = LiveRunner(
        sage_cb, db=db, out_dir=out_dir, invoke=invoke, reset_fn=reset_fn, gauge_version=gauge_version,
        settle_timeout=settle_timeout, capture_probes_fn=capture_probes_fn,
    )
    run_fn = runner.as_run_fn(seeds)
    return gx.run_gate_experiment(
        list(configs), list(scenarios), run_fn,
        seeds=seeds, rho_threshold=rho_threshold, results_dir=results_dir, write_record=write_record,
    )
