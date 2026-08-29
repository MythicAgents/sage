#!/usr/bin/env python3
"""Re-score Gate 1P on tool-call validity: the dense measure BOTH arms emit by construction.

WHY THIS METRIC
---------------
The binary objective bit compressed an 81x cost difference into `1` vs `0` and produced a headline
with two-tailed Fisher p = 1.0 at n=1. `bias_bounds.py` then showed the claim does not survive
imputation: all four observed wins came from pairs whose naive arm was killed by a budget cap only
that arm runs under.

Per-capability verifier outcomes cannot fix this. They are Sage-internal (`ISA.md:126` names the
planner, capability catalog and private ledgers as treatment-side), so the control arm scores
structurally UNDEFINED rather than zero -- measuring the harness with the harness's own ruler.

Tool calls are the right unit. Both arms reach the range only through Mythic and BloodHound; the
product contract forbids anything else. So both emit tool calls by construction, and a run that
died on the budget wall STILL EMITTED EVERY CALL IT MADE BEFORE DYING. That converts most censoring
into measurement: pairs unusable for the binary metric are partially informative here.

The campaign's own lessons already said this. `REFERENCES_AND_LESSONS.md:147` (L5): "Density of the
metric, not seed count, is the dominant term." `:158` (L7): "Fixing reward density shrinks the
parallelism requirement from '50' to 'a handful'." And `:135` (L3): "A capability gauge is
structurally blind to control failure" -- a 461K-token loop and a clean run score identically on
capability, while duplicate-rate and error-rate separate them immediately.

SCORING
-------
One tool call is INVALID if any of:
  * its recorded status is an error;
  * it exactly duplicates an earlier call in the same run (same tool + same arguments);
  * it returned no output where output was required.
Otherwise VALID. Sourced from Mythic/BloodHound-derived records, never from trace prose.

TWO ANALYSES, BOTH REPORTED
---------------------------
  primary     equal-work truncation at K = min(exposure) within each pair, differenced per pair.
              Standard for unequal exposure, and paired, which is L7's prescription.
  sensitivity full exposure.
If the two disagree, that disagreement IS the budget confound, quantified.

NEVER report a rate without its denominator. A clean arm that finishes early has thin exposure and
can reach 1.0 cheaply; under the binary metric censoring flattered Sage, and under a naive rate thin
exposure could flatter it too. Equal-work truncation is what neutralizes both directions.

USAGE
    python3 Payload_Type/sage/ai/hillclimb/toolcall_validity.py [--json] [--self-test]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    from ai.hillclimb.disposition_census import ARMS, census, pair_root
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai.hillclimb.disposition_census import ARMS, census, pair_root

ERROR_STATUSES = {"error", "failed", "failure", "exception"}


def _canon(value: Any) -> str:
    """Stable key for duplicate detection."""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def extract_calls(episode: dict, arm: str) -> list[dict[str, Any]]:
    """Normalize both arms' transcripts into one call shape.

    The two arms persist different structures and this is the only place that difference lives:
      shipping_sage  transcript = list of Mythic chat messages; tool calls are `metadata.tool_use`
                     cards (special_type == "tool_use"), one per emission.
      true_naive     transcript = {"events": [{"call": ..., "obs": ..., "response_id": ...}]}
    """
    transcript = episode.get("transcript")
    calls: list[dict[str, Any]] = []

    if isinstance(transcript, list):  # shipping_sage
        seen_card: set[str] = set()
        for message in transcript:
            if not isinstance(message, dict):
                continue
            metadata = message.get("metadata") or {}
            if not isinstance(metadata, dict) or metadata.get("special_type") != "tool_use":
                continue
            card = metadata.get("tool_use") or {}
            if not isinstance(card, dict):
                continue
            # Started and finished emissions reuse one tool_call_id; count the CALL once.
            call_id = str(card.get("tool_call_id") or card.get("event_id") or len(calls))
            status = str(card.get("status") or "").lower()
            if call_id in seen_card:
                for existing in calls:
                    if existing["call_id"] == call_id and status:
                        existing["status"] = status
                        existing["output_available"] = bool(card.get("output_available"))
                continue
            seen_card.add(call_id)
            calls.append({
                "call_id": call_id,
                "tool": str(card.get("tool_name") or ""),
                "source": str(card.get("tool_source") or ""),
                "args": _canon(card.get("arguments")),
                "status": status,
                "output_available": bool(card.get("output_available")),
            })
        return calls

    if isinstance(transcript, dict):  # true_naive
        for index, event in enumerate(transcript.get("events") or []):
            if not isinstance(event, dict):
                continue
            call = event.get("call")
            obs = event.get("obs")
            tool = ""
            args: Any = call
            if isinstance(call, dict):
                tool = str(call.get("name") or call.get("tool") or call.get("tool_name") or "")
                args = call.get("arguments", call.get("args", call))
            status = ""
            if isinstance(obs, dict):
                status = str(obs.get("status") or obs.get("error") or "").lower()
            elif isinstance(obs, str) and obs.lower().startswith("error"):
                status = "error"
            calls.append({
                "call_id": str(event.get("response_id") or index),
                "tool": tool,
                "source": "bare",
                "args": _canon(args),
                "status": status,
                "output_available": obs not in (None, "", {}, []),
            })
    return calls


def _decoded_args(call: dict[str, Any]) -> Any:
    """`arguments` can be double-encoded JSON; unwrap until it stops being a string."""
    value: Any = call.get("args")
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            break
    return value


def is_status_poll(call: dict[str, Any]) -> bool:
    """A repeated async-status check is legitimate work, not waste.

    Ratified by Russel 2026-08-29. Measured across the eight Gate 1P shipping arms: of 197 exact
    repeats, 37 were `file_upload` calls with `info_type: "status"` polling a BloodHound ingest job
    to completion, and 160 were byte-identical `cypher_query` / `domain_info` re-runs against an
    unchanged graph. Only the second class is a defect. Counting polls as invalid understated the
    shipping arm's validity rate by roughly 12 points.
    """
    args = _decoded_args(call)
    return isinstance(args, dict) and args.get("info_type") == "status"


def score(calls: list[dict[str, Any]]) -> dict[str, Any]:
    seen: set[tuple[str, str]] = set()
    invalid = {"error": 0, "duplicate": 0, "empty_output": 0}
    polls = 0
    for call in calls:
        key = (call["tool"], call["args"])
        repeated = key in seen
        if any(token in call["status"] for token in ERROR_STATUSES) and call["status"]:
            invalid["error"] += 1
        elif repeated and is_status_poll(call):
            polls += 1  # exempt: async status polling
        elif repeated:
            invalid["duplicate"] += 1
        elif not call["output_available"]:
            invalid["empty_output"] += 1
        seen.add(key)
    bad = sum(invalid.values())
    total = len(calls)
    return {
        "calls": total,
        "invalid": bad,
        "valid": total - bad,
        "validity_rate": round((total - bad) / total, 4) if total else None,
        "breakdown": invalid,
        "status_polls_exempt": polls,
    }


def collect(root: Path | None = None) -> dict[str, Any]:
    base = pair_root(root)
    rows = census(root)["rows"]
    by_pair: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_pair.setdefault(row["pair"], {})[row["arm"]] = row

    pairs = []
    for pair_id in sorted(by_pair):
        pair_dir = next(base.glob(f"*pair-{pair_id}"), None)
        if pair_dir is None:
            continue
        entry: dict[str, Any] = {"pair": pair_id, "arms": {}}
        for arm in ARMS:
            path = pair_dir / f"{arm}-episode.json"
            if not path.is_file():
                entry["arms"][arm] = None
                continue
            try:
                episode = json.loads(path.read_text(errors="replace"))
            except (json.JSONDecodeError, ValueError):
                entry["arms"][arm] = None
                continue
            calls = extract_calls(episode, arm)
            entry["arms"][arm] = {**score(calls), "_calls": calls}
        pairs.append(entry)
    return {"pairs": pairs, "pair_root": str(base)}


def paired_analysis(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [p for p in pairs if all(p["arms"][a] for a in ARMS)]
    full, truncated = [], []
    for entry in complete:
        ship, naive = entry["arms"]["shipping_sage"], entry["arms"]["true_naive"]
        full.append({
            "pair": entry["pair"],
            "delta": round(ship["validity_rate"] - naive["validity_rate"], 4),
            "shipping": ship["validity_rate"], "naive": naive["validity_rate"],
            "n_shipping": ship["calls"], "n_naive": naive["calls"],
        })
        k = min(ship["calls"], naive["calls"])
        s_k, n_k = score(ship["_calls"][:k]), score(naive["_calls"][:k])
        truncated.append({
            "pair": entry["pair"], "k": k,
            "delta": round(s_k["validity_rate"] - n_k["validity_rate"], 4)
            if s_k["validity_rate"] is not None and n_k["validity_rate"] is not None else None,
            "shipping": s_k["validity_rate"], "naive": n_k["validity_rate"],
        })
    return {"full_exposure": full, "equal_work": truncated}


def self_test() -> int:
    failures: list[str] = []
    calls = [
        {"call_id": "1", "tool": "t", "source": "s", "args": "{}", "status": "", "output_available": True},
        {"call_id": "2", "tool": "t", "source": "s", "args": "{}", "status": "", "output_available": True},
        {"call_id": "3", "tool": "u", "source": "s", "args": "{}", "status": "error", "output_available": True},
        {"call_id": "4", "tool": "v", "source": "s", "args": "{}", "status": "", "output_available": False},
    ]
    poll_args = '{"info_type": "status", "job_id": 1}'
    calls += [
        {"call_id": "5", "tool": "file_upload", "source": "mcp", "args": poll_args,
         "status": "", "output_available": True},
        {"call_id": "6", "tool": "file_upload", "source": "mcp", "args": poll_args,
         "status": "", "output_available": True},
    ]
    result = score(calls)
    if result["status_polls_exempt"] != 1:
        failures.append(f"repeated status poll must be exempt, got {result['status_polls_exempt']}")
    if result["breakdown"] != {"error": 1, "duplicate": 1, "empty_output": 1}:
        failures.append(f"scorer breakdown wrong: {result['breakdown']}")
    if result["valid"] != 3:
        failures.append(f"expected 3 valid calls (1 original + 2 polls), got {result['valid']}")

    # POSITIVE CONTROL on real data. A zero-call extraction would make every rate below vacuous,
    # and the two arms persist DIFFERENT transcript structures, so each needs its own control.
    data = collect()
    p024 = next((p for p in data["pairs"] if p["pair"] == "024"), None)
    if p024 is None:
        failures.append("positive control: pair 024 missing")
    else:
        ship, naive = p024["arms"]["shipping_sage"], p024["arms"]["true_naive"]
        if not ship or ship["calls"] < 10:
            failures.append(f"positive control: shipping 024 extracted {ship and ship['calls']} calls, expected >=10")
        if not naive or naive["calls"] < 5:
            failures.append(f"positive control: naive 024 extracted {naive and naive['calls']} calls, expected >=5")

    if failures:
        for line in failures:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1
    print("toolcall_validity self-test passed (2 scorer + 2 real-data extraction controls)")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    data = collect()
    analysis = paired_analysis(data["pairs"])

    if "--json" in sys.argv:
        for entry in data["pairs"]:
            for arm in ARMS:
                if entry["arms"][arm]:
                    entry["arms"][arm].pop("_calls", None)
        print(json.dumps({**data, "analysis": analysis}, indent=2))
        return 0

    print("Tool-call validity -- the unit both arms emit by construction\n")
    print(f"{'pair':>5}  {'arm':<14} {'calls':>6} {'valid':>6} {'rate':>7}   err  dup  empty")
    print("-" * 62)
    for entry in data["pairs"]:
        for arm in ARMS:
            s = entry["arms"][arm]
            if not s:
                print(f"{entry['pair']:>5}  {arm:<14} {'--':>6} {'--':>6} {'--':>7}")
                continue
            b = s["breakdown"]
            print(f"{entry['pair']:>5}  {arm:<14} {s['calls']:>6} {s['valid']:>6} "
                  f"{s['validity_rate']:>7}   {b['error']:>3}  {b['duplicate']:>3}  {b['empty_output']:>5}")

    print("\nPaired analysis -- primary is equal-work truncation, sensitivity is full exposure")
    print(f"\n{'pair':>5}  {'K':>4} {'ship@K':>8} {'naive@K':>8} {'delta@K':>8}   |  "
          f"{'ship':>6} {'naive':>6} {'delta':>7}  n_ship n_naive")
    print("-" * 92)
    for eq, fu in zip(analysis["equal_work"], analysis["full_exposure"]):
        print(f"{eq['pair']:>5}  {eq['k']:>4} {str(eq['shipping']):>8} {str(eq['naive']):>8} "
              f"{str(eq['delta']):>8}   |  {str(fu['shipping']):>6} {str(fu['naive']):>6} "
              f"{str(fu['delta']):>7}  {fu['n_shipping']:>6} {fu['n_naive']:>7}")
    print("\nIf equal-work and full-exposure disagree, that disagreement is the budget confound.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
