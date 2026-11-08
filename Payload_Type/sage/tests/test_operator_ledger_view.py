"""Permanent hop numbers shared by every ledger view, and the operator-facing `/state` and turn-end tables.

Hops carry permanent numbers shared by every view. The model no longer receives a copy of a table the operator was
shown: every Supervisor and Mythic_Operator call carries its own fresh view of the same rows (see
test_engagement_ledger_phase5_fresh_awareness.py), so these operator surfaces queue nothing for the model.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.langgraph import engagement_ledger as el
from ai.langgraph import model as graph_model
from ai.langgraph import mythic_tools
from ai.langgraph import engagement_state
from sage_chat import slash
from sage_chat.headless import build_chat_request
from tests.esl_fixtures import seed_ledger


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))


# ── permanent hop numbers (allocated by the shared store) ───────────────────────────────


def _session(namespace="numbers"):
    mythic_tools.SAGE_ENGAGEMENT_ID = namespace
    return mythic_tools.MythicTools(agent_task_id="solve-1")


def _pending(*targets):
    state = engagement_state.EngagementState(objective="test")
    for target in targets:
        state = engagement_state.record_hop_result(
            state, "gpo-abuse", target, "pending", {"source": "test"}, "2026-10-29T00:00:00Z",
        )
    return state.hops


def _numbers(mt):
    return [(hop["target"], hop["number"]) for hop in mt._ledger_snapshot().display_hops()]


def _state(mt, arg, channel_id=5):
    model = SimpleNamespace(mythic_client=mt, mode="supervised", provider="test", model="test", state={})
    request = build_chat_request("x", channel_id=channel_id, operation_id=0)
    return asyncio.run(slash._handle_state(model, request, arg))


@pytest.fixture
def _restore_namespace(monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", mythic_tools.SAGE_ENGAGEMENT_ID)


def test_numbers_are_assigned_once_in_creation_order(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local", "three.local"))
    assert _numbers(mt) == [("one.local", 1), ("two.local", 2), ("three.local", 3)]
    seed_ledger(mt, hops=_pending("two.local"))  # an identical re-observation allocates nothing
    assert _numbers(mt) == [("one.local", 1), ("two.local", 2), ("three.local", 3)]
    assert mt._ledger_snapshot().next_hop_number == 4


def test_retraction_keeps_the_row_its_number_and_later_numbers(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local", "three.local"))
    shown = _state(mt, "remove 1 --reason duplicate")
    assert "Retracted 1 hop(s)" in shown and "operator retracted" in shown and "duplicate" in shown
    assert _numbers(mt) == [("one.local", 1), ("two.local", 2), ("three.local", 3)]
    assert [hop.target for hop in mt._engagement_hops] == ["two.local", "three.local"]


def test_a_number_is_never_reused_after_wipe(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local"))
    _state(mt, "wipe")
    assert mt._engagement_hops == []
    seed_ledger(mt, hops=_pending("new.local"))
    assert _numbers(mt)[-1] == ("new.local", 3)


def test_a_session_round_trip_keeps_stored_numbers_and_revisions(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local", "three.local"))
    _state(mt, "remove 1")
    other = _session()  # a second session of the same namespace, writing after the operator's retraction
    hops = list(other._engagement_hops)
    assert [(hop.number, hop.revision) for hop in hops] == [(2, 1), (3, 1)]
    updated = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test", hops=hops),
        "gpo-abuse", "four.local", "pending", {"source": "test"}, "2026-10-29T00:00:00Z",
    )
    assert [hop.number for hop in updated.hops] == [2, 3, 0]  # dataclasses keep stored numbers; new rows have none
    assert other._ledger_commit_hops(hops, updated.hops)
    assert _numbers(mt) == [("one.local", 1), ("two.local", 2), ("three.local", 3), ("four.local", 4)]
    assert "operator retracted" in _state(mt, "")


@pytest.mark.parametrize("selector", ["0", "5", "-1", "1.0"])
def test_selector_that_is_not_an_existing_hop_number_matches_nothing(_restore_namespace, selector):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local"))
    assert "Retracted 0 hop(s)" in _state(mt, f"remove {selector}")
    assert len(mt._engagement_hops) == 2


def test_state_set_cannot_set_a_status_and_changes_nothing(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local"))
    assert "Only `achieved` can be set" in _state(mt, "set 1 failed")
    assert mt._engagement_hops[0].status == "pending"


# ── one shared table ─────────────────────────────────────────────────────────────────────


def _hops():
    return [
        {"id": "kerberoast:svc", "effect": "creds:svc@corp.local", "status": "achieved", "number": 1,
         "evidence": {"mythic_task_id": 11, "callback_id": 3, "result_preview": "TARGET-OUTPUT-PREVIEW"}},
        {"id": "dcsync:corp.local", "effect": "krbtgt-hash:corp.local", "status": "pending", "number": 4,
         "evidence": {"mythic_task_id": 12}},
    ]


def test_one_table_for_every_view_links_tasks_and_never_shows_output():
    table = "\n".join(el.render_hop_table(_hops()))
    assert "| 4 | `dcsync:corp.local` | krbtgt-hash:corp.local | `pending (awaiting effect)` | [12](/new/task/12) | - |" \
        in table
    assert "TARGET-OUTPUT-PREVIEW" not in table and "Evidence" not in table


def test_table_limit_keeps_newest_numbers():
    lines = el.render_hop_table(_hops(), limit=1)
    assert len(lines) == 3 and lines[2].startswith("| 4 |")


def test_operator_view_copy_plumbing_is_retired():
    for name in ("record_operator_view", "take_operator_view", "operator_view_text", "_OPERATOR_VIEWS"):
        assert not hasattr(el, name), name


# ── /state and the turn-end table: unchanged for the operator, no model copy ───────────


def _seeded_view_session():
    mt = _session("view")
    seed_ledger(mt, hops=[
        {"id": "kerberoast:svc", "technique": "kerberoast", "target": "svc", "effect": "creds:svc@corp.local",
         "status": "achieved", "evidence": {"mythic_task_id": 11, "callback_id": 3,
                                             "result_preview": "TARGET-OUTPUT-PREVIEW"}},
        {"id": "dcsync:corp.local", "technique": "dcsync", "target": "corp.local",
         "effect": "krbtgt-hash:corp.local", "status": "pending", "evidence": {"mythic_task_id": 12}},
    ])
    mt._ledger_store().update_objective(mt._require_ledger_binding(), "compromise CORP", "operator")
    return mt


def test_state_command_shows_the_rows_unchanged(_restore_namespace):
    mt = _seeded_view_session()
    shown = _state(mt, "")
    assert "| 2 | `dcsync:corp.local` | krbtgt-hash:corp.local | `pending (awaiting effect)` | [12](/new/task/12) |" \
        in shown
    assert "TARGET-OUTPUT-PREVIEW" not in shown
    assert "compromise CORP" in shown


def test_turn_end_table_shows_the_rows_and_unchanged_on_repeat(_restore_namespace):
    mt = _seeded_view_session()
    model = object.__new__(graph_model.Model)
    model.mythic_client = mt
    model.channel_id = 7
    shown = asyncio.run(model._engagement_state_turn_summary_text())
    assert "| 1 | `kerberoast:svc` |" in shown and "| 2 | `dcsync:corp.local` |" in shown
    assert "unchanged" in asyncio.run(model._engagement_state_turn_summary_text())

    # A retraction changes neither technique, target nor historical status, but it is a change.
    _state(mt, "remove 1")
    shown = asyncio.run(model._engagement_state_turn_summary_text())
    assert "unchanged" not in shown and "operator retracted" in shown


# ── the assembled request path ───────────────────────────────────────────────────────────


def _inventory_model(channel_id):
    """A minimal Model driven through invoke() on the deterministic callback-inventory path."""
    m = object.__new__(graph_model.Model)
    m.mode = "supervised"
    m.command_name = "chat"
    m.channel_id = channel_id
    m._autonomous_solve = False
    m._controller_hitl_pending = None
    m._thread_id_override = f"channel-ledger-view-{channel_id}"
    m._running_tasks = set()
    m._message_seq = 1
    m.graph = object()
    m.state = {"messages": [], "_message_seq": 1}
    m.provider = "test"
    m.model = "test"
    m.is_interactive = True
    streamed = []

    class FakeMythic:
        async def list_callbacks(self):
            return [{"id": 1, "agent": "apollo", "host": "TEST-HOST", "user": "operator",
                     "integrity": 2, "status": "alive", "secs_since_checkin": 1}]

    async def no_interrupt(_thread_id):
        return False

    async def stream(text):
        streamed.append(text)
        return True

    m.mythic_client = FakeMythic()
    m._hitl_interrupt_pending = no_interrupt
    m._seed_autonomous_objective = lambda _prompt: None
    m._format_message_for_streaming = graph_model.Model._format_message_for_streaming.__get__(m)
    m._stream_message_to_mythic = stream
    return m, streamed


def _drive(m):
    assert asyncio.run(m.invoke("list_callbacks", is_interactive=True)) == ""
    return [
        msg for msg in m.state["supervisor_messages"]
        if isinstance(msg, graph_model.HumanMessage)
    ]


def test_a_request_adds_no_ledger_copy_to_the_supervisor_history():
    m, streamed = _inventory_model(9)
    humans = _drive(m)
    assert [msg.content for msg in humans] == ["list_callbacks"]
    assert not any("ledger view" in str(msg.content) for msg in m.state["supervisor_messages"])
    assert not any("ledger view" in text for text in streamed)


# ── the stored compatibility objective: labelled as an operation setting, never a placeholder ───────────────────
#
# The operation's objective setting is a compatibility bridge, not a channel goal (channel goals are not
# implemented). Every operator surface labels it as such, and an internal `sage-engagement` identifier or blank
# value shows no objective line at all.

LABEL = "Stored compatibility objective (operation setting, not a channel goal):"
PLACEHOLDERS = [None, "", "   ", "sage-engagement", "SAGE-Engagement", "  sage-engagement:solve-7  ",
                "Sage-Engagement:"]
HUMAN = ["compromise CORP", "sage-engagement-review", "  review the sage-engagement: notes  "]
TEMPORARY = "Display validation only; no target action requested."
# conversation, supervised, supervised with Autonomous Solve, auto: the same slash and summary code serves each.
LANES = [("conversation", False), ("supervised", False), ("supervised", True), ("auto", True)]


def _view_session(objective, namespace="phase6"):
    mt = _session(namespace)
    seed_ledger(mt, hops=[
        {"id": "kerberoast:svc", "technique": "kerberoast", "target": "svc", "effect": "creds:svc@corp.local",
         "status": "achieved", "evidence": {"mythic_task_id": 11, "callback_id": 3}},
        {"id": "dcsync:corp.local", "technique": "dcsync", "target": "corp.local",
         "effect": "krbtgt-hash:corp.local", "status": "pending", "evidence": {"mythic_task_id": 12}},
    ])
    if objective is not None:
        mt._ledger_store().update_objective(mt._require_ledger_binding(), objective, "operator")
    return mt


def _objective_lines(text):
    """Lines presenting an objective. The edit footer's `/state objective <text>` usage is not one."""
    return [line for line in text.splitlines()
            if "objective" in line.casefold() and "/state objective" not in line]


def _shows_placeholder(text, stored):
    """Whether a stored placeholder's text appears on any line other than the engagement-key header."""
    needle = str(stored or "").strip().casefold()
    return bool(needle) and any(needle in line.casefold() for line in text.splitlines()
                                if not line.startswith(("**Engagement state", "📊 **Engagement state")))


def _fingerprint(mt):
    snapshot = mt._ledger_snapshot()
    exported = mt._ledger_store().export(mt._require_ledger_binding())
    return snapshot.objective_setting(), snapshot.change_signature(), exported["revision"], len(exported["history"])


def _state_in(mt, arg, mode="supervised", autonomous_solve=False):
    model = SimpleNamespace(mythic_client=mt, mode=mode, provider="test", model="test", state={},
                            _autonomous_solve=autonomous_solve)
    return asyncio.run(slash._handle_state(model, build_chat_request("x", channel_id=5, operation_id=0), arg))


@pytest.mark.parametrize("mode,autonomous_solve", LANES)
@pytest.mark.parametrize("stored", PLACEHOLDERS)
def test_state_shows_no_objective_line_for_a_blank_or_placeholder_setting(_restore_namespace, stored, mode,
                                                                          autonomous_solve):
    mt = _view_session(stored)
    before = _fingerprint(mt)
    shown = _state_in(mt, "", mode, autonomous_solve)
    assert _objective_lines(shown) == []
    assert not _shows_placeholder(shown, stored) and "solve-7" not in shown
    assert "| 1 | `kerberoast:svc` |" in shown and "| 2 | `dcsync:corp.local` |" in shown
    assert _fingerprint(mt) == before  # rendering sets, clears and adopts nothing


@pytest.mark.parametrize("mode,autonomous_solve", LANES)
@pytest.mark.parametrize("stored", HUMAN)
def test_state_labels_a_human_objective_as_an_operation_compatibility_setting(_restore_namespace, stored, mode,
                                                                             autonomous_solve):
    mt = _view_session(stored)
    before = _fingerprint(mt)
    shown = _state_in(mt, "", mode, autonomous_solve)
    assert _objective_lines(shown) == [f"**{LABEL}** {stored.strip()}"]
    assert _fingerprint(mt) == before


def test_the_objective_line_probe_detects_a_bypassed_placeholder_filter(_restore_namespace, monkeypatch):
    """Negative control: with the filter bypassed, the same probe sees the placeholder, so its silence is real."""
    monkeypatch.setattr(mythic_tools.MythicTools, "_human_engagement_objective",
                        staticmethod(lambda value: str(value or "").strip()))
    mt = _view_session("sage-engagement:solve-7")
    assert _objective_lines(_state_in(mt, "")) == [f"**{LABEL}** sage-engagement:solve-7"]
    model = object.__new__(graph_model.Model)
    model.mythic_client = mt
    assert f"{LABEL} sage-engagement:solve-7" in _objective_lines(
        asyncio.run(model._engagement_state_turn_summary_text()))


def _summary_model(mt, mode="supervised", autonomous_solve=False):
    model = object.__new__(graph_model.Model)
    model.mythic_client = mt
    model.channel_id = 7
    model.mode = mode
    model._autonomous_solve = autonomous_solve
    return model


def _delete_objective_settings(mt):
    """The live protocol's restoration: remove exactly the two compatibility rows (there is no clear command)."""
    import sqlite3

    from contextlib import closing

    binding = mt._require_ledger_binding()
    with closing(sqlite3.connect(binding.database_path)) as connection, connection:
        deleted = connection.execute(
            "DELETE FROM esl_legacy_settings WHERE operation_id = ? AND name IN ('objective', 'objective_source')",
            (binding.store_operation_id,),
        ).rowcount
    assert deleted == 2


@pytest.mark.parametrize("mode,autonomous_solve", LANES)
def test_turn_end_table_labels_the_objective_and_redraws_on_every_setting_transition(_restore_namespace, mode,
                                                                                    autonomous_solve):
    mt = _view_session(None)
    model = _summary_model(mt, mode, autonomous_solve)

    def summary():
        return asyncio.run(model._engagement_state_turn_summary_text())

    first = summary()
    assert _objective_lines(first) == [] and "| 1 | `kerberoast:svc` |" in first
    assert summary() == "📊 Engagement state unchanged since the last turn."

    set_reply = _state_in(mt, f"objective {TEMPORARY}", mode, autonomous_solve)  # the product setter
    assert "Stored compatibility objective updated (operation setting, not a channel goal)." in set_reply
    assert _objective_lines(set_reply)[-1] == f"**{LABEL}** {TEMPORARY}"
    shown = summary()
    assert "unchanged" not in shown
    assert _objective_lines(shown) == [f"{LABEL} {TEMPORARY}"]
    assert "| 1 | `kerberoast:svc` |" in shown and "| 2 | `dcsync:corp.local` |" in shown
    assert summary() == "📊 Engagement state unchanged since the last turn."

    _delete_objective_settings(mt)  # restoring the absent setting is a change the table must show
    assert _objective_lines(_state_in(mt, "")) == []
    restored = summary()
    assert "unchanged" not in restored and _objective_lines(restored) == []
    assert "| 1 | `kerberoast:svc` |" in restored

    mt._ledger_store().update_objective(mt._require_ledger_binding(), "sage-engagement:solve-9", "autonomous")
    assert summary() == "📊 Engagement state unchanged since the last turn."  # a placeholder shows as no objective


def _closing_model(mt, mode, autonomous_solve):
    from ai.langgraph.request_events import stable_event_id

    sent = []

    async def emitter(text):
        sent.append(text)
        return True

    model = _summary_model(mt, mode, autonomous_solve)
    model._response_emitter = emitter
    model._active_delegations = {}
    model._streamed_supervisor_message_keys = set()
    model.verbose = False
    model.begin_visibility_turn("request:phase6", operator_prompt="read rows", native_request_id="1",
                                logical_request_id="request:phase6")
    model._request_event_ledger.record_once(
        event_id=stable_event_id("request:phase6", "control_transition", "contract-installed"),
        kind="control_transition", phase="request_installed", content="request contract installed")
    return model, sent


@pytest.mark.parametrize("stored,expected", [("SAGE-Engagement", []), (TEMPORARY, [f"{LABEL} {TEMPORARY}"])])
@pytest.mark.parametrize("mode,autonomous_solve", LANES)
def test_the_final_report_carries_the_labelled_table_in_one_closing_response(_restore_namespace, stored, expected,
                                                                            mode, autonomous_solve):
    from langchain_core.messages import AIMessage

    mt = _view_session(stored)
    model, sent = _closing_model(mt, mode, autonomous_solve)
    report = AIMessage(content="Rows 1 and 2 are recorded.", name="Supervisor",
                       additional_kwargs={"_is_final_report": True, "_turn_outcome": "completed"})
    asyncio.run(model._process_stream_event({"Supervisor": {"supervisor_messages": [report]}}))

    assert len(sent) == 1
    assert "Rows 1 and 2 are recorded." in sent[0] and "📊 **Engagement state**" in sent[0]
    assert _objective_lines(sent[0]) == expected
    assert stored == TEMPORARY or not _shows_placeholder(sent[0], stored)
    assert "| 1 | `kerberoast:svc` |" in sent[0] and "| 2 | `dcsync:corp.local` |" in sent[0]


@pytest.mark.parametrize("stored,expected", [("sage-engagement:solve-7", []), (TEMPORARY, [f"{LABEL} {TEMPORARY}"])])
@pytest.mark.parametrize("mode,autonomous_solve", LANES)
def test_the_stop_notice_carries_the_labelled_table_in_one_closing_response(_restore_namespace, stored, expected,
                                                                           mode, autonomous_solve):
    mt = _view_session(stored)
    model, sent = _closing_model(mt, mode, autonomous_solve)
    asyncio.run(model._emit_operator_stop("Session stopped."))

    assert len(sent) == 1
    assert sent[0].startswith("Session stopped.\n\n📊 **Engagement state**")
    assert _objective_lines(sent[0]) == expected
    assert stored == TEMPORARY or not _shows_placeholder(sent[0], stored)
    assert "| 1 | `kerberoast:svc` |" in sent[0]


@pytest.mark.parametrize("arg", ["set 1 invalid-phase6-status", "set 1 failed", "set 1 achieved"])
def test_arbitrary_or_uncited_state_set_is_refused_without_a_change(_restore_namespace, arg):
    mt = _view_session(TEMPORARY)
    before = _fingerprint(mt)
    reply = _state_in(mt, arg)
    assert reply.startswith("⚠️") and "never takes the secret itself" in reply
    assert _fingerprint(mt) == before
