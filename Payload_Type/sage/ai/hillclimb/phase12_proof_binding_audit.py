"""Phase 12 retained proof-binding audit and candidate effect-path inventory.

This module is intentionally offline and append-only. It inspects retained Phase 6
R5 artifacts without rewriting them, labels each retained terminal proof as
``valid``, ``invalid``, or ``unverifiable``, and emits a candidate-only inventory
for later Phase 16 sealing. It does not reconstruct missing raw Mythic evidence,
authorize a live run, or create a prospective authorization manifest for
historical rows.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


AUDIT_KIND = "phase12_retained_proof_binding_audit"
AUDIT_SCHEMA_VERSION = 1
INVENTORY_KIND = "phase12_candidate_evaluated_effect_path_inventory"
INVENTORY_SCHEMA_VERSION = 1

VALID = "valid"
INVALID = "invalid"
UNVERIFIABLE = "unverifiable"
AUDIT_STATUSES = frozenset({VALID, INVALID, UNVERIFIABLE})

HISTORICAL_AUTH_PROVENANCE_STATUSES = frozenset({"retained", "out_of_band", "unavailable", "unverifiable"})
DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[2] / ".hillclimb" / "results"
DEFAULT_SOURCE_ARTIFACTS = (
    DEFAULT_RESULTS_ROOT / "laps_family_transfer_mechanics_canaries_pinned_r5_20260715.jsonl",
    DEFAULT_RESULTS_ROOT / "laps_family_transfer_forced_confirmations_pinned_r5_20260715.jsonl",
    DEFAULT_RESULTS_ROOT / "laps_family_transfer_policy_matrix_pinned_r5_20260715.jsonl",
)
DEFAULT_AUDIT_OUTPUT = (
    Path(__file__).resolve().parents[4]
    / "Plans"
    / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE12_PROOF_BINDING_AUDIT_2026-07-16.json"
)
DEFAULT_INVENTORY_OUTPUT = (
    Path(__file__).resolve().parents[4]
    / "Plans"
    / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE12_EVALUATED_EFFECT_PATH_INVENTORY_2026-07-16.json"
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_SUCCESS = frozenset({"complete", "completed", "success", "succeeded"})
_CLEANUP_COMMANDS = frozenset({"rev2self"})
_RAW_RESULT_FIELDS = (
    "raw_output_sha256",
    "raw_result_sha256",
    "result_sha256",
    "output_sha256",
    "task_output_sha256",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).casefold()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _portable_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.name


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _is_sha256(value: Any) -> bool:
    return bool(_HEX64_RE.fullmatch(_lower(value).removeprefix("sha256:")))


def _child_tasks(transaction: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in list(transaction.get("child_tasks") or []) if isinstance(item, Mapping)]


def _proof_lineages(transaction: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in list(transaction.get("proof_lineage") or []) if isinstance(item, Mapping)]


def _task_by_id(transaction: Mapping[str, Any], task_id: Any) -> dict[str, Any] | None:
    wanted = _text(task_id)
    if not wanted:
        return None
    for child in _child_tasks(transaction):
        if _text(child.get("task_id")) == wanted:
            return child
    return None


def _retained_raw_result_sha256(child: Mapping[str, Any] | None, lineage: Mapping[str, Any]) -> str:
    for source in (child or {}, lineage):
        for key in _RAW_RESULT_FIELDS:
            value = _lower(source.get(key)).removeprefix("sha256:")
            if _is_sha256(value):
                return value
    return ""


def _expected_evidence_command_candidates(transaction: Mapping[str, Any], proof_task_id: Any) -> list[str]:
    """Return retained pre-cleanup commands that could have produced the observed proof.

    This is descriptive only. It never reconstructs the original verifier input or
    upgrades an unverifiable row into a valid one.
    """
    tasks = _child_tasks(transaction)
    proof_index = next(
        (index for index, child in enumerate(tasks) if _text(child.get("task_id")) == _text(proof_task_id)),
        -1,
    )
    prior = tasks[:proof_index] if proof_index >= 0 else tasks
    return [
        _text(child.get("command"))
        for child in prior
        if _text(child.get("command")) and _lower(child.get("command")) not in _CLEANUP_COMMANDS
    ][-2:]


def classify_terminal_proof(
    transaction: Mapping[str, Any],
    lineage: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Classify one retained terminal proof without inventing missing evidence."""
    transaction_id = _text(transaction.get("transaction_id"))
    callback_id = _text(transaction.get("callback_id"))
    proof_task_id = _text(lineage.get("task_id"))
    proof_transaction_id = _text(lineage.get("transaction_id"))
    child = _task_by_id(transaction, proof_task_id)
    tasks = _child_tasks(transaction)
    last_child = tasks[-1] if tasks else {}
    proof_command = _text((child or {}).get("command"))
    last_command = _text(last_child.get("command"))
    evidence = {
        "proof_task_id": proof_task_id,
        "proof_task_command": proof_command,
        "last_child_task_id": _text(last_child.get("task_id")),
        "last_child_command": last_command,
        "proof_transaction_id": proof_transaction_id,
        "transaction_id": transaction_id,
        "callback_id": callback_id,
        "retained_raw_result_sha256": _retained_raw_result_sha256(child, lineage),
        "retained_verifier_input_sha256": _lower(lineage.get("verifier_input_sha256")).removeprefix("sha256:"),
        "retained_verifier_result_sha256": _lower(lineage.get("verifier_result_sha256")).removeprefix("sha256:"),
        "expected_evidence_command_candidates": _expected_evidence_command_candidates(transaction, proof_task_id),
    }
    if not proof_task_id or child is None:
        return INVALID, "proof_task_missing_from_retained_child_tasks", evidence
    if transaction_id and proof_transaction_id and transaction_id != proof_transaction_id:
        return INVALID, "proof_transaction_mismatch", evidence
    if callback_id and _text(lineage.get("callback_id")) and callback_id != _text(lineage.get("callback_id")):
        return INVALID, "proof_callback_mismatch", evidence
    if _lower(child.get("terminal_status")) not in _TERMINAL_SUCCESS:
        return INVALID, "proof_task_not_terminal_success", evidence
    if (
        _lower(transaction.get("capability")) == "execute-as-local-admin"
        and proof_task_id == _text(last_child.get("task_id"))
        and _lower(last_command) in _CLEANUP_COMMANDS
    ):
        return INVALID, "proof_bound_to_cleanup_last_child", evidence
    if (
        not _is_sha256(evidence["retained_raw_result_sha256"])
        or not _is_sha256(evidence["retained_verifier_input_sha256"])
        or not _is_sha256(evidence["retained_verifier_result_sha256"])
    ):
        return UNVERIFIABLE, "retained_raw_task_result_or_v2_commitment_missing", evidence
    return VALID, "retained_exact_task_result_and_v2_commitments_present", evidence


def historical_authorization_provenance() -> list[dict[str, Any]]:
    """Describe historical Phase 0-10 authorization honestly, without retrofitting manifests."""
    return [
        {
            "phase": phase,
            "provenance_status": "out_of_band",
            "prospective_manifest_binding_status": "unavailable",
            "reason_code": "historical_phase_predates_phase18_manifest_action_envelope_contract",
            "retained_historical_approval_may_exist": True,
            "prospective_manifest_synthesized": False,
            "retrofit_permitted": False,
        }
        for phase in range(0, 11)
    ]


def build_phase12_proof_binding_audit(
    *,
    repo_root: Path | None = None,
    source_artifacts: Sequence[Path] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or _repo_root())
    sources = [Path(path) for path in (source_artifacts or DEFAULT_SOURCE_ARTIFACTS)]
    source_records: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    for source in sources:
        rows = _read_jsonl(source)
        source_records.append({
            "path": _portable_path(source, root),
            "sha256": _file_sha256(source),
            "row_count": len(rows),
        })
        for row_index, row in enumerate(rows, start=1):
            for transaction_index, transaction in enumerate(
                [item for item in list(row.get("transactions") or []) if isinstance(item, Mapping)],
                start=1,
            ):
                for lineage_index, lineage in enumerate(_proof_lineages(transaction), start=1):
                    status, reason_code, retained_evidence = classify_terminal_proof(transaction, lineage)
                    audit_rows.append({
                        "audit_unit_id": "sha256:" + _sha256_json({
                            "source": _portable_path(source, root),
                            "row_index": row_index,
                            "transaction_index": transaction_index,
                            "lineage_index": lineage_index,
                            "transaction_id": transaction.get("transaction_id"),
                            "proof_envelope_id": lineage.get("proof_envelope_id"),
                        }),
                        "source_artifact": _portable_path(source, root),
                        "source_row_index": row_index,
                        "planned_row_id": _text(row.get("phase6_planned_row_id")),
                        "transaction_index": transaction_index,
                        "transaction_id": _text(transaction.get("transaction_id")),
                        "callback_id": _text(transaction.get("callback_id")),
                        "capability": _text(transaction.get("capability")),
                        "selected_target": _text(transaction.get("selected_target") or transaction.get("target")),
                        "proof_lineage_index": lineage_index,
                        "proof_envelope_id": _text(lineage.get("proof_envelope_id")),
                        "verifier_id": _text(lineage.get("verifier_id")),
                        "status": status,
                        "reason_code": reason_code,
                        "retained_evidence": retained_evidence,
                        "original_row_mutated": False,
                    })
    status_counts = Counter(row["status"] for row in audit_rows)
    capability_counts = Counter(row["capability"] for row in audit_rows)
    reason_counts = Counter(row["reason_code"] for row in audit_rows)
    audit = {
        "kind": AUDIT_KIND,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "source_plan": "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_COMPLETION_PLAN_2026-07-14.md#6.6",
        "source_artifacts": source_records,
        "audit_scope": {
            "phase": 6,
            "retained_surface": "sealed_laps_family_transfer_r5",
            "unit": "retained_terminal_proof_lineage",
            "classification_vocabulary": sorted(AUDIT_STATUSES),
            "raw_evidence_stop_loss": (
                "If retained raw Mythic task/result evidence or v2 verifier commitments are absent, "
                "the row remains unverifiable; this audit never reconstructs proof from summaries."
            ),
        },
        "totals": {
            "source_artifact_count": len(source_records),
            "source_row_count": sum(item["row_count"] for item in source_records),
            "terminal_proof_count": len(audit_rows),
            "status_counts": dict(sorted(status_counts.items())),
            "capability_counts": dict(sorted(capability_counts.items())),
            "reason_counts": dict(sorted(reason_counts.items())),
        },
        "historical_authorization_provenance": historical_authorization_provenance(),
        "rows": audit_rows,
        "immutability": {
            "original_rows_rewritten": False,
            "source_hashes_recorded_before_audit": True,
            "audit_is_separate_artifact": True,
        },
    }
    audit["validation"] = validate_phase12_proof_binding_audit(audit)
    return audit


def _observed_capability_sequences(audit: Mapping[str, Any]) -> dict[str, list[list[str]]]:
    sequences: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    for row in list(audit.get("rows") or []):
        if not isinstance(row, Mapping):
            continue
        capability = _text(row.get("capability"))
        retained = row.get("retained_evidence") if isinstance(row.get("retained_evidence"), Mapping) else {}
        commands = tuple(
            command
            for command in (
                *list(retained.get("expected_evidence_command_candidates") or []),
                _text(retained.get("proof_task_command")),
            )
            if command
        )
        if capability and commands:
            sequences[capability].add(commands)
    return {
        capability: [list(sequence) for sequence in sorted(values)]
        for capability, values in sorted(sequences.items())
    }


def build_candidate_effect_path_inventory(
    audit: Mapping[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    sequences = _observed_capability_sequences(audit)
    path_templates = {
        "collect-graph": {
            "effect_class": "graph_collection_and_ingest",
            "candidate_final_adapter_boundary": "MythicTools.ingest_collection -> _runtime_bloodhound_proof_envelope",
            "candidate_proof_origin": "bloodhound_ingest",
        },
        "read-managed-local-admin-secret": {
            "effect_class": "managed_secret_read",
            "candidate_final_adapter_boundary": "MythicTools.execute_capability -> _issue_capability_callback_command",
            "candidate_proof_origin": "mythic_task",
        },
        "use-managed-local-admin-secret": {
            "effect_class": "managed_secret_use",
            "candidate_final_adapter_boundary": "MythicTools.execute_capability -> _issue_capability_callback_command",
            "candidate_proof_origin": "mythic_task",
        },
        "execute-as-local-admin": {
            "effect_class": "remote_execution",
            "candidate_final_adapter_boundary": "MythicTools.execute_capability -> _issue_capability_callback_command",
            "candidate_proof_origin": "mythic_task",
        },
    }
    paths = []
    for capability, observed_sequences in sorted(sequences.items()):
        template = path_templates.get(capability, {})
        paths.append({
            "path_id": f"phase6_r5:{capability}",
            "capability": capability,
            "effect_class": template.get("effect_class", "unknown"),
            "observed_command_sequences": observed_sequences,
            "candidate_final_adapter_boundary": template.get("candidate_final_adapter_boundary", "unresolved"),
            "candidate_proof_origin": template.get("candidate_proof_origin", "unresolved"),
            "phase12_status": "candidate_only_unsealed",
            "phase16_seal_required": True,
            "phase17_final_boundary_coverage_required": True,
            "activation_authorized": False,
        })
    inventory = {
        "kind": INVENTORY_KIND,
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "source_audit_kind": audit.get("kind"),
        "source_audit_terminal_proof_count": ((audit.get("totals") or {}).get("terminal_proof_count")),
        "inventory_scope": {
            "status": "candidate_only",
            "basis": "retained Phase 6 R5 terminal-proof transactions plus current common executor seams",
            "non_claim": (
                "This is not the Phase 16 frozen coverage manifest and does not prove that every Phase 18 "
                "external-effect path is mediated."
            ),
        },
        "activation_status": "blocked_pending_phase16_seal_and_phase17_final_boundary_coverage",
        "paths": paths,
        "required_next_checks": [
            "Phase 16 must seal the exact study surface and coverage manifest before treatment assignment.",
            "Phase 17 must prove the same authorization authority runs at each final adapter boundary.",
            "Any newly discovered reachable effect path after seal triggers the Phase 17/18 stop-loss.",
        ],
    }
    inventory["validation"] = validate_candidate_effect_path_inventory(inventory)
    return inventory


def validate_phase12_proof_binding_audit(audit: Mapping[str, Any]) -> dict[str, Any]:
    rows = [row for row in list(audit.get("rows") or []) if isinstance(row, Mapping)]
    totals = audit.get("totals") if isinstance(audit.get("totals"), Mapping) else {}
    status_counts = Counter(_text(row.get("status")) for row in rows)
    auth_rows = [
        row for row in list(audit.get("historical_authorization_provenance") or []) if isinstance(row, Mapping)
    ]
    checks = {
        "kind_matches": audit.get("kind") == AUDIT_KIND,
        "every_row_has_allowed_status": bool(rows) and all(_text(row.get("status")) in AUDIT_STATUSES for row in rows),
        "terminal_proof_total_reconciles": len(rows) == totals.get("terminal_proof_count"),
        "status_counts_reconcile": dict(sorted(status_counts.items())) == dict(totals.get("status_counts") or {}),
        "source_hashes_present": all(_is_sha256(item.get("sha256")) for item in list(audit.get("source_artifacts") or [])),
        "original_rows_untouched": all(row.get("original_row_mutated") is False for row in rows),
        "historical_phases_0_through_10_present": [row.get("phase") for row in auth_rows] == list(range(0, 11)),
        "historical_auth_uses_only_allowed_statuses": all(
            _text(row.get("provenance_status")) in HISTORICAL_AUTH_PROVENANCE_STATUSES
            and _text(row.get("prospective_manifest_binding_status")) in HISTORICAL_AUTH_PROVENANCE_STATUSES
            for row in auth_rows
        ),
        "historical_auth_not_retrofitted": all(
            row.get("prospective_manifest_synthesized") is False and row.get("retrofit_permitted") is False
            for row in auth_rows
        ),
    }
    return {
        "passes_gate": all(checks.values()),
        "checks": checks,
    }


def validate_candidate_effect_path_inventory(inventory: Mapping[str, Any]) -> dict[str, Any]:
    paths = [row for row in list(inventory.get("paths") or []) if isinstance(row, Mapping)]
    checks = {
        "kind_matches": inventory.get("kind") == INVENTORY_KIND,
        "candidate_only_status": ((inventory.get("inventory_scope") or {}).get("status") == "candidate_only"),
        "activation_blocked": inventory.get("activation_status") == "blocked_pending_phase16_seal_and_phase17_final_boundary_coverage",
        "paths_present": bool(paths),
        "no_path_is_active": all(row.get("activation_authorized") is False for row in paths),
        "every_path_requires_phase16_and_phase17": all(
            row.get("phase16_seal_required") is True
            and row.get("phase17_final_boundary_coverage_required") is True
            for row in paths
        ),
    }
    return {
        "passes_gate": all(checks.values()),
        "checks": checks,
    }


def render_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"


def write_json_with_sha256(path: Path, value: Mapping[str, Any]) -> dict[str, str]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_json(value)
    path.write_text(rendered, encoding="utf-8")
    digest = _file_sha256(path)
    sidecar = path.with_suffix(".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return {
        "path": str(path),
        "sha256": digest,
        "sidecar": str(sidecar),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase12-proof-binding-audit",
        help="emit the offline Phase 12 retained proof-binding audit and candidate effect-path inventory",
    )
    parser.add_argument("--audit-output", default=str(DEFAULT_AUDIT_OUTPUT), help="JSON audit output path")
    parser.add_argument("--inventory-output", default=str(DEFAULT_INVENTORY_OUTPUT), help="JSON inventory output path")
    parser.set_defaults(func=_cmd_phase12_proof_binding_audit)


def _cmd_phase12_proof_binding_audit(args: Any) -> int:
    audit = build_phase12_proof_binding_audit()
    inventory = build_candidate_effect_path_inventory(audit)
    outputs = {
        "audit": write_json_with_sha256(Path(args.audit_output), audit),
        "inventory": write_json_with_sha256(Path(args.inventory_output), inventory),
    }
    print(json.dumps({
        "audit_validation": audit["validation"],
        "inventory_validation": inventory["validation"],
        "totals": audit["totals"],
        "outputs": outputs,
    }, indent=2, sort_keys=True))
    return 0 if audit["validation"]["passes_gate"] and inventory["validation"]["passes_gate"] else 1
