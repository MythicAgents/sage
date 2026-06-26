"""C3 — verifier reliability / noise floor for the Sage eval gauge (Phase 0).

"One run is an anecdote." Run ONE fixed config N times and measure how much the gauge's
scores wander when nothing changed. That spread is the noise floor, and it sets the
`min_detectable_effect` (MDE): any later "config A > config B" claim must exceed it, or
the difference is just noise.

Two layers:
  * `noise_floor(scorecards)` — PURE statistics over N already-computed C2 ScoreCards of the
    same config. Unit-testable; no live execution.
  * `measure(run_fn, repeats)` — thin orchestration: calls an INJECTED `run_fn(seed)->ScoreCard`
    N times, then `noise_floor`. The runner is injected so this stays hermetic (tests pass a
    fake; the CLI passes a harness-backed runner). C3 never imports the live harness.

It also test-retests the VERIFIER itself: `label_agreement` per milestone = how often the N
repeats agreed on that milestone's boolean. < 1.0 means the gauge (not just the agent) is
non-deterministic on that milestone — a different, more alarming kind of noise.

Refuses to mix `verifier_hash`es: comparing across gauge versions is apples-to-oranges (ISC-9).
Reliability records append to a gitignored results dir (ISC-15).
"""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable, Sequence

try:  # package import
    from .fitness import ScoreCard
except Exception:  # script / sys.path import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fitness import ScoreCard  # type: ignore


def default_results_dir() -> Path:
    """`<sage>/.hillclimb/results` — gitignored runtime artifacts (never committed)."""
    return Path(__file__).resolve().parents[2] / ".hillclimb" / "results"


def default_results_path() -> Path:
    return default_results_dir() / "bare_vs_harness.jsonl"


def _scorecard_from_dict(card: dict) -> "ScoreCard | None":
    """Rebuild a ScoreCard from a recorded jsonl `card` object, tolerating schema drift (older records may
    lack newer fields — those fall back to dataclass defaults). Returns None on a malformed record."""
    try:
        names = {f.name for f in fields(ScoreCard)}
        return ScoreCard(**{k: v for k, v in card.items() if k in names})
    except Exception:
        return None


def noise_floor_from_results(
    results_path: str | os.PathLike | None = None,
    *,
    scenario: str,
    side: str = "harness",
    n: int | None = None,
    mde_sigma: float = 2.0,
) -> ReliabilityReport:
    """Compute the noise floor from ALREADY-RECORDED gauge runs — no lab. Reads the bare-vs-harness jsonl,
    keeps `side`/`scenario` records sharing the LATEST `verifier_hash` (results only comparable within one
    gauge version), rebuilds ScoreCards, and runs `noise_floor` over the last `n` (default: all). This turns
    the seeds an `orchestrate.py --seeds N` run already produced into a noise floor + MDE."""
    path = Path(results_path) if results_path is not None else default_results_path()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    recs = [
        r for r in rows
        if r.get("side") == side and r.get("scenario") == scenario and isinstance(r.get("card"), dict)
    ]
    cards = [c for c in (_scorecard_from_dict(r["card"]) for r in recs) if c is not None]
    if not cards:
        raise ValueError(f"no usable {side}/{scenario} ScoreCards in {path}")
    latest_hash = cards[-1].verifier_hash  # only compare within the current gauge version
    cards = [c for c in cards if c.verifier_hash == latest_hash]
    if n:
        cards = cards[-int(n):]
    return noise_floor(cards, mde_sigma=mde_sigma)


@dataclass
class ReliabilityReport:
    verifier_hash: str
    scenario: str
    repeats: int
    capability_mean: float
    capability_stdev: float
    capability_min: float
    capability_max: float
    # The threshold a later (candidate - best) capability difference must EXCEED to be real (ISC-14).
    # Heuristic: mde_sigma * stdev. Phase 1's acceptor refines this with bootstrap CIs over paired seeds.
    min_detectable_effect: float
    # Per-milestone test-retest agreement over the repeats; 1.0 = perfectly stable, 0.5 = coin-flip.
    label_agreement: dict[str, float] = field(default_factory=dict)
    least_stable_milestone: str | None = None
    least_stable_agreement: float = 1.0
    mde_sigma: float = 2.0


def noise_floor(scorecards: Sequence[ScoreCard], *, mde_sigma: float = 2.0) -> ReliabilityReport:
    """Pure noise-floor statistics over N ScoreCards of the SAME config (same verifier_hash)."""
    cards = list(scorecards)
    if not cards:
        raise ValueError("noise_floor needs at least one ScoreCard")
    hashes = {c.verifier_hash for c in cards}
    if len(hashes) != 1:
        raise ValueError(
            f"noise_floor requires a single verifier_hash; got {sorted(hashes)} — "
            "cannot mix gauge versions (results are only comparable within one hash)"
        )

    n = len(cards)
    caps = [float(c.capability) for c in cards]
    stdev = statistics.stdev(caps) if n > 1 else 0.0

    # Union of milestone keys so card ordering / partial scenarios can't drop a milestone.
    keys: list[str] = []
    seen: set[str] = set()
    for c in cards:
        for k in c.milestones:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    agreement: dict[str, float] = {}
    for k in keys:
        trues = sum(1 for c in cards if c.milestones.get(k))
        agreement[k] = max(trues, n - trues) / n  # missing key -> falsy -> counted as False

    least = min(agreement, key=agreement.get) if agreement else None
    return ReliabilityReport(
        verifier_hash=next(iter(hashes)),
        scenario=cards[0].scenario,
        repeats=n,
        capability_mean=statistics.mean(caps),
        capability_stdev=stdev,
        capability_min=min(caps),
        capability_max=max(caps),
        min_detectable_effect=mde_sigma * stdev,
        label_agreement=agreement,
        least_stable_milestone=least,
        least_stable_agreement=(agreement[least] if least is not None else 1.0),
        mde_sigma=mde_sigma,
    )


def measure(
    run_fn: Callable[[int], ScoreCard],
    repeats: int = 5,
    *,
    mde_sigma: float = 2.0,
) -> ReliabilityReport:
    """Run a fixed config `repeats` times via the injected `run_fn(seed) -> ScoreCard`, then
    compute the noise floor. `run_fn` is injected so this stays hermetic and testable."""
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    cards = [run_fn(seed) for seed in range(repeats)]
    return noise_floor(cards, mde_sigma=mde_sigma)


def write_reliability_record(
    report: ReliabilityReport,
    *,
    results_dir: str | os.PathLike | None = None,
) -> str:
    """Append the report as a JSONL line keyed by verifier_hash (ISC-15). Returns the path."""
    directory = Path(results_dir) if results_dir is not None else default_results_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "reliability.jsonl"
    record = {"kind": "reliability", **asdict(report)}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return str(path)
