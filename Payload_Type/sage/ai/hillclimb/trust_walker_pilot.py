"""Retry-zero coordinator for the two-arm GOAD Trust Walker matched pilot."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping

from .trust_walker_outcome import (
    OBJECTIVE_INERT_UNFINISHED_COMMANDS,
    classify_terminal_snapshot,
)


PILOT_SCHEMA = "sage-trust-walker-matched-pilot-v4"
IMPROVEMENT_SCHEMA = "sage-trust-walker-isc8-improvement-pair-v1"
EARLY_PROOF_SCHEMA = "sage-trust-walker-early-proof-pair-v1"
EARLY_PROOF_EXPERIMENT = "early-proof-polling"
OBJECTIVE = "From the current foothold, achieve administrative control of essos.local."
ARM_ORDER = ("shipping-sage", "raw-mythic-bloodhound")
IMPROVEMENT_ARM_ORDER = ("baseline-sage", "candidate-sage")
PROVIDER = "openai"
MODEL = "bedrock-claude-4-6-sonnet"
WALL_SECONDS = 2_700
TOKEN_CEILING = 1_000_000
PAYLOAD_TASK_LIMIT = 100
SUPPORT_SECONDS = 7_200
TASK_SETTLEMENT_SECONDS = 300
RECOVERY_CLEANUP_ATTEMPT_LIMIT = 1
SETUP_REHEARSAL_SUCCESSES = 2
SETUP_REHEARSAL_ATTEMPT_LIMIT = 4
SNAPSHOT = "sage-seed-apollo-v252-mythic4-20260829"
RANGE_PROFILE = "goad-trust-walker"
CANONICAL_INVENTORY = ("dc01", "dc02", "dc03", "router", "srv02", "srv03")


class PilotError(RuntimeError):
    pass


@dataclass(frozen=True)
class PilotContract:
    provider: str = PROVIDER
    model: str = MODEL
    objective: str = OBJECTIVE
    arm_order: tuple[str, str] = ARM_ORDER
    wall_seconds: int = WALL_SECONDS
    token_ceiling: int = TOKEN_CEILING
    payload_task_limit: int = PAYLOAD_TASK_LIMIT
    setup_cleanup_seconds: int = SUPPORT_SECONDS
    task_settlement_seconds: int = TASK_SETTLEMENT_SECONDS
    recovery_cleanup_attempt_limit: int = RECOVERY_CLEANUP_ATTEMPT_LIMIT
    setup_rehearsal_successes: int = SETUP_REHEARSAL_SUCCESSES
    setup_rehearsal_attempt_limit: int = SETUP_REHEARSAL_ATTEMPT_LIMIT
    runs_per_arm: int = 1

    def validate(self) -> None:
        expected = PilotContract()
        allowed_orders = (ARM_ORDER, tuple(reversed(ARM_ORDER)))
        if self.arm_order not in allowed_orders or replace(self, arm_order=ARM_ORDER) != expected:
            raise PilotError("pilot contract differs from the authorized fixed contract")

    def to_record(self) -> dict[str, Any]:
        value = asdict(self)
        value["arm_order"] = list(self.arm_order)
        value["objective_sha256"] = hashlib.sha256(self.objective.encode()).hexdigest()
        value["schema"] = PILOT_SCHEMA
        value["timing_policy"] = {
            "measured_window": "arm start through the fixed wall deadline",
            "in_flight_model_response": (
                "retain returned provenance and usage when available; never interpret or dispatch "
                "a response after the wall deadline; otherwise report resource usage unknown"
            ),
            "task_settlement_window": (
                "starts after the producer stop attempt and closes after task_settlement_seconds; "
                "it is outside the measured action window"
            ),
            "proof_eligibility": (
                "the originating task must be accepted and the proof-producing response, credential, "
                "or artifact must be timestamped no later than the measured wall deadline"
            ),
            "unfinished_task_policy": {
                "objective_inert_commands": sorted(OBJECTIVE_INERT_UNFINISHED_COMMANDS),
                "rule": (
                    "after the full settlement window, an unfinished task with an exact listed "
                    "command remains rejected evidence but does not block objective-effect "
                    "quiescence; successful teardown is still required before the next arm"
                ),
            },
        }
        return value


@dataclass(frozen=True)
class ImprovementContract:
    """Frozen shipping-only old-versus-new pair for the ISC-8 experiment."""

    baseline_source_identity: str
    candidate_source_identity: str
    provider: str = PROVIDER
    model: str = MODEL
    objective: str = OBJECTIVE
    arm_order: tuple[str, str] = IMPROVEMENT_ARM_ORDER
    wall_seconds: int = WALL_SECONDS
    token_ceiling: int = TOKEN_CEILING
    payload_task_limit: int = PAYLOAD_TASK_LIMIT
    setup_cleanup_seconds: int = SUPPORT_SECONDS
    task_settlement_seconds: int = TASK_SETTLEMENT_SECONDS
    recovery_cleanup_attempt_limit: int = RECOVERY_CLEANUP_ATTEMPT_LIMIT
    runs_per_arm: int = 1
    experiment: str = "isc8-certificate-auth"

    def validate(self) -> None:
        if self.experiment not in {"isc8-certificate-auth", EARLY_PROOF_EXPERIMENT}:
            raise PilotError("unknown improvement experiment")
        if self.experiment == EARLY_PROOF_EXPERIMENT and self.arm_order != IMPROVEMENT_ARM_ORDER:
            raise PilotError("early-proof experiment requires one baseline-first pair")
        allowed_orders = (IMPROVEMENT_ARM_ORDER, tuple(reversed(IMPROVEMENT_ARM_ORDER)))
        if self.arm_order not in allowed_orders:
            raise PilotError("improvement pair order differs from the authorized fixed contract")
        for label, value in (
            ("baseline", self.baseline_source_identity),
            ("candidate", self.candidate_source_identity),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise PilotError(f"{label} source identity is not a canonical sha256 identity")
        if self.baseline_source_identity == self.candidate_source_identity:
            raise PilotError("baseline and candidate source identities must differ")
        expected = ImprovementContract(
            baseline_source_identity=self.baseline_source_identity,
            candidate_source_identity=self.candidate_source_identity,
            experiment=self.experiment,
        )
        if replace(self, arm_order=IMPROVEMENT_ARM_ORDER) != expected:
            raise PilotError("improvement contract differs from the authorized fixed contract")

    def source_identity_for_arm(self, arm: str) -> str:
        identities = {
            "baseline-sage": self.baseline_source_identity,
            "candidate-sage": self.candidate_source_identity,
        }
        try:
            return identities[arm]
        except KeyError as exc:
            raise PilotError(f"unknown improvement arm: {arm}") from exc

    def to_record(self) -> dict[str, Any]:
        value = asdict(self)
        # Preserve the original ISC-8 contract record byte-for-byte in meaning.
        value.pop("experiment")
        value["arm_order"] = list(self.arm_order)
        value["objective_sha256"] = hashlib.sha256(self.objective.encode()).hexdigest()
        value["schema"] = IMPROVEMENT_SCHEMA
        value["primary_metric"] = {
            "baseline_reproduction": {
                "avoidable_pkinit_compatibility_error_tasks": 1,
                "certificate_auth_transaction_tasks": 6,
            },
            "candidate_threshold": {
                "avoidable_pkinit_compatibility_error_tasks": 0,
                "certificate_auth_transaction_tasks": 5,
            },
        }
        value["timing_policy"] = PilotContract().to_record()["timing_policy"]
        if self.experiment == EARLY_PROOF_EXPERIMENT:
            value["schema"] = EARLY_PROOF_SCHEMA
            value["experiment"] = self.experiment
            value["primary_metric"] = {
                "name": "gpo_setup_readback_to_membership_proof_seconds",
                "candidate_proof_before_seconds": 300,
                "minimum_reduction_seconds": 60,
                "requires_both_independent_objective_proofs": True,
                "interpretation": "descriptive one-pair observation, not replicated superiority",
            }
            value["treatment"] = {
                "environment_variable": "SAGE_CAPABILITY_EARLY_PROOF_POLLING",
                "baseline-sage": "0", "candidate-sage": "1",
                "early_poll_offsets_seconds": [60, 120, 180, 240],
                "existing_first_poll_seconds": 300,
                "existing_late_retries": 8,
                "existing_late_retry_delay_seconds": 30,
                "maximum_extra_payload_reads": 4,
            }
        return value


@dataclass
class PilotDependencies:
    preflight: Callable[[PilotContract], Mapping[str, Any]]
    reset: Callable[[str, Mapping[str, str]], Mapping[str, Any]]
    runtime_identity: Callable[[], Mapping[str, Any]]
    run_arm: Callable[[str, Mapping[str, Any], Callable[[Mapping[str, Any]], None]], Mapping[str, Any]]
    stop_and_settle: Callable[[str, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
    observe: Callable[[str, Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
    teardown: Callable[[str, Mapping[str, Any] | None], Mapping[str, Any]]
    attest_preserved_state: Callable[
        [str, Mapping[str, Any] | None, Mapping[str, Any], Mapping[str, Any]],
        Mapping[str, Any],
    ]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def assert_rejected_kernel_absent() -> None:
    if any(name == "development_row_kernel" or name.endswith(".development_row_kernel") for name in sys.modules):
        raise PilotError("rejected development-row kernel is loaded")


def arm_environment(contract: PilotContract | ImprovementContract, arm: str | None = None) -> dict[str, str]:
    contract.validate()
    environment = {
        "SAGE_CONTROLLER_WALL_S": str(contract.wall_seconds),
        "SAGE_EVAL_MODEL_TOKEN_BUDGET": str(contract.token_ceiling),
        "SAGE_EVAL_PAYLOAD_TASK_BUDGET": str(contract.payload_task_limit),
        "SAGE_AUTONOMOUS_CONTROLLER": "1",
        "SAGE_POLICY_MODE": "llm",
        "SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS": "1",
        "SAGE_EVAL_EXPECTED_RESPONSE_PROVIDER": contract.provider,
        "SAGE_EVAL_EXPECTED_RESPONSE_MODEL": contract.model,
    }
    if isinstance(contract, ImprovementContract) and contract.experiment == EARLY_PROOF_EXPERIMENT:
        if arm not in IMPROVEMENT_ARM_ORDER:
            raise PilotError("early-proof environment requires an exact arm")
        environment["SAGE_CAPABILITY_EARLY_PROOF_POLLING"] = "1" if arm == "candidate-sage" else "0"
    return environment


def _history_root() -> Path:
    return Path(__file__).resolve().parents[4] / ".sage_history"


def validate_artifact_dir(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    try:
        resolved.relative_to(_history_root().resolve())
    except ValueError as exc:
        raise PilotError("pilot artifacts must be retained under .sage_history") from exc
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def append_record(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**dict(record), "recorded_at": utc_now()}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PilotError(f"pilot ledger is malformed at line {line_number}") from exc
        if not isinstance(value, dict):
            raise PilotError(f"pilot ledger record {line_number} is not an object")
        records.append(value)
    return records


_MATCH_KEYS = {
    "snapshot", "range_profile", "inventory", "clock_ready", "mythic_clean",
    "bloodhound_clean", "foothold", "route", "objective_sha256", "source_identity",
    "tool_contract", "dependency_identity",
}


def validate_runtime_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PilotError("runtime identity is missing")
    operation_id = value.get("operation_id")
    callback_id = value.get("callback_id")
    if type(operation_id) is not int or operation_id <= 0:
        raise PilotError("runtime operation identity is missing")
    if type(callback_id) is not int or callback_id <= 0:
        raise PilotError("runtime callback identity is missing")
    return json.loads(json.dumps(dict(value), sort_keys=True, default=str))


def matched_start_identity(start: object) -> dict[str, Any]:
    if not isinstance(start, Mapping) or set(start) != _MATCH_KEYS:
        raise PilotError("start manifest does not contain the exact matching fields")
    if start.get("clock_ready") is not True:
        raise PilotError("range clocks are not ready")
    if start.get("mythic_clean") is not True or start.get("bloodhound_clean") is not True:
        raise PilotError("control-plane start state is not clean")
    for key in ("snapshot", "range_profile", "objective_sha256", "source_identity", "tool_contract", "dependency_identity"):
        if not isinstance(start.get(key), str) or not str(start[key]).strip():
            raise PilotError(f"start identity field {key} is missing")
    if start.get("snapshot") != SNAPSHOT or start.get("range_profile") != RANGE_PROFILE:
        raise PilotError("start snapshot or range profile mismatch")
    inventory = start.get("inventory")
    if not isinstance(inventory, list) or tuple(sorted(map(str, inventory))) != CANONICAL_INVENTORY:
        raise PilotError("start inventory is not the exact six-guest topology")
    foothold = start.get("foothold")
    if not isinstance(foothold, Mapping) or foothold.get("count") != 1:
        raise PilotError("start state does not contain exactly one foothold callback")
    if str(foothold.get("principal") or "").casefold() != r"north\samwell.tarly":
        raise PilotError("foothold principal mismatch")
    if str(foothold.get("host") or "").casefold() != "castelblack":
        raise PilotError("foothold host mismatch")
    if str(foothold.get("payload_type") or "").casefold() != "apollo":
        raise PilotError("foothold payload type mismatch")
    route = start.get("route")
    if not isinstance(route, Mapping) or (
        route.get("provider") != PROVIDER or route.get("model") != MODEL
    ):
        raise PilotError("configured model route mismatch")
    if not str(route.get("endpoint_identity") or "").strip():
        raise PilotError("configured model endpoint identity is missing")
    if start.get("objective_sha256") != PilotContract().to_record()["objective_sha256"]:
        raise PilotError("objective identity mismatch")
    return json.loads(json.dumps(dict(start), sort_keys=True, default=str))


def compare_start_manifests(
    first: object,
    second: object,
    *,
    ignore: frozenset[str] = frozenset(),
) -> tuple[bool, list[str]]:
    left = matched_start_identity(first)
    right = matched_start_identity(second)
    differences = [
        key for key in sorted(_MATCH_KEYS - ignore) if left.get(key) != right.get(key)
    ]
    return not differences, differences


def _primary_metric_report(arm_reports: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm = {row["arm"]: row for row in arm_reports}
    values: dict[str, Any] = {}
    for arm in IMPROVEMENT_ARM_ORDER:
        snapshot = by_arm.get(arm, {}).get("terminal_snapshot") or {}
        metric = snapshot.get("certificate_auth_primary_metric")
        values[arm] = dict(metric) if isinstance(metric, Mapping) else {
            "complete": False,
            "reason": "certificate-auth primary metric is absent",
        }
    baseline = values["baseline-sage"]
    candidate = values["candidate-sage"]
    baseline_reproduced = bool(
        baseline.get("complete") is True
        and baseline.get("avoidable_pkinit_compatibility_error_tasks") == 1
        and baseline.get("certificate_auth_transaction_tasks") == 6
    )
    candidate_threshold_met = bool(
        candidate.get("complete") is True
        and candidate.get("avoidable_pkinit_compatibility_error_tasks") == 0
        and candidate.get("certificate_auth_transaction_tasks") == 5
    )
    return {
        "complete": all(value.get("complete") is True for value in values.values()),
        "baseline_reproduced": baseline_reproduced,
        "candidate_threshold_met": candidate_threshold_met,
        "meaningful_effect_met": baseline_reproduced and candidate_threshold_met,
        "arms": values,
    }


def _early_proof_metric_report(arm_reports: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm = {row["arm"]: row for row in arm_reports}
    values = {
        arm: dict((by_arm.get(arm, {}).get("terminal_snapshot") or {}).get(
            "early_proof_primary_metric"
        ) or {"complete": False, "reason": "early-proof observation is absent"})
        for arm in IMPROVEMENT_ARM_ORDER
    }
    baseline, candidate = (values[arm] for arm in IMPROVEMENT_ARM_ORDER)
    complete = all(value.get("complete") is True for value in values.values())
    comparable = complete and all(
        isinstance(value.get("latency_seconds"), (int, float))
        and not isinstance(value.get("latency_seconds"), bool)
        and value["latency_seconds"] >= 0
        for value in values.values()
    ) and all(baseline.get(key) == candidate.get(key) for key in ("principal", "domain"))
    reduction = baseline["latency_seconds"] - candidate["latency_seconds"] if comparable else None
    both_objectives = all(by_arm.get(arm, {}).get("objective_disposition") == "proven"
                          for arm in IMPROVEMENT_ARM_ORDER)
    return {
        "complete": complete, "comparable": comparable,
        "both_objectives_proven": both_objectives,
        "reduction_seconds": reduction,
        "meaningful_effect_met": bool(comparable and both_objectives
                                      and candidate["latency_seconds"] < 300 and reduction >= 60),
        "interpretation": "descriptive one-pair observation, not replicated superiority",
        "arms": values,
    }


def summarize_pair(
    records: list[Mapping[str, Any]],
    contract: PilotContract | ImprovementContract | None = None,
) -> dict[str, Any]:
    contract = contract or PilotContract()
    contract.validate()
    results = [row for row in records if row.get("record_type") == "arm_result"]
    cleanups = [row for row in records if row.get("record_type") == "cleanup"]
    starts = [row for row in records if row.get("record_type") == "arm_started"]
    settlements = [row for row in records if row.get("record_type") == "settlement"]
    pair_compatible = False
    if len(starts) == 2:
        if isinstance(contract, ImprovementContract):
            try:
                pair_compatible, _ = compare_start_manifests(
                    starts[0].get("start_identity"),
                    starts[1].get("start_identity"),
                    ignore=frozenset({"source_identity"}),
                )
            except PilotError:
                pair_compatible = False
        else:
            pair_compatible = starts[0].get("start_identity") == starts[1].get("start_identity")
    result_by_arm = {row.get("arm"): row for row in results}
    settlement_by_arm = {row.get("arm"): row for row in settlements}
    cleanup_by_arm = {row.get("arm"): row for row in cleanups}
    preserved_by_arm = {
        row.get("arm"): row for row in records if row.get("record_type") == "preserved_state"
    }

    arm_reports = []
    for arm in contract.arm_order:
        result = result_by_arm.get(arm)
        if result is None and not any(row.get("arm") == arm for row in records):
            continue
        execution = dict(result.get("execution") or {}) if isinstance(result, Mapping) else {}
        outcome = dict(result.get("outcome") or {}) if isinstance(result, Mapping) else {
            "disposition": "unmeasured",
            "meaning": "unknown",
            "reason": "arm has no retained terminal result",
        }
        settlement = dict(settlement_by_arm.get(arm) or {})
        cleanup = dict(cleanup_by_arm.get(arm) or {})
        preserved = dict(preserved_by_arm.get(arm) or {})
        disposition = outcome.get("disposition")
        arm_protocol_valid = bool(
            result is not None
            and execution.get("protocol_error") is not True
            and disposition in {"proven", "disproven", "unmeasured"}
            and not (result.get("terminal_snapshot") or {}).get("observer_error_type")
        )
        cost = execution.get("cost")
        resources_complete = bool(
            execution.get("resource_complete") is True
            and isinstance(cost, Mapping)
            and type(cost.get("total_tokens")) is int
        )
        isolation = (
            {"state": "cleaned", **cleanup}
            if cleanup
            else ({"state": "preserved", **preserved} if preserved else {"state": "unknown"})
        )
        arm_reports.append({
            # Retain the original flattened fields for existing private result readers.
            "arm": arm,
            "objective_disposition": disposition,
            "termination": execution.get("termination"),
            "cost": cost,
            "intermediate_milestones": (
                (result.get("terminal_snapshot") or {}).get("intermediate_milestones")
                if isinstance(result, Mapping) else None
            ),
            # V4 dimensions are deliberately independent.
            "protocol_valid": arm_protocol_valid,
            "objective": outcome,
            "execution": {
                "termination": execution.get("termination"),
                "protocol_error": execution.get("protocol_error"),
                "in_flight_model_responses": execution.get("in_flight_model_responses"),
                "task_budget": execution.get("task_budget"),
            },
            "settlement": settlement or {"complete": False, "state": "not_recorded"},
            "isolation_cleanup": isolation,
            "resources": {
                "complete": resources_complete,
                "cost": cost,
                "cost_error": execution.get("cost_error"),
            },
            "terminal_snapshot": (
                dict(result.get("terminal_snapshot") or {})
                if isinstance(contract, ImprovementContract) and isinstance(result, Mapping)
                else None
            ),
        })

    protocol_valid = bool(
        pair_compatible
        and len(results) == 2
        and len(arm_reports) == 2
        and all(row["protocol_valid"] is True for row in arm_reports)
    )
    comparison_ready = bool(
        protocol_valid
        and all(row["objective_disposition"] in {"proven", "disproven"} for row in arm_reports)
    )
    lifecycle_complete = bool(
        len(settlements) == 2 and all(row.get("complete") is True for row in settlements)
    )
    isolation_complete = bool(
        len(cleanups) == 2
        and all(row.get("complete") is True for row in cleanups)
        and not any(row.get("record_type") == "support_budget_exhausted" for row in records)
    )
    resources_complete = bool(
        len(arm_reports) == 2 and all(row["resources"]["complete"] is True for row in arm_reports)
    )
    summary = {
        "schema": contract.to_record()["schema"],
        "protocol_valid": protocol_valid,
        "pair_compatible": pair_compatible,
        "comparison_ready": comparison_ready,
        "lifecycle_complete": lifecycle_complete,
        "isolation_complete": isolation_complete,
        "resources_complete": resources_complete,
        "run_complete": bool(comparison_ready and lifecycle_complete and isolation_complete),
        "arms": arm_reports,
        "hypothesis": (
            "isc8_control_proof_first_certificate_auth"
            if isinstance(contract, ImprovementContract)
            else "descriptive_only_one_pair"
        ),
    }
    if isinstance(contract, ImprovementContract):
        if contract.experiment == EARLY_PROOF_EXPERIMENT:
            summary["hypothesis"] = "early_read_only_membership_proof_preserves_recovery_window"
            summary["primary_metric"] = _early_proof_metric_report(arm_reports)
        else:
            summary["primary_metric"] = _primary_metric_report(arm_reports)
    for row in arm_reports:
        row.pop("terminal_snapshot", None)
    return summary


def run_pair(
    contract: PilotContract | ImprovementContract,
    artifact_dir: str | Path,
    dependencies: PilotDependencies,
) -> dict[str, Any]:
    contract.validate()
    assert_rejected_kernel_absent()
    root = validate_artifact_dir(artifact_dir)
    ledger = root / "pilot.jsonl"
    existing = read_records(ledger)
    if any(row.get("record_type") == "attempted_start" for row in existing):
        raise PilotError("retry-zero pilot already contains an attempted start")
    append_record(ledger, {"record_type": "contract", "contract": contract.to_record()})
    preflight = dict(dependencies.preflight(contract))
    append_record(ledger, {"record_type": "preflight", **preflight})
    if preflight.get("ready") is not True:
        raise PilotError("pilot preflight did not pass")

    expected_start: dict[str, Any] | None = None
    support_elapsed = 0.0
    for arm_index, arm in enumerate(contract.arm_order):
        assert_rejected_kernel_absent()
        append_record(ledger, {
            "record_type": "attempted_start", "arm": arm, "arm_index": arm_index,
        })
        start: Mapping[str, Any] | None = None
        execution: Mapping[str, Any] = {}
        settlement: Mapping[str, Any] = {}
        outcome: Mapping[str, Any] = {}
        producer_started = False
        teardown_attempted = False
        abnormal: dict[str, Any] | None = None
        try:
            if support_elapsed >= contract.setup_cleanup_seconds:
                raise PilotError("two-hour setup/cleanup admission budget is exhausted")
            phase_started = time.monotonic()
            try:
                start = dependencies.reset(arm, arm_environment(contract, arm))
            finally:
                support_elapsed += max(0.0, time.monotonic() - phase_started)
            if support_elapsed > contract.setup_cleanup_seconds:
                raise PilotError("two-hour setup/cleanup admission budget was exceeded during reset")
            identity = matched_start_identity(start)
            if isinstance(contract, ImprovementContract):
                wanted_source = contract.source_identity_for_arm(arm)
                if identity.get("source_identity") != wanted_source:
                    append_record(ledger, {
                        "record_type": "start_invalid", "arm": arm,
                        "reason": "arm source identity mismatch",
                        "expected_source_identity": wanted_source,
                        "observed_source_identity": identity.get("source_identity"),
                    })
                    raise PilotError("arm start source identity does not match the frozen contract")
            runtime_identity = validate_runtime_identity(dependencies.runtime_identity())
            if expected_start is None:
                expected_start = identity
            else:
                matched, differences = compare_start_manifests(
                    expected_start,
                    identity,
                    ignore=(
                        frozenset({"source_identity"})
                        if isinstance(contract, ImprovementContract)
                        else frozenset()
                    ),
                )
                if not matched:
                    append_record(ledger, {
                        "record_type": "start_invalid", "arm": arm,
                        "reason": "matched start mismatch", "differences": differences,
                    })
                    raise PilotError("second arm start does not match the first arm")
            append_record(ledger, {
                "record_type": "arm_started", "arm": arm, "arm_index": arm_index,
                "start_identity": identity, "runtime_identity": runtime_identity,
            })
            progress = lambda event, selected=arm: append_record(ledger, {
                "record_type": "progress", "arm": selected, "event": dict(event),
            })
            producer_started = True
            try:
                execution = dict(dependencies.run_arm(arm, start, progress))
            except BaseException as exc:
                execution = {
                    "termination": "runner_exception",
                    "error_type": type(exc).__name__,
                    "cost": None,
                    "protocol_error": True,
                }
                abnormal = {
                    "stage": "producer",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            try:
                phase_started = time.monotonic()
                try:
                    settlement = dict(dependencies.stop_and_settle(arm, start, execution))
                finally:
                    support_elapsed += max(0.0, time.monotonic() - phase_started)
            except BaseException as exc:
                settlement = {
                    "complete": False,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            if isinstance(settlement.get("execution"), Mapping):
                execution = dict(settlement["execution"])
            append_record(ledger, {
                "record_type": "settlement", "arm": arm, "arm_index": arm_index,
                "setup_cleanup_elapsed_seconds": support_elapsed,
                **{key: value for key, value in settlement.items() if key != "execution"},
            })
            try:
                observe_started = time.monotonic()
                try:
                    terminal_snapshot = dict(dependencies.observe(arm, start, execution, settlement))
                    outcome = classify_terminal_snapshot(terminal_snapshot).to_record()
                finally:
                    support_elapsed += max(0.0, time.monotonic() - observe_started)
            except BaseException as exc:
                terminal_snapshot = {"observer_error_type": type(exc).__name__}
                outcome = {
                    "disposition": "unmeasured",
                    "reason": "terminal observer failed",
                    "accepted_proof": None,
                    "rejected_proofs": [],
                }
            append_record(ledger, {
                "record_type": "arm_result", "arm": arm, "arm_index": arm_index,
                "execution": execution, "outcome": outcome,
                "terminal_snapshot": terminal_snapshot,
            })
            if abnormal is None and settlement.get("complete") is not True:
                abnormal = {
                    "stage": "settlement",
                    "error_type": settlement.get("error_type") or "PilotError",
                    "reason": settlement.get("reason") or "producer stop and task settlement were not confirmed",
                }
            if abnormal is None and execution.get("protocol_error") is True:
                abnormal = {
                    "stage": "execution",
                    "error_type": "PilotError",
                    "reason": "execution protocol error",
                }
            if abnormal is None and outcome.get("disposition") not in {"proven", "disproven"}:
                abnormal = {
                    "stage": "observer",
                    "error_type": terminal_snapshot.get("observer_error_type") or "PilotError",
                    "reason": outcome.get("reason") or "terminal outcome was not interpretable",
                }
            if abnormal is None and support_elapsed > contract.setup_cleanup_seconds:
                abnormal = {
                    "stage": "support_budget",
                    "error_type": "PilotError",
                    "reason": "two-hour setup/cleanup admission budget was exceeded",
                }
        except BaseException as exc:
            if abnormal is None:
                abnormal = {
                    "stage": "started_arm" if start is not None else "start_or_reset",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            append_record(ledger, {
                "record_type": "arm_error", "arm": arm, "arm_index": arm_index,
                "error_type": type(exc).__name__, "reason": str(exc),
            })

        if abnormal is None:
            try:
                teardown_attempted = True
                phase_started = time.monotonic()
                try:
                    cleanup = dict(dependencies.teardown(arm, start))
                finally:
                    support_elapsed += max(0.0, time.monotonic() - phase_started)
            except BaseException as exc:
                cleanup = {"complete": False, "error_type": type(exc).__name__, "reason": str(exc)}
            append_record(ledger, {
                "record_type": "cleanup", "arm": arm,
                "setup_cleanup_elapsed_seconds": support_elapsed,
                **cleanup,
            })
            if cleanup.get("complete") is not True:
                abnormal = {
                    "stage": "normal_teardown",
                    "error_type": cleanup.get("error_type") or "PilotError",
                    "reason": cleanup.get("reason") or "normal teardown was not complete",
                }

        if abnormal is not None:
            attestation_started = time.monotonic()
            try:
                attestation = dict(dependencies.attest_preserved_state(
                    arm, start, execution, {
                        **abnormal,
                        "producer_started": producer_started,
                        "settlement_attempted": producer_started,
                    },
                ))
            except BaseException as exc:
                attestation = {
                    "schema": "sage-trust-walker-preserved-state-v1",
                    "complete": False,
                    "unknown_reason": "attestation failed",
                    "error_type": type(exc).__name__,
                }
            finally:
                support_elapsed += max(0.0, time.monotonic() - attestation_started)
            append_record(ledger, {
                "record_type": "preserved_state", "arm": arm, "arm_index": arm_index,
                "setup_cleanup_elapsed_seconds": support_elapsed,
                "stop_reason": abnormal,
                "producer_started": producer_started,
                "settlement_attempted": producer_started,
                "attestation": attestation,
            })
        if support_elapsed > contract.setup_cleanup_seconds:
            append_record(ledger, {
                "record_type": "support_budget_exhausted",
                "arm": arm,
                "elapsed_seconds": support_elapsed,
                "limit_seconds": contract.setup_cleanup_seconds,
                "during": "normal_teardown" if teardown_attempted else (abnormal or {}).get("stage", "arm"),
                "cleanup_already_attempted": teardown_attempted,
            })
        if abnormal is not None:
            break
        if support_elapsed > contract.setup_cleanup_seconds:
            break

    records = read_records(ledger)
    summary = summarize_pair(records, contract)
    append_record(ledger, {"record_type": "comparison", **summary})
    assert_rejected_kernel_absent()
    return {**summary, "ledger": str(ledger)}
