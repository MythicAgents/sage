from __future__ import annotations

import asyncio
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
