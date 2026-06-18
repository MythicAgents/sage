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
    from . import live_seams, bare_runner, probes as probes_mod
    from .scenarios import goad_scenarios, CHILD, OBJECTIVE
    from .range_state import Milestone
    from .fitness import ScoreCard
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import live_seams, bare_runner, probes as probes_mod  # type: ignore
    from scenarios import goad_scenarios, CHILD, OBJECTIVE  # type: ignore
    from range_state import Milestone  # type: ignore
    from fitness import ScoreCard  # type: ignore


@dataclass
class Config:
    sage_cb: int = 1
    apollo_cb: int = 4
    engagement_op: str = "Operation_Chimera_1"
    max_steps: int = 40
    solve_timeout: int = 1800

    @property
    def results_path(self) -> Path:
        return Path(__file__).resolve().parents[2] / ".hillclimb" / "results" / "bare_vs_harness.jsonl"


def build_probes(tasker, baseline: dict) -> dict:
    """All collection-independent. KRBTGT via Mythic loot; DA/OBJECTIVE via AD-DIRECT live DC membership
    (escalation vs the post-reset baseline). krbtgt-loot also covers DA achieved without a membership add."""
    return {
        Milestone.KRBTGT_DUMPED: live_seams.mythic_credential_probe("krbtgt"),
        Milestone.DA_CHILD: live_seams.ad_domain_admins_probe(tasker, CHILD, baseline=baseline.get(CHILD, set())),
        Milestone.OBJECTIVE: live_seams.ad_domain_admins_probe(tasker, OBJECTIVE, baseline=baseline.get(OBJECTIVE, set())),
    }


def _scenario(cfg: Config, name: str):
    for s in goad_scenarios(cfg.engagement_op):
        if s.name == name:
            return s
    raise SystemExit(f"unknown scenario {name!r}; choices: {[s.name for s in goad_scenarios(cfg.engagement_op)]}")


def run_side(cfg: Config, side: str, scenario_name: str) -> ScoreCard:
    """Run ONE side on ONE scenario (range assumed freshly reset), score via the shared probes, record."""
    scn = _scenario(cfg, scenario_name)
    client = live_seams.default_mythic_client()
    tasker = live_seams.make_tool_executor(client, cfg.apollo_cb)
    # AD-direct baseline (post-reset, pre-agent): lets the DA/OBJECTIVE probe detect escalation vs default.
    baseline = {CHILD: live_seams.ad_domain_admins(tasker, CHILD),
                OBJECTIVE: live_seams.ad_domain_admins(tasker, OBJECTIVE)}
    probes = build_probes(tasker, baseline)
    if side == "harness":
        solve = live_seams.make_harness_solver(client, cfg.sage_cb, timeout=cfg.solve_timeout, max_steps=0)
        solve(scn.objective)                                   # full autonomous Sage solve
        card = bare_runner.score_from_probes(scn, probes, status="done")
    elif side == "bare":
        bare = build_bare_runner(cfg, tasker=tasker)
        result = bare.run(scn.objective)
        card = bare_runner.score_bare_run(result, scn, probes)
    else:
        raise SystemExit("--side must be harness|bare")

    rec = {"side": side, "scenario": scenario_name, "ts": time.time(), "card": asdict(card)}
    p = cfg.results_path
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    print(f"[{side}/{scenario_name}] capability={card.capability:.3f} furthest={card.furthest_milestone} "
          f"-> recorded to {p}", flush=True)
    return card


def build_bare_runner(cfg: Config, tasker=None) -> bare_runner.BareModelRunner:
    d = live_seams.load_sage_defaults()
    model_fn = live_seams.make_model_fn(d["provider"], d["model"], api_key=d["api_key"], base_url=d["base_url"])
    if tasker is None:
        client = live_seams.default_mythic_client()
        tasker = live_seams.make_tool_executor(client, cfg.apollo_cb)
    return bare_runner.BareModelRunner(model_fn, tasker,
                                       tools_spec=live_seams.apollo_tools_spec(), max_steps=cfg.max_steps)


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
    c = sub.add_parser("compare", help="combine recorded ScoreCards for a scenario into a verdict")
    c.add_argument("--scenario", required=True)
    args = ap.parse_args(argv)

    cfg = Config()
    if getattr(args, "sage_cb", None) is not None:
        cfg.sage_cb = args.sage_cb
    if getattr(args, "apollo_cb", None) is not None:
        cfg.apollo_cb = args.apollo_cb
    if getattr(args, "solve_timeout", None) is not None:
        cfg.solve_timeout = args.solve_timeout

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

    run_side(cfg, args.side, args.scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
