"""Offline probes for the next-attempt campaign stop-loss boundary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import experiment_contracts as contracts  # noqa: E402
import run_gauge_live as rgl  # noqa: E402


def _attempt(
    attempt_id: str,
    *,
    child_tasks: int = 0,
    total_tokens: int | None = None,
    evidence_state: str = "admitted",
    supersedes: str = "",
    amendment_reason: str = "",
) -> contracts.AttemptContract:
    return contracts.AttemptContract(
        attempt_id=attempt_id,
        experiment_id="scenario",
        campaign_id="scenario",
        treatment_id="harness",
        evidence_state=evidence_state,
        supersedes_attempt_id=supersedes,
        amendment_reason=amendment_reason,
        source={"ordinary_result": True},
        cost=contracts.CostMeasurement(
            child_task_count=child_tasks,
            total_tokens=total_tokens,
        ),
    )


def _result_row(attempt: contracts.AttemptContract) -> dict:
    """Make a retained ordinary-result row with its hash over the full record."""
    row = {
        **attempt.to_dict(),
        "scenario": "scenario",
        "record_type": "result",
    }
    row["record_hash"] = contracts.content_hash(row)
    return row


def test_campaign_limit_includes_other_scenarios_when_campaign_is_explicit(tmp_path):
    row = _result_row(_attempt("other-scenario-spend", child_tasks=3))
    row.update(scenario="other-scenario", campaign_id="shared-campaign")
    row["record_hash"] = contracts.content_hash({k: v for k, v in row.items() if k != "record_hash"})
    path = tmp_path / "campaign.jsonl"
    path.write_text(json.dumps(row) + "\n")
    cfg = rgl.Config(development_results_path=path, campaign_id="shared-campaign",
                     campaign_limits=contracts.CostLimits(child_task_count=3))
    with pytest.raises(SystemExit, match="limit_exhausted"):
        rgl.run_side(cfg, "harness", "new-scenario")


def test_generic_row_stoploss_reads_bound_storage_before_client(tmp_path, monkeypatch):
    bound = tmp_path / "authoritative.jsonl"
    bound.write_text(json.dumps(_result_row(_attempt("already-spent", child_tasks=1))) + "\n")

    class Protocol:
        def __init__(self, *args, **kwargs):
            pass

        def validate_request(self, **kwargs):
            pytest.fail("exhausted bound campaign should stop before request validation or clients")

    monkeypatch.setattr(rgl, "DevelopmentRowProtocol", Protocol)
    monkeypatch.setattr(rgl, "_resolved_harness_model_route", lambda cfg: {})
    monkeypatch.setattr(rgl, "_bind_development_row_storage",
                        lambda cfg, protocol: setattr(cfg, "development_results_path", bound))
    cfg = rgl.Config(measurement_kind="row", row_contract_path="fixture.json", row_contract_sha256="fixture",
                     row_id="row-1", development_results_path=tmp_path / "unrelated-empty.jsonl",
                     campaign_limits=contracts.CostLimits(child_task_count=1))
    with pytest.raises(SystemExit, match="limit_exhausted"):
        rgl.run_side(cfg, "harness", "scenario")


@pytest.mark.parametrize("existing", [False, True], ids=["missing-history", "empty-history"])
def test_run_side_blocks_unpriced_dollar_limit_before_constructing_client(tmp_path, monkeypatch, existing):
    results = tmp_path / "results.jsonl"
    if existing:
        results.write_text("", encoding="utf-8")
    opened = []
    monkeypatch.setattr(rgl.live_seams, "default_mythic_client", lambda: opened.append(True))
    cfg = rgl.Config(
        development_results_path=results,
        campaign_limits=contracts.CostLimits(monetary_cost_usd=1.0),
    )

    with pytest.raises(SystemExit, match="required_axis_unavailable"):
        rgl.run_side(cfg, "harness", "scenario")

    assert opened == []


def test_exact_consumed_limit_blocks_the_next_attempt():
    attempt = _attempt("spent-once", child_tasks=3)

    disposition = contracts.campaign_start_disposition(
        [attempt],
        limits=contracts.CostLimits(child_task_count=3),
        required_axes=frozenset({"child_task_count"}),
    )

    assert not disposition.may_start
    assert disposition.reason == "limit_exhausted"
    assert disposition.summary.child_task_count == 3


def test_required_unknown_axis_is_unavailable_and_total_only_tokens_remain_unsplit():
    attempt = _attempt("total-only", total_tokens=17)

    disposition = contracts.campaign_start_disposition(
        [attempt],
        limits=contracts.CostLimits(total_tokens=18, input_tokens=20),
        required_axes=frozenset({"total_tokens", "input_tokens"}),
    )

    assert not disposition.may_start
    assert disposition.reason == "required_axis_unavailable"
    assert disposition.unavailable_axes == ("input_tokens",)
    assert disposition.summary.total_tokens == 17
    assert disposition.summary.input_tokens is None
    assert disposition.summary.output_tokens is None


def test_missing_retained_task_and_wall_measurements_cannot_pass_declared_limits():
    row = _result_row(_attempt("missing-cost"))
    row["cost"] = {}
    row["record_hash"] = contracts.content_hash({key: value for key, value in row.items() if key != "record_hash"})

    parsed = contracts.CostMeasurement.from_dict(row["cost"], schema_version=2)
    disposition = contracts.campaign_start_disposition(
        [row],
        limits=contracts.CostLimits(child_task_count=1, wall_time_seconds=1.0),
    )

    assert parsed.child_task_count is None
    assert parsed.wall_time_seconds is None
    assert not disposition.may_start
    assert disposition.reason == "required_axis_unavailable"
    assert disposition.unavailable_axes == ("child_task_count", "wall_time_seconds")


def test_run_side_charges_valid_amendment_once_at_exact_limit_before_client(tmp_path, monkeypatch):
    original = _attempt("original", child_tasks=1)
    amendment = _attempt(
        "amendment",
        child_tasks=1,
        supersedes="original",
        amendment_reason="metadata correction",
    )
    results = tmp_path / "results.jsonl"
    results.write_text(
        "\n".join(json.dumps(row) for row in (_result_row(original), _result_row(amendment))) + "\n",
        encoding="utf-8",
    )
    opened = []
    monkeypatch.setattr(rgl.live_seams, "default_mythic_client", lambda: opened.append(True))
    cfg = rgl.Config(
        development_results_path=results,
        campaign_limits=contracts.CostLimits(child_task_count=1),
        campaign_required_axes=frozenset({"child_task_count"}),
    )

    with pytest.raises(SystemExit, match="limit_exhausted"):
        rgl.run_side(cfg, "harness", "scenario")

    assert opened == []


def test_run_side_continues_to_the_client_when_retained_spend_is_below_limit(tmp_path, monkeypatch):
    """The stop-loss check blocks only exhausted starts; this sentinel is the next boundary."""
    class ClientConstructed(Exception):
        pass

    opened = []

    def _client():
        opened.append(True)
        raise ClientConstructed

    monkeypatch.setattr(rgl.live_seams, "default_mythic_client", _client)
    cfg = rgl.Config(
        development_results_path=tmp_path / "results.jsonl",
        campaign_limits=contracts.CostLimits(child_task_count=1),
        campaign_required_axes=frozenset({"child_task_count"}),
    )

    with pytest.raises(ClientConstructed):
        rgl.run_side(cfg, "harness", "child-da")

    assert opened == [True]


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        pytest.param(
            lambda: [_result_row(_attempt("burned", child_tasks=1, evidence_state="burned"))],
            "limit_exhausted",
            id="burned-spend-remains-charged",
        ),
        pytest.param(
            lambda: [
                _result_row(_attempt("original", child_tasks=1)),
                _result_row(_attempt("changed", child_tasks=2, supersedes="original", amendment_reason="bad rewrite")),
            ],
            "invalid_ledger",
            id="amendment-cannot-change-cost",
        ),
    ],
)
def test_run_side_refuses_burned_or_invalid_lifecycle_before_client(tmp_path, monkeypatch, rows, reason):
    results = tmp_path / "results.jsonl"
    results.write_text("\n".join(json.dumps(row) for row in rows()) + "\n", encoding="utf-8")
    opened = []
    monkeypatch.setattr(rgl.live_seams, "default_mythic_client", lambda: opened.append(True))
    cfg = rgl.Config(
        development_results_path=results,
        campaign_limits=contracts.CostLimits(child_task_count=1),
        campaign_required_axes=frozenset({"child_task_count"}),
    )

    with pytest.raises(SystemExit, match=reason):
        rgl.run_side(cfg, "harness", "scenario")

    assert opened == []
