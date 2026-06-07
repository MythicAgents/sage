import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state  # noqa: E402


def _foothold(host="WINTERFELL", forest="north.local", integrity="medium", alive=True):
    return engagement_state.Foothold(
        callback_id="cb50",
        agent="apollo",
        host=host,
        forest=forest,
        identity="NORTH\\arya",
        integrity=integrity,
        alive=alive,
        source="mythic",
        timestamp="2026-06-06T12:00:00Z",
    )


def _state_with_effect(effect, technique="seed", target="seed", evidence=None):
    return engagement_state.EngagementState(
        objective="essos DA",
        hops=[
            engagement_state.Hop(
                id=f"{technique}:{target}",
                technique=technique,
                target=target,
                effect=effect,
                status="achieved",
                evidence=evidence or {"task_id": "seed"},
                preconditions=[],
                satisfied_effects=[effect],
                source="test",
                timestamp="2026-06-06T12:00:00Z",
            )
        ],
    )


def test_gate_skips_gpo_abuse_when_effect_already_achieved_with_evidence():
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="essos DA"),
        "gpo-abuse",
        "WINTERFELL",
        "achieved",
        {"task_id": "2285", "source": "mythic"},
        "2026-06-06T12:34:00Z",
    )

    decision, reason = engagement_state.gate_decision("gpo-abuse", "WINTERFELL", state)

    assert decision == engagement_state.GateDecision.SKIP
    assert "effect already achieved" in reason
    assert "2285" in reason


def test_gate_defers_dcsync_rights_grant_without_essos_access():
    state = engagement_state.EngagementState(
        objective="essos DA",
        footholds=[_foothold(host="WINTERFELL", forest="north.local")],
    )

    decision, reason = engagement_state.gate_decision("dcsync-rights-grant", "essos.local", state)

    assert decision == engagement_state.GateDecision.DEFER
    assert "missing precondition" in reason
    # belief-aware: write-dacl is graph-derived; with no graph data reconciled it is UNKNOWN, not
    # false, so it must NOT block. The DEFER stands on the foothold-observable precondition only.
    assert "live-foothold:essos.local" in reason
    assert "write-dacl:domain:essos.local" not in reason


def test_gate_proceeds_when_preconditions_are_met():
    state = _state_with_effect(
        "write-dacl:domain:essos.local",
        technique="acl-discovery",
        target="essos.local",
    )
    state.footholds.append(_foothold(host="MEEREEN", forest="essos.local", integrity="high"))

    decision, reason = engagement_state.gate_decision("dcsync-rights-grant", "essos.local", state)

    assert decision == engagement_state.GateDecision.PROCEED
    assert "preconditions met" in reason


def test_gate_fail_open_for_unknown_technique():
    decision, reason = engagement_state.gate_decision(
        "totally-unknown-technique",
        "X",
        engagement_state.EngagementState(objective="essos DA"),
    )

    assert decision == engagement_state.GateDecision.PROCEED
    assert "fail-open" in reason


def test_gate_fail_soft_on_malformed_state():
    decision, reason = engagement_state.gate_decision("gpo-abuse", "WINTERFELL", object())

    assert decision == engagement_state.GateDecision.PROCEED
    assert "fail-open" in reason


def test_verify_effect_uses_structured_gpo_probe_only():
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", {"scheduled_task_present": True}) == "achieved"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", {"scheduled_task_present": False}) == "failed"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", {}) == "failed"


def test_verify_effect_ignores_error_strings_for_verdicts():
    achieved_probe = {
        "scheduled_task_present": True,
        "error": "System.UnauthorizedAccessException: Access to ScheduledTasks.xml is denied",
    }
    same_probe_different_error = {
        "scheduled_task_present": True,
        "error": "completely different tool text",
    }
    error_only = {
        "error": "System.UnauthorizedAccessException: Access to ScheduledTasks.xml is denied",
    }

    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", achieved_probe) == "achieved"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", same_probe_different_error) == "achieved"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", error_only) == "failed"
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", "UnauthorizedAccessException") == "failed"


def test_verify_effect_returns_partial_for_partial_structured_probe():
    assert engagement_state.verify_effect("gpo-abuse", "WINTERFELL", {"gpo_modified": True}) == "partial"


def test_record_hop_result_round_trips_with_provenance_and_updates():
    now = "2026-06-06T13:00:00Z"
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="essos DA"),
        "gpo-abuse",
        "WINTERFELL",
        "pending",
        {"task_id": "2284", "source": "mythic"},
        "2026-06-06T12:59:00Z",
    )
    updated = engagement_state.record_hop_result(
        state,
        "gpo-abuse",
        "WINTERFELL",
        "achieved",
        {"task_id": "2285", "source": "mythic"},
        now,
    )

    assert len(updated.hops) == 1
    hop = updated.hops[0]
    assert hop.status == "achieved"
    assert hop.source == "mythic"
    assert hop.timestamp == now
    assert hop.preconditions == ["generic-write:gpo:winterfell", "live-foothold:*"]
    assert hop.satisfied_effects == ["system:winterfell"]
    assert "system:winterfell" in updated.achieved_effects()

    decision, reason = engagement_state.gate_decision("gpo-abuse", "WINTERFELL", updated)

    assert decision == engagement_state.GateDecision.SKIP
    assert "2285" in reason


def test_technique_model_chains_dcsync_to_golden_ticket():
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="essos DA"),
        "dcsync",
        "essos.local",
        "achieved",
        {"task_id": "2400", "source": "mythic"},
        "2026-06-06T14:00:00Z",
    )

    decision, reason = engagement_state.gate_decision("golden-ticket", "essos.local", state)

    assert len(engagement_state.TECHNIQUE_MODEL) >= 6
    assert decision == engagement_state.GateDecision.PROCEED
    assert "preconditions met" in reason
