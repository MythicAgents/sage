import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import intent_classifier  # noqa: E402


def test_sharpgpoabuse_cli_extracts_gpo_name_case_insensitive():
    assert intent_classifier.classify_tool_call(
        "execute_assembly",
        "--Assembly SharpGPOAbuse.exe --GPOName CastleBlack",
    ) == ("gpo-abuse", "castleblack")


def test_sharpgpoabuse_missing_gpo_name_returns_empty_target():
    assert intent_classifier.classify_tool_call(
        "SharpGPOAbuse",
        {"arguments": "--AddComputerTask --TaskName run"},
    ) == ("gpo-abuse", "")


def test_standin_grant_domain_dn_json_string_derives_fqdn():
    params = '{"arguments": "StandIn --object CN=Domain,CN=System,DC=Essos,DC=Local --grant user"}'
    assert intent_classifier.classify_tool_call("execute_assembly", params) == (
        "dcsync-rights-grant",
        "essos.local",
    )


def test_standin_guid_domain_dn_dict_derives_fqdn():
    assert intent_classifier.classify_tool_call(
        "standin",
        {"object": "CN=foo,DC=SEVENKINGDOMS,DC=LOCAL", "guid": "abc"},
    ) == ("dcsync-rights-grant", "sevenkingdoms.local")


def test_standin_grant_prefixed_dn_keeps_first_component():
    # 2026-06-09 BUG: a DN embedded behind a prefix ("distinguishedname=DC=north,...") had its FIRST
    # label dropped by the old (?:^|,) anchor -> the NORTH grant was filed under sevenkingdoms.local.
    assert intent_classifier.classify_tool_call(
        "standin",
        {"object": "distinguishedname=DC=north,DC=sevenkingdoms,DC=local", "grant": "samwell.tarly"},
    ) == ("dcsync-rights-grant", "north.sevenkingdoms.local")


def test_fqdn_from_dn_prefixed_and_plain():
    assert intent_classifier._fqdn_from_dn("DC=north,DC=sevenkingdoms,DC=local") == "north.sevenkingdoms.local"
    assert intent_classifier._fqdn_from_dn("distinguishedname=DC=north,DC=sevenkingdoms,DC=local") \
        == "north.sevenkingdoms.local"
    assert intent_classifier._fqdn_from_dn("CN=foo,DC=Essos,DC=Local") == "Essos.Local"
    assert intent_classifier._fqdn_from_dn("no-domain-here") == ""


def test_standin_rbcd_colon_form_extracts_target():
    assert intent_classifier.classify_tool_call(
        "execute_assembly",
        "StandIn --rbcd --target:WINTERFELL.NORTH.LOCAL",
    ) == ("rbcd-standin", "winterfell.north.local")


def test_mimikatz_lsadump_dcsync_extracts_slash_domain():
    assert intent_classifier.classify_tool_call(
        "mimikatz",
        "lsadump::dcsync /domain:ESSOS.LOCAL /user:krbtgt",
    ) == ("dcsync", "essos.local")


def test_apollo_dcsync_command_extracts_dict_domain():
    assert intent_classifier.classify_tool_call(
        "dcsync",
        {"domain": "NORTH.SEVENKINGDOMS.LOCAL"},
    ) == ("dcsync", "north.sevenkingdoms.local")


def test_dcsync_krbtgt_dn_target_is_domain_krbtgt_not_user_dcsync():
    assert intent_classifier.classify_tool_call(
        "dcsync",
        {
            "domain": "north.sevenkingdoms.local",
            "user": "CN=krbtgt,CN=Users,DC=north,DC=sevenkingdoms,DC=local",
            "dc": "winterfell.north.sevenkingdoms.local",
        },
    ) == ("dcsync", "north.sevenkingdoms.local")


def test_dcsync_domain_qualified_krbtgt_is_domain_krbtgt_not_user_dcsync():
    assert intent_classifier.classify_tool_call(
        "execute_pe",
        {"Commands": ["lsadump::dcsync /domain:north.sevenkingdoms.local /user:NORTH\\krbtgt"]},
    ) == ("dcsync", "north.sevenkingdoms.local")


def test_dcsync_order_wins_over_bare_golden_token():
    assert intent_classifier.classify_tool_call(
        "mimikatz",
        "lsadump::dcsync /domain:ESSOS.LOCAL golden",
    ) == ("dcsync", "essos.local")


def test_kerberos_golden_extracts_domain():
    assert intent_classifier.classify_tool_call(
        "mimikatz",
        "kerberos::golden --domain ESSOS.LOCAL",
    ) == ("golden-ticket", "essos.local")


def test_bare_golden_token_extracts_domain_from_cli():
    assert intent_classifier.classify_tool_call(
        "rubeus",
        "golden /domain:SevenKingdoms.Local",
    ) == ("golden-ticket", "sevenkingdoms.local")


def test_net_domain_admins_read_is_membership_check_not_gpo_add():
    assert intent_classifier.classify_tool_call(
        "run",
        'net group "Domain Admins" /domain',
    ) == ("domain-admin-membership-check", "")
    assert intent_classifier.classify_tool_call(
        "run",
        'net group "Domain Admins" alice /add /domain',
    ) is None


def test_lsass_dump_nanodump_extracts_host():
    assert intent_classifier.classify_tool_call(
        "nanodump",
        "--computer KINGSLANDING",
    ) == ("lsass-dump", "kingslanding")


def test_lsass_dump_sekurlsa_without_host_returns_empty_target():
    assert intent_classifier.classify_tool_call(
        "mimikatz",
        "sekurlsa::logonpasswords",
    ) == ("lsass-dump", "")


def test_lsass_dump_comsvcs_minidump_indicator():
    assert intent_classifier.classify_tool_call(
        "shell",
        "rundll32.exe C:\\Windows\\System32\\comsvcs.dll MiniDump lsass.exe C:\\temp\\lsass.dmp full --host MEEREEN",
    ) == ("lsass-dump", "meereen")


def test_none_and_garbage_inputs_do_not_raise_and_return_none():
    assert intent_classifier.classify_tool_call(None, None) is None
    assert intent_classifier.classify_tool_call("whoami", object()) is None


def test_unmodeled_call_returns_none():
    assert intent_classifier.classify_tool_call("whoami", "") is None


def test_lsass_dump_falls_back_to_callback_host_when_args_lack_host():
    # nanodump with no host in args -> empty target (the false-DEFER bug)
    assert intent_classifier.classify_tool_call("nanodump", {"arguments": "--write C:\\t.dmp"}) == ("lsass-dump", "")
    # with the callback's host supplied, it binds the host so the precondition can resolve
    assert intent_classifier.classify_tool_call(
        "nanodump", {"arguments": "--write C:\\t.dmp"}, callback_host="CASTELBLACK"
    ) == ("lsass-dump", "castelblack")
    # an explicit host in args still wins over the callback fallback
    assert intent_classifier.classify_tool_call(
        "nanodump", "--host WINTERFELL", callback_host="CASTELBLACK"
    ) == ("lsass-dump", "winterfell")
    # callback_host must NOT leak into non-host-scoped techniques (dcsync keys on domain)
    assert intent_classifier.classify_tool_call(
        "dcsync", "/domain:essos.local /user:krbtgt", callback_host="CASTELBLACK"
    ) == ("dcsync", "essos.local")
