"""Phase 16 fresh structural benchmark portfolio preregistration.

This module is offline-only. It freezes the first prospective portfolio that can
test learned branch selection against strong deterministic controls without
reusing the exposed GOAD, Phase 6, Phase 7, or purpose-range families.

It deliberately does not deploy a range, run a model, bind a live callback, or
claim that the planned mechanics already work live. Phase 17 owns development
canaries, exact callback binding, and final adapter-boundary coverage proof.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import permutations, product
from math import comb
import hashlib
import inspect
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

try:  # package import
    from ..langgraph import capabilities, evaluation_authorization as auth, policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import capabilities  # type: ignore
    import evaluation_authorization as auth  # type: ignore
    import policy  # type: ignore


KIND = "phase16_structural_benchmark_portfolio_preregistration"
SCHEMA_VERSION = 2
SEALED_PORTFOLIO_ID = "phase16-structural-portfolio-v2"
OPERATOR_AUTHORIZATION_ID = "russel-phase16-range-and-validator-scope-2026-07-16"
INDEPENDENT_REVIEWER = "Russel"
SOURCE_PLAN = "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_COMPLETION_PLAN_2026-07-14.md#6.10"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT_PATH = (
    DEFAULT_REPO_ROOT
    / "Plans"
    / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE16_STRUCTURAL_BENCHMARK_PORTFOLIO_2026-07-16.json"
)
DEFAULT_COVERAGE_OUTPUT_PATH = (
    DEFAULT_REPO_ROOT
    / "Plans"
    / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE16_EFFECT_PATH_COVERAGE_MANIFEST_2026-07-16.json"
)
DEFAULT_REVIEW_GUIDE_PATH = (
    DEFAULT_REPO_ROOT
    / "Plans"
    / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE16_PRE_UNSEAL_REVIEW_GUIDE_2026-07-16.md"
)
DEFAULT_FAMILY_MANIFEST_PATHS = {
    "sealed-family-s1": (
        DEFAULT_REPO_ROOT
        / "Plans"
        / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE16_SEALED_FAMILY_S1_MANIFEST_2026-07-16.json"
    ),
    "sealed-family-s2": (
        DEFAULT_REPO_ROOT
        / "Plans"
        / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE16_SEALED_FAMILY_S2_MANIFEST_2026-07-16.json"
    ),
}

PRIMARY_ARMS = (
    "hybrid",
    "shipping_symbolic",
    "modeled_reachability",
    "objective_effect_aware",
    "null_no_decision",
)
STRONGEST_DETERMINISTIC_CONTROL = "modeled_reachability"
CHEAPEST_REVIEWER_FALSIFIER = "objective_effect_aware"
PURE_LLM_DISPOSITION = "excluded_no_preregistered_mechanism_difference_after_phase15"
MODEL_PROVIDER_IDENTITY = {
    "configured_backend": "openai:gpt-5.5-cyber-preview",
    "effective_backend_required": "openai:gpt-5.5-cyber-preview",
    "route_binding": "response_verified_exact_match_required_before_unseal",
    "provider_fallback_policy": "no_unrecorded_fallback_in_countable_cells",
}
POWER_SPEC = {
    "test": "one_sided_exact_paired_sign_test_over_informative_pair_wins",
    "alpha": 0.05,
    "null_win_probability": 0.5,
    "alternative_win_probability": 0.85,
    "smallest_relevant_effect": 0.35,
    "required_informative_pairs_per_family": 13,
    "scheduled_pair_slots_per_family": 13,
}
OPERATIONAL_BUDGETS = {
    "budget_schema": "phase16-operational-budget-v2",
    "max_wall_seconds_per_cell": 1500,
    "max_semantic_transactions_per_cell": 8,
    "max_model_calls_per_cell": 2,
    "max_provider_retries_per_cell": 2,
    "max_active_ranges": 1,
    "max_live_cells_in_parallel": 1,
    "max_powered_vms_per_active_range": 7,
    "max_ram_gb_per_active_range": 28,
    "max_vcpus_per_active_range": 14,
    "budget_counting_scope": "declared_windows_guest_vms_only",
    "co_location_policy": "forbidden_without_reseal",
}
DECLARED_WINDOWS_VM_PROFILE = {
    "template": "win2022-server-x64-template",
    "ram_gb": 4,
    "vcpus": 2,
}
PHYSICAL_REALIZATION_CONVENTION = {
    "convention_id": "sage-purpose-range-one-logical-domain-or-host-per-declared-windows-guest-v1",
    "mapping_policy": "one_declared_windows_guest_per_logical_domain_or_host_node",
    "budget_counting_scope": OPERATIONAL_BUDGETS["budget_counting_scope"],
    "co_location_policy": OPERATIONAL_BUDGETS["co_location_policy"],
    "declared_windows_vm_profile": DECLARED_WINDOWS_VM_PROFILE,
    "source_evidence": [
        {
            "path": "ludus/sage-purpose-ranges/blueprints/sage-replication-range/range-config.yml",
            "observation": "Each declared DC, member server, and foothold workstation is a separate 4 GB / 2 CPU Windows guest.",
        },
        {
            "path": "../DreadGOAD/ad/SAGE-TRUST-CONTEXT/providers/ludus/config.yml",
            "observation": "Each declared DC and foothold workstation is a separate 4 GB / 2 CPU Windows guest.",
        },
        {
            "path": "../DreadGOAD/ad/SAGE-POLICY-RANGE/providers/ludus/config.yml",
            "observation": "Each declared DC, CA/member server, and foothold workstation is a separate 4 GB / 2 CPU Windows guest.",
        },
    ],
}
FORBIDDEN_FAMILY_FIELD_NAMES = frozenset(
    {
        "answer",
        "designated_answer",
        "correct",
        "preferred",
        "best",
        "outcome",
        "regret",
        "result_derived_cost",
        "result-derived-cost",
    }
)
POLICY_AUTH_FIELD_NAMES = frozenset(
    {
        "authorization_manifest_id",
        "authorization_decision_id",
        "authorization_reason_code",
        "cell_authorization_id",
        "enforcement_projection_sha256",
        "allowed_targets",
        "operator_authorization_id",
    }
)


@dataclass(frozen=True)
class TopologyNode:
    node_id: str
    kind: str


@dataclass(frozen=True)
class TopologyEdge:
    source: str
    relation: str
    target: str


@dataclass(frozen=True)
class PhysicalVm:
    vm_id: str
    hostname: str
    vm_role: str
    logical_node_id: str
    ad_domain_fqdn: str
    ip_last_octet: int
    template: str = str(DECLARED_WINDOWS_VM_PROFILE["template"])
    ram_gb: int = int(DECLARED_WINDOWS_VM_PROFILE["ram_gb"])
    vcpus: int = int(DECLARED_WINDOWS_VM_PROFILE["vcpus"])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BranchContract:
    branch_id: str
    capability_path: tuple[str, ...]
    effect_classes: tuple[str, ...]
    proof_modes: tuple[str, ...]
    sample_target_fields: Mapping[str, str]
    sample_effects: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "capability_path": list(self.capability_path),
            "effect_classes": list(self.effect_classes),
            "proof_modes": list(self.proof_modes),
            "sample_target_fields": dict(self.sample_target_fields),
            "sample_effects": list(self.sample_effects),
        }


@dataclass(frozen=True)
class PacketFixture:
    packet_id: str
    objective: str
    objective_anchors: tuple[str, ...]
    graph_facts: tuple[str, ...]
    admissible_frontier: tuple[Mapping[str, Any], ...]
    minimum_relation_hops: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "objective": self.objective,
            "objective_anchors": list(self.objective_anchors),
            "normalized_state": {"graph_facts": list(self.graph_facts)},
            "admissible_frontier": [dict(item) for item in self.admissible_frontier],
            "candidate_order_variants": [
                [int(index) for index in order]
                for order in permutations(range(len(self.admissible_frontier)))
            ],
            "minimum_relation_hops": self.minimum_relation_hops,
        }


@dataclass(frozen=True)
class FamilySpec:
    family_id: str
    partition: str
    topology_family: str
    engagement_id: str
    range_id: str
    baseline_snapshot_id: str
    live_reset_snapshot_id: str
    source_pattern: str
    selector_host: str
    selector_domain: str
    selector_identity: str
    target_realms: tuple[str, ...]
    allowed_targets: Mapping[str, tuple[str, ...]]
    branches: tuple[BranchContract, ...]
    packet_fixtures: tuple[PacketFixture, ...]
    nodes: tuple[TopologyNode, ...]
    edges: tuple[TopologyEdge, ...]
    physical_vms: tuple[PhysicalVm, ...]
    exercised_target_dimensions: tuple[str, ...]
    explicit_denied_capabilities: tuple[str, ...]
    explicit_denied_effects: tuple[str, ...]
    phase17_mechanics_obligations: tuple[str, ...]

    def to_public_manifest_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "phase16_sealed_structural_family_manifest",
            "portfolio_id": SEALED_PORTFOLIO_ID,
            "family_id": self.family_id,
            "partition": self.partition,
            "topology_family": self.topology_family,
            "engagement_id": self.engagement_id,
            "range_plan": {
                "range_id": self.range_id,
                "baseline_snapshot_id": self.baseline_snapshot_id,
                "live_reset_snapshot_id": self.live_reset_snapshot_id,
                "source_pattern": self.source_pattern,
                "ludus_connection": "ludus_sagerepl",
                "deployment_status": "not_deployed_phase16_design_only",
                "physical_realization": _physical_realization_payload(self),
            },
            "topology": {
                "nodes": [asdict(item) for item in self.nodes],
                "edges": [asdict(item) for item in self.edges],
                "canonical_topology_hash": canonical_topology_hash(self.nodes, self.edges),
            },
            "branches": [branch.to_dict() for branch in self.branches],
            "packet_fixtures": [fixture.to_dict() for fixture in self.packet_fixtures],
            "phase17_mechanics_obligations": list(self.phase17_mechanics_obligations),
            "live_policy_matrix_authorized": False,
            "phase17_live_callback_binding_required": True,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(DEFAULT_REPO_ROOT.resolve()).as_posix()
    except Exception:
        return path.name


def _write_json_with_sidecar(path: Path, payload: Mapping[str, Any]) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    digest = _file_sha256(path)
    sidecar = path.with_suffix(".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return {
        "path": _portable_path(path),
        "sha256": digest,
        "sidecar_path": _portable_path(sidecar),
    }


def _topology_nodes(*pairs: tuple[str, str]) -> tuple[TopologyNode, ...]:
    return tuple(TopologyNode(node_id=node_id, kind=kind) for node_id, kind in pairs)


def _topology_edges(*triples: tuple[str, str, str]) -> tuple[TopologyEdge, ...]:
    return tuple(TopologyEdge(source=source, relation=relation, target=target) for source, relation, target in triples)


def _physical_vms(*rows: tuple[str, str, str, str, str, int]) -> tuple[PhysicalVm, ...]:
    return tuple(
        PhysicalVm(
            vm_id=vm_id,
            hostname=hostname,
            vm_role=vm_role,
            logical_node_id=logical_node_id,
            ad_domain_fqdn=ad_domain_fqdn,
            ip_last_octet=ip_last_octet,
        )
        for vm_id, hostname, vm_role, logical_node_id, ad_domain_fqdn, ip_last_octet in rows
    )


def canonical_topology_signature(nodes: Iterable[TopologyNode], edges: Iterable[TopologyEdge]) -> str:
    """Return a rename-invariant labeled graph signature for small benchmark topologies."""
    nodes = tuple(nodes)
    edges = tuple(edges)
    by_kind: dict[str, list[str]] = {}
    for node in nodes:
        by_kind.setdefault(node.kind, []).append(node.node_id)
    kind_order = sorted(by_kind)
    grouped_permutations = [
        list(permutations(sorted(by_kind[kind])))
        for kind in kind_order
    ]
    serializations: list[str] = []
    for ordering in product(*grouped_permutations):
        ordered_ids: list[str] = []
        ordered_kinds: list[str] = []
        for kind, ids in zip(kind_order, ordering):
            ordered_ids.extend(ids)
            ordered_kinds.extend([kind] * len(ids))
        index = {node_id: position for position, node_id in enumerate(ordered_ids)}
        edge_rows = sorted(
            (index[edge.source], edge.relation, index[edge.target])
            for edge in edges
        )
        serializations.append(_canonical_json({"node_kinds": ordered_kinds, "edges": edge_rows}))
    return min(serializations) if serializations else _canonical_json({"node_kinds": [], "edges": []})


def canonical_topology_hash(nodes: Iterable[TopologyNode], edges: Iterable[TopologyEdge]) -> str:
    return _content_hash(canonical_topology_signature(nodes, edges))


def _candidate_payload(name: str, target: str, effects: tuple[str, ...], reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "target": target,
        "preconditions": [],
        "effects": list(effects),
        "operational_cost": {
            "interaction_class": "direct",
            "execution_scope": "direct",
            "requires_propagation_wait": False,
            "expected_wait_seconds": 0,
            "wait_reasons": [],
        },
        "reason": reason,
    }


SEALED_FAMILIES = (
    FamilySpec(
        family_id="sealed-family-s1",
        partition="sealed_confirmatory",
        topology_family="four_realm_sibling_plus_partner",
        engagement_id="phase18-s1-marble-v1",
        range_id="SAGESTRUCTA",
        baseline_snapshot_id="sage-structural-a-base-v1",
        live_reset_snapshot_id="sage-structural-a-apollo-staged-v1",
        source_pattern="ludus/sage-purpose-ranges/blueprints/sage-structural-a",
        selector_host="marble-ws01",
        selector_domain="marble.local",
        selector_identity=r"marble\analyst1",
        target_realms=("larch.marble.local", "onyx.partner.local"),
        allowed_targets={
            "hosts": ("marble-ws01", "n01", "n02"),
            "domains": ("marble.local", "larch.marble.local", "onyx.partner.local"),
            "principals": (r"marble\analyst1", "administrator@onyx.partner.local"),
            "directory_objects": ("policy-17", "template-17"),
            "trust_edges": ("marble.local->larch.marble.local", "marble.local->onyx.partner.local"),
        },
        branches=(
            BranchContract(
                branch_id="branch-0",
                capability_path=(
                    "gpo-controlled-system-exec",
                    "grant-directory-rights",
                    "dcsync-krbtgt",
                    "forge-golden-ticket",
                ),
                effect_classes=(
                    "gpo_system_execution",
                    "directory_replication_rights",
                    "krbtgt_secret_material",
                    "domain_admin_context",
                ),
                proof_modes=(
                    "mythic_task_output",
                    "mythic_credential_store",
                    "payload_collected_bloodhound_fact",
                ),
                sample_target_fields={
                    "domains": "larch.marble.local",
                    "directory_objects": "policy-17",
                },
                sample_effects=(
                    "system-exec:gpo:policy-17@larch.marble.local",
                    "ds-replication-rights:larch.marble.local",
                    "krbtgt-hash:larch.marble.local",
                    "da:larch.marble.local",
                ),
            ),
            BranchContract(
                branch_id="branch-1",
                capability_path=(
                    "adcs-esc-certificate-enroll",
                    "adcs-certificate-auth",
                ),
                effect_classes=(
                    "adcs_enrollment",
                    "certificate_authentication",
                    "domain_admin_context",
                ),
                proof_modes=(
                    "mythic_task_output",
                    "mythic_credential_store",
                ),
                sample_target_fields={
                    "domains": "onyx.partner.local",
                    "hosts": "n02",
                    "principals": "administrator@onyx.partner.local",
                },
                sample_effects=(
                    "adcs-enrolled-certificate:administrator@onyx.partner.local",
                    "certificate-auth:administrator@onyx.partner.local",
                    "da:onyx.partner.local",
                ),
            ),
        ),
        packet_fixtures=(
            PacketFixture(
                packet_id="s1-packet-0",
                objective=(
                    "From the current foothold, obtain administrative control of the realm that contains "
                    "the host serving role token queue-17."
                ),
                objective_anchors=("queue-17",),
                graph_facts=(
                    "role-token:queue-17:svc-17",
                    "service-host:svc-17:n01",
                    "host-realm:n01:larch.marble.local",
                    "role-token:queue-41:svc-41",
                    "service-host:svc-41:n02",
                    "host-realm:n02:onyx.partner.local",
                ),
                admissible_frontier=(
                    _candidate_payload(
                        "gpo-controlled-system-exec",
                        "gpo=policy-17;domain=larch.marble.local",
                        ("system-exec:gpo:policy-17@larch.marble.local",),
                        "controlled policy object is admissible",
                    ),
                    _candidate_payload(
                        "adcs-esc-certificate-enroll",
                        "domain=onyx.partner.local;account=administrator;ca_host=n02;callback=fixture-callback",
                        ("adcs-enrolled-certificate:administrator@onyx.partner.local",),
                        "certificate enrollment path is admissible",
                    ),
                ),
                minimum_relation_hops=3,
            ),
            PacketFixture(
                packet_id="s1-packet-1",
                objective=(
                    "From the current foothold, obtain administrative control of the realm that contains "
                    "the host serving role token queue-41."
                ),
                objective_anchors=("queue-41",),
                graph_facts=(
                    "role-token:queue-17:svc-17",
                    "service-host:svc-17:n01",
                    "host-realm:n01:larch.marble.local",
                    "role-token:queue-41:svc-41",
                    "service-host:svc-41:n02",
                    "host-realm:n02:onyx.partner.local",
                ),
                admissible_frontier=(
                    _candidate_payload(
                        "gpo-controlled-system-exec",
                        "gpo=policy-17;domain=larch.marble.local",
                        ("system-exec:gpo:policy-17@larch.marble.local",),
                        "controlled policy object is admissible",
                    ),
                    _candidate_payload(
                        "adcs-esc-certificate-enroll",
                        "domain=onyx.partner.local;account=administrator;ca_host=n02;callback=fixture-callback",
                        ("adcs-enrolled-certificate:administrator@onyx.partner.local",),
                        "certificate enrollment path is admissible",
                    ),
                ),
                minimum_relation_hops=3,
            ),
        ),
        nodes=_topology_nodes(
            ("root", "domain"),
            ("child-a", "domain"),
            ("child-b", "domain"),
            ("partner", "domain"),
            ("foothold", "host"),
            ("branch-a", "host"),
            ("branch-b", "host"),
            ("policy", "directory_object"),
            ("template", "directory_object"),
        ),
        edges=_topology_edges(
            ("root", "parent_child", "child-a"),
            ("root", "parent_child", "child-b"),
            ("root", "external_trust", "partner"),
            ("root", "contains", "foothold"),
            ("child-a", "contains", "branch-a"),
            ("partner", "contains", "branch-b"),
            ("child-a", "controls", "policy"),
            ("partner", "controls", "template"),
        ),
        physical_vms=_physical_vms(
            ("SAGESTRUCTA-DC01", "marble-dc01", "domain_controller", "root", "marble.local", 10),
            ("SAGESTRUCTA-DC02", "larch-dc01", "domain_controller", "child-a", "larch.marble.local", 11),
            ("SAGESTRUCTA-DC03", "ivory-dc01", "domain_controller", "child-b", "ivory.marble.local", 12),
            ("SAGESTRUCTA-DC04", "onyx-dc01", "domain_controller", "partner", "onyx.partner.local", 13),
            ("SAGESTRUCTA-WS01", "marble-ws01", "foothold_workstation", "foothold", "marble.local", 31),
            ("SAGESTRUCTA-SRV01", "n01", "branch_member_server", "branch-a", "larch.marble.local", 21),
            ("SAGESTRUCTA-SRV02", "n02", "branch_ca_server", "branch-b", "onyx.partner.local", 22),
        ),
        exercised_target_dimensions=("hosts", "domains", "principals", "directory_objects", "trust_edges"),
        explicit_denied_capabilities=(
            "endpoint-protection-adjustment",
            "read-managed-local-admin-secret",
            "use-managed-local-admin-secret",
        ),
        explicit_denied_effects=(
            "endpoint-protection-adjusted:n01@larch.marble.local",
            "managed-local-admin-secret:n01@larch.marble.local",
        ),
        phase17_mechanics_obligations=(
            "Prove the GPO, replication-right, DCSync, and ticket path through exact Mythic-derived proof.",
            "Prove the ADCS enrollment and certificate-auth path without off-agent target I/O.",
            "Prove both branch packets survive candidate-order permutation and exact callback binding.",
        ),
    ),
    FamilySpec(
        family_id="sealed-family-s2",
        partition="sealed_confirmatory",
        topology_family="root_with_peer_and_nested_partner",
        engagement_id="phase18-s2-quartz-v1",
        range_id="SAGESTRUCTB",
        baseline_snapshot_id="sage-structural-b-base-v1",
        live_reset_snapshot_id="sage-structural-b-apollo-staged-v1",
        source_pattern="ludus/sage-purpose-ranges/blueprints/sage-structural-b",
        selector_host="quartz-ws01",
        selector_domain="quartz.local",
        selector_identity=r"quartz\analyst1",
        target_realms=("cedar.partner.local", "tide.harbor.local"),
        allowed_targets={
            "hosts": ("quartz-ws01", "n11", "n12"),
            "domains": ("quartz.local", "cedar.partner.local", "harbor.local", "tide.harbor.local"),
            "principals": (
                r"quartz\analyst1",
                "administrator@cedar.partner.local",
                "krbtgt@tide.harbor.local",
            ),
            "directory_objects": ("node-22", "node-44"),
            "trust_edges": (
                "quartz.local->cedar.partner.local",
                "quartz.local->harbor.local",
                "harbor.local->tide.harbor.local",
            ),
        },
        branches=(
            BranchContract(
                branch_id="branch-0",
                capability_path=(
                    "read-managed-local-admin-secret",
                    "use-managed-local-admin-secret",
                    "execute-as-local-admin",
                    "adcs-ca-private-key-export",
                    "adcs-certificate-auth",
                ),
                effect_classes=(
                    "managed_secret_read",
                    "managed_secret_use",
                    "remote_execution",
                    "adcs_ca_key_export",
                    "certificate_authentication",
                    "domain_admin_context",
                ),
                proof_modes=(
                    "mythic_task_output",
                    "mythic_credential_store",
                ),
                sample_target_fields={
                    "domains": "cedar.partner.local",
                    "hosts": "n11",
                    "principals": "administrator@cedar.partner.local",
                },
                sample_effects=(
                    "managed-local-admin-secret:n11@cedar.partner.local",
                    "local-admin:n11@cedar.partner.local",
                    "remote-exec:n11@cedar.partner.local",
                    "adcs-ca-private-key:n11@cedar.partner.local",
                    "certificate-auth:administrator@cedar.partner.local",
                    "da:cedar.partner.local",
                ),
            ),
            BranchContract(
                branch_id="branch-1",
                capability_path=(
                    "dcsync-krbtgt",
                    "forge-golden-ticket",
                ),
                effect_classes=(
                    "krbtgt_secret_material",
                    "domain_admin_context",
                ),
                proof_modes=(
                    "mythic_task_output",
                    "mythic_credential_store",
                ),
                sample_target_fields={
                    "domains": "tide.harbor.local",
                    "principals": "krbtgt@tide.harbor.local",
                },
                sample_effects=(
                    "krbtgt-hash:tide.harbor.local",
                    "da:tide.harbor.local",
                ),
            ),
        ),
        packet_fixtures=(
            PacketFixture(
                packet_id="s2-packet-0",
                objective=(
                    "From the current foothold, obtain administrative control of the realm that owns "
                    "the directory object reached from relation token beacon-22."
                ),
                objective_anchors=("beacon-22",),
                graph_facts=(
                    "relation-token:beacon-22:obj-22",
                    "object-container:obj-22:node-22",
                    "container-realm:node-22:cedar.partner.local",
                    "relation-token:beacon-44:obj-44",
                    "object-container:obj-44:node-44",
                    "container-realm:node-44:tide.harbor.local",
                ),
                admissible_frontier=(
                    _candidate_payload(
                        "read-managed-local-admin-secret",
                        (
                            "account=analyst1;account_domain=quartz.local;"
                            "target=n11;target_domain=cedar.partner.local;callback=fixture-callback"
                        ),
                        ("managed-local-admin-secret:n11@cedar.partner.local",),
                        "managed secret path is admissible",
                    ),
                    _candidate_payload(
                        "dcsync-krbtgt",
                        "domain=tide.harbor.local;account=krbtgt",
                        ("krbtgt-hash:tide.harbor.local",),
                        "replication path is admissible",
                    ),
                ),
                minimum_relation_hops=3,
            ),
            PacketFixture(
                packet_id="s2-packet-1",
                objective=(
                    "From the current foothold, obtain administrative control of the realm that owns "
                    "the directory object reached from relation token beacon-44."
                ),
                objective_anchors=("beacon-44",),
                graph_facts=(
                    "relation-token:beacon-22:obj-22",
                    "object-container:obj-22:node-22",
                    "container-realm:node-22:cedar.partner.local",
                    "relation-token:beacon-44:obj-44",
                    "object-container:obj-44:node-44",
                    "container-realm:node-44:tide.harbor.local",
                ),
                admissible_frontier=(
                    _candidate_payload(
                        "read-managed-local-admin-secret",
                        (
                            "account=analyst1;account_domain=quartz.local;"
                            "target=n11;target_domain=cedar.partner.local;callback=fixture-callback"
                        ),
                        ("managed-local-admin-secret:n11@cedar.partner.local",),
                        "managed secret path is admissible",
                    ),
                    _candidate_payload(
                        "dcsync-krbtgt",
                        "domain=tide.harbor.local;account=krbtgt",
                        ("krbtgt-hash:tide.harbor.local",),
                        "replication path is admissible",
                    ),
                ),
                minimum_relation_hops=3,
            ),
        ),
        nodes=_topology_nodes(
            ("root", "domain"),
            ("peer-a", "domain"),
            ("peer-b-root", "domain"),
            ("peer-b-child", "domain"),
            ("foothold", "host"),
            ("branch-a", "host"),
            ("branch-b", "host"),
            ("object-a", "directory_object"),
            ("object-b", "directory_object"),
        ),
        edges=_topology_edges(
            ("root", "external_trust", "peer-a"),
            ("root", "external_trust", "peer-b-root"),
            ("peer-b-root", "parent_child", "peer-b-child"),
            ("root", "contains", "foothold"),
            ("peer-a", "contains", "branch-a"),
            ("peer-b-child", "contains", "branch-b"),
            ("branch-a", "relates", "object-a"),
            ("branch-b", "relates", "object-b"),
        ),
        physical_vms=_physical_vms(
            ("SAGESTRUCTB-DC01", "quartz-dc01", "domain_controller", "root", "quartz.local", 10),
            ("SAGESTRUCTB-DC02", "cedar-dc01", "domain_controller", "peer-a", "cedar.partner.local", 11),
            ("SAGESTRUCTB-DC03", "harbor-dc01", "domain_controller", "peer-b-root", "harbor.local", 12),
            ("SAGESTRUCTB-DC04", "tide-dc01", "domain_controller", "peer-b-child", "tide.harbor.local", 13),
            ("SAGESTRUCTB-WS01", "quartz-ws01", "foothold_workstation", "foothold", "quartz.local", 31),
            ("SAGESTRUCTB-SRV01", "n11", "branch_ca_server", "branch-a", "cedar.partner.local", 21),
            ("SAGESTRUCTB-SRV02", "n12", "branch_member_server", "branch-b", "tide.harbor.local", 22),
        ),
        exercised_target_dimensions=("hosts", "domains", "principals", "directory_objects", "trust_edges"),
        explicit_denied_capabilities=(
            "endpoint-protection-adjustment",
            "adcs-esc-certificate-enroll",
            "grant-directory-rights",
        ),
        explicit_denied_effects=(
            "endpoint-protection-adjusted:n11@cedar.partner.local",
            "ds-replication-rights:cedar.partner.local",
        ),
        phase17_mechanics_obligations=(
            "Prove the managed-secret, local-admin, remote-exec, CA-export, and certificate-auth path.",
            "Prove the direct DCSync and ticket path through exact Mythic-derived proof.",
            "Prove both branch packets survive candidate-order permutation and exact callback binding.",
        ),
    ),
)


EXPOSED_FAMILY_TOPOLOGIES = {
    "goad-trust-walker": (
        _topology_nodes(
            ("root", "domain"),
            ("child", "domain"),
            ("forest", "domain"),
            ("foothold", "host"),
            ("mid", "host"),
            ("target", "host"),
        ),
        _topology_edges(
            ("root", "parent_child", "child"),
            ("root", "external_trust", "forest"),
            ("child", "contains", "foothold"),
            ("root", "contains", "mid"),
            ("forest", "contains", "target"),
        ),
    ),
    "phase6-laps-family-transfer-r5": (
        _topology_nodes(
            ("root", "domain"),
            ("child-a", "domain"),
            ("child-b", "domain"),
            ("foothold", "host"),
            ("target-a", "host"),
            ("target-b", "host"),
        ),
        _topology_edges(
            ("root", "parent_child", "child-a"),
            ("root", "parent_child", "child-b"),
            ("root", "contains", "foothold"),
            ("child-a", "contains", "target-a"),
            ("child-b", "contains", "target-b"),
        ),
    ),
    "phase7-trust-context-corroboration-v2": (
        _topology_nodes(
            ("root", "domain"),
            ("child", "domain"),
            ("trusted", "domain"),
            ("foothold", "host"),
            ("branch-a", "host"),
            ("branch-b", "host"),
        ),
        _topology_edges(
            ("root", "parent_child", "child"),
            ("root", "external_trust", "trusted"),
            ("child", "contains", "foothold"),
            ("child", "contains", "branch-a"),
            ("trusted", "contains", "branch-b"),
        ),
    ),
    "purpose-range": (
        _topology_nodes(
            ("domain", "domain"),
            ("foothold", "host"),
            ("gpo", "host"),
            ("ca", "host"),
        ),
        _topology_edges(
            ("domain", "contains", "foothold"),
            ("domain", "contains", "gpo"),
            ("domain", "contains", "ca"),
        ),
    ),
    "replication-purpose-range": (
        _topology_nodes(
            ("domain", "domain"),
            ("foothold", "host"),
            ("gpo", "host"),
        ),
        _topology_edges(
            ("domain", "contains", "foothold"),
            ("domain", "contains", "gpo"),
        ),
    ),
    "same-domain-gpo-dc-scope-late-blocker": (
        _topology_nodes(
            ("domain", "domain"),
            ("foothold", "host"),
            ("gpo-a", "host"),
            ("gpo-b", "host"),
            ("policy-a", "directory_object"),
            ("policy-b", "directory_object"),
        ),
        _topology_edges(
            ("domain", "contains", "foothold"),
            ("domain", "contains", "gpo-a"),
            ("domain", "contains", "gpo-b"),
            ("gpo-a", "controlled_by", "policy-a"),
            ("gpo-b", "controlled_by", "policy-b"),
        ),
    ),
}


def _physical_realization_payload(family: FamilySpec) -> dict[str, Any]:
    vm_rows = [vm.to_dict() for vm in family.physical_vms]
    non_vm_nodes = [
        {
            "node_id": node.node_id,
            "kind": node.kind,
            "realization": "directory_object_created_inside_ad_not_a_powered_vm",
        }
        for node in family.nodes
        if node.kind not in {"domain", "host"}
    ]
    return {
        "realization_id": f"{family.family_id}-physical-realization-v1",
        "mapping_policy": PHYSICAL_REALIZATION_CONVENTION["mapping_policy"],
        "budget_counting_scope": PHYSICAL_REALIZATION_CONVENTION["budget_counting_scope"],
        "co_location_policy": PHYSICAL_REALIZATION_CONVENTION["co_location_policy"],
        "declared_windows_vm_profile": dict(DECLARED_WINDOWS_VM_PROFILE),
        "declared_windows_guest_vms": vm_rows,
        "node_to_vm": {
            vm.logical_node_id: vm.vm_id
            for vm in family.physical_vms
        },
        "non_vm_logical_nodes": non_vm_nodes,
        "resource_totals": {
            "powered_vm_count": len(vm_rows),
            "ram_gb": sum(int(vm["ram_gb"]) for vm in vm_rows),
            "vcpus": sum(int(vm["vcpus"]) for vm in vm_rows),
        },
    }


def _physical_realization_audit(family: FamilySpec) -> dict[str, Any]:
    payload = _physical_realization_payload(family)
    vm_rows = tuple(family.physical_vms)
    required_vm_nodes = {
        node.node_id
        for node in family.nodes
        if node.kind in {"domain", "host"}
    }
    domain_nodes = {
        node.node_id
        for node in family.nodes
        if node.kind == "domain"
    }
    host_nodes = {
        node.node_id
        for node in family.nodes
        if node.kind == "host"
    }
    directory_object_nodes = {
        node.node_id
        for node in family.nodes
        if node.kind == "directory_object"
    }
    mapped_nodes = [vm.logical_node_id for vm in vm_rows]
    mapping_counts = Counter(mapped_nodes)
    non_vm_nodes = {
        str(node.get("node_id") or "")
        for node in payload["non_vm_logical_nodes"]
    }
    resource_totals = dict(payload["resource_totals"])
    expected_totals = {
        "powered_vm_count": int(OPERATIONAL_BUDGETS["max_powered_vms_per_active_range"]),
        "ram_gb": int(OPERATIONAL_BUDGETS["max_ram_gb_per_active_range"]),
        "vcpus": int(OPERATIONAL_BUDGETS["max_vcpus_per_active_range"]),
    }
    host_roles = {"foothold_workstation", "branch_member_server", "branch_ca_server"}
    checks = {
        "all_domain_and_host_nodes_are_mapped": set(mapped_nodes) == required_vm_nodes,
        "all_domain_and_host_nodes_are_mapped_exactly_once": (
            set(mapped_nodes) == required_vm_nodes
            and all(mapping_counts[node_id] == 1 for node_id in required_vm_nodes)
        ),
        "directory_objects_are_explicit_non_vm_nodes": non_vm_nodes == directory_object_nodes,
        "vm_ids_are_unique": len({vm.vm_id for vm in vm_rows}) == len(vm_rows),
        "hostnames_are_unique": len({vm.hostname for vm in vm_rows}) == len(vm_rows),
        "ip_last_octets_are_unique": len({vm.ip_last_octet for vm in vm_rows}) == len(vm_rows),
        "domain_nodes_use_dedicated_domain_controller_vms": all(
            vm.logical_node_id not in domain_nodes or vm.vm_role == "domain_controller"
            for vm in vm_rows
        ),
        "host_nodes_use_dedicated_non_dc_vms": all(
            vm.logical_node_id not in host_nodes or vm.vm_role in host_roles
            for vm in vm_rows
        ),
        "foothold_is_a_dedicated_workstation": any(
            vm.logical_node_id == "foothold" and vm.vm_role == "foothold_workstation"
            for vm in vm_rows
        ),
        "every_vm_uses_frozen_guest_profile": all(
            vm.template == DECLARED_WINDOWS_VM_PROFILE["template"]
            and vm.ram_gb == DECLARED_WINDOWS_VM_PROFILE["ram_gb"]
            and vm.vcpus == DECLARED_WINDOWS_VM_PROFILE["vcpus"]
            for vm in vm_rows
        ),
        "one_logical_node_per_declared_vm_no_colocation": len(mapped_nodes) == len(vm_rows),
        "resource_totals_fit_frozen_budget": (
            int(resource_totals["powered_vm_count"]) <= int(OPERATIONAL_BUDGETS["max_powered_vms_per_active_range"])
            and int(resource_totals["ram_gb"]) <= int(OPERATIONAL_BUDGETS["max_ram_gb_per_active_range"])
            and int(resource_totals["vcpus"]) <= int(OPERATIONAL_BUDGETS["max_vcpus_per_active_range"])
        ),
        "resource_totals_match_frozen_active_range_envelope": resource_totals == expected_totals,
        "budget_counting_scope_is_explicit": (
            payload["budget_counting_scope"] == OPERATIONAL_BUDGETS["budget_counting_scope"]
        ),
        "co_location_is_forbidden_without_reseal": (
            payload["co_location_policy"] == OPERATIONAL_BUDGETS["co_location_policy"]
        ),
    }
    return {
        "family_id": family.family_id,
        "realization_id": payload["realization_id"],
        "required_vm_logical_nodes": sorted(required_vm_nodes),
        "mapped_vm_logical_nodes": sorted(mapped_nodes),
        "non_vm_logical_nodes": sorted(non_vm_nodes),
        "resource_totals": resource_totals,
        "frozen_active_range_envelope": expected_totals,
        "checks": checks,
        "passes": all(checks.values()),
    }


def _catalog_names() -> set[str]:
    return {str(item.get("name") or "") for item in capabilities.capability_catalog()}


def _branch_capabilities(family: FamilySpec) -> set[str]:
    return {
        capability
        for branch in family.branches
        for capability in branch.capability_path
    }


def _branch_effects(family: FamilySpec) -> set[str]:
    return {
        effect
        for branch in family.branches
        for effect in branch.sample_effects
    }


def _allowed_cells(family: FamilySpec) -> tuple[str, ...]:
    return tuple(
        f"{family.family_id}:pair-{pair_index:02d}:{arm}"
        for pair_index in range(1, int(POWER_SPEC["scheduled_pair_slots_per_family"]) + 1)
        for arm in PRIMARY_ARMS
    )


def authorization_manifest_for_family(family: FamilySpec) -> auth.EvaluationAuthorizationManifest:
    return auth.EvaluationAuthorizationManifest(
        manifest_id=f"{family.family_id}-authorization-v2",
        version="2",
        operator_authorization_id=OPERATOR_AUTHORIZATION_ID,
        engagement_id=family.engagement_id,
        range_id=family.range_id,
        snapshot_id=family.live_reset_snapshot_id,
        valid_from="2026-07-16T00:00:00+00:00",
        valid_until="2026-09-01T00:00:00+00:00",
        allowed_cells=_allowed_cells(family),
        callbacks=(
            auth.CallbackSelector(
                callback_id="",
                host=family.selector_host,
                domain=family.selector_domain,
                identity=family.selector_identity,
            ),
        ),
        target_realms=family.target_realms,
        allowed_targets=family.allowed_targets,
        allowed_capabilities=tuple(sorted({"collect-graph", *_branch_capabilities(family)})),
        denied_capabilities=family.explicit_denied_capabilities,
        allowed_effects=tuple(sorted({"graph-collected", *_branch_effects(family)})),
        denied_effects=family.explicit_denied_effects,
    )


def _family_manifest_payload(family: FamilySpec) -> dict[str, Any]:
    manifest = authorization_manifest_for_family(family)
    payload = family.to_public_manifest_dict()
    payload["authorization_manifest"] = manifest.to_dict()
    payload["authorization_manifest_sha256"] = manifest.sha256
    payload["authorization_gate_version"] = auth.GATE_VERSION
    payload["manifest_content_hash"] = _content_hash(payload)
    return payload


def _effect_class_for_capability(capability: str) -> str:
    return {
        "collect-graph": "graph_collection_and_ingest",
        "gpo-controlled-system-exec": "gpo_system_execution",
        "grant-directory-rights": "directory_replication_rights",
        "dcsync-krbtgt": "krbtgt_secret_material",
        "forge-golden-ticket": "domain_admin_context",
        "read-managed-local-admin-secret": "managed_secret_read",
        "use-managed-local-admin-secret": "managed_secret_use",
        "execute-as-local-admin": "remote_execution",
        "adcs-ca-private-key-export": "adcs_ca_key_export",
        "adcs-esc-certificate-enroll": "adcs_enrollment",
        "adcs-certificate-auth": "certificate_authentication",
    }.get(capability, "unknown")


def build_coverage_manifest() -> dict[str, Any]:
    capabilities_in_scope = sorted(
        {"collect-graph", *{capability for family in SEALED_FAMILIES for capability in _branch_capabilities(family)}}
    )
    paths = []
    for capability_name in capabilities_in_scope:
        is_collection = capability_name == "collect-graph"
        paths.append(
            {
                "path_id": f"phase16:{capability_name}",
                "capability": capability_name,
                "effect_class": _effect_class_for_capability(capability_name),
                "final_adapter_boundary": (
                    "MythicTools.ingest_collection -> _runtime_bloodhound_proof_envelope"
                    if is_collection
                    else "MythicTools.execute_capability -> _issue_capability_callback_command"
                ),
                "proof_origin": "bloodhound_ingest" if is_collection else "mythic_task",
                "authorization_authority": "evaluation_authorization.authorize_action",
                "gate_required_at_final_adapter": True,
                "phase17_final_boundary_attestation_required": True,
                "activation_authorized": False,
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "phase16_evaluated_effect_path_coverage_manifest",
        "portfolio_id": SEALED_PORTFOLIO_ID,
        "coverage_status": "frozen_pending_phase17_final_boundary_attestation",
        "source_inventory": {
            "path": "Plans/SAGE_ARCHITECTURE_POLICY_EVAL_PHASE12_EVALUATED_EFFECT_PATH_INVENTORY_2026-07-16.json",
            "status": "candidate_only_input_not_sufficient_by_itself",
        },
        "paths": paths,
        "phase17_obligation": (
            "Exercise every frozen path at its final adapter boundary and prove one exact authorization "
            "authority runs after normalization and before the covered mutation."
        ),
        "phase18_unseal_authorized": False,
    }
    payload["coverage_manifest_hash"] = _content_hash(payload)
    return payload


def _binomial_tail(n: int, threshold: int, probability: float) -> float:
    return sum(
        comb(n, wins) * (probability ** wins) * ((1.0 - probability) ** (n - wins))
        for wins in range(threshold, n + 1)
    )


def build_power_report() -> dict[str, Any]:
    n = int(POWER_SPEC["required_informative_pairs_per_family"])
    alpha = float(POWER_SPEC["alpha"])
    null_p = float(POWER_SPEC["null_win_probability"])
    alt_p = float(POWER_SPEC["alternative_win_probability"])
    critical_wins = next(
        wins
        for wins in range(n + 1)
        if _binomial_tail(n, wins, null_p) <= alpha
    )
    achieved_power = _binomial_tail(n, critical_wins, alt_p)
    total_cells = n * len(PRIMARY_ARMS) * len(SEALED_FAMILIES)
    max_wall_hours = total_cells * int(OPERATIONAL_BUDGETS["max_wall_seconds_per_cell"]) / 3600.0
    return {
        "analysis_unit": "informative_paired_instance_within_family",
        **POWER_SPEC,
        "critical_wins_for_rejection": critical_wins,
        "null_tail_probability_at_critical_value": _binomial_tail(n, critical_wins, null_p),
        "achieved_power": achieved_power,
        "required_power": 0.8,
        "total_phase18_cells_primary_schedule": total_cells,
        "max_primary_schedule_wall_hours_at_cell_ceiling": max_wall_hours,
        "feasibility_rule": (
            "If a family yields fewer than 13 informative countable pairs after frozen burn rules, that "
            "family is underpowered and Phase 18 is inconclusive rather than adaptively extended."
        ),
        "passes_power_gate": achieved_power >= 0.8 and max_wall_hours <= 60.0,
    }


def _hash_seed(*parts: str) -> str:
    return hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()


def build_randomization_schedule() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for family in SEALED_FAMILIES:
        for pair_index in range(1, int(POWER_SPEC["scheduled_pair_slots_per_family"]) + 1):
            seed = _hash_seed(SEALED_PORTFOLIO_ID, family.family_id, str(pair_index))
            arm_order = sorted(PRIMARY_ARMS, key=lambda arm: _hash_seed(seed, arm))
            candidate_order = [0, 1] if int(seed[0], 16) % 2 == 0 else [1, 0]
            rows.append(
                {
                    "family_id": family.family_id,
                    "pair_index": pair_index,
                    "arm_order": arm_order,
                    "candidate_order": candidate_order,
                    "seed_commitment": "sha256:" + seed,
                }
            )
    payload = {
        "schedule_id": "phase16-counterbalanced-schedule-v2",
        "seed_rule": "sha256(portfolio_id::family_id::pair_index)",
        "counterbalancing_rule": (
            "Every family has 13 fixed pair slots; arm order is hash-determined and candidate order alternates "
            "by the first seed nibble. No result-dependent reordering or adaptive extension is allowed."
        ),
        "rows": rows,
    }
    payload["schedule_hash"] = _content_hash(payload)
    return payload


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key).casefold()
            yield from _walk_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_keys(item)


def _all_allowed_target_values(family: FamilySpec) -> set[str]:
    return {
        str(value).casefold()
        for values in family.allowed_targets.values()
        for value in values
    }


def _packet_leakage_audit(family: FamilySpec) -> dict[str, Any]:
    allowed_values = _all_allowed_target_values(family)
    packet_reports = []
    for fixture in family.packet_fixtures:
        objective_low = fixture.objective.casefold()
        candidate_strings = [
            _canonical_json(candidate).casefold()
            for candidate in fixture.admissible_frontier
        ]
        objective_mentions_allowed_target = any(value and value in objective_low for value in allowed_values)
        one_fact_contains_anchor_and_target = any(
            any(anchor.casefold() in fact.casefold() for anchor in fixture.objective_anchors)
            and any(value and value in fact.casefold() for value in allowed_values)
            for fact in fixture.graph_facts
        )
        candidate_field_sets = [set(candidate) for candidate in fixture.admissible_frontier]
        candidate_fields_match_current_policy = all(
            fields
            <= {"name", "target", "preconditions", "effects", "operational_cost", "reason"}
            for fields in candidate_field_sets
        )
        packet_reports.append(
            {
                "packet_id": fixture.packet_id,
                "objective_mentions_allowed_target": objective_mentions_allowed_target,
                "one_fact_contains_anchor_and_target": one_fact_contains_anchor_and_target,
                "candidate_field_sets": [sorted(fields) for fields in candidate_field_sets],
                "candidate_fields_match_current_policy": candidate_fields_match_current_policy,
                "candidate_order_variants_present": len(list(permutations(range(len(candidate_strings))))) == 2,
                "minimum_relation_hops": fixture.minimum_relation_hops,
                "passes": (
                    not objective_mentions_allowed_target
                    and not one_fact_contains_anchor_and_target
                    and candidate_fields_match_current_policy
                    and fixture.minimum_relation_hops >= 2
                    and len(candidate_strings) == 2
                ),
            }
        )
    family_manifest_keys = set(_walk_keys(_family_manifest_payload(family)))
    forbidden_keys_present = sorted(FORBIDDEN_FAMILY_FIELD_NAMES & family_manifest_keys)
    return {
        "family_id": family.family_id,
        "packet_reports": packet_reports,
        "forbidden_family_fields_present": forbidden_keys_present,
        "passes": all(item["passes"] is True for item in packet_reports) and not forbidden_keys_present,
    }


def _topology_exposure_audit() -> dict[str, Any]:
    exposed_hashes = {
        family_id: canonical_topology_hash(nodes, edges)
        for family_id, (nodes, edges) in EXPOSED_FAMILY_TOPOLOGIES.items()
    }
    sealed_hashes = {
        family.family_id: canonical_topology_hash(family.nodes, family.edges)
        for family in SEALED_FAMILIES
    }
    collisions = [
        {
            "sealed_family_id": family_id,
            "exposed_family_id": exposed_id,
            "topology_hash": topology_hash,
        }
        for family_id, topology_hash in sealed_hashes.items()
        for exposed_id, exposed_hash in exposed_hashes.items()
        if topology_hash == exposed_hash
    ]
    sealed_pairwise_unique = len(set(sealed_hashes.values())) == len(sealed_hashes)
    return {
        "exposed_family_hashes": exposed_hashes,
        "sealed_family_hashes": sealed_hashes,
        "collisions": collisions,
        "sealed_pairwise_unique": sealed_pairwise_unique,
        "passes": not collisions and sealed_pairwise_unique,
    }


def _split_manifest() -> dict[str, Any]:
    rows = [
        {
            "family_id": "purpose-range",
            "partition": "mechanics_development",
            "engagement_id": "historical-purpose-range",
            "topology_hash": canonical_topology_hash(*EXPOSED_FAMILY_TOPOLOGIES["purpose-range"]),
            "confirmatory_eligible": False,
            "reason_code": "previously_exercised_development_family",
        },
        {
            "family_id": "replication-purpose-range",
            "partition": "mechanics_development",
            "engagement_id": "historical-replication-purpose-range",
            "topology_hash": canonical_topology_hash(*EXPOSED_FAMILY_TOPOLOGIES["replication-purpose-range"]),
            "confirmatory_eligible": False,
            "reason_code": "previously_exercised_development_family",
        },
        {
            "family_id": "same-domain-gpo-dc-scope-late-blocker",
            "partition": "structural_t1_development",
            "engagement_id": "historical-gpo-dc-scope-late-blocker",
            "topology_hash": canonical_topology_hash(*EXPOSED_FAMILY_TOPOLOGIES["same-domain-gpo-dc-scope-late-blocker"]),
            "confirmatory_eligible": False,
            "reason_code": "previously_exercised_structural_development_family",
        },
        *[
            {
                "family_id": family.family_id,
                "partition": family.partition,
                "engagement_id": family.engagement_id,
                "topology_hash": canonical_topology_hash(family.nodes, family.edges),
                "confirmatory_eligible": True,
                "reason_code": "fresh_unexposed_topology_and_engagement",
            }
            for family in SEALED_FAMILIES
        ],
    ]
    topology_hashes = [row["topology_hash"] for row in rows]
    engagement_ids = [row["engagement_id"] for row in rows]
    payload = {
        "split_manifest_id": "phase16-split-manifest-v2",
        "rows": rows,
        "partitions_present": sorted({row["partition"] for row in rows}),
        "checks": {
            "required_partitions_present": {
                "mechanics_development",
                "structural_t1_development",
                "sealed_confirmatory",
            }
            <= {row["partition"] for row in rows},
            "topologies_are_disjoint": len(set(topology_hashes)) == len(topology_hashes),
            "engagements_are_disjoint": len(set(engagement_ids)) == len(engagement_ids),
            "sealed_families_are_confirmatory_eligible": all(
                row["confirmatory_eligible"] is True
                for row in rows
                if row["partition"] == "sealed_confirmatory"
            ),
        },
    }
    payload["split_manifest_hash"] = _content_hash(payload)
    return payload


def _manifest_audit(family: FamilySpec) -> dict[str, Any]:
    manifest = authorization_manifest_for_family(family)
    manifest_dict = manifest.to_dict()
    branch_capabilities = _branch_capabilities(family)
    branch_effects = _branch_effects(family)
    allowed_target_dimensions = set(manifest.allowed_targets)
    required_dimensions = set(family.exercised_target_dimensions)
    checks = {
        "schema_is_current": manifest.schema == auth.MANIFEST_SCHEMA,
        "required_identity_fields_present": all(
            (
                manifest.manifest_id,
                manifest.version,
                manifest.operator_authorization_id,
                manifest.engagement_id,
                manifest.range_id,
                manifest.snapshot_id,
                manifest.valid_from,
                manifest.valid_until,
            )
        ),
        "selector_is_exact_host_domain_identity_without_live_id": (
            len(manifest.callbacks) == 1
            and manifest.callbacks[0].has_exact_identity_selector
            and manifest.callbacks[0].callback_id == ""
        ),
        "exact_target_dimensions_complete": allowed_target_dimensions == required_dimensions,
        "every_target_dimension_has_default_deny_allowlist": all(
            bool(manifest.allowed_targets.get(dimension))
            for dimension in required_dimensions
        ),
        "branch_capabilities_allowed": branch_capabilities <= set(manifest.allowed_capabilities),
        "branch_effects_allowed": branch_effects <= set(manifest.allowed_effects),
        "explicit_denies_present": bool(manifest.denied_capabilities) and bool(manifest.denied_effects),
        "explicit_denies_do_not_overlap_allows": (
            not (set(manifest.denied_capabilities) & set(manifest.allowed_capabilities))
            and not (set(manifest.denied_effects) & set(manifest.allowed_effects))
        ),
        "allowed_cells_cover_frozen_schedule": set(manifest.allowed_cells) == set(_allowed_cells(family)),
        "authorization_manifest_has_no_budget_fields": not any(
            "budget" in key
            for key in _walk_keys(manifest_dict)
        ),
        "authorization_manifest_has_no_forbidden_answer_fields": not (
            FORBIDDEN_FAMILY_FIELD_NAMES & set(_walk_keys(manifest_dict))
        ),
    }
    return {
        "family_id": family.family_id,
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.sha256,
        "checks": checks,
        "passes": all(checks.values()),
    }


def _arm_invariance_audit(family: FamilySpec) -> dict[str, Any]:
    manifest = authorization_manifest_for_family(family)
    runtime_callback = auth.CallbackSelector(
        callback_id="phase17-fixture-callback-1",
        host=family.selector_host,
        domain=family.selector_domain,
        identity=family.selector_identity,
    )
    cell_id = _allowed_cells(family)[0]
    binding = auth.TrustedCellBinding(
        cell_id=cell_id,
        cell_authorization_id=f"{family.family_id}-cell-auth-1",
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.sha256,
        engagement_id=manifest.engagement_id,
        callback=runtime_callback,
        issued_at="2026-07-16T00:00:00+00:00",
        expires_at="2026-09-01T00:00:00+00:00",
    )
    branch = family.branches[0]
    envelope_a = auth.build_action_envelope(
        manifest,
        binding,
        callback=runtime_callback,
        target_fields=branch.sample_target_fields,
        capability=branch.capability_path[0],
        effects=(branch.sample_effects[0],),
        concrete_arguments={"fixture": family.family_id, "branch": branch.branch_id},
        transaction_id=f"{family.family_id}-transaction-1",
        decision_origin="hybrid_model_branch",
        policy_decision_id="decision-hybrid-1",
    )
    envelope_b = auth.build_action_envelope(
        manifest,
        binding,
        callback=runtime_callback,
        target_fields=branch.sample_target_fields,
        capability=branch.capability_path[0],
        effects=(branch.sample_effects[0],),
        concrete_arguments={"fixture": family.family_id, "branch": branch.branch_id},
        transaction_id=f"{family.family_id}-transaction-1",
        decision_origin="symbolic_control",
        policy_decision_id="decision-symbolic-1",
    )
    if envelope_a is None or envelope_b is None:
        return {
            "family_id": family.family_id,
            "passes": False,
            "reason_code": "fixture_action_envelope_construction_failed",
        }
    decision_a = auth.authorize_action(manifest, binding, envelope_a, now="2026-07-17T00:00:00+00:00")
    decision_b = auth.authorize_action(manifest, binding, envelope_b, now="2026-07-17T00:00:00+00:00")
    checks = {
        "enforcement_projection_hash_is_arm_invariant": (
            envelope_a.enforcement_projection_sha256 == envelope_b.enforcement_projection_sha256
        ),
        "action_envelope_hash_retains_audit_difference": envelope_a.sha256 != envelope_b.sha256,
        "decision_is_arm_invariant": decision_a.decision == decision_b.decision == auth.ALLOW,
        "decision_id_is_arm_invariant": decision_a.decision_id == decision_b.decision_id,
    }
    return {
        "family_id": family.family_id,
        "decision_a": decision_a.to_dict(),
        "decision_b": decision_b.to_dict(),
        "checks": checks,
        "passes": all(checks.values()),
    }


def _policy_input_invisibility_audit() -> dict[str, Any]:
    source_fragments = {
        "_candidate_payload": inspect.getsource(policy._candidate_payload),  # type: ignore[attr-defined]
        "_normalized_state_payload": inspect.getsource(policy._normalized_state_payload),  # type: ignore[attr-defined]
        "_decision_packet": inspect.getsource(policy._decision_packet),  # type: ignore[attr-defined]
    }
    hits = {
        function_name: sorted(
            field_name
            for field_name in POLICY_AUTH_FIELD_NAMES
            if field_name in source
        )
        for function_name, source in source_fragments.items()
    }
    return {
        "source_functions": sorted(source_fragments),
        "authorization_field_hits": hits,
        "passes": not any(hits.values()),
    }


def _authorization_dependency_audit() -> dict[str, Any]:
    source = Path(auth.__file__).read_text(encoding="utf-8").casefold()
    forbidden_dependencies = (
        "langchain",
        "openai",
        "bedrock",
        "prompt",
        "rationale",
        "transcript",
        "task_output",
        "outcome_metadata",
    )
    hits = [item for item in forbidden_dependencies if item in source]
    return {
        "module_path": _portable_path(Path(auth.__file__)),
        "forbidden_dependency_tokens": list(forbidden_dependencies),
        "hits": hits,
        "passes": not hits,
    }


def _gate_freeze() -> dict[str, Any]:
    gate_module_path = Path(auth.__file__).resolve()
    interpretation_rules = {
        "gate_version": auth.GATE_VERSION,
        "decision_vocabulary": [auth.ALLOW, auth.DENY, auth.UNKNOWN],
        "deny_precedence": "explicit deny overrides allow",
        "target_matching": "exact canonical equality only",
        "manifest_callback_selector_semantics": (
            "host/domain/identity are frozen before Phase 17; manifest callback_id may be empty"
        ),
        "runtime_callback_binding_semantics": (
            "trusted cell binding and action envelope must carry the exact fresh runtime callback ID"
        ),
        "audit_only_fields_excluded_from_enforcement_projection": [
            "decision_origin",
            "policy_decision_id",
        ],
        "no_model_or_prompt_dependency": True,
    }
    callback_binding_contract = {
        "contract_id": "phase16-callback-binding-contract-v2",
        "phase16_selector_fields": ["host", "domain", "identity"],
        "phase17_runtime_binding_fields": ["callback_id", "host", "domain", "identity"],
        "exact_match_required": True,
        "substring_or_suffix_match_permitted": False,
        "duplicate_callback_permitted": False,
        "cross_cell_or_cross_engagement_reuse_permitted": False,
        "phase17_live_binding_required": True,
    }
    payload = {
        "gate_module_path": _portable_path(gate_module_path),
        "gate_module_sha256": _file_sha256(gate_module_path),
        "manifest_schema": auth.MANIFEST_SCHEMA,
        "cell_binding_schema": auth.CELL_BINDING_SCHEMA,
        "action_envelope_schema": auth.ACTION_ENVELOPE_SCHEMA,
        "decision_schema": auth.DECISION_SCHEMA,
        "gate_version": auth.GATE_VERSION,
        "interpretation_rules": interpretation_rules,
        "callback_binding_contract": callback_binding_contract,
    }
    payload["interpretation_rules_hash"] = _content_hash(interpretation_rules)
    payload["callback_binding_contract_hash"] = _content_hash(callback_binding_contract)
    payload["gate_freeze_hash"] = _content_hash(payload)
    return payload


def _paired_arm_freeze(
    family: FamilySpec,
    *,
    gate_freeze: Mapping[str, Any],
    coverage_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = authorization_manifest_for_family(family)
    budget_hash = _content_hash(OPERATIONAL_BUDGETS)
    rows = [
        {
            "arm": arm,
            "authorization_manifest_sha256": manifest.sha256,
            "authorization_gate_version": auth.GATE_VERSION,
            "interpretation_rules_hash": gate_freeze["interpretation_rules_hash"],
            "callback_binding_contract_hash": gate_freeze["callback_binding_contract_hash"],
            "coverage_manifest_hash": coverage_manifest["coverage_manifest_hash"],
            "operational_budget_hash": budget_hash,
        }
        for arm in PRIMARY_ARMS
    ]
    comparable_fields = (
        "authorization_manifest_sha256",
        "authorization_gate_version",
        "interpretation_rules_hash",
        "callback_binding_contract_hash",
        "coverage_manifest_hash",
        "operational_budget_hash",
    )
    checks = {
        field_name: len({row[field_name] for row in rows}) == 1
        for field_name in comparable_fields
    }
    return {
        "family_id": family.family_id,
        "rows": rows,
        "checks": checks,
        "passes": all(checks.values()),
    }


def _candidate_inventory(topology_audit: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "family_id": "goad-trust-walker",
            "source": "existing_go_ad_benchmark",
            "disposition": "rejected_confirmatory_transfer",
            "reason_code": "existing_goad_anchor_not_fresh_structural_holdout",
        },
        {
            "family_id": "phase6-laps-family-transfer-r5",
            "source": "phase6",
            "disposition": "rejected_confirmatory_transfer",
            "reason_code": "previously_exercised_phase6_family",
        },
        {
            "family_id": "phase7-trust-context-corroboration-v2",
            "source": "phase7",
            "disposition": "rejected_confirmatory_transfer",
            "reason_code": "previously_exercised_phase7_family",
        },
        {
            "family_id": "purpose-range",
            "source": "existing_development_range",
            "disposition": "retain_mechanics_development_only",
            "reason_code": "previously_exercised_development_family",
        },
        {
            "family_id": "replication-purpose-range",
            "source": "existing_development_range",
            "disposition": "retain_mechanics_development_only",
            "reason_code": "previously_exercised_development_family",
        },
        {
            "family_id": "same-domain-gpo-dc-scope-late-blocker",
            "source": "existing_structural_development_surface",
            "disposition": "retain_structural_t1_development_only",
            "reason_code": "previously_exercised_structural_development_family",
        },
        *[
            {
                "family_id": family.family_id,
                "source": "phase16_preregistered_design",
                "disposition": "sealed_confirmatory_candidate",
                "reason_code": "fresh_unexposed_topology_and_engagement",
            }
            for family in SEALED_FAMILIES
        ],
    ]
    exposed_hashes = dict(topology_audit.get("exposed_family_hashes") or {})
    sealed_hashes = dict(topology_audit.get("sealed_family_hashes") or {})
    for row in rows:
        row["topology_hash"] = sealed_hashes.get(row["family_id"]) or exposed_hashes.get(row["family_id"])
    return rows


def build_phase16_report(*, generated_at: str | None = None) -> dict[str, Any]:
    topology_audit = _topology_exposure_audit()
    split_manifest = _split_manifest()
    family_manifests = {
        family.family_id: _family_manifest_payload(family)
        for family in SEALED_FAMILIES
    }
    manifest_audits = [_manifest_audit(family) for family in SEALED_FAMILIES]
    leakage_audits = [_packet_leakage_audit(family) for family in SEALED_FAMILIES]
    physical_realization_audits = [_physical_realization_audit(family) for family in SEALED_FAMILIES]
    arm_invariance_audits = [_arm_invariance_audit(family) for family in SEALED_FAMILIES]
    coverage_manifest = build_coverage_manifest()
    coverage_capabilities = {
        str(path.get("capability") or "")
        for path in coverage_manifest["paths"]
    }
    planned_capabilities = {
        "collect-graph",
        *{
            capability
            for family in SEALED_FAMILIES
            for capability in _branch_capabilities(family)
        },
    }
    power_report = build_power_report()
    randomization_schedule = build_randomization_schedule()
    gate_freeze = _gate_freeze()
    policy_input_audit = _policy_input_invisibility_audit()
    authorization_dependency_audit = _authorization_dependency_audit()
    paired_arm_freezes = [
        _paired_arm_freeze(
            family,
            gate_freeze=gate_freeze,
            coverage_manifest=coverage_manifest,
        )
        for family in SEALED_FAMILIES
    ]
    catalog_names = _catalog_names()
    all_planned_capabilities = {
        capability
        for family in SEALED_FAMILIES
        for capability in _branch_capabilities(family)
    }
    checks = {
        "two_sealed_confirmatory_families_present": len(SEALED_FAMILIES) >= 2,
        "all_planned_capabilities_are_current_generic_capabilities": all_planned_capabilities <= catalog_names,
        "topology_exposure_audit_passes": topology_audit["passes"] is True,
        "split_manifest_passes": all(split_manifest["checks"].values()),
        "leakage_audits_pass": all(item["passes"] is True for item in leakage_audits),
        "physical_realization_audits_pass": all(item["passes"] is True for item in physical_realization_audits),
        "power_report_passes": power_report["passes_power_gate"] is True,
        "manifest_audits_pass": all(item["passes"] is True for item in manifest_audits),
        "arm_invariance_audits_pass": all(item["passes"] is True for item in arm_invariance_audits),
        "paired_arm_freezes_pass": all(item["passes"] is True for item in paired_arm_freezes),
        "coverage_manifest_matches_planned_capabilities": coverage_capabilities == planned_capabilities,
        "coverage_manifest_stays_inactive_until_phase17": all(
            path["activation_authorized"] is False
            and path["phase17_final_boundary_attestation_required"] is True
            for path in coverage_manifest["paths"]
        ),
        "policy_input_invisibility_audit_passes": policy_input_audit["passes"] is True,
        "authorization_dependency_audit_passes": authorization_dependency_audit["passes"] is True,
        "budget_scope_separation_preserved": all(
            "budget" not in key
            for manifest in family_manifests.values()
            for key in _walk_keys(manifest["authorization_manifest"])
        ),
        "phase18_unseal_remains_unauthorized": coverage_manifest["phase18_unseal_authorized"] is False,
    }
    isc_status = {
        "R-ISC-29": checks["topology_exposure_audit_passes"],
        "R-ISC-30": checks["split_manifest_passes"],
        "R-ISC-32": checks["leakage_audits_pass"],
        "R-ISC-33": checks["power_report_passes"] and checks["physical_realization_audits_pass"],
        "R-ISC-50": checks["manifest_audits_pass"],
        "R-ISC-51": checks["manifest_audits_pass"],
        "R-ISC-52": checks["manifest_audits_pass"],
        "R-ISC-53": checks["budget_scope_separation_preserved"],
        "R-ISC-57": checks["arm_invariance_audits_pass"] and checks["paired_arm_freezes_pass"],
        "R-ISC-60": checks["policy_input_invisibility_audit_passes"] and checks["authorization_dependency_audit_passes"],
        "R-ISC-61": bool(gate_freeze["gate_freeze_hash"]),
        "R-ISC-66": checks["leakage_audits_pass"] and checks["manifest_audits_pass"],
        "R-ISC-68": (
            checks["arm_invariance_audits_pass"]
            and checks["policy_input_invisibility_audit_passes"]
            and checks["authorization_dependency_audit_passes"]
        ),
        "R-ISC-69": checks["arm_invariance_audits_pass"],
        "R-ISC-80": checks["policy_input_invisibility_audit_passes"],
        "R-ISC-81": checks["manifest_audits_pass"],
    }
    report = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "portfolio_id": SEALED_PORTFOLIO_ID,
        "source_plan": SOURCE_PLAN,
        "entry_evidence": {
            "phase15_complete": True,
            "independent_benchmark_owner_reviewer": INDEPENDENT_REVIEWER,
            "range_authorization": "approved_existing_ludus_environment_with_ludus_sagerepl_preference",
            "purpose_range_and_validator_scope": "approved_and_confirmed",
            "operator_authorization_id": OPERATOR_AUTHORIZATION_ID,
            "live_policy_matrix_authorized": False,
        },
        "candidate_inventory": _candidate_inventory(topology_audit),
        "topology_exposure_audit": topology_audit,
        "split_manifest": split_manifest,
        "sealed_family_manifests": family_manifests,
        "preregistration": {
            "hypotheses": [
                {
                    "hypothesis_id": "H16-PRIMARY",
                    "statement": (
                        "Within each sealed family, Hybrid improves informative paired branch wins over "
                        "modeled_reachability by at least the preregistered smallest relevant effect while "
                        "preserving proof and clean-stop requirements."
                    ),
                },
                {
                    "hypothesis_id": "H16-TRANSFER",
                    "statement": (
                        "A transfer claim is permitted only if the primary family-level contrast survives "
                        "independently in both sealed families under the frozen non-inferiority rule."
                    ),
                },
            ],
            "primary_arms": list(PRIMARY_ARMS),
            "strongest_deterministic_control": STRONGEST_DETERMINISTIC_CONTROL,
            "cheapest_reviewer_falsifier": CHEAPEST_REVIEWER_FALSIFIER,
            "pure_llm_disposition": PURE_LLM_DISPOSITION,
            "conditional_model_treatments": {
                "weak_model": "not_in_primary_schedule_without_phase17_response_verified_distinctness",
                "strong_model": "not_in_primary_schedule_without_phase17_response_verified_distinctness",
            },
            "forced_branch_canaries": [
                {
                    "family_id": family.family_id,
                    "branch_ids": [branch.branch_id for branch in family.branches],
                    "labels_source": "forced development controls only",
                    "policy_win_credit": False,
                }
                for family in SEALED_FAMILIES
            ],
            "proof_modes": sorted(
                {
                    proof_mode
                    for family in SEALED_FAMILIES
                    for branch in family.branches
                    for proof_mode in branch.proof_modes
                }
            ),
            "validators": [
                "topology_exposure_audit",
                "split_manifest_validator",
                "packet_leakage_metamorphic_audit",
                "physical_realization_and_resource_envelope_audit",
                "authorization_manifest_schema_and_branch_coverage_audit",
                "arm_invariance_audit",
                "policy_input_invisibility_audit",
                "effect_path_coverage_manifest_validator",
                "phase17_exact_callback_binding_preflight",
            ],
            "exclusion_rules": [
                "Exclude any cell with manifest, gate, budget, provider, callback, reset, proof, or coverage hash drift.",
                "Exclude any cell whose plan-valid branch is not representable under the frozen action envelope.",
                "Exclude any cell with uncovered reachable external-effect path or exact-allow/proof join failure.",
            ],
            "burn_rules": [
                "Burn any cell affected by a post-seal authorization, gate, interpretation, coverage, or budget change.",
                "Burn any cell affected by shared setup, normalization, adapter, or measurement defects.",
                "Do not burn arm-attributed valid denials or policy-origin unknowns; Phase 17 freezes their scoring.",
            ],
            "model_provider_identity": MODEL_PROVIDER_IDENTITY,
            "operational_budgets": OPERATIONAL_BUDGETS,
            "power_report": power_report,
            "randomization_schedule": randomization_schedule,
            "analysis_plan": {
                "primary_contrast": "hybrid_vs_modeled_reachability_within_family",
                "secondary_contrasts": [
                    "hybrid_vs_shipping_symbolic",
                    "hybrid_vs_objective_effect_aware",
                    "hybrid_vs_null_no_decision",
                ],
                "family_non_inferiority_rule": (
                    "No pooled transfer claim is permitted if either sealed family fails its own primary "
                    "contrast or becomes underpowered after frozen burn rules."
                ),
                "claim_ladder": [
                    "descriptive_family_result",
                    "within_family_causal_result",
                    "two_family_transfer_result",
                    "bounded_authorized_lab_harness_product_recommendation_only_after_phase19",
                ],
                "adaptive_stopping_for_favorable_result_permitted": False,
            },
        },
        "authorization_freeze": {
            "family_manifest_audits": manifest_audits,
            "arm_invariance_audits": arm_invariance_audits,
            "paired_arm_freezes": paired_arm_freezes,
            "gate_freeze": gate_freeze,
            "coverage_manifest": coverage_manifest,
            "policy_input_invisibility_audit": policy_input_audit,
            "authorization_dependency_audit": authorization_dependency_audit,
        },
        "leakage_audits": leakage_audits,
        "physical_realization_audits": physical_realization_audits,
        "range_management": {
            "ludus_connection": "ludus_sagerepl",
            "new_ludus_user_required_now": False,
            "new_ludus_user_request_rule": (
                "Ask Russel only if Phase 17 proves ludus_sagerepl cannot create or isolate the planned range IDs."
            ),
            "resource_envelope": OPERATIONAL_BUDGETS,
            "physical_realization_convention": PHYSICAL_REALIZATION_CONVENTION,
            "family_resource_feasibility": physical_realization_audits,
            "operator_rules": [
                "Power down inactive ranges before deploying or powering on a Phase 17 development range.",
                "Keep at most one purpose range powered on at a time.",
                "Do not run parallel live cells on the current Proxmox host.",
                "Re-check host RAM and CPU headroom before any range operation.",
                "Do not co-locate sealed logical domain or host nodes without burning affected cells and resealing.",
            ],
        },
        "independent_pre_unseal_review": {
            "reviewer": INDEPENDENT_REVIEWER,
            "review_guide_path": _portable_path(DEFAULT_REVIEW_GUIDE_PATH),
            "status": "arranged_pending_reviewer_execution_before_phase18_unseal",
            "required_confirmation_topics": [
                "manifest_completeness",
                "exact_field_semantics",
                "answer_blindness",
                "arm_invariance",
                "physical_realization_and_resource_feasibility",
                "hash_and_sidecar_integrity",
            ],
            "phase18_unseal_blocked_until_review_confirmation": True,
        },
        "stop_loss": {
            "reason_code": "structural_confirmatory_benchmark_not_ready",
            "emitted": False,
            "why_not_emitted": (
                "Two topology-distinct, answer-blind, current-capability-only sealed designs pass the offline "
                "Phase 16 contract with explicit seven-guest physical realizations under the resealed one-range "
                "resource envelope. Phase 17 still must prove live mechanics and exact final-boundary mediation."
            ),
        },
        "isc_status": isc_status,
        "checks": checks,
        "passes_gate": all(checks.values()) and all(isc_status.values()),
    }
    report["report_hash"] = _content_hash(report)
    return report


def write_phase16_artifacts(
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    coverage_output_path: Path = DEFAULT_COVERAGE_OUTPUT_PATH,
    family_manifest_paths: Mapping[str, Path] = DEFAULT_FAMILY_MANIFEST_PATHS,
    generated_at: str | None = None,
) -> dict[str, Any]:
    report = build_phase16_report(generated_at=generated_at)
    written: dict[str, Any] = {
        "coverage_manifest": _write_json_with_sidecar(
            coverage_output_path,
            report["authorization_freeze"]["coverage_manifest"],
        ),
        "family_manifests": {},
    }
    for family_id, manifest in report["sealed_family_manifests"].items():
        written["family_manifests"][family_id] = _write_json_with_sidecar(
            Path(family_manifest_paths[family_id]),
            manifest,
        )
    written["portfolio_report"] = _write_json_with_sidecar(output_path, report)
    return {
        "report": report,
        "written_artifacts": written,
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase16-structural-benchmark-portfolio",
        help="emit the Phase 16 fresh structural benchmark portfolio preregistration and seal artifacts",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--coverage-output", default=str(DEFAULT_COVERAGE_OUTPUT_PATH))
    parser.add_argument("--family-s1-output", default=str(DEFAULT_FAMILY_MANIFEST_PATHS["sealed-family-s1"]))
    parser.add_argument("--family-s2-output", default=str(DEFAULT_FAMILY_MANIFEST_PATHS["sealed-family-s2"]))
    parser.add_argument("--generated-at", default=None)
    parser.set_defaults(func=_cmd_phase16_structural_benchmark_portfolio)


def _cmd_phase16_structural_benchmark_portfolio(args: Any) -> int:
    result = write_phase16_artifacts(
        output_path=Path(args.output),
        coverage_output_path=Path(args.coverage_output),
        family_manifest_paths={
            "sealed-family-s1": Path(args.family_s1_output),
            "sealed-family-s2": Path(args.family_s2_output),
        },
        generated_at=args.generated_at,
    )
    report = result["report"]
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(phase18_unseal_authorized={report['entry_evidence']['live_policy_matrix_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
