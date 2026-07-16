from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ai.hillclimb import phase15_r5_retrospective_falsifiers as phase15


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT.parents[1] / ".venv" / "bin" / "python"


def test_phase15_rejects_h3_when_predecision_deterministic_controls_reproduce_hybrid():
    report = phase15.build_phase15_report(generated_at="2026-07-16T00:00:00+00:00")

    assert report["passes_gate"] is True
    assert report["canonical_case_accounting"]["retained_hybrid_model_owned_decision_occurrence_count"] == 18
    assert report["canonical_case_accounting"]["canonical_unique_predecision_case_count"] == 6
    assert report["permutation_matrix"]["total_permutation_count"] == 12
    reviewer = report["observed_model_choice_reproduction"]["reviewer_objective_target_matcher_result"]
    assert reviewer["reproduces_reviewer_result"] is True
    assert reviewer["reproduced_decision_count"] == 18
    assert report["conclusion"]["h3_disposition"] == "rejected_for_r5"
    assert report["conclusion"]["matching_or_better_deterministic_controls"] == [
        "objective_effect_aware",
        "modeled_reachability",
    ]
    assert report["conclusion"]["promotion_evidence_authorized"] is False
    assert report["conclusion"]["positive_promotion_gate_opened"] is False


def test_phase15_keeps_controls_predecision_only_and_records_llm_hybrid_strategic_equivalence():
    report = phase15.build_phase15_report(generated_at="2026-07-16T00:00:00+00:00")

    audit = report["control_input_audit"]
    assert all(audit["checks"].values())
    assert all(
        control["predecision_only"] is True
        and control["receives_full_result_row"] is False
        and control["result_derived_fields_used"] == []
        for control in audit["controls"].values()
    )
    assert report["permutation_matrix"]["control_aggregates"]["objective_effect_aware"][
        "matches_hybrid_on_every_canonical_permutation"
    ] is True
    assert report["permutation_matrix"]["control_aggregates"]["modeled_reachability"][
        "matches_hybrid_on_every_canonical_permutation"
    ] is True
    assert report["observed_path_reproduction_regret"]["controls"]["objective_effect_aware"][
        "paired_regret_values"
    ] == [0.0, 0.0]
    identifiability = report["policy_identifiability_audit"]
    assert identifiability["branch_semantic_information_projection_identical"] is True
    assert identifiability["falsifiable_branch_mechanism_difference_identified"] is False
    assert identifiability["strategic_equivalence"] is True
    assert identifiability["live_comparison_disposition"] == "cost_or_singleton_overhead_only"
    assert identifiability["singleton_ownership_audit"]["model_call_counts"] == {"llm": 1, "hybrid": 0}


def test_phase15_cli_writes_negative_only_report(tmp_path):
    output = tmp_path / "phase15.json"
    result = subprocess.run(
        [
            str(PY),
            "-m",
            "ai.hillclimb",
            "phase15-r5-retrospective-falsifiers",
            "--output",
            str(output),
            "--generated-at",
            "2026-07-16T00:00:00+00:00",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["passes_gate"] is True
    assert payload["conclusion"]["h3_disposition"] == "rejected_for_r5"
    assert payload["conclusion"]["claim_scope"] == "retrospective_r5_falsifier_only"
    assert output.with_suffix(".sha256").is_file()
    assert "VERDICT: PASS  (h3_disposition=rejected_for_r5)" in result.stdout
