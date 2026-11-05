"""Bind a focused capability script's MythicTools to the target callback's own Mythic operation.

The shared Engagement State Ledger binds from an explicit operation id (never an implicit "single visible
operation" guess). Focused scripts know their target callback, so the callback's operation is the binding.
"""

from __future__ import annotations

_CALLBACK_OPERATION = (
    "query FocusedCallbackOperation($cb: Int!) { "
    "callback(where: {display_id: {_eq: $cb}}, limit: 1) { operation_id } }"
)


async def bound_tools(mythic_tools, client, agent_task_id: str, callback_display_id):
    from mythic import mythic

    resp = await mythic.execute_custom_query(client, _CALLBACK_OPERATION, variables={"cb": int(callback_display_id)})
    rows = (resp or {}).get("callback") or []
    operation_id = rows[0].get("operation_id") if rows else None
    if not operation_id:
        raise SystemExit(f"ERR: callback {callback_display_id} has no readable Mythic operation")
    tools = mythic_tools.MythicTools(agent_task_id=agent_task_id, operation_id=int(operation_id))
    tools.client = client
    await tools._ensure_engagement_key()
    if getattr(tools, "_ledger_binding", None) is None:
        raise SystemExit(f"ERR: {tools._ledger_binding_error or 'engagement ledger could not be bound'}")
    return tools
