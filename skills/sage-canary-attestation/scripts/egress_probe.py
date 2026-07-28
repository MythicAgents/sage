#!/usr/bin/env python3
"""
ISC-49R 49R-20 range egress probe.

Falsifier of 49R-20: "a successful connection from the range to an undeclared destination during a
scored canary." This probe measures, from a range host over WinRM (the Ludus ansible-inventory admin
session — the same transport `sync_range_time.py` uses), whether:

  - each DECLARED endpoint (Mythic C2, BloodHound) is reachable, and
  - each UNDECLARED internet destination is REFUSED.

It is read-only: bounded TCP connect attempts, no payload, no mutation, no task. Run it during a scored
canary's lifetime to observe the range's egress posture for that window.

Verdict:
  egress_bounded          — every declared endpoint reachable AND every undeclared destination refused.
  undeclared_reachable    — at least one undeclared destination connected (49R-20 FALSIFIED for this range).
  declared_unreachable    — a declared endpoint was refused (canary transport itself is impaired).
Exit 0 only on egress_bounded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills" / "sage-goad-reset" / "scripts"))
import sync_range_time as srt  # winrm_session(), run_ps(), load_inventory(), flatten_inventory()

# Declared allow-set (host, port, label). Overridable via --declared host:port=label.
DEFAULT_DECLARED = [
    ("100.108.59.85", 80, "mythic-c2"),
    ("ludus.tailbe1dd5.ts.net", 8080, "bloodhound"),
]
# Undeclared destinations that must be refused if egress is bounded.
DEFAULT_UNDECLARED = [
    ("1.1.1.1", 443, "cloudflare-dns-tls"),
    ("8.8.8.8", 53, "google-dns"),
    ("9.9.9.9", 443, "quad9-tls"),
]

# Bounded TCP connect in PowerShell (fast, no Test-NetConnection ICMP/DNS overhead beyond the connect).
PROBE_PS = r"""
$targets = @({targets})
$out = @()
foreach ($t in $targets) {{
  $host_ = $t[0]; $port = [int]$t[1]; $label = $t[2]
  $ok = $false; $err = ''
  try {{
    $c = New-Object System.Net.Sockets.TcpClient
    $iar = $c.BeginConnect($host_, $port, $null, $null)
    $ok = $iar.AsyncWaitHandle.WaitOne({timeout_ms}, $false) -and $c.Connected
    if ($ok) {{ $c.EndConnect($iar) }}
    $c.Close()
  }} catch {{ $err = $_.Exception.Message }}
  $out += [PSCustomObject]@{{ label=$label; host=$host_; port=$port; connected=$ok; error=$err }}
}}
$out | ConvertTo-Json -Compress
"""


def _ps_targets(targets):
    return ",".join("@(%r,%d,%r)" % (h, p, lbl) for (h, p, lbl) in targets).replace("'", "'")


def probe_host(host_row, declared, undeclared, timeout_ms):
    all_targets = list(declared) + list(undeclared)
    script = PROBE_PS.format(targets=_ps_targets(all_targets), timeout_ms=timeout_ms)
    resp = srt.run_ps(srt.winrm_session(host_row), script)
    raw = json.loads(resp["stdout"]) if resp["stdout"].strip() else []
    if isinstance(raw, dict):
        raw = [raw]
    labels_declared = {lbl for (_, _, lbl) in declared}
    declared_results = [r for r in raw if r["label"] in labels_declared]
    undeclared_results = [r for r in raw if r["label"] not in labels_declared]
    return declared_results, undeclared_results


# The GOAD computer name (e.g. CASTELBLACK) is not the Ludus inventory name (GOADf255df-GOAD-SRV02);
# map the well-known GOAD aliases so the probe runs from the intended host, not a fallback.
_GOAD_ALIAS = {"CASTELBLACK": "SRV02", "KINGSLANDING": "DC01", "WINTERFELL": "DC02",
               "MEEREEN": "DC03", "BRAAVOS": "SRV03"}


def select_host(inventory, name_or_ip):
    hosts = srt.windows_hosts(inventory)
    needle = name_or_ip.upper()
    alias = _GOAD_ALIAS.get(needle, needle)
    for h in hosts:
        hn = str(h.get("inventory_hostname", "")).upper()
        if needle in hn or alias in hn or name_or_ip == str(h.get("ansible_host", "")):
            return h
    return hosts[0] if hosts else None


def parse_pairs(items):
    out = []
    for it in items or []:
        # host:port=label
        hp, _, label = it.partition("=")
        host, _, port = hp.rpartition(":")
        out.append((host, int(port), label or f"{host}:{port}"))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="ISC-49R 49R-20 range egress probe (read-only).")
    p.add_argument("--host", default="CASTELBLACK", help="range host substring to probe from")
    p.add_argument("--mcp-path", default=str(REPO / ".mcp.json"))
    p.add_argument("--timeout-ms", type=int, default=3000)
    p.add_argument("--declared", nargs="*", help="override declared allow-set: host:port=label")
    p.add_argument("--undeclared", nargs="*", help="override undeclared set: host:port=label")
    args = p.parse_args(argv)

    declared = parse_pairs(args.declared) or DEFAULT_DECLARED
    undeclared = parse_pairs(args.undeclared) or DEFAULT_UNDECLARED

    inventory = srt.load_inventory(Path(args.mcp_path))
    host_row = select_host(inventory, args.host)
    if host_row is None:
        print(json.dumps({"verdict": "no_host", "error": "no windows host in inventory"}))
        return 2

    declared_results, undeclared_results = probe_host(host_row, declared, undeclared, args.timeout_ms)
    declared_all_reachable = all(r["connected"] for r in declared_results) and bool(declared_results)
    undeclared_any_reachable = any(r["connected"] for r in undeclared_results)

    if undeclared_any_reachable:
        verdict = "undeclared_reachable"       # 49R-20 FALSIFIED for this range
    elif not declared_all_reachable:
        verdict = "declared_unreachable"
    else:
        verdict = "egress_bounded"

    result = {
        "verdict": verdict,
        "probed_from": host_row.get("inventory_hostname"),
        "probed_ip": host_row.get("ansible_host"),
        "declared": declared_results,
        "undeclared": undeclared_results,
        "timeout_ms": args.timeout_ms,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if verdict == "egress_bounded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
