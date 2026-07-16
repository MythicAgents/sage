from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.hillclimb import phase16_structural_benchmark_portfolio as phase16  # noqa: E402


GENERATED_AT = "2026-07-16T00:00:00+00:00"


def test_phase16_portfolio_passes_required_static_and_fixture_gates():
    report = phase16.build_phase16_report(generated_at=GENERATED_AT)

    assert report["passes_gate"] is True
    assert report["stop_loss"]["emitted"] is False
    assert report["entry_evidence"]["live_policy_matrix_authorized"] is False
    assert report["isc_status"] == {
        "R-ISC-29": True,
        "R-ISC-30": True,
        "R-ISC-32": True,
        "R-ISC-33": True,
        "R-ISC-50": True,
        "R-ISC-51": True,
        "R-ISC-52": True,
        "R-ISC-53": True,
        "R-ISC-57": True,
        "R-ISC-60": True,
        "R-ISC-61": True,
        "R-ISC-66": True,
        "R-ISC-68": True,
        "R-ISC-69": True,
        "R-ISC-80": True,
        "R-ISC-81": True,
    }


def test_phase16_sealed_families_are_fresh_split_and_answer_blind():
    report = phase16.build_phase16_report(generated_at=GENERATED_AT)
    topology = report["topology_exposure_audit"]
    split = report["split_manifest"]

    assert topology["passes"] is True
    assert topology["collisions"] == []
    assert len(topology["sealed_family_hashes"]) == 2
    assert all(split["checks"].values())
    assert {row["partition"] for row in split["rows"]} == {
        "mechanics_development",
        "structural_t1_development",
        "sealed_confirmatory",
    }
    assert all(item["passes"] is True for item in report["leakage_audits"])
    assert all(item["forbidden_family_fields_present"] == [] for item in report["leakage_audits"])


def test_phase16_topology_hash_is_rename_invariant_but_family_distinguishing():
    family = phase16.SEALED_FAMILIES[0]
    renamed_nodes = tuple(
        phase16.TopologyNode(node_id=f"renamed-{index}", kind=node.kind)
        for index, node in enumerate(family.nodes)
    )
    mapping = {
        node.node_id: renamed.node_id
        for node, renamed in zip(family.nodes, renamed_nodes)
    }
    renamed_edges = tuple(
        phase16.TopologyEdge(
            source=mapping[edge.source],
            relation=edge.relation,
            target=mapping[edge.target],
        )
        for edge in family.edges
    )

    assert phase16.canonical_topology_hash(family.nodes, family.edges) == phase16.canonical_topology_hash(
        renamed_nodes,
        renamed_edges,
    )
    assert phase16.canonical_topology_hash(
        phase16.SEALED_FAMILIES[0].nodes,
        phase16.SEALED_FAMILIES[0].edges,
    ) != phase16.canonical_topology_hash(
        phase16.SEALED_FAMILIES[1].nodes,
        phase16.SEALED_FAMILIES[1].edges,
    )


def test_phase16_power_and_resource_envelope_are_frozen_and_feasible():
    report = phase16.build_phase16_report(generated_at=GENERATED_AT)
    power = report["preregistration"]["power_report"]
    budgets = report["preregistration"]["operational_budgets"]
    range_management = report["range_management"]

    assert power["required_informative_pairs_per_family"] == 13
    assert power["critical_wins_for_rejection"] == 10
    assert power["achieved_power"] >= 0.8
    assert power["max_primary_schedule_wall_hours_at_cell_ceiling"] <= 60.0
    assert budgets["max_active_ranges"] == 1
    assert budgets["max_live_cells_in_parallel"] == 1
    assert range_management["ludus_connection"] == "ludus_sagerepl"
    assert range_management["new_ludus_user_required_now"] is False


def test_phase16_authorization_manifests_are_exact_arm_blind_and_prebind_safe():
    report = phase16.build_phase16_report(generated_at=GENERATED_AT)
    manifests = report["sealed_family_manifests"]
    audits = report["authorization_freeze"]["family_manifest_audits"]
    invariance = report["authorization_freeze"]["arm_invariance_audits"]
    paired_freezes = report["authorization_freeze"]["paired_arm_freezes"]
    coverage = report["authorization_freeze"]["coverage_manifest"]

    assert all(item["passes"] is True for item in audits)
    assert all(item["passes"] is True for item in invariance)
    assert all(item["passes"] is True for item in paired_freezes)
    assert report["authorization_freeze"]["authorization_dependency_audit"]["passes"] is True
    assert all(manifest["authorization_manifest"]["callbacks"][0]["callback_id"] == "" for manifest in manifests.values())
    assert all(
        set(manifest["authorization_manifest"]["allowed_targets"])
        == {"hosts", "domains", "principals", "directory_objects", "trust_edges"}
        for manifest in manifests.values()
    )
    assert coverage["coverage_status"] == "frozen_pending_phase17_final_boundary_attestation"
    assert coverage["phase18_unseal_authorized"] is False
    assert all(path["activation_authorized"] is False for path in coverage["paths"])


def test_phase16_writer_emits_portable_hashed_artifacts(tmp_path):
    result = phase16.write_phase16_artifacts(
        output_path=tmp_path / "portfolio.json",
        coverage_output_path=tmp_path / "coverage.json",
        family_manifest_paths={
            "sealed-family-s1": tmp_path / "s1.json",
            "sealed-family-s2": tmp_path / "s2.json",
        },
        generated_at=GENERATED_AT,
    )

    assert result["report"]["passes_gate"] is True
    for record in (
        result["written_artifacts"]["portfolio_report"],
        result["written_artifacts"]["coverage_manifest"],
        *result["written_artifacts"]["family_manifests"].values(),
    ):
        path = tmp_path / Path(record["path"]).name
        sidecar = tmp_path / Path(record["sidecar_path"]).name
        assert path.exists()
        assert sidecar.exists()
        assert record["sha256"] in sidecar.read_text()
