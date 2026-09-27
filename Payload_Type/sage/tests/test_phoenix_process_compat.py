from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from ai.phoenix_process_compat import (
    PhoenixProcessStartupError,
    child_startup_diagnostic,
    compatible_command,
    launch_capture_phoenix,
    project_child_output,
)


def _installed_command() -> list[str]:
    from phoenix.services import AppService

    service = object.__new__(AppService)
    service.database_url = "sqlite:////tmp/parser-only.db"
    service.host = "127.0.0.1"
    service.port = 6006
    service.root_path = ""
    return service.command


def test_installed_producer_rejects_and_compatible_command_reaches_real_parser(monkeypatch):
    from phoenix.server import main as entry
    from phoenix.server.cli.commands import serve

    reached = []
    monkeypatch.setattr(entry, "initialize_settings", lambda: None)
    monkeypatch.setattr(entry, "setup_logging", lambda: None)
    monkeypatch.setattr(serve, "run", lambda args: reached.append(args))
    original = _installed_command()

    monkeypatch.setattr(sys, "argv", original[1:])
    with pytest.raises(SystemExit, match="2"):
        entry.main()
    assert reached == []

    corrected = compatible_command(original)
    monkeypatch.setattr(sys, "argv", corrected[corrected.index("main.py"):])
    entry.main()
    assert len(reached) == 1

    assert compatible_command(corrected) == corrected
    with pytest.raises(PhoenixProcessStartupError, match="shape is unsupported"):
        compatible_command(original[:-1] + ["trace-fixture"])


class _StubProcess:
    def __init__(self, *, active: bool, output: str = "", returncode: int = 2) -> None:
        self.running = active
        self.returncode = None if active else returncode
        self.stdout = io.StringIO(output)

    def is_running(self):
        return self.running

    def status(self):
        return psutil.STATUS_RUNNING if self.running else psutil.STATUS_ZOMBIE

    def poll(self):
        return self.returncode

    def terminate(self):
        self.running = False
        self.returncode = 0


def _stub_process_effect(monkeypatch, process: _StubProcess) -> None:
    import phoenix.services as services
    import phoenix.session.session as session_module

    launched = False
    calls = []
    monkeypatch.setattr(
        services, "get_running_pid", lambda: 123 if launched and process.running else None
    )
    def popen(*args, **kwargs):
        nonlocal launched
        launched = True
        calls.append((args, kwargs))
        return process
    monkeypatch.setattr(services.psutil, "Popen", popen)
    monkeypatch.setattr(
        services.Service, "stop_any",
        lambda: (_ for _ in ()).throw(AssertionError("must not stop another service")),
    )
    monkeypatch.setattr(session_module, "_session", None)
    monkeypatch.setattr(
        session_module, "get_env_database_connection_str",
        lambda: "sqlite:////tmp/parser-only.db",
    )
    return calls


def test_launch_uses_installed_process_session_and_stop_lifecycle(monkeypatch):
    import phoenix as px
    import phoenix.session.session as session_module

    process = _StubProcess(active=True)
    popen_calls = _stub_process_effect(monkeypatch, process)
    installed = session_module.AppService
    session = launch_capture_phoenix(px)
    assert isinstance(session, session_module.ProcessSession)
    assert session_module.AppService is installed
    argv = popen_calls[0][0][0]
    assert argv[1:4] == ["-P", "main.py", "serve"] and argv[-2:] == ["--port", "6006"]
    assert popen_calls[0][1]["cwd"] == installed.working_dir
    assert popen_calls[0][1]["env"] == dict(__import__("os").environ)
    session.end()
    assert process.running is False


def test_exited_child_diagnostic_is_bounded_and_redacted(monkeypatch):
    import phoenix as px
    from ai import observation_channel

    secret = "SYNTHETIC_SECRET_TOKEN"
    process = _StubProcess(active=False, output=f"usage: phoenix\nphoenix: error: bad flag\n{secret}")
    _stub_process_effect(monkeypatch, process)
    events = []
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append((kind, payload)) or True)

    with pytest.raises(PhoenixProcessStartupError, match="process failed to start") as exc:
        launch_capture_phoenix(px)
    assert secret not in str(exc.value) and secret not in repr(events)
    assert events[0][0] == "tool_event"
    assert events[0][1] == {
        "event": "phoenix_process_startup_diagnostic",
        "status": "failure",
        "error_category": "argument_parser_rejected",
        "returncode": 2,
        "output_sha256": "0cad60fe995563f31134d0e3538403d028b787968ae72a33129a276933a2ba17",
        "exception_class": "SystemExit",
        "frames": [],
        "output_bytes": 62,
        "retained_bytes": 62,
        "output_complete": True,
        "alive": False,
        "lifecycle_status": "zombie",
        "lifecycle_running": False,
    }


def test_exit_two_without_argparse_signature_is_child_exit(monkeypatch):
    import phoenix as px
    from ai import observation_channel

    process = _StubProcess(active=False, output="unrecognized startup failure")
    _stub_process_effect(monkeypatch, process)
    events = []
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append(payload) or True)
    with pytest.raises(PhoenixProcessStartupError):
        launch_capture_phoenix(px)
    assert events[0]["error_category"] == "child_exited"


@pytest.mark.parametrize(("name", "expected"), [
    ("ModuleNotFoundError", "dependency_import_error"),
    ("OperationalError", "database_startup_error"),
    ("RuntimeError", "startup_error"),
    ("InventedPrivateError", "child_exited"),
])
def test_child_projection_allowlists_exception_and_source_bound_frames(name, expected):
    source = Path(__file__).parents[1] / "ai" / "phoenix_process_compat.py"
    secret = b"SYNTHETIC_SECRET_TOKEN"
    output = (b'Traceback:\n  File "' + str(source).encode() + b'", line 34, in <module>\n'
              b"    private source line " + secret + b"\n" + name.encode() + b": " + secret)
    result = project_child_output(output, 1)
    assert secret.decode() not in repr(result)
    assert result["error_category"] == expected
    assert result["exception_class"] == (name if name != "InventedPrivateError" else "unknown")
    assert result["frames"] == [{"relative_path": "Payload_Type/sage/ai/phoenix_process_compat.py",
        "line": 34, "source_sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest()}]


def test_preexisting_pid_entry_fails_before_stop_any_or_spawn(monkeypatch, tmp_path):
    import phoenix as px
    import phoenix.services as services
    import phoenix.session.session as session_module
    from ai import observation_channel

    pid_dir = tmp_path / "pids"; pid_dir.mkdir(); (pid_dir / "lock").write_text("")
    monkeypatch.setattr(services, "get_pids_path", lambda: pid_dir)
    monkeypatch.setattr(services.Service, "stop_any", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(services.psutil, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(session_module, "_session", None)
    monkeypatch.setattr(session_module, "get_env_database_connection_str", lambda: "sqlite:////tmp/unused.db")
    events = []; monkeypatch.setattr(observation_channel, "emit", lambda _k, p: events.append(p) or True)
    with pytest.raises(PhoenixProcessStartupError) as exc:
        launch_capture_phoenix(px)
    assert exc.value.diagnostic is not None
    assert events[0]["error_category"] == "preexisting_pid_entry"


def test_projection_rejects_history_nonpython_and_invalid_line(tmp_path):
    private = Path(__file__).parents[3] / ".sage_history" / "private.py"
    nonpython = tmp_path / "private.env"; nonpython.write_text("secret")
    source = Path(__file__).parents[1] / "ai" / "phoenix_process_compat.py"
    output = (f'File "{private}", line 1\nFile "{nonpython}", line 1\n'
              f'File "{source}", line 999999\nRuntimeError: SYNTHETIC_SECRET').encode()
    result = project_child_output(output, 1)
    assert result["frames"] == [] and result["exception_class"] == "RuntimeError"


def test_real_child_failure_projects_without_message_or_private_path():
    secret = "SYNTHETIC_SECRET_TOKEN"
    child = subprocess.run(
        [sys.executable, "-c", f"raise ModuleNotFoundError({secret!r})"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=5, check=False,
    )
    result = project_child_output(child.stdout, child.returncode)
    assert result["exception_class"] == "ModuleNotFoundError"
    assert result["error_category"] == "dependency_import_error"
    assert secret not in repr(result) and all(not Path(row["relative_path"]).is_absolute()
                                              for row in result["frames"])


def test_isolated_interpreter_blocks_installed_server_stdlib_shadowing():
    from phoenix.config import SERVER_DIR
    plain = subprocess.run([sys.executable, "-c", "import argparse"], cwd=SERVER_DIR,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    isolated = subprocess.run([sys.executable, "-P", "-c", "import argparse"], cwd=SERVER_DIR,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    assert plain.returncode == 1 and isolated.returncode == 0


def test_start_drains_large_output_before_pid_wait_and_shutdown_joins(monkeypatch, tmp_path):
    import phoenix as px
    import phoenix.services as services
    import phoenix.session.session as session_module

    sentinel = tmp_path / "started"; pid_dir = tmp_path / "pids"; pid_dir.mkdir()
    real_popen = services.psutil.Popen; launched = None
    secret = "SYNTHETIC_SECRET_TOKEN"
    script = ("import os,sys,time;os.write(1,(%r+'x'*200000).encode());"
              "open(%r,'w').write('ready');time.sleep(30)") % (secret, str(sentinel))
    def popen(*_args, **_kwargs):
        nonlocal launched
        launched = real_popen([sys.executable, "-P", "-c", script], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        return launched
    monkeypatch.setattr(services, "get_pids_path", lambda: pid_dir)
    monkeypatch.setattr(services, "get_running_pid", lambda: 1 if launched and launched.is_running()
                        and sentinel.exists() else None)
    monkeypatch.setattr(services.psutil, "Popen", popen)
    monkeypatch.setattr(session_module, "_session", None)
    monkeypatch.setattr(session_module, "get_env_database_connection_str", lambda: "sqlite:////tmp/unused.db")
    session = launch_capture_phoenix(px)
    started = time.monotonic()
    diagnostic = child_startup_diagnostic(launched, category="alive_but_unready")
    assert time.monotonic() - started < .5
    assert sentinel.exists() and diagnostic["alive"] is True
    assert diagnostic["output_bytes"] > 65536 and diagnostic["retained_bytes"] == 65536
    assert secret not in repr(diagnostic) and diagnostic["output_complete"] is False
    session.end()
    diagnostic = child_startup_diagnostic(launched)
    assert diagnostic["alive"] is False and diagnostic["output_complete"] is True


def test_inactive_launch_reaps_before_complete_redacted_projection(monkeypatch, tmp_path):
    import phoenix as px
    import phoenix.services as services
    import phoenix.session.session as session_module

    pid_dir = tmp_path / "pids"; pid_dir.mkdir(); launched = None
    real_popen = services.psutil.Popen
    secret = "SYNTHETIC_SECRET_TOKEN"
    source = Path(__file__).parents[1] / "ai" / "phoenix_process_compat.py"
    script = ("import os;os.write(1,b'x'*70000);"
              "exec(compile(\"raise RuntimeError(%r)\",%r,'exec'))") % (secret, str(source))
    def popen(*_args, **_kwargs):
        nonlocal launched
        launched = real_popen([sys.executable, "-P", "-c", script], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        return launched
    monkeypatch.setattr(services, "get_pids_path", lambda: pid_dir)
    monkeypatch.setattr(services, "get_running_pid", lambda: None)
    monkeypatch.setattr(services.psutil, "Popen", popen)
    monkeypatch.setattr(session_module, "_session", None)
    monkeypatch.setattr(session_module, "get_env_database_connection_str",
                        lambda: "sqlite:////tmp/unused.db")
    with pytest.raises(PhoenixProcessStartupError) as caught:
        launch_capture_phoenix(px)
    result = caught.value.diagnostic
    assert result is not None and result["returncode"] == 1
    assert result["lifecycle_status"] == "zombie" and result["alive"] is False
    assert result["exception_class"] == "RuntimeError" and result["output_complete"] is True
    assert result["output_bytes"] > 65536 and result["retained_bytes"] == 65536
    assert result["frames"] == [{"relative_path": "Payload_Type/sage/ai/phoenix_process_compat.py",
        "line": 1, "source_sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest()}]
    assert secret not in repr(result)
