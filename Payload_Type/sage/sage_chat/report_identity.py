"""Strict, transport-bound Mythic engagement identity for retained reports."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from mythic import mythic

from ai.langgraph.operation_context import (
    _ENGAGEMENT_MARKER_PREFIX,
    _ENGAGEMENT_MARKER_SOURCE,
    get_or_create_engagement_uuid,
)


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_IDENTITY = (
    "query SageReportIdentity($op: Int!) { "
    "whoami { status current_operation_id active deleted } "
    "operationeventlog("
    "where: {operation_id: {_eq: $op}, source: {_eq: \"" + _ENGAGEMENT_MARKER_SOURCE + "\"}, "
    "deleted: {_eq: false}}, order_by: {id: asc}, limit: 1) { message } }"
)


async def canonical_engagement_uuid(client: Any, operation_id: Any, *, create: bool = False) -> str | None:
    """Return the exact current operation's persisted earliest marker, or fail closed.

    Creation is only for a native collection. The newly minted value is never trusted
    until the exact-operation canonical readback converges.
    """
    if client is None or type(operation_id) is not int or operation_id <= 0:
        return None
    try:
        async def read() -> tuple[bool, str | None]:
            result = await asyncio.wait_for(mythic.execute_custom_query(
                client, _IDENTITY, variables={"op": operation_id}), timeout=5)
            row = result.get("whoami") if isinstance(result, dict) else None
            if (not isinstance(row, dict) or row.get("status") != "success"
                    or row.get("active") is not True or row.get("deleted") is True
                    or type(row.get("current_operation_id")) is not int
                    or row["current_operation_id"] != operation_id):
                return False, None
            rows = result.get("operationeventlog") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                return False, None
            if not rows:
                return True, None
            message = rows[0].get("message") if isinstance(rows[0], dict) else None
            if not isinstance(message, str) or not message.startswith(_ENGAGEMENT_MARKER_PREFIX):
                return False, None
            value = message[len(_ENGAGEMENT_MARKER_PREFIX):]
            return (bool(_UUID.fullmatch(value)), value if _UUID.fullmatch(value) else None)
        valid, value = await read()
        if not valid or value or not create:
            return value if valid else None
        await asyncio.wait_for(get_or_create_engagement_uuid(client, operation_id), timeout=5)
        valid, value = await read()
        return value if valid else None
    except Exception:
        return None
