from __future__ import annotations

import importlib.util
import http.client
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/observe_windows_process_exit.py"
spec = importlib.util.spec_from_file_location("observe_windows_process_exit", SCRIPT)
assert spec and spec.loader
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)

START = "2026-10-11T20:00:00Z"
HASH = "a" * 64


def args(tmp_path):
    return SimpleNamespace(inventory_host="range-SRV02", host_ip="10.4.10.22",
        guest_host="CASTELBLACK", pid=524, exe_path=r"C:\Users\Public\apollo.exe",
        exe_sha256=HASH, start_utc=START, duration=5, output=tmp_path / "out.jsonl",
        mcp_path=tmp_path / "mcp.json", range_id="exact-range", mcp_server="ludus")


def record(event, **extra):
    return (json.dumps({"schema": 1, "event": event, "pid": 524, **extra}) + "\n").encode()


ARMED = record("armed", start_utc=START, exe_sha256=HASH)
EXIT = record("exit", start_utc=START, exit_utc="2026-10-11T20:00:03Z",
    exit_code_signed=37, exit_code_hex="0x00000025", lifetime_ms=3000)


class Protocol:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.calls = []

    def open_shell(self):
        self.calls.append("open")
        return "shell"

    def run_command(self, shell, command, argv):
        assert shell == "shell" and command == "powershell"
        assert argv[:3] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
        self.calls.append("run")
        return "command"

    def get_command_output_raw(self, shell, command):
        self.calls.append("receive")
        return next(self.frames)

    def cleanup_command(self, shell, command):
        self.calls.append("cleanup")

    def close_shell(self, shell):
        self.calls.append("close")


class Ledger:
    def __init__(self):
        self.reservations = []

    def reserve(self, amount, operation):
        self.reservations.append((amount, operation))


class Helper:
    def __init__(self, ledger):
        self.ledger = ledger
        self.calls = []

    def configured_resource_ledger(self):
        self.calls.append("ledger")
        return self.ledger

    def selected_range_id(self, value):
        return value

    def load_inventory(self, *args):
        self.calls.append("inventory")
        return {"unused": True}

    def flatten_inventory(self, inventory):
        self.calls.append("flatten")
        return {"range-SRV02": {"ansible_host": "10.4.10.22", "ansible_connection": "winrm"}}

    def winrm_session(self, host):
        self.calls.append("session")
        raise AssertionError("test must pass an injected session")


def execute(tmp_path, frames, ledger=None):
    bound = args(tmp_path)
    helper = Helper(ledger if ledger is not None else Ledger())
    protocol = Protocol(frames)
    observer.observe(bound, helper=helper, session=SimpleNamespace(protocol=protocol))
    return [json.loads(line) for line in bound.output.read_text().splitlines()], protocol, helper


def test_armed_is_durable_before_exit_and_cleanup(tmp_path):
    rows, protocol, helper = execute(tmp_path, [(ARMED[:12], b"", -1, False),
        (ARMED[12:], b"", -1, False), (EXIT, b"", 0, True)])
    assert [row["event"] for row in rows] == ["armed", "exit"]
    assert all("controller_utc" in row and "controller_monotonic_ns" in row for row in rows)
    assert rows[0]["controller_monotonic_ns"] <= rows[1]["controller_monotonic_ns"]
    assert protocol.calls[-2:] == ["cleanup", "close"]
    assert helper.calls[:2] == ["ledger", "inventory"]
    assert rows[1]["exit_code_signed"] == 37


def test_armed_is_fsynced_before_waiting_for_next_receive(tmp_path):
    bound = args(tmp_path)
    class InspectProtocol(Protocol):
        def get_command_output_raw(self, shell, command):
            if self.calls.count("receive") == 1:
                assert [json.loads(line)["event"] for line in bound.output.read_text().splitlines()] == ["armed"]
            return super().get_command_output_raw(shell, command)
    protocol = InspectProtocol([(ARMED, b"", -1, False), (EXIT, b"", 0, True)])
    observer.observe(bound, helper=Helper(Ledger()), session=SimpleNamespace(protocol=protocol))


def test_timeout_still_present(tmp_path):
    rows, _, _ = execute(tmp_path, [(ARMED, b"", -1, False),
        (record("timeout_still_present", start_utc=START), b"", 0, True)])
    assert [row["event"] for row in rows] == ["armed", "timeout_still_present"]


def test_signed_exit_has_exact_unsigned_hex(tmp_path):
    negative = record("exit", start_utc=START, exit_utc="2026-10-11T20:00:03Z",
        exit_code_signed=-1073741819, exit_code_hex="0xC0000005", lifetime_ms=3000)
    rows, _, _ = execute(tmp_path, [(ARMED, b"", -1, False), (negative, b"", 0, True)])
    assert rows[1]["exit_code_signed"] == -1073741819
    assert rows[1]["exit_code_hex"] == "0xC0000005"


@pytest.mark.parametrize("terminal", ["missed_before_handle", "access_failure", "identity_mismatch"])
def test_prearm_terminal(tmp_path, terminal):
    rows, _, _ = execute(tmp_path, [(record(terminal), b"", 0, True)])
    assert [row["event"] for row in rows] == [terminal]


@pytest.mark.parametrize("bad", [b"garbage\n", b"{}\n", b"x" * 2049 + b"\n",
    record("armed", start_utc=START, exe_sha256=HASH, secret="bad")])
def test_rejects_malformed_unknown_or_oversized_output_without_echo(tmp_path, bad):
    bound = args(tmp_path)
    helper = Helper(Ledger())
    protocol = Protocol([(bad, b"", 0, True)])
    with pytest.raises(ValueError):
        observer.observe(bound, helper=helper, session=SimpleNamespace(protocol=protocol))
    assert [json.loads(line)["event"] for line in bound.output.read_text().splitlines()] == ["transport_error"]
    assert "garbage" not in bound.output.read_text() and "secret" not in bound.output.read_text()
    assert protocol.calls[-2:] == ["cleanup", "close"]


def test_missing_ledger_fails_before_inventory_or_session(tmp_path):
    bound = args(tmp_path)
    helper = Helper(None)
    with pytest.raises(RuntimeError, match="configured resource ledger"):
        observer.observe(bound, helper=helper)
    assert helper.calls == ["ledger"]
    assert not bound.output.exists()


def test_missing_range_id_fails_before_inventory_or_session(tmp_path):
    bound = args(tmp_path)
    bound.range_id = None
    helper = Helper(Ledger())
    with pytest.raises(RuntimeError, match="explicit Ludus range ID"):
        observer.observe(bound, helper=helper)
    assert helper.calls == []
    assert not bound.output.exists()


def test_wrong_inventory_identity_fails_before_guest_command(tmp_path):
    bound = args(tmp_path)
    bound.host_ip = "10.4.10.23"
    helper = Helper(Ledger())
    with pytest.raises(RuntimeError, match="binding failed"):
        observer.observe(bound, helper=helper)
    assert helper.ledger.reservations == []
    assert not bound.output.exists()


def test_stderr_is_rejected_and_cleanup_runs(tmp_path):
    bound = args(tmp_path)
    protocol = Protocol([(ARMED, b"private detail", -1, False)])
    with pytest.raises(ValueError):
        observer.observe(bound, helper=Helper(Ledger()), session=SimpleNamespace(protocol=protocol))
    assert "private detail" not in bound.output.read_text()
    assert protocol.calls[-2:] == ["cleanup", "close"]


def test_duplicate_armed_record_is_rejected(tmp_path):
    bound = args(tmp_path)
    protocol = Protocol([(ARMED, b"", -1, False), (ARMED, b"", 0, True)])
    with pytest.raises(ValueError, match="invalid armed"):
        observer.observe(bound, helper=Helper(Ledger()), session=SimpleNamespace(protocol=protocol))
    assert [json.loads(line)["event"] for line in bound.output.read_text().splitlines()] == ["armed", "transport_error"]
    assert protocol.calls[-2:] == ["cleanup", "close"]


def test_open_failure_reports_transport_without_raw_error(tmp_path):
    bound = args(tmp_path)
    class FailingOpen(Protocol):
        def open_shell(self):
            raise RuntimeError("private open content")
    with pytest.raises(RuntimeError, match="private open content"):
        observer.observe(bound, helper=Helper(Ledger()), session=SimpleNamespace(protocol=FailingOpen([])))
    assert [json.loads(line)["event"] for line in bound.output.read_text().splitlines()] == ["transport_error"]
    assert "private" not in bound.output.read_text()


def test_receive_failure_closes_command_and_shell(tmp_path):
    bound = args(tmp_path)
    protocol = Protocol([])
    with pytest.raises(StopIteration):
        observer.observe(bound, helper=Helper(Ledger()), session=SimpleNamespace(protocol=protocol))
    assert [json.loads(line)["event"] for line in bound.output.read_text().splitlines()] == ["transport_error"]
    assert protocol.calls[-2:] == ["cleanup", "close"]


@pytest.mark.parametrize("failure", ["command", "shell"])
def test_cleanup_failure_is_sanitized_and_fails(tmp_path, failure):
    bound = args(tmp_path)
    class FailingProtocol(Protocol):
        def cleanup_command(self, shell, command):
            super().cleanup_command(shell, command)
            if failure == "command": raise RuntimeError("private cleanup content")
        def close_shell(self, shell):
            super().close_shell(shell)
            if failure == "shell": raise RuntimeError("private close content")
    protocol = FailingProtocol([(record("access_failure"), b"", 0, True)])
    with pytest.raises(RuntimeError, match="observer cleanup failed"):
        observer.observe(bound, helper=Helper(Ledger()), session=SimpleNamespace(protocol=protocol))
    rows = [json.loads(line) for line in bound.output.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["access_failure", "cleanup_failure"]
    assert rows[-1]["stages"] == [failure]
    assert "private" not in bound.output.read_text()
    assert protocol.calls[-2:] == ["cleanup", "close"]


def test_real_inventory_helper_charges_control_request_before_fake_dispatch(monkeypatch, tmp_path):
    helper = observer._helper()
    ledger = Ledger()
    monkeypatch.setattr(helper, "configured_resource_ledger", lambda: ledger)
    monkeypatch.setattr(helper, "ludus_creds", lambda *_: ("https://unit.invalid", "inert"))
    body = json.dumps({"all": {"hosts": {"range-SRV02": {
        "ansible_host": "10.4.10.22", "ansible_connection": "winrm"}}}}).encode()
    class FakeSocket:
        def sendall(self, data):
            assert ledger.reservations[0] == ({"control_requests": 1}, "ludus-control-request")
        def makefile(self, *_args, **_kwargs):
            return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                + b"\r\n\r\n" + body)
        def close(self): pass
    monkeypatch.setattr(http.client.HTTPSConnection, "connect",
        lambda connection: setattr(connection, "sock", FakeSocket()))
    bound = args(tmp_path)
    protocol = Protocol([(record("missed_before_handle"), b"", 0, True)])
    observer.observe(bound, helper=helper, session=SimpleNamespace(protocol=protocol))
    assert ledger.reservations == [({"control_requests": 1}, "ludus-control-request"),
        ({"guest_commands": 1}, "guest-command-process-exit-observer")]
