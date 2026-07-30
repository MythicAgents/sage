"""Eval-only decision gate for generic runtime target-value modeling.

The current evidence has two different failure modes:

1. the runtime capability model may genuinely lack a reusable target-value concept;
2. a cheap offline selector may simply fail to model downstream reachability that the
   runtime model and learned policies already use.

This module keeps those separate. It consumes the existing multi-family target-value
census and the expanded packet-backed selector experiment, then emits one explicit
architecture decision. It does not change runtime policy or capability semantics.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

try:  # package import
    from . import policy_replay_selector_experiment as selector_experiment
    from . import target_value_census
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import policy_replay_selector_experiment as selector_experiment  # type: ignore
    import target_value_census  # type: ignore


class TargetValueRuntimeDecisionError(ValueError):
    """Raised when the decision inputs are missing or malformed."""


def _list_field(payload: dict[str, Any], *path: str) -> list[Any]:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    return list(value) if isinstance(value, list) else []


def _bool_field(payload: dict[str, Any], *path: str) -> bool:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return False
        value = value.get(key)
    return value is True


def run_target_value_runtime_decision(
    *,
    census_report: dict[str, Any] | None = None,
    selector_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the Step 8 architecture decision from current eval evidence."""

    census = census_report or target_value_census.run_target_value_census()
    selector = selector_report or selector_experiment.run_selector_experiment()
    if not isinstance(census, dict) or not isinstance(selector, dict):
        raise TargetValueRuntimeDecisionError("decision inputs must be report dictionaries")

    natural_families = sorted(str(item) for item in _list_field(census, "aggregate", "naturally_asymmetric_families"))
    collapsed_families = sorted(str(item) for item in _list_field(census, "aggregate", "collapsed_equal_cost_families"))
    classifications = census.get("aggregate", {}).get("classifications", {})
    classifications = classifications if isinstance(classifications, dict) else {}
    indistinguishable_surfaces = sorted(
        str(item)
        for item in list(classifications.get(target_value_census.INDISTINGUISHABLE_FROM_CURRENT_FACTS) or [])
    )
    selector_divergence_cases = sorted(
        str(item)
        for item in _list_field(
            selector,
            "aggregate",
            "case_ids_where_experimental_selector_diverges_from_live_learned_choices",
        )
    )
    selector_failure_cases = sorted(
        str(item)
        for item in _list_field(
            selector,
            "aggregate",
            "case_ids_where_experimental_selector_fails_expected_order",
        )
    )
    multiple_natural_families = len(set(natural_families)) >= 2
    collapse_and_fact_absence_are_distinguished = bool(collapsed_families) and bool(indistinguishable_surfaces)
    selector_gap_observed = bool(selector_divergence_cases) and bool(selector_failure_cases)
    add_runtime_target_value_abstraction = False
    recommended_next_move = "build_cheap_unseen_candidate_evaluator"

    checks = {
        "census_passes": census.get("passes_gate") is True,
        "selector_experiment_passes": selector.get("passes_gate") is True,
        "multiple_unrelated_families_already_express_target_asymmetry": multiple_natural_families,
        "collapse_and_fact_absence_are_distinguished": collapse_and_fact_absence_are_distinguished,
        "packet_selector_gap_is_observed": selector_gap_observed,
        "learned_policy_tie_is_still_reported": _bool_field(
            selector,
            "selector_findings",
            "experimental_selector_preserves_learned_policy_tie",
        ),
        "decision_rejects_runtime_field_without_multi_family_gap": add_runtime_target_value_abstraction is False,
        "next_move_is_cheap_evaluator": recommended_next_move == "build_cheap_unseen_candidate_evaluator",
    }
    return {
        "kind": "target_value_runtime_decision",
        "evidence": {
            "naturally_asymmetric_families": natural_families,
            "collapsed_equal_cost_families": collapsed_families,
            "indistinguishable_surface_ids": indistinguishable_surfaces,
            "selector_divergence_case_ids": selector_divergence_cases,
            "selector_expected_order_failure_case_ids": selector_failure_cases,
            "selector_case_count": int((selector.get("aggregate") or {}).get("case_count") or 0),
            "selector_adds_discrimination_over_lowest_visible_wait": _bool_field(
                selector,
                "aggregate",
                "adds_discrimination_over_lowest_visible_wait",
            ),
            "selector_any_improves_over_lowest_visible_wait": _bool_field(
                selector,
                "aggregate",
                "any_selector_improves_over_lowest_visible_wait",
            ),
        },
        "decision": {
            "add_runtime_target_value_abstraction": add_runtime_target_value_abstraction,
            "recommended_next_move": recommended_next_move,
            "diagnosis": "offline_downstream_reachability_gap_not_runtime_target_value_gap",
            "runtime_change_scope": "none",
            "reason": (
                "Current generic facts and effects already express target asymmetry in multiple unrelated "
                "capability families, and the learned policies use that signal on the live GPO target-choice "
                "surface. The failing packet-local selector lacks downstream reachability scoring; adding a "
                "runtime target-value field would duplicate information the runtime model already carries."
            ),
            "falsifier": (
                "Revisit this decision only if multiple unrelated live or census families remain equal-cost "
                "despite current generic facts and effects that should distinguish objective reachability."
            ),
            "minimal_runtime_impact": (
                "Keep runtime policy and capability schemas unchanged; move the next experiment into an "
                "eval-only unseen-candidate scorer."
            ),
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "target-value-runtime-decision",
        help="decide whether current evidence justifies a generic runtime target-value abstraction",
    )
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.set_defaults(func=_cmd_target_value_runtime_decision)


def _cmd_target_value_runtime_decision(args: Any) -> int:
    try:
        report = run_target_value_runtime_decision()
    except (TargetValueRuntimeDecisionError, selector_experiment.SelectorExperimentError) as exc:
        print(f"target-value-runtime-decision: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(add_runtime_target_value_abstraction="
        f"{report['decision']['add_runtime_target_value_abstraction']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1

