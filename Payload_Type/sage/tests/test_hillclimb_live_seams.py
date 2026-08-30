"""Pure-helper tests for the live seams (the error-prone parsing the lab adapters depend on).

The lab-touching factories (make_model_fn/tool_executor/cypher_run) are validated on the range, not
here. These pin the parsing: LLM decision, BloodHound literals, and the domain-count read.
"""
import sys
import base64
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import live_seams as ls  # noqa: E402


def test_mythic_preflight_consumes_exact_runtime_raw_surface(monkeypatch):
    import bare_mythic_tools as bmt
    from mythic import mythic

    client = object()
    seen = {"raw": 0}

    async def login():
        return client

    async def query(actual_client, _query, variables=None):
        assert actual_client is client
        return {}

    async def raw_preflight(actual_client, *, timeout):
        assert actual_client is client
        assert timeout == 7
        seen["raw"] += 1
        return False, "planted raw runtime schema drift"

    monkeypatch.setattr(ls, "_mythic_login_async_safe", login)
    monkeypatch.setattr(mythic, "execute_custom_query", query)
    monkeypatch.setattr(bmt, "raw_mythic_surface_preflight", raw_preflight)

    ok, detail = ls.mythic_queries_valid(timeout=7)

    assert ok is False
    assert "planted raw runtime schema drift" in detail
    assert seen == {"raw": 1}


def test_bloodhound_raw_surface_preflight_requires_query_parity_and_hidden_ingest(monkeypatch):
    import bare_bloodhound as bb

    specs = [
        {"function": {"name": name}}
        for name in sorted(bb.SHIPPING_BLOODHOUND_TOOL_NAMES)
    ]
    registry = {name: object() for name in bb.SHIPPING_BLOODHOUND_TOOL_NAMES}
    monkeypatch.setattr(bb, "load_bloodhound_mcp_surface", lambda: (specs, registry, object()))
    assert ls.bloodhound_raw_surface_valid() == (
        True,
        "raw BloodHound surface validated: 9 query tools plus hidden file_upload adapter",
    )

    monkeypatch.setattr(bb, "load_bloodhound_mcp_surface", lambda: (specs, registry, None))
    ok, detail = ls.bloodhound_raw_surface_valid()
    assert ok is False
    assert "file_upload" in detail

    monkeypatch.setattr(bb, "load_bloodhound_mcp_surface", lambda: (specs[:-1], registry, object()))
    ok, detail = ls.bloodhound_raw_surface_valid()
    assert ok is False
    assert "nine-name allowlist" in detail
import credential_artifacts as ca  # noqa: E402


CHILD = "north.sevenkingdoms.local"
GOOD_NT = "c1a2b3c4d5e6f708192a3b4c5d6e7f80"


class _Msg:
    def __init__(self, tool_calls=None, content="", response_metadata=None):
        self.tool_calls = tool_calls or []
        self.content = content
        self.response_metadata = response_metadata or {}


def test_parse_model_decision_tool_call():
    d = ls.parse_model_decision(_Msg(tool_calls=[{"name": "shell", "args": {"command": "whoami"}}]))
    assert d == {"tool": "shell", "args": {"command": "whoami"}}


def test_bare_model_factory_applies_shared_max_output_tokens(monkeypatch):
    import langchain.chat_models

    calls = []
    events = []
    monkeypatch.setattr(
        ls,
        "configure_sage_ssl_cert_file",
        lambda: events.append("ssl-bootstrap") or "",
    )
    monkeypatch.setattr(
        langchain.chat_models,
        "init_chat_model",
        lambda **kwargs: events.append("model-init") or calls.append(kwargs) or object(),
    )

    model_fn = ls.make_model_fn(
        "openai",
        "bedrock-claude-4-6-sonnet",
        api_key="test-key",
        base_url="https://provider.invalid/v1",
        max_output_tokens=8192,
    )

    assert callable(model_fn)
    assert events == ["ssl-bootstrap", "model-init"]
    assert calls == [{
        "model_provider": "openai",
        "model": "bedrock-claude-4-6-sonnet",
        "api_key": "test-key",
        "base_url": "https://provider.invalid/v1",
        "max_tokens": 8192,
    }]


@pytest.mark.parametrize(
    ("code", "expected_cause"),
    [
        ("context_length_exceeded", "per_request_context_limit"),
        ("rate_limit_exceeded", "other_declared_boundary"),
        (None, "other_declared_boundary"),
    ],
)
def test_model_factory_classifies_only_typed_provider_context_codes(monkeypatch, code, expected_cause):
    import langchain.chat_models

    class ProviderFailure(RuntimeError):
        pass

    failure = ProviderFailure("context_length_exceeded appears only in untrusted prose")
    failure.code = code

    class FakeModel:
        def invoke(self, _messages):
            raise failure

    monkeypatch.setattr(ls, "configure_sage_ssl_cert_file", lambda: "")
    monkeypatch.setattr(langchain.chat_models, "init_chat_model", lambda **_kwargs: FakeModel())
    invoke = ls.make_model_fn("openai", "model")

    with pytest.raises(ls.ModelRequestBoundaryError) as raised:
        invoke("system", [], [])

    assert raised.value.termination_cause == expected_cause
    assert raised.value.termination_subtype == (
        "provider_typed_code" if expected_cause == "per_request_context_limit" else "model_error"
    )


def test_model_route_auth_smoke_uses_exact_route_and_returns_only_allowlisted_metadata(monkeypatch):
    seen = {}

    def fake_factory(provider, model, **kwargs):
        seen.update({"provider": provider, "model": model, **kwargs})

        def invoke(system, tools, history):
            seen.update({"system": system, "tools": tools, "history": history})
            return {
                "final": "SECRET RESPONSE CONTENT",
                "_usage": {"input_tokens": 11, "output_tokens": 1, "ignored": "not-an-int"},
                "_backend": {
                    "effective_backend": "openai:observed-revision",
                    "observed_provider": "openai",
                    "observed_model": "observed-revision",
                    "untrusted": "must-not-leak",
                },
            }

        return invoke

    monkeypatch.setattr(ls, "make_model_fn", fake_factory)
    ok, detail = ls.model_route_auth_smoke(
        "openai",
        "configured-model",
        api_key="super-secret-key",
        base_url="https://secret-endpoint.invalid/v1",
        max_output_tokens=8192,
    )

    assert ok is True
    assert seen["provider"] == "openai" and seen["model"] == "configured-model"
    assert seen["api_key"] == "super-secret-key"
    assert seen["base_url"] == "https://secret-endpoint.invalid/v1"
    assert seen["max_output_tokens"] == 8
    assert seen["request_timeout"] == 10
    assert seen["max_retries"] == 0
    assert seen["tools"] == [] and seen["history"] == []
    assert "super-secret-key" not in detail
    assert "secret-endpoint" not in detail
    assert "SECRET RESPONSE CONTENT" not in detail
    assert "must-not-leak" not in detail
    assert '"accounting": "pre_arm_smoke"' in detail
    assert '"input_tokens": 11' in detail and '"output_tokens": 1' in detail


def test_model_route_auth_smoke_fails_closed_without_leaking_provider_exception(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("credential=super-secret endpoint=https://secret.invalid")

    monkeypatch.setattr(ls, "make_model_fn", fail)
    ok, detail = ls.model_route_auth_smoke(
        "openai",
        "configured-model",
        api_key="super-secret",
        base_url="https://secret.invalid",
    )

    assert ok is False
    assert detail == "model route request failed (RuntimeError)"
    assert "super-secret" not in detail and "secret.invalid" not in detail


def test_gate1p_task_states_binds_query_to_authenticated_operation(monkeypatch):
    seen = {}

    async def fake_login():
        return types.SimpleNamespace(current_operation_id=41)

    class FakeMythic:
        @staticmethod
        async def execute_custom_query(client, query, variables):
            seen.update({"client": client, "query": query, "variables": variables})
            return {"task": [{"operation_id": 41, "display_id": 3}]}

    monkeypatch.setattr(ls, "_mythic_login_async_safe", fake_login)
    monkeypatch.setitem(sys.modules, "mythic", types.SimpleNamespace(mythic=FakeMythic))

    rows = ls.mythic_operation_task_states(expected_operation_id=41)

    assert rows == [{"operation_id": 41, "display_id": 3}]
    assert seen["variables"] == {"operationId": 41}
    assert "operation_id: {_eq: $operationId}" in seen["query"]


def test_gate1p_task_states_rejects_authenticated_operation_drift(monkeypatch):
    async def fake_login():
        return types.SimpleNamespace(current_operation_id=42)

    monkeypatch.setattr(ls, "_mythic_login_async_safe", fake_login)
    monkeypatch.setitem(sys.modules, "mythic", types.SimpleNamespace(mythic=object()))

    with pytest.raises(RuntimeError, match="operation identity drifted"):
        ls.mythic_operation_task_states(expected_operation_id=41)


def test_gate1p_task_states_rejects_boolean_expected_operation_identity():
    with pytest.raises(RuntimeError, match="expected Mythic operation identity"):
        ls.mythic_operation_task_states(expected_operation_id=True)


def test_gate1p_task_states_rejects_non_mapping_task_rows(monkeypatch):
    async def fake_login():
        return types.SimpleNamespace(current_operation_id=41)

    class FakeMythic:
        @staticmethod
        async def execute_custom_query(_client, _query, variables):
            del variables
            return {"task": [None]}

    monkeypatch.setattr(ls, "_mythic_login_async_safe", fake_login)
    monkeypatch.setitem(sys.modules, "mythic", types.SimpleNamespace(mythic=FakeMythic))

    with pytest.raises(RuntimeError, match="non-mapping task row"):
        ls.mythic_operation_task_states(expected_operation_id=41)


def test_parse_model_decision_final():
    assert ls.parse_model_decision(_Msg(content="objective complete")) == {"final": "objective complete"}


def test_bare_history_requests_typed_objective_or_stuck_terminal():
    rendered = ls._render_history("objective", [])
    assert "OBJECTIVE_MET" in rendered
    assert "STUCK" in rendered
    assert "FINAL if done/stuck" not in rendered


def test_bare_history_renders_every_action_observation_without_compaction():
    history = [
        {"call": {"tool": "task", "args": {"index": index}}, "obs": f"observation-{index}"}
        for index in range(25)
    ]

    rendered = ls._render_history("objective", history)

    for index in range(25):
        assert f'"index": {index}' in rendered
        assert f"observation-{index}" in rendered


def test_response_backend_record_uses_observed_model_revision_not_configured_alias():
    record = ls._response_backend_record(
        _Msg(response_metadata={
            "model_name": "actual-backend-revision-2026-08-26",
            "model_provider": "provider-a",
            "system_fingerprint": "fp-123",
        }),
    )
    assert record == {
        "effective_backend": "provider-a:actual-backend-revision-2026-08-26",
        "backend_provenance_source": "response_metadata.model_provider+response_metadata.model_name",
        "observed_model": "actual-backend-revision-2026-08-26",
        "observed_provider": "provider-a",
        "system_fingerprint": "fp-123",
    }


def test_response_backend_record_does_not_construct_backend_with_missing_observed_identity():
    missing_provider = ls._response_backend_record(
        _Msg(response_metadata={"model_name": "observed-revision"}),
    )
    assert missing_provider == {
        "backend_provenance_source": "unavailable",
        "observed_model": "observed-revision",
        "observed_provider": "",
    }

    missing_model = ls._response_backend_record(
        _Msg(response_metadata={"model_provider": "provider-a"}),
    )
    assert missing_model == {
        "backend_provenance_source": "unavailable",
        "observed_model": "",
        "observed_provider": "provider-a",
    }


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
        {"output": output, "realm": CHILD},
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
    assert ls._realms_match(CHILD, CHILD.upper()) is True
    assert ls._realms_match("north.attacker.local", CHILD) is False
    assert ls._realms_match("NORTH", "SOUTH") is False

    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [
        {"account": "NORTH\\krbtgt", "realm": "NORTH"},
    ])
    assert ls.mythic_credential_probe("krbtgt", realm=CHILD)() is True

    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [
        {"account": "krbtgt@north.sevenkingdoms.local", "realm": CHILD},
    ])
    assert ls.mythic_credential_probe("krbtgt", realm="NORTH")() is True


def test_dcsync_credential_store_realm_binding_matrix(monkeypatch):
    cases = {
        "absent": False,
        "not a realm": False,
        "SOUTH": False,
        "north.attacker.local": False,
        CHILD: True,
        "NORTH": True,
    }
    for candidate_realm, expected in cases.items():
        monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [
            {"account": "krbtgt", "realm": candidate_realm},
        ])
        monkeypatch.setattr(ls, "_fetch_krbtgt_dcsync_task_outputs", lambda **kw: [])
        assert ls.krbtgt_dumped_probe(realm=CHILD)() is expected, candidate_realm


def test_dcsync_task_output_realm_binding_matrix(monkeypatch):
    assert GOOD_NT not in ca._DEGENERATE_HASHES
    output = f"krbtgt:502:aad3b435b51404eeaad3b435b51404ee:{GOOD_NT}:::"
    monkeypatch.setattr(ls, "_fetch_credentials_for_probe", lambda **kw: [])
    cases = {
        "absent": False,
        "???": False,
        "SOUTH": False,
        "north.attacker.local": False,
        CHILD: True,
        "NORTH": True,
    }
    for candidate_realm, expected in cases.items():
        monkeypatch.setattr(ls, "_fetch_krbtgt_dcsync_task_outputs", lambda **kw: [
            {"output": output, "realm": candidate_realm},
        ])
        assert ls.krbtgt_dumped_probe(realm=CHILD)() is expected, candidate_realm


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


def test_certificate_admin_control_probe_binds_account_from_proof_marker(monkeypatch):
    ticket = base64.b64encode(b"B" * 80).decode("ascii")
    rows = [
        {
            "display_id": 62,
            "callback_display_id": 3,
            "output": (
                "SAGE_CERT_AUTH_PROOF_service_admin_north_sevenkingdoms_local_3\n"
                " Directory of \\\\winterfell.north.sevenkingdoms.local\\C$\nWindows"
            ),
        },
        {
            "display_id": 61,
            "callback_display_id": 3,
            "output": f"[*] Action: Ask TGT\n[*] base64(ticket.kirbi):\n{ticket}",
        },
    ]
    monkeypatch.setattr(ls, "_fetch_certificate_auth_task_outputs", lambda **kw: rows)

    assert ls.certificate_admin_control_probe(
        None,
        realm="north.sevenkingdoms.local",
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


def test_certificate_admin_control_probe_preserves_observer_unavailable(monkeypatch):
    def unavailable(**_kwargs):
        raise ls.ProbeReadError("task query unavailable")

    monkeypatch.setattr(ls, "_fetch_certificate_auth_task_outputs", unavailable)

    with pytest.raises(ls.ProbeReadError, match="task query unavailable"):
        ls.certificate_admin_control_probe(None, realm=CHILD)()


def test_remote_execution_probe_accepts_new_privileged_target_callback(monkeypatch):
    rows = iter([
        [{"display_id": 1, "host": "CASTELBLACK", "domain": "north.sevenkingdoms.local"}],
        [
            {"display_id": 1, "host": "CASTELBLACK", "domain": "north.sevenkingdoms.local"},
            {
                "display_id": 7,
                "host": "EAST-OPS01",
                "domain": "east.hub.local",
                "user": r"NT AUTHORITY\SYSTEM",
                "integrity_level": 3,
            },
        ],
    ])
    monkeypatch.setattr(ls, "_fetch_callbacks_for_probe", lambda **kw: next(rows))

    assert ls.remote_execution_probe(
        "east-ops01",
        realm="east.hub.local",
    )() is True


def test_remote_execution_probe_rejects_new_callback_on_wrong_target(monkeypatch):
    rows = iter([
        [],
        [{
            "display_id": 7,
            "host": "WEST-OPS01",
            "domain": "east.hub.local",
            "user": r"NT AUTHORITY\SYSTEM",
            "integrity_level": 3,
        }],
    ])
    monkeypatch.setattr(ls, "_fetch_callbacks_for_probe", lambda **kw: next(rows))

    assert ls.remote_execution_probe(
        "east-ops01",
        realm="east.hub.local",
    )() is False


def test_remote_execution_probe_rejects_fabricated_task_text_and_wrong_realm(monkeypatch):
    rows = iter([
        [],
        [{
            "display_id": 7,
            "host": "EAST-OPS01",
            "domain": "south.hub.local",
            "user": r"NT AUTHORITY\SYSTEM",
            "integrity_level": 3,
        }],
    ])
    monkeypatch.setattr(ls, "_fetch_callbacks_for_probe", lambda **kw: next(rows))
    monkeypatch.setattr(ls, "_fetch_completed_task_outputs", lambda **kw: [{
        "output": "SAGE_REMOTE_EXEC_PROOF_fabricated\nEAST-OPS01",
        "callback_domain": "east.hub.local",
    }])

    assert ls.remote_execution_probe("east-ops01", realm="east.hub.local")() is False


def test_system_execution_probe_accepts_verified_system_identity_without_route_requirement(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_completed_task_outputs", lambda **kw: [{
        "display_id": 71,
        "callback_display_id": 8,
        "callback_domain": "north.sevenkingdoms.local",
        "command_name": "whoami",
        "original_params": "",
        "output": "nt authority\\system\n",
    }])
    assert ls.system_execution_probe(realm="north.sevenkingdoms.local")() is True


def test_system_execution_probe_rejects_admin_without_system_identity(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_completed_task_outputs", lambda **kw: [{
        "display_id": 72,
        "callback_display_id": 8,
        "callback_domain": "north.sevenkingdoms.local",
        "command_name": "whoami",
        "original_params": "",
        "output": "BUILTIN\\Administrators\ncommand completed successfully\n",
    }])
    assert ls.system_execution_probe(realm="north.sevenkingdoms.local")() is False


def test_system_execution_probe_rejects_system_identity_from_other_realm(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_completed_task_outputs", lambda **kw: [{
        "display_id": 73,
        "callback_display_id": 9,
        "callback_domain": "essos.local",
        "command_name": "whoami",
        "original_params": "",
        "output": "nt authority\\system\n",
    }])
    assert ls.system_execution_probe(realm="north.sevenkingdoms.local")() is False


def test_system_execution_probe_rejects_same_label_wrong_fqdn_realm(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_completed_task_outputs", lambda **kw: [{
        "display_id": 73,
        "callback_display_id": 9,
        "callback_domain": "north.attacker.local",
        "command_name": "whoami",
        "original_params": "",
        "output": "nt authority\\system\n",
    }])
    assert ls.system_execution_probe(realm="north.sevenkingdoms.local")() is False


def test_system_execution_probe_rejects_model_controlled_echo(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_completed_task_outputs", lambda **kw: [{
        "display_id": 74,
        "callback_display_id": 8,
        "callback_domain": "north.sevenkingdoms.local",
        "command_name": "shell",
        "original_params": "echo NT AUTHORITY\\SYSTEM",
        "output": "untrusted echoed text only: NT AUTHORITY\\SYSTEM",
    }])
    assert ls.system_execution_probe(realm="north.sevenkingdoms.local")() is False


def test_system_execution_probe_binds_optional_target_host(monkeypatch):
    monkeypatch.setattr(ls, "_fetch_completed_task_outputs", lambda **kw: [{
        "callback_domain": "north.sevenkingdoms.local",
        "callback_host": "CASTELBLACK",
        "command_name": "whoami",
        "original_params": "",
        "output": "nt authority\\system",
    }])
    assert ls.system_execution_probe(
        realm="north.sevenkingdoms.local",
        host="WINTERFELL",
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


def test_ticket_admin_control_probe_preserves_observer_unavailable(monkeypatch):
    def unavailable(**_kwargs):
        raise ls.ProbeReadError("task query unavailable")

    monkeypatch.setattr(ls, "_fetch_certificate_auth_task_outputs", unavailable)

    with pytest.raises(ls.ProbeReadError, match="task query unavailable"):
        ls.ticket_admin_control_probe(realm=CHILD)()


def test_strict_da_equivalent_reader_does_not_hide_builtin_failure(monkeypatch):
    def reader(_domain, *, config=None, group="Domain Admins"):
        del config
        if group == "Administrators":
            raise RuntimeError("Builtin read unavailable")
        return {"north\\domain admins"}

    monkeypatch.setattr(ls, "ldap_domain_admins", reader)

    assert ls.ldap_da_equivalent_members(CHILD, config={}, strict=False) == {
        "north\\domain admins"
    }
    with pytest.raises(RuntimeError, match="Builtin read unavailable"):
        ls.ldap_da_equivalent_members(CHILD, config={}, strict=True)


def test_dsa_info_single_value_accepts_case_insensitive_rootdse_attribute():
    info = types.SimpleNamespace(other={"DEFAULTNAMINGCONTEXT": ["DC=north,DC=sevenkingdoms,DC=local"]})
    assert ls._dsa_info_single_value(info, "defaultNamingContext") == "DC=north,DC=sevenkingdoms,DC=local"


def test_dsa_info_single_value_accepts_ldap3_production_mapping_type():
    from ldap3.utils.ciDict import CaseInsensitiveWithAliasDict

    other = CaseInsensitiveWithAliasDict()
    other["defaultNamingContext"] = ["DC=north,DC=sevenkingdoms,DC=local"]
    info = types.SimpleNamespace(other=other)

    assert ls._dsa_info_single_value(info, "DEFAULTNAMINGCONTEXT") == "DC=north,DC=sevenkingdoms,DC=local"


@pytest.mark.parametrize("other", [
    {},
    {"defaultNamingContext": []},
    {"defaultNamingContext": ["DC=one", "DC=two"]},
    {"defaultNamingContext": ["DC=one"], "DEFAULTNAMINGCONTEXT": ["DC=two"]},
])
def test_dsa_info_single_value_rejects_missing_duplicate_or_ambiguous_values(other):
    with pytest.raises(RuntimeError, match="RootDSE defaultNamingContext"):
        ls._dsa_info_single_value(types.SimpleNamespace(other=other), "defaultNamingContext")


def test_ldap_domain_admins_uses_dsa_info_then_schema_checked_group_search(monkeypatch):
    all_marker = object()
    seen = {"searches": []}

    class FakeServer:
        def __init__(self, host, **kwargs):
            seen["server"] = (host, kwargs)
            self.info = types.SimpleNamespace(
                other={"defaultNamingContext": ["DC=NORTH,DC=SEVENKINGDOMS,DC=LOCAL"]},
            )

    class FakeEntry:
        member = types.SimpleNamespace(values=[
            "CN=Administrator,CN=Users,DC=north,DC=sevenkingdoms,DC=local",
            "CN=evil.admin,CN=Users,DC=north,DC=sevenkingdoms,DC=local",
        ])

        def __contains__(self, key):
            return key == "member"

    class FakeConnection:
        def __init__(self, server, **kwargs):
            seen["connection"] = (server, kwargs)
            self.entries = []

        def search(self, base, query, **kwargs):
            seen["searches"].append((base, query, kwargs))
            self.entries = [FakeEntry()]
            return True

        def unbind(self):
            seen["unbound"] = True

    monkeypatch.setitem(sys.modules, "ldap3", types.SimpleNamespace(
        ALL=all_marker,
        Connection=FakeConnection,
        NTLM="NTLM",
        Server=FakeServer,
    ))
    config = {CHILD: {"dc_ip": "10.0.0.1", "user": "ref", "password": "secret"}}

    members = ls.ldap_domain_admins(CHILD, config=config)

    assert members == {"administrator", "evil.admin"}
    assert seen["server"] == ("10.0.0.1", {
        "use_ssl": False,
        "connect_timeout": 15,
        "get_info": all_marker,
    })
    assert len(seen["searches"]) == 1
    assert seen["searches"][0][0] == "DC=north,DC=sevenkingdoms,DC=local"
    assert "sAMAccountName=Domain Admins" in seen["searches"][0][1]
    assert seen["unbound"] is True


def test_ldap_domain_admins_rejects_wrong_rootdse_domain_before_group_search(monkeypatch):
    seen = {"searches": 0}

    class FakeServer:
        def __init__(self, *_args, **_kwargs):
            self.info = types.SimpleNamespace(other={"defaultNamingContext": ["DC=essos,DC=local"]})

    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, *_args, **_kwargs):
            seen["searches"] += 1

        def unbind(self):
            seen["unbound"] = True

    monkeypatch.setitem(sys.modules, "ldap3", types.SimpleNamespace(
        ALL="ALL", Connection=FakeConnection, NTLM="NTLM", Server=FakeServer,
    ))
    config = {CHILD: {"dc_ip": "10.0.0.1", "user": "ref", "password": "secret"}}

    with pytest.raises(RuntimeError, match="does not match declared domain"):
        ls.ldap_domain_admins(CHILD, config=config)
    assert seen["searches"] == 0 and seen["unbound"] is True


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


def test_any_probe_accepts_available_positive_despite_an_unavailable_optional_path():
    def unavailable():
        raise ls.ProbeReadError("certificate task query unavailable")

    assert ls.any_probe(unavailable, lambda: True)() is True


def test_any_probe_reports_unavailable_when_no_proof_path_can_decide():
    def unavailable():
        raise ls.ProbeReadError("certificate task query unavailable")

    with pytest.raises(ls.ProbeReadError, match="certificate task query unavailable"):
        ls.any_probe(unavailable, lambda: False)()


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
