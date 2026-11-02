"""Structural scope for a supervised delegation, checked before dispatch.

A brief names what one delegated step acts on: the callback, the targets, the capability classes, the inputs it
uses by reference, and the result it should produce. Validation is structural only. Required fields must be
present, the callback and targets must be ones Sage already knows or the operator named, and references must
resolve within data Sage already holds. Wording is never judged, and the brief grants no authority: the worker
still receives the exact operator input, and every guarded action still needs the operator's approval.

Pure and offline: the caller passes in what Sage holds; nothing here queries Mythic.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Mapping

try:
    from . import capabilities
except ImportError:  # pragma: no cover - flat import layout
    import capabilities

CONTROL_PLANE = "control_plane"
TARGET_FACING_WORKERS = frozenset({"Mythic_Operator"})
REJECTIONS_BEFORE_STOP = 2

_REFERENCE_RE = re.compile(r"^(task|@?cred):(\d+)$")
_TASK_IN_TEXT_RE = re.compile(r"\btask\s*#?\s*(\d+)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._\\@$-]*", re.IGNORECASE)


def _items(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int)):
        value = [value]
    return tuple(text for text in (str(item).strip() for item in value) if text)


def _callback(value: Any) -> str:
    text = str(value if value is not None else "").strip().lstrip("#")
    return str(int(text)) if text.isdigit() else text


def _name(value: Any) -> str:
    return str(value or "").strip().strip("'\"").rstrip(".").casefold()


def catalog_names() -> tuple[str, ...]:
    """Capability classes as the catalog defines them now; read at each graph build, never copied here."""
    return tuple(item["name"] for item in capabilities.capability_catalog())


def capability_choices() -> tuple[str, ...]:
    """Every value a brief may name in `allowed_capabilities`: a catalog procedure, or control-plane reads only.

    Work outside the catalog names its guarded tools in `allowed_tools` instead, so it is always scoped.
    """
    return (*catalog_names(), CONTROL_PLANE)


def capability_legend() -> str:
    """The catalog rendered as an argument legend for the delegation tool; generated from the catalog at each
    graph build so a new capability appears automatically."""
    return capabilities.capability_legend()


@dataclass(frozen=True)
class HandoffBrief:
    targets: tuple[str, ...] = ()
    callback_display_id: str = ""
    allowed_capabilities: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    inputs_by_reference: tuple[str, ...] = ()
    expected_result: str = ""
    # Reserved: step budgets are enforced by the step-bounds work and plan identity by the plan work.
    budget: str = ""
    step_id: str = ""
    plan_revision: str = ""

    @classmethod
    def from_args(cls, *, targets: Any = None, callback_display_id: Any = None, allowed_capabilities: Any = None,
                  allowed_tools: Any = None, inputs_by_reference: Any = None,
                  expected_result: Any = None) -> "HandoffBrief":
        return cls(
            targets=_items(targets),
            callback_display_id=_callback(callback_display_id),
            allowed_capabilities=tuple(dict.fromkeys(item.casefold() for item in _items(allowed_capabilities))),
            allowed_tools=tuple(dict.fromkeys(_items(allowed_tools))),
            inputs_by_reference=tuple(item.casefold() for item in _items(inputs_by_reference)),
            expected_result=str(expected_result or "").strip(),
        )

    @property
    def control_plane_only(self) -> bool:
        return self.allowed_capabilities == (CONTROL_PLANE,)

    @property
    def off_catalog(self) -> bool:
        """Work that is not a catalog procedure: the step names its guarded tools instead of a procedure."""
        return bool(self.allowed_tools)

    def as_metadata(self) -> dict[str, Any]:
        return {
            "targets": list(self.targets),
            "callback_display_id": self.callback_display_id,
            "allowed_capabilities": list(self.allowed_capabilities),
            "allowed_tools": list(self.allowed_tools),
            "inputs_by_reference": list(self.inputs_by_reference),
            "expected_result": self.expected_result,
        }


@dataclass(frozen=True)
class KnownScope:
    """What Sage already holds for this request, normalized for comparison."""

    operator_input: str = ""
    bound_callback: str = ""
    callbacks: frozenset[str] = frozenset()
    hosts: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    principals: frozenset[str] = frozenset()
    tasks: frozenset[str] = frozenset()
    credentials: frozenset[str] = frozenset()
    objects: frozenset[str] = frozenset()

    def names_target(self, target: str) -> bool:
        name = _name(target)
        if not name:
            return False
        if name in self.hosts or name in self.domains or name in self.principals or name in self.objects:
            return True
        if _named_in(name, self.operator_input):
            return True
        if "." not in name and len([d for d in self.domains if d.split(".", 1)[0] == name]) == 1:
            return True
        # A bare label names a host only when exactly one known full host name carries it, so a label seen in one
        # domain is never admitted in another.
        if "." not in name and len([h for h in self.hosts if "." in h and h.split(".", 1)[0] == name]) == 1:
            return True
        label, _, rest = name.partition(".")
        if rest and label in self.hosts and rest in self.domains:
            return True
        for separator in ("\\", "@"):
            left, found, right = name.partition(separator)
            if found:
                user, domain = (right, left) if separator == "\\" else (left, right)
                if (user in self.principals or user in self.objects) and (
                        domain in self.domains or self.names_target(domain)):
                    return True
        return False


def _named_in(name: str, text: str) -> bool:
    pattern = r"(?<![\w.\\@-])" + re.escape(name) + r"(?![\w\\@-]|\.\w)"
    return re.search(pattern, text.casefold()) is not None


# Graph facts that name a host and the GPO applying to it, as `graph_reconciler` writes them:
# `<kind>:{gpo}:{short_host}:{domain}`.
_GPO_HOST_FACTS = ("gpo-affects-computer", "gpo-affects-dc")


def _graph_fact_names(predicate: str) -> tuple[set[str], set[str]]:
    """The full host names and directory objects (GPOs) one graph fact names; empty sets for any other shape.

    Only facts whose layout is known are read. `generic-write:computer:{short_host}` is skipped: it carries no
    domain, and a bare label would otherwise be admitted in every known domain.
    """
    kind, _, rest = predicate.partition(":")
    parts = [_name(part) for part in rest.split(":")]
    if kind in _GPO_HOST_FACTS and len(parts) == 3 and all(parts) and "." in parts[2]:
        gpo, host, domain = parts
        return {host if "." in host else f"{host}.{domain}"}, {gpo}
    if kind == "generic-write" and len(parts) == 2 and parts[0] == "gpo" and parts[1]:
        return set(), {parts[1]}
    return set(), set()


def known_scope(mythic_client: Any, *, operator_input: str = "", bound_callback: Any = "") -> KnownScope:
    """Collect callbacks, names, tasks and credentials Sage already holds in memory. Never queries Mythic."""
    callbacks: set[str] = set()
    hosts: set[str] = set()
    domains: set[str] = set()
    principals: set[str] = set()
    tasks: set[str] = set(_TASK_IN_TEXT_RE.findall(operator_input or ""))
    credentials: set[str] = set()
    objects: set[str] = set()

    def principal(value: Any) -> None:
        text = _name(value)
        if text:
            principals.add(text)
            principals.add(re.split(r"[\\]", text)[-1].split("@", 1)[0])

    def attr(name: str, kind: type) -> Any:
        value = getattr(mythic_client, name, None)
        return value if isinstance(value, kind) else kind()

    snapshots = attr("_recon_snapshot_cache", dict).get(("list_callbacks", "all"))
    for row in snapshots if isinstance(snapshots, list) else []:
        if isinstance(row, Mapping):
            callbacks.add(_callback(row.get("id")))
            hosts.add(_name(row.get("host")))
            principal(row.get("user"))
    for foothold in attr("_engagement_footholds", list):
        callbacks.add(_callback(getattr(foothold, "callback_id", "")))
        host = _name(getattr(foothold, "host", ""))
        forest = _name(getattr(foothold, "forest", ""))
        hosts.add(host)
        domains.add(forest)
        if host and forest and "." not in host:
            hosts.add(f"{host}.{forest}")
        principal(getattr(foothold, "identity", ""))
    for hop in attr("_engagement_hops", list):
        target = _name(getattr(hop, "target", ""))
        (domains if "." in target and "\\" not in target and "@" not in target else hosts).add(target)
        evidence = getattr(hop, "evidence", None)
        if isinstance(evidence, Mapping):
            tasks.update(_items(evidence.get(key) for key in ("mythic_task_id", "task_id", "display_id")
                                if evidence.get(key) is not None))
    for fact in attr("_engagement_graph_facts", list):
        predicate = str(getattr(fact, "predicate", "") or "")
        for segment in predicate.split(":")[1:]:
            if "." in segment:
                domains.add(_name(segment))
        fact_hosts, fact_objects = _graph_fact_names(predicate)
        hosts.update(fact_hosts)
        objects.update(fact_objects)
    for cache in ("_task_source_request_ids", "_task_output_record_cache", "_task_output_cache"):
        tasks.update(str(key) for key in attr(cache, dict))
    last_task = getattr(mythic_client, "_last_issued_task_display_id", None)
    if isinstance(last_task, (int, str)) and str(last_task).strip():
        tasks.add(str(last_task).strip())
    last_callback = getattr(mythic_client, "_last_issued_callback_id", None)
    if isinstance(last_callback, (int, str)):
        callbacks.add(_callback(last_callback))
    for row in attr("_cred_cache", list):
        if isinstance(row, Mapping) and row.get("id") is not None:
            credentials.add(str(row.get("id")))
    for reference in attr("_credential_reference_bindings", dict).values():
        match = _REFERENCE_RE.match(str(reference))
        if match:
            credentials.add(match.group(2))
    bound = _callback(bound_callback)
    if bound:
        callbacks.add(bound)
    return KnownScope(
        operator_input=str(operator_input or ""),
        bound_callback=bound,
        callbacks=frozenset(callbacks - {""}),
        hosts=frozenset(hosts - {""}),
        domains=frozenset(domains - {""}),
        principals=frozenset(principals - {""}),
        tasks=frozenset(tasks - {""}),
        credentials=frozenset(credentials - {""}),
        objects=frozenset(objects - {""}),
    )


def validate_brief(brief: HandoffBrief, *, worker: str, require_scope: bool, scope: KnownScope,
                   worker_tools: Iterable[str] | None = None) -> tuple[str, ...]:
    """Return one error per failing field; empty when the brief may be dispatched."""
    errors: list[str] = []
    unknown = [name for name in brief.allowed_capabilities if name not in {*catalog_names(), CONTROL_PLANE}]
    if unknown:
        errors.append(f"allowed_capabilities: {', '.join(unknown)} is not a catalog procedure; name the step's "
                      "guarded tools in allowed_tools for work outside the catalog")
    if brief.allowed_capabilities and brief.allowed_tools:
        errors.append("name either allowed_capabilities or allowed_tools for a step, not both")
    if worker_tools is not None:
        unknown = [name for name in brief.allowed_tools if name not in set(worker_tools)]
        if unknown:
            errors.append(f"allowed_tools: {', '.join(unknown)} is not a guarded tool of {worker}")
    if CONTROL_PLANE in brief.allowed_capabilities and not brief.control_plane_only:
        errors.append(f"allowed_capabilities: `{CONTROL_PLANE}` cannot be combined with other classes")
    if require_scope and worker in TARGET_FACING_WORKERS:
        missing = ["expected_result"] if not brief.expected_result else []
        if not brief.control_plane_only:
            missing = [name for name, value in (
                ("callback_display_id", brief.callback_display_id),
                ("targets", brief.targets),
                ("allowed_capabilities or allowed_tools", brief.allowed_capabilities or brief.allowed_tools),
            ) if not value] + missing
        if missing:
            errors.append(
                f"missing required field(s): {', '.join(missing)}; a supervised {worker} step must name its "
                "callback, targets, what its one guarded action may use (catalog procedures or tools) and its "
                f"expected result, or set allowed_capabilities to [`{CONTROL_PLANE}`] for a step that only reads "
                "Mythic or BloodHound records"
            )
    if brief.callback_display_id:
        if scope.bound_callback and brief.callback_display_id != scope.bound_callback:
            errors.append(
                f"callback_display_id: {brief.callback_display_id} is not callback {scope.bound_callback}, "
                "the one this request is bound to"
            )
        elif brief.callback_display_id not in scope.callbacks:
            errors.append(
                f"callback_display_id: callback {brief.callback_display_id} is not one Sage has observed; "
                f"list callbacks first with a `{CONTROL_PLANE}` step"
            )
    unrequested = [target for target in brief.targets if not scope.names_target(target)]
    if unrequested:
        errors.append(
            f"targets: {', '.join(unrequested)} named neither in the operator's request nor in "
            "engagement state; a step may act only on requested or observed targets"
        )
    for reference in brief.inputs_by_reference:
        match = _REFERENCE_RE.match(reference)
        if not match:
            errors.append(f"inputs_by_reference: `{reference}` is not `task:<id>` or `cred:<id>`")
        elif (scope.tasks if match.group(1) == "task" else scope.credentials).isdisjoint({match.group(2)}):
            errors.append(f"inputs_by_reference: `{reference}` does not resolve to a record Sage holds")
    return tuple(errors)


def render_scope(brief: Mapping[str, Any] | None) -> str:
    """The step's enforced scope, as Sage tells the worker before its step; empty when the brief sets none.

    Only what Sage enforces is rendered: what the one guarded action may use, and the validated callback and
    targets. The Supervisor's free-text fields stay out of this Sage-authored block.
    """
    if not isinstance(brief, Mapping):
        return ""
    capabilities = [str(item) for item in brief.get("allowed_capabilities") or [] if str(item).strip()]
    tools = [str(item) for item in brief.get("allowed_tools") or [] if str(item).strip()]
    callback = str(brief.get("callback_display_id") or "").strip()
    if capabilities == [CONTROL_PLANE]:
        rule = ("This step only reads Mythic or BloodHound records: it takes no guarded action, and any guarded "
                "call is removed before an approval card and ends the step.")
    elif capabilities:
        example = json.dumps({"capability": capabilities[0], "callback_id": callback or "<callback id>"})
        allowed = ("only `execute_capability`, for " + ", ".join(f"`{name}`" for name in capabilities)
                   + f". Sage runs each as one procedure; name it in the call, e.g. action={example}. "
                   "Sage fills in the context it already holds (your foothold identity and graph-derived "
                   "facts), so you need not restate them. If a procedure needs an input Sage does not hold, "
                   "it tells you which one — supply it or obtain it, and do not invent a value.")
    elif tools:
        allowed = "only " + ", ".join(f"`{name}`" for name in tools)
    else:
        return ""
    if capabilities != [CONTROL_PLANE]:
        rule = (f"Its one guarded action may use {allowed}. Any other guarded call is removed before an approval "
                "card and ends the step.")
    lines = ["<step_scope>", f"Sage checks this step against its brief. {rule}"]
    if callback:
        lines.append(f"Callback: {callback}.")
    targets = [str(item) for item in brief.get("targets") or [] if str(item).strip()]
    if targets:
        lines.append("Targets: " + json.dumps(targets, ensure_ascii=False) + ".")
    lines.append("</step_scope>")
    return "\n".join(lines)


def restatement(instruction: str, operator_input: str) -> float:
    """Share of the assignment's words that repeat the operator input. A metric only; never a reason to reject."""
    words = [word for word in _WORD_RE.findall(str(instruction or "").casefold()) if len(word) > 2]
    if not words:
        return 0.0
    source = set(_WORD_RE.findall(str(operator_input or "").casefold()))
    return round(sum(word in source for word in words) / len(words), 3)


def scope_signature(allowed_capabilities: Iterable[Any] = (), allowed_tools: Iterable[Any] = ()) -> str:
    """A stable identity for a step's execution scope, so re-sending the SAME scope is detectable.

    Keyed on the scope the Supervisor chose, never on the payload command: a changed command under one scope is
    progress and resets nothing, while repeating a scope that keeps failing at execution is the stuck loop.
    """
    tools = sorted(str(item) for item in (allowed_tools or ()))
    if tools:
        return "tools:" + ",".join(tools)
    caps = sorted(str(item).casefold() for item in (allowed_capabilities or ()))
    if caps:
        return "capabilities:" + ",".join(caps)
    return ""


def _event_meta(event: Any, key: str) -> str:
    for name, value in getattr(event, "metadata", ()) or ():
        if name == key:
            return str(value)
    return ""


def scope_resend_exhausted(events: Iterable[Any], signature: str) -> bool:
    """Whether re-sending `signature` unchanged would only fail again at execution.

    A scope whose own action named an excluded capability is wrong as it stands, so one such rejection ends it. A
    worker calling a tool its step does not offer is a deviation the same scope may retry once; a second ends it.
    Rejections recorded without a kind count as wrong scopes."""
    tail = []
    for event in reversed(tuple(events)):
        if not signature or _event_meta(event, "scope_signature") != signature:
            break
        tail.append(event)
    if not tail:
        return False
    return len(tail) >= REJECTIONS_BEFORE_STOP or any(_event_meta(event, "kind") != "not_offered" for event in tail)


def rejection_count(events: Iterable[Any]) -> int:
    """Consecutive brief rejections at the end of a request's brief events."""
    count = 0
    for event in events:
        count = count + 1 if getattr(event, "phase", "") == "rejected" else 0
    return count
