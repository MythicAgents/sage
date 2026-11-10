import argparse
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


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


def test_wait_for_active_interactive_session_preserves_active_named_user(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {
            "output": (
                " localuser             console             1  Active      none   now\r\n"
                " samwell.tarly         rdp-tcp#4           4  Active          .  now\r\n"
            ),
            "status_code": 0,
        },
    )

    result = deploy.wait_for_active_interactive_session(
        object(), r"NORTH\samwell.tarly", timeout_seconds=0, poll_interval=0.25
    )

    assert result["session_id"] == "4"


def test_wait_for_active_interactive_session_rejects_unavailable_census(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {"output": "Access is denied.", "status_code": 5},
    )

    with pytest.raises(deploy.DeployError, match="Could not enumerate Windows sessions") as exc:
        deploy.wait_for_active_interactive_session(
            object(), r"NORTH\samwell.tarly", timeout_seconds=0, poll_interval=0.25
        )

    assert "Access is denied" not in str(exc.value)


def test_wait_for_active_interactive_session_reports_genuine_empty_census(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {"output": "quser : No User exists for *", "status_code": 1},
    )

    with pytest.raises(deploy.DeployError, match="No Windows user sessions are available yet"):
        deploy.wait_for_active_interactive_session(
            object(), r"NORTH\samwell.tarly", timeout_seconds=0, poll_interval=0.25
        )


def test_wait_for_active_interactive_session_reports_inactive_near_match_without_rows(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {
            "output": " samwell.tarly                             3  Disc           10  now\r\n",
            "status_code": 0,
        },
    )

    with pytest.raises(deploy.DeployError, match="matching user sessions: 1") as exc:
        deploy.wait_for_active_interactive_session(
            object(), r"NORTH\samwell.tarly", timeout_seconds=0, poll_interval=0.25
        )

    assert "samwell.tarly                             3" not in str(exc.value)


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

    result = deploy.logoff_user_sessions(
        object(), "localuser", allow_already_absent=True
    )

    assert [row["session_id"] for row in result["logged_off"]] == ["1"]
    assert result["remaining"] == []
    assert result["already_absent"] is False
    assert len(scripts) == 1
    assert "logoff.exe" in scripts[0]
    assert "'1'" in scripts[0]
    assert "4" not in scripts[0]


def test_logoff_user_sessions_rejects_empty_initial_enumeration(monkeypatch):
    scripts = []
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {
            "output": "quser : No User exists for *",
            "status_code": 1,
        },
    )
    monkeypatch.setattr(
        deploy,
        "run_ps",
        lambda session, script: scripts.append(script),
    )

    with pytest.raises(deploy.DeployError, match="No Windows user sessions are available yet"):
        deploy.logoff_user_sessions(object(), "localuser")

    assert scripts == []


def test_logoff_user_sessions_rejects_initial_enumeration_error(monkeypatch):
    scripts = []
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {
            "output": "Access is denied.",
            "status_code": 1,
        },
    )
    monkeypatch.setattr(
        deploy,
        "run_ps",
        lambda session, script: scripts.append(script),
    )

    with pytest.raises(deploy.DeployError, match="Could not enumerate Windows sessions before logoff"):
        deploy.logoff_user_sessions(object(), "localuser")

    assert scripts == []


def test_logoff_user_sessions_rejects_zero_match_without_touching_other_users(monkeypatch):
    scripts = []
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {
            "output": " robb.stark            rdp-tcp#1           3  Active          .  now\r\n",
            "status_code": 0,
        },
    )
    monkeypatch.setattr(
        deploy,
        "run_ps",
        lambda session, script: scripts.append(script),
    )

    with pytest.raises(deploy.DeployError, match="No session owned by 'localuser' was found"):
        deploy.logoff_user_sessions(object(), "localuser")

    assert scripts == []


def test_logoff_user_sessions_allows_verified_absence_without_touching_other_users(monkeypatch):
    scripts = []
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {
            "output": " robb.stark            rdp-tcp#1           3  Active          .  now\r\n",
            "status_code": 0,
        },
    )
    monkeypatch.setattr(deploy, "run_ps", lambda session, script: scripts.append(script))

    result = deploy.logoff_user_sessions(
        object(), "localuser", allow_already_absent=True
    )

    assert result["already_absent"] is True
    assert result["logged_off"] == []
    assert scripts == []


def test_logoff_user_sessions_allows_recognized_empty_census(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {"output": "quser : No User exists for *", "status_code": 1},
    )

    result = deploy.logoff_user_sessions(
        object(), "localuser", allow_already_absent=True
    )

    assert result["already_absent"] is True
    assert result["logged_off"] == []


def test_logoff_user_sessions_allow_absent_rejects_ambiguous_named_row(monkeypatch):
    scripts = []
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {"output": " localuser unknown output", "status_code": 0},
    )
    monkeypatch.setattr(deploy, "run_ps", lambda session, script: scripts.append(script))

    with pytest.raises(deploy.DeployError, match="ambiguous named row"):
        deploy.logoff_user_sessions(object(), "localuser", allow_already_absent=True)

    assert scripts == []


def test_logoff_user_sessions_allow_absent_rejects_enumeration_error(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {"output": "Access is denied.", "status_code": 1},
    )

    with pytest.raises(deploy.DeployError, match="Could not enumerate Windows sessions"):
        deploy.logoff_user_sessions(object(), "localuser", allow_already_absent=True)


def test_logoff_user_sessions_accepts_verified_empty_after_exact_logoff(monkeypatch):
    outputs = iter([
        {
            "output": " localuser             console             1  Active      none   now\r\n",
            "status_code": 0,
        },
        {
            "output": "quser : No User exists for *",
            "status_code": 1,
        },
    ])
    scripts = []
    monkeypatch.setattr(deploy, "query_user_sessions", lambda session: next(outputs))
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


def test_logoff_user_sessions_rejects_named_user_remaining_after_logoff(monkeypatch):
    row = " localuser             console             1  Active      none   now\r\n"
    outputs = iter([
        {"output": row, "status_code": 0},
        {"output": row, "status_code": 0},
    ])
    scripts = []
    monkeypatch.setattr(deploy, "query_user_sessions", lambda session: next(outputs))
    monkeypatch.setattr(
        deploy,
        "run_ps",
        lambda session, script: scripts.append(script) or {
            "status_code": 0,
            "stdout": "",
            "stderr": "",
        },
    )

    with pytest.raises(deploy.DeployError, match="Sessions for 'localuser' remain after logoff"):
        deploy.logoff_user_sessions(object(), "localuser")

    assert len(scripts) == 1


def test_logoff_user_sessions_rejects_unverifiable_after_enumeration(monkeypatch):
    outputs = iter([
        {
            "output": " localuser             console             1  Active      none   now\r\n",
            "status_code": 0,
        },
        {
            "output": "Access is denied.",
            "status_code": 1,
        },
    ])
    scripts = []
    monkeypatch.setattr(deploy, "query_user_sessions", lambda session: next(outputs))
    monkeypatch.setattr(
        deploy,
        "run_ps",
        lambda session, script: scripts.append(script) or {
            "status_code": 0,
            "stdout": "",
            "stderr": "",
        },
    )

    with pytest.raises(deploy.DeployError, match="Could not verify Windows sessions after logoff"):
        deploy.logoff_user_sessions(object(), "localuser")

    assert len(scripts) == 1


def test_logoff_user_cli_exits_nonzero_for_empty_initial_enumeration(monkeypatch, capsys):
    monkeypatch.setattr(
        deploy,
        "select_ludus_host",
        lambda args: {
            "inventory_hostname": "TEST-SRV02",
            "ansible_host": "192.0.2.22",
        },
    )
    monkeypatch.setattr(deploy, "winrm_session", lambda *args: object())
    monkeypatch.setattr(
        deploy,
        "query_user_sessions",
        lambda session: {
            "output": "quser : No User exists for *",
            "status_code": 1,
        },
    )

    status = asyncio.run(deploy.async_main(["logoff-user", "--username", "localuser"]))

    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert "No Windows user sessions are available yet" in captured.err


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


def test_select_ludus_host_allows_unique_exact_ip_fallback_for_custom_range_hostname(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "load_ludus_inventory",
        lambda *_args, **_kwargs: {
            "SAGELAPSR320260714-SAGE-LAPS-HOLDOUT-R3-WS01": {
                "inventory_hostname": "SAGELAPSR320260714-SAGE-LAPS-HOLDOUT-R3-WS01",
                "ansible_host": "10.11.10.31",
            },
            "SAGELAPSR320260714-SAGE-LAPS-HOLDOUT-R3-SRV02": {
                "inventory_hostname": "SAGELAPSR320260714-SAGE-LAPS-HOLDOUT-R3-SRV02",
                "ansible_host": "10.11.10.22",
            },
        },
    )

    result = deploy.select_ludus_host(argparse.Namespace(
        mcp_path=".mcp.json",
        ludus_range_id="SAGELAPSR320260714",
        ludus_mcp_server="ludus_sagerepl",
        ludus_host=None,
        target_host="HARBOR-WS01",
        target_ip="10.11.10.31",
    ))

    assert result["inventory_hostname"].endswith("-WS01")
    assert result["ansible_host"] == "10.11.10.31"


def test_select_ludus_host_rejects_ambiguous_exact_ip_fallback(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "load_ludus_inventory",
        lambda *_args, **_kwargs: {
            "range-a-WS01": {"inventory_hostname": "range-a-WS01", "ansible_host": "10.11.10.31"},
            "range-b-WS01": {"inventory_hostname": "range-b-WS01", "ansible_host": "10.11.10.31"},
        },
    )

    with pytest.raises(deploy.DeployError, match="Multiple Ludus inventory hosts matched explicit target IP"):
        deploy.select_ludus_host(argparse.Namespace(
            mcp_path=".mcp.json",
            ludus_range_id="SAGELAPSR320260714",
            ludus_mcp_server="ludus_sagerepl",
            ludus_host=None,
            target_host="HARBOR-WS01",
            target_ip="10.11.10.31",
        ))


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

    responses = iter([
        (
            '{"TaskName":"SageApolloBootstrap","RunAsUser":"samwell.tarly",'
            '"State":"Ready","Execute":"C:\\\\Users\\\\Public\\\\apollo.exe",'
            '"ExecutableSha256":"abc123"}'
        ),
        (
            '{"Method":"existing-scheduled-task-interactive",'
            '"TaskName":"SageApolloBootstrap","RunAsUser":"samwell.tarly"}'
        ),
    ])

    def fake_run_ps(session, script):
        scripts.append(script)
        return {"stdout": next(responses)}

    monkeypatch.setattr(deploy, "run_ps", fake_run_ps)

    result = deploy.launch_existing_scheduled_task_interactive(
        object(),
        "SageApolloBootstrap",
        r"NORTH\samwell.tarly",
        timeout_seconds=30,
        poll_interval=1.0,
        expected_task_state="Ready",
        expected_remote_path=r"C:\Users\Public\apollo.exe",
        expected_remote_sha256="abc123",
    )

    assert result["RunAsUser"] == "samwell.tarly"
    assert "$actualUser -notmatch '\\\\'" in scripts[0]
    assert "$actualUser -eq $expectedLeaf" in scripts[0]
    assert "Start-ScheduledTask" not in scripts[0]
    assert "Start-ScheduledTask" in scripts[1]
    assert result["prelaunch"]["State"] == "Ready"


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("State", "Running", "state is"),
        ("Execute", r"C:\\Windows\\temp\\apollo.exe", "executable path"),
        ("ExecutableSha256", "wrong", "executable hash"),
    ],
)
def test_launch_existing_rejects_fixture_mismatch_before_start(monkeypatch, field, value, match):
    monkeypatch.setattr(
        deploy,
        "wait_for_active_interactive_session",
        lambda *args, **kwargs: {"session_id": "4"},
    )
    prelaunch = {
        "TaskName": "SageApolloBootstrap",
        "RunAsUser": "samwell.tarly",
        "State": "Ready",
        "Execute": r"C:\Users\Public\apollo.exe",
        "ExecutableSha256": "abc123",
    }
    prelaunch[field] = value
    scripts = []

    def fake_run_ps(session, script):
        scripts.append(script)
        return {"stdout": json.dumps(prelaunch)}

    monkeypatch.setattr(deploy, "run_ps", fake_run_ps)

    with pytest.raises(deploy.DeployError, match=match):
        deploy.launch_existing_scheduled_task_interactive(
            object(),
            "SageApolloBootstrap",
            r"NORTH\samwell.tarly",
            timeout_seconds=30,
            poll_interval=1.0,
            expected_task_state="Ready",
            expected_remote_path=r"C:\Users\Public\apollo.exe",
            expected_remote_sha256="abc123",
        )

    assert len(scripts) == 1
    assert "Start-ScheduledTask" not in scripts[0]


def test_launch_scheduled_task_interactive_uses_manual_only_far_future_trigger(monkeypatch):
    scripts = []

    def fake_run_ps(session, script):
        scripts.append(script)
        return {"stdout": '{"Method":"scheduled-task-interactive","TaskName":"SageApolloBootstrap"}'}

    monkeypatch.setattr(deploy, "run_ps", fake_run_ps)

    result = deploy.launch_scheduled_task_interactive(
        object(),
        r"C:\Users\Public\apollo.exe",
        r"NORTH\samwell.tarly",
        "SageApolloBootstrap",
    )

    assert result["TaskName"] == "SageApolloBootstrap"
    assert f"AddYears({deploy.DEFAULT_MANUAL_TASK_TRIGGER_YEARS})" in scripts[0]
    assert "AddMinutes(1)" not in scripts[0]
    assert "Start-ScheduledTask -TaskName $taskName" in scripts[0]


def test_matching_advanced_callback_lanes_excludes_inactive_and_unchanged_rows():
    rows = [
        {
            "display_id": 1,
            "host": "SUMMIT-WS01",
            "user": "user1",
            "active": False,
            "last_checkin": "new-imported",
            "payload": {"payloadtype": {"name": "apollo"}},
        },
        {
            "display_id": 2,
            "host": "SUMMIT-WS01",
            "user": "user1",
            "active": True,
            "last_checkin": "old",
            "payload": {"payloadtype": {"name": "apollo"}},
        },
        {
            "display_id": 3,
            "host": "SUMMIT-WS01",
            "user": "user1",
            "active": True,
            "last_checkin": "new",
            "payload": {"payloadtype": {"name": "apollo"}},
        },
    ]

    matches = deploy.matching_advanced_callback_lanes(
        rows,
        {1: "old-imported", 2: "old"},
        payload_type="apollo",
        host="SUMMIT-WS01",
        user="user1",
    )

    assert [row["display_id"] for row in matches] == [3]


def test_command_launch_existing_rejects_duplicate_settled_callback_lanes(monkeypatch, capsys):
    selected = {
        "display_id": 2,
        "host": "SUMMIT-WS01",
        "user": "user1",
        "active": True,
        "last_checkin": "new-2",
        "payload": {"payloadtype": {"name": "apollo"}},
    }
    duplicate = {
        "display_id": 3,
        "host": "SUMMIT-WS01",
        "user": "user1",
        "active": True,
        "last_checkin": "new-3",
        "payload": {"payloadtype": {"name": "apollo"}},
    }
    disconnects = []

    async def fake_login(args):
        return object()

    async def fake_get_callbacks(client):
        return [{
            "display_id": 1,
            "host": "SUMMIT-WS01",
            "user": "user1",
            "active": False,
            "last_checkin": "old",
            "payload": {"payloadtype": {"name": "apollo"}},
        }]

    async def fake_wait_for_callback_checkin_advance(*args, **kwargs):
        return [selected], selected

    async def fake_wait_for_settled_callback_lanes(*args, **kwargs):
        return [selected, duplicate], [selected, duplicate]

    monkeypatch.setattr(deploy, "mythic_login", fake_login)
    monkeypatch.setattr(deploy, "get_callbacks", fake_get_callbacks)
    monkeypatch.setattr(deploy, "select_ludus_host", lambda args: {})
    monkeypatch.setattr(deploy, "winrm_session", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        deploy,
        "launch_existing_scheduled_task_interactive",
        lambda *args, **kwargs: {"interactive_session": {"session_id": "4"}},
    )
    monkeypatch.setattr(deploy, "wait_for_callback_checkin_advance", fake_wait_for_callback_checkin_advance)
    monkeypatch.setattr(deploy, "wait_for_settled_callback_lanes", fake_wait_for_settled_callback_lanes)
    monkeypatch.setattr(
        deploy,
        "disconnect_interactive_session",
        lambda session, session_id: disconnects.append(session_id) or {"SessionId": session_id},
    )

    args = argparse.Namespace(
        require_unique_callback=True,
        callback_settle_seconds=90,
        winrm_operation_timeout_seconds=45,
        winrm_read_timeout_seconds=75,
        task_name="SageApolloBootstrap",
        run_as_user=r"SUMMIT\user1",
        wait_interactive_session_seconds=120,
        poll_interval=3.0,
        payload_type="apollo",
        callback_host="SUMMIT-WS01",
        callback_user="user1",
        wait_callbacks_seconds=90,
        disconnect_interactive_session=True,
    )

    with pytest.raises(deploy.DeployError, match="Expected exactly one settled active apollo callback lane"):
        asyncio.run(deploy.command_launch_existing(args))

    output = json.loads(capsys.readouterr().out)
    assert [row["display_id"] for row in output["callback_settle"]["matching_advanced_active_callbacks"]] == [2, 3]
    assert disconnects == ["4"]


# --- Ludus addresses a range by its ID, never by its number ----------------------------------------------------

_ACCESSIBLE = [
    {"rangeNumber": 1, "rangeID": "SAGEPOLICY20260712", "accessType": "Direct"},
    {"rangeNumber": 4, "rangeID": "GOADf255df", "accessType": "Direct"},
]


_BASE = "https://ludus.invalid"


def _stub_ludus_http(monkeypatch, paths: list) -> None:
    """Stub only the HTTP exchange under `ludus_get`, so its own range scoping is what the tests exercise."""
    class Response:
        def __init__(self, payload):
            self._raw = json.dumps(payload).encode()

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def urlopen(request, *, timeout, context):
        path = request.full_url[len(_BASE):]
        paths.append(path)
        if path == "/api/v2/ranges/accessible":
            return Response(_ACCESSIBLE)
        return Response({"inventory": {"all": {"hosts": {}}}})

    monkeypatch.setattr(deploy, "ludus_creds", lambda *_args: (_BASE, "unit-key"))
    monkeypatch.setattr(deploy, "metered_ludus_urlopen", urlopen)


def _run_logoff(monkeypatch, argv: list) -> tuple[int, list, list]:
    """Run the CLI's `logoff-user` entry with the command body replaced by an inventory read."""
    paths: list = []
    seen: list = []
    _stub_ludus_http(monkeypatch, paths)

    async def command(args):
        seen.append(args.ludus_range_id)
        deploy.ludus_get("/api/v2/range/ansibleinventory", Path(args.mcp_path), args.ludus_range_id,
                         args.ludus_mcp_server)

    monkeypatch.setattr(deploy, "command_logoff_user", command)
    code = asyncio.run(deploy.async_main(["logoff-user", *argv]))
    return code, paths, seen


@pytest.mark.parametrize("env", [None, "4"])
def test_a_range_number_on_the_cli_reads_the_inventory_of_its_range_id(monkeypatch, env):
    monkeypatch.delenv(deploy.LUDUS_RANGE_ID_ENV, raising=False)
    argv = ["--ludus-range-id", "4"]
    if env is not None:
        monkeypatch.setenv(deploy.LUDUS_RANGE_ID_ENV, env)
        argv = []

    code, paths, seen = _run_logoff(monkeypatch, argv)

    assert code == 0 and seen == ["GOADf255df"]
    assert paths == ["/api/v2/ranges/accessible", "/api/v2/range/ansibleinventory?rangeID=GOADf255df"]


def test_a_range_id_on_the_cli_is_used_without_a_lookup(monkeypatch):
    monkeypatch.delenv(deploy.LUDUS_RANGE_ID_ENV, raising=False)
    code, paths, seen = _run_logoff(monkeypatch, ["--ludus-range-id", "GOADf255df"])

    assert code == 0 and seen == ["GOADf255df"]
    assert paths == ["/api/v2/range/ansibleinventory?rangeID=GOADf255df"]


def test_an_unknown_range_number_fails_before_any_range_request(monkeypatch, capsys):
    monkeypatch.delenv(deploy.LUDUS_RANGE_ID_ENV, raising=False)
    code, paths, seen = _run_logoff(monkeypatch, ["--ludus-range-id", "9"])

    assert code == 2 and seen == []
    assert paths == ["/api/v2/ranges/accessible"]
    assert "GOADf255df (number 4)" in capsys.readouterr().err


def test_a_range_number_never_reaches_the_api_unresolved(monkeypatch):
    with pytest.raises(deploy.DeployError, match="range number"):
        deploy.with_ludus_range_id("/api/v2/range/ansibleinventory", "4")


# --- launch-existing: a new callback ID is the launch's callback, and cleanup runs on every exit ---------------

def _apollo_row(display_id: int, checkin: str, *, active: bool = True, host: str = "CASTELBLACK") -> dict:
    return {"display_id": display_id, "host": host, "user": "samwell.tarly", "active": active,
            "last_checkin": checkin, "payload": {"payloadtype": {"name": "apollo"}}}


def _launch_existing_args(**overrides) -> argparse.Namespace:
    values = dict(
        require_unique_callback=True, callback_settle_seconds=1, winrm_operation_timeout_seconds=45,
        winrm_read_timeout_seconds=75, task_name="SageApolloBootstrap", run_as_user=r"NORTH\samwell.tarly",
        wait_interactive_session_seconds=1, poll_interval=0.25, payload_type="apollo",
        callback_host="CASTELBLACK", callback_user="samwell.tarly", wait_callbacks_seconds=1,
        disconnect_interactive_session=True,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _patch_launch_existing(monkeypatch, censuses: list[list[dict]], *, launch=None, disconnect=None):
    """Drive command_launch_existing through the real selectors over a scripted Mythic callback census."""
    disconnects: list[str] = []
    reads = iter(censuses)
    last: list[list[dict]] = [censuses[0]]

    async def fake_get_callbacks(client):
        last[0] = next(reads, last[0])
        return last[0]

    async def fake_login(args):
        return object()

    clock = {"now": 0.0}

    def monotonic():  # each read advances the launcher's clock, so every wait ends after a few polls
        clock["now"] += 0.5
        return clock["now"]

    def default_launch(session, task_name, run_as_user, **kwargs):
        sink = kwargs.get("session_sink")
        if isinstance(sink, dict):
            sink["session_id"] = "4"
        return {"interactive_session": {"session_id": "4"}}

    def default_disconnect(session, session_id):
        disconnects.append(session_id)
        return {"Method": "tsdiscon", "SessionId": session_id}

    monkeypatch.setattr(deploy, "mythic_login", fake_login)
    monkeypatch.setattr(deploy, "get_callbacks", fake_get_callbacks)
    monkeypatch.setattr(deploy, "select_ludus_host", lambda args: {})
    monkeypatch.setattr(deploy, "winrm_session", lambda *args, **kwargs: object())
    monkeypatch.setattr(deploy, "launch_existing_scheduled_task_interactive", launch or default_launch)
    monkeypatch.setattr(deploy, "disconnect_interactive_session", disconnect or default_disconnect)
    monkeypatch.setattr(deploy, "time", type("_Clock", (), {"monotonic": staticmethod(monotonic),
                                                             "sleep": staticmethod(lambda seconds: None)}))
    return disconnects


def test_launch_existing_accepts_a_new_matching_callback_beside_the_stale_imported_one(monkeypatch, capsys):
    """The staged Apollo checks in as a new callback ID while the imported one stays stale: that new ID is the
    launch's callback (no selection change was needed; the 2026-11-09 failure was a host mismatch)."""
    before = [_apollo_row(1, "import", active=False)]
    after = [_apollo_row(1, "import", active=False), _apollo_row(2, "new-2")]
    disconnects = _patch_launch_existing(monkeypatch, [before, after, after, after])

    asyncio.run(deploy.command_launch_existing(_launch_existing_args()))

    output = json.loads(capsys.readouterr().out)
    assert output["callback"]["display_id"] == 2
    assert [row["display_id"] for row in output["callback_settle"]["matching_advanced_active_callbacks"]] == [2]
    assert disconnects == ["4"]
    assert output["cleanup"]["interactive_session_disconnect"] == "disconnected"


def test_launch_existing_disconnects_the_session_when_no_callback_matches(monkeypatch, capsys):
    """The 2026-11-09 branch: no row matched within the wait. The session it opened is still disconnected, and the
    failure is reported as a launch failure, never masked by the cleanup."""
    before = [_apollo_row(1, "import", active=False)]
    after = [_apollo_row(1, "import", active=False), _apollo_row(2, "new-2")]
    disconnects = _patch_launch_existing(monkeypatch, [before, after])

    with pytest.raises(deploy.DeployError, match="No apollo callback check-in advanced"):
        asyncio.run(deploy.command_launch_existing(_launch_existing_args(callback_host="http://100.108.59.85")))

    output = json.loads(capsys.readouterr().out)
    assert output["callback"] is None
    assert disconnects == ["4"]
    assert output["cleanup"]["interactive_session_disconnect"] == "disconnected"


def test_launch_existing_disconnects_the_session_after_an_exception_following_task_launch(monkeypatch, capsys):
    before = [_apollo_row(1, "import", active=False)]
    disconnects = _patch_launch_existing(monkeypatch, [before])

    async def broken_wait(*args, **kwargs):
        raise RuntimeError("transport dropped during the callback wait")

    monkeypatch.setattr(deploy, "wait_for_callback_checkin_advance", broken_wait)
    with pytest.raises(RuntimeError, match="transport dropped"):
        asyncio.run(deploy.command_launch_existing(_launch_existing_args()))

    assert disconnects == ["4"]
    output = json.loads(capsys.readouterr().out)
    assert output["callback"] is None and output["cleanup"]["interactive_session_disconnect"] == "disconnected"


def test_launch_existing_disconnects_a_session_found_before_a_refused_launch(monkeypatch, capsys):
    before = [_apollo_row(1, "import", active=False)]

    def refused(session, task_name, run_as_user, **kwargs):
        kwargs["session_sink"]["session_id"] = "4"
        raise deploy.DeployError("Task SageApolloBootstrap executable hash does not match; refusing launch.")

    disconnects = _patch_launch_existing(monkeypatch, [before], launch=refused)
    with pytest.raises(deploy.DeployError, match="refusing launch"):
        asyncio.run(deploy.command_launch_existing(_launch_existing_args()))
    assert disconnects == ["4"]


def test_launch_existing_reports_a_live_callback_and_a_failed_cleanup_separately(monkeypatch, capsys):
    before = [_apollo_row(1, "import", active=False)]
    after = [_apollo_row(1, "import", active=False), _apollo_row(2, "new-2")]

    def failing_disconnect(session, session_id):
        raise deploy.DeployError("tsdiscon failed for session 4 with exit code 1")

    _patch_launch_existing(monkeypatch, [before, after, after, after], disconnect=failing_disconnect)
    with pytest.raises(deploy.DeployError, match="callback 2 checked in.*cleanup failed"):
        asyncio.run(deploy.command_launch_existing(_launch_existing_args()))

    output = json.loads(capsys.readouterr().out)
    assert output["callback"]["display_id"] == 2
    assert output["cleanup"]["interactive_session_disconnect"].startswith("failed")


def test_launch_existing_still_reports_duplicate_lanes_after_disconnecting(monkeypatch, capsys):
    before = [_apollo_row(1, "import", active=False)]
    after = [_apollo_row(1, "import", active=False), _apollo_row(2, "new-2"), _apollo_row(3, "new-3")]
    disconnects = _patch_launch_existing(monkeypatch, [before, after, after, after])

    with pytest.raises(deploy.DeployError, match="Expected exactly one settled active apollo callback lane"):
        asyncio.run(deploy.command_launch_existing(_launch_existing_args()))
    assert disconnects == ["4"]
