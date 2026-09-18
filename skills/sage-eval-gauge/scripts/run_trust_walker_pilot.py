#!/usr/bin/env python3
"""Run the fixed, retry-zero two-arm Trust Walker matched pilot."""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import sys
import time
import signal
import threading
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[3]
PAYLOAD = ROOT / "Payload_Type" / "sage"
PYTHON = str(ROOT / ".venv" / "bin" / "python")
if str(PAYLOAD) not in sys.path:
    sys.path.insert(0, str(PAYLOAD))

from ai import task_budget
from ai.hillclimb import bare_bloodhound, bare_mythic_tools, effect_registry, live_seams
from ai.hillclimb.bare_runner import BareModelRunner, make_stdout_logger
from ai.hillclimb.range_state import Milestone
from ai.hillclimb.trust_walker_outcome import (
    OBJECTIVE_INERT_UNFINISHED_COMMANDS,
    PROOF_CONTRACT,
    SNAPSHOT_SCHEMA,
    certificate_admin_candidate,
    classify_terminal_snapshot,
)
from ai.hillclimb.trust_walker_pilot import (
    ARM_ORDER, IMPROVEMENT_ARM_ORDER, CANONICAL_INVENTORY, MODEL, OBJECTIVE, PROVIDER, SNAPSHOT,
    ALTERNATE_START_SCHEMA, AlternateStartBinding,
    RECOVERY_CLEANUP_ATTEMPT_LIMIT, TASK_SETTLEMENT_SECONDS,
    SETUP_REHEARSAL_ATTEMPT_LIMIT, SETUP_REHEARSAL_SUCCESSES,
    ImprovementContract, PilotContract, PilotDependencies, PilotError,
    append_record, arm_environment, assert_rejected_kernel_absent,
    compare_start_manifests, matched_start_identity, read_records, retained_alternate_start_binding, run_pair,
    summarize_pair,
    validate_artifact_dir, validate_runtime_identity,
)


SCENARIO = "cross-forest-objective"
RANGE_PROFILE = "goad-trust-walker"
NETBIOS_MAP = '{"NORTH":"north.sevenkingdoms.local","SEVENKINGDOMS":"sevenkingdoms.local","ESSOS":"essos.local"}'
EXPECTED_BACKEND = f"{PROVIDER}:{MODEL}"
STAGED_FOOTHOLD_LAUNCHER = "skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh"
STAGED_FOOTHOLD_CALLBACK_SETTLE_SECONDS = 90
STAGED_FOOTHOLD_TASK = "SageApolloBootstrap"
STAGED_FOOTHOLD_REMOTE_PATH = r"C:\Users\Public\apollo.exe"
STAGED_FOOTHOLD_REMOTE_SHA256 = "aea555ddff1b6f1941714fdffe7a760deb5058c20861965ab711c7aa19091b37"
STAGED_FOOTHOLD_CONFIG = ROOT / "skills" / "sage-callback-bootstrap" / "apollo_callback_config.json"
STAGED_FOOTHOLD_CONFIG_SHA256 = "d7ef31912788a78b46c45bdd05920ef3519b31ab298889f52348d41a2163fe82"
STAGED_FOOTHOLD_PAYLOAD_UUID = "aef50036-02ad-4c7a-8786-7ca778281ad2"
OPERATOR_LOCAL_LINKS = (
    ".env",
    ".mcp.json",
    "Payload_Type/sage/.env.local",
    "Payload_Type/sage/certs/bundle.pem",
    "skills/sage-eval-gauge/.env.local",
    "skills/sage-foothold-launch/.env",
    "skills/sage-callback-bootstrap/.env",
    "skills/sage-callback-bootstrap/apollo_callback_config.json",
)
OPERATOR_LOCAL_VENV = ".venv"
OPERATOR_RUNTIME_TOOLS_DIR = "Payload_Type/sage/tools"
TRUST_WALKER_REQUIRED_RUNTIME_TOOLS = ("SharpHound.exe",)
SAGE_TLS_BOOTSTRAP = "Payload_Type/sage/ssl_bootstrap.py"
SAGE_TLS_CA_BUNDLE = "Payload_Type/sage/certs/bundle.pem"
HOST_EXECUTABLES = ("git", "uv", "tmux", "pgrep", "python3", "xfreerdp3", "docker")
MYTHIC_ENV_KEYS = ("MYTHIC_ADMIN_PASSWORD", "RABBITMQ_PASSWORD")
BLOODHOUND_ENV_KEYS = (
    "BLOODHOUND_SCHEME",
    "BLOODHOUND_DOMAIN",
    "BLOODHOUND_PORT",
    "BLOODHOUND_TOKEN_ID",
    "BLOODHOUND_TOKEN_KEY",
)
STANDING_LIVE_AUTHORITY_SCHEMA = "sage-standing-live-authority-v1"
STANDING_LIVE_AUTHORITY_SCOPE = "sage-goad-trust-walker-evaluation-recovery"
STANDING_LIVE_COMMANDS = frozenset({
    "rehearse-setup", "continue-setup", "run-pair", "run-improvement-pair",
})
STANDING_LIVE_OPERATION_CLASSES = frozenset({
    "local-implementation",
    "offline-verification",
    "preflight",
    "control-plane-observation",
    "control-plane-mutation",
    "tls-check",
    "provider-model-call",
    "reset",
    "snapshot-rollback",
    "bloodhound-operation",
    "sage-restart",
    "callback-launch",
    "callback-deactivation",
    "mythic-payload-task",
    "private-evidence-retention",
    "recovery-cleanup",
    "successor-experiment",
})
STANDING_LIVE_EXCLUSIONS = frozenset({
    "new-target",
    "new-environment",
    "broader-runtime-authority",
    "changed-acceptance-criteria",
    "changed-route",
    "increased-budgets",
    "uncontrolled-activity",
    "publication",
    "commit",
    "apollo-diagnostic",
    "mythic-upstream-work",
    "out-of-scope-action",
})
PROTECTED_ISC11_PATHS = frozenset({
    "Payload_Type/sage/ai/hillclimb/policy_replay_calibration.py",
    "Payload_Type/sage/ai/hillclimb/run_gauge_live.py",
    "Payload_Type/sage/tests/test_eval_gauge_orchestrate.py",
    "Payload_Type/sage/tests/test_hillclimb_policy_replay_calibration.py",
    "Payload_Type/sage/tests/test_hillclimb_run_gauge_live.py",
    "skills/sage-eval-gauge/scripts/orchestrate.py",
    "Payload_Type/sage/ai/hillclimb/development_row_kernel.py",
})
TASK_OUTPUT_QUERY = """
query TrustWalkerPilotTasks($operationId: Int!) {
  task(where: {operation_id: {_eq: $operationId}}, order_by: {display_id: asc}) {
    operation_id display_id command_name display_params original_params completed status timestamp
    status_timestamp_preprocessing status_timestamp_submitted status_timestamp_processing
    status_timestamp_processed
    callback { display_id host domain user active }
    responses { id sequence_number timestamp response_text }
  }
  credential(where: {operation_id: {_eq: $operationId}}, order_by: {id: asc}) {
    id operation_id task_id timestamp account realm type credential_text metadata
  }
  callback(where: {operation_id: {_eq: $operationId}}, order_by: {display_id: asc}) {
    display_id operation_id host domain user integrity_level active
    payload { payloadtype { name } }
  }
}
"""
PRESERVED_STATE_QUERY = """
query TrustWalkerPreservedState($operationId: Int!) {
  task(where: {operation_id: {_eq: $operationId}}, order_by: {display_id: asc}) {
    operation_id display_id command_name completed status timestamp
    callback { display_id host domain user active }
  }
  callback(where: {operation_id: {_eq: $operationId}}, order_by: {display_id: asc}) {
    operation_id display_id host domain user active
    payload { payloadtype { name } }
  }
  consuming_container(
    where: {name: {_eq: "sage"}, type: {_eq: "chat"}, deleted: {_eq: false}}
    order_by: {id: desc}
  ) {
    id name type container_running deleted updated_at
  }
}
"""
PRESERVED_REQUEST_QUERY = """
query TrustWalkerPreservedRequest($requestId: Int!) {
  chat_request(where: {id: {_eq: $requestId}}, limit: 1) {
    id channel_id status error updated_at
  }
}
"""
_ENGAGEMENT_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class SetupInvariantError(PilotError):
    """A rehearsal found a wrong or contaminated start that must not be retried."""


class OperatorDependencyError(PilotError):
    """The local pre-measurement dependency gate found one or more actionable defects."""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        unique = {
            (item["dependency"], item["action"]): dict(item)
            for item in problems
        }
        self.problems = list(unique.values())
        super().__init__(_format_dependency_problems(self.problems))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PilotError(f"cannot load required helper {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _orchestrate(source_root: Path = ROOT):
    path = source_root / "skills" / "sage-eval-gauge" / "scripts" / "orchestrate.py"
    identity = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    module = _load(f"trust_walker_reset_plumbing_{identity}", path)
    assert_rejected_kernel_absent()
    return module


def _native_chat(source_root: Path = ROOT):
    path = source_root / "skills" / "sage-live-runner" / "scripts" / "native_chat.py"
    identity = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    return _load(
        f"trust_walker_native_chat_{identity}",
        path,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _write_private(path: Path, value: Any) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)
    return {"path": str(path), "sha256": _sha256(path)}


def _retain_native_observation(path: Path, observed: Mapping[str, Any]) -> dict[str, str]:
    """Retain evaluator-only source evidence without replacing an earlier observation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(observed, indent=2, sort_keys=True, default=str) + "\n").encode()
    # Stage on the destination filesystem, private from creation. Linking publishes atomically
    # and fails if an earlier observation (including a symlink) already occupies this path.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".native-observation-") as temporary:
        temporary.write(data)
        temporary.flush()
        os.fsync(temporary.fileno())
        os.link(temporary.name, path)
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def _staged_foothold_fixture(
    source_root: Path = ROOT,
    alternate_start: AlternateStartBinding | None = None,
) -> dict[str, Any]:
    """Validate and return the non-secret identity of the staged foothold fixture."""
    if alternate_start is not None:
        alternate_start.validate(require_config_path=True)
        config_path = Path(alternate_start.retained_config_path).expanduser().resolve()
        expected_config_sha256 = alternate_start.retained_config_sha256
        expected_payload_uuid = alternate_start.payload_uuid
        snapshot = alternate_start.snapshot
        task_name = alternate_start.task_name
        task_prelaunch_state = alternate_start.task_prelaunch_state
        remote_path = alternate_start.remote_path
        remote_sha256 = alternate_start.remote_sha256
    else:
        config_path = (
            STAGED_FOOTHOLD_CONFIG
            if source_root.resolve() == ROOT.resolve()
            else source_root / "skills" / "sage-callback-bootstrap" / "apollo_callback_config.json"
        )
        expected_config_sha256 = STAGED_FOOTHOLD_CONFIG_SHA256
        expected_payload_uuid = STAGED_FOOTHOLD_PAYLOAD_UUID
        snapshot = SNAPSHOT
        task_name = STAGED_FOOTHOLD_TASK
        task_prelaunch_state = "Ready"
        remote_path = STAGED_FOOTHOLD_REMOTE_PATH
        remote_sha256 = STAGED_FOOTHOLD_REMOTE_SHA256
        if snapshot != "sage-seed-apollo-v252-mythic4-20260829":
            raise PilotError("staged foothold snapshot is not the qualified Apollo v2.5.2 fixture")
    if not config_path.is_file():
        raise PilotError("staged foothold retained callback config is missing")
    observed_config_sha256 = _sha256(config_path)
    if observed_config_sha256 != expected_config_sha256:
        raise PilotError("staged foothold retained callback config hash mismatch")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        observed_payload_uuid = config["config"]["payload"]["uuid"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PilotError("staged foothold retained callback config is malformed") from exc
    if observed_payload_uuid != expected_payload_uuid:
        raise PilotError("staged foothold retained callback payload UUID mismatch")
    fixture = {
        "snapshot": snapshot,
        "retained_config_sha256": observed_config_sha256,
        "payload_uuid": observed_payload_uuid,
        "task_name": task_name,
        "task_prelaunch_state": task_prelaunch_state,
        "remote_path": remote_path,
        "remote_sha256": remote_sha256,
    }
    if alternate_start is not None:
        fixture["case_identity"] = alternate_start.identity
    return fixture


class _FixtureBoundFoothold:
    """Add immutable fixture guards without changing shared rejected orchestration source."""

    def __init__(self, base: Any, fixture: Mapping[str, Any]) -> None:
        self._base = base
        self._fixture = dict(fixture)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def launch_argv(self) -> list[str]:
        return [
            *self._base.launch_argv(),
            "--task-name", str(self._fixture["task_name"]),
            "--expected-task-state", str(self._fixture["task_prelaunch_state"]),
            "--expected-remote-path", str(self._fixture["remote_path"]),
            "--expected-remote-sha256", str(self._fixture["remote_sha256"]),
        ]


def _select_unique_active_callback(value: object, expected_display_id: int) -> dict[str, Any]:
    if not isinstance(value, list):
        raise PilotError("post-reset operation callback census is malformed")
    active = [dict(row) for row in value if isinstance(row, Mapping) and row.get("active") is True]
    if len(active) != 1:
        raise PilotError("post-reset operation does not contain exactly one active callback")
    callback = active[0]
    if callback.get("display_id") != expected_display_id:
        raise PilotError("post-reset active callback does not match the selected foothold")
    return callback


def _safe_command(
    argv: list[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: Path = ROOT,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=dict(env) if env is not None else None,
            text=True, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": True, "wall_seconds": time.monotonic() - started}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "wall_seconds": time.monotonic() - started,
    }


def _call_before_deadline(deadline: float, fn, *args):
    """Interrupt a synchronous local adapter at the arm deadline on the CLI main thread."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("arm wall deadline reached")
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        return fn(*args)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(_signum, _frame):
        raise TimeoutError("arm wall deadline reached inside blocking adapter")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        return fn(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _source_identity(source_root: Path = ROOT) -> str:
    source_root = source_root.expanduser().resolve()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source_root, text=True, capture_output=True, check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"], cwd=source_root, capture_output=True, check=True,
    ).stdout
    digest = hashlib.sha256(head.encode() + b"\0" + status)
    for raw in status.split(b"\0"):
        if not raw:
            continue
        relative = raw[3:].decode(errors="surrogateescape")
        if " -> " in relative:
            relative = relative.split(" -> ", 1)[1]
        path = source_root / relative
        if path.is_file() and ".sage_history" not in path.parts and "__pycache__" not in path.parts:
            digest.update(relative.encode(errors="surrogateescape") + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _validate_protected(path: Path, source_root: Path = ROOT) -> dict[str, Any]:
    try:
        expected = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PilotError("protected-source manifest is missing or malformed") from exc
    if not isinstance(expected, dict) or set(expected) != PROTECTED_ISC11_PATHS:
        raise PilotError("protected-source manifest does not name the canonical seven ISC-11 files")
    mismatches = []
    for relative, wanted in expected.items():
        candidate = source_root / relative
        if not candidate.is_file() or _sha256(candidate) != wanted:
            mismatches.append(str(relative))
    if mismatches:
        raise PilotError("protected ISC-11 source changed: " + ", ".join(sorted(mismatches)))
    return {"count": 7, "manifest_sha256": _sha256(path)}


def _source_identity_at(source_root: Path) -> str:
    return _source_identity() if source_root.resolve() == ROOT.resolve() else _source_identity(source_root)


def _validate_protected_at(path: Path, source_root: Path) -> dict[str, Any]:
    return (
        _validate_protected(path)
        if source_root.resolve() == ROOT.resolve()
        else _validate_protected(path, source_root)
    )


def _fixture_at(
    source_root: Path,
    alternate_start: AlternateStartBinding | None = None,
) -> dict[str, Any]:
    if alternate_start is not None:
        return _staged_foothold_fixture(source_root, alternate_start)
    return (
        _staged_foothold_fixture()
        if source_root.resolve() == ROOT.resolve()
        else _staged_foothold_fixture(source_root)
    )


def _load_alternate_start(path: str | Path) -> AlternateStartBinding:
    """Load the exact private Jorah/MEEREEN descriptor without retaining its path."""
    try:
        descriptor = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError("case descriptor is missing or malformed") from exc
    top_keys = {
        "schema", "case_id", "snapshot", "range_profile", "inventory", "principal", "host",
        "host_ip", "payload_type", "retained_config", "staged_executable",
    }
    if not isinstance(descriptor, Mapping) or set(descriptor) != top_keys:
        raise PilotError("case descriptor does not contain the exact alternate-start fields")
    retained = descriptor.get("retained_config")
    executable = descriptor.get("staged_executable")
    if not isinstance(retained, Mapping) or set(retained) != {"path", "sha256", "payload_uuid"}:
        raise PilotError("case descriptor retained_config fields are incomplete")
    if not isinstance(executable, Mapping) or set(executable) != {
        "task_name", "task_prelaunch_state", "remote_path", "sha256",
    }:
        raise PilotError("case descriptor staged_executable fields are incomplete")
    inventory = descriptor.get("inventory")
    if not isinstance(inventory, list) or any(not isinstance(item, str) for item in inventory):
        raise PilotError("case descriptor inventory is malformed")
    scalar_keys = top_keys - {"inventory", "retained_config", "staged_executable"}
    if any(not isinstance(descriptor.get(key), str) for key in scalar_keys) or any(
        not isinstance(value, str) for value in retained.values()
    ) or any(not isinstance(value, str) for value in executable.values()):
        raise PilotError("case descriptor scalar fields must be strings")
    binding = AlternateStartBinding(
        schema=descriptor["schema"],
        case_id=descriptor["case_id"],
        snapshot=descriptor["snapshot"],
        range_profile=descriptor["range_profile"],
        inventory=tuple(inventory),
        principal=descriptor["principal"],
        host=descriptor["host"],
        host_ip=descriptor["host_ip"],
        payload_type=descriptor["payload_type"],
        retained_config_path=str(Path(retained["path"]).expanduser().resolve()),
        retained_config_sha256=retained["sha256"],
        payload_uuid=retained["payload_uuid"],
        task_name=executable["task_name"],
        task_prelaunch_state=executable["task_prelaunch_state"],
        remote_path=executable["remote_path"],
        remote_sha256=executable["sha256"],
    )
    binding.validate(require_config_path=True)
    return binding


def _route(orchestrate) -> dict[str, str]:
    route = orchestrate.load_treatment_route(
        orchestrate.DEFAULT_ROUTE_ENV,
        "sonnet",
        orchestrate._load_sage_model_environment(),
    )
    if route["provider"] != PROVIDER or route["model"] != MODEL:
        raise PilotError("configured treatment is not the authorized Sonnet route")
    parsed = urlparse(route["api_endpoint"])
    if not parsed.hostname or parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}:
        raise PilotError("configured Sonnet route is missing or loopback")
    return route


def _safe_route_identity(route: Mapping[str, str]) -> dict[str, str]:
    endpoint = urlparse(route["api_endpoint"])
    return {
        "provider": route["provider"],
        "model": route["model"],
        "endpoint_identity": hashlib.sha256(
            f"{endpoint.scheme}://{endpoint.hostname}:{endpoint.port or ''}{endpoint.path}".encode()
        ).hexdigest(),
    }


def _validate_route_smoke(path: Path, route: Mapping[str, str]) -> dict[str, Any]:
    try:
        smoke = json.loads(path.read_text(encoding="utf-8"))
        detail = smoke["detail"]
        backend = detail["observed_backend"]
        usage = detail["usage"]
    except Exception as exc:
        raise PilotError("route smoke evidence is missing or malformed") from exc
    if (
        smoke.get("ok") is not True
        or detail.get("configured_provider") != PROVIDER
        or detail.get("configured_model") != MODEL
        or backend.get("effective_backend") != EXPECTED_BACKEND
        or backend.get("observed_provider") != PROVIDER
        or backend.get("observed_model") != MODEL
        or type(usage.get("total_tokens")) is not int
        or usage["total_tokens"] <= 0
    ):
        raise PilotError("route smoke does not attest the authorized effective backend")
    probe_path = path.with_name("sonnet-runtime-route-probe.json")
    try:
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PilotError("route catalog probe is missing or malformed") from exc
    endpoint = urlparse(route["api_endpoint"])
    if (
        probe.get("provider") != PROVIDER
        or probe.get("model") != MODEL
        or probe.get("endpoint_host") != endpoint.hostname
        or probe.get("endpoint_port") != (endpoint.port or (443 if endpoint.scheme == "https" else 80))
        or str(probe.get("endpoint_path") or "") != str(endpoint.path or "")
        or probe.get("reachable") is not True
        or probe.get("listed") is not True
        or probe.get("inference_calls") != 0
    ):
        raise PilotError("route catalog probe does not bind the configured endpoint")
    return {
        "sha256": _sha256(path),
        "usage": usage,
        "effective_backend": EXPECTED_BACKEND,
        "endpoint_identity": _safe_route_identity(route)["endpoint_identity"],
        "route_probe_sha256": _sha256(probe_path),
        "binding": "configured endpoint probe plus response-derived backend smoke",
    }


def _validate_external_paths() -> dict[str, str]:
    mythic_env = Path(os.environ.get("MYTHIC_ENV_PATH", "")).expanduser()
    mythic_cli = Path(os.environ.get("MYTHIC_CLI", "")).expanduser()
    bloodhound = Path(os.environ.get("SAGE_BLOODHOUND_MCP_DIR", "")).expanduser()
    if not mythic_env.is_file():
        raise PilotError("MYTHIC_ENV_PATH is missing or not a file")
    if not mythic_cli.is_file() or not os.access(mythic_cli, os.X_OK):
        raise PilotError("MYTHIC_CLI is missing or not executable")
    if mythic_env.resolve().parent != mythic_cli.resolve().parent:
        raise PilotError("MYTHIC_ENV_PATH and MYTHIC_CLI identify different installs")
    if not bloodhound.is_dir():
        raise PilotError("SAGE_BLOODHOUND_MCP_DIR is missing or not a directory")
    return {
        "mythic_install": hashlib.sha256(str(mythic_cli.resolve().parent).encode()).hexdigest(),
        "bloodhound_checkout": hashlib.sha256(str(bloodhound.resolve()).encode()).hexdigest(),
    }


def _env_assignments(path: Path) -> dict[str, str]:
    """Read simple operator env assignments without exporting or exposing their values."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError("contains a non-assignment line")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("contains an invalid assignment name")
        values[key] = value.strip().strip("'\"")
    return values


def _dependency_problem(identifier: str, action: str) -> dict[str, str]:
    return {"dependency": identifier, "action": action}


def _format_dependency_problems(problems: list[dict[str, str]]) -> str:
    lines = ["operator dependency preflight failed:"]
    for item in sorted(problems, key=lambda row: (row["dependency"], row["action"])):
        lines.append(f"- {item['dependency']}: {item['action']}")
    return "\n".join(lines)


def _configured_path_matches(configured: str, observed: Path) -> bool:
    if not configured:
        return False
    return Path(configured).expanduser().resolve() == observed.expanduser().resolve()


def _live_contract_binding(contract: PilotContract | ImprovementContract) -> dict[str, Any]:
    """Return the identity-independent live semantics a standing authority freezes."""

    record = contract.to_record()
    keys = (
        "schema",
        "provider",
        "model",
        "objective_sha256",
        "wall_seconds",
        "token_ceiling",
        "payload_task_limit",
        "setup_cleanup_seconds",
        "task_settlement_seconds",
        "recovery_cleanup_attempt_limit",
        "setup_rehearsal_successes",
        "setup_rehearsal_attempt_limit",
        "runs_per_arm",
        "primary_metric",
        "timing_policy",
        "case_identity",
    )
    return {key: record[key] for key in keys if key in record}


def _load_standing_live_authority(
    path: Path,
    *,
    command: str,
    contract: PilotContract | ImprovementContract,
) -> dict[str, Any]:
    """Validate one durable authority without trusting its filename or caller-supplied ID."""

    try:
        raw = path.expanduser().resolve().read_text(encoding="utf-8")
        authority = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError("standing live authority is missing or malformed") from exc
    if not isinstance(authority, Mapping):
        raise PilotError("standing live authority must be a JSON object")
    authority_id = str(authority.get("authority_id") or "").strip()
    if (
        authority.get("schema") != STANDING_LIVE_AUTHORITY_SCHEMA
        or not authority_id
        or authority.get("status") != "active"
        or authority.get("revoked_at") is not None
    ):
        raise PilotError("standing live authority is absent, inactive, or revoked")
    if authority.get("scope_id") != STANDING_LIVE_AUTHORITY_SCOPE:
        raise PilotError("standing live authority names a different environment scope")
    allowed_commands = authority.get("allowed_commands")
    if (
        not isinstance(allowed_commands, list)
        or set(map(str, allowed_commands)) != STANDING_LIVE_COMMANDS
        or command not in allowed_commands
    ):
        raise PilotError("standing live authority does not allow this live command")
    operations = authority.get("allowed_operation_classes")
    if not isinstance(operations, list) or set(map(str, operations)) != STANDING_LIVE_OPERATION_CLASSES:
        raise PilotError("standing live authority operation classes are incomplete or broader than expected")
    exclusions = authority.get("stop_and_reauthorize_on")
    if not isinstance(exclusions, list) or set(map(str, exclusions)) != STANDING_LIVE_EXCLUSIONS:
        raise PilotError("standing live authority stop boundaries are incomplete or changed")
    environment = authority.get("environment_binding")
    if not isinstance(environment, Mapping) or dict(environment) != {
        "range_profile": RANGE_PROFILE,
        "snapshot": SNAPSHOT,
        "target_domain": "essos.local",
        "target_io": "Mythic payload tasks only",
    }:
        raise PilotError("standing live authority environment binding does not match Trust Walker")
    expected_binding = _live_contract_binding(contract)
    bindings = authority.get("contract_bindings")
    if (
        not isinstance(bindings, Mapping)
        or bindings.get(expected_binding["schema"]) != expected_binding
    ):
        raise PilotError("standing live authority does not bind the exact frozen live contract")
    successor_policy = authority.get("successor_policy")
    if not isinstance(successor_policy, Mapping) or dict(successor_policy) != {
        "source_identity_changes_allowed": True,
        "requires_in_scope_evaluator_or_staging_repair": True,
        "requires_distinct_source_roots": True,
        "requires_preledger_dependency_gate": True,
        "requires_unchanged_contract_binding": True,
    }:
        raise PilotError("standing live authority successor policy is missing or changed")
    persistence = authority.get("persistence")
    if not isinstance(persistence, Mapping) or {
        "consumed_by_completed_run": persistence.get("consumed_by_completed_run"),
        "requires_new_conversation_authorization_for_in_scope_successor": persistence.get(
            "requires_new_conversation_authorization_for_in_scope_successor"
        ),
    } != {
        "consumed_by_completed_run": False,
        "requires_new_conversation_authorization_for_in_scope_successor": False,
    }:
        raise PilotError("standing live authority persistence policy is missing or changed")
    endpoint_identity = str(authority.get("endpoint_identity") or "")
    if len(endpoint_identity) != 64 or any(char not in "0123456789abcdef" for char in endpoint_identity):
        raise PilotError("standing live authority endpoint identity is invalid")
    return {
        "mode": "standing",
        "schema": STANDING_LIVE_AUTHORITY_SCHEMA,
        "authority_id": authority_id,
        "authority_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "scope_id": STANDING_LIVE_AUTHORITY_SCOPE,
        "granted_at": str(authority.get("granted_at") or ""),
        "command": command,
        "contract_binding_sha256": _json_hash(expected_binding),
        "expected_endpoint_identity": endpoint_identity,
        "expected_snapshot": SNAPSHOT,
    }


def _resolve_live_authority(
    args: argparse.Namespace,
    contract: PilotContract | ImprovementContract,
) -> dict[str, Any]:
    authority_id = str(getattr(args, "live_authority_id", "") or "").strip()
    authority_file = str(getattr(args, "live_authority_file", "") or "").strip()
    if isinstance(contract, PilotContract) and contract.alternate_start is not None and authority_file:
        raise PilotError("standing live authority cannot authorize the alternate Jorah/MEEREEN case")
    if authority_file:
        receipt = _load_standing_live_authority(
            Path(authority_file), command=str(args.command), contract=contract,
        )
        if authority_id and authority_id != receipt["authority_id"]:
            raise PilotError("caller authority ID does not match the standing authority")
        return receipt
    if not authority_id:
        raise PilotError("live authority identity or standing authority file is required")
    return {
        "mode": "invocation",
        "authority_id": authority_id,
        "command": str(args.command),
        "contract_binding_sha256": _json_hash(_live_contract_binding(contract)),
    }


def _bind_authority_to_preledger(
    receipt: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> None:
    """Fail a standing improvement run before ledger allocation on route/snapshot drift."""

    if receipt.get("mode") != "standing":
        return
    route = prepared.get("route_identity")
    if (
        prepared.get("ready") is not True
        or prepared.get("ledger_allocated") is not False
        or prepared.get("attempted_start") is not False
        or not isinstance(route, Mapping)
        or route.get("endpoint_identity") != receipt.get("expected_endpoint_identity")
        or route.get("provider") != PROVIDER
        or route.get("model") != MODEL
    ):
        raise PilotError("standing live authority does not match the prepared route or preledger boundary")


def _authority_preflight_receipt(
    receipt: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    safe = dict(receipt)
    if receipt.get("mode") != "standing":
        return safe
    route = preflight.get("route")
    fixture = (preflight.get("foothold_bootstrap") or {}).get("fixture")
    if (
        not isinstance(route, Mapping)
        or route.get("endpoint_identity") != receipt.get("expected_endpoint_identity")
        or route.get("provider") != PROVIDER
        or route.get("model") != MODEL
        or not isinstance(fixture, Mapping)
        or fixture.get("snapshot") != receipt.get("expected_snapshot")
    ):
        raise PilotError("standing live authority does not match the runtime route or snapshot")
    safe["runtime_binding_verified"] = True
    return safe


_STAGED_TLS_PROBE = r"""
import json
from pathlib import Path
import ssl
import sys

root = Path(sys.argv[1]).resolve()
payload = root / "Payload_Type" / "sage"
sys.path.insert(0, str(payload))
import ssl_bootstrap

expected_custom = (root / "Payload_Type" / "sage" / "certs" / "bundle.pem").resolve()
observed_custom = Path(ssl_bootstrap.DEFAULT_SAGE_CUSTOM_CA_BUNDLE_PATH).resolve()
if observed_custom != expected_custom:
    raise RuntimeError("TLS dependency contract does not match the production consumer")

expected_combined = (root / "Payload_Type" / "sage" / "certs" / "combined-bundle.pem").resolve()
environ = {}
selected = ssl_bootstrap.configure_sage_ssl_cert_file(environ=environ)
if not selected or Path(selected).resolve() != expected_combined:
    raise RuntimeError("production TLS bootstrap did not select the staged combined bundle")
if environ.get("SSL_CERT_FILE") != selected:
    raise RuntimeError("production TLS bootstrap did not export SSL_CERT_FILE")

ssl.create_default_context(cafile=str(expected_custom))
ssl.create_default_context(cafile=selected)
print(json.dumps({"ready": True, "network_activity": False}, sort_keys=True))
"""


def _probe_staged_tls_trust(source_root: Path, python: Path) -> bool:
    """Exercise the selected tree's real root-relative TLS producer without network access."""

    try:
        probe = subprocess.run(
            [str(python), "-c", _STAGED_TLS_PROBE, str(source_root.resolve())],
            cwd=source_root,
            text=True,
            capture_output=True,
            timeout=30,
        )
        result = json.loads(probe.stdout) if probe.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    return result == {"network_activity": False, "ready": True}


_STAGED_RUNTIME_TOOLS_PROBE = r"""
import json
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
expected_names = json.loads(sys.argv[2])
required_names = json.loads(sys.argv[3])
payload = root / "Payload_Type" / "sage"
sys.path.insert(0, str(payload))
from ai.langgraph import ttp_library

expected_dir = (payload / "tools").resolve()
if ttp_library.TOOLS_DIR.resolve() != expected_dir:
    raise RuntimeError("runtime-tool dependency contract does not match the production producer")
frontmatter, _ = ttp_library.load_ttp("sharphound")
if not frontmatter or frontmatter.get("binary_filename") != "SharpHound.exe":
    raise RuntimeError("Trust Walker collection metadata no longer selects SharpHound.exe")
if any(name not in expected_names for name in required_names):
    raise RuntimeError("required Trust Walker runtime tool is absent from the staged inventory")
for name in expected_names:
    path = ttp_library.TOOLS_DIR / name
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("staged runtime tool is missing or empty")
    with path.open("rb") as handle:
        prefix = handle.read(2)
    if name in required_names and prefix != b"MZ":
        raise RuntimeError("required Trust Walker runtime tool is not a loadable PE input")
print(json.dumps({"network_activity": False, "ready": True}, sort_keys=True))
"""


def _probe_staged_runtime_tools(
    source_root: Path,
    python: Path,
    expected_names: tuple[str, ...],
) -> bool:
    """Exercise root-relative runtime-tool resolution through the selected tree."""

    try:
        probe = subprocess.run(
            [
                str(python), "-c", _STAGED_RUNTIME_TOOLS_PROBE,
                str(source_root.resolve()), json.dumps(expected_names),
                json.dumps(TRUST_WALKER_REQUIRED_RUNTIME_TOOLS),
            ],
            cwd=source_root,
            text=True,
            capture_output=True,
            timeout=30,
        )
        result = json.loads(probe.stdout) if probe.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    return result == {"network_activity": False, "ready": True}


def _operator_runtime_tool_inventory(
    config_root: Path,
) -> tuple[tuple[tuple[str, Path], ...], list[dict[str, str]]]:
    """Inventory ignored operator runtime tools without reading or reporting their bytes."""

    tools_dir = config_root / OPERATOR_RUNTIME_TOOLS_DIR
    if not tools_dir.is_dir():
        return (), [_dependency_problem(
            f"operator-config:{OPERATOR_RUNTIME_TOOLS_DIR}",
            "restore the operator-local runtime-tool drop zone in the configured checkout",
        )]
    inventory: list[tuple[str, Path]] = []
    problems: list[dict[str, str]] = []
    for source in sorted(tools_dir.iterdir(), key=lambda path: path.name.casefold()):
        if source.name == ".keep":
            continue
        if not source.is_file() or source.stat().st_size <= 0 or not os.access(source, os.R_OK):
            problems.append(_dependency_problem(
                f"operator-config:{OPERATOR_RUNTIME_TOOLS_DIR}",
                "remove unreadable, empty, or non-file entries from the runtime-tool drop zone",
            ))
            continue
        inventory.append((source.name, source))
    names = {name for name, _source in inventory}
    for required in TRUST_WALKER_REQUIRED_RUNTIME_TOOLS:
        if required not in names:
            problems.append(_dependency_problem(
                f"operator-config:{OPERATOR_RUNTIME_TOOLS_DIR}/{required}",
                "restore the required Trust Walker collection tool in the configured checkout",
            ))
    return tuple(inventory), problems


def _host_display_ready() -> bool:
    selected = str(os.environ.get("SAGE_RDP_DISPLAY") or "").strip()
    displays = [selected] if selected else [":0", ":99"]
    for display in displays:
        suffix = display.lstrip(":").split(".", 1)[0]
        socket_path = Path("/tmp/.X11-unix") / f"X{suffix}"
        if not socket_path.exists():
            continue
        if display == ":0":
            authority = str(os.environ.get("XAUTHORITY") or "").strip()
            if not authority or not Path(authority).expanduser().is_file():
                continue
        return True
    return False


def _stage_operator_links(
    operator_config_root: Path,
    source_roots: Mapping[str, Path],
) -> tuple[list[str], list[dict[str, str]]]:
    """Link sensitive ignored configuration into scratch trees without copying its bytes."""
    problems: list[dict[str, str]] = []
    planned: list[tuple[Path, Path, str]] = []
    config_root = operator_config_root.expanduser().resolve()
    if not config_root.is_dir():
        return [], [_dependency_problem(
            "operator-config-root", "set --operator-config-root to the configured Sage checkout",
        )]
    runtime_tools, runtime_problems = _operator_runtime_tool_inventory(config_root)
    problems.extend(runtime_problems)
    for relative in OPERATOR_LOCAL_LINKS:
        source = config_root / relative
        if not source.is_file():
            problems.append(_dependency_problem(
                f"operator-config:{relative}",
                "create the required operator-local dependency in the configured checkout",
            ))
            continue
        for arm, root in source_roots.items():
            target = root / relative
            if not root.is_dir():
                problems.append(_dependency_problem(
                    f"{arm}:source-root", "provide an existing reconstructed source directory",
                ))
                continue
            if target.is_symlink():
                if target.resolve() != source.resolve():
                    problems.append(_dependency_problem(
                        f"{arm}:{relative}",
                        "replace the inconsistent link with the configured operator dependency",
                    ))
            elif target.exists():
                problems.append(_dependency_problem(
                    f"{arm}:{relative}",
                    "remove the copied/conflicting item so staging can create a non-copying link",
                ))
            else:
                planned.append((source, target, f"{arm}:{relative}"))
    expected_tool_names = {name for name, _source in runtime_tools}
    for arm, root in source_roots.items():
        staged_tools = root / OPERATOR_RUNTIME_TOOLS_DIR
        if staged_tools.is_dir():
            observed = {entry.name for entry in staged_tools.iterdir() if entry.name != ".keep"}
            if observed - expected_tool_names:
                problems.append(_dependency_problem(
                    f"{arm}:runtime-tool-drop-zone",
                    "remove entries that are not linked from the configured operator checkout",
                ))
    for name, source in runtime_tools:
        relative = f"{OPERATOR_RUNTIME_TOOLS_DIR}/{name}"
        for arm, root in source_roots.items():
            target = root / relative
            if not root.is_dir():
                problems.append(_dependency_problem(
                    f"{arm}:source-root", "provide an existing reconstructed source directory",
                ))
                continue
            if target.is_symlink():
                if target.resolve() != source.resolve():
                    problems.append(_dependency_problem(
                        f"{arm}:{relative}",
                        "replace the inconsistent link with the configured operator runtime tool",
                    ))
            elif target.exists():
                problems.append(_dependency_problem(
                    f"{arm}:{relative}",
                    "remove the copied/conflicting item so staging can create a non-copying link",
                ))
            else:
                planned.append((source, target, f"{arm}:{relative}"))
    operator_venv = config_root / OPERATOR_LOCAL_VENV
    if not operator_venv.is_dir():
        problems.append(_dependency_problem(
            f"operator-config:{OPERATOR_LOCAL_VENV}",
            "create the operator virtual environment used to reconstruct both scratch trees",
        ))
    for arm, root in source_roots.items():
        staged_venv = root / OPERATOR_LOCAL_VENV
        if not staged_venv.is_dir() or staged_venv.is_symlink():
            problems.append(_dependency_problem(
                f"{arm}:{OPERATOR_LOCAL_VENV}",
                "materialize a real ignored virtual-environment directory before staging",
            ))
    if problems:
        return [], problems
    materialized: list[str] = []
    for source, target, identifier in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source.resolve(), target_is_directory=source.is_dir())
        materialized.append(identifier)
    return sorted(materialized), []


def _validate_operator_dependencies(
    *,
    operator_config_root: Path,
    source_roots: Mapping[str, Path],
    source_identities: Mapping[str, str],
    protected_manifest: Path,
    route_smoke: Path,
) -> dict[str, Any]:
    """Validate every local dependency consumed before, during, or after a measured arm.

    This is deliberately local-only: it reads files, imports the selected source helpers, and
    executes interpreter probes, but never calls a control plane, provider, or target.
    """
    problems: list[dict[str, str]] = []
    config_root = operator_config_root.expanduser().resolve()
    if not config_root.is_dir():
        problems.append(_dependency_problem(
            "operator-config-root", "set --operator-config-root to the configured Sage checkout",
        ))
    roots = {arm: path.expanduser().resolve() for arm, path in source_roots.items()}
    runtime_tools, runtime_tool_problems = _operator_runtime_tool_inventory(config_root)
    problems.extend(runtime_tool_problems)
    runtime_tool_names = tuple(name for name, _source in runtime_tools)
    if set(roots) != set(IMPROVEMENT_ARM_ORDER):
        problems.append(_dependency_problem(
            "source-roots", "provide exactly one baseline-sage and one candidate-sage root",
        ))
    elif len(set(roots.values())) != 2:
        problems.append(_dependency_problem(
            "source-roots", "reconstruct distinct baseline and candidate roots",
        ))

    root_env: dict[str, str] = {}
    payload_env: dict[str, str] = {}
    try:
        root_env = _env_assignments(config_root / ".env")
    except (OSError, ValueError):
        problems.append(_dependency_problem(
            "operator-config:.env", "use a readable file containing only valid environment assignments",
        ))
    try:
        payload_env = _env_assignments(config_root / "Payload_Type/sage/.env.local")
    except (OSError, ValueError):
        problems.append(_dependency_problem(
            "operator-config:Payload_Type/sage/.env.local",
            "use a readable file containing only valid environment assignments",
        ))

    mythic_env = Path(str(os.environ.get("MYTHIC_ENV_PATH") or "")).expanduser()
    mythic_cli = Path(str(os.environ.get("MYTHIC_CLI") or "")).expanduser()
    bloodhound = Path(str(os.environ.get("SAGE_BLOODHOUND_MCP_DIR") or "")).expanduser()
    if not mythic_env.is_file():
        problems.append(_dependency_problem(
            "external:MYTHIC_ENV_PATH", "export the absolute path to the selected Mythic install .env",
        ))
    if not mythic_cli.is_file() or not os.access(mythic_cli, os.X_OK):
        problems.append(_dependency_problem(
            "external:MYTHIC_CLI", "export the executable mythic-cli path for the selected install",
        ))
    if mythic_env.is_file() and mythic_cli.is_file() and mythic_env.resolve().parent != mythic_cli.resolve().parent:
        problems.append(_dependency_problem(
            "external:mythic-install-pair", "point MYTHIC_ENV_PATH and MYTHIC_CLI at the same install",
        ))
    if root_env and (
        not _configured_path_matches(root_env.get("MYTHIC_ENV_PATH", ""), mythic_env)
        or not _configured_path_matches(root_env.get("MYTHIC_CLI", ""), mythic_cli)
    ):
        problems.append(_dependency_problem(
            "operator-config:.env", "make its Mythic paths match the exported selected install",
        ))
    if mythic_env.is_file():
        try:
            mythic_values = _env_assignments(mythic_env)
            missing = [key for key in MYTHIC_ENV_KEYS if not mythic_values.get(key)]
            if missing:
                problems.append(_dependency_problem(
                    "external:MYTHIC_ENV_PATH",
                    "set required Mythic keys: " + ", ".join(missing),
                ))
        except (OSError, ValueError):
            problems.append(_dependency_problem(
                "external:MYTHIC_ENV_PATH", "use a readable file containing valid environment assignments",
            ))

    if not bloodhound.is_dir():
        problems.append(_dependency_problem(
            "external:SAGE_BLOODHOUND_MCP_DIR", "export the selected BloodHound MCP checkout directory",
        ))
    else:
        for relative in ("main.py", "pyproject.toml", ".env"):
            if not (bloodhound / relative).is_file():
                problems.append(_dependency_problem(
                    f"external:bloodhound/{relative}", "restore the required BloodHound integration file",
                ))
        try:
            bloodhound_values = _env_assignments(bloodhound / ".env")
            missing = [key for key in BLOODHOUND_ENV_KEYS if not bloodhound_values.get(key)]
            if missing:
                problems.append(_dependency_problem(
                    "external:bloodhound/.env",
                    "set required BloodHound keys: " + ", ".join(missing),
                ))
        except (OSError, ValueError):
            problems.append(_dependency_problem(
                "external:bloodhound/.env", "use a readable file containing valid environment assignments",
            ))
    if payload_env and not _configured_path_matches(
        payload_env.get("SAGE_BLOODHOUND_MCP_DIR", ""), bloodhound,
    ):
        problems.append(_dependency_problem(
            "operator-config:Payload_Type/sage/.env.local",
            "make SAGE_BLOODHOUND_MCP_DIR match the exported selected checkout",
        ))

    for executable in HOST_EXECUTABLES:
        if shutil.which(executable) is None:
            problems.append(_dependency_problem(
                f"host-executable:{executable}", f"install {executable} on the operator host",
            ))
    if not _host_display_ready():
        problems.append(_dependency_problem(
            "host:interactive-display",
            "start Xwayland :0 or Xvfb :99 and configure its authority before callback launch",
        ))

    if not protected_manifest.is_file():
        problems.append(_dependency_problem(
            "input:protected-manifest", "provide the retained protected-source manifest",
        ))
    if not route_smoke.is_file():
        problems.append(_dependency_problem(
            "input:route-smoke", "provide the retained response-derived route smoke",
        ))
    if not route_smoke.with_name("sonnet-runtime-route-probe.json").is_file():
        problems.append(_dependency_problem(
            "input:route-catalog-probe", "place the retained catalog probe beside the route smoke",
        ))

    route_identities: dict[str, dict[str, str]] = {}
    venv_signatures: dict[str, str] = {}
    for arm in IMPROVEMENT_ARM_ORDER:
        root = roots.get(arm)
        expected = source_identities.get(arm)
        if root is None or not root.is_dir():
            continue
        for relative in OPERATOR_LOCAL_LINKS:
            target = root / relative
            source = config_root / relative
            if not target.is_symlink() or target.resolve() != source.resolve():
                problems.append(_dependency_problem(
                    f"{arm}:{relative}", "rerun staging to create the exact non-copying operator link",
                ))
        for name, source in runtime_tools:
            relative = f"{OPERATOR_RUNTIME_TOOLS_DIR}/{name}"
            target = root / relative
            if not target.is_symlink() or target.resolve() != source.resolve():
                problems.append(_dependency_problem(
                    f"{arm}:{relative}",
                    "rerun staging to create the exact non-copying operator runtime-tool link",
                ))
        staged_tools = root / OPERATOR_RUNTIME_TOOLS_DIR
        if staged_tools.is_dir():
            observed = {entry.name for entry in staged_tools.iterdir() if entry.name != ".keep"}
            if observed - set(runtime_tool_names):
                problems.append(_dependency_problem(
                    f"{arm}:runtime-tool-drop-zone",
                    "remove entries that are not linked from the configured operator checkout",
                ))
        mcp_path = root / ".mcp.json"
        try:
            mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
            server_name = str(os.environ.get("SAGE_LUDUS_MCP_SERVER") or "ludus").strip() or "ludus"
            server = (mcp.get("mcpServers") or mcp).get(server_name, {})
            ludus_env = server.get("env") if isinstance(server, Mapping) else None
            if (
                not isinstance(ludus_env, Mapping)
                or not str(ludus_env.get("LUDUS_URL") or "").strip()
                or not str(ludus_env.get("LUDUS_API_KEY") or "").strip()
            ):
                raise ValueError("selected Ludus binding is incomplete")
        except (OSError, ValueError, TypeError, AttributeError):
            problems.append(_dependency_problem(
                f"{arm}:.mcp.json",
                "repair the selected Ludus server binding with nonblank URL and API key",
            ))
        python = root / OPERATOR_LOCAL_VENV / "bin" / "python"
        if not python.is_file() or not os.access(python, os.X_OK):
            problems.append(_dependency_problem(
                f"{arm}:{OPERATOR_LOCAL_VENV}", "provide an executable staged virtual environment",
            ))
        else:
            probe = subprocess.run(
                [str(python), "-c", "import mythic_container, sys; print(sys.version_info[:3])"],
                cwd=root, text=True, capture_output=True, timeout=30,
            )
            if probe.returncode != 0:
                problems.append(_dependency_problem(
                    f"{arm}:{OPERATOR_LOCAL_VENV}", "install the runtime dependencies including mythic_container",
                ))
            else:
                venv_signatures[arm] = hashlib.sha256(probe.stdout.strip().encode()).hexdigest()
                if not _probe_staged_tls_trust(root, python):
                    problems.append(_dependency_problem(
                        f"{arm}:sage-tls-trust",
                        "stage a readable, valid operator CA bundle matching the Sage TLS bootstrap contract",
                    ))
                if not _probe_staged_runtime_tools(root, python, runtime_tool_names):
                    problems.append(_dependency_problem(
                        f"{arm}:runtime-tool-drop-zone",
                        "stage readable operator runtime tools matching the selected tree's production consumer",
                    ))
        if not expected:
            problems.append(_dependency_problem(
                f"{arm}:source-identity", "provide the expected canonical source identity",
            ))
        else:
            try:
                observed = _source_identity_at(root)
            except (OSError, subprocess.SubprocessError):
                observed = ""
            if observed != expected:
                problems.append(_dependency_problem(
                    f"{arm}:source-identity", "reconstruct the exact expected source tree before execution",
                ))
        if protected_manifest.is_file():
            try:
                _validate_protected_at(protected_manifest, root)
            except PilotError:
                problems.append(_dependency_problem(
                    f"{arm}:protected-source", "restore the exact seven-file protected source set",
                ))
        try:
            _fixture_at(root)
        except PilotError:
            problems.append(_dependency_problem(
                f"{arm}:retained-fixture", "restore the exact retained Apollo callback configuration",
            ))
        try:
            selected = _orchestrate(root)
            if not selected._resolve_password_source():
                problems.append(_dependency_problem(
                    f"{arm}:run-as-credential", "configure the operator run-as credential source",
                ))
            route = _route(selected)
            route_identities[arm] = _safe_route_identity(route)
            if route_smoke.is_file() and route_smoke.with_name("sonnet-runtime-route-probe.json").is_file():
                _validate_route_smoke(route_smoke, route)
        except (PilotError, SystemExit, OSError, RuntimeError, ValueError):
            problems.append(_dependency_problem(
                f"{arm}:route-binding", "repair the staged evaluator/runtime route and retained smoke binding",
            ))

    if len(set(venv_signatures.values())) > 1:
        problems.append(_dependency_problem(
            "staged-venv-symmetry", "use compatible virtual environments for both source roots",
        ))
    if len(route_identities) == 2 and len({_json_hash(value) for value in route_identities.values()}) != 1:
        problems.append(_dependency_problem(
            "staged-route-symmetry", "bind baseline and candidate to the same provider/model/endpoint",
        ))
    if problems:
        raise OperatorDependencyError(problems)
    route_identity = dict(route_identities[IMPROVEMENT_ARM_ORDER[0]])
    return {
        "ready": True,
        "network_activity": False,
        "ledger_allocated": False,
        "attempted_start": False,
        "source_roots_distinct": True,
        "route_identity": route_identity,
        "validated_dependencies": [
            "operator-config-links",
            "staged-virtualenv",
            "sage-tls-trust-bootstrap",
            "runtime-tool-drop-zone",
            "canonical-source-identities",
            "protected-source-manifest",
            "retained-foothold-fixture",
            "mythic-install-pair",
            "bloodhound-integration",
            "run-as-credential-source",
            "route-smoke-binding",
            "host-reset-and-launch-prerequisites",
        ],
    }


def prepare_improvement_dependencies(
    *,
    operator_config_root: Path,
    source_roots: Mapping[str, Path],
    source_identities: Mapping[str, str],
    protected_manifest: Path,
    route_smoke: Path,
) -> dict[str, Any]:
    materialized, problems = _stage_operator_links(operator_config_root, source_roots)
    try:
        report = _validate_operator_dependencies(
            operator_config_root=operator_config_root,
            source_roots=source_roots,
            source_identities=source_identities,
            protected_manifest=protected_manifest,
            route_smoke=route_smoke,
        )
    except OperatorDependencyError as exc:
        problems.extend(exc.problems)
        report = {}
    if problems:
        raise OperatorDependencyError(problems)
    report["materialized_links"] = materialized
    return report


def _tool_contract() -> str:
    bh_specs, bh_registry, upload = bare_bloodhound.load_bloodhound_mcp_surface()
    if upload is None:
        raise PilotError("raw BloodHound collection adapter is unavailable")
    payload = {
        "raw_mythic": bare_mythic_tools.bare_tool_specs(),
        "raw_bloodhound": bh_specs,
        "raw_bloodhound_names": sorted(bh_registry),
    }
    return _json_hash(payload)


def _inspect_response_records(
    records: object, *, model_calls: object,
) -> tuple[int | None, list[dict[str, Any]], bool]:
    if type(model_calls) is not int or model_calls < 0 or not isinstance(records, list) or len(records) != model_calls:
        raise PilotError("model response accounting is incomplete")
    total = 0
    usage_complete = True
    safe = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, Mapping):
            raise PilotError("model response record is malformed")
        observed_provider = record.get("observed_provider") or record.get("effective_model_provider")
        observed_model = record.get("observed_model") or record.get("effective_model_id")
        if (
            record.get("effective_backend") != EXPECTED_BACKEND
            or observed_provider != PROVIDER
            or observed_model != MODEL
        ):
            raise PilotError("response-derived model route mismatch")
        usage = record.get("token_usage") if isinstance(record.get("token_usage"), Mapping) else record
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        value = usage.get("total_tokens")
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or type(value) is not int
            or min(input_tokens, output_tokens, value) < 0
            or value != input_tokens + output_tokens
        ):
            usage_complete = False
            value = None
        else:
            total += value
        safe.append({
            "response_index": index,
            "effective_backend": EXPECTED_BACKEND,
            "total_tokens": value,
        })
    return total if usage_complete else None, safe, usage_complete


def _validate_response_records(records: object, *, model_calls: object) -> tuple[int, list[dict[str, Any]]]:
    total, safe, usage_complete = _inspect_response_records(records, model_calls=model_calls)
    if not usage_complete or total is None:
        raise PilotError("model response usage is incomplete")
    return total, safe


def _first_response_prefix_satisfying(
    rows: object,
    predicate: Callable[[str], bool],
) -> dict[str, Any] | None:
    """Return the first response prefix that independently satisfies a proof predicate."""
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return None

    def order_key(row: Mapping[str, Any]) -> tuple[int, int, int]:
        sequence = row.get("sequence_number")
        response_id = row.get("id")
        if type(sequence) is int:
            return (0, sequence, response_id if type(response_id) is int else 0)
        return (1, response_id if type(response_id) is int else 0, 0)

    prefix_rows: list[Mapping[str, Any]] = []
    for row in sorted(rows, key=order_key):
        prefix_rows.append(row)
        output = live_seams._decode_mythic_response_rows(list(prefix_rows))
        if predicate(output):
            return {
                "output": output,
                "effect_at": row.get("timestamp"),
                "response_id": row.get("id"),
            }
    return None


def _shipping_response_accounting(
    telemetry: Mapping[str, Any],
) -> tuple[int, int, int | None, list[dict[str, Any]], str | None, str | None]:
    """Separate sent shipping requests from completed, usage-bearing responses."""
    attempts = telemetry.get("model_calls")
    records = telemetry.get("model_response_records") or telemetry.get("effective_backend_requests")
    completed = len(records) if isinstance(records, list) else -1
    route_error = None
    safe_records: list[dict[str, Any]] = []
    completed_total: int | None = None
    completed_usage = False
    try:
        completed_total, safe_records, completed_usage = _inspect_response_records(
            records, model_calls=completed,
        )
    except PilotError as exc:
        route_error = str(exc)
    cost_error = None
    if type(attempts) is not int or attempts < 0 or completed < 0 or completed > attempts:
        cost_error = "shipping provider request accounting is malformed"
    elif attempts != completed or telemetry.get("token_usage_complete") is not True or not completed_usage:
        cost_error = "provider request has no authoritative response usage"
    total_tokens = completed_total if route_error is None and cost_error is None else None
    return (
        attempts if type(attempts) is int and attempts >= 0 else -1,
        max(0, completed),
        total_tokens,
        safe_records,
        route_error,
        cost_error,
    )


def _task_text(row: Mapping[str, Any]) -> str:
    return json.dumps(
        [row.get("display_params"), row.get("original_params")],
        sort_keys=True,
        default=str,
    ).casefold()


def _certificate_auth_task_class(row: Mapping[str, Any], output: str) -> str | None:
    command = str(row.get("command_name") or "").strip().casefold()
    params = _task_text(row)
    lowered_output = output.casefold()
    if command in {"shell", "run"} and re.search(r"(^|[^a-z])klist([^a-z]|$)", params):
        return "current-context-ticket-inventory"
    if command in {"shell", "run"} and "dir " in params and "\\\\" in params and "\\c$" in params:
        return "current-context-access-probe"
    if command == "upload" and "sage_ca_signing_" in params and ".pfx" in params:
        return "ca-pfx-stage"
    if (
        command == "execute_assembly"
        and "certify.exe" in params
        and "forge" in params
        and "sage_forged_cert_" in params
    ):
        return "certificate-forge"
    if (
        command == "execute_assembly"
        and "rubeus.exe" in params
        and "asktgt" in params
        and "/certificate:" in params
    ):
        return "pkinit-certificate-request"
    if (
        command == "powerpick"
        and "cert_auth_method=schannel-ldap" in lowered_output
        and "cert_auth_status=ok" in lowered_output
    ):
        return "schannel-control-proof"
    return None


def certificate_auth_primary_metric(rows: object) -> dict[str, Any]:
    """Extract the frozen ISC-8 metric from evaluator-owned Mythic task lineage."""
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return {"complete": False, "reason": "Mythic task lineage is malformed"}
    classified = []
    for row in rows:
        output = live_seams._decode_mythic_response_rows(row.get("responses") or [])
        task_class = _certificate_auth_task_class(row, output)
        classified.append((row, output, task_class))
    proofs = [item for item in classified if item[2] == "schannel-control-proof"]
    if len(proofs) != 1:
        return {
            "complete": False,
            "reason": "expected exactly one achieved Schannel certificate-auth proof task",
            "proof_task_count": len(proofs),
        }
    proof_id = proofs[0][0].get("display_id")
    if type(proof_id) is not int:
        return {"complete": False, "reason": "certificate-auth proof task has no display ID"}

    def latest_before(task_class: str, before: int) -> int | None:
        ids = [
            row.get("display_id")
            for row, _output, observed_class in classified
            if observed_class == task_class
            and type(row.get("display_id")) is int
            and row.get("display_id") < before
        ]
        return max(ids) if ids else None

    forge_id = latest_before("certificate-forge", proof_id)
    upload_id = latest_before("ca-pfx-stage", forge_id) if forge_id is not None else None
    access_id = latest_before("current-context-access-probe", upload_id) if upload_id is not None else None
    inventory_id = (
        latest_before("current-context-ticket-inventory", access_id)
        if access_id is not None else None
    )
    anchors = {
        "current_context_ticket_inventory": inventory_id,
        "current_context_access_probe": access_id,
        "ca_pfx_stage": upload_id,
        "certificate_forge": forge_id,
        "schannel_control_proof": proof_id,
    }
    if any(value is None for value in anchors.values()):
        return {
            "complete": False,
            "reason": "certificate-auth transaction anchors are incomplete or out of order",
            "anchor_task_ids": anchors,
        }
    transaction = [
        item for item in classified
        if type(item[0].get("display_id")) is int
        and inventory_id <= item[0].get("display_id") <= proof_id
    ]
    transaction.sort(key=lambda item: item[0].get("display_id"))
    pkinit_errors = [
        row.get("display_id")
        for row, output, task_class in transaction
        if task_class == "pkinit-certificate-request"
        and ("KRB-ERROR (16)" in output or "KDC_ERR_PADATA_TYPE_NOSUPP" in output)
    ]
    return {
        "complete": True,
        "certificate_auth_transaction_tasks": len(transaction),
        "transaction_task_ids": [row.get("display_id") for row, _output, _class in transaction],
        "transaction_task_classes": [
            {"display_id": row.get("display_id"), "class": task_class or "unclassified"}
            for row, _output, task_class in transaction
        ],
        "avoidable_pkinit_compatibility_error_tasks": len(pkinit_errors),
        "avoidable_pkinit_compatibility_error_task_ids": pkinit_errors,
        "anchor_task_ids": anchors,
    }


class RuntimePilot:
    def __init__(
        self,
        artifact_dir: Path,
        protected_manifest: Path,
        route_smoke: Path,
        *,
        source_roots: Mapping[str, Path] | None = None,
        source_identities: Mapping[str, str] | None = None,
        alternate_start: AlternateStartBinding | None = None,
    ):
        self.artifact_dir = artifact_dir
        self.protected_manifest = protected_manifest
        self.route_smoke = route_smoke
        self.source_roots = {
            arm: Path(path).expanduser().resolve()
            for arm, path in (source_roots or {}).items()
        }
        self.source_identities = dict(source_identities or {})
        self.alternate_start = alternate_start
        initial_root = next(iter(self.source_roots.values()), ROOT)
        self.source_root = initial_root
        self.orchestrate = _orchestrate(initial_root)
        self.native = _native_chat(initial_root)
        self.route = _route(self.orchestrate)
        self.safe_route = _safe_route_identity(self.route)
        self.current_client = None
        self.current_operation_id = None
        self.current_callback_id = None
        self.current_arm = None
        self.current_chat_channel_id = None
        self.current_chat_request_id = None
        self.current_deadline_monotonic = None
        self.current_started_at = None
        self.current_deadline_at = None
        self.current_budget = None
        self.current_execution = None
        self._settled_operation_snapshot = None
        self.current_reset_started = False
        self.live_authority_id = ""
        self.live_authority_receipt: dict[str, Any] = {}

    def _source_runtime_preflight(self, arm: str, source_root: Path, expected: str) -> dict[str, Any]:
        required = (
            source_root / "Payload_Type" / "sage" / "main.py",
            source_root / "skills" / "sage-goad-reset" / "scripts" / "sage_restart.sh",
            source_root / "skills" / "sage-eval-gauge" / "scripts" / "orchestrate.py",
            source_root / ".venv" / "bin" / "python",
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise PilotError(f"{arm} source root lacks runtime prerequisites: " + ", ".join(missing))
        observed = _source_identity_at(source_root)
        if observed != expected:
            raise PilotError(
                f"{arm} source identity mismatch before reset: expected {expected}, observed {observed}"
            )
        _validate_protected_at(self.protected_manifest, source_root)
        result = {
            "source_root": str(source_root),
            "expected_source_identity": expected,
            "observed_source_identity": observed,
        }

    def _activate_source(self, arm: str) -> None:
        source_roots = getattr(self, "source_roots", {})
        if not source_roots:
            self.source_root = getattr(self, "source_root", ROOT)
            return
        source_root = source_roots.get(arm)
        expected = getattr(self, "source_identities", {}).get(arm)
        if source_root is None or not expected:
            raise PilotError(f"no exact source binding exists for {arm}")
        self._source_runtime_preflight(arm, source_root, expected)
        self.source_root = source_root
        self.orchestrate = _orchestrate(source_root)
        self.native = _native_chat(source_root)
        self.route = _route(self.orchestrate)
        self.safe_route = _safe_route_identity(self.route)

    def _engagement_id(self, arm: str) -> str:
        """Return an attempt-and-arm-scoped key for Sage's persistent engagement ledger."""
        try:
            relative = self.artifact_dir.resolve().relative_to((ROOT / ".sage_history").resolve())
        except ValueError as exc:
            raise PilotError("pilot artifacts must be retained under .sage_history") from exc
        safe_attempt = _ENGAGEMENT_FILENAME_SAFE.sub("_", self.artifact_dir.name)[:48]
        identity = hashlib.sha256(str(relative).encode()).hexdigest()[:12]
        return f"trust-walker-pilot-{safe_attempt}-{arm}-{identity}"

    def _engagement_state_path(self, arm: str) -> Path:
        source_root = getattr(self, "source_roots", {}).get(arm, getattr(self, "source_root", ROOT))
        payload = source_root / "Payload_Type" / "sage"
        state_root = Path(
            os.environ.get("SAGE_ENGAGEMENT_STATE_DIR", "").strip()
            or payload / ".sage_engagement"
        )
        if not state_root.is_absolute():
            state_root = payload / state_root
        engagement_id = self._engagement_id(arm)
        safe_id = _ENGAGEMENT_FILENAME_SAFE.sub("_", engagement_id)[:128]
        return state_root / f"state_{safe_id}.json"

    def _fresh_engagement_state(self, arms: tuple[str, ...] | None = None) -> dict[str, Any]:
        state = {}
        for arm in arms or ("shipping-sage", "raw-mythic-bloodhound"):
            path = self._engagement_state_path(arm)
            state[arm] = {
                "engagement_id": self._engagement_id(arm),
                "state_file_absent": not path.exists(),
            }
        contaminated = [arm for arm, value in state.items() if not value["state_file_absent"]]
        if contaminated:
            raise PilotError(
                "attempt-scoped engagement state already exists: " + ", ".join(contaminated)
            )
        return state

    @classmethod
    def for_recovery(cls, artifact_dir: Path) -> tuple["RuntimePilot", Mapping[str, Any], Mapping[str, Any]]:
        root = validate_artifact_dir(artifact_dir)
        ledger = root / "pilot.jsonl"
        records = read_records(ledger)
        starts = [row for row in records if row.get("record_type") == "arm_started"]
        if not starts:
            raise PilotError("recovery ledger has no bound arm start")
        last = starts[-1]
        runtime_identity = last.get("runtime_identity")
        if not isinstance(runtime_identity, Mapping):
            raise PilotError("recovery ledger has no runtime identity")
        operation_id = runtime_identity.get("operation_id")
        callback_id = runtime_identity.get("callback_id")
        if type(operation_id) is not int or operation_id <= 0 or type(callback_id) is not int or callback_id <= 0:
            raise PilotError("recovery runtime identity is malformed")
        obj = object.__new__(cls)
        obj.artifact_dir = root
        obj.protected_manifest = None
        obj.route_smoke = None
        recorded_source_root = Path(str(runtime_identity.get("source_root") or ROOT)).resolve()
        expected_source_identity = str((last.get("start_identity") or {}).get("source_identity") or "")
        if expected_source_identity and _source_identity_at(recorded_source_root) != expected_source_identity:
            raise PilotError("recovery source root no longer matches the bound arm identity")
        obj.source_root = recorded_source_root
        obj.source_roots = {str(last.get("arm") or ""): recorded_source_root}
        obj.source_identities = {str(last.get("arm") or ""): expected_source_identity}
        obj.alternate_start = retained_alternate_start_binding(records)
        obj.orchestrate = _orchestrate(recorded_source_root)
        obj.native = (
            _native_chat()
            if recorded_source_root.resolve() == ROOT.resolve()
            else _native_chat(recorded_source_root)
        )
        obj.route = {}
        obj.safe_route = {}
        obj.current_client = asyncio.run(obj.native.login())
        if getattr(obj.current_client, "current_operation_id", None) != operation_id:
            raise PilotError("recovery login resolved a different Mythic operation")
        obj.current_operation_id = operation_id
        obj.current_callback_id = callback_id
        obj.current_arm = str(last.get("arm") or "")
        obj.current_chat_channel_id = None
        obj.current_chat_request_id = None
        for row in records:
            if row.get("record_type") != "progress" or row.get("arm") != obj.current_arm:
                continue
            event = row.get("event")
            if isinstance(event, Mapping) and event.get("event") == "request_started":
                obj.current_chat_channel_id = event.get("chat_channel_id")
                obj.current_chat_request_id = event.get("chat_request_id")
        obj.current_deadline_monotonic = None
        obj.current_started_at = None
        obj.current_deadline_at = None
        obj.current_budget = None
        obj.current_execution = None
        obj._settled_operation_snapshot = None
        obj.current_reset_started = True
        obj.prior_support_elapsed_seconds = max(
            [
                float(row.get("setup_cleanup_elapsed_seconds", 0.0) or 0.0)
                for row in records
                if isinstance(row.get("setup_cleanup_elapsed_seconds"), (int, float))
            ]
            + [
                float(row.get("elapsed_seconds", 0.0) or 0.0)
                for row in records
                if row.get("record_type") == "support_budget_exhausted"
                and isinstance(row.get("elapsed_seconds"), (int, float))
            ]
            + [0.0]
        )
        obj.live_authority_id = "recovery-from-ledger"
        arm_results = [
            row for row in records
            if row.get("record_type") == "arm_result" and row.get("arm") == obj.current_arm
        ]
        execution = dict(arm_results[-1].get("execution") or {}) if arm_results else {
            "termination": "recovery_after_process_loss",
            "protocol_error": True,
            "cost": None,
        }
        return obj, dict(last.get("start_identity") or {}), execution

    @classmethod
    def for_preserved_inspection(
        cls, artifact_dir: Path,
    ) -> tuple["RuntimePilot", Mapping[str, Any] | None, Mapping[str, Any]]:
        root = Path(artifact_dir).expanduser().resolve()
        try:
            root.relative_to(ROOT / ".sage_history")
        except ValueError as exc:
            raise PilotError("pilot artifacts must be retained under .sage_history") from exc
        if not root.is_dir():
            raise PilotError("preserved-state artifact directory does not exist")
        records = read_records(root / "pilot.jsonl")
        starts = [row for row in records if row.get("record_type") == "arm_started"]
        last = starts[-1] if starts else {}
        runtime_identity = last.get("runtime_identity") if isinstance(last, Mapping) else {}
        obj = object.__new__(cls)
        obj.artifact_dir = root
        obj.protected_manifest = None
        obj.route_smoke = None
        recorded_source_root = Path(str((runtime_identity or {}).get("source_root") or ROOT)).resolve()
        obj.source_root = recorded_source_root
        obj.source_roots = {str(last.get("arm") or "unknown"): recorded_source_root}
        obj.source_identities = {}
        obj.alternate_start = retained_alternate_start_binding(records)
        obj.orchestrate = None
        obj.native = (
            _native_chat()
            if recorded_source_root.resolve() == ROOT.resolve()
            else _native_chat(recorded_source_root)
        )
        obj.route = {}
        obj.safe_route = {}
        obj.current_client = asyncio.run(obj.native.login())
        authenticated_operation = getattr(obj.current_client, "current_operation_id", None)
        expected_operation = (
            runtime_identity.get("operation_id") if isinstance(runtime_identity, Mapping) else None
        )
        if expected_operation is not None and expected_operation != authenticated_operation:
            raise PilotError("preserved-state inspection resolved a different Mythic operation")
        obj.current_operation_id = expected_operation or authenticated_operation
        obj.current_callback_id = (
            runtime_identity.get("callback_id") if isinstance(runtime_identity, Mapping) else None
        )
        obj.current_arm = str(last.get("arm") or "unknown")
        obj.current_chat_channel_id = None
        obj.current_chat_request_id = None
        for row in records:
            event = row.get("event") if isinstance(row, Mapping) else None
            if (
                row.get("arm") == obj.current_arm
                and isinstance(event, Mapping)
                and event.get("event") == "request_started"
            ):
                obj.current_chat_channel_id = event.get("chat_channel_id")
                obj.current_chat_request_id = event.get("chat_request_id")
        obj.current_deadline_monotonic = None
        obj.current_started_at = None
        obj.current_deadline_at = None
        obj.current_budget = None
        obj.current_execution = None
        obj._settled_operation_snapshot = None
        obj.current_reset_started = False
        obj.live_authority_id = "preserved-state-read-only-inspection"
        arm_results = [
            row for row in records
            if row.get("record_type") == "arm_result" and row.get("arm") == obj.current_arm
        ]
        execution = dict(arm_results[-1].get("execution") or {}) if arm_results else {}
        return obj, dict(last.get("start_identity") or {}) if last else None, execution

    def preflight(self, contract: PilotContract | ImprovementContract) -> Mapping[str, Any]:
        if isinstance(contract, ImprovementContract) and self.alternate_start is not None:
            raise PilotError("improvement pair cannot select an alternate start")
        if isinstance(contract, PilotContract) and contract.alternate_start != self.alternate_start:
            raise PilotError("runtime case binding does not match the pilot contract")
        source_bindings = {}
        if isinstance(contract, ImprovementContract):
            if set(self.source_roots) != set(IMPROVEMENT_ARM_ORDER):
                raise PilotError("improvement preflight requires exactly baseline and candidate source roots")
            if len(set(self.source_roots.values())) != 2:
                raise PilotError("baseline and candidate source roots must be distinct")
            for arm in IMPROVEMENT_ARM_ORDER:
                source_bindings[arm] = self._source_runtime_preflight(
                    arm,
                    self.source_roots[arm],
                    contract.source_identity_for_arm(arm),
                )
            self._activate_source(contract.arm_order[0])
        self.route = _route(self.orchestrate)
        self.safe_route = _safe_route_identity(self.route)
        fixture = _fixture_at(self.source_root, self.alternate_start)
        foothold = self._staged_interactive_foothold()
        protected = _validate_protected_at(self.protected_manifest, self.source_root)
        paths = _validate_external_paths()
        smoke = _validate_route_smoke(self.route_smoke, self.route)
        bloodhound = self._bloodhound_service_preflight()
        engagement_state = self._fresh_engagement_state(contract.arm_order)
        assert_rejected_kernel_absent()
        result = {
            "ready": True,
            "contract": contract.to_record(),
            "protected_source": protected,
            "route_smoke": smoke,
            "route": self.safe_route,
            "dependency_paths": paths,
            "control_plane_preflight": {"bloodhound": bloodhound},
            "source_identity": _source_identity_at(self.source_root),
            "source_bindings": source_bindings or None,
            "tool_contract": _tool_contract(),
            "engagement_state": engagement_state,
            "foothold_bootstrap": {
                "snapshot": fixture["snapshot"],
                "mode": "pre-staged-interactive-launch-existing",
                "launcher": foothold.launch_argv()[1],
                "callback_settle_seconds": foothold.callback_settle_seconds,
                "require_unique_callback": foothold.require_unique_callback,
                "requires_mythic_payload_download": False,
                "fixture": fixture,
            },
            "live_authority_id": self.live_authority_id or None,
        }
        result["live_authority"] = _authority_preflight_receipt(
            self.live_authority_receipt, result,
        )
        return result

    def _bloodhound_service_preflight(self) -> dict[str, Any]:
        readiness = self.native._readiness_contract_module()
        try:
            observed = readiness.probe_bloodhound_api_status()
        except Exception as exc:
            raise PilotError(
                "BloodHound API is unreachable before setup reset; restore the authorized control-plane dependency before mutation"
            ) from exc
        if (
            not isinstance(observed, Mapping)
            or observed.get("status_code") != 200
            or type(observed.get("domain_count")) is not int
        ):
            raise PilotError("BloodHound API preflight returned malformed state")
        return {
            "reachable": True,
            "status_code": 200,
            "domain_count": observed["domain_count"],
        }

    def _staged_interactive_foothold(self):
        self.source_root = getattr(self, "source_root", ROOT)
        alternate_start = getattr(self, "alternate_start", None)
        fixture = _fixture_at(self.source_root, alternate_start)
        foothold_args: dict[str, Any] = {
            "callback_settle_seconds": STAGED_FOOTHOLD_CALLBACK_SETTLE_SECONDS,
            "require_unique_callback": True,
        }
        if alternate_start is not None:
            foothold_args.update({
                "host": alternate_start.host,
                "ip": alternate_start.host_ip,
                "user": alternate_start.principal,
                "password_env": "SAGE_MEEREEN_JORAH_PASSWORD",
                "callback_host": alternate_start.host,
                "callback_user": alternate_start.principal.split("\\", 1)[1],
                "range_profile_id": alternate_start.range_profile,
            })
        foothold = _FixtureBoundFoothold(self.orchestrate.FootholdSpec(**foothold_args), fixture)
        argv = foothold.launch_argv()
        def argument_value(flag: str) -> str | None:
            try:
                return argv[argv.index(flag) + 1]
            except (ValueError, IndexError):
                return None

        try:
            settle_seconds = argv[argv.index("--callback-settle-seconds") + 1]
        except (ValueError, IndexError):
            settle_seconds = None
        if (
            argv[:2] != ["/bin/bash", STAGED_FOOTHOLD_LAUNCHER]
            or foothold.direct_retained_launch
            or "--require-unique-callback" not in argv
            or settle_seconds != str(STAGED_FOOTHOLD_CALLBACK_SETTLE_SECONDS)
            or argument_value("--expected-task-state") != "Ready"
            or argument_value("--expected-remote-path") != fixture["remote_path"]
            or argument_value("--expected-remote-sha256") != fixture["remote_sha256"]
            or (
                alternate_start is not None
                and (
                    argv[2:4] != [alternate_start.host_ip, alternate_start.principal]
                    or argument_value("--target-host") != alternate_start.host
                    or argument_value("--callback-host") != alternate_start.host
                    or argument_value("--callback-user") != alternate_start.principal.split("\\", 1)[1]
                )
            )
        ):
            raise PilotError(
                "staged Trust Walker snapshot is not bound to the canonical interactive launch-existing path"
            )
        return foothold

    def _post_reset_readiness(self, client: Any) -> dict[str, Any]:
        alternate_start = getattr(self, "alternate_start", None)
        if alternate_start is None:
            return asyncio.run(self.native.inspect_readiness(client, runtime_dbs_archived=True))
        bootstrap = _load(
            "trust_walker_alternate_start_readiness",
            self.source_root / "skills" / "sage-callback-bootstrap" / "scripts" / "bootstrap_payloads.py",
        )
        return asyncio.run(bootstrap.readiness(
            client,
            repo_root=self.source_root,
            runtime_dbs_archived=True,
            foothold_payload_type=alternate_start.payload_type,
            foothold_host=alternate_start.host,
            foothold_user_match=alternate_start.principal.split("\\", 1)[1],
        ))

    def _operation_snapshot(self, client: Any, operation_id: int, *, timeout: float = 60) -> dict[str, Any]:
        async def query():
            from mythic import mythic
            return await asyncio.wait_for(
                mythic.execute_custom_query(
                    client,
                    TASK_OUTPUT_QUERY,
                    variables={"operationId": operation_id},
                ),
                timeout=timeout,
            )

        value = asyncio.run(query())
        if not isinstance(value, dict):
            raise PilotError("operation-scoped Mythic observer returned a malformed result")
        return value

    @staticmethod
    def _unknown_section(exc: BaseException) -> dict[str, Any]:
        return {
            "state": "unknown",
            "error_type": type(exc).__name__,
            "unknown_reason": str(exc),
        }

    def attest_preserved_state(
        self,
        arm: str,
        _start: Mapping[str, Any] | None,
        _execution: Mapping[str, Any],
        stop_reason: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Read independent control planes after an abnormal stop without changing lab state."""
        from mythic import mythic

        observed_at = datetime.now(UTC).isoformat()
        sections: dict[str, dict[str, Any]] = {}
        try:
            client = self.current_client or asyncio.run(self.native.login())
            operation_id = self.current_operation_id or getattr(client, "current_operation_id", None)
            if type(operation_id) is not int or operation_id <= 0:
                raise PilotError("authenticated Mythic operation identity is unavailable")

            async def mythic_state():
                result = await asyncio.wait_for(
                    mythic.execute_custom_query(
                        client, PRESERVED_STATE_QUERY, variables={"operationId": operation_id},
                    ),
                    timeout=60,
                )
                request = None
                if type(self.current_chat_request_id) is int and self.current_chat_request_id > 0:
                    request_result = await asyncio.wait_for(
                        mythic.execute_custom_query(
                            client,
                            PRESERVED_REQUEST_QUERY,
                            variables={"requestId": self.current_chat_request_id},
                        ),
                        timeout=30,
                    )
                    rows = request_result.get("chat_request")
                    request = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
                return result, request

            result, request = asyncio.run(mythic_state())
            tasks = result.get("task") if isinstance(result, Mapping) else None
            callbacks = result.get("callback") if isinstance(result, Mapping) else None
            containers = result.get("consuming_container") if isinstance(result, Mapping) else None
            if not isinstance(tasks, list) or not isinstance(callbacks, list) or not isinstance(containers, list):
                raise PilotError("Mythic preserved-state response is malformed")
            if any(not isinstance(row, Mapping) for row in [*tasks, *callbacks, *containers]):
                raise PilotError("Mythic preserved-state row is malformed")
            if any(
                row.get("operation_id") != operation_id
                or type(row.get("display_id")) is not int
                or type(row.get("completed")) is not bool
                for row in tasks
            ) or any(
                row.get("operation_id") != operation_id
                or type(row.get("display_id")) is not int
                or type(row.get("active")) is not bool
                for row in callbacks
            ):
                raise PilotError("Mythic preserved-state lineage is malformed")
            if any(type(row.get("container_running")) is not bool for row in containers):
                raise PilotError("Sage chat container state is malformed")
            if type(self.current_chat_request_id) is int and not isinstance(request, Mapping):
                raise PilotError("Sage chat request state is unavailable")
            sections["mythic"] = {
                "state": "observed",
                "operation_id": operation_id,
                "tasks": [dict(row) for row in tasks],
                "callbacks": [dict(row) for row in callbacks],
            }
            sections["sage_chat"] = {
                "state": "observed",
                "containers": [dict(row) for row in containers],
                "request": dict(request) if isinstance(request, Mapping) else None,
            }
        except BaseException as exc:
            unknown = self._unknown_section(exc)
            sections["mythic"] = dict(unknown)
            sections["sage_chat"] = dict(unknown)

        try:
            readiness = self.native._readiness_contract_module()
            databases = readiness.runtime_db_status(
                getattr(self, "source_root", ROOT), runtime_dbs_archived=False,
            )
            active_paths = databases.get("existing_required") if isinstance(databases, Mapping) else None
            if not isinstance(active_paths, list):
                raise PilotError("active runtime database path census is malformed")
            sections["runtime_databases"] = {
                "state": "observed",
                "active_runtime_db_paths": list(active_paths),
            }
        except BaseException as exc:
            sections["runtime_databases"] = self._unknown_section(exc)

        try:
            deployment = readiness.sage_deployment_status(repo_root=getattr(self, "source_root", ROOT))
            if (
                not isinstance(deployment, Mapping)
                or not str(deployment.get("mode") or "").strip()
                or type(deployment.get("container_running")) is not bool
                or type(deployment.get("local_process_running")) is not bool
            ):
                raise PilotError("Sage process census is malformed")
            sections["sage_process"] = {"state": "observed", **dict(deployment)}
        except BaseException as exc:
            sections["sage_process"] = self._unknown_section(exc)

        try:
            ludus = readiness.probe_ludus_status(profile=RANGE_PROFILE)
            vms = ludus.get("vms") if isinstance(ludus, Mapping) else None
            if (
                not isinstance(vms, list)
                or any(not isinstance(row, Mapping) for row in vms)
                or not ludus.get("range_number")
                or len(vms) != len(CANONICAL_INVENTORY)
            ):
                raise PilotError("Ludus VM census is malformed")
            sections["ludus"] = {
                "state": "observed",
                "range_number": ludus.get("range_number"),
                "range_state": ludus.get("range_state"),
                "vm_count": len(vms),
                "vms": [dict(row) for row in vms],
            }
        except BaseException as exc:
            sections["ludus"] = self._unknown_section(exc)

        try:
            bloodhound = readiness.probe_bloodhound_api_status()
            if (
                not isinstance(bloodhound, Mapping)
                or bloodhound.get("status_code") != 200
                or type(bloodhound.get("domain_count")) is not int
                or not isinstance(bloodhound.get("domains"), list)
            ):
                raise PilotError("BloodHound state census is malformed")
            sections["bloodhound"] = {
                "state": "observed",
                "status_code": bloodhound.get("status_code"),
                "domain_count": bloodhound.get("domain_count"),
                "domains": bloodhound.get("domains"),
            }
        except BaseException as exc:
            sections["bloodhound"] = self._unknown_section(exc)

        unknown = sorted(name for name, value in sections.items() if value.get("state") != "observed")
        operation_id = (sections.get("mythic") or {}).get("operation_id")
        callback_id = self.current_callback_id
        return {
            "schema": "sage-trust-walker-preserved-state-v1",
            "complete": not unknown,
            "observation_scope": "read-only control-plane and local process/file state",
            "observed_at": observed_at,
            "arm": arm,
            "stop_reason": dict(stop_reason),
            "sections": sections,
            "unknown_sections": unknown,
            "payload_tasks_issued_by_attestation": 0,
            "state_mutations_by_attestation": 0,
            "inspection_instructions": [
                (
                    "set -a; . ./.env; . Payload_Type/sage/.env.local; set +a; "
                    ".venv/bin/python skills/sage-eval-gauge/scripts/run_trust_walker_pilot.py "
                    f"inspect-preserved --artifact-dir {self.artifact_dir}"
                ),
                f"Mythic UI: operation {operation_id or '<unknown>'} -> callback {callback_id or '<unknown>'} -> task list and unfinished task output",
                (
                    ".venv/bin/python skills/sage-live-runner/scripts/native_chat.py status "
                    f"--request-id {self.current_chat_request_id}"
                    if type(self.current_chat_request_id) is int
                    else f"Mythic UI: operation {operation_id or '<unknown>'} -> consuming containers -> Sage chat status"
                ),
                ".venv/bin/python skills/sage-goad-reset/scripts/ludus.py status",
                "uv --directory \"$SAGE_BLOODHOUND_MCP_DIR\" run python skills/sage-goad-reset/scripts/bh_reset.py status",
                ".venv/bin/python skills/sage-goad-reset/scripts/sage_deployment.py check",
                "ls -l Payload_Type/sage/sage.db Payload_Type/sage/.phoenix/phoenix.db",
            ],
        }

    def reset(self, arm: str, arm_env: Mapping[str, str]) -> Mapping[str, Any]:
        self.current_reset_started = False
        self._activate_source(arm)
        _validate_protected_at(self.protected_manifest, self.source_root)
        assert_rejected_kernel_absent()
        alternate_start = getattr(self, "alternate_start", None)
        fixture = _fixture_at(self.source_root, alternate_start)
        foothold = self._staged_interactive_foothold()
        self._fresh_engagement_state((arm,))
        engagement_id = self._engagement_id(arm)
        restart_env = {
            **dict(arm_env),
            "SAGE_ENGAGEMENT_GATE": "1",
            "SAGE_BLOODHOUND_MCP_DIR": os.environ["SAGE_BLOODHOUND_MCP_DIR"],
            "SAGE_ENGAGEMENT_NETBIOS_MAP": NETBIOS_MAP,
            "SAGE_ENGAGEMENT_ID": engagement_id,
        }
        self.current_reset_started = True
        _unused, callback_id = self.orchestrate.full_reset_and_ready(
            restart_env=restart_env,
            snapshot=alternate_start.snapshot if alternate_start is not None else SNAPSHOT,
            retained_callback_config=(
                Path(alternate_start.retained_config_path)
                if alternate_start is not None
                else self.source_root / "skills" / "sage-callback-bootstrap" / "apollo_callback_config.json"
            ),
            foothold=foothold,
        )
        client = self.native.login
        logged_in = asyncio.run(client())
        readiness = self._post_reset_readiness(logged_in)
        clean_bh = live_seams.bloodhound_empty_state_evidence()
        if readiness.get("ready") is not True:
            raise SetupInvariantError("post-reset shared readiness contract is not ready")
        if clean_bh.get("returncode") != 0 or clean_bh.get("domain_count") != 0:
            raise SetupInvariantError("post-reset BloodHound state is not empty")
        operation_id = getattr(logged_in, "current_operation_id", None)
        if type(operation_id) is not int or operation_id <= 0:
            raise SetupInvariantError("authenticated Mythic operation identity is unavailable")
        operation_snapshot = self._operation_snapshot(logged_in, operation_id)
        if operation_snapshot.get("task") != [] or operation_snapshot.get("credential") != []:
            raise SetupInvariantError("post-reset authenticated Mythic operation is not empty")
        ludus = readiness.get("ludus") or {}
        clock = readiness.get("clock") or {}
        observed_profile = ludus.get("range_profile") or {}
        if (
            observed_profile.get("id") != RANGE_PROFILE
            or (observed_profile.get("qualification") or {}).get("state") != "live_qualified"
            or SCENARIO not in (observed_profile.get("scenarios") or [])
        ):
            raise SetupInvariantError("post-reset range profile is not the live-qualified Trust Walker scenario")
        foothold_state = readiness.get("foothold") or {}
        callbacks = foothold_state.get("callbacks") or []
        selected = [row for row in callbacks if row.get("display_id") == callback_id]
        if len(selected) != 1:
            raise SetupInvariantError("readiness did not bind the unique selected foothold callback")
        readiness_callback = selected[0]
        operation_callbacks = operation_snapshot.get("callback")
        try:
            callback = _select_unique_active_callback(operation_callbacks, callback_id)
        except PilotError as exc:
            raise SetupInvariantError(str(exc)) from exc
        if callback.get("display_id") != callback_id or readiness_callback.get("display_id") != callback_id:
            raise SetupInvariantError("readiness and operation callback identities disagree")
        payload = callback.get("payload") if isinstance(callback.get("payload"), Mapping) else {}
        payload_type = payload.get("payloadtype") if isinstance(payload.get("payloadtype"), Mapping) else {}
        domain = str(callback.get("domain") or "").strip()
        user = str(callback.get("user") or "").strip()
        principal = user if "\\" in user else f"{domain}\\{user}"
        expected_principal = alternate_start.principal if alternate_start is not None else r"NORTH\samwell.tarly"
        expected_host = alternate_start.host if alternate_start is not None else "CASTELBLACK"
        if principal.casefold() != expected_principal.casefold() or str(callback.get("host") or "").casefold() != expected_host.casefold():
            raise SetupInvariantError("post-reset callback does not match the selected foothold principal and host")
        try:
            self.route = _route(self.orchestrate)
        except PilotError as exc:
            raise SetupInvariantError(str(exc)) from exc
        self.safe_route = _safe_route_identity(self.route)
        source_identity = _source_identity_at(self.source_root)
        tool_identity = _tool_contract()
        dependency_identity = _json_hash({
            "topology_identity": (readiness.get("ludus") or {}).get("range_profile", {}).get("topology_identity"),
            "external_paths": _validate_external_paths(),
            "staged_foothold_fixture": fixture,
        })
        self.current_client = logged_in
        self.current_operation_id = operation_id
        self.current_callback_id = callback_id
        self.current_arm = arm
        self.current_chat_channel_id = None
        self.current_chat_request_id = None
        self.current_deadline_monotonic = None
        self.current_started_at = None
        self.current_deadline_at = None
        self.current_budget = None
        self.current_execution = None
        start = {
            "snapshot": alternate_start.snapshot if alternate_start is not None else SNAPSHOT,
            "range_profile": observed_profile.get("id"),
            "inventory": sorted(str(row.get("canonical_name") or "") for row in ludus.get("vms") or []),
            "clock_ready": clock.get("ready") is True,
            "mythic_clean": True,
            "bloodhound_clean": True,
            "foothold": {
                "count": 1,
                "principal": principal,
                "host": str(callback.get("host") or "").upper(),
                "payload_type": str(payload_type.get("name") or readiness_callback.get("payloadtype") or ""),
            },
            "route": self.safe_route,
            "objective_sha256": hashlib.sha256(OBJECTIVE.encode()).hexdigest(),
            "source_identity": source_identity,
            "tool_contract": tool_identity,
            "dependency_identity": dependency_identity,
        }
        if alternate_start is not None:
            start["case_identity"] = alternate_start.identity
        return start

    def runtime_identity(self) -> Mapping[str, Any]:
        return {
            "operation_id": self.current_operation_id,
            "callback_id": self.current_callback_id,
            "arm": self.current_arm,
            "engagement_id": (
                self._engagement_id(self.current_arm) if self.current_arm else None
            ),
            "source_root": str(self.source_root),
        }

    def _deactivate_active_callbacks(self) -> dict[str, Any]:
        """Mark every active callback in the freshly reset pilot operation inactive."""
        from mythic import mythic

        client = self.current_client or asyncio.run(self.native.login())

        async def deactivate() -> dict[str, Any]:
            before = await mythic.get_all_active_callbacks(client)
            display_ids = []
            for row in before or []:
                display_id = row.get("display_id") if isinstance(row, Mapping) else None
                if type(display_id) is not int:
                    raise PilotError("cleanup encountered an active callback without a display ID")
                response = await mythic.update_callback(
                    client, callback_display_id=display_id, active=False,
                )
                if not isinstance(response, Mapping) or response.get("status") != "success":
                    raise PilotError(f"cleanup could not deactivate callback {display_id}")
                display_ids.append(display_id)
            after = await mythic.get_all_active_callbacks(client)
            if after:
                raise PilotError("cleanup left active Mythic callbacks")
            return {
                "ok": True,
                "deactivated_display_ids": display_ids,
                "active_callback_count": 0,
            }

        return asyncio.run(deactivate())

    def _deadline_model(self, deadline: float):
        def invoke(system, tools, history):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("arm wall deadline reached before model request")
            model = live_seams.make_model_fn(
                PROVIDER,
                MODEL,
                api_key=self.route["api_key"],
                base_url=self.route["api_endpoint"],
                objective=OBJECTIVE,
                request_timeout=remaining,
                max_retries=0,
            )
            return _call_before_deadline(deadline, model, system, tools, history)

        return invoke

    @staticmethod
    def _shipping_arm(arm: str) -> bool:
        return arm in {"shipping-sage", "baseline-sage", "candidate-sage"}

    @staticmethod
    def _validate_one_raw_response(record: dict[str, Any]) -> None:
        _validate_response_records([record], model_calls=1)

    def _raw_runner(self, deadline: float, progress):
        bh_specs, bh_registry, upload = bare_bloodhound.load_bloodhound_mcp_surface()
        mythic_dispatch = bare_mythic_tools.make_mythic_dispatcher(
            self.current_client,
            collection_ingestor=bare_bloodhound.make_collection_ingestor(upload),
        )
        bh_dispatch = bare_bloodhound.make_bloodhound_dispatcher(bh_registry)
        mythic_names = {spec["function"]["name"] for spec in bare_mythic_tools.bare_tool_specs()}

        def dispatch(call):
            selected = mythic_dispatch if call.get("tool") in mythic_names else bh_dispatch
            return _call_before_deadline(deadline, selected, call)

        return BareModelRunner(
            self._deadline_model(deadline),
            dispatch,
            tools_spec=[*bare_mythic_tools.bare_tool_specs(), *bh_specs],
            max_steps=0,
            timeout=max(0.0, deadline - time.monotonic()),
            logger=make_stdout_logger(),
            token_ceiling=1_000_000,
            require_complete_usage=True,
            event_sink=progress,
            response_validator=self._validate_one_raw_response,
        )

    def run_arm(self, arm: str, _start: Mapping[str, Any], progress) -> Mapping[str, Any]:
        if self.current_client is None or self.current_operation_id is None:
            raise PilotError("arm runtime has no bound Mythic operation")
        started_at = datetime.now(UTC)
        deadline_at = started_at + timedelta(seconds=2700)
        deadline = time.monotonic() + 2700
        self.current_started_at = started_at
        self.current_deadline_at = deadline_at
        self.current_deadline_monotonic = deadline
        progress({
            "event": "execution_started",
            "arm_started_at": started_at.isoformat(),
            "arm_deadline_at": deadline_at.isoformat(),
        })
        if self._shipping_arm(arm):
            def shipping_progress(event):
                if isinstance(event, Mapping) and event.get("event") == "request_started":
                    self.current_chat_channel_id = event.get("chat_channel_id")
                    self.current_chat_request_id = event.get("chat_request_id")
                progress(event)
            metadata = self.native.default_ai_metadata()
            config = metadata.setdefault("config", {})
            config.update({
                "provider": PROVIDER,
                "model": MODEL,
                "API_ENDPOINT": self.route["api_endpoint"],
                "API_KEY": self.route["api_key"],
                "mode": "auto",
                "autonomous_solve": True,
                "max_steps": 0,
                "SAGE_EVAL_ARM_DEADLINE_EPOCH": str(deadline_at.timestamp()),
            })
            try:
                async def execute_shipping():
                    remaining = max(0.001, deadline - time.monotonic())
                    return await asyncio.wait_for(
                        self.native.run_native_chat_turn(
                            self.current_client,
                            OBJECTIVE,
                            timeout_seconds=remaining,
                            poll_interval_seconds=min(5, remaining),
                            channel_name="Trust Walker matched pilot — shipping Sage",
                            metadata=metadata,
                            use_prepared_channel=False,
                            progress_sink=shipping_progress,
                            expected_runtime_identity={
                                "provider": PROVIDER,
                                "model": MODEL,
                                "route": self.native._chat_runtime_identity_from_metadata(metadata)["route"],
                            },
                        ),
                        timeout=remaining,
                    )

                result = asyncio.run(execute_shipping())
                termination = str(result.get("status") or "unknown")
            except self.native.RequestWaitTimeout as exc:
                result = self.native.request_snapshot_result(exc.last_snapshot or {})
                termination = "timeout"
            except TimeoutError:
                result = {}
                termination = "timeout"
            execution = {
                "termination": termination,
                "arm_started_at": started_at.isoformat(),
                "arm_deadline_at": deadline_at.isoformat(),
                "pending_finalization": True,
                "resource_complete": False,
            }
            self.current_execution = {**execution, "_shipping_result": result}
            return execution
        if arm != "raw-mythic-bloodhound":
            raise PilotError("unexpected pilot arm")
        budget = task_budget.TaskBudget(100, deadline, event_sink=progress)
        self.current_budget = budget
        runner = self._raw_runner(deadline, progress)
        with task_budget.task_budget_scope(budget):
            result = runner.run(OBJECTIVE)
        budget_snapshot = budget.snapshot()
        transcript = _write_private(self.artifact_dir / "raw-mythic-bloodhound-transcript.json", {
            "objective": result.objective,
            "events": result.transcript,
            "final": result.final_text,
            "termination": result.termination,
            "response_records": result.model_response_records,
            "recoverable_tool_errors": result.recoverable_tool_errors,
        })
        route_error = None
        inspected_usage_complete = False
        try:
            inspected_total, safe_records, inspected_usage_complete = _inspect_response_records(
                result.model_response_records, model_calls=result.model_calls,
            )
        except PilotError as exc:
            inspected_total = None
            safe_records = []
            route_error = str(exc)
        cost_error = None
        if result.model_attempts != result.model_calls:
            cost_error = "provider request has no authoritative response usage"
        elif result.token_usage_complete is not True or not inspected_usage_complete:
            cost_error = "completed provider response has incomplete authoritative usage"
        total_tokens = inspected_total if route_error is None and cost_error is None else None
        if (
            not isinstance(budget_snapshot, Mapping)
            or budget_snapshot.get("limit") != 100
            or type(budget_snapshot.get("reservations")) is not int
            or not 0 <= budget_snapshot.get("reservations", 101) <= 100
            or not isinstance(budget_snapshot.get("confirmed_task_ids"), list)
            or len(budget_snapshot.get("confirmed_task_ids")) > budget_snapshot.get("reservations")
        ):
            raise PilotError("raw task-budget accounting is missing or invalid")
        execution = {
            "termination": result.termination,
            "transcript": transcript,
            "response_records": safe_records,
            "arm_started_at": started_at.isoformat(),
            "arm_deadline_at": deadline_at.isoformat(),
            "task_budget": budget_snapshot,
            "protocol_error": route_error is not None,
            "route_error": route_error,
            "cost_error": cost_error,
            "resource_complete": route_error is None and cost_error is None,
            "in_flight_model_responses": max(0, result.model_attempts - result.model_calls),
            "cost": {
                "model_attempts": result.model_attempts,
                "model_calls": result.model_calls,
                "total_tokens": total_tokens,
                "payload_tasks": budget_snapshot.get("reservations"),
                "wall_seconds": result.wall_seconds,
                "token_overshoot": (
                    result.token_budget_overshoot if type(total_tokens) is int else None
                ),
            },
        }
        self.current_execution = execution
        return execution

    def stop_and_settle(
        self,
        arm: str,
        _start: Mapping[str, Any],
        execution: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Stop the producer, settle accepted tasks, and finalize cost before outcome observation."""
        if self.current_client is None or self.current_operation_id is None:
            raise PilotError("settlement has no bound Mythic operation")
        steps: list[dict[str, Any]] = []
        finalized = dict(execution)
        if self.current_budget is not None:
            self.current_budget.close()

        producer_terminal = arm == "raw-mythic-bloodhound"
        if self._shipping_arm(arm):
            if type(self.current_chat_request_id) is not int or type(self.current_chat_channel_id) is not int:
                steps.append({"name": "shipping_producer_stop", "ok": False, "reason": "request identity missing"})
            else:
                snapshot = asyncio.run(
                    self.native.fetch_request_snapshot(self.current_client, self.current_chat_request_id)
                )
                status = str((snapshot.get("request") or {}).get("status") or "").casefold()
                if status in self.native.TERMINAL_STATUSES:
                    stop_result = {
                        "remote_stop_confirmed": True,
                        "already_terminal": True,
                        "original_request_status": status,
                    }
                else:
                    stop_result = asyncio.run(self.native.stop_native_chat_request(
                        self.current_client,
                        channel_id=self.current_chat_channel_id,
                        request_id=self.current_chat_request_id,
                        timeout_seconds=120,
                    ))
                producer_terminal = stop_result.get("remote_stop_confirmed") is True
                steps.append({"name": "shipping_producer_stop", "ok": producer_terminal, "result": stop_result})
                snapshot = asyncio.run(
                    self.native.fetch_request_snapshot(self.current_client, self.current_chat_request_id)
                )
                shipping_result = self.native.request_snapshot_result(snapshot)
                transcript_name = (
                    "shipping-sage-transcript.json"
                    if arm == "shipping-sage"
                    else f"{arm}-transcript.json"
                )
                transcript = _write_private(self.artifact_dir / transcript_name, shipping_result)
                telemetry = shipping_result.get("runtime_telemetry") or {}
                (
                    model_attempts,
                    completed_model_calls,
                    total_tokens,
                    safe_records,
                    route_error,
                    cost_error,
                ) = _shipping_response_accounting(telemetry)
                task_state = telemetry.get("task_budget")
                task_valid = bool(
                    isinstance(task_state, Mapping)
                    and task_state.get("limit") == 100
                    and type(task_state.get("reservations")) is int
                    and 0 <= task_state.get("reservations", 101) <= 100
                    and isinstance(task_state.get("confirmed_task_ids"), list)
                    and len(task_state.get("confirmed_task_ids")) <= task_state.get("reservations")
                )
                finalized.update({
                    "termination": str(shipping_result.get("status") or finalized.get("termination") or "unknown"),
                    "pending_finalization": False,
                    "transcript": transcript,
                    "response_records": safe_records,
                    "task_budget": dict(task_state) if isinstance(task_state, Mapping) else None,
                    "protocol_error": route_error is not None or not task_valid,
                    "route_error": route_error,
                    "cost_error": cost_error,
                    "resource_complete": route_error is None and cost_error is None and task_valid,
                    "in_flight_model_responses": max(0, model_attempts - completed_model_calls),
                    "cost": {
                        "model_attempts": model_attempts,
                        "model_calls": completed_model_calls,
                        "total_tokens": total_tokens,
                        "payload_tasks": task_state.get("reservations") if isinstance(task_state, Mapping) else None,
                        "wall_seconds": (
                            min(2700.0, max(0.0, time.monotonic() - self.current_deadline_monotonic + 2700))
                            if isinstance(self.current_deadline_monotonic, (int, float))
                            else (finalized.get("cost") or {}).get("wall_seconds")
                        ),
                        "token_overshoot": total_tokens > 1_000_000 if type(total_tokens) is int else None,
                    },
                })

        settlement_started_at = datetime.now(UTC)
        settlement_started_monotonic = time.monotonic()
        settle_deadline = settlement_started_monotonic + TASK_SETTLEMENT_SECONDS
        operation_snapshot = None
        poll_count = 0
        poll_errors: list[dict[str, str]] = []
        while time.monotonic() < settle_deadline:
            try:
                operation_snapshot = self._operation_snapshot(
                    self.current_client,
                    self.current_operation_id,
                    timeout=min(30, max(0.001, settle_deadline - time.monotonic())),
                )
            except Exception as exc:
                operation_snapshot = None
                poll_errors.append({"error_type": type(exc).__name__, "reason": str(exc)})
            poll_count += 1
            rows = operation_snapshot.get("task") if isinstance(operation_snapshot, Mapping) else None
            if isinstance(rows, list) and all(row.get("completed") is True for row in rows if isinstance(row, Mapping)):
                break
            time.sleep(min(5, max(0.0, settle_deadline - time.monotonic())))
        rows = operation_snapshot.get("task") if isinstance(operation_snapshot, Mapping) else None
        quiescent = bool(isinstance(rows, list) and all(
            isinstance(row, Mapping) and row.get("completed") is True for row in rows
        ))
        completed_task_ids = [
            row.get("display_id") for row in rows or []
            if isinstance(row, Mapping) and row.get("completed") is True
        ]
        outstanding_task_ids = [
            row.get("display_id") for row in rows or []
            if isinstance(row, Mapping) and row.get("completed") is not True
        ]
        objective_inert_outstanding_task_ids = [
            row.get("display_id") for row in rows or []
            if (
                isinstance(row, Mapping)
                and row.get("completed") is False
                and str(row.get("command_name") or "").strip().casefold()
                in OBJECTIVE_INERT_UNFINISHED_COMMANDS
            )
        ]
        objective_effect_quiescent = bool(
            isinstance(rows, list)
            and len(objective_inert_outstanding_task_ids) == len(outstanding_task_ids)
        )
        settlement_ended_at = datetime.now(UTC)
        settlement_elapsed = max(0.0, time.monotonic() - settlement_started_monotonic)
        steps.append({
            "name": "task_quiescence",
            "ok": quiescent,
            "task_count": len(rows) if isinstance(rows, list) else None,
            "completed_task_ids": completed_task_ids,
            "outstanding_task_ids": outstanding_task_ids,
            "objective_inert_outstanding_task_ids": objective_inert_outstanding_task_ids,
            "objective_effect_quiescent": objective_effect_quiescent,
        })
        self.current_execution = finalized
        self._settled_operation_snapshot = operation_snapshot
        return {
            "complete": producer_terminal and objective_effect_quiescent,
            "producer_terminal": producer_terminal,
            "task_quiescent": quiescent,
            "objective_effect_quiescent": objective_effect_quiescent,
            "objective_inert_outstanding_task_ids": objective_inert_outstanding_task_ids,
            "window_seconds": TASK_SETTLEMENT_SECONDS,
            "window_relationship": "outside_measured_window_after_producer_stop_attempt",
            "started_at": settlement_started_at.isoformat(),
            "deadline_at": (
                settlement_started_at + timedelta(seconds=TASK_SETTLEMENT_SECONDS)
            ).isoformat(),
            "ended_at": settlement_ended_at.isoformat(),
            "elapsed_seconds": settlement_elapsed,
            "poll_count": poll_count,
            "poll_errors": poll_errors,
            "completed_task_ids": completed_task_ids,
            "outstanding_task_ids": outstanding_task_ids,
            "steps": steps,
            "execution": finalized,
        }

    def observe(
        self,
        arm: str,
        _start: Mapping[str, Any],
        execution: Mapping[str, Any],
        _settlement: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self.current_client is None or self.current_operation_id is None:
            raise PilotError("terminal observer has no bound operation")
        if arm not in (*ARM_ORDER, *IMPROVEMENT_ARM_ORDER):
            raise PilotError("terminal observer has an unknown arm")

        observed = self._operation_snapshot(self.current_client, self.current_operation_id)
        native_evidence = _retain_native_observation(
            self.artifact_dir / f"{arm}-native-observation.json", observed,
        )
        bh_count = live_seams.bloodhound_domain_count(timeout=60)
        snapshot = self._terminal_snapshot_from_observation(
            arm, observed, execution, bh_count=bh_count,
        )
        snapshot["native_evidence"] = {
            **native_evidence,
            "arm": arm,
            "operation_id": self.current_operation_id,
            "query_sha256": hashlib.sha256(TASK_OUTPUT_QUERY.encode()).hexdigest(),
        }
        return snapshot

    def _terminal_snapshot_from_observation(
        self,
        arm: str,
        observed: Mapping[str, Any],
        execution: Mapping[str, Any],
        *,
        bh_count: int | None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """Use the same outcome projection for live observation and local evidence replay."""
        rows = observed.get("task")
        credentials = observed.get("credential")
        callbacks = observed.get("callback")
        complete = {
            "tasks": isinstance(rows, list),
            "credentials": bool(isinstance(credentials, list) and all(
                isinstance(row, Mapping)
                and row.get("operation_id") == self.current_operation_id
                and type(row.get("id")) is int
                and bool(str(row.get("timestamp") or "").strip())
                for row in credentials
            )),
            "callbacks": isinstance(callbacks, list),
            "bloodhound": type(bh_count) is int,
        }
        task_states = []
        candidates = []
        proof_rejections = []
        decoded_rows = []
        for row in rows or []:
            task_id = row.get("display_id")
            task_states.append({
                "operation_id": row.get("operation_id"),
                "display_id": task_id,
                "command_name": row.get("command_name"),
                "completed": row.get("completed"),
                "status": row.get("status"),
                "created_at": row.get("status_timestamp_preprocessing"),
                "submitted_at": row.get("status_timestamp_submitted"),
                "processing_at": row.get("status_timestamp_processing"),
                "first_response_at": row.get("status_timestamp_processed"),
                "last_update_at": row.get("timestamp"),
            })
            response_rows = row.get("responses") or []
            output = live_seams._decode_mythic_response_rows(response_rows)
            decoded_rows.append((row, output))
            if row.get("completed") is not True:
                continue
            certificate_prefix = _first_response_prefix_satisfying(
                response_rows,
                lambda prefix: certificate_admin_candidate(
                    row,
                    prefix,
                    effect_at=None,
                    operation_id=self.current_operation_id,
                    target="essos.local",
                    netbios_domains=json.loads(NETBIOS_MAP),
                ) is not None,
            )
            if certificate_prefix is not None:
                certificate_candidate = certificate_admin_candidate(
                    row,
                    certificate_prefix["output"],
                    effect_at=certificate_prefix["effect_at"],
                    operation_id=self.current_operation_id,
                    target="essos.local",
                    netbios_domains=json.loads(NETBIOS_MAP),
                )
                if certificate_candidate is not None:
                    certificate_candidate["response_id"] = certificate_prefix["response_id"]
                    candidates.append(certificate_candidate)
            else:
                # Diagnose the complete output once, not every partial response prefix.
                certificate_admin_candidate(
                    row, output, effect_at=None,
                    operation_id=self.current_operation_id,
                    target="essos.local", netbios_domains=json.loads(NETBIOS_MAP),
                    rejected_proofs=proof_rejections,
                )
            realm = live_seams._krbtgt_dcsync_task_realm(dict(row))
            if realm is None or not live_seams._dcsync_realm_matches(realm, "essos.local"):
                continue
            krbtgt_prefix = _first_response_prefix_satisfying(
                response_rows,
                lambda prefix: bool(live_seams._extract_credential_material(
                    prefix, account="krbtgt", realm="essos.local",
                )),
            )
            if krbtgt_prefix is None:
                continue
            contract = effect_registry.contract_for(Milestone.OBJECTIVE)
            observation = effect_registry.OracleObservation(
                registry_version=effect_registry.REGISTRY_VERSION,
                oracle_id=contract.oracle_id,
                origin=effect_registry.ProofOrigin.MYTHIC_TASK_OUTPUT,
                proof_kind="krbtgt_material",
                outcome=True,
                observation_id=f"operation-{self.current_operation_id}-task-{task_id}",
                target="essos.local",
            )
            proof = effect_registry.admit_observation(
                Milestone.OBJECTIVE, observation, expected_target="essos.local",
            )
            candidates.append({
                "operation_id": self.current_operation_id,
                "target": "essos.local",
                "task_ids": [task_id],
                "effect_at": krbtgt_prefix["effect_at"],
                "response_id": krbtgt_prefix["response_id"],
                "task_output_sha256": hashlib.sha256(
                    krbtgt_prefix["output"].encode()
                ).hexdigest(),
                "proof_record": proof.to_record(),
            })

        task_by_id = {
            row.get("display_id"): row
            for row in rows or []
            if isinstance(row, Mapping) and type(row.get("display_id")) is int
        }
        for credential in credentials or []:
            if not isinstance(credential, Mapping):
                continue
            task_id = credential.get("task_id")
            task_row = task_by_id.get(task_id)
            if (
                task_row is None
                or credential.get("operation_id") != self.current_operation_id
                or task_row.get("completed") is not True
                or str(credential.get("account") or "").strip().casefold() != "krbtgt"
                or not live_seams._dcsync_realm_matches(
                    str(credential.get("realm") or ""), "essos.local"
                )
                or not str(credential.get("credential_text") or "").strip()
            ):
                continue
            contract = effect_registry.contract_for(Milestone.OBJECTIVE)
            observation = effect_registry.OracleObservation(
                registry_version=effect_registry.REGISTRY_VERSION,
                oracle_id=contract.oracle_id,
                origin=effect_registry.ProofOrigin.MYTHIC_CREDENTIAL_STORE,
                proof_kind="krbtgt_material",
                outcome=True,
                observation_id=f"operation-{self.current_operation_id}-credential-{credential.get('id')}",
                target="essos.local",
            )
            proof = effect_registry.admit_observation(
                Milestone.OBJECTIVE, observation, expected_target="essos.local",
            )
            candidates.append({
                "operation_id": self.current_operation_id,
                "target": "essos.local",
                "task_ids": [task_id],
                "effect_at": credential.get("timestamp"),
                "credential_id": credential.get("id"),
                "credential_material_sha256": hashlib.sha256(
                    str(credential.get("credential_text")).encode()
                ).hexdigest(),
                "proof_record": proof.to_record(),
            })

        def krbtgt_milestone(realm):
            supporting = []
            for row, output in decoded_rows:
                if row.get("completed") is not True:
                    continue
                observed_realm = live_seams._krbtgt_dcsync_task_realm(dict(row))
                if observed_realm is None or not live_seams._dcsync_realm_matches(observed_realm, realm):
                    continue
                if live_seams._extract_credential_material(output, account="krbtgt", realm=realm):
                    supporting.append(row.get("display_id"))
            return {"disposition": "proven" if supporting else "disproven", "task_ids": supporting}

        system_tasks = [
            row.get("display_id")
            for row, output in decoded_rows
            if row.get("completed") is True
            and live_seams._realms_match((row.get("callback") or {}).get("domain"), "north.sevenkingdoms.local")
            and live_seams._task_is_identity_query(dict(row))
            and live_seams._output_is_system_identity(output)
        ]
        intermediate = {
            "FOOTHOLD": {"disposition": "proven", "source": "matched_start"},
            "GRAPH_COLLECTED": {
                "disposition": "proven" if bh_count > 0 else "disproven",
                "domain_count": bh_count,
            },
            "SYSTEM_ON_HOST": {
                "disposition": "proven" if system_tasks else "disproven",
                "task_ids": system_tasks,
            },
            "KRBTGT_DUMPED": krbtgt_milestone("north.sevenkingdoms.local"),
            "DCSYNC_PARENT": krbtgt_milestone("sevenkingdoms.local"),
            "OBJECTIVE": {
                "disposition": "proven" if candidates else "disproven",
                "task_ids": [task_id for candidate in candidates for task_id in candidate["task_ids"]],
            },
        }
        snapshot = {
            "schema": SNAPSHOT_SCHEMA,
            "operation_id": self.current_operation_id,
            "target": "essos.local",
            "proof_contract": PROOF_CONTRACT,
            "arm_started_at": self.current_started_at.isoformat() if self.current_started_at else None,
            "arm_deadline_at": self.current_deadline_at.isoformat() if self.current_deadline_at else None,
            "observed_at": observed_at or datetime.now(UTC).isoformat(),
            "observer_complete": complete,
            "tasks": task_states,
            "proof_candidates": candidates,
            "proof_rejections": proof_rejections,
            "task_accounting": {
                "limit": (execution.get("task_budget") or {}).get("limit"),
                "reservations": (execution.get("task_budget") or {}).get("reservations"),
                "confirmed_task_ids": (execution.get("task_budget") or {}).get("confirmed_task_ids"),
            },
            "credential_count": len(credentials or []),
            "callback_count": len(callbacks or []),
            "bloodhound_domain_count": bh_count,
            "arm": arm,
            "intermediate_milestones": intermediate,
        }
        if arm in IMPROVEMENT_ARM_ORDER:
            snapshot["certificate_auth_primary_metric"] = certificate_auth_primary_metric(rows)
        return snapshot

    def teardown(self, arm: str, _start: Mapping[str, Any] | None) -> Mapping[str, Any]:
        steps = []
        python = str(self.source_root / ".venv" / "bin" / "python")
        for name, argv, timeout in (
            ("stop_sage", ["/bin/bash", "skills/sage-goad-reset/scripts/sage_stop.sh"], 120),
            ("archive_runtime_dbs", [python, "skills/sage-goad-reset/scripts/archive_runtime_dbs.py"], 180),
            ("poweroff_range", [python, "skills/sage-goad-reset/scripts/ludus.py", "poweroff", "all"], 900),
            ("wipe_bloodhound", ["uv", "--directory", os.environ.get("SAGE_BLOODHOUND_MCP_DIR", ""), "run", "python", str(self.source_root / "skills/sage-goad-reset/scripts/bh_reset.py"), "wipe", "--yes"], 180),
        ):
            result = _safe_command(argv, timeout=timeout, cwd=self.source_root)
            steps.append({"name": name, **result})
            if name == "poweroff_range" and result.get("ok") is True:
                try:
                    callback_cleanup = self._deactivate_active_callbacks()
                except BaseException as exc:
                    callback_cleanup = {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    }
                steps.append({"name": "callbacks_inactive", **callback_cleanup})
        bh = live_seams.bloodhound_empty_state_evidence(timeout=60)
        steps.append({
            "name": "bloodhound_empty",
            "ok": bh.get("returncode") == 0 and bh.get("domain_count") == 0,
            "domain_count": bh.get("domain_count"),
        })
        complete = all(step.get("ok") is True for step in steps)
        self.current_client = None
        self.current_operation_id = None
        self.current_callback_id = None
        self.current_arm = None
        self.current_chat_channel_id = None
        self.current_chat_request_id = None
        self.current_deadline_monotonic = None
        self.current_started_at = None
        self.current_deadline_at = None
        self.current_budget = None
        self.current_execution = None
        self.current_reset_started = False
        self._settled_operation_snapshot = None
        return {"complete": complete, "steps": steps}


def _setup_continuation_state(
    root: Path,
    records: list[dict[str, Any]],
    authority_id: str,
    contract: PilotContract,
) -> tuple[int, float, dict[str, Any]]:
    if any(row.get("record_type") == "setup_rehearsal_result" for row in records):
        raise PilotError("completed setup rehearsal series cannot be continued")
    attempted = [row for row in records if row.get("record_type") == "setup_attempted"]
    completed = [row for row in records if row.get("record_type") == "setup_attempt"]
    preflights = [row for row in records if row.get("record_type") == "setup_preflight"]
    if (
        len(attempted) != 1
        or attempted[0].get("attempt") != 1
        or attempted[0].get("rehearsal_id") != "setup-rehearsal-1"
        or completed
        or len(preflights) != 1
        or preflights[0].get("ready") is not True
        or preflights[0].get("live_authority_id") != authority_id
        or (
            preflights[0].get("contract") != contract.to_record()
            if contract.alternate_start is not None
            else preflights[0].get("contract", contract.to_record()) != contract.to_record()
        )
    ):
        raise PilotError("setup continuation requires the exact orphaned attempt-1 ledger")
    report_path = root / "attempt-001-abort-report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError("setup continuation abort report is missing or malformed") from exc
    counts = report.get("resource_and_activity_counts") if isinstance(report, Mapping) else None
    continuation = report.get("continuation") if isinstance(report, Mapping) else None
    if (
        report.get("schema") != "sage-trust-walker-setup-rehearsal-abort-v1"
        or report.get("authority_id") != authority_id
        or report.get("attempt") != 1
        or report.get("disposition") != "stopped_before_reset_mutation"
        or not isinstance(counts, Mapping)
        or counts.get("setup_attempts_consumed") != 1
        or counts.get("model_provider_calls") != 0
        or counts.get("measured_arms_started") != 0
        or counts.get("readiness_payload_tasks") != 0
        or not isinstance(continuation, Mapping)
        or continuation.get("performed") is not False
    ):
        raise PilotError("setup continuation abort report does not bind the authorized no-mutation attempt")
    elapsed = preflights[0].get("setup_cleanup_elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or elapsed < 0:
        raise PilotError("setup continuation has invalid prior support accounting")
    return 2, float(elapsed), dict(report)


def run_setup_rehearsals(
    runtime: RuntimePilot,
    contract: PilotContract,
    artifact_dir: str | Path,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    """Reach two consecutive compatible clean starts without invoking either model arm."""
    contract.validate()
    root = validate_artifact_dir(artifact_dir)
    ledger = root / "setup-rehearsals.jsonl"
    records = read_records(ledger)
    if not resume and any(row.get("record_type") == "setup_attempted" for row in records):
        raise PilotError("setup rehearsal series already contains an attempted start")

    start_attempt = 1
    support_elapsed = 0.0
    abort_report: dict[str, Any] | None = None
    if resume:
        start_attempt, support_elapsed, abort_report = _setup_continuation_state(
            root, records, runtime.live_authority_id, contract,
        )
    preflight_started = time.monotonic()
    try:
        preflight = dict(runtime.preflight(contract))
    finally:
        support_elapsed += max(0.0, time.monotonic() - preflight_started)
    if preflight.get("ready") is not True:
        raise PilotError("setup rehearsal preflight did not pass")
    if resume:
        failure = abort_report.get("failure") if isinstance(abort_report, Mapping) else {}
        append_record(ledger, {
            "record_type": "setup_attempt",
            "attempt": 1,
            "rehearsal_id": "setup-rehearsal-1",
            "success": False,
            "recoverable": True,
            "start_identity": None,
            "runtime_identity": None,
            "cleanup": {"complete": True, "state": "not_required_no_mutation"},
            "error_type": (failure or {}).get("error_type"),
            "reason": (failure or {}).get("reason"),
            "setup_cleanup_elapsed_seconds": support_elapsed,
            "retained_abort_report": str(root / "attempt-001-abort-report.json"),
        })
        append_record(ledger, {
            "record_type": "setup_resume_preflight",
            "next_attempt": start_attempt,
            "setup_cleanup_elapsed_seconds": support_elapsed,
            **preflight,
        })
    else:
        append_record(ledger, {
            "record_type": "setup_preflight",
            "setup_cleanup_elapsed_seconds": support_elapsed,
            **preflight,
        })

    consecutive_successes = 0
    expected_start: dict[str, Any] | None = None
    hard_stop: dict[str, Any] | None = None
    attempts = start_attempt - 1
    for attempt in range(start_attempt, SETUP_REHEARSAL_ATTEMPT_LIMIT + 1):
        if support_elapsed >= contract.setup_cleanup_seconds:
            hard_stop = {
                "stage": "support_budget",
                "reason": "two-hour setup/recovery allowance is exhausted",
            }
            break
        attempts = attempt
        rehearsal_id = f"setup-rehearsal-{attempt}"
        append_record(ledger, {
            "record_type": "setup_attempted",
            "attempt": attempt,
            "rehearsal_id": rehearsal_id,
        })
        start: Mapping[str, Any] | None = None
        reset_error: Exception | None = None
        reset_started = time.monotonic()
        try:
            start = runtime.reset(rehearsal_id, arm_environment(contract))
        except (Exception, SystemExit) as exc:
            reset_error = exc
        finally:
            support_elapsed += max(0.0, time.monotonic() - reset_started)

        identity: dict[str, Any] | None = None
        runtime_identity: dict[str, Any] | None = None
        invariant_error: Exception | None = None
        if reset_error is None:
            try:
                identity = matched_start_identity(start, contract.alternate_start)
                runtime_identity = validate_runtime_identity(runtime.runtime_identity())
                if expected_start is not None:
                    matched, differences = compare_start_manifests(
                        expected_start, identity, alternate_start=contract.alternate_start,
                    )
                    if not matched:
                        raise SetupInvariantError(
                            "consecutive setup starts differ: " + ", ".join(differences)
                        )
            except Exception as exc:
                invariant_error = exc

        cleanup: dict[str, Any] = {"complete": False, "state": "not_attempted"}
        reset_mutated = bool(runtime.current_reset_started)
        if reset_mutated:
            cleanup_started = time.monotonic()
            try:
                cleanup = dict(runtime.teardown(rehearsal_id, start))
            except Exception as exc:
                cleanup = {
                    "complete": False,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            finally:
                support_elapsed += max(0.0, time.monotonic() - cleanup_started)

        error = invariant_error or reset_error
        recoverable = bool(
            reset_error is not None
            and reset_mutated
            and not isinstance(reset_error, SetupInvariantError)
            and cleanup.get("complete") is True
        )

        success = bool(
            error is None
            and identity is not None
            and runtime_identity is not None
            and cleanup.get("complete") is True
            and support_elapsed <= contract.setup_cleanup_seconds
        )
        append_record(ledger, {
            "record_type": "setup_attempt",
            "attempt": attempt,
            "rehearsal_id": rehearsal_id,
            "success": success,
            "recoverable": recoverable,
            "start_identity": identity,
            "runtime_identity": runtime_identity,
            "cleanup": cleanup,
            "error_type": type(error).__name__ if error is not None else None,
            "reason": str(error) if error is not None else None,
            "setup_cleanup_elapsed_seconds": support_elapsed,
        })
        if success:
            consecutive_successes += 1
            expected_start = identity
            if consecutive_successes >= SETUP_REHEARSAL_SUCCESSES:
                break
            continue

        consecutive_successes = 0
        expected_start = None
        if error is None:
            hard_stop = {
                "stage": "cleanup" if cleanup.get("complete") is not True else "support_budget",
                "reason": cleanup.get("reason") or "setup rehearsal did not complete cleanly",
            }
            break
        if not recoverable:
            hard_stop = {
                "stage": "start_invariant" if invariant_error is not None else "reset",
                "error_type": type(error).__name__,
                "reason": str(error),
            }
            break

    complete = consecutive_successes >= SETUP_REHEARSAL_SUCCESSES
    if not complete and hard_stop is None:
        hard_stop = {
            "stage": "attempt_limit",
            "reason": "setup rehearsal attempt limit is exhausted",
        }
    result = {
        "schema": "sage-trust-walker-setup-rehearsals-v1",
        "complete": complete,
        "attempts": attempts,
        "attempt_limit": SETUP_REHEARSAL_ATTEMPT_LIMIT,
        "required_consecutive_successes": SETUP_REHEARSAL_SUCCESSES,
        "consecutive_successes": consecutive_successes,
        "setup_cleanup_elapsed_seconds": support_elapsed,
        "support_limit_seconds": contract.setup_cleanup_seconds,
        "support_budget_exhausted": support_elapsed > contract.setup_cleanup_seconds,
        "hard_stop": hard_stop,
        "model_inference_calls": 0,
        "measured_arms_started": 0,
        "continued_after_no_mutation_abort": resume,
        "ledger": str(ledger),
    }
    append_record(ledger, {"record_type": "setup_rehearsal_result", **result})
    return result


def _runtime(
    args,
    contract: PilotContract | ImprovementContract | None = None,
) -> RuntimePilot:
    contract = contract or PilotContract()
    source_roots = None
    source_identities = None
    if isinstance(contract, ImprovementContract):
        source_roots = {
            "baseline-sage": Path(args.baseline_source_root),
            "candidate-sage": Path(args.candidate_source_root),
        }
        source_identities = {
            "baseline-sage": contract.baseline_source_identity,
            "candidate-sage": contract.candidate_source_identity,
        }
    return RuntimePilot(
        Path(args.artifact_dir).expanduser().resolve(),
        Path(args.protected_manifest).expanduser().resolve(),
        Path(args.route_smoke).expanduser().resolve(),
        source_roots=source_roots,
        source_identities=source_identities,
        alternate_start=(contract.alternate_start if isinstance(contract, PilotContract) else None),
    )


def _add_improvement_identity_args(command: argparse.ArgumentParser, *, roots: bool) -> None:
    command.add_argument("--baseline-source-identity", required=True)
    command.add_argument("--candidate-source-identity", required=True)
    if roots:
        command.add_argument("--baseline-source-root", required=True)
        command.add_argument("--candidate-source-root", required=True)


def _improvement_dependency_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--operator-config-root", required=True)
    command.add_argument("--protected-manifest", required=True)
    command.add_argument("--route-smoke", required=True)
    _add_improvement_identity_args(command, roots=True)


def _add_live_authority_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--live-authority-id")
    command.add_argument("--live-authority-file")


def _prepared_improvement_dependencies(args, contract: ImprovementContract) -> dict[str, Any]:
    return prepare_improvement_dependencies(
        operator_config_root=Path(args.operator_config_root),
        source_roots={
            "baseline-sage": Path(args.baseline_source_root),
            "candidate-sage": Path(args.candidate_source_root),
        },
        source_identities={
            "baseline-sage": contract.baseline_source_identity,
            "candidate-sage": contract.candidate_source_identity,
        },
        protected_manifest=Path(args.protected_manifest).expanduser().resolve(),
        route_smoke=Path(args.route_smoke).expanduser().resolve(),
    )


def _improvement_contract(args) -> ImprovementContract:
    order = IMPROVEMENT_ARM_ORDER
    if getattr(args, "pair_order", "baseline-first") == "candidate-first":
        order = tuple(reversed(IMPROVEMENT_ARM_ORDER))
    contract = ImprovementContract(
        baseline_source_identity=str(args.baseline_source_identity),
        candidate_source_identity=str(args.candidate_source_identity),
        arm_order=order,
    )
    contract.validate()
    return contract


def _retained_pair(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    records = read_records(Path(path).expanduser().resolve() / "pilot.jsonl")
    contracts = [row.get("contract") for row in records if row.get("record_type") == "contract"]
    comparisons = [row for row in records if row.get("record_type") == "comparison"]
    if len(contracts) != 1 or len(comparisons) != 1:
        raise PilotError("improvement pair ledger lacks one exact contract and comparison")
    if not isinstance(contracts[0], Mapping):
        raise PilotError("improvement pair contract record is malformed")
    return dict(contracts[0]), dict(comparisons[0])


def _replay_contract(records: list[dict[str, Any]]) -> PilotContract | ImprovementContract:
    retained = [row.get("contract") for row in records if row.get("record_type") == "contract"]
    if len(retained) != 1 or not isinstance(retained[0], Mapping):
        raise PilotError("replay requires one retained contract")
    value = retained[0]
    order = value.get("arm_order")
    if not isinstance(order, list):
        raise PilotError("retained contract lacks an arm order")
    if value.get("schema") == PilotContract().to_record()["schema"]:
        contract = PilotContract(
            arm_order=tuple(order), alternate_start=retained_alternate_start_binding(records),
        )
    elif value.get("schema") == "sage-trust-walker-isc8-improvement-pair-v1":
        contract = ImprovementContract(
            baseline_source_identity=str(value.get("baseline_source_identity") or ""),
            candidate_source_identity=str(value.get("candidate_source_identity") or ""),
            arm_order=tuple(order),
        )
    else:
        raise PilotError("retained protocol version is not supported for replay")
    contract.validate()
    if contract.to_record() != value:
        raise PilotError("retained contract is malformed or changed")
    return contract


def _check_native_rows(observed: object, operation_id: int) -> None:
    if not isinstance(observed, Mapping):
        raise PilotError("native observation is malformed")
    for key, id_key in (("task", "display_id"), ("credential", "id"), ("callback", "display_id")):
        rows = observed.get(key)
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping)
            or type(row.get("operation_id")) is not int
            or row["operation_id"] != operation_id
            or type(row.get(id_key)) is not int
            for row in rows
        ):
            raise PilotError("native observation rows lack the bound operation or identifiers")
        if len({row[id_key] for row in rows}) != len(rows):
            raise PilotError("native observation contains duplicate identifiers")
    response_ids = set()
    for row in observed["task"]:
        responses = row.get("responses")
        if not isinstance(responses, list):
            raise PilotError("native observation lacks task response rows")
        for response in responses:
            if (
                not isinstance(response, Mapping)
                or type(response.get("id")) is not int
                or response["id"] in response_ids
                or not isinstance(response.get("response_text"), str)
                or not isinstance(response.get("timestamp"), str)
            ):
                raise PilotError("native observation has malformed or duplicate responses")
            response_ids.add(response["id"])


def replay_pair(artifact_dir: str | Path, native_evidence_manifest: str | Path | None = None) -> dict[str, Any]:
    """Recompute retained outcomes locally; hashes bind files, not live-source authenticity."""
    root = Path(artifact_dir).expanduser().resolve()
    ledger = root / "pilot.jsonl"
    ledger_bytes = ledger.read_bytes()
    records = [json.loads(line) for line in ledger_bytes.decode().splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in records):
        raise PilotError("retained ledger contains malformed records")
    contract = _replay_contract(records)
    external = {}
    manifest_receipt = None
    if native_evidence_manifest is not None:
        manifest_path = Path(native_evidence_manifest).expanduser().resolve()
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("schema") != "sage-native-mythic-evidence-capture-receipt-v1"
            or manifest.get("query_sha256") != hashlib.sha256(TASK_OUTPUT_QUERY.encode()).hexdigest()
            or not isinstance(manifest.get("captures"), list)
        ):
            raise PilotError("external native evidence manifest is malformed or uses a different query")
        for capture in manifest["captures"]:
            if not isinstance(capture, Mapping) or capture.get("arm") not in contract.arm_order:
                raise PilotError("external native evidence manifest has an unknown arm")
            if capture["arm"] in external:
                raise PilotError("external native evidence manifest duplicates an arm")
            external[capture["arm"]] = {**capture, "query_sha256": manifest["query_sha256"]}
        manifest_receipt = {"sha256": hashlib.sha256(manifest_bytes).hexdigest()}

    reports = []
    replayed_records = [dict(row) for row in records]
    for arm in contract.arm_order:
        selected = {}
        for kind in ("arm_started", "arm_result"):
            rows = [row for row in replayed_records if row.get("record_type") == kind and row.get("arm") == arm]
            if len(rows) != 1:
                raise PilotError("replay requires one start and terminal result per arm")
            selected[kind] = rows[0]
        start, result = selected["arm_started"], selected["arm_result"]
        snapshot, execution = result.get("terminal_snapshot"), result.get("execution")
        if not isinstance(snapshot, Mapping) or not isinstance(execution, Mapping):
            raise PilotError("retained terminal result is malformed")
        operation_id = (start.get("runtime_identity") or {}).get("operation_id")
        if type(operation_id) is not int or snapshot.get("operation_id") != operation_id or snapshot.get("arm") != arm:
            raise PilotError("retained terminal result does not match the started arm")
        native = snapshot.get("native_evidence")
        binding = "terminal_observer"
        evidence_root = root
        if native is None:
            native = external.get(arm)
            binding = "external_capture_manifest"
            if native_evidence_manifest is not None:
                evidence_root = manifest_path.parent
        elif arm in external:
            raise PilotError("external capture cannot replace an observer-bound observation")
        if native is None:
            raise PilotError("original native evidence is missing; this retained arm cannot be replayed")
        if (
            not isinstance(native, Mapping) or native.get("arm") != arm
            or native.get("operation_id") != operation_id
            or native.get("query_sha256") != hashlib.sha256(TASK_OUTPUT_QUERY.encode()).hexdigest()
            or not isinstance(native.get("path"), str) or not native["path"]
        ):
            raise PilotError("native evidence receipt does not match the arm, operation, or query")
        native_path = evidence_root / Path(native["path"]).expanduser()
        native_bytes = native_path.read_bytes()
        if hashlib.sha256(native_bytes).hexdigest() != native.get("sha256"):
            raise PilotError("native evidence hash mismatch")
        observed = json.loads(native_bytes)
        _check_native_rows(observed, operation_id)
        # Bypass RuntimePilot construction: replay needs no credentials, fixtures, or live adapters.
        projector = object.__new__(RuntimePilot)
        projector.current_operation_id = operation_id
        for key in ("arm_started_at", "arm_deadline_at"):
            if not isinstance(snapshot.get(key), str) or snapshot[key] != execution.get(key):
                raise PilotError("terminal observation and execution windows disagree")
        projector.current_started_at = datetime.fromisoformat(snapshot["arm_started_at"])
        projector.current_deadline_at = datetime.fromisoformat(snapshot["arm_deadline_at"])
        if (
            projector.current_started_at.tzinfo is None or projector.current_deadline_at.tzinfo is None
            or (projector.current_deadline_at - projector.current_started_at).total_seconds() != contract.wall_seconds
        ):
            raise PilotError("retained execution window differs from the fixed contract")
        projected = projector._terminal_snapshot_from_observation(
            arm, observed, execution, bh_count=snapshot.get("bloodhound_domain_count"),
            observed_at=snapshot.get("observed_at"),
        )
        if projected != {key: value for key, value in snapshot.items() if key != "native_evidence"}:
            raise PilotError("native evidence does not reproduce the retained terminal observation")
        outcome = classify_terminal_snapshot(projected).to_record()
        if outcome != result.get("outcome"):
            raise PilotError("native evidence does not reproduce the retained outcome")
        cost = execution.get("cost") or {}
        responses = execution.get("response_records")
        if (
            not isinstance(responses, list) or type(cost.get("model_calls")) is not int
            or len(responses) != cost["model_calls"]
            or any(
                not isinstance(row, Mapping) or row.get("response_index") != index
                or row.get("effective_backend") != f"{contract.provider}:{contract.model}"
                or (row.get("total_tokens") is not None and (
                    type(row["total_tokens"]) is not int or row["total_tokens"] < 0
                ))
                for index, row in enumerate(responses, 1)
            )
        ):
            raise PilotError("retained response accounting is incomplete or has a different route")
        usage_complete = all(type(row.get("total_tokens")) is int for row in responses)
        tokens = sum(row["total_tokens"] for row in responses) if usage_complete else None
        if tokens != cost.get("total_tokens") or (execution.get("resource_complete") is True and not usage_complete):
            raise PilotError("response-derived usage disagrees with retained cost")
        result.update(terminal_snapshot=projected, outcome=outcome)
        reports.append({
            "arm": arm, "binding": binding, "native_sha256": native["sha256"],
            "operation_id": operation_id, "native_tasks": len(observed["task"]),
            "native_responses": sum(len(row["responses"]) for row in observed["task"]),
            "outcome": outcome, "model_calls": cost.get("model_calls"), "total_tokens": tokens,
            "source_identity": (start.get("start_identity") or {}).get("source_identity"),
        })
    summary = summarize_pair(replayed_records, contract)
    comparisons = [row for row in records if row.get("record_type") == "comparison"]
    if len(comparisons) != 1 or summary != {
        key: value for key, value in comparisons[0].items() if key not in {"record_type", "recorded_at"}
    }:
        raise PilotError("reconstructed comparison differs from the retained comparison")
    return {
        "schema": "sage-trust-walker-offline-replay-v1", "reproduced": True,
        "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
        "external_manifest": manifest_receipt, "contract": contract.to_record(),
        "projection_sha256": _sha256(Path(__file__)),
        "outcome_parser_sha256": _sha256(PAYLOAD / "ai/hillclimb/trust_walker_outcome.py"),
        "arms": reports,
        "dimensions": {key: summary[key] for key in (
            "protocol_valid", "pair_compatible", "comparison_ready", "lifecycle_complete",
            "isolation_complete", "resources_complete", "run_complete",
        )},
        "limitations": [
            "Local reproduction checks retained files and their bindings, not live-source authenticity.",
            "BloodHound count, start, settlement, cleanup, and provider telemetry remain retained observations.",
            "Replay uses the identified current parser; it does not rerun the measured model or range.",
        ],
    }


def _require_first_pair_ready(path: str | Path, contract: ImprovementContract) -> None:
    retained_contract, comparison = _retained_pair(path)
    if (
        retained_contract.get("schema") != "sage-trust-walker-isc8-improvement-pair-v1"
        or retained_contract.get("arm_order") != list(IMPROVEMENT_ARM_ORDER)
        or retained_contract.get("baseline_source_identity") != contract.baseline_source_identity
        or retained_contract.get("candidate_source_identity") != contract.candidate_source_identity
        or comparison.get("comparison_ready") is not True
        or comparison.get("lifecycle_complete") is not True
        or comparison.get("isolation_complete") is not True
        or comparison.get("resources_complete") is not True
        or comparison.get("run_complete") is not True
    ):
        raise PilotError("pair 2 requires a comparison-ready, safely cleaned baseline-first pair 1")


def summarize_improvement_experiment(
    pair_one: str | Path,
    pair_two: str | Path,
    contract: ImprovementContract,
) -> dict[str, Any]:
    first_contract, first = _retained_pair(pair_one)
    second_contract, second = _retained_pair(pair_two)
    return summarize_improvement_comparisons(
        (first_contract, second_contract), (first, second), contract,
    )


def summarize_improvement_comparisons(
    contract_records: tuple[Mapping[str, Any], Mapping[str, Any]],
    comparisons: tuple[Mapping[str, Any], Mapping[str, Any]],
    contract: ImprovementContract,
) -> dict[str, Any]:
    """Apply the same frozen decision to live summaries or separately derived reanalysis."""
    first, second = comparisons
    expected_orders = (list(IMPROVEMENT_ARM_ORDER), list(reversed(IMPROVEMENT_ARM_ORDER)))
    identity_bound = all(
        row.get("baseline_source_identity") == contract.baseline_source_identity
        and row.get("candidate_source_identity") == contract.candidate_source_identity
        for row in contract_records
    )
    orders_bound = tuple(row.get("arm_order") for row in contract_records) == expected_orders
    protocol_ready = bool(identity_bound and orders_bound and all(
        row.get("comparison_ready") is True
        and row.get("lifecycle_complete") is True
        and row.get("isolation_complete") is True
        and row.get("resources_complete") is True
        and row.get("run_complete") is True
        for row in (first, second)
    ))
    arms = [arm for row in (first, second) for arm in row.get("arms") or []]
    baseline_outcomes = [
        arm.get("objective_disposition") for arm in arms if arm.get("arm") == "baseline-sage"
    ]
    candidate_outcomes = [
        arm.get("objective_disposition") for arm in arms if arm.get("arm") == "candidate-sage"
    ]
    metrics = [row.get("primary_metric") or {} for row in (first, second)]
    metric_complete = len(metrics) == 2 and all(row.get("complete") is True for row in metrics)
    meaningful_effect = len(metrics) == 2 and all(
        row.get("meaningful_effect_met") is True for row in metrics
    )
    if not protocol_ready or len(baseline_outcomes) != 2 or len(candidate_outcomes) != 2:
        disposition = "inconclusive"
        reason = "one or both order-balanced pairs are not attributable and comparison-ready"
    elif any(value != "proven" for value in baseline_outcomes):
        disposition = "inconclusive"
        reason = "the unchanged path did not reproduce administrative control in both arms"
    elif any(value != "proven" for value in candidate_outcomes):
        disposition = "reject"
        reason = "the candidate regressed independent administrative-control proof"
    elif not metric_complete:
        disposition = "inconclusive"
        reason = "the frozen primary metric could not be extracted in every arm"
    elif meaningful_effect:
        disposition = "accept"
        reason = "both candidate arms met the frozen meaningful-effect threshold without outcome regression"
    else:
        disposition = "reject"
        reason = "the candidate did not meet the frozen meaningful-effect threshold in both pairs"
    return {
        "schema": "sage-trust-walker-isc8-improvement-experiment-result-v1",
        "disposition": disposition,
        "reason": reason,
        "protocol_ready": protocol_ready,
        "identity_bound": identity_bound,
        "orders_bound": orders_bound,
        "baseline_objective_dispositions": baseline_outcomes,
        "candidate_objective_dispositions": candidate_outcomes,
        "primary_metrics": metrics,
        "meaningful_effect_met_in_both_pairs": meaningful_effect,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--case-descriptor")
    improvement_plan = sub.add_parser("improvement-plan")
    _add_improvement_identity_args(improvement_plan, roots=False)
    improvement_prepare = sub.add_parser("prepare-improvement-pair")
    _improvement_dependency_args(improvement_prepare)
    for name in ("preflight", "rehearse-setup", "continue-setup", "run-pair"):
        command = sub.add_parser(name)
        command.add_argument("--artifact-dir", required=True)
        command.add_argument("--protected-manifest", required=True)
        command.add_argument("--route-smoke", required=True)
        command.add_argument("--case-descriptor")
        if name in {"rehearse-setup", "continue-setup", "run-pair"}:
            _add_live_authority_args(command)
        if name == "run-pair":
            command.add_argument(
                "--arm-order",
                choices=("shipping-first", "raw-first"),
                default="shipping-first",
                help="Prospectively select one of the two fixed matched-pair arm orders.",
            )
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--artifact-dir", required=True)
    inspect_preserved = sub.add_parser("inspect-preserved")
    inspect_preserved.add_argument("--artifact-dir", required=True)
    improvement = sub.add_parser("run-improvement-pair")
    improvement.add_argument("--artifact-dir", required=True)
    _add_live_authority_args(improvement)
    improvement.add_argument(
        "--pair-order", choices=("baseline-first", "candidate-first"), required=True,
    )
    improvement.add_argument("--prior-pair-artifact-dir")
    _improvement_dependency_args(improvement)
    summary = sub.add_parser("summarize-improvement")
    summary.add_argument("--pair-one-artifact-dir", required=True)
    summary.add_argument("--pair-two-artifact-dir", required=True)
    _add_improvement_identity_args(summary, roots=False)
    replay = sub.add_parser("replay-pair", help="Reproduce a retained comparison from native evidence, offline.")
    replay.add_argument("--artifact-dir", required=True)
    replay.add_argument("--native-evidence-manifest", help="Explicit capture receipt for older runs without observer retention.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "replay-pair":
        try:
            replayed = replay_pair(args.artifact_dir, args.native_evidence_manifest)
        except (PilotError, OSError, ValueError, TypeError, KeyError) as exc:
            print(json.dumps({
                "reproduced": False, "error_type": type(exc).__name__,
                "reason": str(exc) if isinstance(exc, PilotError) else "retained evidence is missing or malformed",
            }, indent=2, sort_keys=True))
            return 1
        print(json.dumps(replayed, indent=2, sort_keys=True))
        return 0
    try:
        if args.command in {
            "improvement-plan", "prepare-improvement-pair", "run-improvement-pair", "summarize-improvement",
        }:
            contract: PilotContract | ImprovementContract = _improvement_contract(args)
        else:
            arm_order = ARM_ORDER
            if getattr(args, "arm_order", "shipping-first") == "raw-first":
                arm_order = tuple(reversed(ARM_ORDER))
            alternate_start = (
                _load_alternate_start(args.case_descriptor)
                if getattr(args, "case_descriptor", None)
                else None
            )
            contract = PilotContract(arm_order=arm_order, alternate_start=alternate_start)
        contract.validate()
    except (PilotError, OSError) as exc:
        print(json.dumps({"ready": False, "error_type": type(exc).__name__, "reason": str(exc)}, indent=2, sort_keys=True))
        return 1
    authority_receipt: dict[str, Any] = {}
    if args.command in STANDING_LIVE_COMMANDS:
        try:
            authority_receipt = _resolve_live_authority(args, contract)
        except (PilotError, OSError, RuntimeError) as exc:
            print(json.dumps({
                "ready": False,
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }, indent=2, sort_keys=True))
            return 1
    if args.command == "plan":
        print(json.dumps({
            "contract": contract.to_record(),
            "supported_pair_orders": {
                "shipping-first": list(ARM_ORDER),
                "raw-first": list(reversed(ARM_ORDER)),
            },
            "live_surface": "run-pair",
            "setup_rehearsal_surface": "rehearse-setup",
            "setup_rehearsal_continuation_surface": "continue-setup",
            "recovery_surface": "cleanup",
            "single_arm_surface": False,
            "target_io": "Mythic payload tasks only",
        }, indent=2, sort_keys=True))
        return 0
    if args.command == "improvement-plan":
        print(json.dumps({
            "contract": contract.to_record(),
            "supported_pair_orders": {
                "baseline-first": list(IMPROVEMENT_ARM_ORDER),
                "candidate-first": list(reversed(IMPROVEMENT_ARM_ORDER)),
            },
            "live_surface": "run-improvement-pair",
            "final_summary_surface": "summarize-improvement",
            "single_arm_surface": False,
            "target_io": "Mythic payload tasks only",
        }, indent=2, sort_keys=True))
        return 0
    if args.command == "prepare-improvement-pair":
        try:
            result = _prepared_improvement_dependencies(args, contract)
        except (PilotError, OSError, RuntimeError) as exc:
            print(json.dumps({
                "ready": False,
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }, indent=2, sort_keys=True))
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "summarize-improvement":
        try:
            result = summarize_improvement_experiment(
                args.pair_one_artifact_dir,
                args.pair_two_artifact_dir,
                contract,
            )
        except (PilotError, OSError, RuntimeError) as exc:
            print(json.dumps({
                "disposition": "inconclusive",
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }, indent=2, sort_keys=True))
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "cleanup":
        try:
            recovery_ledger = validate_artifact_dir(args.artifact_dir) / "pilot.jsonl"
            prior_recoveries = sum(
                row.get("record_type") == "recovery_cleanup"
                for row in read_records(recovery_ledger)
            )
            if prior_recoveries >= RECOVERY_CLEANUP_ATTEMPT_LIMIT:
                raise PilotError("bounded recovery cleanup attempt limit is exhausted")
            runtime, start, execution = RuntimePilot.for_recovery(
                Path(args.artifact_dir).expanduser().resolve()
            )
            recovery_elapsed = 0.0
            settlement = {"complete": False, "reason": "recovery settlement did not run"}
            teardown = {"complete": False, "reason": "recovery teardown did not run"}
            settlement_started = time.monotonic()
            try:
                settlement = dict(runtime.stop_and_settle(runtime.current_arm, start, execution))
            except BaseException as exc:
                settlement = {"complete": False, "error_type": type(exc).__name__, "reason": str(exc)}
            finally:
                recovery_elapsed += max(0.0, time.monotonic() - settlement_started)
            teardown_started = time.monotonic()
            try:
                teardown = dict(runtime.teardown(runtime.current_arm, start))
            except BaseException as exc:
                teardown = {"complete": False, "error_type": type(exc).__name__, "reason": str(exc)}
            finally:
                recovery_elapsed += max(0.0, time.monotonic() - teardown_started)
            total_support_elapsed = runtime.prior_support_elapsed_seconds + recovery_elapsed
            result = {
                "complete": settlement.get("complete") is True and teardown.get("complete") is True,
                "settlement": {key: value for key, value in settlement.items() if key != "execution"},
                "teardown": teardown,
                "recovery_elapsed_seconds": recovery_elapsed,
                "setup_cleanup_elapsed_seconds": total_support_elapsed,
                "support_budget_exhausted": total_support_elapsed > contract.setup_cleanup_seconds,
            }
            append_record(recovery_ledger, {
                "record_type": "recovery_cleanup", **result,
            })
            if result["support_budget_exhausted"]:
                append_record(recovery_ledger, {
                    "record_type": "support_budget_exhausted",
                    "arm": runtime.current_arm,
                    "elapsed_seconds": total_support_elapsed,
                    "limit_seconds": contract.setup_cleanup_seconds,
                    "during": "recovery_cleanup",
                })
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result.get("complete") else 1
        except (PilotError, OSError, RuntimeError) as exc:
            print(json.dumps({
                "complete": False,
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }, indent=2, sort_keys=True))
            return 1
    if args.command == "inspect-preserved":
        try:
            runtime, start, execution = RuntimePilot.for_preserved_inspection(
                Path(args.artifact_dir).expanduser().resolve()
            )
            result = runtime.attest_preserved_state(
                runtime.current_arm,
                start,
                execution,
                {"stage": "independent_reinspection"},
            )
        except (PilotError, OSError, RuntimeError) as exc:
            result = {
                "schema": "sage-trust-walker-preserved-state-v1",
                "complete": False,
                "unknown_reason": str(exc),
                "error_type": type(exc).__name__,
            }
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0 if result.get("complete") is True else 1
    try:
        if args.command == "run-improvement-pair":
            prepared = _prepared_improvement_dependencies(args, contract)
            _bind_authority_to_preledger(authority_receipt, prepared)
        runtime = (
            _runtime(args, contract)
            if isinstance(contract, ImprovementContract) or contract.alternate_start is not None
            else _runtime(args)
        )
        runtime.live_authority_id = str(authority_receipt.get("authority_id") or "")
        runtime.live_authority_receipt = dict(authority_receipt)
        if args.command == "preflight":
            result = dict(runtime.preflight(contract))
            retained = _write_private(
                validate_artifact_dir(args.artifact_dir) / "preflight.json", result,
            )
            result["retained_preflight"] = retained
        elif args.command in {"rehearse-setup", "continue-setup"}:
            result = run_setup_rehearsals(
                runtime,
                contract,
                args.artifact_dir,
                resume=args.command == "continue-setup",
            )
        else:
            if args.command == "run-improvement-pair":
                if contract.arm_order == tuple(reversed(IMPROVEMENT_ARM_ORDER)):
                    if not args.prior_pair_artifact_dir:
                        raise PilotError("candidate-first pair 2 requires --prior-pair-artifact-dir")
                    _require_first_pair_ready(args.prior_pair_artifact_dir, contract)
                elif args.prior_pair_artifact_dir:
                    raise PilotError("baseline-first pair 1 must not name a prior pair")
            dependencies = PilotDependencies(
                runtime.preflight,
                runtime.reset,
                runtime.runtime_identity,
                runtime.run_arm,
                runtime.stop_and_settle,
                runtime.observe,
                runtime.teardown,
                runtime.attest_preserved_state,
            )
            result = run_pair(contract, args.artifact_dir, dependencies)
    except (PilotError, SystemExit, KeyError, OSError) as exc:
        print(json.dumps({"ready": False, "error_type": type(exc).__name__, "reason": str(exc)}, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if (
        args.command == "preflight"
        or (args.command in {"rehearse-setup", "continue-setup"} and result.get("complete") is True)
        or (args.command in {"run-pair", "run-improvement-pair"} and result.get("run_complete") is True)
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
