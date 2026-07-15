"""Redacted multi-iteration proposer/provider canary for Phase 4 readiness.

The canary is a bounded orchestration contract, not a candidate generator.  It accepts
an injected executor, preserves every transient attempt, applies one preregistered
retry/failover policy, and fails closed when safeguard termination, refusal, backend
mismatch, proxy failure exhaustion, or incomplete output occurs.  No failure becomes
a candidate efficacy score.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
import re
import signal
from typing import Any, Callable, Iterable, Protocol, Sequence

from .experiment_contracts import content_hash
from ..trajectory.schema import redact_text


RETRIABLE_FAILURES = frozenset({"provider_error", "proxy_failure", "timeout"})
UNRECOVERABLE_FAILURES = frozenset(
    {"safeguard_termination", "refusal", "backend_mismatch", "incomplete_output"}
)


@dataclass(frozen=True)
class ProviderRoute:
    provider: str
    effective_backend: str


@dataclass(frozen=True)
class ProposerOutcome:
    status: str
    output: str = ""
    effective_backend: str = ""
    process_group_id: int | None = None
    child_process_ids: tuple[int, ...] = field(default_factory=tuple)
    detail: str = ""


@dataclass(frozen=True)
class ProposerAttempt:
    iteration: int
    route_index: int
    retry_index: int
    provider: str
    expected_backend: str
    effective_backend: str
    status: str
    retriable: bool
    terminal: bool
    redacted_input_hash: str
    output_hash: str
    detail: str = ""
    process_group_id: int | None = None
    child_process_ids: tuple[int, ...] = field(default_factory=tuple)
    candidate_score: float | None = None


@dataclass(frozen=True)
class ProposerCanaryReport:
    iterations_requested: int
    iterations_completed: int
    retry_policy: dict[str, Any]
    failover_policy: dict[str, Any]
    attempts: tuple[ProposerAttempt, ...]
    killed_process_groups: tuple[int, ...]
    orphan_process_ids: tuple[int, ...]
    terminal_disposition: str
    passed: bool
    failures: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iterations_requested": self.iterations_requested,
            "iterations_completed": self.iterations_completed,
            "retry_policy": dict(self.retry_policy),
            "failover_policy": dict(self.failover_policy),
            "attempts": [asdict(item) for item in self.attempts],
            "killed_process_groups": list(self.killed_process_groups),
            "orphan_process_ids": list(self.orphan_process_ids),
            "terminal_disposition": self.terminal_disposition,
            "passed": self.passed,
            "failures": list(self.failures),
        }


class ProcessController(Protocol):
    def kill_process_group(self, process_group_id: int) -> None: ...

    def detect_orphans(self, child_process_ids: Sequence[int]) -> Sequence[int]: ...


class SystemProcessController:
    """Best-effort default used only when a real canary executor supplies process IDs."""

    def kill_process_group(self, process_group_id: int) -> None:
        try:
            os.killpg(int(process_group_id), signal.SIGTERM)
        except ProcessLookupError:
            return

    def detect_orphans(self, child_process_ids: Sequence[int]) -> Sequence[int]:
        orphans: list[int] = []
        for pid in child_process_ids:
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                orphans.append(int(pid))
            else:
                orphans.append(int(pid))
        return tuple(orphans)


Executor = Callable[[str, ProviderRoute, int, int], ProposerOutcome]

_REDACTED_MARKER_RE = re.compile(
    r"<(?:password:redacted|base64_blob|local-path:redacted|"
    r"(?:ntlm|aes256|sage-secret|secret):sha256:[^>]+)>"
)


def _input_is_redacted(value: str) -> bool:
    normalized = _REDACTED_MARKER_RE.sub("redacted", value)
    return redact_text(normalized) == normalized


def _normalized_outcome(outcome: ProposerOutcome, route: ProviderRoute) -> ProposerOutcome:
    status = str(outcome.status or "").strip().casefold()
    effective_backend = str(outcome.effective_backend or "").strip()
    if status == "success" and effective_backend != route.effective_backend:
        status = "backend_mismatch"
    if status == "success" and not str(outcome.output or "").strip():
        status = "incomplete_output"
    return ProposerOutcome(
        status=status,
        output=str(outcome.output or ""),
        effective_backend=effective_backend,
        process_group_id=outcome.process_group_id,
        child_process_ids=tuple(int(item) for item in outcome.child_process_ids),
        detail=str(outcome.detail or ""),
    )


def run_proposer_canary(
    *,
    redacted_input: str,
    routes: Sequence[ProviderRoute],
    executor: Executor,
    iterations: int = 3,
    max_retries_per_route: int = 1,
    process_controller: ProcessController | None = None,
    sealed_feedback_fragments: Sequence[str] = (),
) -> ProposerCanaryReport:
    """Run a bounded proposer canary without ever converting infra failure into score."""

    controller = process_controller or SystemProcessController()
    attempts: list[ProposerAttempt] = []
    killed_groups: list[int] = []
    child_pids: list[int] = []
    failures: list[str] = []
    completed = 0
    terminal_disposition = "completed"
    clean_input = str(redacted_input or "")
    sealed_feedback_exposed = any(
        str(fragment or "").strip()
        and str(fragment).casefold() in clean_input.casefold()
        for fragment in sealed_feedback_fragments
    )
    if not _input_is_redacted(clean_input) or sealed_feedback_exposed:
        return ProposerCanaryReport(
            iterations_requested=max(0, int(iterations or 0)),
            iterations_completed=0,
            retry_policy={
                "max_retries_per_route": max(0, int(max_retries_per_route or 0)),
                "retriable_statuses": sorted(RETRIABLE_FAILURES),
            },
            failover_policy={"routes": [asdict(route) for route in routes], "ordered": True},
            attempts=(),
            killed_process_groups=(),
            orphan_process_ids=(),
            terminal_disposition=(
                "sealed_feedback_exposed"
                if sealed_feedback_exposed
                else "redaction_failure"
            ),
            passed=False,
            failures=(
                ("sealed_feedback_exposed",)
                if sealed_feedback_exposed
                else ("proposer_input_not_redacted",)
            ),
        )
    if not routes:
        failures.append("no_provider_routes")
    input_hash = content_hash(clean_input)
    for iteration in range(max(0, int(iterations or 0))):
        if failures:
            break
        iteration_succeeded = False
        for route_index, route in enumerate(routes):
            for retry_index in range(max(0, int(max_retries_per_route or 0)) + 1):
                outcome = _normalized_outcome(executor(clean_input, route, iteration, retry_index), route)
                retriable = outcome.status in RETRIABLE_FAILURES
                terminal = outcome.status in UNRECOVERABLE_FAILURES or (
                    outcome.status in RETRIABLE_FAILURES
                    and retry_index >= max(0, int(max_retries_per_route or 0))
                    and route_index >= len(routes) - 1
                )
                attempts.append(
                    ProposerAttempt(
                        iteration=iteration,
                        route_index=route_index,
                        retry_index=retry_index,
                        provider=route.provider,
                        expected_backend=route.effective_backend,
                        effective_backend=outcome.effective_backend,
                        status=outcome.status,
                        retriable=retriable,
                        terminal=terminal,
                        redacted_input_hash=input_hash,
                        output_hash=content_hash(outcome.output) if outcome.output else "",
                        detail=outcome.detail,
                        process_group_id=outcome.process_group_id,
                        child_process_ids=outcome.child_process_ids,
                        candidate_score=None,
                    )
                )
                child_pids.extend(outcome.child_process_ids)
                if outcome.status == "success":
                    iteration_succeeded = True
                    completed += 1
                    break
                if outcome.process_group_id is not None:
                    controller.kill_process_group(outcome.process_group_id)
                    killed_groups.append(outcome.process_group_id)
                if outcome.status in UNRECOVERABLE_FAILURES:
                    failures.append(outcome.status)
                    terminal_disposition = outcome.status
                    break
                if retriable and retry_index < max(0, int(max_retries_per_route or 0)):
                    continue
                if retriable and route_index < len(routes) - 1:
                    break
                failures.append(outcome.status or "unknown_failure")
                terminal_disposition = outcome.status or "unknown_failure"
                break
            if iteration_succeeded or failures:
                break
        if not iteration_succeeded and not failures:
            failures.append("iteration_incomplete")
            terminal_disposition = "iteration_incomplete"
    orphans = tuple(int(pid) for pid in controller.detect_orphans(tuple(dict.fromkeys(child_pids))))
    if orphans:
        failures.append("orphan_process_detected")
        terminal_disposition = "orphan_process_detected"
    passed = bool(not failures and completed == max(0, int(iterations or 0)))
    return ProposerCanaryReport(
        iterations_requested=max(0, int(iterations or 0)),
        iterations_completed=completed,
        retry_policy={
            "max_retries_per_route": max(0, int(max_retries_per_route or 0)),
            "retriable_statuses": sorted(RETRIABLE_FAILURES),
        },
        failover_policy={"routes": [asdict(route) for route in routes], "ordered": True},
        attempts=tuple(attempts),
        killed_process_groups=tuple(killed_groups),
        orphan_process_ids=orphans,
        terminal_disposition=terminal_disposition,
        passed=passed,
        failures=tuple(dict.fromkeys(failures)),
    )
