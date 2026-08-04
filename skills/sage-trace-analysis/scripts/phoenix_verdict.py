#!/usr/bin/env python3
"""Re-derive the Phoenix tracing verdict for a run, without reconstructing anyone's session.

Answers the question that took a day to answer the hard way: **is the autonomous path actually traced,
and is the trace structured?** A span count alone cannot tell you. The signature of the untraced kernel
was 13 spans that all looked fine individually — every one a root, with nothing above them.

Three things decide the verdict:

* **kernel spans present** — `sage.kernel.*` from the deterministic controller. Zero means the
  autonomous path emitted nothing of its own and you are looking at incidental LangChain capture.
* **tool spans parented** — a parentless tool span means it ran outside any traced step.
* **one root per run** — many roots means many disconnected traces, and no per-run total is derivable.

Reads WAL-inclusively by copying `phoenix.db`, `-wal` and `-shm` to a temp dir first. Reading the `.db`
alone misses everything not yet checkpointed, which is how roughly half the archived databases in this
repo appear empty.

Usage:
    python3 skills/sage-trace-analysis/scripts/phoenix_verdict.py
    python3 skills/sage-trace-analysis/scripts/phoenix_verdict.py --since '2026-08-03 14:09:18'
    python3 skills/sage-trace-analysis/scripts/phoenix_verdict.py --latest-episode --json
    python3 skills/sage-trace-analysis/scripts/phoenix_verdict.py --db path/to/archived.db

Exit status is 0 when the verdict is TRACED, 1 when it is UNTRACED or PARTIAL, so it can gate a check.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = REPO_ROOT / "Payload_Type" / "sage" / ".phoenix" / "phoenix.db"
KERNEL_PREFIX = "sage.kernel."


def _open_wal_inclusive(db_path: Path, workdir: Path) -> sqlite3.Connection:
    """Copy the database and its sidecars so uncheckpointed spans are visible."""
    if not db_path.is_file():
        raise SystemExit(f"no phoenix database at {db_path}")
    target = workdir / db_path.name
    shutil.copy2(db_path, target)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, workdir / sidecar.name)
    return sqlite3.connect(target)


def _scope(conn: sqlite3.Connection, since: str | None, latest_episode: bool) -> tuple[str, list]:
    if latest_episode:
        row = conn.execute(
            "SELECT trace_rowid FROM spans WHERE name = ? ORDER BY start_time DESC LIMIT 1",
            (KERNEL_PREFIX + "episode",),
        ).fetchone()
        if row is None:
            raise SystemExit("no sage.kernel.episode span found; is this database from an autonomous run?")
        return "trace_rowid = ?", [row[0]]
    if since:
        return "start_time > ?", [since]
    return "1=1", []


def collect(conn: sqlite3.Connection, where: str, params: list) -> dict:
    rows = conn.execute(
        f"SELECT name, span_kind, COUNT(*), SUM(parent_id IS NULL) FROM spans WHERE {where} "
        "GROUP BY 1, 2 ORDER BY 3 DESC",
        params,
    ).fetchall()
    total = sum(r[2] for r in rows)
    roots = sum(r[3] or 0 for r in rows)
    kernel = sum(r[2] for r in rows if r[0].startswith(KERNEL_PREFIX))
    tool_rows = [r for r in rows if r[1] == "TOOL"]
    tool_total = sum(r[2] for r in tool_rows)
    tool_roots = sum(r[3] or 0 for r in tool_rows)
    tokens = conn.execute(
        f"SELECT COALESCE(SUM(llm_token_count_prompt), 0), COALESCE(SUM(llm_token_count_completion), 0) "
        f"FROM spans WHERE {where}",
        params,
    ).fetchone()

    if total == 0:
        verdict = "EMPTY"
    elif kernel == 0:
        verdict = "UNTRACED"
    elif tool_total and tool_roots:
        verdict = "PARTIAL"
    else:
        verdict = "TRACED"

    return {
        "verdict": verdict,
        "spans": total,
        "roots": roots,
        "kernel_spans": kernel,
        "tool_spans": tool_total,
        "tool_spans_parentless": tool_roots,
        "prompt_tokens": tokens[0],
        "completion_tokens": tokens[1],
        "by_name": [
            {"name": n, "kind": k, "count": c, "roots": r or 0} for n, k, c, r in rows
        ],
    }


def render(report: dict) -> str:
    lines = [
        f"verdict            {report['verdict']}",
        f"spans / roots      {report['spans']} / {report['roots']}",
        f"kernel spans       {report['kernel_spans']}",
        f"tool spans         {report['tool_spans']} ({report['tool_spans_parentless']} parentless)",
        f"tokens             prompt={report['prompt_tokens']} completion={report['completion_tokens']}",
        "",
        "  count  roots  kind    name",
    ]
    for row in report["by_name"]:
        lines.append(f"  {row['count']:5d}  {row['roots']:5d}  {row['kind']:6s}  {row['name']}")
    explanation = {
        "TRACED": "kernel spans present and every tool span has a parent",
        "PARTIAL": "kernel spans present but some tool spans are parentless",
        "UNTRACED": "no sage.kernel.* spans: the autonomous path emitted nothing of its own",
        "EMPTY": "no spans in scope (check --since, or whether the run wrote to this database)",
    }[report["verdict"]]
    lines += ["", explanation]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="phoenix database (default: the live one)")
    parser.add_argument("--since", help="only spans with start_time greater than this UTC 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("--latest-episode", action="store_true", help="scope to the most recent kernel episode")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        conn = _open_wal_inclusive(args.db, Path(tmp))
        where, params = _scope(conn, args.since, args.latest_episode)
        report = collect(conn, where, params)

    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["verdict"] == "TRACED" else 1


if __name__ == "__main__":
    sys.exit(main())
