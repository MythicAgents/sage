"""Focused tests for the first bounded replay hill-climb iteration."""
from __future__ import annotations

from pathlib import Path

from ai.hillclimb import policy_replay_hillclimb_iteration as iteration


def test_hillclimb_iteration_halts_when_claimed_mechanism_needs_unscorable_behavior():
    report = iteration.run_hillclimb_iteration()

    assert report["passes_gate"] is True
    assert report["checks"]["candidate_is_single_variable_diff"] is True
    assert report["aggregate"]["baseline_total_score"] == 8.0
    assert report["aggregate"]["candidate_total_score"] == 7.0
    assert report["aggregate"]["score_delta"] == 1.0
    assert report["aggregate"]["changed_case_ids"] == ["gpo-dc-scope-late-blocker"]
    assert report["aggregate"]["improved_case_ids"] == ["gpo-dc-scope-late-blocker"]
    assert report["decision"]["keep_candidate"] is False
    assert report["decision"]["action"] == "halt_at_live_boundary"
    assert report["decision"]["disposition"] == "unscorable_new_behavior"
    assert report["decision"]["retain_artifact_for_review"] is False
    assert report["decision"]["typed_verdict"]["promotion_evidence_passed"] is False
    assert report["aggregate"]["training_exposure"]["exercised_family_ids"] == [
        "gpo-directory",
        "managed-local-admin",
        "replication-kerberos",
    ]
    assert report["aggregate"]["training_exposure"]["ties_counted"] is True
    assert report["decision"]["runtime_promotion_authorized"] is False
    assert report["iteration"]["verifier_hash"].startswith("sha256:")


def test_hillclimb_iteration_reverts_when_threshold_is_not_cleared():
    report = iteration.run_hillclimb_iteration(acceptance_threshold=2.0)

    assert report["passes_gate"] is True
    assert report["aggregate"]["score_delta"] == 1.0
    assert report["decision"]["keep_candidate"] is False
    assert report["decision"]["action"] == "halt_at_live_boundary"


def test_hillclimb_candidate_source_has_no_current_corpus_case_literals():
    source = Path(iteration.__file__).read_text(encoding="utf-8")

    assert "replication-visible-cost" not in source
    assert "ca-export-replanning" not in source
    assert "gpo-dc-scope-late-blocker" not in source
    assert "range.local" not in source
