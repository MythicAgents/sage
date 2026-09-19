"""Explicit evaluator coverage across runtime capabilities and legacy techniques.

The runtime capability catalog and ``TECHNIQUE_MODEL`` are separate semantic
universes: the former names current capability actions, while the latter names
legacy state-transition techniques.  Neither is a gauge scoring vocabulary.
This evaluator-only inventory records whether a source effect has an exact
independent oracle and producer today; it does not make the runtime import this
module, add a proof producer, or equate nearby effects (for example, credential
material with Domain Administrator control).  ``FOOTHOLD`` is also intentionally
absent because it is an observed starting condition, not an action in either
source universe.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

try:  # package import
    from ..langgraph import capabilities, engagement_state
    from . import effect_registry
except ImportError:  # direct evaluator/test import
    import capabilities  # type: ignore
    import engagement_state  # type: ignore
    import effect_registry  # type: ignore


COVERAGE_VERSION = "sage-effect-coverage-v1"
RUNTIME_CAPABILITY_UNIVERSE = "runtime_capability"
LEGACY_TECHNIQUE_UNIVERSE = "legacy_technique"


class CoverageStatus(StrEnum):
    SUPPORTED = "supported"
    DECLARED_UNPRODUCED = "declared_unproduced"
    UNSCORED = "unscored"


@dataclass(frozen=True)
class EffectCoverage:
    universe: str
    name: str
    effect_family: str
    milestone: str | None
    oracle_id: str | None
    status: CoverageStatus
    producer: str | None
    reason: str


@dataclass(frozen=True)
class OracleCoverage:
    milestone: str
    oracle_id: str
    status: CoverageStatus
    producer: str | None
    reason: str


def _row(
    universe: str,
    name: str,
    effect_family: str,
    *,
    milestone: str | None = None,
    status: CoverageStatus,
    producer: str | None = None,
    reason: str,
) -> EffectCoverage:
    oracle_id = effect_registry.contract_for(milestone).oracle_id if milestone else None
    return EffectCoverage(universe, name, effect_family, milestone, oracle_id, status, producer, reason)


EFFECT_COVERAGE: tuple[EffectCoverage, ...] = (
    _row(RUNTIME_CAPABILITY_UNIVERSE, "collect-graph", "directory graph collection",
         milestone="GRAPH_COLLECTED", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes -> live_seams.graph_collected_probe",
         reason="the generic graph fact producer is registered when GRAPH_COLLECTED is selected"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "gpo-controlled-system-exec", "SYSTEM execution",
         milestone="SYSTEM_ON_HOST", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes -> live_seams.system_execution_probe",
         reason="the generic SYSTEM identity producer is registered when SYSTEM_ON_HOST is selected"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "gpo-controlled-system-exec", "conditional child-domain administration",
         milestone="DA_CHILD", status=CoverageStatus.DECLARED_UNPRODUCED,
         reason="SYSTEM evidence does not satisfy the required matched administrator differential"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "grant-directory-rights", "directory replication rights",
         milestone="REPLICATION_RIGHTS_PARENT", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> replication_rights.parent_replication_rights_observation",
         reason="pure replay is available only with an exact committed payload-derived receipt; generic live assembly does not capture it"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "dcsync-krbtgt", "krbtgt credential material",
         milestone="KRBTGT_DUMPED", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes.krbtgt_paths",
         reason="the generic producer binds credential-store and task-output krbtgt evidence"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "dcsync-krbtgt", "parent-domain krbtgt credential material",
         milestone="DCSYNC_PARENT", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes.krbtgt_paths",
         reason="the generic producer binds exact-parent credential-store and task-output krbtgt evidence"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "dcsync-krbtgt", "objective-bound krbtgt credential material",
         milestone="OBJECTIVE", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes.krbtgt_paths",
         reason="selected objective routes reuse the exact-realm krbtgt producer"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "dcsync-account", "account credential material",
         milestone="ACCOUNT_CREDENTIAL_MATERIAL", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> credential_effects.account_credential_material_candidate",
         reason="pure replay requires exact fresh DCSync account request/output bindings; it does not substitute parent krbtgt material"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "forge-golden-ticket", "domain administrator control",
         milestone="DA_CHILD", status=CoverageStatus.DECLARED_UNPRODUCED,
         reason="ticket creation cannot substitute for the matched child administrator differential"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "forge-golden-ticket", "parent service context",
         milestone="CROSS_DOMAIN_CONTEXT", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes -> ticket_context.cross_domain_context_candidate",
         reason="only a fresh exact-operation/callback forged-ticket then parent-service sequence is supported"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "ensure-kerberos-context", "callback-scoped Kerberos context",
         milestone="CROSS_DOMAIN_CONTEXT", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes -> ticket_context.cross_domain_context_candidate",
         reason="only the fresh forged-ticket/service sequence proves this bounded parent-context route"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "ensure-account-kerberos-context", "account Kerberos context",
         milestone="ACCOUNT_KERBEROS_CONTEXT", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> account_context.account_kerberos_context_observation",
         reason="pure replay requires exact operation/callback/fresh retained task lineage; no generic live builder claim"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "read-managed-local-admin-secret", "managed local-admin credential read",
         milestone="MANAGED_LOCAL_ADMIN_SECRET", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> credential_effects.managed_local_admin_secret_candidate",
         reason="pure replay requires exact fresh retained credential material; it remains separate from administrator control"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "use-managed-local-admin-secret", "managed local-admin authentication",
         milestone="MANAGED_LOCAL_ADMIN_ACCESS", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> credential_effects.managed_local_admin_access_candidate",
         reason="pure replay requires exact credential-to-target access lineage"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "execute-as-local-admin", "local-admin execution context",
         milestone="MANAGED_LOCAL_ADMIN_ACCESS", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> credential_effects.managed_local_admin_access_candidate",
         reason="exact target access is a pure replay producer; it is not a generic objective substitute"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "endpoint-protection-adjustment", "endpoint protection change",
         milestone="ENDPOINT_PROTECTION_STATE", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> directory_endpoint_effects.endpoint_protection_observation",
         reason="pure replay requires exact fresh native task and generated request/output bindings"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "adcs-ca-private-key-export", "CA private-key material",
         milestone="CA_KEY_EXPORTED", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> certificate_material.certificate_material_observation",
         reason="pure replay requires frozen request, response, artifact, and certificate lineage"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "adcs-esc-certificate-enroll", "ADCS certificate enrollment",
         milestone="CERTIFICATE_ENROLLED", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> certificate_material.certificate_material_observation",
         reason="pure replay requires frozen enrollment request, response, and artifact lineage"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "adcs-certificate-auth", "certificate-based authentication",
         milestone="OBJECTIVE", status=CoverageStatus.SUPPORTED,
         producer="trust_walker_outcome.certificate_admin_candidate (pilot_only)",
         reason="the bounded Trust Walker certificate differential is an evaluator-owned objective producer"),
    _row(RUNTIME_CAPABILITY_UNIVERSE, "adcs-certificate-auth", "child-domain administrator control",
         milestone="DA_CHILD", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes -> trust_walker_outcome.domain_admin_differential_candidate",
         reason="requires fresh raw SACL allow/non-admin denial with exact child identities; authentication alone is insufficient"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "collect-graph", "directory graph collection",
         milestone="GRAPH_COLLECTED", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes -> live_seams.graph_collected_probe",
         reason="the generic graph fact producer is registered when GRAPH_COLLECTED is selected"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "gpo-abuse", "SYSTEM execution",
         milestone="SYSTEM_ON_HOST", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes -> live_seams.system_execution_probe",
         reason="the generic SYSTEM identity producer is registered when SYSTEM_ON_HOST is selected"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "lsass-dump", "host credential material",
         milestone="HOST_CREDENTIAL_MATERIAL", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> credential_effects.host_credential_material_candidate",
         reason="pure replay requires an exact fresh callback-host dump request and labeled material"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "rbcd-standin", "resource-based constrained delegation",
         milestone="RBCD_RIGHTS", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> directory_endpoint_effects.rbcd_rights_observation",
         reason="pure replay requires exact StandIn request and committed payload-derived AllowedToAct poststate"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "dcsync-rights-grant", "directory replication rights",
         milestone="REPLICATION_RIGHTS_PARENT", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> replication_rights.parent_replication_rights_observation",
         reason="pure committed-receipt replay exists; generic live assembly does not capture this effect"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "dcsync-user", "account credential material",
         milestone="ACCOUNT_CREDENTIAL_MATERIAL", status=CoverageStatus.SUPPORTED,
         producer="reproducible_claims.capability_effect -> credential_effects.account_credential_material_candidate",
         reason="pure replay requires exact fresh DCSync account request/output bindings; it is not parent krbtgt material"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "dcsync", "krbtgt credential material",
         milestone="KRBTGT_DUMPED", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes.krbtgt_paths",
         reason="the generic producer binds credential-store and task-output krbtgt evidence"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "dcsync", "parent-domain krbtgt credential material",
         milestone="DCSYNC_PARENT", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes.krbtgt_paths",
         reason="the generic producer binds exact-parent credential-store and task-output krbtgt evidence"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "dcsync", "objective-bound krbtgt credential material",
         milestone="OBJECTIVE", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes.krbtgt_paths",
         reason="selected objective routes reuse the exact-realm krbtgt producer"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "domain-admin-membership-check", "child-domain administrator membership",
         milestone="DA_CHILD", status=CoverageStatus.DECLARED_UNPRODUCED,
         reason="the registry requires matched admin/non-admin task evidence, which build_probes marks unavailable"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "golden-ticket", "domain administrator control",
         milestone="DA_CHILD", status=CoverageStatus.DECLARED_UNPRODUCED,
         reason="the child-domain oracle exists, but build_probes has no matched differential producer"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "golden-ticket", "parent service context",
         milestone="CROSS_DOMAIN_CONTEXT", status=CoverageStatus.SUPPORTED,
         producer="run_gauge_live.build_probes -> ticket_context.cross_domain_context_candidate",
         reason="only a fresh exact-operation/callback forged-ticket then parent-service sequence is supported"),
    _row(LEGACY_TECHNIQUE_UNIVERSE, "sid-history-escalation", "parent-domain administrator control",
         milestone="DA_PARENT", status=CoverageStatus.DECLARED_UNPRODUCED,
         reason="the exact-target admin differential oracle exists; this SID-history route lacks native matched allow/deny capture"),
)


ORACLE_COVERAGE: tuple[OracleCoverage, ...] = (
    OracleCoverage("FOOTHOLD", "mythic.callback.foothold.v1", CoverageStatus.SUPPORTED,
                   "probes.read_ground_truth_from_probes", "typed starting-callback observation"),
    OracleCoverage("GRAPH_COLLECTED", "bloodhound.graph-collected.v1", CoverageStatus.SUPPORTED,
                   "run_gauge_live.build_probes -> live_seams.graph_collected_probe", "payload-derived graph fact"),
    OracleCoverage("SYSTEM_ON_HOST", "mythic.task.system-identity.v1", CoverageStatus.SUPPORTED,
                   "run_gauge_live.build_probes -> live_seams.system_execution_probe", "completed Mythic identity task"),
    OracleCoverage("DA_CHILD", "mythic.task.domain-admin-differential.v1", CoverageStatus.SUPPORTED,
                   "run_gauge_live.build_probes -> trust_walker_outcome.domain_admin_differential_candidate",
                   "certificate SACL differential only; missing exact operation or principal aliases remains unavailable"),
    OracleCoverage("KRBTGT_DUMPED", "mythic.krbtgt-material.v1", CoverageStatus.SUPPORTED,
                   "run_gauge_live.build_probes.krbtgt_paths", "exact-realm credential-store or task-output material"),
    OracleCoverage("CROSS_DOMAIN_CONTEXT", "mythic.cross-domain-context.v1", CoverageStatus.SUPPORTED,
                   "run_gauge_live.build_probes -> ticket_context.cross_domain_context_candidate",
                   "fresh parent ticket/service sequence only; exact operation/callback/realm bindings required"),
    OracleCoverage("REPLICATION_RIGHTS_PARENT", "bloodhound.replication-rights.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> replication_rights.parent_replication_rights_observation",
                   "pure committed-receipt replay; no generic live capture branch"),
    OracleCoverage("DCSYNC_PARENT", "mythic.dcsync-material.v1", CoverageStatus.SUPPORTED,
                   "run_gauge_live.build_probes.krbtgt_paths",
                   "exact-parent credential-store or completed DCSync task-output material"),
    OracleCoverage("CERT_FORGED", "mythic.certificate-material.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> certificate_material.certificate_material_observation",
                   "pure frozen request/response/artifact replay; no generic live capture branch"),
    OracleCoverage("OBJECTIVE", "independent.objective-control.v1", CoverageStatus.SUPPORTED,
                   "run_gauge_live.build_probes; trust_walker_outcome.certificate_admin_candidate (pilot_only)",
                   "only the named krbtgt, remote-exec, and pilot certificate routes are supported"),
    OracleCoverage("ACCOUNT_KERBEROS_CONTEXT", "mythic.account-kerberos-context.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> account_context.account_kerberos_context_observation",
                   "exact retained operation/callback task sequence only"),
    OracleCoverage("MANAGED_LOCAL_ADMIN_SECRET", "mythic.managed-local-admin-secret.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> credential_effects.managed_local_admin_secret_candidate",
                   "exact retained fresh material only"),
    OracleCoverage("MANAGED_LOCAL_ADMIN_ACCESS", "mythic.managed-local-admin-access.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> credential_effects.managed_local_admin_access_candidate",
                   "exact credential-to-target access lineage only"),
    OracleCoverage("ENDPOINT_PROTECTION_STATE", "mythic.endpoint-protection-state.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> directory_endpoint_effects.endpoint_protection_observation",
                   "exact fresh native generated request/output only"),
    OracleCoverage("CA_KEY_EXPORTED", "mythic.ca-private-key-material.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> certificate_material.certificate_material_observation",
                   "frozen material receipt only"),
    OracleCoverage("CERTIFICATE_ENROLLED", "mythic.certificate-enrollment.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> certificate_material.certificate_material_observation",
                   "frozen enrollment receipt only"),
    OracleCoverage("HOST_CREDENTIAL_MATERIAL", "mythic.host-credential-material.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> credential_effects.host_credential_material_candidate",
                   "exact fresh callback-host dump only"),
    OracleCoverage("RBCD_RIGHTS", "bloodhound.rbcd-rights.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> directory_endpoint_effects.rbcd_rights_observation",
                   "exact StandIn request and committed payload-derived poststate only"),
    OracleCoverage("ACCOUNT_CREDENTIAL_MATERIAL", "mythic.account-credential-material.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> credential_effects.account_credential_material_candidate",
                   "exact fresh DCSync account request/output only"),
    OracleCoverage("DA_PARENT", "mythic.task.parent-domain-admin-differential.v1", CoverageStatus.SUPPORTED,
                   "reproducible_claims.capability_effect -> trust_walker_outcome.domain_admin_observation",
                   "exact-target SACL allow/non-admin denial only; no SID-history route capture is inferred"),
)

# These are names of the evaluator seams referenced by the general
# ``build_probes`` assembly.  They are an inventory, not a trust decision about
# a callable implementation; tests exercise the returned typed observations.
GENERAL_BUILD_PROBE_FACTORIES = frozenset({
    "_krbtgt_task_output_has_material",
    "graph_collected_probe",
    "mythic_credential_probe",
    "operation_task_evidence",
    "remote_execution_probe",
    "system_execution_probe",
})


def validate_general_probe_factories(factory_names: Iterable[str]) -> frozenset[str]:
    """Fail closed when general assembly gains, loses, or substitutes a producer.

    The inventory deliberately compares factory identities only.  It does not
    infer internals of trusted evaluator callables or define a new admission
    mechanism.
    """
    observed = frozenset(str(name) for name in factory_names)
    if observed != GENERAL_BUILD_PROBE_FACTORIES:
        missing = sorted(GENERAL_BUILD_PROBE_FACTORIES - observed)
        unknown = sorted(observed - GENERAL_BUILD_PROBE_FACTORIES)
        raise ValueError(f"general probe factory inventory mismatch: missing={missing}, unknown={unknown}")
    return observed


def _source_names() -> dict[str, set[str]]:
    return {
        RUNTIME_CAPABILITY_UNIVERSE: {row["name"] for row in capabilities.capability_catalog()},
        LEGACY_TECHNIQUE_UNIVERSE: set(engagement_state.TECHNIQUE_MODEL),
    }


def validate_effect_coverage(rows: Iterable[EffectCoverage] = EFFECT_COVERAGE) -> tuple[EffectCoverage, ...]:
    """Validate row identity, source parity, and registry bindings; return canonical rows."""
    checked = tuple(rows)
    if not checked:
        raise ValueError("effect coverage is empty")
    source_names = _source_names()
    seen: set[tuple[str, str, str | None]] = set()
    covered: dict[str, set[str]] = {universe: set() for universe in source_names}
    for row in checked:
        key = (row.universe, row.name, row.milestone)
        if row.universe not in source_names:
            raise ValueError(f"unknown effect coverage universe: {row.universe}")
        if key in seen:
            raise ValueError(f"duplicate effect coverage row: {row.universe}:{row.name}:{row.milestone}")
        if row.name not in source_names[row.universe]:
            raise ValueError(f"unknown source semantic name: {row.universe}:{row.name}")
        if not row.effect_family or not row.reason:
            raise ValueError(f"effect coverage row is incomplete: {row.universe}:{row.name}")
        if row.milestone is None:
            if row.oracle_id is not None:
                raise ValueError(f"unscored row has an oracle binding: {row.universe}:{row.name}")
        else:
            try:
                contract = effect_registry.contract_for(row.milestone)
            except KeyError as exc:
                raise ValueError(f"unknown effect coverage milestone: {row.milestone}") from exc
            if row.oracle_id != contract.oracle_id:
                raise ValueError(f"oracle binding mismatch: {row.universe}:{row.name}")
        if row.status is CoverageStatus.SUPPORTED and not row.producer:
            raise ValueError(f"supported row has no producer: {row.universe}:{row.name}")
        if row.status is not CoverageStatus.SUPPORTED and row.producer is not None:
            raise ValueError(f"unavailable row claims a producer: {row.universe}:{row.name}")
        seen.add(key)
        covered[row.universe].add(row.name)
    for universe, names in source_names.items():
        if covered[universe] != names:
            missing = sorted(names - covered[universe])
            extra = sorted(covered[universe] - names)
            raise ValueError(f"source parity mismatch for {universe}: missing={missing}, extra={extra}")
    return checked


def validate_oracle_coverage(rows: Iterable[OracleCoverage] = ORACLE_COVERAGE) -> tuple[OracleCoverage, ...]:
    """Validate the complete current registry support inventory."""
    checked = tuple(rows)
    registered = {
        **effect_registry.validate_registry(),
        **effect_registry.validate_registry(effect_registry.CAPABILITY_EFFECT_REGISTRY),
    }
    if {row.milestone for row in checked} != set(registered):
        raise ValueError("oracle coverage does not match the registry")
    if len({row.milestone for row in checked}) != len(checked):
        raise ValueError("duplicate oracle coverage milestone")
    for row in checked:
        if registered[row.milestone].oracle_id != row.oracle_id:
            raise ValueError(f"oracle coverage binding mismatch: {row.milestone}")
        if row.status is CoverageStatus.SUPPORTED and not row.producer:
            raise ValueError(f"supported oracle has no producer: {row.milestone}")
        if row.status is not CoverageStatus.SUPPORTED and row.producer is not None:
            raise ValueError(f"unavailable oracle claims a producer: {row.milestone}")
    return checked


def effect_coverage_inventory() -> dict[str, object]:
    """Return the validated, versioned evaluator coverage inventory."""
    rows = validate_effect_coverage()
    oracles = validate_oracle_coverage()
    return {
        "coverage_version": COVERAGE_VERSION,
        "rows": [
            {
                "universe": row.universe,
                "name": row.name,
                "effect_family": row.effect_family,
                "milestone": row.milestone,
                "oracle_id": row.oracle_id,
                "status": row.status.value,
                "producer": row.producer,
                "reason": row.reason,
            }
            for row in rows
        ],
        "oracles": [
            {
                "milestone": row.milestone,
                "oracle_id": row.oracle_id,
                "status": row.status.value,
                "producer": row.producer,
                "reason": row.reason,
            }
            for row in oracles
        ],
    }
