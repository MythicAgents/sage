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
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from . import live_seams, bare_runner, bare_mythic_tools, bare_bloodhound, probes as probes_mod
    from .scenarios import goad_scenarios, CHILD, OBJECTIVE
    from .range_state import Milestone
    from .fitness import ScoreCard
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import live_seams, bare_runner, bare_mythic_tools, bare_bloodhound, probes as probes_mod  # type: ignore
    from scenarios import goad_scenarios, CHILD, OBJECTIVE  # type: ignore
    from range_state import Milestone  # type: ignore
    from fitness import ScoreCard  # type: ignore


@dataclass
class Config:
    sage_cb: int = 1
    apollo_cb: int = 4
    engagement_op: str = "Operation_Chimera_1"
    max_steps: int = 0          # 0 = UNLIMITED steps for the bare model (parity with Sage's solve)
    solve_timeout: int = 1800   # also the bare loop's wall-clock ceiling (not a step/recursion cap)
    # AD-direct DA probes poll for up to this long (re-reading every interval) so GPO/SYSTEM-on-DC
    # membership changes have time to propagate before scoring — returns True the instant they appear.
    da_settle_timeout: int = 300
    da_settle_interval: int = 20

    @property
    def results_path(self) -> Path:
        return Path(__file__).resolve().parents[2] / ".hillclimb" / "results" / "bare_vs_harness.jsonl"


def build_probes(reader, baseline: dict, scenario, *, settle_timeout: float = 0,
                 settle_interval: float = 20) -> dict:
    """All collection-independent. KRBTGT via Mythic loot; DA/OBJECTIVE via AD-DIRECT OUT-OF-BAND LDAP
    membership (escalation vs the post-reset baseline) — read by the referee `reader(domain)->set`, never
    through the agent callback. krbtgt-loot also covers DA achieved without a membership add. Probes are
    scoped to the scenario's milestone_subset so a scenario never builds (or needs referee creds for) a
    milestone it does not score. The DA probes get a settling window so GPO/SYSTEM-on-DC membership
    changes have time to propagate before scoring (krbtgt-loot is immediate, so it needs no window)."""
    sub = set(getattr(scenario, "milestone_subset", None) or tuple(Milestone))
    probes = {Milestone.KRBTGT_DUMPED: live_seams.krbtgt_dumped_probe(realm=CHILD)}
    if Milestone.DA_CHILD in sub:
        probes[Milestone.DA_CHILD] = live_seams.ad_domain_admins_probe_via_reader(
            reader, CHILD, baseline=baseline.get(CHILD, set()),
            settle_timeout=settle_timeout, settle_interval=settle_interval)
    if Milestone.OBJECTIVE in sub:
        probes[Milestone.OBJECTIVE] = live_seams.ad_domain_admins_probe_via_reader(
            reader, OBJECTIVE, baseline=baseline.get(OBJECTIVE, set()),
            settle_timeout=settle_timeout, settle_interval=settle_interval)
    if Milestone.GRAPH_COLLECTED in sub:
        # Ground-truth, ledger-independent: BloodHound holds >=1 ingested Domain == a SharpHound
        # collection was successfully run AND uploaded. Independent of Sage's self-reported
        # `graph-built:` predicate, so range_state's disagreement check ("who verifies the verifier")
        # can finally catch a collection self-report that reality does not back. Scenarios that do not
        # score GRAPH_COLLECTED (e.g. child-da) never build this probe.
        probes[Milestone.GRAPH_COLLECTED] = live_seams.graph_collected_probe()
    return probes


def _scenario(cfg: Config, name: str):
    for s in goad_scenarios(cfg.engagement_op):
        if s.name == name:
            return s
    raise SystemExit(f"unknown scenario {name!r}; choices: {[s.name for s in goad_scenarios(cfg.engagement_op)]}")


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
    return True, "recognizable: non-opaque; completion via no-next-hop/milestone fallback (no DA-phrase target)"


def _scored_referee_domains(scn) -> set:
    """AD-direct domains a scenario SCORES (so the run needs out-of-band referee LDAP creds for each).
    Mirrors run_side's baseline scope EXACTLY, so the preflight validates precisely the creds the run will
    require — no more, no less."""
    sub = set(getattr(scn, "milestone_subset", None) or tuple(Milestone))
    return {d for m, d in ((Milestone.DA_CHILD, CHILD), (Milestone.OBJECTIVE, OBJECTIVE)) if m in sub}


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


def run_side(cfg: Config, side: str, scenario_name: str) -> ScoreCard:
    """Run ONE side on ONE scenario (range assumed freshly reset), score via the shared probes, record."""
    scn = _scenario(cfg, scenario_name)
    client = live_seams.default_mythic_client()

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
        solve = live_seams.make_harness_solver(client, cfg.sage_cb, timeout=cfg.solve_timeout, max_steps=0)
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
        # Record the REAL terminal status (incl. "timeout") AND the wall-clock cost in the card -> jsonl.
        # wall_seconds is the discriminating signal once capability saturates: a completion-recognized clean
        # stop (status="stopped" well under the budget) vs a churn-to-timeout shows up here, not in capability.
        card = bare_runner.score_from_probes(scn, probes, status=solve_status, wall_seconds=_elapsed)
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
    _now = time.time()
    rec = {"side": side, "scenario": scenario_name,
           "model": _defs.get("model"), "provider": _defs.get("provider"),
           # ts = epoch (sortable); ts_iso = local-time human stamp to eyeball-correlate to the archived
           # sage_<YYYYMMDD-HHMM>.db / phoenix_<...>.db moved at the NEXT reset (which holds THIS run's data).
           "ts": _now, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_now)),
           "card": asdict(card)}
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
