#!/usr/bin/env python3
"""Unattended orchestrator for bare-vs-harness gauge runs on a FRESH range.

Chains, with guards + per-step timeouts + abort-on-failure (so the gauge never runs on a half-reset
range and produces lies): full reset -> bootstrap callbacks -> readiness ->
discover callbacks -> run ONE side -> record.  A scenario sweep does: reset+harness, reset+bare, compare.

⚠️  RUNS LIVE OFFENSIVE TOOLING + resets the lab with `--go`. Intended for Codex/operator UNATTENDED
    iteration. Safe without `--go`: prints the exact command plan (your attended-cycle runbook).
⚠️  NOT validated against the live lab. The Apollo-deploy step is lab-specific: this orchestrator does
    Imports the baked Apollo callback config when configured; otherwise it creates a fresh Apollo payload
    and still requires deployment before readiness can pass. Confirm/tune the first baked-callback run.

Run from the sage repo root:  .venv/bin/python skills/sage-eval-gauge/scripts/orchestrate.py --help
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]            # /home/john/dev/sage
PAYLOAD = ROOT / "Payload_Type" / "sage"
PY = str(ROOT / ".venv" / "bin" / "python")
BH = "/home/john/dev/bloodhound_mcp"

# (name, argv, cwd, timeout_s). Reset + bootstrap, in order, per sage-goad-reset + sage-callback-bootstrap.
RESET_STEPS = [
    ("stop sage",        ["/bin/bash", "skills/sage-goad-reset/scripts/sage_stop.sh"], ROOT, 120),
    ("archive dbs",      [PY, "skills/sage-goad-reset/scripts/archive_runtime_dbs.py"], ROOT, 180),
    ("reset mythic",     ["/bin/bash", "skills/sage-goad-reset/scripts/mythic_reset.sh", "--yes"], ROOT, 900),
    ("ludus rollback",   [PY, "skills/sage-goad-reset/scripts/ludus.py", "rollback", "--yes"], ROOT, 2400),
    ("ludus poweron",    [PY, "skills/sage-goad-reset/scripts/ludus.py", "poweron", "all"], ROOT, 900),
    ("restart sage",     ["/bin/bash", "skills/sage-goad-reset/scripts/sage_restart.sh",
                          "SAGE_ENGAGEMENT_GATE=1", f"SAGE_BLOODHOUND_MCP_DIR={BH}"], ROOT, 240),
    ("wipe bloodhound",  ["uv", "--directory", BH, "run", "python",
                          str(ROOT / "skills/sage-goad-reset/scripts/bh_reset.py"), "wipe", "--yes"], ROOT, 180),
    ("bootstrap callbacks", [PY, "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py", "bootstrap-reset"], ROOT, 900),
]
LUDUS_STATUS = [PY, "skills/sage-goad-reset/scripts/ludus.py", "status"]
READINESS = [PY, "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py", "readiness", "--runtime-dbs-archived"]
CALLBACKS = [PY, "skills/sage-live-runner/scripts/sage_task.py", "callbacks"]


def _run(name, argv, cwd, timeout):
    print(f"\n=== {name} ===\n$ (cd {cwd}) {' '.join(argv)}", flush=True)
    proc = subprocess.run(argv, cwd=str(cwd), timeout=timeout, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"ABORT: step '{name}' failed (exit {proc.returncode}) — not running the gauge on a bad range.")


def _poll(name, argv, cwd, predicate, *, timeout, interval=20):
    print(f"\n=== poll: {name} (<= {timeout}s) ===", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True).stdout
        if predicate(out):
            return out
        time.sleep(interval)
    raise SystemExit(f"ABORT: '{name}' not ready within {timeout}s — fix the lab before running the gauge.")


def _readiness_ok(out: str) -> bool:
    """True iff bootstrap_payloads.py `readiness` reports overall ready. That command prints a JSON object
    (`json.dumps`), so parse it and check the TOP-LEVEL `ready` key — a substring match would also catch the
    nested runtime_databases/callbacks `ready` flags and pass prematurely. Fail-safe to False (keep polling)
    on any parse error or partial output."""
    try:
        return json.loads(out).get("ready") is True
    except Exception:
        return False


def discover_callbacks() -> tuple[int, int]:
    out = subprocess.run(CALLBACKS, cwd=str(ROOT), capture_output=True, text=True).stdout
    sage = [int(m) for m in re.findall(r"id=(\d+)\s+payloadtype=sage", out)]
    apollo = [int(m) for m in re.findall(r"id=(\d+)\s+payloadtype=apollo", out)]
    if not sage or not apollo:
        raise SystemExit(f"ABORT: missing callbacks (sage={sage} apollo={apollo}).\n{out}")
    return sage[-1], apollo[-1]   # newest of each


def full_reset_and_ready(restart_env: dict | None = None) -> tuple[int, int]:
    """Full clean reset -> readiness -> callback discovery; returns (sage_cb, apollo_cb).

    `restart_env` (e.g. {"SAGE_ENGAGEMENT_ID": <run token>, "SAGE_MODEL": <tier>}) is appended as positional
    KEY=VAL overrides to the `restart sage` step, so the relaunched Sage runs under that engagement id and
    per-config settings. This is the token/config seam the Gate Experiment needs: Sage freezes
    SAGE_ENGAGEMENT_ID at startup and writes its ledger under it, so the gauge can only read THIS run's ground
    truth if Sage was restarted with the run's token. sage_restart.sh applies KEY=VAL after the env snapshot
    (last value wins), so these override the snapshot. Which config keys actually change behavior depends on
    what Sage reads at startup (SAGE_ENGAGEMENT_ID always; model/provider if read from env; a prompt-set
    selector would need its own knob)."""
    for name, argv, cwd, timeout in RESET_STEPS:
        step_argv = argv
        if name == "restart sage" and restart_env:
            step_argv = list(argv) + [f"{k}={v}" for k, v in restart_env.items()]
        _run(name, step_argv, cwd, timeout)
    _poll("ludus guests up", LUDUS_STATUS, ROOT, lambda o: "10.4.10.10" in o, timeout=1800)
    # Apollo must reconnect from the baked snapshot or be deployed from the fallback build.
    _poll("sage+apollo ready", READINESS, ROOT, _readiness_ok, timeout=1200)
    return discover_callbacks()


# Seconds the per-run gauge SUBPROCESS may take ON TOP OF the solve itself — covers reset-independent
# post-solve work (DA settle polling up to ~300s, milestone probes, ScoreCard recording). The step cap is
# solve_timeout + this, floored at the historical 3600 so the default behaviour is unchanged.
_GAUGE_STEP_OVERHEAD_S = 900


def run_side(scenario: str, side: str, *, go: bool, solve_timeout: int, controller: bool = False) -> None:
    # Fail in seconds, not after a ~60-min range run: assert the scenario objective is completion-recognizable
    # BEFORE spending a reset + live solve. Guards the harness->Sage objective seam that shipped opaque once
    # (the gauge's read-only/seam-injected design makes that seam invisible to offline unit tests). Aborts
    # (via _run's non-zero SystemExit) on a dropped/opaque/unparseable objective.
    _run(f"preflight {scenario}", [PY, "ai/hillclimb/run_gauge_live.py", "preflight", "--scenario", scenario], PAYLOAD, 120)
    # --controller: restart Sage with SAGE_AUTONOMOUS_CONTROLLER=1 so the harness `query` (mode=auto,
    # autonomous_solve=True) runs the deterministic autonomous controller instead of the Supervisor/worker
    # astream path. Opt-in + off by default so this is a clean A/B against the existing autonomous path.
    # SAGE_ENGAGEMENT_NETBIOS_MAP is the per-engagement NetBIOS->FQDN map (range-agnostic mechanism; the GOAD
    # values live HERE in the eval harness, not in Sage code). The controller's deterministic frontier needs
    # FQDN-form principal UPNs to match BloodHound's graph (a short forest like 'north' yields
    # samwell.tarly@north, which the graph cypher can't match vs samwell.tarly@north.sevenkingdoms.local) —
    # without it the controller halts at GRAPH_COLLECTED with an empty frontier.
    restart_env = None
    if controller:
        restart_env = {
            "SAGE_AUTONOMOUS_CONTROLLER": "1",
            "SAGE_ENGAGEMENT_NETBIOS_MAP": (
                '{"NORTH":"north.sevenkingdoms.local",'
                '"SEVENKINGDOMS":"sevenkingdoms.local",'
                '"ESSOS":"essos.local"}'
            ),
        }
    sage_cb, apollo_cb = full_reset_and_ready(restart_env=restart_env)
    argv = [PY, "ai/hillclimb/run_gauge_live.py", "run", "--side", side, "--scenario", scenario,
            "--sage-cb", str(sage_cb), "--apollo-cb", str(apollo_cb),
            "--solve-timeout", str(solve_timeout)]
    if go:
        argv.append("--go")
    # The subprocess cap MUST exceed the solve-timeout or it kills a still-progressing solve early (the bug
    # this fixes: a 30-min solve under a 60-min step cap was fine, but raising the solve past 60 min silently
    # hit the old fixed 3600). Scale the cap with the solve budget.
    step_timeout = max(3600, solve_timeout + _GAUGE_STEP_OVERHEAD_S)
    _run(f"gauge {side}/{scenario}", argv, PAYLOAD, step_timeout)


def compare(scenario: str) -> None:
    _run(f"compare {scenario}", [PY, "ai/hillclimb/run_gauge_live.py", "compare", "--scenario", scenario], PAYLOAD, 120)


def _dry_run_plan(scenario, side, seeds, solve_timeout):
    step_timeout = max(3600, solve_timeout + _GAUGE_STEP_OVERHEAD_S)
    print("DRY RUN (no --go). Plan — each gauge run gets its OWN fresh range:\n")
    print(f"  solve-timeout={solve_timeout}s per solve; per-run subprocess cap={step_timeout}s\n")
    for s in range(seeds):
        for sd in ([side] if side else ["harness", "bare"]):
            print(f"--- iteration seed={s} side={sd} ---")
            for name, argv, cwd, _ in RESET_STEPS:
                print(f"  (cd {cwd}) {' '.join(argv)}")
            print(f"  (cd {ROOT}) {' '.join(LUDUS_STATUS)}     # poll until guests up")
            print(
                f"  (cd {ROOT}) {' '.join(READINESS)}        "
                "# poll until baked Apollo reconnects or fallback Apollo is deployed"
            )
            print(f"  (cd {ROOT}) {' '.join(CALLBACKS)}        # parse sage_cb + apollo_cb")
            print(f"  (cd {PAYLOAD}) {PY} ai/hillclimb/run_gauge_live.py run --go --side {sd} "
                  f"--scenario {scenario} --sage-cb <sage> --apollo-cb <apollo> --solve-timeout {solve_timeout}")
    print(f"  (cd {PAYLOAD}) {PY} ai/hillclimb/run_gauge_live.py compare --scenario {scenario}")
    print("\nRe-run with --go to execute. ABORTS on any reset/bootstrap/callback failure.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bare-vs-harness gauge orchestrator (resets the lab per run).")
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--side", choices=["harness", "bare"], default=None,
                    help="run only this side; omit to sweep harness+bare then compare")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--solve-timeout", type=int, default=1800,
                    help="seconds per gauge solve (default 1800=30min). The per-run subprocess cap is raised "
                         "to cover this + post-solve scoring overhead, so long solves are not killed early.")
    ap.add_argument("--go", action="store_true", help="actually reset the lab + run offensive tooling")
    ap.add_argument("--controller", action="store_true",
                    help="restart Sage with SAGE_AUTONOMOUS_CONTROLLER=1 so the harness solve runs the "
                         "deterministic autonomous controller (A/B vs the default Supervisor/worker path)")
    args = ap.parse_args(argv)

    if not args.go:
        _dry_run_plan(args.scenario, args.side, args.seeds, args.solve_timeout)
        if args.controller:
            print("\n  NOTE: --controller -> Sage restarted with SAGE_AUTONOMOUS_CONTROLLER=1 "
                  "(deterministic autonomous controller path).")
        return 0

    for _ in range(args.seeds):
        for side in ([args.side] if args.side else ["harness", "bare"]):
            run_side(args.scenario, side, go=True, solve_timeout=args.solve_timeout, controller=args.controller)
    if not args.side:
        compare(args.scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
