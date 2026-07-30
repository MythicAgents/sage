from __future__ import annotations

import copy

from ai.hillclimb import trust_context_corroboration as contract
from ai.hillclimb.scenarios import all_scenarios, trust_context_corroboration_scenarios
from ai.langgraph import capabilities


def _decision(decision_id: str, capability: str, target: str) -> dict:
    return {
        "decision_id": decision_id,
        "selected_capability": capability,
        "selected_target": target,
    }


def _transaction(decision_id: str, *, capability: str = "", commands: list[str] | None = None) -> dict:
    return {
        "decision_id": decision_id,
        "capability": capability,
        "callback_id": contract.CANONICAL_CALLBACK_ID,
        "child_tasks": [
            {
                "task_id": f"task-{decision_id}-{index}",
                "command": command,
                "terminal_status": "completed",
            }
            for index, command in enumerate(commands or [], start=1)
        ],
        "proof_lineage": [
            {
                "proof_envelope_id": f"proof-{decision_id}",
                "task_id": f"task-{decision_id}",
                "verifier_id": f"verifier-{decision_id}",
                "admissible_for_runtime_achievement": True,
            }
        ],
    }


def _row(control: str, *, index: int = 1) -> dict:
    decisions = []
    transactions = []
    achieved_effects = []
    if control == "positive":
        context = _decision(
            f"positive-{index}-forge",
            "forge-golden-ticket",
            f"domain={contract.CHILD_DOMAIN};target_domain={contract.ROOT_DOMAIN}",
        )
        dcsync = _decision(
            f"positive-{index}-dcsync",
            "dcsync-krbtgt",
            f"domain={contract.ROOT_DOMAIN};account=krbtgt",
        )
        decisions = [context, dcsync]
        transactions = [
            _transaction(
                context["decision_id"],
                capability="forge-golden-ticket",
                commands=["ticket_cache_add", "shell"],
            ),
            _transaction(
                dcsync["decision_id"],
                capability="dcsync-krbtgt",
                commands=["dcsync"],
            ),
        ]
        achieved_effects = [
            f"da:{contract.ROOT_DOMAIN}",
            f"kerberos-context:{contract.ROOT_DOMAIN}@callback:{contract.CANONICAL_CALLBACK_ID}",
            f"krbtgt-hash:{contract.ROOT_DOMAIN}",
        ]
    return {
        "scenario": contract.SCENARIO_NAME,
        "phase7_manifest_hash": contract.sealed_manifest()["manifest_hash"],
        "phase7_topology_hash": contract.topology_hash(),
        "phase7_control": control,
        "phase7_attempt_index": index,
        "objective_proven": control == "positive",
        "clean_stop": True,
        "decisions": decisions,
        "transactions": transactions,
        "achieved_effects": achieved_effects,
        "ts_iso": f"2026-07-15T00:00:0{index}",
        "chat_channel_id": index,
        "chat_request_id": index,
    }


def _valid_rows() -> list[dict]:
    return [
        _row("positive", index=1),
        _row("positive", index=2),
        _row("positive", index=3),
    ]


def _valid_graph_control_report() -> dict:
    evidence = contract.new_graph_evidence_manifest([
        contract.make_graph_observation(
            label="graph-only-control",
            graph_facts=contract._synthetic_initial_graph_facts(),
            collected_domains=list(contract.REQUIRED_COLLECTED_DOMAINS),
        )
    ])
    return contract.validate_graph_control_evidence(evidence)


def test_phase7_contract_freezes_exact_topology_claims_and_controls():
    spec = contract.TRUST_CONTEXT_CORROBORATION
    manifest = contract.sealed_manifest()

    assert spec.name == "sage-trust-context-corroboration-v2"
    assert spec.range_id == "SAGETRUST20260715"
    assert spec.root_domain == "branch.local"
    assert spec.child_domain == "zeta.branch.local"
    assert spec.trusted_domain == "alpha.local"
    assert spec.foothold_identity == r"ZETA\user1"
    assert spec.positive_repetitions == 3
    assert spec.negative_controls == ("graph-only", "missing-context", "stale-callback")
    assert manifest["manifest_hash"].startswith("sha256:")
    assert manifest["countable_requirements"]["direct_graph_only_replication_is_comparator_not_negative"] is True
    assert manifest["countable_requirements"]["live_rows_are_positive_only"] is True
    assert manifest["countable_requirements"]["graph_only_control_uses_collection_only_scopes"] == [
        "current-forest",
        "alpha.local",
    ]


def test_phase7_contract_validator_blocks_admin_backed_dcsync_until_fresh_context():
    report = contract.validate_trust_context_corroboration()

    assert report["passes_gate"] is True
    assert report["checks"]["all_negative_controls_block_parent_dcsync"] is True
    assert report["checks"]["missing_context_requires_current_callback_context"] is True
    assert report["checks"]["stale_callback_requires_current_callback_context"] is True
    assert report["checks"]["fresh_context_unlocks_parent_dcsync"] is True
    assert report["checks"]["direct_cross_forest_graph_only_exemption_preserved"] is True
    assert report["checks"]["cross_domain_forge_proves_context_before_any_dcsync"] is True


def test_phase7_contract_synthetic_states_preserve_direct_replication_exception():
    missing_actions = capabilities.actions_from_state(contract.missing_context_negative_state())
    direct_actions = capabilities.actions_from_state(contract.direct_cross_forest_comparator_state())

    assert not any(
        action.name.startswith("dcsync") and contract.ROOT_DOMAIN in action.target
        for action in missing_actions
    )
    assert any(
        action.name == "dcsync-krbtgt" and contract.TRUSTED_DOMAIN in action.target
        for action in direct_actions
    )


def test_phase7_targeted_collection_request_is_collection_only_alpha_expansion():
    request = contract._targeted_trusted_collection_request(contract._foothold())

    assert request.scope_domain == contract.TRUSTED_DOMAIN
    assert request.reason == "phase7-graph-only-trusted-scope-expansion"
    assert contract.TRUSTED_DOMAIN in request.support
    assert contract.CHILD_DOMAIN in request.support


def test_phase7_targeted_collection_callback_match_accepts_bare_user_display():
    bare = contract._foothold()
    bare = bare.__class__(
        callback_id=bare.callback_id,
        agent=bare.agent,
        host=f"{bare.host}.{contract.CHILD_DOMAIN}",
        forest=bare.forest,
        identity="user1",
        integrity=bare.integrity,
        alive=bare.alive,
        source=bare.source,
        timestamp=bare.timestamp,
    )
    wrong_forest = bare.__class__(
        callback_id="wrong-forest",
        agent=bare.agent,
        host=bare.host,
        forest=contract.ROOT_DOMAIN,
        identity="user1",
        integrity=bare.integrity,
        alive=bare.alive,
        source=bare.source,
        timestamp=bare.timestamp,
    )

    assert contract._matching_live_footholds([bare, wrong_forest]) == [bare]


def test_phase7_graph_control_report_grounds_negative_replay_in_live_surface_shape():
    report = _valid_graph_control_report()

    assert report["passes_gate"] is True
    assert report["authorization"]["positive_repetitions_authorized"] is True
    observation = report["observations"][0]
    assert observation["checks"]["graph_only_initial_surface_matches_contract"] is True
    assert observation["checks"]["graph_only_direct_alpha_comparator_present"] is True
    assert observation["checks"]["missing_context_requires_current_callback_context"] is True
    assert observation["checks"]["stale_callback_requires_current_callback_context"] is True


def test_phase7_scenario_registers_parent_dcsync_objective():
    scenarios = trust_context_corroboration_scenarios("phase7-eval")
    all_names = {scenario.name for scenario in all_scenarios("phase7-eval")}

    assert [scenario.name for scenario in scenarios] == [contract.SCENARIO_NAME]
    assert scenarios[0].domains["objective"] == contract.ROOT_DOMAIN
    assert scenarios[0].domains["child"] == contract.ROOT_DOMAIN
    assert contract.SCENARIO_NAME in all_names


def test_phase7_live_validator_requires_three_post_context_positive_proofs_and_passed_controls():
    report = contract.validate_live_rows(_valid_rows(), _valid_graph_control_report())

    assert report["passes_gate"] is True
    assert report["authorization"]["phase7_complete"] is True
    assert report["checks"]["exact_three_positive_rows"] is True
    assert report["checks"]["graph_control_report_passes"] is True
    assert report["checks"]["positive_attempt_indices_exact"] is True
    assert report["checks"]["three_post_context_mythic_credential_proofs"] is True
    assert report["checks"]["zero_premature_parent_dcsync_exposure"] is True


def test_phase7_live_validator_fails_closed_on_premature_parent_dcsync():
    rows = _valid_rows()
    bad = copy.deepcopy(rows[0])
    bad["decisions"].insert(
        0,
        _decision("premature-dcsync", "dcsync-krbtgt", f"domain={contract.ROOT_DOMAIN};account=krbtgt"),
    )
    rows[0] = bad

    report = contract.validate_live_rows(rows, _valid_graph_control_report())

    assert report["passes_gate"] is False
    assert report["checks"]["zero_premature_parent_dcsync_exposure"] is False


def test_phase7_live_validator_does_not_misclassify_child_dcsync_as_parent_dcsync():
    rows = _valid_rows()
    child_dcsync = _decision(
        "child-dcsync",
        "dcsync-krbtgt",
        f"domain={contract.CHILD_DOMAIN};account=krbtgt",
    )
    rows[0]["decisions"].insert(0, child_dcsync)
    rows[0]["transactions"].insert(
        0,
        _transaction(child_dcsync["decision_id"], capability="dcsync-krbtgt", commands=["dcsync"]),
    )

    report = contract.validate_live_rows(rows, _valid_graph_control_report())

    assert report["passes_gate"] is True
    assert report["rows"][0]["checks"]["no_premature_parent_dcsync"] is True
    assert [decision[1]["decision_id"] for decision in report["rows"][0]["parent_dcsync_decisions"]] == [
        "positive-1-dcsync",
    ]


def test_phase7_live_validator_fails_closed_on_internal_parent_forge_dcsync_proof():
    rows = _valid_rows()
    bad = copy.deepcopy(rows[0])
    bad["transactions"][0]["child_tasks"].append({
        "task_id": "task-internal-parent-dcsync",
        "command": "dcsync",
        "terminal_status": "completed",
    })
    rows[0] = bad

    report = contract.validate_live_rows(rows, _valid_graph_control_report())

    assert report["passes_gate"] is False
    assert report["rows"][0]["checks"]["parent_forge_never_uses_internal_dcsync_proof"] is False


def test_phase7_live_validator_rejects_negative_rows_as_live_evidence():
    rows = _valid_rows()
    rows.append(_row("graph-only", index=4))

    report = contract.validate_live_rows(rows, _valid_graph_control_report())

    assert report["passes_gate"] is False
    assert report["rows"][-1]["checks"]["control_is_positive_live_row"] is False
