"""Durable-hop re-verify-by-probe (not re-run) tests.

Russel's correction (2026-06-09): "re-verify that something in the ledger is complete" must mean
READ-PROBE whether the artifact is still present — NOT re-run the entire attack. Bug it fixes: after a
Sage restart the gpo-abuse hop reloaded as a durable belief, couldn't be corroborated (its effect
system:{gpo} maps to no live foothold/graph predicate), and the gate re-ran SharpGPOAbuse "to verify".

New gate semantics for a durable (loaded-from-disk) achieved hop:
  - corroborated by a live signal            -> SKIP (verified present)
  - effect was PROBED this turn but ABSENT   -> PROCEED (artifact genuinely gone -> re-run is legit)
  - no probe applies to this effect          -> SKIP (trust ledger; do NOT re-run the attack to verify)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state as es  # noqa: E402

NOW = "2026-06-09T12:00:00Z"


def _fh(integrity="medium", host="CASTELBLACK"):
    return es.Foothold(callback_id="3", agent="merlin", host=host, forest="north.sevenkingdoms.local",
                       identity="NORTH\\samwell.tarly", integrity=integrity, alive=True, source="t", timestamp=NOW)


def _hop(technique, target, effect, provenance="durable"):
    return es.Hop(id=f"{technique}:{target}", technique=technique, target=target, effect=effect,
                  status="achieved", evidence={"provenance": provenance}, preconditions=[],
                  satisfied_effects=[effect], source="ledger", timestamp=NOW)


def test_durable_unprobed_effect_is_trusted_not_rerun():
    # THE FIX: gpo-abuse reloaded as durable, no probe for its 'system:{gpo}' effect -> trust, do NOT re-run.
    st = es.EngagementState(objective="x", footholds=[_fh()],
                            hops=[_hop("gpo-abuse", "starkwallpaper", "system:starkwallpaper")])
    d, r = es.gate_decision("gpo-abuse", "starkwallpaper", st)
    assert d == es.GateDecision.SKIP
    assert "durable, unprobed" in r and "do NOT re-run" in r


def test_durable_probed_but_absent_reruns():
    # lsass-dump durable; we probed the cred store ('creds' in probed) and the secret is ABSENT -> the
    # result is genuinely gone, so re-running is legitimate. (SYSTEM foothold satisfies the precondition.)
    st = es.EngagementState(objective="x", footholds=[_fh(integrity="system")],
                            hops=[_hop("lsass-dump", "castelblack", "creds:castelblack")],
                            probed_effect_prefixes={"creds", "krbtgt-hash"})
    d, r = es.gate_decision("lsass-dump", "castelblack", st)
    assert d == es.GateDecision.PROCEED
    assert "durable, unprobed" not in r


def test_durable_probed_and_present_skips():
    # Same, but the cred-store probe injected the artifact (creds:castelblack) -> corroborated -> SKIP.
    fact = es.GraphFact("creds:castelblack", "live-probe:creds", NOW, 600)
    st = es.EngagementState(objective="x", footholds=[_fh(integrity="system")],
                            hops=[_hop("lsass-dump", "castelblack", "creds:castelblack")],
                            graph_facts=[fact], probed_effect_prefixes={"creds", "krbtgt-hash"})
    d, r = es.gate_decision("lsass-dump", "castelblack", st)
    assert d == es.GateDecision.SKIP
    assert "corroborated" in r


def test_run_provenance_still_hard_skips():
    # A hop achieved THIS process (run provenance) is trustworthy -> hard SKIP, unchanged.
    st = es.EngagementState(objective="x", footholds=[_fh()],
                            hops=[_hop("gpo-abuse", "starkwallpaper", "system:starkwallpaper", provenance="run")])
    d, _ = es.gate_decision("gpo-abuse", "starkwallpaper", st)
    assert d == es.GateDecision.SKIP
