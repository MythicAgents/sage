#!/usr/bin/env python3
"""Derive composite outer facts from actual bounded live-capture producers."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "Payload_Type/sage"
if str(SOURCE) not in sys.path: sys.path.insert(0, str(SOURCE))

from ai.hillclimb import effect_registry, evaluator_evidence, gate1p_stagea, run_gauge_live
from ai.hillclimb.range_state import Milestone
from ai.langgraph import proof_boundary


class LiveCaptureFactsIncomplete(ValueError): pass


def _load(name: str):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("capture_live_" + name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
    assert spec.loader is not None; spec.loader.exec_module(module)
    return module


native_actions = _load("capture_native_actions")
native_inventory = _load("native_inventory")
_SHA = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_CONTRACT_FIELDS = frozenset({
    "expected_target", "row_identity", "configured_policy_mode", "configured_provider",
    "configured_model", "arm", "pair_id", "observer_version", "token_ceiling",
    "model_context_limit", "max_output_tokens", "current_engagement_id",
    "expected_operation_id", "selected_channel", "selected_request",
})


def _hash(value: Any) -> str:
    try: raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode()
    except (TypeError, ValueError): raise LiveCaptureFactsIncomplete("capture snapshot is malformed") from None
    return hashlib.sha256(raw).hexdigest()


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping): raise LiveCaptureFactsIncomplete(f"{name} is incomplete")
    return value


def _inventory(value: Any, contract: Mapping[str, Any], name: str) -> dict[str, Any]:
    snapshot = _mapping(value, name)
    required = {"schema", "operation_id", "complete", "failures", "snapshot_consistency",
                "query_hashes", "rows"}
    rows = snapshot.get("rows")
    if (set(snapshot) != required or snapshot.get("schema") != "sage-m3-native-inventory-v1"
            or snapshot.get("operation_id") != contract["expected_operation_id"]
            or snapshot.get("complete") is not True or snapshot.get("failures") != []
            or snapshot.get("snapshot_consistency") != "single_operation_root_query"
            or not isinstance(snapshot.get("query_hashes"), Mapping)
            or not isinstance(rows, Mapping) or set(rows) != set(native_inventory.TABLE_FIELDS)):
        raise LiveCaptureFactsIncomplete(f"{name} is malformed or incomplete")
    copied = {}
    for table, fields in native_inventory.TABLE_FIELDS.items():
        records = rows[table]
        if not isinstance(records, list) or any(not isinstance(row, Mapping) or set(row) != set(fields)
                                                for row in records):
            raise LiveCaptureFactsIncomplete(f"{name} {table} inventory is malformed")
        copied[table] = [dict(row) for row in records]
    if native_inventory._lineage_and_terminal_failures(copied):
        raise LiveCaptureFactsIncomplete(f"{name} lineage or terminal inventory is incomplete")
    return copied


def _canonical_objective(record: Any, *, target: str) -> effect_registry.ProofResult:
    result = effect_registry.admit_record(Milestone.OBJECTIVE, record, expected_target=target)
    if result.disposition not in {effect_registry.ProofDisposition.PROVEN,
                                  effect_registry.ProofDisposition.DISPROVEN}:
        raise LiveCaptureFactsIncomplete("independent objective proof is unavailable")
    return result


def _engagement_envelopes(value: Any, engagement_id: str, name: str) -> list[proof_boundary.ProofEnvelope]:
    ledger = _mapping(value, name)
    if ledger.get("engagement_id") != engagement_id:
        raise LiveCaptureFactsIncomplete(f"{name} engagement binding differs")
    envelopes = []
    for collection in ("hops", "graph_facts"):
        rows = ledger.get(collection)
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise LiveCaptureFactsIncomplete(f"{name} {collection} snapshot is malformed")
        for row in rows:
            raw = row.get("proof_envelope")
            if raw is None: continue
            envelope = proof_boundary.ProofEnvelope.from_dict(raw)
            if envelope is None: raise LiveCaptureFactsIncomplete(f"{name} proof envelope is malformed")
            envelopes.append(envelope)
    return envelopes


def derive_live_outer_facts(
    episode: Mapping[str, Any], *, contract: Mapping[str, Any], native_first: Mapping[str, Any],
    native_final: Mapping[str, Any], projected_events: Sequence[Mapping[str, Any]],
    containment_receipt: Mapping[str, Any], engagement_first: Mapping[str, Any],
    engagement_final: Mapping[str, Any],
) -> dict[str, Any]:
    """Join authoritative live producers without scoring or retaining raw ledgers."""
    episode = _mapping(episode, "capture episode"); contract = _mapping(contract, "capture contract")
    if not _CONTRACT_FIELDS <= set(contract):
        raise LiveCaptureFactsIncomplete("capture contract fields are incomplete")
    text_fields = _CONTRACT_FIELDS - {"row_identity", "token_ceiling", "model_context_limit",
                                    "max_output_tokens", "expected_operation_id",
                                    "selected_channel", "selected_request"}
    if any(not isinstance(contract[field], str) or not contract[field].strip() for field in text_fields):
        raise LiveCaptureFactsIncomplete("capture contract text binding is incomplete")
    if any(type(contract[field]) is not int or contract[field] <= 0 for field in
           ("token_ceiling", "model_context_limit", "max_output_tokens", "expected_operation_id")):
        raise LiveCaptureFactsIncomplete("capture contract numerical binding is incomplete")
    if any(type(contract[field]) not in (str, int) or contract[field] in ("", 0)
           for field in ("selected_channel", "selected_request")):
        raise LiveCaptureFactsIncomplete("native chat selection binding is incomplete")
    row_identity = contract["row_identity"]
    if not isinstance(row_identity, Mapping) or set(row_identity) != {"row_id", "surface_id", "family_id"} \
            or any(not isinstance(value, str) or not value for value in row_identity.values()):
        raise LiveCaptureFactsIncomplete("capture row identity is incomplete")

    score = _mapping(episode.get("scorecard"), "capture scorecard")
    proof_records = _mapping(episode.get("proof_records"), "capture proof records")
    objective = _canonical_objective(proof_records.get("objective_proof"),
                                     target=contract["expected_target"])
    objective_success = objective.proven
    oracle = _mapping(episode.get("oracle"), "capture oracle")
    if oracle.get("success") is not objective_success or score.get("objective_proven") is not objective_success:
        raise LiveCaptureFactsIncomplete("objective producers disagree")

    solver = _mapping(episode.get("native_solver_result"), "native solver result")
    telemetry = _mapping(solver.get("runtime_telemetry"), "runtime telemetry")
    if (score.get("policy_mode") != telemetry.get("policy_mode")
            or score.get("configured_policy_mode") != telemetry.get("configured_policy_mode")
            or score.get("policy_identity_valid") is not True
            or score.get("model_provider") != contract["configured_provider"]
            or score.get("model_id") != contract["configured_model"]):
        raise LiveCaptureFactsIncomplete("scorecard policy route differs")
    try:
        run_gauge_live.validate_harness_runtime_telemetry(
            contract["configured_policy_mode"], dict(telemetry),
            configured_provider=contract["configured_provider"],
            configured_model=contract["configured_model"], require_all_response_provenance=True,
            require_complete_usage=True, expected_token_budget=contract["token_ceiling"],
            expected_model_context_limit=contract["model_context_limit"],
            expected_max_output_tokens=contract["max_output_tokens"],
        )
    except Exception: raise LiveCaptureFactsIncomplete("runtime telemetry validation failed") from None
    usage = gate1p_stagea.response_usage_disposition(
        episode.get("response_records"), model_calls=telemetry.get("model_calls"))
    expected_backend = f"{contract['configured_provider']}:{contract['configured_model']}"
    if (usage.get("countable") is not True or usage.get("observed_route", (None,))[0].casefold()
            != expected_backend.casefold() or usage.get("total_tokens") != telemetry.get("total_tokens")
            or score.get("model_calls") != telemetry.get("model_calls")
            or score.get("total_tokens") != usage.get("total_tokens")):
        raise LiveCaptureFactsIncomplete("response usage or backend producers disagree")

    def objective_replay(raw: Any) -> dict[str, Any]:
        raw = _mapping(raw, "observer objective")
        if set(raw) != {"success", "proof_record"}:
            raise gate1p_stagea.ObserverUnavailable("capture objective evidence malformed")
        replayed = _canonical_objective(raw.get("proof_record"), target=contract["expected_target"])
        if raw.get("success") is not replayed.proven or replayed.proven is not objective_success:
            raise gate1p_stagea.ObserverUnavailable("capture objective producers disagree")
        return {"success": replayed.proven, "proof_record": replayed.to_record()}

    observation = episode.get("task_state_observation")
    observer = gate1p_stagea.observer_facts_disposition(
        observation, arm=contract["arm"], pair_id=contract["pair_id"],
        observer_version=contract["observer_version"], oracle_canonicalizer=objective_replay)
    if observer.get("countable") is not True:
        raise LiveCaptureFactsIncomplete("task-state observer evidence is incomplete")
    outcome = gate1p_stagea.episode_outcome_disposition(
        observation, arm=contract["arm"], total_tokens=usage["total_tokens"],
        token_ceiling=contract["token_ceiling"], facts_disposition=lambda raw, *, arm: (
            gate1p_stagea.observer_facts_disposition(raw, arm=arm, pair_id=contract["pair_id"],
                observer_version=contract["observer_version"], oracle_canonicalizer=objective_replay)))
    settlement = _mapping(solver.get("producer_settlement"), "native producer settlement")
    if (outcome.get("countable") is not True or type(outcome.get("clean_stop")) is not bool
            or score.get("clean_stop") is not outcome["clean_stop"]
            or settlement.get("native_producer_terminal") is not True):
        raise LiveCaptureFactsIncomplete("terminal outcome producers disagree")
    terminal_events = [row for row in observer["events"]
        if row.get("source") == "wrapper" and row.get("kind") == "terminal"]
    terminal_within_budget = bool(
        terminal_events[0]["status"].strip().casefold() in gate1p_stagea._NORMAL_WRAPPER_STATUSES[contract["arm"]]
        and outcome.get("token_budget_exhausted") is False)

    first_rows = _inventory(native_first, contract, "initial native snapshot")
    final_rows = _inventory(native_final, contract, "terminal native snapshot")
    first_display_ids = {str(row["display_id"]) for row in first_rows["task"]}
    final_task_by_display = {str(row["display_id"]): row for row in final_rows["task"]}
    final_callback_by_id = {row["id"]: row for row in final_rows["callback"]}
    if len(final_task_by_display) != len(final_rows["task"]):
        raise LiveCaptureFactsIncomplete("terminal task display identity is duplicated")
    observer_task_ids = {str(row.get("display_id") or "") for row in observer["tasks"]}

    transactions = telemetry.get("transactions")
    if not isinstance(transactions, list) or any(not isinstance(row, Mapping) for row in transactions):
        raise LiveCaptureFactsIncomplete("semantic transaction inventory is incomplete")
    transaction_ids = [str(row.get("transaction_id") or "").strip() for row in transactions]
    decisions = telemetry.get("decisions")
    decision_id_values = ([str(row.get("decision_id") or "").strip() for row in decisions]
                          if isinstance(decisions, list)
                          and all(isinstance(row, Mapping) for row in decisions) else [])
    if any(not value for value in decision_id_values) or len(set(decision_id_values)) != len(decision_id_values):
        raise LiveCaptureFactsIncomplete("runtime decision inventory is incomplete")
    decision_ids = set(decision_id_values)
    episode_task_ids = []
    if (any(not value for value in transaction_ids) or len(set(transaction_ids)) != len(transaction_ids)
            or telemetry.get("semantic_transaction_count") != len(transaction_ids)
            or score.get("semantic_transaction_count") != len(transaction_ids)
            or score.get("authorized_transaction_count") != len(transaction_ids)
            or telemetry.get("authorized_transaction_count") != len(transaction_ids)):
        raise LiveCaptureFactsIncomplete("semantic transaction counts or identities differ")
    for transaction in transactions:
        if (str(transaction.get("decision_id") or "") not in decision_ids
                or transaction.get("policy_mode") != contract["configured_policy_mode"]):
            raise LiveCaptureFactsIncomplete("semantic transaction policy lineage is incomplete")
        children = transaction.get("child_tasks")
        if not isinstance(children, list) or any(not isinstance(child, Mapping) for child in children):
            raise LiveCaptureFactsIncomplete("semantic child-task lineage is incomplete")
        for child in children:
            task_id = str(child.get("task_id") or "")
            task = final_task_by_display.get(task_id)
            callback = final_callback_by_id.get(task.get("callback_id")) if task is not None else None
            native_status = str(task.get("status") or "").strip().casefold() if task is not None else ""
            native_terminal = ("completed" if native_status == "completed" else
                               "failed" if native_status not in {"", "started"} else native_status)
            if (not task_id or task_id in first_display_ids or task is None or task.get("completed") is not True
                    or task_id not in observer_task_ids or callback is None
                    or str(callback.get("display_id") or "") != str(transaction.get("callback_id") or "")
                    or native_terminal != str(child.get("terminal_status") or "").strip().casefold()):
                raise LiveCaptureFactsIncomplete("semantic task is absent from independent inventory")
            episode_task_ids.append(task["id"])
    if len(episode_task_ids) != len(set(episode_task_ids)):
        raise LiveCaptureFactsIncomplete("semantic task lineage is duplicated")
    actions = native_actions.extract_native_actions(
        final_rows["task"], final_rows["response"], final_rows["callback"],
        expected_operation_id=contract["expected_operation_id"], episode_task_ids=episode_task_ids)
    if actions.get("complete") is not True:
        raise LiveCaptureFactsIncomplete("native action extraction is incomplete")

    if not isinstance(projected_events, list) or any(not isinstance(row, Mapping) for row in projected_events):
        raise LiveCaptureFactsIncomplete("projected event inventory is malformed")
    policy_rows = [row for row in projected_events if row.get("kind") == "tool_event"
                   and isinstance(row.get("payload"), Mapping)
                   and row["payload"].get("event") == "policy_decision"]
    event_decisions = [row["payload"].get("decision") for row in policy_rows]
    event_ids = [row.get("event_id") for row in policy_rows]
    separated = {"decision_packet", "decision_packet_hash"}
    packet_receipts = []
    if (len(event_decisions) != len(decisions)
            or any(not isinstance(row, Mapping) for row in event_decisions)
            or any(set(event) != set(runtime) | separated
                   or any(event[key] != runtime[key] for key in runtime)
                   or not isinstance(event.get("decision_packet"), Mapping)
                   or not isinstance(event.get("decision_packet_hash"), str)
                   or not _SHA.fullmatch(event["decision_packet_hash"])
                   for event, runtime in zip(event_decisions, decisions))
            or any(not isinstance(value, str) or not value for value in event_ids)
            or len(set(event_ids)) != len(event_ids)):
        raise LiveCaptureFactsIncomplete("retained policy-decision events are incomplete")
    packet_receipts = [{"decision_id": runtime["decision_id"],
                        "decision_packet_hash": event["decision_packet_hash"]}
                       for event, runtime in zip(event_decisions, decisions)]
    model_branch = any(str(row.get("decision_owner") or "") == "model_branch"
                       and type(row.get("branch_opportunity_count")) is int
                       and row["branch_opportunity_count"] > 0 for row in decisions)

    containment = _mapping(containment_receipt, "containment receipt")
    steps = containment.get("cleanup_steps")
    if (containment.get("complete") is not True or containment.get("owned_processes_dead") is not True
            or not isinstance(steps, list) or not steps
            or any(not isinstance(step, Mapping) or not isinstance(step.get("name"), str)
                   or not step["name"] or step.get("result") != "complete" for step in steps)):
        raise LiveCaptureFactsIncomplete("independent containment is incomplete")
    if (type(outcome.get("task_quiescent")) is not bool
            or score.get("clean_stop") is not bool(terminal_within_budget
                                                   and outcome["task_quiescent"])):
        raise LiveCaptureFactsIncomplete("clean-stop containment evidence disagrees")

    first_envelopes = {envelope.hash for envelope in _engagement_envelopes(
        engagement_first, contract["current_engagement_id"], "initial engagement snapshot")}
    candidates = [envelope for envelope in _engagement_envelopes(
        engagement_final, contract["current_engagement_id"], "terminal engagement snapshot")
        if envelope.hash not in first_envelopes]
    safe_proofs = []
    tasks_by_display = {str(row["display_id"]): row for row in final_rows["task"]}
    for envelope in candidates:
        transaction = next((row for row in transactions
                            if row["transaction_id"] == envelope.transaction_id), None)
        child = next((row for row in (transaction or {}).get("child_tasks", [])
                      if str(row.get("task_id") or "") == envelope.task_id), None)
        task = tasks_by_display.get(envelope.task_id)
        lineages = [row for row in (transaction or {}).get("proof_lineage", [])
            if isinstance(row, Mapping) and row.get("proof_envelope_id") == envelope.hash]
        lineage = lineages[0] if len(lineages) == 1 else None
        if (transaction is None or child is None or task is None or task.get("completed") is not True
                or not isinstance(lineage, Mapping) or lineage.get("transaction_id") != envelope.transaction_id
                or str(lineage.get("task_id") or "") != envelope.task_id
                or lineage.get("verifier_id") != envelope.verifier_id
                or lineage.get("verifier_hash") != envelope.verifier_hash
                or lineage.get("admissible_for_runtime_achievement") is not True
                or envelope.hash not in transaction.get("proof_envelope_ids", [])
                or envelope.verifier_id not in transaction.get("verifier_ids", [])):
            raise LiveCaptureFactsIncomplete("proof transaction lineage is incomplete")
        admission = proof_boundary.admit_runtime_envelope(
            envelope, current_engagement_id=contract["current_engagement_id"],
            expected_callback_id=str(transaction.get("callback_id") or ""),
            expected_transaction_id=transaction["transaction_id"], expected_task_id=child["task_id"],
            expected_terminal_status=str(child.get("terminal_status") or ""),
            expected_verifier_id=str(lineage.get("verifier_id") or ""))
        if not admission.admitted:
            raise LiveCaptureFactsIncomplete("runtime proof envelope was not admitted")
        safe_proofs.append(envelope.to_dict())
    if objective_success and not safe_proofs:
        raise LiveCaptureFactsIncomplete("positive objective lacks a new current-episode proof")
    proof_projection = evaluator_evidence.project_canonical_evidence(
        {}, row_id=row_identity["row_id"], policy_arm=score.get("measurement_identity", {}).get("policy_arm"),
        surface_id=row_identity["surface_id"], family_id=row_identity["family_id"],
        derived_outcome={}, proof_envelopes=safe_proofs,
        semantic_transaction_count=len(transaction_ids), model_owned_branch_observed=model_branch)

    selected_channel, selected_request = contract["selected_channel"], contract["selected_request"]
    request = next((row for row in final_rows["chat_request"]
                    if row["id"] == selected_request and row["channel_id"] == selected_channel), None)
    chat_rows = [row for row in final_rows["chat_message"]
                 if row.get("chat_request_id") == selected_request
                 and row.get("channel_id") == selected_channel]
    if request is None or not chat_rows:
        raise LiveCaptureFactsIncomplete("native terminal chat selection is incomplete")
    for row in chat_rows:
        for key in ("message", "tool_output", "metadata"):
            value = row.get(key)
            if value in (None, "", {}, []): continue
            if isinstance(value, Mapping): continue
            if isinstance(value, str):
                try: decoded = json.loads(value)
                except ValueError: decoded = None
                if isinstance(decoded, Mapping): continue
            metadata_taxonomy_complete = False
            break
        else:
            continue
        break
    else:
        metadata_taxonomy_complete = True

    return {"source_kind": "live", "row_identity": dict(row_identity),
        "proof_envelopes": proof_projection["proof_envelopes"],
        "cleanup_complete": True, "negative_oracle_complete": not objective_success,
        "backend_inventory_complete": True, "semantic_transactions": transaction_ids,
        "policy_event_inventory": {"complete": True, "model_owned_branch_observed": model_branch},
        "native_actions": actions, "native_chat_rows": chat_rows,
        "selected_channel": selected_channel, "selected_request": selected_request,
        "terminal_snapshot": {"terminal": terminal_within_budget, "inventory_complete": True,
            "native_tasks_quiescent": outcome["task_quiescent"], "chat_inventory_complete": True,
            "metadata_taxonomy_complete": metadata_taxonomy_complete,
            "source_sha256": _hash(native_final)},
        "observer_version": contract["observer_version"],
        "source_receipts": {"objective_disposition": objective.disposition.value,
            "usage_sha256": _hash(usage["canonical_records"]),
            "task_observer_sha256": _hash(observation), "projected_events_sha256": _hash(projected_events),
            "native_first_sha256": _hash(native_first), "native_final_sha256": _hash(native_final),
            "containment_sha256": _hash(containment_receipt), "engagement_first_sha256": _hash(engagement_first),
            "engagement_final_sha256": _hash(engagement_final), "proof_count": len(safe_proofs),
            "policy_decision_packets": packet_receipts}}
