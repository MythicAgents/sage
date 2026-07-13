import argparse
import asyncio
import importlib.util
import json
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "sage-mythic-payload-deploy"
    / "scripts"
    / "deploy_payload_via_ludus.py"
)
SPEC = importlib.util.spec_from_file_location("deploy_payload_via_ludus", SCRIPT)
deploy = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(deploy)


def test_find_active_interactive_session_matches_only_active_named_user():
    output = (
        " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\r\n"
        " localuser             console             1  Active      none   6/25/2026 7:27 PM\r\n"
        " samwell.tarly                             3  Disc           10  6/25/2026 8:30 PM\r\n"
        " samwell.tarly         rdp-tcp#4           4  Active          .  6/25/2026 8:45 PM\r\n"
    )

    result = deploy.find_active_interactive_session(output, r"NORTH\samwell.tarly")

    assert result == {
        "user": "samwell.tarly",
        "session_id": "4",
        "line": "samwell.tarly         rdp-tcp#4           4  Active          .  6/25/2026 8:45 PM",
    }


def test_find_user_sessions_matches_named_user_in_any_state():
    output = (
        " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\r\n"
        " localuser             console             1  Active      none   7/11/2026 9:00 AM\r\n"
        " localuser                                 2  Disc           10  7/11/2026 8:00 AM\r\n"
        " samwell.tarly         rdp-tcp#4           4  Active          .  7/11/2026 9:05 AM\r\n"
    )

    assert deploy.find_user_sessions(output, "localuser") == [
        {
            "user": "localuser",
            "session_id": "1",
            "state": "Active",
            "line": "localuser             console             1  Active      none   7/11/2026 9:00 AM",
        },
        {
            "user": "localuser",
            "session_id": "2",
            "state": "Disc",
            "line": "localuser                                 2  Disc           10  7/11/2026 8:00 AM",
        },
    ]


def test_logoff_user_sessions_only_logs_off_named_user(monkeypatch):
    outputs = iter([
        (
            " localuser             console             1  Active      none   now\r\n"
            " samwell.tarly         rdp-tcp#4           4  Active          .  now\r\n"
        ),
        " samwell.tarly         rdp-tcp#4           4  Active          .  now\r\n",
    ])
    scripts = []

    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {"output": next(outputs), "status_code": 0},
    )
    monkeypatch.setattr(
        deploy,
        "run_ps",
        lambda session, script: scripts.append(script) or {
            "status_code": 0,
            "stdout": "",
            "stderr": "",
        },
    )

    result = deploy.logoff_user_sessions(object(), "localuser")

    assert [row["session_id"] for row in result["logged_off"]] == ["1"]
    assert result["remaining"] == []
    assert len(scripts) == 1
    assert "logoff.exe" in scripts[0]
    assert "'1'" in scripts[0]
    assert "4" not in scripts[0]


def test_default_remote_filename_preserves_interactive_payload_name():
    assert deploy.default_remote_filename("apollo.exe", "scheduled-task-interactive") == "apollo.exe"


def test_with_ludus_range_id_preserves_existing_query_and_does_not_duplicate():
    assert (
        deploy.with_ludus_range_id("/api/v2/range/logs?tail=60", "SAGEPOLICY20260712")
        == "/api/v2/range/logs?tail=60&rangeID=SAGEPOLICY20260712"
    )
    assert (
        deploy.with_ludus_range_id("/api/v2/range?rangeID=existing", "SAGEPOLICY20260712")
        == "/api/v2/range?rangeID=existing"
    )


def test_ludus_creds_selects_named_mcp_server_without_changing_default(monkeypatch, tmp_path):
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ludus": {"env": {"LUDUS_URL": "https://goad", "LUDUS_API_KEY": "goad.key"}},
                    "ludus_sagerepl": {
                        "env": {"LUDUS_URL": "https://sagerepl", "LUDUS_API_KEY": "sagerepl.key"}
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    assert deploy.ludus_creds(mcp_path) == ("https://goad", "goad.key")
    assert deploy.ludus_creds(mcp_path, "ludus_sagerepl") == ("https://sagerepl", "sagerepl.key")

    monkeypatch.setenv(deploy.LUDUS_MCP_SERVER_ENV, "ludus_sagerepl")
    assert deploy.ludus_creds(mcp_path) == ("https://sagerepl", "sagerepl.key")


def test_launch_payload_interactive_waits_for_session_and_uses_interactive_task(monkeypatch):
    calls = []

    def fake_wait(session, run_as_user, *, timeout_seconds, poll_interval):
        calls.append(("wait", session, run_as_user, timeout_seconds, poll_interval))
        return {"session_line": "samwell.tarly rdp-tcp#4 4 Active"}

    def fake_launch(session, remote_path, run_as_user, task_name):
        calls.append(("launch", session, remote_path, run_as_user, task_name))
        return {"Method": "scheduled-task-interactive", "TaskName": task_name}

    monkeypatch.setattr(deploy, "wait_for_active_interactive_session", fake_wait)
    monkeypatch.setattr(deploy, "launch_scheduled_task_interactive", fake_launch)

    args = argparse.Namespace(
        launch_method="scheduled-task-interactive",
        run_as_user=r"NORTH\samwell.tarly",
        run_as_password=None,
        run_as_password_env=None,
        wait_interactive_session_seconds=120,
        poll_interval=3.0,
        task_name="SageApolloInteractive",
    )

    session = object()
    result = deploy.launch_payload(session, r"C:\Users\Public\apollo.exe", args)

    assert result["Method"] == "scheduled-task-interactive"
    assert result["interactive_session"]["session_line"].endswith("Active")
    assert calls == [
        ("wait", session, r"NORTH\samwell.tarly", 120, 3.0),
        ("launch", session, r"C:\Users\Public\apollo.exe", r"NORTH\samwell.tarly", "SageApolloInteractive"),
    ]


def test_maybe_disconnect_interactive_session_after_new_callback(monkeypatch):
    calls = []

    def fake_disconnect(session, session_id):
        calls.append((session, session_id))
        return {"Method": "tsdiscon", "SessionId": session_id}

    monkeypatch.setattr(deploy, "disconnect_interactive_session", fake_disconnect)

    args = argparse.Namespace(
        launch_method="scheduled-task-interactive",
        disconnect_interactive_session=True,
    )
    session = object()
    result = deploy.maybe_disconnect_interactive_session(
        session,
        args,
        {"interactive_session": {"session_id": "4"}},
        [{"display_id": 3}],
    )

    assert result == {"Method": "tsdiscon", "SessionId": "4"}
    assert calls == [(session, "4")]


def test_maybe_disconnect_interactive_session_keeps_session_when_callback_missing():
    args = argparse.Namespace(
        launch_method="scheduled-task-interactive",
        disconnect_interactive_session=True,
    )

    result = deploy.maybe_disconnect_interactive_session(
        object(),
        args,
        {"interactive_session": {"session_id": "4"}},
        [],
    )

    assert result == {"skipped": True, "reason": "no-new-callback-observed"}


def test_wait_for_callback_checkin_advance_accepts_retained_callback(monkeypatch):
    rows = [{
        "display_id": 1,
        "host": "CASTELBLACK",
        "user": "samwell.tarly",
        "last_checkin": "new",
        "payload": {"payloadtype": {"name": "apollo"}},
    }]

    async def fake_callbacks(client):
        return rows

    monkeypatch.setattr(deploy, "get_callbacks", fake_callbacks)

    latest, callback = asyncio.run(deploy.wait_for_callback_checkin_advance(
        object(),
        {1: "old"},
        payload_type="apollo",
        host="CASTELBLACK",
        user="samwell.tarly",
        seconds=0,
        poll_interval=0.1,
    ))

    assert latest == rows
    assert callback == rows[0]


def test_launch_existing_accepts_windows_normalized_bare_principal(monkeypatch):
    scripts = []

    monkeypatch.setattr(
        deploy,
        "wait_for_active_interactive_session",
        lambda *args, **kwargs: {"session_id": "4"},
    )

    def fake_run_ps(session, script):
        scripts.append(script)
        return {
            "stdout": (
                '{"Method":"existing-scheduled-task-interactive",'
                '"TaskName":"SageApolloBootstrap","RunAsUser":"samwell.tarly"}'
            )
        }

    monkeypatch.setattr(deploy, "run_ps", fake_run_ps)

    result = deploy.launch_existing_scheduled_task_interactive(
        object(),
        "SageApolloBootstrap",
        r"NORTH\samwell.tarly",
        timeout_seconds=30,
        poll_interval=1.0,
    )

    assert result["RunAsUser"] == "samwell.tarly"
    assert "$actualUser -notmatch '\\\\'" in scripts[0]
    assert "$actualUser -eq $expectedLeaf" in scripts[0]
