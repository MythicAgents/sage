"""Reconcile verified effects + credential material from completed Mythic task history.

Re-homed (Phase 4) from the deleted PayloadType ``state`` command's ``reconcile`` action so the
chat ``/state reconcile`` slash command can drive it. Pure logic over the Mythic scripting client +
the durable ledger — no PayloadType/task context. The autonomous solve imports credentials on its
own via ``MythicTools._import_capability_credential_material``; this is the operator-facing manual
reconcile ("import what task N discovered") equivalent.

Security note preserved from the original: credential material is parsed from task ``response_text``,
which is attacker-influenceable on a real engagement, so ``_import_reconciled_credentials`` is
DRY-RUN by default — nothing is written to the Mythic credential store without ``apply=True``.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from mythic import mythic

try:
    from ai.langgraph import operation_context
    from ai.langgraph import engagement_state as _es
except ImportError:  # pragma: no cover - import-path fallback
    from . import operation_context  # type: ignore
    from . import engagement_state as _es  # type: ignore

try:
    from ai.langgraph import task_reconciler as _task_reconciler
except Exception:  # pragma: no cover
    _task_reconciler = None

try:
    from ai.langgraph import access_reconciler as _access_reconciler
except Exception:  # pragma: no cover
    _access_reconciler = None


async def _fetch_task_by_display_id(client, task_id: int) -> dict:
    query = """
    query task_by_display_id($id: Int!) {
      task(where: {display_id: {_eq: $id}}, limit: 1) {
        id
        display_id
        command_name
        original_params
        display_params
        status
        completed
        timestamp
        operator { username }
        callback { display_id host user }
      }
    }
    """
    resp = await mythic.execute_custom_query(client, query, variables={"id": int(task_id)})
    rows = resp.get("task", []) if isinstance(resp, dict) else []
    return rows[0] if rows else {}


async def _fetch_task_output_text(client, task_id: int) -> str:
    resp = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=int(task_id))
    if not isinstance(resp, list):
        return str(resp or "")
    chunks: list[str] = []
    for item in resp:
        if not isinstance(item, dict):
            chunks.append(str(item))
            continue
        text = item.get("response_text", "")
        if isinstance(text, bytes):
            chunks.append(text.decode(errors="replace"))
            continue
        raw = str(text or "")
        if raw:
            try:
                chunks.append(base64.b64decode(raw).decode("utf-8", "replace"))
                continue
            except Exception:
                pass
        chunks.append(str(item.get("response") or raw or ""))
    return "\n".join(chunk for chunk in chunks if chunk)


async def _fetch_existing_credentials(client) -> list[dict]:
    try:
        resolved = await operation_context.resolve_operation(client)
        op_id = resolved[0] if resolved else None
    except Exception:
        op_id = None
    if op_id is not None:
        query = """
        query SageStateReadCredentials($op: Int) {
          credential(where: {deleted: {_eq: false}, operation_id: {_eq: $op}}, order_by: {id: desc}, limit: 500) {
            id
            account
            realm
            type
            credential_text
            comment
          }
        }
        """
        variables = {"op": op_id}
    else:
        query = """
        query SageStateReadCredentials {
          credential(where: {deleted: {_eq: false}}, order_by: {id: desc}, limit: 500) {
            id
            account
            realm
            type
            credential_text
            comment
          }
        }
        """
        variables = None
    try:
        resp = await mythic.execute_custom_query(client, query, variables=variables)
        rows = resp.get("credential", []) if isinstance(resp, dict) else []
        return [row for row in rows if isinstance(row, dict)]
    except Exception:
        return []


def _credential_ref(row: dict, material: dict, status: str) -> dict:
    return {
        "id": row.get("id"),
        "account": material.get("account") or row.get("account") or "",
        "realm": material.get("realm") or row.get("realm") or "",
        "secret_type": material.get("secret_type") or "",
        "credential_type": material.get("credential_type") or row.get("type") or "",
        "status": status,
    }


def _find_existing_credential(rows: list[dict], material: dict) -> dict:
    account = str(material.get("account") or "").casefold()
    realm = str(material.get("realm") or "").casefold()
    credential = str(material.get("credential") or "").casefold()
    for row in rows:
        if str(row.get("account") or "").casefold() != account:
            continue
        if str(row.get("realm") or "").casefold() != realm:
            continue
        if str(row.get("credential_text") or "").casefold() != credential:
            continue
        return row
    return {}


async def _import_reconciled_credentials(client, materials: list | tuple, task_id, apply: bool = False) -> tuple[list[dict], list[str]]:
    if not materials:
        return [], []
    existing = await _fetch_existing_credentials(client)
    refs: list[dict] = []
    notes: list[str] = []
    for material in materials:
        if not isinstance(material, dict):
            continue
        credential = str(material.get("credential") or "").strip()
        account = str(material.get("account") or "").strip()
        realm = str(material.get("realm") or "").strip()
        credential_type = str(material.get("credential_type") or "hash").strip() or "hash"
        secret_type = str(material.get("secret_type") or credential_type).strip() or credential_type
        if not credential or not account or not realm:
            continue
        found = _find_existing_credential(existing, material)
        if found:
            refs.append(_credential_ref(found, material, "existing"))
            notes.append(
                f"- credential store reused task {task_id}: {account}@{realm} {secret_type} "
                f"(id={found.get('id')})"
            )
            continue
        if not apply:
            # Dry-run default: the credential is parsed from task `response_text`, which is attacker-
            # influenceable on a real engagement. Do NOT write it to the Mythic credential store without an
            # explicit operator opt-in. Surface what WOULD be written; record nothing.
            notes.append(
                f"- [dry-run] would add credential task {task_id}: {account}@{realm} {secret_type} "
                f"— re-run `/state reconcile {task_id} apply` to write it to the Mythic credential store."
            )
            continue
        comment = f"Sage task-history reconcile from Mythic task {task_id}: {secret_type}"
        try:
            result = await mythic.create_credential(
                client,
                credential=credential,
                account=account,
                realm=realm,
                comment=comment,
                credential_type=credential_type,
            )
        except Exception as exc:
            notes.append(f"- credential store add failed task {task_id}: {account}@{realm} {secret_type} ({exc})")
            continue
        if isinstance(result, dict) and result.get("status") == "success":
            row = {
                "id": result.get("id"),
                "account": account,
                "realm": realm,
                "type": credential_type,
                "credential_text": credential,
                "comment": comment,
            }
            existing.append(row)
            refs.append(_credential_ref(row, material, "added"))
            notes.append(
                f"- credential store added task {task_id}: {account}@{realm} {secret_type} "
                f"(id={result.get('id')})"
            )
        else:
            notes.append(f"- credential store add did not succeed task {task_id}: {account}@{realm} {secret_type}")
    return refs, notes


async def _reconcile_state_footholds_from_client(client) -> list:
    if _access_reconciler is None or client is None:
        return []
    try:
        class _StateAccessShim:
            def __init__(self, mythic_client):
                self.client = mythic_client

            async def get_all_active_callbacks(self):
                callbacks = await mythic.get_all_active_callbacks(self.client)
                return json.dumps(callbacks, default=str)

        now = datetime.now(timezone.utc).isoformat()
        return list(await _access_reconciler.reconcile_access(_StateAccessShim(client), now) or [])
    except Exception:
        return []


def _task_with_foothold_context(task: dict, foothold_by_callback: dict[str, object]) -> dict:
    if not isinstance(task, dict):
        return task
    enriched = dict(task)
    callback = dict(task.get("callback") or {})
    callback_id = (
        callback.get("display_id")
        or callback.get("id")
        or task.get("callback_display_id")
        or task.get("callback_id")
    )
    foothold = foothold_by_callback.get(str(callback_id))
    if foothold is not None:
        forest = str(getattr(foothold, "forest", "") or "")
        identity = str(getattr(foothold, "identity", "") or "")
        if forest and not callback.get("forest"):
            callback["forest"] = forest
        if identity and not callback.get("identity"):
            callback["identity"] = identity
        if identity and not callback.get("user"):
            callback["user"] = identity
    enriched["callback"] = callback
    return enriched


async def _candidate_reconcile_tasks(client, task_id: str, callback_id: str, limit: int) -> list[dict]:
    limit = max(1, min(int(limit or 25), 200))
    if task_id:
        task = await _fetch_task_by_display_id(client, int(task_id))
        return [task] if task else []
    if callback_id:
        rows = await mythic.get_all_tasks(mythic=client, callback_display_id=int(callback_id))
        rows = rows if isinstance(rows, list) else []
        return sorted(rows, key=lambda row: int(row.get("display_id") or row.get("id") or 0), reverse=True)[:limit]

    callbacks = await mythic.get_all_active_callbacks(client)
    out: list[dict] = []
    for callback in (callbacks if isinstance(callbacks, list) else []):
        cbid = callback.get("display_id") or callback.get("id")
        if cbid is None:
            continue
        try:
            rows = await mythic.get_all_tasks(mythic=client, callback_display_id=int(cbid))
            rows = rows if isinstance(rows, list) else []
            out.extend(rows[:limit])
        except Exception:
            continue
    return sorted(out, key=lambda row: int(row.get("display_id") or row.get("id") or 0), reverse=True)[:limit]


async def reconcile_task_history(client, data: dict, task_id: str, callback_id: str, limit: int, now: str, apply: bool = False) -> tuple[dict, list[str]]:
    """Inspect completed Mythic task history, record any achieved modeled effects into the ledger, and
    (dry-run by default) import discovered credential material. Returns (updated_ledger, notes)."""
    if _task_reconciler is None or _es is None:
        return data, ["task reconciliation unavailable in this Sage build."]
    tasks = await _candidate_reconcile_tasks(client, task_id, callback_id, limit)
    if not tasks:
        return data, ["No candidate Mythic tasks found."]
    footholds = await _reconcile_state_footholds_from_client(client)
    foothold_by_callback = {
        str(getattr(foothold, "callback_id", "")): foothold
        for foothold in footholds
        if str(getattr(foothold, "callback_id", ""))
    }

    state = _es.EngagementState(
        objective=str(data.get("objective") or data.get("engagement_id") or ""),
        hops=_es.hops_from_dicts(data.get("hops") or []),
    )
    notes: list[str] = []
    imported = 0
    inspected = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        display_id = task.get("display_id") or task.get("id")
        if display_id is None:
            continue
        inspected += 1
        output = await _fetch_task_output_text(client, int(display_id))
        task_for_reconcile = _task_with_foothold_context(task, foothold_by_callback)
        record = _task_reconciler.reconcile_task(task_for_reconcile, output, now)
        if record is None:
            notes.append(
                f"- skipped task {display_id}: no achieved modeled effect "
                f"(cmd={task.get('command_name')}, output_chars={len(output or '')})"
            )
            continue
        evidence = dict(record.evidence)
        credential_refs, credential_notes = await _import_reconciled_credentials(
            client,
            record.credential_material,
            display_id,
            apply=apply,
        )
        if credential_refs:
            evidence["credential_material_imported"] = True
            evidence["credential_store_refs"] = credential_refs
        if credential_notes:
            notes.extend(credential_notes)
        before = {(hop.technique, hop.target, hop.status) for hop in state.hops}
        state = _es.record_hop_result(
            state,
            record.technique,
            record.target,
            record.status,
            evidence,
            now,
        )
        after = {(hop.technique, hop.target, hop.status) for hop in state.hops}
        imported += 1
        action = "updated" if before == after else "imported"
        notes.append(
            f"- {action} task {display_id}: {record.technique} -> {record.target} "
            f"({state.hops[-1].effect})"
        )

    data["hops"] = _es.hops_to_dicts(state.hops)
    data["updated"] = now
    notes.insert(0, f"Reconciled {imported} achieved effect(s) from {inspected} inspected task(s).")
    return data, notes
