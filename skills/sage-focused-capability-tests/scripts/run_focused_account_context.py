#!/usr/bin/env python3
"""Bounded live proof for generic account Kerberos context establishment.

No LLM is used. The script builds Sage's payload-agnostic
`ensure-account-kerberos-context` capability into Mythic commands, executes the
builder sequence from a selected callback, and records the account-scoped
Kerberos context only after both the expected account ticket and service access
are proven.
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
    r"(?im)^.*(?:hash\s+ntlm|\bntlm(?:-\s*\d+)?|aes256[_-]?hmac|aes128[_-]?hmac|"
    r"rc4[_-]?hmac|using\s+aes|using\s+rc4|asrep\s+\(key\)|base64\(key\)).*$"
)
SECRET_FLAG_RE = re.compile(r"(?i)(/(?:aes256|aes128|rc4|ntlm|key):)([0-9a-f]{32,64})")
LONG_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/=]{160,})(?![A-Za-z0-9+/=])")
KEY_NAMES = {"key", "credential", "credential_text", "secret", "aes256", "aes128", "rc4", "ntlm"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if str(key).casefold() in KEY_NAMES and item else _redact(item)
            for key, item in value.items()
        }
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
        "deferred": command.get("deferred"),
        "produces": command.get("produces"),
        "consumes": command.get("consumes"),
    }), sort_keys=True)


def _callback_payload_type(callback: dict[str, Any]) -> str:
    payload = callback.get("payload") if isinstance(callback.get("payload"), dict) else {}
    payload_type = payload.get("payloadtype") if isinstance(payload.get("payloadtype"), dict) else {}
    return str(payload_type.get("name") or callback.get("payloadtype") or "")


def _ticket_cache_has_account(tools: mythic_tools.MythicTools, output: str, account: str, domain: str) -> bool:
    return bool(tools._ticket_cache_output_has_account(output, account, domain))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callback", type=int, default=int(os.environ.get("APOLLO_CB", "13")))
    parser.add_argument("--domain", default="sevenkingdoms.local")
    parser.add_argument("--account", default=os.environ.get("ACCOUNT_CONTEXT_USER", "cersei.lannister"))
    parser.add_argument("--dc", default="kingslanding.sevenkingdoms.local")
    parser.add_argument("--proof-resource", default="")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    proof_resource = args.proof_resource or f"\\\\{args.dc}\\SYSVOL"
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

    tools = mythic_tools.MythicTools(agent_task_id="focused-account-context")
    tools.client = client
    await tools._ensure_engagement_key()
    print(f"engagement: {tools._eng_key()}")
    original_objective = str(engagement_ledger.load(tools._eng_key()).get("objective") or "")

    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target=f"domain={args.domain};account={args.account};callback={args.callback}",
        preconditions=[f"creds:{args.account.casefold()}@{args.domain.casefold()}", f"live-callback:{args.callback}"],
        effects=[f"kerberos-account-context:{args.account.casefold()}@{args.domain.casefold()}@callback:{args.callback}"],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": args.domain,
            "account": args.account,
            "callback_id": str(args.callback),
        },
    )
    raw_plan = await tools.build_capability_commands(action, {
        "dc": args.dc,
        "proof_resource": proof_resource,
    })
    plan = json.loads(raw_plan)
    print(f"builder ok={plan.get('ok')} reason={plan.get('reason')}")
    if not plan.get("ok"):
        print(json.dumps(_redact(plan), indent=2, sort_keys=True))
        return 1

    account_ticket_seen = False
    service_proof_seen = False
    proof_task_id = None
    last_probe: dict[str, Any] = {}
    for command in list(plan.get("commands") or []):
        print(f"\nissuing {_command_summary(command)}")
        output = await tools.issue_task_and_waitfor_task_output(
            str(command.get("command") or ""),
            command.get("parameters"),
            args.callback,
            timeout=args.timeout,
        )
        task_id = tools._last_issued_task_display_id
        expected_probe = str(command.get("expected_probe") or "").casefold()
        print(f"task_id={task_id} expected_probe={expected_probe}")
        print(f"output_tail:\n{_tail(output)}")

        if expected_probe == "extract_account_ticket_cache_probe":
            if _ticket_cache_has_account(tools, str(output or ""), args.account, args.domain):
                account_ticket_seen = True
                print("account_ticket_seen=True")
            continue

        if expected_probe == "extract_kerberos_tgt_artifact":
            if not tools._capability_artifacts.get("kerberos_ticket_base64"):
                print("RESULT: TGT artifact was not extracted; stopping bounded run")
                return 2
            print("ticket_artifact_cached=True")
            continue

        if expected_probe == "extract_account_ticket_probe":
            ticket_probe = dict(credential_artifacts.extract_ticket_probe(output))
            service_proof_seen = bool(ticket_probe.get("ticket_valid") or ticket_probe.get("service_access_proven"))
            last_probe = {
                **ticket_probe,
                "callback_id": str(args.callback),
                "account": args.account.casefold(),
                "domain": args.domain.casefold(),
                "account_ticket_present": account_ticket_seen,
            }
            verification = capabilities.verify_capability("ensure-account-kerberos-context", last_probe)
            print(f"account_context_verdict={verification.verdict} reason={verification.reason}")
            if verification.verdict == "achieved":
                proof_task_id = task_id
                break

    if not (account_ticket_seen and service_proof_seen and proof_task_id):
        print("RESULT: failed to prove account Kerberos context in this bounded run")
        return 2

    verification = tools.record_capability_result(
        action,
        last_probe,
        evidence={
            "source": "focused_account_context",
            "provenance": "run",
            "mythic_task_id": proof_task_id,
            "callback_id": args.callback,
            "account_ticket_seen": True,
            "service_proof_resource": proof_resource,
        },
    )
    if original_objective:
        data = engagement_ledger.load(tools._eng_key())
        data["objective"] = original_objective
        engagement_ledger.save(data, tools._eng_key())
    print(f"record verdict={verification.verdict} reason={verification.reason}")
    print(f"RESULT: achieved kerberos-account-context:{args.account.casefold()}@{args.domain.casefold()}@callback:{args.callback}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
