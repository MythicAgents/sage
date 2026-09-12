from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "range_profile.py"
SPEC = importlib.util.spec_from_file_location("range_profile", SCRIPT)
profile = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(profile)


def test_catalog_inventories_distinct_source_controlled_topologies():
    catalog = profile.load_catalog()

    assert set(catalog) == {
        "goad-trust-walker",
        "sage-replication-range",
        "sage-structural-a",
        "sage-structural-b",
    }
    assert len({profile.topology_identity(item) for item in catalog.values()}) == 4
    assert catalog["goad-trust-walker"]["qualification"]["state"] == "live_qualified"
    assert profile.require_scenario(
        catalog["goad-trust-walker"], "trust-context-parent-dcsync"
    )["snapshot"] == "sage-seed-apollo-staged-20260710"
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
