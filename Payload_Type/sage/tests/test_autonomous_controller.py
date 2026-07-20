"""Tests for the deterministic AutonomousController (control-state P0).

Run: cd Payload_Type/sage && python3 -m pytest tests/test_autonomous_controller.py -q

Covers: the happy-path loop reaches the objective; the `1116` redelegation loop is structurally impossible
(the controller selects the prerequisite from the precondition-checked REAL frontier, never the blocked
capability) AND the loop-breaker backstops a persistently-blocked action; the hard stop-losses (no-progress,
budget, empty-frontier clean halt); bounded-LLM tie ranking; and once-per-need collection.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import autonomous_controller as ac  # noqa: E402
import engagement_state as es  # noqa: E402


# --------------------------------------------------------------------------------------------------------
# Fakes — a mutable effect "world" + scripted frontier, so the loop logic is exercised without the runtime.
# --------------------------------------------------------------------------------------------------------
class FakeAction:
    def __init__(self, name, target="t", effects=(), preconditions=()):
        self.name = name
        self.target = target
        self.effects = list(effects)
        self.preconditions = list(preconditions)


class FakeState:
    def __init__(self, effects):
        self._e = set(effects)

    def achieved_effects(self):
        return set(self._e)

    def satisfies_predicate(self, predicate):
        return predicate in self._e


class World:
    """Holds the achieved-effect set; execute(action) applies a scripted transition to it."""
    def __init__(self, effects=()):
        self.effects = set(effects)

    def observe(self):
        return FakeState(self.effects)


def run(controller):
    return asyncio.run(controller.run())


# --------------------------------------------------------------------------------------------------------
def test_happy_path_reaches_objective():
    """A scripted frontier that advances the chain until da:essos -> STATUS_COMPLETE (range-agnostic)."""
    w = World()
    plan = [
        (None, "collect", "graph-built:host"),
        ("graph-built:host", "gpo-controlled-system-exec", "da:north"),
        ("da:north", "dcsync-krbtgt", "krbtgt-hash:north"),
        ("krbtgt-hash:north", "forge-golden-ticket", "da:sevenkingdoms.local"),
        ("da:sevenkingdoms.local", "read-managed-local-admin-secret", "managed-local-admin-secret:braavos@essos.local"),
        ("managed-local-admin-secret:braavos@essos.local", "use-managed-local-admin-secret", "local-admin:braavos@essos.local"),
        ("local-admin:braavos@essos.local", "execute-as-local-admin", "remote-exec:braavos@essos.local"),
        ("remote-exec:braavos@essos.local", "adcs-ca-private-key-export", "adcs-ca-private-key:braavos@essos.local"),
        ("adcs-ca-private-key:braavos@essos.local", "adcs-certificate-auth", "da:essos.local"),
    ]

    def frontier(state):
        ach = state.achieved_effects()
        for pre, name, eff in plan:
            if (pre is None or pre in ach) and eff not in ach:
                return [FakeAction(name, effects=[eff])]
        return []

    def execute(action):
        for eff in action.effects:
            w.effects.add(eff)
        return {"ok": True}

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier,
                               objective_met=lambda s: "da:essos.local" in s.achieved_effects())
    r = run(c)
    assert r.status == ac.STATUS_COMPLETE, r.to_dict()
    assert "da:essos.local" in r.achieved_effects


def test_1116_prerequisite_selected_from_real_frontier():
    """With remote-exec:braavos@essos achieved but NO ca-private-key, the REAL frontier offers
    adcs-ca-private-key-export and NOT adcs-certificate-auth. The controller selects the prerequisite, so the
    `1116` 'fire adcs-certificate-auth before its prerequisite' loop is structurally impossible."""
    import capabilities as cap
    foothold = es.Foothold(callback_id="3", agent="apollo", host="braavos", forest="essos.local",
                           identity="essos\\administrator", integrity="high", alive=True,
                           source="test", timestamp="")
    hop = es.Hop(id="h", technique="capability:execute-as-local-admin", target="braavos",
                 effect="remote-exec:braavos@essos.local", status="achieved", evidence={},
                 preconditions=[], satisfied_effects=["remote-exec:braavos@essos.local"],
                 source="test", timestamp="")
    state = es.EngagementState(objective="obtain administrative control of essos.local",
                              footholds=[foothold], hops=[hop], graph_facts=[])
    names = {a.name for a in cap.actions_from_state(state)}
    assert "adcs-ca-private-key-export" in names, names
    assert "adcs-certificate-auth" not in names, names  # cannot be selected before its prerequisite exists


def test_account_context_prerequisite_selected_before_protected_secret_read():
    """A downstream secret-read edge plus credential material selects the account-context prerequisite first."""
    import capabilities as cap

    foothold = es.Foothold(
        callback_id="13",
        agent="apollo",
        host="workstation",
        forest="lab.local",
        identity="lab\\operator",
        integrity="medium",
        alive=True,
        source="test",
        timestamp="",
    )
    credential = es.Hop(
        id="creds",
        technique="dcsync-account",
        target="alice",
        effect="creds:alice@lab.local",
        status="achieved",
        evidence={},
        preconditions=[],
        satisfied_effects=["creds:alice@lab.local"],
        source="test",
        timestamp="",
    )
    edge = es.GraphFact(
        predicate=(
            "can-read-managed-local-admin-secret:"
            "account=alice;account_domain=lab.local;"
            "target=server01;target_domain=child.lab.local"
        ),
        source="test",
        timestamp="",
        ttl_seconds=600,
    )
    state = es.EngagementState(
        objective="obtain administrative control of child.lab.local",
        footholds=[foothold],
        hops=[credential],
        graph_facts=[edge],
    )

    names = {action.name for action in cap.actions_from_state(state)}

    assert "ensure-account-kerberos-context" in names, names
    assert "read-managed-local-admin-secret" not in names, names


def test_loop_breaker_stops_persistently_blocked_action():
    """Frontier keeps offering one capability; execute always returns the SAME blocker. The loop-breaker
    promotes the repeated blocker to a terminal STOP within a couple cycles — never a 48x redelegation."""
    w = World({"remote-exec:braavos@essos.local"})

    def frontier(state):
        return [FakeAction("adcs-certificate-auth", "braavos",
                           effects=["da:essos.local"])]  # offered but always blocked below

    def execute(action):
        return {"ok": False, "reason": "missing CA private key", "run_first": "adcs-ca-private-key-export"}

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier,
                               config=ac.ControllerConfig(max_cycles=60))
    r = run(c)
    assert r.status == ac.STATUS_BLOCKED, r.to_dict()
    assert r.cycle_count <= 3, f"loop-breaker must stop fast, got {r.cycle_count} cycles"
    assert r.blocker and "adcs-certificate-auth" in (r.blocker.get("capability") or "")


def test_no_progress_halt_on_changing_useless_actions():
    """Distinct 'successful' actions that each verify NO new effect -> halt at max_no_effect_cycles. (Distinct
    names dodge the loop-breaker's same-blocker STOP, so this exercises the separate no-progress stop-loss; a
    same-name no-op would correctly STOP even faster via the loop-breaker.)"""
    w = World({"da:north"})
    n = {"i": 0}

    def frontier(state):
        n["i"] += 1
        return [FakeAction(f"noop-{n['i']}", effects=["never-lands"])]  # claims success, effect never appears

    def execute(action):
        return {"ok": True}  # self-reported success, world unchanged -> no verified effect

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier,
                               config=ac.ControllerConfig(max_no_effect_cycles=4))
    r = run(c)
    assert r.status == ac.STATUS_NO_PROGRESS, r.to_dict()
    assert r.cycle_count == 4


def test_string_failure_result_is_blocker_not_silent_success():
    """THE critical wiring landmine (Forge #1): the real execute_capability returns a JSON STRING. A blocked
    capability returned as a string must be parsed as a FAILURE and reach STATUS_BLOCKED — never coerced to a
    silent success. (Unit fakes returning dicts hid this; this fake returns the real string shape.)"""
    import json as _json
    w = World({"remote-exec:braavos@essos.local"})

    def frontier(state):
        return [FakeAction("adcs-certificate-auth", "braavos", effects=["da:essos.local"])]

    def execute(action):
        return _json.dumps({"ok": False, "verdict": "blocked", "capability": "adcs-certificate-auth",
                            "reason": "missing CA private key",
                            "suggested_capability": "adcs-ca-private-key-export"})

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier)
    r = run(c)
    assert r.status == ac.STATUS_BLOCKED, r.to_dict()
    assert r.cycle_count <= 3
    assert r.blocker.get("suggested_capability") == "adcs-ca-private-key-export"


def test_ok_false_but_verified_effect_counts_as_progress():
    """Over-suppression guard (Forge #3): verify-by-probe overrides self-report. An action reporting ok=False
    whose expected effect DOES land is PROGRESS — the loop-breaker must not STOP it as a repeated blocker."""
    w = World({"da:north"})
    plan = [("da:north", "dcsync-krbtgt", "krbtgt-hash:north"),
            ("krbtgt-hash:north", "forge-golden-ticket", "da:sevenkingdoms.local")]

    def frontier(state):
        ach = state.achieved_effects()
        for pre, name, eff in plan:
            if pre in ach and eff not in ach:
                return [FakeAction(name, effects=[eff])]
        return []

    def execute(action):
        for eff in action.effects:
            w.effects.add(eff)            # effect lands...
        return {"ok": False, "reason": "self-report says failed"}  # ...despite a False self-report

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier,
                               objective_met=lambda s: "da:sevenkingdoms.local" in s.achieved_effects())
    r = run(c)
    assert r.status == ac.STATUS_COMPLETE, r.to_dict()


def test_zero_retry_trajectory_repair_is_immediate_terminal_blocker():
    w = World({"da:north"})

    def frontier(_state):
        return [FakeAction("dcsync-krbtgt", "north", effects=["krbtgt-hash:north"])]

    def execute(_action):
        return {
            "ok": False,
            "reason": "repair exhausted",
            "trajectory_repair": {
                "failure_label": "unsupported_mechanism",
                "repair": {
                    "kind": "replan_without_retry",
                    "retry_budget": 0,
                },
            },
        }

    controller = ac.AutonomousController(
        observe=w.observe,
        execute=execute,
        frontier_fn=frontier,
        config=ac.ControllerConfig(max_no_effect_cycles=4),
    )

    result = run(controller)

    assert result.status == ac.STATUS_BLOCKED, result.to_dict()
    assert result.cycle_count == 1
    assert result.blocker["trajectory_repair"]["retry_budget"] == 0
    assert result.blocker["trajectory_repair"]["repair_kind"] == "replan_without_retry"


def test_zero_retry_trajectory_repair_does_not_override_verified_progress():
    w = World({"da:north"})

    def frontier(state):
        if "krbtgt-hash:north" in state.achieved_effects():
            return []
        return [FakeAction("dcsync-krbtgt", "north", effects=["krbtgt-hash:north"])]

    def execute(action):
        w.effects.update(action.effects)
        return {
            "ok": False,
            "reason": "task output looked failed before verifier re-observed",
            "trajectory_repair": {
                "failure_label": "transient_output_mismatch",
                "repair": {
                    "kind": "replan_without_retry",
                    "retry_budget": 0,
                },
            },
        }

    controller = ac.AutonomousController(
        observe=w.observe,
        execute=execute,
        frontier_fn=frontier,
        objective_met=lambda state: "krbtgt-hash:north" in state.achieved_effects(),
    )

    result = run(controller)

    assert result.status == ac.STATUS_COMPLETE, result.to_dict()


def test_malformed_zero_retry_trajectory_payload_is_advisory_only():
    w = World({"da:north"})

    def frontier(_state):
        return [FakeAction("dcsync-krbtgt", "north", effects=["krbtgt-hash:north"])]

    def execute(_action):
        return {
            "ok": False,
            "reason": "still retryable",
            "trajectory_repair": {
                "failure_label": "",
                "repair": {
                    "kind": "replan_without_retry",
                    "retry_budget": "0",
                },
            },
        }

    controller = ac.AutonomousController(
        observe=w.observe,
        execute=execute,
        frontier_fn=frontier,
        config=ac.ControllerConfig(max_no_effect_cycles=1),
    )

    result = run(controller)

    assert result.status == ac.STATUS_NO_PROGRESS, result.to_dict()


def test_route_discovery_candidate_must_pass_preconditions():
    """Forge #2: an LLM route-discovery candidate is precondition-checked before execute, so a bad suggestion
    cannot start a loop. Unmet precondition -> rejected -> clean halt, execute never called."""
    w = World({"da:north"})  # krbtgt-hash:north is ABSENT
    executed = []

    def route_unmet(state):
        # candidate requires krbtgt-hash:north, which is not satisfied -> must be rejected
        return FakeAction("forge-golden-ticket", "x", effects=["da:sevenkingdoms.local"],
                          preconditions=["krbtgt-hash:north"])

    bad = ac.AutonomousController(observe=w.observe,
                                  execute=lambda a: executed.append(a) or {"ok": True},
                                  frontier_fn=lambda s: [], route_discovery=route_unmet)
    r = run(bad)
    assert r.status == ac.STATUS_NO_ACTION, r.to_dict()
    assert executed == []


def test_explicit_policy_cannot_fall_back_to_route_discovery():
    w = World({"da:north", "krbtgt-hash:north"})
    executed = []
    discovered = []

    class ExplicitPolicy:
        mode = "llm"

        async def select(self, **_kwargs):
            raise AssertionError("empty frontier should halt before policy selection")

    def route_admissible(_state):
        discovered.append(True)
        return FakeAction(
            "forge-golden-ticket",
            "x",
            effects=["da:sevenkingdoms.local"],
            preconditions=["krbtgt-hash:north"],
        )

    controller = ac.AutonomousController(
        observe=w.observe,
        execute=lambda action: executed.append(action) or {"ok": True},
        frontier_fn=lambda _state: [],
        route_discovery=route_admissible,
        policy_backend=ExplicitPolicy(),
    )

    result = run(controller)

    assert result.status == ac.STATUS_NO_ACTION, result.to_dict()
    assert discovered == []
    assert executed == []
    assert result.reason == "empty frontier under explicit policy"


def test_execute_exception_is_clean_blocker_not_crash():
    """Forge #5: a throwing live seam becomes a diagnostic halt, never a traceback escaping run()."""
    w = World({"remote-exec:braavos@essos.local"})

    def boom(action):
        raise RuntimeError("mythic API exploded")

    c = ac.AutonomousController(observe=w.observe, execute=boom,
                               frontier_fn=lambda s: [FakeAction("adcs-certificate-auth", "b", effects=["da:essos.local"])])
    r = run(c)  # must not raise
    assert r.status == ac.STATUS_BLOCKED, r.to_dict()


def test_budget_halt_before_any_execute():
    """Token budget already exhausted -> clean budget halt on cycle 1, no execute, no 2-hour timeout."""
    w = World({"da:north"})
    executed = []

    def frontier(state):
        return [FakeAction("dcsync-krbtgt", effects=["krbtgt-hash:north"])]

    def execute(action):
        executed.append(action)
        return {"ok": True}

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier,
                               tokens_spent=lambda: 10_000_000,
                               config=ac.ControllerConfig(token_budget=3_000_000))
    r = run(c)
    assert r.status == ac.STATUS_BUDGET, r.to_dict()
    assert executed == []


def test_bounded_llm_tie_ranking():
    """Two top-priority (tie) actions -> rank_ties is consulted and may only REORDER admissible actions."""
    w = World({"da:north"})
    a1 = FakeAction("dcsync-account", "userA", effects=["creds:a@north"])
    a2 = FakeAction("dcsync-account", "userB", effects=["creds:b@north"])
    calls = []

    def frontier(state):
        ach = state.achieved_effects()
        out = [a for a in (a1, a2) if a.effects[0] not in ach]
        return out

    def rank_ties(actions):
        calls.append([a.target for a in actions])
        return actions[-1]  # prefer userB first

    def execute(action):
        w.effects.add(action.effects[0])
        return {"ok": True}

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier,
                               rank_ties=rank_ties, objective_met=lambda s: "creds:a@north" in s.achieved_effects()
                               and "creds:b@north" in s.achieved_effects())
    r = run(c)
    assert calls, "rank_ties must be consulted on a priority tie"
    assert r.status == ac.STATUS_COMPLETE
    assert r.cycles[0].target == "userB"  # the LLM's reordering was honored


def test_empty_frontier_clean_halt_with_blocker():
    """No admissible action and no route-discovery -> clean halt with a precise blocker (not a wander)."""
    w = World(set())

    c = ac.AutonomousController(observe=w.observe, execute=lambda a: {"ok": True},
                               frontier_fn=lambda s: [])
    r = run(c)
    assert r.status == ac.STATUS_NO_ACTION, r.to_dict()
    assert r.blocker and r.blocker.get("reason")


def test_collection_runs_once_when_needed():
    """needs_collection -> collect fires once, then the frontier proceeds (covers the empty-frontier early
    phase = the latest canary's poll/ingest/recon wander)."""
    w = World(set())
    collected = []

    def needs_collection(state):
        return "graph-built:host" not in state.achieved_effects()

    def collect(state):
        collected.append(1)
        w.effects.add("graph-built:host")
        return {"ok": True, "status": "collected"}

    def frontier(state):
        ach = state.achieved_effects()
        if "graph-built:host" in ach and "da:north" not in ach:
            return [FakeAction("gpo-controlled-system-exec", "gpo", effects=["da:north"])]
        return []

    def execute(action):
        w.effects.add(action.effects[0])
        return {"ok": True}

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier,
                               needs_collection=needs_collection, collect=collect,
                               objective_met=lambda s: "da:north" in s.achieved_effects())
    r = run(c)
    assert collected == [1], "collection must run exactly once"
    assert r.status == ac.STATUS_COMPLETE


def test_distinct_collection_requests_are_not_blocked_by_a_low_global_cap():
    """Four justified scopes/epochs can collect in one solve; the old hardcoded max_collections=3 would stop
    before the fourth even though each request was distinct."""
    w = World(set())
    collected = []
    request_order = ["baseline", "authority", "objective-scope", "next-domain"]

    def needs_collection(state):
        for key in request_order:
            if f"graph-built:{key}" not in state.achieved_effects():
                return {"collection_key": key}
        return False

    def collect(state):
        for key in request_order:
            if f"graph-built:{key}" not in state.achieved_effects():
                collected.append(key)
                w.effects.add(f"graph-built:{key}")
                return {"ok": True, "status": "ingested", "collection_key": key}
        return {"ok": False, "status": "unexpected"}

    def frontier(state):
        if all(f"graph-built:{key}" in state.achieved_effects() for key in request_order):
            if "da:target" not in state.achieved_effects():
                return [FakeAction("finish", effects=["da:target"])]
        return []

    def execute(action):
        w.effects.update(action.effects)
        return {"ok": True}

    c = ac.AutonomousController(
        observe=w.observe,
        execute=execute,
        frontier_fn=frontier,
        needs_collection=needs_collection,
        collect=collect,
        objective_met=lambda s: "da:target" in s.achieved_effects(),
    )
    r = run(c)

    assert collected == request_order
    assert r.status == ac.STATUS_COMPLETE, r.to_dict()


def test_collection_retry_budget_is_per_request():
    """A broken request retries once and then halts; it no longer consumes a solve-wide three-collection budget."""
    w = World(set())
    attempts = []

    def needs_collection(_state):
        return {"collection_key": "scope:broken"}

    def collect(_state):
        attempts.append(1)
        return {"ok": False, "status": "ingest_failed", "collection_key": "scope:broken"}

    c = ac.AutonomousController(
        observe=w.observe,
        execute=lambda _action: {"ok": True},
        frontier_fn=lambda _state: [],
        needs_collection=needs_collection,
        collect=collect,
    )
    r = run(c)

    assert len(attempts) == 2
    assert r.status == ac.STATUS_BLOCKED, r.to_dict()
    assert r.blocker and r.blocker.get("collection_key") == "scope:broken"


def test_no_op_success_is_no_progress_not_blocked():
    """Forge NEW-4 regression: a self-reported SUCCESS that verifies no effect must halt as NO_PROGRESS (at the
    no-effect ceiling), NOT be mislabeled a terminal blocker. (Decoupling epoch from outcome classification.)"""
    w = World({"da:north"})

    def frontier(state):
        return [FakeAction("dcsync-krbtgt", "north", effects=["krbtgt-hash:north"])]

    def execute(action):
        return {"ok": True}  # claims success; world never changes -> expected effect never lands

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier,
                               config=ac.ControllerConfig(max_no_effect_cycles=4))
    r = run(c)
    assert r.status == ac.STATUS_NO_PROGRESS, r.to_dict()
    assert r.cycle_count == 4


def test_async_seam_timeout_halts_cleanly():
    """Forge NEW-2: a hung ASYNC execute is bounded by seam_timeout_s and becomes a clean blocker, never an
    unbounded hang. Exercises the timeout path that the default (None) config leaves untested."""
    import asyncio as _asyncio
    w = World({"remote-exec:braavos@essos.local"})

    async def slow_execute(action):
        await _asyncio.sleep(5.0)
        return {"ok": True}

    c = ac.AutonomousController(observe=w.observe, execute=slow_execute,
                               frontier_fn=lambda s: [FakeAction("adcs-certificate-auth", "b", effects=["da:essos.local"])],
                               config=ac.ControllerConfig(seam_timeout_s=0.01))
    r = run(c)  # must return promptly, not hang 5s/cycle
    assert r.status == ac.STATUS_BLOCKED, r.to_dict()


def test_budget_meter_failure_fails_closed():
    """Forge NEW-5: if the token/clock meter raises, the ceiling must fail CLOSED (halt), never silently
    disable the last-resort runaway guard."""
    w = World({"da:north"})

    def boom_meter():
        raise RuntimeError("phoenix unavailable")

    c = ac.AutonomousController(observe=w.observe, execute=lambda a: {"ok": True},
                               frontier_fn=lambda s: [FakeAction("dcsync-krbtgt", effects=["krbtgt-hash:north"])],
                               tokens_spent=boom_meter)
    r = run(c)
    assert r.status == ac.STATUS_BUDGET, r.to_dict()


def test_route_candidate_with_no_preconditions_rejected():
    """Forge NEW-1: an LLM route candidate with empty/missing preconditions is rejected (all([]) is vacuously
    true), so an unconditional ad-hoc action cannot be admitted and start a loop."""
    w = World({"da:north"})
    executed = []

    def route_unconditional(state):
        return FakeAction("adcs-certificate-auth", "x", effects=["da:essos.local"])  # NO preconditions declared

    c = ac.AutonomousController(observe=w.observe,
                               execute=lambda a: executed.append(a) or {"ok": True},
                               frontier_fn=lambda s: [], route_discovery=route_unconditional)
    r = run(c)
    assert r.status == ac.STATUS_NO_ACTION, r.to_dict()
    assert executed == []


def test_should_abort_honors_operator_stop_between_cycles():
    """Cooperative kill switch: when should_abort() flips True, the controller halts STATUS_ABORTED at the next
    cycle boundary rather than running on."""
    w = World({"da:north"})
    flag = {"stop": False}

    def frontier(state):
        return [FakeAction("dcsync-krbtgt", "north", effects=["krbtgt-hash:north"])]

    def execute(action):
        flag["stop"] = True  # operator stops mid-run
        return {"ok": True}  # would otherwise keep going (no objective_met)

    c = ac.AutonomousController(observe=w.observe, execute=execute, frontier_fn=frontier,
                               should_abort=lambda: flag["stop"])
    r = run(c)
    assert r.status == ac.STATUS_ABORTED, r.to_dict()
    assert r.cycle_count <= 2


def test_controller_is_range_agnostic_no_goad_literals():
    """Guardrail: the runtime controller module must not bake in GOAD range literals (walls live in the eval
    layer). Catches a regression that re-introduces range-specific strings into ai/langgraph/."""
    import inspect
    src = inspect.getsource(ac).lower()
    # Range TOPOLOGY names (the GOAD lab's domains/hosts) must never appear in the runtime. Generic AD attack
    # primitives (dcsync, krbtgt, adcs-*, graph-built) are legitimate Sage vocabulary and are NOT range-specific.
    for literal in ("north", "sevenkingdoms", "essos", "braavos", "winterfell", "castelblack"):
        assert literal not in src, f"GOAD range-topology literal '{literal}' leaked into the runtime controller"
