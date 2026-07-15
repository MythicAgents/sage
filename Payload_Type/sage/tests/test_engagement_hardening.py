"""Durable-ledger hardening tests: provenance, TTL, corroboration-gated SKIP, render marking.

Closes the stale-durable-state false-SKIP residual:
- run-provenance hops still hard-SKIP (within-run loop fix preserved);
- a durable (loaded) hop only hard-SKIPs when an INDEPENDENT live foothold corroborates its effect;
- an uncorroborated durable hop PROCEEDs (never silently skipped) so a post-redeploy stale belief can't
  suppress a real hop;
- TTL drops stale durable hops at load;
- the render marks uncorroborated durable beliefs distinctly.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state as es  # noqa: E402
import mythic_tools  # noqa: E402


def _arm_runtime_lineage(mt, task_id="450", callback_id="50", command="test-command"):
    mt._last_issued_task_display_id = task_id
    mt._last_issued_callback_id = callback_id
    mt._last_issued_task_terminal_status = "completed"
    mt._last_issued_command = command


def _state(provenance, footholds=None, target="winterfell"):
    s = es.EngagementState(objective="t", footholds=footholds or [])
    return es.record_hop_result(
        s, "gpo-abuse", target, "achieved",
        {"source": "issue_task", "provenance": provenance}, "2026-06-07T00:00:00+00:00",
    )


def _foothold(host, integrity, alive=True):
    return es.Foothold(
        callback_id="50", agent="apollo", host=host, forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly", integrity=integrity, alive=alive,
        source="reconcile", timestamp="2026-06-07T00:00:00+00:00",
    )


# --- corroboration helpers ---------------------------------------------------

def test_foothold_predicates_excludes_hops():
    s = _state("run", footholds=[_foothold("castelblack", "medium")])
    fp = es.foothold_predicates(s)
    assert "live-foothold:*" in fp
    assert "system:winterfell" not in fp  # the achieved hop must NOT corroborate itself


def test_corroborate_true_when_live_system_foothold():
    s = _state("durable", footholds=[_foothold("winterfell", "system")])
    assert es.corroborate_effect("system:winterfell", s) is True


def test_corroborate_false_without_independent_signal():
    s = _state("durable", footholds=[_foothold("castelblack", "medium")])
    assert es.corroborate_effect("system:winterfell", s) is False


# --- TTL ---------------------------------------------------------------------

def _hop_at(ts):
    s = es.record_hop_result(
        es.EngagementState(objective="t"), "gpo-abuse", "winterfell", "achieved",
        {"source": "issue_task"}, ts,
    )
    return s.hops


def test_ttl_drops_stale_keeps_fresh():
    now = "2026-06-07T00:00:00+00:00"
    old = _hop_at("2026-06-01T00:00:00+00:00")   # 6 days old
    fresh = _hop_at("2026-06-06T23:00:00+00:00")  # 1 hour old
    kept, dropped = es.filter_hops_by_ttl(old + fresh, now, 24)
    assert dropped == 1 and len(kept) == 1
    assert kept[0].timestamp == "2026-06-06T23:00:00+00:00"


def test_ttl_disabled_keeps_all():
    now = "2026-06-07T00:00:00+00:00"
    hops = _hop_at("2020-01-01T00:00:00+00:00")
    kept, dropped = es.filter_hops_by_ttl(hops, now, 0)
    assert dropped == 0 and len(kept) == 1


# --- render marking ----------------------------------------------------------

def test_render_marks_durable_unverified():
    s = _state("durable", footholds=[_foothold("castelblack", "medium")])
    out = es.render_engagement_state(s)
    assert "durable, unverified" in out


def test_render_no_mark_for_corroborated_durable():
    s = _state("durable", footholds=[_foothold("winterfell", "system")])
    out = es.render_engagement_state(s)
    assert "durable, unverified" not in out


def test_render_no_mark_for_run_hop():
    s = _state("run", footholds=[_foothold("castelblack", "medium")])
    out = es.render_engagement_state(s)
    assert "durable, unverified" not in out


# --- persistence integration: run -> persist -> load = durable --------------

def test_loaded_hop_becomes_durable_and_is_not_silently_skipped(monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "harden-test")

    mt1 = mythic_tools.MythicTools(agent_task_id="run-1")
    _arm_runtime_lineage(mt1)
    mt1._pending_engagement_hop = ("gpo-abuse", "winterfell", "2026-06-07T00:00:00+00:00")
    mt1._record_engagement_success("whoami\r\nnt authority\\system\r\n")
    # run hop on disk
    assert mt1._engagement_hops[0].evidence.get("provenance") == "run"

    # New process loads it -> tagged durable.
    mt2 = mythic_tools.MythicTools(agent_task_id="run-2")
    assert mt2._engagement_hops, "ledger should have loaded"
    assert mt2._engagement_hops[0].evidence.get("provenance") == "durable"


def test_footholds_are_never_persisted_so_corroboration_is_live_only(monkeypatch):
    # Advisor (a): the highest-priority independence check. The durable ledger must persist ONLY hops,
    # never footholds — otherwise a stale replayed foothold could corroborate a stale hop (the original
    # bug, back at the persistence layer). Corroboration draws on self._engagement_footholds, which is
    # set ONLY from live reconcile_access; this proves it is never written to / read from disk.
    import json
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "indep-test")
    mt = mythic_tools.MythicTools(agent_task_id="r1")
    _arm_runtime_lineage(mt)
    mt._engagement_footholds = [_foothold("winterfell", "system")]  # live cache — must NOT be persisted
    mt._pending_engagement_hop = ("gpo-abuse", "winterfell", "2026-06-07T00:00:00+00:00")
    mt._record_engagement_success("whoami\r\nnt authority\\system\r\n")
    payload = json.loads(Path(mt._engagement_ledger_path()).read_text())
    assert "footholds" not in payload
    assert all("footholds" not in h for h in payload["hops"])
    # A fresh load starts with NO footholds (live reconcile repopulates them at gate time).
    mt2 = mythic_tools.MythicTools(agent_task_id="r2")
    assert mt2._engagement_footholds == []


def test_durable_to_run_upgrade_on_reachieve():
    # Re-achieving a durable hop UPGRADES provenance to run and REPLACES (not duplicates) the hop.
    s = _state("durable", footholds=[_foothold("castelblack", "medium")])
    s2 = es.record_hop_result(
        s, "gpo-abuse", "winterfell", "achieved",
        {"source": "issue_task", "provenance": "run"}, "2026-06-07T01:00:00+00:00",
    )
    assert len(s2.hops) == 1  # replaced, not duplicated
    assert es._hop_provenance(s2.hops[0]) == "run"


def test_ttl_keeps_hop_with_missing_timestamp():
    # Advisor (c2): unparseable/legacy timestamp must fail-open to KEEP (drop is safe but a throw breaks load).
    hops = es.hops_from_dicts([{"technique": "gpo-abuse", "target": "x", "status": "achieved", "timestamp": ""}])
    kept, dropped = es.filter_hops_by_ttl(hops, "2026-06-07T00:00:00+00:00", 24)
    assert dropped == 0 and len(kept) == 1


def test_ttl_naive_timestamp_treated_as_utc_no_crash():
    # Advisor (c1): a naive (tz-less) timestamp must not crash the tz-aware comparison; treat as UTC.
    old = es.hops_from_dicts([{"technique": "gpo-abuse", "target": "x", "status": "achieved",
                               "timestamp": "2026-06-01T00:00:00"}])
    kept, dropped = es.filter_hops_by_ttl(old, "2026-06-07T00:00:00+00:00", 24)
    assert dropped == 1 and kept == []


def test_record_attaches_mythic_task_id(monkeypatch, tmp_path):
    # The recorded hop's evidence must capture the Mythic task display_id that proved the effect.
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "task-id-test")
    mt = mythic_tools.MythicTools(agent_task_id="r1")
    _arm_runtime_lineage(mt, task_id=2712)
    mt._pending_engagement_hop = ("gpo-abuse", "winterfell", "2026-06-07T00:00:00+00:00")
    mt._record_engagement_success("whoami\r\nnt authority\\system\r\n")
    assert mt._engagement_hops[0].evidence.get("mythic_task_id") == "2712"
    assert mt._engagement_hops[0].evidence.get("callback_id") == "50"


def test_ttl_drops_at_load(monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "harden-ttl")
    monkeypatch.setenv("SAGE_ENGAGEMENT_HOP_TTL_HOURS", "0.0001")  # ~0.36s — anything on disk is stale

    mt1 = mythic_tools.MythicTools(agent_task_id="ttl-1")
    _arm_runtime_lineage(mt1)
    mt1._pending_engagement_hop = ("gpo-abuse", "winterfell", "2020-01-01T00:00:00+00:00")
    mt1._record_engagement_success("whoami\r\nnt authority\\system\r\n")
    # The persisted hop carries the old timestamp; a tiny TTL expires it at load.
    mt2 = mythic_tools.MythicTools(agent_task_id="ttl-2")
    assert mt2._engagement_hops == []  # dropped as stale
