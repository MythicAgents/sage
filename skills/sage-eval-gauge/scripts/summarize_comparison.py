#!/usr/bin/env python3
"""Read retained JSONL results and print a descriptive summary; never execute a run."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, fields
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type" / "sage"))

from ai.hillclimb.bare_runner import compare_bare_vs_harness
from ai.hillclimb.fitness import ScoreCard
from ai.hillclimb.live_seams import summarize_task_quiescence
from ai.hillclimb.range_state import Milestone


def summarize(records: list, scenario: str) -> dict:
    """Retain record counts and malformed-result locations, including unsuccessful attempts."""
    cards = {"bare": [], "harness": []}
    kinds: Counter = Counter()
    invalid = []
    other_scenarios = 0
    card_fields = {field.name for field in fields(ScoreCard)}
    for line, record in enumerate(records, 1):
        if not isinstance(record, dict):
            invalid.append(line)
            continue
        kind = record.get("record_type")
        kinds[kind if isinstance(kind, str) and kind in {"result", "attempt", "cleanup", "prestart_cleanup"} else "other"] += 1
        if record.get("scenario") != scenario:
            other_scenarios += 1
            continue
        if kind != "result":
            continue
        side = record.get("side")
        payload = record.get("card")
        if not isinstance(side, str) or side not in cards or not isinstance(payload, dict):
            invalid.append(line)
            continue
        try:
            card = ScoreCard(**{key: value for key, value in payload.items() if key in card_fields})
        except TypeError:
            invalid.append(line)
            continue
        if card.scenario != scenario:
            invalid.append(line)
            continue
        if ("objective_proof_disposition" not in payload and isinstance(card.milestones, dict)
                and all(key in Milestone.__members__ for key in card.milestones)
                and isinstance(card.proof_dispositions, dict)):
            terminal = max((Milestone[key] for key in card.milestones), default=None)
            if terminal is not None:
                card.objective_proof_disposition = card.proof_dispositions.get(terminal.name, "unmeasured")
        card.cost_countable = payload.get("cost_countable", record.get("cost_countable", {}))
        cards[side].append(card)
    comparison = asdict(compare_bare_vs_harness(
        scenario, cards["bare"], cards["harness"], allow_missing_rates=True,
    ))
    if invalid:
        comparison["resolved"] = False
        comparison["verdict"] = "incomplete-result-records"
    return {
        "scope": "descriptive_summary_only",
        "protocol_validity": "not_assessed",
        "record_count": len(records),
        "record_types": dict(kinds),
        "other_or_unscoped_scenario_records": other_scenarios,
        "invalid_record_lines": invalid,
        "comparison": comparison,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--task-states", type=Path, help="Optional retained JSON array; never queried live")
    parser.add_argument("--operation-id", type=int)
    args = parser.parse_args(argv)
    if args.task_states and (args.operation_id is None or args.operation_id <= 0):
        parser.error("--task-states requires a positive --operation-id")
    records = []
    try:
        for line in args.results.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append(None)
        report = summarize(records, args.scenario)
        if args.task_states:
            try:
                rows = json.loads(args.task_states.read_text())
            except json.JSONDecodeError:
                rows = None
            report["task_snapshot"] = summarize_task_quiescence(rows, expected_operation_id=args.operation_id)
    except (OSError, UnicodeError):
        print(json.dumps({"error": "input_file_unreadable"}))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 2 if report["invalid_record_lines"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
