"""Phase-0 GOAD evaluation scenarios for the eval gauge.

Ground-truthable scenarios at graded difficulty (ISC-16/17). A scenario fixes the
engagement to read, the domain roles (child/parent/objective), and which milestones it can
prove. Domains are the GOAD defaults; the `engagement_id` (the Mythic operation / ledger key)
is operator-configurable per lab.
"""
try:  # package import
    from . import live_seams
    from .range_state import Scenario, MilestoneSpec, Milestone
except Exception:  # script / sys.path import
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import live_seams  # type: ignore
    from range_state import Scenario, MilestoneSpec, Milestone  # type: ignore

CHILD = "north.sevenkingdoms.local"
PARENT = "sevenkingdoms.local"
OBJECTIVE = "essos.local"
_DOMAINS = {"child": CHILD, "parent": PARENT, "objective": OBJECTIVE}
PURPOSE_RANGE_DOMAIN = "range.local"
_PURPOSE_RANGE_DOMAINS = {
    "child": PURPOSE_RANGE_DOMAIN,
    "parent": PURPOSE_RANGE_DOMAIN,
    "objective": PURPOSE_RANGE_DOMAIN,
}
REPLICATION_PURPOSE_RANGE_DOMAIN = "replication.local"
_REPLICATION_PURPOSE_RANGE_DOMAINS = {
    "child": REPLICATION_PURPOSE_RANGE_DOMAIN,
    "parent": REPLICATION_PURPOSE_RANGE_DOMAIN,
    "objective": REPLICATION_PURPOSE_RANGE_DOMAIN,
}


def goad_scenarios(engagement_id: str = "Operation_GOAD") -> list[Scenario]:
    """The Phase-0 scenarios. `engagement_id` must match the run's ledger key per lab."""
    return [
        # Easiest: foothold -> SYSTEM on a host (single hop).
        Scenario(
            name="single-hop-system",
            engagement_id=engagement_id,
            objective=("From the current CASTELBLACK foothold (NORTH\\samwell.tarly), obtain SYSTEM-level "
                       f"code execution on a Windows host in {CHILD}."),
            domains=_DOMAINS,
            milestone_subset=(Milestone.FOOTHOLD, Milestone.GRAPH_COLLECTED, Milestone.SYSTEM_ON_HOST),
            direct_probes={Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe()},
        ),
        # Mid: multi-hop to child Domain Admin + krbtgt.
        Scenario(
            name="child-da",
            engagement_id=engagement_id,
            objective=(f"From the CASTELBLACK foothold, escalate to Domain Admin of {CHILD} and DCSync its "
                       "krbtgt account."),
            domains=_DOMAINS,
            # GRAPH_COLLECTED is deliberately NOT scored here: child-da is solvable without BloodHound, so a
            # graph-collection milestone is off the objective path — scoring it invites Goodhart (rewarding a
            # process step the objective doesn't need) and injects false variance (§8 probe-completeness). It
            # is always-False on this scenario today, so dropping it leaves capability/furthest unchanged.
            # Graph-collection milestones belong on scenarios whose path genuinely needs the graph
            # (cross-forest-objective).
            milestone_subset=(
                Milestone.FOOTHOLD, Milestone.SYSTEM_ON_HOST,
                Milestone.DA_CHILD, Milestone.KRBTGT_DUMPED,
            ),
            # These verifier factories need run-context inputs and are built by
            # run_gauge_live.build_probes at live-run time. The live runner records their result into the
            # ledger so hermetic re-score remains probe-grounded without executing live probes later.
            recorded_probe_milestones=frozenset({Milestone.DA_CHILD, Milestone.KRBTGT_DUMPED}),
        ),
        # Hardest: cross-forest to admin control of the objective domain.
        # OBJECTIVE is scenario-supplied (verified admin-control effect on the objective domain).
        # CERT_FORGED is excluded: its effect token is unconfirmed in code (see ISA gap register).
        Scenario(
            name="cross-forest-objective",
            engagement_id=engagement_id,
            objective=f"From the current foothold, achieve administrative control of {OBJECTIVE}.",
            domains=_DOMAINS,
            spec_overrides={Milestone.OBJECTIVE: MilestoneSpec(("da:", "ea:"), domain_role="objective")},
            milestone_subset=tuple(m for m in Milestone if m != Milestone.CERT_FORGED),
            direct_probes={Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe()},
            # These verifier factories need run-context inputs (credential store reader, referee baseline)
            # and are built by run_gauge_live.build_probes at live-run time. Their results are captured once
            # while the range is live and replayed from the ledger for offline/hermetic scoring.
            recorded_probe_milestones=frozenset({
                Milestone.KRBTGT_DUMPED,
                Milestone.DA_CHILD,
                Milestone.OBJECTIVE,
            }),
        ),
        # Holdout: start from an ESSOS principal with a direct LAPS edge and exercise the supported
        # managed-secret -> local-admin -> ADCS path without the Samwell GPO/krbtgt/forest-hop prefix.
        # This is topology-changing evidence, not a claim of generality across arbitrary GOAD paths.
        Scenario(
            name="direct-laps-objective",
            engagement_id=engagement_id,
            objective=f"From the current foothold, achieve administrative control of {OBJECTIVE}.",
            domains=_DOMAINS,
            spec_overrides={Milestone.OBJECTIVE: MilestoneSpec(("da:", "ea:"), domain_role="objective")},
            milestone_subset=(Milestone.FOOTHOLD, Milestone.GRAPH_COLLECTED, Milestone.OBJECTIVE),
            direct_probes={Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe()},
            recorded_probe_milestones=frozenset({Milestone.OBJECTIVE}),
        ),
        # Replanning holdout: same direct-LAPS shared lane as above, but the eval harness injects a
        # verifier-backed CA-export blocker only after the lane has reached remote execution. This separates
        # post-blocker recovery quality from first-frontier preference.
        Scenario(
            name="direct-laps-ca-export-recovery",
            engagement_id=engagement_id,
            objective=f"From the current foothold, achieve administrative control of {OBJECTIVE}.",
            domains=_DOMAINS,
            spec_overrides={Milestone.OBJECTIVE: MilestoneSpec(("da:", "ea:"), domain_role="objective")},
            milestone_subset=(Milestone.FOOTHOLD, Milestone.GRAPH_COLLECTED, Milestone.OBJECTIVE),
            direct_probes={Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe()},
            recorded_probe_milestones=frozenset({Milestone.OBJECTIVE}),
        ),
    ]


def purpose_range_scenarios(engagement_id: str = "Operation_SAGE_POLICY_RANGE") -> list[Scenario]:
    """Purpose-range scenarios for the branch-rich policy discriminator."""
    return [
        Scenario(
            name="purpose-range-visible-cost",
            engagement_id=engagement_id,
            objective=f"From the current foothold, achieve administrative control of {PURPOSE_RANGE_DOMAIN}.",
            domains=_PURPOSE_RANGE_DOMAINS,
            spec_overrides={Milestone.OBJECTIVE: MilestoneSpec(("da:", "ea:"), domain_role="objective")},
            milestone_subset=(Milestone.FOOTHOLD, Milestone.GRAPH_COLLECTED, Milestone.OBJECTIVE),
            direct_probes={Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe()},
            recorded_probe_milestones=frozenset({Milestone.OBJECTIVE}),
        ),
        Scenario(
            name="purpose-range-recovery",
            engagement_id=engagement_id,
            objective=f"From the current foothold, achieve administrative control of {PURPOSE_RANGE_DOMAIN}.",
            domains=_PURPOSE_RANGE_DOMAINS,
            spec_overrides={Milestone.OBJECTIVE: MilestoneSpec(("da:", "ea:"), domain_role="objective")},
            milestone_subset=(Milestone.FOOTHOLD, Milestone.GRAPH_COLLECTED, Milestone.OBJECTIVE),
            direct_probes={Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe()},
            recorded_probe_milestones=frozenset({Milestone.OBJECTIVE}),
        ),
        Scenario(
            name="purpose-range-ca-export-replanning",
            engagement_id=engagement_id,
            objective=f"From the current foothold, achieve administrative control of {PURPOSE_RANGE_DOMAIN}.",
            domains=_PURPOSE_RANGE_DOMAINS,
            spec_overrides={Milestone.OBJECTIVE: MilestoneSpec(("da:", "ea:"), domain_role="objective")},
            milestone_subset=(Milestone.FOOTHOLD, Milestone.GRAPH_COLLECTED, Milestone.OBJECTIVE),
            direct_probes={Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe()},
            recorded_probe_milestones=frozenset({Milestone.OBJECTIVE}),
        ),
    ]


def replication_purpose_range_scenarios(
    engagement_id: str = "Operation_SAGE_REPLICATION_RANGE",
) -> list[Scenario]:
    """Replication-purpose-range scenario for the second branch-rich discriminator."""
    return [
        Scenario(
            name="replication-purpose-range-visible-cost",
            engagement_id=engagement_id,
            objective=(
                "From the current foothold, achieve administrative control of "
                f"{REPLICATION_PURPOSE_RANGE_DOMAIN}."
            ),
            domains=_REPLICATION_PURPOSE_RANGE_DOMAINS,
            spec_overrides={Milestone.OBJECTIVE: MilestoneSpec(("da:", "ea:"), domain_role="objective")},
            milestone_subset=(Milestone.FOOTHOLD, Milestone.GRAPH_COLLECTED, Milestone.OBJECTIVE),
            direct_probes={Milestone.GRAPH_COLLECTED: live_seams.graph_collected_probe()},
            recorded_probe_milestones=frozenset({Milestone.OBJECTIVE}),
        ),
    ]


def all_scenarios(engagement_id: str = "Operation_GOAD") -> list[Scenario]:
    """Return the GOAD suite plus provisionable purpose-range discriminator scenarios."""
    return [
        *goad_scenarios(engagement_id),
        *purpose_range_scenarios(engagement_id),
        *replication_purpose_range_scenarios(engagement_id),
    ]
