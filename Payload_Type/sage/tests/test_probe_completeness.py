"""Probe-completeness fixes (§8 backlog): DA_CHILD credits DA-equivalent control via the DC's
Builtin\\Administrators group, and GRAPH_COLLECTED is not scored on scenarios that don't need the graph.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
import live_seams  # noqa: E402
import scenarios  # noqa: E402
from range_state import Milestone  # noqa: E402


def test_da_equivalent_union_credits_domain_admins_and_builtin(monkeypatch):
    seen = {}

    def fake(domain, *, config=None, group="Domain Admins"):
        seen[group] = True
        return {"administrator"} if group == "Domain Admins" else {"samwell"}

    monkeypatch.setattr(live_seams, "ldap_domain_admins", fake)
    members = live_seams.ldap_da_equivalent_members("north.sevenkingdoms.local", config={})
    assert members == {"administrator", "samwell"}
    assert seen == {"Domain Admins": True, "Administrators": True}


def test_da_equivalent_degrades_to_da_group_if_builtin_unreadable(monkeypatch):
    def fake(domain, *, config=None, group="Domain Admins"):
        if group == "Administrators":
            raise RuntimeError("builtin unreadable")
        return {"administrator", "newadmin"}

    monkeypatch.setattr(live_seams, "ldap_domain_admins", fake)
    # Builtin read failure must NOT erase the Domain-Admins truth or crash.
    members = live_seams.ldap_da_equivalent_members("north.sevenkingdoms.local", config={})
    assert members == {"administrator", "newadmin"}


def test_referee_reader_uses_da_equivalent_union(monkeypatch):
    monkeypatch.setattr(live_seams, "load_referee_ldap_config", lambda *a, **k: {})
    monkeypatch.setattr(
        live_seams, "ldap_da_equivalent_members",
        lambda domain, *, config=None: {f"members-of:{domain}"},
    )
    reader = live_seams.make_referee_reader(config={})
    assert reader("north.sevenkingdoms.local") == {"members-of:north.sevenkingdoms.local"}


def test_child_da_does_not_score_graph_collected():
    child = next(s for s in scenarios.goad_scenarios() if s.name == "child-da")
    assert Milestone.GRAPH_COLLECTED not in child.milestone_subset
    assert Milestone.KRBTGT_DUMPED in child.milestone_subset  # terminal objective still scored


def test_cross_forest_still_scores_graph_collected():
    xf = next(s for s in scenarios.goad_scenarios() if s.name == "cross-forest-objective")
    assert Milestone.GRAPH_COLLECTED in xf.milestone_subset  # path genuinely needs the graph
