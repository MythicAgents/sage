"""Live driver: bare-model vs harness on GOAD, scored by ONE shared ground-truth ruler.

⚠️  RUNS LIVE OFFENSIVE TOOLING with `--go`. Each run mutates the range, so a FAIR comparison requires a
    CLEAN RESET BEFORE EVERY RUN (your row, via `sage-goad-reset`). Therefore this driver runs exactly
    ONE side on ONE scenario per `--go` invocation and RECORDS the ScoreCard; you reset between runs;
    then `compare` combines the records. Safe to import / dry-run; only `--go` acts.

Both sides are scored by the SAME ledger-independent probes (collection-independent where it matters):
  * KRBTGT_DUMPED -> Mythic loot (mythic_credential_probe): FAIR — reflects what was actually dumped.
  * DA_CHILD / OBJECTIVE -> BloodHound cypher: COLLECTION-BIASED (favors Sage, which ingests BloodHound);
    treat as provisional until an AD-direct probe replaces them.

Operator loop per scenario:
  reset -> `run --go --side harness --scenario child-da`
  reset -> `run --go --side bare    --scenario child-da`
  `compare --scenario child-da`
"""
from __future__ import annotations

import argparse
import os
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from . import live_seams, bare_runner, bare_mythic_tools, bare_bloodhound, probes as probes_mod
    from .scenarios import all_scenarios, CHILD, OBJECTIVE
    from .range_state import Milestone
    from .fitness import ScoreCard
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import live_seams, bare_runner, bare_mythic_tools, bare_bloodhound, probes as probes_mod  # type: ignore
    from scenarios import all_scenarios, CHILD, OBJECTIVE  # type: ignore
    from range_state import Milestone  # type: ignore
    from fitness import ScoreCard  # type: ignore


@dataclass
class Config:
    sage_cb: int | None = None  # legacy payload path only
    apollo_cb: int = 4
    engagement_op: str = "Operation_Chimera_1"
    max_steps: int = 0          # 0 = UNLIMITED steps for the bare model (parity with Sage's solve)
    solve_timeout: int = 1800   # also the bare loop's wall-clock ceiling (not a step/recursion cap)
    # AD-direct DA probes poll for up to this long (re-reading every interval) so GPO/SYSTEM-on-DC
    # membership changes have time to propagate before scoring — returns True the instant they appear.
    da_settle_timeout: int = 300
    da_settle_interval: int = 20
    policy_mode: str = "llm"
    model_provider: str | None = None
    model_id: str | None = None
    model_api_endpoint: str | None = None
    model_api_key: str | None = None
    null_model: bool = False

    @property
    def results_path(self) -> Path:
        override = str(os.environ.get("SAGE_EVAL_RESULTS_PATH") or "").strip()
        if override:
            return Path(override).expanduser()
        return Path(__file__).resolve().parents[2] / ".hillclimb" / "results" / "bare_vs_harness.jsonl"


def _resolved_harness_model_route(cfg: Config) -> dict[str, str | None]:
    """Resolve the route the harness should use when no explicit treatment override is supplied.

    Native chat gets its defaults from Sage's local env-backed metadata. The headless controller path bypasses
    that channel metadata entirely, so it must carry the same provider/model/endpoint/key explicitly or it
    constructs ``Model`` with an empty config and fails before the first controller cycle.
    """
    defaults = live_seams.load_sage_defaults()
    return {
        "provider": cfg.model_provider or defaults.get("provider"),
        "model": cfg.model_id or defaults.get("model"),
        "api_endpoint": cfg.model_api_endpoint or defaults.get("base_url"),
        "api_key": cfg.model_api_key or defaults.get("api_key"),
    }


def build_probes(reader, baseline: dict, scenario, *, settle_timeout: float = 0,
                 settle_interval: float = 20) -> dict:
    """Build ledger-independent range probes shared by harness and bare-model runs.

    KRBTGT is proven from Mythic loot/task output. DA is proven from out-of-band LDAP membership delta.
    OBJECTIVE accepts either LDAP admin membership or completed Mythic task output that independently
    replays the certificate-auth / forged-ticket verifiers; those access paths do not mutate group
    membership.
    """
    sub = set(getattr(scenario, "milestone_subset", None) or tuple(Milestone))
    domains = dict(getattr(scenario, "domains", {}) or {})
    child_domain = str(domains.get("child") or CHILD)
    objective_domain = str(domains.get("objective") or OBJECTIVE)
    probes = {Milestone.KRBTGT_DUMPED: live_seams.krbtgt_dumped_probe(realm=child_domain)}
    if Milestone.DA_CHILD in sub:
        probes[Milestone.DA_CHILD] = live_seams.ad_domain_admins_probe_via_reader(
            reader, child_domain, baseline=baseline.get(child_domain, set()),
            settle_timeout=settle_timeout, settle_interval=settle_interval)
    if Milestone.OBJECTIVE in sub:
        if _objective_uses_remote_exec_probe(scenario):
            remote_targets = _objective_remote_exec_targets(scenario)
            if len(remote_targets) != 1:
                raise ValueError(
                    "remote-exec OBJECTIVE scenarios must declare exactly one host/domain target"
                )
            target_host, target_domain = remote_targets[0]
            target_domain = target_domain or objective_domain
            probes[Milestone.OBJECTIVE] = live_seams.any_probe(
                live_seams.remote_execution_probe(target_host, realm=target_domain),
                settle_timeout=settle_timeout,
                settle_interval=settle_interval,
            )
        else:
            probes[Milestone.OBJECTIVE] = live_seams.any_probe(
                live_seams.certificate_admin_control_probe("administrator", realm=objective_domain),
                live_seams.ticket_admin_control_probe(realm=objective_domain),
                live_seams.ad_domain_admins_probe_via_reader(
                    reader,
                    objective_domain,
                    baseline=baseline.get(objective_domain, set()),
                    settle_timeout=0,
                ),
                settle_timeout=settle_timeout,
                settle_interval=settle_interval,
            )
    if Milestone.GRAPH_COLLECTED in sub:
        # Ground-truth, ledger-independent: BloodHound holds >=1 ingested Domain == a SharpHound
        # collection was successfully run AND uploaded. Independent of Sage's self-reported
        # `graph-built:` predicate, so range_state's disagreement check ("who verifies the verifier")
        # can finally catch a collection self-report that reality does not back. Scenarios that do not
        # score GRAPH_COLLECTED (e.g. child-da) never build this probe.
        probes[Milestone.GRAPH_COLLECTED] = live_seams.graph_collected_probe()
    return probes


def _scenario(cfg: Config, name: str):
    scenarios = all_scenarios(cfg.engagement_op)
    for s in scenarios:
        if s.name == name:
            return s
    raise SystemExit(f"unknown scenario {name!r}; choices: {[s.name for s in scenarios]}")


def objective_recognizable(objective: str) -> tuple[bool, str]:
    """Cheap pre-run contract check: is completion-recognition REACHABLE for this objective?

    Guards the harness->Sage objective seam that regressed in the Phase-0 re-set (the gauge's read-only/
    seam-injected architecture made the seam untestable offline, so the only detector was a ~60-min live
    range run). The HARD contract is the exact thing that regressed: the objective Sage runs with must NOT
    be blank/opaque (`sage-engagement:*`), since engagement_state._objective_is_complete deliberately never
    completes an opaque objective. Target-domain parse is reported as a DIAGNOSTIC only, NOT a failure:
    objectives like single-hop-system ("SYSTEM on a host") and cross-forest-objective ("control of THE
    objective domain X") legitimately have no DA-phrase target and complete via the no-next-hop/milestone
    fallback — gating on target-parse would wrongly abort valid scenarios. Returns (ok, reason)."""
    o = str(objective or "").strip()
    if not o or o.casefold().startswith("sage-engagement"):
        return False, f"objective is empty/opaque (completion-recognition unreachable): {o!r}"
    try:
        from ..langgraph import engagement_state as _es
    except Exception:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
        import engagement_state as _es  # type: ignore
    targets = _es._objective_target_domains(o)
    if targets:
        return True, f"recognizable: target-matched completion (targets={sorted(targets)})"
    remote_exec_targets = _es._objective_remote_exec_targets(o)
    if remote_exec_targets:
        return True, f"recognizable: remote-exec target-matched completion (targets={remote_exec_targets})"
    return True, "recognizable: non-opaque; completion via no-next-hop/milestone fallback (no DA-phrase target)"


def _scored_referee_domains(scn) -> set:
    """AD-direct domains a scenario SCORES (so the run needs out-of-band referee LDAP creds for each).
    Mirrors run_side's baseline scope EXACTLY, so the preflight validates precisely the creds the run will
    require — no more, no less."""
    sub = set(getattr(scn, "milestone_subset", None) or tuple(Milestone))
    domains = dict(getattr(scn, "domains", {}) or {})
    scored: set[str] = set()
    if Milestone.DA_CHILD in sub:
        child = str(domains.get("child") or "").strip()
        if child:
            scored.add(child)
    if Milestone.OBJECTIVE in sub and not _objective_uses_remote_exec_probe(scn):
        objective = str(domains.get("objective") or "").strip()
        if objective:
            scored.add(objective)
    return scored


def _objective_effect_prefixes(scenario) -> tuple[str, ...]:
    try:
        spec = scenario.spec()
    except Exception:
        return ()
    objective_spec = spec.get(Milestone.OBJECTIVE)
    return tuple(str(prefix or "").strip().casefold() for prefix in getattr(objective_spec, "effect_prefixes", ()) or ())


def _objective_uses_remote_exec_probe(scenario) -> bool:
    return "remote-exec:" in _objective_effect_prefixes(scenario)


def _objective_remote_exec_targets(scenario) -> list[tuple[str, str]]:
    try:
        from ..langgraph import engagement_state as _es
    except Exception:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
        import engagement_state as _es  # type: ignore
    return list(_es._objective_remote_exec_targets(str(getattr(scenario, "objective", "") or "")))


def _referee_creds_present(domain: str) -> tuple[bool, str]:
    """OFFLINE check: the referee config has dc_ip+user+password for `domain` (reads the JSON config; does
    NOT bind to LDAP). This is the cheap tier of the cred precondition — the live tier (can we actually bind
    to the DC) genuinely needs the range up, so only the offline tier is hoisted before the reset."""
    try:
        from . import live_seams as _ls
    except Exception:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import live_seams as _ls  # type: ignore
    try:
        _ls.referee_domain_entry(domain, config=_ls.load_referee_ldap_config())
        return True, f"referee creds present for {domain}"
    except Exception as e:
        return False, str(e)


def scenario_preconditions(cfg: "Config", scenario_name: str) -> list:
    """DECLARED preconditions for a scenario — the manifest the preflight ITERATES (vs a hand-maintained
    allowlist). Each entry is (name, cost, check) where check()->(ok, detail) and cost is:
      'offline' — validatable with no lab/reset (hoisted before any expensive step), or
      'live'    — genuinely needs the running range (cannot be hoisted; runs in-path).
    The fail-cheap-before-expensive contract: declare a new precondition HERE, never validate it lazily at
    point-of-use inside the post-reset path. New preconditions are then gated automatically — no one has to
    "remember to add it to preflight" after the next incident."""
    scn = _scenario(cfg, scenario_name)
    checks: list = [("objective-recognizable", "offline", lambda: objective_recognizable(scn.objective))]
    # 'smoke' tier: needs Mythic/BloodHound UP (but NOT the GOAD range), so still hoistable before the
    # expensive reset+solve. Fail-CLOSED validity checks that run the EXACT probe queries against the live
    # schema — a GraphQL field typo / down service fails in ~5s here instead of after a ~2h solve (the
    # 2026-06-21 `responses{response}` bug). All scenarios score KRBTGT_DUMPED (Mythic); only graph-scoring
    # scenarios use the BloodHound REST path.
    checks.append(("mythic-queries-valid", "smoke", lambda: live_seams.mythic_queries_valid()))
    sub = set(getattr(scn, "milestone_subset", None) or tuple(Milestone))
    if Milestone.GRAPH_COLLECTED in sub:
        checks.append(("bloodhound-reachable", "smoke", lambda: live_seams.bloodhound_reachable()))
    for d in sorted(_scored_referee_domains(scn)):
        checks.append((f"referee-creds:{d}", "offline", (lambda d=d: _referee_creds_present(d))))
    return checks


def validate_harness_runtime_telemetry(
    configured_policy_mode: str,
    telemetry: dict,
    *,
    configured_provider: str | None = None,
    configured_model: str | None = None,
) -> None:
    """Reject mislabeled or incompletely attributed harness runs."""
    if not isinstance(telemetry, dict) or not telemetry:
        raise RuntimeError("Sage returned no observed runtime telemetry")
    observed = str(telemetry.get("policy_mode") or "").strip().casefold()
    configured = str(configured_policy_mode or "").strip().casefold()
    if observed != configured:
        raise RuntimeError(
            f"configured policy mode {configured!r} did not match observed mode {observed!r}"
        )
    if str(telemetry.get("configured_policy_mode") or "").strip().casefold() != configured:
        raise RuntimeError("runtime telemetry did not preserve the configured policy mode")
    switches = telemetry.get("policy_switches")
    if not isinstance(switches, list):
        raise RuntimeError("runtime telemetry omitted policy switch records")
    if switches or telemetry.get("policy_identity_valid") is not True:
        raise RuntimeError(f"runtime policy identity invalid; switches={switches!r}")
    for label, expected, key in (
        ("provider", configured_provider, "model_provider"),
        ("model", configured_model, "model_id"),
    ):
        if not expected:
            continue
        actual = str(telemetry.get(key) or "").strip()
        if actual.casefold() != str(expected).strip().casefold():
            raise RuntimeError(
                f"configured {label} {expected!r} did not match observed {label} {actual!r}"
            )
    total = int(telemetry.get("semantic_transaction_count", 0) or 0)
    authorized = int(telemetry.get("authorized_transaction_count", 0) or 0)
    coverage = float(telemetry.get("semantic_policy_coverage", 0.0) or 0.0)
    if total < 0 or authorized < 0 or authorized > total:
        raise RuntimeError(
            f"invalid semantic transaction counts: authorized={authorized}, total={total}"
        )
    expected_coverage = authorized / total if total else 1.0
    if abs(coverage - expected_coverage) > 1e-9:
        raise RuntimeError(
            f"semantic policy coverage {coverage} disagrees with counts "
            f"{authorized}/{total}"
        )
    if authorized != total:
        raise RuntimeError(
            f"semantic policy provenance incomplete: {authorized}/{total} transactions authorized"
        )
    model_calls = int(telemetry.get("model_calls", 0) or 0)
    if configured in {"llm", "hybrid"} and model_calls:
        backend_requests = telemetry.get("effective_backend_requests")
        if not isinstance(backend_requests, list):
            raise RuntimeError("runtime telemetry omitted effective backend request records")
        if len(backend_requests) != model_calls:
            raise RuntimeError(
                f"effective backend provenance incomplete: {len(backend_requests)}/{model_calls} model responses"
            )
        for request in backend_requests:
            if not isinstance(request, dict):
                raise RuntimeError("runtime telemetry contains a malformed effective backend record")
            if not str(request.get("effective_backend") or "").strip():
                raise RuntimeError("runtime telemetry omitted the response-derived effective backend")
            if str(request.get("backend_provenance_source") or "").strip() in {"", "unavailable"}:
                raise RuntimeError("runtime telemetry omitted the effective backend provenance source")
        if telemetry.get("backend_provenance_complete") is not True:
            raise RuntimeError("runtime telemetry marked effective backend provenance incomplete")


def runtime_evidence_fields(telemetry: dict | None) -> dict:
    """Persist the controller evidence needed to audit branch quality after the next lab reset."""
    if not isinstance(telemetry, dict):
        return {}
    keys = (
        "controller_status",
        "controller_cycle_count",
        "controller_cycles",
        "controller_blocker",
        "achieved_effects",
        "model_calls",
        "backend_provenance_complete",
        "policy_switches",
        "decisions",
        "transactions",
    )
    return {key: telemetry[key] for key in keys if key in telemetry}


def run_side(cfg: Config, side: str, scenario_name: str) -> ScoreCard:
    """Run ONE side on ONE scenario (range assumed freshly reset), score via the shared probes, record."""
    scn = _scenario(cfg, scenario_name)
    client = live_seams.default_mythic_client()
    runtime_telemetry: dict = {}

    # GROUND TRUTH is read OUT-OF-BAND over LDAP (the referee), NOT through the agent's Apollo callback.
    # Routing it through that callback would (a) pollute the HARNESS — Sage reconciles the callback's task
    # history at solve start, so it would see the recon AND the answer, asymmetrically vs the bare model —
    # and (b) misread the domain (`net group /domain` only enumerates the host's own domain). Scope the
    # baseline to the AD-direct milestones THIS scenario scores, so e.g. child-da neither queries nor needs
    # referee creds for essos. The baseline is captured pre-solve but off-callback, so it cannot pollute.
    needed = _scored_referee_domains(scn)   # same scope the preflight validates creds for (single source)
    reader = live_seams.make_referee_reader() if needed else (lambda _d: set())
    baseline = {d: reader(d) for d in needed}
    probes = build_probes(reader, baseline, scn,
                          settle_timeout=cfg.da_settle_timeout, settle_interval=cfg.da_settle_interval)

    if side == "harness":
        harness_route = _resolved_harness_model_route(cfg)
        if cfg.null_model or os.environ.get("SAGE_EVAL_HEADLESS"):
            # Option A (Phase-4 migration): run the solve IN-PROCESS via the chat Model instead of tasking
            # the PayloadType `query` on the virtual callback. Alongside path — selected only when the flag
            # is set — so a migration run can compare in-process vs task-based on the same reset. Ledger key
            # comes from SAGE_ENGAGEMENT_ID (the run token the reset sets) or the operation name.
            _eng = os.environ.get("SAGE_ENGAGEMENT_ID") or cfg.engagement_op
            treatment = "null-model" if cfg.null_model else "headless"
            print(
                f"[harness/{scenario_name}] {treatment} in-process solve (engagement={_eng})",
                flush=True,
            )
            solve = live_seams.make_headless_solver(client, engagement_id=_eng,
                                                    timeout=cfg.solve_timeout, max_steps=0,
                                                    policy_mode=cfg.policy_mode,
                                                    provider=harness_route["provider"],
                                                    model=harness_route["model"],
                                                    api_endpoint=harness_route["api_endpoint"],
                                                    api_key=harness_route["api_key"],
                                                    null_model=cfg.null_model)
        else:
            solve = live_seams.make_native_chat_solver(
                client,
                timeout=cfg.solve_timeout,
                provider=cfg.model_provider,
                model=cfg.model_id,
                api_endpoint=cfg.model_api_endpoint,
                api_key=cfg.model_api_key,
                eval_force_capability_prefix_json=os.environ.get(
                    "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON"
                ) or None,
            )
        _start = time.time()
        _deadline = _start + cfg.solve_timeout
        print(f"[harness/{scenario_name}] started {time.strftime('%H:%M:%S', time.localtime(_start))} · "
              f"times out by {time.strftime('%H:%M:%S', time.localtime(_deadline))} "
              f"(+{cfg.solve_timeout // 60} min via --solve-timeout, unless Sage finishes first)", flush=True)
        solve_status = "done"
        try:
            # make_harness_solver returns the Mythic task status: "success" when Sage finished,
            # "timeout" when the wall-clock poll expired (Sage may still be churning in the background).
            solve_status = solve(scn.objective) or "done"
        except KeyboardInterrupt:
            solve_status = "interrupted"
            print("⎈ interrupted — scoring the current range state", flush=True)
        _elapsed = int(time.time() - _start)
        print(f"[harness/{scenario_name}] solve returned {time.strftime('%H:%M:%S')} "
              f"(elapsed {_elapsed // 60}m{_elapsed % 60}s, status={solve_status})", flush=True)
        native_result = getattr(solve, "last_result", None)
        runtime_telemetry = (
            dict(native_result.get("runtime_telemetry") or {})
            if isinstance(native_result, dict)
            else {}
        )
        validate_harness_runtime_telemetry(
            cfg.policy_mode,
            runtime_telemetry,
            configured_provider=harness_route["provider"],
            configured_model=harness_route["model"],
        )
        # Record the REAL terminal status (incl. "timeout") AND the wall-clock cost in the card -> jsonl.
        # wall_seconds is the discriminating signal once capability saturates: a completion-recognized clean
        # stop (status="stopped" well under the budget) vs a churn-to-timeout shows up here, not in capability.
        card = bare_runner.score_from_probes(
            scn,
            probes,
            status=solve_status,
            wall_seconds=_elapsed,
            runtime_telemetry=runtime_telemetry,
            configured_policy_mode=cfg.policy_mode,
        )
    elif side == "bare":
        bare = build_bare_runner(cfg)            # builds its own stripped-Mythic dispatcher (all callbacks)
        result = bare.run(scn.objective)
        card = bare_runner.score_bare_run(result, scn, probes)
    else:
        raise SystemExit("--side must be harness|bare")

    # Record which LLM produced this run, for provenance and multi-LLM matrices. Today both sides read
    # the model from Sage's .env (bare via build_bare_runner -> load_sage_defaults; harness = whatever Sage
    # is running). When a bare-side --model override is added, record THAT for the bare side instead.
    _defs = live_seams.load_sage_defaults()
    if cfg.model_provider:
        _defs["provider"] = cfg.model_provider
    if cfg.model_id:
        _defs["model"] = cfg.model_id
    if side == "bare":
        card.policy_mode = "llm"
        card.configured_policy_mode = "llm"
        card.policy_identity_valid = True
        card.model_provider = str(_defs.get("provider") or "")
        card.model_id = str(_defs.get("model") or "")
    _now = time.time()
    rec = {"side": side, "scenario": scenario_name,
           "model": _defs.get("model"), "provider": _defs.get("provider"),
           "policy_mode": card.policy_mode,
           "configured_policy_mode": card.configured_policy_mode,
           "policy_identity_valid": card.policy_identity_valid,
           "request_completed": card.request_completed,
           "objective_recognized": card.objective_recognized,
           "objective_proven": card.objective_proven,
           "clean_stop": card.clean_stop,
           "controller_terminal_reason": card.controller_terminal_reason,
           "semantic_transaction_count": card.semantic_transaction_count,
           "authorized_transaction_count": card.authorized_transaction_count,
           "semantic_policy_coverage": card.semantic_policy_coverage,
           "effective_backends": list(card.effective_backends),
           "effective_backend_requests": list(card.effective_backend_requests),
           # ts = epoch (sortable); ts_iso = local-time human stamp to eyeball-correlate to the archived
           # sage_<YYYYMMDD-HHMM>.db / phoenix_<...>.db moved at the NEXT reset (which holds THIS run's data).
           "ts": _now, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_now)),
           "card": asdict(card)}
    for env_key, row_key in (
        ("SAGE_EVAL_PHASE6_MANIFEST_HASH", "phase6_manifest_hash"),
        ("SAGE_EVAL_PHASE6_TOPOLOGY_HASH", "phase6_topology_hash"),
        ("SAGE_EVAL_PHASE6_CANDIDATE_SET_HASH", "phase6_candidate_set_hash"),
        ("SAGE_EVAL_PHASE6_ORDERED_FRONTIER_HASH", "phase6_ordered_frontier_hash"),
        ("SAGE_EVAL_PHASE6_FORCED_PATH", "phase6_forced_path"),
        ("SAGE_EVAL_PHASE6_PLANNED_ROW_ID", "phase6_planned_row_id"),
        ("SAGE_EVAL_PHASE6_ATTEMPT_INDEX", "phase6_attempt_index"),
        (
            "SAGE_EVAL_PHASE6_MAX_PRE_FRONTIER_DIAGNOSTIC_RETRIES",
            "phase6_max_pre_frontier_diagnostic_retries",
        ),
    ):
        value = str(os.environ.get(env_key) or "").strip()
        if value:
            rec[row_key] = (
                int(value)
                if row_key in {"phase6_attempt_index", "phase6_max_pre_frontier_diagnostic_retries"}
                else value
            )
    rec.update(runtime_evidence_fields(runtime_telemetry))
    native_result = getattr(locals().get("solve"), "last_result", None)
    if isinstance(native_result, dict):
        rec["chat_channel_id"] = native_result.get("chat_channel_id")
        rec["chat_request_id"] = native_result.get("chat_request_id")
    p = cfg.results_path
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    print(f"[{side}/{scenario_name}] capability={card.capability:.3f} furthest={card.furthest_milestone} "
          f"-> recorded to {p}", flush=True)
    return card


def build_bare_runner(cfg: Config) -> bare_runner.BareModelRunner:
    d = live_seams.load_sage_defaults()
    model_fn = live_seams.make_model_fn(d["provider"], d["model"], api_key=d["api_key"], base_url=d["base_url"])
    # Bare uses the SAME Mythic interface as Sage (enumerate payloads -> commands -> args -> task), via the
    # STRIPPED (Sage-free) toolset — NOT a hardcoded Apollo command list. The dispatcher routes the model's
    # tool calls straight to the raw Mythic SDK. No step cap (cfg.max_steps=0); solve_timeout is a
    # wall-clock ceiling; a live stdout logger lets the operator watch the model between Mythic tasks.
    client = live_seams.default_mythic_client()
    mythic_exec = bare_mythic_tools.make_mythic_dispatcher(client)

    # BloodHound: discover EVERY MCP tool dynamically (not hardcoded). Raw external tool — Sage's
    # BloodHound *agent* (ingest reconciliation, collect-once gate, graph-fact injection) stays excluded.
    bh_specs, bh_registry = bare_bloodhound.load_bloodhound_mcp_tools()
    bh_exec = bare_bloodhound.make_bloodhound_dispatcher(bh_registry)
    print(f"[bare] toolset: {len(bare_mythic_tools.TOOLS)} Mythic + {len(bh_specs)} BloodHound MCP tools", flush=True)

    def executor(call: dict) -> str:
        name = call.get("tool", "")
        if name in bare_mythic_tools.TOOLS:
            return mythic_exec(call)
        bh = bh_exec(call)
        if bh is not None:
            return bh
        return f"[unknown tool] {name!r}"

    return bare_runner.BareModelRunner(
        model_fn, executor,
        tools_spec=bare_mythic_tools.bare_tool_specs() + bh_specs,
        max_steps=cfg.max_steps,
        timeout=cfg.solve_timeout,
        logger=bare_runner.make_stdout_logger(),
    )


def compare(cfg: Config, scenario_name: str) -> None:
    p = cfg.results_path
    if not p.exists():
        raise SystemExit(f"no records at {p}; run both sides first")
    bare, harness = [], []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("scenario") != scenario_name:
            continue
        card = ScoreCard(**rec["card"])
        (harness if rec["side"] == "harness" else bare).append(card)
    if not bare or not harness:
        raise SystemExit(f"need >=1 bare and >=1 harness record for {scenario_name} "
                         f"(have bare={len(bare)} harness={len(harness)})")
    cmp = bare_runner.compare_bare_vs_harness(scenario_name, bare, harness)
    print(f"[{scenario_name}] verdict={cmp.verdict} delta={cmp.delta:+.3f} "
          f"(harness={cmp.harness_capability:.3f} bare={cmp.bare_capability:.3f} mde={cmp.min_detectable_effect:.3f})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live bare-vs-harness gauge (GOAD). Reset before EVERY --go run.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run ONE side on ONE scenario (range must be freshly reset)")
    r.add_argument("--side", choices=["harness", "bare"], required=True)
    r.add_argument("--scenario", required=True)
    r.add_argument("--go", action="store_true", help="actually run live offensive tooling")
    r.add_argument("--sage-cb", type=int, default=None)
    r.add_argument("--apollo-cb", type=int, default=None)
    r.add_argument("--solve-timeout", type=int, default=None,
                   help="seconds to wait for the harness solve (default 1800=30min); raise for full solves")
    r.add_argument("--policy-mode", choices=["llm", "hybrid", "symbolic"], default="llm",
                   help="policy identity of the running Sage harness")
    r.add_argument("--provider", default=None,
                   help="explicit harness model provider; required for controlled multi-model runs")
    r.add_argument("--model", default=None,
                   help="explicit harness model ID; required for controlled multi-model runs")
    r.add_argument("--null-model", action="store_true",
                   help="headless ablation: disable the policy model after Model initialization")
    r.add_argument("--da-settle-timeout", type=int, default=None,
                   help="seconds the DA probes poll for GPO/SYSTEM-on-DC membership to propagate before "
                        "scoring (default 300=5min). Returns True the instant it appears; 0 = immediate.")
    c = sub.add_parser("compare", help="combine recorded ScoreCards for a scenario into a verdict")
    c.add_argument("--scenario", required=True)
    pf = sub.add_parser("preflight",
                        help="fast (<5s) check that the scenario objective is completion-recognizable; "
                             "run BEFORE a reset+solve so a dropped/opaque objective fails cheap, not after 60 min")
    pf.add_argument("--scenario", required=True)
    args = ap.parse_args(argv)

    cfg = Config()
    cfg.model_api_endpoint = os.environ.get("SAGE_EVAL_API_ENDPOINT") or None
    cfg.model_api_key = os.environ.get("SAGE_EVAL_API_KEY") or None

    if args.cmd == "preflight":
        # Iterate the DECLARED manifest and run every OFFLINE precondition before any expensive step.
        # Exit non-zero on ANY failure so orchestrate aborts before spending a reset (fail-cheap-first).
        failures = []
        offline = [(n, c) for (n, cost, c) in scenario_preconditions(cfg, args.scenario)
                   if cost in ("offline", "smoke")]
        for name, check in offline:
            try:
                ok, detail = check()
            except Exception as e:
                ok, detail = False, f"check raised: {e}"
            print(f"[preflight/{args.scenario}] {'OK  ' if ok else 'FAIL'} {name}: {detail}", flush=True)
            if not ok:
                failures.append(name)
        if failures:
            print(f"[preflight/{args.scenario}] {len(failures)} precondition(s) FAILED before any reset: "
                  f"{failures} — fix these (they are all knowable offline) then re-run.", flush=True)
            return 2
        print(f"[preflight/{args.scenario}] all {len(offline)} offline preconditions passed", flush=True)
        return 0
    if getattr(args, "sage_cb", None) is not None:
        cfg.sage_cb = args.sage_cb
    if getattr(args, "apollo_cb", None) is not None:
        cfg.apollo_cb = args.apollo_cb
    if getattr(args, "solve_timeout", None) is not None:
        cfg.solve_timeout = args.solve_timeout
    if getattr(args, "da_settle_timeout", None) is not None:
        cfg.da_settle_timeout = args.da_settle_timeout
    if getattr(args, "policy_mode", None):
        cfg.policy_mode = args.policy_mode
    if getattr(args, "provider", None):
        cfg.model_provider = args.provider
    if getattr(args, "model", None):
        cfg.model_id = args.model
    if getattr(args, "null_model", False):
        cfg.null_model = True

    if args.cmd == "compare":
        compare(cfg, args.scenario)
        return 0

    if not args.go:
        d = live_seams.load_sage_defaults()
        print(f"DRY RUN (no --go). Would run side={args.side} scenario={args.scenario}")
        print(f"  RESET the range first ({{sage-goad-reset}}) — each run needs a clean lab.")
        print(f"  bare model (Sage .env): provider={d['provider']} model={d['model']}")
        print(f"  probes (all collection-independent): KRBTGT_DUMPED (Mythic loot), "
              f"DA_CHILD/OBJECTIVE (AD-direct: live DC Domain Admins vs post-reset baseline)")
        print(f"  objective: {_scenario(cfg, args.scenario).objective}")
        if cfg.null_model:
            print("  treatment: null policy model (headless; no policy inference calls)")
        return 0

    try:
        run_side(cfg, args.side, args.scenario)
    except KeyboardInterrupt:
        # Backstop for a Ctrl-C OUTSIDE the model loop / harness wait (e.g. during baseline LDAP or
        # scoring); interrupts inside those are already caught and scored. Exit cleanly, no traceback.
        print("\n⎈ interrupted by operator — exiting cleanly.", flush=True)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
