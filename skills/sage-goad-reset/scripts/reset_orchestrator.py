#!/usr/bin/env python3
"""Run the canonical Sage GOAD reset as a resumable, task-free state machine."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
RESET_DIR = REPO_ROOT / "skills" / "sage-goad-reset"
BOOTSTRAP = (
    REPO_ROOT
    / "skills"
    / "sage-callback-bootstrap"
    / "scripts"
    / "bootstrap_payloads.py"
)
READINESS_CONTRACT = RESET_DIR / "scripts" / "readiness_contract.py"
RANGE_PROFILE = RESET_DIR / "scripts" / "range_profile.py"
DEFAULT_CHECKPOINT_DIR = REPO_ROOT / ".sage_engagement" / "reset_runs"
SCHEMA = "sage-goad-reset-orchestration-v2"
RANGE_FIXTURE_QUALIFICATION_SCHEMA = "sage-range-fixture-qualification-v1"
TASK_COUNT_QUERY = """
query SageResetTaskCount($operationId: Int!) {
  whoami {
    status
    user_id
    username
    current_operation_id
  }
  task_aggregate(where: {operation_id: {_eq: $operationId}}) {
    aggregate {
      count
    }
  }
  task(
    where: {operation_id: {_eq: $operationId}}
    order_by: {id: desc}
    limit: 1
  ) {
    id
  }
}
"""
PHASE_NAMES = (
    "stop-sage",
    "archive-runtime-dbs",
    "reset-mythic",
    "rollback-range",
    "poweron-range",
    "wait-range-ips",
    "sync-range-time",
    "wipe-bloodhound",
    "restart-sage",
    "bootstrap-chat-and-foothold",
    "await-operator-foothold-launch",
    "post-callback-preflight",
    "final-readiness",
)
RANGE_FIXTURE_QUALIFICATION_PHASES = (
    "capture-task-and-callback-baseline",
    "rollback-range",
    "poweron-range",
    "wait-range-ips",
    "sync-range-time",
    "await-operator-foothold-launch",
    "fresh-callback-selection",
    "final-start-readiness",
)
_FIXTURE_ACTION_BINDING_KEYS = {
    "mechanism", "adapter", "range_profile_id", "ludus_range_id",
    "ludus_mcp_server",
}
AMBIGUOUS_ON_INTERRUPT = {
    "reset-mythic",
    "bootstrap-chat-and-foothold",
}
NON_RETRYABLE_FAILED_PHASES = {
    *AMBIGUOUS_ON_INTERRUPT,
    "await-operator-foothold-launch",
}
TASK_OBSERVATION_PHASES = {
    "reset-mythic",
    "bootstrap-chat-and-foothold",
    "post-callback-preflight",
    "final-readiness",
}
TASK_OBSERVATION_SEQUENCE = (
    "reset-mythic",
    "bootstrap-chat-and-foothold",
    "operator-foothold-launch",
    "post-callback-preflight",
    "final-readiness",
)
READY_RESULT_PHASES = {
    "wait-range-ips",
    "final-readiness",
}
PHASE_STATUSES = {
    "pending",
    "running",
    "completed",
    "failed",
    "awaiting_operator",
}
TERMINAL_STATES = {
    "in_progress",
    "awaiting_operator",
    "blocked",
    "complete",
}
INTERACTIVE_STATUSES = {
    "pending",
    "awaiting_operator",
    "completed",
}
INPUT_KEYS = {
    "snapshot",
    "range_profile_id",
    "ludus_range_id",
    "ludus_mcp_server",
    "bootstrap_mode",
    "retained_callback_config",
    "foothold_payload_type",
    "foothold_host",
    "foothold_user_match",
    "callback_host",
    "download_dir",
    "prepare_chat",
    "restart_env",
    "range_ready_timeout",
    "range_poll_interval",
    "mythic_server",
    "mythic_user",
    "mythic_env_path",
}
SECRET_KEY = re.compile(
    r"(?:password|secret|token|api[_-]?key|credential)",
    flags=re.IGNORECASE,
)


class ResetError(RuntimeError):
    pass


def _raise_reset(message: str) -> None:
    raise ResetError(message)


def _failure_reason(exc: Exception) -> str:
    """Persist a non-empty diagnostic without exposing an exception object."""
    message = str(exc).strip()
    return message or type(exc).__name__


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ResetError(f"Unable to load helper module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if SECRET_KEY.search(str(key))
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def _parse_restart_env(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ResetError(f"restart env must use KEY=VALUE: {value!r}")
        key, item = value.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ResetError(f"invalid restart env key: {key!r}")
        if SECRET_KEY.search(key):
            raise ResetError(
                f"secret-bearing restart env is not persisted: {key}"
            )
        parsed[key] = item
    return parsed


def _config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = str(args.snapshot or "").strip()
    if not snapshot:
        raise ResetError("--snapshot must name the exact live restore target")
    bootstrap_mode = str(args.bootstrap_mode)
    retained_path = (
        str(Path(args.retained_callback_config).expanduser())
        if args.retained_callback_config
        else ""
    )
    if bootstrap_mode == "retained-callback" and not retained_path:
        raise ResetError(
            "retained-callback mode requires --retained-callback-config"
        )
    if retained_path and not Path(retained_path).is_file():
        raise ResetError(
            f"retained callback config does not exist: {retained_path}"
        )
    callback_host = str(
        args.callback_host
        or os.environ.get("APOLLO_CALLBACK_HOST")
        or ""
    ).strip()
    if bootstrap_mode == "fresh-apollo" and not callback_host:
        raise ResetError(
            "fresh-apollo mode requires --callback-host or "
            "APOLLO_CALLBACK_HOST"
        )
    restart_env = _parse_restart_env(args.restart_env)
    profile_module = _load_module("sage_reset_range_profile_config", RANGE_PROFILE)
    try:
        profile = profile_module.get_profile(args.range_profile)
    except ValueError as exc:
        raise ResetError(str(exc)) from exc
    config = {
        "snapshot": snapshot,
        "range_profile_id": profile["id"],
        "ludus_range_id": str(args.ludus_range_id or "").strip(),
        "ludus_mcp_server": str(args.ludus_mcp_server or "").strip(),
        "bootstrap_mode": bootstrap_mode,
        "retained_callback_config": retained_path,
        "foothold_payload_type": str(args.foothold_payload_type),
        "foothold_host": str(args.foothold_host),
        "foothold_user_match": str(args.foothold_user_match),
        "callback_host": callback_host,
        "download_dir": str(args.download_dir or ""),
        "prepare_chat": bool(args.prepare_chat),
        "restart_env": restart_env,
        "range_ready_timeout": int(args.range_ready_timeout),
        "range_poll_interval": float(args.range_poll_interval),
        "mythic_server": str(args.mythic_server),
        "mythic_user": str(args.mythic_user),
        "mythic_env_path": (
            str(Path(args.mythic_env_path).expanduser())
            if str(args.mythic_env_path or "").strip()
            else ""
        ),
    }
    contract = _load_module(
        "sage_reset_readiness_contract_config", READINESS_CONTRACT
    )
    try:
        contract.validate_timeout_budget(
            outer_timeout_seconds=config["range_ready_timeout"],
            child_timeout_seconds=contract.LUDUS_PROBE_TIMEOUT_SECONDS,
            grace_seconds=contract.DEFAULT_TIMEOUT_GRACE_SECONDS,
        )
    except ValueError as exc:
        raise ResetError(str(exc)) from exc
    return config


def _phase_commands(config: dict[str, Any]) -> dict[str, list[str]]:
    ludus_prefix = [str(PYTHON), str(RESET_DIR / "scripts" / "ludus.py")]
    sync_prefix = [str(PYTHON), str(RESET_DIR / "scripts" / "sync_range_time.py")]
    for prefix in (ludus_prefix, sync_prefix):
        if config["ludus_mcp_server"]:
            prefix.extend(["--mcp-server", config["ludus_mcp_server"]])
        if config["ludus_range_id"]:
            prefix.extend(["--range-id", config["ludus_range_id"]])
    bootstrap = [
        str(PYTHON),
        str(BOOTSTRAP),
        "bootstrap-reset",
        (
            "--prepare-chat"
            if config["prepare_chat"]
            else "--no-prepare-chat"
        ),
    ]
    if config["bootstrap_mode"] == "retained-callback":
        bootstrap.extend(
            [
                "--use-retained-callback",
                "--retained-callback-config",
                config["retained_callback_config"],
            ]
        )
    else:
        bootstrap.extend(["--callback-host", config["callback_host"]])
        if config["download_dir"]:
            bootstrap.extend(["--download-dir", config["download_dir"]])

    restart = [
        "/bin/bash",
        str(RESET_DIR / "scripts" / "sage_restart.sh"),
        *[
            f"{key}={value}"
            for key, value in sorted(config["restart_env"].items())
        ],
    ]
    readiness = [
        str(PYTHON),
        str(BOOTSTRAP),
        "readiness",
        "--runtime-dbs-archived",
        "--foothold-payload-type",
        config["foothold_payload_type"],
        "--foothold-host",
        config["foothold_host"],
        "--foothold-user-match",
        config["foothold_user_match"],
        (
            "--require-prepared-channel"
            if config["prepare_chat"]
            else "--no-require-prepared-channel"
        ),
    ]
    return {
        "stop-sage": [
            "/bin/bash",
            str(RESET_DIR / "scripts" / "sage_stop.sh"),
        ],
        "archive-runtime-dbs": [
            str(PYTHON),
            str(RESET_DIR / "scripts" / "archive_runtime_dbs.py"),
        ],
        "reset-mythic": [
            "/bin/bash",
            str(RESET_DIR / "scripts" / "mythic_reset.sh"),
            "--yes",
        ],
        "rollback-range": [
            *ludus_prefix,
            "rollback",
            config["snapshot"],
            "--yes",
        ],
        "poweron-range": [
            *ludus_prefix,
            "poweron",
            "all",
        ],
        "wait-range-ips": [
            "internal:wait-ludus-ready",
            str(config["range_ready_timeout"]),
        ],
        "sync-range-time": [
            *sync_prefix,
            "sync",
            "--yes",
        ],
        "wipe-bloodhound": [
            "uv",
            "--directory",
            str(REPO_ROOT.parent / "bloodhound_mcp"),
            "run",
            "python",
            str(RESET_DIR / "scripts" / "bh_reset.py"),
            "wipe",
            "--yes",
        ],
        "restart-sage": restart,
        "bootstrap-chat-and-foothold": bootstrap,
        "await-operator-foothold-launch": [
            "operator:launch-foothold-payload"
        ],
        "post-callback-preflight": [
            str(PYTHON),
            str(BOOTSTRAP),
            "post-callback-preflight",
            "--foothold-host",
            config["foothold_host"],
            "--foothold-user-match",
            config["foothold_user_match"],
        ],
        "final-readiness": readiness,
    }


def _new_checkpoint(
    config: dict[str, Any],
    run_id: str,
    *,
    scenario_manifest_binding: dict[str, str] | None = None,
) -> dict[str, Any]:
    commands = _phase_commands(config)
    checkpoint = {
        "schema": SCHEMA,
        "run_id": run_id,
        "created_at": _now(),
        "updated_at": _now(),
        "inputs": _redact(config),
        "phases": [
            {
                "name": name,
                "status": "pending",
                "command": _redact(commands[name]),
                "started_at": None,
                "finished_at": None,
                "result_summary": None,
                "error": None,
            }
            for name in PHASE_NAMES
        ],
        "task_baseline": None,
        "task_observations": [],
        "interactive_boundary": {
            "required": True,
            "kind": "operator-launched-foothold-payload",
            "status": "pending",
        },
        "terminal": {"state": "in_progress", "reason": None},
    }
    if scenario_manifest_binding is not None:
        checkpoint["scenario_manifest_binding"] = _validate_scenario_manifest_binding(
            scenario_manifest_binding
        )
    return checkpoint


def _checkpoint_summary(
    checkpoint: dict[str, Any], path: Path
) -> dict[str, Any]:
    completed = [
        phase["name"]
        for phase in checkpoint.get("phases") or []
        if phase.get("status") == "completed"
    ]
    current = next(
        (
            phase
            for phase in checkpoint.get("phases") or []
            if phase.get("status")
            in {"pending", "running", "failed", "awaiting_operator"}
        ),
        None,
    )
    return {
        "schema": SCHEMA,
        "run_id": checkpoint.get("run_id"),
        "checkpoint": str(path),
        "terminal": checkpoint.get("terminal"),
        "completed_phases": len(completed),
        "total_phases": len(checkpoint.get("phases") or []),
        "last_completed_phase": completed[-1] if completed else None,
        "current_phase": current.get("name") if current else None,
        "current_phase_status": (
            current.get("status") if current else "complete"
        ),
        "next_command": current.get("command") if current else None,
        "task_baseline": checkpoint.get("task_baseline"),
        "latest_task_observation": (
            checkpoint.get("task_observations") or [None]
        )[-1],
        "interactive_boundary": checkpoint.get("interactive_boundary"),
    }


def _raise_invalid(path: Path, message: str) -> None:
    raise ResetError(f"invalid reset checkpoint at {path}: {message}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _aware_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _validate_phase_output(
    phase_name: str,
    output: Any,
    *,
    error: Callable[[str], None],
) -> dict[str, Any]:
    if not isinstance(output, dict):
        error(f"{phase_name} result must be an object")
    if phase_name == "wait-range-ips":
        if (
            output.get("schema") != "sage-readiness-decision-v1"
            or output.get("lane") != "range_boot"
            or output.get("ready") is not True
            or output.get("launch_released") is not True
        ):
            error("wait-range-ips requires the canonical range_boot readiness decision")
    if phase_name == "final-readiness":
        contract = _load_module(
            "sage_reset_readiness_contract_output", READINESS_CONTRACT
        )
        decision = contract.parse_release_output(output, expected_lane="full")
        if decision.get("ready") is not True:
            error(
                "final-readiness requires canonical full readiness: "
                + "; ".join(decision.get("blockers") or [])
            )
    if phase_name == "await-operator-foothold-launch" and (
        output.get("operator_acknowledged") is not True
        or not _is_int(output.get("payload_tasks_issued"))
        or output["payload_tasks_issued"] != 0
    ):
        error(
            "await-operator-foothold-launch requires operator "
            "acknowledgement and zero payload tasks"
        )
    for field in ("ok", "ready", "success"):
        if field in output and output[field] is not True:
            error(f"{phase_name} result field {field} must be true")
    status = output.get("status")
    if (
        isinstance(status, str)
        and status.strip().casefold()
        in {"error", "failed", "failure", "blocked"}
    ):
        error(f"{phase_name} result status reports failure")
    if output.get("error"):
        error(f"{phase_name} result contains an error")
    if "exit_code" in output and (
        not _is_int(output["exit_code"]) or output["exit_code"] != 0
    ):
        error(f"{phase_name} result exit_code must be integer zero")
    return output


def _completed_result(
    phase_name: str, output: dict[str, Any]
) -> dict[str, Any]:
    return {
        "phase": phase_name,
        "succeeded": True,
        "output": _redact(output),
    }


def _validate_completed_result(
    phase_name: str,
    value: Any,
    path: Path,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _raise_invalid(path, f"{label} must be an object")
    if set(value) != {"phase", "succeeded", "output"}:
        _raise_invalid(path, f"{label} has an invalid success envelope")
    if value.get("phase") != phase_name:
        _raise_invalid(path, f"{label}.phase does not match phase name")
    if value.get("succeeded") is not True:
        _raise_invalid(path, f"{label}.succeeded must be true")
    return _validate_phase_output(
        phase_name,
        value.get("output"),
        error=lambda message: _raise_invalid(path, f"{label}: {message}"),
    )


def _validate_inputs(
    inputs: Any, path: Path, *, require_live_qualified: bool = True
) -> dict[str, Any]:
    if not isinstance(inputs, dict):
        _raise_invalid(path, "inputs must be an object")
    if set(inputs) != INPUT_KEYS:
        _raise_invalid(
            path,
            "inputs keys must exactly match the orchestration contract",
        )
    required_strings = (
        "snapshot",
        "range_profile_id",
        "foothold_payload_type",
        "foothold_host",
        "foothold_user_match",
        "mythic_server",
        "mythic_user",
        "mythic_env_path",
    )
    for key in required_strings:
        if not isinstance(inputs.get(key), str) or not inputs[key].strip():
            _raise_invalid(path, f"inputs.{key} must be a non-empty string")
    for key in (
        "retained_callback_config", "callback_host", "download_dir",
        "ludus_range_id", "ludus_mcp_server",
    ):
        if not isinstance(inputs.get(key), str):
            _raise_invalid(path, f"inputs.{key} must be a string")
    profile_module = _load_module("sage_reset_range_profile_checkpoint", RANGE_PROFILE)
    try:
        profile = profile_module.get_profile(inputs["range_profile_id"])
        if require_live_qualified:
            profile_module.require_live_qualified(profile)
    except ValueError as exc:
        _raise_invalid(path, f"inputs.range_profile_id is not releasable: {exc}")
    if inputs.get("bootstrap_mode") not in {
        "fresh-apollo",
        "retained-callback",
    }:
        _raise_invalid(path, "inputs.bootstrap_mode is unsupported")
    if (
        inputs["bootstrap_mode"] == "fresh-apollo"
        and not inputs["callback_host"].strip()
    ):
        _raise_invalid(
            path,
            "fresh-apollo inputs require a non-empty callback_host",
        )
    if (
        inputs["bootstrap_mode"] == "retained-callback"
        and not inputs["retained_callback_config"].strip()
    ):
        _raise_invalid(
            path,
            "retained-callback inputs require retained_callback_config",
        )
    if not isinstance(inputs.get("prepare_chat"), bool):
        _raise_invalid(path, "inputs.prepare_chat must be boolean")
    if not _is_int(inputs.get("range_ready_timeout")):
        _raise_invalid(path, "inputs.range_ready_timeout must be an integer")
    if inputs["range_ready_timeout"] <= 0:
        _raise_invalid(path, "inputs.range_ready_timeout must be positive")
    poll_interval = inputs.get("range_poll_interval")
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, (int, float))
        or poll_interval <= 0
    ):
        _raise_invalid(
            path,
            "inputs.range_poll_interval must be a positive number",
        )
    restart_env = inputs.get("restart_env")
    if not isinstance(restart_env, dict):
        _raise_invalid(path, "inputs.restart_env must be an object")
    for key, value in restart_env.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or SECRET_KEY.search(key)
            or not isinstance(value, str)
        ):
            _raise_invalid(
                path,
                "inputs.restart_env contains an invalid persisted entry",
            )
    return inputs


def _validate_observation(
    value: Any, path: Path, *, label: str, include_phase: bool
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _raise_invalid(path, f"{label} must be an object")
    if not _is_int(value.get("count")) or value["count"] < 0:
        _raise_invalid(path, f"{label}.count must be a non-negative integer")
    if value.get("scope") != "mythic-operation":
        _raise_invalid(path, f"{label}.scope must be mythic-operation")
    if (
        not _is_int(value.get("operation_id"))
        or value["operation_id"] <= 0
    ):
        _raise_invalid(path, f"{label}.operation_id must be positive")
    if not _is_int(value.get("operator_id")) or value["operator_id"] <= 0:
        _raise_invalid(path, f"{label}.operator_id must be positive")
    if (
        not isinstance(value.get("operator_username"), str)
        or not value["operator_username"].strip()
    ):
        _raise_invalid(path, f"{label}.operator_username is invalid")
    if _aware_time(value.get("observed_at")) is None:
        _raise_invalid(
            path,
            f"{label}.observed_at must be timezone-aware ISO-8601",
        )
    max_task_id = value.get("max_task_id")
    if max_task_id is not None and (
        not _is_int(max_task_id) or max_task_id < 0
    ):
        _raise_invalid(
            path,
            f"{label}.max_task_id must be null or a non-negative integer",
        )
    if include_phase:
        if value.get("phase") not in TASK_OBSERVATION_SEQUENCE:
            _raise_invalid(path, f"{label}.phase is not recognized")
        if not _is_int(value.get("delta_from_baseline")):
            _raise_invalid(
                path,
                f"{label}.delta_from_baseline must be an integer",
            )
    else:
        if "phase" in value or "delta_from_baseline" in value:
            _raise_invalid(
                path,
                f"{label} must not include phase or delta fields",
            )
    return value


def _validate_checkpoint(
    checkpoint: Any, path: Path, *, require_live_qualified: bool = True
) -> dict[str, Any]:
    if not isinstance(checkpoint, dict) or checkpoint.get("schema") != SCHEMA:
        raise ResetError(f"unsupported reset checkpoint at {path}")
    if "scenario_manifest_binding" in checkpoint:
        try:
            _validate_scenario_manifest_binding(checkpoint["scenario_manifest_binding"])
        except ResetError as exc:
            _raise_invalid(path, str(exc))
    if not isinstance(checkpoint.get("run_id"), str) or not checkpoint[
        "run_id"
    ].strip():
        _raise_invalid(path, "run_id must be a non-empty string")
    for key in ("created_at", "updated_at"):
        if not isinstance(checkpoint.get(key), str) or not checkpoint[key]:
            _raise_invalid(path, f"{key} must be a non-empty string")
    inputs = _validate_inputs(
        checkpoint.get("inputs"), path, require_live_qualified=require_live_qualified
    )

    phases = checkpoint.get("phases")
    if not isinstance(phases, list):
        _raise_invalid(path, "phases must be a list")
    names = [phase.get("name") if isinstance(phase, dict) else None for phase in phases]
    if names != list(PHASE_NAMES):
        _raise_invalid(
            path,
            "phases must contain the exact canonical names in order",
        )
    commands = _phase_commands(inputs)
    first_incomplete: int | None = None
    active_status: str | None = None
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            _raise_invalid(path, f"phases[{index}] must be an object")
        status = phase.get("status")
        if status not in PHASE_STATUSES:
            _raise_invalid(path, f"phases[{index}].status is not recognized")
        if phase.get("command") != _redact(commands[phase["name"]]):
            _raise_invalid(
                path,
                f"phases[{index}].command does not match inputs",
            )
        started_at = phase.get("started_at")
        finished_at = phase.get("finished_at")
        if started_at is not None and not isinstance(started_at, str):
            _raise_invalid(path, f"phases[{index}].started_at is invalid")
        if finished_at is not None and not isinstance(finished_at, str):
            _raise_invalid(path, f"phases[{index}].finished_at is invalid")
        error = phase.get("error")
        if error is not None and not isinstance(error, str):
            _raise_invalid(path, f"phases[{index}].error is invalid")
        result_summary = phase.get("result_summary")
        if result_summary is not None and not isinstance(
            result_summary, dict
        ):
            _raise_invalid(
                path,
                f"phases[{index}].result_summary is invalid",
            )
        if status == "pending":
            if (
                started_at is not None
                or finished_at is not None
                or result_summary is not None
                or error is not None
            ):
                _raise_invalid(
                    path,
                    f"phases[{index}] pending state carries execution data",
                )
        elif status == "running":
            if (
                started_at is None
                or finished_at is not None
                or result_summary is not None
                or error is not None
            ):
                _raise_invalid(path, f"phases[{index}] running state is invalid")
        elif status == "completed":
            if (
                started_at is None
                or finished_at is None
                or result_summary is None
                or error is not None
            ):
                _raise_invalid(
                    path,
                    f"phases[{index}] completed state is invalid",
                )
            _validate_completed_result(
                phase["name"],
                result_summary,
                path,
                label=f"phases[{index}].result_summary",
            )
        elif status == "failed":
            if (
                started_at is None
                or finished_at is None
                or not isinstance(error, str)
                or not error
            ):
                _raise_invalid(path, f"phases[{index}] failed state is invalid")
        elif status == "awaiting_operator":
            if (
                phase["name"] != "await-operator-foothold-launch"
                or started_at is None
                or finished_at is not None
                or result_summary is not None
                or error is not None
            ):
                _raise_invalid(
                    path,
                    f"phases[{index}] awaiting_operator state is invalid",
                )
        if status != "completed":
            if first_incomplete is None:
                first_incomplete = index
                active_status = status
            elif status != "pending":
                _raise_invalid(
                    path,
                    "only the first incomplete phase may be active",
                )
        elif first_incomplete is not None:
            _raise_invalid(
                path,
                "completed phases must form a contiguous prefix",
            )

    boundary = checkpoint.get("interactive_boundary")
    if not isinstance(boundary, dict):
        _raise_invalid(path, "interactive_boundary must be an object")
    if (
        boundary.get("required") is not True
        or boundary.get("kind") != "operator-launched-foothold-payload"
        or boundary.get("status") not in INTERACTIVE_STATUSES
    ):
        _raise_invalid(path, "interactive_boundary shape is invalid")
    await_phase = phases[PHASE_NAMES.index("await-operator-foothold-launch")]
    expected_boundary_status = "pending"
    if await_phase["status"] in {"awaiting_operator", "failed"}:
        expected_boundary_status = "awaiting_operator"
    elif await_phase["status"] == "completed":
        expected_boundary_status = "completed"
    if boundary["status"] != expected_boundary_status:
        _raise_invalid(
            path,
            "interactive_boundary status does not match phase progress",
        )

    terminal = checkpoint.get("terminal")
    if not isinstance(terminal, dict):
        _raise_invalid(path, "terminal must be an object")
    terminal_state = terminal.get("state")
    terminal_reason = terminal.get("reason")
    if terminal_state not in TERMINAL_STATES:
        _raise_invalid(path, "terminal.state is not recognized")
    if terminal_reason is not None and not isinstance(terminal_reason, str):
        _raise_invalid(path, "terminal.reason is invalid")
    if terminal_state in {"in_progress", "complete"}:
        if terminal_reason is not None:
            _raise_invalid(
                path,
                f"terminal.reason must be null for {terminal_state}",
            )
    elif not terminal_reason:
        _raise_invalid(
            path,
            f"terminal.reason must be non-empty for {terminal_state}",
        )
    expected_terminal_state = "in_progress"
    if first_incomplete is None:
        expected_terminal_state = "complete"
    elif active_status == "failed":
        expected_terminal_state = "blocked"
    elif active_status == "awaiting_operator":
        expected_terminal_state = "awaiting_operator"
    if terminal_state != expected_terminal_state:
        _raise_invalid(
            path,
            "terminal.state does not match phase progress",
        )

    baseline = checkpoint.get("task_baseline")
    observations = checkpoint.get("task_observations")
    if not isinstance(observations, list):
        _raise_invalid(path, "task_observations must be a list")
    observation_phases: list[str] = []
    for index, observation in enumerate(observations):
        parsed = _validate_observation(
            observation,
            path,
            label=f"task_observations[{index}]",
            include_phase=True,
        )
        observation_phases.append(parsed["phase"])
    completed_names = {
        phase["name"] for phase in phases if phase["status"] == "completed"
    }
    required_observations = [
        phase_name
        for phase_name in TASK_OBSERVATION_SEQUENCE
        if (
            phase_name in completed_names
            or (
                phase_name == "operator-foothold-launch"
                and "await-operator-foothold-launch" in completed_names
            )
        )
    ]
    failed_phase_name = (
        phases[first_incomplete]["name"]
        if first_incomplete is not None and active_status == "failed"
        else None
    )
    allowed_observations = list(required_observations)
    if failed_phase_name in TASK_OBSERVATION_SEQUENCE:
        allowed_observations.append(failed_phase_name)
    if observation_phases not in (
        required_observations,
        allowed_observations,
    ):
        _raise_invalid(
            path,
            "task_observations do not match completed phase progress",
        )
    if observation_phases:
        if baseline is None:
            _raise_invalid(path, "task_baseline is missing")
        parsed_baseline = _validate_observation(
            baseline,
            path,
            label="task_baseline",
            include_phase=False,
        )
        if parsed_baseline["count"] != 0:
            _raise_invalid(path, "task_baseline.count must remain zero")
        if parsed_baseline["max_task_id"] is not None:
            _raise_invalid(
                path,
                "task_baseline.max_task_id must be null at zero tasks",
            )
        first_observation = observations[0]
        for key, value in parsed_baseline.items():
            if first_observation.get(key) != value:
                _raise_invalid(
                    path,
                    "task_baseline does not match reset-mythic observation",
                )
        if first_observation["phase"] != "reset-mythic":
            _raise_invalid(
                path,
                "first task observation must come from reset-mythic",
            )
        previous_observed_at = _aware_time(
            parsed_baseline["observed_at"]
        )
        for index, observation in enumerate(observations):
            expected_delta = observation["count"] - parsed_baseline["count"]
            if observation["delta_from_baseline"] != expected_delta:
                _raise_invalid(
                    path,
                    f"task_observations[{index}] delta is inconsistent",
                )
            if observation["delta_from_baseline"] != 0:
                _raise_invalid(
                    path,
                    f"task_observations[{index}] records a nonzero task delta",
                )
            if observation["max_task_id"] != parsed_baseline["max_task_id"]:
                _raise_invalid(
                    path,
                    f"task_observations[{index}] latest task id drifted",
                )
            for field in (
                "scope",
                "operation_id",
                "operator_id",
                "operator_username",
            ):
                if observation[field] != parsed_baseline[field]:
                    _raise_invalid(
                        path,
                        f"task_observations[{index}] {field} drifted",
                    )
            observed_at = _aware_time(observation["observed_at"])
            if observed_at < previous_observed_at:
                _raise_invalid(
                    path,
                    f"task_observations[{index}] reverses observation time",
                )
            previous_observed_at = observed_at
    elif baseline is not None:
        _raise_invalid(
            path,
            "task_baseline cannot exist without task_observations",
        )
    return checkpoint


def validate_scenario_reset_binding(
    manifest: Any,
    checkpoint: Any,
    path: Path,
    *,
    expected_run_id: str,
    not_before: str,
    expected_range_id: str | None = None,
    expected_ludus_mcp_server: str | None = None,
    external_source_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Bind a scenario manifest to a completed reset checkpoint without granting readiness.

    Optional range and MCP-server selectors compare prospective caller inputs to
    persisted checkpoint inputs. They identify the intended reset record; they
    do not prove endpoint provenance or readiness.
    """
    if checkpoint is None:
        raise ResetError("reset evidence is missing")
    if not isinstance(expected_run_id, str) or not expected_run_id.strip():
        raise ResetError("expected reset run ID is missing")
    selectors = {
        "range ID": expected_range_id,
        "Ludus MCP server": expected_ludus_mcp_server,
    }
    for label, value in selectors.items():
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ResetError(
                f"expected {label} must be a non-empty string when supplied"
            )
    bound = _aware_time(not_before)
    if bound is None:
        raise ResetError("reset not-before bound must be timezone-aware ISO-8601")
    profile_module = _load_module("sage_reset_range_profile_manifest", RANGE_PROFILE)
    report = profile_module.validate_scenario_manifest(
        manifest, external_source_dir=external_source_dir,
    )
    if not report["manifest_valid"]:
        raise ResetError("scenario manifest is invalid: " + ", ".join(report["errors"]))
    parsed = _validate_checkpoint(checkpoint, path, require_live_qualified=False)
    if parsed["run_id"] != expected_run_id:
        raise ResetError("checkpoint run ID does not match prospective binding")
    created_at = _aware_time(parsed["created_at"])
    if created_at is None or created_at < bound:
        raise ResetError("checkpoint predates prospective not-before bound")
    inputs = parsed["inputs"]
    if inputs["range_profile_id"] != manifest["profile_id"]:
        raise ResetError("checkpoint profile does not match scenario manifest")
    if inputs["snapshot"] != manifest["snapshot_id"]:
        raise ResetError("checkpoint snapshot does not match scenario manifest")
    if (
        expected_range_id is not None
        and inputs["ludus_range_id"] != expected_range_id.strip()
    ):
        raise ResetError("checkpoint range ID does not match prospective selector")
    if (
        expected_ludus_mcp_server is not None
        and inputs["ludus_mcp_server"] != expected_ludus_mcp_server.strip()
    ):
        raise ResetError(
            "checkpoint Ludus MCP server does not match prospective selector"
        )
    if parsed["schema"] != manifest["reset_checkpoint_schema"]:
        raise ResetError("checkpoint schema does not match scenario manifest")
    expected_binding = _scenario_manifest_binding(manifest)
    if parsed.get("scenario_manifest_binding") != expected_binding:
        raise ResetError(
            "reset checkpoint does not bind scenario manifest source; future reset producer must persist "
            "scenario_manifest_binding with the manifest hash, profile, source, and snapshot"
        )
    if report["readiness_blockers"]:
        raise ResetError(
            "scenario reset binding is not live-ready: "
            + ", ".join(report["readiness_blockers"])
        )
    if parsed["terminal"]["state"] != "complete":
        raise ResetError("reset checkpoint is incomplete")
    return parsed


def _scenario_manifest_binding(manifest: dict[str, Any]) -> dict[str, str]:
    encoded = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    return {
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "profile_id": manifest["profile_id"],
        "source_path": manifest["source_path"],
        "source_sha256": manifest["source_sha256"],
        "snapshot_id": manifest["snapshot_id"],
    }


def _validate_scenario_manifest_binding(value: Any) -> dict[str, str]:
    expected = {
        "manifest_sha256", "profile_id", "source_path", "source_sha256", "snapshot_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ResetError("scenario manifest binding has invalid fields")
    binding = {key: value[key] for key in expected}
    if any(not isinstance(item, str) or not item.strip() for item in binding.values()):
        raise ResetError("scenario manifest binding values must be non-empty strings")
    for key in ("manifest_sha256", "source_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", binding[key]):
            raise ResetError(f"scenario manifest binding {key} must be a SHA-256 hex digest")
    return binding


def _scenario_manifest_capture(
    args: argparse.Namespace, config: dict[str, Any]
) -> dict[str, str] | None:
    value = str(getattr(args, "scenario_manifest", "") or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResetError(f"unable to load scenario manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ResetError("scenario manifest must be a JSON object")
    profile_module = _load_module("sage_reset_range_profile_capture", RANGE_PROFILE)
    report = profile_module.validate_scenario_manifest(
        manifest,
        external_source_dir=getattr(args, "external_source_dir", None),
    )
    if not report["manifest_valid"]:
        raise ResetError("scenario manifest is invalid: " + ", ".join(report["errors"]))
    if manifest["profile_id"] != config["range_profile_id"]:
        raise ResetError("scenario manifest profile does not match reset profile")
    if manifest["snapshot_id"] != config["snapshot"]:
        raise ResetError("scenario manifest snapshot does not match reset snapshot")
    if manifest["reset_checkpoint_schema"] != SCHEMA:
        raise ResetError("scenario manifest checkpoint schema does not match reset schema")
    return _validate_scenario_manifest_binding(_scenario_manifest_binding(manifest))


def _fixture_callback_expectation(value: Any) -> dict[str, Any]:
    expected = {
        "host", "domain", "user", "payload_type", "integrity",
        "operation_id", "payload_uuid", "active",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ResetError("fixture callback expectation has invalid fields")
    normalized = {
        key: str(value[key] or "").strip().casefold()
        for key in ("host", "domain", "user", "payload_type", "integrity", "payload_uuid")
    }
    if any(not item for item in normalized.values()):
        raise ResetError("fixture callback expectation fields must be non-empty")
    if normalized["integrity"] not in {"low", "medium", "high"}:
        raise ResetError("fixture callback expectation integrity is unsupported")
    operation_id = value.get("operation_id")
    if not _is_int(operation_id) or operation_id <= 0:
        raise ResetError("fixture callback expectation operation_id is invalid")
    if value.get("active") is not True:
        raise ResetError("fixture callback expectation active must be true")
    normalized["operation_id"] = operation_id
    normalized["active"] = True
    return normalized


def _fixture_callback_row(
    value: Any, *, require_active: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResetError("fixture callback observation must be an object")
    callback_id = value.get("display_id")
    if not _is_int(callback_id) or callback_id <= 0:
        raise ResetError("fixture callback observation has invalid display_id")
    observed_at = _aware_time(value.get("observed_at"))
    if observed_at is None:
        raise ResetError("fixture callback observation timestamp must be timezone-aware")
    fields = {
        key: str(value.get(key) or "").strip().casefold()
        for key in ("host", "domain", "user", "payload_type", "payload_uuid")
    }
    if any(not item for item in fields.values()):
        raise ResetError("fixture callback observation identity is incomplete")
    operation_id = value.get("operation_id")
    if not _is_int(operation_id) or operation_id <= 0:
        raise ResetError("fixture callback observation operation_id is invalid")
    active = value.get("active")
    if not isinstance(active, bool):
        raise ResetError("fixture callback observation active is invalid")
    if require_active and active is not True:
        raise ResetError("fixture callback observation must be active")
    level = value.get("integrity_level")
    if not _is_int(level) or level not in {1, 2, 3}:
        raise ResetError("fixture callback observation integrity_level is invalid")
    integrity = {1: "low", 2: "medium", 3: "high"}[level]
    return {
        "display_id": callback_id,
        **fields,
        "integrity": integrity,
        "integrity_level": level,
        "operation_id": operation_id,
        "active": active,
        "observed_at": observed_at.isoformat(),
    }


def _fixture_action_binding(
    value: Any, config: dict[str, Any], *, label: str,
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _FIXTURE_ACTION_BINDING_KEYS:
        raise ResetError(f"{label} has invalid fields")
    binding = {key: str(value[key] or "").strip() for key in value}
    if any(not item for item in binding.values()):
        raise ResetError(f"{label} values must be non-empty")
    for key in ("range_profile_id", "ludus_range_id", "ludus_mcp_server"):
        if binding[key] != config[key]:
            raise ResetError(f"{label} {key} does not match inputs")
    return binding


def _fixture_action_bindings(value: Any, config: dict[str, Any]) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != set(RANGE_FIXTURE_QUALIFICATION_PHASES):
        raise ResetError("fixture qualification action bindings must cover each phase")
    return {
        phase: _fixture_action_binding(item, config, label=f"action binding {phase}")
        for phase, item in value.items()
    }


def _fixture_phase_output(
    phase_name: str,
    output: Any,
    *,
    action_binding: dict[str, str],
    error: Callable[[str], None],
) -> dict[str, Any]:
    """Validate evidence-bearing range-fixture phase output.

    The v2 reset protocol deliberately permits generic successful phase maps.
    This narrow observation scope does not: its range effects must retain a
    checked transport result and an observer state, while inventory remains a
    non-releasing observation.
    """
    if not isinstance(output, dict):
        error(f"{phase_name} result must be an object")
    if phase_name not in {
        "capture-task-and-callback-baseline", "fresh-callback-selection",
    } and (
        output.get("mechanism") != action_binding["mechanism"]
        or output.get("adapter") != action_binding["adapter"]
    ):
        error(f"{phase_name} result mechanism does not match its action binding")
    if phase_name == "wait-range-ips":
        inventory = output.get("inventory")
        if (
            output.get("schema") != "sage-range-fixture-inventory-v1"
            or output.get("live_ready") is not False
            or not isinstance(output.get("observation_id"), str)
            or not output["observation_id"].strip()
            or not isinstance(inventory, dict)
            or inventory.get("qualification_observation") is not True
            or inventory.get("live_ready") is not False
            or inventory.get("ready") is not True
        ):
            error("wait-range-ips requires a ready non-releasing fixture inventory")
        return output
    if phase_name == "await-operator-foothold-launch":
        _validate_phase_output(phase_name, output, error=error)
        return output
    if phase_name in {
        "rollback-range", "poweron-range", "sync-range-time",
    }:
        _validate_phase_output(phase_name, output, error=error)
        if output.get("ok") is not True or output.get("exit_code") != 0:
            error(f"{phase_name} requires explicit ok and exit_code zero")
        observer_state = output.get("observer_state")
        if not isinstance(observer_state, (str, dict, list)) or not observer_state:
            error(f"{phase_name} requires a non-empty observer_state")
        return output
    if phase_name == "final-start-readiness":
        if (
            output.get("ok") is not True
            or output.get("endpoint_observed") is not True
            or not _is_int(output.get("callback_id"))
            or output["callback_id"] <= 0
        ):
            error("final-start-readiness requires a task-free endpoint observation")
        return output
    if phase_name == "capture-task-and-callback-baseline":
        if (
            output.get("ok") is not True
            or not _is_int(output.get("task_count"))
            or output["task_count"] < 0
            or not _is_int(output.get("callback_count"))
            or output["callback_count"] < 0
        ):
            error("capture baseline result is invalid")
        return output
    if phase_name == "fresh-callback-selection":
        if output.get("ok") is not True or not _is_int(output.get("callback_id")):
            error("fresh callback selection result is invalid")
        return output
    error(f"fixture qualification phase is unsupported: {phase_name}")
    raise AssertionError("error callback must raise")


def _validate_fixture_completed_result(
    phase_name: str, value: Any, path: Path, *, label: str,
    action_binding: dict[str, str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"phase", "succeeded", "output"}:
        _raise_invalid(path, f"{label} has an invalid success envelope")
    if value.get("phase") != phase_name or value.get("succeeded") is not True:
        _raise_invalid(path, f"{label} does not match its completed phase")
    return _fixture_phase_output(
        phase_name,
        value.get("output"),
        action_binding=action_binding,
        error=lambda message: _raise_invalid(path, f"{label}: {message}"),
    )


def _fixture_manifest_and_inputs(manifest: Any, config: dict[str, Any]) -> dict[str, Any]:
    profile_module = _load_module("sage_reset_range_fixture_profile", RANGE_PROFILE)
    report = profile_module.validate_scenario_manifest(
        manifest, qualification_observation=True,
    )
    if not report["manifest_valid"]:
        raise ResetError("scenario manifest is invalid: " + ", ".join(report["errors"]))
    if manifest.get("reset_checkpoint_schema") != RANGE_FIXTURE_QUALIFICATION_SCHEMA:
        raise ResetError("fixture qualification manifest checkpoint schema does not match")
    if manifest.get("snapshot_id") == profile_module.UNALLOCATED_SNAPSHOT:
        raise ResetError("fixture qualification requires an allocated snapshot")
    if config.get("range_profile_id") != manifest["profile_id"]:
        raise ResetError("fixture qualification profile does not match scenario manifest")
    if config.get("snapshot") != manifest["snapshot_id"]:
        raise ResetError("fixture qualification snapshot does not match scenario manifest")
    for key, label in (("ludus_range_id", "range ID"), ("ludus_mcp_server", "Ludus MCP server")):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ResetError(f"fixture qualification {label} must be non-empty")
    return report


def _new_range_fixture_checkpoint(
    config: dict[str, Any], run_id: str, *, manifest: dict[str, Any],
    callback_expectation: dict[str, Any], action_bindings: dict[str, dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema": RANGE_FIXTURE_QUALIFICATION_SCHEMA,
        "scope": "range-fixture-qualification",
        "run_id": run_id,
        "created_at": _now(),
        "updated_at": _now(),
        "inputs": _redact(config),
        "scenario_manifest_binding": _scenario_manifest_binding(manifest),
        "callback_expectation": callback_expectation,
        "task_baseline": None,
        "callback_task_observation": None,
        "callback_baseline": None,
        "fresh_callback": None,
        "phases": [
            {"name": name, "status": "pending", "action_binding": action_bindings[name],
             "started_at": None, "finished_at": None, "result_summary": None, "error": None}
            for name in RANGE_FIXTURE_QUALIFICATION_PHASES
        ],
        "terminal": {"state": "in_progress", "reason": None},
    }


def _validate_range_fixture_checkpoint(checkpoint: Any, path: Path) -> dict[str, Any]:
    keys = {"schema", "scope", "run_id", "created_at", "updated_at", "inputs", "scenario_manifest_binding",
            "callback_expectation", "task_baseline", "callback_task_observation", "callback_baseline", "fresh_callback", "phases", "terminal"}
    if not isinstance(checkpoint, dict) or set(checkpoint) != keys or checkpoint.get("schema") != RANGE_FIXTURE_QUALIFICATION_SCHEMA or checkpoint.get("scope") != "range-fixture-qualification":
        _raise_invalid(path, "fixture qualification checkpoint schema is invalid")
    if not isinstance(checkpoint.get("run_id"), str) or not checkpoint["run_id"].strip():
        _raise_invalid(path, "fixture qualification run_id is invalid")
    for key in ("created_at", "updated_at"):
        if _aware_time(checkpoint.get(key)) is None:
            _raise_invalid(path, f"fixture qualification {key} is invalid")
    _validate_inputs(checkpoint["inputs"], path, require_live_qualified=False)
    try:
        _validate_scenario_manifest_binding(checkpoint["scenario_manifest_binding"])
        _fixture_callback_expectation(checkpoint["callback_expectation"])
    except ResetError as exc:
        _raise_invalid(path, str(exc))
    phases = checkpoint.get("phases")
    if not isinstance(phases, list) or [item.get("name") if isinstance(item, dict) else None for item in phases] != list(RANGE_FIXTURE_QUALIFICATION_PHASES):
        _raise_invalid(path, "fixture qualification phases are invalid")
    first_incomplete = None
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict) or set(phase) != {"name", "status", "action_binding", "started_at", "finished_at", "result_summary", "error"}:
            _raise_invalid(path, f"fixture qualification phase {index} is invalid")
        status = phase["status"]
        if status not in PHASE_STATUSES:
            _raise_invalid(path, f"fixture qualification phase {index} has invalid status")
        try:
            action_binding = _fixture_action_binding(
                phase["action_binding"], checkpoint["inputs"],
                label=f"phases[{index}].action_binding",
            )
        except ResetError as exc:
            _raise_invalid(path, str(exc))
        if status == "completed":
            if _aware_time(phase["started_at"]) is None or _aware_time(phase["finished_at"]) is None or phase["error"] is not None:
                _raise_invalid(path, f"fixture qualification phase {index} completion is invalid")
            _validate_fixture_completed_result(
                phase["name"], phase["result_summary"], path,
                label=f"phases[{index}].result_summary",
                action_binding=action_binding,
            )
        elif status == "pending":
            if any(phase[key] is not None for key in ("started_at", "finished_at", "result_summary", "error")):
                _raise_invalid(path, f"fixture qualification pending phase {index} carries result data")
        elif status == "running":
            if _aware_time(phase["started_at"]) is None or any(phase[key] is not None for key in ("finished_at", "result_summary", "error")):
                _raise_invalid(path, f"fixture qualification running phase {index} is invalid")
        elif status == "failed":
            if _aware_time(phase["started_at"]) is None or _aware_time(phase["finished_at"]) is None or not isinstance(phase["error"], str) or not phase["error"]:
                _raise_invalid(path, f"fixture qualification failed phase {index} is invalid")
        else:
            _raise_invalid(path, "fixture qualification only permits pending, completed, or failed phases")
        if status != "completed" and first_incomplete is None:
            first_incomplete = index
        elif status == "completed" and first_incomplete is not None:
            _raise_invalid(path, "fixture qualification completed phases must form a prefix")
        elif first_incomplete is not None and status not in {"pending", "completed"}:
            _raise_invalid(path, "fixture qualification has multiple active phases")
    baseline = checkpoint["task_baseline"]
    if baseline is not None:
        _validate_observation(baseline, path, label="fixture task baseline", include_phase=False)
    callback_tasks = checkpoint["callback_task_observation"]
    if callback_tasks is not None:
        _validate_observation(
            callback_tasks,
            path,
            label="fixture callback task observation",
            include_phase=False,
        )
        if baseline is None:
            _raise_invalid(path, "fixture callback task observation lacks baseline")
        drifted = False
        for key in (
            "scope", "operation_id", "operator_id", "operator_username",
            "count", "max_task_id",
        ):
            if callback_tasks[key] != baseline[key]:
                drifted = True
        if _aware_time(callback_tasks["observed_at"]) < _aware_time(baseline["observed_at"]):
            drifted = True
        fresh_phase = phases[RANGE_FIXTURE_QUALIFICATION_PHASES.index("fresh-callback-selection")]
        if drifted and fresh_phase["status"] != "failed":
            _raise_invalid(path, "fixture callback task observation drifted")
        if _aware_time(callback_tasks["observed_at"]) < _aware_time(baseline["observed_at"]) and fresh_phase["status"] != "failed":
            _raise_invalid(path, "fixture callback task observation reverses time")
    callback_baseline = checkpoint["callback_baseline"]
    if callback_baseline is not None:
        if not isinstance(callback_baseline, dict) or set(callback_baseline) != {"observed_at", "callback_ids"} or _aware_time(callback_baseline.get("observed_at")) is None or not isinstance(callback_baseline.get("callback_ids"), list) or any(not _is_int(value) or value <= 0 for value in callback_baseline["callback_ids"]) or len(set(callback_baseline["callback_ids"])) != len(callback_baseline["callback_ids"]):
            _raise_invalid(path, "fixture callback baseline is invalid")
    fresh = checkpoint["fresh_callback"]
    if fresh is not None:
        try:
            fresh_row = _fixture_callback_row(fresh)
            expectation = _fixture_callback_expectation(checkpoint["callback_expectation"])
        except ResetError as exc:
            _raise_invalid(path, str(exc))
        if callback_baseline is None or callback_tasks is None:
            _raise_invalid(path, "fixture fresh callback lacks prerequisite observations")
        if fresh_row["display_id"] in callback_baseline["callback_ids"]:
            _raise_invalid(path, "fixture fresh callback was present at baseline")
        if _aware_time(fresh_row["observed_at"]) <= _aware_time(callback_baseline["observed_at"]):
            _raise_invalid(path, "fixture fresh callback predates baseline boundary")
        if any(fresh_row[key] != expectation[key] for key in expectation):
            _raise_invalid(path, "fixture fresh callback does not match expectation")
        if fresh_row["operation_id"] != baseline["operation_id"]:
            _raise_invalid(path, "fixture fresh callback does not match task operation")
    terminal = checkpoint["terminal"]
    if not isinstance(terminal, dict) or set(terminal) != {"state", "reason"} or terminal.get("state") not in {"in_progress", "blocked", "complete"}:
        _raise_invalid(path, "fixture qualification terminal is invalid")
    if terminal["state"] == "complete" and (first_incomplete is not None or baseline is None or callback_tasks is None or callback_baseline is None or fresh is None):
        _raise_invalid(path, "fixture qualification complete checkpoint lacks required observations")
    if terminal["state"] == "complete":
        results = {phase["name"]: phase["result_summary"]["output"] for phase in phases}
        inventory_profile = results["wait-range-ips"]["inventory"].get("range_profile")
        if (
            not isinstance(inventory_profile, dict)
            or inventory_profile.get("id") != checkpoint["inputs"]["range_profile_id"]
        ):
            _raise_invalid(path, "fixture inventory profile does not match checkpoint inputs")
        if results["capture-task-and-callback-baseline"]["task_count"] != baseline["count"] or results["capture-task-and-callback-baseline"]["callback_count"] != len(callback_baseline["callback_ids"]):
            _raise_invalid(path, "fixture capture result does not match baseline observations")
        if results["fresh-callback-selection"]["callback_id"] != fresh["display_id"]:
            _raise_invalid(path, "fixture callback selection result does not match callback")
        if results["final-start-readiness"]["callback_id"] != fresh["display_id"]:
            _raise_invalid(path, "fixture final endpoint does not match callback")
    return checkpoint


def run_range_fixture_qualification(
    config: dict[str, Any], *, manifest: Any, run_id: str, checkpoint_path: Path,
    callback_expectation: Any, action_bindings: Any,
    phase_runner: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]],
    task_observer: Callable[[dict[str, Any]], dict[str, Any]], callback_observer: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Run only range-local fixture reset phases with injected operator actions.

    This producer records an observation-only checkpoint and never grants readiness.
    """
    if checkpoint_path.exists():
        raise ResetError(f"checkpoint already exists; refuse to overwrite: {checkpoint_path}")
    if not isinstance(manifest, dict):
        raise ResetError("fixture qualification manifest must be an object")
    _fixture_manifest_and_inputs(manifest, config)
    expectation = _fixture_callback_expectation(callback_expectation)
    bindings = _fixture_action_bindings(action_bindings, config)
    checkpoint = _new_range_fixture_checkpoint(
        config, run_id, manifest=manifest,
        callback_expectation=expectation, action_bindings=bindings,
    )

    def write() -> None:
        checkpoint["updated_at"] = _now()
        _validate_range_fixture_checkpoint(checkpoint, checkpoint_path)
        _atomic_write(checkpoint_path, checkpoint)

    write()
    for phase in checkpoint["phases"]:
        name = phase["name"]
        phase["status"] = "running"
        phase["started_at"] = _now()
        write()
        try:
            if name == "capture-task-and-callback-baseline":
                baseline = dict(task_observer(config))
                _validate_observation(baseline, checkpoint_path, label="fixture task baseline", include_phase=False)
                callbacks = [
                    _fixture_callback_row(item, require_active=False)
                    for item in callback_observer(config)
                ]
                ids = [item["display_id"] for item in callbacks]
                if len(ids) != len(set(ids)):
                    raise ResetError("fixture callback baseline has duplicate IDs")
                checkpoint["task_baseline"] = baseline
                checkpoint["callback_baseline"] = {"observed_at": _now(), "callback_ids": ids}
                output = {"ok": True, "task_count": baseline["count"], "callback_count": len(ids)}
            elif name == "fresh-callback-selection":
                baseline = checkpoint["task_baseline"]
                if not isinstance(baseline, dict):
                    raise ResetError("fixture task baseline is missing")
                now_tasks = dict(task_observer(config))
                _validate_observation(now_tasks, checkpoint_path, label="fixture callback task observation", include_phase=False)
                checkpoint["callback_task_observation"] = now_tasks
                for key in ("scope", "operation_id", "operator_id", "operator_username", "count", "max_task_id"):
                    if now_tasks[key] != baseline[key]:
                        raise ResetError("fixture task baseline changed before callback selection")
                if expectation["operation_id"] != baseline["operation_id"]:
                    raise ResetError("fixture callback expectation does not match task operation")
                callback_baseline = checkpoint["callback_baseline"]
                if not isinstance(callback_baseline, dict):
                    raise ResetError("fixture callback baseline is missing")
                floor = _aware_time(callback_baseline["observed_at"])
                candidates = []
                for item in callback_observer(config):
                    # Baseline rows can be inactive historical callbacks.  A
                    # candidate still must match the active=true expectation.
                    row = _fixture_callback_row(item, require_active=False)
                    if row["display_id"] in callback_baseline["callback_ids"] or _aware_time(row["observed_at"]) <= floor:
                        continue
                    if all(row[key] == expectation[key] for key in expectation):
                        candidates.append(row)
                if len(candidates) != 1:
                    raise ResetError("fixture callback selection requires exactly one fresh matching callback")
                checkpoint["fresh_callback"] = candidates[0]
                output = {"ok": True, "callback_id": candidates[0]["display_id"]}
            elif name == "final-start-readiness":
                fresh = checkpoint["fresh_callback"]
                if not isinstance(fresh, dict):
                    raise ResetError("fixture final endpoint lacks fresh callback")
                output = _fixture_phase_output(
                    name,
                    phase_runner(name, phase["action_binding"], config),
                    action_binding=phase["action_binding"],
                    error=_raise_reset,
                )
                if output["callback_id"] != fresh["display_id"]:
                    raise ResetError("fixture final endpoint does not match fresh callback")
            else:
                output = _fixture_phase_output(
                    name,
                    phase_runner(name, phase["action_binding"], config),
                    action_binding=phase["action_binding"],
                    error=_raise_reset,
                )
            phase["result_summary"] = _completed_result(name, output)
            phase["status"] = "completed"
            phase["finished_at"] = _now()
            phase["error"] = None
            if name == RANGE_FIXTURE_QUALIFICATION_PHASES[-1]:
                checkpoint["terminal"] = {"state": "complete", "reason": None}
            write()
        except Exception as exc:
            reason = _failure_reason(exc)
            phase["status"] = "failed"
            phase["finished_at"] = _now()
            phase["error"] = reason
            checkpoint["terminal"] = {"state": "blocked", "reason": reason}
            write()
            raise ResetError(reason) from exc
    return {"checkpoint": checkpoint, "live_ready": False, "observation_only": True}


def validate_range_fixture_qualification(
    manifest: Any, checkpoint: Any, path: Path, *, expected_run_id: str, not_before: str,
    expected_range_id: str, expected_ludus_mcp_server: str,
) -> dict[str, Any]:
    if not isinstance(expected_run_id, str) or not expected_run_id.strip() or _aware_time(not_before) is None:
        raise ResetError("fixture qualification run ID and not-before timestamp are required")
    for value, label in ((expected_range_id, "range ID"), (expected_ludus_mcp_server, "Ludus MCP server")):
        if not isinstance(value, str) or not value.strip():
            raise ResetError(f"fixture qualification expected {label} must be non-empty")
    if not isinstance(manifest, dict):
        raise ResetError("fixture qualification manifest must be an object")
    if not isinstance(checkpoint, dict):
        raise ResetError("fixture qualification checkpoint must be an object")
    parsed = _validate_range_fixture_checkpoint(checkpoint, path)
    _fixture_manifest_and_inputs(manifest, parsed["inputs"])
    if parsed["run_id"] != expected_run_id or _aware_time(parsed["created_at"]) < _aware_time(not_before):
        raise ResetError("fixture qualification run binding does not match")
    if parsed["inputs"]["ludus_range_id"] != expected_range_id.strip() or parsed["inputs"]["ludus_mcp_server"] != expected_ludus_mcp_server.strip():
        raise ResetError("fixture qualification selector does not match")
    if parsed["scenario_manifest_binding"] != _scenario_manifest_binding(manifest):
        raise ResetError("fixture qualification manifest binding does not match")
    if parsed["terminal"]["state"] != "complete":
        raise ResetError("fixture qualification checkpoint is incomplete")
    return {"checkpoint": parsed, "live_ready": False, "observation_only": True}


def _load_checkpoint(path: Path) -> dict[str, Any]:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResetError(f"unable to load checkpoint {path}: {exc}") from exc
    return _validate_checkpoint(checkpoint, path)


async def _observe_mythic_tasks_async(
    config: dict[str, Any]
) -> dict[str, Any]:
    bootstrap = _load_module(
        "sage_reset_bootstrap_for_task_count", BOOTSTRAP
    )
    namespace = argparse.Namespace(
        server=config["mythic_server"],
        user=config["mythic_user"],
        password=None,
        env_path=config["mythic_env_path"],
    )
    client = await bootstrap.login(namespace)
    operation_id = getattr(client, "current_operation_id", None)
    if not _is_int(operation_id) or operation_id <= 0:
        raise ResetError(
            "Mythic login has no positive current operation id"
        )
    result = await bootstrap.mythic.execute_custom_query(
        client,
        TASK_COUNT_QUERY,
        variables={"operationId": operation_id},
    )
    whoami = result.get("whoami") or {}
    if str(whoami.get("status") or "").casefold() != "success":
        raise ResetError("Mythic whoami did not return success")
    observed_operation_id = whoami.get("current_operation_id")
    operator_id = whoami.get("user_id")
    operator_username = str(whoami.get("username") or "").strip()
    if (
        not _is_int(observed_operation_id)
        or observed_operation_id != operation_id
    ):
        raise ResetError(
            "Mythic operation changed during task observation"
        )
    if (
        not _is_int(operator_id)
        or operator_id <= 0
        or not operator_username
    ):
        raise ResetError(
            "Mythic whoami returned incomplete operator identity"
        )
    if operator_username != str(config["mythic_user"]).strip():
        raise ResetError(
            "Mythic whoami operator does not match configured user"
        )
    aggregate = (
        (result.get("task_aggregate") or {}).get("aggregate") or {}
    )
    rows = result.get("task") or []
    count = aggregate.get("count")
    if not _is_int(count) or count < 0:
        raise ResetError("Mythic task aggregate returned an invalid count")
    max_task_id = rows[0].get("id") if rows else None
    if max_task_id is not None and (
        not _is_int(max_task_id) or max_task_id < 0
    ):
        raise ResetError("Mythic task query returned an invalid task id")
    return {
        "scope": "mythic-operation",
        "operation_id": operation_id,
        "operator_id": operator_id,
        "operator_username": operator_username,
        "count": count,
        "max_task_id": max_task_id,
        "observed_at": _now(),
    }


def observe_mythic_tasks(config: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(_observe_mythic_tasks_async(config))


def _wait_for_range(
    config: dict[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    contract = _load_module(
        "sage_reset_readiness_contract", READINESS_CONTRACT
    )
    probe = lambda: contract.range_boot_observation(
        config["ludus_range_id"] or None,
        config["ludus_mcp_server"] or None,
        config["range_profile_id"],
    )
    decision = contract.poll_readiness(
        probe,
        lane="range_boot",
        outer_timeout_seconds=config["range_ready_timeout"],
        child_timeout_seconds=contract.LUDUS_PROBE_TIMEOUT_SECONDS,
        grace_seconds=contract.DEFAULT_TIMEOUT_GRACE_SECONDS,
        poll_seconds=config["range_poll_interval"],
        sleep=sleep,
    )
    if decision.get("ready") is not True:
        raise ResetError(
            f"range did not reach {config['range_profile_id']} readiness; blockers="
            + json.dumps(decision.get("blockers") or [])
        )
    return decision


def run_phase_command(
    phase_name: str,
    command: list[str],
    config: dict[str, Any],
) -> dict[str, Any]:
    if phase_name == "wait-range-ips":
        return _wait_for_range(config)
    env = dict(os.environ)
    env["SAGE_RANGE_PROFILE_ID"] = config["range_profile_id"]
    if config["ludus_range_id"]:
        env["SAGE_LUDUS_RANGE_ID"] = config["ludus_range_id"]
    if config["ludus_mcp_server"]:
        env["SAGE_LUDUS_MCP_SERVER"] = config["ludus_mcp_server"]
    result = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    if result.returncode:
        raise ResetError(
            f"{phase_name} exited {result.returncode}: "
            f"{result.stderr.strip()[-2000:]}"
        )
    stdout = result.stdout.strip()
    if not stdout:
        return {"exit_code": 0}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        parsed = {"stdout_tail": stdout[-4000:]}
    return _redact(parsed)


class ResetOrchestrator:
    def __init__(
        self,
        checkpoint_path: Path,
        *,
        phase_runner: Callable[
            [str, list[str], dict[str, Any]], dict[str, Any]
        ] = run_phase_command,
        task_observer: Callable[
            [dict[str, Any]], dict[str, Any]
        ] = observe_mythic_tasks,
    ):
        self.path = checkpoint_path
        self.phase_runner = phase_runner
        self.task_observer = task_observer

    def _write(self, checkpoint: dict[str, Any]) -> None:
        checkpoint["updated_at"] = _now()
        _validate_checkpoint(checkpoint, self.path)
        _atomic_write(self.path, checkpoint)

    def _record_task_observation(
        self, checkpoint: dict[str, Any], phase_name: str
    ) -> None:
        observation = dict(self.task_observer(checkpoint["inputs"]))
        _validate_observation(
            observation,
            self.path,
            label=f"{phase_name} task observation",
            include_phase=False,
        )
        if phase_name == "reset-mythic":
            if observation["count"] != 0:
                raise ResetError(
                    "Mythic reset did not establish a zero-task baseline"
                )
            checkpoint["task_baseline"] = dict(observation)
            observation["phase"] = phase_name
            observation["delta_from_baseline"] = 0
        else:
            baseline = checkpoint.get("task_baseline")
            if not isinstance(baseline, dict):
                raise ResetError("task baseline is missing")
            for field in (
                "scope",
                "operation_id",
                "operator_id",
                "operator_username",
            ):
                if observation[field] != baseline[field]:
                    raise ResetError(
                        f"Mythic task observation {field} changed after reset"
                    )
            previous = (
                checkpoint.get("task_observations") or [baseline]
            )[-1]
            if _aware_time(observation["observed_at"]) < _aware_time(
                previous["observed_at"]
            ):
                raise ResetError(
                    "Mythic task observation time reversed after reset"
                )
            delta = observation["count"] - baseline["count"]
            if delta != 0:
                raise ResetError(
                    f"Mythic payload task delta became {delta} after "
                    f"{phase_name}"
                )
            if observation["max_task_id"] != baseline["max_task_id"]:
                raise ResetError(
                    "Mythic latest task id changed after reset"
                )
            observation["phase"] = phase_name
            observation["delta_from_baseline"] = delta
        checkpoint["task_observations"].append(observation)

    def start(
        self,
        config: dict[str, Any],
        *,
        run_id: str | None = None,
        scenario_manifest_binding: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if self.path.exists():
            raise ResetError(
                f"checkpoint already exists; use resume: {self.path}"
            )
        checkpoint = _new_checkpoint(
            config,
            run_id
            or f"reset-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}",
            scenario_manifest_binding=scenario_manifest_binding,
        )
        self._write(checkpoint)
        return self._advance(checkpoint)

    def resume(
        self, *, operator_foothold_launched: bool
    ) -> tuple[int, dict[str, Any]]:
        checkpoint = _load_checkpoint(self.path)
        running = next(
            (
                phase
                for phase in checkpoint["phases"]
                if phase.get("status") == "running"
            ),
            None,
        )
        if running is not None:
            if running["name"] in AMBIGUOUS_ON_INTERRUPT:
                running["status"] = "failed"
                running["finished_at"] = _now()
                running["error"] = "interrupted_ambiguous_completion"
                checkpoint["terminal"] = {
                    "state": "blocked",
                    "reason": (
                        f"{running['name']} was interrupted with ambiguous "
                        "completion; inspect before starting a new reset"
                    ),
                }
                self._write(checkpoint)
                return 1, checkpoint
            running["status"] = "pending"
            running["started_at"] = None
            running["finished_at"] = None
            running["result_summary"] = None
            running["error"] = None
        failed_ambiguous = next(
            (
                phase
                for phase in checkpoint["phases"]
                if phase.get("status") == "failed"
                and phase.get("name") in NON_RETRYABLE_FAILED_PHASES
            ),
            None,
        )
        if failed_ambiguous is not None:
            checkpoint["terminal"] = {
                "state": "blocked",
                "reason": (
                    f"{failed_ambiguous['name']} has ambiguous failed state; "
                    "inspect it and begin a new reset checkpoint"
                ),
            }
            self._write(checkpoint)
            return 1, checkpoint
        failed_retryable = next(
            (
                phase
                for phase in checkpoint["phases"]
                if phase.get("status") == "failed"
            ),
            None,
        )
        if failed_retryable is not None:
            if (
                checkpoint["task_observations"]
                and checkpoint["task_observations"][-1].get("phase")
                == failed_retryable["name"]
            ):
                checkpoint["task_observations"].pop()
            failed_retryable["status"] = "pending"
            failed_retryable["started_at"] = None
            failed_retryable["finished_at"] = None
            failed_retryable["result_summary"] = None
            failed_retryable["error"] = None
            checkpoint["terminal"] = {
                "state": "in_progress",
                "reason": None,
            }
            self._write(checkpoint)
        awaiting = next(
            (
                phase
                for phase in checkpoint["phases"]
                if phase.get("status") == "awaiting_operator"
            ),
            None,
        )
        if awaiting is not None:
            if not operator_foothold_launched:
                raise ResetError(
                    "resume requires --operator-foothold-launched at the "
                    "interactive boundary"
                )
            try:
                self._record_task_observation(
                    checkpoint, "operator-foothold-launch"
                )
            except ResetError as exc:
                awaiting["status"] = "failed"
                awaiting["finished_at"] = _now()
                awaiting["error"] = str(exc)
                checkpoint["terminal"] = {
                    "state": "blocked",
                    "reason": str(exc),
                }
                self._write(checkpoint)
                return 1, checkpoint
            awaiting["status"] = "completed"
            awaiting["finished_at"] = _now()
            awaiting["result_summary"] = _completed_result(
                awaiting["name"],
                {
                    "operator_acknowledged": True,
                    "payload_tasks_issued": 0,
                },
            )
            checkpoint["interactive_boundary"]["status"] = "completed"
            checkpoint["terminal"] = {
                "state": "in_progress",
                "reason": None,
            }
            self._write(checkpoint)
        return self._advance(checkpoint)

    def _advance(
        self, checkpoint: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        commands = _phase_commands(checkpoint["inputs"])
        for phase in checkpoint["phases"]:
            if phase["status"] == "completed":
                continue
            name = phase["name"]
            if name == "await-operator-foothold-launch":
                phase["status"] = "awaiting_operator"
                phase["started_at"] = _now()
                checkpoint["interactive_boundary"]["status"] = (
                    "awaiting_operator"
                )
                checkpoint["terminal"] = {
                    "state": "awaiting_operator",
                    "reason": (
                        "launch the foothold payload through the documented "
                        "interactive operator workflow, then resume"
                    ),
                }
                self._write(checkpoint)
                return 0, checkpoint
            phase["status"] = "running"
            phase["started_at"] = _now()
            phase["error"] = None
            self._write(checkpoint)
            try:
                result = self.phase_runner(
                    name, commands[name], checkpoint["inputs"]
                )
                result = _validate_phase_output(
                    name,
                    result,
                    error=_raise_reset,
                )
                if name in TASK_OBSERVATION_PHASES:
                    self._record_task_observation(checkpoint, name)
                phase["result_summary"] = _completed_result(name, result)
            except Exception as exc:
                phase["status"] = "failed"
                phase["finished_at"] = _now()
                phase["error"] = str(exc)
                checkpoint["terminal"] = {
                    "state": "blocked",
                    "reason": str(exc),
                }
                self._write(checkpoint)
                return 1, checkpoint
            phase["status"] = "completed"
            phase["finished_at"] = _now()
            if name == PHASE_NAMES[-1]:
                checkpoint["terminal"] = {
                    "state": "complete",
                    "reason": None,
                }
            self._write(checkpoint)
        checkpoint["terminal"] = {"state": "complete", "reason": None}
        self._write(checkpoint)
        return 0, checkpoint


def _add_start_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--range-profile", default="goad-trust-walker")
    parser.add_argument("--scenario-manifest")
    parser.add_argument("--external-source-dir")
    parser.add_argument("--ludus-range-id", default=os.environ.get("SAGE_LUDUS_RANGE_ID") or "")
    parser.add_argument("--ludus-mcp-server", default=os.environ.get("SAGE_LUDUS_MCP_SERVER") or "")
    parser.add_argument(
        "--bootstrap-mode",
        choices=("fresh-apollo", "retained-callback"),
        default="fresh-apollo",
    )
    parser.add_argument("--retained-callback-config")
    parser.add_argument("--foothold-payload-type", default="apollo")
    parser.add_argument("--foothold-host", default="CASTELBLACK")
    parser.add_argument("--foothold-user-match", default="samwell.tarly")
    parser.add_argument("--callback-host")
    parser.add_argument("--download-dir", default="/tmp/sage_payloads")
    parser.add_argument(
        "--prepare-chat",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--restart-env",
        action="append",
        default=[
            "SAGE_ENGAGEMENT_GATE=1",
            f"SAGE_BLOODHOUND_MCP_DIR={REPO_ROOT.parent / 'bloodhound_mcp'}",
        ],
    )
    parser.add_argument("--range-ready-timeout", type=int, default=600)
    parser.add_argument("--range-poll-interval", type=float, default=10.0)
    parser.add_argument("--mythic-server", default="127.0.0.1")
    parser.add_argument("--mythic-user", default="mythic_admin")
    parser.add_argument(
        "--mythic-env-path",
        # Honor MYTHIC_ENV_PATH so one variable configures every tool at once. No directory-name
        # default: guessing a checkout name would bake one machine's layout into the repo.
        default=os.environ.get("MYTHIC_ENV_PATH") or "",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    _add_start_args(plan)

    start = subparsers.add_parser("start")
    _add_start_args(start)
    start.add_argument("--yes", action="store_true")
    start.add_argument("--checkpoint")

    resume = subparsers.add_parser("resume")
    resume.add_argument("--checkpoint", required=True)
    resume.add_argument(
        "--operator-foothold-launched", action="store_true"
    )
    resume.add_argument("--yes", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--checkpoint", required=True)

    validate_manifest = subparsers.add_parser("validate-scenario-manifest")
    validate_manifest.add_argument("--manifest", required=True)
    validate_manifest.add_argument("--checkpoint", required=True)
    validate_manifest.add_argument("--expected-run-id", required=True)
    validate_manifest.add_argument("--not-before", required=True)
    validate_manifest.add_argument("--expected-range-id")
    validate_manifest.add_argument("--expected-ludus-mcp-server")
    validate_manifest.add_argument("--external-source-dir")

    validate_fixture = subparsers.add_parser("validate-range-fixture-qualification")
    validate_fixture.add_argument("--manifest", required=True)
    validate_fixture.add_argument("--checkpoint", required=True)
    validate_fixture.add_argument("--expected-run-id", required=True)
    validate_fixture.add_argument("--not-before", required=True)
    validate_fixture.add_argument("--expected-range-id", required=True)
    validate_fixture.add_argument("--expected-ludus-mcp-server", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            path = Path(args.checkpoint).expanduser()
            checkpoint = _load_checkpoint(path)
            print(
                json.dumps(
                    _checkpoint_summary(checkpoint, path),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "validate-scenario-manifest":
            manifest_path = Path(args.manifest).expanduser()
            checkpoint_path = Path(args.checkpoint).expanduser()
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ResetError(f"unable to load offline validation input: {exc}") from exc
            validated = validate_scenario_reset_binding(
                manifest,
                checkpoint,
                checkpoint_path,
                expected_run_id=args.expected_run_id,
                not_before=args.not_before,
                expected_range_id=args.expected_range_id,
                expected_ludus_mcp_server=args.expected_ludus_mcp_server,
                external_source_dir=args.external_source_dir,
            )
            print(json.dumps({
                "ok": True,
                "run_id": validated["run_id"],
                "live_activity_performed": False,
            }, indent=2, sort_keys=True))
            return 0
        if args.command == "validate-range-fixture-qualification":
            manifest_path = Path(args.manifest).expanduser()
            checkpoint_path = Path(args.checkpoint).expanduser()
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ResetError(f"unable to load offline validation input: {exc}") from exc
            validated = validate_range_fixture_qualification(
                manifest, checkpoint, checkpoint_path,
                expected_run_id=args.expected_run_id,
                not_before=args.not_before,
                expected_range_id=args.expected_range_id,
                expected_ludus_mcp_server=args.expected_ludus_mcp_server,
            )
            print(json.dumps({
                "ok": True,
                "run_id": validated["checkpoint"]["run_id"],
                "live_ready": False,
                "observation_only": True,
                "live_activity_performed": False,
            }, indent=2, sort_keys=True))
            return 0
        if args.command == "plan":
            config = _config_from_args(args)
            scenario_manifest_binding = _scenario_manifest_capture(args, config)
            plan = {
                "schema": SCHEMA,
                "inputs": _redact(config),
                "phases": [
                    {
                        "name": name,
                        "command": _redact(command),
                    }
                    for name, command in _phase_commands(config).items()
                ],
                "live_activity_performed": False,
            }
            if scenario_manifest_binding is not None:
                plan["scenario_manifest_binding"] = scenario_manifest_binding
            print(
                json.dumps(
                    plan,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if not args.yes:
            raise ResetError(
                f"{args.command} changes live lab state; pass --yes"
            )
        if args.command == "start":
            config = _config_from_args(args)
            scenario_manifest_binding = _scenario_manifest_capture(args, config)
            profile_module = _load_module("sage_reset_range_profile_start", RANGE_PROFILE)
            try:
                profile_module.require_live_qualified(
                    profile_module.get_profile(config["range_profile_id"])
                )
            except ValueError as exc:
                raise ResetError(str(exc)) from exc
            run_id = (
                f"reset-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
            )
            path = (
                Path(args.checkpoint).expanduser()
                if args.checkpoint
                else DEFAULT_CHECKPOINT_DIR / f"{run_id}.json"
            )
            code, checkpoint = ResetOrchestrator(path).start(
                config, run_id=run_id, scenario_manifest_binding=scenario_manifest_binding
            )
        else:
            path = Path(args.checkpoint).expanduser()
            code, checkpoint = ResetOrchestrator(path).resume(
                operator_foothold_launched=(
                    args.operator_foothold_launched
                )
            )
        print(
            json.dumps(
                _checkpoint_summary(checkpoint, path),
                indent=2,
                sort_keys=True,
            )
        )
        return code
    except ResetError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "reset_orchestration_error",
                    "error": str(exc),
                },
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
