"""Empirical pre-DCSync rights precheck (2026-06-10).

The 2026-06-09 RCA demoted the engagement gate to a silent advisor (it stopped vetoing missing
preconditions because the static STRIPS model produced fatal false-negatives). Side effect: the agent
fired DCSync without replication rights and burned steps on 8453 (solve #134). This precheck re-blocks the
premature DCSync — but EMPIRICALLY (only when the graph is populated and shows the right absent) and CAPPED
(per-domain), so it can never re-create the permanent deadlock that demoted the gate.

Pure helpers — no Mythic/network. Mirrors the repo's no-pytest-asyncio convention.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools  # noqa: E402

MT = mythic_tools.MythicTools
MAX = MT._DCSYNC_PRECHECK_MAX_BLOCKS
REASON = "missing precondition(s): ds-replication-rights:north.sevenkingdoms.local, live-foothold:*"


def test_target_domain_dcsync_is_bare_domain():
    assert MT._dcsync_target_domain("dcsync", "north.sevenkingdoms.local") == "north.sevenkingdoms.local"


def test_target_domain_dcsync_user_is_realm_half():
    assert MT._dcsync_target_domain("dcsync-user", "cersei.lannister@sevenkingdoms.local") == "sevenkingdoms.local"


def test_block_when_graph_populated_and_rights_missing():
    assert MT._should_block_premature_dcsync("dcsync", REASON, True, 0, MAX) is True
    assert MT._should_block_premature_dcsync("dcsync-user", REASON, True, 0, MAX) is True


def test_failopen_on_empty_graph():
    # Ignorance (no graph facts) must NEVER block — this is the anti-deadlock invariant.
    assert MT._should_block_premature_dcsync("dcsync", REASON, False, 0, MAX) is False


def test_no_block_when_reason_is_not_replication_rights():
    assert MT._should_block_premature_dcsync("dcsync", "missing precondition(s): live-foothold:*", True, 0, MAX) is False


def test_no_block_for_non_dcsync_technique():
    assert MT._should_block_premature_dcsync("gpo-abuse", REASON, True, 0, MAX) is False
    assert MT._should_block_premature_dcsync("dcsync-rights-grant", REASON, True, 0, MAX) is False


def test_cap_yields_after_max_blocks():
    # Capped: at/after the cap it must fall through to advisory-proceed (no permanent deadlock).
    assert MT._should_block_premature_dcsync("dcsync", REASON, True, MAX - 1, MAX) is True
    assert MT._should_block_premature_dcsync("dcsync", REASON, True, MAX, MAX) is False
    assert MT._should_block_premature_dcsync("dcsync", REASON, True, MAX + 5, MAX) is False


def test_cross_domain_forge_parent_scopes_same_forest_only():
    # The cross-domain forge grants the parent DS-Replication right (via the imported parent-EA referral) ONLY
    # for a same-forest child->parent target — the parent must be a DNS suffix of the child. A cross-FOREST
    # target must get no grant (no implicit Enterprise-Admins path), so the precheck still guards it.
    import capabilities  # noqa: E402

    mt = MT.__new__(MT)

    def action(child, parent):
        return capabilities.CapabilityAction(
            name="forge-golden-ticket",
            target=f"domain={child}" + (f";target_domain={parent}" if parent else ""),
            preconditions=[], effects=[],
            intent={"capability": "forge-golden-ticket", "domain": child, **({"target_domain": parent} if parent else {})},
        )

    assert mt._cross_domain_forge_parent(action("north.sevenkingdoms.local", "sevenkingdoms.local"), {}) == "sevenkingdoms.local"
    assert mt._cross_domain_forge_parent(action("north.sevenkingdoms.local", "essos.local"), {}) == ""   # cross-forest
    assert mt._cross_domain_forge_parent(action("sevenkingdoms.local", ""), {}) == ""                    # same-domain
    other = capabilities.CapabilityAction(name="dcsync-krbtgt", target="domain=sevenkingdoms.local;account=krbtgt", preconditions=[], effects=[], intent={})
    assert mt._cross_domain_forge_parent(other, {}) == ""


def test_guidance_frames_rights_not_syntax():
    g = MT._dcsync_rights_guidance("north.sevenkingdoms.local")
    assert "north.sevenkingdoms.local" in g
    assert "8453" in g
    assert "RIGHTS problem" in g
    assert "NOT a" in g and "command" in g          # explicitly not a syntax problem (anti-misread)
