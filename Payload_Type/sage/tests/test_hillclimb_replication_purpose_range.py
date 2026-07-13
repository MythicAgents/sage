from ai.hillclimb import replication_purpose_range
from ai.hillclimb import run_gauge_live
from ai.hillclimb.scenarios import all_scenarios, replication_purpose_range_scenarios
from ai.langgraph import capabilities


def test_replication_purpose_range_manifest_is_three_host_two_branch_design():
    spec = replication_purpose_range.REPLICATION_PURPOSE_RANGE

    assert spec.hosts == ("DC01", "SRV02", "WS01")
    assert spec.foothold_identity == r"REPLICATION\user1"
    assert spec.objective.endswith("replication.local.")
    assert [branch.first_action for branch in spec.branches] == [
        "gpo-controlled-system-exec",
        "dcsync-krbtgt",
    ]
    assert {branch.family for branch in spec.branches} == {"gpo-directory", "replication-kerberos"}


def test_replication_purpose_range_collected_state_exposes_exact_branch_point():
    actions = capabilities.actions_from_state(replication_purpose_range.synthetic_collected_state())

    assert [action.name for action in actions] == [
        "gpo-controlled-system-exec",
        "dcsync-krbtgt",
    ]


def test_replication_purpose_range_validator_proves_direct_replication_is_cheaper():
    report = replication_purpose_range.validate_replication_purpose_range()

    assert report["passes_gate"] is True
    assert report["checks"]["both_branches_reach_objective"] is True
    assert report["checks"]["direct_replication_is_cheaper"] is True
    assert report["branches"][0]["modeled_transactions"] == 3
    assert report["branches"][1]["modeled_transactions"] == 2
    assert report["branches"][0]["path_names"] == [
        "gpo-controlled-system-exec",
        "dcsync-krbtgt",
        "forge-golden-ticket",
    ]
    assert report["branches"][1]["path_names"] == [
        "dcsync-krbtgt",
        "forge-golden-ticket",
    ]


def test_replication_purpose_range_scenario_uses_replication_referee_domain():
    scenarios = replication_purpose_range_scenarios("replication-eval")

    assert [scenario.name for scenario in scenarios] == [
        "replication-purpose-range-visible-cost",
    ]
    assert "replication-purpose-range-visible-cost" in {
        scenario.name for scenario in all_scenarios("replication-eval")
    }
    assert run_gauge_live._scored_referee_domains(scenarios[0]) == {"replication.local"}
