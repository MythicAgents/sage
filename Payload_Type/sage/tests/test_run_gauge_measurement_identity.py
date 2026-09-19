"""Offline producer and reporting guards for run-gauge measurement labels."""
import contextlib
import io
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import run_gauge_live as rgl  # noqa: E402
from fitness import ScoreCard  # noqa: E402


def _card(*, side="bare", backend="openai:gpt-test", capability=0.0):
    proven = capability > 0
    return ScoreCard(
        scenario="identity-scenario", verifier_hash="sha256:test", furthest_milestone="FOOTHOLD",
        capability=capability, milestones_reached=0, milestones={"FOOTHOLD": True},
        substring_score=0.0, metric_capability_gap=0.0, tool_calls=1, model_calls=1,
        productive_action_ratio=0.0, turn_efficiency=0.0, status="pass", recursion_deaths=0,
        error_count=0, total_tokens=7, wall_seconds=1.0, policy_mode=("naive" if side == "bare" else "llm"),
        model_provider="openai", model_id="gpt-test", effective_backends=[backend],
        policy_identity_valid=True,
        objective_proven=proven, objective_proof_disposition=("proven" if proven else "disproven"),
    )


def _config(tmp_path):
    return rgl.Config(
        development_results_path=tmp_path / "results.jsonl", protocol_id="protocol-1", pair_id="pair-1",
        logical_seed=0, model_version="2026-09", source_version="tree:abc", max_output_tokens=64,
        model_api_endpoint="https://synthetic.invalid",
    )


def test_identity_producer_is_complete_only_when_observed_route_matches(tmp_path):
    cfg = _config(tmp_path)
    card = _card()
    identity, labels = rgl._measurement_identity_for_run(cfg, card, "bare")
    assert labels["performance_assertion_allowed"] is True
    assert identity == labels["identities"][0]
    assert identity["backend_route"].startswith("openai:gpt-test#endpoint:")
    assert identity["logical_seed"] == 0
    assert identity["wrapper_revision"].startswith("sha256:")

    identity, labels = rgl._measurement_identity_for_run(cfg, _card(backend="openai:wrong"), "bare")
    assert identity is None
    assert labels["performance_assertion_allowed"] is False
    assert any("unknown measurement label" in reason for reason in labels["identity_failures"])


def test_compare_refuses_identity_free_or_stale_rows_but_keeps_diagnostics(tmp_path):
    cfg = _config(tmp_path)
    rows = [
        {"record_type": "result", "scenario": "identity-scenario", "side": "bare", "card": asdict(_card()), "cost_countable": {}},
        {"record_type": "result", "scenario": "identity-scenario", "side": "harness", "card": asdict(_card(side="harness", capability=1.0)), "cost_countable": {}},
    ]
    cfg.results_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        rgl.compare(cfg, "identity-scenario")
    output = stream.getvalue()
    assert "performance assertion refused" in output
    assert "delta=" not in output

    for row in rows:
        card = ScoreCard(**row["card"])
        identity, labels = rgl._measurement_identity_for_run(cfg, card, row["side"])
        card.measurement_identity, card.measurement = identity, labels
        row["card"] = asdict(card)
        row["measurement_identity"] = identity
    cfg.results_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        rgl.compare(cfg, "identity-scenario")
    assert "delta=" in stream.getvalue()

    rows[0]["card"]["measurement_identity"]["model_version"] = "stale-version"
    cfg.results_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        rgl.compare(cfg, "identity-scenario")
    assert "stale measurement label: model_version" in stream.getvalue()


def test_compare_refuses_mismatched_envelope_or_incompatible_retained_producers(tmp_path):
    cfg = _config(tmp_path)
    rows = []
    for side, capability in (("bare", 0.0), ("harness", 1.0)):
        card = _card(side=side, capability=capability)
        label, measurement = rgl._measurement_identity_for_run(cfg, card, side)
        card.measurement_identity, card.measurement = label, measurement
        rows.append({"record_type": "result", "scenario": "identity-scenario", "side": side,
                     "card": asdict(card), "measurement_identity": label, "cost_countable": {}})

    rows[0]["measurement_identity"] = {**rows[0]["measurement_identity"], "source_version": "outer-stale"}
    cfg.results_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        rgl.compare(cfg, "identity-scenario")
    assert "ScoreCard labels differ" in stream.getvalue()
    assert "delta=" not in stream.getvalue()

    rows[0]["measurement_identity"] = rows[0]["card"]["measurement_identity"]
    rows[1]["card"]["measurement_identity"]["model_version"] = "different-but-well-formed"
    rows[1]["measurement_identity"]["model_version"] = "different-but-well-formed"
    cfg.results_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        rgl.compare(cfg, "identity-scenario")
    assert "stale measurement label: model_version" in stream.getvalue()
    assert "delta=" not in stream.getvalue()


def test_comparison_labels_accept_two_pairs_with_distinct_seeds_and_refuse_bad_arm_membership(tmp_path):
    cfg = _config(tmp_path)
    cards, records = [], []
    for pair_id, seed in (("pair-a", 0), ("pair-b", 1)):
        for side, capability in (("bare", 0.0), ("harness", 1.0)):
            card = _card(side=side, capability=capability)
            label, measurement = rgl._measurement_identity_for_run(cfg, card, side)
            label = {**label, "pair_id": pair_id, "logical_seed": seed}
            card.measurement_identity, card.measurement = label, measurement
            cards.append(card)
            records.append({"measurement_identity": label})
    labels = rgl._performance_labels(cards, records=records)
    assert labels["performance_assertion_allowed"] is True

    duplicate = [*cards]
    duplicate[-1].measurement_identity = dict(duplicate[-1].measurement_identity)
    duplicate[-1].measurement_identity["policy_arm"] = duplicate[-2].measurement_identity["policy_arm"]
    labels = rgl._performance_labels(duplicate, records=[{"measurement_identity": card.measurement_identity} for card in duplicate])
    assert labels["performance_assertion_allowed"] is False
    assert labels["identity_failures"] == ["incompatible pair membership or logical seed"]
