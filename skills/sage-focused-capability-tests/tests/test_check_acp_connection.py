"""Offline checks for the ACP connection live-check tool's pass rules (no Mythic, no Docker, no provider).

Reply texts follow Sage's real `/acp` rendering observed against the built worker on 2026-11-21; the run file and
worker events are synthetic and shaped like the ones `run` records.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_acp_connection.py"
SPEC = importlib.util.spec_from_file_location("check_acp_connection", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

SENTINEL = "public-acp-check-sentinel-abc123"
DEFAULT, OTHER = "gpt-6.2-sol", "gpt-6.2-astra"


def status(transport, *, effective="", saved="", saved_profile=False, auth="authenticated", extra=""):
    rows = ["**Coding agent connection**", "", "| Field | Value |", "|---|---|",
            f"| Profile | `codex`{' (saved for this channel)' if saved_profile else ''} |",
            f"| Transport | `{transport}` |"]
    if transport == "connected":
        rows += ["| Protocol | ACP v1 |", "| Agent | @agentclientprotocol/codex-acp 2.9.0 |",
                 f"| Authentication (observed) | `{auth}` |"]
    rows.append("| Provider access | unverified: Sage sends the coding agent no model request |")
    if effective:
        rows.append(f"| Effective model | `{effective}` |")
    rows.append(f"| Saved model | {f'`{saved}`' if saved else 'agent default'} |")
    return "\n".join(rows) + (f"\n\n- {extra}" if extra else "")


MODELS = ("**Coding agent models**\n\n| Model ID | Name | Current |\n|---|---|---|\n"
          f"| `{DEFAULT}` | 6.2 Sol | yes |\n| `{OTHER}` | 6.2 Astra |  |\n\nSelect one with `/acp model <id>`.")

SEQUENCE = [  # (step, channel, reply)
    ("C1", "A", status("disconnected")),
    ("C1", "A", "Unknown coding-agent profile `claude`. Available: `codex`. Nothing was started."),
    ("C1", "A", status("connected", effective=DEFAULT, saved_profile=True)),
    ("C1", "A", MODELS),
    ("C2", "A", status("connected", effective=OTHER, saved=OTHER, saved_profile=True,
                       extra=f"Selected model `{OTHER}` for this channel.")),
    ("C2", "A", status("connected", effective=OTHER, saved=OTHER, saved_profile=True)),
    ("C3", "B", status("connected", effective=DEFAULT, saved_profile=True)),
    ("C3", "B", status("connected", effective=DEFAULT, saved_profile=True)),
    ("C4", "A", status("connected", effective=OTHER, saved=OTHER, saved_profile=True,
                       extra="`not-a-real-model` is not a model this agent advertises. See `/acp models`.")),
    ("C4", "A", status("connected", effective=OTHER, saved=OTHER, saved_profile=True)),
    ("C5", "A", "Disconnected this channel's coding agent and cleared its saved selection."),
    ("C5", "B", status("connected", effective=DEFAULT, saved_profile=True)),
    ("C6", "B", status("disconnected", saved_profile=True, extra="Connection lost: the coding agent closed the connection.")),
    ("C6", "B", status("connected", effective=DEFAULT, saved_profile=True)),
    ("C7", "B", status("disconnected", saved_profile=True)),
    ("C7", "B", status("connected", effective=DEFAULT, saved_profile=True)),
    ("C8", "B", "No running session to stop on this channel. Closed this channel's coding-agent connection; "
                "its saved selection remains."),
    ("C8", "B", status("disconnected", saved_profile=True)),
    ("C9", "A", status("connected", saved_profile=True, auth="unauthenticated",
                       extra="No usable API-key configuration in the worker (the agent answered: Internal error: "
                             "CODEX_API_KEY or OPENAI_API_KEY is not set).")),
    ("C9", "A", status("connected", saved_profile=True, auth="unauthenticated",
                       extra="No usable API-key configuration in the worker.")),
    ("C9", "A", "Disconnected this channel's coding agent and cleared its saved selection."),
    ("C10", "A", status("failed", extra="The coding-agent worker is unavailable (FileNotFoundError: No such file or "
                                        "directory).")),
]
ACCEPTS = [0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 4, 4, 4, 1, 1, 1, 1]


def _event(conn, event, t, **extra):
    return {"conn": conn, "event": event, "time": f"2026-11-21T22:00:{t:02d}.000Z", **extra}


T0 = check._epoch("2026-11-21T22:00:00.000Z")


def _passing():
    run = {"readiness": {"sage": {"started_at": "before"}}, "sentinel": SENTINEL, "task_baseline": 9,
           "started_at": T0 - 100, "finished_at": T0 + 300, "llm_spans": 0,
           "models": {"default": DEFAULT, "other": OTHER}, "conns": {"A1": 1, "B1": 2, "B2": 3, "B3": 4, "A2": 1},
           "worker_restart": {"listening": True}, "sage_restart": {"consuming": True, "started_at": "after"},
           "steps": {}}
    rows = {}
    for index, (step, channel, text) in enumerate(SEQUENCE):
        request_id = 500 + index
        rows[request_id] = {"status": "complete", "messages": [
            {"author_type": "operator", "message": "/acp"}, {"author_type": "ai", "message": text}]}
        entry = {"channel": channel, "request_id": request_id, "tasks_after": 9, "accepts_after": ACCEPTS[index],
                 "sent_at": T0 + index, "completed_at": T0 + index + 0.5,
                 "processes": [{"pgid": "10"}, {"pgid": "20"}] if step == "C3" else []}
        if channel == "A" and index >= 2:
            entry["a_settings_after"] = 1 if index >= 4 else 0
        run["steps"].setdefault(step, {"requests": []})["requests"].append(entry)
    c5 = run["steps"]["C5"]["requests"][0]["completed_at"]
    c8 = run["steps"]["C8"]["requests"][0]["completed_at"]
    first = [_event(1, "accept", 1), _event(1, "message", 2, method="session/set_config_option", dir="to_agent"),
             _event(2, "accept", 3), {**_event(1, "cleanup", 0, exited=True, stage="term"),
                                      "time": _iso(c5 + 2)},
             _event(3, "accept", 5), _event(4, "accept", 6),
             {**_event(4, "cleanup", 0, exited=True, stage="term"), "time": _iso(c8 + 1.6)}]
    second = [{"event": "listening", "api_key_provisioned": False},
              _event(1, "accept", 30, api_key_provisioned=False), _event(1, "cleanup", 31, exited=True)]
    run["worker_logs"] = {"first": first, "second": second}
    return run, rows


def _iso(epoch):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _reply(run, rows, step, index, text):
    rows[run["steps"][step]["requests"][index]["request_id"]]["messages"][-1]["message"] = text


def test_a_recorded_shaped_passing_run_meets_every_rule():
    run, rows = _passing()
    criteria = check.criteria_for(run, rows)
    assert criteria == {key: True for key in criteria}, [k for k, v in criteria.items() if not v]
    assert len(SEQUENCE) == check.EXPECTED_REQUESTS


def _unconfigured(run, rows):
    _reply(run, rows, "C1", 0, status("unconfigured"))


def _unknown_profile_spawned(run, rows):
    run["steps"]["C1"]["requests"][1]["accepts_after"] = 1


def _model_not_applied(run, rows):
    _reply(run, rows, "C2", 0, status("connected", effective=DEFAULT, saved_profile=True))


def _shared_connection(run, rows):
    run["conns"]["B1"] = run["conns"]["A1"]


def _unknown_model_sent(run, rows):
    run["steps"]["C4"]["requests"][0]["a_settings_after"] = 2


def _slow_cleanup(run, rows):
    run["worker_logs"]["first"][3]["time"] = _iso(run["steps"]["C5"]["requests"][0]["completed_at"] + 6)


def _stale_connected(run, rows):
    _reply(run, rows, "C6", 0, status("connected", effective=DEFAULT, saved_profile=True))


def _sage_not_restarted(run, rows):
    run["sage_restart"]["started_at"] = "before"


def _stop_wording_changed(run, rows):
    _reply(run, rows, "C8", 0, "Stop requested. Closed this channel's coding-agent connection; its saved "
                               "selection remains.")


def _credential_claimed(run, rows):
    _reply(run, rows, "C9", 0, status("connected", saved_profile=True, auth="authenticated",
                                      extra="No usable API-key configuration in the worker."))


def _fallback(run, rows):
    _reply(run, rows, "C10", 0, status("connected", effective=DEFAULT))


def _two_replies(run, rows):
    rows[run["steps"]["C3"]["requests"][0]["request_id"]]["messages"].append({"author_type": "ai", "message": "x"})


def _task_created(run, rows):
    run["steps"]["C7"]["requests"][1]["tasks_after"] = 10


def _prompt_relayed(run, rows):
    run["worker_logs"]["first"].append(_event(4, "message", 9, method="session/prompt", dir="to_agent"))


def _sentinel_leaked(run, rows):
    _reply(run, rows, "C2", 1, status("connected", effective=OTHER, extra=SENTINEL))


def _llm_span(run, rows):
    run["llm_spans"] = 1


@pytest.mark.parametrize("mutate,rule", [
    (_unconfigured, "C1"), (_unknown_profile_spawned, "C1"), (_model_not_applied, "C2"), (_shared_connection, "C3"),
    (_unknown_model_sent, "C4"), (_slow_cleanup, "C5"), (_stale_connected, "C6"), (_sage_not_restarted, "C7"),
    (_stop_wording_changed, "C8"), (_credential_claimed, "C9"), (_fallback, "C10"),
    (_two_replies, "terminal_one_reply"), (_task_created, "no_tasks_no_inference"),
    (_llm_span, "no_tasks_no_inference"), (_prompt_relayed, "no_forbidden_methods"), (_sentinel_leaked, "no_sentinel"),
])
def test_each_rule_fails_on_its_counterexample(mutate, rule):
    run, rows = _passing()
    mutate(run, rows)
    criteria = check.criteria_for(copy.deepcopy(run), rows)
    assert criteria[rule] is False


def test_env_setting_reads_the_last_uncommented_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text("#SAGE_ACP_SOCKET=/old\nSAGE_ACP_SOCKET=/Mythic/.sage_acp/acp.sock\n")
    assert check.env_setting(env, "SAGE_ACP_SOCKET") == check.SAGE_SOCKET
    env.write_text("#SAGE_ACP_SOCKET=/Mythic/.sage_acp/acp.sock\n")
    assert check.env_setting(env, "SAGE_ACP_SOCKET") == ""
