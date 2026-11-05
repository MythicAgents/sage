"""Engagement ledger Phase 4: progress rows from any issuer, per-request retry memory, credential material.

These reproduce the observed failures with real `MythicTools` sessions on a scratch `sage.db`. Only the Mythic
transport readers are stubbed. The store, reducers, proof boundary, verifiers and request contract are real.

Evidence these fixtures follow (shapes only; no range output, credential or hash is copied):

- Channel 4, task 17: the operator ran a step by hand and no ledger row recorded it.
- Channel 3/4: a failed capability row kept suppressing that capability for every later request and channel.
- The #130 channel 3 run: a task-linked credential-store entry from a Kerberoast became a working credential.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from ai.langgraph import capabilities, engagement_state
from ai.langgraph import model as model_module
from ai.langgraph import mythic_tools as mythic_tools_module
from ai.langgraph import state_reconcile as sr
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract
from tests.esl_fixtures import ingest_proof, seed_ledger

ENGAGEMENT = "eng-phase4"
DOMAIN = "sevenkingdoms.local"
NORTH = "north.sevenkingdoms.local"
# Synthetic, secret-free key material shaped like DCSync output.
NTLM = "0123456789abcdef0123456789abcdef"
AES256 = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
TGS_HASH = "$krb5tgs$23$*svc_fixture$" + DOMAIN.upper() + "$fixture/spn*$" + "ab" * 16 + "$" + "cd" * 40


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _contract(request_id: str, channel_id: str, mode: str = "supervised"):
    return build_request_contract(
        request_id=request_id,
        channel_id=channel_id,
        operation_id="operation-1",
        mode=mode,
        autonomous_solve=False,
    )


def _foothold(callback_id: str = "3") -> engagement_state.Foothold:
    return engagement_state.Foothold(
        callback_id=callback_id, agent="apollo", host="castelblack", forest=NORTH,
        identity="north\\samwell.tarly", integrity="medium", alive=True, source="mythic", timestamp=_now(),
    )


def _session(name: str, channel: int, request: int) -> MythicTools:
    tools = MythicTools(agent_task_id=name)
    tools.client = object()
    tools.set_request_contract(_contract(f"chat:{channel}:request:{request}", f"channel-{channel}"))
    tools._engagement_footholds = [_foothold()]
    tools._engagement_objective = lambda: ""  # type: ignore[method-assign]
    tools._require_ledger_binding()
    return tools


@pytest.fixture
def operation(monkeypatch):
    monkeypatch.setattr(mythic_tools_module, "SAGE_ENGAGEMENT_ID", ENGAGEMENT)
    return _session


def _frontier(tools: MythicTools) -> list:
    """The capability frontier as the runtime builds it for this session's current request."""
    view = tools._ledger_view()
    state = engagement_state.EngagementState(
        objective="", footholds=list(tools._engagement_footholds), hops=list(view.hops),
        graph_facts=list(view.graph_facts), engagement_id=tools._eng_key(), runtime_scope=True,
    )
    return capabilities.actions_from_state(state)


def _achieved(tools: MythicTools) -> set[str]:
    return engagement_state.EngagementState(
        objective="", hops=list(tools._engagement_hops), engagement_id=tools._eng_key(), runtime_scope=True,
    ).achieved_effects()


# --- operator work from another channel -------------------------------------------------------------------------

def _dcsync_task(display_id: int, callback: int) -> dict:
    return {
        "display_id": display_id,
        "id": display_id,
        "command_name": "dcsync",
        "original_params": json.dumps({"Domain": DOMAIN, "User": "krbtgt", "DC": f"kingslanding.{DOMAIN}"}),
        "status": "completed",
        "completed": True,
        "operator": {"username": "operator_by_hand"},
        "callback": {"display_id": callback, "host": "CASTELBLACK", "user": "samwell.tarly"},
        "callback_display_id": callback,
    }


def _dcsync_output() -> str:
    return (
        "Object RDN           : krbtgt\n** SAM ACCOUNT **\nCredentials:\n"
        f"  Hash NTLM: {NTLM}\n* Primary:Kerberos-Newer-Keys *\n    Credentials\n"
        f"      aes256_hmac       (4096) : {AES256}\n"
    )


@pytest.fixture
def mythic_records(monkeypatch):
    """Stub the Mythic transport readers the reconciliation uses with an operation's recorded tasks."""
    records: dict[str, Any] = {"callbacks": [{"display_id": 13, "id": 13}], "tasks": [], "outputs": {}}

    async def active_callbacks(client):
        return records["callbacks"]

    async def tasks(mythic=None, callback_display_id=None, **_kwargs):
        return [t for t in records["tasks"] if callback_display_id in (None, t["callback_display_id"])]

    async def task_by_id(client, task_id):
        return next((t for t in records["tasks"] if t["display_id"] == task_id), {})

    async def output(client, task_id):
        return records["outputs"].get(task_id, "")

    async def footholds(client):
        return []

    monkeypatch.setattr(sr.mythic, "get_all_active_callbacks", active_callbacks)
    monkeypatch.setattr(sr.mythic, "get_all_tasks", tasks)
    monkeypatch.setattr(sr, "_fetch_task_by_display_id", task_by_id)
    monkeypatch.setattr(sr, "_fetch_task_output_text", output)
    monkeypatch.setattr(sr, "_reconcile_state_footholds_from_client", footholds)
    return records


def _turn_end(tools: MythicTools) -> str:
    model = SimpleNamespace(mythic_client=tools)
    return asyncio.run(model_module.Model._engagement_state_turn_summary_text(model))


@pytest.mark.xfail(strict=True, reason="reproduction: fixed by operation-wide reconciliation (plan section 3)")
def test_a_task_the_operator_ran_by_hand_is_recorded_at_the_next_turn_end(operation, mythic_records):
    channel_a = operation("channel-a", channel=3, request=1)
    mythic_records["tasks"] = [_dcsync_task(17, callback=13)]
    mythic_records["outputs"] = {17: _dcsync_output()}

    _turn_end(channel_a)  # channel A issued nothing; the task came from the operator on another channel

    rows = [hop for hop in channel_a._engagement_hops if hop.effect == f"krbtgt-hash:{DOMAIN}"]
    assert [(hop.status, str(hop.evidence.get("mythic_task_id"))) for hop in rows] == [("achieved", "17")]
    count = len(channel_a._ledger_snapshot().hop_rows)
    _turn_end(channel_a)  # replaying the pass changes nothing
    assert len(channel_a._ledger_snapshot().hop_rows) == count


# --- retry memory ends with the request (D18) -------------------------------------------------------------------

def _seed_gpo_control(tools: MythicTools) -> None:
    proof = ingest_proof(tools._eng_key(), task_id="101", job_id="11")
    seed_ledger(tools, graph_facts=[
        {"predicate": predicate, "source": "bloodhound:cypher", "timestamp": _now(), "ttl_seconds": 3600,
         "proof_envelope": proof}
        for predicate in ("generic-write:gpo:starkwallpaper", f"gpo-domain:starkwallpaper:{NORTH}")
    ])


def _record_terminal_failure(tools: MythicTools, action) -> list[str]:
    payload = {"ok": False, "reason": "Access is denied.", "failure_class": "genuine"}
    issued = [{"task_id": 201, "command": "shell", "_output": "Access is denied."}]
    return tools._capability_executor_record_failed_attempt(payload, action, {}, "3", issued, {})


def _names(actions) -> set[tuple[str, str]]:
    return {(action.name, action.target) for action in actions}


@pytest.mark.xfail(strict=True, reason="reproduction: fixed by per-request attempt memory (plan section 2)")
def test_a_terminal_failure_suppresses_its_capability_only_for_the_rest_of_its_request(operation):
    channel_a = operation("channel-a", channel=3, request=1)
    _seed_gpo_control(channel_a)
    offered = _frontier(channel_a)
    assert offered, "fixture must offer a capability"
    action = offered[0]
    assert _record_terminal_failure(channel_a, action)

    assert (action.name, action.target) not in _names(_frontier(channel_a))  # same request: suppressed

    channel_b = operation("channel-b", channel=4, request=1)
    assert (action.name, action.target) in _names(_frontier(channel_b))  # another channel: offered

    channel_a.set_request_contract(_contract("chat:3:request:2", "channel-3"))
    assert (action.name, action.target) in _names(_frontier(channel_a))  # the next request: offered again


# --- credential material is not a working credential (D20) ------------------------------------------------------

def _credential_task(display_id: int, command: str, params: dict) -> dict:
    return {"display_id": display_id, "status": "success", "completed": True, "command_name": command,
            "original_params": json.dumps(params), "callback": {"display_id": 3}}


def _step_with_credential(tools: MythicTools, credential: dict) -> dict:
    async def no_callbacks():
        return []

    async def no_records(_task_ids):
        return {}

    async def credentials(_now, force=False):
        return [credential]

    tools._step_callback_rows, tools._step_task_records, tools._fetch_credentials_cached = (
        no_callbacks, no_records, credentials)
    references = {"tasks": [], "graph_reads": [],
                  "credentials": [{"credential_id": credential["id"],
                                   "source_task_id": credential["task"]["display_id"]}]}
    return asyncio.run(tools._record_step_ledger(references, []))


def test_a_kerberoast_hash_in_the_credential_store_is_material_not_a_working_credential(operation):
    tools = operation("channel-a", channel=3, request=1)
    _step_with_credential(tools, {
        "id": 41, "account": "svc_fixture", "realm": DOMAIN.upper(), "type": "hash", "credential_text": TGS_HASH,
        "task": _credential_task(30, "execute_assembly",
                                 {"assembly_name": "Rubeus.exe", "assembly_arguments": "kerberoast /nowrap"}),
    })

    achieved = _achieved(tools)
    assert f"creds:svc_fixture@{DOMAIN}" not in achieved
    assert any(effect.startswith("cred-material:") and effect.endswith(f":svc_fixture@{DOMAIN}")
               for hop in tools._engagement_hops for effect in hop.satisfied_effects)


def test_a_dcsync_key_in_the_credential_store_stays_a_working_credential(operation):
    tools = operation("channel-a", channel=3, request=1)
    _step_with_credential(tools, {
        "id": 42, "account": "eddard.stark", "realm": DOMAIN.upper(), "type": "hash", "credential_text": NTLM,
        "task": _credential_task(31, "dcsync", {"Domain": DOMAIN, "User": "eddard.stark"}),
    })

    assert f"creds:eddard.stark@{DOMAIN}" in _achieved(tools)
