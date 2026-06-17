#!/usr/bin/env python3
"""Focused live smoke for Sage capability command construction.

This does not issue target-agent tasks and does not call the LLM. It logs into Mythic, instantiates the
same MythicTools builder used by Sage, verifies krbtgt material can be selected from Mythic credentials,
then prints a redacted command-plan summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "skills" / "sage-live-runner" / "scripts"))

from mythic import mythic
from sage_task import resolve_password

sys.path.insert(0, str(ROOT / "Payload_Type" / "sage" / "ai" / "langgraph"))

import mythic_tools  # noqa: E402

SERVER = "127.0.0.1"
USER = "mythic_admin"

SECRET_RE = re.compile(r"(?i)(/(?:aes256|aes128|rc4|ntlm|krbtgt_hash):)([0-9a-f]{32,64})")
KEY_NAMES = {"key", "credential", "credential_text", "secret", "aes256", "aes128", "rc4", "ntlm", "krbtgt_hash"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[key] = "<redacted>" if str(key).casefold() in KEY_NAMES and item else _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_RE.sub(lambda m: f"{m.group(1)}<redacted>", value)
    return value


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="sevenkingdoms.local")
    parser.add_argument("--domain-sid", default="S-1-5-21-111-222-333")
    parser.add_argument("--proof-host", default="")
    args = parser.parse_args()

    client = await mythic.login(server_ip=SERVER, username=USER, password=resolve_password())
    tools = mythic_tools.MythicTools(agent_task_id="capability-smoke")
    tools.client = client
    if args.proof_host:
        tools._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result=args.proof_host)

    selected = await tools._select_krbtgt_credential(args.domain)
    if not selected:
        print(f"ERROR: no usable krbtgt credential in Mythic store for {args.domain}")
        return 1

    inputs = {
        "domain_sid": args.domain_sid,
        "domain_sid_source": "focused live smoke input",
        "proof_host": args.proof_host or f"dc.{args.domain}",
    }
    raw = await tools.build_capability_commands(
        {"capability": "forge-golden-ticket", "domain": args.domain},
        inputs,
    )
    plan = json.loads(raw)
    print(
        f"selected krbtgt credential: id={selected.get('id')} "
        f"key_type={selected.get('key_type')} length={len(selected.get('credential') or '')}"
    )
    print(f"builder ok: {plan.get('ok')}")
    if not plan.get("ok"):
        print(json.dumps(_redact(plan), indent=2, sort_keys=True))
        return 1
    print("commands:", ", ".join(str(cmd.get("command")) for cmd in plan.get("commands", [])))
    print("effects:", ", ".join(plan.get("action", {}).get("effects", []) or []))
    forge = next(
        (
            cmd for cmd in plan.get("commands", [])
            if "kerberos_ticket_base64" in (cmd.get("produces") or [])
            or "kerberos_ticket_file" in (cmd.get("produces") or [])
        ),
        {},
    )
    print("forge command:", json.dumps(_redact(forge), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
