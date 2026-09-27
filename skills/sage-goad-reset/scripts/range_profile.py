#!/usr/bin/env python3
"""Strict, source-controlled substrate profiles for Sage reset/readiness tooling."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG = REPO_ROOT / "ludus" / "sage-purpose-ranges" / "substrate_profiles.json"
CATALOG_SCHEMA = "sage-range-profile-catalog-v1"
DEFAULT_PROFILE_ID = "goad-trust-walker"
QUALIFICATION_STATES = {"live_qualified", "historically_observed", "defined_only"}
_PROFILE_KEYS = {
    "id", "source", "qualification", "network", "vms", "topology", "foothold", "bindings"
}
_VM_KEYS = {"role", "aliases", "suffixes", "last_octet", "os"}
_VM_OPTIONAL_KEYS = {"vlan", "accepted_vlans"}
_FOOTHOLD_KEYS = {"role", "principal", "callback_host", "callback_user", "password_env"}
_BINDING_KEYS = {"scenario", "snapshot"}
SCENARIO_RESET_MANIFEST_SCHEMA = "sage-scenario-reset-manifest-v2"
LEGACY_SCENARIO_RESET_MANIFEST_SCHEMA = "sage-scenario-reset-manifest-v1"
UNALLOCATED_SNAPSHOT = "unallocated"
_SCENARIO_MANIFEST_KEYS = {
    "schema", "scenario_id", "profile_id", "source_path", "source_sha256",
    "available_capabilities", "required_capabilities", "required_routes",
    "required_route_coverage", "oracle_dependencies", "snapshot_id",
    "reset_checkpoint_schema",
}
_LEGACY_SCENARIO_MANIFEST_KEYS = {
    "schema", "scenario_id", "profile_id", "source_path", "source_sha256",
    "required_capabilities", "required_routes", "oracle_dependencies", "snapshot_id",
    "reset_checkpoint_schema",
}
_TOPOLOGY_REQUIRED_KEYS = {"capabilities", "branches"}
_TOPOLOGY_OPTIONAL_KEYS = {
    "required_routes", "required_route_coverage", "required_route_scenario",
}
_REQUIRED_ROUTE_COVERAGE = {
    "complete", "incomplete", "not_applicable", "legacy_availability_only",
}
_MECHANICAL_CAPABILITY_ALIASES = {
    "abuse-child-domain-gpo": "abuse-controlled-gpo",
    "replicate-child-domain-secrets": "replicate-directory-secrets",
    "replicate-nested-child-secrets": "replicate-directory-secrets",
    "abuse-partner-certificate-services": "abuse-certificate-services",
    "read-partner-managed-local-admin-secret": "read-managed-local-admin-secret",
}


class RangeProfileError(ValueError):
    pass


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise RangeProfileError(f"{label} must contain exactly: {', '.join(sorted(expected))}")
    return value


def _keys_with_optional(
    value: Any, required: set[str], optional: set[str], label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not required.issubset(value) or not set(value).issubset(required | optional):
        allowed = ", ".join(sorted(required | optional))
        raise RangeProfileError(f"{label} must contain required keys and only: {allowed}")
    return value


def _ip_octet(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 255:
        raise RangeProfileError(f"{label} must be an IP octet")
    return value


def _effective_vlan(vm: Mapping[str, Any], network: Mapping[str, Any]) -> int:
    return vm.get("vlan", network["vlan"])


def _accepted_vlans(vm: Mapping[str, Any], network: Mapping[str, Any]) -> tuple[int, ...]:
    """Return the primary address VLAN plus explicitly observed interfaces."""
    primary = _effective_vlan(vm, network)
    values = [primary, *(vm.get("accepted_vlans") or [])]
    return tuple(dict.fromkeys(values))


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


def _mechanical_capability(value: Any) -> str:
    capability = str(value or "").strip().casefold()
    return _MECHANICAL_CAPABILITY_ALIASES.get(capability, capability)


def available_capability_set(profile: Mapping[str, Any]) -> frozenset[str]:
    """Canonical union of capabilities available on a profile's known routes."""
    topology = profile.get("topology") if isinstance(profile, Mapping) else None
    raw = topology.get("capabilities") if isinstance(topology, Mapping) else None
    capabilities = _strings(raw, "topology.capabilities")
    return frozenset(_mechanical_capability(item) for item in capabilities)


def required_route_coverage(profile: Mapping[str, Any]) -> str:
    """Return whether declared routes are complete for one fixed start/objective."""
    topology = profile.get("topology") if isinstance(profile, Mapping) else None
    if not isinstance(topology, Mapping):
        raise RangeProfileError("topology is missing")
    return str(topology.get("required_route_coverage") or "legacy_availability_only")


def required_route_scenario(profile: Mapping[str, Any]) -> str | None:
    """Case whose fixed start/objective gives the declared routes their meaning."""
    topology = profile.get("topology") if isinstance(profile, Mapping) else None
    if not isinstance(topology, Mapping):
        raise RangeProfileError("topology is missing")
    scenario = str(topology.get("required_route_scenario") or "").strip()
    return scenario or None


def declared_required_routes(profile: Mapping[str, Any]) -> tuple[frozenset[str], ...]:
    """Known route sets; these are not complete unless coverage is ``complete``."""
    topology = profile.get("topology") if isinstance(profile, Mapping) else None
    if not isinstance(topology, Mapping):
        raise RangeProfileError("topology is missing")
    routes = topology.get("required_routes")
    if routes is None:
        return ()
    if not isinstance(routes, list):
        raise RangeProfileError("topology.required_routes must be a list")
    route_sets: list[frozenset[str]] = []
    for route in routes:
        values = _strings(route, "topology.required_route")
        route_sets.append(frozenset(_mechanical_capability(item) for item in values))
    return tuple(route_sets)


def required_capability_set(profile: Mapping[str, Any]) -> frozenset[str]:
    """Intersection of declared routes, never a countable set without complete coverage."""
    routes = declared_required_routes(profile)
    if not routes:
        return frozenset()
    return frozenset.intersection(*routes)


def structural_capability_identity(
    profile: Mapping[str, Any], scenario_id: str | None = None,
) -> str | None:
    """Hash a required set only when all valid routes have been declared."""
    if (
        required_route_coverage(profile) != "complete"
        or required_route_scenario(profile) != str(scenario_id or "").strip()
    ):
        return None
    required = required_capability_set(profile)
    if not required:
        return None
    encoded = json.dumps(sorted(required), separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def source_tree_sha256(source_path: str | Path) -> str:
    path = (REPO_ROOT / source_path).resolve()
    if not path.is_dir() or not path.is_relative_to(REPO_ROOT):
        raise RangeProfileError(f"source path is not a repository directory: {source_path}")
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(item.read_bytes()).digest())
    return digest.hexdigest()


def validate_scenario_manifest(
    manifest: Any, *, qualification_observation: bool = False,
    external_source_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Validate an offline structural manifest without turning it into live readiness."""
    errors: list[str] = []
    blockers: list[str] = []
    if not isinstance(manifest, Mapping):
        return {
            "manifest_valid": False,
            "reset_prerequisites_ready": False,
            "live_ready": False,
            "errors": ["manifest_missing"],
            "readiness_blockers": [],
        }
    schema = manifest.get("schema")
    legacy_schema = schema == LEGACY_SCENARIO_RESET_MANIFEST_SCHEMA
    expected_keys = _LEGACY_SCENARIO_MANIFEST_KEYS if legacy_schema else _SCENARIO_MANIFEST_KEYS
    if set(manifest) != expected_keys:
        errors.append("manifest_schema_fields_mismatch")
    if schema not in {SCENARIO_RESET_MANIFEST_SCHEMA, LEGACY_SCENARIO_RESET_MANIFEST_SCHEMA}:
        errors.append("manifest_schema_mismatch")
    profile_id = str(manifest.get("profile_id") or "").strip()
    scenario_id = str(manifest.get("scenario_id") or "").strip()
    source_path = str(manifest.get("source_path") or "").strip()
    snapshot_id = str(manifest.get("snapshot_id") or "").strip()
    try:
        profile = get_profile(profile_id)
    except ValueError:
        errors.append("profile_missing")
        profile = None
    if profile is not None:
        if profile["source"]["path"] != source_path:
            errors.append("profile_mismatch")
        try:
            binding = require_scenario(profile, scenario_id)
        except ValueError:
            errors.append("scenario_binding_missing")
            binding = None
        if binding is not None and binding["snapshot"] != snapshot_id:
            # A reusable public definition deliberately carries no allocated
            # snapshot. A private qualification observation may bind one
            # prospectively, but cannot override an already concrete profile
            # snapshot.
            allocation_override = (
                qualification_observation
                and binding["snapshot"] == UNALLOCATED_SNAPSHOT
                and snapshot_id != UNALLOCATED_SNAPSHOT
            )
            if not allocation_override:
                errors.append("snapshot_binding_mismatch")
        expected_available_capabilities = available_capability_set(profile)
        supplied_available_capabilities = frozenset(
            _mechanical_capability(item)
            for item in _strings(
                manifest.get("required_capabilities") if legacy_schema else manifest.get("available_capabilities"),
                "manifest.required_capabilities" if legacy_schema else "manifest.available_capabilities",
            )
        )
        if supplied_available_capabilities != expected_available_capabilities:
            errors.append("legacy_available_capability_set_mismatch" if legacy_schema else "available_capability_set_mismatch")
        if not legacy_schema:
            if required_route_scenario(profile) != scenario_id:
                errors.append("required_route_scope_mismatch")
            expected_capabilities = required_capability_set(profile)
            supplied_capabilities = frozenset(
                _mechanical_capability(item)
                for item in _strings(manifest.get("required_capabilities"), "manifest.required_capabilities")
            )
            if supplied_capabilities != expected_capabilities:
                errors.append("required_capability_set_mismatch")
        routes = manifest.get("required_routes")
        if not isinstance(routes, list) or not routes:
            errors.append("required_routes_missing")
        else:
            route_sets: list[frozenset[str]] = []
            for route in routes:
                try:
                    route_set = frozenset(
                        _mechanical_capability(item)
                        for item in _strings(route, "manifest.required_route")
                    )
                except RangeProfileError:
                    errors.append("required_route_invalid")
                    continue
                if not route_set.issubset(supplied_available_capabilities):
                    errors.append("required_route_outside_available_capability_set")
                route_sets.append(route_set)
            if route_sets and frozenset().union(*route_sets) != supplied_available_capabilities:
                errors.append("required_routes_do_not_cover_available_capability_set")
            if not legacy_schema and tuple(route_sets) != declared_required_routes(profile):
                errors.append("required_routes_profile_mismatch")
        supplied_coverage = "legacy_availability_only" if legacy_schema else str(
            manifest.get("required_route_coverage") or ""
        ).strip()
        if not legacy_schema and supplied_coverage != required_route_coverage(profile):
            errors.append("required_route_coverage_mismatch")
        if profile["source"]["kind"] == "repo":
            try:
                actual_source_hash = source_tree_sha256(source_path)
            except ValueError:
                errors.append("source_missing")
            else:
                if manifest.get("source_sha256") != actual_source_hash:
                    errors.append("source_hash_mismatch")
        elif external_source_dir is None:
            errors.append("external_source_unverified")
        else:
            external_path = Path(external_source_dir).expanduser().resolve()
            if not external_path.is_dir():
                errors.append("external_source_missing")
            else:
                digest = hashlib.sha256()
                for item in sorted(candidate for candidate in external_path.rglob("*") if candidate.is_file()):
                    digest.update(item.relative_to(external_path).as_posix().encode())
                    digest.update(b"\0")
                    digest.update(hashlib.sha256(item.read_bytes()).digest())
                if manifest.get("source_sha256") != digest.hexdigest():
                    errors.append("source_hash_mismatch")
        oracle_dependencies = manifest.get("oracle_dependencies")
        try:
            _strings(oracle_dependencies, "manifest.oracle_dependencies")
        except RangeProfileError:
            errors.append("oracle_dependencies_invalid")
        if profile["qualification"]["state"] != "live_qualified":
            blockers.append("profile_not_live_qualified")
    if snapshot_id == UNALLOCATED_SNAPSHOT:
        blockers.insert(0, "snapshot_unallocated")
    if not isinstance(manifest.get("reset_checkpoint_schema"), str) or not manifest["reset_checkpoint_schema"].strip():
        errors.append("reset_checkpoint_schema_missing")
    return {
        "manifest_valid": not errors,
        "reset_prerequisites_ready": not errors and not blockers,
        "structural_count_eligible": (
            not legacy_schema and not errors and profile is not None
            and required_route_coverage(profile) == "complete"
            and required_route_scenario(profile) == scenario_id
        ),
        "route_semantics": "legacy_availability_only" if legacy_schema else "declared_route_intersection",
        # A manifest lacks a reset receipt and independent outcome evidence.
        "live_ready": False,
        "errors": sorted(set(errors)),
        "readiness_blockers": blockers,
    }


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
        _ip_octet(network[key], f"{profile_id}.network.{key}")
    if network["range_number_octet"] is not True:
        raise RangeProfileError(f"{profile_id}.network.range_number_octet must be true")
    if not isinstance(profile["vms"], list) or not profile["vms"]:
        raise RangeProfileError(f"{profile_id}.vms must be non-empty")
    roles: set[str] = set()
    matchers: set[str] = set()
    effective_addresses: set[tuple[int, int]] = set()
    for raw_vm in profile["vms"]:
        vm = _keys_with_optional(raw_vm, _VM_KEYS, _VM_OPTIONAL_KEYS, f"{profile_id}.vm")
        role = str(vm["role"] or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", role) or role in roles:
            raise RangeProfileError(f"{profile_id} has an invalid or duplicate VM role")
        roles.add(role)
        aliases = _strings(vm["aliases"], f"{profile_id}.{role}.aliases")
        suffixes = _strings(vm["suffixes"], f"{profile_id}.{role}.suffixes", empty=True)
        if matchers.intersection(aliases + suffixes):
            raise RangeProfileError(f"{profile_id} has ambiguous VM name matchers")
        matchers.update(aliases + suffixes)
        if type(vm["last_octet"]) is not int or not 1 <= vm["last_octet"] <= 254:
            raise RangeProfileError(f"{profile_id}.{role}.last_octet is invalid")
        vlan = _ip_octet(_effective_vlan(vm, network), f"{profile_id}.{role}.vlan")
        accepted_vlans = vm.get("accepted_vlans")
        if accepted_vlans is not None:
            if not isinstance(accepted_vlans, list) or not accepted_vlans:
                raise RangeProfileError(f"{profile_id}.{role}.accepted_vlans must be a non-empty list")
            if any(type(item) is not int or not 0 <= item <= 255 for item in accepted_vlans):
                raise RangeProfileError(f"{profile_id}.{role}.accepted_vlans must contain IP octets")
            if len(set(accepted_vlans)) != len(accepted_vlans):
                raise RangeProfileError(f"{profile_id}.{role}.accepted_vlans contains duplicates")
            if vlan not in accepted_vlans:
                raise RangeProfileError(f"{profile_id}.{role}.accepted_vlans must include its primary VLAN")
        address = (vlan, vm["last_octet"])
        if address in effective_addresses:
            raise RangeProfileError(f"{profile_id}.{role} has a duplicate effective IP")
        effective_addresses.add(address)
        if vm["os"] not in {"windows", "linux"}:
            raise RangeProfileError(f"{profile_id}.{role}.os is unsupported")
    topology = _keys_with_optional(
        profile["topology"], _TOPOLOGY_REQUIRED_KEYS, _TOPOLOGY_OPTIONAL_KEYS,
        f"{profile_id}.topology",
    )
    _strings(topology["capabilities"], f"{profile_id}.topology.capabilities")
    _strings(topology["branches"], f"{profile_id}.topology.branches")
    coverage = str(topology.get("required_route_coverage") or "legacy_availability_only").strip()
    if coverage not in _REQUIRED_ROUTE_COVERAGE:
        raise RangeProfileError(f"{profile_id}.topology.required_route_coverage is unsupported")
    routes = topology.get("required_routes")
    if coverage in {"complete", "incomplete"}:
        if not isinstance(routes, list) or not routes:
            raise RangeProfileError(f"{profile_id}.topology.required_routes must be non-empty")
        route_sets = declared_required_routes(profile)
        if frozenset().union(*route_sets) != available_capability_set(profile):
            raise RangeProfileError(f"{profile_id}.topology.required_routes must cover available capabilities")
    elif routes not in (None, []):
        raise RangeProfileError(f"{profile_id}.topology.required_routes is only valid with route coverage")
    route_scenario = required_route_scenario(profile)
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
    if coverage in {"complete", "incomplete"} and route_scenario not in scenarios:
        raise RangeProfileError(
            f"{profile_id}.topology.required_route_scenario must select a declared binding"
        )
    if coverage not in {"complete", "incomplete"} and route_scenario is not None:
        raise RangeProfileError(
            f"{profile_id}.topology.required_route_scenario is only valid with route coverage"
        )
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
        vm["role"]: f"{network['first_octet']}.{octet}.{_effective_vlan(vm, network)}.{vm['last_octet']}"
        for vm in profile["vms"]
    }


def accepted_ips(profile: dict[str, Any], range_number: Any) -> dict[str, set[str]]:
    """Return source-declared reported addresses without changing primary IPs."""
    try:
        octet = int(range_number)
    except (TypeError, ValueError) as exc:
        raise RangeProfileError("rangeNumber must be an IP octet") from exc
    if not 0 <= octet <= 255:
        raise RangeProfileError("rangeNumber must be an IP octet")
    network = profile["network"]
    return {
        vm["role"]: {
            f"{network['first_octet']}.{octet}.{vlan}.{vm['last_octet']}"
            for vlan in _accepted_vlans(vm, network)
        }
        for vm in profile["vms"]
    }


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": profile["id"],
        "qualification": dict(profile["qualification"]),
        "topology_identity": topology_identity(profile),
        "structural_capability_identity": structural_capability_identity(
            profile, required_route_scenario(profile),
        ),
        "required_route_scenario": required_route_scenario(profile),
        "required_route_coverage": required_route_coverage(profile),
        "available_capabilities": sorted(available_capability_set(profile)),
        "required_capabilities": sorted(required_capability_set(profile)),
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
        network["first_octet"], _effective_vlan(vm, network), vm["last_octet"]
    ):
        mismatches.append("IP role")
    return mismatches


def require_live_qualified(profile: dict[str, Any]) -> None:
    if profile["qualification"]["state"] != "live_qualified":
        raise RangeProfileError(
            f"range profile {profile['id']!r} is {profile['qualification']['state']}, not live-qualified"
        )
