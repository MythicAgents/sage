"""Live driver: bare-model vs harness on GOAD, scored by the gauge.

⚠️  RUNS LIVE OFFENSIVE TOOLING against GOAD with `--go` (the OPERATOR's row, on a freshly-reset lab).
    Safe to import / `--help` / dry-run; only acts with `--go`.

The bare model uses Sage's OWN configured model (from sage-callback-bootstrap/.env) — provider=OpenAI,
model=gpt-5.5-cyber-preview, local endpoint — so it's a fair same-model comparison. No --model needed.
It gets Apollo's real command catalog (live-queried) as its toolset. Ground truth comes from BloodHound
(GRAPH_COLLECTED via REST domains; DA/OBJECTIVE via read-only cypher).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from . import live_seams, bare_runner, probes as probes_mod
    from .live_runner import LiveRunner, LiveConfig
    from .scenarios import goad_scenarios, CHILD, OBJECTIVE
    from .range_state import Milestone
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import live_seams, bare_runner, probes as probes_mod  # type: ignore
    from live_runner import LiveRunner, LiveConfig  # type: ignore
    from scenarios import goad_scenarios, CHILD, OBJECTIVE  # type: ignore
    from range_state import Milestone  # type: ignore


@dataclass
class Config:
    sage_cb: int = 1                 # live Sage callback (sage_task.py callbacks)
    apollo_cb: int = 4               # live Apollo callback the bare model acts through (cb2 was dead)
    engagement_op: str = "Operation_Chimera_1"
    seeds: int = 3
    max_steps: int = 40


def build_bare_runner(cfg: Config) -> bare_runner.BareModelRunner:
    d = live_seams.load_sage_defaults()                       # same model as Sage; no --model
    model_fn = live_seams.make_model_fn(d["provider"], d["model"], api_key=d["api_key"], base_url=d["base_url"])
    client = live_seams.default_mythic_client()
    tool_executor = live_seams.make_tool_executor(client, cfg.apollo_cb)
    tools = live_seams.apollo_tools_spec()                    # Apollo's real command catalog
    return bare_runner.BareModelRunner(model_fn, tool_executor, tools_spec=tools, max_steps=cfg.max_steps)


def build_probes() -> dict:
    """Ledger-independent ground truth from BloodHound. GRAPH_COLLECTED via REST; DA/OBJECTIVE via cypher.
    (krbtgt/creds milestones live in Mythic loot, not the graph — a separate probe source, TODO.)"""
    return {
        Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe(),
        Milestone.DA_CHILD: probes_mod.domain_admin_probe(live_seams.bloodhound_cypher, CHILD),
        Milestone.OBJECTIVE: probes_mod.domain_admin_probe(live_seams.bloodhound_cypher, OBJECTIVE),
    }


def run(cfg: Config) -> list:
    scenarios = goad_scenarios(cfg.engagement_op)
    probes = build_probes()
    harness = LiveRunner(sage_cb=cfg.sage_cb)
    harness_fn = harness.as_run_fn(cfg.seeds)
    bare = build_bare_runner(cfg)

    results = []
    for scn in scenarios:
        harness_cards = [harness_fn(LiveConfig("harness"), scn, s) for s in range(cfg.seeds)]
        bare_cards = [bare_runner.score_bare_run(bare.run(scn.name), scn, probes) for _ in range(cfg.seeds)]
        cmp = bare_runner.compare_bare_vs_harness(scn.name, bare_cards, harness_cards)
        print(f"[{scn.name}] verdict={cmp.verdict} delta={cmp.delta:+.3f} "
              f"(harness={cmp.harness_capability:.3f} bare={cmp.bare_capability:.3f} mde={cmp.min_detectable_effect:.3f})",
              flush=True)
        results.append(cmp)
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Live bare-vs-harness gauge run (GOAD).")
    p.add_argument("--go", action="store_true", help="actually run live offensive tooling against GOAD")
    p.add_argument("--sage-cb", type=int, default=None)
    p.add_argument("--apollo-cb", type=int, default=None)
    p.add_argument("--seeds", type=int, default=None)
    args = p.parse_args(argv)

    cfg = Config()
    if args.sage_cb is not None: cfg.sage_cb = args.sage_cb
    if args.apollo_cb is not None: cfg.apollo_cb = args.apollo_cb
    if args.seeds is not None: cfg.seeds = args.seeds

    if not args.go:
        d = live_seams.load_sage_defaults()
        print("DRY RUN (no --go). Would compare bare-vs-harness on GOAD with:")
        print(f"  sage_cb={cfg.sage_cb} apollo_cb={cfg.apollo_cb} seeds={cfg.seeds}")
        print(f"  bare model (from Sage .env): provider={d['provider']} model={d['model']} endpoint={d['base_url']}")
        print(f"  apollo tools: {len(live_seams.apollo_tools_spec())} commands")
        print(f"  scenarios: {[s.name for s in goad_scenarios(cfg.engagement_op)]}")
        print(f"  probes: GRAPH_COLLECTED, DA_CHILD, OBJECTIVE | BloodHound domains now: {live_seams.bloodhound_domain_count()}")
        print("Re-run with --go on a freshly-reset lab to execute. This issues real attacks.")
        return 0

    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
