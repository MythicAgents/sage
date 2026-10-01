"""Fresh-process style controller journal probes; no Mythic or provider calls."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.langgraph.autonomous_controller import AutonomousController, ControllerConfig, STATUS_COMPLETE
from ai.langgraph.controller_recovery import ControllerRecoveryStore, RecoveryBlocked
from ai.langgraph.policy import LLMPolicy


class Decision:
    def __init__(self, episode_id, candidate, index, number):
        self.decision_id = f"decision-{number}"
        self.episode_id = episode_id
        self.policy_mode = "test"
        self.candidate_hash = "test-frontier"
        self.disposition = "select"
        self.selected_index = index
        self.selected_capability = candidate.name
        self.selected_target = candidate.target

    def to_dict(self):
        return dict(vars(self))


class Policy:
    mode = "test"

    def __init__(self, calls):
        self.calls = calls

    async def select(self, *, episode_id, candidates, **_kwargs):
        self.calls.append(candidates[0].name)
        return Decision(episode_id, candidates[0], 0, len(self.calls))


def _controller(store, binding, world, policy_calls, effect_calls, *, restored=None, crash_at_cycle=0,
                crash_effect=False):
    def observe():
        return SimpleNamespace(achieved_effects=lambda: set(world))

    def frontier(_state):
        for effect in ("first", "second"):
            if effect not in world:
                return [SimpleNamespace(name=effect, target="target", effects=[effect])]
        return []

    def execute(action):
        effect_calls.append(action.name)
        if crash_effect:
            raise asyncio.CancelledError()
        world.add(action.name)
        return {"ok": True}

    def should_abort():
        if crash_at_cycle and controller._current_cycle == crash_at_cycle:
            raise asyncio.CancelledError()
        return False

    controller = AutonomousController(
        observe=observe,
        execute=execute,
        objective_met=lambda _state: "second" in world,
        frontier_fn=frontier,
        policy_backend=Policy(policy_calls),
        should_abort=should_abort,
        episode_id="episode-1",
        config=ControllerConfig(max_cycles=3, token_budget=None, wall_clock_budget_s=None),
        recovery_store=store,
        recovery_binding=binding,
        recovery_state=restored,
        recovery_resources=lambda: {"model_calls": len(policy_calls)},
    )
    if restored is None:
        controller._recovery_snapshot.update({"original_request_id": "chat:2:request:7", "objective": "objective"})
        store.write(binding, "starting", controller._recovery_snapshot)
    else:
        controller._recovery_snapshot.update(restored)
    return controller


def test_clean_cycle_resumes_without_repeating_policy_or_action(tmp_path):
    path = str(tmp_path / "sage.db")
    binding = {"operation_id": 1, "channel_id": 2, "contract": "original"}
    world, policy_calls, effect_calls = set(), [], []
    first = ControllerRecoveryStore(1, 2, db_path=path)
    first.acquire()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_controller(first, binding, world, policy_calls, effect_calls,
                                crash_at_cycle=2).run())
    first.release()
    saved = ControllerRecoveryStore(1, 2, db_path=path)
    assert saved.inspect()["phase"] == "clean"
    state = saved.require_clean(binding, "chat:2:request:7")
    assert [item["action"] for item in state["cycles"]] == ["first"]
    assert state["resources"]["model_calls"] == 1
    saved.acquire()
    result = asyncio.run(_controller(saved, binding, world, policy_calls, effect_calls,
                                     restored=state).run())
    saved.release()
    assert result.status == STATUS_COMPLETE
    assert result.cycle_count == 2
    assert effect_calls == ["first", "second"]
    assert policy_calls == ["first", "second"]
    assert saved.inspect()["phase"] == "terminal_pending"


def test_pending_effect_and_wrong_binding_block(tmp_path):
    path = str(tmp_path / "sage.db")
    binding = {"operation_id": 1, "channel_id": 2, "contract": "original"}
    store = ControllerRecoveryStore(1, 2, db_path=path)
    store.acquire()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_controller(store, binding, set(), [], [], crash_effect=True).run())
    assert store.inspect()["active"] is True
    store.release()
    assert store.inspect()["phase"] == "pending_execute"
    with pytest.raises(RecoveryBlocked, match="pending_execute"):
        store.require_clean(binding, "chat:2:request:7")
    with pytest.raises(RecoveryBlocked, match="binding changed"):
        store.require_clean({**binding, "contract": "different"}, "chat:2:request:7")


def test_inspect_missing_store_is_read_only(tmp_path):
    path = tmp_path / "sage.db"
    store = ControllerRecoveryStore(1, 2, db_path=str(path))
    assert store.inspect() == {
        "exists": False, "active": False, "phase": "", "original_request_id": "", "terminal_status": ""
    }
    assert not path.exists()
    assert not Path(f"{path}.controller.1.2.lock").exists()


def test_bounded_auto_recovery_refused_before_controller_or_provider(monkeypatch):
    from ai.langgraph.model import Model

    monkeypatch.setenv("SAGE_AUTO_CONTROLLER_RECOVERY", "1")
    monkeypatch.delenv("SAGE_EVAL_UNBOUNDED_ARM", raising=False)
    model = object.__new__(Model)
    model.mode = "auto"
    model.channel_id = 2
    with pytest.raises(RecoveryBlocked, match="explicitly unbounded"):
        asyncio.run(model._run_autonomous_controller("objective"))


def test_real_llm_policy_receives_rehydrated_prior_decision(tmp_path):
    path = str(tmp_path / "sage.db")
    binding = {"operation_id": 1, "channel_id": 2, "contract": "original"}
    world, policy_calls, effect_calls = set(), [], []
    store = ControllerRecoveryStore(1, 2, db_path=path)
    store.acquire()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_controller(store, binding, world, policy_calls, effect_calls,
                                crash_at_cycle=2).run())
    store.release()
    saved = store.require_clean(binding, "chat:2:request:7")
    assert saved["decisions"], saved
    observed_requests = []

    def decide(request):
        observed_requests.append(request)
        return {"disposition": "stop", "rationale": "bounded probe"}

    store.acquire()
    controller = _controller(store, binding, world, policy_calls, effect_calls, restored=saved)
    controller._policy = LLMPolicy(decide, catalog=[{"name": "second"}],
                                   completed_call_token_ceiling=1000)
    result = asyncio.run(controller.run())
    store.release()
    assert observed_requests, result.blocker
    assert observed_requests[0]["prior_decisions"], (saved["decisions"], observed_requests, result.to_dict())
    assert observed_requests[0]["prior_decisions"][0]["decision_id"] == "decision-1"
    assert result.cycle_count == 2
    assert effect_calls == ["first"]


def test_explicit_stop_revokes_clean_run_even_without_model(tmp_path, monkeypatch):
    from sage_chat import slash
    from sage_chat.headless import build_chat_request
    from ai.langgraph import controller_recovery as recovery
    from ai.langgraph import model as model_module

    path = str(tmp_path / "sage.db")
    class FixedStore(ControllerRecoveryStore):
        def __init__(self, operation_id, channel_id):
            super().__init__(operation_id, channel_id, db_path=path)

    monkeypatch.setenv("SAGE_AUTO_CONTROLLER_RECOVERY", "1")
    monkeypatch.setattr(recovery, "ControllerRecoveryStore", FixedStore)
    async def no_sessions(_key):
        return {}
    monkeypatch.setattr(model_module, "request_stop_for_sessions", no_sessions)
    store = FixedStore(1, 2)
    binding = {"run": "original"}
    store.acquire()
    store.write(binding, "clean", {"original_request_id": "chat:2:request:7"})
    store.release()
    request = build_chat_request("", operation_id=1, channel_id=2)
    asyncio.run(slash._handle_stop(request, None))
    assert store.inspect()["phase"] == "revoked"
    store.acquire()
    with pytest.raises(RecoveryBlocked, match="revoked"):
        store.write(binding, "clean", {"original_request_id": "chat:2:request:7"})
    store.release()


def test_dedicated_model_resume_skips_fresh_turn_completion_preflight():
    from ai.langgraph.model import Model
    from ai.langgraph.request_contract import build_request_contract

    model = object.__new__(Model)
    contract = build_request_contract(request_id="chat:2:request:7", channel_id="2",
                                      operation_id="1", mode="auto", autonomous_solve=True)
    model._request_contract = contract
    model._controller_resume_state = {
        "request_contract_digest": contract.digest,
        "objective": "already achieved objective",
    }
    model.mode = "auto"
    seen = []
    model._install_turn_authority = lambda authority: seen.append(("authority", authority.is_autonomous_objective))
    model._seed_autonomous_objective = lambda objective: seen.append(("seed", objective))
    model.mythic_client = SimpleNamespace(
        begin_operator_turn=lambda objective, **_kwargs: seen.append(("begin", objective))
    )
    async def kernel(objective):
        seen.append(("controller", objective))
        return "saved terminal"
    model._run_autonomous_controller = kernel
    model._objective_completion_preflight_allowed = lambda _prompt: (_ for _ in ()).throw(
        AssertionError("fresh-turn preflight must not run")
    )
    assert asyncio.run(model.resume_autonomous_controller()) == "saved terminal"
    assert seen[-1] == ("controller", "already achieved objective")


def test_fresh_model_observes_persisted_admitted_effect(monkeypatch, tmp_path):
    """A clean boundary can recover runtime proof from the durable engagement ledger."""
    from datetime import datetime, timezone
    from ai.langgraph import access_reconciler, engagement_ledger, engagement_state, mythic_tools, proof_boundary
    from ai.langgraph.model import Model

    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    engagement_id = "engagement-recovery-fixture"
    proof = proof_boundary.make_runtime_task_envelope(
        engagement_id=engagement_id, callback_id="13", task_id="450",
        terminal_status="completed", command="dcsync",
        verifier_id="test:recovery", transaction_id="transaction-recovery",
        verifier_input={"probe": {"krbtgt_hash_present": True}},
        verifier_result={"verdict": "achieved"},
        captured_at=datetime.now(timezone.utc).isoformat(),
    ).to_dict()
    engagement_ledger.save_runtime({"engagement_id": engagement_id, "hops": [{
        "id": "proof-hop", "technique": "dcsync", "target": "lab.local",
        "effect": "krbtgt-hash:lab.local", "status": "achieved",
        "evidence": {"proof_envelope": proof}, "proof_envelope": proof,
        "satisfied_effects": ["krbtgt-hash:lab.local"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]}, engagement_id)
    fresh_client = SimpleNamespace(
        _engagement_hops=[], _engagement_graph_facts=[],
        _eng_key=lambda: engagement_id,
        _engagement_ledger_path=lambda: engagement_ledger.ledger_path(engagement_id),
        _human_engagement_objective=lambda value: value,
        _engagement_objective=lambda: "test objective",
    )
    mythic_tools.MythicTools._load_engagement_ledger(fresh_client, replace=True)
    async def no_footholds(_client, _now):
        return []
    monkeypatch.setattr(access_reconciler, "reconcile_access", no_footholds)
    fresh_model = object.__new__(Model)
    fresh_model.mythic_client = fresh_client
    observed = asyncio.run(fresh_model._build_current_engagement_state())
    assert observed is not None
    assert observed.achieved_effects() == {"krbtgt-hash:lab.local"}
    wrong_scope = engagement_state.EngagementState(
        objective="test objective", hops=observed.hops,
        engagement_id="different-engagement", runtime_scope=True,
    )
    assert wrong_scope.achieved_effects() == set()
