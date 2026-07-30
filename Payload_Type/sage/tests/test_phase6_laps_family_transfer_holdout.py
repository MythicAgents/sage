from ai.hillclimb import laps_family_transfer_holdout as holdout
from ai.hillclimb.scenarios import all_scenarios, laps_family_transfer_holdout_scenarios
from ai.langgraph import capabilities, engagement_state


def _achieved_hop(effect: str) -> engagement_state.Hop:
    return engagement_state.Hop(
        id=f"test:{effect}",
        technique="capability:execute-as-local-admin",
        target=effect,
        effect=effect,
        status="achieved",
        evidence={"source": "test", "mythic_task_id": "1"},
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp="2026-07-14T00:00:00Z",
    )


def test_phase6_holdout_manifest_freezes_exact_laps_topology_and_budget():
    spec = holdout.LAPS_FAMILY_TRANSFER_HOLDOUT
    manifest = holdout.sealed_manifest()

    assert spec.name == "sage-laps-family-transfer-holdout-r5"
    assert spec.range_id == "SAGELAPSR520260715"
    assert spec.root_domain == "cinder.local"
    assert spec.child_domains == ("ember.cinder.local", "ash.cinder.local")
    assert spec.foothold_identity == r"CINDER\user1"
    assert spec.targets == (
        ("ASH-OPS01", "ash.cinder.local"),
        ("EMBER-OPS01", "ember.cinder.local"),
    )
    assert spec.capability_chain == (
        "read-managed-local-admin-secret",
        "use-managed-local-admin-secret",
        "execute-as-local-admin",
    )
    assert spec.baseline_snapshot == "sage-laps-family-transfer-r5-base-v1"
    assert spec.live_reset_snapshot == "sage-laps-transfer-r5-apollo-staged-v1"
    assert spec.budgets.frontier_preflights == 2
    assert spec.budgets.mechanics_canaries == 4
    assert spec.budgets.forced_confirmation_runs == 12
    assert manifest["manifest_hash"].startswith("sha256:")
    assert manifest["countable_run_requirements"]["forced_labels_are_policy_wins"] is False


def test_phase6_holdout_collected_state_exposes_exact_two_cross_domain_laps_candidates():
    ash_state = holdout.synthetic_collected_state("ash-remote-exec")
    ember_state = holdout.synthetic_collected_state("ember-remote-exec")
    ash_actions = capabilities.actions_from_state(ash_state)
    ember_actions = capabilities.actions_from_state(ember_state)

    assert [action.name for action in ash_actions] == [
        "read-managed-local-admin-secret",
        "read-managed-local-admin-secret",
    ]
    assert [action.target for action in ash_actions] == [
        "account=user1;account_domain=cinder.local;target=ash-ops01;target_domain=ash.cinder.local;callback=laps-family-transfer-r5-1",
        "account=user1;account_domain=cinder.local;target=ember-ops01;target_domain=ember.cinder.local;callback=laps-family-transfer-r5-1",
    ]
    assert [action.target for action in ember_actions] == [action.target for action in ash_actions]


def test_phase6_holdout_validator_proves_cost_split_and_control_failures():
    report = holdout.validate_laps_family_transfer_holdout()

    assert report["passes_gate"] is True
    assert report["checks"]["exact_two_cross_domain_laps_candidates"] is True
    assert report["checks"]["objective_flip_preserves_frontier_hashes"] is True
    assert report["checks"]["fixed_order_controls_each_fail_one_variant"] is True
    assert report["paired_costs"] == {
        "ash-remote-exec": {"correct_first": 3, "wrong_first": 6},
        "ember-remote-exec": {"correct_first": 3, "wrong_first": 6},
    }


def test_phase6_frontier_identity_normalizes_callback_ids_without_changing_targets():
    actions = list(capabilities.actions_from_state(holdout.synthetic_collected_state()))
    payload = holdout.canonical_candidate_payload(actions[0])
    packet_payload = {
        **payload,
        "target": payload["target"].replace(holdout.CANONICAL_CALLBACK_ID, "42"),
        "preconditions": [
            value.replace(holdout.CANONICAL_CALLBACK_ID, "42")
            for value in payload["preconditions"]
        ],
    }

    assert holdout.canonicalize_callback_text("target=east;callback=42") == (
        f"target=east;callback={holdout.CANONICAL_CALLBACK_ID}"
    )
    assert holdout.canonicalize_callback_text("kerberos-account-context:user@cinder@callback:42") == (
        f"kerberos-account-context:user@cinder@callback:{holdout.CANONICAL_CALLBACK_ID}"
    )
    assert holdout.canonicalize_callback_text("live-callback:42") == (
        f"live-callback:{holdout.CANONICAL_CALLBACK_ID}"
    )
    assert holdout.canonical_semantic_candidate_id(packet_payload) == holdout.canonical_semantic_candidate_id(actions[0])
    assert holdout.canonical_candidate_set_hash([packet_payload]) == holdout.canonical_candidate_set_hash([actions[0]])


def test_phase6_remote_exec_objective_completion_is_host_specific_and_effect_backed():
    ash_objective = "From the current foothold, prove bounded remote execution on ASH-OPS01."
    wrong_state = engagement_state.EngagementState(
        objective=ash_objective,
        hops=[_achieved_hop("remote-exec:ember-ops01@ember.cinder.local")],
    )
    right_state = engagement_state.EngagementState(
        objective=ash_objective,
        hops=[_achieved_hop("remote-exec:ash-ops01@ash.cinder.local")],
    )
    explicit_domain_state = engagement_state.EngagementState(
        objective="Prove remote execution on ASH-OPS01 in ash.cinder.local.",
        hops=[_achieved_hop("remote-exec:ash-ops01@ember.cinder.local")],
    )

    assert engagement_state.objective_effects_complete(wrong_state) is False
    assert engagement_state.objective_effects_complete(right_state) is True
    assert engagement_state.objective_effects_complete(explicit_domain_state) is False


def test_phase6_scenarios_bind_remote_exec_objective_to_each_child_domain():
    scenarios = laps_family_transfer_holdout_scenarios("phase6-eval")
    all_names = {scenario.name for scenario in all_scenarios("phase6-eval")}

    assert [scenario.name for scenario in scenarios] == [
        "laps-family-transfer-ash-remote-exec",
        "laps-family-transfer-ember-remote-exec",
    ]
    assert {scenario.domains["objective"] for scenario in scenarios} == {
        "ash.cinder.local",
        "ember.cinder.local",
    }
    assert {scenario.name for scenario in scenarios} <= all_names
