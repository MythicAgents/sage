"""Real regressions observed in the shared engagement ledger (channel 3/4, 2026-11-04).

Each test drives real ``MythicTools`` sessions, the real producer path (``_record_engagement_success``), the
real ``/state`` slash handler and the real end-of-turn reconciliation. Only the external Mythic transport is
stubbed. The store, proof boundary, serializers and correction handling are the production code.

1. Two sessions opened before either writes: A records X, then B records Y. Both rows must survive.
2. ``/state remove`` while another session is running: that session's next write must not bring the row back,
   and its own gate view must stop relying on the row.
3. A legacy ledger numbered 23–29 (high-water 30): repeated reconciliation keeps 23–29 and the next new hop is 30.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.langgraph import engagement_ledger
from ai.langgraph import engagement_state
from ai.langgraph import model as graph_model
from ai.langgraph import mythic_tools
from mythic import mythic as MM
from sage_chat import slash
from sage_chat.headless import build_chat_request

from tests.esl_fixtures import (
    ENGAGEMENT_KEY,
    MARKER_UUID,
    OPERATION_ID,
    OPERATION_NAME,
    channel34_ledger,
    observe_task,
    write_legacy_ledger,
)

_ROW = re.compile(r"^\| (\d+) \| `([^`]*)` \| .*? \| `([^`]*)` \|")


@pytest.fixture(autouse=True)
def _ordinary_session_binding(monkeypatch):
    # Ordinary native-chat sessions: no explicit engagement namespace, bound by their operation.
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "default", raising=False)
    monkeypatch.delenv("SAGE_ENGAGEMENT_ID", raising=False)


def _stub_mythic(monkeypatch, *, tasks=()):
    """Stub only Mythic's transport: one visible operation, one persisted marker, optional task history."""

    async def fake_query(_client, query, variables=None):
        if "SageOpList" in query:
            return {"operation": [{"id": OPERATION_ID, "name": OPERATION_NAME}]}
        if "SageOperationById" in query:
            return {"operation_by_pk": {"id": OPERATION_ID, "name": OPERATION_NAME}}
        if "SageEngagementMarker" in query:
            return {"operationeventlog": [{"message": f"Sage durable engagement ledger id: {MARKER_UUID}"}]}
        if "SageOpFromCallback" in query:
            return {"callback": []}
        return {}

    async def fake_send(_client, message, level="info", source=""):
        return {"status": "success"}

    async def fake_callbacks(_client):
        if not tasks:
            return []
        return [{"display_id": 3, "id": 3, "host": "CASTELBLACK", "user": "samwell.tarly",
                 "domain": "north.sevenkingdoms.local", "integrity_level": 2, "active": True}]

    async def fake_tasks(mythic=None, callback_display_id=None):
        return [dict(task) for task in tasks]

    async def fake_output(mythic=None, task_display_id=None):
        return [{"response_text": "Directory listing complete"}]

    monkeypatch.setattr(MM, "execute_custom_query", fake_query)
    monkeypatch.setattr(MM, "send_event_log_message", fake_send)
    monkeypatch.setattr(MM, "get_all_active_callbacks", fake_callbacks)
    monkeypatch.setattr(MM, "get_all_tasks", fake_tasks)
    monkeypatch.setattr(MM, "get_all_task_output_by_id", fake_output)


def _seed_channel34():
    """Install the 23–29 fixture the way an operator would find it: a legacy file, imported when the build
    has an offline migration entry point (the pre-SQLite runtime reads the file directly)."""
    state_dir = os.environ["SAGE_ENGAGEMENT_STATE_DIR"]
    write_legacy_ledger(state_dir, channel34_ledger())
    try:
        from ai.langgraph import engagement_store
    except ImportError:
        return
    engagement_store.import_legacy_directory(state_dir)


def _session(channel_id: int = 3):
    tools = mythic_tools.MythicTools(operation_id=OPERATION_ID, channel_id=channel_id, apitoken_id=0)
    tools.client = object()
    asyncio.run(tools._ensure_engagement_key())
    return tools


def _record(tools, technique: str, target: str, task_id: str) -> None:
    """The issue-task producer path: a pending modeled hop verified from a completed task's output."""
    tools._last_issued_task_display_id = task_id
    tools._last_issued_callback_id = "3"
    tools._last_issued_task_terminal_status = "completed"
    tools._last_issued_command = "shell"
    observe_task(tools, task_id, "3")
    tools._pending_engagement_hop = (technique, target, "2026-11-04T18:00:00+00:00")
    tools._record_engagement_success(
        "whoami\r\nnt authority\\system\r\n",
        task_id=task_id,
        callback_id="3",
        command="shell",
    )


def _state_rows(tools, arg: str = "") -> dict[str, tuple[int, str]]:
    model = SimpleNamespace(mythic_client=tools, mode="supervised", provider="test", model="test", state={})
    request = build_chat_request("x", channel_id=tools.channel_id, operation_id=OPERATION_ID)
    shown = asyncio.run(slash._handle_state(model, request, arg))
    rows: dict[str, tuple[int, str]] = {}
    for line in shown.splitlines():
        match = _ROW.match(line)
        if match:
            rows[match.group(2)] = (int(match.group(1)), match.group(3))
    return rows


def _runtime_effects(tools) -> set[str]:
    state = engagement_state.EngagementState(
        objective="test",
        hops=list(tools._engagement_hops),
        engagement_id=tools._eng_key(),
        runtime_scope=True,
    )
    return state.achieved_effects()


def test_two_sessions_opened_before_either_writes_keep_both_rows(monkeypatch):
    _stub_mythic(monkeypatch)
    first = _session(3)
    second = _session(4)

    _record(first, "gpo-abuse", "kingslanding", "201")
    _record(second, "gpo-abuse", "meereen", "202")

    rows = _state_rows(_session(5))
    assert "gpo-abuse:kingslanding" in rows, rows
    assert "gpo-abuse:meereen" in rows, rows
    numbers = [rows["gpo-abuse:kingslanding"][0], rows["gpo-abuse:meereen"][0]]
    assert sorted(numbers) == [1, 2]

    # Reverse order on a fresh pair: the earlier-opened session writes last.
    third = _session(6)
    fourth = _session(7)
    _record(fourth, "gpo-abuse", "braavos", "203")
    _record(third, "gpo-abuse", "pentos", "204")
    rows = _state_rows(_session(8))
    assert {"gpo-abuse:kingslanding", "gpo-abuse:meereen", "gpo-abuse:braavos", "gpo-abuse:pentos"} <= set(rows)
    assert sorted(number for number, _ in rows.values()) == [1, 2, 3, 4]


def test_operator_removal_is_not_resurrected_by_a_running_session(monkeypatch):
    _stub_mythic(monkeypatch)
    _seed_channel34()
    running = _session(3)
    operator = _session(4)
    assert "system:starkwallpaper" in _runtime_effects(running)

    _state_rows(operator, "remove 24")
    _record(running, "gpo-abuse", "kingslanding", "301")

    rows = _state_rows(_session(5))
    number, status = rows.get("gpo-abuse:starkwallpaper", (24, "achieved"))
    assert number == 24
    assert status != "achieved", "the removed row came back as current achievement"
    assert "system:starkwallpaper" not in _runtime_effects(running)
    assert rows["gpo-abuse:kingslanding"][0] == 30


def test_reconciliation_keeps_legacy_numbers_and_continues_from_thirty(monkeypatch):
    unmodeled = {"display_id": 900, "id": 900, "command_name": "ls", "status": "completed",
                 "completed": True, "original_params": "", "callback": {"display_id": 3}}
    _stub_mythic(monkeypatch, tasks=[unmodeled])
    _seed_channel34()
    tools = _session(4)
    expected = {engagement_ledger.md_cell(label, 48): number for label, number in {
        "collect-graph:north|baseline": 23,
        "gpo-abuse:starkwallpaper": 24,
        "domain-admin-membership-check:north": 25,
        "capability:gpo-controlled-system-exec:gpo=starkwallpaper;domain=north.sevenkingdoms.local": 26,
        "capability:dcsync-krbtgt:domain=north.sevenkingdoms.local;account=krbtgt": 27,
        "dcsync:north.sevenkingdoms.local": 28,
        "collect-graph:north|da:north|scope:essos.local": 29,
    }.items()}

    model = object.__new__(graph_model.Model)
    model.mythic_client = tools
    model.channel_id = 4
    for marker in ("901", "902", "903"):
        tools._last_issued_task_display_id = marker
        asyncio.run(model._engagement_state_turn_summary_text())
        assert {label: number for label, (number, _) in _state_rows(tools).items()} == expected

    _state_rows(tools, "reconcile")
    assert {label: number for label, (number, _) in _state_rows(_session(5)).items()} == expected

    _record(tools, "gpo-abuse", "kingslanding", "904")
    assert _state_rows(_session(6))["gpo-abuse:kingslanding"][0] == 30


def test_a_rejected_raw_candidate_keeps_its_reason_through_state_and_reconciliation(monkeypatch):
    """A raw result whose task lineage is missing (fault-injected here) stays ``pending (proof missing)`` with the
    actual admission reason after end-of-turn reconciliation, ``/state reconcile`` and a fresh session's reload."""
    _stub_mythic(monkeypatch)
    writer = _session(3)
    _record(writer, "gpo-abuse", "kingslanding", None)

    model = object.__new__(graph_model.Model)
    model.mythic_client = writer
    model.channel_id = 3
    asyncio.run(model._engagement_state_turn_summary_text())
    _state_rows(writer, "reconcile")

    reader = _session(4)
    assert _state_rows(reader)["gpo-abuse:kingslanding"] == (1, "pending (proof missing)")
    [hop] = [hop for hop in reader._ledger_view().hops if hop.technique == "gpo-abuse"]
    assert hop.evidence["proof_admission_reason"] == "missing Mythic task lineage"
    assert hop.proof_envelope["task_id"] == "" and hop.proof_envelope["callback_id"] == "3"
    assert "system:kingslanding" not in _runtime_effects(reader)
    assert "system:kingslanding" not in _runtime_effects(writer)


def test_a_rejected_gpo_setup_is_not_promoted_through_state_reconciliation_or_reload(monkeypatch,
                                                                                      mythic_task_records):
    """A SharpGPOAbuse task whose observation is not terminal-success (fault-injected) leaves a retained, rejected
    candidate carrying its capability transaction. A later admitted SYSTEM-execution proof in that transaction must
    not promote the setup row: after end-of-turn reconciliation, ``/state reconcile`` and a fresh session's reload it
    keeps its row number, ``pending`` status, its own candidate and the actual rejection reason."""
    _stub_mythic(monkeypatch)
    transaction = "capability-exec:gpo-kingslanding"
    effect = "system-exec:gpo:kingslanding@north.sevenkingdoms.local"
    writer = _session(3)
    token = mythic_tools._task_visibility_context.set({"transaction_id": transaction})
    try:
        mythic_task_records[77] = {"completed": False, "status": "submitted"}  # Mythic: not yet handed to the agent
        observe_task(writer, "77", "3")
        writer._pending_engagement_hop = ("gpo-abuse", "kingslanding", "2026-11-04T18:00:00+00:00")
        writer._record_engagement_success(
            "whoami\r\nnt authority\\system\r\n", task_id="77", callback_id="3", command="execute_assembly",
        )
    finally:
        mythic_tools._task_visibility_context.reset(token)
    number = _state_rows(writer)["gpo-abuse:kingslanding"][0]

    now = "2026-11-04T18:05:00+00:00"
    proof = writer._runtime_task_proof_envelope(
        "capability:gpo-controlled-system-exec", now, callback_id="3", task_id="78", terminal_status="completed",
        command="shell", transaction_id=transaction, verifier_input={"task_id": "78"},
        verifier_result={"verdict": "achieved"},
    )
    assert proof, "the execution proof must be admitted for this control"
    state = engagement_state.EngagementState(
        objective="test", hops=list(writer._ledger_view().hops), engagement_id=writer._eng_key(), runtime_scope=True,
    )
    updated = engagement_state.record_effect_result(
        state, "capability:gpo-controlled-system-exec", "gpo=kingslanding", effect, "achieved",
        {"source": "capability", "mythic_task_id": "78", "callback_id": "3", "transaction_id": transaction,
         "terminal_task_status": "completed", "command": "shell"},
        now, satisfied_effects=[effect], proof_envelope=proof, require_admissible_proof=True,
        engagement_id=writer._eng_key(),
    )
    assert writer._ledger_commit_hops(state.hops, updated.hops, what="test execution")

    model = object.__new__(graph_model.Model)
    model.mythic_client = writer
    model.channel_id = 3
    asyncio.run(model._engagement_state_turn_summary_text())
    _state_rows(writer, "reconcile")

    reader = _session(4)
    assert _state_rows(reader)["gpo-abuse:kingslanding"] == (number, "pending (proof missing)")
    [hop] = [hop for hop in reader._ledger_view().hops if hop.technique == "gpo-abuse"]
    assert hop.evidence["proof_admission_reason"] == "task is not terminal-success"
    assert "reconciled_from_hop" not in hop.evidence
    assert (hop.proof_envelope["task_id"], hop.proof_envelope["transaction_id"]) == ("77", transaction)
    for session in (reader, writer):
        effects = _runtime_effects(session)
        assert effect in effects and "system:kingslanding" not in effects
