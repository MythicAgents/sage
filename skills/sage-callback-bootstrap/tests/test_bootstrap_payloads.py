from __future__ import annotations

import asyncio
import argparse
import importlib.util
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"))
from ai import resource_budget


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_payloads.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_payloads", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def _replacement_budget(tmp_path, limit=2):
    config = tmp_path / "replacement-config.json"
    ledger = tmp_path / "replacement-ledger.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {"replacement_payloads": {"limit": limit, "cleanup_reserve": 0}},
        "calendar_seconds": 1000,
        "cleanup_seconds": 1,
    }))
    return resource_budget.ResourceLedger(config, ledger)


@pytest.mark.parametrize("metered", [False, True])
def test_login_installs_optional_meter_before_client_io(monkeypatch, metered):
    from ai import resource_clients
    calls = []
    monkeypatch.delenv("SAGE_RESOURCE_BUDGET_CONFIG", raising=False)
    monkeypatch.delenv("SAGE_RESOURCE_BUDGET_LEDGER", raising=False)
    if metered:
        monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", "fixture-config")
    monkeypatch.setattr(resource_clients, "install", lambda: calls.append("meter"))

    async def fake_login(**kwargs):
        assert calls == (["meter"] if metered else [])
        assert kwargs == {"server_ip": "unit.invalid", "username": "fixture", "password": "fixture"}
        calls.append("login")
        return "client"

    monkeypatch.setattr(bootstrap.mythic, "login", fake_login)
    args = argparse.Namespace(server="unit.invalid", user="fixture", password="fixture", env_path="unused")
    assert asyncio.run(bootstrap.login(args)) == "client"
    assert calls[-1] == "login"


def test_payload_create_reserves_once_and_retains_failed_build(monkeypatch, tmp_path):
    ledger = _replacement_budget(tmp_path)
    calls = []
    monkeypatch.setattr(bootstrap, "reserve_effect", lambda amounts, operation: ledger.reserve(amounts, operation))
    monkeypatch.setattr(bootstrap, "apollo_c2_profiles", lambda _args: [])
    monkeypatch.setattr(bootstrap, "apollo_build_parameters", lambda _args: [])

    async def create_payload(_client, **kwargs):
        calls.append(kwargs["filename"])
        if len(calls) == 2:
            raise RuntimeError("build failure")
        return {"build_phase": "success"}

    monkeypatch.setattr(bootstrap.mythic, "create_payload", create_payload)
    args = SimpleNamespace(callback_host="https://unit.invalid", apollo_filename="apollo.exe")
    assert asyncio.run(bootstrap.create_apollo(object(), args))["build_phase"] == "success"
    with pytest.raises(RuntimeError):
        asyncio.run(bootstrap.create_apollo(object(), args))
    assert calls == ["apollo.exe", "apollo.exe"]
    assert ledger.status()["effective_resource_charges"]["replacement_payloads"] == 2


def test_payload_create_prerequisite_and_exhaustion_deny_before_build(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bootstrap, "reserve_effect", lambda *_a, **_k: pytest.fail("reserved before prerequisite"),
    )
    with pytest.raises(ValueError):
        asyncio.run(bootstrap.create_apollo(object(), SimpleNamespace(callback_host="")))

    ledger = _replacement_budget(tmp_path, 1)
    ledger.reserve({"replacement_payloads": 1}, "prior")
    monkeypatch.setattr(bootstrap, "reserve_effect", lambda amounts, operation: ledger.reserve(amounts, operation))
    monkeypatch.setattr(bootstrap, "apollo_c2_profiles", lambda _args: [])
    monkeypatch.setattr(bootstrap, "apollo_build_parameters", lambda _args: [])
    monkeypatch.setattr(
        bootstrap.mythic, "create_payload", lambda *_args, **_kwargs: pytest.fail("payload build dispatched"),
    )
    args = SimpleNamespace(callback_host="https://unit.invalid", apollo_filename="apollo.exe")
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        asyncio.run(bootstrap.create_apollo(object(), args))


def _bootstrap_args(**overrides):
    values = {
        "use_baked_apollo": False,
        "use_retained_callback": False,
        "callback_config": None,
        "prepare_chat": True,
        "download_dir": None,
        "sage_chat_registration_timeout": 1.0,
        "sage_chat_registration_poll_seconds": 0.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_bootstrap_reset_waits_for_registration_before_exactly_one_apollo_create(
    monkeypatch, capsys
) -> None:
    queries = iter([
        {"consuming_container": []},
        {"consuming_container": [{"id": 9, "container_running": True, "deleted": False}]},
    ])
    creates = []

    async def fake_login(_args):
        return object()

    async def fake_query(_client, query, variables=None):
        del variables
        if query == bootstrap.CHAT_CONTAINER_QUERY:
            return next(queries)
        if query == bootstrap.CALLBACK_QUERY:
            return {"callback": []}
        raise AssertionError("unexpected query")

    async def fake_create(_client, _args):
        creates.append("apollo")
        return {"uuid": "payload-uuid", "build_phase": "success"}

    async def fake_prepare(_client):
        return {}

    async def fake_download(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bootstrap, "login", fake_login)
    monkeypatch.setattr(bootstrap.mythic, "execute_custom_query", fake_query)
    monkeypatch.setattr(bootstrap, "prepare_sage_chat", fake_prepare)
    monkeypatch.setattr(bootstrap, "create_apollo", fake_create)
    monkeypatch.setattr(bootstrap, "maybe_download_payload", fake_download)

    asyncio.run(bootstrap.command_bootstrap_reset(_bootstrap_args()))

    assert creates == ["apollo"]
    assert __import__("json").loads(capsys.readouterr().out)["sage_chat_container"]["id"] == 9


def test_bootstrap_reset_times_out_without_any_foothold_effect(monkeypatch) -> None:
    effects = []

    async def fake_query(_client, _query, variables=None):
        del variables
        return {"consuming_container": []}

    async def fake_login(_args):
        return object()

    monkeypatch.setattr(bootstrap, "login", fake_login)
    monkeypatch.setattr(bootstrap.mythic, "execute_custom_query", fake_query)
    monkeypatch.setattr(bootstrap, "create_apollo", lambda *_a: effects.append("create"))
    monkeypatch.setattr(bootstrap, "import_callback_config", lambda *_a: effects.append("import"))

    with pytest.raises(RuntimeError, match="Timed out"):
        asyncio.run(bootstrap.command_bootstrap_reset(_bootstrap_args(sage_chat_registration_timeout=0)))

    assert effects == []


def test_bootstrap_reset_deleted_and_nonrunning_rows_never_create_or_import(
    monkeypatch,
) -> None:
    effects = []

    async def fake_query(_client, _query, variables=None):
        del variables
        return {
            "consuming_container": [
                {"id": 1, "container_running": True, "deleted": True},
                {"id": 2, "container_running": False, "deleted": False},
            ]
        }

    monkeypatch.setattr(bootstrap, "login", lambda _args: asyncio.sleep(0, result=object()))
    monkeypatch.setattr(bootstrap.mythic, "execute_custom_query", fake_query)
    monkeypatch.setattr(bootstrap, "create_apollo", lambda *_a: effects.append("create"))
    monkeypatch.setattr(bootstrap, "import_callback_config", lambda *_a: effects.append("import"))

    with pytest.raises(RuntimeError, match="Timed out"):
        asyncio.run(bootstrap.command_bootstrap_reset(_bootstrap_args(sage_chat_registration_timeout=0)))

    assert effects == []


def test_bootstrap_reset_already_running_registration_does_not_sleep(
    monkeypatch, capsys
) -> None:
    sleep_calls = []

    async def fake_login(_args):
        return object()

    async def fake_query(_client, query, variables=None):
        del variables
        if query == bootstrap.CHAT_CONTAINER_QUERY:
            return {"consuming_container": [{"id": 3, "container_running": True, "deleted": False}]}
        if query == bootstrap.CALLBACK_QUERY:
            return {"callback": []}
        raise AssertionError("unexpected query")

    monkeypatch.setattr(bootstrap, "login", fake_login)
    monkeypatch.setattr(bootstrap.mythic, "execute_custom_query", fake_query)
    monkeypatch.setattr(bootstrap.asyncio, "sleep", lambda _seconds: sleep_calls.append(_seconds))

    async def fake_prepare(_client):
        return {}

    async def fake_create(*_args):
        return {"uuid": "one"}

    async def fake_download(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bootstrap, "prepare_sage_chat", fake_prepare)
    monkeypatch.setattr(bootstrap, "create_apollo", fake_create)
    monkeypatch.setattr(bootstrap, "maybe_download_payload", fake_download)

    asyncio.run(bootstrap.command_bootstrap_reset(_bootstrap_args()))
    capsys.readouterr()

    assert sleep_calls == []


@pytest.mark.parametrize("field", ["timeout_seconds", "poll_seconds"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -1.0])
def test_registration_wait_rejects_nonfinite_and_negative_controls(field, value) -> None:
    kwargs = {"timeout_seconds": 0.0, "poll_seconds": 0.0, field: value}

    with pytest.raises(ValueError, match="finite non-negative"):
        asyncio.run(bootstrap.wait_for_running_sage_chat_container(object(), **kwargs))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -1.0])
def test_registration_wait_cli_rejects_unbounded_numeric_timeout(value) -> None:
    parser = bootstrap.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["bootstrap-reset", "--sage-chat-registration-timeout", str(value)])


@pytest.mark.parametrize("timeout_seconds,poll_seconds", [(0.0, 0.0), (0.1, 0.01), (30.0, 1.0)])
def test_registration_wait_accepts_zero_positive_and_default_controls(
    timeout_seconds, poll_seconds
) -> None:
    bootstrap.validate_registration_wait_controls(timeout_seconds, poll_seconds)


def test_registration_wait_emits_typed_success_timeout_and_transport_error(monkeypatch):
    diagnostics = []
    monkeypatch.setattr(bootstrap, "_emit_registration_diagnostic", lambda **value: diagnostics.append(value))
    async def success(*_args):
        return {"consuming_container": [{"id": 7, "container_running": True, "deleted": False,
                                          "name": "SYNTHETIC_SECRET"}]}
    monkeypatch.setattr(bootstrap.mythic, "execute_custom_query", success)
    asyncio.run(bootstrap.wait_for_running_sage_chat_container(object(), timeout_seconds=0, poll_seconds=0))
    assert diagnostics == [{"status": "success", "attempt_count": 1, "rows": [{"id": 7,
        "container_running": True, "deleted": False, "name": "SYNTHETIC_SECRET"}]}]
    diagnostics.clear()
    async def absent(*_args): return {"consuming_container": []}
    monkeypatch.setattr(bootstrap.mythic, "execute_custom_query", absent)
    with pytest.raises(RuntimeError, match="Timed out"):
        asyncio.run(bootstrap.wait_for_running_sage_chat_container(object(), timeout_seconds=0, poll_seconds=0))
    assert diagnostics == [{"status": "timeout", "attempt_count": 1, "rows": [], "error_category": "timeout"}]
    diagnostics.clear()
    async def failed(*_args): raise ConnectionError("SYNTHETIC_SECRET")
    monkeypatch.setattr(bootstrap.mythic, "execute_custom_query", failed)
    with pytest.raises(ConnectionError):
        asyncio.run(bootstrap.wait_for_running_sage_chat_container(object(), timeout_seconds=0, poll_seconds=0))
    assert diagnostics[0]["error_category"] == "connection" and "SYNTHETIC_SECRET" not in repr(diagnostics)


def test_post_callback_preflight_is_task_free(monkeypatch) -> None:
    async def fake_wait(*args, **kwargs):
        return {
            "display_id": 2,
            "host": "CASTELBLACK",
            "user": r"NORTH\samwell.tarly",
            "liveness": {"alive": True},
        }

    def fake_sync(max_skew_seconds):
        return {"ready": True, "max_skew_seconds": max_skew_seconds, "hosts": [{"computer": "DC01"}]}

    async def fail_issue_task(*args, **kwargs):
        raise AssertionError("post-callback-preflight must not issue Mythic payload tasks")

    monkeypatch.setattr(bootstrap, "wait_for_foothold_apollo_callback", fake_wait)
    monkeypatch.setattr(bootstrap, "synchronize_range_clocks", fake_sync)
    monkeypatch.setattr(bootstrap.mythic, "issue_task", fail_issue_task)

    result = asyncio.run(bootstrap.post_callback_preflight(object()))

    assert result["ready"] is True
    assert result["apollo_callback"]["display_id"] == 2
    assert result["range_clocks"]["ready"] is True
    assert result["preflight_scope"] == "control-plane-read-only"
    assert result["payload_tasking_performed"] is False
    assert result["payload_tasks_issued"] == 0
    assert result["target_identity_probed"] is False
    assert result["kerberos_purge_performed"] is False


def test_bootstrap_reset_can_leave_chat_creation_to_operator(
    monkeypatch, capsys
) -> None:
    async def fake_login(_args):
        return object()

    async def fake_query(_client, query, variables=None):
        del variables
        if query == bootstrap.CHAT_CONTAINER_QUERY:
            return {
                "consuming_container": [
                    {
                        "id": 1,
                        "container_running": True,
                        "deleted": False,
                    }
                ]
            }
        if query == bootstrap.CALLBACK_QUERY:
            return {"callback": []}
        raise AssertionError("unexpected query")

    async def fail_prepare(_client):
        raise AssertionError("chat must remain operator managed")

    async def fake_token(_client):
        return {
            "created": True,
            "api_token": {"id": 3, "scopes": ["*"]},
        }

    async def fake_apollo(_client, _args):
        return {"uuid": "payload-uuid", "build_phase": "success"}

    async def fake_download(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bootstrap, "login", fake_login)
    monkeypatch.setattr(
        bootstrap.mythic, "execute_custom_query", fake_query
    )
    monkeypatch.setattr(bootstrap, "prepare_sage_chat", fail_prepare)
    monkeypatch.setattr(
        bootstrap, "ensure_sage_chat_api_token", fake_token
    )
    monkeypatch.setattr(bootstrap, "create_apollo", fake_apollo)
    monkeypatch.setattr(
        bootstrap, "maybe_download_payload", fake_download
    )
    args = argparse.Namespace(
        use_baked_apollo=False,
        use_retained_callback=False,
        callback_config=None,
        prepare_chat=False,
        download_dir=None,
    )

    asyncio.run(bootstrap.command_bootstrap_reset(args))
    result = __import__("json").loads(capsys.readouterr().out)

    assert result["sage_chat"] == {
        "api_token": {
            "created": True,
            "api_token": {"id": 3, "scopes": ["*"]},
        },
        "prepared": False,
        "reason": "operator_managed_chat_creation",
    }


def test_import_callback_config_explicitly_hides_imported_callback(monkeypatch) -> None:
    queries = []
    updates = []

    async def fake_query(client, query, variables=None):
        queries.append((query, variables))
        if "importCallbackConfig" in query:
            assert variables["config"]["callback"]["active"] is False
            return {"importCallbackConfig": {"status": "success", "error": ""}}
        return {
            "callback": [{
                "display_id": 7,
                "agent_callback_id": "callback-uuid",
            }]
        }

    async def fake_update(client, callback_display_id, active=None, **kwargs):
        updates.append((callback_display_id, active))
        return {"status": "success", "error": ""}

    monkeypatch.setattr(bootstrap.mythic, "execute_custom_query", fake_query)
    monkeypatch.setattr(bootstrap.mythic, "update_callback", fake_update)

    result = asyncio.run(bootstrap.import_callback_config(object(), {
        "callback": {
            "agent_callback_id": "callback-uuid",
            "active": True,
        }
    }))

    assert result["callback_hidden"] is True
    assert result["callback_display_id"] == 7
    assert updates == [(7, False)]
