from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ai.hillclimb import full_frontier_t3
from ai.langgraph import policy


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT.parents[1] / ".venv" / "bin" / "python"


@dataclass(frozen=True)
class _Response:
    content: str
    response_metadata: dict


def _preferred_maps():
    _stored, contexts, _validation = full_frontier_t3._load_contexts()  # type: ignore[attr-defined]
    preferred = {}
    weak = {}
    for context in contexts:
        key = frozenset(context.semantic_ids)
        preferred[key] = context.preferred_ids[0]
        weak[key] = next(
            candidate_id
            for candidate_id in context.semantic_ids
            if candidate_id in context.observed_metrics and candidate_id not in context.preferred_ids
        )
    return preferred, weak


def _treatments():
    return [
        full_frontier_t3.ModelTreatment("weak", "fixture", "weak-model", temperature=0.0),
        full_frontier_t3.ModelTreatment("strong", "fixture", "strong-model", temperature=0.0),
    ]


def _invoker_factory(*, include_metadata: bool = True):
    preferred, weak = _preferred_maps()

    def factory(treatment: full_frontier_t3.ModelTreatment):
        def invoke(request: dict):
            candidate_ids = list(request["semantic_candidate_ids"])
            selected_id = (
                preferred[frozenset(candidate_ids)]
                if treatment.name == "strong"
                else weak[frozenset(candidate_ids)]
            )
            if request["selection_contract"] == policy.SELECTION_CONTRACT_HYBRID:
                payload = {
                    "disposition": "select",
                    "candidate_id": selected_id,
                    "rationale": "fixture semantic choice",
                    "confidence": 1.0,
                    "expected_evidence": "fixture",
                }
            else:
                index = candidate_ids.index(selected_id)
                selected = request["current_admissible_actions"][index]
                payload = {
                    "disposition": "select",
                    "capability": selected["name"],
                    "target": selected["target"],
                    "rationale": "fixture semantic choice",
                    "confidence": 1.0,
                    "expected_evidence": "fixture",
                }
            metadata = (
                {
                    "model_provider": "fixture",
                    "model_name": f"{treatment.name}-effective-model",
                }
                if include_metadata
                else {}
            )
            return _Response(content=json.dumps(payload), response_metadata=metadata)

        return invoke

    return factory


def test_phase5_offline_falsifier_enumerates_all_permutations_and_fails_closed_on_current_corpus():
    report = full_frontier_t3.build_phase5_report()

    assert report["verdict"] == "benchmark_nondiscriminating"
    assert report["promotion_evidence_authorized"] is False
    assert report["null_seam"]["passes_gate"] is True
    assert report["deterministic_falsifier"]["deterministic_reproducers"] == ["modeled-reachability"]
    assert report["checks"]["all_permutations_enumerated"] is True
    assert report["checks"]["preferred_action_placed_at_every_position"] is True
    assert report["checks"]["only_independently_observed_branches_scored"] is True
    assert report["checks"]["raw_and_rejection_evidence_available"] is False
    cases = {case["id"]: case for case in report["deterministic_falsifier"]["cases"]}
    assert cases["replication-visible-cost"]["permutation_count"] == 2
    assert cases["ca-export-replanning"]["permutation_count"] == 6
    assert cases["gpo-dc-scope-late-blocker"]["permutation_count"] == 2
    assert cases["ca-export-replanning"]["preferred_positions_covered"] == [0, 1, 2]
    assert cases["replication-visible-cost"]["retained_policy_replays"]["hybrid"]["selected_candidate_id"]
    assert cases["replication-visible-cost"]["retained_policy_replays"]["llm"]["selected_candidate_id"]
    assert cases["ca-export-replanning"]["raw_admissible_audit"]["reason_codes"] == [
        "legacy_packet_missing_raw_frontier",
        "legacy_packet_missing_rejection_reasons",
    ]
    assert any(
        not row["controls"]["always-first"]["scored_from_independently_observed_branch"]
        for row in cases["ca-export-replanning"]["permutations"]
    )


def test_phase5_null_branch_seam_proves_branch_stop_and_singleton_kernel_ownership():
    report = full_frontier_t3.run_null_branch_seam()

    assert report["passes_gate"] is True
    assert report["null_branch"]["executed_targets"] == []
    assert report["enabled_branch"]["executed_targets"] == ["target-b"]
    assert report["null_singleton"]["executed_targets"] == ["target-singleton"]
    assert report["checks"] == {
        "null_branch_executes_no_action": True,
        "null_branch_stops": True,
        "null_branch_is_model_owned_opportunity": True,
        "enabled_branch_selects_semantic_candidate": True,
        "singleton_remains_kernel_owned": True,
    }


def test_phase5_t3_matrix_uses_semantic_ids_distinct_response_backends_and_empirical_only_scoring():
    report = full_frontier_t3.build_phase5_report(
        run_model_matrix=True,
        treatments=_treatments(),
        invoker_factory=_invoker_factory(),
    )
    t3 = report["model_strength_t3"]

    assert t3["checks"]["temperature_zero"] is True
    assert t3["checks"]["five_samples_per_real_cell"] is True
    assert t3["checks"]["matched_packets_and_schemas"] is True
    assert t3["checks"]["response_schema_success_rate_100_percent"] is True
    assert t3["checks"]["response_provenance_success_rate_100_percent"] is True
    assert t3["checks"]["effective_weak_and_strong_backends_are_distinct"] is True
    assert t3["checks"]["semantic_order_invariance_100_percent"] is True
    assert t3["checks"]["strong_chooses_empirical_best_at_least_80_percent"] is True
    assert t3["checks"]["strong_beats_best_positional_control"] is True
    assert t3["checks"]["strong_beats_weak"] is True
    assert t3["effective_backend_identity"] == {
        "weak": ["fixture:weak-effective-model"],
        "strong": ["fixture:strong-effective-model"],
    }
    assert t3["primary_hybrid"]["strong"]["empirical_best_accuracy"] == 1.0
    assert t3["primary_hybrid"]["weak"]["empirical_best_accuracy"] == 0.0
    assert t3["annotation_ablations"]["without_reason"]["semantic_selection_agreement_rate"] == 1.0
    assert any(
        row["case_id"] == "ca-export-replanning"
        and row["scored_from_independently_observed_branch"] is False
        for row in t3["rows"]
        if row["treatment"] == "weak"
    ) is False
    assert all(row["selected_candidate_id"] for row in t3["rows"])


def test_phase5_t3_rejects_configured_only_backend_labels_without_response_provenance():
    report = full_frontier_t3.build_phase5_report(
        run_model_matrix=True,
        treatments=_treatments(),
        invoker_factory=_invoker_factory(include_metadata=False),
        ablations=("intact",),
    )
    t3 = report["model_strength_t3"]

    assert t3["checks"]["response_provenance_success_rate_100_percent"] is False
    assert t3["checks"]["effective_weak_and_strong_backends_are_distinct"] is False
    assert t3["effective_backend_identity"] == {"weak": [], "strong": []}


def test_phase5_t3_invoker_reuses_existing_sage_ca_bundle_without_overriding_explicit_env(monkeypatch, tmp_path):
    combined = tmp_path / "combined-bundle.pem"
    custom = tmp_path / "bundle.pem"
    combined.write_text("combined", encoding="utf-8")
    custom.write_text("custom", encoding="utf-8")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    selected = full_frontier_t3.configure_sage_ssl_cert_file(
        combined_bundle_path=combined,
        custom_bundle_path=custom,
    )

    assert selected == str(combined.resolve())
    assert selected == full_frontier_t3.os.environ["SSL_CERT_FILE"]

    monkeypatch.setenv("SSL_CERT_FILE", "/operator/explicit.pem")
    assert full_frontier_t3.configure_sage_ssl_cert_file(
        combined_bundle_path=combined,
        custom_bundle_path=custom,
    ) == "/operator/explicit.pem"


def test_phase5_cli_emits_offline_negative_verdict_without_model_calls():
    result = subprocess.run(
        [str(PY), "-m", "ai.hillclimb", "phase5-full-frontier-t3"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout.split("\nVERDICT:", 1)[0])
    assert payload["kind"] == "phase5_full_frontier_t3"
    assert payload["verdict"] == "benchmark_nondiscriminating"
    assert payload["model_strength_t3"]["disposition"] == "skipped_current_packets_nondiscriminating"
