#!/usr/bin/env python3
"""Shared, read-only readiness contract for Sage rehearsal and native-chat runs.

This module is intentionally operator-side. It may inspect Ludus, BloodHound, and Mythic
control-plane state when a helper explicitly asks for readiness, but it never performs
target-facing tradecraft from the Sage runtime itself.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BLOODHOUND_MCP_DIR = Path(
    os.environ.get("SAGE_BLOODHOUND_MCP_DIR")
    or (REPO_ROOT.parent / "bloodhound_mcp")
)
DEFAULT_STARTUP_IDENTITY_PATH = Path(
    os.environ.get("SAGE_STARTUP_IDENTITY_PATH", "/tmp/sage_startup_identity.json")
)
REQUIRED_BLOODHOUND_TOOLS = frozenset({"file_upload", "domain_info", "cypher_query"})
REQUIRED_STARTUP_ENV = ("SAGE_ENGAGEMENT_GATE", "SAGE_BLOODHOUND_MCP_DIR")
READINESS_DECISION_SCHEMA = "sage-readiness-decision-v1"
READINESS_CONTRACT_SCHEMA = "sage-readiness-contract-v1"
RANGE_PROFILE_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "range_profile.py"
LUDUS_PROBE_TIMEOUT_SECONDS = 30.0
CALLBACK_PROBE_TIMEOUT_SECONDS = 210.0
DEFAULT_TIMEOUT_GRACE_SECONDS = 5.0
READINESS_LANE_SECTIONS = {
    "range_boot": ("ludus",),
    "callback": ("ludus", "clock", "authentication"),
    "full": (
        "sage_deployment",
        "runtime_identity",
        "runtime_databases",
        "ludus",
        "clock",
        "authentication",
        "bloodhound_api",
        "bloodhound_mcp",
        "mythic_chat",
        "foothold",
        "channel",
    ),
}
_SECRETISH_KEYS = {
    "api_key",
    "aes",
    "aespsk",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "base64ticket",
    "credential",
    "credential_text",
    "key",
    "mythic_admin_password",
    "ntlm",
    "password",
    "private_key",
    "rabbitmq_password",
    "secret",
    "ticket",
    "token",
}
_SECRETISH_SUFFIXES = ("_password", "_secret", "_key")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load helper module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def _range_profile_module():
    return _load_module("sage_readiness_range_profile", RANGE_PROFILE_PATH)


def _range_profile(value: str | dict[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
    module = _range_profile_module()
    return module, module.get_profile(value)


def _route_summary(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme and parts.netloc:
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        return f"{parts.scheme}://{host}{parts.path or ''}"
    return text


def _is_secret_key(key: str) -> bool:
    normalized = _normalize_name(key)
    return normalized in _SECRETISH_KEYS or normalized.endswith(_SECRETISH_SUFFIXES)


def _safe_value(key: str, value: Any) -> Any:
    if _is_secret_key(key):
        return "<redacted>"
    return value


def redact_structure(value: Any) -> Any:
    """Recursively redact common secret-bearing keys from manifests/readiness output."""
    if isinstance(value, dict):
        return {
            str(key): redact_structure(_safe_value(str(key), item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, tuple):
        return [redact_structure(item) for item in value]
    return value


def _new_observation_id() -> str:
    return uuid4().hex


def evaluate_readiness(
    *,
    lane: str,
    sections: dict[str, Any],
    observation_id: str,
    terminal: bool = False,
    poll_count: int = 1,
) -> dict[str, Any]:
    """Return the sole typed release decision for every readiness consumer."""
    if lane not in READINESS_LANE_SECTIONS:
        raise ValueError(f"unknown readiness lane: {lane!r}")
    blockers: list[str] = []
    observation_id = str(observation_id or "").strip()
    if not observation_id:
        blockers.append("observation_id is missing")
    for name in READINESS_LANE_SECTIONS[lane]:
        section = sections.get(name)
        if not isinstance(section, dict):
            blockers.append(f"{name}: typed readiness section is missing")
            continue
        if section.get("ready") is not True:
            detail = section.get("blockers")
            if isinstance(detail, list) and detail:
                blockers.extend(f"{name}: {item}" for item in detail)
            else:
                blockers.append(f"{name}: ready must be the JSON boolean true")
    ready = not blockers
    return {
        "schema": READINESS_DECISION_SCHEMA,
        "lane": lane,
        "state": "ready" if ready else ("blocked" if terminal else "waiting"),
        "ready": ready,
        "launch_released": ready,
        "capture_attempt_consumed": False,
        "observation_id": observation_id,
        "poll_count": int(poll_count),
        "required_sections": list(READINESS_LANE_SECTIONS[lane]),
        "blockers": blockers,
    }


def _blocked_release(lane: str, blocker: str) -> dict[str, Any]:
    return {
        "schema": READINESS_DECISION_SCHEMA,
        "lane": lane,
        "state": "blocked",
        "ready": False,
        "launch_released": False,
        "capture_attempt_consumed": False,
        "observation_id": "invalid-output",
        "poll_count": 0,
        "required_sections": list(READINESS_LANE_SECTIONS.get(lane, ())),
        "blockers": [blocker],
    }


def parse_release_output(value: Any, *, expected_lane: str) -> dict[str, Any]:
    """Validate a serialized readiness report without trusting a loose ``ready`` field."""
    if expected_lane not in READINESS_LANE_SECTIONS:
        raise ValueError(f"unknown readiness lane: {expected_lane!r}")
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except Exception as exc:
        return _blocked_release(expected_lane, f"readiness output is not JSON: {exc}")
    if not isinstance(payload, dict):
        return _blocked_release(expected_lane, "readiness output must be a JSON object")
    decision = payload.get("readiness")
    if payload.get("schema") != READINESS_CONTRACT_SCHEMA or not isinstance(decision, dict):
        return _blocked_release(expected_lane, "canonical readiness contract is missing")
    sections = {name: payload.get(name) for name in READINESS_LANE_SECTIONS[expected_lane]}
    recomputed = evaluate_readiness(
        lane=expected_lane,
        sections=sections,
        observation_id=str(decision.get("observation_id") or ""),
        terminal=True,
        poll_count=1,
    )
    checks = (
        (decision.get("schema") == READINESS_DECISION_SCHEMA, "typed readiness decision is missing"),
        (decision.get("lane") == expected_lane, "readiness lane does not match the consumer"),
        (isinstance(decision.get("observation_id"), str) and bool(decision["observation_id"].strip()),
         "readiness observation_id is missing"),
        (type(decision.get("ready")) is bool, "readiness ready field is not a JSON boolean"),
        (type(decision.get("launch_released")) is bool,
         "readiness launch_released field is not a JSON boolean"),
        (type(payload.get("ready")) is bool, "contract ready field is not a JSON boolean"),
        (payload.get("ready") is decision.get("ready"), "contract and decision readiness disagree"),
        (payload.get("observation_id") == decision.get("observation_id"),
         "contract and decision observation IDs disagree"),
        (decision.get("ready") is recomputed.get("ready"),
         "typed sections and decision readiness disagree"),
        (decision.get("blockers") == recomputed.get("blockers"),
         "typed sections and decision blockers disagree"),
        (decision.get("launch_released") is decision.get("ready"),
         "launch release does not match readiness"),
        (decision.get("state") == ("ready" if decision.get("ready") else "blocked"),
         "readiness state is not terminal"),
    )
    for passed, blocker in checks:
        if not passed:
            return _blocked_release(expected_lane, blocker)
    return dict(decision)


def validate_timeout_budget(
    *,
    outer_timeout_seconds: float,
    child_timeout_seconds: float,
    grace_seconds: float,
) -> dict[str, Any]:
    values = (outer_timeout_seconds, child_timeout_seconds, grace_seconds)
    if not all(math.isfinite(float(value)) and float(value) >= 0 for value in values):
        raise ValueError("readiness timeouts must be finite non-negative values")
    if float(outer_timeout_seconds) <= float(child_timeout_seconds) + float(grace_seconds):
        raise ValueError(
            "outer readiness timeout must be strictly greater than child timeout plus grace"
        )
    return {
        "valid": True,
        "outer_timeout_seconds": float(outer_timeout_seconds),
        "child_timeout_seconds": float(child_timeout_seconds),
        "grace_seconds": float(grace_seconds),
    }


def poll_readiness(
    probe: Callable[[], dict[str, Any]],
    *,
    lane: str,
    outer_timeout_seconds: float,
    child_timeout_seconds: float,
    grace_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll fresh typed observations and return one terminal release decision."""
    validate_timeout_budget(
        outer_timeout_seconds=outer_timeout_seconds,
        child_timeout_seconds=child_timeout_seconds,
        grace_seconds=grace_seconds,
    )
    if not math.isfinite(float(poll_seconds)) or float(poll_seconds) < 0:
        raise ValueError("readiness poll interval must be finite and non-negative")
    deadline = now() + float(outer_timeout_seconds)
    minimum_child_budget = float(child_timeout_seconds) + float(grace_seconds)
    seen: set[str] = set()
    count = 0
    last = _blocked_release(lane, "no readiness observation was collected")
    while True:
        remaining = deadline - now()
        if remaining <= minimum_child_budget:
            blocked = dict(last)
            blocked.update(state="blocked", ready=False, launch_released=False)
            blocked["blockers"] = [
                *last["blockers"],
                "readiness timeout exhausted before another safe child probe",
            ]
            return blocked
        count += 1
        try:
            observation = probe()
        except Exception as exc:
            return _blocked_release(lane, f"readiness probe failed: {type(exc).__name__}: {exc}")
        if not isinstance(observation, dict) or not isinstance(observation.get("sections"), dict):
            return _blocked_release(lane, "readiness probe did not return typed sections")
        observation_id = str(observation.get("observation_id") or "").strip()
        if not observation_id:
            return _blocked_release(lane, "readiness probe did not return observation_id")
        if observation_id in seen:
            return _blocked_release(lane, f"stale observation_id was reused: {observation_id}")
        seen.add(observation_id)
        last = evaluate_readiness(
            lane=lane,
            sections=observation["sections"],
            observation_id=observation_id,
            poll_count=count,
        )
        remaining = deadline - now()
        if last["ready"] and remaining > 0:
            return last
        if remaining <= minimum_child_budget:
            blocked = dict(last)
            blocked.update(state="blocked", ready=False, launch_released=False)
            blocked["blockers"] = [
                *last["blockers"],
                "readiness timeout exhausted before another safe child probe",
            ]
            return blocked
        sleep(min(float(poll_seconds), remaining - minimum_child_budget))


def hash_file(path: str | Path) -> dict[str, str]:
    resolved = Path(path).expanduser()
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    try:
        rendered = str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        rendered = str(resolved)
    return {"path": rendered, "sha256": digest}


def runtime_db_status(
    repo_root: Path = REPO_ROOT,
    *,
    runtime_dbs_archived: bool = False,
    operator_db_cleanup_confirmed: bool | None = None,
) -> dict[str, Any]:
    if operator_db_cleanup_confirmed is not None:
        runtime_dbs_archived = operator_db_cleanup_confirmed
    required = [
        repo_root / "Payload_Type" / "sage" / "sage.db",
        repo_root / "Payload_Type" / "sage" / ".phoenix" / "phoenix.db",
    ]
    sage_archives = sorted((repo_root / "Payload_Type" / "sage").glob("sage_*.db"))
    phoenix_archives = sorted((repo_root / "Payload_Type" / "sage" / ".phoenix").glob("phoenix_*.db"))
    existing_required = [str(path.relative_to(repo_root)) for path in required if path.exists()]
    existing_archives = [
        str(path.relative_to(repo_root))
        for path in (*sage_archives, *phoenix_archives)
        if path.exists()
    ]
    blocks = bool(existing_required and not runtime_dbs_archived)
    return {
        "ready": not blocks,
        "runtime_dbs_archived": runtime_dbs_archived,
        "operator_db_cleanup_confirmed": runtime_dbs_archived,
        "existing_required": existing_required,
        "existing_archives": existing_archives,
        "existing_session": [
            path for path in existing_archives if Path(path).name.startswith("sage_")
        ],
        "blockers": (
            []
            if not blocks
            else ["archive stale Sage/Phoenix runtime DBs before restarting Sage"]
        ),
        "note": (
            "Archive active DBs with sage-goad-reset before Sage restart. Recreated active DB files are expected "
            "after restart and do not block when runtime archival was confirmed."
        ),
    }


def startup_identity_from_env(
    env: dict[str, str] | None = None,
    *,
    identity_path: Path = DEFAULT_STARTUP_IDENTITY_PATH,
    process_probe: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    del env
    recorded: dict[str, Any] = {}
    if identity_path.exists():
        try:
            raw = json.loads(identity_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                recorded = raw
        except Exception:
            recorded = {}

    provider = str(recorded.get("provider") or "").strip().lower()
    model = str(recorded.get("model") or "").strip()
    route = _route_summary(recorded.get("route") or "")
    recorded_required = recorded.get("required_env")
    if not isinstance(recorded_required, dict):
        recorded_required = {}
    required_env = {
        name: bool(recorded_required.get(name))
        for name in REQUIRED_STARTUP_ENV
    }
    pid = recorded.get("pid")
    cwd = str(recorded.get("cwd") or "").strip()
    probe_result: dict[str, Any] = {}
    blockers = []
    if not recorded:
        blockers.append("startup identity file is missing or unreadable")
    if not provider:
        blockers.append("provider is not recorded")
    if not model:
        blockers.append("model is not recorded")
    for name, present in required_env.items():
        if not present:
            blockers.append(f"required env missing: {name}")
    if not isinstance(pid, int) or pid <= 0:
        blockers.append("startup identity pid is missing")
    elif not cwd:
        blockers.append("startup identity cwd is missing")
    else:
        probe = process_probe or _probe_process
        try:
            probe_result = dict(probe(pid) or {})
        except Exception as exc:
            blockers.append(f"startup process probe failed: {exc}")
        else:
            if not probe_result.get("exists"):
                blockers.append(f"recorded Sage process is not running: pid={pid}")
            cmdline = str(probe_result.get("cmdline") or "")
            if "main.py" not in cmdline:
                blockers.append("recorded Sage process cmdline does not contain main.py")
            observed_cwd = str(probe_result.get("cwd") or "").strip()
            if observed_cwd != cwd:
                blockers.append("recorded Sage process cwd does not match startup identity")
    return {
        "ready": not blockers,
        "provider": provider,
        "model": model,
        "route": route,
        "required_env": required_env,
        "pid": pid,
        "cwd": cwd,
        "observed_process": {
            "exists": bool(probe_result.get("exists")),
            "cmdline": str(probe_result.get("cmdline") or ""),
            "cwd": str(probe_result.get("cwd") or ""),
        },
        "source": "startup_identity_file",
        "recorded_at": recorded.get("recorded_at"),
        "blockers": blockers,
    }


def _probe_process(pid: int) -> dict[str, Any]:
    proc_root = Path("/proc") / str(pid)
    if not proc_root.exists():
        return {"exists": False, "cmdline": "", "cwd": ""}
    try:
        cmdline = proc_root.joinpath("cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8",
            errors="replace",
        ).strip()
    except Exception:
        cmdline = ""
    try:
        cwd = str(proc_root.joinpath("cwd").resolve())
    except Exception:
        cwd = ""
    return {"exists": True, "cmdline": cmdline, "cwd": cwd}


def write_startup_identity(
    path: Path,
    env: dict[str, str],
    *,
    pid: int | None = None,
    cwd: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    payload = {
        "provider": str(env.get("provider") or "").strip().lower(),
        "model": str(env.get("model") or "").strip(),
        "route": _route_summary(env.get("API_ENDPOINT") or ""),
        "required_env": {name: bool(env.get(name)) for name in REQUIRED_STARTUP_ENV},
        "pid": pid,
        "cwd": cwd or "",
        "recorded_at": recorded_at,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return payload


def ludus_status(
    range_state: dict[str, Any] | None,
    profile: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile_module, selected = _range_profile(profile)
    range_number = (range_state or {}).get("rangeNumber")
    try:
        expected_ips = profile_module.expected_ips(selected, range_number)
    except ValueError as exc:
        expected_ips = {}
        profile_error = str(exc)
    else:
        profile_error = ""
    rows = []
    by_name: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    unexpected: list[str] = []
    for vm in (range_state or {}).get("VMs", []) or []:
        canonical = profile_module.canonical_vm_name(vm.get("name"), selected)
        row = {
            "name": str(vm.get("name") or ""),
            "canonical_name": canonical,
            "powered_on": bool(vm.get("poweredOn")),
            "ip": vm.get("ip"),
        }
        rows.append(row)
        if canonical is None:
            unexpected.append(row["name"])
            continue
        if canonical in by_name:
            duplicates.add(canonical)
            continue
        by_name[canonical] = row
    missing = [name for name in expected_ips if name not in by_name]
    wrong_ip = [
        name
        for name, expected_ip in expected_ips.items()
        if name in by_name and str(by_name[name].get("ip") or "") != expected_ip
    ]
    powered_off = [
        name
        for name in expected_ips
        if name in by_name and not by_name[name].get("powered_on")
    ]
    blockers = []
    if profile_error:
        blockers.append(f"range profile could not derive expected IPs: {profile_error}")
    if selected["qualification"]["state"] != "live_qualified":
        blockers.append(
            f"range profile {selected['id']} is {selected['qualification']['state']}, not live-qualified"
        )
    if missing:
        blockers.append(f"missing Ludus VMs: {', '.join(missing)}")
    if unexpected:
        blockers.append(f"unexpected Ludus VM names: {', '.join(unexpected)}")
    if duplicates:
        blockers.append(f"duplicate Ludus VM identities: {', '.join(sorted(duplicates))}")
    if wrong_ip:
        blockers.append(f"unexpected Ludus VM IPs: {', '.join(wrong_ip)}")
    if powered_off:
        blockers.append(f"Ludus VMs not powered on: {', '.join(powered_off)}")
    return {
        "ready": not blockers,
        "range_profile": profile_module.profile_summary(selected),
        "range_number": range_number,
        "range_state": (range_state or {}).get("rangeState"),
        "expected_ips": expected_ips,
        "vms": rows,
        "blockers": blockers,
    }


def clock_status(observation: dict[str, Any] | None) -> dict[str, Any]:
    observation = dict(observation or {})
    ready = bool(observation.get("ready"))
    blockers = []
    if not ready:
        blockers.append("range clock check is not ready")
    return {
        "ready": ready,
        "max_skew_seconds": observation.get("max_skew_seconds"),
        "hosts": observation.get("hosts") or [],
        "over_limit": observation.get("over_limit") or [],
        "time_service_enabled": observation.get("time_service_enabled") or [],
        "errors": observation.get("errors") or [],
        "blockers": blockers,
    }


def authentication_status(
    observation: dict[str, Any] | None,
    profile: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove that every expected Windows guest completed an authenticated clock probe."""
    observation = dict(observation or {})
    hosts = observation.get("hosts") if isinstance(observation.get("hosts"), list) else []
    errors = observation.get("errors") if isinstance(observation.get("errors"), list) else []
    profile_module, selected = _range_profile(profile)
    expected = {vm["role"] for vm in selected["vms"] if vm["os"] == "windows"}
    observed: list[str] = []
    duplicates: set[str] = set()
    for host in hosts:
        if not isinstance(host, dict):
            continue
        canonical = profile_module.canonical_vm_name(
            host.get("inventory_hostname") or host.get("computer"), selected
        )
        if canonical is None:
            continue
        if canonical in observed:
            duplicates.add(canonical)
        observed.append(canonical)
    observed_set = set(observed)
    missing = sorted(expected.difference(observed_set))
    unexpected = sorted(observed_set.difference(expected))
    blockers = []
    if selected["qualification"]["state"] != "live_qualified":
        blockers.append(
            f"range profile {selected['id']} is {selected['qualification']['state']}, not live-qualified"
        )
    if errors:
        blockers.append("WinRM authentication probe returned errors")
    if missing:
        blockers.append(f"WinRM authentication missing hosts: {', '.join(missing)}")
    if unexpected:
        blockers.append(f"WinRM authentication included unexpected hosts: {', '.join(unexpected)}")
    if duplicates:
        blockers.append(f"WinRM authentication duplicated hosts: {', '.join(sorted(duplicates))}")
    return {
        "ready": not blockers,
        "range_profile": profile_module.profile_summary(selected),
        "expected_hosts": sorted(expected),
        "observed_hosts": sorted(observed_set),
        "errors": errors,
        "blockers": blockers,
    }


def bloodhound_api_status(status_code: int | None, domains: Any) -> dict[str, Any]:
    domain_rows = domains if isinstance(domains, list) else []
    ready = int(status_code or 0) == 200
    blockers = [] if ready else [f"BloodHound API unavailable: status={status_code}"]
    return {
        "ready": ready,
        "status_code": status_code,
        "domain_count": len(domain_rows),
        "domains": [str(item.get("name") or "") for item in domain_rows if isinstance(item, dict)],
        "blockers": blockers,
    }


def bloodhound_mcp_status(
    directory: str | Path | None,
    tool_names: Iterable[str] | None,
) -> dict[str, Any]:
    path = Path(directory or DEFAULT_BLOODHOUND_MCP_DIR).expanduser()
    names = sorted({str(name).strip() for name in (tool_names or []) if str(name).strip()})
    missing = sorted(REQUIRED_BLOODHOUND_TOOLS.difference(names))
    blockers = []
    if not path.is_dir():
        blockers.append(f"BloodHound MCP checkout missing: {path}")
    if missing:
        blockers.append(f"BloodHound MCP missing exact tools: {', '.join(missing)}")
    return {
        "ready": not blockers,
        "directory": str(path),
        "checkout_exists": path.is_dir(),
        "required_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
        "tool_names": names,
        "missing_tools": missing,
        "blockers": blockers,
    }


def mythic_chat_status(
    chat_containers: list[dict[str, Any]] | None,
    api_token: dict[str, Any] | None,
) -> dict[str, Any]:
    live_chat = [
        row
        for row in (chat_containers or [])
        if row.get("container_running") and not row.get("deleted")
    ]
    scopes = {
        str(item).strip()
        for item in ((api_token or {}).get("scopes") or [])
        if str(item).strip()
    }
    token_ready = bool(api_token) and "*" in scopes
    blockers = []
    if not live_chat:
        blockers.append("Sage chat container is not running")
    if not token_ready:
        blockers.append("wildcard-scoped Mythic API token is unavailable")
    return {
        "ready": not blockers,
        "chat_containers": live_chat,
        "selected_chat_container_id": live_chat[0].get("id") if live_chat else None,
        "api_token": {
            "id": (api_token or {}).get("id"),
            "name": (api_token or {}).get("name"),
            "operator_id": (api_token or {}).get("operator_id"),
            "scopes": sorted(scopes),
        },
        "blockers": blockers,
    }


def foothold_status(summary: dict[str, Any] | None) -> dict[str, Any]:
    summary = dict(summary or {})
    selected = summary.get("selected_foothold_cb")
    duplicates = summary.get("duplicate_live_footholds") or []
    ready = bool(summary.get("ready")) and selected is not None and not duplicates
    blockers = []
    if selected is None:
        blockers.append("unique live foothold is unavailable")
    if duplicates:
        blockers.append("multiple live foothold callbacks match the selector")
    return {
        "ready": ready,
        "selected_foothold_cb": selected,
        "foothold_payload_type": summary.get("foothold_payload_type"),
        "callbacks": summary.get("callbacks") or [],
        "duplicate_live_footholds": duplicates,
        "blockers": blockers,
    }


def channel_status(
    channel: dict[str, Any] | None, *, required: bool = True
) -> dict[str, Any]:
    channel = dict(channel or {})
    channel_id = channel.get("chat_channel_id")
    prepared = bool(channel.get("prepared"))
    ready = (channel_id is not None and prepared) if required else True
    return {
        "ready": ready,
        "required": required,
        "chat_channel_id": channel_id,
        "chat_channel_name": channel.get("chat_channel_name"),
        "prepared": prepared,
        "reused": bool(channel.get("reused")),
        "blockers": (
            []
            if ready
            else ["prepared native-chat channel is unavailable"]
        ),
    }


# "not supplied, go and probe" must not share a value with "probed, and the answer is unknown".
# Collapsing them is how a fail-closed branch becomes unreachable.
_UNSET: Any = object()

SAGE_DEPLOYMENT_MODES = ("local", "container")
DEFAULT_SAGE_DEPLOYMENT_MODE = "local"
SAGE_CONTAINER_NAME = "sage"


def resolve_sage_deployment_mode(value: Any = None) -> str:
    """Which Sage is meant to serve Mythic: the tmux process or the Docker container."""
    return str(
        value or os.environ.get("SAGE_DEPLOYMENT_MODE") or DEFAULT_SAGE_DEPLOYMENT_MODE
    ).strip().casefold()


def probe_sage_container_running(name: str = SAGE_CONTAINER_NAME) -> bool | None:
    """True/False when docker answers, None when it cannot be asked.

    None is deliberately distinct from False: "no container" and "cannot tell" must not collapse,
    because the second is a reason to refuse rather than to proceed.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def probe_local_sage_running(repo_root: Path = REPO_ROOT, proc_root: Path = Path("/proc")) -> bool:
    """A host process running this repo's own `main.py`, found through /proc.

    Deliberately not `pgrep -f main.py`: that matches the shell which invoked it, which is exactly
    how a cwd check once reported the calling command instead of Sage.
    """
    marker = str(repo_root / ".venv")
    if not proc_root.is_dir():
        return False
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if "main.py" in cmdline and marker in cmdline:
            return True
    return False


def sage_deployment_status(
    *,
    mode: Any = None,
    container_running: bool | None = _UNSET,
    local_running: bool | None = _UNSET,
    repo_root: Path = REPO_ROOT,
    require_intended_running: bool = True,
) -> dict[str, Any]:
    """Exactly one Sage may serve Mythic's `sage` service queue.

    `require_intended_running=False` narrows this to the conflict alone: the unintended Sage must
    be down, but the intended one need not be up yet. That is the mid-reset state — Mythic is
    restarted long before Sage is, so demanding a live local Sage there would fail every reset.

    `mythic-cli start` starts EVERY registered service, so resetting Mythic silently brings the
    Sage container up alongside a tmux Sage. Both register as `sage`, one wins the RabbitMQ queue,
    and requests are answered by whichever won. Observed 2026-08-01: the container answered a chat
    request using code that was not the working tree and had no BloodHound MCP directory, while the
    losing tmux process logged `Another instance of this service, sage, is running` on a loop.
    Nothing in this contract could see that, so it reported `ready: true`.
    """
    resolved = resolve_sage_deployment_mode(mode)
    if container_running is _UNSET:
        container_running = probe_sage_container_running()
    if local_running is _UNSET:
        local_running = probe_local_sage_running(repo_root)
    blockers: list[str] = []
    if resolved not in SAGE_DEPLOYMENT_MODES:
        blockers.append(
            f"unknown SAGE_DEPLOYMENT_MODE {resolved!r}; expected one of "
            f"{', '.join(SAGE_DEPLOYMENT_MODES)}"
        )
    elif container_running is None:
        blockers.append(
            "could not determine whether the Sage container is running; docker did not answer"
        )
    elif resolved == "local":
        if container_running:
            blockers.append(
                "Sage docker container is running in local mode; stop it "
                "(`mythic-cli stop sage`) or set SAGE_DEPLOYMENT_MODE=container"
            )
        if require_intended_running and not local_running:
            blockers.append("no local Sage process is running in local mode")
    else:
        if require_intended_running and not container_running:
            blockers.append("Sage docker container is not running in container mode")
        if local_running:
            blockers.append(
                "a local Sage process is running in container mode; stop it "
                "(`sage_stop.sh`) or set SAGE_DEPLOYMENT_MODE=local"
            )
    return {
        "ready": not blockers,
        "mode": resolved,
        "container_running": container_running,
        "local_process_running": bool(local_running),
        "require_intended_running": require_intended_running,
        "blockers": blockers,
    }


def build_readiness_report(
    *,
    sage_deployment: dict[str, Any],
    runtime_identity: dict[str, Any],
    runtime_databases: dict[str, Any],
    ludus: dict[str, Any],
    clock: dict[str, Any],
    authentication: dict[str, Any],
    bloodhound_api: dict[str, Any],
    bloodhound_mcp: dict[str, Any],
    mythic_chat: dict[str, Any],
    foothold: dict[str, Any],
    channel: dict[str, Any],
) -> dict[str, Any]:
    sections = {
        "sage_deployment": sage_deployment,
        "runtime_identity": runtime_identity,
        "runtime_databases": runtime_databases,
        "ludus": ludus,
        "clock": clock,
        "authentication": authentication,
        "bloodhound_api": bloodhound_api,
        "bloodhound_mcp": bloodhound_mcp,
        "mythic_chat": mythic_chat,
        "foothold": foothold,
        "channel": channel,
    }
    observation_id = _new_observation_id()
    decision = evaluate_readiness(
        lane="full",
        sections=sections,
        observation_id=observation_id,
        terminal=True,
    )
    return redact_structure({
        "schema": READINESS_CONTRACT_SCHEMA,
        "ready": decision["ready"],
        "state": decision["state"],
        "observation_id": observation_id,
        "blockers": decision["blockers"],
        "readiness": decision,
        **sections,
    })


async def probe_bloodhound_mcp_tools(directory: str | Path | None = None) -> dict[str, Any]:
    path = Path(directory or os.environ.get("SAGE_BLOODHOUND_MCP_DIR") or DEFAULT_BLOODHOUND_MCP_DIR)
    if not path.is_dir():
        return bloodhound_mcp_status(path, [])
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except Exception:
        return bloodhound_mcp_status(path, [])
    command = os.environ.get("SAGE_BLOODHOUND_MCP_COMMAND", "uv")
    client = MultiServerMCPClient({
        "BloodHound": {
            "transport": "stdio",
            "command": command,
            "args": ["--directory", str(path), "run", "main.py"],
            "cwd": str(path),
        }
    })
    try:
        tools = await client.get_tools(server_name="BloodHound")
        names = [getattr(tool, "name", "") for tool in tools]
    except Exception:
        names = []
    return bloodhound_mcp_status(path, names)


def probe_ludus_status(
    range_id: str | None = None,
    mcp_server: str | None = None,
    profile: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    module = _load_module("sage_readiness_ludus", REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "ludus.py")
    code, payload = module._call(
        "GET", "/api/v2/range", range_id=range_id, mcp_server=mcp_server
    )
    if code != 200 or not isinstance(payload, dict):
        return ludus_status({}, profile)
    return ludus_status(payload, profile)


def probe_clock_status(
    range_id: str | None = None,
    mcp_server: str | None = None,
) -> dict[str, Any]:
    module = _load_module(
        "sage_readiness_clock",
        REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sync_range_time.py",
    )
    hosts = module.windows_hosts(
        module.load_inventory(module.DEFAULT_MCP_PATH, range_id, mcp_server)
    )
    return clock_status(module.check_clocks(hosts, module.DEFAULT_MAX_SKEW_SECONDS))


def range_boot_observation(
    range_id: str | None = None,
    mcp_server: str | None = None,
    profile: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "observation_id": _new_observation_id(),
        "sections": {"ludus": probe_ludus_status(range_id, mcp_server, profile)},
    }


def callback_readiness_observation(
    range_id: str | None = None,
    mcp_server: str | None = None,
    profile: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    clock = probe_clock_status(range_id, mcp_server)
    return {
        "observation_id": _new_observation_id(),
        "sections": {
            "ludus": probe_ludus_status(range_id, mcp_server, profile),
            "clock": clock,
            "authentication": authentication_status(clock, profile),
        },
    }


def probe_bloodhound_api_status() -> dict[str, Any]:
    module = _load_module(
        "sage_readiness_bloodhound",
        REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "bh_reset.py",
    )
    code, domains = module._domains()
    return bloodhound_api_status(code, domains)


async def collect_operator_readiness(
    *,
    repo_root: Path = REPO_ROOT,
    runtime_dbs_archived: bool = False,
    callback_summary: dict[str, Any],
    chat_containers: list[dict[str, Any]],
    api_token: dict[str, Any] | None,
    channel: dict[str, Any] | None,
    ludus_observation: dict[str, Any] | None = None,
    clock_observation: dict[str, Any] | None = None,
    bloodhound_api_observation: dict[str, Any] | None = None,
    bloodhound_mcp_observation: dict[str, Any] | None = None,
    require_prepared_channel: bool = True,
    sage_deployment_observation: dict[str, Any] | None = None,
    range_profile: str | dict[str, Any] | None = None,
    ludus_range_id: str | None = None,
    ludus_mcp_server: str | None = None,
) -> dict[str, Any]:
    selected_profile = range_profile or os.environ.get("SAGE_RANGE_PROFILE_ID")
    range_id = ludus_range_id or os.environ.get("SAGE_LUDUS_RANGE_ID")
    mcp_server = ludus_mcp_server or os.environ.get("SAGE_LUDUS_MCP_SERVER")
    ludus_section = (
        ludus_observation
        if ludus_observation is not None
        else probe_ludus_status(range_id, mcp_server, selected_profile)
    )
    clock_section = (
        clock_observation
        if clock_observation is not None
        else probe_clock_status(range_id, mcp_server)
    )
    bloodhound_api_section = (
        bloodhound_api_observation
        if bloodhound_api_observation is not None
        else probe_bloodhound_api_status()
    )
    bloodhound_mcp_section = (
        bloodhound_mcp_observation
        if bloodhound_mcp_observation is not None
        else await probe_bloodhound_mcp_tools()
    )
    return build_readiness_report(
        sage_deployment=(
            sage_deployment_observation
            if sage_deployment_observation is not None
            else sage_deployment_status(repo_root=repo_root)
        ),
        runtime_identity=startup_identity_from_env(),
        runtime_databases=runtime_db_status(repo_root, runtime_dbs_archived=runtime_dbs_archived),
        ludus=ludus_section,
        clock=clock_section,
        authentication=authentication_status(clock_section, selected_profile),
        bloodhound_api=bloodhound_api_section,
        bloodhound_mcp=bloodhound_mcp_section,
        mythic_chat=mythic_chat_status(chat_containers, api_token),
        foothold=foothold_status(callback_summary),
        channel=channel_status(
            channel, required=require_prepared_channel
        ),
    )


if __name__ == "__main__":  # pragma: no cover - importable helper, not a standalone probe yet
    print(json.dumps(startup_identity_from_env(), indent=2, sort_keys=True))
