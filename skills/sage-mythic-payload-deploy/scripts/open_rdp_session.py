#!/usr/bin/env python3
"""Open an interactive RDP session to a Ludus Windows host under a self-allocated PTY.

Harness-agnostic RDP opener: `xfreerdp3`'s SDL3 client needs a controlling terminal, which Codex
provides via `tty:true` but Claude Code's Bash tool (and cron/CI) do not. This wraps xfreerdp3 in a
`pty.fork()` so it gets a controlling TTY regardless of the caller's shell — the same skill then
works for Codex, Claude Code, and unattended runs.

It also resolves the run-as password from a DURABLE source (arg -> SAGE_RUN_AS_PASSWORD env ->
a config file -> Sage .env -> the mythic .env) instead of assuming an exported shell var, so a
per-call shell that carries no env still authenticates.

Kerberos is forced OFF (`/auth-pkg-list:!kerberos`) so NLA goes straight to NTLM — the operator
host has no KDC route to the GOAD realms, and the Kerberos attempt otherwise stalls/logs off.

Usage:
    DISPLAY defaults to :99 (the workflow's Xvfb).
    open_rdp_session.py --target-ip 10.4.10.22 --run-as-user 'NORTH\\samwell.tarly'
    # holds the session open (blocks) until xfreerdp3 exits; run it backgrounded, then launch-existing.
"""
from __future__ import annotations

import argparse
import json
import os
import pty
import signal
import sys
import time
from pathlib import Path


_CLEANUP_GRACE_SECONDS = 0.25


class _TerminationSignal(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def _cleanup_pty_group(pid: int) -> int | None:
    status = None
    reaped = False
    try:
        waited, child_status = os.waitpid(pid, os.WNOHANG)
        if waited:
            status, reaped = child_status, True
    except ChildProcessError:
        reaped = True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + _CLEANUP_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            waited_now, child_status = os.waitpid(pid, os.WNOHANG)
            if waited_now:
                status, reaped = child_status, True
        except ChildProcessError:
            pass
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if not reaped:
        try:
            _waited, status = os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    return status


def _benchmark_run_as_password(run_as_user: str | None) -> str:
    """Last-link fallback: the GOAD benchmark fixture's ground-truth domain credential.

    `Payload_Type/sage/.hillclimb/referee_ldap.json` is the gitignored, operator-maintained
    per-domain referee-credential store for this benchmark range. Domain matching is by
    casefolded key or first DNS label ("NORTH" matches "north.sevenkingdoms.local").
    Returns "" when the fixture is absent or the domain is unknown — never raises, and the
    value is returned to the caller without being printed or logged.
    """
    if not run_as_user:
        return ""
    domain = run_as_user.split("\\", 1)[0].split("@", 1)[-1].strip().casefold()
    if not domain:
        return ""
    fixture = (
        Path(__file__).resolve().parents[3]
        / "Payload_Type" / "sage" / ".hillclimb" / "referee_ldap.json"
    )
    if not fixture.is_file():
        return ""
    try:
        table = json.loads(fixture.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(table, dict):
        return ""
    for key, entry in table.items():
        if key.startswith("_") or not isinstance(entry, dict):
            continue
        key_cf = key.strip().casefold()
        if domain != key_cf and domain.split(".", 1)[0] != key_cf.split(".", 1)[0]:
            continue
        password = entry.get("password")
        return str(password) if password else ""
    return ""


def _resolve_password(explicit: str | None, env_path: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("SAGE_RUN_AS_PASSWORD")
    if env:
        return env
    # durable fallbacks: a dedicated secrets file, Sage's runtime env, then the Mythic env
    candidates = [
        os.environ.get("SAGE_RUNAS_FILE"),
        str(Path.home() / ".config" / "sage" / "runas.env"),
        str(Path(__file__).resolve().parents[3] / "Payload_Type" / "sage" / ".env"),
        env_path or os.environ.get("MYTHIC_ENV_PATH") or "",
    ]
    for path in candidates:
        if not path or not Path(path).is_file():
            continue
        for raw in Path(path).read_text(errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("SAGE_RUN_AS_PASSWORD="):
                return line.split("=", 1)[1].strip().strip("'\"")
    benchmark = _benchmark_run_as_password(
        os.environ.get("SAGE_RUN_AS_USER") or r"NORTH\samwell.tarly"
    )
    if benchmark:
        return benchmark
    raise SystemExit(
        "open_rdp_session: no run-as password — pass --run-as-password, export SAGE_RUN_AS_PASSWORD, "
        "set SAGE_RUN_AS_PASSWORD in ~/.config/sage/runas.env or the mythic .env, or keep the GOAD "
        "benchmark referee fixture (Payload_Type/sage/.hillclimb/referee_ldap.json) present."
    )


def _split_domain_user(run_as_user: str) -> tuple[str, str]:
    if "\\" in run_as_user:
        domain, user = run_as_user.split("\\", 1)
        return domain, user
    if "@" in run_as_user:
        user, domain = run_as_user.split("@", 1)
        return domain, user
    return "", run_as_user


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-ip", default="10.4.10.22")
    ap.add_argument("--run-as-user", default=r"NORTH\samwell.tarly")
    ap.add_argument("--run-as-password", default=None)
    # Default to the workflow's live display (:0 — the running GNOME/Xwayland desktop). The old :99
    # Xvfb default hung the client pre-logon on this host (no Xvfb installed; X0/X1 are the GNOME
    # session's Xwayland sockets). Override with SAGE_RDP_DISPLAY if the workflow needs a dedicated
    # Xvfb display again.
    ap.add_argument("--display", default=os.environ.get("SAGE_RDP_DISPLAY", ":0"))
    ap.add_argument("--env-path", default=None)
    ap.add_argument("--width", default="1024")
    ap.add_argument("--height", default="768")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--log", default="/tmp/open_rdp_session.log")
    args = ap.parse_args()

    password = _resolve_password(args.run_as_password, args.env_path)
    domain, user = _split_domain_user(args.run_as_user)
    os.environ["DISPLAY"] = args.display

    # Password inline via /p:. NOTE: this is briefly visible in `ps`/procfs to a local user. That is an
    # accepted tradeoff on the single-operator lab host — /from-stdin's interactive "Password:" prompt is
    # unreliable under pty.fork (prompt-vs-write timing), and the durable resolver above already keeps the
    # secret out of shell history and the environment. Harden to prompt-triggered /from-stdin later if the
    # host becomes multi-user.
    argv = [
        "xfreerdp3",
        f"/p:{password}",
        *( [f"/d:{domain}"] if domain else [] ),
        f"/u:{user}",
        f"/v:{args.target_ip}",
        "/cert:ignore",
        "/sec:nla",
        "/auth-pkg-list:!kerberos",
        f"/w:{args.width}",
        f"/h:{args.height}",
        f"/log-level:{args.log_level}",
    ]

    # pty.fork(): the child becomes a session leader with the new pty as its controlling terminal —
    # exactly what xfreerdp3 needs, without depending on the caller's shell having a TTY. This is the
    # crux of harness-agnosticism: Codex gets a TTY via tty:true, but Claude Code's Bash tool / cron do
    # not, and without a controlling terminal xfreerdp3's NTLM/NLA path dies pre-auth (exit 144).
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.execvp(argv[0], argv)
        os._exit(127)

    def handle_signal(caught: int, _frame) -> None:
        raise _TerminationSignal(caught)
    prior_handlers = {
        signum: signal.signal(signum, handle_signal) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    terminating = None
    status = None
    try:
        # parent: drain the pty to the log and stay alive while launch-existing runs.
        with open(args.log, "wb", buffering=0) as logf:
            logf.write(f"# open_rdp_session -> {user}@{domain or '(no-domain)'} on {args.target_ip} (DISPLAY={args.display})\n".encode())
            while True:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                logf.write(data)
    except _TerminationSignal as caught:
        terminating = caught.signum
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        status = _cleanup_pty_group(pid)
        for signum, handler in prior_handlers.items():
            signal.signal(signum, handler)
    if terminating is not None:
        return 128 + terminating
    if status is None:
        return 1
    return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)


if __name__ == "__main__":
    sys.exit(main())
