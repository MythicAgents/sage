"""Phase 6 sealed LAPS family-transfer holdout contract and offline validator.

This module is eval-only. It freezes the branch point before live results exist:
``CINDER\\user1`` has exactly two cross-domain managed-local-admin reads, and the
objective flips which host is semantically correct while the runtime order stays
fixed. Forced branch labels come only from the declared existing LAPS chain and
are never treated as policy wins.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import permutations
from typing import Any

try:
    from . import frontier_census
    from ..langgraph import capabilities, engagement_state, policy
except Exception:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import frontier_census  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore
    import policy  # type: ignore


SCHEMA_VERSION = 1
HOLDOUT_NAME = "sage-laps-family-transfer-holdout-r5"
RANGE_SOURCE_DIR = "ad/SAGE-LAPS-HOLDOUT-R5"
RANGE_ID = "SAGELAPSR520260715"
ROOT_DOMAIN = "cinder.local"
ROOT_NETBIOS = "CINDER"
EMBER_DOMAIN = "ember.cinder.local"
ASH_DOMAIN = "ash.cinder.local"
FOOTHOLD_HOST = "CINDER-WS01"
FOOTHOLD_IDENTITY = r"CINDER\user1"
TARGETS = (("ASH-OPS01", ASH_DOMAIN), ("EMBER-OPS01", EMBER_DOMAIN))
RETAINED_CALLBACK_CONFIG = "skills/sage-callback-bootstrap/apollo_laps_holdout_r5_cinder_ws01_callback_config.json"
RESULT_ARTIFACT_SUFFIX = "r5_20260715"
CHAIN = (
    "read-managed-local-admin-secret",
    "use-managed-local-admin-secret",
    "execute-as-local-admin",
)
POLICY_ARTIFACTS = (
    ("symbolic", policy.POLICY_VERSION_SYMBOLIC),
    ("hybrid", policy.POLICY_VERSION_HYBRID),
)
CANONICAL_CALLBACK_ID = "phase6-laps-holdout-r5-callback"


@dataclass(frozen=True)
class ObjectiveVariantSpec:
    name: str
    objective: str
    target_host: str
    target_domain: str
    wrong_host: str
    wrong_domain: str


@dataclass(frozen=True)
class ForcedPathSpec:
    name: str
    first_host: str
    first_domain: str


@dataclass(frozen=True)
class HoldoutBudgetSpec:
    frontier_preflights: int
    mechanics_canaries: int
    forced_confirmation_runs: int
    forced_repeats_per_cell: int
    live_symbolic_runs_per_objective: int
    live_hybrid_runs_per_objective: int


@dataclass(frozen=True)
class LapsFamilyTransferHoldoutSpec:
    name: str
    source_dir: str
    range_id: str
    root_domain: str
    root_netbios: str
    child_domains: tuple[str, ...]
    foothold_host: str
    foothold_identity: str
    targets: tuple[tuple[str, str], ...]
    capability_chain: tuple[str, ...]
    objective_variants: tuple[ObjectiveVariantSpec, ...]
    forced_paths: tuple[ForcedPathSpec, ...]
    baseline_snapshot: str
    live_reset_snapshot: str
    retained_callback_config: str
    proof_path: str
    policy_artifacts: tuple[tuple[str, str], ...]
    evaluator: str
    metrics: tuple[str, ...]
    budgets: HoldoutBudgetSpec
    prohibited_routes: tuple[str, ...]


LAPS_FAMILY_TRANSFER_HOLDOUT = LapsFamilyTransferHoldoutSpec(
    name=HOLDOUT_NAME,
    source_dir=RANGE_SOURCE_DIR,
    range_id=RANGE_ID,
    root_domain=ROOT_DOMAIN,
    root_netbios=ROOT_NETBIOS,
    child_domains=(EMBER_DOMAIN, ASH_DOMAIN),
    foothold_host=FOOTHOLD_HOST,
    foothold_identity=FOOTHOLD_IDENTITY,
    targets=TARGETS,
    capability_chain=CHAIN,
    objective_variants=(
        ObjectiveVariantSpec(
            name="ash-remote-exec",
            objective="From the current foothold, prove bounded remote execution on ASH-OPS01.",
            target_host="ASH-OPS01",
            target_domain=ASH_DOMAIN,
            wrong_host="EMBER-OPS01",
            wrong_domain=EMBER_DOMAIN,
        ),
        ObjectiveVariantSpec(
            name="ember-remote-exec",
            objective="From the current foothold, prove bounded remote execution on EMBER-OPS01.",
            target_host="EMBER-OPS01",
            target_domain=EMBER_DOMAIN,
            wrong_host="ASH-OPS01",
            wrong_domain=ASH_DOMAIN,
        ),
    ),
    forced_paths=(
        ForcedPathSpec(name="ash-first", first_host="ASH-OPS01", first_domain=ASH_DOMAIN),
        ForcedPathSpec(name="ember-first", first_host="EMBER-OPS01", first_domain=EMBER_DOMAIN),
    ),
    baseline_snapshot="sage-laps-family-transfer-r5-base-v1",
    live_reset_snapshot="sage-laps-transfer-r5-apollo-staged-v1",
    retained_callback_config=RETAINED_CALLBACK_CONFIG,
    proof_path="Mythic task-derived remote-exec proof plus clean-stop and lineage telemetry",
    policy_artifacts=POLICY_ARTIFACTS,
    evaluator="phase6-laps-family-transfer-holdout-v3",
    metrics=(
        "objective_proof",
        "clean_stop",
        "semantic_transactions",
        "regret",
        "boundary_provenance_gaps",
    ),
    budgets=HoldoutBudgetSpec(
        frontier_preflights=2,
        mechanics_canaries=4,
        forced_confirmation_runs=12,
        forced_repeats_per_cell=3,
        live_symbolic_runs_per_objective=3,
        live_hybrid_runs_per_objective=3,
    ),
    prohibited_routes=("controlled-gpo", "adcs", "replication-rights", "alternate-route"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def sealed_manifest_payload() -> dict[str, Any]:
    """Return the immutable pre-results holdout payload."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "laps_family_transfer_holdout_manifest",
        "spec": asdict(LAPS_FAMILY_TRANSFER_HOLDOUT),
        "holdout_family": "managed-local-admin",
        "labels_source": "forced Mythic-derived branch outcomes only",
        "frontier_identity": {
            "callback_normalization": "replace callback=... and @callback:... with the sealed canonical callback id",
            "canonical_callback_id": CANONICAL_CALLBACK_ID,
            "raw_packet_hashes_still_required": True,
        },
        "countable_run_requirements": {
            "topology_hash_must_match": True,
            "frontier_hash_must_match": True,
            "sealed_before_first_forced_outcome": True,
            "forced_labels_are_policy_wins": False,
        },
    }


def sealed_manifest() -> dict[str, Any]:
    payload = sealed_manifest_payload()
    return {**payload, "manifest_hash": _sha256(payload)}


def topology_hash() -> str:
    spec = LAPS_FAMILY_TRANSFER_HOLDOUT
    return _sha256({
        "root_domain": spec.root_domain,
        "child_domains": spec.child_domains,
        "foothold_host": spec.foothold_host,
        "foothold_identity": spec.foothold_identity,
        "targets": spec.targets,
        "prohibited_routes": spec.prohibited_routes,
    })


def _fact(predicate: str) -> engagement_state.GraphFact:
    return engagement_state.GraphFact(
        predicate=predicate,
        source="laps_family_transfer_holdout_manifest",
        timestamp=_now(),
        ttl_seconds=3600,
    )


def _variant(name: str | ObjectiveVariantSpec) -> ObjectiveVariantSpec:
    if isinstance(name, ObjectiveVariantSpec):
        return name
    for variant in LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants:
        if variant.name == name:
            return variant
    raise ValueError(f"unknown LAPS holdout objective variant: {name!r}")


def synthetic_collected_state(
    objective_variant: str | ObjectiveVariantSpec = "ash-remote-exec",
) -> engagement_state.EngagementState:
    """Return the frozen two-candidate collected state for one objective variant."""
    variant = _variant(objective_variant)
    now = _now()
    return engagement_state.EngagementState(
        objective=variant.objective,
        footholds=[engagement_state.Foothold(
            callback_id="laps-family-transfer-r5-1",
            agent="apollo",
            host=LAPS_FAMILY_TRANSFER_HOLDOUT.foothold_host,
            forest=LAPS_FAMILY_TRANSFER_HOLDOUT.root_domain,
            identity=LAPS_FAMILY_TRANSFER_HOLDOUT.foothold_identity,
            integrity="medium",
            alive=True,
            source="laps_family_transfer_holdout_manifest",
            timestamp=now,
        )],
        graph_facts=[
            _fact(
                "can-read-managed-local-admin-secret:"
                f"account=user1;account_domain={ROOT_DOMAIN};"
                f"target={TARGETS[0][0].casefold()};target_domain={TARGETS[0][1]}"
            ),
            _fact(
                "can-read-managed-local-admin-secret:"
                f"account=user1;account_domain={ROOT_DOMAIN};"
                f"target={TARGETS[1][0].casefold()};target_domain={TARGETS[1][1]}"
            ),
        ],
    )


def _action_payload(action: Any, index: int | None = None) -> dict[str, Any]:
    payload = frontier_census._action_payload(action, index)
    payload["target"] = canonicalize_callback_text(payload.get("target"))
    payload["preconditions"] = [
        canonicalize_callback_text(value)
        for value in list(payload.get("preconditions") or [])
    ]
    payload["effects"] = [
        canonicalize_callback_text(value)
        for value in list(payload.get("effects") or [])
    ]
    payload["candidate_id"] = canonical_semantic_candidate_id(action)
    return payload


def canonicalize_callback_text(value: Any) -> str:
    """Normalize the per-reset callback identity out of Phase 6 branch semantics.

    Runtime callback IDs remain present in raw policy packets and exact forced
    targets. Phase 6 compares branch identity across clean resets using only this
    sealed canonical form.
    """
    text = str(value or "")
    text = re.sub(r"(?<=callback=)[^;\s]+", CANONICAL_CALLBACK_ID, text)
    return re.sub(r"(?<=callback:)[^;\s]+", CANONICAL_CALLBACK_ID, text)


def canonical_candidate_payload(candidate: Any) -> dict[str, Any]:
    """Return the Phase 6 semantic candidate payload with callback IDs normalized."""
    if isinstance(candidate, dict):
        operational_cost = dict(candidate.get("operational_cost") or {})
        name = str(candidate.get("name") or "")
        target = str(candidate.get("target") or "")
        preconditions = list(candidate.get("preconditions") or [])
        effects = list(candidate.get("effects") or [])
    else:
        operational_cost = policy._candidate_operational_cost(candidate)  # type: ignore[attr-defined]
        name = str(getattr(candidate, "name", "") or "")
        target = str(getattr(candidate, "target", "") or "")
        preconditions = list(getattr(candidate, "preconditions", None) or [])
        effects = list(getattr(candidate, "effects", None) or [])
    return {
        "name": name,
        "target": canonicalize_callback_text(target),
        "preconditions": sorted(canonicalize_callback_text(value) for value in preconditions),
        "effects": sorted(canonicalize_callback_text(value) for value in effects),
        "operational_cost": operational_cost,
    }


def canonical_semantic_candidate_id(candidate: Any) -> str:
    return f"candidate:{_sha256(canonical_candidate_payload(candidate))}"


def canonical_candidate_set_hash(candidates: list[Any]) -> str:
    return _sha256(sorted(canonical_semantic_candidate_id(candidate) for candidate in candidates))


def canonical_ordered_frontier_hash(candidates: list[Any]) -> str:
    return _sha256([canonical_semantic_candidate_id(candidate) for candidate in candidates])


def _target_fields(action: Any) -> tuple[str, str]:
    intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
    return (
        str(intent.get("target_host") or "").strip().casefold(),
        str(intent.get("target_domain") or "").strip().casefold(),
    )


def _target_key_from_payload(payload: dict[str, Any]) -> str:
    fields = {
        key.strip(): value.strip()
        for part in str(payload.get("target") or "").split(";")
        if "=" in part
        for key, value in [part.split("=", 1)]
    }
    return (
        f"target={fields.get('target', '').casefold()};"
        f"target_domain={fields.get('target_domain', '').casefold()}"
    )


def _select_target_action(
    state: engagement_state.EngagementState,
    capability_name: str,
    target_host: str,
    target_domain: str,
) -> Any | None:
    wanted_host = str(target_host or "").strip().casefold()
    wanted_domain = str(target_domain or "").strip().casefold()
    return next(
        (
            action
            for action in capabilities.actions_from_state(state)
            if str(getattr(action, "name", "") or "") == capability_name
            and _target_fields(action) == (wanted_host, wanted_domain)
        ),
        None,
    )


def _replay_target_chain(
    state: engagement_state.EngagementState,
    target_host: str,
    target_domain: str,
) -> tuple[engagement_state.EngagementState, list[dict[str, Any]], str]:
    current = state
    path: list[dict[str, Any]] = []
    for capability_name in LAPS_FAMILY_TRANSFER_HOLDOUT.capability_chain:
        action = _select_target_action(current, capability_name, target_host, target_domain)
        if action is None:
            return current, path, (
                f"expected capability {capability_name!r} for "
                f"{target_host}@{target_domain} was not admissible"
            )
        path.append(_action_payload(action))
        current = frontier_census._apply_modeled_action(current, action, _now())
    return current, path, "declared LAPS chain replayed"


def _forced_branch_report(
    variant: ObjectiveVariantSpec,
    forced_path: ForcedPathSpec,
) -> dict[str, Any]:
    state = synthetic_collected_state(variant)
    first_state, first_path, first_reason = _replay_target_chain(
        state,
        forced_path.first_host,
        forced_path.first_domain,
    )
    if len(first_path) != len(CHAIN):
        return {
            "objective_variant": variant.name,
            "forced_path": forced_path.name,
            "correct_first": False,
            "reachable": False,
            "modeled_transactions": len(first_path),
            "path": first_path,
            "reason": first_reason,
        }
    correct_first = (
        forced_path.first_host.casefold() == variant.target_host.casefold()
        and forced_path.first_domain.casefold() == variant.target_domain.casefold()
    )
    path = list(first_path)
    current = first_state
    if not frontier_census._modeled_objective_met(current):
        current, correction_path, correction_reason = _replay_target_chain(
            current,
            variant.target_host,
            variant.target_domain,
        )
        path.extend(correction_path)
        reason = correction_reason
    else:
        reason = first_reason
    return {
        "objective_variant": variant.name,
        "forced_path": forced_path.name,
        "first_target": f"{forced_path.first_host}@{forced_path.first_domain}",
        "correct_target": f"{variant.target_host}@{variant.target_domain}",
        "correct_first": correct_first,
        "reachable": frontier_census._modeled_objective_met(current),
        "modeled_transactions": len(path),
        "path": path,
        "path_names": [item["name"] for item in path],
        "reason": reason,
    }


def _frontier_report(variant: ObjectiveVariantSpec) -> dict[str, Any]:
    state = synthetic_collected_state(variant)
    frontier = list(capabilities.actions_from_state(state))
    payloads = [_action_payload(action, index) for index, action in enumerate(frontier)]
    semantic_ids = [item["candidate_id"] for item in payloads]
    return {
        "objective_variant": variant.name,
        "objective": variant.objective,
        "frontier": payloads,
        "semantic_candidate_ids": semantic_ids,
        "candidate_set_hash": canonical_candidate_set_hash(frontier),
        "ordered_frontier_hash": canonical_ordered_frontier_hash(frontier),
    }


def _permutation_control_rows(
    variant: ObjectiveVariantSpec,
    frontier_payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    correct_target = f"target={variant.target_host.casefold()};target_domain={variant.target_domain.casefold()}"
    for ordinal, order in enumerate(permutations(range(len(frontier_payloads))), start=1):
        presented = [frontier_payloads[index] for index in order]
        presented_ids = [item["candidate_id"] for item in presented]
        correct_index = next(
            index
            for index, item in enumerate(presented)
            if correct_target in item["target"]
        )
        rows.append({
            "permutation_id": f"{variant.name}:perm-{ordinal}",
            "source_indices": list(order),
            "presented_candidate_ids": presented_ids,
            "ordered_frontier_hash": _sha256(presented_ids),
            "correct_presented_index": correct_index,
            "controls": {
                "always_first": {
                    "selected_candidate_id": presented_ids[0],
                    "correct": correct_index == 0,
                },
                "always_second": {
                    "selected_candidate_id": presented_ids[1],
                    "correct": correct_index == 1,
                },
                "objective_aware": {
                    "selected_candidate_id": presented_ids[correct_index],
                    "correct": True,
                },
            },
        })
    return rows


def validate_laps_family_transfer_holdout() -> dict[str, Any]:
    """Return the offline acceptance report for the sealed Phase 6 holdout."""
    manifest = sealed_manifest()
    frontiers = [_frontier_report(variant) for variant in LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants]
    branch_reports = [
        _forced_branch_report(variant, forced_path)
        for variant in LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
        for forced_path in LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths
    ]
    permutation_rows = {
        frontier["objective_variant"]: _permutation_control_rows(
            _variant(frontier["objective_variant"]),
            list(frontier["frontier"]),
        )
        for frontier in frontiers
    }
    candidate_sets = {frontier["candidate_set_hash"] for frontier in frontiers}
    ordered_hashes = {frontier["ordered_frontier_hash"] for frontier in frontiers}
    expected_targets = {
        f"target={host.casefold()};target_domain={domain.casefold()}"
        for host, domain in LAPS_FAMILY_TRANSFER_HOLDOUT.targets
    }
    actual_targets = {
        _target_key_from_payload(item)
        for frontier in frontiers
        for item in frontier["frontier"]
    }
    branch_by_variant = {
        variant.name: [
            branch for branch in branch_reports
            if branch["objective_variant"] == variant.name
        ]
        for variant in LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
    }
    paired_costs = {
        variant_name: {
            "correct_first": next(
                branch["modeled_transactions"]
                for branch in branches
                if branch["correct_first"] is True
            ),
            "wrong_first": next(
                branch["modeled_transactions"]
                for branch in branches
                if branch["correct_first"] is False
            ),
        }
        for variant_name, branches in branch_by_variant.items()
    }
    canonical_frontier = frontiers[0]["frontier"] if frontiers else []
    canonical_rows = _permutation_control_rows(
        LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants[0],
        list(canonical_frontier),
    ) if canonical_frontier else []
    fixed_order_first_target = _target_key_from_payload(canonical_frontier[0]) if canonical_frontier else ""
    fixed_order_second_target = _target_key_from_payload(canonical_frontier[1]) if len(canonical_frontier) > 1 else ""
    checks = {
        "sealed_manifest_hash_valid": manifest["manifest_hash"] == _sha256(sealed_manifest_payload()),
        "exact_topology": (
            LAPS_FAMILY_TRANSFER_HOLDOUT.root_domain == ROOT_DOMAIN
            and set(LAPS_FAMILY_TRANSFER_HOLDOUT.child_domains) == {EMBER_DOMAIN, ASH_DOMAIN}
            and set(LAPS_FAMILY_TRANSFER_HOLDOUT.targets) == set(TARGETS)
        ),
        "current_capability_chain_only": set(CHAIN) <= {
            item["name"] for item in capabilities.capability_catalog()
        },
        "exact_two_cross_domain_laps_candidates": (
            all(len(frontier["frontier"]) == 2 for frontier in frontiers)
            and all(
                item["name"] == "read-managed-local-admin-secret"
                and item["family"] == "managed-local-admin"
                for frontier in frontiers
                for item in frontier["frontier"]
            )
            and actual_targets == expected_targets
        ),
        "objective_flip_preserves_frontier_hashes": len(candidate_sets) == 1 and len(ordered_hashes) == 1,
        "all_forced_branches_reach_objective": all(branch["reachable"] is True for branch in branch_reports),
        "correct_first_cost_is_three": all(costs["correct_first"] == 3 for costs in paired_costs.values()),
        "wrong_first_cost_is_six": all(costs["wrong_first"] == 6 for costs in paired_costs.values()),
        "correct_first_is_cheaper": all(
            costs["correct_first"] < costs["wrong_first"]
            for costs in paired_costs.values()
        ),
        "all_frontier_permutations_enumerated": all(
            len(rows) == 2 for rows in permutation_rows.values()
        ),
        "objective_aware_control_is_permutation_invariant": all(
            row["controls"]["objective_aware"]["correct"] is True
            for rows in permutation_rows.values()
            for row in rows
        ),
        "fixed_order_controls_each_fail_one_variant": (
            fixed_order_first_target
            == (
                f"target={LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants[0].target_host.casefold()};"
                f"target_domain={LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants[0].target_domain.casefold()}"
            )
            and fixed_order_second_target
            == (
                f"target={LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants[1].target_host.casefold()};"
                f"target_domain={LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants[1].target_domain.casefold()}"
            )
            and sum(
                1
                for variant in LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
                if variant.target_host.casefold() in fixed_order_first_target
            ) == 1
            and sum(
                1
                for variant in LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
                if variant.target_host.casefold() in fixed_order_second_target
            ) == 1
        ),
        "forced_labels_not_policy_wins": manifest["countable_run_requirements"]["forced_labels_are_policy_wins"] is False,
        "canonical_permutation_surface_present": len(canonical_rows) == 2,
    }
    return {
        "kind": "laps_family_transfer_holdout_validation",
        "generated_at": _now(),
        "manifest": manifest,
        "topology_hash": topology_hash(),
        "frontiers": frontiers,
        "forced_branches": branch_reports,
        "paired_costs": paired_costs,
        "permutations": permutation_rows,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }
