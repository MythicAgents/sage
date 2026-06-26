import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import auth_context  # noqa: E402


def test_context_filters_tickets_to_current_luid_and_accepts_current_luid_domain_tgt():
    context = auth_context.build_authentication_context(
        2,
        "BRAAVOS",
        "Local Identity: BRAAVOS\\Administrator\nImpersonation Identity: BRAAVOS\\Administrator",
        """[
          {"client_name":"administrator","client_realm":"ESSOS.LOCAL",
           "service_name":"krbtgt/ESSOS.LOCAL","luid":"0x123","current_luid":"0x123"},
          {"client_name":"samwell","client_realm":"NORTH.LOCAL",
           "service_name":"krbtgt/NORTH.LOCAL","luid":"0x999","current_luid":"0x123"}
        ]""",
    )

    assert context.current_luid == "0x123"
    assert len(context.current_luid_tickets) == 1
    assert context.has_domain_token is False
    assert context.has_domain_tgt is True
    assert context.domain_capable is True


def test_context_accepts_domain_netonly_impersonation_without_cached_tgt():
    context = auth_context.build_authentication_context(
        2,
        "BRAAVOS",
        "Local Identity: BRAAVOS\\Administrator\nImpersonation Identity: ESSOS\\administrator",
        "0x456\n",
        {"essos"},
    )

    assert context.current_luid == "0x456"
    assert context.has_domain_token is True
    assert context.domain_capable is True


def test_context_rejects_remote_machine_local_token_without_current_luid_tgt():
    context = auth_context.build_authentication_context(
        2,
        "CASTELBLACK",
        "Local Identity: NORTH\\samwell.tarly\nImpersonation Identity: BRAAVOS\\Administrator",
        "0x32fab4\n",
        {"north", "north.sevenkingdoms.local", "sevenkingdoms.local"},
    )

    assert context.has_domain_token is False
    assert context.has_domain_tgt is False
    assert context.domain_capable is False


def test_context_retains_observed_domain_authority_without_cached_tgt():
    first = auth_context.build_authentication_context(
        2,
        "CASTELBLACK",
        "Local Identity: NORTH\\samwell.tarly\nImpersonation Identity: NORTH\\samwell.tarly",
        """[{"client_name":"samwell","client_realm":"NORTH.SEVENKINGDOMS.LOCAL",
        "service_name":"krbtgt/NORTH.SEVENKINGDOMS.LOCAL","luid":"0x123","current_luid":"0x123"}]""",
    )
    second = auth_context.build_authentication_context(
        2,
        "CASTELBLACK",
        "Local Identity: NORTH\\samwell.tarly\nImpersonation Identity: NORTH\\samwell.tarly",
        "0x456\n",
        set(first.known_domain_authorities),
    )

    assert second.has_domain_token is True
    assert second.has_domain_tgt is False
    assert second.domain_capable is True


def test_context_rejects_local_token_with_only_other_luid_domain_tickets():
    context = auth_context.build_authentication_context(
        2,
        "BRAAVOS",
        "Local Identity: BRAAVOS\\Administrator\nImpersonation Identity: BRAAVOS\\Administrator",
        """[{"client_name":"administrator","client_realm":"ESSOS.LOCAL",
        "service_name":"krbtgt/ESSOS.LOCAL","luid":"0x999","current_luid":"0x123"}]""",
    )

    assert context.current_luid_tickets == ()
    assert context.domain_capable is False


def test_context_decodes_mythic_task_wrapper_bytes_repr():
    context = auth_context.build_authentication_context(
        2,
        "CASTELBLACK",
        "b'Local Identity: NORTH\\\\samwell.tarly\\n"
        "Impersonation Identity: NORTH\\\\samwell.tarly'",
        "b'[{\"client_name\":\"samwell.tarly\","
        "\"client_realm\":\"NORTH.SEVENKINGDOMS.LOCAL\","
        "\"service_name\":\"krbtgt\\\\/NORTH.SEVENKINGDOMS.LOCAL\","
        "\"luid\":\"0x80c3e\",\"current_luid\":\"0x80c3e\"}]'",
    )

    assert context.active_identity == "NORTH\\samwell.tarly"
    assert context.current_luid == "0x80c3e"
    assert context.domain_capable is True
