"""Deterministic execution kernel for policy-selected capability transactions.

THE PROBLEM: autonomous control is diffuse —
selection / sequencing / polling / termination are owned by the Supervisor+worker LLMs negotiating in text.
Locally-correct components form globally-incorrect loops (the `1116` 461K-token redelegation tail; the latest
canary's poll/ingest/recon wander). The execution substrate is deterministic and guided-proven, and the
precondition/effect model is already declared (`capabilities.actions_from_state` returns a priority-ordered
admissible frontier — Spike 0 confirmed it reproduces a full proven attack chain's critical path at rank 0).

The kernel owns the bounded transaction cycle

    observe -> collect-once-if-needed -> compute frontier -> select one admissible action
            -> execute -> verify -> update state -> decide(continue|route|stop)

while an explicit policy backend owns semantic capability selection. Deterministic code retains observation,
admissibility, polling, verification, repeat-policy, and termination.

WHY THE `1116` LOOP BECOMES STRUCTURALLY IMPOSSIBLE: the controller selects ONLY from the precondition-checked
frontier, so it cannot select `adcs-certificate-auth` before `adcs-ca-private-key` exists — it picks the
prerequisite export first. The `worker_outcome` loop-breaker is the backstop: the same blocker at the same
progress epoch -> STOP, never a 48x redelegation.

This module is dependency-light and OFFLINE-TESTABLE: every side-effecting boundary (observe / execute /
collect / objective-check / token+clock budget / LLM tie-rank / LLM route-discovery) is an INJECTED callable
(sync or async). The runtime wiring (a later step) supplies the real seams; the core loop logic is unit-tested
with fakes here. Per the C2 retro lesson the wiring step must still PROVE the real seam fires at runtime — unit
tests on fakes are necessary, not sufficient.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

try:
    from . import capabilities as cap
    from . import worker_outcome as wo
    from . import kernel_tracing as _tracing
except ImportError:  # flat-import runtimes (mirrors the codebase's relative-then-flat pattern)
    import capabilities as cap
    import worker_outcome as wo
    import kernel_tracing as _tracing


# NOTE: progress/milestone "wall checkpoints" are deliberately NOT defined here. They are scenario-specific
# (GOAD route literals etc.) and belong in the eval layer, never in the generic Sage runtime. The controller
# emits RAW achieved effects (ControllerResult.achieved_effects + per-cycle new_effects); the eval/hillclimb
# layer maps those to scenario walls (see ai/hillclimb/wall_checkpoints.py). Keep ai/langgraph/ range-agnostic.


# ---------------------------------------------------------------------------------------------------------
# Config / result types
# ---------------------------------------------------------------------------------------------------------
@dataclass
class ControllerConfig:
    """Hard runtime stop-losses. The point is that a run NEVER 2-hour-timeouts: it halts cleanly with a
    precise blocker. Every ceiling is enforced in the loop, not aspirational."""
    max_cycles: int = 60                 # absolute backstop on loop iterations
    max_no_effect_cycles: int = 4        # consecutive verified-no-new-effect cycles -> halt
    token_budget: int = 3_000_000        # cumulative output/observed tokens -> halt
    wall_clock_budget_s: float = 2700.0  # 45 min -> halt
    max_collections: int | None = None   # optional emergency global cap; policy is per-request, not global
    max_collection_attempts_per_request: int = 2  # retry one failed request once, then halt diagnostically
    seam_timeout_s: float | None = None  # per-seam await deadline (None in tests; runtime sets it so a hung
                                         # observe/execute/collect halts cleanly instead of a 2-hour timeout)


# Halt status taxonomy — every terminal state is diagnostic.
STATUS_COMPLETE = "complete"                  # objective achieved
STATUS_BLOCKED = "halted_blocked"             # same blocker recurred at same state (loop-breaker STOP)
STATUS_NO_ACTION = "halted_no_action"         # empty frontier, no collection/route-discovery move
STATUS_NO_PROGRESS = "halted_no_progress"     # max_no_effect_cycles with no new effect
STATUS_BUDGET = "halted_budget"               # token or wall-clock budget exhausted
STATUS_MAX_CYCLES = "halted_max_cycles"       # absolute cycle backstop
STATUS_ABORTED = "halted_aborted"             # operator stop requested between cycles (cooperative kill switch)


@dataclass
class CycleRecord:
    cycle: int
    phase: str            # 'collect' | 'execute' | 'route_discovery'
    action: str = ""
    target: str = ""
    ok: bool = False
    new_effects: list[str] = field(default_factory=list)
    note: str = ""
    decision_id: str = ""
    policy_mode: str = ""


@dataclass
class ControllerResult:
    status: str
    reason: str
    blocker: dict | None
    cycle_count: int
    cycles: list[CycleRecord]
    achieved_effects: list[str]
    episode_id: str = ""
    policy_mode: str = ""
    decisions: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        policy_switches = []
        for source, items in (("decision", self.decisions), ("transaction", self.transactions)):
            for index, item in enumerate(items):
                observed = str(item.get("policy_mode") or "")
                if observed != self.policy_mode:
                    policy_switches.append({
                        "source": source,
                        "index": index,
                        "configured_policy_mode": self.policy_mode,
                        "observed_policy_mode": observed,
                    })
        authorized = sum(
            1
            for item in self.transactions
            if item.get("decision_id") and str(item.get("policy_mode") or "") == self.policy_mode
        )
        transaction_count = len(self.transactions)
        return {
            "status": self.status,
            "reason": self.reason,
            "blocker": self.blocker,
            "cycle_count": self.cycle_count,
            "achieved_effects": self.achieved_effects,
            "episode_id": self.episode_id,
            "policy_mode": self.policy_mode,
            "policy_identity_valid": not policy_switches,
            "policy_switches": policy_switches,
            "decisions": self.decisions,
            "transactions": self.transactions,
            "semantic_transaction_count": transaction_count,
            "authorized_transaction_count": authorized,
            "semantic_policy_coverage": (
                authorized / transaction_count if transaction_count else 1.0
            ),
            "cycles": [vars(c) for c in self.cycles],
        }


@dataclass(frozen=True)
class _CollectionCandidate:
    name: str = "collect-graph"
    target: str = ""
    preconditions: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=lambda: ["graph-collected"])
    reason: str = "refresh graph observations required by the current state"


def _parse_result(value: Any) -> dict:
    """Coerce a capability/collection result into a dict, FAIL-CLOSED. The real `execute_capability` returns a
    JSON *string* (`-> str`), not a dict — so a naive `isinstance(dict)` check would treat every live result
    (success AND failure) as non-dict. A malformed / None / unparseable result must become a BLOCKER, never a
    silent success (the inverse would convert live failures into successes — the `1116`-class landmine)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {"ok": False, "reason": f"unparseable capability result: {value[:160]}"}
    return {"ok": False, "reason": f"non-dict capability result of type {type(value).__name__}"}


def _action_effects(action: Any) -> set[str]:
    return {cap._canonical_effect(e) for e in (getattr(action, "effects", None) or []) if e}


def _tie_cluster(frontier: list[Any]) -> list[Any]:
    """The set of top-priority actions (a genuine selection tie the LLM may rank). Same priority value as the
    rank-0 action; the deterministic order already put them first."""
    if not frontier:
        return []
    top = cap._capability_action_priority(frontier[0])
    return [a for a in frontier if cap._capability_action_priority(a) == top]


def _collection_request_key(value: Any) -> str:
    """Best-effort stable key for one requested collection reason/scope.

    Runtime wiring returns a `_ControllerCollectionRequest`; unit fakes often return `True`. Anonymous requests
    still get a retry budget, but real requests are isolated so one failed scope cannot consume another scope's
    allowance."""
    if isinstance(value, dict):
        key = value.get("collection_key") or value.get("request_key")
        return str(key or "").strip()
    key = getattr(value, "collection_key", "")
    return str(key or "").strip()


def _decision_transaction_fields(decision: Any | None) -> dict[str, Any]:
    """Return the decision provenance worth carrying on each semantic transaction."""
    if decision is None:
        return {"decision_id": "", "policy_mode": ""}
    data = decision.to_dict() if hasattr(decision, "to_dict") else (
        dict(decision) if isinstance(decision, dict) else {}
    )
    keys = (
        "decision_id",
        "policy_mode",
        "candidate_hash",
        "candidate_count",
        "selected_index",
        "selected_family",
        "selected_is_first_admissible",
        "disposition",
        "rationale",
        "raw_response",
        "raw_disposition",
        "raw_rationale",
        "model_response_observed",
        "effective_backend",
        "effective_model_provider",
        "effective_model_id",
        "backend_provenance_source",
        "policy_version",
        "selection_contract",
        "selection_contract_hash",
        "decision_owner",
        "semantic_candidate_ids",
        "candidate_set_hash",
        "ordered_frontier_hash",
        "selected_candidate_id",
        "symbolic_counterfactual_candidate_id",
        "forced_intervention",
        "intervention_id",
        "forced_policy_win_credit",
    )
    return {key: data.get(key) for key in keys if key in data}


def _transaction_record(kind: str, capability: str, target: str, decision: Any | None) -> dict[str, Any]:
    return {
        "transaction_id": f"transaction-{uuid4().hex}",
        "parent_transaction_id": "",
        "kind": kind,
        "capability": capability,
        "target": target,
        "callback_id": "",
        "child_tasks": [],
        "verifier_ids": [],
        "proof_envelope_ids": [],
        "proof_lineage": [],
        "wait_count": 0,
        "retry_count": 0,
        **_decision_transaction_fields(decision),
    }


def _trajectory_zero_retry_blocker(result: dict[str, Any], *, progressed: bool) -> dict[str, Any] | None:
    """Return a typed terminal blocker only for an explicit zero-retry trajectory repair.

    The trajectory bridge is advisory by default. We only turn one repair into an immediate
    terminal disposition when the result is already a failed capability, the nested repair
    payload is well formed, `retry_budget` is the exact integer zero, and re-observation did
    not verify the action's expected effect. Malformed or near-match advisory payloads remain
    non-terminal.
    """
    if progressed or result.get("ok") is not False:
        return None
    trajectory = result.get("trajectory_repair")
    if not isinstance(trajectory, dict):
        return None
    repair = trajectory.get("repair")
    if not isinstance(repair, dict):
        return None
    retry_budget = repair.get("retry_budget")
    if not isinstance(retry_budget, int) or isinstance(retry_budget, bool) or retry_budget != 0:
        return None
    repair_kind = str(repair.get("kind") or "").strip()
    failure_label = str(trajectory.get("failure_label") or "").strip()
    if not repair_kind or not failure_label:
        return None
    return {
        "reason": str(result.get("reason") or result.get("error") or ""),
        "failure_label": failure_label,
        "repair_kind": repair_kind,
        "retry_budget": 0,
    }


class AutonomousController:
    """Owns the deterministic autonomous control loop. All boundaries are injected callables.

    Required seams:
      observe()            -> EngagementState     (rebuild current state; runtime: Model._build_current_engagement_state)
      execute(action)      -> dict result         (runtime: mythic_client.execute_capability; result has 'ok', maybe 'reason'/'run_first')

    Optional seams (default to safe no-ops):
      objective_met(state) -> bool                (completion recognition; default: never)
      needs_collection(state) -> request|bool    (truthy request when a fresh collection would help; default: never)
      collect(state)       -> dict               (run collection once; idempotent)
      rank_ties(actions)   -> action             (bounded LLM rank among admissible ties; default: first)
      route_discovery(state) -> action|None      (bounded LLM candidate when frontier EMPTY; default: None)
      tokens_spent()       -> int                (cumulative tokens; default: 0)
      clock()              -> float               (monotonic seconds; default: 0.0)
    """

    def __init__(self, *, observe: Callable[[], Any], execute: Callable[[Any], Any],
                 objective_met: Callable[[Any], Any] | None = None,
                 needs_collection: Callable[[Any], Any] | None = None,
                 collect: Callable[[Any], Any] | None = None,
                 rank_ties: Callable[[list[Any]], Any] | None = None,
                 route_discovery: Callable[[Any], Any] | None = None,
                 tokens_spent: Callable[[], int] | None = None,
                 clock: Callable[[], float] | None = None,
                 should_abort: Callable[[], bool] | None = None,
                 frontier_fn: Callable[[Any], list[Any]] | None = None,
                 policy_backend: Any | None = None,
                 objective: str = "",
                 episode_id: str = "",
                 config: ControllerConfig | None = None,
                 logger: Callable[[str], None] | None = None):
        self._observe = observe
        self._execute = execute
        self._objective_met = objective_met
        self._needs_collection = needs_collection
        self._collect = collect
        self._rank_ties = rank_ties
        self._route_discovery = route_discovery
        self._tokens_spent = tokens_spent
        self._clock = clock
        self._should_abort = should_abort
        self._frontier_fn = frontier_fn or cap.actions_from_state
        self._policy = policy_backend
        self._objective = str(objective or "")
        self._episode_id = str(episode_id or "")
        self.config = config or ControllerConfig()
        self._log = logger or (lambda _m: None)
        self._loop = wo.LoopBreakerState()
        self._collections = 0
        self._collection_attempts: dict[str, int] = {}
        self._no_effect = 0

    @staticmethod
    def _annotate_policy_span(span: Any, status: str, decision: Any) -> None:
        """Put the policy decision itself on the `policy_select` span.

        A failed model call is not an exception here: `policy.py:716-731` converts it into a structured
        stop decision carrying the reason, and the seam correctly reports `ok` because the backend did
        return a decision. That is the right behaviour, but it left the trace understating severity —
        a green seam over an ERROR `ChatOpenAI` child, with the reason visible nowhere in the tree.

        These attributes close that gap without touching the seam's `ok|timeout|error` vocabulary, which
        means "did the callable complete", not "was the outcome good". `model_response_observed` is the
        unambiguous one: false with `disposition=stop` is exactly the model-never-answered case.
        """
        if decision is None or status != "ok":
            return
        fields = {
            "sage.policy.disposition": getattr(decision, "disposition", None),
            "sage.policy.rationale": getattr(decision, "rationale", None),
            "sage.policy.mode": getattr(decision, "policy_mode", None),
            "sage.policy.model_response_observed": getattr(decision, "model_response_observed", None),
            "sage.policy.selected_capability": getattr(decision, "selected_capability", None) or None,
            "sage.policy.selected_target": getattr(decision, "selected_target", None) or None,
        }
        for key, value in fields.items():
            if value is None or value == "":
                continue
            span.set_attribute(key, value if isinstance(value, bool) else str(value)[:1024])

    async def _policy_select(
        self,
        state: Any,
        candidates: list[Any],
        history: list[Any],
    ) -> tuple[Any | None, Any | None]:
        if self._policy is None:
            return await self._select(candidates), None
        status, decision = await self._seam(
            lambda: self._policy.select(
                episode_id=self._episode_id,
                objective=self._objective,
                state=state,
                candidates=list(candidates),
                history=list(history),
                budgets={
                    "max_cycles": self.config.max_cycles,
                    "wall_clock_budget_s": self.config.wall_clock_budget_s,
                    "token_budget": self.config.token_budget,
                },
            ),
            "policy_select",
            annotate=self._annotate_policy_span,
        )
        if status != "ok" or decision is None:
            return None, None
        if not str(getattr(decision, "decision_id", "") or ""):
            return None, decision
        if (
            self._episode_id
            and str(getattr(decision, "episode_id", "") or "") != self._episode_id
        ):
            return None, decision
        if str(getattr(decision, "policy_mode", "") or "") != str(getattr(self._policy, "mode", "") or ""):
            return None, decision
        if str(getattr(decision, "disposition", "")) != "select":
            return None, decision
        index = getattr(decision, "selected_index", None)
        if not isinstance(index, int) or index < 0 or index >= len(candidates):
            return None, decision
        selected = candidates[index]
        if str(getattr(decision, "selected_capability", "") or "") != str(getattr(selected, "name", "")):
            return None, decision
        if str(getattr(decision, "selected_target", "") or "") != str(getattr(selected, "target", "")):
            return None, decision
        return selected, decision

    def _achieved(self, state: Any) -> set[str]:
        """Canonical achieved-effect set, normalized with the SAME fn the frontier uses (cap._canonical_effect)
        so the controller's progress diff agrees with the frontier's notion of 'already achieved'."""
        return {cap._canonical_effect(e) for e in (state.achieved_effects() or [])}

    async def _seam(
        self,
        thunk: Callable[[], Any],
        name: str,
        annotate: Callable[[Any, str, Any], None] | None = None,
    ) -> tuple[str, Any]:
        """Call an injected seam (sync or async) with exception->diagnostic and an optional per-seam deadline.
        Returns (status, value): status is 'ok' | 'timeout' | 'error'. This is what makes the 'NEVER a 2-hour
        timeout, always a clean halt' promise true even when a live seam hangs or raises.

        Every controller operation funnels through here, so this is also the one place kernel spans need to be
        emitted from to cover observe/execute/collect/verify/policy_select. The span is emission-only: the
        (status, value) contract below is untouched, and a seam that fails still returns its diagnostic rather
        than raising. `policy_select` in particular runs the model call inside this span, which is what gives
        the otherwise-parentless LLM span a parent."""
        status, value = "ok", None
        with _tracing.kernel_span(
            f"sage.kernel.seam.{name}",
            **{"sage.seam.name": name, "sage.cycle.index": getattr(self, "_current_cycle", 0)},
        ) as span:
            try:
                value = thunk()
                if inspect.isawaitable(value):
                    if self.config.seam_timeout_s:
                        value = await asyncio.wait_for(value, self.config.seam_timeout_s)
                    else:
                        value = await value
            except asyncio.TimeoutError:
                status = "timeout"
                value = f"{name} exceeded seam_timeout_s={self.config.seam_timeout_s}"
            except Exception as e:  # a live observe/execute WILL occasionally throw; convert, don't crash the loop
                status = "error"
                value = f"{name} raised {type(e).__name__}: {e}"
            _tracing.record_seam_outcome(span, status, None if status == "ok" else value)
            if annotate is not None:
                try:
                    annotate(span, status, value)
                except Exception:  # an annotator is telemetry; it must never affect the seam
                    pass
        return (status, value)

    async def _budget_exceeded(self) -> str:
        # FAIL CLOSED: budgets are the last-resort guard against a runaway run, so a meter that errors/times out
        # halts the run rather than silently disabling the ceiling (Forge NEW-5).
        if self._tokens_spent is not None:
            st, v = await self._seam(lambda: self._tokens_spent(), "tokens_spent")
            if st != "ok":
                return f"token meter unavailable ({st}); failing closed"
            if int(v) >= self.config.token_budget:
                return "token budget exhausted"
        if self._clock is not None:
            st, v = await self._seam(lambda: self._clock(), "clock")
            if st != "ok":
                return f"clock unavailable ({st}); failing closed"
            if float(v) >= self.config.wall_clock_budget_s:
                return "wall-clock budget exhausted"
        return ""

    async def _select(self, frontier: list[Any]) -> Any:
        """Top-priority admissible action; bounded-LLM rank only among a genuine priority tie."""
        cluster = _tie_cluster(frontier)
        if len(cluster) <= 1:
            return frontier[0]
        if self._rank_ties is not None:
            st, chosen = await self._seam(lambda: self._rank_ties(list(cluster)), "rank_ties")
            if st == "ok" and chosen in cluster:  # the LLM may only REORDER admissible actions, never inject one
                return chosen
            self._log(f"rank_ties unusable ({st}); deterministic top")
        return cluster[0]

    def _candidate_admissible(self, state: Any, action: Any) -> bool:
        """A route-discovery (LLM-proposed) candidate must still pass precondition checking — design §3's
        guarantee that 'a bad LLM suggestion cannot start a loop'. The deterministic frontier was empty, so the
        candidate is novel; we admit it ONLY if it carries EXPLICIT prerequisites that are ALL satisfied by
        observed state. An empty/missing precondition list is REJECTED (Forge NEW-1): `all([])` is vacuously
        true, so an ad-hoc unconditional LLM action would otherwise be admitted and could start a loop — and a
        catalog action whose preconditions were truly met would already be in the (here-empty) frontier."""
        try:
            preconds = list(getattr(action, "preconditions", None) or [])
            if not preconds:
                return False
            return all(state.satisfies_predicate(p) for p in preconds)
        except Exception:
            return False

    async def run(self) -> ControllerResult:
        """Trace one autonomous episode, then delegate unchanged to the controller loop.

        A wrapper rather than a `with` around the loop body: `_run` has many `return done(...)` paths, and
        re-indenting ~190 lines of kernel control flow to add a span is a poor trade against the risk of a
        mis-indent. This covers every exit path, including exceptions, without touching one line of the loop.
        """
        with _tracing.kernel_span(
            "sage.kernel.episode",
            **{"sage.episode.id": self._episode_id, "sage.episode.objective": self._objective},
        ) as span:
            result = await self._run()
            try:
                span.set_attribute("sage.episode.status", str(getattr(result, "status", "")))
                span.set_attribute("sage.episode.cycle_count", int(getattr(result, "cycle_count", 0) or 0))
                span.set_attribute("sage.episode.effect_count", len(getattr(result, "achieved_effects", ()) or ()))
                reason = str(getattr(result, "reason", "") or "")
                if reason:
                    span.set_attribute("sage.stop.reason", reason)
            except Exception:
                pass
            return result

    async def _run(self) -> ControllerResult:
        # Per-solve reset (a Sage Model may reuse one controller across solves; never leak a prior objective's
        # blockers/counters — the cross-solve leak Forge caught in the earlier P0 wiring).
        self._loop = wo.LoopBreakerState()
        self._current_cycle = 0  # span attribute only; never read by control flow
        self._collections = 0
        self._collection_attempts = {}
        self._no_effect = 0
        cycles: list[CycleRecord] = []
        decisions: list[Any] = []
        transactions: list[dict[str, Any]] = []
        blocker: dict | None = None
        achieved: set[str] = set()

        def done(status: str, reason: str) -> ControllerResult:
            return ControllerResult(status=status, reason=reason, blocker=blocker,
                                    cycle_count=len(cycles), cycles=cycles,
                                    achieved_effects=sorted(achieved),
                                    episode_id=self._episode_id,
                                    policy_mode=str(getattr(self._policy, "mode", "") or ""),
                                    decisions=[
                                        d.to_dict() if hasattr(d, "to_dict") else dict(d)
                                        for d in decisions
                                    ],
                                    transactions=list(transactions))

        # Budget check BEFORE the first (potentially expensive) observe — fail cheap.
        over = await self._budget_exceeded()
        if over:
            return done(STATUS_BUDGET, over)
        st, state = await self._seam(lambda: self._observe(), "observe")
        if st != "ok" or state is None:
            blocker = {"reason": f"observe unavailable: {state if st != 'ok' else 'None state'}"}
            return done(STATUS_NO_ACTION, "could not observe engagement state")
        achieved = self._achieved(state)

        for cycle in range(1, self.config.max_cycles + 1):
            # One span per controller cycle. The seams below nest beneath it, and because the
            # LangChain instrumentor falls back to ambient context for a parentless run, the
            # policy model call and MCP tool calls land under the cycle that caused them.
            with _tracing.kernel_span(
                    "sage.kernel.cycle", **{"sage.cycle.index": cycle}
            ):
                # Stamp the cycle onto every seam span opened below. A true per-cycle span would need this whole
                # body re-indented inside a `with`, which is not worth a mis-indent in kernel control flow; the
                # attribute groups a cycle's seams in Phoenix without touching the loop.
                self._current_cycle = cycle
                # 0) cooperative kill switch — honor an operator stop BETWEEN cycles (a hung in-cycle seam is bounded
                #    separately by seam_timeout_s). Cheap; checked before any expensive step.
                if self._should_abort is not None:
                    st, ab = await self._seam(lambda: self._should_abort(), "should_abort")
                    if st == "ok" and bool(ab):
                        return done(STATUS_ABORTED, "operator requested stop")

                # 1) completion
                if self._objective_met is not None:
                    st, met = await self._seam(lambda: self._objective_met(state), "objective_met")
                    if st == "ok" and bool(met):
                        return done(STATUS_COMPLETE, "objective satisfied")

                # 2) budget stop-loss (before any expensive step)
                over = await self._budget_exceeded()
                if over:
                    return done(STATUS_BUDGET, over)

                # 3) Compute collection and capability candidates once so policy sees the complete peer set.
                frontier = list(self._frontier_fn(state) or [])
                action = None
                decision = None
                phase = ""
                need = False
                if self._needs_collection is not None and self._collect is not None:
                    st, requested = await self._seam(lambda: self._needs_collection(state), "needs_collection")
                    if st == "ok":
                        need = requested
                    if bool(need):
                        collect_candidate = _CollectionCandidate(
                            target=_collection_request_key(need),
                        )
                        if self._policy is None:
                            selected, decision = collect_candidate, None
                        else:
                            selected, decision = await self._policy_select(
                                state,
                                [collect_candidate, *frontier],
                                decisions,
                            )
                        if decision is not None:
                            decisions.append(decision)
                        if selected is None:
                            rationale = str(getattr(decision, "rationale", "") or "policy did not authorize collection")
                            blocker = {"reason": rationale, "policy_mode": str(getattr(decision, "policy_mode", "") or "")}
                            cycles.append(CycleRecord(
                                cycle,
                                "collect",
                                action="collect-graph",
                                ok=False,
                                note=rationale,
                                decision_id=str(getattr(decision, "decision_id", "") or ""),
                                policy_mode=str(getattr(decision, "policy_mode", "") or ""),
                            ))
                            return done(STATUS_NO_ACTION, "policy declined graph collection")
                        if selected is collect_candidate:
                            request_key = _collection_request_key(need) or "__anonymous_collection_request__"
                            attempts = self._collection_attempts.get(request_key, 0)
                            if attempts >= self.config.max_collection_attempts_per_request:
                                blocker = {
                                    "reason": "collection retry budget exhausted",
                                    "collection_key": request_key,
                                    "attempts": attempts,
                                    "achieved": sorted(achieved),
                                }
                                return done(STATUS_BLOCKED, "collection retry budget exhausted for one request")
                            if self.config.max_collections is not None and self._collections >= self.config.max_collections:
                                blocker = {
                                    "reason": "configured global collection emergency cap exhausted",
                                    "collection_key": request_key,
                                    "attempts": attempts,
                                    "achieved": sorted(achieved),
                                }
                                return done(STATUS_BLOCKED, "configured global collection emergency cap exhausted")
                            transactions.append(_transaction_record("collection", "collect-graph", request_key, decision))
                            cst, cres = await self._collect_seam(state, decision)
                            self._collections += 1
                            attempts += 1
                            self._collection_attempts[request_key] = attempts
                            res = _parse_result(cres) if cst == "ok" else {"ok": False, "reason": cres}
                            collection_ok = res.get("ok") is True
                            note = str(res.get("status") or res.get("reason") or "")
                            collection_reason = str(res.get("collection_reason") or "")
                            if collection_reason:
                                note = f"{collection_reason}: {note}" if note else collection_reason
                            cycles.append(CycleRecord(
                                cycle,
                                "collect",
                                action="collect_graph",
                                ok=collection_ok,
                                note=note,
                                decision_id=str(getattr(decision, "decision_id", "") or ""),
                                policy_mode=str(getattr(decision, "policy_mode", "") or ""),
                            ))
                            if not collection_ok and attempts >= self.config.max_collection_attempts_per_request:
                                blocker = {
                                    "reason": note or "collection failed",
                                    "collection_key": request_key,
                                    "attempts": attempts,
                                    "achieved": sorted(achieved),
                                }
                                return done(STATUS_BLOCKED, "collection retry budget exhausted for one request")
                            ost, state = await self._seam(lambda: self._observe(), "observe")
                            if ost != "ok" or state is None:
                                blocker = {"reason": f"observe unavailable after collect: {state}"}
                                return done(STATUS_NO_ACTION, "could not observe after collection")
                            achieved = self._achieved(state)
                            continue
                        action = selected
                        phase = "execute"

                # 4) Resolve the already-computed capability frontier when collection was absent or not selected.
                if action is None and not frontier:
                    if self._policy is not None:
                        blocker = {
                            "reason": "no admissible capability action from observed state",
                            "policy_mode": str(getattr(self._policy, "mode", "") or ""),
                            "achieved": sorted(achieved),
                        }
                        cycles.append(CycleRecord(
                            cycle,
                            "policy",
                            ok=False,
                            note="empty frontier; explicit policy cannot use legacy route discovery",
                            policy_mode=str(getattr(self._policy, "mode", "") or ""),
                        ))
                        return done(STATUS_NO_ACTION, "empty frontier under explicit policy")
                    # 5) empty frontier -> bounded LLM route discovery (precondition-checked), else clean halt.
                    candidate = None
                    if self._route_discovery is not None:
                        st, cand = await self._seam(lambda: self._route_discovery(state), "route_discovery")
                        if st == "ok" and cand is not None and self._candidate_admissible(state, cand):
                            candidate = cand
                        elif st == "ok" and cand is not None:
                            self._log("route_discovery candidate failed precondition check; rejected")
                    if candidate is None:
                        blocker = {"reason": "no admissible capability action from observed state",
                                   "achieved": sorted(achieved)}
                        cycles.append(CycleRecord(cycle, "route_discovery", ok=False,
                                                  note="empty frontier; no admissible route-discovery candidate"))
                        return done(STATUS_NO_ACTION, "empty frontier and no admissible route-discovery move")
                    action = candidate
                    phase = "route_discovery"
                elif action is None:
                    action, decision = await self._policy_select(state, frontier, decisions)
                    if decision is not None:
                        decisions.append(decision)
                    if action is None:
                        rationale = str(getattr(decision, "rationale", "") or "policy produced no executable selection")
                        blocker = {
                            "reason": rationale,
                            "policy_mode": str(getattr(decision, "policy_mode", "") or ""),
                            "decision_id": str(getattr(decision, "decision_id", "") or ""),
                            "achieved": sorted(achieved),
                        }
                        cycles.append(CycleRecord(
                            cycle,
                            "policy",
                            ok=False,
                            note=rationale,
                            decision_id=str(getattr(decision, "decision_id", "") or ""),
                            policy_mode=str(getattr(decision, "policy_mode", "") or ""),
                        ))
                        return done(STATUS_NO_ACTION, "policy selected no offensive capability")
                    phase = "execute"

                name = cap._normalize(getattr(action, "name", ""))
                target = str(getattr(action, "target", ""))
                expected = _action_effects(action)

                # 6) execute (exception/timeout -> a blocker result, never a crash or a silent success)
                transactions.append(_transaction_record("capability", name, target, decision))
                est, eres = await self._execute_seam(action, decision)
                result = _parse_result(eres) if est == "ok" else {"ok": False, "reason": f"{est}: {eres}"}

                # 7) verify — re-observe and diff achieved effects; PROGRESS is attributed to THIS action's expected
                #    effect (not global drift, which unrelated effects could fake — Forge #4).
                prev = achieved
                ost, state = await self._seam(lambda: self._observe(), "observe")
                if ost != "ok" or state is None:
                    blocker = {"reason": f"observe unavailable after execute: {state}", "last_action": name}
                    return done(STATUS_NO_ACTION, "could not observe after execute")
                achieved = self._achieved(state)
                new_effects = sorted(achieved - prev)
                # PROGRESS is attributed to THIS action's own declared (canonical) effect — NOT to global drift
                # (unrelated effects landing could otherwise fake progress, Forge #4) and NOT via a global-drift
                # fallback for effectless actions (Forge NEW-3): an action with no declared effect cannot progress
                # by definition, so it must register as no-progress and be caught by the loop-breaker / no-effect cap.
                progressed = bool(expected & set(new_effects))

                cycles.append(CycleRecord(cycle, phase, action=name, target=target,
                                          ok=bool(result.get("ok", True)), new_effects=new_effects,
                                          note=str(result.get("reason", ""))[:160],
                                          decision_id=str(getattr(decision, "decision_id", "") or ""),
                                          policy_mode=str(getattr(decision, "policy_mode", "") or "")))
                self._log(f"cycle {cycle}: {phase} {name}->{target} ok={result.get('ok')} "
                          f"progressed={progressed} new_effects={new_effects}")

                zero_retry_blocker = _trajectory_zero_retry_blocker(result, progressed=progressed)
                if zero_retry_blocker is not None:
                    blocker = {
                        "capability": name,
                        "achieved": sorted(achieved),
                        "trajectory_repair": zero_retry_blocker,
                    }
                    return done(
                        STATUS_BLOCKED,
                        f"terminal trajectory blocker on {name}: retry budget exhausted",
                    )

                # 8) loop-breaker (worker_outcome): the EPOCH advances on VERIFIED progress (decoupled from the
                #    self-reported `ok`, Forge #3/NEW-4) while the OUTCOME is classified from the REAL result — so a
                #    real retry after a verified state change is not over-suppressed, a self-reported success with no
                #    verified effect cannot mask a loop, AND a slow/idempotent real success is not mislabeled a
                #    blocker. Same blocker + same epoch -> STOP (kills the 1116 redelegation tail).
                should_stop = wo.observe_capability_outcome(self._loop, name or target, result,
                                                            turn_key=str(cycle), progressed=progressed)
                if should_stop:
                    blocker = {"capability": name, "reason": str(result.get("reason") or result.get("error") or ""),
                               "suggested_capability": str(result.get("suggested_capability")
                                                           or result.get("run_first")
                                                           or result.get("next_capability") or ""),
                               "achieved": sorted(achieved)}
                    return done(STATUS_BLOCKED, f"terminal blocker on {name}: same blocker recurred without progress")

                # 9) no-progress stop-loss
                if progressed:
                    self._no_effect = 0
                else:
                    self._no_effect += 1
                    if self._no_effect >= self.config.max_no_effect_cycles:
                        blocker = {"reason": "no new verified effect", "last_action": name,
                                   "achieved": sorted(achieved)}
                        return done(STATUS_NO_PROGRESS,
                                    f"{self._no_effect} consecutive cycles produced no new verified effect")

        return done(STATUS_MAX_CYCLES, f"reached max_cycles={self.config.max_cycles}")

    async def _execute_seam(self, action: Any, decision: Any | None = None) -> tuple[str, Any]:
        try:
            signature = inspect.signature(self._execute)
            accepts_decision = any(
                parameter.kind == inspect.Parameter.VAR_POSITIONAL
                for parameter in signature.parameters.values()
            ) or len([
                parameter for parameter in signature.parameters.values()
                if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]) >= 2
        except (TypeError, ValueError):
            accepts_decision = False
        if accepts_decision:
            return await self._seam(lambda: self._execute(action, decision), "execute")
        return await self._seam(lambda: self._execute(action), "execute")

    async def _collect_seam(self, state: Any, decision: Any | None = None) -> tuple[str, Any]:
        try:
            signature = inspect.signature(self._collect)
            accepts_decision = any(
                parameter.kind == inspect.Parameter.VAR_POSITIONAL
                for parameter in signature.parameters.values()
            ) or len([
                parameter for parameter in signature.parameters.values()
                if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]) >= 2
        except (TypeError, ValueError):
            accepts_decision = False
        if accepts_decision:
            return await self._seam(lambda: self._collect(state, decision), "collect")
        return await self._seam(lambda: self._collect(state), "collect")
