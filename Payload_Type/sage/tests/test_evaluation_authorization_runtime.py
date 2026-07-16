from __future__ import annotations

import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import evaluation_authorization as ea  # noqa: E402
import evaluation_authorization_runtime as ear  # noqa: E402
import mythic_tools  # noqa: E402
from mythic_tools import MythicTools  # noqa: E402


NOW = "2026-07-16T12:00:00+00:00"


def _callback(callback_id: str = "2", host: str = "cinder-ws01", domain: str = "cinder.local", identity: str = "cinder\\user1"):
    return ea.CallbackSelector(callback_id=callback_id, host=host, domain=domain, identity=identity)


def _manifest():
    return ea.EvaluationAuthorizationManifest(
        manifest_id="manifest-phase18-r1",
        version="1",
        operator_authorization_id="operator-approval-1",
        engagement_id="engagement-1",
        range_id="range-1",
        snapshot_id="snapshot-1",
        valid_from="2026-07-16T00:00:00+00:00",
        valid_until="2026-07-17T00:00:00+00:00",
        allowed_cells=("cell-1",),
        callbacks=(_callback(callback_id=""),),
        target_realms=("ash.cinder.local",),
        allowed_targets={
            "hosts": ("cinder-ws01", "ash-ops01"),
            "domains": ("ash.cinder.local",),
            "principals": ("administrator@ash.cinder.local",),
        },
        allowed_capabilities=("collect-graph", "execute-as-local-admin"),
        denied_capabilities=("delete-domain",),
        allowed_effects=("graph-collected", "remote-exec:ash-ops01@ash.cinder.local"),
        denied_effects=("domain-destroyed:ash.cinder.local",),
    )


def _binding(manifest=None, callback=None):
    manifest = manifest or _manifest()
    return ea.TrustedCellBinding(
        cell_id="cell-1",
        cell_authorization_id="cell-auth-1",
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.sha256,
        engagement_id=manifest.engagement_id,
        callback=callback or _callback(),
        issued_at="2026-07-16T00:00:00+00:00",
        expires_at="2026-07-17T00:00:00+00:00",
    )


def _context(manifest=None, binding=None):
    manifest = manifest or _manifest()
    binding = binding or _binding(manifest)
    return {
        "authorization_manifest": manifest.to_dict(),
        "trusted_cell_binding": binding.to_dict(),
    }


def _tools(context=None):
    mt = MythicTools(agent_task_id="test")
    mt.client = object()
    mt.set_evaluation_authorization_context(context or _context())
    return mt


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("users.json", json.dumps({"data": ["x" * 256]}))
    return buf.getvalue()


def test_runtime_context_is_disabled_by_default_and_fails_closed_when_enabled_without_context(monkeypatch):
    monkeypatch.delenv(ear.AUTHORIZATION_MODE_ENV, raising=False)
    assert ear.EvaluationAuthorizationRuntime.from_env().enabled is False

    monkeypatch.setenv(ear.AUTHORIZATION_MODE_ENV, "1")
    monkeypatch.delenv(ear.AUTHORIZATION_CONTEXT_PATH_ENV, raising=False)
    monkeypatch.delenv(ear.AUTHORIZATION_CONTEXT_JSON_ENV, raising=False)
    runtime = ear.EvaluationAuthorizationRuntime.from_env()
    outcome = runtime.authorize(
        callback=_callback(),
        target_fields={"hosts": "ash-ops01"},
        capability="execute-as-local-admin",
        effects=("remote-exec:ash-ops01@ash.cinder.local",),
        concrete_arguments={"command": "cat"},
        transaction_id="transaction-1",
        decision_origin="symbolic_control",
        now=NOW,
    )

    assert runtime.enabled is True
    assert runtime.available is False
    assert outcome.allowed is False
    assert outcome.reason_code == "authorization_context_missing"


def test_runtime_allow_is_arm_blind_and_same_envelope_replay_is_denied():
    runtime_a = ear.EvaluationAuthorizationRuntime.from_dict(_context())
    runtime_b = ear.EvaluationAuthorizationRuntime.from_dict(_context())
    kwargs = {
        "callback": _callback(),
        "target_fields": {"hosts": "ash-ops01", "domains": "ash.cinder.local"},
        "capability": "execute-as-local-admin",
        "effects": ("remote-exec:ash-ops01@ash.cinder.local",),
        "concrete_arguments": {"command": "cat", "path": r"C:\proof.txt"},
        "transaction_id": "transaction-1",
        "now": NOW,
    }

    first = runtime_a.authorize(**kwargs, decision_origin="hybrid_model_branch", policy_decision_id="decision-hybrid")
    other_arm = runtime_b.authorize(**kwargs, decision_origin="symbolic_control", policy_decision_id="decision-symbolic")
    replay = runtime_a.authorize(**kwargs, decision_origin="hybrid_model_branch", policy_decision_id="decision-hybrid")

    assert first.allowed is True
    assert other_arm.allowed is True
    assert first.decision is not None and other_arm.decision is not None
    assert first.decision.decision_id == other_arm.decision.decision_id
    assert first.authorization["authorization_decision_id"] == first.decision.decision_id
    assert replay.allowed is False
    assert replay.reason_code == "replay_detected"


def test_callback_issue_boundary_allows_once_then_blocks_replay_without_second_task(monkeypatch):
    mt = _tools()
    params = {"command": "cat", "path": r"C:\proof.txt"}
    command_obj = {
        "command": "run",
        "parameters": params,
        "purpose": "prove remote execution",
        "expected_probe": "extract_remote_execution_probe",
        "produces": ["remote_execution_proof"],
        "consumes": [],
    }
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("run", params)
    ] = {
        "capability": "execute-as-local-admin",
        "target": "target=ash-ops01;target_domain=ash.cinder.local",
        "effects": ["remote-exec:ash-ops01@ash.cinder.local"],
        "intent": {"target_host": "ash-ops01", "target_domain": "ash.cinder.local"},
        "runtime_inputs": {},
        "policy_decision": {"decision_owner": "symbolic_control", "decision_id": "decision-1"},
        "transaction_id": "transaction-1",
    }
    calls = {"count": 0}

    async def fake_issue(*_args, **_kwargs):
        calls["count"] += 1
        mt._last_issued_task_display_id = 901
        return "proof"

    monkeypatch.setattr(mt, "issue_task_and_waitfor_task_output", fake_issue)
    output, task_id, _trace_id, authorization = asyncio.run(
        mt._issue_capability_callback_command(command_obj, "run", params, 2, 5, "execute-as-local-admin")
    )

    assert output == "proof"
    assert task_id == 901
    assert calls["count"] == 1
    assert authorization["authorization_decision"] == ea.ALLOW
    assert mt.evaluation_authorization_audit_snapshot()["events"][0]["allowed"] is True

    with pytest.raises(ear.EvaluationAuthorizationTerminal) as exc:
        asyncio.run(mt._issue_capability_callback_command(command_obj, "run", params, 2, 5, "execute-as-local-admin"))

    assert exc.value.outcome.reason_code == "replay_detected"
    assert calls["count"] == 1


def test_callback_mismatch_and_ingest_deny_produce_zero_covered_mutation(monkeypatch):
    mt = _tools()
    params = {"command": "cat", "path": r"C:\proof.txt"}
    command_obj = {"command": "run", "parameters": params, "purpose": "proof"}
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("run", params)
    ] = {
        "capability": "execute-as-local-admin",
        "target": "target=ash-ops01;target_domain=ash.cinder.local",
        "effects": ["remote-exec:ash-ops01@ash.cinder.local"],
        "intent": {"target_host": "ash-ops01", "target_domain": "ash.cinder.local"},
        "runtime_inputs": {},
        "policy_decision": {},
        "transaction_id": "transaction-2",
    }
    calls = {"tasks": 0, "download": 0}

    async def fake_issue(*_args, **_kwargs):
        calls["tasks"] += 1
        return "should not run"

    async def fake_meta(_file_uuid):
        return {"filename_utf8": "bloodhound.zip", "task": {"callback": {"display_id": 3}, "display_id": 11, "command_name": "download"}}

    async def fake_download(*_args, **_kwargs):
        calls["download"] += 1
        return _zip_bytes()

    monkeypatch.setattr(mt, "issue_task_and_waitfor_task_output", fake_issue)
    monkeypatch.setattr(mt, "_get_file_metadata", fake_meta)
    monkeypatch.setattr(mythic_tools.mythic, "download_file", fake_download)

    with pytest.raises(ear.EvaluationAuthorizationTerminal) as exc:
        asyncio.run(mt._issue_capability_callback_command(command_obj, "run", params, 3, 5, "execute-as-local-admin"))
    assert exc.value.outcome.reason_code == "callback_binding_mismatch"
    assert calls["tasks"] == 0

    ingest = json.loads(asyncio.run(mt.ingest_collection(file_uuid="file-1")))
    assert ingest["verdict"] == "authorization_safe_terminal"
    assert ingest["authorization_safe_terminal"] is True
    assert ingest["objective_proven"] is False
    assert ingest["policy_success"] is False
    assert ingest["retry_permitted"] is False
    assert ingest["reason"] == "callback_binding_mismatch"
    assert calls["download"] == 1


def test_private_authorization_lineage_is_attached_to_proof_without_public_issued_leak():
    mt = _tools()
    runtime = mt._evaluation_authorization_runtime()
    outcome = runtime.authorize(
        callback=_callback(),
        target_fields={"hosts": "ash-ops01", "domains": "ash.cinder.local"},
        capability="execute-as-local-admin",
        effects=("remote-exec:ash-ops01@ash.cinder.local",),
        concrete_arguments={"command": "cat"},
        transaction_id="transaction-9",
        decision_origin="symbolic_control",
        now=NOW,
    )
    item = {
        "task_id": 901,
        "callback_id": 2,
        "terminal_status": "completed",
        "command": "run",
        "_authorization": outcome.authorization,
    }
    proof = mt._capability_proof_reference(
        item,
        {"expected_probe": "extract_remote_execution_probe"},
        {"remote_execution_proven": True},
        None,
        transaction_id="transaction-9",
    )

    assert proof["authorization"]["authorization_decision_id"] == outcome.authorization["authorization_decision_id"]
    assert "_authorization" not in mt._capability_executor_public_issued([item])[0]


def test_eval_mode_runtime_proof_requires_exact_allow_lineage():
    mt = _tools()
    kwargs = {
        "callback_id": 2,
        "task_id": 901,
        "terminal_status": "completed",
        "command": "run",
        "transaction_id": "transaction-10",
        "verifier_input": {"probe": "input"},
        "verifier_result": {"verdict": "achieved"},
    }

    assert mt._runtime_task_proof_envelope("extract_remote_execution_probe", NOW, **kwargs) == {}

    outcome = mt._evaluation_authorization_runtime().authorize(
        callback=_callback(),
        target_fields={"hosts": "ash-ops01", "domains": "ash.cinder.local"},
        capability="execute-as-local-admin",
        effects=("remote-exec:ash-ops01@ash.cinder.local",),
        concrete_arguments={"command": "cat"},
        transaction_id="transaction-10",
        decision_origin="symbolic_control",
        now=NOW,
    )
    proof = mt._runtime_task_proof_envelope(
        "extract_remote_execution_probe",
        NOW,
        authorization=outcome.authorization,
        **kwargs,
    )

    assert proof["authorization_decision_id"] == outcome.authorization["authorization_decision_id"]


def test_deterministic_command_proof_uses_private_visibility_authorization_without_transaction_leak():
    mt = _tools()
    outcome = mt._evaluation_authorization_runtime().authorize(
        callback=_callback(),
        target_fields={"hosts": "ash-ops01", "domains": "ash.cinder.local"},
        capability="execute-as-local-admin",
        effects=("remote-exec:ash-ops01@ash.cinder.local",),
        concrete_arguments={"command": "cat"},
        transaction_id="transaction-11",
        decision_origin="symbolic_control",
        now=NOW,
    )
    token = mythic_tools._task_visibility_context.set({"authorization": outcome.authorization})
    try:
        evidence = mt._deterministic_capability_proof_evidence(
            {
                "transaction_id": "transaction-11",
                "expected_probe": "extract_remote_execution_probe",
                "intent": {},
            },
            "run",
            2,
            901,
            "completed",
            {"remote_execution_proven": True},
            SimpleNamespace(verdict="achieved", reason="proved", evidence={}),
        )
    finally:
        mythic_tools._task_visibility_context.reset(token)

    assert evidence["_proof_reference"]["authorization"]["authorization_decision_id"] == outcome.authorization["authorization_decision_id"]

    transaction = mt._capability_transaction_start(
        SimpleNamespace(
            name="execute-as-local-admin",
            target="target=ash-ops01;target_domain=ash.cinder.local",
            effects=("remote-exec:ash-ops01@ash.cinder.local",),
            intent={"transaction_id": "transaction-11", "authorization": outcome.authorization},
        ),
        {"commands": []},
    )
    assert "authorization_decision_id" not in transaction
    assert "authorization_manifest_id" not in transaction
    assert "decision_origin" not in transaction
