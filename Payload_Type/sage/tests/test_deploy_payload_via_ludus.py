import argparse
import asyncio
import importlib.util
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


def test_default_remote_filename_preserves_interactive_payload_name():
    assert deploy.default_remote_filename("apollo.exe", "scheduled-task-interactive") == "apollo.exe"


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
