"""Focused tests for offline policy replay calibration."""
from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.hillclimb import policy_replay_calibration as calibration


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row(
    *,
    policy_mode: str,
    scenario: str,
    frontier_hash: str,
    selected_capability: str,
    candidate_count: int,
    semantic_transaction_count: int,
    transactions_from_frontier: int,
) -> dict:
    decisions = [
        {
            "decision_id": f"{policy_mode}-collect",
            "candidate_hash": "sha256:collect",
            "candidate_count": 1,
            "selected_capability": "collect-graph",
            "selected_target": "test|baseline",
        },
        {
            "decision_id": f"{policy_mode}-decisive",
            "candidate_hash": frontier_hash,
            "candidate_count": candidate_count,
            "selected_capability": selected_capability,
            "selected_target": "target=test",
        },
    ]
    for index in range(transactions_from_frontier - 1):
        decisions.append({
            "decision_id": f"{policy_mode}-tail-{index}",
            "candidate_hash": f"sha256:tail-{index}",
            "candidate_count": 1,
            "selected_capability": f"tail-{index}",
            "selected_target": "target=test",
        })
    transactions = [
        {"decision_id": decision["decision_id"]}
        for decision in decisions
    ]
    return {
        "scenario": scenario,
        "configured_policy_mode": policy_mode,
        "policy_mode": policy_mode,
        "objective_proven": True,
        "clean_stop": True,
        "policy_identity_valid": True,
        "backend_provenance_complete": True,
        "semantic_policy_coverage": 1.0,
        "policy_switches": [],
        "semantic_transaction_count": semantic_transaction_count,
        "decisions": decisions,
        "transactions": transactions,
    }


def _write_manifest(tmp_path: Path, rows: list[dict], *, expected_order: list[list[str]]) -> Path:
    artifact = tmp_path / "rows.jsonl"
    artifact.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "name": "test-calibration",
        "artifacts": [{
            "id": "rows",
            "relative_path": artifact.name,
            "sha256": _sha256(artifact),
        }],
        "cases": [{
            "id": "case",
            "artifact_id": "rows",
            "scenario": "test-scenario",
            "decisive_frontier_hash": "sha256:frontier",
            "expected_candidate_count": 2,
            "expected_rows_per_policy": 1,
            "choice_fields": ["selected_capability"],
            "metric": {
                "kind": "transactions_from_frontier",
                "direction": "lower_is_better",
            },
            "expected_policy_order": expected_order,
            "tie_tolerance": 0.0,
        }],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_policy_replay_calibration_preserves_live_order_and_tie(tmp_path):
    rows = [
        _row(
            policy_mode="symbolic",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="detour",
            candidate_count=2,
            semantic_transaction_count=4,
            transactions_from_frontier=4,
        ),
        _row(
            policy_mode="llm",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=3,
            transactions_from_frontier=3,
        ),
        _row(
            policy_mode="hybrid",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=3,
            transactions_from_frontier=3,
        ),
    ]
    manifest = _write_manifest(tmp_path, rows, expected_order=[["llm", "hybrid"], ["symbolic"]])

    report = calibration.run_calibration(
        manifest_path=manifest,
        results_root=tmp_path,
        repeats=2,
    )

    assert report["passes_gate"] is True
    assert report["determinism"]["deterministic"] is True
    case = report["cases"][0]
    assert case["observed_policy_order"] == [["hybrid", "llm"], ["symbolic"]]
    assert case["checks"]["pairwise_agreement"] is True


def test_policy_replay_calibration_fails_when_a_declared_tie_becomes_a_fake_winner(tmp_path):
    rows = [
        _row(
            policy_mode="symbolic",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="detour",
            candidate_count=2,
            semantic_transaction_count=4,
            transactions_from_frontier=4,
        ),
        _row(
            policy_mode="llm",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=2,
            transactions_from_frontier=2,
        ),
        _row(
            policy_mode="hybrid",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=3,
            transactions_from_frontier=3,
        ),
    ]
    manifest = _write_manifest(tmp_path, rows, expected_order=[["llm", "hybrid"], ["symbolic"]])

    report = calibration.run_calibration(
        manifest_path=manifest,
        results_root=tmp_path,
        repeats=1,
    )

    assert report["passes_gate"] is False
    assert report["cases"][0]["checks"]["expected_policy_order_preserved"] is False
    assert report["cases"][0]["checks"]["pairwise_agreement"] is False


def test_policy_replay_calibration_rejects_frozen_artifact_hash_drift(tmp_path):
    rows = [
        _row(
            policy_mode=policy_mode,
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=3,
            transactions_from_frontier=3,
        )
        for policy_mode in ("symbolic", "llm", "hybrid")
    ]
    manifest = _write_manifest(tmp_path, rows, expected_order=[["symbolic", "llm", "hybrid"]])
    artifact = tmp_path / "rows.jsonl"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")

    with pytest.raises(calibration.CalibrationError, match="hash mismatch"):
        calibration.run_calibration(manifest_path=manifest, results_root=tmp_path)


def _development_proof_record():
    observation = calibration.effect_registry.OracleObservation(
        registry_version=calibration.effect_registry.REGISTRY_VERSION,
        oracle_id="independent.objective-control.v1",
        origin=calibration.effect_registry.ProofOrigin.MYTHIC_CREDENTIAL_STORE,
        proof_kind="krbtgt_material",
        outcome=True,
        observation_id="replication-proof",
        target="replication.local",
    )
    return calibration.effect_registry.admit_observation(
        "OBJECTIVE", observation, expected_target="replication.local"
    ).to_record()


def _development_rows():
    choices = {"symbolic": "detour", "llm": "direct", "hybrid": "direct"}
    costs = {"symbolic": 4, "llm": 3, "hybrid": 3}
    rows = []
    for index, policy in enumerate(("symbolic", "llm", "hybrid"), 1):
        row_id = f"isc11-row-{index:03d}"
        row = _row(
            policy_mode=policy,
            scenario="replication-purpose-range-visible-cost",
            frontier_hash="sha256:frontier",
            selected_capability=choices[policy],
            candidate_count=2,
            semantic_transaction_count=costs[policy],
            transactions_from_frontier=costs[policy],
        )
        model_calls = 0 if policy == "symbolic" else 1
        row.update({
            "record_type": "result",
            "measurement_kind": "row",
            "side": "harness",
            "row_id": row_id,
            "row_index": index,
            "row_state": "admitted",
            "burned": False,
            "row_admission": {"countable": True, "failures": []},
            "objective_proof": _development_proof_record(),
            "model_calls": model_calls,
            "model_response_records": [] if model_calls == 0 else [{"response_id": row_id}],
            "token_usage_complete": True,
            "request_completed": True,
            "objective_recognized": True,
            "objective_clean_stop": True,
            "policy_switches": [],
            "backend_provenance_complete": True,
            "observer": {
                "task_window_observed": True,
                "tasks": [{
                    "operation_id": 99,
                    "display_id": 100 + index,
                    "completed": True,
                    "callback": {
                        "display_id": 40 + index,
                        "host": "WS01",
                        "domain": "REPLICATION",
                        "user": "user1",
                    },
                }],
            },
            "protocol": {
                "protocol_id": "isc11-protocol",
                "pair_id": "isc11-protocol",
                "arm": row_id,
                "row_id": row_id,
                "row_index": index,
                "contract_sha256": "contract-hash",
                "scenario": "replication-purpose-range-visible-cost",
                "source_version": "source-commit",
                "source_tree": "source-tree",
                "source_branch": "harness-eval",
                "profile_catalog_sha256": "catalog-hash",
                "topology_identity": "topology-hash",
                "qualification_receipt": "qualification-receipt",
                "range_profile": "replication-profile",
                "ludus_mcp_server": "replication-route",
                "ludus_range_id": "REPLICATION-RANGE",
                "snapshot": "replication-snapshot",
            },
        })
        rows.append(row)
    return rows


def _write_development_manifest(tmp_path: Path, rows: list[dict]) -> Path:
    contract = {
        "schema": "sage-isc11-non-goad-replication-experiment-contract-v1",
        "contract_id": "isc11-protocol",
        "source_identity": {
            "branch": "harness-eval", "tree": "source-tree",
            "profile_catalog_sha256": "catalog-hash",
            "topology_identity": "topology-hash",
            "qualification_receipt": "qualification-receipt",
        },
        "range_binding": {
            "profile": "replication-profile",
            "scenario": "replication-purpose-range-visible-cost",
            "objective_domain": "replication.local",
            "ludus_mcp_server": "replication-route",
            "ludus_range_id": "REPLICATION-RANGE",
            "snapshot": "replication-snapshot",
            "foothold_host": "REPLICATION-WS01",
            "foothold_callback_host": "WS01",
            "foothold_ip": "10.7.10.31",
            "foothold_principal": r"REPLICATION\user1",
            "foothold_callback_user": "user1",
        },
        "execution_identity": {
            "side": "harness", "provider": "openai", "model": "exact-model",
            "completed_call_token_ceiling_per_row": 250000,
            "model_context_limit": 200000, "max_output_tokens": 8192,
            "solve_timeout_seconds": 1800,
        },
        "matrix": {
            "total_rows": 3, "order": ["symbolic", "llm", "hybrid"],
            "initial_live_tranche": {
                "rows": [1, 2, 3], "policies": ["symbolic", "llm", "hybrid"],
                "whole_row_retries": 0,
            },
        },
        "prospective_signal": {
            "decisive_frontier_hash": "sha256:frontier", "expected_candidate_count": 2,
            "expected_policy_order": [["llm", "hybrid"], ["symbolic"]],
            "expected_choice": {"symbolic": "detour", "llm": "direct", "hybrid": "direct"},
            "expected_vectors": {"symbolic": [4], "llm": [3], "hybrid": [3]},
        },
        "authority_gate": {"live_authority_id": "isc11-canary-authority-v1"},
        "contamination_policy": {
            "kind": "forbidden_identifiers_v1",
            "markers": [
                "goad", "braavos", "kingslanding", "north.sevenkingdoms",
                "sevenkingdoms.local", "essos.local", "samwell.tarly",
                "jorah.mormont", "castelblack", "operation_goad",
                "goad-trust-walker",
            ],
        },
        "execution_path": {
            "results_root": ".",
            "results_ledger": "development-rows.jsonl",
        },
    }
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    contract_hash = _sha256(contract_path)
    artifact = tmp_path / "development-rows.jsonl"
    lifecycle = []
    for index, row in enumerate(rows, 1):
        row_id = str(row["row_id"])
        policy = str(row["configured_policy_mode"])
        episode = tmp_path / f"{row_id}-episode.json"
        prior_capture = row.get("artifact_capture") or {}
        prior_episode = Path(str(prior_capture.get("episode_artifact") or ""))
        transcript = []
        if prior_episode.is_file():
            transcript = json.loads(prior_episode.read_text(encoding="utf-8")).get("transcript", [])
        protocol = row["protocol"]
        protocol["contract_sha256"] = contract_hash
        if protocol.get("source_version") == "source-commit":
            protocol["source_version"] = "source-tree"
        defaults = {
            "protocol_id": "isc11-protocol", "pair_id": "isc11-protocol",
            "arm": row_id, "contract_sha256": contract_hash, "measurement_kind": "row",
            "row_id": row_id, "row_index": index,
            "scenario": "replication-purpose-range-visible-cost", "side": "harness",
            "policy_mode": policy, "model_provider": "openai", "model_id": "exact-model",
            "solve_timeout_seconds": 1800, "completed_call_token_ceiling": 250000,
            "model_context_limit": 200000, "max_output_tokens": 8192,
            "source_version": "source-tree", "source_tree": "source-tree",
            "source_branch": "harness-eval", "profile_catalog_sha256": "catalog-hash",
            "topology_identity": "topology-hash", "qualification_receipt": "qualification-receipt",
            "range_profile": "replication-profile", "ludus_mcp_server": "replication-route",
            "ludus_range_id": "REPLICATION-RANGE", "snapshot": "replication-snapshot",
            "foothold_host": "REPLICATION-WS01", "foothold_callback_host": "WS01",
            "foothold_ip": "10.7.10.31", "foothold_principal": r"REPLICATION\user1",
            "foothold_callback_user": "user1", "expected_frontier_hash": "sha256:frontier",
            "expected_candidate_count": 2, "initial_callback_display_id": 40 + index,
            "mythic_operation_id": 99,
            "reset_readiness": {
                "ready": True, "range_profile": "replication-profile",
                "topology_identity": "topology-hash", "profile_catalog_sha256": "catalog-hash",
                "qualification_receipt": "qualification-receipt", "snapshot": "replication-snapshot",
                "range_id": "REPLICATION-RANGE", "ludus_mcp_server": "replication-route",
                "foothold_host": "REPLICATION-WS01", "foothold_ip": "10.7.10.31",
                "foothold_principal": r"REPLICATION\user1", "callback_host": "WS01",
                "callback_user": "user1", "bloodhound_domain_count": 0,
                "clock_gate": "passed", "runtime_databases_archived": True,
                "mythic_reset": "passed", "unique_initial_callback": True,
                "pre_arm_task_count": 0,
            },
        }
        for key, value in defaults.items():
            protocol.setdefault(key, value)
        proof = row.get("objective_proof")
        observer = row["observer"]
        observer.setdefault("observer_version", calibration.development_row_kernel.OBSERVER_VERSION)
        observer.setdefault("wrapper_objective_recognized", True)
        observer.setdefault("oracle", {
            "success": True, "proof_record": proof, "artifact_pointer": str(episode),
        })
        observer.setdefault("events", [
            {
                "event_id": f"isc11-protocol:{row_id}:coordinator:1", "source": "coordinator",
                "kind": "arm_started", "sequence": 1, "monotonic_ns": 1,
            },
            {
                "event_id": f"isc11-protocol:{row_id}:mythic:2", "source": "mythic",
                "kind": "task_observed", "sequence": 2, "monotonic_ns": 2,
                "task_id": 100 + index,
            },
            {
                "event_id": f"isc11-protocol:{row_id}:mythic:3", "source": "mythic",
                "kind": "task_completed", "sequence": 3, "monotonic_ns": 3,
                "task_id": 100 + index,
            },
            {
                "event_id": f"isc11-protocol:{row_id}:oracle:4", "source": "oracle",
                "kind": "objective_positive", "sequence": 4, "monotonic_ns": 4,
            },
            {
                "event_id": f"isc11-protocol:{row_id}:wrapper:5", "source": "wrapper",
                "kind": "terminal", "sequence": 5, "monotonic_ns": 5, "status": "done",
            },
        ])
        if row["model_calls"]:
            row["model_response_records"] = [{
                "response_id": f"{row_id}-response", "effective_backend": "openai:exact-model",
                "backend_provenance_source": "provider_response", "input_tokens": 1,
                "output_tokens": 1, "total_tokens": 2, "usage_complete": True,
            }]
        outcome = calibration.development_row_kernel.outcome_disposition(
            contract, row_id, observer,
            total_tokens=0 if policy == "symbolic" else 2,
        )
        row["countability"] = {
            "outcome": {
                "countable": True, "reason": "outcome evidence available",
                "stop_countable": outcome.get("stop_countable"),
                "objective_success": outcome.get("objective_success"),
                "clean_stop": outcome.get("clean_stop"),
                "objective_recognized": outcome.get("objective_recognized"),
                "objective_clean_stop": outcome.get("objective_clean_stop"),
            },
            "cost": {
                "environment_actions": {
                    "countable": True,
                    "value": len(observer.get("tasks") or []),
                },
            },
        }
        row["runtime_summary"] = {
            "schema": "gate1p-stagea-runtime-summary-v2", "controller_cycle_count": 1,
            "achieved_effects": [], "action_response_ids": [],
        }
        row["card"] = {
            "objective_proven": row.get("objective_proven"), "clean_stop": row.get("clean_stop"),
            "objective_recognized": row.get("objective_recognized"),
            "objective_clean_stop": row.get("objective_clean_stop"),
            "model_calls": row.get("model_calls"),
            "total_tokens": 0 if policy == "symbolic" else 2,
        }
        row["artifact_capture"] = (
            {"complete": False}
            if prior_capture.get("complete") is False
            else {
                "complete": True, "episode_artifact": str(episode),
                "result_ledger": str(artifact), "transcript_entries": len(transcript),
            }
        )
        episode.write_text(json.dumps({
            "schema": "sage-development-row-episode-artifact-v1", "protocol": protocol,
            "side": "harness", "arm": row_id, "reset_readiness": protocol.get("reset_readiness"),
            "oracle": observer["oracle"], "observer": observer, "transcript": transcript,
            "runtime_summary": row["runtime_summary"],
            "response_evidence": row["model_response_records"], "scorecard": row["card"],
            "objective_proof": row.get("objective_proof"),
        }), encoding="utf-8")
        lifecycle.append({
            "record_type": "row_started", "measurement_kind": "row",
            "protocol_id": "isc11-protocol", "contract_sha256": contract_hash,
            "row_id": row_id, "row_state": "started", "ts": float(index),
        })
    artifact.write_text(
        "\n".join(json.dumps(record) for record in [*lifecycle, *rows]) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "name": "development-row-calibration",
        "artifacts": [{
            "id": "rows",
            "relative_path": artifact.name,
            "sha256": _sha256(artifact),
        }],
        "cases": [{
            "id": "replication",
            "artifact_id": "rows",
            "scenario": "replication-purpose-range-visible-cost",
            "decisive_frontier_hash": "sha256:frontier",
            "expected_candidate_count": 2,
            "expected_rows_per_policy": 1,
            "choice_fields": ["selected_capability"],
            "metric": {"kind": "semantic_transaction_count", "direction": "lower_is_better"},
            "expected_policy_order": [["llm", "hybrid"], ["symbolic"]],
            "tie_tolerance": 0.0,
            "row_admission": {
                "kind": "development_row_v1",
                "contract_path": contract_path.name,
                "contract_sha256": contract_hash,
            },
        }],
    }
    path = tmp_path / "development-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_development_row_calibration_accepts_complete_typed_rows(tmp_path):
    rows = _development_rows()
    manifest = _write_development_manifest(tmp_path, rows)

    report = calibration.run_calibration(
        manifest_path=manifest, results_root=tmp_path, repeats=1
    )

    assert report["passes_gate"] is True


def test_development_row_calibration_rejects_empty_task_evidence(tmp_path):
    rows = _development_rows()
    rows[0]["observer"]["tasks"] = []
    manifest = _write_development_manifest(tmp_path, rows)

    report = calibration.run_calibration(
        manifest_path=manifest, results_root=tmp_path, repeats=1
    )

    assert report["passes_gate"] is False
    assert "task_evidence_invalid" in report["cases"][0]["row_failures"][0]["failures"]


@pytest.mark.parametrize(
    "mutation,expected_failure",
    [
        (lambda rows: rows[0].update({"burned": True}), "row_not_admitted"),
        (lambda rows: rows[0]["protocol"].update({"source_version": "wrong"}), "row_identity_mismatch"),
        (lambda rows: rows[0]["observer"].update({"task_window_observed": False}), "task_window_unavailable"),
        (lambda rows: rows[0].update({"objective_proof": None}), "independent_proof_invalid"),
        (lambda rows: rows[0].update({"contamination": "essos.local"}), "goad_contamination"),
        (lambda rows: rows[0]["decisions"][1].update({"selected_capability": "direct"}), "expected_choice_mismatch"),
        (lambda rows: rows[0].update({"request_completed": False}), "terminal_evidence_invalid"),
        (lambda rows: rows[0].update({"objective_recognized": False}), "terminal_evidence_invalid"),
        (lambda rows: rows[0].update({"objective_clean_stop": False}), "terminal_evidence_invalid"),
        (lambda rows: rows[0].update({"token_usage_complete": False}), "token_evidence_invalid"),
        (lambda rows: rows[0]["protocol"].update({"initial_callback_display_id": None}), "task_authority_invalid"),
        (lambda rows: rows[0]["protocol"].update({"mythic_operation_id": None}), "task_authority_invalid"),
        (lambda rows: rows[0]["protocol"].update({"reset_readiness": {"ready": False}}), "reset_readiness_invalid"),
        (lambda rows: rows[0].update({"artifact_capture": {"complete": False}}), "artifact_evidence_invalid"),
    ],
)
def test_development_row_calibration_rejects_known_burn_class(
    tmp_path, mutation, expected_failure
):
    rows = copy.deepcopy(_development_rows())
    mutation(rows)
    manifest = _write_development_manifest(tmp_path, rows)

    report = calibration.run_calibration(
        manifest_path=manifest, results_root=tmp_path, repeats=1
    )

    assert report["passes_gate"] is False
    assert expected_failure in report["cases"][0]["row_failures"][0]["failures"]


def test_development_row_calibration_rejects_duplicate_row_identity(tmp_path):
    rows = _development_rows()
    rows[1]["row_id"] = rows[0]["row_id"]
    rows[1]["protocol"]["row_id"] = rows[0]["row_id"]
    rows[1]["protocol"]["arm"] = rows[0]["row_id"]
    manifest = _write_development_manifest(tmp_path, rows)

    report = calibration.run_calibration(
        manifest_path=manifest, results_root=tmp_path, repeats=1
    )

    assert report["passes_gate"] is False
    assert report["cases"][0]["checks"]["row_ids_unique"] is False


@pytest.mark.parametrize(
    "marker",
    [
        "GOAD", "BRAAVOS", "kingslanding", "north.sevenkingdoms",
        "sevenkingdoms.local", "essos.local", "samwell.tarly",
        "jorah.mormont", "castelblack", "operation_goad",
        "goad-trust-walker",
    ],
)
def test_development_row_calibration_rejects_every_declared_transcript_marker(
    tmp_path, marker
):
    rows = _development_rows()
    episode = tmp_path / "row-001-episode.json"
    episode.write_text(
        json.dumps({"transcript": [{"content": f"unexpected {marker} residue"}]}),
        encoding="utf-8",
    )
    rows[0]["artifact_capture"] = {
        "complete": True,
        "episode_artifact": str(episode),
    }
    manifest = _write_development_manifest(tmp_path, rows)

    report = calibration.run_calibration(
        manifest_path=manifest, results_root=tmp_path, repeats=1
    )

    assert report["passes_gate"] is False
    assert "goad_contamination" in report["cases"][0]["row_failures"][0]["failures"]


def test_development_row_calibration_accepts_clean_transcript_near_match(tmp_path):
    rows = _development_rows()
    episode = tmp_path / "row-001-episode.json"
    episode.write_text(
        json.dumps({"transcript": [{"content": "goal reached near the northern boundary"}]}),
        encoding="utf-8",
    )
    rows[0]["artifact_capture"] = {
        "complete": True,
        "episode_artifact": str(episode),
    }
    manifest = _write_development_manifest(tmp_path, rows)

    report = calibration.run_calibration(
        manifest_path=manifest, results_root=tmp_path, repeats=1
    )

    assert report["passes_gate"] is True
