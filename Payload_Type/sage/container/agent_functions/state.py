"""Operator-facing `state` command — SHOW and EDIT Sage's durable engagement state (the hop ledger).

The engagement state is Sage's grounded record of achieved hops (effects like `da:<domain>`, `creds:<user>`)
that drives the autonomous solve and prevents re-doing work. It is normally an internal JSON file; this
command surfaces it to the operator in the Mythic UI and lets them correct it — e.g. remove a hop the gate
recorded as achieved when the underlying operation actually failed (a "false-achieved" hop), or flip a hop's
status so the gate re-attempts it.

C2-agnostic: a Sage-level command operating on Sage's own state, not a target agent's tradecraft. Shares the
on-disk ledger with the running agent via `ai.langgraph.engagement_ledger` (single source of truth).
"""
from mythic_container.MythicCommandBase import (
    TaskArguments, CommandBase, CommandParameter, ParameterType, ParameterGroupInfo,
    PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse,
)
from mythic_container.MythicRPC import (
    MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate,
    MythicRPCCallbackUpdateMessage, SendMythicRPCCallbackUpdate,
)
from mythic_container.logging import logger

import os
import json
import base64
from datetime import datetime, timezone

from mythic import mythic
from mythic_container.MythicGoRPC import SendMythicRPCAPITokenCreate, MythicRPCAPITokenCreateMessage

from ai.langgraph import engagement_ledger, operation_context
try:
    from ai.langgraph import engagement_state as _es
except Exception:  # rendering is best-effort; the detail view below does not depend on it
    _es = None
try:
    from ai.langgraph import access_reconciler as _access_reconciler
except Exception:
    _access_reconciler = None
try:
    from ai.langgraph import task_reconciler as _task_reconciler
except Exception:
    _task_reconciler = None


async def _state_mythic_client(taskData):
    """Build a Mythic GraphQL client for this command the SAME way the agent does (an API token scoped to
    this task's operator/operation) so the durable-UUID resolution matches. Returns None on any failure."""
    try:
        resp = await SendMythicRPCAPITokenCreate(MythicRPCAPITokenCreateMessage(AgentTaskID=taskData.Task.AgentTaskID))
        if not getattr(resp, "Success", False):
            return None
        ip = os.environ.get("NGINX_HOST", "127.0.0.1")
        port = int(os.environ.get("NGINX_PORT", 7443))
        ssl = os.environ.get("NGINX_SSL", "true").lower() in ("true", "1", "yes")
        return await mythic.login(apitoken=resp.APIToken, server_ip=ip, server_port=port, ssl=ssl)
    except Exception:
        return None


async def _resolve_engagement_id(taskData) -> str:
    """The durable-ledger key for this `state` invocation, matching the agent's precedence:
    explicit `engagement` arg > explicit SAGE_ENGAGEMENT_ID env (!= 'default') > the current Mythic
    OPERATION incl. its durable UUID (read/created via Mythic, same as the agent) > the operation
    name+id from taskData.Callback (no uuid, best-effort) > 'default'."""
    arg = (taskData.args.get_arg("engagement") or "").strip()
    if arg:
        return arg
    env = os.environ.get("SAGE_ENGAGEMENT_ID", "").strip()
    if env and env != "default":
        return env
    # Resolve the full operation key WITH the durable uuid (identical to the agent's _ensure_engagement_key).
    try:
        client = await _state_mythic_client(taskData)
        if client is not None:
            key = await operation_context.resolve_operation_key(client)
            if key:
                return key
    except Exception:
        pass
    # Fallback (no Mythic client): operation name+id without the durable uuid, then 'default'.
    cb = getattr(taskData, "Callback", None)
    op_id = getattr(cb, "OperationID", None)
    if op_id is not None:
        return operation_context.operation_key(getattr(cb, "OperationName", None) or "operation", op_id)
    return "default"


async def _reconcile_state_footholds(taskData) -> list:
    if _access_reconciler is None:
        return []
    try:
        client = await _state_mythic_client(taskData)
        if client is None:
            return []

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
                f"— re-run `state reconcile {task_id} apply` to write it to the Mythic credential store."
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


async def _reconcile_task_history(client, data: dict, task_id: str, callback_id: str, limit: int, now: str, apply: bool = False) -> tuple[dict, list[str]]:
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


class StateArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)
        self.args = [
            CommandParameter(
                name="action", display_name="Action", cli_name="action", type=ParameterType.ChooseOne,
                choices=["show", "remove", "set", "objective", "wipe", "reconcile"], default_value="show",
                description="show/edit the engagement state, or reconcile verified effects from completed Mythic tasks.",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=0)],
            ),
            CommandParameter(
                name="hop", display_name="Hop", cli_name="hop", type=ParameterType.String, default_value="",
                description="Hop selector for remove/set, or the objective text for `objective`.",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=1)],
            ),
            CommandParameter(
                name="status", display_name="Status", cli_name="status", type=ParameterType.String, default_value="",
                description="New status for `set` (achieved | failed | blocked | pending).",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=2)],
            ),
            CommandParameter(
                name="engagement", display_name="Engagement", cli_name="engagement", type=ParameterType.String, default_value="",
                description="Engagement/ledger id to target (default: the current Mythic operation, e.g. Operation_Chimera_1).",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=3)],
            ),
            CommandParameter(
                name="task_id", display_name="Task ID", cli_name="task_id", type=ParameterType.Number, default_value=0,
                description="For `reconcile`: specific Mythic task display_id to inspect, e.g. 450.",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=4)],
            ),
            CommandParameter(
                name="callback_id", display_name="Callback ID", cli_name="callback_id", type=ParameterType.Number, default_value=0,
                description="For `reconcile`: callback display_id whose recent tasks should be scanned.",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=5)],
            ),
            CommandParameter(
                name="limit", display_name="Limit", cli_name="limit", type=ParameterType.Number, default_value=25,
                description="For `reconcile`: max recent tasks to inspect when task_id is not supplied.",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=6)],
            ),
            CommandParameter(
                name="apply", display_name="Apply", cli_name="apply", type=ParameterType.Boolean, default_value=False,
                description="For `reconcile`: actually WRITE reconciled credentials to the Mythic credential store. "
                            "Default false = dry-run (shows what WOULD be imported from task output without writing). "
                            "Task output is attacker-influenceable on a real engagement, so cred writes are opt-in.",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=7)],
            ),
        ]

    async def parse_arguments(self):
        line = (self.command_line or "").strip()
        if not line:
            self.add_arg("action", "show")
            return
        if line.startswith("{"):
            self.load_args_from_json_string(line)
            return
        parts = line.split()
        action = parts[0].lower()
        self.add_arg("action", action)
        if action in {"remove", "objective"} and len(parts) > 1:
            # capture the whole remainder so a CSV ('9,10,11' or '9, 10, 11') survives splitting.
            self.add_arg("hop", " ".join(parts[1:]))
        elif len(parts) > 1:
            if action == "reconcile":
                for p in parts[1:]:
                    if p.lower() == "apply":
                        self.add_arg("apply", True)
                    elif p.lower() in ("dry-run", "dryrun"):
                        self.add_arg("apply", False)
                    else:
                        self.add_arg("task_id", p)
            else:
                self.add_arg("hop", parts[1])
                if len(parts) > 2:
                    self.add_arg("status", parts[2])

    async def parse_dictionary(self, dictionary_arguments):
        self.load_args_from_dictionary(dictionary_arguments)


def _truncate(value, n=70) -> str:
    text = " ".join(str(value or "").split())
    return text[: n - 1] + "…" if len(text) > n else text


def _render_ledger(data: dict, engagement_id: str, path: str, footholds: list | None = None) -> str:
    hops = data.get("hops") or []
    lines = [
        f"=== ENGAGEMENT STATE: {engagement_id} ===",
        f"path: {path}",
        f"hops: {len(hops)}",
        "",
    ]
    if not hops:
        lines.append("(empty — no achieved hops recorded)")
    else:
        lines.append(f"{'#':>2}  {'hop (id)':48}  {'effect':34}  {'status':9}  {'prov':8}  {'task':6}  {'cb':4}  evidence")
        lines.append("-" * 140)
        for i, hop in enumerate(hops, 1):
            ev = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
            prov = str(ev.get("provenance") or "-")
            task_id = ev.get("mythic_task_id")
            cb_id = ev.get("callback_id")
            evidence = ev.get("result_preview") or ev.get("source") or ""
            lines.append(
                f"{i:>2}  {_truncate(engagement_ledger.hop_label(hop), 48):48}  "
                f"{_truncate(hop.get('effect'), 34):34}  {str(hop.get('status') or '-'):9}  "
                f"{prov:8}  {str(task_id if task_id is not None else '-'):6}  "
                f"{str(cb_id if cb_id is not None else '-'):4}  {_truncate(evidence, 36)}"
            )
    # Agent's-eye view (exactly what gets injected into the model each turn), best-effort.
    if _es is not None:
        try:
            state = _es.EngagementState(
                objective=str(data.get("objective") or ""),
                footholds=list(footholds or []),
                hops=_es.hops_from_dicts(hops),
                graph_facts=_es.graph_facts_from_dicts(data.get("graph_facts")),
            )
            lines += ["", "--- agent view (rendered into the model each turn) ---", _es.render_engagement_state(state)]
        except Exception:
            pass
    lines += [
        "",
        "Edit: `state objective <text>` | `state remove <hop[,hop,...]>` | `state set <hop> <status>` | `state wipe`",
        "(<hop> = ROW NUMBER(s) (#) above, or id/effect/technique; CSV ok: `state remove 5,9` ; `state remove 9` drops the junk hop.)",
    ]
    return "\n".join(lines)


class StateCommand(CommandBase):
    cmd = "state"
    needs_admin = False
    help_cmd = "state [show | reconcile [task_id] | remove <hop[,hop,...]> | set <hop> <status> | objective <text> | wipe]"
    description = ("Show and edit Sage's engagement state — the durable ledger of achieved hops/effects that "
                   "drives the autonomous solve. Use it to inspect Sage's grounded state and correct a hop the "
                   "gate recorded as achieved when the operation actually failed (false-achieved), so the gate "
                   "re-attempts it.")
    version = 1
    author = "@sage"
    argument_class = StateArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(TaskID=taskData.Task.ID, Success=True)
        try:
            action = (taskData.args.get_arg("action") or "show").strip().lower()
            hop = (taskData.args.get_arg("hop") or "").strip()
            status = (taskData.args.get_arg("status") or "").strip()
            task_id = str(taskData.args.get_arg("task_id") or "").strip()
            callback_id = str(taskData.args.get_arg("callback_id") or "").strip()
            task_id = "" if task_id in {"0", "0.0"} else task_id
            callback_id = "" if callback_id in {"0", "0.0"} else callback_id
            limit = int(taskData.args.get_arg("limit") or 25)
            engagement_id = await _resolve_engagement_id(taskData)
            path = engagement_ledger.ledger_path(engagement_id)

            if action == "show":
                footholds = await _reconcile_state_footholds(taskData)
                out = _render_ledger(engagement_ledger.load(engagement_id), engagement_id, path, footholds)

            elif action == "remove":
                selectors = [s.strip() for s in hop.split(",") if s.strip()]
                if not selectors:
                    out = "remove requires a hop selector or CSV: `state remove <hop>` or `state remove 9,10,11`."
                else:
                    data, n = engagement_ledger.remove_hops(engagement_ledger.load(engagement_id), selectors)
                    engagement_ledger.save(data, engagement_id)
                    out = f"Removed {n} hop(s) matching {selectors} from engagement '{engagement_id}'.\n\n" + \
                          _render_ledger(data, engagement_id, path)

            elif action == "set":
                if not hop or not status:
                    out = "set requires a hop and a status: `state set <hop> <achieved|failed|blocked|pending>`."
                else:
                    data, n = engagement_ledger.set_hop_status(engagement_ledger.load(engagement_id), hop, status)
                    engagement_ledger.save(data, engagement_id)
                    out = f"Set {n} hop(s) matching '{hop}' to status '{status}' in engagement '{engagement_id}'.\n\n" + \
                          _render_ledger(data, engagement_id, path)

            elif action == "objective":
                if not hop:
                    out = "objective requires text: `state objective obtain administrative control of example.local`."
                else:
                    data = engagement_ledger.load(engagement_id)
                    data["objective"] = hop
                    data["updated"] = datetime.now(timezone.utc).isoformat()
                    engagement_ledger.save(data, engagement_id)
                    footholds = await _reconcile_state_footholds(taskData)
                    out = f"Set objective for engagement '{engagement_id}' to: {hop}\n\n" + \
                          _render_ledger(data, engagement_id, path, footholds)

            elif action == "wipe":
                if (hop or status).strip().lower() != "confirm":
                    out = (f"Refusing to wipe engagement '{engagement_id}' without confirmation — this deletes "
                           f"the ENTIRE durable ledger at {path} with no backup. Re-run `state wipe confirm` to proceed.")
                else:
                    removed = engagement_ledger.wipe(engagement_id)
                    out = (f"Wiped engagement state '{engagement_id}' ({path})." if removed
                           else f"No ledger to wipe for engagement '{engagement_id}' ({path}).")

            elif action == "reconcile":
                apply = bool(taskData.args.get_arg("apply"))
                client = await _state_mythic_client(taskData)
                if client is None:
                    out = "reconcile could not create a Mythic client for task-history inspection."
                else:
                    data, notes = await _reconcile_task_history(
                        client,
                        engagement_ledger.load(engagement_id),
                        task_id or hop,
                        callback_id,
                        limit,
                        datetime.now(timezone.utc).isoformat(),
                        apply=apply,
                    )
                    engagement_ledger.save(data, engagement_id)
                    footholds = await _reconcile_state_footholds(taskData)
                    out = "\n".join(notes) + "\n\n" + _render_ledger(data, engagement_id, path, footholds)

            else:
                out = f"Unknown action '{action}'. Use: show | reconcile [task_id] | remove <hop> | set <hop> <status> | objective <text> | wipe."

        except Exception as e:
            logger.error(f"state command error: {e}")
            response.Success = False
            response.Error = f"state command error: {e}"
            return response

        resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(taskData.Task.ID, out.encode()))
        if not resp.Success:
            response.Success = False
            response.Error = resp.Error
            return response
        await SendMythicRPCCallbackUpdate(MythicRPCCallbackUpdateMessage(
            TaskID=taskData.Task.ID, UpdateLastCheckinTime=True, UpdateLastCheckinTimeViaC2Profile=""))
        response.Completed = True
        return response
