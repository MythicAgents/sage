"""Shared authority, lifecycle, and admission kernel for development-measurement rows.

The live driver, its coordinator, and offline calibration must call this module rather
than trusting one another's derived ``row_admission`` fields.  The kernel consumes the
prospective contract plus retained raw observer/artifact evidence and fails closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

try:
    from . import effect_registry, gate1p_stagea
    from .range_state import Milestone
except ImportError:
    from ai.hillclimb import effect_registry, gate1p_stagea  # type: ignore
    from ai.hillclimb.range_state import Milestone  # type: ignore


CONTRACT_SCHEMA = "sage-isc11-non-goad-replication-experiment-contract-v1"
OBSERVER_VERSION = "sage-development-row-observer-facts-v1"
ROW_ID = re.compile(r"isc11-row-(\d{3})\Z")


def hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_contract(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    resolved = Path(path).expanduser().resolve()
    try:
        raw = resolved.read_bytes()
        contract = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise gate1p_stagea.ProtocolError(f"development-row contract is unreadable: {exc}") from exc
    observed = hashlib.sha256(raw).hexdigest()
    if not isinstance(contract, dict) or contract.get("schema") != CONTRACT_SCHEMA:
        raise gate1p_stagea.ProtocolError("development-row contract schema is missing or mismatched")
    if expected_sha256 is not None and observed != str(expected_sha256).casefold():
        raise gate1p_stagea.ProtocolError("development-row contract hash differs from the expected identity")
    return contract, observed


def contamination_markers(contract: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the prospective benchmark-exclusion policy in canonical form."""
    policy = contract.get("contamination_policy")
    markers = policy.get("markers") if isinstance(policy, Mapping) else None
    if (
        not isinstance(policy, Mapping)
        or set(policy) != {"kind", "markers"}
        or policy.get("kind") != "forbidden_identifiers_v1"
        or not isinstance(markers, list)
        or not markers
    ):
        raise gate1p_stagea.ProtocolError("development-row contamination policy is missing or malformed")
    canonical = tuple(str(marker).strip().casefold() for marker in markers)
    if (
        any(not marker or len(marker) < 4 for marker in canonical)
        or len(canonical) != len(set(canonical))
    ):
        raise gate1p_stagea.ProtocolError("development-row contamination markers are empty, short, or duplicated")
    return canonical


def contract_storage_paths(
    contract: Mapping[str, Any], *, repo_root: Path,
) -> tuple[Path, Path]:
    """Resolve the one experiment-owned artifact root and lifecycle ledger."""
    execution = contract.get("execution_path")
    raw_root = str(execution.get("results_root") or "") if isinstance(execution, Mapping) else ""
    ledger_name = str(execution.get("results_ledger") or "") if isinstance(execution, Mapping) else ""
    relative_root = Path(raw_root)
    if (
        not raw_root
        or relative_root.is_absolute()
        or ".." in relative_root.parts
        or not ledger_name
        or Path(ledger_name).name != ledger_name
    ):
        raise gate1p_stagea.ProtocolError("development-row contract storage identity is malformed")
    root = (repo_root.resolve() / relative_root).resolve()
    history_root = (repo_root.resolve() / ".sage_history").resolve()
    if root == history_root or history_root not in root.parents:
        raise gate1p_stagea.ProtocolError("development-row result root must stay below private .sage_history")
    return root, root / ledger_name


def matrix_row(contract: Mapping[str, Any], row_id: str) -> tuple[int, str]:
    match = ROW_ID.fullmatch(str(row_id or ""))
    if match is None:
        raise gate1p_stagea.ProtocolError("row_id must have the exact form isc11-row-NNN")
    row_index = int(match.group(1))
    matrix = contract.get("matrix")
    order = matrix.get("order") if isinstance(matrix, Mapping) else None
    if (
        not isinstance(order, list)
        or len(order) != matrix.get("total_rows")
        or row_index < 1
        or row_index > len(order)
        or any(item not in {"symbolic", "llm", "hybrid"} for item in order)
    ):
        raise gate1p_stagea.ProtocolError("development-row matrix order is malformed or row is out of range")
    return row_index, str(order[row_index - 1])


def authorized_live_row_ids(contract: Mapping[str, Any]) -> tuple[str, ...]:
    matrix = contract.get("matrix")
    initial = matrix.get("initial_live_tranche") if isinstance(matrix, Mapping) else None
    rows = initial.get("rows") if isinstance(initial, Mapping) else None
    policies = initial.get("policies") if isinstance(initial, Mapping) else None
    if (
        not isinstance(rows, list)
        or not rows
        or any(type(index) is not int or index <= 0 for index in rows)
        or len(rows) != len(set(rows))
        or initial.get("whole_row_retries") != 0
        or not isinstance(policies, list)
        or len(policies) != len(rows)
    ):
        raise gate1p_stagea.ProtocolError("development-row initial live tranche is malformed")
    expected: list[str] = []
    for index, policy in zip(rows, policies):
        row_id = f"isc11-row-{index:03d}"
        _index, actual_policy = matrix_row(contract, row_id)
        if actual_policy != policy:
            raise gate1p_stagea.ProtocolError("development-row live tranche policy order is mismatched")
        expected.append(row_id)
    return tuple(expected)


def validate_live_authority(
    contract: Mapping[str, Any],
    *,
    row_id: str,
    live_authority_id: str | None,
) -> None:
    gate = contract.get("authority_gate")
    expected_authority = str(gate.get("live_authority_id") or "") if isinstance(gate, Mapping) else ""
    if not expected_authority or live_authority_id != expected_authority:
        raise gate1p_stagea.ProtocolError(
            f"live development row requires separate authority ID {expected_authority!r}"
        )
    if row_id not in authorized_live_row_ids(contract):
        raise gate1p_stagea.ProtocolError(
            f"row {row_id!r} is outside the exact authorized live tranche"
        )


def append_jsonl_durable(path: Path, record: Mapping[str, Any]) -> None:
    """Append one lifecycle fact and fsync it before the caller may mutate the range."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def begin_row(
    contract: Mapping[str, Any],
    *,
    repo_root: Path,
    row_id: str,
    protocol_id: str,
    contract_sha256: str,
    timestamp: float,
) -> dict[str, Any]:
    _root, path = contract_storage_paths(contract, repo_root=repo_root)
    try:
        existing = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ] if path.exists() else []
    except (OSError, json.JSONDecodeError) as exc:
        raise gate1p_stagea.ProtocolError(f"development-row lifecycle ledger is unreadable: {exc}") from exc
    if any(isinstance(record, Mapping) and record.get("row_id") == row_id for record in existing):
        raise gate1p_stagea.ProtocolError(f"retry-zero row {row_id} already has retained state")
    record = {
        "record_type": "row_started",
        "measurement_kind": "row",
        "protocol_id": protocol_id,
        "contract_sha256": contract_sha256,
        "row_id": row_id,
        "row_state": "started",
        "ts": timestamp,
    }
    append_jsonl_durable(path, record)
    return record


def _started_disposition_path(
    path: Path,
    *,
    row_id: str,
    protocol_id: str,
    contract_sha256: str,
) -> dict[str, Any]:
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "reason": f"development-row lifecycle ledger is unreadable: {exc}"}
    matches = [
        record for record in records
        if isinstance(record, Mapping) and record.get("row_id") == row_id
        and record.get("record_type") == "row_started"
    ]
    expected = {
        "record_type": "row_started", "measurement_kind": "row",
        "protocol_id": protocol_id, "contract_sha256": contract_sha256,
        "row_id": row_id, "row_state": "started",
    }
    if len(matches) != 1 or any(matches[0].get(key) != value for key, value in expected.items()):
        return {"valid": False, "reason": "exactly one matching durable row_started fact is required"}
    if type(matches[0].get("ts")) not in (int, float):
        return {"valid": False, "reason": "durable row_started timestamp is malformed"}
    return {"valid": True, "reason": "durable retry-zero start is present", "record": matches[0]}


def started_disposition(
    contract: Mapping[str, Any],
    *,
    repo_root: Path,
    row_id: str,
    protocol_id: str,
    contract_sha256: str,
) -> dict[str, Any]:
    try:
        _root, path = contract_storage_paths(contract, repo_root=repo_root)
    except gate1p_stagea.ProtocolError as exc:
        return {"valid": False, "reason": str(exc)}
    return _started_disposition_path(
        path,
        row_id=row_id,
        protocol_id=protocol_id,
        contract_sha256=contract_sha256,
    )


def canonical_oracle_fact(contract: Mapping[str, Any], raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {"success", "proof_record", "artifact_pointer"}:
        raise gate1p_stagea.ObserverUnavailable("development-row objective oracle schema is not exact")
    binding = contract.get("range_binding") or {}
    expected_target = str(binding.get("objective_domain") or "")
    proof = effect_registry.admit_record(Milestone.OBJECTIVE, raw.get("proof_record"), expected_target=expected_target)
    if proof.disposition not in {
        effect_registry.ProofDisposition.PROVEN,
        effect_registry.ProofDisposition.DISPROVEN,
    }:
        raise gate1p_stagea.ObserverUnavailable(proof.reason)
    if type(raw.get("success")) is not bool or raw["success"] is not proof.proven:
        raise gate1p_stagea.ObserverUnavailable("development-row oracle outcome disagrees with typed proof")
    pointer = str(raw.get("artifact_pointer") or "").strip()
    if not pointer:
        raise gate1p_stagea.ObserverUnavailable("development-row oracle artifact pointer is missing")
    return {"success": proof.proven, "proof_record": proof.to_record(), "artifact_pointer": pointer}


def observer_facts_disposition(
    contract: Mapping[str, Any], row_id: str, observer: object,
) -> dict[str, Any]:
    protocol_id = str(contract.get("contract_id") or "")
    return gate1p_stagea.observer_facts_disposition(
        observer,
        arm=row_id,
        pair_id=protocol_id,
        arms=(row_id,),
        observer_version=OBSERVER_VERSION,
        oracle_canonicalizer=lambda raw: canonical_oracle_fact(contract, raw),
    )


def response_usage_disposition(
    contract: Mapping[str, Any], row_id: str, response_records: object, *, model_calls: object,
) -> dict[str, Any]:
    _index, policy_mode = matrix_row(contract, row_id)
    if policy_mode == "symbolic" and model_calls == 0 and response_records == []:
        return {
            "countable": True,
            "reason": "symbolic policy made no model calls",
            "observed_route": None,
            "total_tokens": 0,
            "normalized_usage": [],
            "canonical_records": [],
        }
    return gate1p_stagea.response_usage_disposition(response_records, model_calls=model_calls)


def outcome_disposition(
    contract: Mapping[str, Any], row_id: str, observer: object, *, total_tokens: object,
) -> dict[str, Any]:
    facts = observer_facts_disposition(contract, row_id, observer)
    if facts.get("countable") is not True:
        return facts
    ceiling = (contract.get("execution_identity") or {}).get("completed_call_token_ceiling_per_row")
    if type(total_tokens) is not int or total_tokens < 0 or type(ceiling) is not int or ceiling <= 0:
        return {"countable": False, "reason": "token evidence is malformed"}
    terminal = [
        event for event in facts["events"]
        if event.get("source") == "wrapper" and event.get("kind") == "terminal"
    ]
    if len(terminal) != 1:
        return {"countable": False, "reason": "observer lacks exactly one wrapper terminal"}
    if not facts["tasks"]:
        return {"countable": False, "reason": "positive development row lacks retained Mythic task evidence"}
    normal = str(terminal[0].get("status") or "").strip().casefold() in {
        "done", "success", "complete", "completed", "stopped",
    }
    task_window = facts["task_window_observed"] is True
    task_quiescent = all(task["completed"] is True for task in facts["tasks"]) if task_window else None
    clean_stop = bool(normal and task_quiescent and total_tokens < ceiling) if task_window else None
    recognized = facts["wrapper_objective_recognized"] if task_window else None
    objective_success = facts["oracle"]["success"]
    return {
        "countable": True,
        "reason": "outcome and evaluator-owned task-stop evidence are available",
        "stop_countable": task_window,
        "objective_success": objective_success,
        "clean_stop": clean_stop,
        "objective_recognized": recognized,
        "objective_clean_stop": bool(objective_success and clean_stop and recognized) if task_window else None,
        "task_quiescent": task_quiescent,
        "token_budget_exhausted": total_tokens >= ceiling,
        "token_budget_overshoot": total_tokens > ceiling,
    }


def _authority_disposition(
    contract: Mapping[str, Any], tasks: object, *, initial_callback_id: object, operation_id: object,
) -> bool:
    if type(initial_callback_id) is not int or initial_callback_id <= 0:
        return False
    if type(operation_id) is not int or operation_id <= 0 or not isinstance(tasks, list) or not tasks:
        return False
    binding = contract.get("range_binding") or {}
    expected_host = str(binding.get("foothold_callback_host") or "").casefold()
    expected_user = str(binding.get("foothold_callback_user") or "").casefold()
    objective_domain = str(binding.get("objective_domain") or "").casefold()
    expected_domains = {objective_domain, objective_domain.split(".", 1)[0]}
    for task in tasks:
        callback = task.get("callback") if isinstance(task, Mapping) else None
        if (
            not isinstance(task, Mapping)
            or task.get("operation_id") != operation_id
            or not isinstance(callback, Mapping)
            or callback.get("display_id") != initial_callback_id
            or str(callback.get("host") or "").casefold() != expected_host
            or str(callback.get("user") or "").casefold() != expected_user
            or str(callback.get("domain") or "").casefold() not in expected_domains
        ):
            return False
    return True


def _retained_path(value: object, *, history_root: Path) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    try:
        path.relative_to(history_root.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _artifact_failures(
    contract: Mapping[str, Any], row: Mapping[str, Any], *, history_root: Path,
) -> tuple[list[str], Mapping[str, Any] | None]:
    capture = row.get("artifact_capture")
    if not isinstance(capture, Mapping) or capture.get("complete") is not True:
        return ["artifact_evidence_invalid"], None
    episode_path = _retained_path(capture.get("episode_artifact"), history_root=history_root)
    ledger_path = _retained_path(capture.get("result_ledger"), history_root=history_root)
    if episode_path is None or ledger_path is None:
        return ["artifact_evidence_invalid"], None
    execution = contract.get("execution_path") or {}
    ledger_name = str(execution.get("results_ledger") or "") if isinstance(execution, Mapping) else ""
    if not ledger_name or Path(ledger_name).name != ledger_name or ledger_path != (history_root / ledger_name).resolve():
        return ["artifact_evidence_invalid"], None
    try:
        artifact = json.loads(episode_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["artifact_evidence_invalid"], None
    protocol = row.get("protocol") or {}
    transcript = artifact.get("transcript") if isinstance(artifact, Mapping) else None
    transcript_entries = (
        len(transcript.get("events") or []) + 1
        if isinstance(transcript, Mapping)
        else len(transcript) if isinstance(transcript, list) else -1
    )
    expected_equal = {
        "protocol": protocol,
        "side": row.get("side"),
        "arm": row.get("row_id"),
        "reset_readiness": protocol.get("reset_readiness") if isinstance(protocol, Mapping) else None,
        "oracle": (row.get("observer") or {}).get("oracle"),
        "observer": row.get("observer"),
        "runtime_summary": row.get("runtime_summary"),
        "response_evidence": row.get("model_response_records"),
        "scorecard": row.get("card"),
        "objective_proof": row.get("objective_proof"),
    }
    oracle_pointer = str(((row.get("observer") or {}).get("oracle") or {}).get("artifact_pointer") or "")
    if (
        not isinstance(artifact, Mapping)
        or artifact.get("schema") != "sage-development-row-episode-artifact-v1"
        or any(artifact.get(key) != value for key, value in expected_equal.items())
        or "transcript" not in artifact
        or capture.get("transcript_entries") != transcript_entries
        or Path(oracle_pointer).expanduser().resolve() != episode_path
    ):
        return ["artifact_evidence_invalid"], artifact if isinstance(artifact, Mapping) else None
    lifecycle = _started_disposition_path(
        ledger_path,
        row_id=str(row.get("row_id") or ""),
        protocol_id=str(protocol.get("protocol_id") or ""),
        contract_sha256=str(protocol.get("contract_sha256") or ""),
    )
    if lifecycle.get("valid") is not True:
        return ["lifecycle_evidence_invalid"], artifact
    try:
        markers = contamination_markers(contract)
    except gate1p_stagea.ProtocolError:
        return ["contamination_policy_invalid"], artifact
    serialized = json.dumps(artifact, sort_keys=True, ensure_ascii=True).casefold()
    if any(marker in serialized for marker in markers):
        return ["goad_contamination"], artifact
    return [], artifact


def admission_disposition(
    contract: Mapping[str, Any],
    *,
    contract_sha256: str,
    row: object,
    history_root: Path,
) -> dict[str, Any]:
    """Recompute one row from contract, raw observer facts, and the retained episode."""
    if not isinstance(row, Mapping):
        return {"countable": False, "reason": "result row is not a mapping", "failures": ["row_schema"]}
    failures: list[str] = []
    row_id = str(row.get("row_id") or "")
    try:
        row_index, policy_mode = matrix_row(contract, row_id)
    except gate1p_stagea.ProtocolError:
        return {"countable": False, "reason": "generic development row admission failed", "failures": ["row_id_mismatch"]}
    source = contract.get("source_identity") or {}
    binding = contract.get("range_binding") or {}
    execution = contract.get("execution_identity") or {}
    signal = contract.get("prospective_signal") or {}
    try:
        markers = contamination_markers(contract)
    except gate1p_stagea.ProtocolError:
        markers = ()
        failures.append("contamination_policy_invalid")
    protocol = row.get("protocol")
    expected_protocol = {
        "protocol_id": contract.get("contract_id"), "pair_id": contract.get("contract_id"),
        "arm": row_id, "contract_sha256": contract_sha256, "measurement_kind": "row",
        "row_id": row_id, "row_index": row_index, "scenario": binding.get("scenario"),
        "side": execution.get("side"), "policy_mode": policy_mode,
        "model_provider": execution.get("provider"), "model_id": execution.get("model"),
        "solve_timeout_seconds": execution.get("solve_timeout_seconds"),
        "completed_call_token_ceiling": execution.get("completed_call_token_ceiling_per_row"),
        "model_context_limit": execution.get("model_context_limit"),
        "max_output_tokens": execution.get("max_output_tokens"),
        "source_version": source.get("tree") or source.get("commit"), "source_tree": source.get("tree"),
        "source_branch": source.get("branch"), "profile_catalog_sha256": source.get("profile_catalog_sha256"),
        "topology_identity": source.get("topology_identity"), "qualification_receipt": source.get("qualification_receipt"),
        "range_profile": binding.get("profile"), "ludus_mcp_server": binding.get("ludus_mcp_server"),
        "ludus_range_id": binding.get("ludus_range_id"), "snapshot": binding.get("snapshot"),
        "foothold_host": binding.get("foothold_host"), "foothold_callback_host": binding.get("foothold_callback_host"),
        "foothold_ip": binding.get("foothold_ip"), "foothold_principal": binding.get("foothold_principal"),
        "foothold_callback_user": binding.get("foothold_callback_user"),
        "expected_frontier_hash": signal.get("decisive_frontier_hash"),
        "expected_candidate_count": signal.get("expected_candidate_count"),
    }
    if not isinstance(protocol, Mapping) or any(protocol.get(key) != value for key, value in expected_protocol.items()):
        failures.append("row_identity_mismatch")
    readiness = protocol.get("reset_readiness") if isinstance(protocol, Mapping) else None
    readiness_expected = {
        "ready": True, "range_profile": binding.get("profile"),
        "topology_identity": source.get("topology_identity"),
        "profile_catalog_sha256": source.get("profile_catalog_sha256"),
        "qualification_receipt": source.get("qualification_receipt"),
        "snapshot": binding.get("snapshot"), "range_id": binding.get("ludus_range_id"),
        "ludus_mcp_server": binding.get("ludus_mcp_server"),
        "foothold_host": binding.get("foothold_host"), "foothold_ip": binding.get("foothold_ip"),
        "foothold_principal": binding.get("foothold_principal"),
        "callback_host": binding.get("foothold_callback_host"),
        "callback_user": binding.get("foothold_callback_user"),
        "bloodhound_domain_count": 0, "clock_gate": "passed",
        "runtime_databases_archived": True, "mythic_reset": "passed",
        "unique_initial_callback": True, "pre_arm_task_count": 0,
    }
    if not isinstance(readiness, Mapping) or any(
        readiness.get(key) != value for key, value in readiness_expected.items()
    ):
        failures.append("reset_readiness_invalid")
    observer = row.get("observer")
    facts = observer_facts_disposition(contract, row_id, observer)
    if facts.get("countable") is not True or facts.get("task_window_observed") is not True:
        failures.append("task_window_unavailable")
    environment_actions = (((row.get("countability") or {}).get("cost") or {}).get("environment_actions") or {})
    if (
        not facts.get("tasks")
        or environment_actions.get("countable") is not True
        or environment_actions.get("value") != len(facts.get("tasks") or [])
    ):
        failures.append("task_evidence_invalid")
    usage = response_usage_disposition(contract, row_id, row.get("model_response_records"), model_calls=row.get("model_calls"))
    if usage.get("countable") is not True or row.get("model_response_records") != usage.get("canonical_records"):
        failures.append("token_evidence_invalid")
    elif usage.get("observed_route") is not None:
        expected_backend = f"{execution.get('provider')}:{execution.get('model')}"
        if str(usage["observed_route"][0]).casefold() != expected_backend.casefold():
            failures.append("response_route_invalid")
    outcome = outcome_disposition(contract, row_id, observer, total_tokens=usage.get("total_tokens")) if usage.get("countable") is True else {"countable": False}
    projected = ((row.get("countability") or {}).get("outcome") or {})
    expected_projection = {
        "countable": True,
        "reason": "outcome evidence available",
        "stop_countable": outcome.get("stop_countable"),
        "objective_success": outcome.get("objective_success"),
        "clean_stop": outcome.get("clean_stop"),
        "objective_recognized": outcome.get("objective_recognized"),
        "objective_clean_stop": outcome.get("objective_clean_stop"),
    }
    if outcome.get("countable") is not True or projected != expected_projection:
        failures.append("outcome_projection_invalid")
    semantic_projection = {
        "request_completed": outcome.get("clean_stop"),
        "objective_recognized": outcome.get("objective_recognized"),
        "objective_proven": outcome.get("objective_success"),
        "clean_stop": outcome.get("clean_stop"),
        "objective_clean_stop": outcome.get("objective_clean_stop"),
    }
    if any(row.get(key) is not value or value is not True for key, value in semantic_projection.items()):
        failures.append("terminal_evidence_invalid")
    if row.get("token_usage_complete") is not True:
        failures.append("token_evidence_invalid")
    runtime_summary = gate1p_stagea.runtime_summary_disposition(row.get("runtime_summary"))
    if (
        runtime_summary.get("countable") is not True
        or row.get("runtime_summary") != runtime_summary.get("canonical_summary")
    ):
        failures.append("runtime_summary_invalid")
    card = row.get("card")
    if (
        not isinstance(card, Mapping)
        or card.get("model_calls") != row.get("model_calls")
        or card.get("total_tokens") != usage.get("total_tokens")
        or any(card.get(field) is not semantic_projection.get(field) for field in (
            "objective_proven", "clean_stop", "objective_recognized", "objective_clean_stop",
        ))
    ):
        failures.append("scorecard_projection_invalid")
    if (
        row.get("record_type") != "result" or row.get("measurement_kind") != "row"
        or row.get("side") != "harness" or row.get("scenario") != binding.get("scenario")
        or row.get("configured_policy_mode") != policy_mode or row.get("policy_mode") != policy_mode
        or row.get("policy_identity_valid") is not True or row.get("policy_switches") != []
        or float(row.get("semantic_policy_coverage", 0.0) or 0.0) != 1.0
        or row.get("backend_provenance_complete") is not True
    ):
        failures.append("runtime_identity_invalid")
    proof = effect_registry.admit_record(Milestone.OBJECTIVE, row.get("objective_proof"), expected_target=str(binding.get("objective_domain") or ""))
    if proof.disposition is not effect_registry.ProofDisposition.PROVEN:
        failures.append("independent_proof_invalid")
    if not isinstance(protocol, Mapping) or not _authority_disposition(
        contract, facts.get("tasks") if facts.get("countable") is True else None,
        initial_callback_id=protocol.get("initial_callback_display_id") if isinstance(protocol, Mapping) else None,
        operation_id=protocol.get("mythic_operation_id") if isinstance(protocol, Mapping) else None,
    ):
        failures.append("task_authority_invalid")
    lifecycle_spec = gate1p_stagea.PairAdmissionSpec(
        protocol_id=str(contract.get("contract_id") or ""),
        pair_id=str(contract.get("contract_id") or ""),
        scenario=str(binding.get("scenario") or ""),
    )
    lifecycle = gate1p_stagea.coordinator_lifecycle_disposition(
        result=row,
        admission=lifecycle_spec,
    )
    if lifecycle.get("countable") is not True:
        failures.append("coordinator_lifecycle_invalid")
    choices = signal.get("expected_choice") or {}
    expected_choice = choices.get(policy_mode) if isinstance(choices, Mapping) else None
    vectors = signal.get("expected_vectors") or {}
    costs = vectors.get(policy_mode) if isinstance(vectors, Mapping) else None
    order = (contract.get("matrix") or {}).get("order") or []
    occurrence = sum(1 for item in order[:row_index] if item == policy_mode)
    expected_cost = costs[occurrence - 1] if isinstance(costs, list) and occurrence <= len(costs) else None
    decisions = row.get("decisions")
    matching = [decision for decision in decisions or [] if isinstance(decision, Mapping) and decision.get("candidate_hash") == signal.get("decisive_frontier_hash")] if isinstance(decisions, list) else []
    if (
        len(matching) != 1
        or matching[0].get("candidate_count") != signal.get("expected_candidate_count")
        or matching[0].get("selected_capability") != expected_choice
        or type(expected_cost) is not int
        or row.get("semantic_transaction_count") != expected_cost
    ):
        failures.append("expected_choice_mismatch")
    artifact_failures, _artifact = _artifact_failures(contract, row, history_root=history_root)
    failures.extend(artifact_failures)
    if any(marker in json.dumps(row, sort_keys=True, ensure_ascii=True).casefold() for marker in markers):
        failures.append("goad_contamination")
    failures = list(dict.fromkeys(failures))
    return {
        "countable": not failures,
        "reason": "generic development row satisfies the shared admission kernel" if not failures else "generic development row admission failed",
        "failures": failures,
    }
