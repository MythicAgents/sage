import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import engagement_state as es  # noqa: E402
import proof_boundary as pb  # noqa: E402


NOW = "2026-07-14T00:00:00+00:00"
ENGAGEMENT = "op-1"


def _task_proof(**overrides):
    values = {
        "engagement_id": ENGAGEMENT,
        "callback_id": "13",
        "task_id": "450",
        "terminal_status": "completed",
        "command": "dcsync",
        "verifier_id": "test:dcsync",
        "transaction_id": "transaction-1",
        "verifier_input": {"probe": {"krbtgt_hash_present": True}},
        "verifier_result": {"verdict": "achieved"},
        "captured_at": NOW,
    }
    values.update(overrides)
    return pb.make_runtime_task_envelope(**values)


def _credential_proof(**overrides):
    values = {
        "engagement_id": ENGAGEMENT,
        "callback_id": "13",
        "task_id": "451",
        "terminal_status": "completed",
        "command": "dcsync",
        "credential_id": "9001",
        "verifier_id": "test:credential-store",
        "transaction_id": "transaction-2",
        "verifier_input": {"credential_id": "9001"},
        "verifier_result": {"observed": True},
        "captured_at": NOW,
    }
    values.update(overrides)
    return pb.make_runtime_credential_envelope(**values)


def test_runtime_task_envelope_is_admitted_and_frozen():
    envelope = _task_proof()
    admission = pb.admit_runtime_envelope(envelope, current_engagement_id=ENGAGEMENT)

    assert admission.admitted is True
    assert envelope.hash.startswith("sha256:")
    try:
        envelope.origin = "host"
    except Exception as exc:
        assert type(exc).__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("ProofEnvelope must be immutable")


def test_disallowed_origins_and_wrong_engagement_fail_closed():
    host = pb.ProofEnvelope(
        scope=pb.RUNTIME_SCOPE,
        origin="host",
        engagement_id=ENGAGEMENT,
        callback_id="13",
        task_id="450",
        terminal_status="completed",
        command="dcsync",
        verifier_id="test:dcsync",
        verifier_hash=pb.stable_verifier_hash("test:dcsync"),
        verifier_input_sha256=pb.canonical_commitment_sha256({"probe": {"krbtgt_hash_present": True}}),
        verifier_result_sha256=pb.canonical_commitment_sha256({"verdict": "achieved"}),
        transaction_id="transaction-1",
        captured_at=NOW,
    )
    assert pb.admit_runtime_envelope(host, current_engagement_id=ENGAGEMENT).admitted is False
    assert pb.admit_runtime_envelope(_task_proof(), current_engagement_id="other-op").admitted is False


def test_runtime_credential_envelope_requires_task_lineage_and_credential_id():
    admission = pb.admit_runtime_envelope(_credential_proof(), current_engagement_id=ENGAGEMENT)
    missing_id = pb.admit_runtime_envelope(
        _credential_proof(credential_id=""),
        current_engagement_id=ENGAGEMENT,
    )

    assert admission.admitted is True
    assert admission.envelope.credential_id == "9001"
    assert missing_id.admitted is False


def test_untrusted_evidence_cannot_overwrite_reserved_lineage():
    envelope = _task_proof()
    evidence, admission = pb.attach_proof(
        {
            "source": "capability_verifier",
            "mythic_task_id": "spoofed",
            "callback_id": "spoofed",
            "origin": "host",
            "proof_envelope": {"origin": "host"},
            "note": "kept",
        },
        envelope,
        current_engagement_id=ENGAGEMENT,
    )

    assert admission.admitted is True
    assert evidence["mythic_task_id"] == "450"
    assert evidence["callback_id"] == "13"
    assert evidence["origin"] == pb.ORIGIN_MYTHIC_TASK
    assert evidence["note"] == "kept"


def test_runtime_state_quarantines_unproven_and_synthetic_achievements():
    unproven = es.Hop(
        id="a",
        technique="dcsync",
        target="lab.local",
        effect="krbtgt-hash:lab.local",
        status="achieved",
        evidence={},
        preconditions=[],
        satisfied_effects=["krbtgt-hash:lab.local"],
        source="test",
        timestamp=NOW,
    )
    synthetic = es.Hop(
        id="b",
        technique="dcsync",
        target="other.local",
        effect="krbtgt-hash:other.local",
        status="achieved",
        evidence={"proof_envelope": pb.synthetic_eval_envelope().to_dict()},
        preconditions=[],
        satisfied_effects=["krbtgt-hash:other.local"],
        source="test",
        timestamp=NOW,
        proof_envelope=pb.synthetic_eval_envelope().to_dict(),
    )
    admitted = es.Hop(
        id="c",
        technique="dcsync",
        target="good.local",
        effect="krbtgt-hash:good.local",
        status="achieved",
        evidence={"proof_envelope": _task_proof().to_dict()},
        preconditions=[],
        satisfied_effects=["krbtgt-hash:good.local"],
        source="test",
        timestamp=NOW,
        proof_envelope=_task_proof().to_dict(),
    )
    state = es.EngagementState(
        objective="x",
        hops=[unproven, synthetic, admitted],
        engagement_id=ENGAGEMENT,
        runtime_scope=True,
    )

    assert state.achieved_effects() == {"krbtgt-hash:good.local"}


def test_recording_runtime_achievement_without_proof_becomes_legacy_unverified():
    state = es.EngagementState(objective="x", engagement_id=ENGAGEMENT, runtime_scope=True)
    updated = es.record_effect_result(
        state,
        "capability:test",
        "target",
        "effect:target",
        "achieved",
        {"source": "host", "origin": "host"},
        NOW,
    )

    assert updated.hops[0].status == "legacy_unverified"
    assert updated.achieved_effects() == set()
    assert updated.hops[0].evidence["proof_persistence_state"] == pb.LEGACY_UNVERIFIED


def test_runtime_graph_facts_accept_credential_proof_only_for_credential_predicates():
    proof = _credential_proof().to_dict()
    state = es.EngagementState(
        objective="x",
        graph_facts=[
            es.GraphFact("creds:alice@lab.local", "live-probe", NOW, 600, proof),
            es.GraphFact("krbtgt-hash:lab.local", "live-probe", NOW, 600, proof),
            es.GraphFact("ds-replication-rights:lab.local", "live-probe", NOW, 600, proof),
        ],
        engagement_id=ENGAGEMENT,
        runtime_scope=True,
    )

    predicates = state.satisfied_predicates()
    assert "creds:alice@lab.local" in predicates
    assert "krbtgt-hash:lab.local" in predicates
    assert "ds-replication-rights:lab.local" not in predicates


def test_updating_existing_runtime_hop_preserves_admitted_proof_envelope():
    state = es.EngagementState(
        objective="x",
        hops=[
            es.Hop(
                id="stable-id",
                technique="capability:test",
                target="lab.local",
                effect="creds:alice@lab.local",
                status="pending",
                evidence={},
                preconditions=[],
                satisfied_effects=[],
                source="test",
                timestamp=NOW,
            )
        ],
        engagement_id=ENGAGEMENT,
        runtime_scope=True,
    )
    proof = _task_proof().to_dict()

    updated = es.record_effect_result(
        state,
        "capability:test",
        "lab.local",
        "creds:alice@lab.local",
        "achieved",
        {"source": "test"},
        NOW,
        proof_envelope=proof,
    )

    assert updated.hops[0].id == "stable-id"
    assert updated.hops[0].proof_envelope == proof
    assert updated.achieved_effects() == {"creds:alice@lab.local"}


def test_missing_exact_task_or_verifier_commitment_rejects_achievement():
    missing_task = _task_proof(task_id="")
    missing_input = _task_proof(verifier_input=None)
    missing_result = _task_proof(verifier_result=None)

    assert pb.admit_runtime_envelope(missing_task, current_engagement_id=ENGAGEMENT).admitted is False
    assert pb.admit_runtime_envelope(missing_input, current_engagement_id=ENGAGEMENT).admitted is False
    assert pb.admit_runtime_envelope(missing_result, current_engagement_id=ENGAGEMENT).admitted is False


def test_cross_transaction_callback_and_tampered_result_reject_at_boundary():
    envelope = _task_proof()
    assert pb.admit_runtime_envelope(
        envelope,
        current_engagement_id=ENGAGEMENT,
        expected_callback_id="14",
    ).admitted is False
    assert pb.admit_runtime_envelope(
        envelope,
        current_engagement_id=ENGAGEMENT,
        expected_transaction_id="transaction-other",
    ).admitted is False
    assert pb.admit_runtime_envelope(
        envelope,
        current_engagement_id=ENGAGEMENT,
        expected_task_id="451",
    ).admitted is False
    tampered = pb.ProofEnvelope.from_dict({
        **envelope.to_dict(),
        "verifier_result_sha256": "0" * 64,
    })
    assert tampered is not None
    assert pb.admit_runtime_envelope(
        tampered,
        current_engagement_id=ENGAGEMENT,
        expected_task_id="450",
        expected_verifier_result_sha256=envelope.verifier_result_sha256,
    ).admitted is False
