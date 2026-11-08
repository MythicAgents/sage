"""Reconcile verified effects from the operation's completed Mythic task history.

One reconciliation pass reads the operation's tasks above a stored cursor, from every callback and every issuer,
and sends each completed task through the verifiers: the modeled-technique task verifiers here, plus the callback
and credential verifiers the caller runs. It serves the end-of-turn reconciliation and the chat ``/state reconcile``
command, which can also cite single tasks. Pure logic over the Mythic scripting client and the caller's ledger
projection; the caller commits the rows and the cursor in one store transaction.

Security note: credential material is parsed from task ``response_text``, which is attacker-influenceable on a real
engagement, so ``_import_reconciled_credentials`` is DRY-RUN by default: nothing is written to the Mythic credential
store without ``apply=True``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field

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

try:
    from ai.langgraph import task_evidence as _task_evidence
except ImportError:  # pragma: no cover - import-path fallback
    from . import task_evidence as _task_evidence  # type: ignore


PASS_LIMIT = 50
HELD_LIMIT = 50
CREDIT_TASK_LIMIT = 200

_TASK_FIELDS = """
        id
        display_id
        operation_id
        command_name
        original_params
        display_params
        status
        completed
        status_timestamp_processing
        status_timestamp_processed
        timestamp
        operator { username }
        callback { display_id host user domain integrity_level agent_callback_id init_callback }
"""
_CREDENTIAL_FIELDS = (
    "id operation_id account realm type credential_text comment timestamp "
    "task { display_id operation_id status completed status_timestamp_processing status_timestamp_processed "
    "command_name original_params callback { display_id } }"
)


def _rows(resp, key: str) -> list[dict]:
    rows = resp.get(key) if isinstance(resp, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"Mythic returned no {key} list")
    return [row for row in rows if isinstance(row, dict)]


async def fetch_operation_tasks(client, operation_id, cursor: int, held, limit: int = PASS_LIMIT) -> list[dict]:
    """One query: the operation's held tasks plus up to ``limit`` tasks above the cursor, ascending.

    Every issuer and every callback, including tasks Sage did not issue. A transport error raises, so the caller
    skips the pass and the cursor does not move.
    """
    held = sorted({int(item) for item in held or ()})
    where = "{_or: [{display_id: {_gt: $cursor}}, {display_id: {_in: $held}}]%s}" % (
        ", operation_id: {_eq: $op}" if operation_id is not None else "")
    params = "$cursor: Int!, $held: [Int!]!, $limit: Int!" + (", $op: Int!" if operation_id is not None else "")
    query = (f"query SageReconcileTasks({params}) {{ task(where: {where}, order_by: {{display_id: asc}}, "
             f"limit: $limit) {{ {_TASK_FIELDS} }} }}")
    variables = {"cursor": int(cursor), "held": held, "limit": int(limit) + len(held)}
    if operation_id is not None:
        variables["op"] = int(operation_id)
    resp = await mythic.execute_custom_query(client, query, variables=variables)
    return _rows(resp, "task")


async def fetch_cited_task(client, operation_id, display_id: int) -> tuple[dict, str]:
    """The cited task in this operation, or ("", reason). Task display IDs are unique only within an operation."""
    query = f"query SageCitedTask($id: Int!) {{ task(where: {{display_id: {{_eq: $id}}}}) {{ {_TASK_FIELDS} }} }}"
    rows = _rows(await mythic.execute_custom_query(client, query, variables={"id": int(display_id)}), "task")
    if operation_id is not None:
        matching = [row for row in rows if row.get("operation_id") == operation_id]
    else:
        matching = rows
    if matching:
        return matching[0], ""
    return {}, ("not in this operation" if rows else "no such task")


async def fetch_task_credentials(client, operation_id, task_ids) -> list[dict]:
    """The operation's credential-store entries produced by these tasks."""
    ids = sorted({int(item) for item in task_ids or ()})
    if not ids:
        return []
    if operation_id is not None:
        query = ("query SageTaskCredentials($ids: [Int!]!, $op: Int!) { credential(where: {deleted: {_eq: false}, "
                 "operation_id: {_eq: $op}, task: {display_id: {_in: $ids}, operation_id: {_eq: $op}}}, "
                 f"order_by: {{id: asc}}) {{ {_CREDENTIAL_FIELDS} }} }}")
        variables = {"ids": ids, "op": int(operation_id)}
    else:
        query = ("query SageTaskCredentials($ids: [Int!]!) { credential(where: {deleted: {_eq: false}, "
                 f"task: {{display_id: {{_in: $ids}}}}}}, order_by: {{id: asc}}) {{ {_CREDENTIAL_FIELDS} }} }}")
        variables = {"ids": ids}
    return _rows(await mythic.execute_custom_query(client, query, variables=variables), "credential")


async def fetch_cited_credential(client, operation_id, credential_id: int) -> tuple[dict, str]:
    """The cited credential-store entry in this operation, or ({}, reason)."""
    query = ("query SageCitedCredential($id: Int!) { credential(where: {id: {_eq: $id}, deleted: {_eq: false}}) "
             f"{{ {_CREDENTIAL_FIELDS} }} }}")
    rows = _rows(await mythic.execute_custom_query(client, query, variables={"id": int(credential_id)}), "credential")
    matching = [row for row in rows if operation_id is None or row.get("operation_id") == operation_id]
    if matching:
        return matching[0], ""
    return {}, ("not in this operation" if rows else "no such credential")


async def fetch_callback_credit_tasks(client, operation_id, since: str, until: str, callback_ids) -> list[dict]:
    """Operation tasks that may be credited with a callback gain: tasks created in the attribution window, and the
    latest tasks on the gained callbacks. Any issuer.

    A task's issue time is when Mythic created its record (``status_timestamp_preprocessing``). ``timestamp`` is the
    task's last update, which moves with every response, so it is neither filtered on nor selected.
    """
    op_filter = ", operation_id: {_eq: $op}" if operation_id is not None else ""
    op_param = ", $op: Int!" if operation_id is not None else ""
    fields = "display_id status completed command_name status_timestamp_preprocessing callback { display_id }"
    query = (
        f"query SageCreditTasks($since: timestamp!, $until: timestamp!, $cbs: [Int!]!, $limit: Int!{op_param}) {{ "
        f"window: task(where: {{status_timestamp_preprocessing: {{_gte: $since, _lte: $until}}{op_filter}}}, "
        f"order_by: {{display_id: desc}}, limit: $limit) {{ {fields} }} "
        f"on_callback: task(where: {{callback: {{display_id: {{_in: $cbs}}}}{op_filter}}}, "
        f"order_by: {{display_id: desc}}, limit: $limit) {{ {fields} }} }}"
    )
    variables = {"since": since, "until": until, "cbs": sorted({int(c) for c in callback_ids or ()}),
                 "limit": CREDIT_TASK_LIMIT}
    if operation_id is not None:
        variables["op"] = int(operation_id)
    resp = await mythic.execute_custom_query(client, query, variables=variables)
    merged: dict[int, dict] = {}
    for key in ("window", "on_callback"):
        for row in _rows(resp, key):
            display_id = row.get("display_id")
            if type(display_id) is int:
                merged[display_id] = row
    return [merged[key] for key in sorted(merged)]


@dataclass(frozen=True)
class CursorStep:
    """What one pass evaluates and where it leaves the cursor."""

    cursor: int
    held_added: tuple[int, ...]
    held_resolved: tuple[int, ...]
    evaluate: tuple[dict, ...]


def is_terminal(task: dict) -> bool:
    """A task that finished, or one the operator cleared from the queue (it will never run).

    A task Mythic is still finishing (``completed`` with a settling status such as its completion function running)
    is not terminal under the shared task classification: its final status and any credentials the completion
    function stores are still to come, so the pass holds it and reads it again instead of evaluating it now.
    """
    status = str(task.get("status") or "").strip().casefold()
    if status == "cleared":
        return True
    if status in _task_evidence.MYTHIC_SETTLING_STATES:
        return False
    return bool(_task_reconciler and _task_reconciler._is_terminal(task))


def plan_cursor(cursor: int, held, tasks, *, held_limit: int = HELD_LIMIT) -> CursorStep:
    """Advance across the batch: a completed task is evaluated, a running one is held and read again next pass.

    The cursor passes a running task only while the held list has room; past that bound it stops below the task.
    A held task that is now terminal is evaluated and resolved; one Mythic no longer returns is resolved too.
    """
    by_id = {int(task["display_id"]): task for task in tasks if type(task.get("display_id")) is int}
    held = sorted({int(item) for item in held or ()})
    resolved = [item for item in held if item not in by_id or is_terminal(by_id[item])]
    evaluate = [by_id[item] for item in held if item in by_id and is_terminal(by_id[item])]
    still_held = len(held) - len(resolved)
    added: list[int] = []
    position = int(cursor)
    for display_id in sorted(item for item in by_id if item > int(cursor)):
        task = by_id[display_id]
        if is_terminal(task):
            evaluate.append(task)
        elif still_held + len(added) < held_limit:
            added.append(display_id)
        else:
            break
        position = display_id
    return CursorStep(position, tuple(added), tuple(resolved), tuple(evaluate))


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


def _reconcile_mutation_id(engagement_id: str, display_id, identity: str, evidence: dict) -> str:
    """Stable per (operation, task, row, verified result): replaying the same evidence is idempotent."""
    proof = evidence.get("proof_envelope") if isinstance(evidence.get("proof_envelope"), dict) else {}
    commitment = str(proof.get("verifier_result_sha256") or "")
    if not commitment:
        stable = {k: v for k, v in evidence.items() if k not in {"timestamp", "captured_at", "provenance"}}
        commitment = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
    return hashlib.sha256(
        f"reconcile|{engagement_id}|{display_id}|{identity}|{commitment}".encode()
    ).hexdigest()


@dataclass(frozen=True)
class VerifiedTask:
    """A modeled technique a completed task proved, with its evidence and credential-import notes."""

    technique: str
    target: str
    evidence: dict
    notes: tuple[str, ...]
    # The task output, held in memory only for a task whose sole failure was Mythic's completion function, so the
    # caller can run the credential review over it. Never persisted, logged or rendered.
    review_output: str = field(default="", repr=False, compare=False)


async def verify_task(client, task: dict, now: str, *, engagement_id: str, foothold_by_callback: dict,
                      apply: bool = False) -> VerifiedTask | None:
    """Run one completed task through the modeled-technique verifiers, whoever issued it; None if it proves none.

    Credential material the output carries is imported into the Mythic credential store only with ``apply``.
    """
    verified, _observations = await verify_and_observe_task(
        client, task, now, engagement_id=engagement_id, foothold_by_callback=foothold_by_callback, apply=apply)
    return verified


async def verify_and_observe_task(client, task: dict, now: str, *, engagement_id: str, foothold_by_callback: dict,
                                  apply: bool = False) -> tuple[VerifiedTask | None, list]:
    """:func:`verify_task` plus the directory observations the same output supplies (a directory write's awaiting
    join, a directory read), from one read of the task's output."""
    if _task_reconciler is None:
        return None, []
    display_id = task.get("display_id")
    output = await _fetch_task_output_text(client, int(display_id))
    context = _task_with_foothold_context(task, foothold_by_callback)
    observations = list(_task_reconciler.directory_observations(context, output, now))
    return await _verified_from_output(context, output, now, engagement_id=engagement_id, display_id=display_id,
                                 client=client, apply=apply, observations=observations)


async def _verified_from_output(context, output, now, *, engagement_id, display_id, client, apply, observations):
    record = _task_reconciler.reconcile_task(context, output, now, engagement_id=str(engagement_id or ""))
    if record is None:
        candidate = _task_reconciler.review_candidate(context, output)
        if candidate is None:
            return None, observations
        # No verifier credited the task: its observation carries lineage and no proof, so the row stays pending
        # unless the credential review proves material.
        technique, target, evidence = candidate
        return VerifiedTask(technique, target, evidence, (), review_output=str(output or "")), observations
    evidence = dict(record.evidence)
    refs, notes = await _import_reconciled_credentials(client, record.credential_material, display_id, apply=apply)
    if refs:
        evidence["credential_material_imported"] = True
        evidence["credential_store_refs"] = refs
    reviewable = evidence.get("task_classification") == "postprocessing_error_with_output"
    return VerifiedTask(record.technique, record.target, evidence, tuple(notes),
                        review_output=str(output or "") if reviewable else ""), observations


def footholds_from_tasks(tasks, now: str) -> dict[str, object]:
    """Callback context for each task from the task's own Mythic callback row, keyed by callback display ID.

    The forest comes from the callback's domain or user through the engagement's NetBIOS map, exactly as live
    footholds derive it, so a task on a callback that has since died is evaluated the same way. No Mythic read.
    """
    if _access_reconciler is None:
        return {}
    rows: dict[str, dict] = {}
    for task in tasks:
        callback = task.get("callback") if isinstance(task, dict) else None
        if isinstance(callback, dict) and callback.get("display_id") is not None:
            rows.setdefault(str(callback["display_id"]), callback)
    footholds = _access_reconciler.project_access(
        list(rows.values()), now, {}, netbios_to_fqdn=_access_reconciler._engagement_netbios_map(),
    )
    return {str(foothold.callback_id): foothold for foothold in footholds if str(foothold.callback_id)}
