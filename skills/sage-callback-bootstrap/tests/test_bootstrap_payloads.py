from __future__ import annotations

import asyncio
import argparse
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_payloads.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_payloads", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


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
