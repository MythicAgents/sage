"""Pure-helper tests for the live seams (the error-prone parsing the lab adapters depend on).

The lab-touching factories (make_model_fn/tool_executor/cypher_run) are validated on the range, not
here. These pin the parsing: LLM decision, BloodHound literals, and the domain-count read.
"""
import sys
import base64
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import live_seams as ls  # noqa: E402
import credential_artifacts as ca  # noqa: E402


CHILD = "north.sevenkingdoms.local"
GOOD_NT = "c1a2b3c4d5e6f708192a3b4c5d6e7f80"


class _Msg:
    def __init__(self, tool_calls=None, content=""):
        self.tool_calls = tool_calls or []
        self.content = content


def test_parse_model_decision_tool_call():
    d = ls.parse_model_decision(_Msg(tool_calls=[{"name": "shell", "args": {"command": "whoami"}}]))
    assert d == {"tool": "shell", "args": {"command": "whoami"}}


def test_parse_model_decision_final():
    assert ls.parse_model_decision(_Msg(content="objective complete")) == {"final": "objective complete"}


def test_extract_literals():
    resp = {"data": {"literals": [{"value": "sevenkingdoms.local"}, {"value": "essos.local"}, {"k": "x"}]}}
    assert ls.extract_literals(resp) == ["sevenkingdoms.local", "essos.local"]
    assert ls.extract_literals("garbage") == []
    assert ls.extract_literals({"data": {}}) == []


def test_parse_domain_count():
    assert ls.parse_domain_count("available-domains: 200 count=3 -> ['a','b','c']") == 3
    assert ls.parse_domain_count("available-domains: 200 count=0 -> []") == 0
    assert ls.parse_domain_count("connection refused") == 0


def test_load_sage_defaults(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# Local Sage defaults\nSAGE_PROVIDER=OpenAI\nSAGE_MODEL=gpt-5.5-cyber-preview\n"
                   "SAGE_API_ENDPOINT=http://127.0.0.1:8100/v1\nSAGE_API_KEY=sk-abc\n")
    d = ls.load_sage_defaults(str(env))
    assert d["provider"] == "openai"            # lowercased for init_chat_model
    assert d["model"] == "gpt-5.5-cyber-preview"
    assert d["base_url"] == "http://127.0.0.1:8100/v1"
    assert d["api_key"] == "sk-abc"


def test_apollo_tools_spec_shape():
    spec = ls.apollo_tools_spec([{"cmd": "shell", "description": "run a shell command"}, {"cmd": "whoami"}])
    assert spec[0]["type"] == "function"
    assert spec[0]["function"]["name"] == "shell"
    assert "command_line" in spec[0]["function"]["parameters"]["properties"]


def test_apollo_tools_spec_curated_fallback():
    names = {t["function"]["name"] for t in ls.apollo_tools_spec([])}  # empty -> curated set
    assert "shell" in names and "whoami" in names


_NET_GROUP_OUT = """Group name     Domain Admins
Comment        Designated administrators of the domain

Members

-------------------------------------------------------------------------------
Administrator            samwell.tarly            evil.admin
The command completed successfully.
"""


def test_parse_net_group_members():
    assert ls.parse_net_group_members(_NET_GROUP_OUT) == {"administrator", "samwell.tarly", "evil.admin"}
    assert ls.parse_net_group_members("connection refused") == set()


def test_ad_probe_detects_escalation_vs_baseline():
    grew = lambda call: "Members\n----\nAdministrator  evil.admin\nThe command completed successfully."
    same = lambda call: "Members\n----\nAdministrator\nThe command completed successfully."
    assert ls.ad_domain_admins_probe(grew, "north", baseline={"administrator"})() is True
    assert ls.ad_domain_admins_probe(same, "north", baseline={"administrator"})() is False


def test_ad_probe_win_principal():
    tasker = lambda call: "Members\n----\nAdministrator  pwned\nThe command completed successfully."
    assert ls.ad_domain_admins_probe(tasker, "north", win_principals={"pwned"})() is True
    assert ls.ad_domain_admins_probe(tasker, "north", win_principals={"nope"})() is False


def test_krbtgt_probe_store_hit_short_circuits_task_fetch(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [
        {"account": "NORTH\\krbtgt$", "realm": CHILD},
    ])

    def unexpected_task_fetch(**kw):
        raise AssertionError("task-output path should not run after a store hit")

    monkeypatch.setattr(ls, "_fetch_krbtgt_dcsync_task_outputs", unexpected_task_fetch)
    assert ls.krbtgt_dumped_probe(realm=CHILD)() is True


def test_krbtgt_probe_task_output_real_secretsdump_material(monkeypatch):
    assert GOOD_NT not in ca._DEGENERATE_HASHES
    assert set(GOOD_NT) != {"0"}
    output = f"krbtgt:502:aad3b435b51404eeaad3b435b51404ee:{GOOD_NT}:::"
    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [])
    monkeypatch.setattr(ls, "_fetch_krbtgt_dcsync_task_outputs", lambda **kw: [
        {"output": output, "realm": ""},
    ])

    assert ls.krbtgt_dumped_probe(realm=CHILD)() is True


def test_krbtgt_probe_rejects_degenerate_task_hash(monkeypatch):
    output = (
        "krbtgt:502:aad3b435b51404eeaad3b435b51404ee:"
        "31d6cfe0d16ae931b73c59d7e0c089c0:::"
    )
    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [])
    monkeypatch.setattr(ls, "_fetch_krbtgt_dcsync_task_outputs", lambda **kw: [
        {"output": output, "realm": CHILD},
    ])

    assert ls.krbtgt_dumped_probe(realm=CHILD)() is False


def test_krbtgt_probe_empty_store_and_no_task_material_false(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [])
    monkeypatch.setattr(ls, "_fetch_krbtgt_dcsync_task_outputs", lambda **kw: [
        {"output": "dcsync completed but no secret material", "realm": CHILD},
    ])

    assert ls.krbtgt_dumped_probe(realm=CHILD)() is False


def test_krbtgt_realm_netbios_and_fqdn_match(monkeypatch):
    assert ls._realms_match("NORTH", CHILD) is True
    assert ls._realms_match(CHILD, "NORTH") is True

    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [
        {"account": "NORTH\\krbtgt", "realm": "NORTH"},
    ])
    assert ls.mythic_credential_probe("krbtgt", realm=CHILD)() is True

    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [
        {"account": "krbtgt@north.sevenkingdoms.local", "realm": CHILD},
    ])
    assert ls.mythic_credential_probe("krbtgt", realm="NORTH")() is True


def test_krbtgt_dumped_probe_callable_no_live_range_no_hang(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [])
    monkeypatch.setattr(ls, "_fetch_krbtgt_dcsync_task_outputs", lambda **kw: [])
    probe = ls.krbtgt_dumped_probe(realm=CHILD)

    start = time.monotonic()
    assert probe() is False
    assert time.monotonic() - start < 1.0


def test_certificate_admin_control_probe_replays_multi_task_proof(monkeypatch):
    ticket = base64.b64encode(b"A" * 80).decode("ascii")
    rows = [
        {
            "display_id": 52,
            "callback_display_id": 2,
            "output": (
                "SAGE_CERT_AUTH_PROOF_administrator_essos_local_2\n"
                " Directory of \\\\braavos.essos.local\\C$\nWindows"
            ),
        },
        {
            "display_id": 51,
            "callback_display_id": 2,
            "output": f"[*] Action: Ask TGT\n[*] base64(ticket.kirbi):\n{ticket}",
        },
    ]
    monkeypatch.setattr(ls, "_fetch_certificate_auth_task_outputs", lambda **kw: rows)

    assert ls.certificate_admin_control_probe(
        "administrator",
        realm="essos.local",
    )() is True


def test_certificate_admin_control_probe_rejects_other_realm(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_certificate_auth_task_outputs", lambda **kw: [{
        "display_id": 52,
        "callback_display_id": 2,
        "output": (
            "SAGE_CERT_AUTH_PROOF_administrator_north_sevenkingdoms_local_2\n"
            "CERT_AUTH_STATUS=OK"
        ),
    }])

    assert ls.certificate_admin_control_probe(
        "administrator",
        realm="essos.local",
    )() is False


def _golden_ticket_admin_task_rows():
    return [
        {
            "display_id": 18,
            "callback_display_id": 2,
            "output": " Directory of \\\\DC01.RANGE.LOCAL\\C$\nWindows",
        },
        {
            "display_id": 17,
            "callback_display_id": 2,
            "output": "Cached Tickets: (1)\nServer: krbtgt/RANGE.LOCAL @ RANGE.LOCAL",
        },
        {
            "display_id": 16,
            "callback_display_id": 2,
            "output": "ticket_store_add completed",
        },
        {
            "display_id": 15,
            "callback_display_id": 2,
            "output": "make_token completed",
        },
        {
            "display_id": 14,
            "callback_display_id": 2,
            "output": "[*] Action: Build TGT\n[*] Building PAC",
        },
        {
            "display_id": 13,
            "callback_display_id": 2,
            "output": " Directory of \\\\DC01.RANGE.LOCAL\\C$\nAccess is denied.",
        },
    ]


def test_ticket_admin_control_probe_replays_from_forge_after_denied_preflight(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_certificate_auth_task_outputs", lambda **kw: _golden_ticket_admin_task_rows())

    assert ls.ticket_admin_control_probe(realm="range.local")() is True


def test_ticket_admin_control_probe_rejects_other_realm(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_certificate_auth_task_outputs", lambda **kw: _golden_ticket_admin_task_rows())

    assert ls.ticket_admin_control_probe(realm="essos.local")() is False


def test_ticket_admin_control_probe_rejects_listing_without_forge(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_certificate_auth_task_outputs", lambda **kw: _golden_ticket_admin_task_rows()[:2])

    assert ls.ticket_admin_control_probe(realm="range.local")() is False


def test_any_probe_retries_all_paths_within_one_shared_settle_window(monkeypatch):
    calls = {"certificate": 0, "ldap": 0}

    def delayed_certificate():
        calls["certificate"] += 1
        return calls["certificate"] >= 2

    def unchanged_ldap():
        calls["ldap"] += 1
        return False

    monkeypatch.setattr(ls.time, "sleep", lambda _seconds: None)

    assert ls.any_probe(
        delayed_certificate,
        unchanged_ldap,
        settle_timeout=1,
        settle_interval=1,
    )() is True
    assert calls == {"certificate": 2, "ldap": 1}


def test_decode_mythic_response_rows_base64_with_raw_fallback():
    encoded = base64.b64encode(b"decoded output").decode("ascii")
    assert ls._decode_mythic_response_rows([{"response_text": encoded}]) == "decoded output"
    assert ls._decode_mythic_response_rows([
        {"response_text": "not-base64", "response": "raw output"},
    ]) == "raw output"


def test_krbtgt_dcsync_task_realm_parses_native_and_mimikatz():
    native = {
        "command_name": "dcsync",
        "original_params": '{"Domain":"north.sevenkingdoms.local","User":"krbtgt"}',
    }
    mimikatz = {
        "command_name": "execute_pe",
        "display_params": "lsadump::dcsync /domain:essos.local /user:ESSOS\\krbtgt",
    }
    assert ls._krbtgt_dcsync_task_realm(native) == CHILD
    assert ls._krbtgt_dcsync_task_realm(mimikatz) == "essos.local"
