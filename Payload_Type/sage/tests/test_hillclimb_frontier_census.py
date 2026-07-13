import asyncio
import json

from ai.hillclimb import frontier_census as census
from ai.langgraph import capabilities


def _action(name, effect):
    return capabilities.CapabilityAction(
        name=name,
        target=name,
        effects=[effect],
        reason=f"modeled {name}",
    )


def _branching_frontier(state):
    effects = state.achieved_effects()
    if not effects:
        return [
            _action("gpo-controlled-system-exec", "lane:gpo"),
            _action("read-managed-local-admin-secret", "lane:laps"),
        ]
    if "lane:gpo" in effects and "da:essos.local" not in effects:
        return [_action("grant-directory-rights", "da:essos.local")]
    if "lane:laps" in effects and "lane:laps-stage-2" not in effects:
        return [_action("use-managed-local-admin-secret", "lane:laps-stage-2")]
    if "lane:laps-stage-2" in effects and "da:essos.local" not in effects:
        return [_action("adcs-certificate-auth", "da:essos.local")]
    return []


def test_capability_family_distinguishes_branch_lanes():
    assert census.policy.capability_family("gpo-controlled-system-exec") == "gpo-directory"
    assert census.policy.capability_family("read-managed-local-admin-secret") == "managed-local-admin"
    assert census.policy.capability_family("collect-graph") == "collection"


def test_modeled_branch_reachability_records_path_length():
    spec = census.StartSpec(
        name="synthetic",
        host="WS01",
        forest="range.local",
        identity=r"RANGE\user1",
        bootstrap_evidence="synthetic fixture",
    )
    state = census._state_for_start(spec)
    first = _branching_frontier(state)[1]

    result = asyncio.run(census.modeled_branch_reachability(
        state,
        first,
        frontier_fn=_branching_frontier,
    ))

    assert result["reachable"] is True
    assert result["modeled_transactions"] == 3
    assert [item["name"] for item in result["path"]] == [
        "read-managed-local-admin-secret",
        "use-managed-local-admin-secret",
        "adcs-certificate-auth",
    ]


def test_analyze_start_passes_only_for_branch_rich_visible_tradeoff():
    spec = census.StartSpec(
        name="synthetic",
        host="WS01",
        forest="range.local",
        identity=r"RANGE\user1",
        bootstrap_evidence="synthetic fixture",
    )

    async def refresh(state):
        return state

    report = asyncio.run(census.analyze_start(
        spec,
        refresh_fn=refresh,
        frontier_fn=_branching_frontier,
    ))

    assert report["distinct_families"] == ["gpo-directory", "managed-local-admin"]
    assert report["decision_quality"]["visible_tradeoff"] is True
    assert report["passes_gate"] is True
    assert report["proofability"]["proofable"] is True


def test_gate_rejects_two_families_without_stable_proof():
    report = {
        "distinct_families": ["gpo-directory", "managed-local-admin"],
        "reachability": [
            {"family": "gpo-directory", "reachable": True},
            {"family": "managed-local-admin", "reachable": True},
        ],
        "decision_quality": {"visible_tradeoff": True},
        "proofability": {"proofable": False},
    }

    passes, reason = census._gate_start(report)

    assert passes is False
    assert "not stable" in reason


def test_gate_rejects_missing_collected_domain_as_incomplete_input():
    report = {
        "graph_input": {
            "required_domain": "north.sevenkingdoms.local",
            "collected_domains": ["essos.local"],
            "complete_for_start": False,
        },
        "distinct_families": ["gpo-directory", "managed-local-admin"],
        "reachability": [
            {"family": "gpo-directory", "reachable": True},
            {"family": "managed-local-admin", "reachable": True},
        ],
        "decision_quality": {"visible_tradeoff": True},
        "proofability": {"proofable": True},
    }

    passes, reason = census._gate_start(report)

    assert passes is False
    assert "incomplete" in reason


def test_retained_purpose_range_start_uses_engagement_netbios_map(monkeypatch, tmp_path):
    path = tmp_path / "apollo_policy_range_ws01_callback_config.json"
    path.write_text(json.dumps({
        "config": {
            "callback": {
                "host": "WS01",
                "domain": "RANGE",
                "user": "user1",
            }
        }
    }))
    monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", '{"RANGE":"range.local"}')

    starts = census.discover_retained_starts(tmp_path)

    assert len(starts) == 1
    assert starts[0].forest == "range.local"
    assert starts[0].objective.endswith("administrative control of range.local.")
    assert starts[0].proof_scenario == "purpose-range-visible-cost"
    assert census._proofability(starts[0])["proofable"] is True


def test_retained_replication_range_start_uses_replication_proof_scenario(monkeypatch, tmp_path):
    path = tmp_path / "apollo_replication_ws01_callback_config.json"
    path.write_text(json.dumps({
        "config": {
            "callback": {
                "host": "WS01",
                "domain": "REPLICATION",
                "user": "user1",
            }
        }
    }))
    monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", '{"REPLICATION":"replication.local"}')

    starts = census.discover_retained_starts(tmp_path)

    assert len(starts) == 1
    assert starts[0].forest == "replication.local"
    assert starts[0].objective.endswith("administrative control of replication.local.")
    assert starts[0].proof_scenario == "replication-purpose-range-visible-cost"
    assert census._proofability(starts[0])["proofable"] is True


def test_run_census_labels_purpose_range_without_calling_it_goad(monkeypatch):
    spec = census.StartSpec(
        name="retained-apollo_replication_range_ws01",
        host="WS01",
        forest="replication.local",
        identity=r"REPLICATION\user1",
        proof_scenario="replication-purpose-range-visible-cost",
        bootstrap_evidence="retained callback config",
    )

    async def fake_analyze_start(*args, **kwargs):
        return {
            "start": {
                "name": spec.name,
                "proof_scenario": spec.proof_scenario,
            },
            "passes_gate": True,
        }

    async def refresh(state):
        return state

    monkeypatch.setattr(census, "analyze_start", fake_analyze_start)

    report = asyncio.run(census._run_census(
        [spec],
        refresh,
        collected_domains=["replication.local"],
        max_depth=1,
        max_nodes=1,
    ))

    assert report["selected_start"] == spec.name
    assert report["recommended_discriminator"] == "replication-purpose-range-visible-cost"
    assert report["gate_reason"] == "1 censused start(s) satisfy the branch-rich discriminator gate"


def test_run_census_preserves_goad_discriminator_for_goad_start(monkeypatch):
    spec = census.StartSpec(
        name="jorah-meereen",
        host="MEEREEN",
        forest="essos.local",
        identity=r"ESSOS\jorah.mormont",
        proof_scenario="direct-laps-objective",
        bootstrap_evidence="retained callback config",
    )

    async def fake_analyze_start(*args, **kwargs):
        return {
            "start": {
                "name": spec.name,
                "proof_scenario": spec.proof_scenario,
            },
            "passes_gate": True,
        }

    async def refresh(state):
        return state

    monkeypatch.setattr(census, "analyze_start", fake_analyze_start)

    report = asyncio.run(census._run_census(
        [spec],
        refresh,
        collected_domains=["essos.local"],
        max_depth=1,
        max_nodes=1,
    ))

    assert report["recommended_discriminator"] == "goad"
