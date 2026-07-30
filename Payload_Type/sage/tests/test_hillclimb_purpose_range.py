from ai.hillclimb import purpose_range
from ai.hillclimb import run_gauge_live
from ai.hillclimb.scenarios import purpose_range_scenarios
from ai.langgraph import capabilities


def test_purpose_range_manifest_is_exact_four_host_two_lane_design():
    spec = purpose_range.PURPOSE_RANGE

    assert spec.hosts == ("DC01", "CA01", "SRV02", "WS01")
    assert spec.foothold_identity == r"RANGE\user1"
    assert spec.objective.endswith("range.local.")
    assert [lane.first_action for lane in spec.lanes] == [
        "gpo-controlled-system-exec",
        "read-managed-local-admin-secret",
    ]
    assert {lane.family for lane in spec.lanes} == {"gpo-directory", "managed-local-admin"}


def test_purpose_range_collected_state_exposes_exact_branch_point():
    actions = capabilities.actions_from_state(purpose_range.synthetic_collected_state())

    assert [action.name for action in actions] == [
        "gpo-controlled-system-exec",
        "read-managed-local-admin-secret",
    ]


def test_purpose_range_validator_proves_cost_and_recovery_variants():
    report = purpose_range.validate_purpose_range()

    assert report["passes_gate"] is True
    assert report["checks"]["both_lanes_reach_objective"] is True
    assert report["lanes"][0]["modeled_transactions"] == 4
    assert report["lanes"][1]["modeled_transactions"] == 5
    assert report["variants"][0]["passes"] is True
    assert report["variants"][1]["passes"] is True
    assert any(
        action["name"] == "read-managed-local-admin-secret"
        for action in report["variants"][1]["post_blocker_frontier"]
    )
    assert all(
        action["name"] != "gpo-controlled-system-exec"
        for action in report["variants"][1]["post_blocker_frontier"]
    )


def test_purpose_range_scenarios_use_range_referee_domain():
    scenarios = purpose_range_scenarios("range-eval")

    assert [scenario.name for scenario in scenarios] == [
        "purpose-range-visible-cost",
        "purpose-range-recovery",
        "purpose-range-ca-export-replanning",
        "purpose-range-gpo-dc-scope-late-blocker",
    ]
    assert run_gauge_live._scored_referee_domains(scenarios[0]) == {"range.local"}
