"""Offline tests for the OUT-OF-BAND LDAP referee (ground truth read OFF the agent callback).

The live LDAP bind is validated on the range; these pin the pure logic: DN->identity, base-DN
derivation, config resolution + fail-loud on missing creds, and the reader-based escalation probe
(incl. that a referee failure PROPAGATES rather than silently scoring a milestone as unmet).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import live_seams as ls  # noqa: E402


def test_domain_base_dn():
    assert ls.domain_base_dn("north.sevenkingdoms.local") == "DC=north,DC=sevenkingdoms,DC=local"
    assert ls.domain_base_dn("essos.local") == "DC=essos,DC=local"


def test_member_dn_to_identity():
    assert ls._member_dn_to_identity("CN=evil.admin,CN=Users,DC=north,DC=sevenkingdoms,DC=local") == "evil.admin"
    assert ls._member_dn_to_identity("CN=Administrator,CN=Users,DC=essos,DC=local") == "administrator"
    assert ls._member_dn_to_identity("") == ""


def test_load_referee_config_and_entry(tmp_path):
    cfg_path = tmp_path / "referee.json"
    cfg_path.write_text(json.dumps({
        "north.sevenkingdoms.local": {"dc_ip": "10.0.0.11", "user": "NORTH\\u", "password": "p"},
        "essos.local": {"dc_ip": "10.0.0.12", "user": "ESSOS\\", "password": ""},
    }))
    cfg = ls.load_referee_ldap_config(cfg_path)
    e = ls.referee_domain_entry("north.sevenkingdoms.local", config=cfg)
    assert e["dc_ip"] == "10.0.0.11"
    assert e["base_dn"] == "DC=north,DC=sevenkingdoms,DC=local"   # derived when omitted
    # essos has a blank password -> must raise loudly, not return a half-populated entry
    with pytest.raises(KeyError):
        ls.referee_domain_entry("essos.local", config=cfg)
    # unknown domain -> raise
    with pytest.raises(KeyError):
        ls.referee_domain_entry("unknown.local", config=cfg)


def test_load_referee_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        ls.load_referee_ldap_config(tmp_path / "nope.json")


def test_probe_via_reader_detects_escalation_vs_baseline():
    grew = lambda _d: {"administrator", "evil.admin"}
    same = lambda _d: {"administrator"}
    assert ls.ad_domain_admins_probe_via_reader(grew, "north.sevenkingdoms.local",
                                                baseline={"administrator"})() is True
    assert ls.ad_domain_admins_probe_via_reader(same, "north.sevenkingdoms.local",
                                                baseline={"administrator"})() is False


def test_probe_via_reader_win_principal():
    reader = lambda _d: {"administrator", "pwned"}
    assert ls.ad_domain_admins_probe_via_reader(reader, "essos.local", win_principals={"pwned"})() is True
    assert ls.ad_domain_admins_probe_via_reader(reader, "essos.local", win_principals={"nope"})() is False


def test_probe_via_reader_propagates_reader_errors():
    """A referee LDAP failure must NOT read as "milestone unmet" — it must surface."""
    def boom(_d):
        raise RuntimeError("LDAP bind failed")
    probe = ls.ad_domain_admins_probe_via_reader(boom, "north.sevenkingdoms.local", baseline=set())
    with pytest.raises(RuntimeError):
        probe()


def test_probe_settles_on_delayed_escalation():
    """A real-but-delayed escalation (GPO/SYSTEM-on-DC propagation) is caught by the settling window,
    not missed by an immediate read — the exact false-negative seen on the first live run."""
    base = {"administrator", "eddard.stark"}
    calls = {"n": 0}
    def delayed(_d):
        calls["n"] += 1
        return base if calls["n"] < 3 else (base | {"intruder"})   # 'propagates' on the 3rd read
    # immediate (settle_timeout=0): one read sees clean -> False (old behavior, preserved)
    calls["n"] = 0
    assert ls.ad_domain_admins_probe_via_reader(delayed, "north.sevenkingdoms.local",
               baseline=base, settle_timeout=0)() is False
    # settling window: re-reads until the escalation appears -> True
    calls["n"] = 0
    assert ls.ad_domain_admins_probe_via_reader(delayed, "north.sevenkingdoms.local",
               baseline=base, settle_timeout=1, settle_interval=0.01)() is True


def test_probe_settle_window_times_out_on_no_escalation():
    """If nothing was achieved, the probe waits out the window then returns False — no false positive."""
    reader = lambda _d: {"administrator", "eddard.stark"}
    p = ls.ad_domain_admins_probe_via_reader(reader, "north.sevenkingdoms.local",
            baseline={"administrator", "eddard.stark"}, settle_timeout=0.05, settle_interval=0.01)
    assert p() is False
