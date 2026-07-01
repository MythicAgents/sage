"""Focused tests for frontier replay's benign-omission classifier."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.hillclimb import frontier_replay as fr  # noqa: E402
from ai.langgraph import capabilities as cap  # noqa: E402
from ai.langgraph import engagement_state as es  # noqa: E402


def _hop(technique: str, effects: list[str]) -> es.Hop:
    return es.Hop(
        id=technique,
        technique=technique,
        target="",
        effect=effects[0],
        status="achieved",
        evidence={},
        preconditions=[],
        satisfied_effects=effects,
        source="test",
        timestamp="",
    )


def test_domain_alias_admin_proof_is_an_ordering_artifact():
    truth = _hop(
        "capability:gpo-controlled-system-exec",
        ["system-exec:gpo:policy-a@child.example.test", "da:child.example.test"],
    )
    prefix = [_hop("domain-admin-membership-check", ["da:child"])]

    assert fr._benign_reason(truth, prefix, []) == "ordering_artifact"


def test_legacy_proof_row_nested_under_selected_capability_is_benign():
    truth = _hop("dcsync", ["krbtgt-hash:root.example.test"])
    frontier = [
        cap.CapabilityAction(
            name="forge-golden-ticket",
            target="domain=child.example.test;target_domain=root.example.test",
            effects=["da:root.example.test"],
        )
    ]
    tail = [_hop("capability:forge-golden-ticket", ["da:root.example.test"])]

    assert fr._benign_reason(truth, [], tail, frontier=frontier) == "nested_capability_proof"


def test_explicit_capability_gap_is_not_masked_as_nested_proof():
    truth = _hop("capability:dcsync-krbtgt", ["krbtgt-hash:root.example.test"])
    frontier = [
        cap.CapabilityAction(
            name="forge-golden-ticket",
            target="domain=child.example.test;target_domain=root.example.test",
            effects=["da:root.example.test"],
        )
    ]
    tail = [_hop("capability:forge-golden-ticket", ["da:root.example.test"])]

    assert fr._benign_reason(truth, [], tail, frontier=frontier) == ""
