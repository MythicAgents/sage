#!/usr/bin/env python3
"""Observe one bound Windows process exit through the existing Ludus WinRM path."""

from __future__ import annotations

import argparse
from base64 import b64encode
from datetime import datetime, timezone
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
HELPER_PATH = ROOT / "skills/sage-goad-reset/scripts/sync_range_time.py"
EVENTS = {"armed", "exit", "timeout_still_present", "missed_before_handle", "access_failure", "identity_mismatch", "observer_error"}
TERMINAL = EVENTS - {"armed"}
MAX_LINE = 2048


def _helper():
    spec = importlib.util.spec_from_file_location("sage_range_time_for_exit_observer", HELPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("range inventory helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _utc(value: str) -> str:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("start time needs UTC offset")
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate(args):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.inventory_host):
        raise ValueError("invalid inventory host")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,63}", args.guest_host):
        raise ValueError("invalid guest host")
    ipaddress.ip_address(args.host_ip)
    if not 0 < args.pid <= 2**31 - 1 or not 0 < args.duration <= 300:
        raise ValueError("PID or duration out of bounds")
    if not args.exe_path or len(args.exe_path) > 260 or '\n' in args.exe_path or '\r' in args.exe_path:
        raise ValueError("invalid executable path")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.exe_sha256):
        raise ValueError("invalid executable SHA256")
    args.start_utc = _utc(args.start_utc)
    if not args.output.is_absolute() or args.output.exists():
        raise ValueError("output must be a new absolute scratch path")


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell(args) -> str:
    # Values are fixed typed CLI bindings. No caller-provided PowerShell is accepted.
    return r'''$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$targetPid=PID_VALUE
$wantedComputer=GUEST_VALUE
$wantedPath=PATH_VALUE
$wantedHash=HASH_VALUE
$wantedStart=[DateTime]::Parse(START_VALUE,[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
$waitMs=WAIT_VALUE
$p=$null
function Emit($event,$extra) {
  $record=@{schema=1;event=$event;pid=$targetPid}
  foreach($key in $extra.Keys) {$record[$key]=$extra[$key]}
  [Console]::Out.WriteLine(($record|ConvertTo-Json -Compress -Depth 4))
  [Console]::Out.Flush()
}
try {
  try {$p=[Diagnostics.Process]::GetProcessById($targetPid)}
  catch [ArgumentException] {Emit 'missed_before_handle' @{}; exit 0}
  try {$held=$p.Handle}
  catch [InvalidOperationException] {Emit 'missed_before_handle' @{}; exit 0}
  catch [ComponentModel.Win32Exception] {
    if ($p.HasExited) {Emit 'missed_before_handle' @{}}
    else {Emit 'access_failure' @{}}
    exit 0
  }
  if ($p.HasExited) {Emit 'missed_before_handle' @{}; exit 0}
  $cim=Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $targetPid" -ErrorAction Stop
  if ($null -eq $cim) {Emit 'missed_before_handle' @{}; exit 0}
  $actualPath=[string]$cim.ExecutablePath
  if ([string]::IsNullOrWhiteSpace($actualPath)) {Emit 'access_failure' @{}; exit 0}
  $actualHash=(Get-FileHash -LiteralPath $actualPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
  $actualStart=$cim.CreationDate.ToUniversalTime()
  $handleStart=$p.StartTime.ToUniversalTime()
  if ($env:COMPUTERNAME -ine $wantedComputer -or $actualPath -ine $wantedPath -or
      $actualHash -ne $wantedHash -or $actualStart.Ticks -ne $wantedStart.Ticks -or
      [Math]::Abs($handleStart.Ticks-$actualStart.Ticks) -gt 10000) {
    Emit 'identity_mismatch' @{}; exit 0
  }
  if ($p.HasExited) {Emit 'missed_before_handle' @{}; exit 0}
  Emit 'armed' @{start_utc=$actualStart.ToString('o');exe_sha256=$actualHash}
  if ($p.WaitForExit($waitMs)) {
    $signed=[int]$p.ExitCode
    $unsigned=[BitConverter]::ToUInt32([BitConverter]::GetBytes($signed),0)
    Emit 'exit' @{exit_code_signed=$signed;exit_code_hex=('0x{0:X8}' -f $unsigned);
      exit_utc=$p.ExitTime.ToUniversalTime().ToString('o');start_utc=$actualStart.ToString('o');
      lifetime_ms=[long]($p.ExitTime.ToUniversalTime()-$actualStart).TotalMilliseconds}
  } else {Emit 'timeout_still_present' @{start_utc=$actualStart.ToString('o')}}
} catch [UnauthorizedAccessException] {Emit 'access_failure' @{}}
  catch [ComponentModel.Win32Exception] {Emit 'access_failure' @{}}
  catch {Emit 'observer_error' @{}}
finally {if ($null -ne $p) {$p.Dispose()}}
'''.replace("PID_VALUE", str(args.pid)).replace("GUEST_VALUE", _ps_quote(args.guest_host)).replace(
        "PATH_VALUE", _ps_quote(args.exe_path)).replace("HASH_VALUE", _ps_quote(args.exe_sha256.lower())).replace(
        "START_VALUE", _ps_quote(args.start_utc)).replace("WAIT_VALUE", str(args.duration * 1000))


def _validate_record(raw: bytes, args, armed: bool) -> dict:
    if len(raw) > MAX_LINE:
        raise ValueError("oversized observer output")
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed observer output") from exc
    if not isinstance(record, dict) or record.get("schema") != 1 or record.get("event") not in EVENTS or record.get("pid") != args.pid:
        raise ValueError("invalid observer record")
    event = record["event"]
    required = {"schema", "event", "pid"}
    if event == "armed":
        required |= {"start_utc", "exe_sha256"}
        if armed or record["exe_sha256"] != args.exe_sha256.lower() or _utc(record["start_utc"]) != args.start_utc:
            raise ValueError("invalid armed record")
    elif event == "exit":
        required |= {"start_utc", "exit_utc", "exit_code_signed", "exit_code_hex", "lifetime_ms"}
        if not armed or _utc(record["start_utc"]) != args.start_utc or not isinstance(record["exit_code_signed"], int) or isinstance(record["exit_code_signed"], bool) or not -2**31 <= record["exit_code_signed"] < 2**31 or record["exit_code_hex"] != f"0x{record['exit_code_signed'] & 0xffffffff:08X}" or not isinstance(record["lifetime_ms"], int) or record["lifetime_ms"] < 0:
            raise ValueError("invalid exit record")
        _utc(record["exit_utc"])
    elif event == "timeout_still_present":
        required |= {"start_utc"}
        if not armed or _utc(record["start_utc"]) != args.start_utc:
            raise ValueError("invalid timeout record")
    elif armed:
        raise ValueError("unexpected record after armed")
    if set(record) != required:
        raise ValueError("unknown observer fields")
    return record


def _write(out, event: dict):
    event = dict(event)
    event["controller_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    event["controller_monotonic_ns"] = time.monotonic_ns()
    out.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    out.flush()
    os.fsync(out.fileno())


def observe(args, *, helper=None, session=None):
    validate(args)
    helper = helper or _helper()
    args.range_id = helper.selected_range_id(args.range_id)
    if not args.range_id:
        raise RuntimeError("explicit Ludus range ID required")
    ledger = helper.configured_resource_ledger()
    if ledger is None:
        raise RuntimeError("configured resource ledger required before observer I/O")
    inventory = helper.load_inventory(args.mcp_path, args.range_id, args.mcp_server)
    hosts = helper.flatten_inventory(inventory)
    host = hosts.get(args.inventory_host)
    if host is None or str(host.get("ansible_host")) != args.host_ip or str(host.get("ansible_connection", "winrm")).casefold() != "winrm":
        raise RuntimeError("exact Ludus inventory host/IP/WinRM binding failed")
    ledger.reserve({"guest_commands": 1}, "guest-command-process-exit-observer")
    session = session or helper.winrm_session(host)
    protocol = session.protocol
    shell_id = command_id = None
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        try:
            from winrm.exceptions import WinRMOperationTimeoutError
            shell_id = protocol.open_shell()
            encoded = b64encode(powershell(args).encode("utf-16-le")).decode("ascii")
            command_id = protocol.run_command(shell_id, "powershell", ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])
            buffer = b""
            armed = terminal = False
            deadline = time.monotonic() + args.duration + 45
            while time.monotonic() < deadline:
                try:
                    stdout, stderr, status, done = protocol.get_command_output_raw(shell_id, command_id)
                except WinRMOperationTimeoutError:
                    continue
                if stderr or len(stdout) + len(buffer) > MAX_LINE * 3:
                    raise ValueError("observer output channel invalid")
                buffer += stdout
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    record = _validate_record(line.strip(), args, armed)
                    if terminal:
                        raise ValueError("extra observer record")
                    _write(out, record)
                    armed |= record["event"] == "armed"
                    terminal |= record["event"] in TERMINAL
                if done:
                    if buffer or not terminal or status != 0:
                        raise ValueError("incomplete observer result")
                    return
            raise TimeoutError("observer command exceeded controller deadline")
        except Exception:
            _write(out, {"schema": 1, "event": "transport_error", "pid": args.pid})
            raise
        finally:
            cleanup_failures = []
            if command_id is not None:
                try: protocol.cleanup_command(shell_id, command_id)
                except Exception: cleanup_failures.append("command")
            if shell_id is not None:
                try: protocol.close_shell(shell_id)
                except Exception: cleanup_failures.append("shell")
            if cleanup_failures:
                _write(out, {"schema": 1, "event": "cleanup_failure", "pid": args.pid,
                    "stages": cleanup_failures})
                raise RuntimeError("observer cleanup failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-host", required=True)
    parser.add_argument("--host-ip", required=True)
    parser.add_argument("--guest-host", required=True)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--exe-path", required=True)
    parser.add_argument("--exe-sha256", required=True)
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--duration", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mcp-path", type=Path, default=ROOT / ".mcp.json")
    parser.add_argument("--range-id")
    parser.add_argument("--mcp-server")
    args = parser.parse_args(argv)
    try:
        observe(args)
    except Exception as exc:
        print(f"observer failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
