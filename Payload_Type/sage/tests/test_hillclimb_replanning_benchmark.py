from ai.hillclimb import replanning_benchmark
from ai.hillclimb import run_gauge_live
from ai.hillclimb.scenarios import all_scenarios, purpose_range_scenarios
from ai.langgraph import capabilities


def test_replanning_benchmark_collected_state_starts_from_branch_rich_purpose_range():
    actions = capabilities.actions_from_state(replanning_benchmark.synthetic_collected_state())

    assert [action.name for action in actions] == [
        "gpo-controlled-system-exec",
        "read-managed-local-admin-secret",
    ]


def test_replanning_benchmark_validator_proves_late_blocker_and_two_recovery_routes():
    report = replanning_benchmark.validate_replanning_benchmark()

    assert report["passes_gate"] is True
    assert report["checks"]["natural_initial_frontier_is_branch_rich"] is True
    assert report["checks"]["blocker_is_verifier_backed_and_repairable"] is True
    assert report["checks"]["post_blocker_frontier_has_multiple_recovery_families"] is True
    assert [item["name"] for item in report["shared_path"]] == [
        "read-managed-local-admin-secret",
        "use-managed-local-admin-secret",
        "execute-as-local-admin",
    ]
    assert report["blocker"]["action"]["name"] == "adcs-ca-private-key-export"
    assert report["blocker"]["failure_class"] == "transient"
    assert report["blocker"]["verification"]["verdict"] == "blocked"
    assert {item["name"] for item in report["blocker"]["post_blocker_frontier"]} >= {
        "gpo-controlled-system-exec",
        "endpoint-protection-adjustment",
        "adcs-ca-private-key-export",
    }
    assert [item["name"] for item in report["repair_path"]] == [
        "endpoint-protection-adjustment",
        "adcs-ca-private-key-export",
        "adcs-certificate-auth",
    ]
    assert [item["name"] for item in report["detour_path"]] == [
        "gpo-controlled-system-exec",
        "grant-directory-rights",
        "dcsync-krbtgt",
        "forge-golden-ticket",
    ]


def test_replanning_benchmark_scenario_uses_existing_purpose_range_referee_domain():
    scenarios = purpose_range_scenarios("replanning-eval")
    scenario = next(item for item in scenarios if item.name == "purpose-range-ca-export-replanning")

    assert "purpose-range-ca-export-replanning" in {
        item.name for item in all_scenarios("replanning-eval")
    }
    assert run_gauge_live._scored_referee_domains(scenario) == {"range.local"}
