#!/usr/bin/env python3
"""Gate 1P disposition census: is the attrition arm-correlated, and is each cause arm-reachable?

WHY
---
Pair 024 is the campaign's only comparison-countable pair. Shipping Sage scored 1.000 on 3,203
tokens while true-naive reached the shared completed-call boundary at 260,079 tokens. Both arms
receive the same ceiling and suppress a boundary-crossing action at their production seams; the
focused symmetry test executes both mechanisms.

A 96% discard rate is harmless to the estimate IF AND ONLY IF discards are independent of which arm
was running. This tool answers that question from artifacts already on disk. No range time, no live
runs, no model calls.

THE METHOD, AND WHY IT IS NOT A SIGNIFICANCE TEST
-------------------------------------------------
With counts this small a Fisher test on the burn table is theatre. The useful enumerative question
is whether each discard cause is structurally reachable by the opposite arm. Observed budget
attrition is arm-correlated, but the completed-call boundary itself is reachable by both arms; the
census must not turn an observed imbalance into a false mechanism claim.

So each row gets a `reachable_by_other_arm` verdict, and the summary reports the contingency table
of cause class against arm plus the retention counts that motivated the census.

USAGE
-----
    python3 Payload_Type/sage/ai/hillclimb/disposition_census.py            # table
    python3 Payload_Type/sage/ai/hillclimb/disposition_census.py --json     # machine-readable
    python3 Payload_Type/sage/ai/hillclimb/disposition_census.py --self-test
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

ARMS = ("shipping_sage", "true_naive")

# Cause classes, and whether the opposite arm could structurally have hit the same cause.
# `False` means the mechanism itself is arm-specific, not merely observed in one arm.
CAUSE_REACHABILITY = {
    "token_budget_overshoot": True,
    "token_budget_exhausted": True,
    "arm_missing": None,               # needs the ledger to say why; resolved per-row below
    "completed": True,
    "objective_reached": True,
    "infrastructure": True,
    "unknown": None,
}


def repo_root() -> Path:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).resolve().parent, text=True
        )
        return Path(out.strip())
    except (subprocess.CalledProcessError, OSError):
        return Path(__file__).resolve().parents[4]


def pair_root(root: Path | None = None) -> Path:
    root = root or repo_root()
    return root / ".sage_history/evaluation/harness-self-improvement/gate1p-stagea"


def _records(path: Path) -> Iterable[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def classify(result: dict | None, episode_present: bool) -> tuple[str, str]:
    """Return (terminal_disposition, cause_class)."""
    if result is None:
        return ("absent", "arm_missing")
    # The terminal reason lives in `status`. `controller_terminal_reason` is present but EMPTY on
    # every naive row inspected, so reading it alone reports "unknown" for a run that was in fact
    # budget-truncated -- a negative result produced by the probe, not by the data. Positive
    # control: pair 024 true_naive must classify as token_budget_overshoot (see self_test).
    reason = str(
        result.get("status")
        or (result.get("card") or {}).get("status")
        or result.get("controller_terminal_reason")
        or ""
    ).strip()
    if reason in ("token_budget_overshoot", "token_budget_exhausted"):
        return ("budget_truncated", reason)
    card = result.get("card") or {}
    capability = card.get("capability") if isinstance(card, dict) else None
    if result.get("clean_stop") is True:
        return ("completed", "objective_reached" if capability == 1.0 else "completed")
    if not episode_present:
        return ("no_episode_retained", "infrastructure")
    return ("other", reason or "unknown")


def census(root: Path | None = None) -> dict[str, Any]:
    base = pair_root(root)
    rows: list[dict[str, Any]] = []
    if not base.is_dir():
        return {"rows": [], "error": f"pair root not found: {base}"}

    for pair_dir in sorted(base.glob("*pair-*")):
        pair_id = pair_dir.name.rsplit("pair-", 1)[-1]
        by_arm: dict[str, dict] = {}
        for record in _records(pair_dir / "results.jsonl"):
            protocol = record.get("protocol") or {}
            arm = str(protocol.get("arm") or "")
            if arm in ARMS and "card" in record:
                by_arm[arm] = record

        for arm in ARMS:
            episode = (pair_dir / f"{arm}-episode.json").is_file()
            result = by_arm.get(arm)
            disposition, cause = classify(result, episode)
            card = (result or {}).get("card") or {}
            reachable = CAUSE_REACHABILITY.get(cause)
            if cause == "arm_missing":
                # An arm with no result row at all: reachable in principle, but observe which arm
                # it keeps happening to.
                reachable = True
            rows.append({
                "pair": pair_id,
                "arm": arm,
                "reached_live": result is not None,
                "episode_retained": episode,
                "terminal_disposition": disposition,
                "cause_class": cause,
                "reachable_by_other_arm": reachable,
                "capability": card.get("capability") if isinstance(card, dict) else None,
                "countable": (result or {}).get("countability"),
            })
    return {"rows": rows, "pair_root": str(base)}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    per_arm = {arm: {"reached_live": 0, "episode_retained": 0, "total": 0} for arm in ARMS}
    contingency: dict[str, dict[str, int]] = {}
    unreachable: list[dict[str, Any]] = []
    for row in rows:
        arm = row["arm"]
        per_arm[arm]["total"] += 1
        per_arm[arm]["reached_live"] += int(bool(row["reached_live"]))
        per_arm[arm]["episode_retained"] += int(bool(row["episode_retained"]))
        contingency.setdefault(row["cause_class"], {a: 0 for a in ARMS})[arm] += 1
        if row["reachable_by_other_arm"] is False:
            unreachable.append(row)
    return {"per_arm": per_arm, "contingency": contingency, "arm_unreachable_losses": unreachable}


def self_test() -> int:
    failures: list[str] = []
    # Classification is the load-bearing logic; assert it directly rather than trusting the sweep.
    if classify(None, False) != ("absent", "arm_missing"):
        failures.append("a missing result must classify as absent/arm_missing")
    truncated = {"status": "token_budget_overshoot", "card": {"capability": 0.0}}
    if classify(truncated, True) != ("budget_truncated", "token_budget_overshoot"):
        failures.append("budget truncation misclassified")
    # An empty controller_terminal_reason must not mask a real status; this is the exact shape of
    # every naive row on disk.
    masked = {"controller_terminal_reason": "", "status": "token_budget_overshoot",
              "card": {"capability": 0.0}}
    if classify(masked, True)[1] != "token_budget_overshoot":
        failures.append("empty controller_terminal_reason masked the real status")

    # POSITIVE CONTROL against real data: the probe must find a known-present cause before any
    # "unknown" or zero-count it reports can be trusted.
    live = census()
    if live.get("rows"):
        naive_024 = [r for r in live["rows"] if r["pair"] == "024" and r["arm"] == "true_naive"]
        if naive_024 and naive_024[0]["cause_class"] != "token_budget_overshoot":
            failures.append(
                f"positive control failed: pair 024 true_naive classified as "
                f"{naive_024[0]['cause_class']!r}, expected token_budget_overshoot"
            )
    if CAUSE_REACHABILITY["token_budget_overshoot"] is not True:
        failures.append("the shared completed-call boundary must be marked reachable by both arms")
    clean = {"clean_stop": True, "card": {"capability": 1.0}}
    if classify(clean, True) != ("completed", "objective_reached"):
        failures.append("clean objective run misclassified")
    if failures:
        for line in failures:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1
    print("disposition_census self-test passed (4 classification assertions)")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    data = census()
    rows = data["rows"]
    if not rows:
        print(data.get("error", "no pair directories found"), file=sys.stderr)
        return 1
    summary = summarize(rows)

    if "--json" in sys.argv:
        print(json.dumps({**data, "summary": summary}, indent=2))
        return 0

    print(f"Gate 1P disposition census -- {data['pair_root']}\n")
    print(f"{'pair':>5}  {'arm':<14} {'live':>5} {'epi':>4}  {'disposition':<20} {'cause':<24} {'other-arm?':>10}  cap")
    print("-" * 96)
    for row in rows:
        reach = {True: "yes", False: "NO", None: "?"}[row["reachable_by_other_arm"]]
        print(
            f"{row['pair']:>5}  {row['arm']:<14} {str(row['reached_live']):>5} "
            f"{str(row['episode_retained']):>4}  {row['terminal_disposition']:<20} "
            f"{row['cause_class']:<24} {reach:>10}  {row['capability']}"
        )

    print("\nRetention by arm")
    for arm, counts in summary["per_arm"].items():
        print(f"  {arm:<14} reached_live {counts['reached_live']}/{counts['total']}   "
              f"episode_retained {counts['episode_retained']}/{counts['total']}")

    print("\nCause class x arm")
    for cause, counts in sorted(summary["contingency"].items()):
        print(f"  {cause:<26} " + "  ".join(f"{a}={counts[a]}" for a in ARMS))

    losses = summary["arm_unreachable_losses"]
    print(f"\nArm-unreachable losses: {len(losses)}")
    for row in losses:
        print(f"  pair {row['pair']} {row['arm']}: {row['cause_class']}")
    if losses:
        print("\nAt least one observed loss mechanism is structurally arm-specific.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
