from __future__ import annotations

import copy

from ai.hillclimb import phase8_goad_regression as phase8


def _phase6_report() -> dict:
    return {
        "kind": "laps_family_transfer_matrix_validation",
        "passes_gate": True,
        "authorization": {"phase6_complete": True},
        "checks": {
            "hybrid_selects_correct_first_in_all_six": True,
            "hybrid_proves_and_clean_stops_in_all_six": True,
            "hybrid_has_lower_aggregate_regret_than_symbolic": True,
        },
        "policy_summaries": {
            "symbolic": {"aggregate_regret": 15.0},
            "hybrid": {"aggregate_regret": 0.0, "correct_first_count": 6},
        },
    }


def _phase7_report() -> dict:
    return {
        "kind": "trust_context_corroboration_live_validation",
        "passes_gate": True,
        "authorization": {"phase7_complete": True},
        "checks": {
            "all_rows_pass": True,
            "three_post_context_mythic_credential_proofs": True,
            "zero_premature_parent_dcsync_exposure": True,
        },
    }


def _decision(decision_id: str, *, owner: str, hybrid: bool) -> dict:
    return {
        "decision_id": decision_id,
        "decision_owner": owner,
        "selected_capability": "gpo-controlled-system-exec",
        "selected_target": "gpo=generic;domain=example.local",
        "decision_packet_hash": f"sha256:{decision_id:0<64}"[:71],
        "effective_backend": "openai:gpt-5.5-cyber-preview" if hybrid and owner == "model_branch" else "",
        "backend_provenance_source": "response_metadata" if hybrid and owner == "model_branch" else "",
    }


def _transaction(decision: dict, index: int) -> dict:
    transaction_id = f"transaction-{decision['decision_id']}"
    task_id = f"task-{decision['decision_id']}"
    verifier_id = f"verifier-{decision['decision_id']}"
    proof_id = f"sha256:{index:064d}"
    return {
        "transaction_id": transaction_id,
        "decision_id": decision["decision_id"],
        "child_tasks": [
            {
                "task_id": task_id,
                "command": "shell",
                "terminal_status": "completed",
            }
        ],
        "verifier_ids": [verifier_id],
        "proof_envelope_ids": [proof_id],
        "proof_lineage": [
            {
                "proof_envelope_id": proof_id,
                "transaction_id": transaction_id,
                "task_id": task_id,
                "verifier_id": verifier_id,
                "scope": "runtime",
                "origin": "mythic_task",
                "admissible_for_runtime_achievement": True,
            }
        ],
    }


def _row(
    policy_arm: str,
    seed_index: int,
    *,
    attempt_index: int = 1,
    cost: int | None = None,
    hybrid_branch: bool = True,
) -> dict:
    hybrid = policy_arm == "hybrid"
    model_branch = hybrid and hybrid_branch
    decisions = [
        _decision(
            f"{policy_arm}-{seed_index}-1",
            owner="model_branch" if model_branch else "kernel_singleton",
            hybrid=hybrid,
        ),
        _decision(f"{policy_arm}-{seed_index}-2", owner="kernel_singleton", hybrid=hybrid),
    ]
    for decision in decisions:
        decision["branch_opportunity_count"] = 1 if decision["decision_owner"] == "model_branch" else 0
    transactions = [_transaction(decision, index) for index, decision in enumerate(decisions, start=1)]
    return {
        "side": "harness",
        "scenario": phase8.SCENARIO_NAME,
        "phase8_contract_hash": phase8.sealed_manifest()["manifest_hash"],
        "phase8_policy_arm": policy_arm,
        "phase8_planned_row_id": phase8.planned_row_id(policy_arm, seed_index),
        "phase8_attempt_index": attempt_index,
        "configured_policy_mode": policy_arm,
        "policy_mode": policy_arm,
        "policy_identity_valid": True,
        "backend_provenance_complete": True,
        "semantic_policy_coverage": 1.0,
        "policy_switches": [],
        "request_completed": True,
        "objective_proven": True,
        "clean_stop": True,
        "semantic_transaction_count": cost if cost is not None else (5 if hybrid else 6),
        "model_calls": 1 if model_branch else 0,
        "effective_backends": ["openai:gpt-5.5-cyber-preview"] if model_branch else [],
        "effective_backend_requests": (
            [
                {
                    "effective_backend": "openai:gpt-5.5-cyber-preview",
                    "backend_provenance_source": "response_metadata",
                }
            ]
            if model_branch
            else []
        ),
        "chat_channel_id": seed_index,
        "chat_request_id": 1,
        "decisions": decisions,
        "transactions": transactions,
        "ts_iso": f"2026-07-15T00:00:0{seed_index}",
    }


def _valid_rows(*, hybrid_branch: bool = True) -> list[dict]:
    return [
        *[_row("symbolic", index) for index in range(1, 6)],
        *[_row("hybrid", index, hybrid_branch=hybrid_branch) for index in range(1, 6)],
    ]


def _validate(rows: list[dict]) -> dict:
    return phase8.validate_goad_regression_rows(
        rows,
        _phase6_report(),
        _phase7_report(),
        results_artifact_sha256="sha256:rows",
        phase6_artifact_sha256=phase8.EXPECTED_PHASE6_REPORT_SHA256,
        phase7_artifact_sha256=phase8.EXPECTED_PHASE7_REPORT_SHA256,
    )


def test_phase8_contract_freezes_goals_budgets_and_recommendation():
    manifest = phase8.sealed_manifest()

    assert manifest["contract_name"] == "sage-phase8-goad-regression-v2"
    assert manifest["scenario"] == "cross-forest-objective"
    assert manifest["budgets"]["required_seeds_per_policy"] == 5
    assert manifest["budgets"]["max_llm_canary_rows"] == 1
    assert manifest["recommendation"] == "hybrid_default_recommended_pending_operator_approval"
    assert manifest["manifest_hash"].startswith("sha256:")


def test_phase8_validator_recommends_hybrid_after_full_regression_and_prerequisites():
    report = _validate(_valid_rows())

    assert report["passes_gate"] is True
    assert report["authorization"]["phase8_complete"] is True
    assert report["authorization"]["product_default_changed"] is False
    assert report["recommendation"]["decision"] == phase8.RECOMMENDATION
    assert report["typed_verdict"]["promotion_evidence_passed"] is True
    assert report["checks"]["exact_five_symbolic_rows"] is True
    assert report["checks"]["exact_five_hybrid_rows"] is True
    assert report["checks"]["conference_visible_hybrid_decision_attribution"] is True
    assert report["checks"]["hybrid_cost_not_worse_than_symbolic"] is True


def test_phase8_validator_accepts_kernel_only_goad_reliability_when_phase6_supplies_causal_vignette():
    report = _validate(_valid_rows(hybrid_branch=False))

    assert report["passes_gate"] is True
    assert report["checks"]["hybrid_backend_identity_is_stable"] is True
    assert report["checks"]["conference_visible_hybrid_decision_attribution"] is True
    assert report["policy_summaries"]["hybrid"]["model_branch_decision_count"] == 0
    assert report["policy_summaries"]["hybrid"]["branch_opportunity_count"] == 0
    assert (
        report["policy_summaries"]["hybrid"]["attribution_mode"]
        == "goad_kernel_only_reliability_phase6_causal_vignette"
    )


def test_phase8_validator_rejects_kernel_only_hybrid_when_a_branch_opportunity_is_present():
    rows = _valid_rows(hybrid_branch=False)
    rows[-1]["decisions"][0]["branch_opportunity_count"] = 1

    report = _validate(rows)

    assert report["passes_gate"] is False
    assert report["checks"]["hybrid_backend_identity_is_stable"] is False
    assert report["checks"]["conference_visible_hybrid_decision_attribution"] is False
    assert report["policy_summaries"]["hybrid"]["attribution_mode"] == "invalid_or_incomplete"


def test_phase8_validator_retains_prefrontier_diagnostic_without_counting_it_as_seed():
    rows = _valid_rows()
    diagnostic = _row("symbolic", 1, attempt_index=1)
    diagnostic["decisions"] = [
        {
            "decision_id": "symbolic-1-collect",
            "decision_owner": "kernel_singleton",
            "selected_capability": "collect-graph",
        }
    ]
    diagnostic["transactions"] = []
    diagnostic["request_completed"] = False
    rows[0]["phase8_attempt_index"] = 2
    rows.insert(0, diagnostic)

    report = _validate(rows)

    assert report["passes_gate"] is True
    assert report["attempt_accounting"]["matched_attempt_row_count"] == 11
    assert report["attempt_accounting"]["countable_row_count"] == 10
    assert report["attempt_accounting"]["diagnostic_row_count"] == 1
    assert report["checks"]["attempt_accounting_valid"] is True


def test_phase8_validator_fails_closed_on_off_agent_proof():
    rows = _valid_rows()
    bad = copy.deepcopy(rows[-1])
    bad["transactions"][0]["proof_lineage"][0]["origin"] = "host"
    rows[-1] = bad

    report = _validate(rows)

    assert report["passes_gate"] is False
    assert report["authorization"]["phase8_complete"] is False
    assert report["checks"]["zero_off_agent_proof"] is False
    assert report["typed_verdict"]["boundary_passed"] is False


def test_phase8_validator_rejects_hybrid_cost_regression():
    rows = _valid_rows()
    for row in rows:
        if row["phase8_policy_arm"] == "hybrid":
            row["semantic_transaction_count"] = 8

    report = _validate(rows)

    assert report["passes_gate"] is False
    assert report["checks"]["hybrid_cost_not_worse_than_symbolic"] is False
    assert report["typed_verdict"]["non_regression_passed"] is False
    assert report["recommendation"]["decision"] == phase8.REJECTION
