"""Fresh-client account-context revalidation at the native capability boundary."""

import asyncio
import copy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.langgraph import capabilities  # noqa: E402
from ai.langgraph import engagement_state as es  # noqa: E402
from ai.langgraph import model  # noqa: E402
from ai.langgraph import mythic_tools as mt  # noqa: E402
from ai.langgraph.request_contract import action_spec_from_tool_call, build_request_contract  # noqa: E402
from ai.langgraph.turn_authority import authority_from_request_contract  # noqa: E402
from sage_chat.hitl import (  # noqa: E402
    approval_action_digest,
    approval_action_fingerprint,
    approval_proposal_digest,
    approval_selection_digest,
    build_approval_request,
)
from tests.test_autonomous_controller_wiring import (  # noqa: E402
    _bare_model,
    _install_exact_card_claim,
    _install_supervised_contract,
)


ACCOUNT = "alice"
ACCOUNT_DOMAIN = "lab.local"
CALLBACK = "7"
TARGET_HOST = "ws01"
TARGET_DOMAIN = "child.lab.local"
CONTEXT_EFFECT = f"kerberos-account-context:{ACCOUNT}@{ACCOUNT_DOMAIN}@callback:{CALLBACK}"
LAPS_FACT = (
    "can-read-managed-local-admin-secret:"
    f"account={ACCOUNT};account_domain={ACCOUNT_DOMAIN};"
    f"target={TARGET_HOST};target_domain={TARGET_DOMAIN}"
)


class _RuntimeState:
    runtime_scope = True
    objective = f"obtain administrative control of {TARGET_DOMAIN}"

    def __init__(self, *, current=(), identity=r"LAB\operator", forest=ACCOUNT_DOMAIN):
        self.current_account_contexts = frozenset(current)
        self.footholds = [es.Foothold(
            callback_id=CALLBACK,
            agent="apollo",
            host="entry",
            forest=forest,
            identity=identity,
            integrity="medium",
            alive=True,
            source="test",
            timestamp="",
        )]
        self.graph_facts = [es.GraphFact(
            predicate=LAPS_FACT,
            source="bloodhound:cypher",
            timestamp=datetime.now(timezone.utc).isoformat(),
            ttl_seconds=3600,
        )]
        collection_key = es.collection_target_key(self, self.footholds[0])
        self.hops = [es.Hop(
            id="collection-covered",
            technique="collect-graph",
            target=collection_key,
            effect=f"graph-built:{collection_key}",
            status="achieved",
            evidence={"graph_verified": True, "covered_domains": [forest]},
            preconditions=[],
            satisfied_effects=[f"graph-built:{collection_key}"],
            source="test",
            timestamp="",
        )]

    def achieved_effects(self):
        # Both are durable history. Only ``current_account_contexts`` or a live
        # exact-identity callback says whether the context is valid right now.
        return {f"creds:{ACCOUNT}@{ACCOUNT_DOMAIN}", CONTEXT_EFFECT}


def _actions(state, name):
    return [action for action in capabilities.actions_from_state(state) if action.name == name]


def _assembled_model(state):
    calls = []
    assembled = _install_supervised_contract(
        _bare_model("", state, calls),
        actions_complete=False,
        request_id="account-context-two-card",
        callback_ids=(CALLBACK,),
    )
    assembled.policy_mode = "symbolic"
    assembled.mythic_client = mt.MythicTools(agent_task_id=1)
    assembled.mythic_client._engagement_graph_facts = list(state.graph_facts)

    async def keep_inert_graph_cache(_now):
        return None

    assembled.mythic_client._refresh_graph_facts_if_stale = keep_inert_graph_cache
    # The dirty-root disclosure experiment currently previews only its GPO
    # branch. Keep this controller-delivery fixture inert for the separately
    # approved generic capability while retaining the real selector and cards.
    if hasattr(assembled.mythic_client, "_prepare_supervised_gpo_disclosure"):
        async def prepare_declared_test_capability(_action, inputs):
            return dict(inputs)
        assembled.mythic_client._prepare_supervised_gpo_disclosure = prepare_declared_test_capability
    assembled.install_request_contract(assembled._request_contract)
    return assembled, calls


def _guarded_client(action_request):
    contract = build_request_contract(
        request_id="request-revalidation",
        channel_id="channel-revalidation",
        operation_id="operation-revalidation",
        mode="supervised",
        autonomous_solve=True,
        requested_actions=(action_spec_from_tool_call(action_request),),
    )
    client = mt.MythicTools(agent_task_id=1)
    client.set_request_contract(contract)
    client.set_turn_authority(authority_from_request_contract(contract))
    digest = approval_action_digest([action_request])
    ids = [approval_action_fingerprint(action_request)]
    client.set_approval_claim({
        "approval_id": "approval-revalidation",
        "request_id": contract.request_id,
        "request_contract_digest": contract.digest,
        "tool_name": "execute_capability",
        "selection_mode": "single",
        "actions": [action_request],
        "approved_actions": [action_request],
        "approved_action_ids": ids,
        "action_digest": digest,
        "proposal_digest": approval_proposal_digest(contract.digest, digest),
        "selection_digest": approval_selection_digest(contract.digest, digest, ids),
    })
    return client


def test_fresh_runtime_reproposes_one_forced_context_card_before_laps_read():
    state = _RuntimeState()

    context_actions = _actions(state, "ensure-account-kerberos-context")
    assert len(context_actions) == 1
    assert _actions(state, "read-managed-local-admin-secret") == []
    action = context_actions[0]
    assert action.target == f"domain={ACCOUNT_DOMAIN};account={ACCOUNT};callback={CALLBACK}"
    assert action.intent["force_revalidate"] is True

    payload = model._capability_action_payload(action)
    inputs = model._autonomous_capability_inputs(action, state)
    assert inputs["callback_id"] == CALLBACK
    assert inputs["force_revalidate"] is True
    pending = object.__new__(model.Model)._controller_hitl_capability_request(
        payload, inputs, state.objective,
    )
    card = build_approval_request([{
        "name": pending["tool"],
        "display_name": pending["display_name"],
        "args": pending["args"],
    }])
    assert card["data"]["guarded_action_count"] == 1
    assert card["data"]["arguments"] == pending["args"]
    assert card["data"]["arguments"]["inputs"]["force_revalidate"] is True


def test_same_client_context_advances_to_separate_read_card_without_refresh():
    state = _RuntimeState(current={(CALLBACK, ACCOUNT, ACCOUNT_DOMAIN)})

    assert _actions(state, "ensure-account-kerberos-context") == []
    reads = _actions(state, "read-managed-local-admin-secret")
    assert len(reads) == 1
    inputs = model._autonomous_capability_inputs(reads[0], state)
    assert inputs["callback_id"] == CALLBACK
    assert "force_revalidate" not in inputs


@pytest.mark.parametrize("current", [
    {("8", ACCOUNT, ACCOUNT_DOMAIN)},
    {(CALLBACK, "bob", ACCOUNT_DOMAIN)},
    {(CALLBACK, ACCOUNT, "other.local")},
    {(CALLBACK, ACCOUNT, ACCOUNT_DOMAIN, "extra")},
])
def test_near_match_or_malformed_current_context_cannot_unlock_read(current):
    state = _RuntimeState(current=current)

    assert _actions(state, "read-managed-local-admin-secret") == []
    assert len(_actions(state, "ensure-account-kerberos-context")) == 1


def test_rejected_context_leaves_durable_history_but_still_denies_read():
    # A rejection/cancellation makes no current-process proof. A subsequent
    # state projection may re-surface context, but cannot expose the LAPS read.
    before = _RuntimeState()
    after_rejection = _RuntimeState()

    assert before.achieved_effects() == after_rejection.achieved_effects()
    assert _actions(after_rejection, "read-managed-local-admin-secret") == []
    assert len(_actions(after_rejection, "ensure-account-kerberos-context")) == 1


def test_live_exact_identity_is_current_proof_but_account_and_domain_near_matches_are_not():
    exact = _RuntimeState(identity=r"LAB\alice")
    wrong_account = _RuntimeState(identity=r"LAB\bob")
    wrong_domain = _RuntimeState(identity=r"OTHER\alice", forest="other.local")

    assert len(_actions(exact, "read-managed-local-admin-secret")) == 1
    assert _actions(exact, "ensure-account-kerberos-context") == []
    for state in (wrong_account, wrong_domain):
        assert _actions(state, "read-managed-local-admin-secret") == []
        assert len(_actions(state, "ensure-account-kerberos-context")) == 1


def test_projection_requires_both_process_proofs_and_existing_rev2self_invalidation():
    client = mt.MythicTools(agent_task_id=1)
    key = (CALLBACK, ACCOUNT, ACCOUNT_DOMAIN)
    client._kerberos_logon_account_context_keys = {key}
    assert client._current_account_context_keys() == frozenset()
    client._kerberos_account_context_keys = {key}
    assert client._current_account_context_keys() == frozenset({key})

    client._record_kerberos_logon_context("rev2self", int(CALLBACK), {}, "success")
    assert key not in client._kerberos_logon_account_context_keys
    assert key in client._kerberos_account_context_keys
    assert client._current_account_context_keys() == frozenset()


def test_verified_account_context_repopulates_both_proofs():
    client = mt.MythicTools(agent_task_id=1)
    action = _actions(_RuntimeState(), "ensure-account-kerberos-context")[0]

    key = client._record_verified_account_kerberos_context(action, {}, int(CALLBACK))

    assert key == (CALLBACK, ACCOUNT, ACCOUNT_DOMAIN)
    assert client._current_account_context_keys() == frozenset({key})


def test_force_revalidate_bypasses_durable_effect_and_repopulates_current_proof():
    client = mt.MythicTools(agent_task_id=1)
    client.client = object()
    client._capability_achieved_effects = lambda: {CONTEXT_EFFECT}
    action = _actions(_RuntimeState(), "ensure-account-kerberos-context")[0]
    calls = []

    async def noop(*_args, **_kwargs):
        return None

    async def achieved_preflight(current_action, inputs, callback_id, *_args):
        calls.append((current_action.name, inputs["force_revalidate"], callback_id))
        client._record_verified_account_kerberos_context(current_action, inputs, callback_id)
        return {"status": "achieved", "issued": [], "ran": True}

    client._augment_capability_runtime_inputs = noop
    client._bind_capability_mythic_adapter = noop
    client._ensure_capability_executor_proof_target = noop
    client._execute_capability_current_context_preflight = achieved_preflight
    payload = asyncio.run(client._execute_capability_impl(
        asdict(action), {"callback_id": CALLBACK, "force_revalidate": True},
    ))

    assert '"stopped_after": "current_context_preflight"' in payload
    assert calls == [("ensure-account-kerberos-context", True, int(CALLBACK))]
    assert client._current_account_context_keys() == frozenset({
        (CALLBACK, ACCOUNT, ACCOUNT_DOMAIN),
    })


def test_transient_projection_survives_same_process_state_copy_and_safe_client_fallback():
    key = (CALLBACK, ACCOUNT, ACCOUNT_DOMAIN)
    state = es.EngagementState(
        objective="test",
        current_account_contexts=frozenset({key}),
    )
    updated = es.record_effect_result(
        state, "test", "target", "effect", "failed", {}, "",
    )

    assert updated.current_account_contexts == frozenset({key})
    assert model._runtime_current_account_contexts(SimpleNamespace()) == frozenset()

    class BrokenProjector:
        def _current_account_context_keys(self):
            raise RuntimeError("projection unavailable")

    assert model._runtime_current_account_contexts(BrokenProjector()) == frozenset()


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_assembled_controller_separates_revalidation_and_read_cards(decision, monkeypatch):
    monkeypatch.setattr(
        "socket.socket.connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("offline controller test attempted network access")
        ),
    )
    state = _RuntimeState()
    assembled, calls = _assembled_model(state)
    cards = []

    async def emit(items):
        cards.append(copy.deepcopy(items))

    async def inert_effect(action, inputs):
        calls.append((copy.deepcopy(action), copy.deepcopy(inputs)))
        restored = capabilities.CapabilityAction(**action)
        assembled.mythic_client._record_verified_account_kerberos_context(
            restored, inputs, int(inputs["callback_id"]),
        )
        state.current_account_contexts = assembled.mythic_client._current_account_context_keys()
        return '{"ok": true, "verdict": "achieved"}'

    assembled._hitl_card_emitter = emit
    assembled._hitl_card_pending = False
    assembled._stream_message_to_mythic = lambda _text: asyncio.sleep(0, result=True)
    assembled.mythic_client.execute_capability = inert_effect

    async def episode():
        assert await assembled._run_autonomous_controller(state.objective) == ""
        assert len(cards) == 1 and calls == []
        first = cards[0][0]["args"]
        assert first["action"]["name"] == "ensure-account-kerberos-context"
        assert first["action"]["intent"]["force_revalidate"] is True
        assert first["inputs"]["force_revalidate"] is True
        digest = _install_exact_card_claim(assembled, cards[0], "account-context")
        await assembled.handle_controller_hitl_resume(
            decision,
            expected_action_digest=digest,
        )

    asyncio.run(episode())
    if decision == "deny":
        assert calls == []
        assert not any(
            card[0]["args"].get("action", {}).get("name")
            == "read-managed-local-admin-secret"
            for card in cards
        )
        assert state.current_account_contexts == frozenset()
        assert _actions(state, "read-managed-local-admin-secret") == []
    else:
        assert [item[0]["name"] for item in calls] == ["ensure-account-kerberos-context"]
        assert assembled.mythic_client._current_account_context_keys() == frozenset({
            (CALLBACK, ACCOUNT, ACCOUNT_DOMAIN),
        })
        assert len(cards) == 2
        second = cards[1][0]["args"]
        assert second["action"]["name"] == "read-managed-local-admin-secret"
        assert "force_revalidate" not in second["inputs"]


def test_legacy_nested_context_fallback_is_json_but_remains_outside_read_approval():
    read_action = _actions(
        _RuntimeState(current={(CALLBACK, ACCOUNT, ACCOUNT_DOMAIN)}),
        "read-managed-local-admin-secret",
    )[0]
    root = {
        "name": "execute_capability",
        "args": {"action": asdict(read_action), "inputs": {"callback_id": CALLBACK}},
    }
    client = _guarded_client(root)
    with pytest.raises(PermissionError, match="approved proposal does not cover this exact effect"):
        asyncio.run(client._execute_capability_account_context_prerequisite(
            read_action,
            {"callback_id": CALLBACK},
            int(CALLBACK),
            None,
            capabilities,
        ))


def test_exact_separately_approved_context_passes_real_guard_near_and_malformed_do_not():
    stale = _RuntimeState()
    action = _actions(stale, "ensure-account-kerberos-context")[0]
    args = {
        "action": asdict(action),
        "inputs": model._autonomous_capability_inputs(action, stale),
    }
    request = {"name": "execute_capability", "args": args}
    client = _guarded_client(request)

    assert client._request_contract_identity_blocker("execute_capability", args) == ""
    for near in (
        {**args, "action": {**args["action"], "intent": {**args["action"]["intent"], "account": "bob"}}},
        {**args, "action": {**args["action"], "intent": {**args["action"]["intent"], "domain": "other.local"}}},
        {**args, "inputs": {**args["inputs"], "callback_id": "8"}},
        {**args, "inputs": {**args["inputs"], "unexpected_override": True}},
    ):
        assert "does not cover this exact effect" in client._request_contract_identity_blocker(
            "execute_capability", near,
        )
    malformed = {"action": object(), "inputs": args["inputs"]}
    assert "non-JSON value object" in client._request_contract_identity_blocker(
        "execute_capability", malformed,
    )
