"""Sealed, hash-chained kernel decision record (ISC-49R criterion 49R-16).

EVALUATOR EVIDENCE ONLY. This module serialises the *existing* `RequestEventLedger` at request
terminalisation so an independent reader can prove the kernel mediated a run. It is a pure read over a
ledger the kernel has already finished writing: it never records, mutates, reorders, or suppresses an
event, and every failure path is swallowed and logged rather than raised. Fail-safe for behaviour;
fail-closed for evidence, because a missing record is exactly 49R-16's falsifier and the attester reports
it as a FAIL.

Shape deliberately mirrors the accepted `engagement_state._trace_rights_decision` emitter: same
`.sage_engagement` home, same never-affects-behavior contract, same `PYTEST_CURRENT_TEST` guard so test
runs cannot pollute the live artifact.

Why a chain and not just the per-event digest: `RequestEventLedger._append` already binds each event's
`record_id` to its content *and* its sequence, which detects edits in place. It cannot detect a deleted,
reordered, or truncated tail. Chaining each record into the next makes those visible too.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = "sage-kernel-decision-record-v1"
RECORD_SUBDIR = "decision_records"
# Set to bypass the pytest guard when a test deliberately exercises the active emitter. Pair it with
# SAGE_ENGAGEMENT_STATE_DIR so the write lands in a temp dir, never the live engagement state.
TEST_OVERRIDE_ENV = "SAGE_DECISION_RECORD_ALLOW_TEST"


def _state_dir() -> str:
    try:
        try:
            from . import engagement_ledger as _el
        except ImportError:  # ai/langgraph on sys.path directly (tests, some runtimes)
            import engagement_ledger as _el  # type: ignore[no-redef]
        return _el.state_dir()
    except Exception:
        return os.path.join(os.getcwd(), ".sage_engagement")


def record_dir() -> str:
    return os.path.join(_state_dir(), RECORD_SUBDIR)


def _safe_name(request_id: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in request_id)[:200]


def record_paths(request_id: str) -> tuple[str, str]:
    """(record_path, seal_path) for a request id. Pure; creates nothing."""
    base = os.path.join(record_dir(), _safe_name(request_id))
    return f"{base}.jsonl", f"{base}.seal.json"


def _chain(previous: str, record_id: str, request_id: str) -> str:
    seed = previous or request_id
    return hashlib.sha256(f"{seed}\0{record_id}".encode("utf-8")).hexdigest()


def _terminal_state(events: tuple[Any, ...]) -> str:
    """Terminal status from the ledger itself, so the caller needs to supply nothing."""
    for event in reversed(events):
        if getattr(event, "kind", "") == "control_transition" and getattr(event, "phase", "") == "request_terminal":
            return str(getattr(event, "content", "") or "")
    return ""


def build_lines(events: tuple[Any, ...], request_id: str) -> tuple[list[str], str]:
    """Render ledger events as chained JSONL lines. Returns (lines, terminal_chain)."""
    lines: list[str] = []
    chain = ""
    for seq, event in enumerate(events):
        payload = event.to_dict()
        chain = _chain(chain, str(payload.get("record_id") or ""), request_id)
        lines.append(json.dumps({"seq": seq, "chain": chain, "event": payload}, sort_keys=True))
    return lines, chain


def seal_request_decision_record(ledger: Any, *, summary: Any = None) -> str | None:
    """Write and seal the decision record for a terminated request. Never raises.

    Idempotent: a request whose seal already exists is left untouched, which matters because the success
    path calls `finalize_visibility_turn(require_final=True)` twice (service.py:780 and :793).
    """
    try:
        if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(TEST_OVERRIDE_ENV):
            return None
        request_id = str(getattr(ledger, "request_id", "") or "")
        if not request_id:
            return None
        events = tuple(getattr(ledger, "events", ()) or ())
        record_path, seal_path = record_paths(request_id)
        if os.path.exists(seal_path):
            return record_path
        os.makedirs(record_dir(), exist_ok=True)

        lines, terminal_chain = build_lines(events, request_id)
        blob = "".join(f"{line}\n" for line in lines)
        seal = {
            "schema": SCHEMA,
            "request_id": request_id,
            "event_count": len(lines),
            "terminal_state": _terminal_state(events),
            "terminal_chain": terminal_chain,
            "record_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            "sealed_at": datetime.now(timezone.utc).isoformat(),
        }
        if isinstance(summary, dict):
            seal["reconcile_ok"] = bool(summary.get("ok"))

        _atomic_write(record_path, blob)
        _atomic_write(seal_path, json.dumps(seal, indent=2, sort_keys=True) + "\n")
        for path in (record_path, seal_path):
            try:
                os.chmod(path, 0o444)
            except OSError:
                pass
        return record_path
    except Exception as exc:  # never propagate into the request path
        logger.warning("decision record seal failed (evidence only, request unaffected): %s", exc)
        return None


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def verify_decision_record(record_path: str, seal_path: str) -> dict[str, Any]:
    """Recompute the chain and compare against the seal. Independent of Sage runtime state.

    This is what makes the record re-verifiable by a third party (49R-13).
    """
    result: dict[str, Any] = {"ok": False, "errors": [], "event_count": 0}
    try:
        with open(seal_path, encoding="utf-8") as handle:
            seal = json.load(handle)
        with open(record_path, encoding="utf-8") as handle:
            blob = handle.read()
    except (OSError, ValueError) as exc:
        result["errors"].append(f"unreadable record or seal: {exc}")
        return result

    if seal.get("schema") != SCHEMA:
        result["errors"].append(f"unexpected schema: {seal.get('schema')!r}")
    if hashlib.sha256(blob.encode("utf-8")).hexdigest() != seal.get("record_sha256"):
        result["errors"].append("record sha256 does not match seal")

    request_id = str(seal.get("request_id") or "")
    chain = ""
    rows = [line for line in blob.splitlines() if line.strip()]
    for index, line in enumerate(rows):
        try:
            row = json.loads(line)
        except ValueError:
            result["errors"].append(f"line {index} is not valid JSON")
            break
        if row.get("seq") != index:
            result["errors"].append(f"line {index} has out-of-order seq {row.get('seq')!r}")
        chain = _chain(chain, str((row.get("event") or {}).get("record_id") or ""), request_id)
        if row.get("chain") != chain:
            result["errors"].append(f"chain break at line {index}")
            break

    if rows and chain != seal.get("terminal_chain"):
        result["errors"].append("terminal chain does not match seal")
    if seal.get("event_count") != len(rows):
        result["errors"].append(
            f"event_count {seal.get('event_count')!r} does not match {len(rows)} rows"
        )

    result["event_count"] = len(rows)
    result["request_id"] = request_id
    result["terminal_state"] = seal.get("terminal_state", "")
    result["ok"] = not result["errors"]
    return result
