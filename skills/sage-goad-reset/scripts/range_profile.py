#!/usr/bin/env python3
"""Strict, source-controlled substrate profiles for Sage reset/readiness tooling."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG = REPO_ROOT / "ludus" / "sage-purpose-ranges" / "substrate_profiles.json"
CATALOG_SCHEMA = "sage-range-profile-catalog-v1"
DEFAULT_PROFILE_ID = "goad-trust-walker"
QUALIFICATION_STATES = {"live_qualified", "historically_observed", "defined_only"}
_PROFILE_KEYS = {
    "id", "source", "qualification", "network", "vms", "topology", "foothold", "bindings"
}
_VM_KEYS = {"role", "aliases", "suffixes", "last_octet", "os"}
_FOOTHOLD_KEYS = {"role", "principal", "callback_host", "callback_user", "password_env"}
_BINDING_KEYS = {"scenario", "snapshot"}


class RangeProfileError(ValueError):
    pass


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise RangeProfileError(f"{label} must contain exactly: {', '.join(sorted(expected))}")
    return value


def _strings(value: Any, label: str, *, empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not empty):
        raise RangeProfileError(f"{label} must be a {'possibly empty ' if empty else 'non-empty '}list")
    items = [str(item).strip().casefold() for item in value]
    if any(not item for item in items) or len(items) != len(set(items)):
        raise RangeProfileError(f"{label} contains an empty or duplicate value")
    return items


def topology_identity(profile: dict[str, Any]) -> str:
    topology = profile["topology"]
    normalized = {
        "capabilities": sorted(_strings(topology["capabilities"], "topology.capabilities")),
        "branches": sorted(_strings(topology["branches"], "topology.branches")),
    }
    encoded = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_profile(raw: Any) -> dict[str, Any]:
    profile = _exact_keys(raw, _PROFILE_KEYS, "profile")
    profile_id = str(profile["id"] or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", profile_id):
        raise RangeProfileError("profile.id must be a lowercase slug")
    source = _exact_keys(profile["source"], {"kind", "path"}, f"{profile_id}.source")
    if source["kind"] not in {"repo", "external"}:
        raise RangeProfileError(f"{profile_id}.source.kind is unsupported")
    source_path = str(source["path"] or "").strip()
    if not source_path or Path(source_path).is_absolute() or source_path.startswith(("~", "Plans/", "Notes/", ".sage_history/")):
        raise RangeProfileError(f"{profile_id}.source.path must be a publishable relative locator")
    resolved_source = (REPO_ROOT / source_path).resolve()
    if not resolved_source.is_relative_to(REPO_ROOT):
        raise RangeProfileError(f"{profile_id}.source.path escapes the repository")
    if source["kind"] == "repo" and not resolved_source.exists():
        raise RangeProfileError(f"{profile_id}.source.path does not exist: {source_path}")
    qualification = _exact_keys(
        profile["qualification"], {"state", "receipt"}, f"{profile_id}.qualification"
    )
    if qualification["state"] not in QUALIFICATION_STATES:
        raise RangeProfileError(f"{profile_id}.qualification.state is unsupported")
    receipt = str(qualification["receipt"] or "").strip()
    if qualification["state"] == "live_qualified" and not receipt:
        raise RangeProfileError(f"{profile_id} is live-qualified without a receipt")
    network = _exact_keys(
        profile["network"], {"first_octet", "vlan", "range_number_octet"}, f"{profile_id}.network"
    )
    for key in ("first_octet", "vlan"):
        if type(network[key]) is not int or not 0 <= network[key] <= 255:
            raise RangeProfileError(f"{profile_id}.network.{key} must be an IP octet")
    if network["range_number_octet"] is not True:
        raise RangeProfileError(f"{profile_id}.network.range_number_octet must be true")
    if not isinstance(profile["vms"], list) or not profile["vms"]:
        raise RangeProfileError(f"{profile_id}.vms must be non-empty")
    roles: set[str] = set()
    matchers: set[str] = set()
    octets: set[int] = set()
    for raw_vm in profile["vms"]:
        vm = _exact_keys(raw_vm, _VM_KEYS, f"{profile_id}.vm")
        role = str(vm["role"] or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", role) or role in roles:
            raise RangeProfileError(f"{profile_id} has an invalid or duplicate VM role")
        roles.add(role)
        aliases = _strings(vm["aliases"], f"{profile_id}.{role}.aliases")
        suffixes = _strings(vm["suffixes"], f"{profile_id}.{role}.suffixes", empty=True)
        if matchers.intersection(aliases + suffixes):
            raise RangeProfileError(f"{profile_id} has ambiguous VM name matchers")
        matchers.update(aliases + suffixes)
        if type(vm["last_octet"]) is not int or not 1 <= vm["last_octet"] <= 254 or vm["last_octet"] in octets:
            raise RangeProfileError(f"{profile_id}.{role}.last_octet is invalid or duplicate")
        octets.add(vm["last_octet"])
        if vm["os"] not in {"windows", "linux"}:
            raise RangeProfileError(f"{profile_id}.{role}.os is unsupported")
    _exact_keys(profile["topology"], {"capabilities", "branches"}, f"{profile_id}.topology")
    _strings(profile["topology"]["capabilities"], f"{profile_id}.topology.capabilities")
    _strings(profile["topology"]["branches"], f"{profile_id}.topology.branches")
    foothold = _exact_keys(profile["foothold"], _FOOTHOLD_KEYS, f"{profile_id}.foothold")
    if str(foothold["role"] or "").strip().casefold() not in roles:
        raise RangeProfileError(f"{profile_id}.foothold.role is unknown")
    if any(not str(foothold[key] or "").strip() for key in _FOOTHOLD_KEYS - {"role"}):
        raise RangeProfileError(f"{profile_id}.foothold fields must be non-empty")
    if not isinstance(profile["bindings"], list):
        raise RangeProfileError(f"{profile_id}.bindings must be a list")
    scenarios: set[str] = set()
    for raw_binding in profile["bindings"]:
        binding = _exact_keys(raw_binding, _BINDING_KEYS, f"{profile_id}.binding")
        scenario = str(binding["scenario"] or "").strip()
        snapshot = str(binding["snapshot"] or "").strip()
        if not scenario or scenario in scenarios or not snapshot:
            raise RangeProfileError(f"{profile_id} has an invalid or duplicate scenario binding")
        scenarios.add(scenario)
    return profile


def load_catalog(path: str | Path = DEFAULT_CATALOG) -> dict[str, dict[str, Any]]:
    catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    _exact_keys(catalog, {"schema", "profiles"}, "catalog")
    if catalog["schema"] != CATALOG_SCHEMA or not isinstance(catalog["profiles"], list):
        raise RangeProfileError(f"unsupported range-profile catalog schema: {catalog.get('schema')!r}")
    profiles = [_validate_profile(item) for item in catalog["profiles"]]
    by_id = {item["id"]: item for item in profiles}
    if len(by_id) != len(profiles) or DEFAULT_PROFILE_ID not in by_id:
        raise RangeProfileError("profile IDs must be unique and include the default GOAD profile")
    return by_id


def get_profile(profile: str | dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(profile, dict):
        return _validate_profile(profile)
    profile_id = str(profile or DEFAULT_PROFILE_ID).strip()
    try:
        return load_catalog()[profile_id]
    except KeyError as exc:
        raise RangeProfileError(f"unknown range profile: {profile_id}") from exc


def canonical_vm_name(value: Any, profile: dict[str, Any]) -> str | None:
    normalized = str(value or "").strip().casefold()
    for vm in profile["vms"]:
        if normalized in {str(item).casefold() for item in vm["aliases"]}:
            return vm["role"]
        for suffix in vm["suffixes"]:
            suffix = str(suffix).casefold()
            if normalized.endswith(f"-{suffix}") and normalized != suffix:
                return vm["role"]
    return None


def expected_ips(profile: dict[str, Any], range_number: Any) -> dict[str, str]:
    try:
        octet = int(range_number)
    except (TypeError, ValueError) as exc:
        raise RangeProfileError("rangeNumber must be an IP octet") from exc
    if not 0 <= octet <= 255:
        raise RangeProfileError("rangeNumber must be an IP octet")
    network = profile["network"]
    return {
        vm["role"]: f"{network['first_octet']}.{octet}.{network['vlan']}.{vm['last_octet']}"
        for vm in profile["vms"]
    }


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": profile["id"],
        "qualification": dict(profile["qualification"]),
        "topology_identity": topology_identity(profile),
        "scenarios": [item["scenario"] for item in profile["bindings"]],
    }


def require_scenario(profile: dict[str, Any], scenario: str) -> dict[str, Any]:
    for binding in profile["bindings"]:
        if binding["scenario"] == scenario:
            return binding
    raise RangeProfileError(f"scenario {scenario!r} is not declared by range profile {profile['id']!r}")


def foothold_mismatches(
    profile: dict[str, Any], *, host: str, ip: str, principal: str,
    callback_host: str, callback_user: str, password_env: str,
) -> list[str]:
    expected = profile["foothold"]
    mismatches: list[str] = []
    if canonical_vm_name(host, profile) != expected["role"]:
        mismatches.append("inventory role")
    for key, label, actual in (
        ("principal", "principal", principal),
        ("callback_host", "callback host", callback_host),
        ("callback_user", "callback user", callback_user),
        ("password_env", "password environment", password_env),
    ):
        if str(actual).strip().casefold() != str(expected[key]).strip().casefold():
            mismatches.append(label)
    vm = next(item for item in profile["vms"] if item["role"] == expected["role"])
    try:
        octets = [int(item) for item in str(ip).split(".")]
    except ValueError:
        octets = []
    network = profile["network"]
    if len(octets) != 4 or (octets[0], octets[2], octets[3]) != (
        network["first_octet"], network["vlan"], vm["last_octet"]
    ):
        mismatches.append("IP role")
    return mismatches


def require_live_qualified(profile: dict[str, Any]) -> None:
    if profile["qualification"]["state"] != "live_qualified":
        raise RangeProfileError(
            f"range profile {profile['id']!r} is {profile['qualification']['state']}, not live-qualified"
        )
