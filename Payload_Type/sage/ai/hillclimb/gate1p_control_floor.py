"""Allocated one-pair Gate 1P dual-endpoint control-floor canary."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from . import gate1p_stagea, gate1p_stageb
except Exception:
    import gate1p_stagea  # type: ignore
    import gate1p_stageb  # type: ignore


PROTOCOL_ID = "gate1p-control-floor-dual-endpoint-completed-call-budget-v1"
MATRIX_ID = "gate1p-control-floor-canary-v1"
PAIR_ID = "gate1p-control-floor-canary-pair-001"
PAIR_ALLOCATED = True
ALLOCATION_ID = "gate1p-control-floor-canary-allocation-001"
PAIR_IDS = (PAIR_ID,)
PRIMARY_PAIR_IDS = PAIR_IDS
RESERVE_PAIR_IDS: tuple[str, ...] = ()
REQUIRED_COMPARISON_PAIRS = 1
MAX_PAIR_ATTEMPTS = 1
STORAGE_NAMESPACE = f"gate1p-control-floor/{MATRIX_ID}"
PAIR_CONTROL_SCHEMA = "gate1p-control-floor-pair-control-v1"
PRESTART_CLEANUP_SCHEMA = "gate1p-control-floor-prestart-cleanup-v1"
SCENARIO = gate1p_stageb.SCENARIO
SCENARIO_DEFAULT = False
OBJECTIVE_DOMAIN = gate1p_stageb.OBJECTIVE_DOMAIN
OBJECTIVE_TARGET_HOSTS = gate1p_stageb.OBJECTIVE_TARGET_HOSTS
OBJECTIVE = gate1p_stageb.OBJECTIVE
ARMS = gate1p_stagea.ARMS
SIDE_BY_ARM = gate1p_stagea.SIDE_BY_ARM
ARM_LABELS = {
    "shipping_sage": "shipping Sage",
    "true_naive": "same-model raw-tool/raw-history floor",
}
MODEL_PROVIDER = gate1p_stagea.MODEL_PROVIDER
MODEL_ID = gate1p_stagea.MODEL_ID
POLICY_MODE = gate1p_stagea.POLICY_MODE
WALL_SECONDS = gate1p_stagea.WALL_SECONDS
TOKEN_ADMISSION_CEILING = 1_000_000
MODEL_CONTEXT_LIMIT = gate1p_stageb.MODEL_CONTEXT_LIMIT
MAX_OUTPUT_TOKENS = gate1p_stageb.MAX_OUTPUT_TOKENS
ARM_TOKEN_CEILINGS = {arm: TOKEN_ADMISSION_CEILING for arm in ARMS}
RESET_SNAPSHOT = gate1p_stagea.RESET_SNAPSHOT
INITIAL_FOOTHOLD_ROLE = gate1p_stagea.INITIAL_FOOTHOLD_ROLE
OBSERVER_VERSION = "gate1p-control-floor-observer-facts-v1"
RAW_HISTORY_POLICY = "untrimmed-full-action-observation-history-v1"
RAW_ARM_INTERPRETATION = (
    "Same-model raw-tool/raw-history floor only; not a proxy for Codex, Claude Code, "
    "or a competent generic agent harness."
)
RAW_HISTORY_FALSIFIER = (
    "If typed source evidence establishes that the untrimmed transcript must reach the per-request "
    "context limit before the cumulative ceiling can be meaningfully consumed, stop at the architecture "
    "gate; do not add compaction or a third arm."
)
PRIMARY_ENDPOINT = "control_achieved"
SECONDARY_ENDPOINTS = ("control_exercised", "strict_control_result")
PROOF_ADAPTER_FAMILY = "stageb"

OBJECTIVE_PROOF_CLASSES = gate1p_stageb.OBJECTIVE_PROOF_CLASSES
PROOF_SOURCES = gate1p_stageb.PROOF_SOURCES
ProofAdapter = gate1p_stageb.ProofAdapter
ProofUnavailable = gate1p_stageb.ProofUnavailable
ProtocolError = gate1p_stagea.ProtocolError
ObserverUnavailable = gate1p_stagea.ObserverUnavailable

TERMINAL_CAUSES = (
    "cumulative_token_ceiling",
    "per_request_context_limit",
    "wall_time",
    "explicit_terminal_decision",
    "other_declared_boundary",
)
TERMINAL_SUBTYPES = {
    "cumulative_token_ceiling": frozenset({"exhausted", "overshoot"}),
    "per_request_context_limit": frozenset({"provider_typed_code", "local_pre_request_boundary"}),
    "wall_time": frozenset({"wrapper_timeout"}),
    "explicit_terminal_decision": frozenset({"objective_met", "stuck", "other_final"}),
    "other_declared_boundary": frozenset({
        "controller_aborted",
        "controller_blocked",
        "controller_denied",
        "controller_max_cycles",
        "controller_no_action",
        "controller_no_progress",
        "interrupted",
        "measurement_error",
        "model_error",
        "step_budget",
        "unknown",
        "wrapper_error",
    }),
}
CONTRACT_EXTENSIONS = {
    "arm_labels": ARM_LABELS,
    "raw_history_policy": RAW_HISTORY_POLICY,
    "raw_arm_interpretation": RAW_ARM_INTERPRETATION,
    "primary_endpoint": PRIMARY_ENDPOINT,
    "secondary_endpoints": SECONDARY_ENDPOINTS,
    "terminal_causes": TERMINAL_CAUSES,
    "completed_call_token_ceiling_per_arm": ARM_TOKEN_CEILINGS,
    "per_request_context_limit": MODEL_CONTEXT_LIMIT,
    "max_output_tokens": MAX_OUTPUT_TOKENS,
    "raw_history_falsifier": RAW_HISTORY_FALSIFIER,
}

ADMISSION = gate1p_stagea.PairAdmissionSpec(
    protocol_id=PROTOCOL_ID,
    pair_id=PAIR_ID,
    scenario=SCENARIO,
    objective=OBJECTIVE,
    observer_version=OBSERVER_VERSION,
    reset_snapshot=RESET_SNAPSHOT,
    oracle_version="gate1p-control-floor-dual-endpoint-route-neutral-v1",
    readiness_protocol="gate1p-control-floor-qualified-reset-v1",
    logical_seed=0,
    token_admission_ceiling=TOKEN_ADMISSION_CEILING,
)


def make_proof_adapter(name: str, probe):
    return gate1p_stageb.make_proof_adapter(name, probe)


def _project_stageb_oracle(raw: Mapping[str, Any]) -> dict[str, Any]:
    summary = dict(raw["summary"])
    control_achieved = summary["control_satisfied"]
    control_exercised = summary["exercised_action_satisfied"]
    return {
        "success": control_achieved,
        "control_achieved": control_achieved,
        "control_exercised": control_exercised,
        "strict_control_result": control_achieved and control_exercised,
        "control_proof_class": raw["control_proof_class"],
        "exercised_action_proof_class": raw["exercised_action_proof_class"],
        "summary": summary,
        "artifact_pointer": raw["artifact_pointer"],
    }


class ControlFloorObjectiveOracle:
    def __init__(self, adapters: Iterable[ProofAdapter], *, artifact_pointer: str, domain: str = OBJECTIVE_DOMAIN):
        self._stageb = gate1p_stageb.StageBObjectiveOracle(
            adapters,
            artifact_pointer=artifact_pointer,
            domain=domain,
        )
        self.last_observation: dict[str, Any] | None = None

    def __call__(self) -> dict[str, Any]:
        observation = _project_stageb_oracle(gate1p_stageb.canonical_oracle_fact(self._stageb()))
        self.last_observation = observation
        return observation


def canonical_oracle_fact(raw: object) -> dict[str, Any]:
    fields = {
        "success",
        "control_achieved",
        "control_exercised",
        "strict_control_result",
        "control_proof_class",
        "exercised_action_proof_class",
        "summary",
        "artifact_pointer",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ObserverUnavailable("control-floor objective oracle schema is not exact")
    strict = raw.get("strict_control_result")
    control = raw.get("control_achieved")
    exercised = raw.get("control_exercised")
    if (
        type(control) is not bool
        or type(exercised) is not bool
        or type(strict) is not bool
        or raw.get("success") is not control
        or strict is not (control and exercised)
    ):
        raise ObserverUnavailable("control-floor endpoint facts are malformed or contradictory")
    stageb_raw = {
        "success": strict,
        "control_proof_class": raw.get("control_proof_class"),
        "exercised_action_proof_class": raw.get("exercised_action_proof_class"),
        "summary": raw.get("summary"),
        "artifact_pointer": raw.get("artifact_pointer"),
    }
    expected = _project_stageb_oracle(gate1p_stageb.canonical_oracle_fact(stageb_raw))
    if dict(raw) != expected:
        raise ObserverUnavailable("control-floor endpoint facts disagree with independent proof state")
    return expected


def build_objective_oracle(adapters, *, artifact_pointer: str, domain: str = OBJECTIVE_DOMAIN):
    return ControlFloorObjectiveOracle(adapters, artifact_pointer=artifact_pointer, domain=domain)


def build_episode_observer(**kwargs):
    return gate1p_stagea.LiveEpisodeObserver(
        **kwargs,
        oracle_canonicalizer=canonical_oracle_fact,
        observer_version=OBSERVER_VERSION,
        thread_name_prefix="gate1p-control-floor",
    )


def observer_facts_disposition(observer, *, arm: str):
    return gate1p_stagea.observer_facts_disposition(
        observer,
        arm=arm,
        pair_id=PAIR_ID,
        arms=ARMS,
        observer_version=OBSERVER_VERSION,
        oracle_canonicalizer=canonical_oracle_fact,
    )


def episode_outcome_disposition(
    observer,
    *,
    arm: str,
    total_tokens: object,
    token_ceiling: object,
):
    result = gate1p_stagea.episode_outcome_disposition(
        observer,
        arm=arm,
        total_tokens=total_tokens,
        token_ceiling=token_ceiling,
        facts_disposition=observer_facts_disposition,
    )
    if result.get("countable") is not True:
        return result
    oracle = canonical_oracle_fact(observer["oracle"])
    return {
        **result,
        "control_achieved": oracle["control_achieved"],
        "control_exercised": oracle["control_exercised"],
        "strict_control_result": oracle["strict_control_result"],
    }


def pair_invariants(*, pair_id: str | None = None, **kwargs):
    if pair_id is not None and pair_id != PAIR_ID:
        raise ProtocolError(f"pair_id must be {PAIR_ID!r}")
    return gate1p_stagea.pair_invariants(**kwargs, admission=ADMISSION)


def validate_request(**request) -> str:
    if request.get("protocol_id") != PROTOCOL_ID:
        raise ProtocolError(f"protocol_id must be {PROTOCOL_ID!r}")
    if request.get("pair_id") != PAIR_ID:
        raise ProtocolError(f"pair_id must be {PAIR_ID!r}")
    if request.get("scenario") != SCENARIO:
        raise ProtocolError(f"scenario must be {SCENARIO!r}")
    if (
        request.get("model_context_limit") != MODEL_CONTEXT_LIMIT
        or request.get("max_output_tokens") != MAX_OUTPUT_TOKENS
    ):
        raise ProtocolError("control-floor canary requires its fixed request-context and output settings")
    return gate1p_stagea.validate_request(admission=ADMISSION, **request)


def termination_disposition(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {"cause", "subtype"}:
        return {"countable": False, "reason": "terminal cause schema is not exact"}
    cause = raw.get("cause")
    subtype = raw.get("subtype")
    if cause not in TERMINAL_CAUSES or subtype not in TERMINAL_SUBTYPES.get(str(cause), ()):
        return {"countable": False, "reason": "terminal cause or subtype is undeclared"}
    return {"countable": True, "termination": {"cause": cause, "subtype": subtype}}


def _validate_dual_projection(row: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    observer = row.get("observer")
    protocol = row.get("protocol") if isinstance(row.get("protocol"), Mapping) else {}
    invariants = protocol.get("pair_invariants") if isinstance(protocol, Mapping) else {}
    response_records = row.get("model_response_records")
    usage = gate1p_stagea.response_usage_disposition(
        response_records,
        model_calls=row.get("model_calls"),
    )
    if usage.get("countable") is not True or not isinstance(observer, Mapping):
        return {"countable": False, "reason": "dual-endpoint source evidence is unavailable"}
    semantics = episode_outcome_disposition(
        observer,
        arm=str(protocol.get("arm") or ""),
        total_tokens=usage["total_tokens"],
        token_ceiling=(invariants or {}).get("token_admission_ceiling"),
    )
    outcome = ((row.get("countability") or {}).get("outcome") or {})
    endpoints = (PRIMARY_ENDPOINT, *SECONDARY_ENDPOINTS)
    if any(
        type(outcome.get(field)) is not bool or outcome.get(field) is not semantics.get(field)
        for field in endpoints
    ):
        return {"countable": False, "reason": "dual-endpoint result projection disagrees with observer facts"}
    terminal = termination_disposition(row.get("termination"))
    if terminal.get("countable") is not True:
        return terminal
    comparison_input = {
        **dict(result["comparison_input"]),
        "control_achieved": semantics["control_achieved"],
        "control_exercised": semantics["control_exercised"],
        "strict_control_result": semantics["strict_control_result"],
        "termination": terminal["termination"],
    }
    return {**result, "comparison_input": comparison_input}


def arm_comparison_disposition(row, *, history_root: Path | None = None):
    result = gate1p_stagea.arm_comparison_disposition(
        row,
        history_root=history_root,
        admission=ADMISSION,
        outcome_disposition=episode_outcome_disposition,
        oracle_canonicalizer=canonical_oracle_fact,
    )
    if result.get("countable") is not True:
        return result
    return _validate_dual_projection(row, result)


def completed_arm_lifecycle_disposition(records, *, arm: str, history_root=None):
    all_records = list(records)
    result = gate1p_stagea.completed_arm_lifecycle_disposition(
        all_records,
        arm=arm,
        history_root=history_root,
        admission=ADMISSION,
        outcome_disposition=episode_outcome_disposition,
        oracle_canonicalizer=canonical_oracle_fact,
    )
    if result.get("countable") is not True:
        return result
    rows = [row for row in all_records if row.get("record_type") == "result"]
    if len(rows) != 1:
        return {"countable": False, "reason": "completed arm lacks exactly one result row"}
    comparison = arm_comparison_disposition(rows[0], history_root=history_root)
    return result if comparison.get("countable") is True else comparison


def pair_disposition(records, *, history_root: Path | None = None):
    rows = list(records)
    base = gate1p_stagea.pair_disposition(
        rows,
        history_root=history_root,
        admission=ADMISSION,
        outcome_disposition=episode_outcome_disposition,
        oracle_canonicalizer=canonical_oracle_fact,
    )
    if base.get("countable") is not True:
        return base
    results = {
        str((row.get("protocol") or {}).get("arm") or ""): row
        for row in rows
        if row.get("record_type") == "result"
        and (row.get("protocol") or {}).get("protocol_id") == PROTOCOL_ID
        and (row.get("protocol") or {}).get("pair_id") == PAIR_ID
    }
    if set(results) != set(ARMS):
        return {"countable": False, "reason": "dual-endpoint pair results are incomplete"}
    inputs = []
    for arm in ARMS:
        disposition = arm_comparison_disposition(results[arm], history_root=history_root)
        if disposition.get("countable") is not True:
            return disposition
        inputs.append(disposition["comparison_input"])
    primary = [item[PRIMARY_ENDPOINT] for item in inputs]
    informative = primary[0] is not primary[1]
    return {
        **base,
        "reason": (
            "countable primary-discordant control-floor canary"
            if informative
            else "countable primary-concordant canary stops Gate 1P"
        ),
        "comparison_inputs": inputs,
        "primary_endpoint": PRIMARY_ENDPOINT,
        "primary_outcomes": dict(zip(ARMS, primary, strict=True)),
        "strict_outcomes": {
            arm: inputs[index]["strict_control_result"] for index, arm in enumerate(ARMS)
        },
        "informative": informative,
        "continuation": (
            "eligible_for_separately_approved_matrix"
            if informative
            else "stop_gate1p"
        ),
    }


def matrix_disposition(records, *, history_root: Path | None = None):
    return pair_disposition(records, history_root=history_root)
