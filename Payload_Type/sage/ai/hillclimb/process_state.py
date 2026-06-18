"""C1b — tradecraft / process layer for the Sage eval gauge (Phase 0).

The engagement ledger (C1, range_state.py) measures verified OUTCOMES. Sub-hop
tradecraft — sacrificial logon sessions, security-context setup, wrong-target
execution — lives below the milestone grain. C1b reads it from the trajectory
store (the runtime failure log), producing a SEPARATE, DIAGNOSTIC family of the
score vector.

Discipline (ISC-38): these signals are diagnostic/credit ONLY. They never feed
the acceptance reward — capability (C1 ground truth) is the sole acceptance gate.
Optimizing tradecraft metrics directly would reward good-looking tradecraft that
owns nothing (Goodhart).

Grounded scope, Phase 0:
  * Source = the trajectory transition store (`ai/trajectory`), read via
    `schema.load_jsonl`. The runtime bridge records a transition when
    `execute_capability` FAILS, so this store is a FAILURE LOG. That cleanly
    yields failure-class counts and `unclassified_rate` (the measured blind-spot
    size) — but NOT a total-action denominator.
  * Therefore the **productive-action ratio** (ISC-35) is NOT computed here: its
    denominator (all tool actions) needs the full action log (harness schema-v2 /
    Phoenix spans). It is computed in C2 as (C1 milestones) ÷ (harness tool-call
    count). See GAP_REGISTER.
  * Phoenix per-agent span reading is deferred (richer attribution, later phase).
  * READ-ONLY: `store_hash()` lets tests assert byte-identity.
"""
from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:  # package import
    from ..trajectory import schema as _traj_schema
except Exception:  # script / sys.path import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trajectory"))
    import schema as _traj_schema  # type: ignore


def default_store_path() -> Path:
    """Mirror of `trajectory.runtime.default_store_path()` without importing runtime
    (runtime uses package-relative imports that do not resolve under top-level import).
    `<sage>/.trajectory/transitions.jsonl`."""
    return Path(__file__).resolve().parents[2] / ".trajectory" / "transitions.jsonl"


# Known tradecraft dimensions the gauge does NOT yet measure. Making blind spots
# explicit (ISC-37) is the honest alternative to a gauge that silently can't see them.
GAP_REGISTER: tuple[str, ...] = (
    "wrong_execution_locus / wrong-target-host (e.g. `gpupdate /force` on the local host instead of "
    "the DC): no labeler signature today; the action succeeds and advances nothing, so it is invisible "
    "to both the ledger and the failure log. Needs a new label or a target-correctness check.",
    "productive-action ratio: denominator (all tool actions) is not in the failure-only trajectory store; "
    "computed in C2 from C1 milestones ÷ harness schema-v2 tool-call count.",
    "per-agent attribution: requires Phoenix span reading (which agent owned the failing step); deferred.",
    "successful-but-suboptimal tradecraft (noisy/OPSEC-poor paths that still work): a separate signal, "
    "not a failure label.",
)


def _store_path(store_path: str | os.PathLike | None = None) -> str:
    if store_path:
        return str(Path(store_path).expanduser())
    env = os.environ.get("SAGE_TRAJECTORY_STORE")
    if env:
        return str(Path(env).expanduser())
    return str(default_store_path())


def _label_of(record) -> str:
    label = str(getattr(record, "failure_label", "") or "").strip().casefold()
    return label or "unclassified"  # empty label is an unclassified transition


@dataclass
class ProcessSignals:
    """Diagnostic tradecraft signals for a run (or the whole store if run_id is None)."""
    run_id: str | None
    store_path: str
    total_transitions: int
    failure_class_counts: dict[str, int] = field(default_factory=dict)
    unclassified_count: int = 0
    unclassified_rate: float = 0.0  # gauge-HEALTH: fraction the labeler could not classify
    verifier_status_counts: dict[str, int] = field(default_factory=dict)
    gap_register: tuple[str, ...] = GAP_REGISTER


def read_process_signals(
    run_id: str | None = None,
    *,
    store_path: str | os.PathLike | None = None,
) -> ProcessSignals:
    """Read tradecraft signals from the trajectory failure log. READ-ONLY; never raises."""
    path = _store_path(store_path)
    if not os.path.isfile(path):
        return ProcessSignals(run_id=run_id, store_path=path, total_transitions=0)

    try:
        records = _traj_schema.load_jsonl(path)
    except Exception:
        # A corrupt store is itself a (zero-signal) result, not a crash — the gauge must be robust.
        return ProcessSignals(run_id=run_id, store_path=path, total_transitions=0)

    if run_id is not None:
        records = [r for r in records if str(getattr(r, "run_id", "")) == run_id]

    total = len(records)
    label_counts = Counter(_label_of(r) for r in records)
    status_counts = Counter(
        str(getattr(getattr(r, "verifier", None), "status", "") or "unknown").casefold()
        for r in records
    )
    unclassified = label_counts.get("unclassified", 0)

    return ProcessSignals(
        run_id=run_id,
        store_path=path,
        total_transitions=total,
        failure_class_counts=dict(label_counts),
        unclassified_count=unclassified,
        unclassified_rate=(unclassified / total) if total else 0.0,
        verifier_status_counts=dict(status_counts),
    )


def store_hash(store_path: str | os.PathLike | None = None) -> str:
    """SHA-256 over the trajectory store bytes; tests assert byte-identity pre/post (read-only guard)."""
    path = _store_path(store_path)
    h = hashlib.sha256()
    if os.path.isfile(path):
        h.update(path.encode("utf-8"))
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()
