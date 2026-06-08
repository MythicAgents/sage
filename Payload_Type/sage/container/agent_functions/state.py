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

from ai.langgraph import engagement_ledger
try:
    from ai.langgraph import engagement_state as _es
except Exception:  # rendering is best-effort; the detail view below does not depend on it
    _es = None


class StateArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)
        self.args = [
            CommandParameter(
                name="action", display_name="Action", cli_name="action", type=ParameterType.ChooseOne,
                choices=["show", "remove", "set", "wipe"], default_value="show",
                description="show the engagement state, remove a hop, set a hop's status, or wipe the whole ledger.",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=0)],
            ),
            CommandParameter(
                name="hop", display_name="Hop", cli_name="hop", type=ParameterType.String, default_value="",
                description="Hop selector for remove/set — its id (e.g. dcsync-user:cersei.lannister@sevenkingdoms.local), its effect, or its technique.",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=1)],
            ),
            CommandParameter(
                name="status", display_name="Status", cli_name="status", type=ParameterType.String, default_value="",
                description="New status for `set` (achieved | failed | blocked | pending).",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=2)],
            ),
            CommandParameter(
                name="engagement", display_name="Engagement", cli_name="engagement", type=ParameterType.String, default_value="",
                description="Engagement id to target (default: the current SAGE_ENGAGEMENT_ID).",
                parameter_group_info=[ParameterGroupInfo(required=False, ui_position=3)],
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
        if action == "remove" and len(parts) > 1:
            # capture the whole remainder so a CSV ('9,10,11' or '9, 10, 11') survives splitting.
            self.add_arg("hop", " ".join(parts[1:]))
        elif len(parts) > 1:
            self.add_arg("hop", parts[1])
            if len(parts) > 2:
                self.add_arg("status", parts[2])

    async def parse_dictionary(self, dictionary_arguments):
        self.load_args_from_dictionary(dictionary_arguments)


def _truncate(value, n=70) -> str:
    text = " ".join(str(value or "").split())
    return text[: n - 1] + "…" if len(text) > n else text


def _render_ledger(data: dict, engagement_id: str, path: str) -> str:
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
            state = _es.EngagementState(objective=str(data.get("objective") or ""), hops=_es.hops_from_dicts(hops))
            lines += ["", "--- agent view (rendered into the model each turn) ---", _es.render_engagement_state(state)]
        except Exception:
            pass
    lines += [
        "",
        "Edit: `state remove <hop[,hop,...]>` | `state set <hop> <status>` | `state wipe`",
        "(<hop> = ROW NUMBER(s) (#) above, or id/effect/technique; CSV ok: `state remove 5,9` ; `state remove 9` drops the junk hop.)",
    ]
    return "\n".join(lines)


class StateCommand(CommandBase):
    cmd = "state"
    needs_admin = False
    help_cmd = "state [show | remove <hop[,hop,...]> | set <hop> <status> | wipe]"
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
            engagement_id = (taskData.args.get_arg("engagement") or "").strip() or engagement_ledger.default_engagement_id()
            path = engagement_ledger.ledger_path(engagement_id)

            if action == "show":
                out = _render_ledger(engagement_ledger.load(engagement_id), engagement_id, path)

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

            elif action == "wipe":
                removed = engagement_ledger.wipe(engagement_id)
                out = (f"Wiped engagement state '{engagement_id}' ({path})." if removed
                       else f"No ledger to wipe for engagement '{engagement_id}' ({path}).")

            else:
                out = f"Unknown action '{action}'. Use: show | remove <hop> | set <hop> <status> | wipe."

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
