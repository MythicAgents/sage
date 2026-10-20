"""The approval card is the operator's decision instrument, and the engagement
ledger surfaces (reconciled) at every turn end.

Fail-before where the behavior replaces a designed one:
- the capability card prompt did not teach that Reject declines the bundled effect;
- make_card_emitter had no decision-context seam (TypeError on the new kwarg);
- a capability request whose plan cannot be built still consumed an approval card;
- turn ends streamed no engagement ledger;
- a failed hop stayed failed even when later verified task history proved the effect.
"""

import asyncio
import sys
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sage_chat import hitl as hitl_mod  # noqa: E402


# ── The card prompt teaches capability-specific Reject semantics ─────────────────────


def test_capability_card_prompt_teaches_effect_refusal():
    request = hitl_mod.build_approval_request([{
        "name": "execute_capability",
        "display_name": "execute_capability",
        "args": {
            "action": {"capability": "gpo-controlled-system-exec", "domain": "lab.local"},
            "inputs": {"callback_id": 1},
        },
    }])
    assert "Reject denies the whole bundled effect" in request["prompt"]
    assert "will not re-attempt it" in request["prompt"]


def test_raw_task_card_prompt_still_teaches_the_three_affordances():
    request = hitl_mod.build_approval_request([{
        "name": "issue_task_and_waitfor_task_output",
        "display_name": "issue_task",
        "args": {"command": "whoami", "callback_display_id": 1, "parameters": "{}"},
    }])
    assert "Accept approves" in request["prompt"]
    assert "Reject denies without any reply" in request["prompt"]
    assert "Respond denies the action" in request["prompt"]
    # The effect-refusal teaching is capability-specific; raw cards keep their existing wording.
    assert "bundled effect" not in request["prompt"]


# ── The decision-context seam on the emitter ─────────────────────────────────────────


class _FakeChat:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def send_approval_request(self, _request, **kwargs):
        self.kwargs = kwargs


def _fake_request() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        ChannelID=7,
        OperationID=1,
        APITokenID=1,
        InputResponse=None,
    )


def test_make_card_emitter_appends_decision_context():
    chat = _FakeChat()
    emitter = hitl_mod.make_card_emitter(
        chat,
        _fake_request(),
        decision_context_lookup=lambda _requests: "Planned inner tasks (2):\n  1. gpo-wait — wait for group-policy refresh",
    )
    action = {
        "name": "execute_capability",
        "display_name": "execute_capability",
        "args": {"action": {"capability": "gpo-controlled-system-exec", "domain": "lab.local"},
                 "inputs": {"callback_id": 1}},
    }
    asyncio.run(emitter([action]))
    assert chat.kwargs is not None
    assert "Planned inner tasks (2)" in chat.kwargs["description"]
    assert "gpo-wait" in chat.kwargs["description"]
    # The enrichment text rides INSIDE the approval context, so echo validation
    # (approval_response_matches) rebuilds the same description without any caller-side
    # lookup threading.
    assert chat.kwargs["data"]["sage_approval_context"]["decision_context"].startswith(
        "Planned inner tasks (2)"
    )


def test_approval_response_matches_validates_enriched_card_without_lookup():
    """The enrichment text rides inside the echoed context, so a caller with no lookup
    rebuilds the same description (the harness audits exactly this two-argument matcher
    call against a real echoed card — see test_offline_human_led_case)."""
    chat = _FakeChat()
    emitter = hitl_mod.make_card_emitter(
        chat,
        _fake_request(),
        decision_context_lookup=lambda _requests: "⚠️ Tool note (certutil):\nname the CA explicitly",
    )
    action = {
        "name": "issue_task_and_waitfor_task_output",
        "display_name": "issue_task_and_waitfor_task_output",
        "args": {"command": "shell", "callback_display_id": 1, "parameters": "{}"},
    }
    asyncio.run(emitter([action]))
    context = chat.kwargs["data"]["sage_approval_context"]
    rebuilt = hitl_mod.build_approval_request(
        context["actions"], decision_context=context["decision_context"])
    assert rebuilt["description"] == chat.kwargs["description"]


def test_make_card_emitter_lookup_failure_does_not_block_the_card():
    chat = _FakeChat()
    emitter = hitl_mod.make_card_emitter(
        chat,
        _fake_request(),
        decision_context_lookup=lambda _requests: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    action = {
        "name": "issue_task_and_waitfor_task_output",
        "display_name": "issue_task",
        "args": {"command": "whoami", "callback_display_id": 1, "parameters": "{}"},
    }
    asyncio.run(emitter([action]))
    assert chat.kwargs is not None
    assert "whoami" in chat.kwargs["description"]


def test_make_card_emitter_without_lookup_keeps_plain_description():
    chat = _FakeChat()
    emitter = hitl_mod.make_card_emitter(chat, _fake_request())
    action = {
        "name": "issue_task_and_waitfor_task_output",
        "display_name": "issue_task",
        "args": {"command": "whoami", "callback_display_id": 1, "parameters": "{}"},
    }
    asyncio.run(emitter([action]))
    assert chat.kwargs is not None
    assert "Approve: issue_task" == chat.kwargs["title"]


# ── The model-side plan render ───────────────────────────────────────────────────────


def _bare_model(monkeypatch: pytest.MonkeyPatch, mythic_client):
    from ai.langgraph import model as model_mod
    model = object.__new__(model_mod.Model)
    model.mythic_client = mythic_client
    model._turn_authority = None
    model._request_contract = None
    return model


def test_capability_card_plan_enumerates_inner_tasks(monkeypatch: pytest.MonkeyPatch):
    from ai.langgraph import model as model_mod
    from ai.langgraph.mythic_tools import MythicTools
    client = MythicTools(agent_task_id=1)
    model = object.__new__(model_mod.Model)
    model.mythic_client = client
    args = {
        "action": {
            "capability": "gpo-controlled-system-exec",
            "target": "lab.local",
            "domain": "lab.local",
            "gpo": "Sage GPO",
        },
        "inputs": {
            "callback_id": 1,
            "account": "samwell.tarly",
            "command": "cmd.exe",
            "arguments": 'net group "Domain Admins" samwell.tarly /add /domain',
        },
    }
    text = model._hitl_decision_context([{"name": "execute_capability", "args": args}])
    assert "Planned inner tasks" in text, text
    assert "gpo-computer-task" in text
    assert "Intended effects:" in text
    assert "gpo-domain-admin-membership-proof" in text


def test_capability_card_plan_failure_is_visible(monkeypatch: pytest.MonkeyPatch):
    from ai.langgraph import model as model_mod
    from ai.langgraph.mythic_tools import MythicTools
    client = MythicTools(agent_task_id=1)
    model = object.__new__(model_mod.Model)
    model.mythic_client = client
    args = {
        "action": {"capability": "gpo-controlled-system-exec", "domain": "lab.local",
                   "target": "lab.local"},
        "inputs": {"callback_id": 1},
    }
    text = model._hitl_decision_context([{"name": "execute_capability", "args": args}])
    assert "could not be built" in text, text
    assert "gpo" in text


def test_capability_card_without_client_renders_nothing():
    from ai.langgraph import model as model_mod
    model = object.__new__(model_mod.Model)
    model.mythic_client = None
    args = {"action": {"capability": "gpo-controlled-system-exec"}, "inputs": {}}
    assert model._hitl_decision_context([{"name": "execute_capability", "args": args}]) == ""


def test_issue_task_card_states_expected_verification():
    from ai.langgraph import model as model_mod
    model = object.__new__(model_mod.Model)
    model.mythic_client = None
    args = {
        "command": "mimikatz",
        "callback_display_id": 1,
        "parameters": '{"custom_preview": "lsadump::dcsync /domain:lab.local /user:krbtgt"}',
    }
    text = model._hitl_decision_context([{"name": "issue_task_and_waitfor_task_output", "args": args}])
    assert "Intended effect:" in text


def test_issue_task_card_unknown_command_renders_nothing():
    from ai.langgraph import model as model_mod
    model = object.__new__(model_mod.Model)
    model.mythic_client = None
    args = {"command": "whoami", "callback_display_id": 1, "parameters": "{}"}
    assert model._hitl_decision_context([{"name": "issue_task_and_waitfor_task_output", "args": args}]) == ""


# ── Proposals-JSON reports keep their machine-parseable format ──────────────────────


def test_closing_content_guard_classifies_proposals_json():
    from ai.langgraph import model as model_mod
    assert model_mod._closing_content_is_proposals_json('{"proposals":[]}')
    assert model_mod._closing_content_is_proposals_json(
        '{"proposals":[{"verify":["neutral condition requiring observation"]}]}')
    assert not model_mod._closing_content_is_proposals_json(
        '{"proposals":[],"extra":1}')
    assert not model_mod._closing_content_is_proposals_json("Prose report.")
    assert not model_mod._closing_content_is_proposals_json("")
    assert not model_mod._closing_content_is_proposals_json(None)
    assert not model_mod._closing_content_is_proposals_json("{not json")


# ── A bare capability reject teaches the effect-refusal policy to the model ──────────


def test_capability_rejection_message_names_effect_policy():
    from ai.langgraph import model as model_mod
    message = model_mod._capability_rejection_message(
        "execute_capability",
        {"action": {"capability": "gpo-controlled-system-exec", "domain": "lab.local"},
         "inputs": {}},
    )
    assert "gpo-controlled-system-exec" in message
    assert "do NOT re-attempt it in this request through issue_task" in message


def test_raw_task_rejection_keeps_generic_message():
    from ai.langgraph import model as model_mod
    message = model_mod._capability_rejection_message(
        "issue_task",
        {"command": "whoami", "callback_display_id": 1, "parameters": "{}"},
    )
    assert "[DENIED by operator] issue_task was not executed." in message


# ── The reconciled ledger surfaces at turn ends, changed-only ────────────────────────


class _FakeMythicClient:
    def __init__(self) -> None:
        self.engagement_key = f"test-{uuid.uuid4().hex[:12]}"
        self._last_issued_task_display_id = ""
        self.client = None
        self.streamed: list[str] = []

    def _eng_key(self) -> str:
        return self.engagement_key


def _summary_model(monkeypatch: pytest.MonkeyPatch, fake: _FakeMythicClient):
    from ai.langgraph import model as model_mod
    from ai.langgraph import engagement_ledger

    model = object.__new__(model_mod.Model)
    model.mythic_client = fake

    async def _stream(message: str) -> bool:
        fake.streamed.append(message)
        return True

    model._stream_message_to_mythic = _stream
    try:
        yield model, engagement_ledger
    finally:
        engagement_ledger.wipe(fake.engagement_key)


def test_turn_end_summary_renders_ledger(monkeypatch: pytest.MonkeyPatch):
    for model, ledger in _summary_model(monkeypatch, _FakeMythicClient()):
        text = asyncio.run(model._engagement_state_turn_summary_text())
        assert "Engagement state" in text
        assert "_(empty — no hops recorded yet)_" in text


def test_turn_end_summary_one_lines_unchanged_state(monkeypatch: pytest.MonkeyPatch):
    for model, ledger in _summary_model(monkeypatch, _FakeMythicClient()):
        first = asyncio.run(model._engagement_state_turn_summary_text())
        assert "Engagement state" in first
        second = asyncio.run(model._engagement_state_turn_summary_text())
        assert "unchanged" in second
        assert "| Hop |" not in second


def test_turn_end_reconciliation_corrects_failed_hop(monkeypatch: pytest.MonkeyPatch):
    """Status-only reconciliation runs at the turn end when tasks were issued.

    The fake reconciler injects a hop without a proof envelope, so save_runtime's
    quarantine discipline downgrades it to legacy_unverified — by design (a fake
    must not forge admissible proof). The wiring assertions: the reconciler ran
    status-only (apply=False) and the table reflects the reconciled ledger.
    """
    from ai.langgraph import state_reconcile as state_reconcile_mod
    from ai.langgraph import engagement_ledger

    calls: list[dict] = []

    async def _fake_reconcile(client, data, task_id, callback_id, limit, now, apply=False):
        calls.append({"apply": apply, "limit": limit, "task_id": task_id})
        data = dict(data)
        data["hops"] = [{
            "technique": "dcsync-krbtgt",
            "target": "lab.local",
            "effect": "krbtgt-hash:lab.local",
            "status": "achieved",
            "evidence": {"mythic_task_id": 26, "result_preview": "krbtgt: hash dumped"},
        }]
        return data, ["reconciled task 26"]

    original = state_reconcile_mod.reconcile_task_history
    state_reconcile_mod.reconcile_task_history = _fake_reconcile
    try:
        for model, ledger in _summary_model(monkeypatch, _FakeMythicClient()):
            fake = model.mythic_client
            fake._last_issued_task_display_id = 45
            fake.client = object()
            text = asyncio.run(model._engagement_state_turn_summary_text())
        assert calls and calls[0]["apply"] is False
        assert "dcsync-krbtgt" in text
        assert "26" in text
    finally:
        state_reconcile_mod.reconcile_task_history = original


def test_turn_end_reconciliation_runs_only_when_tasks_issued(monkeypatch: pytest.MonkeyPatch):
    from ai.langgraph import state_reconcile as state_reconcile_mod

    calls: list[dict] = []

    async def _fake_reconcile(client, data, task_id, callback_id, limit, now, apply=False):
        calls.append({"limit": limit})
        return data, []

    original = state_reconcile_mod.reconcile_task_history
    state_reconcile_mod.reconcile_task_history = _fake_reconcile
    try:
        for model, ledger in _summary_model(monkeypatch, _FakeMythicClient()):
            fake = model.mythic_client
            text = asyncio.run(model._engagement_state_turn_summary_text())
            assert calls == []  # no tasks issued → no reconciliation round-trip
            fake._last_issued_task_display_id = 45
            fake.client = object()
            text = asyncio.run(model._engagement_state_turn_summary_text())
            assert len(calls) == 1
    finally:
        state_reconcile_mod.reconcile_task_history = original
