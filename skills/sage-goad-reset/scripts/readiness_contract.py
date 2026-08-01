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
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


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
EXPECTED_LUDUS_VMS = {
    "router": "10.4.10.254",
    "dc01": "10.4.10.10",
    "dc02": "10.4.10.11",
    "dc03": "10.4.10.12",
    "srv02": "10.4.10.22",
    "srv03": "10.4.10.23",
}
_VM_ALIASES = {
    "router": "router",
    "dc01": "dc01",
    "dc02": "dc02",
    "dc03": "dc03",
    "srv02": "srv02",
    "castelblack": "srv02",
    "srv03": "srv03",
    "braavos": "srv03",
}
_LUDUS_RANGE_FIELD = r"[a-z0-9]+"
_LUDUS_GENERATED_VM_PATTERNS = (
    re.compile(
        rf"^(?P<range>{_LUDUS_RANGE_FIELD})-router-(?P<image>debian11-x64)$",
        flags=re.ASCII,
    ),
    re.compile(
        rf"^(?P<range>{_LUDUS_RANGE_FIELD})-goad-(?P<role>dc01|dc02|dc03|srv02|srv03)$",
        flags=re.ASCII,
    ),
)
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


def _canonical_ludus_vm_name(value: Any) -> str | None:
    normalized = _normalize_name(value)
    if not normalized:
        return None
    alias = _VM_ALIASES.get(normalized)
    if alias is not None:
        return alias
    for pattern in _LUDUS_GENERATED_VM_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match is None:
            continue
        if "role" in match.groupdict() and match.group("role"):
            return _VM_ALIASES.get(match.group("role"))
        return "router"
    return None


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


def ludus_status(range_state: dict[str, Any] | None) -> dict[str, Any]:
    rows = []
    by_name: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    unexpected: list[str] = []
    for vm in (range_state or {}).get("VMs", []) or []:
        canonical = _canonical_ludus_vm_name(vm.get("name"))
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
    missing = [name for name in EXPECTED_LUDUS_VMS if name not in by_name]
    wrong_ip = [
        name
        for name, expected_ip in EXPECTED_LUDUS_VMS.items()
        if name in by_name and str(by_name[name].get("ip") or "") != expected_ip
    ]
    powered_off = [
        name
        for name in EXPECTED_LUDUS_VMS
        if name in by_name and not by_name[name].get("powered_on")
    ]
    blockers = []
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
        "range_number": (range_state or {}).get("rangeNumber"),
        "range_state": (range_state or {}).get("rangeState"),
        "expected_ips": dict(EXPECTED_LUDUS_VMS),
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
        "bloodhound_api": bloodhound_api,
        "bloodhound_mcp": bloodhound_mcp,
        "mythic_chat": mythic_chat,
        "foothold": foothold,
        "channel": channel,
    }
    blockers = []
    for name, section in sections.items():
        for blocker in section.get("blockers") or []:
            blockers.append(f"{name}: {blocker}")
    return redact_structure({
        "schema": "sage-readiness-contract-v1",
        "ready": all(bool(section.get("ready")) for section in sections.values()),
        "blockers": blockers,
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


def probe_ludus_status() -> dict[str, Any]:
    module = _load_module("sage_readiness_ludus", REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "ludus.py")
    code, payload = module._call("GET", "/api/v2/range")
    if code != 200 or not isinstance(payload, dict):
        return ludus_status({})
    return ludus_status(payload)


def probe_clock_status() -> dict[str, Any]:
    module = _load_module(
        "sage_readiness_clock",
        REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sync_range_time.py",
    )
    hosts = module.windows_hosts(module.load_inventory(module.DEFAULT_MCP_PATH))
    return clock_status(module.check_clocks(hosts, module.DEFAULT_MAX_SKEW_SECONDS))


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
) -> dict[str, Any]:
    ludus_section = ludus_observation if ludus_observation is not None else probe_ludus_status()
    clock_section = clock_observation if clock_observation is not None else probe_clock_status()
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
