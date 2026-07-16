from __future__ import annotations

from dataclasses import replace
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import evaluation_authorization as ea  # noqa: E402
import proof_boundary as pb  # noqa: E402


NOW = "2026-07-16T12:00:00+00:00"


def _callback(callback_id: str = "2", host: str = "cinder-ws01", domain: str = "cinder.local", identity: str = "user1"):
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
        callbacks=(_callback(),),
        target_realms=("ash.cinder.local",),
        allowed_targets={
            "hosts": ("ash-ops01",),
            "domains": ("ash.cinder.local",),
        },
        allowed_capabilities=("execute-as-local-admin",),
        denied_capabilities=("delete-domain",),
        allowed_effects=("remote-exec:ash-ops01@ash.cinder.local",),
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


def _envelope(*, manifest=None, binding=None, callback=None, args=None, decision_origin="symbolic_control", policy_decision_id="decision-1"):
    manifest = manifest or _manifest()
    binding = binding or _binding(manifest)
    return ea.build_action_envelope(
        manifest,
        binding,
        callback=callback or _callback(),
        target_fields={"hosts": "ash-ops01", "domains": "ash.cinder.local"},
        capability="execute-as-local-admin",
        effects=("remote-exec:ash-ops01@ash.cinder.local",),
        concrete_arguments={"command": "cat", "path": r"C:\proof.txt"} if args is None else args,
        transaction_id="transaction-1",
        decision_origin=decision_origin,
        policy_decision_id=policy_decision_id,
    )


def test_post_normalization_action_envelope_and_arm_blind_allow():
    manifest = _manifest()
    binding = _binding(manifest)
    envelope = _envelope(manifest=manifest, binding=binding)
    other_arm = _envelope(
        manifest=manifest,
        binding=binding,
        decision_origin="hybrid_model_branch",
        policy_decision_id="decision-2",
    )

    assert envelope is not None
    assert envelope.exact_arguments_sha256 == ea.canonical_json_sha256({"command": "cat", "path": r"C:\proof.txt"})
    assert envelope.sha256 != other_arm.sha256
    assert envelope.enforcement_projection_sha256 == other_arm.enforcement_projection_sha256
    decision = ea.authorize_action(manifest, binding, envelope, now=NOW)
    other_decision = ea.authorize_action(manifest, binding, other_arm, now=NOW)
    assert decision.decision == ea.ALLOW
    assert decision.reason_code == "manifest_allows_exact_envelope"
    assert decision.decision_id == other_decision.decision_id


def test_authorization_projection_excludes_audit_only_policy_fields_and_module_is_model_free():
    manifest = _manifest()
    binding = _binding(manifest)
    envelope = _envelope(manifest=manifest, binding=binding)
    assert envelope is not None
    projection = envelope.enforcement_projection()
    source = (Path(__file__).resolve().parents[1] / "ai" / "langgraph" / "evaluation_authorization.py").read_text()

    assert "decision_origin" not in projection
    assert "policy_decision_id" not in projection
    assert "langchain" not in source
    assert "prompt" not in source
    assert "rationale" not in source


def test_action_envelope_requires_resolved_callback_target_capability_and_arguments():
    manifest = _manifest()
    binding = _binding(manifest)

    assert ea.build_action_envelope(
        manifest,
        binding,
        callback=_callback(),
        target_fields={"hosts": "ash-ops01"},
        capability="execute-as-local-admin",
        effects=("remote-exec:ash-ops01@ash.cinder.local",),
        concrete_arguments=None,
        transaction_id="transaction-1",
        decision_origin="symbolic_control",
    ) is None
    assert ea.build_action_envelope(
        manifest,
        binding,
        callback=_callback(),
        target_fields={},
        capability="execute-as-local-admin",
        effects=("remote-exec:ash-ops01@ash.cinder.local",),
        concrete_arguments={"command": "cat"},
        transaction_id="transaction-1",
        decision_origin="symbolic_control",
    ) is None


def test_exact_fields_suffix_collision_and_deny_precedence_fail_closed():
    manifest = _manifest()
    binding = _binding(manifest)
    suffix_collision = _envelope(
        manifest=manifest,
        binding=binding,
        callback=_callback(host="not-cinder-ws01", domain="evilcinder.local"),
    )
    assert ea.authorize_action(manifest, binding, suffix_collision, now=NOW).decision == ea.DENY

    denied_manifest = replace(
        manifest,
        denied_capabilities=("execute-as-local-admin",),
    )
    denied_binding = _binding(denied_manifest)
    denied_envelope = _envelope(manifest=denied_manifest, binding=denied_binding)
    decision = ea.authorize_action(denied_manifest, denied_binding, denied_envelope, now=NOW)
    assert decision.decision == ea.DENY
    assert decision.reason_code == "explicit_deny"


def test_stale_replay_cross_cell_cross_callback_and_argument_mutation_reject():
    manifest = _manifest()
    binding = _binding(manifest)
    envelope = _envelope(manifest=manifest, binding=binding)
    assert envelope is not None

    stale = ea.authorize_action(manifest, binding, envelope, now="2026-07-18T00:00:00+00:00")
    replay = ea.authorize_action(
        manifest,
        binding,
        envelope,
        now=NOW,
        seen_enforcement_digests={envelope.enforcement_projection_sha256},
    )
    cross_cell = ea.authorize_action(
        manifest,
        replace(binding, cell_id="cell-2"),
        envelope,
        now=NOW,
    )
    cross_callback = ea.authorize_action(
        manifest,
        _binding(manifest, callback=_callback(callback_id="3")),
        envelope,
        now=NOW,
    )
    decision = ea.authorize_action(manifest, binding, envelope, now=NOW)
    mutated = _envelope(manifest=manifest, binding=binding, args={"command": "cat", "path": r"C:\other.txt"})
    ok, reason = ea.authorization_join_matches(decision, mutated)

    assert stale.reason_code == "stale_authorization_context"
    assert replay.reason_code == "replay_detected"
    assert cross_cell.reason_code == "cell_binding_mismatch"
    assert cross_callback.reason_code == "callback_binding_mismatch"
    assert ok is False
    assert reason == "action_envelope_digest_mismatch"


def test_authorization_provenance_joins_proof_without_conflating_proof_and_auth():
    manifest = _manifest()
    binding = _binding(manifest)
    envelope = _envelope(manifest=manifest, binding=binding)
    decision = ea.authorize_action(manifest, binding, envelope, now=NOW)
    proof = pb.make_runtime_task_envelope(
        engagement_id=manifest.engagement_id,
        callback_id=envelope.callback.callback_id,
        transaction_id=envelope.transaction_id,
        task_id="450",
        terminal_status="completed",
        command="cat",
        verifier_id="capability:execute-as-local-admin",
        verifier_input={"probe": {"remote_execution_proven": True}},
        verifier_result={"verdict": "achieved"},
        authorization=decision.proof_lineage_fields(),
        captured_at=NOW,
    )
    admission = pb.admit_runtime_envelope(
        proof,
        current_engagement_id=manifest.engagement_id,
        expected_authorization=decision.proof_lineage_fields(),
    )
    missing_join = pb.ProofEnvelope.from_dict({
        **proof.to_dict(),
        "authorization_decision_id": "",
    })

    assert admission.admitted is True
    assert proof.verifier_input_sha256 != proof.action_envelope_sha256
    assert missing_join is not None
    assert pb.admit_runtime_envelope(
        missing_join,
        current_engagement_id=manifest.engagement_id,
    ).admitted is False
