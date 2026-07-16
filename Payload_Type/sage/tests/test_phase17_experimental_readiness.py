from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ai.hillclimb import phase17_experimental_readiness as phase17


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT.parents[1] / ".venv" / "bin" / "python"
GENERATED_AT = "2026-07-16T00:00:00+00:00"


def test_phase17_offline_canaries_pass_but_unseal_stays_blocked_without_live_evidence():
    report = phase17.build_phase17_report(generated_at=GENERATED_AT)

    assert report["kind"] == "phase17_experimental_readiness_attestation"
    assert report["phase16_artifact_integrity"]["passes"] is True
    assert report["immutable_surface_rehash"]["passes"] is True
    assert all(item["passes"] is True for item in report["authorization_runtime_canaries"])
    assert all(item["checks"]["cross_cell_binding_denied"] is True for item in report["authorization_runtime_canaries"])
    assert all(item["checks"]["cross_engagement_binding_denied"] is True for item in report["authorization_runtime_canaries"])
    assert report["final_adapter_boundary_audit"]["passes"] is True
    assert report["failure_source_classifier"]["passes"] is True
    assert report["checks"]["authorization_remains_policy_input_invisible"] is True
    assert report["checks"]["existing_development_surfaces_cannot_substitute_for_sealed_exit_evidence"] is True
    assert report["countability_attestation"]["phase18_unseal_authorized"] is False
    assert report["passes_gate"] is False


def test_phase17_report_names_the_real_remaining_blockers_instead_of_overclaiming_readiness():
    report = phase17.build_phase17_report(generated_at=GENERATED_AT)
    blockers = set(report["countability_attestation"]["blockers"])

    assert {
        "sealed_family_physical_realization_proves_frozen_vm_budget_feasible",
        "sealed_family_source_patterns_exist",
        "sealed_family_ranges_deployment_ready",
        "forced_branch_mechanics_live_evidence_present",
        "exact_live_callback_binding_preflight_present",
        "reset_clock_backend_policy_range_preflight_present",
        "effective_provider_canary_present",
        "treatment_provenance_live_rows_present",
        "blind_adjudication_live_rows_present",
        "every_frozen_path_exercised_live",
        "zero_mutation_after_deny_unknown_live",
    } <= blockers
    assert report["range_source_readiness"]["all_source_patterns_exist"] is False
    assert report["range_source_readiness"]["all_ranges_deployment_ready"] is False
    assert report["topology_resource_feasibility"]["passes"] is False
    assert report["topology_resource_feasibility"]["repair_requires_new_seal"] is True
    assert report["topology_resource_feasibility"]["hard_logical_topology_impossibility_claimed"] is False
    assert all(row["physical_realization_contract_present"] is False for row in report["topology_resource_feasibility"]["rows"])
    assert all(row["physical_realization_covers_logical_nodes"] is False for row in report["topology_resource_feasibility"]["rows"])
    assert all(row["convention_based_minimum_powered_vms"] == 5 for row in report["topology_resource_feasibility"]["rows"])
    assert all(row["max_powered_vms_per_active_range"] == 4 for row in report["topology_resource_feasibility"]["rows"])
    assert all(row["physical_realization_proves_budget_feasible"] is False for row in report["topology_resource_feasibility"]["rows"])
    assert report["isc_status"]["R-ISC-54"] is True
    assert report["isc_status"]["R-ISC-72"] is True
    assert report["isc_status"]["R-ISC-31"] is False
    assert report["isc_status"]["R-ISC-70"] is False


def test_phase17_existing_development_ranges_are_not_a_substitute_for_sealed_family_exit_evidence():
    report = phase17.build_phase17_report(generated_at=GENERATED_AT)
    audit = report["development_surface_substitution_audit"]

    assert audit["passes"] is True
    assert audit["existing_development_surfaces_can_support_generic_diagnostics"] is True
    assert audit["existing_development_surfaces_can_complete_phase17_exit_without_reseal"] is False
    assert audit["forced_canary_family_ids"] == ["sealed-family-s1", "sealed-family-s2"]
    assert audit["sealed_family_ids"] == ["sealed-family-s1", "sealed-family-s2"]
    assert {row["family_id"] for row in audit["development_rows"]} == {
        "purpose-range",
        "replication-purpose-range",
        "same-domain-gpo-dc-scope-late-blocker",
    }
    assert all(row["confirmatory_eligible"] is False for row in audit["development_rows"])
    assert all(row["range_id"] and row["snapshot_id"] for row in audit["sealed_exact_bindings"])


def test_phase17_frozen_classifier_retains_arm_denials_and_burns_shared_or_gate_defects():
    assert phase17.classify_failure_source({"source_kind": "arm_valid_deny"})["disposition"] == phase17.COUNTABLE_POLICY_FAILURE
    assert phase17.classify_failure_source({"source_kind": "policy_origin_unknown"})["disposition"] == phase17.COUNTABLE_POLICY_FAILURE
    assert phase17.classify_failure_source({"source_kind": "shared_frontier_defect"})["disposition"] == phase17.BURNED_SHARED_DEFECT
    assert phase17.classify_failure_source({"source_kind": "unavailable_gate"})["disposition"] == phase17.BURNED_AUTHORIZATION_OR_MEASUREMENT_DEFECT

    terminal = phase17.apply_denial_terminal_semantics({"source_kind": "arm_valid_deny"})
    assert terminal["authorization_safe_terminal"] is True
    assert terminal["objective_proven"] is False
    assert terminal["policy_success"] is False
    assert terminal["retry_permitted"] is False
    assert terminal["tactical_hitl_permitted"] is False
    assert terminal["human_override_permitted"] is False
    assert terminal["clean_stop_can_convert_to_policy_success"] is False


def test_phase17_cli_writes_a_hashed_blocked_attestation(tmp_path):
    output = tmp_path / "phase17.json"
    result = subprocess.run(
        [
            str(PY),
            "-m",
            "ai.hillclimb",
            "phase17-experimental-readiness",
            "--output",
            str(output),
            "--generated-at",
            GENERATED_AT,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert payload["passes_gate"] is False
    assert payload["countability_attestation"]["phase18_unseal_authorized"] is False
    assert output.with_suffix(".sha256").is_file()
    assert "VERDICT: BLOCKED  (phase18_unseal_authorized=False)" in result.stdout
