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
from ai.hillclimb.experiment_contracts import select_comparison_results
from ai.hillclimb.fitness import ScoreCard
from ai.hillclimb.live_seams import summarize_task_quiescence
from ai.hillclimb.range_state import Milestone
from ai.hillclimb.reproducible_claims import comparison_labels, reporting_scope


def summarize(
    records: list,
    scenario: str,
    *,
    headline_request: dict | None = None,
) -> dict:
    """Retain record counts and malformed-result locations, including unsuccessful attempts."""
    cards = {"bare": [], "harness": []}
    kinds: Counter = Counter()
    invalid = []
    identity_mismatches = []
    other_scenarios = 0
    identities = []
    producer_case_ids = []
    producer_origins = []
    card_fields = {field.name for field in fields(ScoreCard)}
    selected = select_comparison_results(records, scenario)
    selected_ids = {id(record) for record in selected}
    for line, record in enumerate(records, 1):
        if not isinstance(record, dict):
            invalid.append(line)
            continue
        kind = record.get("record_type")
        kinds[kind if isinstance(kind, str) and kind in {"result", "attempt", "cleanup", "prestart_cleanup"} else "other"] += 1
        if record.get("scenario") != scenario:
            other_scenarios += 1
            continue
        if id(record) not in selected_ids:
            continue
        origin = record.get("evaluation_origin")
        if isinstance(origin, dict):
            retained_origin = {
                key: origin.get(key)
                for key in ("split_registry_id", "cohort_id", "role", "source_case_id")
            }
        else:
            # Every selected row must carry its own producer provenance.  Do not
            # let a summary-level header relabel historical development evidence.
            retained_origin = {}
        producer_origins.append(retained_origin)
        source_case_id = retained_origin.get("source_case_id")
        if isinstance(source_case_id, str) and source_case_id.strip() and source_case_id.strip() not in producer_case_ids:
            producer_case_ids.append(source_case_id.strip())
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
        retained_identity = record.get("measurement_identity")
        identities.append(retained_identity)
        if card.measurement_identity != retained_identity:
            identity_mismatches.append(line)
    comparison = asdict(compare_bare_vs_harness(
        scenario, cards["bare"], cards["harness"], allow_missing_rates=True,
    ))
    if invalid:
        comparison["resolved"] = False
        comparison["verdict"] = "incomplete-result-records"
    measurement = comparison_labels(identities)
    if identity_mismatches:
        measurement = {
            **measurement,
            "performance_assertion_allowed": False,
            "identity_failures": [*measurement["identity_failures"],
                                  "ScoreCard labels differ from retained result metadata"],
        }
    report = {
        "scope": "descriptive_summary_only",
        "protocol_validity": "not_assessed",
        "record_count": len(records),
        "record_types": dict(kinds),
        "other_or_unscoped_scenario_records": other_scenarios,
        "invalid_record_lines": invalid,
        "comparison": comparison,
        "measurement": measurement,
        "identity_mismatch_lines": identity_mismatches,
        "producer_case_ids": producer_case_ids,
        "producer_origins": producer_origins,
    }
    # A comparison remains useful development evidence.  Headline status is a
    # separate final-audit decision over the computed comparison, never a flag
    # supplied by the input records.
    report.update(reporting_scope(
        report,
        reducer_name="comparison",
        headline_request=headline_request,
        producer_case_ids=producer_case_ids,
        producer_origins=producer_origins,
    ))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--task-states", type=Path, help="Optional retained JSON array; never queried live")
    parser.add_argument("--operation-id", type=int)
    parser.add_argument(
        "--headline-audit", type=Path,
        help="optional metadata-only final-audit split/receipt request; does not open outcome packets",
    )
    args = parser.parse_args(argv)
    if args.task_states and (args.operation_id is None or args.operation_id <= 0):
        parser.error("--task-states requires a positive --operation-id")
    records = []
    try:
        headline_request = None
        if args.headline_audit:
            try:
                headline_request = json.loads(args.headline_audit.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                headline_request = {}
        for line in args.results.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append(None)
        report = summarize(records, args.scenario, headline_request=headline_request)
        if args.task_states:
            try:
                rows = json.loads(args.task_states.read_text())
            except json.JSONDecodeError:
                rows = None
            report["task_snapshot"] = summarize_task_quiescence(rows, expected_operation_id=args.operation_id)
    except (OSError, UnicodeError):
        print(json.dumps({"error": "input_file_unreadable"}))
        return 2
    except ValueError:
        print(json.dumps({
            "scope": "descriptive_summary_only",
            "protocol_validity": "not_assessed",
            "measurement": {"performance_assertion_allowed": False, "identities": [],
                            "identity_failures": ["comparison_lifecycle_unavailable"]},
            "comparison": {"available": False, "reason": "comparison_lifecycle_unavailable"},
        }))
        return 2
    if report["measurement"]["performance_assertion_allowed"] is not True:
        # ``summarize`` intentionally retains descriptive arithmetic for local
        # diagnosis.  This public CLI must not render it as a performance report
        # when its producer labels are unavailable or contradictory.
        safe_report = {
            "scope": report["scope"],
            "protocol_validity": report["protocol_validity"],
            "record_count": report["record_count"],
            "record_types": report["record_types"],
            "other_or_unscoped_scenario_records": report["other_or_unscoped_scenario_records"],
            "invalid_record_lines": report["invalid_record_lines"],
            "identity_mismatch_lines": report["identity_mismatch_lines"],
            "measurement": report["measurement"],
            "comparison": {"available": False, "reason": "measurement_labels_unavailable"},
        }
        if "task_snapshot" in report:
            safe_report["task_snapshot"] = report["task_snapshot"]
        print(json.dumps(safe_report, indent=2, allow_nan=False))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 2 if report["invalid_record_lines"] or (
        args.headline_audit is not None and report["headline_lift_allowed"] is not True
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
