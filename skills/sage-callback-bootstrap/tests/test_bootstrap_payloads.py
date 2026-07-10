from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_payloads.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_payloads", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def test_parse_callback_probe_accepts_fresh_domain_identity() -> None:
    controller = datetime(2026, 6, 24, 18, 0, 30, tzinfo=timezone.utc)

    result = bootstrap.parse_callback_probe(
        'noise\n{"Utc":"2026-06-24T18:00:00Z","Domain":"north.local","User":"NORTH\\\\samwell.tarly"}',
        controller_utc=controller,
    )

    assert result["ready"] is True
    assert result["domain"] == "north.local"
    assert result["identity"] == r"NORTH\samwell.tarly"
    assert result["skew_seconds"] == 30.0


def test_parse_callback_probe_rejects_stale_clock() -> None:
    controller = datetime(2026, 6, 24, 18, 2, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="Callback clock skew"):
        bootstrap.parse_callback_probe(
            '{"Utc":"2026-06-24T18:00:00Z","Domain":"north.local","User":"NORTH\\\\samwell.tarly"}',
            controller_utc=controller,
        )


def test_post_callback_preflight_requires_purge_and_probe(monkeypatch) -> None:
    calls = []

    async def fake_wait(*args, **kwargs):
        return {
            "display_id": 2,
            "host": "CASTELBLACK",
            "user": r"NORTH\samwell.tarly",
            "liveness": {"alive": True},
        }

    def fake_sync(max_skew_seconds):
        return {"ready": True, "max_skew_seconds": max_skew_seconds, "hosts": [{"computer": "DC01"}]}

    async def fake_task(client, callback_display_id, command_name, parameters, **kwargs):
        calls.append((callback_display_id, command_name, parameters))
        if parameters == "klist purge":
            return {"task_display_id": 7, "output": "Ticket(s) purged!"}
        return {
            "task_display_id": 8,
            "output": (
                '{"Utc":"'
                + datetime.now(timezone.utc).isoformat()
                + '","Domain":"north.local","User":"NORTH\\\\samwell.tarly"}'
            ),
        }

    monkeypatch.setattr(bootstrap, "wait_for_samwell_apollo_callback", fake_wait)
    monkeypatch.setattr(bootstrap, "synchronize_range_clocks", fake_sync)
    monkeypatch.setattr(bootstrap, "issue_callback_task", fake_task)

    result = asyncio.run(bootstrap.post_callback_preflight(object()))

    assert result["ready"] is True
    assert result["kerberos_purge_task"] == 7
    assert result["identity_probe"]["domain"] == "north.local"
    assert calls[0] == (2, "shell", "klist purge")
    assert "GetCurrentDomain" in calls[1][2]


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
