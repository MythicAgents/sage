"""Credential reconcile from Mythic task history — re-homed (Phase 4) from the deleted PayloadType
`state` command into `ai/langgraph/state_reconcile.py`, now driven by the chat `/state reconcile`.

Security-critical behavior preserved: dry-run is the DEFAULT (task output is attacker-influenceable),
`apply=True` is the explicit opt-in, and the plaintext secret is NEVER echoed into refs/notes.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph import state_reconcile as sr  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_reconcile_imports_credentials_without_returning_secret(monkeypatch):
    secret = "a" * 64
    created = []

    async def fake_resolve_operation(client):
        return (1, "Operation Chimera")

    async def fake_query(client, query, variables=None):
        assert variables == {"op": 1}
        return {"credential": []}

    async def fake_create(client, credential, account="", realm="", comment="", credential_type=""):
        created.append({
            "credential": credential,
            "account": account,
            "realm": realm,
            "comment": comment,
            "credential_type": credential_type,
        })
        return {"status": "success", "id": 99}

    monkeypatch.setattr(sr.operation_context, "resolve_operation", fake_resolve_operation)
    monkeypatch.setattr(sr.mythic, "execute_custom_query", fake_query)
    monkeypatch.setattr(sr.mythic, "create_credential", fake_create)

    material = {
        "account": "krbtgt",
        "realm": "sevenkingdoms.local",
        "secret_type": "aes256",
        "credential_type": "key",
        "credential": secret,
    }

    # Dry-run is the DEFAULT: nothing written, nothing referenced, secret never leaked.
    refs_dry, notes_dry = _run(sr._import_reconciled_credentials(object(), [material], 450))
    assert created == []
    assert refs_dry == []
    assert any("[dry-run]" in n for n in notes_dry)
    assert secret not in "\n".join(notes_dry)

    # apply=True is the explicit opt-in that actually writes the credential.
    refs, notes = _run(sr._import_reconciled_credentials(object(), [material], 450, apply=True))

    assert created == [{
        "credential": secret,
        "account": "krbtgt",
        "realm": "sevenkingdoms.local",
        "comment": "Sage task-history reconcile from Mythic task 450: aes256",
        "credential_type": "key",
    }]
    assert refs == [{
        "id": 99,
        "account": "krbtgt",
        "realm": "sevenkingdoms.local",
        "secret_type": "aes256",
        "credential_type": "key",
        "status": "added",
    }]
    assert secret not in str(refs)
    assert secret not in "\n".join(notes)


def test_reconcile_reuses_existing_exact_credential(monkeypatch):
    secret = "b" * 32
    created = []

    async def fake_resolve_operation(client):
        return (1, "Operation Chimera")

    async def fake_query(client, query, variables=None):
        return {"credential": [{
            "id": 7,
            "account": "krbtgt",
            "realm": "sevenkingdoms.local",
            "type": "hash",
            "credential_text": secret,
            "comment": "manual",
        }]}

    async def fake_create(*args, **kwargs):
        created.append(kwargs)
        return {"status": "success", "id": 100}

    monkeypatch.setattr(sr.operation_context, "resolve_operation", fake_resolve_operation)
    monkeypatch.setattr(sr.mythic, "execute_custom_query", fake_query)
    monkeypatch.setattr(sr.mythic, "create_credential", fake_create)

    refs, notes = _run(sr._import_reconciled_credentials(object(), [{
        "account": "krbtgt",
        "realm": "sevenkingdoms.local",
        "secret_type": "ntlm",
        "credential_type": "hash",
        "credential": secret.upper(),  # case-insensitive match to the stored row
    }], 450))

    assert created == []
    assert refs[0]["id"] == 7
    assert refs[0]["status"] == "existing"
    assert "reused" in "\n".join(notes)
