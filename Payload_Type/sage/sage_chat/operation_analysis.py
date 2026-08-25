"""Deterministic correlation over operation-scoped Mythic evidence.

The analyzer consumes only typed observations already persisted by
``OperationMemoryStore``.  It has no model, prompt, tool, callback-tasking, or
target-network surface.  Unrecognized and free-form records remain inert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import PureWindowsPath
import re
from typing import Any, Iterable, Mapping

from .operation_findings import EvidencePointer, FindingCandidate, FindingState
from .operation_memory import OperationMemoryStore, _required_text


MINIMUM_SEPARATION_SECONDS_EXCLUSIVE = 86_400

_SUCCESS_STATES = frozenset({"completed", "success"})
_SYSTEM_IDENTITY = "nt authority\\system"
_APOLLO_PAYLOAD_TYPE = "apollo"
_SCHEDULED_TASK_STATE_KEYS = frozenset(
    {"scheduled task state", "scheduledtaskstate", "enabled"}
)
_SHARPUP_PATH_KEYS = frozenset(
    {"file path", "path", "task file", "task file path", "task to run"}
)


def _normalized_resource(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip().replace("/", "\\").casefold()


def _normalized_identity(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value.casefold()


def _timestamp(value: Any) -> datetime:
    text = _required_text(value, "observed_at_utc")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observed_at_utc must include a timezone")
    return parsed


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True, order=True)
class CorrelatedEvidencePointer:
    operation_id: str
    record_class: str
    source_record_id: str
    revision_sha256: str
    callback_display_id: str
    task_display_id: str
    task_output_id: str
    observed_at_utc: str

    @classmethod
    def from_record(cls, operation_id: str, row: Mapping[str, Any]) -> "CorrelatedEvidencePointer":
        return cls(
            operation_id=operation_id,
            record_class=_required_text(row.get("record_class"), "record_class"),
            source_record_id=_required_text(
                row.get("source_record_id"), "source_record_id"
            ),
            revision_sha256=_required_text(
                row.get("revision_sha256"), "revision_sha256"
            ),
            callback_display_id=_required_text(
                row.get("callback_display_id"), "callback_display_id"
            ),
            task_display_id=_required_text(
                row.get("task_display_id"), "task_display_id"
            ),
            task_output_id=_required_text(
                row.get("task_output_id"), "task_output_id"
            ),
            observed_at_utc=_required_text(
                row.get("observed_at_utc"), "observed_at_utc"
            ),
        )

    @property
    def record_content_sha256(self) -> str:
        return self.revision_sha256

    def as_finding_pointer(self) -> EvidencePointer:
        return EvidencePointer.build(
            record_class=self.record_class,
            source_record_id=self.source_record_id,
            revision_sha256=self.revision_sha256,
            callback_display_id=self.callback_display_id,
            task_display_id=self.task_display_id,
            task_output_id=self.task_output_id,
        )

    def as_oracle_pointer(self) -> dict[str, str]:
        return {
            "callback_display_id": self.callback_display_id,
            "observed_at_utc": self.observed_at_utc,
            "operation_id": self.operation_id,
            "record_class": self.record_class,
            "record_content_sha256": self.record_content_sha256,
            "task_display_id": self.task_display_id,
            "task_output_id": self.task_output_id,
        }


@dataclass(frozen=True)
class _PeriodicFact:
    callback_id: str
    enabled: bool
    host_key: str
    job_key: str
    path: str
    run_as: str
    observed_at: datetime
    pointer: CorrelatedEvidencePointer


@dataclass(frozen=True)
class _WriteFact:
    callback_id: str
    callback_user: str
    effective_write: bool
    host_key: str
    path: str
    principal: str
    observed_at: datetime
    pointer: CorrelatedEvidencePointer


@dataclass(frozen=True)
class CorrelatedFinding:
    operation_id: str
    finding_key: str
    state: FindingState
    observed_at_utc: str
    host_key: str
    canonical_path: str
    job_key: str
    run_as: str
    actor_key: str
    evidence: tuple[CorrelatedEvidencePointer, ...]
    missing_assumptions: tuple[str, ...]

    finding_type: str = "privileged-writable-execution-target"
    impact: str = "possible code execution as the privileged scheduled identity"
    confidence_label: str = "medium"
    suggested_validation_family: str = "supervised-recheck-and-bounded-safe-proof"
    guarded_actions: int = 0

    def as_candidate(self) -> FindingCandidate:
        return FindingCandidate.build(
            operation_id=self.operation_id,
            finding_key=self.finding_key,
            finding_type=self.finding_type,
            title=f"Privileged execution target on {self.host_key}: {self.canonical_path}",
            state=self.state,
            score=1.0,
            observed_at_utc=self.observed_at_utc,
            confidence=0.7,
            evidence=(pointer.as_finding_pointer() for pointer in self.evidence),
            missing_assumptions=self.missing_assumptions,
            rationale=(
                "A privileged periodic execution binding and an exact-principal "
                "effective-write observation intersect on the same host and full path."
            ),
            suggested_validation="Supervised recheck and bounded safe proof.",
        )

    def as_oracle_finding(self, *, rank: int | None) -> dict[str, Any]:
        return {
            "actor": {"actor_key": self.actor_key},
            "confidence": self.confidence_label,
            "evidence_pointers": [pointer.as_oracle_pointer() for pointer in self.evidence],
            "finding_key": self.finding_key,
            "finding_type": self.finding_type,
            "guarded_actions": self.guarded_actions,
            "impact": self.impact,
            "missing_assumptions": list(self.missing_assumptions),
            "observed_at_utc": self.observed_at_utc,
            "rank": rank,
            "state": self.state.value,
            "suggested_validation_family": self.suggested_validation_family,
            "target": {
                "canonical_path": self.canonical_path,
                "host_key": self.host_key,
                "job_key": self.job_key,
                "run_as": self.run_as,
            },
        }


@dataclass(frozen=True)
class OperationAnalysisResult:
    operation_id: str
    findings: tuple[CorrelatedFinding, ...]
    missing_evidence: tuple[str, ...]

    @property
    def candidates(self) -> tuple[FindingCandidate, ...]:
        return tuple(finding.as_candidate() for finding in self.findings)


def _task_parameters(task: Mapping[str, Any]) -> Mapping[str, Any]:
    for field in ("params", "original_params"):
        value = task.get(field)
        if isinstance(value, Mapping):
            return value
        if not isinstance(value, str) or not value.strip().startswith("{"):
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _callback_payload_type(callback: Mapping[str, Any]) -> str:
    payload = _mapping(callback.get("payload"))
    payload_type = _mapping(payload.get("payloadtype"))
    return str(payload_type.get("name") or "").strip().casefold()


def _bound_native_output(
    row: Mapping[str, Any],
    task: Mapping[str, Any],
    callback: Mapping[str, Any],
    task_id: str,
    callback_id: str,
) -> tuple[str, Mapping[str, Any]] | None:
    if row.get("record_class") != "task_output" or row.get("content_kind") != "text":
        return None
    inline = row.get("inline_text")
    if not isinstance(inline, str) or not inline.strip():
        return None
    if _callback_payload_type(callback) != _APOLLO_PAYLOAD_TYPE:
        return None
    command = str(task.get("command_name") or "").strip().casefold()
    source_task = _mapping(_mapping(row.get("metadata")).get("task"))
    source_callback = _mapping(source_task.get("callback"))
    if (
        not command
        or str(source_task.get("display_id") or "").strip() != task_id
        or str(source_callback.get("display_id") or "").strip() != callback_id
        or str(source_task.get("command_name") or "").strip().casefold() != command
    ):
        return None
    return command, _task_parameters(task)


def _has_switch(arguments: str, switch: str) -> bool:
    return re.search(
        rf"(?:^|\s){re.escape(switch)}(?:\s|$)", arguments, re.IGNORECASE
    ) is not None


def _sch_tasks_parameters(parameters: Mapping[str, Any]) -> bool:
    executable = str(parameters.get("executable") or "").strip()
    arguments = str(parameters.get("arguments") or "").strip()
    return (
        PureWindowsPath(executable).name.casefold() in {"schtasks", "schtasks.exe"}
        and _has_switch(arguments, "/query")
        and _has_switch(arguments, "/v")
        and re.search(r"(?:^|\s)/fo\s+list(?:\s|$)", arguments, re.IGNORECASE)
        is not None
    )


def _sharpup_parameters(parameters: Mapping[str, Any]) -> bool:
    assembly = str(
        parameters.get("assembly_name") or parameters.get("assembly") or ""
    ).strip()
    arguments = str(parameters.get("assembly_arguments") or "").strip()
    return (
        PureWindowsPath(assembly).name.casefold() == "sharpup.exe"
        and arguments.casefold() == "modifiablescheduledtaskfiles"
    )


def _label_blocks(text: str) -> tuple[dict[str, str], ...]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([^:\r\n]+?)\s*:(.*)$", line)
        if match is None:
            if not line.strip() and current:
                blocks.append(current)
                current = {}
            continue
        key = match.group(1).strip().casefold()
        if key == "hostname" and current:
            blocks.append(current)
            current = {}
        value = match.group(2)
        current[key] = value[1:] if value.startswith(" ") else value
    if current:
        blocks.append(current)
    return tuple(blocks)


def _execution_path(command_line: Any) -> str:
    text = _required_text(command_line, "task executable")
    if text.startswith('"'):
        end = text.find('"', 1)
        if end < 2:
            raise ValueError("task executable has an unterminated quote")
        return text[1:end]
    match = re.match(
        r"^(.+?\.(?:bat|cmd|com|exe|js|msi|ps1|scr|vbs))(?:\s|$)",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match is not None else text


def _periodic_facts(
    callback_id: str,
    text: str,
    pointer: CorrelatedEvidencePointer,
    observed_at: datetime,
) -> tuple[_PeriodicFact, ...]:
    facts: list[_PeriodicFact] = []
    for block in _label_blocks(text):
        enabled = next(
            (block[key] for key in _SCHEDULED_TASK_STATE_KEYS if key in block), ""
        ).casefold()
        schedule = block.get("schedule type", "").casefold()
        if enabled not in {"disabled", "enabled", "false", "true"} or schedule in {
            "",
            "n/a",
            "once",
            "one time",
        }:
            continue
        try:
            facts.append(
                _PeriodicFact(
                    callback_id=callback_id,
                    enabled=enabled in {"enabled", "true"},
                    host_key=_normalized_resource(block.get("hostname"), "host name"),
                    job_key=_normalized_resource(block.get("taskname"), "task name"),
                    path=_normalized_resource(
                        _execution_path(block.get("task to run")), "task executable"
                    ),
                    run_as=_normalized_identity(block.get("run as user"), "run as user"),
                    observed_at=observed_at,
                    pointer=pointer,
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(facts)


def _write_facts(
    callback_id: str,
    callback: Mapping[str, Any],
    text: str,
    pointer: CorrelatedEvidencePointer,
    observed_at: datetime,
) -> tuple[_WriteFact, ...]:
    if re.search(
        r"^\s*\[\+\]\s*modifiable\s+scheduled\s+task\s+files?\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    ) is None:
        return ()
    facts: list[_WriteFact] = []
    for block in _label_blocks(text):
        path = next((block[key] for key in _SHARPUP_PATH_KEYS if key in block), "")
        declared_principal = block.get("principal", "")
        access = block.get("effective write", "true").casefold()
        if not path or not declared_principal or access not in {"false", "true"}:
            continue
        try:
            callback_user = _normalized_identity(callback.get("user"), "callback user")
            principal = _normalized_identity(declared_principal, "principal")
            if principal != callback_user:
                continue
            facts.append(
                _WriteFact(
                    callback_id=callback_id,
                    callback_user=callback_user,
                    effective_write=access == "true",
                    host_key=_normalized_resource(callback.get("host"), "callback host"),
                    path=_normalized_resource(path, "task file path"),
                    principal=principal,
                    observed_at=observed_at,
                    pointer=pointer,
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(facts)


def _facts(
    operation_id: str, records: tuple[Mapping[str, Any], ...]
) -> tuple[tuple[_PeriodicFact, ...], tuple[_WriteFact, ...]]:
    callbacks: dict[str, Mapping[str, Any]] = {}
    tasks: dict[str, Mapping[str, Any]] = {}
    for row in records:
        if _required_text(row.get("operation_id"), "operation_id") != operation_id:
            raise ValueError("operation analysis received a cross-operation record")
        metadata = _mapping(row.get("metadata"))
        if row.get("record_class") == "callback":
            display_id = str(metadata.get("display_id") or "").strip()
            if display_id:
                callbacks[display_id] = metadata
        elif row.get("record_class") == "task":
            display_id = str(metadata.get("display_id") or "").strip()
            if display_id:
                tasks[display_id] = metadata

    periodic: list[_PeriodicFact] = []
    writes: list[_WriteFact] = []
    for row in records:
        task_id = str(row.get("task_display_id") or "").strip()
        callback_id = str(row.get("callback_display_id") or "").strip()
        task = tasks.get(task_id)
        callback = callbacks.get(callback_id)
        if task is None or callback is None:
            continue
        task_callback = _mapping(task.get("callback"))
        if str(task_callback.get("display_id") or "").strip() != callback_id:
            continue
        if task.get("completed") is not True:
            continue
        if str(task.get("status") or "").strip().casefold() not in _SUCCESS_STATES:
            continue
        native = _bound_native_output(row, task, callback, task_id, callback_id)
        if native is None:
            continue
        command, parameters = native
        try:
            pointer = CorrelatedEvidencePointer.from_record(operation_id, row)
            observed_at = _timestamp(row.get("observed_at_utc"))
            if command == "run" and _sch_tasks_parameters(parameters):
                periodic.extend(
                    _periodic_facts(callback_id, row["inline_text"], pointer, observed_at)
                )
            elif command == "execute_assembly" and _sharpup_parameters(parameters):
                writes.extend(
                    _write_facts(
                        callback_id,
                        callback,
                        row["inline_text"],
                        pointer,
                        observed_at,
                    )
                )
        except (TypeError, ValueError):
            continue
    return tuple(periodic), tuple(writes)


def _finding_key(operation_id: str, fact_a: _PeriodicFact) -> str:
    return hashlib.sha256(
        "\0".join(
            (operation_id, fact_a.host_key, fact_a.job_key, fact_a.path)
        ).encode()
    ).hexdigest()


def _correlated_finding(
    operation_id: str,
    fact_a: _PeriodicFact,
    fact_b: _WriteFact,
    periodic: tuple[_PeriodicFact, ...],
    writes: tuple[_WriteFact, ...],
) -> CorrelatedFinding:
    newer_than = max(fact_a.observed_at, fact_b.observed_at)
    contradictions: list[tuple[str, datetime, CorrelatedEvidencePointer]] = []
    contradictions.extend(
        ("binding-disabled", row.observed_at, row.pointer)
        for row in periodic
        if not row.enabled
        and row.host_key == fact_a.host_key
        and row.path == fact_a.path
        and row.job_key == fact_a.job_key
        and row.run_as == fact_a.run_as
        and row.observed_at > newer_than
    )
    contradictions.extend(
        ("access-revoked", row.observed_at, row.pointer)
        for row in writes
        if not row.effective_write
        and row.host_key == fact_a.host_key
        and row.path == fact_a.path
        and row.principal == fact_b.principal
        and row.callback_user == row.principal
        and row.observed_at > newer_than
    )
    contradiction = max(contradictions, key=lambda item: item[1], default=None)
    if contradiction is None:
        state = FindingState.NEW
        evidence = (fact_a.pointer, fact_b.pointer)
        assumptions = ("binding remains current", "write access remains current")
        observed_at = fact_b.pointer.observed_at_utc
    else:
        kind, _, pointer = contradiction
        state = FindingState.INVALIDATED
        evidence = (fact_a.pointer, fact_b.pointer, pointer)
        assumptions = (
            ("write access remains current",)
            if kind == "binding-disabled"
            else ("binding remains current",)
        )
        observed_at = pointer.observed_at_utc
    return CorrelatedFinding(
        operation_id=operation_id,
        finding_key=_finding_key(operation_id, fact_a),
        state=state,
        observed_at_utc=observed_at,
        host_key=fact_a.host_key,
        canonical_path=fact_a.path,
        job_key=fact_a.job_key,
        run_as=fact_a.run_as,
        actor_key=fact_b.principal,
        evidence=tuple(sorted(evidence)),
        missing_assumptions=assumptions,
    )


def analyze_operation_records(
    operation_id: Any, records: Iterable[Mapping[str, Any]]
) -> OperationAnalysisResult:
    """Correlate exact typed observations without interpreting free-form text."""
    operation = _required_text(operation_id, "operation_id")
    materialized = tuple(records)
    periodic, writes = _facts(operation, materialized)
    enabled = tuple(
        row for row in periodic if row.enabled and row.run_as == _SYSTEM_IDENTITY
    )
    effective = tuple(row for row in writes if row.effective_write)

    qualifying: dict[str, tuple[_PeriodicFact, _WriteFact]] = {}
    for fact_a in enabled:
        for fact_b in effective:
            if fact_a.host_key != fact_b.host_key or fact_a.path != fact_b.path:
                continue
            if fact_b.principal != fact_b.callback_user:
                continue
            separation = (fact_b.observed_at - fact_a.observed_at).total_seconds()
            if (
                fact_a.callback_id == fact_b.callback_id
                or separation <= MINIMUM_SEPARATION_SECONDS_EXCLUSIVE
            ):
                continue
            key = _finding_key(operation, fact_a)
            pair = qualifying.get(key)
            if pair is None or (
                fact_b.observed_at,
                fact_a.observed_at,
                fact_b.pointer,
                fact_a.pointer,
            ) > (
                pair[1].observed_at,
                pair[0].observed_at,
                pair[1].pointer,
                pair[0].pointer,
            ):
                qualifying[key] = (fact_a, fact_b)

    findings = tuple(
        _correlated_finding(operation, fact_a, fact_b, periodic, writes)
        for _, (fact_a, fact_b) in sorted(qualifying.items())
    )
    if findings:
        missing: tuple[str, ...] = ()
    elif not enabled and not effective:
        missing = ("effective_write", "privileged_periodic_exec")
    elif not enabled:
        missing = ("privileged_periodic_exec",)
    elif not effective:
        missing = ("effective_write",)
    else:
        missing = ("qualifying_cross_callback_cross_day_join",)
    return OperationAnalysisResult(
        operation_id=operation,
        findings=findings,
        missing_evidence=missing,
    )


async def analyze_seeded_operation(
    store: OperationMemoryStore, operation_id: Any
) -> OperationAnalysisResult:
    """Read the operation's current record heads and run the pure analyzer."""
    operation = _required_text(operation_id, "operation_id")
    return analyze_operation_records(operation, await store.list_records(operation))
