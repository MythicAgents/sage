from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SAGE_PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"
sys.path.insert(0, str(SAGE_PACKAGE_ROOT))
from ai.hillclimb.scenarios import structural_scenario_manifests


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "range_profile.py"
SPEC = importlib.util.spec_from_file_location("range_profile", SCRIPT)
profile = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(profile)


def test_catalog_inventories_distinct_source_controlled_topologies():
    catalog = profile.load_catalog()

    assert set(catalog) == {
        "bsl-ad-elastic",
        "goad-trust-walker",
        "sage-replication-range",
        "sage-structural-a",
        "sage-structural-b",
    }
    assert len({profile.topology_identity(item) for item in catalog.values()}) == 5
    assert catalog["goad-trust-walker"]["qualification"]["state"] == "live_qualified"
    assert profile.require_scenario(
        catalog["goad-trust-walker"], "trust-context-parent-dcsync"
    )["snapshot"] == "sage-seed-apollo-staged-20260710"
    assert profile.require_scenario(
        catalog["goad-trust-walker"], "cross-forest-objective"
    )["snapshot"] == "sage-seed-apollo-v252-mythic4-20260829"
    assert catalog["sage-replication-range"]["qualification"] == {
        "state": "live_qualified",
        "receipt": "gate1c-replication-live-qualification-v1",
    }
    assert "abuse-child-domain-gpo" in catalog["sage-structural-a"]["topology"]["capabilities"]
    assert "read-partner-managed-local-admin-secret" in catalog["sage-structural-b"]["topology"]["capabilities"]
    assert all(
        (profile.REPO_ROOT / item["source"]["path"]).exists()
        for item in catalog.values()
        if item["source"]["kind"] == "repo"
    )


def test_topology_identity_ignores_names_and_host_inventory():
    original = profile.get_profile("sage-structural-a")
    renamed = copy.deepcopy(original)
    renamed["id"] = "renamed-range"
    renamed["vms"][0]["aliases"] = ["different-router"]

    assert profile.topology_identity(renamed) == profile.topology_identity(original)


def test_structural_count_uses_canonical_required_capabilities_not_names_or_branches():
    original = profile.get_profile("sage-structural-a")
    renamed = copy.deepcopy(original)
    renamed["topology"]["capabilities"] = [
        "collect-directory-graph",
        "abuse-controlled-gpo",
        "replicate-directory-secrets",
        "abuse-certificate-services",
        "cross-forest-authentication",
    ]
    renamed["topology"]["branches"] = ["renamed-a", "renamed-b", "extra-label"]

    assert profile.available_capability_set(renamed) == profile.available_capability_set(original)
    assert profile.required_capability_set(renamed) == profile.required_capability_set(original)
    assert profile.structural_capability_identity(renamed) is None
    assert profile.structural_capability_identity(original) is None


def test_selected_three_profiles_derive_distinct_source_bounded_required_sets():
    catalog = profile.load_catalog()
    selected = {
        item: profile.structural_capability_identity(
            catalog[item], profile.required_route_scenario(catalog[item]),
        )
        for item in ("goad-trust-walker", "sage-replication-range", "bsl-ad-elastic")
    }

    assert all(selected.values())
    assert len(set(selected.values())) == 3
    assert profile.required_capability_set(catalog["goad-trust-walker"]) == {
        "establish-or-use-authority-across-independent-forest-boundary",
        "obtain-fresh-objective-domain-krbtgt-material",
    }
    assert profile.required_capability_set(catalog["sage-replication-range"]) == {
        "obtain-fresh-objective-domain-krbtgt-material",
    }
    assert profile.required_capability_set(catalog["bsl-ad-elastic"]) == {
        "certificate-based-domain-admin-authentication",
        "privileged-directory-access-differential",
    }


def test_goad_boundary_semantic_is_causal_without_trust_specific_or_optional_steps():
    goad = copy.deepcopy(profile.get_profile("goad-trust-walker"))
    routes = profile.declared_required_routes(goad)

    assert any("traverse-cross-forest-sql-link" in route for route in routes)
    assert not any("cross-forest-authentication" in route for route in routes)
    assert "collect-directory-graph" not in profile.required_capability_set(goad)
    assert "abuse-controlled-gpo" not in profile.required_capability_set(goad)
    assert "replicate-directory-secrets" not in profile.required_capability_set(goad)

    missing_crossing = copy.deepcopy(goad)
    missing_crossing["topology"]["required_routes"][1].remove(
        "establish-or-use-authority-across-independent-forest-boundary"
    )
    missing_crossing = profile.get_profile(missing_crossing)

    assert (
        "establish-or-use-authority-across-independent-forest-boundary"
        not in profile.required_capability_set(missing_crossing)
    )
    assert (
        profile.structural_capability_identity(goad, "cross-forest-objective")
        != profile.structural_capability_identity(missing_crossing, "cross-forest-objective")
    )


def test_replication_direct_route_makes_graph_and_gpo_optional_but_keeps_fresh_krbtgt():
    selected = profile.get_profile("sage-replication-range")

    assert profile.required_capability_set(selected) == {
        "obtain-fresh-objective-domain-krbtgt-material",
    }
    assert profile.declared_required_routes(selected)[1] == {
        "obtain-fresh-objective-domain-krbtgt-material",
    }


def test_bsl_ca_key_alternative_keeps_certificate_proof_but_not_enrollment():
    selected = profile.get_profile("bsl-ad-elastic")

    assert "enroll-certificate-for-admin-authentication" not in profile.required_capability_set(selected)
    assert profile.declared_required_routes(selected)[1] == {
        "use-ca-signing-key-to-issue-admin-certificate",
        "certificate-based-domain-admin-authentication",
        "privileged-directory-access-differential",
    }

    invalid_alternative = copy.deepcopy(selected)
    invalid_alternative["topology"]["required_routes"][1].remove(
        "certificate-based-domain-admin-authentication"
    )
    invalid_alternative = profile.get_profile(invalid_alternative)

    assert (
        "certificate-based-domain-admin-authentication"
        not in profile.required_capability_set(invalid_alternative)
    )
    assert (
        profile.structural_capability_identity(selected, "bsl-certificate-domain-control")
        != profile.structural_capability_identity(
            invalid_alternative, "bsl-certificate-domain-control",
        )
    )


def test_alternate_routes_use_their_intersection_and_require_complete_coverage_for_n():
    candidate = copy.deepcopy(profile.get_profile("sage-structural-a"))
    candidate["topology"]["required_route_coverage"] = "complete"
    same_required_set = copy.deepcopy(candidate)
    same_required_set["topology"]["capabilities"] = [
        "collect-directory-graph", "abuse-controlled-gpo", "replicate-directory-secrets",
        "abuse-certificate-services", "cross-forest-authentication", "extra-available-mechanism",
    ]
    same_required_set["topology"]["required_routes"][0].append("extra-available-mechanism")
    distinct_required_set = copy.deepcopy(candidate)
    distinct_required_set["topology"]["required_routes"] = [[
        "collect-directory-graph", "abuse-controlled-gpo",
    ], ["collect-directory-graph", "abuse-controlled-gpo", "replicate-directory-secrets"]]
    distinct_required_set["topology"]["capabilities"] = [
        "collect-directory-graph", "abuse-controlled-gpo", "replicate-directory-secrets",
    ]

    assert profile.required_capability_set(candidate) == {"collect-directory-graph"}
    assert profile.required_capability_set(same_required_set) == {"collect-directory-graph"}
    assert profile.structural_capability_identity(
        candidate, "structural-a-frontier",
    ) == profile.structural_capability_identity(
        same_required_set, "structural-a-frontier",
    )
    assert profile.required_capability_set(distinct_required_set) == {
        "collect-directory-graph", "abuse-controlled-gpo",
    }
    assert profile.structural_capability_identity(
        candidate, "structural-a-frontier",
    ) != profile.structural_capability_identity(
        distinct_required_set, "structural-a-frontier",
    )


def test_structural_manifests_bind_profiles_sources_routes_and_current_readiness():
    manifests = structural_scenario_manifests()

    assert manifests[0].snapshot_id == "sage-seed-apollo-v252-mythic4-20260829"

    assert [manifest.scenario_id for manifest in manifests] == [
        "cross-forest-objective",
        "bsl-certificate-domain-control",
        "replication-purpose-range-visible-cost",
        "structural-a-frontier",
        "structural-b-frontier",
    ]
    for manifest in manifests[1:]:
        report = profile.validate_scenario_manifest(manifest.as_mapping())
        assert report["manifest_valid"] is True
        assert report["live_ready"] is False
    by_profile = {
        item.profile_id: profile.validate_scenario_manifest(item.as_mapping())
        for item in manifests[1:]
    }
    assert by_profile["bsl-ad-elastic"]["structural_count_eligible"] is True
    assert by_profile["bsl-ad-elastic"]["readiness_blockers"] == [
        "snapshot_unallocated", "profile_not_live_qualified",
    ]
    assert by_profile["sage-replication-range"]["structural_count_eligible"] is True
    assert by_profile["sage-replication-range"]["reset_prerequisites_ready"] is True
    assert by_profile["sage-replication-range"]["readiness_blockers"] == []
    for profile_id in ("sage-structural-a", "sage-structural-b"):
        assert by_profile[profile_id]["reset_prerequisites_ready"] is False
        assert by_profile[profile_id]["structural_count_eligible"] is False
        assert by_profile[profile_id]["readiness_blockers"] == [
            "snapshot_unallocated", "profile_not_live_qualified",
        ]


def test_goad_structural_identity_is_bound_only_to_cross_forest_case(tmp_path):
    goad = profile.get_profile("goad-trust-walker")
    manifest = next(
        item.as_mapping() for item in structural_scenario_manifests()
        if item.profile_id == "goad-trust-walker"
    )
    external = tmp_path / "configured-goad"
    external.mkdir()
    (external / "source.yml").write_text("exact synthetic source\n", encoding="utf-8")
    digest = hashlib.sha256()
    item = external / "source.yml"
    digest.update(item.relative_to(external).as_posix().encode())
    digest.update(b"\0")
    digest.update(hashlib.sha256(item.read_bytes()).digest())
    manifest["source_sha256"] = digest.hexdigest()

    exact = profile.validate_scenario_manifest(manifest, external_source_dir=external)
    wrong_case = copy.deepcopy(manifest)
    wrong_case["scenario_id"] = "child-control"
    wrong = profile.validate_scenario_manifest(wrong_case, external_source_dir=external)

    assert exact["manifest_valid"] is True
    assert exact["structural_count_eligible"] is True
    assert profile.structural_capability_identity(goad, "cross-forest-objective")
    assert profile.structural_capability_identity(goad, "child-control") is None
    assert "required_route_scope_mismatch" in wrong["errors"]
    assert wrong["structural_count_eligible"] is False


def test_external_goad_manifest_fails_closed_without_explicit_source_path():
    manifest = next(
        item.as_mapping() for item in structural_scenario_manifests()
        if item.profile_id == "goad-trust-walker"
    )

    report = profile.validate_scenario_manifest(manifest)

    assert report["errors"] == ["external_source_unverified"]
    assert report["structural_count_eligible"] is False


def test_route_union_cannot_be_submitted_as_the_required_set():
    manifest = next(
        item.as_mapping() for item in structural_scenario_manifests()
        if item.profile_id == "sage-structural-a"
    )
    manifest["required_capabilities"] = manifest["available_capabilities"]

    report = profile.validate_scenario_manifest(manifest)

    assert "required_capability_set_mismatch" in report["errors"]
    assert report["structural_count_eligible"] is False


def test_legacy_route_union_manifest_remains_bindable_but_never_counts_structurally():
    manifest = next(
        item.as_mapping() for item in structural_scenario_manifests()
        if item.profile_id == "sage-structural-a"
    )
    manifest["schema"] = profile.LEGACY_SCENARIO_RESET_MANIFEST_SCHEMA
    manifest["required_capabilities"] = manifest.pop("available_capabilities")
    manifest.pop("required_route_coverage")

    report = profile.validate_scenario_manifest(manifest)

    assert report["manifest_valid"] is True
    assert report["route_semantics"] == "legacy_availability_only"
    assert report["structural_count_eligible"] is False


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda manifest: {**manifest, "source_sha256": "0" * 64}, "source_hash_mismatch"),
        (lambda manifest: {**manifest, "profile_id": "goad-trust-walker"}, "profile_mismatch"),
        (lambda manifest: {**manifest, "snapshot_id": "invented-snapshot"}, "snapshot_binding_mismatch"),
        (lambda manifest: None, "manifest_missing"),
    ],
    ids=("stale-source", "wrong-profile", "wrong-snapshot", "missing-manifest"),
)
def test_structural_manifest_fails_closed_on_stale_or_mismatched_binding(mutator, expected):
    manifest = next(
        item.as_mapping() for item in structural_scenario_manifests()
        if item.profile_id == "bsl-ad-elastic"
    )
    report = profile.validate_scenario_manifest(mutator(manifest))

    assert expected in report["errors"]
    assert report["live_ready"] is False


def test_qualification_observation_allows_only_private_allocation_from_unallocated_default():
    bsl_manifest = next(
        item.as_mapping()
        for item in structural_scenario_manifests()
        if item.profile_id == "bsl-ad-elastic"
    )
    bsl_manifest["snapshot_id"] = "private-allocation-snapshot"

    default_report = profile.validate_scenario_manifest(bsl_manifest)
    observation_report = profile.validate_scenario_manifest(
        bsl_manifest, qualification_observation=True,
    )

    assert "snapshot_binding_mismatch" in default_report["errors"]
    assert observation_report["manifest_valid"] is True
    assert observation_report["live_ready"] is False

    replication = profile.get_profile("sage-replication-range")
    bound_snapshot = replication["bindings"][0]["snapshot"]
    concrete_manifest = {
        "schema": profile.SCENARIO_RESET_MANIFEST_SCHEMA,
        "scenario_id": replication["bindings"][0]["scenario"],
        "profile_id": replication["id"],
        "source_path": replication["source"]["path"],
        "source_sha256": profile.source_tree_sha256(replication["source"]["path"]),
        "available_capabilities": sorted(profile.available_capability_set(replication)),
        "required_capabilities": sorted(profile.required_capability_set(replication)),
        "required_routes": [sorted(route) for route in profile.declared_required_routes(replication)],
        "required_route_coverage": profile.required_route_coverage(replication),
        "oracle_dependencies": ["mythic-task-output-or-artifact"],
        "snapshot_id": "wrong-concrete-snapshot",
        "reset_checkpoint_schema": "sage-goad-reset-orchestration-v2",
    }
    assert bound_snapshot != concrete_manifest["snapshot_id"]
    assert "snapshot_binding_mismatch" in profile.validate_scenario_manifest(
        concrete_manifest, qualification_observation=True,
    )["errors"]


def test_qualified_profile_manifest_cannot_prove_live_readiness_without_reset_evidence():
    selected = profile.get_profile("sage-replication-range")
    source_path = selected["source"]["path"]
    manifest = {
        "schema": profile.SCENARIO_RESET_MANIFEST_SCHEMA,
        "scenario_id": "replication-purpose-range-visible-cost",
        "profile_id": selected["id"],
        "source_path": source_path,
        "source_sha256": profile.source_tree_sha256(source_path),
        "available_capabilities": sorted(profile.available_capability_set(selected)),
        "required_capabilities": sorted(profile.required_capability_set(selected)),
        "required_routes": [sorted(route) for route in profile.declared_required_routes(selected)],
        "required_route_coverage": profile.required_route_coverage(selected),
        "oracle_dependencies": ["mythic-task-output-or-artifact"],
        "snapshot_id": profile.require_scenario(
            selected, "replication-purpose-range-visible-cost"
        )["snapshot"],
        "reset_checkpoint_schema": "sage-goad-reset-orchestration-v2",
    }

    report = profile.validate_scenario_manifest(manifest)

    assert report["manifest_valid"] is True
    assert report["reset_prerequisites_ready"] is True
    assert report["live_ready"] is False


def test_profile_matches_exact_alias_or_delimited_suffix_only():
    selected = profile.get_profile("sage-replication-range")

    assert profile.canonical_vm_name("SAGEREPL-WS01", selected) == "ws01"
    assert profile.canonical_vm_name("WS01", selected) == "ws01"
    assert profile.canonical_vm_name("SAGEREPL-WS011", selected) is None
    assert profile.canonical_vm_name("NOTWS01", selected) is None


def test_expected_ips_derive_from_profile_and_live_range_number():
    selected = profile.get_profile("sage-replication-range")

    assert profile.expected_ips(selected, 7) == {
        "router": "10.7.10.254",
        "dc01": "10.7.10.10",
        "srv02": "10.7.10.22",
        "ws01": "10.7.10.31",
    }


def test_bsl_router_accepts_only_source_declared_multi_interface_addresses():
    selected = profile.get_profile("bsl-ad-elastic")

    assert profile.expected_ips(selected, 16)["router"] == "10.16.10.254"
    assert profile.accepted_ips(selected, 16)["router"] == {
        "10.16.10.254", "10.16.20.254", "10.16.99.254",
    }
    assert "10.16.20.253" not in profile.accepted_ips(selected, 16)["router"]


def test_per_vm_vlan_override_allows_distinct_effective_addresses_and_drives_foothold_binding():
    selected = copy.deepcopy(profile.get_profile("sage-replication-range"))
    selected["vms"][-1]["last_octet"] = 10
    selected["vms"][-1]["vlan"] = 20
    selected = profile.get_profile(selected)

    assert profile.expected_ips(selected, 7)["ws01"] == "10.7.20.10"
    assert profile.foothold_mismatches(
        selected,
        host="SAGEREPL-WS01",
        ip="10.7.20.10",
        principal=selected["foothold"]["principal"],
        callback_host="WS01",
        callback_user="user1",
        password_env="SAGE_REPLICATION_USER1_PASSWORD",
    ) == []
    assert profile.foothold_mismatches(
        selected,
        host="SAGEREPL-WS01",
        ip="10.7.10.10",
        principal=selected["foothold"]["principal"],
        callback_host="WS01",
        callback_user="user1",
        password_env="SAGE_REPLICATION_USER1_PASSWORD",
    ) == ["IP role"]


def test_duplicate_effective_vm_ip_is_rejected_even_when_vm_names_differ():
    selected = copy.deepcopy(profile.get_profile("sage-replication-range"))
    selected["vms"][-1]["last_octet"] = 10

    with pytest.raises(profile.RangeProfileError, match="duplicate effective IP"):
        profile.get_profile(selected)


@pytest.mark.parametrize("vlan", [True, -1, 256, "20"])
def test_per_vm_vlan_must_be_an_ip_octet(vlan):
    selected = copy.deepcopy(profile.get_profile("sage-replication-range"))
    selected["vms"][-1]["vlan"] = vlan

    with pytest.raises(profile.RangeProfileError, match=r"ws01\.vlan must be an IP octet"):
        profile.get_profile(selected)


def test_foothold_binding_requires_role_network_and_exact_identity():
    selected = profile.get_profile("sage-replication-range")

    assert profile.foothold_mismatches(
        selected,
        host="SAGEREPL-WS01",
        ip="10.7.10.31",
        principal=r"REPLICATION\user1",
        callback_host="WS01",
        callback_user="user1",
        password_env="SAGE_REPLICATION_USER1_PASSWORD",
    ) == []
    assert profile.foothold_mismatches(
        selected,
        host="SAGEREPL-SRV02",
        ip="10.7.10.22",
        principal=r"REPLICATION\user1",
        callback_host="WS01",
        callback_user="user1",
        password_env="SAGE_REPLICATION_USER1_PASSWORD",
    ) == ["inventory role", "IP role"]


def test_unknown_schema_and_profile_fail_closed(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"schema": "future", "profiles": []}), encoding="utf-8")

    with pytest.raises(profile.RangeProfileError, match="unsupported"):
        profile.load_catalog(path)
    with pytest.raises(profile.RangeProfileError, match="unknown range profile"):
        profile.get_profile("missing")


def test_nonqualified_profile_cannot_release_live_use():
    with pytest.raises(profile.RangeProfileError, match="not live-qualified"):
        profile.require_live_qualified(profile.get_profile("sage-structural-b"))


def test_incompatible_scenario_is_rejected():
    with pytest.raises(profile.RangeProfileError, match="is not declared"):
        profile.require_scenario(profile.get_profile("sage-structural-a"), "child-domain-control")
