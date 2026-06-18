#!/usr/bin/env python3
"""Unattended orchestrator for bare-vs-harness gauge runs on a FRESH range.

Chains, with guards + per-step timeouts + abort-on-failure (so the gauge never runs on a half-reset
range and produces lies):  full reset -> bootstrap payloads -> (deploy Apollo) -> readiness ->
discover callbacks -> run ONE side -> record.  A scenario sweep does: reset+harness, reset+bare, compare.

⚠️  RUNS LIVE OFFENSIVE TOOLING + resets the lab with `--go`. Intended for Codex/operator UNATTENDED
    iteration. Safe without `--go`: prints the exact command plan (your attended-cycle runbook).
⚠️  NOT validated against the live lab. The Apollo-deploy step is lab-specific: this orchestrator does
    NOT itself deliver the payload — it CREATES payloads then POLLS readiness, and ABORTS if no live
    Apollo callback appears (i.e. you/Codex must deploy Apollo to CASTELBLACK, or the baseline must
    auto-callback). Confirm/tune the steps on the first attended run.

Run from the sage repo root:  .venv/bin/python skills/sage-eval-gauge/scripts/orchestrate.py --help
"""
from __future__ import annotations

import argparse
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
    ("ludus rollback",   [PY, "skills/sage-goad-reset/scripts/ludus.py", "rollback", "clean-baseline", "--yes"], ROOT, 2400),
    ("ludus poweron",    [PY, "skills/sage-goad-reset/scripts/ludus.py", "poweron", "all"], ROOT, 900),
    ("restart sage",     ["/bin/bash", "skills/sage-goad-reset/scripts/sage_restart.sh",
                          "SAGE_ENGAGEMENT_GATE=1", f"SAGE_BLOODHOUND_MCP_DIR={BH}"], ROOT, 240),
    ("wipe bloodhound",  ["uv", "--directory", BH, "run", "python",
                          str(ROOT / "skills/sage-goad-reset/scripts/bh_reset.py"), "wipe", "--yes"], ROOT, 180),
    ("create payloads",  [PY, "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py", "create-all"], ROOT, 900),
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


def discover_callbacks() -> tuple[int, int]:
    out = subprocess.run(CALLBACKS, cwd=str(ROOT), capture_output=True, text=True).stdout
    sage = [int(m) for m in re.findall(r"id=(\d+)\s+payloadtype=sage", out)]
    apollo = [int(m) for m in re.findall(r"id=(\d+)\s+payloadtype=apollo", out)]
    if not sage or not apollo:
        raise SystemExit(f"ABORT: missing callbacks (sage={sage} apollo={apollo}).\n{out}")
    return sage[-1], apollo[-1]   # newest of each


def full_reset_and_ready() -> tuple[int, int]:
    for name, argv, cwd, timeout in RESET_STEPS:
        _run(name, argv, cwd, timeout)
    _poll("ludus guests up", LUDUS_STATUS, ROOT, lambda o: "10.4.10.10" in o, timeout=1800)
    # Apollo must be live (you/Codex deploy it, or the baseline auto-callbacks) for readiness to pass.
    _poll("sage+apollo ready", READINESS, ROOT, lambda o: "ready: true" in o.lower(), timeout=1200)
    return discover_callbacks()


def run_side(scenario: str, side: str, *, go: bool) -> None:
    sage_cb, apollo_cb = full_reset_and_ready()
    argv = [PY, "ai/hillclimb/run_gauge_live.py", "run", "--side", side, "--scenario", scenario,
            "--sage-cb", str(sage_cb), "--apollo-cb", str(apollo_cb)]
    if go:
        argv.append("--go")
    _run(f"gauge {side}/{scenario}", argv, PAYLOAD, 3600)


def compare(scenario: str) -> None:
    _run(f"compare {scenario}", [PY, "ai/hillclimb/run_gauge_live.py", "compare", "--scenario", scenario], PAYLOAD, 120)


def _dry_run_plan(scenario, side, seeds):
    print("DRY RUN (no --go). Plan — each gauge run gets its OWN fresh range:\n")
    for s in range(seeds):
        for sd in ([side] if side else ["harness", "bare"]):
            print(f"--- iteration seed={s} side={sd} ---")
            for name, argv, cwd, _ in RESET_STEPS:
                print(f"  (cd {cwd}) {' '.join(argv)}")
            print(f"  (cd {ROOT}) {' '.join(LUDUS_STATUS)}     # poll until guests up")
            print(f"  (cd {ROOT}) {' '.join(READINESS)}        # poll until ready: true  (DEPLOY Apollo first!)")
            print(f"  (cd {ROOT}) {' '.join(CALLBACKS)}        # parse sage_cb + apollo_cb")
            print(f"  (cd {PAYLOAD}) {PY} ai/hillclimb/run_gauge_live.py run --go --side {sd} "
                  f"--scenario {scenario} --sage-cb <sage> --apollo-cb <apollo>")
    print(f"  (cd {PAYLOAD}) {PY} ai/hillclimb/run_gauge_live.py compare --scenario {scenario}")
    print("\nRe-run with --go to execute. ABORTS on any reset/bootstrap/callback failure.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bare-vs-harness gauge orchestrator (resets the lab per run).")
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--side", choices=["harness", "bare"], default=None,
                    help="run only this side; omit to sweep harness+bare then compare")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--go", action="store_true", help="actually reset the lab + run offensive tooling")
    args = ap.parse_args(argv)

    if not args.go:
        _dry_run_plan(args.scenario, args.side, args.seeds)
        return 0

    for _ in range(args.seeds):
        for side in ([args.side] if args.side else ["harness", "bare"]):
            run_side(args.scenario, side, go=True)
    if not args.side:
        compare(args.scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
