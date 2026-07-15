from __future__ import annotations

import copy
import json

from ai.hillclimb import laps_family_transfer_canary as canary
from ai.hillclimb import laps_family_transfer_holdout as contract
from ai.hillclimb import laps_family_transfer_live_surface as live_surface
from ai.hillclimb import policy_replay_corpus as corpus
from ai.langgraph import capabilities, policy


def _surface_report() -> dict:
    facts = list(contract.synthetic_collected_state().graph_facts)
    return live_surface.validate_live_surface_evidence(
        live_surface.new_evidence_manifest([
            live_surface.make_observation(
                label="clean-reset-1",
                graph_facts=facts,
                collected_domains=["cinder.local", "ember.cinder.local", "ash.cinder.local"],
                captured_at="2026-07-14T00:00:01+00:00",
            ),
            live_surface.make_observation(
                label="clean-reset-2",
                graph_facts=facts,
                collected_domains=["cinder.local", "ember.cinder.local", "ash.cinder.local"],
                captured_at="2026-07-14T00:00:02+00:00",
            ),
        ])
    )


def _frontier_packet(variant_name: str, selection_contract: str) -> tuple[list[object], dict]:
    state = contract.synthetic_collected_state(variant_name)
    frontier = list(capabilities.actions_from_state(state))
    packet = policy._decision_packet(
        objective=state.objective,
        state=state,
        candidates=frontier,
        history=[],
        budgets={},
        selection_contract=selection_contract,
    )
    return frontier, packet


def _decision(
    *,
    decision_id: str,
    capability: str,
    target: str,
    packet: dict | None = None,
    selected_index: int = 0,
    forced: bool = False,
) -> dict:
    packet = copy.deepcopy(packet) if packet is not None else {
        "selection_contract": canary.EXPECTED_SELECTION_CONTRACT,
        "candidate_set_hash": f"sha256:{decision_id}",
        "ordered_frontier_hash": f"sha256:{decision_id}",
        "admissible_frontier": [{"name": capability, "target": target}],
    }
    return {
        "decision_id": decision_id,
        "candidate_hash": packet.get("candidate_hash"),
        "candidate_set_hash": packet["candidate_set_hash"],
        "ordered_frontier_hash": packet["ordered_frontier_hash"],
        "candidate_count": len(packet["admissible_frontier"]),
        "selected_capability": capability,
        "selected_target": target,
        "selected_index": selected_index,
        "selected_candidate_id": (
            (packet.get("semantic_candidate_ids") or ["candidate"])[selected_index]
            if len(packet.get("semantic_candidate_ids") or []) > selected_index
            else "candidate"
        ),
        "decision_packet": packet,
        "decision_packet_hash": corpus._packet_hash(packet),
        "decision_owner": "forced_intervention" if forced else "kernel_singleton",
        "forced_intervention": forced,
        "intervention_id": f"intervention-{decision_id}" if forced else "",
        "forced_policy_win_credit": False if forced else None,
    }


def _transaction(decision: dict) -> dict:
    decision_id = decision["decision_id"]
    return {
        "transaction_id": f"transaction-{decision_id}",
        "decision_id": decision_id,
        "policy_mode": canary.EXPECTED_POLICY_MODE,
        "capability": decision["selected_capability"],
        "target": decision["selected_target"],
        "child_tasks": [{"task_id": f"task-{decision_id}", "terminal_status": "completed"}],
        "verifier_ids": [f"verifier-{decision_id}"],
        "proof_envelope_ids": [f"proof-{decision_id}"],
        "proof_lineage": [{
            "proof_envelope_id": f"proof-{decision_id}",
            "task_id": f"task-{decision_id}",
            "verifier_id": f"verifier-{decision_id}",
            "admissible_for_runtime_achievement": True,
        }],
    }


def _row(variant_name: str, forced_path_name: str, *, ts: float) -> dict:
    variant = next(item for item in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants if item.name == variant_name)
    forced_path = next(item for item in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths if item.name == forced_path_name)
    frontier, packet = _frontier_packet(variant_name, canary.EXPECTED_SELECTION_CONTRACT)
    first_index = next(
        index
        for index, action in enumerate(frontier)
        if forced_path.first_host.casefold() in action.target
    )
    first_target = frontier[first_index].target
    decisions = [
        _decision(
            decision_id=f"{variant_name}-{forced_path_name}-read",
            capability="read-managed-local-admin-secret",
            target=first_target,
            packet=packet,
            selected_index=first_index,
            forced=True,
        ),
        _decision(
            decision_id=f"{variant_name}-{forced_path_name}-use",
            capability="use-managed-local-admin-secret",
            target=f"target={forced_path.first_host.casefold()};target_domain={forced_path.first_domain.casefold()};callback=laps-family-transfer-r5-1",
            forced=True,
        ),
        _decision(
            decision_id=f"{variant_name}-{forced_path_name}-exec",
            capability="execute-as-local-admin",
            target=f"target={forced_path.first_host.casefold()};target_domain={forced_path.first_domain.casefold()};callback=laps-family-transfer-r5-1",
            forced=True,
        ),
    ]
    correct_first = forced_path.first_host.casefold() == variant.target_host.casefold()
    if not correct_first:
        decisions.extend([
            _decision(
                decision_id=f"{variant_name}-{forced_path_name}-read-correct",
                capability="read-managed-local-admin-secret",
                target=f"account=user1;account_domain={contract.ROOT_DOMAIN};target={variant.target_host.casefold()};target_domain={variant.target_domain.casefold()};callback=laps-family-transfer-r5-1",
            ),
            _decision(
                decision_id=f"{variant_name}-{forced_path_name}-use-correct",
                capability="use-managed-local-admin-secret",
                target=f"target={variant.target_host.casefold()};target_domain={variant.target_domain.casefold()};callback=laps-family-transfer-r5-1",
            ),
            _decision(
                decision_id=f"{variant_name}-{forced_path_name}-exec-correct",
                capability="execute-as-local-admin",
                target=f"target={variant.target_host.casefold()};target_domain={variant.target_domain.casefold()};callback=laps-family-transfer-r5-1",
            ),
        ])
    return {
        "side": "harness",
        "scenario": f"laps-family-transfer-{variant_name}",
        "configured_policy_mode": canary.EXPECTED_POLICY_MODE,
        "policy_mode": canary.EXPECTED_POLICY_MODE,
        "objective_proven": True,
        "clean_stop": True,
        "request_completed": True,
        "policy_identity_valid": True,
        "backend_provenance_complete": True,
        "semantic_policy_coverage": 1.0,
        "policy_switches": [],
        "semantic_transaction_count": len(decisions),
        "phase6_manifest_hash": contract.sealed_manifest()["manifest_hash"],
        "phase6_topology_hash": contract.topology_hash(),
        "phase6_candidate_set_hash": contract.canonical_candidate_set_hash(frontier),
        "phase6_ordered_frontier_hash": contract.canonical_ordered_frontier_hash(frontier),
        "phase6_forced_path": forced_path_name,
        "decisions": decisions,
        "transactions": [_transaction(decision) for decision in decisions],
        "ts": ts,
        "ts_iso": f"2026-07-14T00:00:{int(ts):02d}",
        "chat_channel_id": int(ts),
        "chat_request_id": int(ts),
    }


def _rows() -> list[dict]:
    rows: list[dict] = []
    ts = 1.0
    for variant in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants:
        for forced_path in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths:
            rows.append(_row(variant.name, forced_path.name, ts=ts))
            ts += 1.0
    return rows


def _with_attempt_metadata(row: dict, planned_row_id: str, attempt_index: int) -> dict:
    row = copy.deepcopy(row)
    row["phase6_planned_row_id"] = planned_row_id
    row["phase6_attempt_index"] = attempt_index
    row["phase6_max_pre_frontier_diagnostic_retries"] = 1
    return row


def _pre_frontier_diagnostic_row(variant_name: str, forced_path_name: str, *, ts: float) -> dict:
    row = _row(variant_name, forced_path_name, ts=ts)
    collect = _decision(
        decision_id=f"{variant_name}-{forced_path_name}-collect",
        capability="collect-graph",
        target="cinder.local|baseline",
    )
    row["objective_proven"] = False
    row["clean_stop"] = True
    row["controller_status"] = "halted_blocked"
    row["controller_terminal_reason"] = "collection retry budget exhausted for one request"
    row["semantic_transaction_count"] = 1
    row["decisions"] = [collect]
    row["transactions"] = [_transaction(collect)]
    return row


def test_phase6_canary_validator_releases_forced_confirmation_matrix():
    report = canary.validate_canary_results(_rows(), _surface_report())

    assert report["passes_gate"] is True
    assert report["authorization"]["forced_confirmation_matrix_authorized"] is True
    assert report["matching_row_count"] == 4
    assert report["checks"]["all_preregistered_cells_present_once"] is True
    assert report["checks"]["correct_first_cost_is_three"] is True
    assert report["checks"]["wrong_first_cost_is_six"] is True


def test_phase6_canary_validator_excludes_one_pre_frontier_diagnostic_retry():
    rows = []
    for index, row in enumerate(_rows(), start=1):
        planned_row_id = f"canary-{index}"
        if index == 1:
            rows.append(_with_attempt_metadata(
                _pre_frontier_diagnostic_row("ember-remote-exec", "ember-first", ts=0.5),
                planned_row_id,
                1,
            ))
            rows.append(_with_attempt_metadata(row, planned_row_id, 2))
        else:
            rows.append(_with_attempt_metadata(row, planned_row_id, 1))

    report = canary.validate_canary_results(rows, _surface_report())

    assert report["passes_gate"] is True
    assert report["attempt_accounting"]["mode"] == "append-only"
    assert report["attempt_accounting"]["matched_attempt_row_count"] == 5
    assert report["attempt_accounting"]["countable_row_count"] == 4
    assert report["attempt_accounting"]["diagnostic_row_count"] == 1
    assert report["checks"]["attempt_accounting_valid"] is True


def test_phase6_canary_validator_does_not_hide_branch_failure_behind_retry():
    rows = []
    for index, row in enumerate(_rows(), start=1):
        planned_row_id = f"canary-{index}"
        if index == 1:
            failed = _with_attempt_metadata(row, planned_row_id, 1)
            failed["objective_proven"] = False
            failed["request_completed"] = False
            rows.append(failed)
            rows.append(_with_attempt_metadata(row, planned_row_id, 2))
        else:
            rows.append(_with_attempt_metadata(row, planned_row_id, 1))

    report = canary.validate_canary_results(rows, _surface_report())

    assert report["passes_gate"] is False
    assert report["checks"]["attempt_accounting_valid"] is False
    assert report["attempt_accounting"]["checks"]["countable_attempt_is_unique_per_planned_row"] is False
    assert report["attempt_accounting"]["checks"]["no_attempt_after_countable_attempt"] is False


def test_phase6_canary_validator_fails_closed_on_forced_policy_credit():
    rows = _rows()
    rows[0]["decisions"][0]["forced_policy_win_credit"] = True

    report = canary.validate_canary_results(rows, _surface_report())

    assert report["passes_gate"] is False
    assert report["authorization"]["forced_confirmation_matrix_authorized"] is False
    assert report["row_reports"][0]["checks"]["forced_chain_is_exact_target_label_only"] is False


def test_phase6_canary_pin_writes_hashed_rows(tmp_path):
    path = tmp_path / "canary.jsonl"

    metadata = canary.pin_canary_rows(_rows(), path)

    assert metadata["rows"] == 4
    assert metadata["sha256"].startswith("sha256:")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 4
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["scenario"].startswith("laps-family-transfer-")
