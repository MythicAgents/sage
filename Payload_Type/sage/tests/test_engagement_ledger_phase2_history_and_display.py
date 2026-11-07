"""Phase 2: historical rows are revalidated, and every ledger view shows task links and structured notes, never output.

Task 18 (2026-11-06) left two rows for one account secret, written before the shared task classification existed:
hop 9 from the raw writer (pending, the reconciler's rejected candidate) and hop 10 from the capability writer
(achieved on output alone, envelope status ``completed``). Both rows below follow that record; the account, realm and
key material are synthetic. The schema-3 migration folds them into one pending row and holds the effect until a new
observation is admitted. ``result_preview`` (task output in row evidence) is gone: views show Mythic task links and one
note line per task built from structured fields.

Only Mythic's transport is stubbed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ai.langgraph import engagement_ledger, engagement_state, engagement_store, proof_boundary
from ai.langgraph import model as model_module
from sage_chat import slash
from sage_chat.headless import build_chat_request
from tests.esl_fixtures import seed_ledger
from tests.test_engagement_ledger_phase2_unified_proof import (  # noqa: F401 - fixtures are used by name
    ACCOUNT,
    CALLBACK,
    COMPLETION_ERROR,
    DCSYNC_OUTPUT,
    ENGAGEMENT,
    NTLM,
    REALM,
    SUCCESS,
    TASK,
    TYPED,
    WORKING,
    _achieved,
    _capability,
    _capability_issue,
    _now,
    _reader,
    _typed_rows,
    mythic_issue,
    session,
)

RAW_TARGET = f"north\\{ACCOUNT}@{REALM}"
CAPABILITY_TARGET = f"domain={REALM};account={ACCOUNT}"


# --- task 18's two historical rows ----------------------------------------------------------------------------------

def _historical_rows(engagement_id: str) -> list[dict]:
    """Hop 9 and hop 10 as the writers at 50a6518 stored them (no shared classification in either envelope)."""
    raw_effect = engagement_state._technique_effect("dcsync-user", RAW_TARGET)
    rejected = proof_boundary.make_runtime_task_envelope(
        engagement_id=engagement_id, callback_id=CALLBACK, task_id=TASK, terminal_status=COMPLETION_ERROR["status"],
        command="dcsync", verifier_id="task_reconciler:dcsync-user", captured_at=_now(),
        transaction_id=f"operator-task:{TASK}", verifier_input={"probe": {}}, verifier_result={"verdict": "achieved"},
    ).to_dict()
    output_only = proof_boundary.make_runtime_task_envelope(
        engagement_id=engagement_id, callback_id=CALLBACK, task_id=TASK, terminal_status="completed",
        command="dcsync", verifier_id="capability:dcsync-account", captured_at=_now(),
        transaction_id=f"capability-exec:dcsync-account:{ACCOUNT}", verifier_input={"probe": {}},
        verifier_result={"verdict": "achieved"},
    ).to_dict()
    return [
        {"id": f"dcsync-user:{RAW_TARGET}", "technique": "dcsync-user", "target": RAW_TARGET, "effect": raw_effect,
         "status": "pending", "pending_reason": "proof_missing", "satisfied_effects": [raw_effect],
         "preconditions": [], "source": "task_history_reconcile", "timestamp": _now(), "proof_envelope": rejected,
         "evidence": {"source": "task_history_reconcile", "mythic_task_id": TASK, "callback_id": CALLBACK,
                      "terminal_task_status": COMPLETION_ERROR["status"], "result_preview": DCSYNC_OUTPUT[:200],
                      "proof_persistence_state": "rejected",
                      "proof_admission_reason": "task is not terminal-success"}},
        {"id": f"capability:dcsync-account:{CAPABILITY_TARGET}", "technique": "capability:dcsync-account",
         "target": CAPABILITY_TARGET, "effect": WORKING, "status": "achieved", "satisfied_effects": [WORKING],
         "preconditions": [], "source": "execute_capability", "timestamp": _now(), "proof_envelope": output_only,
         "evidence": {"source": "execute_capability", "mythic_task_id": str(TASK), "callback_id": str(CALLBACK),
                      "terminal_task_status": "completed", "command": "dcsync",
                      "transaction_id": f"capability-exec:dcsync-account:{ACCOUNT}"}},
    ]


def _seed_history(tools, rows: list[dict]) -> None:
    binding = tools._require_ledger_binding()
    tools._ledger_store().apply(binding, engagement_store.hop_mutations([], rows))


def _set_schema(tools, version: int) -> None:
    connection = sqlite3.connect(tools._ledger_store().path)
    try:
        connection.execute("UPDATE esl_operations SET schema_version=?", (version,))
        connection.commit()
    finally:
        connection.close()


def _upgrade(tools) -> None:
    """A later Sage process opens the database: its first write transaction (the binding) migrates the schema."""
    engagement_store.EngagementStore(tools._ledger_store().path).bind_namespace(ENGAGEMENT)


def _rows(tools) -> list:
    return tools._ledger_snapshot().hop_rows()


def test_task_18_history_is_folded_into_one_pending_row_on_upgrade(session):
    tools = session()
    _seed_history(tools, _historical_rows(tools._eng_key()))
    hop9, hop10 = _rows(tools)
    assert WORKING in _achieved(tools)  # the output-only capability row still credits a working credential
    events_before = tools._ledger_store().history(tools._require_ledger_binding())
    _set_schema(tools, 2)

    _upgrade(tools)

    owner, merged = _rows(_reader())
    assert (owner.number, owner.identity) == (hop9.number, hop9.identity)  # number and identity are permanent
    assert owner.payload["canonical_effect"] == TYPED and owner.payload["status"] == "pending"
    [contribution] = owner.payload["contributions"]
    assert contribution["task_id"] == str(TASK)
    writers = contribution["writers"]
    assert set(writers) == {f"legacy:{hop9.number}", f"legacy:{hop10.number}"}
    # Hop 10's stored achievement is retained verbatim and re-admitted under the current rule.
    assert "admitted" in writers[f"legacy:{hop10.number}"]
    assert WORKING not in _achieved(_reader())
    assert merged.identity == hop10.identity and merged.retraction is not None
    assert merged.retraction["actor"] == engagement_store.REVALIDATION_ACTOR
    assert merged.retraction["reason"] == f"merged into hop {hop9.number}: one row per canonical effect"
    shown = _state_command(_reader())
    assert (f"- hop {hop9.number} · [task {TASK}](/new/task/{TASK}): does not prove `{ACCOUNT}@{REALM}` · "
            "not admitted: earlier record") in shown  # hop 10's stored achievement no longer proves anything
    assert f"merged by revalidation — merged into hop {hop9.number}" in shown
    assert f"- hop {hop10.number} ·" not in shown  # a merged row is history: no evidence line claims it proves
    assert "SAM Username" not in shown  # hop 9's stored preview is history, never rendered

    binding = tools._require_ledger_binding()
    events = tools._ledger_store().history(binding)
    assert events[:len(events_before)] == events_before  # every earlier event is kept unchanged
    added = events[len(events_before):]
    assert [(e["kind"], e["row_id"], e["actor_name"]) for e in added] == [
        ("update", hop9.row_id, engagement_store.REVALIDATION_ACTOR),
        ("retract", hop10.row_id, engagement_store.REVALIDATION_ACTOR),
    ]
    assert added[0]["reason"] == engagement_store.REVALIDATION_REASON
    # Hop 9's stored preview held the secret: it stays in hop 9's old events, never in a new event or the new head.
    assert NTLM in json.dumps(events_before)
    assert NTLM not in json.dumps(added) and "result_preview" not in json.dumps(added)
    assert NTLM not in json.dumps(owner.payload) and "result_preview" not in json.dumps(owner.payload)
    assert tools._ledger_store().rebuild_heads(binding)[hop9.row_id]["payload"] == owner.payload

    # A second upgrade, or a replay, changes nothing.
    _set_schema(tools, 2)
    _upgrade(tools)
    assert len(tools._ledger_store().history(binding)) == len(events)
    [status] = engagement_store.schema_status(tools._ledger_store().path)
    assert status["schema_version"] == engagement_store.SCHEMA_VERSION
    assert status["revalidated_rows"] == {"updated": 1, "merged": 1}


def test_a_later_proving_task_lands_on_the_revalidated_row(session, mythic_task_records):
    tools = session()
    _seed_history(tools, _historical_rows(tools._eng_key()))
    hop9, _hop10 = _rows(tools)
    _set_schema(tools, 2)
    _upgrade(tools)

    mythic_task_records[TASK + 1] = SUCCESS
    asyncio.run(tools._observe_task_record(TASK + 1, CALLBACK))
    _capability(tools, TASK + 1)

    [row] = [row for row in _typed_rows(_reader()) if row.retraction is None]
    assert row.number == hop9.number
    assert [c["task_id"] for c in row.payload["contributions"]] == [str(TASK), str(TASK + 1)]
    assert row.payload["status"] == "achieved" and WORKING in _achieved(_reader())


def test_an_operator_retracted_old_row_is_not_folded(session):
    tools = session()
    _seed_history(tools, _historical_rows(tools._eng_key()))
    hop9, hop10 = _rows(tools)
    binding = tools._require_ledger_binding()
    tools._ledger_store().retract(binding, [str(hop10.number)], reason="operator removed",
                                  actor=engagement_store.Actor(kind="operator", name="op"))
    _set_schema(tools, 2)
    _upgrade(tools)

    owner, other = _rows(_reader())
    assert set(owner.payload["contributions"][0]["writers"]) == {f"legacy:{hop9.number}"}
    assert other.retraction["actor"] == "op" and "contributions" not in other.payload


def test_a_touch_that_adopts_an_old_row_records_the_revalidation(session, mythic_task_records):
    tools = session()
    _seed_history(tools, _historical_rows(tools._eng_key())[1:])  # only hop 10, written in a current-schema operation
    [old] = _rows(tools)
    mythic_task_records[TASK] = COMPLETION_ERROR
    asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    _capability(tools, TASK)

    [row] = _rows(_reader())
    assert row.row_id == old.row_id and row.payload["status"] == "pending"
    history = tools._ledger_store().history(tools._require_ledger_binding(), row_id=row.row_id)
    adoption = next(event for event in history if event["kind"] == "update")
    assert adoption["reason"] == engagement_store.REVALIDATION_REASON


# --- evidence display: task links and structured notes, no output -----------------------------------------------------

def _state_command(tools) -> str:
    model = SimpleNamespace(mythic_client=tools, mode="supervised", provider="test", model="test", state={})
    request = build_chat_request("x", channel_id=5, operation_id=0)
    return asyncio.run(slash._handle_state(model, request, ""))


def _database_text(tools) -> str:
    connection = sqlite3.connect(tools._ledger_store().path)
    try:
        rows = connection.execute("SELECT payload FROM esl_rows").fetchall()
        events = connection.execute("SELECT payload, reason FROM esl_events").fetchall()
    finally:
        connection.close()
    return json.dumps([list(row) for row in rows] + [list(event) for event in events])


def test_every_view_shows_task_links_and_structured_notes_never_output(session, mythic_issue, mythic_task_records):
    mythic_task_records[TASK] = COMPLETION_ERROR
    mythic_task_records[TASK + 1] = SUCCESS
    tools = session()
    _capability_issue(tools)  # task 18: material through the deterministic review
    asyncio.run(tools._observe_task_record(TASK + 1, CALLBACK))
    _capability(tools, TASK + 1)  # task 19: a later proving task appends its link and line
    [row] = _typed_rows(_reader())

    shown = _state_command(tools)
    model_view = engagement_ledger.take_operator_view(5)
    turn_end = asyncio.run(model_module.Model._engagement_state_turn_summary_text(SimpleNamespace(mythic_client=tools)))
    for text in (shown, model_view, turn_end):
        assert f"[{TASK}](/new/task/{TASK}), [{TASK + 1}](/new/task/{TASK + 1})" in text
        assert (f"- hop {row.number} · [task {TASK}](/new/task/{TASK}): proves `ntlm` material for "
                f"`{ACCOUNT}@{REALM}` · Mythic: completion-function error with output · admitted: credential review · "
                "not admitted: raw, capability · reviewer: deterministic (valid)") in text
        assert (f"- hop {row.number} · [task {TASK + 1}](/new/task/{TASK + 1}): proves working credential for "
                f"`{ACCOUNT}@{REALM}` · Mythic: terminal success · admitted: capability") in text
        assert NTLM not in text and "Hash NTLM" not in text and "SAM Username" not in text
    assert "Evidence |" not in shown

    stored = _database_text(tools)
    assert NTLM not in stored and "result_preview" not in stored


def test_a_pending_rows_candidate_task_is_linked_and_marked_as_not_proving(session, mythic_issue,
                                                                             mythic_task_records, monkeypatch):
    from ai.langgraph import mythic_tools as mythic_tools_module

    monkeypatch.setattr(mythic_tools_module.MythicTools, "_record_credential_review",
                        lambda self, state, **kwargs: state)
    mythic_task_records[TASK] = COMPLETION_ERROR
    tools = session()
    _capability_issue(tools)
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "pending"

    shown = _state_command(tools)
    assert (f"- hop {row.number} · [task {TASK}](/new/task/{TASK}): does not prove `{ACCOUNT}@{REALM}` · "
            "Mythic: completion-function error with output · not admitted: raw, capability") in shown


def test_historical_preview_text_is_never_rendered(session):
    tools = session()
    seed_ledger(tools, hops=[
        {"id": "kerberoast:svc", "technique": "kerberoast", "target": "svc", "effect": "da:corp.local",
         "status": "achieved", "evidence": {"source": "issue_task", "mythic_task_id": 11, "callback_id": 3,
                                             "result_preview": "TARGET-OUTPUT-PREVIEW",
                                             "task_classification": "terminal_success"}},
    ])
    shown = _state_command(tools)
    assert "TARGET-OUTPUT-PREVIEW" not in shown
    assert "[11](/new/task/11)" in shown
    assert "- hop 1 · [task 11](/new/task/11): proves `da:corp.local` · Mythic: terminal success · source: raw" in shown
    summary = model_module._build_esl_summary(tools)
    assert "TARGET-OUTPUT-PREVIEW" not in summary and "tasks 11" in summary


def test_note_values_are_escaped_and_only_numeric_tasks_are_links():
    hop = {"id": "x:y", "technique": "x", "target": "y", "effect": "da:`corp`", "status": "achieved", "number": 3,
           "evidence": {"mythic_task_id": "7) [y](http://z", "source": "[click](http://evil)",
                        "task_classification": "<b>bad</b>"}}
    other = dict(hop, number=4, evidence={"mythic_task_id": "\u0661\u0668"})  # non-ASCII digits are not a display ID
    text = "\n".join(engagement_ledger.render_hop_table([hop, other]) + engagement_ledger.evidence_notes([hop, other]))
    assert "/new/task/" not in text
    assert "[click](http://evil)" not in text.replace("`[click](http://evil)`", "")
    assert "<b>" not in text.replace("`<b>bad</b>`", "")
    assert "`da:corp`" in text  # backticks inside a value cannot close its code span


@pytest.mark.parametrize("hops", [[], [{"id": "a:b", "effect": "da:x", "status": "pending", "number": 1,
                                        "evidence": {}}]])
def test_rows_without_tasks_have_no_links_or_notes(hops):
    assert engagement_ledger.evidence_notes(hops) == []
    assert all("/new/task/" not in line for line in engagement_ledger.render_hop_table(hops))


def test_a_row_adopted_before_admitted_writers_existed_is_rederived_on_upgrade(session, mythic_task_records):
    """A touch under the group A-C code adopted hop 10 into a typed row whose stored head has no admitted_writers."""
    tools = session()
    _seed_history(tools, _historical_rows(tools._eng_key())[1:])
    mythic_task_records[TASK] = COMPLETION_ERROR
    asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    _capability(tools, TASK)
    [row] = _rows(tools)
    stale = dict(row.payload, contributions=[{k: v for k, v in c.items() if k != "admitted_writers"}
                                             for c in row.payload["contributions"]])
    connection = sqlite3.connect(tools._ledger_store().path)
    try:  # the head as that code stored it
        connection.execute("UPDATE esl_rows SET payload=? WHERE id=?", (json.dumps(stale), row.row_id))
        connection.commit()
    finally:
        connection.close()
    assert "proves working credential" not in "\n".join(engagement_ledger.evidence_notes([stale]))  # status pending

    _set_schema(tools, 2)
    _upgrade(tools)
    [after] = _rows(_reader())
    assert after.payload["status"] == "pending"
    assert all("admitted_writers" in c for c in after.payload["contributions"])
    shown = _state_command(_reader())
    assert f"- hop {row.number} · [task {TASK}](/new/task/{TASK}): does not prove" in shown
    assert "proves working credential" not in shown
