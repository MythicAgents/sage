from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_cyber.py"
SPEC = importlib.util.spec_from_file_location("run_cyber", SCRIPT)
assert SPEC and SPEC.loader
run_cyber = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_cyber)


def valid_contract() -> dict[str, object]:
    return {
        "objective": "Inspect one bounded Sage component.",
        "task": "Return a read-only finding.",
        "authorization_mode": "direct",
        "authorization_source": "User requested the inspection.",
        "goal_scope": None,
        "permitted_file_scope": ["Payload_Type/sage/tests/example.py"],
        "protected_surfaces": ["All writes and live systems"],
        "verification_plan": ["Source inspection"],
        "stop_loss": ["Stop before mutation"],
        "workspace_write_authorized": False,
        "network_activity_authorized": False,
        "network_endpoints": [],
        "live_activity_authorized": False,
        "live_run_contract": None,
    }


def active_goal_contract() -> dict[str, object]:
    contract = valid_contract()
    contract.update(
        {
            "authorization_mode": "active-goal",
            "authorization_source": "Active /goal created by the user.",
            "goal_scope": {
                "goal_reference": "goal:test-1",
                "goal_objective": "Complete the bounded Sage engineering task.",
                "live_activity_within_goal": False,
            },
            "workspace_write_authorized": True,
            "permitted_file_scope": ["skills/sage-cyber-runner/**"],
        }
    )
    return contract


def live_run_contract() -> dict[str, object]:
    return {
        "phase_or_tranche": "test-tranche",
        "attempt_id": "attempt-1",
        "range": "owned-lab",
        "snapshot": "clean-baseline",
        "callback_binding": "fresh-exact-callback",
        "provider": "openai",
        "route": "gpt-5.5-cyber-preview",
        "startup_overrides": {},
        "allowed_capabilities": ["read-control-plane-state"],
        "countability_gates": ["callback binding exact"],
        "retry_cap": 0,
        "evidence_schema": "read-only control-plane evidence",
        "artifact_hashes": {},
    }


def test_validate_contract_accepts_read_only_contract() -> None:
    run_cyber.validate_contract(valid_contract(), "read-only")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("objective", "", "objective"),
        ("verification_plan", [], "verification_plan"),
        ("authorization_mode", "unknown", "authorization_mode"),
    ],
)
def test_validate_contract_fails_closed(field: str, value: object, message: str) -> None:
    contract = valid_contract()
    contract[field] = value
    with pytest.raises(run_cyber.ContractError, match=message):
        run_cyber.validate_contract(contract, "read-only")


def test_workspace_write_requires_contract_authorization() -> None:
    with pytest.raises(run_cyber.ContractError, match="workspace-write"):
        run_cyber.validate_contract(valid_contract(), "workspace-write")


def test_network_requires_allowlist_and_workspace_write() -> None:
    contract = active_goal_contract()
    contract["network_activity_authorized"] = True
    with pytest.raises(run_cyber.ContractError, match="allowlisted endpoint"):
        run_cyber.validate_contract(contract, "workspace-write")


def test_network_accepts_exact_local_and_remote_control_plane_hosts() -> None:
    contract = active_goal_contract()
    contract["network_activity_authorized"] = True
    contract["network_endpoints"] = [
        {"host": "127.0.0.1", "role": "mythic", "description": "local API"},
        {"host": "10.4.10.254", "role": "ludus", "description": "remote controller"},
    ]
    run_cyber.validate_contract(contract, "workspace-write")


def test_network_rejects_global_wildcard_and_target_role() -> None:
    with pytest.raises(run_cyber.ContractError, match="exact host"):
        run_cyber.validate_network_endpoints(
            [{"host": "*", "role": "mythic", "description": "too broad"}]
        )
    with pytest.raises(run_cyber.ContractError, match="control-plane role"):
        run_cyber.validate_network_endpoints(
            [{"host": "10.4.10.22", "role": "target", "description": "forbidden"}]
        )


def test_active_goal_skips_per_call_hash_but_direct_write_requires_it() -> None:
    goal_contract = active_goal_contract()
    assert run_cyber.approval_required(goal_contract) is False
    assert run_cyber.verify_approval_hash(goal_contract, None) == (
        run_cyber.canonical_contract_sha256(goal_contract)
    )

    direct_contract = active_goal_contract()
    direct_contract["authorization_mode"] = "direct"
    direct_contract["goal_scope"] = None
    assert run_cyber.approval_required(direct_contract) is True
    with pytest.raises(run_cyber.ContractError, match="approval-sha256"):
        run_cyber.verify_approval_hash(direct_contract, None)


def test_live_goal_requires_live_scope_and_complete_contract() -> None:
    contract = active_goal_contract()
    contract["network_activity_authorized"] = True
    contract["network_endpoints"] = [
        {"host": "127.0.0.1", "role": "mythic", "description": "local API"}
    ]
    contract["live_activity_authorized"] = True
    contract["live_run_contract"] = live_run_contract()
    with pytest.raises(run_cyber.ContractError, match="outside the active goal"):
        run_cyber.validate_contract(contract, "workspace-write")

    assert isinstance(contract["goal_scope"], dict)
    contract["goal_scope"]["live_activity_within_goal"] = True
    run_cyber.validate_contract(contract, "workspace-write")


def test_load_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    contract = valid_contract()
    contract["unexpected"] = "value"
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(run_cyber.ContractError, match="unsupported contract fields"):
        run_cyber.validate_contract(run_cyber.load_contract(path), "read-only")


def test_build_command_pins_model_profile_and_no_nested_agents() -> None:
    command = run_cyber.build_command(
        codex_binary="/usr/bin/codex",
        root=Path("/repo"),
        developer_instructions="specialist instructions",
        sandbox_mode="read-only",
        endpoints=[],
    )
    assert command[command.index("--model") + 1] == "gpt-5.5-cyber-preview"
    assert command[command.index("--disable") + 1] == "multi_agent"
    assert "--ephemeral" in command
    assert "--strict-config" in command
    assert 'approval_policy="never"' in command
    assert "suppress_unstable_features_warning=true" in command
    assert "developer_instructions=\"specialist instructions\"" in command


def test_build_command_enforces_network_proxy_allowlist() -> None:
    command = run_cyber.build_command(
        codex_binary="codex",
        root=Path("/repo"),
        developer_instructions="instructions",
        sandbox_mode="workspace-write",
        endpoints=[
            {"host": "127.0.0.1", "role": "mythic", "description": "local"},
            {"host": "10.4.10.254", "role": "ludus", "description": "remote"},
        ],
    )
    assert "sandbox_workspace_write.network_access=true" in command
    policy = next(value for value in command if value.startswith("features.network_proxy.domains="))
    assert '"127.0.0.1" = "allow"' in policy
    assert '"10.4.10.254" = "allow"' in policy
    assert '"*" = "allow"' not in policy


def test_parse_codex_jsonl_extracts_thread_message_and_usage() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "handoff"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"total_tokens": 42}}),
        ]
    )
    assert run_cyber.parse_codex_jsonl(stdout) == {
        "thread_id": "thread-1",
        "final_message": "handoff",
        "usage": {"total_tokens": 42},
        "errors": [],
    }


def test_smoke_checks_require_profile_nonce_boundary_and_no_side_effects() -> None:
    nonce = "nonce-123"
    message = f"""PROFILE_STATUS: ACTIVE
NONCE: {nonce}
EXECUTION_BOUNDARY: Target-facing LDAP, SMB, Kerberos, and WinRM execute through authorized Mythic payload tasks.
SIDE_EFFECTS: NONE

ACTION ITEMS FOR RUSSEL
None
"""
    assert all(run_cyber.smoke_checks(message, nonce).values())


def test_smoke_checks_reject_generic_profile() -> None:
    checks = run_cyber.smoke_checks(
        "PROFILE_STATUS: PROFILE_NOT_ACTIVE\nSIDE_EFFECTS: NONE", "missing"
    )
    assert checks["profile_active"] is False
    assert checks["nonce_round_trip"] is False


def test_persist_runner_artifacts_keeps_worker_ephemeral_but_records_handoff(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(tmp_path / "history"))
    monkeypatch.setattr(run_cyber, "repository_root", lambda: tmp_path)
    contract = valid_contract()
    result = {
        "ok": True,
        "thread_id": "ephemeral-thread",
        "final_message": "bounded handoff",
    }

    persisted = run_cyber.persist_runner_artifacts(
        command="run",
        result=result,
        contract=contract,
    )

    contract_path = Path(persisted["durable_contract"]["path"])
    result_path = Path(persisted["durable_result"]["path"])
    assert contract_path.is_relative_to(tmp_path / "history")
    assert result_path.is_relative_to(tmp_path / "history")
    assert json.loads(contract_path.read_text()) == contract
    assert json.loads(result_path.read_text())["final_message"] == "bounded handoff"
    assert not any(path.name == "events.jsonl" for path in (tmp_path / "history").rglob("*"))
