"""Offline bounds for completed-call Phoenix cost estimates."""
from __future__ import annotations

import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.hillclimb import bare_runner, phoenix_report, run_gauge_live as rgl  # noqa: E402


def _rates(tmp_path: Path, *, provider="provider-a", model="model-a", maximums=True) -> Path:
    entry = {
        "provider": provider, "model": model,
        "input_usd_per_token": 1.0, "output_usd_per_token": 2.0,
        "cache_read_usd_per_token": 0.1, "cache_write_usd_per_token": 0.5,
    }
    if maximums:
        entry.update(max_input_tokens=9, max_output_tokens=5)
    path = tmp_path / "rates.json"
    path.write_text(json.dumps({
        "schema": phoenix_report.RATE_SCHEMA,
        "price_provenance": {"source_id": "rates", "artifact_path": "", "artifact_sha256": "", "route_model_identity": f"{provider}:{model}", "currency": "USD", "effective_date_or_version": "v1"},
        "models": [entry],
    }), encoding="utf-8")
    return path


def test_completed_call_ceiling_bounds_crossing_response_and_blocks_second_action(tmp_path):
    rates = _rates(tmp_path)
    ceiling = phoenix_report.completed_call_estimate_ceiling(
        rates, provider="provider-a", model="model-a", token_ceiling=10,
    )
    assert ceiling["estimated_run_ceiling_usd"] == "37.000000000"
    actions, decisions = [], iter([
        {"tool": "first", "args": {}, "_usage": {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}},
        {"tool": "must-not-run", "args": {}, "_usage": {"input_tokens": 0, "output_tokens": 5, "total_tokens": 5}},
    ])
    result = bare_runner.BareModelRunner(
        lambda *_args: next(decisions), lambda call: actions.append(call["tool"]) or "ok", token_ceiling=10,
    ).run("objective")
    actual = Decimal(4) * Decimal("1") + Decimal(5) * Decimal("2") + Decimal(5) * Decimal("2")
    assert actual <= Decimal(ceiling["estimated_run_ceiling_usd"])
    assert actual > Decimal(10) * Decimal("2")  # even C × max-rate misses the crossing response
    assert result.total_tokens == 14
    assert result.token_budget_overshoot is True
    assert actions == ["first"]


@pytest.mark.parametrize("provider,model,maximums,token_ceiling", [
    ("wrong", "model-a", True, 10), ("provider-a", "wrong", True, 10),
    ("provider-a", "model-a", False, 10), ("provider-a", "model-a", True, 0),
])
def test_completed_call_ceiling_requires_exact_rates_positive_maxima_and_ceiling(
    tmp_path, provider, model, maximums, token_ceiling,
):
    rates = _rates(tmp_path, maximums=maximums)
    with pytest.raises(ValueError):
        phoenix_report.completed_call_estimate_ceiling(
            rates, provider=provider, model=model, token_ceiling=token_ceiling,
        )


def test_snapshot_retains_quoted_ceiling_and_rate_source_hash(tmp_path):
    rates = _rates(tmp_path)
    cfg = rgl.Config(
        measurement_kind="row", policy_mode="llm", model_token_budget=10,
        run_estimate_rates=rates,
    )
    snapshot = rgl.episode_protocol_snapshot(
        cfg, side="bare", scenario_name="ordinary", model_route={"provider": "provider-a", "model": "model-a"},
    )
    quoted = snapshot["estimated_run_ceiling"]
    assert quoted["estimated_run_ceiling_usd"] == "37.000000000"
    assert quoted["pricing_sha256"] == hashlib.sha256(rates.read_bytes()).hexdigest()
    assert quoted["pricing_path"] == str(rates.resolve())


@pytest.mark.parametrize("measurement_kind,policy_mode,null_model", [
    ("diagnostic", "llm", False), ("row", "symbolic", False), ("row", "llm", True),
])
def test_run_side_rejects_estimate_ineligible_modes_before_client(
    tmp_path, monkeypatch, measurement_kind, policy_mode, null_model,
):
    cfg = rgl.Config(
        measurement_kind=measurement_kind, policy_mode=policy_mode, null_model=null_model,
        model_token_budget=10, run_estimate_rates=_rates(tmp_path),
        model_provider="provider-a", model_id="model-a",
    )
    monkeypatch.setattr(rgl.live_seams, "default_mythic_client", lambda: pytest.fail("client must not open"))
    with pytest.raises(SystemExit, match="run estimate requires a bounded raw or controller LLM row"):
        rgl.run_side(cfg, "bare", "ordinary")


@pytest.mark.parametrize("provider,model,maximums", [
    ("wrong", "model-a", True), ("provider-a", "model-a", False),
])
def test_run_side_rejects_bad_rate_selection_before_client(tmp_path, monkeypatch, provider, model, maximums):
    opened = []
    cfg = rgl.Config(
        measurement_kind="row", policy_mode="llm", model_token_budget=10,
        run_estimate_rates=_rates(tmp_path, maximums=maximums), model_provider=provider, model_id=model,
    )
    monkeypatch.setattr(rgl.live_seams, "default_mythic_client", lambda: opened.append(True))
    with pytest.raises(SystemExit, match="run estimate requires exact model rates"):
        rgl.run_side(cfg, "bare", "ordinary")
    assert opened == []
