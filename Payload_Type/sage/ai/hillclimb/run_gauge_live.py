"""Live driver: bare-model vs harness on GOAD, scored by the gauge.

⚠️  RUNS LIVE OFFENSIVE TOOLING against GOAD (the bare model attacks the range; the harness solves it).
    This is the OPERATOR's row to execute, on a freshly-reset lab. Safe to import / `--help` / inspect;
    it only acts with `--go`. Without `--go` it prints the plan + the CONFIG it needs.

Wiring (all grounded; Mythic path validated, BloodHound path validated):
  * harness side  -> live_runner.LiveRunner (subprocess to evals.harness)
  * bare side     -> bare_runner.BareModelRunner(model_fn=live_seams.make_model_fn,
                                                  tool_executor=live_seams.make_tool_executor)
  * ground truth  -> probes.read_ground_truth_from_probes with live_seams.graph_collected_probe
  * comparison    -> bare_runner.compare_bare_vs_harness (delta judged vs C3 noise floor)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from . import live_seams, bare_runner
    from .live_runner import LiveRunner, LiveConfig
    from .scenarios import goad_scenarios
    from .range_state import Milestone
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import live_seams, bare_runner  # type: ignore
    from live_runner import LiveRunner, LiveConfig  # type: ignore
    from scenarios import goad_scenarios  # type: ignore
    from range_state import Milestone  # type: ignore


@dataclass
class Config:
    # --- FILL IN to match your lab / Sage ---
    sage_cb: int = 1                 # live Sage callback (sage_task.py callbacks)
    apollo_cb: int = 4               # live Apollo callback the bare model acts through (cb2 was dead)
    provider: str = "anthropic"      # same provider Sage runs (Model.__init__)
    model: str = ""                  # FILL IN: same model id Sage runs
    engagement_op: str = "Operation_Chimera_1"   # ledger op key base (live runner pins a fresh id per run)
    seeds: int = 3
    max_steps: int = 40
    # tools_spec: Apollo commands the bare model may call. FILL IN your real Apollo command set.
    # Minimal example — a single passthrough; expand to the commands you want the baseline to have.
    apollo_tools: list = field(default_factory=lambda: [
        {"name": "shell", "description": "Run a shell command on the host", "args": {"command": "str"}},
        {"name": "whoami", "description": "Print current identity", "args": {}},
    ])


def build_bare_runner(cfg: Config) -> bare_runner.BareModelRunner:
    model_fn = live_seams.make_model_fn(cfg.provider, cfg.model)
    client = live_seams.default_mythic_client()
    tool_executor = live_seams.make_tool_executor(client, cfg.apollo_cb)
    return bare_runner.BareModelRunner(model_fn, tool_executor,
                                       tools_spec=cfg.apollo_tools, max_steps=cfg.max_steps)


def run(cfg: Config) -> list:
    """Execute the live comparison. Returns a BareVsHarness per scenario."""
    scenarios = goad_scenarios(cfg.engagement_op)
    probes = {Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe()}
    # NOTE: deeper milestones (DA, krbtgt, objective) need richer cypher probes via
    # live_seams.make_cypher_run(<bloodhound MCP tool>) — add them as you wire the MCP tool.

    harness = LiveRunner(sage_cb=cfg.sage_cb)
    harness_fn = harness.as_run_fn(cfg.seeds)
    bare = build_bare_runner(cfg)

    results = []
    for scn in scenarios:
        harness_cards = [harness_fn(LiveConfig("harness"), scn, s) for s in range(cfg.seeds)]
        bare_cards = [
            bare_runner.score_bare_run(bare.run(scn.name), scn, probes)
            for _ in range(cfg.seeds)
        ]
        cmp = bare_runner.compare_bare_vs_harness(scn.name, bare_cards, harness_cards)
        print(f"[{scn.name}] verdict={cmp.verdict} delta={cmp.delta:+.3f} "
              f"(harness={cmp.harness_capability:.3f} bare={cmp.bare_capability:.3f} mde={cmp.min_detectable_effect:.3f})",
              flush=True)
        results.append(cmp)
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Live bare-vs-harness gauge run (GOAD).")
    p.add_argument("--go", action="store_true", help="actually run live offensive tooling against GOAD")
    p.add_argument("--model", default=None, help="override the bare model id")
    p.add_argument("--sage-cb", type=int, default=None)
    p.add_argument("--apollo-cb", type=int, default=None)
    p.add_argument("--seeds", type=int, default=None)
    args = p.parse_args(argv)

    cfg = Config()
    if args.model: cfg.model = args.model
    if args.sage_cb is not None: cfg.sage_cb = args.sage_cb
    if args.apollo_cb is not None: cfg.apollo_cb = args.apollo_cb
    if args.seeds is not None: cfg.seeds = args.seeds

    if not args.go:
        print("DRY RUN (no --go). Would compare bare-vs-harness on GOAD with:")
        print(f"  sage_cb={cfg.sage_cb} apollo_cb={cfg.apollo_cb} provider={cfg.provider} "
              f"model={cfg.model or '<FILL IN --model>'} seeds={cfg.seeds}")
        print(f"  scenarios={[s.name for s in goad_scenarios(cfg.engagement_op)]}")
        print(f"  probes wired: GRAPH_COLLECTED (BloodHound). domains now: {live_seams.bloodhound_domain_count()}")
        print("Re-run with --go (and --model) on a freshly-reset lab to execute. This issues real attacks.")
        return 0

    if not cfg.model:
        print("ERROR: set --model (the model id Sage runs).", file=sys.stderr)
        return 2
    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
