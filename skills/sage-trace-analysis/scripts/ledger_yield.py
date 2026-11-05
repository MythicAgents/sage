#!/usr/bin/env python3
"""Engagement-ledger yield for a retained Sage run: what the ledger recorded against the Mythic tasks that ran.

The ledger records proven progress (capability gains such as a new session, a rise in integrity, or a captured
credential), not one row per task. This reports, per request window, the Mythic tasks issued, the ledger rows linked
to those tasks, rows by status, and achieved rows carrying a runtime proof envelope, so a run can be compared before
and after a ledger change. A persisted `achieved` row was admitted by Sage's proof boundary when it was written and
again when the ledger was loaded; the script reads the file as it is and never re-admits proofs.

Inputs are a ledger JSON — a historical `.sage_engagement/state_*.json` or an ESL export written by
`skills/sage-goad-reset/scripts/engagement_ledger_migrate.py export` — and a Mythic export with the request windows
and task identities (`{"requests": [{id, created_at, completed_at}], "tasks": [{display_id, command_name, status,
timestamp}]}`, UTC). Task parameters, task output and credential values are never read. An export's retracted or
"no longer holds" rows are counted separately and never as proven current rows.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

RUNTIME_ORIGINS = {"mythic_task", "mythic_artifact", "mythic_credential", "bloodhound_ingest", "mythic_callback"}
METHODS = {
    "request_tasks": "export tasks whose timestamp falls inside the request window",
    "task_linked_rows": "ledger rows whose evidence mythic_task_id is one of the request's tasks",
    "proven_rows": "task-linked rows with status achieved and a proof envelope of a runtime origin",
    "untimed_rows": "ledger rows citing no task in any request window (graph facts are not rows)",
    "corrected_rows": "rows an export marks operator-retracted or no longer holding (history, not current proof)",
}


def _when(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _task_id(value: Any) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() else None


def _corrected(hop: dict[str, Any]) -> bool:
    corrections = hop.get("corrections") if isinstance(hop.get("corrections"), dict) else {}
    marker = corrections.get("no_longer_holds") if isinstance(corrections.get("no_longer_holds"), dict) else {}
    return bool(corrections.get("retracted")) or bool(marker.get("effective"))


def _proven(hop: dict[str, Any]) -> bool:
    proof = hop.get("proof_envelope") or (hop.get("evidence") or {}).get("proof_envelope") or {}
    return str(hop.get("status") or "") == "achieved" and isinstance(proof, dict) \
        and proof.get("origin") in RUNTIME_ORIGINS and not _corrected(hop)


def build_report(ledger: dict[str, Any], export: dict[str, Any], requests: list[int] | None = None) -> dict[str, Any]:
    hops = [hop for hop in ledger.get("hops") or [] if isinstance(hop, dict)]
    by_task: dict[int, list[dict[str, Any]]] = {}
    for hop in hops:
        task_id = _task_id((hop.get("evidence") or {}).get("mythic_task_id"))
        if task_id is not None:
            by_task.setdefault(task_id, []).append(hop)
    tasks = [task for task in export.get("tasks") or [] if isinstance(task, dict)]
    linked_anywhere: set[int] = set()
    rows = []
    for request in export.get("requests") or []:
        if requests and request.get("id") not in requests:
            continue
        start, end = _when(request.get("created_at")), _when(request.get("completed_at"))
        in_window = [t for t in tasks if start and end and (moment := _when(t.get("timestamp"))) and start <= moment <= end]
        ids = {t["display_id"] for t in in_window if type(t.get("display_id")) is int}
        linked = [hop for task_id in sorted(ids) for hop in by_task.get(task_id, [])]
        linked_anywhere |= ids & set(by_task)
        rows.append({
            "request_id": request.get("id"),
            "tasks": len(ids),
            "task_commands": dict(Counter(str(t.get("command_name") or "") for t in in_window).most_common()),
            "task_linked_rows": len(linked),
            "tasks_with_a_row": len(ids & set(by_task)),
            "rows_by_status": dict(Counter(str(hop.get("status") or "") for hop in linked)),
            "proven_rows": sum(_proven(hop) for hop in linked),
            "corrected_rows": sum(_corrected(hop) for hop in linked),
            "rows_per_task": round(len(linked) / len(ids), 3) if ids else None,
        })
    return {
        "methods": METHODS,
        "ledger_rows": len(hops),
        "ledger_rows_by_status": dict(Counter(str(hop.get("status") or "") for hop in hops)),
        "untimed_rows": sum(1 for task_id in by_task if task_id not in linked_anywhere)
        + sum(1 for hop in hops if _task_id((hop.get("evidence") or {}).get("mythic_task_id")) is None),
        "next_hop_number": ledger.get("next_hop_number"),
        "corrected_rows": sum(_corrected(hop) for hop in hops),
        "requests": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Engagement-ledger yield",
        "",
        f"Ledger rows: {report['ledger_rows']} ({report['ledger_rows_by_status']}); rows citing no request task: "
        f"{report['untimed_rows']}; corrected rows: {report.get('corrected_rows', 0)}; "
        f"next hop number: {report['next_hop_number']}.",
        "",
        "| Request | Tasks | Task-linked rows | Tasks with a row | Proven rows | Rows by status |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["requests"]:
        lines.append(f"| {row['request_id']} | {row['tasks']} | {row['task_linked_rows']} | {row['tasks_with_a_row']} "
                     f"| {row['proven_rows']} | {row['rows_by_status'] or '—'} |")
    lines += ["", "Methods:", *(f"- `{key}`: {value}" for key, value in report["methods"].items())]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, required=True,
                        help="Historical ledger JSON or an ESL export (engagement_ledger_migrate.py export).")
    parser.add_argument("--mythic-export", type=Path, required=True, help="Mythic export with requests and tasks.")
    parser.add_argument("--request", type=int, action="append", help="Request id to include; repeatable.")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args(argv)
    report = build_report(json.loads(args.ledger.read_text(encoding="utf-8")),
                          json.loads(args.mythic_export.read_text(encoding="utf-8")), args.request)
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = render_markdown(report)
    if args.markdown_out:
        args.markdown_out.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
