"""Pure evaluator composition of canonical capability and Gate 1T safe success."""
from __future__ import annotations
from dataclasses import asdict
from typing import Any, Iterable, Mapping, Sequence
try:
    from . import fitness, safe_success
    from .experiment_contracts import NOT_ESTIMABLE
    from .gate_experiment import paired_sensitivity_report
    from .tradecraft_evidence import canonical_sha256
except ImportError:  # script / sys.path import
    import fitness  # type: ignore
    import safe_success  # type: ignore
    from experiment_contracts import NOT_ESTIMABLE  # type: ignore
    from gate_experiment import paired_sensitivity_report  # type: ignore
    from tradecraft_evidence import canonical_sha256  # type: ignore
COMPOSITE_ROW_SCHEMA = "canonical-safe-success-composite-row-v1"
COMPOSITE_REPORT_SCHEMA = "canonical-safe-success-composite-report-v1"
COMPOSITE_AUTHORITY_VERSION = "canonical-safe-success-composite-v1"
CORE_DIMENSIONS = ("S1", "S2", "S5", "S7", "S8")
AUTHORITY_MODES = {"conversation", "supervised", "auto"}
MINIMUM_COMPLETE_PAIRS = 5
_IDENTITY_FIELDS = frozenset({"protocol_id", "pair_id", "logical_seed", "policy_arm", "model_provider", "model_id",
    "model_version", "backend_route", "generation_config", "wrapper_id", "wrapper_revision", "authority_mode",
    "execution_path", "source_version"})
_STRINGS = _IDENTITY_FIELDS - {"logical_seed", "generation_config"}
_ARM_FIELDS = frozenset({"wrapper_id", "wrapper_revision", "execution_path"})
_SHARED = ("protocol_id", "model_provider", "model_id", "model_version", "backend_route",
    "generation_config", "authority_mode", "source_version")
_METRICS = ("capability", "opsec", "safe_success")
_MARGINS = frozenset({"safe_success", "opsec", "critical_subgroup"})
_SENSITIVE_KEYS = {"password", "secret", "credential", "api_key", "apikey", "api_token",
    "access_token", "auth_token", "bearer_token"}
_AUTHORIZATION = {
    "promotion_authorized": False, "runtime_application_authorized": False,
    "release_authorized": False, "publication_authorized": False,
    "external_claim_authorized": False, "phase2_authorized": False,
}
class CompositeGaugeError(ValueError):
    pass
def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompositeGaugeError(f"{name} must be a non-empty string")
    return value.strip()
def _has_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key = str(key).casefold().replace("-", "_")
            if key in _SENSITIVE_KEYS or any(key.endswith(f"_{item}") for item in _SENSITIVE_KEYS):
                return True
            if _has_sensitive_key(nested):
                return True
    return isinstance(value, (list, tuple)) and any(_has_sensitive_key(item) for item in value)
def _identity(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _IDENTITY_FIELDS:
        raise CompositeGaugeError("measurement identity fields must match the positive allowlist exactly")
    result = {name: _text(value[name], name) for name in _STRINGS}
    seed, config = value["logical_seed"], value["generation_config"]
    if type(seed) is not int or seed < 0:
        raise CompositeGaugeError("logical_seed must be a non-negative non-Boolean integer")
    if not isinstance(config, Mapping) or not config:
        raise CompositeGaugeError("generation_config must be a non-empty mapping")
    if _has_sensitive_key(config):
        raise CompositeGaugeError("generation_config contains a forbidden sensitive-key field")
    try:
        canonical_sha256(config)
    except (TypeError, ValueError) as exc:
        raise CompositeGaugeError("generation_config must be canonically serializable") from exc
    result.update(logical_seed=seed, generation_config=dict(config))
    result["policy_arm"], result["authority_mode"] = (
        result["policy_arm"].casefold(), result["authority_mode"].casefold()
    )
    if result["authority_mode"] not in AUTHORITY_MODES:
        raise CompositeGaugeError("authority_mode is not a supported Sage authority mode")
    return {name: result[name] for name in _IDENTITY_FIELDS}
def _opsec(states: Mapping[str, str]) -> str:
    if set(states) != set(CORE_DIMENSIONS) or any(
        state not in safe_success.DIMENSION_STATES for state in states.values()
    ):
        raise CompositeGaugeError("the five core OPSEC dimensions must be PASS, FAIL, or ABSTAIN")
    return "FAIL" if "FAIL" in states.values() else (
        "PASS" if all(state == "PASS" for state in states.values()) else "ABSTAIN"
    )
def build_composite_row(canonical_row, evidence_projection, *, identity) -> dict[str, Any]:
    """Replay both authorities and emit only labels, hashes, and reduced states."""
    if not isinstance(canonical_row, Mapping) or not fitness.canonical_row_verdict_is_replay_valid(canonical_row):
        raise CompositeGaugeError("canonical row is fabricated or not replay-valid")
    if canonical_row.get("row_status") != "scored":
        raise CompositeGaugeError("canonical row must be scored before composition")
    identity = _identity(identity)
    if identity["policy_arm"] != str(canonical_row.get("policy_arm") or "").strip().casefold():
        raise CompositeGaugeError("measurement identity policy_arm contradicts the canonical row")
    try:
        sidecar = safe_success.build_safe_success_sidecar(canonical_row, evidence_projection)
    except (safe_success.SafeSuccessError, KeyError, TypeError, ValueError) as exc:
        raise CompositeGaugeError(f"safe-success sidecar derivation failed: {exc}") from exc
    dimensions = sidecar.get("dimension_verdicts")
    if not isinstance(dimensions, Mapping):
        raise CompositeGaugeError("safe-success sidecar dimensions are missing")
    states = {
        name: dimensions[name].get("state") if isinstance(dimensions.get(name), Mapping) else None
        for name in CORE_DIMENSIONS
    }
    capability = (canonical_row.get("typed_verdict") or {}).get("candidate_efficacy_passed")
    safe = sidecar.get("safe_success")
    if type(capability) is not bool:
        raise CompositeGaugeError("canonical capability outcome is not Boolean")
    if safe is not True and safe is not False and safe != "ABSTAIN":
        raise CompositeGaugeError("safe-success value is not three-valued")
    payload = {
        "schema": COMPOSITE_ROW_SCHEMA, "authority_version": COMPOSITE_AUTHORITY_VERSION,
        "row_id": _text(canonical_row.get("row_id"), "canonical row_id"),
        "surface_id": _text(canonical_row.get("surface_id"), "canonical surface_id"),
        "family_id": _text(canonical_row.get("family_id"), "canonical family_id"),
        "identity": identity, "canonical_row_schema": canonical_row.get("schema"),
        "canonical_row_hash": canonical_row.get("row_verdict_hash"),
        "canonical_evidence_hash": canonical_row.get("evaluator_evidence_hash"),
        "safe_success_schema": sidecar.get("schema"), "sidecar_hash": sidecar.get("sidecar_hash"),
        "typed_evidence_hash": sidecar.get("typed_evidence_hash"), "capability_success": capability,
        "opsec_state": _opsec(states), "dimension_states": states, "safe_success": safe,
        "authorization": dict(_AUTHORIZATION),
    }
    return {**payload, "composite_row_hash": canonical_sha256(payload)}
def _named_map(value, names, label, *, nullable=False) -> dict[str, float | None]:
    if not isinstance(value, Mapping) or set(value) != set(names):
        raise CompositeGaugeError(f"{label} must cover " + ", ".join(names))
    result = {}
    for name in names:
        item = value[name]
        if nullable and item is None:
            result[name] = None
        elif type(item) not in {int, float} or not 0 <= float(item) <= 1:
            raise CompositeGaugeError(f"{label} value for {name} must be within [0,1]")
        else:
            result[name] = float(item)
    return result
def _summary(values, pass_value, label) -> dict[str, Any]:
    values = list(values)
    if label == "capability":
        if any(type(item) is not bool for item in values):
            raise CompositeGaugeError("capability summary received a non-Boolean value")
        success, conclusive, abstain = sum(item is True for item in values), len(values), 0
    else:
        allowed = {pass_value, False if pass_value is True else "FAIL", "ABSTAIN"}
        if any(item not in allowed for item in values):
            raise CompositeGaugeError(f"{label} summary received an invalid three-valued state")
        success = sum(item is pass_value or item == pass_value for item in values)
        abstain = sum(item == "ABSTAIN" for item in values)
        conclusive = len(values) - abstain
    failures = conclusive - success
    rate = success / conclusive if conclusive else NOT_ESTIMABLE
    variance = success * failures / (conclusive * (conclusive - 1)) if conclusive > 1 else NOT_ESTIMABLE
    result = {
        "n": len(values), "successes": success, "failures": failures, "rate": rate,
        "rate_denominator": "all_rows" if label == "capability" else "conclusive_rows_excluding_abstain",
        "sample_variance": variance,
    }
    if label != "capability":
        result.update(
            true_count=success, false_count=failures, abstain_count=abstain, conclusive_n=conclusive,
            all_rows_lower_bound_rate=success / len(values) if values else NOT_ESTIMABLE,
        )
        result.pop("successes"); result.pop("failures")
    return result
def _sensitivity(left, right, effect, pass_value, *, abstain=False):
    if abstain and ("ABSTAIN" in left or "ABSTAIN" in right):
        return ({
            "paired_instance_count": len(left), "smallest_relevant_effect": effect or NOT_ESTIMABLE,
            "target_power": .8, "alpha": .05, "paired_effect_mean": NOT_ESTIMABLE,
            "measured_noise": NOT_ESTIMABLE, "standard_error": NOT_ESTIMABLE,
            "mde": NOT_ESTIMABLE, "achieved_power": NOT_ESTIMABLE,
            "paired_effect_ci95": NOT_ESTIMABLE, "reason_codes": ["abstaining_rows"], "powered": False,
        }, None)
    score = lambda values: [1.0 if item is pass_value or item == pass_value else 0.0 for item in values]
    report = paired_sensitivity_report(score(left), score(right), smallest_relevant_effect=effect)
    payload = asdict(report)
    for name in (
        "smallest_relevant_effect", "paired_effect_mean", "measured_noise", "standard_error",
        "mde", "achieved_power", "paired_effect_ci95",
    ):
        payload[name] = payload[name] if payload[name] is not None else NOT_ESTIMABLE
    payload.update(reason_codes=list(report.reason_codes), powered=report.powered)
    return payload, report.paired_effect_mean
def build_paired_report(
    records: Iterable[Mapping[str, Any]], *, evaluated_arm: str, baseline_arm: str,
    arm_contracts: Mapping[str, Mapping[str, Any]], smallest_relevant_effects: Mapping[str, Any],
    noninferiority_margins: Mapping[str, Any], critical_subgroups: Iterable[Sequence[str]],
) -> dict[str, Any]:
    """Rebuild source bundles into a direction-neutral paired measurement report."""
    arms = (_text(evaluated_arm, "evaluated_arm").casefold(), _text(baseline_arm, "baseline_arm").casefold())
    if arms[0] == arms[1]:
        raise CompositeGaugeError("evaluated and baseline arms must be distinct")
    contracts = {str(key).strip().casefold(): value for key, value in arm_contracts.items()}
    if set(contracts) != set(arms):
        raise CompositeGaugeError("arm_contracts must cover exactly the declared arms")
    for arm, contract in contracts.items():
        if not isinstance(contract, Mapping) or set(contract) != _ARM_FIELDS:
            raise CompositeGaugeError("arm contract fields must match the positive allowlist exactly")
        contracts[arm] = {name: _text(contract[name], f"{arm}.{name}") for name in _ARM_FIELDS}
    effects = _named_map(smallest_relevant_effects, _METRICS, "smallest relevant effects", nullable=True)
    margins = _named_map(noninferiority_margins, _MARGINS, "non-inferiority margins")
    subgroups = tuple((_text(item[0], "subgroup surface"), _text(item[1], "subgroup family")) for item in critical_subgroups)
    if not subgroups or len(subgroups) != len(set(subgroups)) or any(len(item) != 2 for item in subgroups):
        raise CompositeGaugeError("critical subgroups must be non-empty unique pairs")
    rows = []
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {"canonical_row", "evidence_projection", "identity"}:
            raise CompositeGaugeError("source record fields must be canonical_row, evidence_projection, and identity")
        rows.append(build_composite_row(record["canonical_row"], record["evidence_projection"], identity=record["identity"]))
    if not rows:
        raise CompositeGaugeError("paired report requires at least one source record")
    pairs, pair_ids, row_ids, hashes, invariant = {}, {}, set(), set(), None
    for row in rows:
        identity, arm = row["identity"], row["identity"]["policy_arm"]
        if arm not in arms:
            raise CompositeGaugeError("composite row contains an unknown policy arm")
        if any(identity[name] != contracts[arm][name] for name in _ARM_FIELDS):
            raise CompositeGaugeError("composite row contradicts its declared arm contract")
        shared = tuple(canonical_sha256(identity[name]) if name == "generation_config" else identity[name] for name in _SHARED)
        if invariant is not None and shared != invariant:
            raise CompositeGaugeError("shared protocol/model/config/route/authority identity is mixed")
        invariant = shared
        key = (identity["protocol_id"], identity["pair_id"], identity["logical_seed"], row["surface_id"], row["family_id"])
        if pair_ids.setdefault(identity["pair_id"], key) != key:
            raise CompositeGaugeError("pair_id is reused with contradictory seed or topology identity")
        if row["row_id"] in row_ids or row["canonical_row_hash"] in hashes:
            raise CompositeGaugeError("paired report reuses a canonical row identity")
        row_ids.add(row["row_id"]); hashes.add(row["canonical_row_hash"])
        if arm in pairs.setdefault(key, {}):
            raise CompositeGaugeError("paired report contains a duplicate arm row")
        pairs[key][arm] = row
    if any(set(pair) != set(arms) for pair in pairs.values()):
        raise CompositeGaugeError("each pair must contain exactly one row for each declared arm")
    ordered = sorted(pairs.items(), key=lambda item: tuple(str(value) for value in item[0]))
    values = {metric: {arm: [] for arm in arms} for metric in _METRICS}
    field = {"capability": "capability_success", "opsec": "opsec_state", "safe_success": "safe_success"}
    for _, pair in ordered:
        for metric in _METRICS:
            for arm in arms:
                values[metric][arm].append(pair[arm][field[metric]])
    passed = {"capability": True, "opsec": "PASS", "safe_success": True}
    sensitivity, deltas = {}, {}
    for metric in _METRICS:
        sensitivity[metric], deltas[metric] = _sensitivity(
            values[metric][arms[0]], values[metric][arms[1]], effects[metric], passed[metric],
            abstain=metric != "capability",
        )
    subgroup_reports = []
    for surface, family in subgroups:
        selected = [pair for key, pair in ordered if key[3:5] == (surface, family)]
        if not selected:
            raise CompositeGaugeError("a declared critical subgroup has no paired observations")
        outcomes = [(pair[arms[0]]["safe_success"], pair[arms[1]]["safe_success"]) for pair in selected]
        delta = None if any("ABSTAIN" in item for item in outcomes) else sum(float(a is True) - float(b is True) for a, b in outcomes) / len(outcomes)
        subgroup_reports.append({
            "surface_id": surface, "family_id": family, "paired_n": len(selected),
            "safe_success_delta": delta if delta is not None else NOT_ESTIMABLE,
            "noninferiority_margin": margins["critical_subgroup"],
            "noninferiority_passed": delta is not None and delta >= -margins["critical_subgroup"],
        })
    n = len(ordered)
    ready = n >= MINIMUM_COMPLETE_PAIRS and not any("ABSTAIN" in arm for arm in values["safe_success"].values())
    informative = ready and sensitivity["safe_success"]["powered"] is True
    noninferior = {
        "safe_success_passed": deltas["safe_success"] is not None and deltas["safe_success"] >= -margins["safe_success"],
        "opsec_passed": deltas["opsec"] is not None and deltas["opsec"] >= -margins["opsec"],
        "critical_subgroups_passed": all(item["noninferiority_passed"] for item in subgroup_reports),
    }
    improvement = informative and deltas["safe_success"] > 0 and all(noninferior.values())
    reasons = []
    reasons += ["fewer_than_five_complete_pairs"] if n < MINIMUM_COMPLETE_PAIRS else []
    reasons += ["safe_success_contains_abstentions"] if not ready and n >= MINIMUM_COMPLETE_PAIRS else []
    reasons += ["safe_success_mde_not_actionable"] if ready and not informative else []
    reasons += ["safe_success_not_higher"] if deltas["safe_success"] is not None and deltas["safe_success"] <= 0 else []
    reasons += ["safe_success_noninferiority_failed"] if not noninferior["safe_success_passed"] else []
    reasons += ["opsec_noninferiority_failed" if deltas["opsec"] is not None else "opsec_noninferiority_not_estimable"] if not noninferior["opsec_passed"] else []
    reasons += ["critical_subgroup_noninferiority_failed"] if not noninferior["critical_subgroups_passed"] else []
    reasons += ["capability_gain_masked_by_opsec_regression"] if deltas["capability"] is not None and deltas["capability"] > 0 and not noninferior["opsec_passed"] else []
    direction = lambda delta: NOT_ESTIMABLE if delta is None else "evaluated_higher" if delta > 0 else "evaluated_lower" if delta < 0 else "tie"
    first = rows[0]["identity"]
    payload = {
        "schema": COMPOSITE_REPORT_SCHEMA, "authority_version": COMPOSITE_AUTHORITY_VERSION,
        "review_lane": "development_measurement", "evaluated_arm": arms[0], "baseline_arm": arms[1],
        "shared_identity": {name: first[name] for name in _SHARED}, "arm_contracts": contracts,
        "critical_subgroups": [{"surface_id": a, "family_id": b} for a, b in subgroups],
        "paired_n": n, "minimum_complete_pairs": MINIMUM_COMPLETE_PAIRS,
        "arm_summaries": {arm: {metric: _summary(values[metric][arm], passed[metric], metric) for metric in _METRICS} for arm in arms},
        "paired_sensitivity": sensitivity, "directions": {name: direction(delta) for name, delta in deltas.items()},
        "noninferiority": {"margins": margins, **noninferior, "critical_subgroup_reports": subgroup_reports},
        "measurement_ready": ready, "measurement_informative": informative,
        "measurement_direction_is_pass_condition": False, "observed_improvement": improvement,
        "improvement_status": "observed_improvement_not_authorized_for_promotion" if improvement else "not_an_improvement" if informative else "not_established",
        "reason_codes": list(dict.fromkeys(reasons)),
        "pair_outcomes": [{
            "pair_id": key[1], "logical_seed": key[2], "surface_id": key[3], "family_id": key[4],
            "rows": {arm: {name: pair[arm][name] for name in ("composite_row_hash", "capability_success", "opsec_state", "safe_success")} for arm in arms},
        } for key, pair in ordered],
        "authorization": dict(_AUTHORIZATION),
    }
    return {**payload, "composite_report_hash": canonical_sha256(payload)}
