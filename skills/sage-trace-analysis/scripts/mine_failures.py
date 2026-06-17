#!/usr/bin/env python3
"""Throwaway (gitignored) — mine ALL Phoenix issue_task tool spans → per-command failure table.

Answers: across every run, which Mythic command invocations fail, with what failure CLASS, so we can
size the deterministic command-construction layer against real data instead of running more solves.
"""
import re
import sqlite3
import sys
from collections import defaultdict
from urllib.parse import quote

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals import phoenix_reader as pr  # noqa: E402

DB = "/home/john/dev/sage/Payload_Type/sage/.phoenix/phoenix.db"

# Failure CLASSES, checked against the tool OUTPUT text (first match wins, order = priority).
CLASSES = [
    ("param_format", re.compile(r"invalid parameters|unknown key|valid parameters are group|required parameter|missing required|parameter_group|ChooseOne|not a valid", re.I)),
    ("failed_create", re.compile(r"failed to create task|error issuing command", re.I)),
    ("empty_param", re.compile(r"takes no command line arguments|no command line arg", re.I)),
    ("targeting_dn", re.compile(r"object not found|bad_dn|0x20f7|getncchanges|does not exist|cannot find|no such object|0x2105|unknown user|target.*not found", re.I)),
    ("access_denied", re.compile(r"access denied|access is denied|0x80070005|status_access_denied|insufficient|not authorized|0x2105", re.I)),
    ("not_registered", re.compile(r"not registered|no file|file not found|not been uploaded|ensure_tool_uploaded|register", re.I)),
    ("os_payload_dud", re.compile(r"127\.0\.0\.1|null uuid|build_phase.*error|placeholder|operating_system", re.I)),
    ("timeout", re.compile(r"timed out|timeout waiting|no output returned|poll", re.I)),
]
ERR_HINT = re.compile(r"error|fail|invalid|denied|exception|cannot|unable|not found|unknown|0x[0-9a-f]{4}", re.I)


def classify(out: str) -> str:
    if not out:
        return "no_output"
    for name, rx in CLASSES:
        if rx.search(out):
            return name
    if ERR_HINT.search(out[:4000]):
        return "other_error"
    return "ok"


def main():
    uri = f"file:{quote(DB, safe='/:')}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    rows = con.execute(
        "SELECT attributes, COALESCE(status_message,'') FROM spans "
        "WHERE name LIKE 'issue_task_and_waitfor_task_output%'"
    ).fetchall()
    print(f"issue_task tool spans total: {len(rows)}")

    per_cmd = defaultdict(lambda: defaultdict(int))   # cmd -> class -> n
    examples = {}                                      # (cmd,class) -> example snippet
    for attrs, _sm in rows:
        attrs = attrs or ""
        cmds = pr._commands_from_attributes(attrs) or ["<unknown>"]
        out = pr._tool_output_text(attrs) or ""
        cls = classify(out)
        cmd = cmds[0]
        per_cmd[cmd][cls] += 1
        key = (cmd, cls)
        if cls not in ("ok", "no_output") and key not in examples:
            snip = re.sub(r"\s+", " ", out[:240]).strip()
            examples[key] = snip

    # recursion / step-cap deaths (span-level errors)
    rec = con.execute("SELECT COUNT(*) FROM spans WHERE status_code='ERROR' AND LOWER(COALESCE(status_message,'')) LIKE '%recursion%'").fetchone()[0]
    errspans = con.execute("SELECT COUNT(*) FROM spans WHERE status_code='ERROR'").fetchone()[0]

    # aggregate
    fail_classes = [c for c, _ in CLASSES] + ["other_error", "no_output"]
    totals = defaultdict(int)
    print("\n=== PER-COMMAND FAILURE TABLE (commands with >=1 failure, by total calls) ===")
    print(f"{'command':<22}{'calls':>6}{'ok':>6}{'FAIL':>6}  fail-breakdown")
    ranked = sorted(per_cmd.items(), key=lambda kv: -sum(kv[1].values()))
    for cmd, d in ranked:
        calls = sum(d.values())
        ok = d.get("ok", 0)
        fails = calls - ok
        if fails == 0:
            continue
        bd = ", ".join(f"{c}:{d[c]}" for c in fail_classes if d.get(c))
        print(f"{cmd[:22]:<22}{calls:>6}{ok:>6}{fails:>6}  {bd}")
        for c in fail_classes:
            totals[c] += d.get(c, 0)

    print("\n=== FAILURE-CLASS TOTALS (across all commands) ===")
    grand = sum(totals.values())
    for c in sorted(totals, key=lambda k: -totals[k]):
        print(f"  {c:<16}{totals[c]:>6}  ({100*totals[c]/max(grand,1):.0f}%)")
    print(f"  {'TOTAL FAILS':<16}{grand:>6}")
    print(f"\nspan-level ERROR spans: {errspans} | recursion-death errors: {rec}")

    print("\n=== EXAMPLE ERROR SNIPPETS (top classes) ===")
    shown = defaultdict(int)
    for (cmd, cls), snip in examples.items():
        if cls in ("param_format", "failed_create", "targeting_dn", "empty_param", "access_denied") and shown[cls] < 2:
            shown[cls] += 1
            print(f"  [{cls}] {cmd}: {snip[:180]}")


if __name__ == "__main__":
    main()
