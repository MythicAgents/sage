"""User-specific DCSync vs domain krbtgt DCSync.

A user DCSync (e.g. `lord.varys`, to forge that user's REAL TGT) must not classify identically to
the domain krbtgt DCSync (`("dcsync", domain)` → `krbtgt-hash:{domain}`); otherwise, once the
krbtgt hash is achieved, the gate SKIPs it. A distinct `dcsync-user` technique is keyed on the
target user, with effect `creds:{user@domain}`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state as es  # noqa: E402
import intent_classifier as ic  # noqa: E402

DOM = "sevenkingdoms.local"


def test_classifier_user_dcsync_distinct_from_krbtgt():
    assert ic.classify_tool_call("execute_pe", {"Commands": [f"lsadump::dcsync /domain:{DOM} /user:krbtgt"]}) == ("dcsync", DOM)
    assert ic.classify_tool_call("execute_pe", {"Commands": [f"lsadump::dcsync /domain:{DOM} /user:cersei.lannister"]}) == ("dcsync-user", f"cersei.lannister@{DOM}")


def test_classifier_handles_quoted_mimikatz_dcsync_command():
    quoted = f'"lsadump::dcsync /domain:{DOM} /user:krbtgt"'
    assert ic.classify_tool_call("mimikatz", {"arguments": quoted}) == ("dcsync", DOM)


def test_classifier_apollo_native_user_dcsync():
    t = ic.classify_tool_call("dcsync", {"Domain": DOM, "User": "lord.varys", "DC": "kingslanding.sevenkingdoms.local"})
    assert t == ("dcsync-user", f"lord.varys@{DOM}")


def test_model_user_effect_and_precondition():
    assert es._technique_effect("dcsync-user", f"cersei.lannister@{DOM}") == f"creds:cersei.lannister@{DOM}"
    assert es._technique_preconditions("dcsync-user", f"cersei.lannister@{DOM}") == [f"ds-replication-rights:{DOM}"]
