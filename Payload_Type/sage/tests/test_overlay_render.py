import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state  # noqa: E402
import prompt_loader  # noqa: E402


HEADER = "=== ENGAGEMENT STATE (observed; trust this over the plan) ==="
MARKER = "{{ENGAGEMENT_STATE}}"


def _foothold():
    return engagement_state.Foothold(
        callback_id="50",
        agent="apollo",
        host="WINTERFELL",
        forest="north.local",
        identity="NORTH\\arya",
        integrity="system",
        alive=True,
        source="mythic",
        timestamp="2026-06-06T12:00:00Z",
    )


def _hop(technique, target, effect, status):
    return engagement_state.Hop(
        id=f"{technique}:{target}",
        technique=technique,
        target=target,
        effect=effect,
        status=status,
        evidence={"task_id": "1234"},
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp="2026-06-06T12:00:00Z",
    )


def _state():
    return engagement_state.EngagementState(
        objective="reach essos DA",
        footholds=[_foothold()],
        hops=[
            _hop("gpo-abuse", "WINTERFELL", "system:winterfell", "achieved"),
            _hop("dcsync", "essos.local", "krbtgt-hash:essos.local", "failed"),
        ],
    )


def test_render_engagement_state_compactly_reports_live_state_and_hops():
    output = engagement_state.render_engagement_state(_state())

    assert HEADER in output
    assert "WINTERFELL" in output
    assert "north.local" in output
    assert "gpo-abuse → WINTERFELL" in output
    assert "dcsync → essos.local" in output
    assert len(output) <= 1600


def test_render_engagement_state_empty_state_is_non_empty_and_safe():
    output = engagement_state.render_engagement_state(
        engagement_state.EngagementState(objective="reach essos DA")
    )

    assert HEADER in output
    assert "(no observed state yet)" in output
    assert output.strip()


def test_overlay_prompt_contains_engagement_state_marker():
    raw = (prompt_loader.PROMPTS_DIR / "demo_autonomous_solve.md").read_text(encoding="utf-8")

    assert MARKER in raw


def test_load_autonomous_overlay_injects_state_when_gate_enabled(monkeypatch):
    monkeypatch.setattr(prompt_loader, "ENGAGEMENT_GATE_ENABLED", True)

    for role in ("Supervisor", "Mythic_Operator"):
        section = prompt_loader.load_autonomous_overlay(role, _state())

        assert HEADER in section
        assert MARKER not in section
        assert "WINTERFELL" in section


def test_load_autonomous_overlay_removes_marker_when_gate_disabled(monkeypatch):
    monkeypatch.setattr(prompt_loader, "ENGAGEMENT_GATE_ENABLED", False)

    section = prompt_loader.load_autonomous_overlay("Supervisor", _state())

    assert MARKER not in section
    assert HEADER not in section
    assert "AUTONOMOUS ATTACK-PATH SOLVE" in section


def test_trust_walker_tradecraft_is_retained_as_prior():
    raw = (prompt_loader.PROMPTS_DIR / "demo_autonomous_solve.md").read_text(encoding="utf-8")

    assert "STARKWALLPAPER" in raw
    assert "golden ticket" in raw
    assert "ADMINISTRATORS@ESSOS" in raw
    assert "Domain Admins is a GLOBAL group" in raw
    assert "it CANNOT hold a foreign-forest member" in raw
    assert "Use the golden-ticket route via the domain-local ADMINISTRATORS@ESSOS group instead" in raw
