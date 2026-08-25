"""Fixed-content Slack egress for operation-findings change notices.

Slack is outside the trusted Mythic/Sage data boundary.  This module therefore
accepts no operation or finding value: configured delivery can only emit the
constant notice below.  Full finding content remains in native Mythic Chat.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable

import aiohttp
from mythic_container.logging import logger


SLACK_FINDINGS_WEBHOOK_ENV = "SAGE_FINDINGS_SLACK_WEBHOOK_URL"
SLACK_FINDINGS_CHANNEL_ID_ENV = "SAGE_FINDINGS_SLACK_CHANNEL_ID"
GENERIC_FINDINGS_CHANGE_NOTICE = "Sage findings changed. Open Mythic to review."
_LEGACY_SLACK_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{8,254}$")


def findings_change_notice_payload() -> dict[str, str]:
    """Build the fixed notice, with an optional legacy Slack channel ID.

    Modern Slack app incoming webhooks are permanently bound to the channel
    selected when the webhook was installed and do not support this override.
    Slack's legacy custom-integration webhooks may honor a ``channel`` field.
    Restricting the optional value to an operator-configured channel ID keeps
    client-derived operation and finding values outside the egress boundary.
    """
    payload = {"text": GENERIC_FINDINGS_CHANGE_NOTICE}
    channel_id = os.environ.get(SLACK_FINDINGS_CHANNEL_ID_ENV, "").strip()
    if not channel_id:
        return payload
    if _LEGACY_SLACK_CHANNEL_ID.fullmatch(channel_id) is None:
        raise ValueError("legacy Slack channel override must be a C/G channel ID")
    payload["channel"] = channel_id
    return payload


async def emit_configured_findings_change_notice(
    *, session_factory: Callable[[], Any] | None = None
) -> bool:
    """Emit one constant notice when a Slack hook is configured.

    The deliberately narrow signature is the redaction boundary: client-derived
    operation/finding values cannot be forwarded because this function does not
    accept them.  The optional legacy channel ID is read only from operator
    configuration. Delivery failures remain fail-soft for the authoritative
    native Mythic notification and never log the webhook URL or exception text.
    """
    webhook_url = os.environ.get(SLACK_FINDINGS_WEBHOOK_ENV, "").strip()
    if not webhook_url:
        return False
    factory = session_factory or aiohttp.ClientSession
    try:
        payload = findings_change_notice_payload()
        async with factory() as session:
            async with session.post(
                webhook_url,
                json=payload,
            ) as response:
                response.raise_for_status()
    except Exception:
        logger.warning(
            "Slack findings-change notice failed; the native Mythic notification "
            "remains authoritative"
        )
        return False
    return True
