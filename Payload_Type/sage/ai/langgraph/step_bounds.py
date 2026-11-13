"""Per-step bounds for supervised worker turns, and the typed outcome Sage writes when a step ends.

A supervised step is one worker invocation, from a Supervisor handoff until control returns. It ends at its one
guarded action, a tool-call bound, an active-time bound, or a run of tool errors. Time spent waiting for an
operator's approval, and time inside a guarded action (which includes the Mythic task wait, capped separately),
never counts toward the active-time bound: the bound limits the agent's own work, not the operator's decision time.

Each worker's bounds come from its prompt front matter (`step_bounds:`), and a valid `SAGE_STEP_MAX_*` operator
override wins over it; they are resolved once per step. A step that reaches a bound gets one final model call with
no tools offered, so it can return what it found; Sage writes a notice instead when that call yields no text.

The outcome is written by Sage from what happened in the step, never by the model. Pure: callers pass the step's
messages, the clock readings and the guarded tool set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
import os
import re
from typing import Any, Iterable, Mapping

from . import step_references

# 2: the outcome carries Sage-read references to the step's tasks, credentials and graph reads.
# 3: it also carries the step's resolved bounds and their sources, blocked calls, and the final-answer status.
# 4: a step rejected by scope also carries Sage's display-safe record of the refused proposal.
STEP_OUTCOME_VERSION = 4
QUEUED_PREFIX = "[Queued]"
DENIED_PREFIX = "[DENIED by operator]"
# Routing results that can appear in a worker's context; they are not work the step did.
CONTROL_TOOLS = frozenset({"handback_to_supervisor", "summarize_and_handback", "request_continuation",
                           "respond_to_user"})
CONTROL_PLANE = "control_plane"


class StepStatus(str, Enum):
    ACTION_EXECUTED = "action_executed"      # its one guarded action ran; a further one needs a new step
    REJECTED = "rejected"                    # the operator denied its guarded action
    REJECTED_BY_SCOPE = "rejected_by_scope"  # it proposed a guarded action outside its brief
    BUDGET_EXHAUSTED = "budget_exhausted"    # a tool-call, active-time or error bound was reached
    PRECONDITION_UNSATISFIABLE = "precondition_unsatisfiable"
    TASK_PENDING = "task_pending"            # a task was accepted and its result is outstanding
    SUBMISSION_UNKNOWN = "submission_unknown"  # it is unknown whether Mythic accepted the task
    ENDED = "ended"                          # the worker ended its own turn


# A step ending in one of these hands control back to the Supervisor to choose the next step; it never ends the
# request by itself. Pending and unknown submissions keep the existing checkpoint end.
CONTINUABLE = frozenset(StepStatus) - {StepStatus.TASK_PENDING, StepStatus.SUBMISSION_UNKNOWN}


@dataclass(frozen=True)
class StepBounds:
    tool_calls: int = 20
    active_seconds: float = 600.0
    consecutive_errors: int = 3
    # One guarded action per step is a decision, not a tunable.
    guarded_actions: int = 1

    def as_dict(self) -> dict[str, int | float]:
        return {name: getattr(self, name) for name in BOUND_ENV}


# Each tunable bound and the operator override that wins over a worker's front matter.
BOUND_ENV = {
    "tool_calls": "SAGE_STEP_MAX_TOOL_CALLS",
    "active_seconds": "SAGE_STEP_MAX_ACTIVE_SECONDS",
    "consecutive_errors": "SAGE_STEP_MAX_CONSECUTIVE_ERRORS",
}
_COUNTS = frozenset({"tool_calls", "consecutive_errors"})
# The most wall time a step's final, tool-free model call may take, within the request's own limits.
WRAP_UP_SECONDS = 30.0


class StepBoundsError(ValueError):
    """A worker's front matter declares step bounds Sage cannot use."""


def _bound_value(name: str, value: Any) -> int | float | None:
    """`value` as a usable bound: positive and finite, and a whole number for a count; None otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    if name in _COUNTS:
        return int(number) if number.is_integer() else None
    return number


def front_matter_bounds(meta: Mapping[str, Any]) -> dict[str, int | float]:
    """The bounds a worker's prompt front matter declares under `step_bounds`; {} when it declares none.

    Raises StepBoundsError for a declaration Sage cannot use, so a malformed prompt fails the graph build instead
    of running its worker with bounds nobody chose."""
    if "step_bounds" not in meta:
        return {}
    declared = meta["step_bounds"]
    if not isinstance(declared, Mapping):
        raise StepBoundsError(f"step_bounds must be a mapping of {', '.join(BOUND_ENV)}, not {declared!r}")
    unknown = sorted(str(name) for name in declared if name not in BOUND_ENV)
    if unknown:
        raise StepBoundsError(f"step_bounds names unknown bounds: {', '.join(unknown)}")
    values: dict[str, int | float] = {}
    for name, raw in declared.items():
        value = _bound_value(name, raw)
        if value is None:
            kind = "whole number" if name in _COUNTS else "number"
            raise StepBoundsError(f"step_bounds.{name} must be a positive finite {kind}, not {raw!r}")
        values[name] = value
    return values


def _override(name: str, raw: str | None) -> int | float | None:
    try:
        return _bound_value(name, float(str(raw).strip())) if raw is not None and str(raw).strip() else None
    except ValueError:
        return None


def resolve_bounds(front_matter: Mapping[str, int | float] | None = None,
                   env: Mapping[str, str] | None = None) -> tuple[StepBounds, dict[str, str]]:
    """One step's bounds and where each came from: a valid operator override, else the worker's front matter (as
    parsed by `front_matter_bounds`), else the default. An invalid override sets nothing."""
    env = os.environ if env is None else env
    values: dict[str, int | float] = {}
    sources: dict[str, str] = {}
    for name, variable in BOUND_ENV.items():
        override = _override(name, env.get(variable))
        if override is not None:
            values[name], sources[name] = override, "env"
        elif front_matter and name in front_matter:
            values[name], sources[name] = front_matter[name], "front_matter"
        else:
            values[name], sources[name] = getattr(StepBounds, name), "default"
    return StepBounds(**values), sources


class WrapUp(str, Enum):
    """The step's final, tool-free model call after a bound. Unset when the step ended without reaching one."""
    PENDING = "pending"      # admitted; the call has not returned
    ANSWERED = "answered"    # the worker's text was returned
    EMPTY = "empty"          # the call returned no text (or only tool calls): Sage's notice was returned
    FAILED = "failed"        # the call raised, or its text could not reach the Supervisor intact: Sage's notice
    #                          was returned (the notice says which)
    TIMEOUT = "timeout"      # the call outran its reserve: Sage's notice was returned
    SKIPPED = "skipped"      # no time was left in the request: no call, Sage's notice was returned
    NOT_RUN = "not_run"      # the step closed before the call ran (a stop, a halt or another guard)


@dataclass
class StepRecord:
    """Accounting for one step that survives an approval pause, keyed by request and delegated message."""

    request_id: str
    delegation_seq: int
    worker: str
    started: float
    tasks_before: frozenset[str] = frozenset()
    credentials_before: frozenset[int] = frozenset()
    active_seconds: float = 0.0
    action_seconds: float = 0.0
    status: StepStatus | None = None
    bound: str = ""
    detail: str = ""
    # Sage proposed the step's bound procedure itself (`bound_procedure_call`); it does so at most once per step.
    procedure_dispatched: bool = False
    # Resolved once, when the step starts (`resolve_bounds`).
    bounds: StepBounds = field(default_factory=StepBounds)
    bound_sources: dict[str, str] = field(default_factory=dict)
    # Calls a model response proposed beyond the step's free call slots; removed before any ran.
    calls_blocked: int = 0
    wrap_up: WrapUp | None = None
    # The guarded call the step's scope refused (`refused_proposal`); empty unless the step was rejected by scope.
    refused: dict[str, str] = field(default_factory=dict)

    def end(self, status: StepStatus, bound: str = "", detail: str = "") -> None:
        if self.status is None:
            self.status, self.bound, self.detail = status, bound, detail


@dataclass(frozen=True)
class StepActivity:
    tool_calls: int = 0
    guarded: tuple[tuple[str, bool], ...] = ()  # (tool name, rejected by the operator) in order
    error_streak: int = 0

    @property
    def executed(self) -> int:
        return sum(not rejected for _, rejected in self.guarded)

    @property
    def rejected(self) -> int:
        return sum(rejected for _, rejected in self.guarded)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content or "")


def is_work_call(name: str) -> bool:
    """Whether a tool call is work the step's call bound counts, rather than routing."""
    return not (name.startswith("transfer_to_") or name in CONTROL_TOOLS)


def step_activity(messages: Iterable[Any], guarded: frozenset[str]) -> StepActivity:
    """Tool results of one step: how many ran, which guarded actions ran or were denied, the trailing error run."""
    tool_calls, streak, results = 0, 0, []
    for message in messages:
        if getattr(message, "type", "") != "tool":
            continue
        name = str(getattr(message, "name", "") or "")
        text = _text(getattr(message, "content", "")).lstrip()
        if text.startswith(QUEUED_PREFIX) or not is_work_call(name):
            continue  # a deferred proposal or a routing result, not work this step did
        tool_calls += 1
        if name in guarded:
            results.append((name, text.startswith(DENIED_PREFIX)))
        streak = streak + 1 if getattr(message, "status", "") == "error" else 0
    return StepActivity(tool_calls=tool_calls, guarded=tuple(results), error_streak=streak)


def bound_reached(activity: StepActivity, record: StepRecord, bounds: StepBounds) -> tuple[str, str] | None:
    """The bound a step has reached before its next model call, as (bound, detail)."""
    if activity.tool_calls >= bounds.tool_calls:
        return "tool_calls", f"{activity.tool_calls} tool calls (bound {bounds.tool_calls})"
    if activity.error_streak >= bounds.consecutive_errors:
        return "consecutive_errors", f"{activity.error_streak} consecutive tool errors"
    if record.active_seconds >= bounds.active_seconds:
        return "active_time", f"{record.active_seconds:.0f}s active (bound {bounds.active_seconds:.0f}s)"
    return None


def budget_line(activity: StepActivity, record: StepRecord) -> str:
    """The Sage-authored line a worker's model call receives about what is left of its step."""
    bounds = record.bounds
    if record.wrap_up is not None:
        return (f"Step budget (Sage): this step reached its bound ({record.detail}). No tools are offered for this "
                "final call: answer now from the evidence above, saying what you found and what remains undone.")
    calls_left = max(0, bounds.tool_calls - activity.tool_calls)
    seconds_left = max(0.0, bounds.active_seconds - record.active_seconds)
    return (f"Step budget (Sage): {calls_left} of {bounds.tool_calls} tool calls left; {seconds_left:.0f}s of "
            f"{bounds.active_seconds:.0f}s active time left; {activity.error_streak} of {bounds.consecutive_errors} "
            "consecutive tool errors. Approval waits and guarded-action time do not count.")


def wrap_up_notice(record: StepRecord, wrap_up: WrapUp, *, undelivered: bool = False) -> str:
    """What the Supervisor receives when a bounded step's final call yields no answer it can deliver.

    `undelivered` (status FAILED) is a final answer the supervised scope filter would have altered on its way to the
    Supervisor: the call itself succeeded, so the notice says the answer was withheld, not that the call failed."""
    if undelivered:
        return (f"[Sage] {record.worker} reached its step bound ({record.detail}). Its final answer was withheld: "
                "it could not reach the Supervisor intact through supervised scope filtering. The tool results and "
                "the step outcome are what this step produced; nothing more ran.")
    why = {
        WrapUp.EMPTY: "its final call returned no text",
        WrapUp.FAILED: "its final call failed",
        WrapUp.TIMEOUT: f"its final call took longer than {WRAP_UP_SECONDS:.0f}s",
        WrapUp.SKIPPED: "no time was left in the request for a final call",
    }[wrap_up]
    return (f"[Sage] {record.worker} reached its step bound ({record.detail}) and gave no final answer: {why}. "
            "The tool results and the step outcome are what this step produced; nothing more ran.")


def _brief_scope(brief: Any) -> tuple[list[str], list[str]]:
    if not isinstance(brief, Mapping):
        return [], []
    return ([str(item) for item in brief.get("allowed_capabilities") or []],
            [str(item) for item in brief.get("allowed_tools") or []])


def tool_scope_violation(brief: Any, tool_name: str) -> str:
    """Why a guarded tool falls outside the step's brief, whatever its arguments; empty when the tool is in scope or
    the brief sets no scope."""
    capabilities, tools = _brief_scope(brief)
    if capabilities == [CONTROL_PLANE]:
        return f"`{tool_name}` is a guarded action and this step was briefed as control-plane reads only"
    if tools:
        if tool_name in tools:
            return ""
        return f"`{tool_name}` is not in this step's allowed_tools ({', '.join(tools)})"
    if capabilities and tool_name != "execute_capability":
        return (f"`{tool_name}` is not offered in this step: its one guarded action is `execute_capability` for "
                f"{', '.join(capabilities)}. Re-send this scope if that procedure is what the operator asked for; "
                "otherwise re-scope the step to what the operator asked for.")
    return ""


def _malformed_capability(capability_name: str, catalog: Iterable[str]) -> bool:
    """Whether a proposed capability names no catalog procedure (a command name, a typo, or nothing). Letter case is
    ignored, so a real procedure written differently is never mistaken for a malformed proposal. Without the
    catalog nothing is malformed."""
    names = {str(item).strip().casefold() for item in catalog}
    return bool(names) and str(capability_name or "").strip().casefold() not in names


def scope_violation(brief: Any, tool_name: str, capability_name: str = "", catalog: Iterable[str] = ()) -> str:
    """Why a guarded call falls outside the step's brief; empty when it is in scope or the brief sets no scope."""
    reason = tool_scope_violation(brief, tool_name)
    if reason:
        return reason
    capabilities, tools = _brief_scope(brief)
    if capabilities and not tools and capability_name not in capabilities:
        shown = display_name(capability_name) or "unknown"  # never a parameter typed after the name
        if _malformed_capability(capability_name, catalog):
            return (f"`{shown}` is not a catalog procedure, so the worker's proposal was "
                    f"malformed: this step's one guarded action is `execute_capability` for "
                    f"{', '.join(capabilities)}. Re-send this scope once if that procedure is what the operator "
                    "asked for; otherwise re-scope the step to what the operator asked for.")
        return (f"capability `{shown}` is not in this step's allowed_capabilities "
                f"({', '.join(capabilities)}); re-scope the step to the capability the operator asked for")
    return ""


# A scope rejection is either a worker deviation — calling a tool its step does not offer, or proposing a capability
# that is no catalog procedure — which the same scope may retry once, or the step's own action naming a real catalog
# procedure its brief excludes (the scope itself is wrong).
NOT_OFFERED = "not_offered"
MIS_SCOPED = "mis_scoped"


def scope_rejection_kind(brief: Any, tool_name: str, capability_name: str = "", catalog: Iterable[str] = ()) -> str:
    """Which kind of scope rejection a guarded call is; only meaningful when `scope_violation` is non-empty."""
    if tool_scope_violation(brief, tool_name) or _malformed_capability(capability_name, catalog):
        return NOT_OFFERED
    return MIS_SCOPED


# A refused proposal is reported by its leading identifier only: a command, capability or tool name always fits, and
# whatever followed it in the same field (parameters, a path, a credential typed into the name) is dropped.
_DISPLAY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")
_DISPLAY_CALLBACK = re.compile(r"[0-9]{1,9}")
REFUSED_KEYS = ("tool", "action", "form", "callback")


def display_name(value: Any) -> str:
    """The leading identifier of a proposed name, or "" when the value starts with none."""
    match = _DISPLAY_NAME.match(value.strip()) if isinstance(value, str) else None
    return match.group(0) if match else ""


def display_callback(value: Any) -> str:
    """A proposed callback display id as digits, or "" when it is not one."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value >= 0 else ""
    text = value.strip() if isinstance(value, str) else ""
    return text if _DISPLAY_CALLBACK.fullmatch(text) else ""


def _proposed_capability(args: Mapping[str, Any]) -> tuple[str, Any]:
    """The capability name and callback an `execute_capability` call proposed, as written."""
    action = args.get("action")
    if isinstance(action, str):
        try:
            action = json.loads(action)
        except ValueError:
            return action, args.get("callback_display_id")
    if isinstance(action, str):
        return action, args.get("callback_display_id")
    if isinstance(action, Mapping):
        return (str(action.get("name") or action.get("capability") or ""),
                action.get("callback_id", args.get("callback_display_id")))
    return "", args.get("callback_display_id")


def refused_proposal(brief: Any, tool_name: str, args: Any, capability_name: str = "", catalog: Iterable[str] = (),
                     *, scope: str = "", worker: str = "", delegation_seq: Any = "") -> dict[str, str]:
    """Sage's record of a guarded call its step's scope refused: the tool, the command or capability it named, the
    callback, the refused scope and the step it came from. Display-safe: never its parameters, inputs or other
    arguments, and never the worker's text. A command submitted as a capability is labelled malformed, not
    converted. `capability_name` is the name the scope check classified, so the form agrees with its kind."""
    args = args if isinstance(args, Mapping) else {}
    if tool_name == "execute_capability":
        proposed, callback = _proposed_capability(args)
        action = display_name(proposed)
        form = "malformed_capability" if _malformed_capability(capability_name, catalog) else "capability"
    else:
        callback = args.get("callback_display_id")
        action = display_name(args.get("command"))
        form = "command" if action else "tool"
    return {
        "tool": display_name(tool_name), "action": action, "form": form, "callback": display_callback(callback),
        "scope": str(scope or ""), "worker": display_name(worker), "delegation_seq": display_callback(delegation_seq),
        "step_callback": display_callback(brief.get("callback_display_id")) if isinstance(brief, Mapping) else "",
    }


def describe_refused_proposal(proposal: Mapping[str, Any]) -> str:
    """One clause naming a refused proposal from its record, for the step handback and the request's terminal."""
    tool = f"`{proposal.get('tool') or 'unknown tool'}`"
    name = str(proposal.get("action") or "")
    form = proposal.get("form")
    if form == "malformed_capability":
        what = (f"`{name}` submitted as a capability" if name else "an unnamed capability") + f" through {tool}"
    elif form == "capability" and name:
        what = f"catalog capability `{name}` through {tool}"
    elif form == "command" and name:
        what = f"`{name}` as a command through {tool}"
    else:
        what = f"a {tool} call"
    callback = proposal.get("callback")
    what += f" on callback {callback}" if callback else " with no readable callback"
    if proposal.get("scope"):
        what += f", outside this step's scope ({proposal['scope']})"
    if form == "malformed_capability":
        what += "; the proposal was malformed: it names no catalog procedure"
    return what


# Tools that teach how to run a technique by hand. A step scoped to catalog procedures has Sage run the procedure,
# so a hand-run recipe only competes with the step's one admissible action (a live worker followed one instead).
TECHNIQUE_GUIDANCE_TOOLS = frozenset({"get_ttp_guidance", "get_ttp_full_reference", "list_ttp_categories"})


def withheld_tools(brief: Any, tool_names: Iterable[str], guarded: frozenset[str]) -> frozenset[str]:
    """The tools a step's model is not offered: guarded tools its brief does not admit (the same rule the step
    bound enforces on a call), and technique guidance in a step scoped to catalog procedures. Empty when the brief
    sets no scope. Offering is not enforcement: the step bound still removes any out-of-scope guarded call."""
    capabilities, tools = _brief_scope(brief)
    if not capabilities and not tools:
        return frozenset()
    procedures = bool(capabilities) and capabilities != [CONTROL_PLANE] and not tools
    return frozenset(name for name in tool_names
                     if (name in guarded and tool_scope_violation(brief, name))
                     or (procedures and name in TECHNIQUE_GUIDANCE_TOOLS))


def step_outcome(record: StepRecord, activity: StepActivity, *, now: float, tasks_issued: Iterable[str] = (),
                 credentials_added: Iterable[int] = (), status: StepStatus | None = None) -> dict[str, Any]:
    final = status or record.status or StepStatus.ENDED
    wall = max(0.0, now - record.started)
    wrap_up = WrapUp.NOT_RUN if record.wrap_up is WrapUp.PENDING else record.wrap_up
    return {
        "version": STEP_OUTCOME_VERSION,
        "worker": record.worker,
        "status": final.value,
        "bound": record.bound if final == record.status else "",
        "detail": record.detail if final == record.status else "",
        "tool_calls": activity.tool_calls,
        "guarded_executed": activity.executed,
        "guarded_rejected": activity.rejected,
        "tasks_issued": sorted(tasks_issued, key=lambda item: (len(item), item)),
        "credentials_added": sorted(credentials_added),
        # Filled from Mythic by the caller before the outcome is rendered (step_references.collect).
        "references": step_references.empty(),
        "active_seconds": round(record.active_seconds, 1),
        "action_seconds": round(record.action_seconds, 1),
        "waiting_seconds": round(max(0.0, wall - record.active_seconds - record.action_seconds), 1),
        "bounds": record.bounds.as_dict(),
        "bound_sources": dict(record.bound_sources),
        "calls_blocked": record.calls_blocked,
        # The final, tool-free call after a bound; "" when the step reached none.
        "wrap_up": wrap_up.value if wrap_up else "",
        # The guarded call the step's scope refused, as Sage recorded it; {} for any other ending.
        "refused_proposal": dict(record.refused) if record.refused and final == record.status else {},
    }


# A step that ends in one of these with nothing changed has given its whole answer; sending the same step again
# returns the same answer. A bounded or rejected step is excluded: continuing it, or retrying after a denial, is
# legitimate work.
ANSWERED = frozenset({StepStatus.ENDED, StepStatus.ACTION_EXECUTED, StepStatus.PRECONDITION_UNSATISFIABLE})


def answered_without_effect(outcome: Mapping[str, Any]) -> bool:
    """Whether a step returned its answer without issuing a task, adding a credential or meeting a denial."""
    return (StepStatus(outcome["status"]) in ANSWERED and not outcome.get("tasks_issued")
            and not outcome.get("credentials_added") and not outcome.get("guarded_rejected"))


def render_outcome(outcome: Mapping[str, Any]) -> str:
    """The Sage-authored line the Supervisor reads when a step returns."""
    reason = outcome["status"] + (f" ({outcome['detail']})" if outcome.get("detail") else "")
    tasks = ", ".join(str(task) for task in outcome.get("tasks_issued") or []) or "none"
    lines = [
        f"[step outcome] {outcome['worker']} step ended: {reason}.",
        f"Guarded actions: {outcome['guarded_executed']} executed, {outcome['guarded_rejected']} rejected by the "
        f"operator. Mythic tasks issued: {tasks}. Tool calls: {outcome['tool_calls']}.",
        f"Time: {outcome['active_seconds']:.0f}s active, {outcome['action_seconds']:.0f}s inside the guarded "
        f"action, {outcome['waiting_seconds']:.0f}s waiting (approval and other).",
    ]
    if outcome.get("refused_proposal"):
        lines.append(f"Refused proposal (Sage's record): {describe_refused_proposal(outcome['refused_proposal'])}. "
                     "It was not approved and not executed: Sage removed it before any approval card or Mythic task.")
    if outcome.get("calls_blocked"):
        lines.append(f"Tool calls not run because the step had no call slots left: {outcome['calls_blocked']}.")
    if outcome.get("wrap_up"):
        lines.append("Final answer after the bound: " + {
            WrapUp.ANSWERED.value: "written by the worker without tools.",
            WrapUp.NOT_RUN.value: "not run; the step was closed first.",
        }.get(outcome["wrap_up"], f"none ({outcome['wrap_up']}); Sage returned a partial-result notice."))
    lines.extend(step_references.render_lines(outcome.get("references")))
    ledger = outcome.get("ledger") if isinstance(outcome.get("ledger"), Mapping) else {}
    if ledger.get("recorded"):
        lines.append("Engagement ledger: " + "; ".join(
            f"`{row['effect']}` {row.get('status') or 'recorded'} (task {row.get('task_id')})"
            for row in ledger["recorded"]) + ".")
    if ledger.get("unattributed"):
        lines.append("Callback gains no Sage task can be credited with (not recorded): " + "; ".join(
            f"callback {row['callback_display_id']} ({', '.join(row['effects'])})" for row in ledger["unattributed"])
            + ".")
    if StepStatus(outcome["status"]) in CONTINUABLE:
        lines.append("Control is back with the Supervisor: choose the next step or answer the operator."
                     + (" Returning control did not cancel any target-side task." if outcome.get("tasks_issued")
                        else ""))
    return "\n".join(lines)


# Catalog procedures Sage proposes itself when the brief binds every argument they take, so no worker model chooses
# again what the Supervisor already chose. Graph collection takes a callback and, optionally, a domain: without one
# the collector uses the callback user's current domain, so a live callback alone binds it.
BRIEF_BOUND_PROCEDURES = frozenset({"collect-graph"})


def bound_procedure_call(brief: Any, known_hosts: Iterable[str]) -> dict[str, Any] | None:
    """The `execute_capability` arguments for a step whose brief fully binds one procedure, else None.

    Bound means the brief names exactly one procedure in BRIEF_BOUND_PROCEDURES and a callback (which brief
    validation has already matched to one Sage observed). At most one target: it becomes the domain unless it names a
    host Sage has seen, in which case the collector's own default (the callback user's domain) applies. A bare label
    names a host when exactly one known full host name carries it; when several do, the choice is left to the worker
    rather than sending a host label as the domain. Several targets leave the choice to the worker. Nothing about the
    network needs to be known beforehand."""
    capabilities, tools = _brief_scope(brief)
    if tools or len(capabilities) != 1 or capabilities[0] not in BRIEF_BOUND_PROCEDURES:
        return None
    callback = str(brief.get("callback_display_id") or "").strip()
    targets = [str(item).strip().strip("'\"").rstrip(".").casefold()
               for item in brief.get("targets") or [] if str(item).strip()]
    if not callback or len(targets) > 1:
        return None
    action = {"capability": capabilities[0], "callback_id": callback}
    hosts = {str(item).strip().casefold() for item in known_hosts if str(item).strip()}
    if targets and targets[0] not in hosts and targets[0].split(".", 1)[0] not in hosts:
        target = targets[0]
        carriers = set() if "." in target else {
            host for host in hosts if "." in host and host.split(".", 1)[0] == target}
        if len(carriers) > 1:
            return None
        if not carriers:
            action["domain"] = target
    return {"action": action}
