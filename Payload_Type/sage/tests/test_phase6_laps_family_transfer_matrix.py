from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

from ai.hillclimb import laps_family_transfer_canary as canary
from ai.hillclimb import laps_family_transfer_matrix as matrix
from ai.langgraph import policy


_HELPER_SPEC = importlib.util.spec_from_file_location(
    "phase6_laps_canary_test_helpers",
    Path(__file__).with_name("test_phase6_laps_family_transfer_canary.py"),
)
_HELPERS = importlib.util.module_from_spec(_HELPER_SPEC)
assert _HELPER_SPEC and _HELPER_SPEC.loader
_HELPER_SPEC.loader.exec_module(_HELPERS)
_row = _HELPERS._row
_rows = _HELPERS._rows
_surface_report = _HELPERS._surface_report
_with_attempt_metadata = _HELPERS._with_attempt_metadata
_pre_frontier_diagnostic_row = _HELPERS._pre_frontier_diagnostic_row
_decision = _HELPERS._decision
_transaction = _HELPERS._transaction


def _canary_report() -> dict:
    return canary.validate_canary_results(_rows(), _surface_report())


def _forced_rows() -> list[dict]:
    rows: list[dict] = []
    ts = 10.0
    for _ in range(3):
        for variant_name in ("ash-remote-exec", "ember-remote-exec"):
            for forced_path_name in ("ash-first", "ember-first"):
                rows.append(_row(variant_name, forced_path_name, ts=ts))
                ts += 1.0
    return rows


def _policy_row(variant_name: str, policy_mode: str, *, select_path: str, ts: float) -> dict:
    row = _row(variant_name, select_path, ts=ts)
    row["configured_policy_mode"] = policy_mode
    row["policy_mode"] = policy_mode
    row.pop("phase6_forced_path")
    row["decisions"] = copy.deepcopy(row["decisions"])
    row["transactions"] = copy.deepcopy(row["transactions"])
    selection_contract = matrix.EXPECTED_SELECTION_CONTRACTS[policy_mode]
    for decision, transaction in zip(row["decisions"], row["transactions"]):
        decision["decision_owner"] = "model_branch" if policy_mode == "hybrid" and decision["candidate_count"] > 1 else "kernel_singleton"
        decision["forced_intervention"] = False
        decision["intervention_id"] = ""
        decision["forced_policy_win_credit"] = None
        decision["decision_packet"]["selection_contract"] = selection_contract
        decision["decision_packet"]["selection_contract_hash"] = policy.selection_contract_hash(selection_contract)
        decision["decision_packet"]["policy_version"] = (
            policy.POLICY_VERSION_HYBRID if policy_mode == "hybrid" else policy.POLICY_VERSION_SYMBOLIC
        )
        decision["decision_packet_hash"] = canary.corpus._packet_hash(decision["decision_packet"])
        transaction["policy_mode"] = policy_mode
    return row


def _policy_rows() -> list[dict]:
    rows: list[dict] = []
    ts = 100.0
    for _ in range(3):
        rows.append(_policy_row("ash-remote-exec", "symbolic", select_path="ash-first", ts=ts))
        ts += 1.0
        rows.append(_policy_row("ember-remote-exec", "symbolic", select_path="ash-first", ts=ts))
        ts += 1.0
        rows.append(_policy_row("ash-remote-exec", "hybrid", select_path="ash-first", ts=ts))
        ts += 1.0
        rows.append(_policy_row("ember-remote-exec", "hybrid", select_path="ember-first", ts=ts))
        ts += 1.0
    return rows


def _interleaved_policy_row(*, extra_transactions: int = 0) -> dict:
    row = _policy_row("ash-remote-exec", "symbolic", select_path="ash-first", ts=99.0)
    sibling_decisions = [
        _decision(
            decision_id="ash-remote-exec-symbolic-read-ember",
            capability="read-managed-local-admin-secret",
            target=(
                "account=user1;account_domain=cinder.local;"
                "target=ember-ops01;target_domain=ember.cinder.local;"
                "callback=laps-family-transfer-r5-1"
            ),
        ),
        _decision(
            decision_id="ash-remote-exec-symbolic-use-ember",
            capability="use-managed-local-admin-secret",
            target="target=ember-ops01;target_domain=ember.cinder.local;callback=laps-family-transfer-r5-1",
        ),
    ]
    for decision in sibling_decisions:
        decision["decision_owner"] = "kernel_singleton"
        decision["decision_packet"]["selection_contract"] = matrix.EXPECTED_SELECTION_CONTRACTS["symbolic"]
        decision["decision_packet"]["selection_contract_hash"] = policy.selection_contract_hash(
            matrix.EXPECTED_SELECTION_CONTRACTS["symbolic"]
        )
        decision["decision_packet"]["policy_version"] = policy.POLICY_VERSION_SYMBOLIC
        decision["decision_packet_hash"] = canary.corpus._packet_hash(decision["decision_packet"])
    row["decisions"] = [
        row["decisions"][0],
        sibling_decisions[0],
        row["decisions"][1],
        sibling_decisions[1],
        row["decisions"][2],
    ]
    row["transactions"] = [
        row["transactions"][0],
        _transaction(sibling_decisions[0]),
        row["transactions"][1],
        _transaction(sibling_decisions[1]),
        row["transactions"][2],
    ]
    for index in range(extra_transactions):
        extra = _decision(
            decision_id=f"ash-remote-exec-symbolic-extra-{index}",
            capability="use-managed-local-admin-secret",
            target="target=ember-ops01;target_domain=ember.cinder.local;callback=laps-family-transfer-r5-1",
        )
        extra["decision_owner"] = "kernel_singleton"
        extra["decision_packet"]["selection_contract"] = matrix.EXPECTED_SELECTION_CONTRACTS["symbolic"]
        extra["decision_packet"]["selection_contract_hash"] = policy.selection_contract_hash(
            matrix.EXPECTED_SELECTION_CONTRACTS["symbolic"]
        )
        extra["decision_packet"]["policy_version"] = policy.POLICY_VERSION_SYMBOLIC
        extra["decision_packet_hash"] = canary.corpus._packet_hash(extra["decision_packet"])
        row["decisions"].append(extra)
        row["transactions"].append(_transaction(extra))
    row["semantic_transaction_count"] = len(row["decisions"])
    return row


def _tracked_forced_rows_with_one_diagnostic() -> list[dict]:
    rows: list[dict] = []
    for index, row in enumerate(_forced_rows(), start=1):
        planned_row_id = f"forced-{index}"
        if index == 1:
            rows.append(_with_attempt_metadata(
                _pre_frontier_diagnostic_row("ash-remote-exec", "ash-first", ts=0.5),
                planned_row_id,
                1,
            ))
            rows.append(_with_attempt_metadata(row, planned_row_id, 2))
        else:
            rows.append(_with_attempt_metadata(row, planned_row_id, 1))
    return rows


def _tracked_policy_rows_with_one_diagnostic() -> list[dict]:
    rows: list[dict] = []
    for index, row in enumerate(_policy_rows(), start=1):
        planned_row_id = f"policy-{index}"
        if index == 1:
            diagnostic = _pre_frontier_diagnostic_row("ash-remote-exec", "ash-first", ts=90.0)
            diagnostic["configured_policy_mode"] = "symbolic"
            diagnostic["policy_mode"] = "symbolic"
            diagnostic.pop("phase6_forced_path")
            rows.append(_with_attempt_metadata(diagnostic, planned_row_id, 1))
            rows.append(_with_attempt_metadata(row, planned_row_id, 2))
        else:
            rows.append(_with_attempt_metadata(row, planned_row_id, 1))
    return rows


def test_phase6_matrix_validator_reports_hybrid_win_and_shadow_failure():
    report = matrix.validate_matrix_results(_forced_rows(), _policy_rows(), _canary_report())

    assert report["passes_gate"] is True
    assert report["authorization"]["phase6_complete"] is True
    assert report["forced_matching_row_count"] == 12
    assert report["policy_matching_row_count"] == 12
    assert report["checks"]["correct_first_is_cheaper_in_all_pairs"] is True
    assert report["checks"]["hybrid_selects_correct_first_in_all_six"] is True
    assert report["checks"]["hybrid_has_lower_aggregate_regret_than_symbolic"] is True
    assert report["checks"]["shadow_fixed_order_controls_each_fail_one_variant"] is True
    assert report["policy_summaries"]["symbolic"]["aggregate_regret"] == 9.0
    assert report["policy_summaries"]["hybrid"]["aggregate_regret"] == 0.0


def test_phase6_matrix_validator_counts_only_branch_surface_attempts():
    report = matrix.validate_matrix_results(
        _tracked_forced_rows_with_one_diagnostic(),
        _tracked_policy_rows_with_one_diagnostic(),
        _canary_report(),
    )

    assert report["passes_gate"] is True
    assert report["forced_attempt_accounting"]["mode"] == "append-only"
    assert report["forced_attempt_accounting"]["matched_attempt_row_count"] == 13
    assert report["forced_attempt_accounting"]["countable_row_count"] == 12
    assert report["forced_attempt_accounting"]["diagnostic_row_count"] == 1
    assert report["policy_attempt_accounting"]["matched_attempt_row_count"] == 13
    assert report["policy_attempt_accounting"]["countable_row_count"] == 12
    assert report["policy_attempt_accounting"]["diagnostic_row_count"] == 1
    assert report["checks"]["forced_attempt_accounting_valid"] is True
    assert report["checks"]["policy_attempt_accounting_valid"] is True


def test_phase6_matrix_validator_fails_closed_on_frontier_drift():
    policy_rows = _policy_rows()
    policy_rows[0]["decisions"][0]["decision_packet"]["admissible_frontier"][0]["target"] = "drifted"

    report = matrix.validate_matrix_results(_forced_rows(), policy_rows, _canary_report())

    assert report["passes_gate"] is False
    assert report["authorization"]["phase6_complete"] is False
    assert report["checks"]["all_policy_rows_preserve_frontier_and_lineage"] is False
    assert report["policy_row_reports"][0]["checks"]["packet_hash_recomputes"] is False


def test_phase6_policy_row_accepts_proof_preserving_interleaving_cost():
    report = matrix._policy_row_report(
        _interleaved_policy_row(),
        matrix._canary_expectations(_canary_report()),
    )

    assert report["passes_gate"] is True
    assert report["transactions_from_frontier"] == 5.0
    assert report["regret"] == 2.0
    assert report["checks"]["observed_cost_within_frozen_policy_budget"] is True


def test_phase6_policy_row_rejects_over_budget_interleaving_cost():
    report = matrix._policy_row_report(
        _interleaved_policy_row(extra_transactions=2),
        matrix._canary_expectations(_canary_report()),
    )

    assert report["passes_gate"] is False
    assert report["transactions_from_frontier"] == 7.0
    assert report["checks"]["observed_cost_within_frozen_policy_budget"] is False
