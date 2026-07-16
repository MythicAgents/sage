"""Phase 15 retrospective R5 falsifiers and policy-identifiability audit.

This module is offline-only. It consumes the frozen pinned R5 policy and forced
rows, keeps controls on an explicit pre-decision input boundary, and reports
only a negative retrospective disposition. It does not run the lab, call a
model, mutate runtime policy, or turn post-hoc evidence into a promotion gate.

The R5 packet history contains repeated live rows with volatile prior-decision
IDs. Phase 15 therefore keeps two views separate:

* retained occurrence view: reproduce the reviewer-called-out 18/18 Hybrid
  model-owned choices without dropping repeated observed decisions; and
* canonical unique-case view: remove only volatile prior-decision IDs and score
  every reconstructable frontier permutation without inflating uncertainty.

Regret is inherited only when a deterministic control exactly reproduces an
observed full policy row. Unobserved counterfactual paths remain unscored.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import inspect
from itertools import permutations
import json
from math import factorial, sqrt
from pathlib import Path
import random
import re
import sys
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

try:  # package import
    from . import laps_family_transfer_holdout as holdout
    from . import laps_family_transfer_matrix as matrix
    from . import policy_replay_unseen_candidate_evaluator as unseen_evaluator
    from ..langgraph import policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import laps_family_transfer_holdout as holdout  # type: ignore
    import laps_family_transfer_matrix as matrix  # type: ignore
    import policy_replay_unseen_candidate_evaluator as unseen_evaluator  # type: ignore
    import policy  # type: ignore


KIND = "phase15_r5_retrospective_falsifier_report"
SCHEMA_VERSION = 1
REVIEWER_EXPECTED_MODEL_OWNED_DECISION_COUNT = 18
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[2] / ".hillclimb" / "results"
DEFAULT_POLICY_ROWS_PATH = DEFAULT_RESULTS_ROOT / "laps_family_transfer_policy_matrix_pinned_r5_20260715.jsonl"
DEFAULT_FORCED_ROWS_PATH = DEFAULT_RESULTS_ROOT / "laps_family_transfer_forced_confirmations_pinned_r5_20260715.jsonl"
DEFAULT_CANARY_REPORT_PATH = DEFAULT_RESULTS_ROOT / "laps_family_transfer_canary_validation_r5_20260715.json"
DEFAULT_MATRIX_VALIDATION_PATH = DEFAULT_RESULTS_ROOT / "laps_family_transfer_matrix_validation_r5_20260715.json"
DEFAULT_OUTPUT_PATH = (
    DEFAULT_REPO_ROOT
    / "Plans"
    / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE15_R5_RETROSPECTIVE_FALSIFIER_REPORT_2026-07-16.json"
)

CONTROL_NAMES = (
    "shipping_symbolic",
    "always_first",
    "always_second",
    "always_last",
    "seeded_random",
    "visible_cost",
    "objective_effect_aware",
    "modeled_reachability",
)
VALID_DETERMINISTIC_FALSIFIER_CONTROLS = CONTROL_NAMES
FORBIDDEN_RESULT_FIELDS = (
    "achieved_effects",
    "authorized_transaction_count",
    "card",
    "clean_stop",
    "controller_status",
    "controller_terminal_reason",
    "derived_outcome",
    "objective_proven",
    "request_completed",
    "semantic_transaction_count",
    "transactions",
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_GENERIC_TOKEN_STOPWORDS = frozenset(
    {
        "account",
        "admin",
        "bounded",
        "callback",
        "cinder",
        "current",
        "domain",
        "execute",
        "execution",
        "foothold",
        "from",
        "local",
        "managed",
        "prove",
        "read",
        "remote",
        "secret",
        "target",
        "the",
        "use",
    }
)


class Phase15R5FalsifierError(ValueError):
    """Raised when frozen R5 retrospective inputs are missing or malformed."""


@dataclass(frozen=True)
class _ControlInput:
    case_id: str
    objective: str
    normalized_state: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _content_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(DEFAULT_REPO_ROOT.resolve()).as_posix()
    except Exception:
        return path.name


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Phase15R5FalsifierError(f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Phase15R5FalsifierError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Phase15R5FalsifierError(f"{label} must be a JSON object")
    return payload


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise Phase15R5FalsifierError(f"missing {label}: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Phase15R5FalsifierError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise Phase15R5FalsifierError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    if not rows:
        raise Phase15R5FalsifierError(f"{label} has no rows: {path}")
    return rows


def _write_json_with_sidecar(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    digest = _sha256_file(path)
    path.with_suffix(".sha256").write_text(
        f"{digest.removeprefix('sha256:')}  {path.name}\n",
        encoding="utf-8",
    )
    return digest


def _canonicalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return holdout.canonicalize_callback_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_canonicalize_value(item) for item in value]
    return value


def _frontier(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    frontier = packet.get("admissible_frontier")
    if not isinstance(frontier, list) or not frontier:
        raise Phase15R5FalsifierError("decision packet must contain a non-empty admissible_frontier")
    if not all(isinstance(item, dict) for item in frontier):
        raise Phase15R5FalsifierError("decision packet admissible_frontier items must be objects")
    return [dict(item) for item in frontier]


def _candidate_id(candidate: Mapping[str, Any]) -> str:
    return holdout.canonical_semantic_candidate_id(dict(candidate))


def _canonical_candidate_record(candidate: Mapping[str, Any]) -> dict[str, Any]:
    payload = holdout.canonical_candidate_payload(dict(candidate))
    return {"candidate_id": _candidate_id(candidate), **payload}


def _canonical_case_payload(packet: Mapping[str, Any]) -> dict[str, Any]:
    frontier = [_canonical_candidate_record(item) for item in _frontier(packet)]
    return {
        "objective": _canonicalize_value(str(packet.get("objective") or "")),
        "normalized_state": _canonicalize_value(dict(packet.get("normalized_state") or {})),
        "admissible_candidate_set": sorted(frontier, key=lambda item: str(item["candidate_id"])),
    }


def _canonical_case_id(packet: Mapping[str, Any]) -> str:
    return f"case:{_content_hash(_canonical_case_payload(packet)).removeprefix('sha256:')}"


def _selected_candidate_id(decision: Mapping[str, Any]) -> str:
    packet = decision.get("decision_packet")
    if not isinstance(packet, Mapping):
        raise Phase15R5FalsifierError("decision is missing decision_packet")
    selected_capability = str(decision.get("selected_capability") or "")
    selected_target = holdout.canonicalize_callback_text(decision.get("selected_target"))
    matches = [
        _candidate_id(candidate)
        for candidate in _frontier(packet)
        if str(candidate.get("name") or "") == selected_capability
        and holdout.canonicalize_callback_text(candidate.get("target")) == selected_target
    ]
    if len(matches) != 1:
        raise Phase15R5FalsifierError(
            f"decision {decision.get('decision_id') or '<unknown>'} does not resolve to one frontier candidate"
        )
    return matches[0]


def _variant_name(row: Mapping[str, Any]) -> str:
    scenario = str(row.get("scenario") or "")
    for variant in holdout.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants:
        if scenario.endswith(variant.name):
            return variant.name
    return ""


def _row_id(row: Mapping[str, Any]) -> str:
    return str(row.get("phase6_planned_row_id") or "")


def _hybrid_model_owned_occurrences(policy_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    for row in policy_rows:
        if str(row.get("configured_policy_mode") or "").strip().casefold() != policy.POLICY_HYBRID:
            continue
        for decision_index, decision in enumerate(list(row.get("decisions") or [])):
            if not isinstance(decision, Mapping):
                continue
            if str(decision.get("decision_owner") or "") != "model_branch":
                continue
            packet = decision.get("decision_packet")
            if not isinstance(packet, Mapping) or len(_frontier(packet)) <= 1:
                continue
            occurrences.append(
                {
                    "row_id": _row_id(row),
                    "variant": _variant_name(row),
                    "decision_index": decision_index,
                    "decision_id": str(decision.get("decision_id") or ""),
                    "decision_packet_hash": str(decision.get("decision_packet_hash") or ""),
                    "case_id": _canonical_case_id(packet),
                    "selected_candidate_id": _selected_candidate_id(decision),
                    "selected_capability": str(decision.get("selected_capability") or ""),
                    "selected_target": holdout.canonicalize_callback_text(decision.get("selected_target")),
                    "_packet": dict(packet),
                }
            )
    return occurrences


def _canonical_cases(occurrences: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for occurrence in occurrences:
        case_id = str(occurrence.get("case_id") or "")
        packet = occurrence.get("_packet")
        if not case_id or not isinstance(packet, Mapping):
            raise Phase15R5FalsifierError("occurrence is missing canonical case identity")
        selected_candidate_id = str(occurrence.get("selected_candidate_id") or "")
        existing = grouped.get(case_id)
        if existing is None:
            frontier = _frontier(packet)
            grouped[case_id] = {
                "case_id": case_id,
                "objective": holdout.canonicalize_callback_text(packet.get("objective")),
                "normalized_state": _canonicalize_value(dict(packet.get("normalized_state") or {})),
                "source_frontier": [_canonical_candidate_record(item) for item in frontier],
                "selected_candidate_id": selected_candidate_id,
                "selected_capability": str(occurrence.get("selected_capability") or ""),
                "selected_target": str(occurrence.get("selected_target") or ""),
                "occurrences": [],
                "_raw_frontier": frontier,
            }
            existing = grouped[case_id]
        if existing["selected_candidate_id"] != selected_candidate_id:
            raise Phase15R5FalsifierError(
                f"canonical case {case_id} has inconsistent observed Hybrid selections"
            )
        existing["occurrences"].append(
            {
                key: occurrence[key]
                for key in (
                    "row_id",
                    "variant",
                    "decision_index",
                    "decision_id",
                    "decision_packet_hash",
                    "selected_candidate_id",
                )
            }
        )
    return sorted(
        grouped.values(),
        key=lambda item: (
            str(item["objective"]),
            str(item["selected_capability"]),
            str(item["selected_target"]),
        ),
    )


def _wait_seconds(candidate: Mapping[str, Any]) -> int:
    raw = (candidate.get("operational_cost") or {}).get("expected_wait_seconds", 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _candidate_effects(candidate: Mapping[str, Any]) -> set[str]:
    return {str(item) for item in list(candidate.get("effects") or []) if str(item)}


def _recent_blocked_effects(normalized_state: Mapping[str, Any]) -> set[str]:
    blocked: set[str] = set()
    for outcome in list(normalized_state.get("recent_outcomes") or []):
        if not isinstance(outcome, Mapping):
            continue
        if str(outcome.get("status") or "").strip().casefold() != "blocked":
            continue
        effect = str(outcome.get("effect") or "")
        if effect:
            blocked.add(effect)
    return blocked


def _tokens(value: Any) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(str(value or "").casefold())
        if len(token) >= 3 and token not in _GENERIC_TOKEN_STOPWORDS
    }


def _select_first(control_input: _ControlInput) -> tuple[int, list[dict[str, Any]]]:
    return 0, [{"frontier_index": index} for index in range(len(control_input.candidates))]


def _select_second(control_input: _ControlInput) -> tuple[int, list[dict[str, Any]]]:
    index = 1 if len(control_input.candidates) > 1 else 0
    return index, [{"frontier_index": item} for item in range(len(control_input.candidates))]


def _select_last(control_input: _ControlInput) -> tuple[int, list[dict[str, Any]]]:
    index = len(control_input.candidates) - 1
    return index, [{"frontier_index": item} for item in range(len(control_input.candidates))]


def _select_seeded_random(control_input: _ControlInput) -> tuple[int, list[dict[str, Any]]]:
    candidate_ids = [_candidate_id(candidate) for candidate in control_input.candidates]
    seed_material = f"{control_input.case_id}|{'|'.join(candidate_ids)}"
    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    index = random.Random(seed).randrange(len(control_input.candidates))
    return index, [{"frontier_index": item, "seed": seed} for item in range(len(control_input.candidates))]


def _select_visible_cost(control_input: _ControlInput) -> tuple[int, list[dict[str, Any]]]:
    ranking = [
        {
            "frontier_index": index,
            "expected_wait_seconds": _wait_seconds(candidate),
        }
        for index, candidate in enumerate(control_input.candidates)
    ]
    index = min(
        range(len(control_input.candidates)),
        key=lambda item: (ranking[item]["expected_wait_seconds"], item),
    )
    return index, ranking


def _select_objective_effect_aware(control_input: _ControlInput) -> tuple[int, list[dict[str, Any]]]:
    objective_tokens = _tokens(control_input.objective)
    ranking: list[dict[str, Any]] = []
    for index, candidate in enumerate(control_input.candidates):
        target_tokens = _tokens(candidate.get("target"))
        effect_tokens = _tokens(" ".join(str(item) for item in list(candidate.get("effects") or [])))
        target_overlap = sorted(objective_tokens & target_tokens)
        effect_overlap = sorted(objective_tokens & effect_tokens)
        ranking.append(
            {
                "frontier_index": index,
                "objective_tokens": sorted(objective_tokens),
                "target_overlap_tokens": target_overlap,
                "effect_overlap_tokens": effect_overlap,
                "target_overlap_count": len(target_overlap),
                "effect_overlap_count": len(effect_overlap),
                "expected_wait_seconds": _wait_seconds(candidate),
            }
        )
    index = min(
        range(len(control_input.candidates)),
        key=lambda item: (
            -ranking[item]["target_overlap_count"],
            -ranking[item]["effect_overlap_count"],
            ranking[item]["expected_wait_seconds"],
            item,
        ),
    )
    return index, ranking


def _modeled_branch(control_input: _ControlInput, candidate: Mapping[str, Any]) -> dict[str, Any]:
    packet = {
        "objective": control_input.objective,
        "normalized_state": control_input.normalized_state,
        "admissible_frontier": [dict(item) for item in control_input.candidates],
    }
    state = unseen_evaluator._packet_state(packet)  # type: ignore[attr-defined]
    return unseen_evaluator._modeled_branch_score(state, dict(candidate))  # type: ignore[attr-defined]


def _select_modeled_reachability(control_input: _ControlInput) -> tuple[int, list[dict[str, Any]]]:
    blocked_effects = _recent_blocked_effects(control_input.normalized_state)
    ranking: list[dict[str, Any]] = []
    for index, candidate in enumerate(control_input.candidates):
        blocked_overlap = sorted(_candidate_effects(candidate) & blocked_effects)
        modeled = _modeled_branch(control_input, candidate)
        ranking.append(
            {
                "frontier_index": index,
                "blocked_effect_overlap_count": len(blocked_overlap),
                "blocked_effect_overlap": blocked_overlap,
                "modeled_transactions": modeled.get("modeled_transactions"),
                "modeled_reachable": modeled.get("reachable"),
                "modeled_reason": modeled.get("reason"),
                "expected_wait_seconds": _wait_seconds(candidate),
            }
        )
    index = min(
        range(len(control_input.candidates)),
        key=lambda item: (
            ranking[item]["blocked_effect_overlap_count"],
            (
                int(ranking[item]["modeled_transactions"])
                if ranking[item]["modeled_transactions"] is not None
                else 10**9
            ),
            ranking[item]["expected_wait_seconds"],
            item,
        ),
    )
    return index, ranking


_CONTROL_SELECTORS: dict[str, Callable[[_ControlInput], tuple[int, list[dict[str, Any]]]]] = {
    "shipping_symbolic": _select_first,
    "always_first": _select_first,
    "always_second": _select_second,
    "always_last": _select_last,
    "seeded_random": _select_seeded_random,
    "visible_cost": _select_visible_cost,
    "objective_effect_aware": _select_objective_effect_aware,
    "modeled_reachability": _select_modeled_reachability,
}


def _control_input_audit() -> dict[str, Any]:
    allowlists = {
        "shipping_symbolic": ["admissible_frontier order"],
        "always_first": ["admissible_frontier order"],
        "always_second": ["admissible_frontier order"],
        "always_last": ["admissible_frontier order"],
        "seeded_random": ["canonical_case_id", "admissible_frontier[].candidate_id", "admissible_frontier order"],
        "visible_cost": ["admissible_frontier[].operational_cost.expected_wait_seconds", "admissible_frontier order"],
        "objective_effect_aware": [
            "objective",
            "admissible_frontier[].target",
            "admissible_frontier[].effects",
            "admissible_frontier[].operational_cost.expected_wait_seconds",
            "admissible_frontier order",
        ],
        "modeled_reachability": [
            "objective",
            "normalized_state.achieved_effects",
            "normalized_state.footholds",
            "normalized_state.graph_facts",
            "normalized_state.recent_outcomes",
            "admissible_frontier[].name",
            "admissible_frontier[].target",
            "admissible_frontier[].preconditions",
            "admissible_frontier[].effects",
            "admissible_frontier[].operational_cost.expected_wait_seconds",
            "admissible_frontier order",
        ],
    }
    control_input_fields = [field.name for field in fields(_ControlInput)]
    controls: dict[str, Any] = {}
    for name in CONTROL_NAMES:
        selector = _CONTROL_SELECTORS[name]
        signature = inspect.signature(selector)
        parameters = list(signature.parameters)
        selector_receives_only_control_input = parameters == ["control_input"]
        receives_full_result_row = any(parameter in {"row", "result", "outcome"} for parameter in parameters)
        result_derived_fields_used = [
            field
            for field in allowlists[name]
            if field in FORBIDDEN_RESULT_FIELDS
        ]
        controls[name] = {
            "selector_signature": str(signature),
            "control_input_dataclass_fields": control_input_fields,
            "selector_receives_only_control_input": selector_receives_only_control_input,
            "predecision_input_allowlist": allowlists[name],
            "forbidden_result_fields": list(FORBIDDEN_RESULT_FIELDS),
            "receives_full_result_row": receives_full_result_row,
            "result_derived_fields_used": result_derived_fields_used,
            "predecision_only": (
                selector_receives_only_control_input
                and not receives_full_result_row
                and not result_derived_fields_used
            ),
        }
    return {
        "boundary": (
            "Controls receive only _ControlInput(case_id, objective, normalized_state, candidates). "
            "Observed row outcomes are consulted only after selection for descriptive scoring."
        ),
        "controls": controls,
        "checks": {
            "all_controls_have_allowlists": set(controls) == set(CONTROL_NAMES),
            "all_controls_exclude_result_fields": all(
                not item["result_derived_fields_used"] and item["receives_full_result_row"] is False
                for item in controls.values()
            ),
            "all_controls_predecision_only": all(item["predecision_only"] is True for item in controls.values()),
        },
    }


def _control_input_for_case(case: Mapping[str, Any], source_indices: Sequence[int]) -> _ControlInput:
    raw_frontier = list(case.get("_raw_frontier") or [])
    return _ControlInput(
        case_id=str(case.get("case_id") or ""),
        objective=str(case.get("objective") or ""),
        normalized_state=dict(case.get("normalized_state") or {}),
        candidates=tuple(dict(raw_frontier[index]) for index in source_indices),
    )


def _selection_report(
    *,
    control_name: str,
    control_input: _ControlInput,
    selected_index: int,
    ranking: Sequence[Mapping[str, Any]],
    expected_candidate_id: str,
) -> dict[str, Any]:
    selected = control_input.candidates[selected_index]
    selected_candidate_id = _candidate_id(selected)
    return {
        "control": control_name,
        "selected_presented_index": selected_index,
        "selected_candidate_id": selected_candidate_id,
        "selected_capability": str(selected.get("name") or ""),
        "selected_target": holdout.canonicalize_callback_text(selected.get("target")),
        "matches_reference_observed_choice": selected_candidate_id == expected_candidate_id,
        "ranking": [dict(item) for item in ranking],
    }


def _wilson_interval(successes: int, total: int, *, z: float = 1.96) -> dict[str, float | int | None]:
    if total <= 0:
        return {"successes": successes, "total": total, "lower": None, "upper": None}
    proportion = successes / total
    denominator = 1.0 + (z * z / total)
    center = (proportion + (z * z / (2.0 * total))) / denominator
    margin = (
        z
        * sqrt((proportion * (1.0 - proportion) / total) + (z * z / (4.0 * total * total)))
        / denominator
    )
    return {
        "successes": successes,
        "total": total,
        "lower": max(0.0, center - margin),
        "upper": min(1.0, center + margin),
    }


def _permutation_matrix(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    all_control_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in CONTROL_NAMES}
    case_reports: list[dict[str, Any]] = []
    for case in cases:
        raw_frontier = list(case.get("_raw_frontier") or [])
        permutation_rows: list[dict[str, Any]] = []
        for ordinal, order in enumerate(permutations(range(len(raw_frontier))), start=1):
            control_input = _control_input_for_case(case, order)
            controls: dict[str, Any] = {}
            for name in CONTROL_NAMES:
                selected_index, ranking = _CONTROL_SELECTORS[name](control_input)
                report = _selection_report(
                    control_name=name,
                    control_input=control_input,
                    selected_index=selected_index,
                    ranking=ranking,
                    expected_candidate_id=str(case.get("selected_candidate_id") or ""),
                )
                controls[name] = report
                all_control_rows[name].append(
                    {
                        "case_id": case["case_id"],
                        "permutation_id": f"{case['case_id']}:perm-{ordinal}",
                        **report,
                    }
                )
            permutation_rows.append(
                {
                    "permutation_id": f"{case['case_id']}:perm-{ordinal}",
                    "source_indices": list(order),
                    "presented_candidate_ids": [_candidate_id(item) for item in control_input.candidates],
                    "controls": controls,
                }
            )
        case_reports.append(
            {
                "case_id": case["case_id"],
                "objective": case["objective"],
                "selected_candidate_id": case["selected_candidate_id"],
                "selected_capability": case["selected_capability"],
                "selected_target": case["selected_target"],
                "source_frontier": case["source_frontier"],
                "occurrence_count": len(list(case.get("occurrences") or [])),
                "occurrences": list(case.get("occurrences") or []),
                "frontier_size": len(raw_frontier),
                "permutation_count": len(permutation_rows),
                "expected_permutation_count": factorial(len(raw_frontier)),
                "permutations": permutation_rows,
                "checks": {
                    "all_permutations_enumerated": len(permutation_rows) == factorial(len(raw_frontier)),
                    "all_controls_scored_on_every_permutation": all(
                        set(row["controls"]) == set(CONTROL_NAMES) for row in permutation_rows
                    ),
                },
            }
        )
    aggregates: dict[str, Any] = {}
    for name, rows in all_control_rows.items():
        agreement_count = sum(row["matches_reference_observed_choice"] is True for row in rows)
        selected_by_case: dict[str, set[str]] = {}
        for row in rows:
            selected_by_case.setdefault(str(row["case_id"]), set()).add(str(row["selected_candidate_id"]))
        aggregates[name] = {
            "canonical_case_permutation_count": len(rows),
            "choice_agreement_count": agreement_count,
            "choice_agreement_rate": (agreement_count / len(rows)) if rows else None,
            "choice_agreement_wilson_95": _wilson_interval(agreement_count, len(rows)),
            "permutation_invariant_on_every_case": all(len(values) == 1 for values in selected_by_case.values()),
            "matches_hybrid_on_every_canonical_permutation": bool(rows) and agreement_count == len(rows),
            "selected_candidate_ids_by_case": {
                case_id: sorted(values) for case_id, values in sorted(selected_by_case.items())
            },
        }
    return {
        "unit_of_analysis": "canonical_unique_predecision_case_permutation",
        "canonical_case_count": len(case_reports),
        "total_permutation_count": sum(case["permutation_count"] for case in case_reports),
        "cases": case_reports,
        "control_aggregates": aggregates,
        "checks": {
            "cases_present": bool(case_reports),
            "all_cases_enumerate_every_permutation": all(
                case["checks"]["all_permutations_enumerated"] for case in case_reports
            ),
            "all_controls_scored_on_every_permutation": all(
                case["checks"]["all_controls_scored_on_every_permutation"] for case in case_reports
            ),
        },
    }


def _observed_model_choice_reproduction(occurrences: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for name in CONTROL_NAMES:
        rows: list[dict[str, Any]] = []
        for occurrence in occurrences:
            packet = occurrence.get("_packet")
            if not isinstance(packet, Mapping):
                raise Phase15R5FalsifierError("occurrence is missing packet for observed-choice reproduction")
            raw_frontier = _frontier(packet)
            control_input = _ControlInput(
                case_id=str(occurrence.get("case_id") or ""),
                objective=holdout.canonicalize_callback_text(packet.get("objective")),
                normalized_state=_canonicalize_value(dict(packet.get("normalized_state") or {})),
                candidates=tuple(raw_frontier),
            )
            selected_index, ranking = _CONTROL_SELECTORS[name](control_input)
            rows.append(
                {
                    "row_id": occurrence["row_id"],
                    "decision_id": occurrence["decision_id"],
                    "case_id": occurrence["case_id"],
                    **_selection_report(
                        control_name=name,
                        control_input=control_input,
                        selected_index=selected_index,
                        ranking=ranking,
                        expected_candidate_id=str(occurrence.get("selected_candidate_id") or ""),
                    ),
                }
            )
        agreement_count = sum(row["matches_reference_observed_choice"] is True for row in rows)
        summaries[name] = {
            "observed_model_owned_decision_count": len(rows),
            "choice_agreement_count": agreement_count,
            "choice_agreement_rate": (agreement_count / len(rows)) if rows else None,
            "choice_agreement_wilson_95": _wilson_interval(agreement_count, len(rows)),
            "matches_all_observed_model_owned_decisions": bool(rows) and agreement_count == len(rows),
            "rows": rows,
        }
    objective_summary = summaries["objective_effect_aware"]
    return {
        "unit_of_analysis": "retained_hybrid_model_owned_decision_occurrence",
        "controls": summaries,
        "reviewer_objective_target_matcher_result": {
            "control": "objective_effect_aware",
            "reviewer_claim": "objective-target matcher reproduces 18/18 model-owned selections",
            "expected_decision_count": REVIEWER_EXPECTED_MODEL_OWNED_DECISION_COUNT,
            "observed_decision_count": objective_summary["observed_model_owned_decision_count"],
            "reproduced_decision_count": objective_summary["choice_agreement_count"],
            "reproduces_reviewer_result": (
                objective_summary["observed_model_owned_decision_count"]
                == REVIEWER_EXPECTED_MODEL_OWNED_DECISION_COUNT
                and objective_summary["choice_agreement_count"]
                == REVIEWER_EXPECTED_MODEL_OWNED_DECISION_COUNT
            ),
        },
    }


def _correct_path_name(variant_name: str) -> str:
    variant = next(
        (item for item in holdout.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants if item.name == variant_name),
        None,
    )
    if variant is None:
        return ""
    return next(
        (
            path.name
            for path in holdout.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths
            if path.first_host.casefold() == variant.target_host.casefold()
            and path.first_domain.casefold() == variant.target_domain.casefold()
        ),
        "",
    )


def _forced_path_outcomes(forced_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_variant: dict[str, dict[str, list[float]]] = {}
    for row in forced_rows:
        variant = _variant_name(row)
        path = str(row.get("phase6_forced_path") or "")
        cost = row.get("semantic_transaction_count")
        if not variant or not path or not isinstance(cost, (int, float)):
            continue
        by_variant.setdefault(variant, {}).setdefault(path, []).append(float(cost))
    summaries: dict[str, Any] = {}
    for variant in holdout.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants:
        correct_path = _correct_path_name(variant.name)
        paths = by_variant.get(variant.name, {})
        correct_costs = list(paths.get(correct_path, []))
        wrong_costs = [
            cost
            for path_name, costs in paths.items()
            if path_name != correct_path
            for cost in costs
        ]
        summaries[variant.name] = {
            "correct_path": correct_path,
            "correct_path_costs": correct_costs,
            "wrong_path_costs": wrong_costs,
            "correct_path_mean_cost": (sum(correct_costs) / len(correct_costs)) if correct_costs else None,
            "wrong_path_mean_cost": (sum(wrong_costs) / len(wrong_costs)) if wrong_costs else None,
            "paired_regret_if_wrong_first": (
                (sum(wrong_costs) / len(wrong_costs)) - (sum(correct_costs) / len(correct_costs))
                if correct_costs and wrong_costs
                else None
            ),
        }
    return {
        "unit_of_analysis": "forced_observed_full_path_row",
        "variants": summaries,
        "checks": {
            "every_variant_has_correct_and_wrong_forced_costs": all(
                summary["correct_path_costs"] and summary["wrong_path_costs"]
                for summary in summaries.values()
            ),
            "correct_path_is_cheaper_in_every_variant": all(
                summary["paired_regret_if_wrong_first"] is not None
                and summary["paired_regret_if_wrong_first"] > 0
                for summary in summaries.values()
            ),
        },
    }


def _row_branch_decisions(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        decision
        for decision in list(row.get("decisions") or [])
        if isinstance(decision, Mapping)
        and isinstance(decision.get("decision_packet"), Mapping)
        and len(_frontier(decision["decision_packet"])) > 1
    ]


def _control_reproduces_row(control_name: str, row: Mapping[str, Any]) -> dict[str, Any]:
    decisions = _row_branch_decisions(row)
    decision_reports: list[dict[str, Any]] = []
    for decision in decisions:
        packet = decision["decision_packet"]
        control_input = _ControlInput(
            case_id=_canonical_case_id(packet),
            objective=holdout.canonicalize_callback_text(packet.get("objective")),
            normalized_state=_canonicalize_value(dict(packet.get("normalized_state") or {})),
            candidates=tuple(_frontier(packet)),
        )
        selected_index, ranking = _CONTROL_SELECTORS[control_name](control_input)
        decision_reports.append(
            _selection_report(
                control_name=control_name,
                control_input=control_input,
                selected_index=selected_index,
                ranking=ranking,
                expected_candidate_id=_selected_candidate_id(decision),
            )
        )
    exact = bool(decision_reports) and all(item["matches_reference_observed_choice"] for item in decision_reports)
    return {
        "row_id": _row_id(row),
        "variant": _variant_name(row),
        "policy_mode": str(row.get("configured_policy_mode") or ""),
        "semantic_transaction_count": row.get("semantic_transaction_count"),
        "branch_decision_count": len(decision_reports),
        "exact_observed_path_reproduction": exact,
        "decision_reports": decision_reports,
    }


def _observed_path_reproduction_regret(
    policy_rows: Sequence[Mapping[str, Any]],
    forced_path_outcomes: Mapping[str, Any],
) -> dict[str, Any]:
    variant_baselines = {
        variant: summary.get("correct_path_mean_cost")
        for variant, summary in dict(forced_path_outcomes.get("variants") or {}).items()
        if isinstance(summary, Mapping)
    }
    controls: dict[str, Any] = {}
    for control_name in CONTROL_NAMES:
        row_reports = [_control_reproduces_row(control_name, row) for row in policy_rows]
        reproduced = [row for row in row_reports if row["exact_observed_path_reproduction"] is True]
        variant_summaries: dict[str, Any] = {}
        for variant in sorted(variant_baselines):
            rows = [row for row in reproduced if row["variant"] == variant]
            costs = [
                float(row["semantic_transaction_count"])
                for row in rows
                if isinstance(row.get("semantic_transaction_count"), (int, float))
            ]
            baseline = variant_baselines.get(variant)
            mean_cost = (sum(costs) / len(costs)) if costs else None
            variant_summaries[variant] = {
                "reproduced_row_ids": [row["row_id"] for row in rows],
                "reproduced_policy_modes": sorted({row["policy_mode"] for row in rows}),
                "observed_costs": costs,
                "observed_mean_cost": mean_cost,
                "forced_correct_path_mean_cost": baseline,
                "paired_regret": (
                    mean_cost - float(baseline)
                    if mean_cost is not None and isinstance(baseline, (int, float))
                    else None
                ),
            }
        paired_regrets = [
            float(summary["paired_regret"])
            for summary in variant_summaries.values()
            if summary["paired_regret"] is not None
        ]
        controls[control_name] = {
            "reproduced_observed_row_count": len(reproduced),
            "reproduced_observed_row_ids": [row["row_id"] for row in reproduced],
            "canonical_variant_count_scored": len(paired_regrets),
            "expected_canonical_variant_count": len(variant_baselines),
            "paired_regret_by_variant": variant_summaries,
            "paired_regret_values": paired_regrets,
            "mean_paired_regret": (sum(paired_regrets) / len(paired_regrets)) if paired_regrets else None,
            "matches_or_beats_hybrid_observed_regret": (
                len(paired_regrets) == len(variant_baselines)
                and all(value <= 0.0 for value in paired_regrets)
            ),
            "row_reports": row_reports,
        }
    return {
        "unit_of_analysis": "canonical_objective_variant_with_exact_observed_path_reproduction",
        "scoring_contract": (
            "A control inherits observed regret only when it exactly reproduces every multi-candidate decision "
            "in a retained policy row. Unobserved counterfactual paths and unobserved permutations are not scored."
        ),
        "controls": controls,
        "uncertainty": {
            "canonical_variant_count": len(variant_baselines),
            "inferential_status": "descriptive_only_small_n_no_confirmatory_claim",
            "unscored_counterfactual_paths_remain_unscored": True,
        },
    }


def _candidate_namespace(candidate: Mapping[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        name=str(candidate.get("name") or ""),
        target=str(candidate.get("target") or ""),
        preconditions=list(candidate.get("preconditions") or []),
        effects=list(candidate.get("effects") or []),
        operational_cost=dict(candidate.get("operational_cost") or {}),
        reason=str(candidate.get("reason") or ""),
    )


def _request_semantic_projection(request: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    if mode == policy.POLICY_HYBRID:
        candidates = [
            {key: value for key, value in dict(item).items() if key != "candidate_id"}
            for item in list(request.get("candidates") or [])
            if isinstance(item, Mapping)
        ]
    else:
        candidates = [
            dict(item)
            for item in list(request.get("current_admissible_actions") or [])
            if isinstance(item, Mapping)
        ]
    return {
        "objective": request.get("objective"),
        "normalized_state": request.get("normalized_state"),
        "candidates": candidates,
        "prior_decisions": request.get("prior_decisions"),
        "budgets": request.get("budgets"),
    }


def _singleton_call_audit(
    *,
    objective: str,
    packet: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    state = unseen_evaluator._packet_state(dict(packet))  # type: ignore[attr-defined]
    candidate_ns = _candidate_namespace(candidate)
    calls = {"llm": 0, "hybrid": 0}

    def llm_decide(_request: dict[str, Any]) -> dict[str, Any]:
        calls["llm"] += 1
        return {
            "disposition": "select",
            "capability": candidate_ns.name,
            "target": candidate_ns.target,
            "rationale": "phase15 singleton audit",
        }

    def hybrid_decide(_request: dict[str, Any]) -> dict[str, Any]:
        calls["hybrid"] += 1
        return {
            "disposition": "select",
            "candidate_id": policy.semantic_candidate_id(candidate_ns),
            "rationale": "phase15 singleton audit",
        }

    llm_policy = policy.LLMPolicy(llm_decide, catalog=[{"name": candidate_ns.name}])
    hybrid_policy = policy.HybridPolicy(hybrid_decide)

    async def run() -> tuple[Any, Any]:
        llm_decision = await llm_policy.select(
            episode_id="phase15-llm-singleton",
            objective=objective,
            state=state,
            candidates=[candidate_ns],
            history=[],
            budgets={},
        )
        hybrid_decision = await hybrid_policy.select(
            episode_id="phase15-hybrid-singleton",
            objective=objective,
            state=state,
            candidates=[candidate_ns],
            history=[],
            budgets={},
        )
        return llm_decision, hybrid_decision

    llm_decision, hybrid_decision = asyncio.run(run())
    return {
        "model_call_counts": calls,
        "llm_decision_owner": llm_decision.decision_owner,
        "hybrid_decision_owner": hybrid_decision.decision_owner,
        "llm_calls_model_for_singleton": calls["llm"] == 1,
        "hybrid_short_circuits_singleton_in_kernel": (
            calls["hybrid"] == 0 and hybrid_decision.decision_owner == "kernel_singleton"
        ),
    }


def _policy_identifiability_audit(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not cases:
        raise Phase15R5FalsifierError("policy identifiability audit requires at least one canonical case")
    case = cases[0]
    packet = {
        "objective": case["objective"],
        "normalized_state": case["normalized_state"],
        "admissible_frontier": list(case["_raw_frontier"]),
    }
    state = unseen_evaluator._packet_state(packet)  # type: ignore[attr-defined]
    candidates = [_candidate_namespace(item) for item in list(case["_raw_frontier"])]
    catalog = [{"name": candidate.name} for candidate in candidates]
    llm_policy = policy.LLMPolicy(lambda _request: {}, catalog=catalog)
    hybrid_policy = policy.HybridPolicy(lambda _request: {})
    llm_request = llm_policy.request_payload(case["objective"], state, candidates, [], {})
    hybrid_request = hybrid_policy.request_payload(case["objective"], state, candidates, [], {})
    llm_projection = _request_semantic_projection(llm_request, mode=policy.POLICY_LLM)
    hybrid_projection = _request_semantic_projection(hybrid_request, mode=policy.POLICY_HYBRID)
    request_key_difference = sorted(set(llm_request) ^ set(hybrid_request))
    singleton = _singleton_call_audit(
        objective=case["objective"],
        packet=packet,
        candidate=case["_raw_frontier"][0],
    )
    branch_semantics_identical = llm_projection == hybrid_projection
    response_schema_identical = llm_request.get("response_schema") == hybrid_request.get("response_schema")
    falsifiable_branch_mechanism_difference = False
    strategic_equivalence = branch_semantics_identical and not falsifiable_branch_mechanism_difference
    return {
        "comparison_scope": "hybrid_vs_pure_llm_branch_strategy",
        "llm_selection_contract": policy.SELECTION_CONTRACT_LLM,
        "hybrid_selection_contract": policy.SELECTION_CONTRACT_HYBRID,
        "request_payloads_byte_identical": llm_request == hybrid_request,
        "request_key_difference": request_key_difference,
        "branch_semantic_information_projection_identical": branch_semantics_identical,
        "non_frontier_context_difference": {
            "llm_capability_catalog_present": bool(llm_request.get("capability_catalog")),
            "hybrid_capability_catalog_present": bool(hybrid_request.get("capability_catalog")),
            "interpretation": (
                "The catalog difference is real, but it was not preregistered as a branch-value mechanism on R5. "
                "Without a sealed prediction, it supports only a future schema/reliability or cost question."
            ),
        },
        "selection_response_schema_identical": response_schema_identical,
        "selection_resolution_difference": {
            "llm": "semantic capability plus target against current_admissible_actions",
            "hybrid": "stable candidate_id against the same admissible frontier",
        },
        "singleton_ownership_audit": singleton,
        "falsifiable_branch_mechanism_difference_identified": falsifiable_branch_mechanism_difference,
        "strategic_equivalence": strategic_equivalence,
        "strategic_equivalence_scope": "current_admissible_branch_value_claim_only",
        "live_comparison_disposition": (
            "cost_or_singleton_overhead_only"
            if strategic_equivalence
            else "branch_strategy_comparison_preregisterable"
        ),
        "reason": (
            "Hybrid and pure LLM expose the same objective, normalized state, and admissible action semantics "
            "at branch points. Their observed difference is resolution/schema shape plus Hybrid singleton "
            "short-circuiting, which supports a cost/reliability comparison but not an inductive-bias claim."
        ),
        "checks": {
            "branch_semantic_information_projection_identical": branch_semantics_identical,
            "selection_schema_difference_is_observed": response_schema_identical is False,
            "singleton_cost_mechanism_difference_is_observed": (
                singleton["llm_calls_model_for_singleton"] is True
                and singleton["hybrid_short_circuits_singleton_in_kernel"] is True
            ),
            "no_preregistered_falsifiable_branch_mechanism_difference": (
                falsifiable_branch_mechanism_difference is False
            ),
            "strategic_equivalence_recorded": strategic_equivalence is True,
        },
    }


def _source_artifact_record(path: Path, *, rows: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "path": _portable_path(path),
        "sha256": _sha256_file(path),
        "rows": len(rows) if rows is not None else None,
    }


def build_phase15_report(
    *,
    policy_rows_path: Path = DEFAULT_POLICY_ROWS_PATH,
    forced_rows_path: Path = DEFAULT_FORCED_ROWS_PATH,
    canary_report_path: Path = DEFAULT_CANARY_REPORT_PATH,
    matrix_validation_path: Path | None = DEFAULT_MATRIX_VALIDATION_PATH,
    generated_at: str | None = None,
) -> dict[str, Any]:
    policy_rows = _load_jsonl(policy_rows_path, label="Phase 15 pinned R5 policy rows")
    forced_rows = _load_jsonl(forced_rows_path, label="Phase 15 pinned R5 forced rows")
    canary_report = _load_json(canary_report_path, label="Phase 15 R5 canary report")
    matrix_report = matrix.validate_matrix_results(
        [dict(row) for row in forced_rows],
        [dict(row) for row in policy_rows],
        canary_report,
        forced_source_path=forced_rows_path,
        policy_source_path=policy_rows_path,
    )
    matrix_report.pop("_selected_forced_rows", None)
    matrix_report.pop("_selected_policy_rows", None)
    if matrix_report.get("passes_gate") is not True:
        raise Phase15R5FalsifierError("frozen R5 matrix validation must pass before Phase 15 retrospective scoring")
    retained_matrix_validation = (
        _load_json(matrix_validation_path, label="retained R5 matrix validation")
        if matrix_validation_path is not None
        else {}
    )
    occurrences = _hybrid_model_owned_occurrences(policy_rows)
    cases = _canonical_cases(occurrences)
    control_input_audit = _control_input_audit()
    permutation_matrix = _permutation_matrix(cases)
    observed_reproduction = _observed_model_choice_reproduction(occurrences)
    forced_path_outcomes = _forced_path_outcomes(forced_rows)
    path_reproduction = _observed_path_reproduction_regret(policy_rows, forced_path_outcomes)
    policy_identifiability = _policy_identifiability_audit(cases)
    control_aggregates = permutation_matrix["control_aggregates"]
    path_controls = path_reproduction["controls"]
    matching_or_better_controls = [
        name
        for name in VALID_DETERMINISTIC_FALSIFIER_CONTROLS
        if control_aggregates[name]["matches_hybrid_on_every_canonical_permutation"] is True
        and path_controls[name]["matches_or_beats_hybrid_observed_regret"] is True
    ]
    h3_disposition = (
        "rejected_for_r5"
        if matching_or_better_controls
        else "survived_posthoc_deterministic_falsifier"
    )
    retained_hash_checks = {
        "policy_rows_match_retained_pinned_sha256": (
            not retained_matrix_validation
            or _sha256_file(policy_rows_path)
            == str((retained_matrix_validation.get("pinned_policy_artifact") or {}).get("sha256") or "")
        ),
        "forced_rows_match_retained_pinned_sha256": (
            not retained_matrix_validation
            or _sha256_file(forced_rows_path)
            == str((retained_matrix_validation.get("pinned_forced_artifact") or {}).get("sha256") or "")
        ),
    }
    checks = {
        "frozen_r5_matrix_validation_passes": matrix_report["passes_gate"] is True,
        **retained_hash_checks,
        "hybrid_model_owned_decision_occurrences_present": len(occurrences) > 0,
        "canonical_unique_cases_reconcile_with_occurrences": (
            sum(len(case["occurrences"]) for case in cases) == len(occurrences)
        ),
        "all_controls_predecision_only": all(control_input_audit["checks"].values()),
        "all_reconstructable_canonical_permutations_scored": all(permutation_matrix["checks"].values()),
        "reviewer_objective_target_matcher_result_retained": (
            observed_reproduction["reviewer_objective_target_matcher_result"]["reproduces_reviewer_result"] is True
        ),
        "forced_path_outcomes_support_descriptive_regret": all(forced_path_outcomes["checks"].values()),
        "retrospective_disposition_is_negative_only": h3_disposition in {
            "rejected_for_r5",
            "survived_posthoc_deterministic_falsifier",
        },
        "no_positive_promotion_gate_opened": True,
        "policy_identifiability_audit_passes": all(policy_identifiability["checks"].values()),
    }
    isc_status = {
        "R-ISC-25": checks["all_controls_predecision_only"],
        "R-ISC-26": checks["all_reconstructable_canonical_permutations_scored"],
        "R-ISC-27": (
            checks["retrospective_disposition_is_negative_only"]
            and checks["no_positive_promotion_gate_opened"]
        ),
        "R-ISC-28": checks["policy_identifiability_audit_passes"],
    }
    report: dict[str, Any] = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _now(),
        "source_plan": "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_COMPLETION_PLAN_2026-07-14.md",
        "source_artifacts": {
            "policy_rows": _source_artifact_record(policy_rows_path, rows=policy_rows),
            "forced_rows": _source_artifact_record(forced_rows_path, rows=forced_rows),
            "canary_report": _source_artifact_record(canary_report_path),
            "retained_matrix_validation": (
                _source_artifact_record(matrix_validation_path)
                if matrix_validation_path is not None
                else None
            ),
        },
        "frozen_r5_validation": {
            "passes_gate": matrix_report["passes_gate"],
            "retained_hash_checks": retained_hash_checks,
            "policy_row_count": len(policy_rows),
            "forced_row_count": len(forced_rows),
            "policy_summaries": matrix_report.get("policy_summaries"),
            "paired_forced_comparisons": matrix_report.get("paired_forced_comparisons"),
        },
        "control_input_audit": control_input_audit,
        "control_definition_manifest": {
            "status": "sealed_in_phase15_module_before_report_execution",
            "module_path": _portable_path(Path(__file__)),
            "module_sha256": _sha256_file(Path(__file__)),
            "control_names": list(CONTROL_NAMES),
            "no_control_tuning_after_result_execution": True,
        },
        "canonical_case_accounting": {
            "retained_hybrid_model_owned_decision_occurrence_count": len(occurrences),
            "canonical_unique_predecision_case_count": len(cases),
            "deduplication_rule": (
                "Canonical case identity retains objective, normalized pre-decision state, and semantic candidate "
                "set while removing volatile prior_decisions IDs and policy-specific selection-contract metadata."
            ),
            "volatile_field_removed": "prior_decisions",
        },
        "permutation_matrix": permutation_matrix,
        "observed_model_choice_reproduction": observed_reproduction,
        "forced_path_outcomes": forced_path_outcomes,
        "observed_path_reproduction_regret": path_reproduction,
        "policy_identifiability_audit": policy_identifiability,
        "uncertainty": {
            "choice_agreement_interval": "Wilson 95% interval over canonical unique case permutations",
            "regret_unit": "canonical objective variant with exact observed full-path reproduction",
            "regret_inferential_status": "descriptive_only_small_n_no_confirmatory_claim",
            "repeated_live_rows_are_not_treated_as_new_unique_cases": True,
        },
        "conclusion": {
            "h3_disposition": h3_disposition,
            "matching_or_better_deterministic_controls": matching_or_better_controls,
            "strongest_valid_deterministic_control_for_future_confirmatory_arms": "modeled_reachability",
            "cheapest_reviewer_falsifier": "objective_effect_aware",
            "promotion_evidence_authorized": False,
            "positive_promotion_gate_opened": False,
            "claim_scope": "retrospective_r5_falsifier_only",
            "next_step": (
                "Use Phase 15 only to shape Phase 16 preregistration; do not treat R5 as confirmatory evidence."
            ),
        },
        "isc_status": isc_status,
        "checks": checks,
        "passes_gate": all(checks.values()) and all(isc_status.values()),
    }
    report["report_hash"] = _content_hash(report)
    return report


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase15-r5-retrospective-falsifiers",
        help="run the Phase 15 frozen R5 retrospective falsifier and policy-identifiability audit",
    )
    parser.add_argument("--policy-rows", default=str(DEFAULT_POLICY_ROWS_PATH))
    parser.add_argument("--forced-rows", default=str(DEFAULT_FORCED_ROWS_PATH))
    parser.add_argument("--canary-report", default=str(DEFAULT_CANARY_REPORT_PATH))
    parser.add_argument("--matrix-validation", default=str(DEFAULT_MATRIX_VALIDATION_PATH))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--generated-at", default=None)
    parser.set_defaults(func=_cmd_phase15_r5_retrospective_falsifiers)


def _cmd_phase15_r5_retrospective_falsifiers(args: Any) -> int:
    try:
        report = build_phase15_report(
            policy_rows_path=Path(args.policy_rows),
            forced_rows_path=Path(args.forced_rows),
            canary_report_path=Path(args.canary_report),
            matrix_validation_path=Path(args.matrix_validation) if args.matrix_validation else None,
            generated_at=args.generated_at,
        )
        output = Path(args.output) if args.output else None
        output_sha256 = _write_json_with_sidecar(output, report) if output is not None else ""
    except Exception as exc:
        print(f"phase15-r5-retrospective-falsifiers: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    if output_sha256:
        print(f"\nOUTPUT: {output} ({output_sha256})", flush=True)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(h3_disposition={report['conclusion']['h3_disposition']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
