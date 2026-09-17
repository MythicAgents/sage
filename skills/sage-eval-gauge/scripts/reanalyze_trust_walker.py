#!/usr/bin/env python3
"""Read-only ISC-8 reanalysis from retained native task cards and pair ledgers.

Print a new derived report; never rewrite inputs, task labels, or historical dispositions.
No runtime construction, network, reset, provider call, or target action is performed.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_trust_walker_pilot as pilot
from ai.hillclimb.trust_walker_outcome import certificate_admin_candidate, classify_terminal_snapshot
from ai.hillclimb.trust_walker_pilot import ImprovementContract, summarize_pair


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconstruct_arm(arm_result: dict, start: dict, transcript: dict) -> tuple[dict, dict]:
    """Join arm-local Mythic task identities to complete, server-timestamped native cards.

    Card completion is an upper bound on the existence of its full task output, not
    a fabricated raw response timestamp. Raw response IDs/sequences remain unknown.
    """
    result = deepcopy(arm_result)
    snapshot = result["terminal_snapshot"]
    runtime = start["runtime_identity"]
    if runtime["operation_id"] != snapshot["operation_id"]:
        raise ValueError("arm operation binding mismatch")
    tasks = {row["display_id"]: row for row in snapshot["tasks"]}
    cards = {}
    for message in transcript["messages"]:
        card = (message.get("metadata") or {}).get("tool_use")
        if not isinstance(card, dict) or card.get("tool_source") != "mythic":
            continue
        arguments = json.loads(card.get("arguments") or "{}")
        task_id = arguments.get("task_id")
        if type(task_id) is not int or task_id not in tasks:
            continue
        if task_id in cards:
            raise ValueError("ambiguous duplicate task card")
        if (
            arguments.get("callback_id") != runtime["callback_id"]
            or message.get("channel_id") != transcript["chat_channel_id"]
            or message.get("chat_request_id") != transcript["chat_request_id"]
            or message.get("deleted") is not False
            or message.get("edited") is not False
        ):
            raise ValueError("native task card lineage mismatch")
        cards[task_id] = (message, card, arguments)

    candidates, rejections, gaps, rows, provenance = [], [], [], [], []
    complete_ids = set()
    for task_id, task in sorted(tasks.items()):
        row = dict(task)
        rows.append(row)  # Keep every task, including unclassified transaction tasks.
        if task_id not in cards:
            gaps.append({"task_id": task_id, "reason": "native card absent"})
            continue
        message, card, arguments = cards[task_id]
        output = card.get("result_preview")
        complete = (
            card.get("status") in {"completed", "error"}
            and card.get("output_available") is True
            and isinstance(output, str)
            and type(card.get("output_size")) is int
            and len(output) == card["output_size"]
        )
        if not complete:
            gaps.append({"task_id": task_id, "reason": "native output absent or truncated"})
            continue
        complete_ids.add(task_id)
        # Parameters are from the retained native execution event, not a model verdict.
        params = arguments.get("parameters")
        row["original_params"] = params if isinstance(params, str) else json.dumps(params)
        row["display_params"] = row["original_params"]
        # Encoding adapter for the metric only; no invented raw response metadata.
        row["responses"] = [{"response_text": base64.b64encode(output.encode()).decode()}]
        artifact_at = message.get("updated_at")
        try:
            valid_time = (
                timestamp(task["created_at"]) <= timestamp(artifact_at) <= timestamp(snapshot["observed_at"])
                and timestamp(snapshot["arm_started_at"]) <= timestamp(message["created_at"])
                <= timestamp(artifact_at)
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            valid_time = False
        if not valid_time:
            gaps.append({"task_id": task_id, "reason": "native artifact timing unavailable or inconsistent"})
            continue
        candidate = certificate_admin_candidate(
            row, output, effect_at=artifact_at, operation_id=snapshot["operation_id"],
            target=snapshot["target"], netbios_domains=json.loads(pilot.NETBIOS_MAP),
            rejected_proofs=rejections,
        )
        if candidate is not None:
            candidate["effect_time_basis"] = "native_chat_artifact_upper_bound"
            candidate["artifact_id"] = message["id"]
            candidates.append(candidate)
            if timestamp(artifact_at) > timestamp(snapshot["arm_deadline_at"]):
                gaps.append({"task_id": task_id, "reason": "artifact upper bound is after deadline; exact effect time unknown"})
            provenance.append({
                "task_id": task_id, "operation_id": snapshot["operation_id"],
                "callback_id": runtime["callback_id"], "message_id": message["id"],
                "chat_channel_id": transcript["chat_channel_id"],
                "chat_request_id": transcript["chat_request_id"],
                "output_characters": len(output),
                "task_output_sha256": candidate["task_output_sha256"],
                "task_parameter_sha256": candidate["task_parameter_sha256"],
                "effect_at_upper_bound": artifact_at,
                "exact_first_proof_response_at": None,
                "raw_response_id": None,
            })

    metric = pilot.certificate_auth_primary_metric(rows)
    if metric.get("complete") and not set(metric["transaction_task_ids"]) <= complete_ids:
        metric = {"complete": False, "reason": "transaction card output coverage is incomplete"}
    original_outcome = deepcopy(result["outcome"])
    original_metric = deepcopy(snapshot.get("certificate_auth_primary_metric"))
    original_proofs = deepcopy(snapshot["proof_candidates"])
    snapshot["proof_candidates"] = candidates
    snapshot["proof_rejections"] = rejections
    snapshot["certificate_auth_primary_metric"] = metric
    if gaps:
        # Incomplete reconstruction must not become a newly inferred negative result.
        snapshot["observer_complete"]["tasks"] = False
    result["outcome"] = classify_terminal_snapshot(snapshot).to_record()
    snapshot["intermediate_milestones"]["OBJECTIVE"] = {
        "disposition": result["outcome"]["disposition"],
        "task_ids": [task_id for item in candidates for task_id in item["task_ids"]]
        if result["outcome"]["disposition"] == "proven" else [],
    }
    return result, {
        "arm": result["arm"], "original_outcome": original_outcome,
        "corrected_outcome": result["outcome"],
        "original_metric": original_metric, "corrected_metric": metric,
        "proof_provenance": provenance, "reconstruction_gaps": gaps,
        "original_proof_timing": [{
            "task_ids": item.get("task_ids"), "effect_at": item.get("effect_at"),
            "response_id": item.get("response_id"),
        } for item in original_proofs],
        "resources": result["execution"]["cost"],
    }


def reanalyze(root: Path) -> dict:
    comparisons, contracts, pairs, inputs = [], [], [], {}
    for dirname in ("pair-001-baseline-first", "pair-002-candidate-first"):
        pair = root / dirname
        ledger = pair / "pilot.jsonl"
        inputs[str(ledger.resolve())] = sha256(ledger)
        records = pilot.read_records(ledger)
        originals = [r["contract"] for r in records if r.get("record_type") == "contract"]
        if len(originals) != 1:
            raise ValueError("pair must have one frozen contract")
        original = originals[0]
        contract = ImprovementContract(
            original["baseline_source_identity"], original["candidate_source_identity"],
            arm_order=tuple(original["arm_order"]),
        )
        contract.validate()
        if original != contract.to_record():
            raise ValueError("retained contract differs from frozen decision criteria")
        contracts.append(original)
        starts = {r["arm"]: r for r in records if r.get("record_type") == "arm_started"}
        derived_records, arms = [], []
        for record in records:
            if record.get("record_type") != "arm_result":
                derived_records.append(record)
                continue
            path = pair / (record["arm"] + "-transcript.json")
            inputs[str(path.resolve())] = sha256(path)
            binding = record["execution"].get("transcript") or {}
            if (
                inputs[str(path.resolve())] != binding.get("sha256")
                or Path(binding.get("path", "")).name != path.name
            ):
                raise ValueError("transcript does not match retained arm hash binding")
            if starts[record["arm"]]["start_identity"].get("source_identity") != contract.source_identity_for_arm(record["arm"]):
                raise ValueError("retained arm source identity mismatch")
            derived, detail = reconstruct_arm(record, starts[record["arm"]], json.loads(path.read_text()))
            derived_records.append(derived)
            arms.append(detail)
        if sorted(r["arm"] for r in arms) != sorted(pilot.IMPROVEMENT_ARM_ORDER):
            raise ValueError("exactly one retained result per arm is required")
        comparison = summarize_pair(derived_records, contract)
        comparisons.append(comparison)
        pairs.append({"pair": dirname, "arms": arms, "corrected_comparison": comparison})
    decision = pilot.summarize_improvement_comparisons(tuple(contracts), tuple(comparisons), contract)
    original_path = root / "final-disposition.json"
    inputs[str(original_path.resolve())] = sha256(original_path)
    # Bind current evaluator bytes independently from the historical runtime identities.
    source = {}
    for relative in (
        "Payload_Type/sage/ai/hillclimb/effect_registry.py",
        "Payload_Type/sage/ai/hillclimb/trust_walker_outcome.py",
        "Payload_Type/sage/ai/hillclimb/trust_walker_pilot.py",
        "skills/sage-eval-gauge/scripts/run_trust_walker_pilot.py",
        "skills/sage-eval-gauge/scripts/reanalyze_trust_walker.py",
    ):
        source[relative] = sha256(pilot.ROOT / relative)
    return {
        "schema": "sage-trust-walker-historical-reanalysis-v1", "new_trial": False,
        "input_sha256": inputs, "evaluator_source_sha256": source,
        "original_disposition": {
            key: value for key, value in json.loads(original_path.read_text()).items()
            if key in {"schema", "disposition", "reason"}
        },
        "corrected_decision": decision, "pairs": pairs,
        "timing_basis": "complete native task-output artifact timestamp is a conservative upper bound",
        "limitations": [
            "No raw candidate response IDs, sequence numbers, or exact first-proof timestamps were retained.",
            "Two order-balanced pairs support the frozen internal development decision, not statistical superiority or release.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(reanalyze(args.attempt_dir.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
