#!/usr/bin/env python3
"""Bounded live proof that a Mythic task reference resolves for a non-default parameter group.

No LLM is used. The script drives Sage's own issue path
(`MythicTools.issue_task_and_waitfor_task_output`) so the schema pre-flight, the parameter
resolver, the credential binder and the parameter-group selection all run exactly as they do in an
autonomous solve — a hand-built `mythic.issue_task` would bypass the very machinery under test.

Why this exists: Mythic expands `@cred:`/`@link:` references by looking a command's parameters up
filtered by parameter group (`rabbitmq/task_reference.go`, `WHERE command_id=$1 AND
parameter_group_name=$2`) and assumes `"Default"` when the task declares no group
(`rabbitmq/util_create_task.go`). The scripting API cannot express a group, so a referenced
parameter living anywhere else was never recognised as reference-bearing: the reference reached the
agent as a literal string and bound nothing. Apollo's `make_token` credential parameter sits in
`credential_store`, which is why every autonomous solve halted at `ensure-account-kerberos-context`
with `Supplied Arguments, []`.

Modes:
  reference    (default) the credential binder rewrites raw material to `@cred:<id>`, and the task
               declares its group. This is the proof.
  raw-control  the binder is neutralized so the raw credential object reaches the wire. This form
               succeeded before the fix and must still succeed after it — a control, not a proof.

A task reporting Mythic status `completed` is NOT evidence of success: a failed agent command
completes too. The verdict below is taken from the decoded agent output only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("SAGE_ENGAGEMENT_GATE", "1")
os.environ.setdefault("SAGE_ENGAGEMENT_STATE_DIR", str(ROOT / "Payload_Type" / "sage" / ".sage_engagement"))

sys.path.insert(0, str(ROOT / "skills" / "sage-live-runner" / "scripts"))
from mythic import mythic  # noqa: E402
from sage_task import resolve_password  # noqa: E402

sys.path.insert(0, str(ROOT / "Payload_Type" / "sage" / "ai" / "langgraph"))

import credential_artifacts  # noqa: E402
import mythic_tools  # noqa: E402

SERVER = "127.0.0.1"
USER = "mythic_admin"

# The two signatures this fix exists to eliminate, quoted from the observed Apollo rejection.
FAILURE_SIGNATURES = (
    "supplied arguments, []",
    "match more than one parameter group",
)
SUCCESS_SIGNATURES = (
    "successfully set primary identity",
    "successfully impersonated",
)
LONG_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/=]{160,})(?![A-Za-z0-9+/=])")


def _redact(text: Any) -> str:
    value = credential_artifacts.redact_credential_material(str(text or ""))
    return LONG_B64_RE.sub("<redacted-base64>", value)


async def _credential_row(tools: "mythic_tools.MythicTools", credential_id: int) -> dict:
    rows = await tools._fetch_credentials_cached(time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    for row in rows or []:
        if int(row.get("id") or 0) == credential_id:
            return row
    raise SystemExit(f"ERROR: credential id {credential_id} not present in the Mythic credential store")


def _admin_password() -> str:
    """`MYTHIC_ADMIN_PASSWORD` first, then `MYTHIC_ENV_PATH`, per AGENTS.md.

    Both are handled by `sage_task.resolve_password`; kept as a named seam so the intent is
    greppable from this script.
    """
    return resolve_password()


async def run(args: argparse.Namespace) -> int:
    client = await mythic.login(server_ip=SERVER, username=USER, password=_admin_password())
    callbacks = await mythic.get_all_active_callbacks(client)
    callback = next(
        (item for item in callbacks if int(item.get("display_id") or 0) == args.callback), {}
    )
    if not callback:
        print(f"ERROR: callback {args.callback} not found or not active")
        return 1
    print(
        f"target callback: cb{args.callback} host={callback.get('host')} "
        f"user={callback.get('user')} payload={(callback.get('payload') or {}).get('payloadtype', {}).get('name')}"
    )

    tools = mythic_tools.MythicTools(agent_task_id="focused-parameter-group-reference")
    tools.client = client
    await tools._ensure_engagement_key()

    row = await _credential_row(tools, args.credential_id)
    print(
        f"credential {args.credential_id}: account={row.get('account')} realm={row.get('realm')} "
        f"type={row.get('type')}"
    )

    # The raw shape Sage's capability adapter builds. In `reference` mode the binder rewrites this
    # to `@cred:<id>` before it reaches the wire — the same rewrite that used to break the task.
    parameters = {
        "credential": {
            "id": str(args.credential_id),
            "account": row.get("account"),
            "realm": row.get("realm"),
            "credential": row.get("credential_text"),
            "type": row.get("type"),
        },
        "netOnly": True,
    }

    if args.mode == "raw-control":
        async def _no_bind(command, params, callback_display_id, **kwargs):
            return params

        tools._bind_mythic_credential_parameters = _no_bind
        print("mode: raw-control — credential binder neutralized, raw object goes to the wire")
    else:
        print("mode: reference — binder rewrites to @cred:<id>, task declares its parameter group")

    output = await tools.issue_task_and_waitfor_task_output(
        args.command, parameters, args.callback, timeout=args.timeout
    )
    # `waitfor_for_task_output` already base64-decodes every `response_text` and returns the
    # aggregated agent output, so this text is the decoded agent response, not a Mythic status.
    text = output.decode(errors="replace") if isinstance(output, (bytes, bytearray)) else str(output)
    print(f"task display_id: {tools._last_issued_task_display_id}")
    print(f"--- decoded agent output ---\n{_redact(text)}\n---")

    lowered = text.casefold()
    hit_failure = [sig for sig in FAILURE_SIGNATURES if sig in lowered]
    hit_success = [sig for sig in SUCCESS_SIGNATURES if sig in lowered]
    verdict = "PASS" if hit_success and not hit_failure else "FAIL"
    print(json.dumps({
        "verdict": verdict,
        "mode": args.mode,
        "command": args.command,
        "callback_display_id": args.callback,
        "credential_id": args.credential_id,
        "task_display_id": tools._last_issued_task_display_id,
        "failure_signatures_present": hit_failure,
        "success_signatures_present": hit_success,
    }, sort_keys=True))

    if args.revert:
        reverted = await tools.issue_task_and_waitfor_task_output("rev2self", {}, args.callback, timeout=args.timeout)
        reverted_text = reverted.decode(errors="replace") if isinstance(reverted, (bytes, bytearray)) else str(reverted)
        print(f"rev2self: {_redact(reverted_text).strip()[:200]}")

    return 0 if verdict == "PASS" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--callback", type=int, default=1, help="callback display_id to task")
    parser.add_argument("--credential-id", type=int, default=12, help="Mythic credential store id to reference")
    parser.add_argument("--command", default="make_token", help="command whose referenced parameter is non-default")
    parser.add_argument("--mode", choices=("reference", "raw-control"), default="reference")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--revert", action="store_true", help="issue rev2self afterwards to restore the token context")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
