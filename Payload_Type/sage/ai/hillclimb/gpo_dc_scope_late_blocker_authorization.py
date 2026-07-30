"""Offline authorization audit for the GPO DC-scope late-blocker contract.

This module is eval-only. It consumes the dedicated Step 1 contract report and
adds the remaining proofability checks needed before any live surface build:

- the full post-blocker target split still satisfies the contract;
- current selectors still miss the unique best target;
- the best target is backed by current generic GPO scope projection;
- the current GPO proof/execution path exists;
- the blocked ADCS lane still uses a current verifier/execution path;
- the existing purpose-range substrate still validates.

The output is an explicit live-authorization decision. Passing this audit does
not mean a live range already exists; it only decides whether Step 3 is justified.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

try:  # package import
    from . import gpo_dc_scope_late_blocker_contract as contract
    from . import policy_replay_selector_experiment as selector_experiment
    from . import purpose_range
    from . import replanning_benchmark
    from ..langgraph import capabilities, graph_reconciler
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import gpo_dc_scope_late_blocker_contract as contract  # type: ignore
    import policy_replay_selector_experiment as selector_experiment  # type: ignore
    import purpose_range  # type: ignore
    import replanning_benchmark  # type: ignore
    import capabilities  # type: ignore
    import graph_reconciler  # type: ignore


REQUIRED_CONTRACT_CHECKS = (
    "shared_prefix_extends_existing_replanning_lane",
    "blocker_is_verifier_backed_and_terminal",
    "post_blocker_frontier_is_exact_two_gpo_targets",
    "equal_visible_operational_cost",
    "all_targets_reach_objective",
    "asymmetric_modeled_downstream_cost",
    "unique_best_target",
    "unique_best_target_is_dc_scoped",
    "current_selectors_choose_worse_target",
    "current_capability_only",
)


class GpoDcScopeLateBlockerAuthorizationError(ValueError):
    """Raised when the authorization audit cannot inspect the declared contract."""


def _target_fields(value: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in str(value or "").split(";"):
        key, sep, raw = item.partition("=")
        if not sep:
            continue
        key = key.strip()
        raw = raw.strip()
        if key:
            fields[key] = raw
    return fields


def _contract_best_candidate(report: dict[str, Any]) -> dict[str, Any] | None:
    frontier = report.get("post_blocker_frontier")
    frontier = frontier if isinstance(frontier, list) else []
    best_indices = report.get("best_indices")
    best_indices = best_indices if isinstance(best_indices, list) else []
    if len(best_indices) != 1:
        return None
    index = best_indices[0]
    if not isinstance(index, int) or not (0 <= index < len(frontier)):
        return None
    candidate = frontier[index]
    return candidate if isinstance(candidate, dict) else None


def _post_blocker_actions() -> tuple[Any, list[Any], Any]:
    state = contract.synthetic_collected_state()
    prefix_state, _path, _reason = contract._replay_achieved(  # type: ignore[attr-defined]
        state,
        contract.GPO_DC_SCOPE_LATE_BLOCKER.shared_prefix,
    )
    blocked_action = contract._select_action(prefix_state, contract.BLOCKED_ACTION)  # type: ignore[attr-defined]
    if blocked_action is None:
        raise GpoDcScopeLateBlockerAuthorizationError(
            f"expected blocked action {contract.BLOCKED_ACTION!r} was not admissible"
        )
    blocked_state, _blocked_effect = contract._blocked_state(prefix_state, blocked_action)  # type: ignore[attr-defined]
    return blocked_state, list(capabilities.actions_from_state(blocked_state)), blocked_action


def _best_action(report: dict[str, Any]) -> Any | None:
    candidate = _contract_best_candidate(report)
    if candidate is None:
        return None
    _state, actions, _blocked_action = _post_blocker_actions()
    target = str(candidate.get("target") or "")
    return next(
        (
            action
            for action in actions
            if str(getattr(action, "name", "") or "") == contract.TARGET_CAPABILITY
            and str(getattr(action, "target", "") or "") == target
        ),
        None,
    )


def _fact_support_report(report: dict[str, Any]) -> dict[str, Any]:
    candidate = _contract_best_candidate(report)
    if candidate is None:
        return {
            "checks": {"unique_best_candidate_available": False},
            "supported": False,
        }
    fields = _target_fields(candidate.get("target"))
    gpo = fields.get("gpo", "")
    domain = fields.get("domain", "")
    dc_host = contract.GPO_DC_SCOPE_LATE_BLOCKER.dc_host
    projected_predicates = graph_reconciler._gpo_scope_facts_from_scalar(  # type: ignore[attr-defined]
        f"{gpo}@{domain}|{dc_host}.{domain}|{domain}|1"
    )
    source_facts = list(candidate.get("source_facts") or [])
    required_prefix = "gpo-affects-dc:"
    checks = {
        "unique_best_candidate_available": bool(gpo and domain),
        "generic_projection_emits_dc_scope_fact": any(
            str(predicate).startswith(required_prefix)
            for predicate in projected_predicates
        ),
        "best_candidate_consumes_dc_scope_fact": any(
            str(fact).startswith(required_prefix)
            for fact in source_facts
        ),
    }
    return {
        "required_fact_prefix": required_prefix,
        "best_target": candidate.get("target"),
        "projected_predicates": projected_predicates,
        "best_candidate_source_facts": source_facts,
        "checks": checks,
        "supported": all(checks.values()),
    }


def _gpo_proof_support_report(report: dict[str, Any]) -> dict[str, Any]:
    action = _best_action(report)
    if action is None:
        return {
            "checks": {"unique_best_action_available": False},
            "supported": False,
        }
    plan = capabilities.build_capability_execution_plan(
        action,
        {
            "controlled_principal": contract.GPO_DC_SCOPE_LATE_BLOCKER.foothold_identity,
            "wait_seconds": 0,
        },
    )
    operations = [step.operation for step in plan.steps]
    verification = capabilities.verify_capability(
        str(getattr(action, "name", "") or ""),
        {"system_command_succeeded": True},
    )
    effects = list(getattr(action, "effects", None) or [])
    checks = {
        "unique_best_action_available": True,
        "execution_plan_builds": plan.ok is True,
        "declares_objective_admin_effect": any(str(effect).startswith("da:") for effect in effects),
        "membership_proof_step_present": "gpo-domain-admin-membership-proof" in operations,
        "capability_verifier_accepts_proof": verification.verdict == "achieved",
    }
    return {
        "best_target": str(getattr(action, "target", "") or ""),
        "plan_ok": plan.ok,
        "plan_reason": plan.reason,
        "plan_operations": operations,
        "verification_verdict": verification.verdict,
        "verification_reason": verification.reason,
        "checks": checks,
        "supported": all(checks.values()),
    }


def _blocker_support_report() -> dict[str, Any]:
    _state, _actions, blocked_action = _post_blocker_actions()
    probe = dict(contract.GPO_DC_SCOPE_LATE_BLOCKER.blocker_probe)
    verification = capabilities.verify_capability(contract.BLOCKED_ACTION, probe)
    plan = capabilities.build_capability_execution_plan(
        blocked_action,
        {
            "ca_pfx_path": r"C:\Windows\Temp\sage_ca_export.pfx",
            "ca_pfx_password": "redacted",
            "dc": f"{contract.GPO_DC_SCOPE_LATE_BLOCKER.dc_host}.{contract.GPO_DC_SCOPE_LATE_BLOCKER.target_domain}",
        },
    )
    operations = [step.operation for step in plan.steps]
    checks = {
        "blocked_action_execution_plan_builds": plan.ok is True,
        "blocked_action_uses_current_certificate_auth_path": (
            "adcs-certificate-forge" in operations
            and "certificate-pkinit-tgt" in operations
            and "kerberos-context-service-proof" in operations
        ),
        "blocker_probe_is_current_verifier_backed": verification.verdict == "blocked",
    }
    return {
        "blocked_action": contract.BLOCKED_ACTION,
        "plan_ok": plan.ok,
        "plan_reason": plan.reason,
        "plan_operations": operations,
        "verification_verdict": verification.verdict,
        "verification_reason": verification.reason,
        "checks": checks,
        "supported": all(checks.values()),
    }


def _substrate_support_report() -> dict[str, Any]:
    purpose_report = purpose_range.validate_purpose_range()
    replanning_report = replanning_benchmark.validate_replanning_benchmark()
    checks = {
        "purpose_range_contract_validates": purpose_report["passes_gate"] is True,
        "existing_replanning_contract_validates": replanning_report["passes_gate"] is True,
        "dedicated_contract_extends_existing_prefix": (
            tuple(contract.GPO_DC_SCOPE_LATE_BLOCKER.shared_prefix[:-1])
            == tuple(replanning_benchmark.SHARED_PREFIX)
        ),
    }
    return {
        "validated_contracts": [
            "purpose-range-validate",
            "replanning-benchmark-validate",
        ],
        "validator_results": {
            "purpose_range": purpose_report["passes_gate"],
            "replanning_benchmark": replanning_report["passes_gate"],
        },
        "checks": checks,
        "supported": all(checks.values()),
    }


def _selector_support_report(report: dict[str, Any]) -> dict[str, Any]:
    scores = report.get("selector_scores")
    scores = scores if isinstance(scores, list) else []
    expected_selectors = set(selector_experiment.SELECTORS)
    observed_selectors = {
        str(item.get("selector") or "")
        for item in scores
        if isinstance(item, dict)
    }
    checks = {
        "all_current_selectors_are_scored": observed_selectors == expected_selectors,
        "all_current_selectors_miss_unique_best_target": bool(scores) and all(
            isinstance(item, dict) and item.get("selected_is_best") is False
            for item in scores
        ),
    }
    return {
        "expected_selectors": sorted(expected_selectors),
        "observed_selectors": sorted(observed_selectors),
        "selector_scores": scores,
        "checks": checks,
        "supported": all(checks.values()),
    }


def run_gpo_dc_scope_late_blocker_authorization_audit(
    contract_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the explicit offline live-authorization decision for the GPO contract."""
    report = contract_report if isinstance(contract_report, dict) else contract.validate_gpo_dc_scope_late_blocker_contract()
    contract_checks = report.get("checks")
    contract_checks = contract_checks if isinstance(contract_checks, dict) else {}
    contract_check_results = {
        name: contract_checks.get(name) is True
        for name in REQUIRED_CONTRACT_CHECKS
    }
    selector_support = _selector_support_report(report)
    fact_support = _fact_support_report(report)
    gpo_proof_support = _gpo_proof_support_report(report)
    blocker_support = _blocker_support_report()
    substrate_support = _substrate_support_report()
    decision_evidence = {
        "dedicated_contract_passes": report.get("passes_gate") is True,
        "dedicated_contract_decisive_checks_pass": all(contract_check_results.values()),
        "current_selectors_miss_unique_best_target": selector_support["supported"] is True,
        "generic_fact_projection_supports_best_target": fact_support["supported"] is True,
        "current_gpo_proof_path_supports_best_target": gpo_proof_support["supported"] is True,
        "blocked_lane_uses_current_verifier_and_execution_path": blocker_support["supported"] is True,
        "existing_purpose_range_substrate_validates": substrate_support["supported"] is True,
    }
    failed_evidence = [
        name
        for name, value in decision_evidence.items()
        if value is not True
    ]
    live_benchmark_authorized = all(decision_evidence.values())
    authorization_reason = (
        "The dedicated GPO late-blocker contract preserves the full two-target split, current selectors still "
        "miss the DC-scoped best target, and the current generic fact, proof, blocker, and substrate checks all "
        "pass. Authorize building the resettable live surface from Step 3."
        if live_benchmark_authorized
        else (
            "Live benchmark authorization remains false because at least one decisive offline contract, "
            "selector, fact, proof, blocker, or substrate check did not pass."
        )
    )
    audit_checks = {
        "required_contract_checks_present": all(name in contract_checks for name in REQUIRED_CONTRACT_CHECKS),
        "decision_evidence_is_complete": set(decision_evidence) == {
            "dedicated_contract_passes",
            "dedicated_contract_decisive_checks_pass",
            "current_selectors_miss_unique_best_target",
            "generic_fact_projection_supports_best_target",
            "current_gpo_proof_path_supports_best_target",
            "blocked_lane_uses_current_verifier_and_execution_path",
            "existing_purpose_range_substrate_validates",
        },
        "authorization_matches_decisive_evidence": live_benchmark_authorized == all(decision_evidence.values()),
        "authorization_decision_is_explicit": isinstance(live_benchmark_authorized, bool),
        "anti_authorization_fails_closed": (
            (live_benchmark_authorized and not failed_evidence)
            or (not live_benchmark_authorized and bool(failed_evidence))
        ),
    }
    return {
        "kind": "gpo_dc_scope_late_blocker_authorization_audit",
        "evidence_scope": (
            "offline dedicated contract output, current generic GPO scope projection, current capability "
            "execution/verifier support, and current purpose-range validation artifacts only"
        ),
        "contract": {
            "kind": report.get("kind"),
            "passes_gate": report.get("passes_gate"),
            "required_check_results": contract_check_results,
            "best_indices": report.get("best_indices"),
            "modeled_transaction_costs": report.get("modeled_transaction_costs"),
        },
        "selector_support": selector_support,
        "fact_support": fact_support,
        "gpo_proof_support": gpo_proof_support,
        "blocker_support": blocker_support,
        "substrate_support": substrate_support,
        "decision_evidence": decision_evidence,
        "authorization": {
            "live_benchmark_authorized": live_benchmark_authorized,
            "failed_evidence": failed_evidence,
            "reason": authorization_reason,
            "next_requirement": (
                "Build the resettable live benchmark surface from roadmap Step 3 before any canary."
                if live_benchmark_authorized
                else "Resolve the failed offline evidence before any live surface build or canary."
            ),
        },
        "checks": audit_checks,
        "passes_gate": all(audit_checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "gpo-dc-scope-late-blocker-authorization-audit",
        help="audit whether the dedicated GPO DC-scope late-blocker contract authorizes live surface work",
    )
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.set_defaults(func=_cmd_gpo_dc_scope_late_blocker_authorization_audit)


def _cmd_gpo_dc_scope_late_blocker_authorization_audit(args: Any) -> int:
    try:
        report = run_gpo_dc_scope_late_blocker_authorization_audit()
    except GpoDcScopeLateBlockerAuthorizationError as exc:
        print(f"gpo-dc-scope-late-blocker-authorization-audit: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(live_benchmark_authorized={report['authorization']['live_benchmark_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
