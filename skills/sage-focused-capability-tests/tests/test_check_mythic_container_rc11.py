"""Offline checks for the mythic-container rc11 live check's pass rules (no Mythic, no container).

Mythic rows, tasks and spans below are synthetic, shaped like Mythic's chat rows and Phoenix's spans table. The
Watcher replies are rendered by Sage's own `render_watcher_status` and the apply line Sage's `/watcher apply` handler
writes, so the parsers read what the deployed Sage prints. They prove the rules accept a correct set and reject each
falsifier.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_mythic_container_rc11.py"
SPEC = importlib.util.spec_from_file_location("check_mythic_container_rc11", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

from sage_chat.findings_watcher import render_watcher_status  # noqa: E402

W = 233
CALLBACK = 2
START = 1_795_600_000.0


def _status(state="running", generation=1, owner=W, scans=1, error="-"):
    return render_watcher_status({
        "operation_id": "1", "status": state, "owner_channel_name": "rc11 live check (Sage Watcher)",
        "owner_channel_id": owner, "generation": generation, "provider": "openai", "model": "route-model",
        "bot_username": "sage_watcher_bot", "last_success_at": "2026-11-25T00:00:00Z", "active_findings": 0,
        "pending_deliveries": 0, "interval_seconds": 300, "channel": "#sage-findings", "findings_channel_id": 12,
        "scan_count": scans, "last_error_code": error,
    })


def _apply_text(generation=1):
    return (f"Watcher profile generation `{generation}` applied from locked owner "
            f"`rc11 live check (Sage Watcher)` (`{W}`).\n\n" + _status("starting", generation, scans=0))


def _rows(text, status="complete"):
    return {"status": status, "messages": [{"author_type": "user", "message": "prompt"},
                                           {"author_type": "ai", "message": text}]}


def _marker(generation=1):
    return {"generation": generation, "state": "starting", "provider": "openai", "model": "route-model",
            "config_sources": {"provider": "environment", "model": "environment"},
            "profile_binding_sha256": "a" * 64, "interval_seconds": 300, "paused": False}


def _passing():
    requests = {name: {"request_id": 100 + i, "sent_at": START + 60 * i, "sends": 1}
                for i, name in enumerate(("L1", "L2", "L3_apply", "L3_status", "L4_status", "L5_scan"))}
    requests["L2"].update(cards_seen=1, cards_accepted=1, cards_rejected=0)
    run = {
        "callback": CALLBACK, "channels": {"C": 231, "S": 232, "W": W}, "restarts": 1, "restart_ok": True,
        "started_at": START - 5, "finished_at": START + 600, "requests": requests, "phoenix_health": [200, 200],
        "a1": {"packages": dict(check.EXPECTED_PACKAGES), "differing_from_constraints": {},
               "pip_check_returncode": 0, "audit_ran": True, "audit_findings": 0},
    }
    observed = {
        "rows": {
            "L1": _rows("| Callback | Host | User |\n|---|---|---|\n| 2 | HOST | user |"),
            "L2": _rows("whoami returned the callback's user."),
            "L3_apply": _rows(_apply_text(1)),
            "L3_status": _rows(_status("starting", 1, scans=0)),
            "L4_status": _rows(_status("running", 1, scans=1)),
            "L5_scan": _rows(_status("running", 1, scans=2)),
        },
        "c_channel_metadata": {"items": [{"key": "channel_id", "value": 231}], "updated_at": "2026-11-25T09:46:41Z"},
        "w_channel_metadata": {"items": [{"key": "watcher", "value": _marker(1)}], "updated_at": "2026-11-25T09:50:00Z"},
        "tasks": [{"display_id": 51, "command_name": "whoami", "completed": True, "status": "completed",
                   "callback": {"display_id": CALLBACK}, "responses": [{"id": 9}]}],
        "llm_spans": [{"prompt_tokens": 4000, "completion_tokens": 300}] * 8,
    }
    # updated_at must not precede L1's send; START is 2026-11-25T09:46:40Z.
    return run, observed


def test_a_correct_set_passes():
    run, observed = _passing()
    result = check.evaluate(run, observed)
    assert result["verdict"] == "pass", result["criteria"]
    assert result["observations"]["apply_generation"] == 1
    assert result["observations"]["status_after_scan"]["scans"] == 2


def test_parsers_read_sages_own_watcher_replies():
    assert check.parse_apply(_apply_text(7)) == 7
    assert check.parse_status(_status("stale-generation", 3, owner=W, scans=4, error="WatcherGenerationFence")) == {
        "state": "stale-generation", "owner": str(W), "generation": 3, "scans": 4,
        "last_error": "WatcherGenerationFence"}
    assert check.parse_apply("Watcher control denied: `WatcherOwnerConflict`.") is None
    assert check.parse_status("Watcher control denied: `WatcherOwnerConflict`.") is None


def _set(path, value):
    def change(run, observed):
        target = run if path[0] == "run" else observed
        for key in path[1:-1]:
            target = target[key]
        target[path[-1]] = value
    return change


@pytest.mark.parametrize("change, failing", [
    (_set(("run", "a1", "packages", "mythic-container"), "0.7.0rc9"), "A1"),
    (_set(("run", "a1", "packages", "ujson"), "5.9.0"), "A1"),
    (_set(("run", "a1", "differing_from_constraints"), {"aiohttp": ["3.14.3", "3.14.4"]}), "A1"),
    (_set(("run", "a1", "pip_check_returncode"), 1), "A1"),
    (_set(("run", "a1", "audit_findings"), 2), "A1"),
    (_set(("run", "a1", "audit_ran"), False), "A1"),
    (_set(("obs", "rows", "L1", "status"), "error"), "L1"),
    (_set(("obs", "c_channel_metadata", "items"), []), "L1"),
    (_set(("obs", "c_channel_metadata", "updated_at"), "2026-11-25T09:46:39Z"), "L1"),
    (_set(("obs", "c_channel_metadata"), {}), "L1"),
    (_set(("run", "requests", "L2", "cards_seen"), 0), "L2"),
    (_set(("run", "requests", "L2", "cards_seen"), 2), "L2"),
    (_set(("run", "requests", "L2", "cards_accepted"), 0), "L2"),
    (_set(("obs", "tasks"), []), "L2"),
    (_set(("obs", "tasks", 0, "callback"), {"display_id": 3}), "L2"),
    (_set(("obs", "tasks", 0, "completed"), False), "L2"),
    (_set(("obs", "tasks", 0, "responses"), []), "L2"),
    (_set(("obs", "rows", "L2", "status"), "error"), "L2"),
    (_set(("obs", "w_channel_metadata"), {"watcher": _marker(1), "updated_at": "x"}), "L3"),
    (_set(("obs", "w_channel_metadata", "items"), [{"key": "watcher", "value": _marker(2)}]), "L3"),
    (_set(("obs", "w_channel_metadata", "items"),
          [{"key": "watcher", "value": _marker(1)}, {"key": "watcher", "value": _marker(1)}]), "L3"),
    (_set(("obs", "rows", "L3_apply"), _rows("Watcher control denied: `WatcherOwnerConflict`.")), "L3"),
    (_set(("obs", "rows", "L3_status"), _rows(_status("stale-generation", 1, scans=0))), "L3"),
    (_set(("obs", "rows", "L3_status"), _rows(_status("starting", 1, owner=999, scans=0))), "L3"),
    (_set(("obs", "rows", "L4_status"), _rows(_status("stale-generation", 1, scans=1))), "L4"),
    (_set(("obs", "rows", "L4_status"), _rows(_status("controller-missing", 1, scans=1))), "L4"),
    (_set(("obs", "rows", "L4_status"), _rows(_status("unconfigured", 0, owner=0, scans=0))), "L4"),
    (_set(("run", "restart_ok"), False), "L4"),
    (_set(("obs", "rows", "L5_scan"), _rows(_status("running", 1, scans=1))), "L5"),
    (_set(("obs", "rows", "L5_scan"), _rows(_status("degraded", 1, scans=2, error="WatcherScopeMismatch"))), "L5"),
    (_set(("obs", "rows", "L5_scan"), _rows("Watcher control denied: `WatcherOwnerConflict`.")), "L5"),
    (_set(("obs", "llm_spans"), [{"prompt_tokens": 1, "completion_tokens": 1}] * 21), "L6"),
    (_set(("obs", "llm_spans"), [{"prompt_tokens": 200_000, "completion_tokens": 200_000}]), "L6"),
    (_set(("run", "finished_at"), START + 31 * 60), "L6"),
    (_set(("run", "restarts"), 2), "L6"),
    (_set(("run", "stopped"), {"request": "L2", "reason": "token limit"}), "L6"),
    (_set(("run", "requests", "L1", "timeout"), "request timeout"), "L6"),
    (_set(("run", "requests", "L1", "sends"), 2), "L6"),
    (_set(("run", "channels", "X"), 1), "L6"),
])
def test_each_falsifier_fails_its_rule(change, failing):
    run, observed = copy.deepcopy(_passing())
    change(run, observed)
    result = check.evaluate(run, observed)
    assert result["criteria"][failing] is False, result["criteria"]
    assert result["verdict"] == "fail"


def test_two_tasks_fail_l2_and_l6():
    run, observed = copy.deepcopy(_passing())
    observed["tasks"].append(dict(observed["tasks"][0], display_id=52))
    result = check.evaluate(run, observed)
    assert result["criteria"]["L2"] is False and result["criteria"]["L6"] is False


def test_a_set_that_never_sent_l1_did_not_run():
    run, observed = copy.deepcopy(_passing())
    run["requests"] = {}
    assert check.evaluate(run, observed)["verdict"] == "did_not_run"


def test_watcher_entries_reads_only_items_keyed_watcher():
    assert check.watcher_entries({"items": [{"key": "watcher", "value": 1}, {"key": "Watcher", "value": 2}]}) == [1]
    assert check.watcher_entries({"watcher": {"generation": 1}}) == []
    assert check.watcher_entries({"items": {"watcher": 1}}) == []
