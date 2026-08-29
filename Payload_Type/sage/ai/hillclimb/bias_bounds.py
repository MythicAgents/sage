#!/usr/bin/env python3
"""Bound the censoring bias in Gate 1P, from artifacts already on disk.

WHY
---
`disposition_census.py` established that attrition is arm-correlated with no overlap: shipping_sage
reached live 8/8 and scored capability 1.0 on all eight; true_naive reached live 4/8 and every one
of those four died on `token_budget_overshoot`. `test_gate1p_budget_symmetry.py` proves in source
that only the naive arm runs under a token budget at all.

That establishes the confound exists. It does not say how big it is, or whether the campaign's
headline ("the harness beats the bare model") survives it. This tool answers that.

THE TWO MEASURES, AND WHY THE BINARY ONE THREW AWAY THE SIGNAL
--------------------------------------------------------------
Pair 024 scored `1` against `0`. Underneath that single bit:

    shipping_sage   8 Mythic tasks issued, 8 completed, objective reached,   3,203 tokens
    true_naive      7 Mythic tasks issued, 7 completed, objective NOT reached, 260,079 tokens

The naive arm did not flail. It issued seven Mythic tasks and completed all seven, then hit a cap
its opponent does not run under. Scoring that as `0` records a right-censored observation as a
failure event. Those are different objects, and conflating them biases toward the arm that is never
censored.

Mythic task records are the right dense unit because BOTH arms emit them by construction -- the
product contract forbids either from touching a target any other way. Sage-internal constructs
(capability catalog, planner, engagement ledger) are treatment-only: the control arm would score
structurally undefined on them, not zero, which is the same category error one level up.

WHAT IT REPORTS
---------------
1. Per-pair dense progress and exposure for every arm with a retained episode.
2. `verified tasks per 1k tokens` -- exposure-normalized, symmetric, ground-truthed from Mythic.
3. Tipping-point bounds on the binary claim:
     best case for the null  -- every censored naive arm WOULD have reached the objective
     worst case for the null -- none of them would
   If "harness > bare" survives the best case for the null, the censoring is immaterial and the
   result stands as a low-power observation. If it flips, the headline is unsupported.

Reads only `.sage_history`. No range time, no model calls, no live runs.

USAGE
    python3 Payload_Type/sage/ai/hillclimb/bias_bounds.py [--json] [--self-test]
"""

from __future__ import annotations

import json
import sys
from math import comb
from pathlib import Path
from typing import Any

try:
    from ai.hillclimb.disposition_census import ARMS, census, pair_root
except ModuleNotFoundError:  # run directly as a script, not as a package module
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai.hillclimb.disposition_census import ARMS, census, pair_root


def _episode(pair_dir: Path, arm: str) -> dict | None:
    path = pair_dir / f"{arm}-episode.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return None


def _tokens(pair_dir: Path, arm: str) -> int | None:
    """Total tokens for one arm, from its result row."""
    results = pair_dir / "results.jsonl"
    if not results.is_file():
        return None
    for line in results.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, dict) or (row.get("protocol") or {}).get("arm") != arm:
            continue
        for key in ("total_tokens", "tokens_total"):
            if isinstance(row.get(key), int):
                return row[key]
        card = row.get("card") or {}
        if isinstance(card, dict) and isinstance(card.get("total_tokens"), int):
            return card["total_tokens"]
    return None


def dense_progress(pair_dir: Path, arm: str) -> dict[str, Any] | None:
    episode = _episode(pair_dir, arm)
    if episode is None:
        return None
    observer = episode.get("observer") or {}
    tasks = observer.get("tasks") or []
    events = observer.get("events") or []
    completed = sum(1 for t in tasks if isinstance(t, dict) and t.get("completed") is True)
    objective = any(
        isinstance(e, dict) and e.get("source") == "oracle" and e.get("kind") == "objective_positive"
        for e in events
    )
    tokens = _tokens(pair_dir, arm)
    return {
        "tasks_issued": len(tasks),
        "tasks_completed": completed,
        "objective_reached": objective,
        "total_tokens": tokens,
        "tasks_per_1k_tokens": round(completed / (tokens / 1000), 3) if tokens else None,
    }


def fisher_two_tailed_perfect_separation(n: int) -> float:
    """2 / C(2n, n): two-tailed p for sustained perfect separation at n per arm."""
    return 2 / comb(2 * n, n) if n > 0 else 1.0


def collect(root: Path | None = None) -> dict[str, Any]:
    base = pair_root(root)
    rows = census(root)["rows"]
    by_pair: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_pair.setdefault(row["pair"], {})[row["arm"]] = row

    pairs: list[dict[str, Any]] = []
    for pair_id in sorted(by_pair):
        pair_dir = next(base.glob(f"*pair-{pair_id}"), None)
        if pair_dir is None:
            continue
        entry: dict[str, Any] = {"pair": pair_id, "arms": {}}
        for arm in ARMS:
            progress = dense_progress(pair_dir, arm)
            entry["arms"][arm] = {
                "census": by_pair[pair_id].get(arm, {}),
                "dense": progress,
            }
        entry["both_arms_present"] = all(entry["arms"][a]["dense"] for a in ARMS)
        pairs.append(entry)
    return {"pairs": pairs, "pair_root": str(base)}


def bounds(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [p for p in pairs if p["both_arms_present"]]
    censored = [
        p for p in complete
        if p["arms"]["true_naive"]["census"].get("cause_class", "").startswith("token_budget")
    ]
    n = len(complete)
    wins = sum(
        1 for p in complete
        if p["arms"]["shipping_sage"]["dense"]["objective_reached"]
        and not p["arms"]["true_naive"]["dense"]["objective_reached"]
    )
    # Best case for the null: every censored naive arm would have reached the objective, so those
    # pairs stop being wins for the harness.
    wins_best_for_null = wins - len(censored)
    return {
        "complete_pairs": n,
        "observed_harness_wins": wins,
        "censored_naive_arms": len(censored),
        "worst_case_for_null": {
            "wins": wins,
            "separation": "perfect" if wins == n else "partial",
            "two_tailed_p": round(fisher_two_tailed_perfect_separation(n), 4) if wins == n else None,
        },
        "best_case_for_null": {
            "wins": wins_best_for_null,
            "separation": "none" if wins_best_for_null <= 0 else "partial",
            "two_tailed_p": None,
            "note": (
                "Every censored naive arm is imputed to have reached the objective. All observed "
                "wins came from censored pairs, so the claim does not survive."
                if wins_best_for_null <= 0 else
                "Some wins survive the imputation."
            ),
        },
    }


def self_test() -> int:
    failures: list[str] = []
    if round(fisher_two_tailed_perfect_separation(1), 4) != 1.0:
        failures.append("n=1 perfect separation must give p=1.0")
    if round(fisher_two_tailed_perfect_separation(3), 4) != 0.1:
        failures.append("n=3 perfect separation must give p=0.1")
    if round(fisher_two_tailed_perfect_separation(5), 4) != 0.0079:
        failures.append("n=5 perfect separation must give p=0.0079")

    # POSITIVE CONTROL on real data: pair 024 must show 8 completed tasks for shipping and 7 for
    # naive. A dense measure that silently reads zero would make every bound below meaningless.
    data = collect()
    p024 = next((p for p in data["pairs"] if p["pair"] == "024"), None)
    if p024 is None:
        failures.append("positive control: pair 024 not found")
    else:
        ship = p024["arms"]["shipping_sage"]["dense"]
        naive = p024["arms"]["true_naive"]["dense"]
        if not ship or ship["tasks_completed"] != 8:
            failures.append(f"positive control: shipping 024 completed tasks = {ship and ship['tasks_completed']}, expected 8")
        if not naive or naive["tasks_completed"] != 7:
            failures.append(f"positive control: naive 024 completed tasks = {naive and naive['tasks_completed']}, expected 7")
        if naive and naive["objective_reached"] is not False:
            failures.append("positive control: naive 024 must not have reached the objective")

    if failures:
        for line in failures:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1
    print("bias_bounds self-test passed (3 Fisher values + 3 real-data positive controls)")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    data = collect()
    result = bounds(data["pairs"])
    if "--json" in sys.argv:
        print(json.dumps({**data, "bounds": result}, indent=2))
        return 0

    print("Dense progress per arm (Mythic tasks -- the unit BOTH arms emit by construction)\n")
    print(f"{'pair':>5}  {'arm':<14} {'issued':>7} {'done':>5} {'objective':>10} {'tokens':>9} {'done/1k tok':>12}")
    print("-" * 74)
    for entry in data["pairs"]:
        for arm in ARMS:
            dense = entry["arms"][arm]["dense"]
            if dense is None:
                print(f"{entry['pair']:>5}  {arm:<14} {'--':>7} {'--':>5} {'--':>10} {'--':>9} {'--':>12}")
                continue
            print(
                f"{entry['pair']:>5}  {arm:<14} {dense['tasks_issued']:>7} {dense['tasks_completed']:>5} "
                f"{str(dense['objective_reached']):>10} {str(dense['total_tokens']):>9} "
                f"{str(dense['tasks_per_1k_tokens']):>12}"
            )

    print(f"\nTipping-point bounds on 'harness > bare'")
    print(f"  complete pairs (both arms retained): {result['complete_pairs']}")
    print(f"  observed harness wins:               {result['observed_harness_wins']}")
    print(f"  naive arms lost to the budget cap:   {result['censored_naive_arms']}")
    worst, best = result["worst_case_for_null"], result["best_case_for_null"]
    print(f"\n  worst case for the null: {worst['wins']} wins, separation {worst['separation']}, "
          f"two-tailed p = {worst['two_tailed_p']}")
    print(f"  best case for the null:  {best['wins']} wins, separation {best['separation']}")
    print(f"    {best['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
