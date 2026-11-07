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


def _summary_model(monkeypatch: pytest.MonkeyPatch):
    """A Model whose Mythic client is a real session bound to a fresh isolated ledger namespace."""
    from ai.langgraph import model as model_mod
    from ai.langgraph import mythic_tools

    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", f"test-{uuid.uuid4().hex[:12]}")
    client = mythic_tools.MythicTools(agent_task_id="turn-end")
    client._require_ledger_binding()
    model = object.__new__(model_mod.Model)
    model.mythic_client = client
    return model, client


def test_turn_end_summary_renders_ledger(monkeypatch: pytest.MonkeyPatch):
    model, _client = _summary_model(monkeypatch)
    text = asyncio.run(model._engagement_state_turn_summary_text())
    assert "Engagement state" in text
    assert "_(empty — no hops recorded yet)_" in text


def test_turn_end_summary_one_lines_unchanged_state(monkeypatch: pytest.MonkeyPatch):
    model, _client = _summary_model(monkeypatch)
    first = asyncio.run(model._engagement_state_turn_summary_text())
    assert "Engagement state" in first
    second = asyncio.run(model._engagement_state_turn_summary_text())
    assert "unchanged" in second
    assert "| Hop |" not in second


def test_turn_end_reconciliation_records_operator_history_without_a_sage_task(monkeypatch: pytest.MonkeyPatch):
    """Every turn end runs one pass over the operation's task history, whether or not this session issued a task.

    Only the Mythic transport is stubbed; the pass, verifiers, proof boundary and store are real.
    """
    from tests.esl_fixtures import stub_mythic_operation

    records = stub_mythic_operation(monkeypatch)
    records["tasks"] = [{
        "display_id": 26, "operation_id": 1, "command_name": "dcsync", "status": "completed", "completed": True,
        "original_params": '{"Domain": "lab.local", "User": "krbtgt"}', "timestamp": "",
        "operator": {"username": "operator"}, "callback": {"display_id": 3, "host": "WS01", "user": "jdoe"},
    }]
    records["outputs"] = {26: (  # synthetic, secret-free DCSync-shaped output
        "Object RDN           : krbtgt\n** SAM ACCOUNT **\nCredentials:\n"
        "  Hash NTLM: 0123456789abcdef0123456789abcdef\n* Primary:Kerberos-Newer-Keys *\n    Credentials\n"
        "      aes256_hmac       (4096) : " + "00112233445566778899aabbccddeeff" * 2 + "\n")}
    model, client = _summary_model(monkeypatch)
    assert asyncio.run(model._engagement_state_turn_summary_text()) and records["queries"] == 0  # no Mythic client
    client.client = object()
    assert getattr(client, "_last_issued_task_display_id", None) is None  # this session issued nothing
    text = asyncio.run(model._engagement_state_turn_summary_text())
    assert "krbtgt-hash:lab.local" in text and "26" in text
    # The krbtgt key is the account secret's typed row; reconciliation is its observation.
    assert [(row["technique"], row["target"]) for row in client._ledger_snapshot().display_hops()] == [
        ("account-secret", "krbtgt@lab.local")]
