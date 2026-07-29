#!/usr/bin/env python3
"""Run a contract-validated Sage task on the pinned Codex cyber model."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


MODEL = "gpt-5.5-cyber-preview"
PROFILE_NAME = "sage_cyber_executor"
NETWORK_ROLES = {
    "mythic",
    "bloodhound",
    "ludus",
    "model-provider",
    "repository-service",
    "other-control-plane",
}
REQUIRED_TEXT_FIELDS = ("objective", "task", "authorization_source")
REQUIRED_LIST_FIELDS = (
    "permitted_file_scope",
    "protected_surfaces",
    "verification_plan",
    "stop_loss",
)
REQUIRED_BOOLEAN_FIELDS = (
    "workspace_write_authorized",
    "network_activity_authorized",
    "live_activity_authorized",
)
ALLOWED_CONTRACT_FIELDS = set(
    REQUIRED_TEXT_FIELDS
    + REQUIRED_LIST_FIELDS
    + REQUIRED_BOOLEAN_FIELDS
    + (
        "authorization_mode",
        "goal_scope",
        "network_endpoints",
        "live_run_contract",
    )
)
LIVE_TEXT_FIELDS = (
    "phase_or_tranche",
    "attempt_id",
    "range",
    "snapshot",
    "callback_binding",
    "provider",
    "route",
    "evidence_schema",
)
LIVE_LIST_FIELDS = ("allowed_capabilities", "countability_gates")
LIVE_OBJECT_FIELDS = ("startup_overrides", "artifact_hashes")
ALLOWED_LIVE_FIELDS = set(
    LIVE_TEXT_FIELDS + LIVE_LIST_FIELDS + LIVE_OBJECT_FIELDS + ("retry_cap",)
)
ARTIFACT_RETENTION_PATH = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "sage-artifact-retention"
    / "scripts"
    / "artifact_retention.py"
)


class ContractError(ValueError):
    """Raised when a supervisor contract fails closed validation."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _retention_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "sage_artifact_retention_for_cyber_runner",
        ARTIFACT_RETENTION_PATH,
    )
    if spec is None or spec.loader is None:
        raise ContractError("cannot load Sage artifact-retention helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def persist_runner_artifacts(
    *,
    command: str,
    result: dict[str, Any],
    contract: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist the validated contract and structured handoff, never the worker stream."""

    retention = _retention_module()
    root = repository_root()
    try:
        if contract is not None:
            contract_sha256 = canonical_contract_sha256(contract)
            contract_path, contract_record = retention.write_json_artifact(
                "contracts/cyber-runner",
                f"{command}-{contract_sha256[:16]}.json",
                contract,
                artifact_type="cyber-runner-contract",
                context=f"sage-cyber-runner {command}",
                root=root,
            )
            result["durable_contract"] = {
                "path": str(contract_path),
                "artifact_id": contract_record["artifact_id"],
                "sha256": contract_record["sha256"],
            }
        result_path, result_record = retention.write_json_artifact(
            "handoffs/cyber-runner",
            f"{command}-result.json",
            result,
            artifact_type="cyber-runner-structured-handoff",
            context=(
                "Ephemeral worker final handoff; full Codex event stream intentionally "
                "not retained"
            ),
            root=root,
        )
    except Exception as exc:
        raise ContractError(f"cannot persist cyber-runner artifacts: {exc}") from exc
    result["durable_result"] = {
        "path": str(result_path),
        "artifact_id": result_record["artifact_id"],
        "sha256": result_record["sha256"],
    }
    return result


def profile_path(root: Path) -> Path:
    return root / ".codex" / "agents" / "sage_cyber_executor.toml"


def load_profile(root: Path) -> tuple[dict[str, Any], str, str]:
    path = profile_path(root)
    try:
        raw = path.read_bytes()
        profile = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"cannot load cyber profile {path}: {exc}") from exc

    if profile.get("name") != PROFILE_NAME:
        raise ContractError(
            f"cyber profile name must be {PROFILE_NAME!r}, got {profile.get('name')!r}"
        )
    if profile.get("model") != MODEL:
        raise ContractError(
            f"cyber profile model must be pinned to {MODEL!r}, got {profile.get('model')!r}"
        )
    instructions = profile.get("developer_instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ContractError("cyber profile developer_instructions must be a non-empty string")
    return profile, instructions, hashlib.sha256(raw).hexdigest()


def load_contract(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 65536:
            raise ContractError("contract exceeds the 64 KiB limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load contract {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("contract must be a JSON object")
    return value


def canonical_contract_sha256(contract: dict[str, Any]) -> str:
    payload = json.dumps(
        contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_nonempty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")


def _validate_nonempty_strings(value: Any, field: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ContractError(f"{field} must be a non-empty list of non-empty strings")


def _validate_host(host: Any) -> str:
    _validate_nonempty_string(host, "network_endpoints[].host")
    assert isinstance(host, str)
    if host == "*" or "://" in host or "/" in host or "@" in host or any(
        char.isspace() for char in host
    ):
        raise ContractError(
            f"network endpoint host must be an exact host/IP or scoped wildcard, got {host!r}"
        )
    candidate = host
    if candidate.startswith("**."):
        candidate = candidate[3:]
    elif candidate.startswith("*."):
        candidate = candidate[2:]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        if candidate != "localhost" and not re.fullmatch(
            r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
            candidate,
        ):
            raise ContractError(f"invalid network endpoint host {host!r}")
    else:
        if address.is_unspecified or address.is_multicast:
            raise ContractError(f"unsafe network endpoint address {host!r}")
    return host


def validate_network_endpoints(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ContractError("network_endpoints must be a list")
    endpoints: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, endpoint in enumerate(value):
        if not isinstance(endpoint, dict):
            raise ContractError(f"network_endpoints[{index}] must be an object")
        if set(endpoint) != {"host", "role", "description"}:
            raise ContractError(
                f"network_endpoints[{index}] must contain only host, role, and description"
            )
        host = _validate_host(endpoint.get("host"))
        role = endpoint.get("role")
        if role not in NETWORK_ROLES:
            raise ContractError(
                f"network_endpoints[{index}].role must be a control-plane role"
            )
        _validate_nonempty_string(
            endpoint.get("description"), f"network_endpoints[{index}].description"
        )
        if host in seen:
            raise ContractError(f"duplicate network endpoint host {host!r}")
        seen.add(host)
        endpoints.append(
            {"host": host, "role": role, "description": endpoint["description"]}
        )
    return endpoints


def validate_live_run_contract(value: Any) -> None:
    if not isinstance(value, dict):
        raise ContractError("live_run_contract must be an object for live activity")
    extra = sorted(set(value) - ALLOWED_LIVE_FIELDS)
    missing = sorted(ALLOWED_LIVE_FIELDS - set(value))
    if extra:
        raise ContractError(f"unsupported live_run_contract fields: {', '.join(extra)}")
    if missing:
        raise ContractError(f"missing live_run_contract fields: {', '.join(missing)}")
    for field in LIVE_TEXT_FIELDS:
        _validate_nonempty_string(value.get(field), f"live_run_contract.{field}")
    for field in LIVE_LIST_FIELDS:
        _validate_nonempty_strings(value.get(field), f"live_run_contract.{field}")
    for field in LIVE_OBJECT_FIELDS:
        if not isinstance(value.get(field), dict):
            raise ContractError(f"live_run_contract.{field} must be an object")
    retry_cap = value.get("retry_cap")
    if not isinstance(retry_cap, int) or isinstance(retry_cap, bool) or retry_cap < 0:
        raise ContractError("live_run_contract.retry_cap must be a non-negative integer")


def validate_goal_scope(value: Any) -> None:
    if not isinstance(value, dict):
        raise ContractError("goal_scope must be an object in active-goal mode")
    expected = {"goal_reference", "goal_objective", "live_activity_within_goal"}
    if set(value) != expected:
        raise ContractError(
            "goal_scope must contain only goal_reference, goal_objective, and live_activity_within_goal"
        )
    _validate_nonempty_string(value.get("goal_reference"), "goal_scope.goal_reference")
    _validate_nonempty_string(value.get("goal_objective"), "goal_scope.goal_objective")
    if not isinstance(value.get("live_activity_within_goal"), bool):
        raise ContractError("goal_scope.live_activity_within_goal must be a boolean")


def validate_contract(contract: dict[str, Any], sandbox_mode: str) -> None:
    extra = sorted(set(contract) - ALLOWED_CONTRACT_FIELDS)
    if extra:
        raise ContractError(f"unsupported contract fields: {', '.join(extra)}")
    missing = sorted(ALLOWED_CONTRACT_FIELDS - set(contract))
    if missing:
        raise ContractError(f"missing contract fields: {', '.join(missing)}")

    for field in REQUIRED_TEXT_FIELDS:
        _validate_nonempty_string(contract.get(field), field)
    for field in REQUIRED_LIST_FIELDS:
        _validate_nonempty_strings(contract.get(field), field)
    for field in REQUIRED_BOOLEAN_FIELDS:
        if not isinstance(contract.get(field), bool):
            raise ContractError(f"{field} must be a boolean")

    authorization_mode = contract.get("authorization_mode")
    if authorization_mode not in {"direct", "active-goal"}:
        raise ContractError("authorization_mode must be direct or active-goal")
    if authorization_mode == "active-goal":
        validate_goal_scope(contract.get("goal_scope"))
    elif contract.get("goal_scope") is not None:
        raise ContractError("goal_scope must be null in direct authorization mode")

    endpoints = validate_network_endpoints(contract.get("network_endpoints"))
    network_authorized = contract["network_activity_authorized"]
    live_authorized = contract["live_activity_authorized"]
    workspace_authorized = contract["workspace_write_authorized"]

    if sandbox_mode == "workspace-write" and not workspace_authorized:
        raise ContractError(
            "workspace-write sandbox requires workspace_write_authorized=true"
        )
    if network_authorized:
        if sandbox_mode != "workspace-write" or not workspace_authorized:
            raise ContractError(
                "network activity requires workspace-write sandbox and authorization"
            )
        if not endpoints:
            raise ContractError("network activity requires at least one allowlisted endpoint")
    elif endpoints:
        raise ContractError("network_endpoints must be empty when network is not authorized")

    if live_authorized:
        if not network_authorized:
            raise ContractError("live activity requires network activity authorization")
        validate_live_run_contract(contract.get("live_run_contract"))
        if authorization_mode == "active-goal" and not contract["goal_scope"][
            "live_activity_within_goal"
        ]:
            raise ContractError(
                "live activity is outside the active goal's authorization"
            )
    elif contract.get("live_run_contract") is not None:
        raise ContractError(
            "live_run_contract must be null when live activity is not authorized"
        )


def approval_required(contract: dict[str, Any]) -> bool:
    if contract["authorization_mode"] == "active-goal":
        return False
    return bool(
        contract["workspace_write_authorized"]
        or contract["network_activity_authorized"]
        or contract["live_activity_authorized"]
    )


def verify_approval_hash(contract: dict[str, Any], approval_sha256: str | None) -> str:
    contract_sha256 = canonical_contract_sha256(contract)
    if approval_required(contract) and approval_sha256 != contract_sha256:
        raise ContractError(
            "writes, network, or live activity require --approval-sha256 matching the prepared contract"
        )
    if approval_sha256 is not None and approval_sha256 != contract_sha256:
        raise ContractError("approval SHA-256 does not match the contract")
    return contract_sha256


def build_prompt(
    contract: dict[str, Any], contract_sha256: str, smoke_nonce: str | None = None
) -> str:
    contract_json = json.dumps(contract, indent=2, sort_keys=True)
    if smoke_nonce is None:
        response_requirements = """
Complete only the contracted task. In active-goal mode, the recorded goal is standing authorization for work inside
that scope; do not ask routine permission questions. Treat network_endpoints as exhaustive. Never connect directly
to a target service; target-facing activity must be issued through an authorized Mythic payload task. Return the
concise, redacted handoff required by your developer instructions, and do not claim evidence outside the
verification plan.
""".strip()
    else:
        response_requirements = f"""
This is a profile and LLM communication smoke test. Perform only read-only startup inspection required by your
developer instructions. Return these labeled fields:

PROFILE_STATUS: ACTIVE only if your developer instructions identify you as the Sage cyber implementation and
live-range execution specialist; otherwise PROFILE_STATUS: PROFILE_NOT_ACTIVE.
NONCE: {smoke_nonce}
EXECUTION_BOUNDARY: one sentence stating where target-facing LDAP, SMB, Kerberos, and WinRM activity must execute.
SIDE_EFFECTS: NONE only if you made no writes and performed no live or task network activity.

End with the required ACTION ITEMS FOR RUSSEL section.
""".strip()

    return f"""SUPERVISOR CONTRACT SHA256: {contract_sha256}

{contract_json}

{response_requirements}
"""


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_allow_table(hosts: list[str]) -> str:
    rows = [f"{_toml_string(host)} = \"allow\"" for host in hosts]
    return "{ " + ", ".join(rows) + " }"


def build_command(
    *,
    codex_binary: str,
    root: Path,
    developer_instructions: str,
    sandbox_mode: str,
    endpoints: list[dict[str, str]],
) -> list[str]:
    command = [
        codex_binary,
        "exec",
        "--ephemeral",
        "--disable",
        "multi_agent",
        "--strict-config",
        "--model",
        MODEL,
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        "suppress_unstable_features_warning=true",
        "-c",
        f"developer_instructions={_toml_string(developer_instructions)}",
    ]
    if endpoints:
        command.extend(
            [
                "-c",
                "sandbox_workspace_write.network_access=true",
                "-c",
                "features.network_proxy.enabled=true",
                "-c",
                f"features.network_proxy.domains={_toml_allow_table([row['host'] for row in endpoints])}",
            ]
        )
    command.extend(
        [
            "--sandbox",
            sandbox_mode,
            "--cd",
            str(root),
            "--json",
            "-",
        ]
    )
    return command


def parse_codex_jsonl(stdout: str) -> dict[str, Any]:
    thread_id: str | None = None
    final_message: str | None = None
    usage: dict[str, Any] | None = None
    errors: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                final_message = item["text"]
            elif item.get("type") == "error" and isinstance(item.get("message"), str):
                errors.append(item["message"])
        elif event_type == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    return {
        "thread_id": thread_id,
        "final_message": final_message,
        "usage": usage,
        "errors": errors,
    }


def execute(
    *,
    contract: dict[str, Any],
    sandbox_mode: str,
    timeout_seconds: int,
    approval_sha256: str | None = None,
    smoke_nonce: str | None = None,
) -> dict[str, Any]:
    root = repository_root()
    profile, developer_instructions, profile_sha256 = load_profile(root)
    validate_contract(contract, sandbox_mode)
    contract_sha256 = verify_approval_hash(contract, approval_sha256)
    endpoints = validate_network_endpoints(contract["network_endpoints"])
    codex_binary = shutil.which("codex")
    if not codex_binary:
        raise ContractError("codex executable was not found on PATH")
    command = build_command(
        codex_binary=codex_binary,
        root=root,
        developer_instructions=developer_instructions,
        sandbox_mode=sandbox_mode,
        endpoints=endpoints,
    )
    try:
        completed = subprocess.run(
            command,
            input=build_prompt(contract, contract_sha256, smoke_nonce),
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "runner": "sage-cyber-runner",
            "requested_model": MODEL,
            "profile_name": profile["name"],
            "profile_sha256": profile_sha256,
            "contract_sha256": contract_sha256,
            "authorization_mode": contract["authorization_mode"],
            "sandbox_mode": sandbox_mode,
            "network_hosts": [row["host"] for row in endpoints],
            "live_activity_authorized": contract["live_activity_authorized"],
            "exit_code": None,
            "errors": [f"Codex worker timed out after {timeout_seconds} seconds"],
        }

    parsed = parse_codex_jsonl(completed.stdout)
    errors = list(parsed["errors"])
    if completed.returncode != 0:
        stderr_tail = completed.stderr.strip()[-2000:]
        errors.append(
            f"Codex worker exited {completed.returncode}"
            + (f": {stderr_tail}" if stderr_tail else "")
        )
    if not parsed["final_message"]:
        errors.append("Codex worker returned no final agent message")

    return {
        "ok": not errors,
        "runner": "sage-cyber-runner",
        "requested_model": MODEL,
        "profile_name": profile["name"],
        "profile_sha256": profile_sha256,
        "contract_sha256": contract_sha256,
        "authorization_mode": contract["authorization_mode"],
        "sandbox_mode": sandbox_mode,
        "network_hosts": [row["host"] for row in endpoints],
        "live_activity_authorized": contract["live_activity_authorized"],
        "thread_id": parsed["thread_id"],
        "exit_code": completed.returncode,
        "usage": parsed["usage"],
        "errors": errors,
        "final_message": parsed["final_message"],
    }


def smoke_checks(final_message: str | None, nonce: str) -> dict[str, bool]:
    message = final_message or ""
    upper = message.upper()
    return {
        "profile_active": bool(
            re.search(r"PROFILE_STATUS\s*:\s*(?:PROFILE_)?ACTIVE\b", upper)
        ),
        "nonce_round_trip": nonce in message,
        "mythic_execution_boundary": (
            "MYTHIC" in upper
            and ("PAYLOAD" in upper or "CALLBACK" in upper)
            and any(protocol in upper for protocol in ("LDAP", "SMB", "KERBEROS", "WINRM"))
        ),
        "zero_side_effects": bool(re.search(r"SIDE_EFFECTS\s*:\s*NONE\b", upper)),
        "action_items_present": "ACTION ITEMS FOR RUSSEL" in upper,
    }


def smoke_contract() -> dict[str, Any]:
    return {
        "objective": "Verify the pinned Sage cyber profile and LLM communication path.",
        "task": "Return the requested profile marker, nonce, execution boundary, and side-effect report.",
        "authorization_source": "User approved implementation and an offline read-only smoke test in this session.",
        "authorization_mode": "direct",
        "goal_scope": None,
        "permitted_file_scope": [
            "Read-only startup inspection of AGENTS.md",
            "Read-only startup inspection of Plans/RESUME.md and Plans/CURRENT_WORK.md",
            "Read-only startup inspection of skills/README.md",
        ],
        "protected_surfaces": [
            "All repository writes",
            "Mythic, BloodHound, Ludus, GOAD, callbacks, runtime databases, credentials, and external systems",
        ],
        "verification_plan": [
            "Profile marker inspection",
            "Nonce round trip",
            "Execution-boundary answer",
            "Zero-side-effect report",
        ],
        "stop_loss": [
            "Stop on missing profile instructions",
            "Stop before any write, live activity, or task network activity",
        ],
        "workspace_write_authorized": False,
        "network_activity_authorized": False,
        "network_endpoints": [],
        "live_activity_authorized": False,
        "live_run_contract": None,
    }


def contract_summary(contract: dict[str, Any], sandbox_mode: str) -> dict[str, Any]:
    endpoints = validate_network_endpoints(contract["network_endpoints"])
    live = contract.get("live_run_contract") or {}
    return {
        "objective": contract["objective"],
        "authorization_source": contract["authorization_source"],
        "authorization_mode": contract["authorization_mode"],
        "goal_scope": contract["goal_scope"],
        "sandbox_mode": sandbox_mode,
        "permitted_file_scope": contract["permitted_file_scope"],
        "network_endpoints": endpoints,
        "live_activity_authorized": contract["live_activity_authorized"],
        "live_attempt_id": live.get("attempt_id"),
        "live_callback_binding": live.get("callback_binding"),
        "allowed_capabilities": live.get("allowed_capabilities", []),
        "stop_loss": contract["stop_loss"],
    }


def emit(result: dict[str, Any]) -> int:
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser(
        "prepare", help="validate and hash a contract without a model call"
    )
    prepare_parser.add_argument("--contract", type=Path, required=True)
    prepare_parser.add_argument(
        "--sandbox", choices=("read-only", "workspace-write"), default="read-only"
    )

    run_parser = subparsers.add_parser("run", help="run an exact approved contract")
    run_parser.add_argument("--contract", type=Path, required=True)
    run_parser.add_argument(
        "--sandbox", choices=("read-only", "workspace-write"), default="read-only"
    )
    run_parser.add_argument("--approval-sha256")
    run_parser.add_argument("--timeout-seconds", type=int, default=900)

    smoke_parser = subparsers.add_parser("smoke", help="run the offline profile/model smoke test")
    smoke_parser.add_argument("--timeout-seconds", type=int, default=240)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            contract = load_contract(args.contract)
            validate_contract(contract, args.sandbox)
            return emit(
                persist_runner_artifacts(
                    command="prepare",
                    contract=contract,
                    result={
                    "ok": True,
                    "contract": str(args.contract),
                    "contract_sha256": canonical_contract_sha256(contract),
                    "approval_required": approval_required(contract),
                    "scope": contract_summary(contract, args.sandbox),
                    },
                )
            )
        if args.command == "run":
            if args.timeout_seconds < 1:
                raise ContractError("timeout-seconds must be positive")
            contract = load_contract(args.contract)
            return emit(
                persist_runner_artifacts(
                    command="run",
                    contract=contract,
                    result=execute(
                        contract=contract,
                        sandbox_mode=args.sandbox,
                        timeout_seconds=args.timeout_seconds,
                        approval_sha256=args.approval_sha256,
                    ),
                )
            )
        if args.timeout_seconds < 1:
            raise ContractError("timeout-seconds must be positive")
        nonce = "SAGE-CYBER-RUNNER-SMOKE-20260716-6F2A"
        result = execute(
            contract=smoke_contract(),
            sandbox_mode="read-only",
            timeout_seconds=args.timeout_seconds,
            smoke_nonce=nonce,
        )
        checks = smoke_checks(result.get("final_message"), nonce)
        result["checks"] = checks
        result["ok"] = bool(result.get("ok") and all(checks.values()))
        return emit(
            persist_runner_artifacts(
                command="smoke",
                contract=smoke_contract(),
                result=result,
            )
        )
    except ContractError as exc:
        return emit({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    sys.exit(main())
