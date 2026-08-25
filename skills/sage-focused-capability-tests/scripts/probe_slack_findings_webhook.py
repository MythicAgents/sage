#!/usr/bin/env python3
"""Opt-in probe for Sage's fixed-content Slack findings notification."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import re
import sys
from typing import Awaitable, Callable


REPO_ROOT = Path(__file__).resolve().parents[3]
SAGE_RUNTIME_DIR = REPO_ROOT / "Payload_Type" / "sage"
sys.path.insert(0, str(SAGE_RUNTIME_DIR))

from dotenv_bootstrap import load_sage_dotenv  # noqa: E402
from sage_chat.findings_slack import (  # noqa: E402
    SLACK_FINDINGS_CHANNEL_ID_ENV,
    SLACK_FINDINGS_WEBHOOK_ENV,
    emit_configured_findings_change_notice,
    findings_change_notice_payload,
)


_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
Sender = Callable[[], Awaitable[bool]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send",
        action="store_true",
        help="acknowledge that this sends the real fixed notice to Slack",
    )
    parser.add_argument(
        "--webhook-env",
        default=SLACK_FINDINGS_WEBHOOK_ENV,
        help=(
            "environment variable containing the webhook URL; select a different "
            "channel-bound webhook without putting its secret on the command line"
        ),
    )
    parser.add_argument(
        "--channel-id",
        help=(
            "legacy custom-integration channel ID (C… or G…); modern Slack app "
            "webhooks cannot override their installed channel"
        ),
    )
    return parser


async def run_probe(
    args: argparse.Namespace,
    *,
    sender: Sender = emit_configured_findings_change_notice,
) -> int:
    if not args.send:
        print("Refusing to send without explicit --send acknowledgement.", file=sys.stderr)
        return 2
    if _ENV_NAME.fullmatch(args.webhook_env) is None:
        print("Invalid --webhook-env name.", file=sys.stderr)
        return 2

    load_sage_dotenv(str(SAGE_RUNTIME_DIR))
    webhook_url = os.environ.get(args.webhook_env, "").strip()
    if not webhook_url:
        print(f"No webhook is configured in {args.webhook_env}.", file=sys.stderr)
        return 2

    previous_webhook = os.environ.get(SLACK_FINDINGS_WEBHOOK_ENV)
    previous_channel = os.environ.get(SLACK_FINDINGS_CHANNEL_ID_ENV)
    os.environ[SLACK_FINDINGS_WEBHOOK_ENV] = webhook_url
    if args.channel_id is not None:
        os.environ[SLACK_FINDINGS_CHANNEL_ID_ENV] = args.channel_id

    try:
        try:
            payload = findings_change_notice_payload()
        except ValueError:
            print(
                "Invalid legacy channel ID; use a Slack C… or G… channel ID.",
                file=sys.stderr,
            )
            return 2

        if "channel" in payload:
            print(
                "Legacy channel override requested. Modern Slack app incoming "
                "webhooks remain bound to their installed channel; HTTP success "
                "cannot prove an override was honored."
            )
        elif args.webhook_env != SLACK_FINDINGS_WEBHOOK_ENV:
            print(f"Using the channel-bound webhook from {args.webhook_env}.")
        else:
            print("Using the configured webhook's bound Slack channel.")

        delivered = await sender()
    finally:
        if previous_webhook is None:
            os.environ.pop(SLACK_FINDINGS_WEBHOOK_ENV, None)
        else:
            os.environ[SLACK_FINDINGS_WEBHOOK_ENV] = previous_webhook
        if previous_channel is None:
            os.environ.pop(SLACK_FINDINGS_CHANNEL_ID_ENV, None)
        else:
            os.environ[SLACK_FINDINGS_CHANNEL_ID_ENV] = previous_channel

    if not delivered:
        print("Slack did not accept the fixed findings-change notice.", file=sys.stderr)
        return 1
    print("Slack accepted the fixed findings-change notice.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run_probe(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
