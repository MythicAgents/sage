"""Resolve Mythic operation identity for the Engagement State Ledger binding.

A native-chat session binds its ledger from its own explicit operation id (`resolve_operation_by_id`) plus the
reset-safe engagement marker stored in Mythic (`get_or_create_engagement_uuid`). The canonical engagement key is
`<SanitizedName>_<id>_<uuid>`; `engagement_store` keeps the first key it saw for an operation identity.

`resolve_operation` (single visible operation, else newest callback) remains only for read scopes such as the
credential store; it is never used to bind a ledger. All functions fail soft (None) and import the `mythic`
library lazily so the module imports in unit tests without a Mythic install.
"""

from __future__ import annotations

import re
import uuid as _uuid

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# A callback carries operation_id + operation{id name}; all of a Sage operator's callbacks share one
# operation, so the newest callback is an unambiguous source. The mythic lib requires NAMED queries.
_OP_FROM_CALLBACK = (
    "query SageOpFromCallback { "
    "callback(limit: 1, order_by: {id: desc}) { operation_id operation { id name } } }"
)
_OP_LIST = "query SageOpList { operation(where: {complete: {_eq: false}}) { id name } }"
_OP_BY_ID = "query SageOperationById($op: Int!) { operation_by_pk(id: $op) { id name } }"

# The Mythic operation id (a Postgres serial) is NOT durable — a `mythic-cli database reset` restarts it at
# 1, so `state_Operation_Chimera_1.json` would be REUSED by a brand-new range after a wipe. To make a clean
# run get a clean ledger, we mint a uuid4 and store it as an operation-event-log marker (it lives in Mythic,
# so a DB reset deletes it). On startup we read the marker; if absent (fresh instance), we generate + store a
# new one. The ledger key becomes `<name>_<id>_<uuid>`; the old `<name>_<id>_<olduuid>.json` is orphaned.
_ENGAGEMENT_MARKER_SOURCE = "sage_engagement_id"
_ENGAGEMENT_MARKER_PREFIX = "Sage durable engagement ledger id: "
_MARKER_READ = (
    "query SageEngagementMarker($op: Int!) { operationeventlog("
    "where: {operation_id: {_eq: $op}, source: {_eq: \"" + _ENGAGEMENT_MARKER_SOURCE + "\"}, "
    "deleted: {_eq: false}}, order_by: {id: asc}, limit: 1) { message } }"
)


def _sanitize(value: str) -> str:
    return _FILENAME_SAFE.sub("_", (value or "").strip())


def operation_key(op_name: str, op_id) -> str:
    """The ledger/engagement key for an operation: `<SanitizedName>_<id>` (e.g. `Operation_Chimera_1`).
    `engagement_ledger.ledger_path` turns this into `state_<key>.json`.
    """
    name = _sanitize(op_name) or "operation"
    return f"{name}_{op_id}"


def _as_op(op_id, op_name) -> tuple[int, str] | None:
    """Coerce a raw (id, name) to (int, str), or None if the id isn't a usable int. Coercing in ONE place
    keeps the agent path (GraphQL int) and the `state` path (taskData int/str) producing identical keys.
    """
    if op_id is None:
        return None
    try:
        return int(op_id), str(op_name or "operation")
    except (TypeError, ValueError):
        return None


async def resolve_operation(client) -> tuple[int, str] | None:
    """Return `(operation_id, operation_name)` for the current Mythic operation, or None if unresolvable.

    Resolution order (correctness over recency): the single non-complete operation visible to the token —
    a Sage payload's token is scoped to exactly its operation, and this works with ZERO callbacks — then,
    only if that is ambiguous (0 or >1 visible ops, e.g. an admin token), anchor on the most recent
    callback's operation. Anchoring on a *global* newest callback alone could misfile under a different
    operation in a shared Mythic, so it is the fallback, not the primary. Never raises.
    """
    if client is None:
        return None
    try:
        from mythic import mythic
    except Exception:
        return None

    ops: list = []
    try:
        resp = await mythic.execute_custom_query(client, _OP_LIST)
        ops = (resp or {}).get("operation") or []
    except Exception:
        ops = []
    if len(ops) == 1:
        resolved = _as_op(ops[0].get("id"), ops[0].get("name"))
        if resolved:
            return resolved

    # Ambiguous (0 or >1 visible operations): tie to recent activity via a callback's own operation.
    try:
        resp = await mythic.execute_custom_query(client, _OP_FROM_CALLBACK)
        callbacks = (resp or {}).get("callback") or []
        if callbacks:
            op = callbacks[0].get("operation") or {}
            resolved = _as_op(op.get("id") if op.get("id") is not None else callbacks[0].get("operation_id"),
                              op.get("name"))
            if resolved:
                return resolved
    except Exception:
        pass

    # Last resort: if multiple ops were visible but no callback anchored us, take the first listed.
    if ops:
        return _as_op(ops[0].get("id"), ops[0].get("name"))
    return None


async def resolve_operation_by_id(client, op_id) -> tuple[int, str] | None:
    """Return ``(operation_id, operation_name)`` for an explicit operation id, or None if unreadable.

    Native-chat sessions know their operation; this never substitutes another operation's callback or the first
    of several visible operations. Never raises.
    """
    if client is None:
        return None
    try:
        from mythic import mythic
    except Exception:
        return None
    try:
        resp = await mythic.execute_custom_query(client, _OP_BY_ID, variables={"op": int(op_id)})
        row = (resp or {}).get("operation_by_pk") or {}
    except Exception:
        return None
    resolved = _as_op(row.get("id"), row.get("name")) if isinstance(row, dict) else None
    if resolved is None or resolved[0] != int(op_id):
        return None
    return resolved


async def _read_earliest_marker(mythic, client, op_id) -> str | None:
    resp = await mythic.execute_custom_query(client, _MARKER_READ, variables={"op": int(op_id)})
    rows = (resp or {}).get("operationeventlog") or []
    if rows:
        found = _UUID_RE.search(str(rows[0].get("message") or ""))
        if found:
            return found.group(0).casefold()
    return None


async def get_or_create_engagement_uuid(client, op_id) -> str | None:
    """The durable Sage engagement UUID for an operation, stored as an `operationeventlog` marker so it is
    wiped on a Mythic DB reset (→ a clean run mints a new UUID → a fresh ledger, never the stale one).

    Reads the EARLIEST marker, creating one if absent, then re-reads the earliest persisted marker so concurrent
    first sessions converge on one identity. Returns None when Mythic is unreachable, the read fails, or no
    persisted marker can be read back: the caller treats that as an unavailable ledger. Never raises.
    """
    if client is None:
        return None
    try:
        from mythic import mythic
    except Exception:
        return None
    try:
        existing = await _read_earliest_marker(mythic, client, op_id)
    except Exception:
        return None
    if existing:
        return existing
    new_uuid = str(_uuid.uuid4())
    try:
        result = await mythic.send_event_log_message(
            client, message=f"{_ENGAGEMENT_MARKER_PREFIX}{new_uuid}", level="info",
            source=_ENGAGEMENT_MARKER_SOURCE,
        )
    except Exception:
        return None
    # createOperationEventLog returns {status, error}; only trust a persisted marker.
    if isinstance(result, dict) and result.get("status") not in (None, "success"):
        return None
    try:
        return await _read_earliest_marker(mythic, client, op_id)
    except Exception:
        return None
