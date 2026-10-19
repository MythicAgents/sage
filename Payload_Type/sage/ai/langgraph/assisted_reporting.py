"""Bounded, source-owned observations for identity reports.

The caller supplies only records produced by the matched Mythic task-output path.
This module recognizes two narrow output representations. It does not infer
account type or establish authentication/access.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import re
from typing import Any, Iterable


_ORIGINS = frozenset({"current_request", "prior_sage_request", "historical_or_external"})
_IDENTITY = re.compile(r"[A-Za-z0-9._-]+\\[A-Za-z0-9._$-]+\Z")
_APOLLO_WHOAMI = re.compile(
    r"Primary Identity: (?P<primary>[A-Za-z0-9._-]+\\[A-Za-z0-9._$-]+)\r?\n"
    r"Impersonation Identity: (?P<impersonation>[A-Za-z0-9._-]+\\[A-Za-z0-9._$-]+)\Z"
)
_NAME = re.compile(r"[A-Za-z0-9._-]+\Z")


@dataclass(frozen=True)
class Source:
    request_id: str
    task_id: int
    origin: str
    callback_display_id: int | None
    stored_command_name: str | None
    task_timestamp: str | None
    response_timestamps: tuple[str, ...]
    partial_snapshot: bool
    truncated: bool
    scoped_content_omitted: bool
    source_metadata_conflict: bool
    collection_witnesses: tuple[str, ...] = ()
    response_ids: tuple[int, ...] = ()
    collection_witnesses_omitted: int = 0


@dataclass(frozen=True)
class Observation:
    kind: str
    value: str
    source: Source


@dataclass(frozen=True)
class ReportProjection:
    observations: tuple[Observation, ...]
    source_count: int


def _source(record: Any) -> Source | None:
    if not isinstance(record, dict):
        return None
    request_id, task_id, origin = (record.get(key) for key in ("request_id", "task_id", "origin"))
    if (not isinstance(request_id, str) or not request_id or len(request_id) > 256
            or type(task_id) is not int or task_id < 0
            or not isinstance(origin, str) or origin not in _ORIGINS):
        return None
    callback = record.get("callback_display_id")
    if callback is not None and (type(callback) is not int or callback < 0):
        return None
    command = record.get("stored_command_name")
    if command is not None and (not isinstance(command, str) or len(command) > 128):
        return None
    task_timestamp = record.get("task_timestamp")
    if task_timestamp is not None and (not isinstance(task_timestamp, str)
                                      or len(task_timestamp) > 128):
        return None
    response_timestamps = record.get("response_timestamps", [])
    if (not isinstance(response_timestamps, list) or len(response_timestamps) > 16
            or any(not isinstance(item, str) or len(item) > 128
                   for item in response_timestamps)):
        return None
    for flag in ("partial_snapshot", "truncated", "scoped_content_omitted"):
        if type(record.get(flag)) is not bool:
            return None
    conflict = record.get("source_metadata_conflict", False)
    if type(conflict) is not bool:
        return None
    witnesses = record.get("collection_witnesses", [])
    if (not isinstance(witnesses, list) or len(witnesses) > 8
            or any(not isinstance(item, str) or len(item) > 512 for item in witnesses)):
        return None
    witness_omissions = record.get("collection_witnesses_omitted", 0)
    if type(witness_omissions) is not int or witness_omissions < 0:
        return None
    response_sources = record.get("response_sources", [])
    if not isinstance(response_sources, list) or len(response_sources) > 16:
        return None
    response_ids = tuple(item["response_id"] for item in response_sources
        if isinstance(item, dict) and type(item.get("response_id")) is int)
    return Source(request_id, task_id, origin, callback, command, task_timestamp,
                  tuple(response_timestamps), record["partial_snapshot"],
                  record["truncated"], record["scoped_content_omitted"], conflict,
                  tuple(witnesses), response_ids, witness_omissions)


def _membership(text: str) -> str | None:
    """Recognize only the explicit four-field host representation."""
    lines = text.splitlines()
    if len(lines) != 4:
        return None
    fields: dict[str, str] = {}
    for line in lines:
        if line.count(": ") != 1:
            return None
        key, value = line.split(": ", 1)
        if key in fields or key not in {"Host", "PartOfDomain", "Domain", "Trust inventory"}:
            return None
        fields[key] = value
    if (set(fields) != {"Host", "PartOfDomain", "Domain", "Trust inventory"}
            or not _NAME.fullmatch(fields["Host"])
            or fields["PartOfDomain"] not in {"True", "False"}
            or not _NAME.fullmatch(fields["Domain"])
            or fields["Trust inventory"] != "not collected"):
        return None
    return (f"Host {fields['Host']} reported PartOfDomain: {fields['PartOfDomain']}; "
            f"Domain: {fields['Domain']}; trust inventory: not collected")


def project_observations(records: Iterable[dict[str, Any]]) -> ReportProjection:
    """Project narrow facts from already matched runtime observation records.

    A record with incomplete/ambiguous provenance or output cannot promote a
    semantic fact. Repeated task IDs are ambiguous within this supplied scope.
    Missing callback/time metadata remains visible in rendered source references.
    """
    supplied = list(records)
    counts: dict[int, int] = {}
    for record in supplied:
        if isinstance(record, dict) and type(record.get("task_id")) is int:
            task_id = record["task_id"]
            counts[task_id] = counts.get(task_id, 0) + 1
    observations: list[Observation] = []
    for record in supplied:
        source = _source(record)
        if (source is None or counts[source.task_id] != 1
                or source.source_metadata_conflict
                or not source.partial_snapshot or source.truncated
                or source.scoped_content_omitted):
            continue
        excerpts = record.get("response_text_excerpts")
        if (not isinstance(excerpts, list) or len(excerpts) != 1
                or not isinstance(excerpts[0], str) or len(excerpts[0]) > 32_000):
            continue
        text = excerpts[0]
        if source.stored_command_name == "whoami":
            identity = text.strip()
            if text.count("\n") == 0 and _IDENTITY.fullmatch(identity):
                observations.append(Observation("reported_identity", identity, source))
            else:
                apollo = _APOLLO_WHOAMI.fullmatch(text)
                if apollo is not None:
                    observations.extend((
                        Observation("reported_primary_identity", apollo["primary"], source),
                        Observation("reported_impersonation_identity", apollo["impersonation"], source),
                    ))
        membership = _membership(text)
        if membership is not None:
            observations.append(Observation("reported_host_membership", membership, source))
    return ReportProjection(tuple(observations), len(supplied))


def _cell(value: Any) -> str:
    result = escape(str(value), quote=False)
    for marker in ("\\", "`", "[", "]", "*", "_", "|", "\n", "\r"):
        result = result.replace(marker, f"&#{ord(marker)};")
    return result


def _source_label(source: Source) -> str:
    return (f"task {source.task_id}; callback {source.callback_display_id if source.callback_display_id is not None else 'not supplied'}; "
            f"collected {source.request_id}; origin {source.origin}; "
            f"collection witness {', '.join(source.collection_witnesses) or 'not supplied'}; "
            f"additional witnesses omitted {source.collection_witnesses_omitted}; "
            f"response IDs {', '.join(str(item) for item in source.response_ids) or 'not supplied'}; "
            f"task time {source.task_timestamp or 'not supplied'}; "
            f"response time {', '.join(source.response_timestamps) or 'not supplied'}; "
            f"partial {source.partial_snapshot}; truncated {source.truncated}; "
            f"scoped content omitted {source.scoped_content_omitted}; "
            f"metadata conflict {source.source_metadata_conflict}")


def render_identity_report(records: Iterable[dict[str, Any]], *,
                           operation_scope: tuple[int, str] | None = None,
                           omitted_sources: int = 0,
                           capacity_reached: bool = False) -> str:
    """Render a bounded identity/access report from matched task records alone."""
    supplied = list(records)
    projection = project_observations(supplied)
    scope = (f"operation {operation_scope[0]} engagement {operation_scope[1]}"
             if operation_scope else "this session")
    lines = ["### Runtime observations in supplied task scope", "",
             f"This report covers up to eight retained task observations from {scope}; it is not a complete inventory.", "",
             "| Observation | Reported value | Source and limits |",
             "|---|---|---|"]
    if omitted_sources:
        lines.insert(3, f"{omitted_sources} additional task source(s) omitted by the display limit.")
    if capacity_reached:
        lines.insert(3, "Retained observation capacity was reached; later collections may be absent.")
    for observation in projection.observations:
        label = {
            "reported_identity": "Identity string",
            "reported_primary_identity": "Primary identity string",
            "reported_impersonation_identity": "Impersonation identity string",
            "reported_host_membership": "Host membership",
        }[observation.kind]
        lines.append(f"| {_cell(label)} | {_cell(observation.value)} | {_cell(_source_label(observation.source))} |")
    if not projection.observations:
        lines.append("| No supported identity or membership observation | unknown | Supplied task scope |")
    lines.extend(["", "| Unestablished proposition | Status | Scope |", "|---|---|---|",
                  "| Account type | unknown | Supplied task scope |",
                  "| Authentication or access to a target | unknown | Supplied task scope; no supported operation adapter in this slice |",
                  "", "These observations describe task output at collection time. They do not establish current state.",
                  "", "### Bounded task output excerpts", "",
                  "The text below is reported output, not a verified interpretation. Incomplete or conflicting sources cannot establish the observations above.",
                  "", "| Source and limits | Literal excerpt (JSON escaped) |", "|---|---|"])
    repeated: set[int] = set()
    task_ids = [item.get("task_id") for item in supplied if isinstance(item, dict)
                and type(item.get("task_id")) is int]
    for task_id in task_ids:
        if task_ids.count(task_id) > 1:
            repeated.add(task_id)
    shown = 0
    for record in supplied:
        source = _source(record)
        if source is None:
            continue
        excerpts = record.get("response_text_excerpts")
        if not isinstance(excerpts, list) or any(not isinstance(item, str) for item in excerpts):
            literal = "no valid text excerpts supplied"
        else:
            literal = json.dumps(excerpts, ensure_ascii=False)
        clipped = len(literal) > 4096
        if clipped:
            literal = literal[:4096]
        source_label = _source_label(source)
        if source.task_id in repeated:
            source_label += "; repeated task ID in supplied scope"
        if clipped:
            source_label += "; excerpt display clipped"
        lines.append(f"| {_cell(source_label)} | {_cell(literal)} |")
        shown += 1
    if not shown:
        lines.append("| No valid task provenance supplied | No source excerpt |")
    identity = any(item.kind in {"reported_identity", "reported_primary_identity",
                                 "reported_impersonation_identity"}
                   for item in projection.observations)
    membership = any(item.kind == "reported_host_membership" for item in projection.observations)
    lines.extend(["", "### Next checks", ""])
    if identity:
        lines.append("- Recheck the current principal on the intended callback before relying on the reported identity string.")
    else:
        lines.append("- Collect a current identity string with `whoami` on the intended callback; the supplied records do not establish one.")
    if membership:
        lines.append("- If directory context matters, collect a current host membership and trust inventory; the reported membership is a collection-time observation and trust inventory was not established.")
    else:
        lines.append("- Collect current host membership on the intended callback if directory context matters; the supplied records do not establish it.")
    lines.append("- If target access matters, verify authentication and access with an appropriately authorized task and inspect its output; identity and host membership alone do not establish access.")
    return "\n".join(lines)


def render_report(narrative: str, records: Iterable[dict[str, Any]]) -> str:
    """Compatibility entry point; model narrative has no report authority."""
    return render_identity_report(records)
