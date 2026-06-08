"""User-specific DCSync vs domain krbtgt DCSync.

Regression for the 2026-06-07 essos run: the agent tried to DCSync `lord.varys` (a SMALL COUNCIL member, to
forge his REAL TGT for the cross-forest LAPS read) but the gate SKIPped it — a user DCSync classified
identically to the domain krbtgt DCSync (`("dcsync", domain)` → `krbtgt-hash:{domain}`), which was already
achieved. Fix: a distinct `dcsync-user` technique keyed on the target user, effect `creds:{user@domain}`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state as es  # noqa: E402
import intent_classifier as ic  # noqa: E402

GD = es.GateDecision
DOM = "sevenkingdoms.local"
TS = "2026-06-07T00:00:00+00:00"


def test_classifier_user_dcsync_distinct_from_krbtgt():
    assert ic.classify_tool_call("execute_pe", {"Commands": [f"lsadump::dcsync /domain:{DOM} /user:krbtgt"]}) == ("dcsync", DOM)
    assert ic.classify_tool_call("execute_pe", {"Commands": [f"lsadump::dcsync /domain:{DOM} /user:cersei.lannister"]}) == ("dcsync-user", f"cersei.lannister@{DOM}")


def test_classifier_apollo_native_user_dcsync():
    t = ic.classify_tool_call("dcsync", {"Domain": DOM, "User": "lord.varys", "DC": "kingslanding.sevenkingdoms.local"})
    assert t == ("dcsync-user", f"lord.varys@{DOM}")


def test_model_user_effect_and_precondition():
    assert es._technique_effect("dcsync-user", f"cersei.lannister@{DOM}") == f"creds:cersei.lannister@{DOM}"
    assert es._technique_preconditions("dcsync-user", f"cersei.lannister@{DOM}") == [f"ds-replication-rights:{DOM}"]


def _da_state():
    # da:sevenkingdoms.local (via SID-history) → implies ds-replication-rights:sevenkingdoms.local; krbtgt dumped.
    s = es.EngagementState(objective="t")
    s = es.record_hop_result(s, "sid-history-escalation", f"north.{DOM}", "achieved", {"provenance": "run"}, TS)
    s = es.record_hop_result(s, "dcsync", DOM, "achieved", {"provenance": "run"}, TS)
    return s


def test_user_dcsync_proceeds_after_krbtgt_dumped():
    # The bug: once krbtgt:DOM was achieved the gate SKIPped a user DCSync. It must PROCEED now.
    d, reason = es.gate_decision("dcsync-user", f"cersei.lannister@{DOM}", _da_state())
    assert d == GD.PROCEED, reason


def test_user_dcsync_skips_only_that_user_once_done():
    s = _da_state()
    s = es.record_hop_result(s, "dcsync-user", f"cersei.lannister@{DOM}", "achieved", {"provenance": "run"}, TS)
    assert es.gate_decision("dcsync-user", f"cersei.lannister@{DOM}", s)[0] == GD.SKIP      # same user → skip
    assert es.gate_decision("dcsync-user", f"lord.varys@{DOM}", s)[0] == GD.PROCEED          # other user → proceed
