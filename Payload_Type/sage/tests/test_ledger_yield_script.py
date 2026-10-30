import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-trace-analysis" / "scripts" / "ledger_yield.py"
SPEC = importlib.util.spec_from_file_location("ledger_yield", SCRIPT)
yield_script = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(yield_script)

EXPORT = {
    "requests": [{"id": 1, "created_at": "2026-10-30T00:00:00", "completed_at": "2026-10-30T00:30:00"},
                 {"id": 2, "created_at": "2026-10-30T01:00:00", "completed_at": "2026-10-30T01:30:00"}],
    "tasks": [{"display_id": 10, "command_name": "jump_psexec", "status": "success", "timestamp": "2026-10-30T00:05:00"},
              {"display_id": 11, "command_name": "powershell", "status": "success", "timestamp": "2026-10-30T00:06:00"},
              {"display_id": 20, "command_name": "whoami", "status": "error", "timestamp": "2026-10-30T01:05:00"}],
}


def _hop(task_id, status="achieved", origin="mythic_callback"):
    return {"technique": "callback-context", "target": f"system:host{task_id}", "status": status,
            "evidence": {"mythic_task_id": str(task_id)}, "proof_envelope": {"origin": origin} if origin else {}}


def test_rows_are_counted_against_the_requests_whose_tasks_they_cite():
    ledger = {"hops": [_hop(10), _hop(20, status="failed", origin=None), _hop(10, origin="synthetic_eval")]}
    report = yield_script.build_report(ledger, EXPORT)
    first, second = report["requests"]
    # Positive: a proven row linked to task 10. Negative: task 11 has no row. Near-match: an achieved row whose
    # proof origin is not a runtime origin is linked but not proven.
    assert (first["tasks"], first["task_linked_rows"], first["tasks_with_a_row"], first["proven_rows"]) == (2, 2, 1, 1)
    # Near-match: task 20's row belongs to request 2, not request 1.
    assert (second["tasks"], second["task_linked_rows"], second["proven_rows"]) == (1, 1, 0)
    assert second["rows_by_status"] == {"failed": 1}


def test_cli_writes_json_and_markdown(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"hops": [_hop(10)], "next_hop_number": 2}))
    export = tmp_path / "export.json"
    export.write_text(json.dumps(EXPORT))
    out_json, out_md = tmp_path / "y.json", tmp_path / "y.md"
    assert yield_script.main(["--ledger", str(ledger), "--mythic-export", str(export), "--request", "1",
                              "--json-out", str(out_json), "--markdown-out", str(out_md)]) == 0
    assert [r["request_id"] for r in json.loads(out_json.read_text())["requests"]] == [1]
    assert "| 1 | 2 | 1 | 1 | 1 |" in out_md.read_text()
