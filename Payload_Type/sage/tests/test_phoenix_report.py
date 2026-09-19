"""Offline integration coverage for trace-bound Phoenix accounting estimates."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.hillclimb import accounting_cli, accounting_report, experiment_contracts, phoenix_report, reproducible_claims, run_gauge_live as rgl  # noqa: E402


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "phoenix.db"
    con = sqlite3.connect(path)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE traces (id INTEGER PRIMARY KEY, trace_id TEXT);
        CREATE TABLE spans (id INTEGER PRIMARY KEY, trace_rowid INTEGER, span_id TEXT, parent_id TEXT, span_kind TEXT, start_time TEXT, end_time TEXT, status_code TEXT, attributesJSONB TEXT, llm_token_count_prompt INTEGER, llm_token_count_completion INTEGER);
    """)
    attrs = json.dumps({"llm": {"model_name": "model-a", "provider": "provider-a", "token_count": {
        "prompt_details": {"cache_read": 4, "cache_write": 1},
        "completion_details": {"reasoning": 2},
    }}})
    con.executemany("INSERT INTO traces VALUES (?, ?)", [(1, "trace-a"), (2, "unrelated")])
    con.executemany("INSERT INTO spans VALUES (?, ?, ?, NULL, 'LLM', '1', '2', 'OK', ?, ?, ?)", [
        (1, 1, "span-a", attrs, 10, 4),
        (2, 2, "span-unrelated", attrs, 99, 99),
    ])
    con.commit()
    con.close()
    return path


def _rates(tmp_path: Path) -> Path:
    path = tmp_path / "rates.json"
    path.write_text(json.dumps({
        "schema": phoenix_report.RATE_SCHEMA,
        "price_provenance": {"source_id": "published-rate-card", "artifact_path": "", "artifact_sha256": "", "route_model_identity": "provider-a:model-a", "currency": "USD", "effective_date_or_version": "v1"},
        "models": [{"provider": "provider-a", "model": "model-a", "input_usd_per_token": 1.0, "output_usd_per_token": 2.0, "cache_read_usd_per_token": 0.1, "cache_write_usd_per_token": 0.5}],
    }), encoding="utf-8")
    return path


def _config(db: Path, rates: Path, *, method="cache_aware", capture_complete=True) -> dict:
    return {"campaign_id": "campaign", "database_path": str(db), "pricing_path": str(rates), "method": method,
            "capture_complete": capture_complete,
            "attempts": [{"attempt_id": "attempt-1", "trace_ids": ["trace-a"], "expected_model_calls": 1}]}


def _ledger(path: Path, *, burned=False, amendment=False) -> Path:
    original = experiment_contracts.AttemptContract(
        schema_version=2, attempt_id="attempt-1", campaign_id="campaign", experiment_id="experiment", treatment_id="treatment",
        evidence_state="burned" if burned else "admitted",
        cost=experiment_contracts.CostMeasurement(model_call_count=1),
    )
    rows = [{"record_hash": original.content_hash, **original.to_dict()}]
    if amendment:
        changed = experiment_contracts.AttemptContract(
            schema_version=2, attempt_id="attempt-1-amendment", campaign_id="campaign", experiment_id="experiment", treatment_id="treatment",
            evidence_state="burned" if burned else "admitted", supersedes_attempt_id="attempt-1", amendment_reason="metadata repair",
            cost=experiment_contracts.CostMeasurement(model_call_count=1),
        )
        rows.append({"record_hash": changed.content_hash, **changed.to_dict()})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_exact_rates_and_cache_aware_estimate_preserve_estimated_status(tmp_path):
    bundle = phoenix_report.build_phoenix_bundle(_config(_db(tmp_path), _rates(tmp_path)))
    cost = bundle["report"]["attempt_costs"]["attempt-1"]
    # (10 - 4 - 1) input + 4 cached-read + 1 cached-write + 4 completion.
    assert cost["monetary_cost_usd"] == pytest.approx(13.9)
    assert cost["monetary_cost_status"] == "estimated"
    assert bundle["report"]["complete"] is True
    wrong = dict(bundle["snapshot"]["rows"][0], model="other")
    assert phoenix_report.estimate_row(wrong, {"provider": "provider-a", "model": "model-a"}, "conservative") is None


def test_conservative_handles_unknown_cache_but_cache_aware_refuses(tmp_path):
    db, rates = _db(tmp_path), _rates(tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE spans SET attributesJSONB = ? WHERE span_id = 'span-a'", (json.dumps({"llm": {"model_name": "model-a", "provider": "provider-a"}}),))
    con.commit(); con.close()
    conservative = phoenix_report.build_phoenix_bundle(_config(db, rates, method="conservative"))
    assert conservative["report"]["complete"] is True
    assert conservative["report"]["attempt_costs"]["attempt-1"]["monetary_cost_status"] == "estimated"
    assert conservative["report"]["attempt_costs"]["attempt-1"]["monetary_cost_usd"] == pytest.approx(18.0)
    cache_aware = phoenix_report.build_phoenix_bundle(_config(db, rates))
    assert cache_aware["report"]["complete"] is False
    assert "model_rates_or_token_categories_unavailable" in cache_aware["report"]["unavailable_reasons"]


def test_bundle_replay_rejects_changed_selected_evidence_but_allows_unrelated_append(tmp_path):
    db, rates = _db(tmp_path), _rates(tmp_path)
    bundle = phoenix_report.build_phoenix_bundle(_config(db, rates))
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    assert phoenix_report.verify_phoenix_bundle(bundle)["report"]["complete"] is True
    con = sqlite3.connect(db)
    con.execute("INSERT INTO traces VALUES (3, 'later-unrelated')")
    con.commit(); con.close()
    assert phoenix_report.verify_phoenix_bundle(bundle)["report"]["complete"] is True
    con = sqlite3.connect(db)
    con.execute("UPDATE spans SET llm_token_count_prompt = 11 WHERE span_id = 'span-a'")
    con.commit(); con.close()
    with pytest.raises(ValueError):
        phoenix_report.verify_phoenix_bundle(bundle)
    with pytest.raises(ValueError):
        phoenix_report.build_phoenix_bundle({**_config(db, rates), "attempts": [{"attempt_id": "attempt-1", "trace_ids": ["trace-a", "trace-a"], "expected_model_calls": 2}]})


def test_replay_rejects_rate_evaluator_and_ledger_binding_changes(tmp_path):
    db, rates, ledger = _db(tmp_path), _rates(tmp_path), _ledger(tmp_path / "ledger.jsonl")
    bundle = phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger)
    changed_rate = json.loads(rates.read_text(encoding="utf-8"))
    changed_rate["models"][0]["input_usd_per_token"] = 1.1
    rates.write_text(json.dumps(changed_rate), encoding="utf-8")
    with pytest.raises(ValueError):
        phoenix_report.verify_phoenix_bundle(bundle)
    rates = _rates(tmp_path)
    bundle = phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger)
    altered = dict(bundle)
    altered["evaluator_bindings"] = {"changed": "binding"}
    with pytest.raises(ValueError):
        phoenix_report.verify_phoenix_bundle(altered)
    ledger.write_text(ledger.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        phoenix_report.verify_phoenix_bundle(bundle)


def test_capture_model_call_and_ledger_lifecycle_fail_closed(tmp_path):
    db, rates = _db(tmp_path), _rates(tmp_path)
    incomplete = phoenix_report.build_phoenix_bundle(_config(db, rates, capture_complete=False))
    assert "trace_capture_not_declared_complete" in incomplete["report"]["unavailable_reasons"]
    mismatch = _config(db, rates)
    mismatch["attempts"][0]["expected_model_calls"] = 2
    assert "model_call_count_mismatch" in phoenix_report.build_phoenix_bundle(mismatch)["report"]["unavailable_reasons"]
    ledger = _ledger(tmp_path / "ledger.jsonl", burned=True, amendment=True)
    bundle = phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger)
    assert bundle["ledger"]["kind"] == "attempts"
    orphan = tmp_path / "orphan.jsonl"
    orphan.write_text(json.dumps({"attempt_id": "orphan", "campaign_id": "campaign", "supersedes_attempt_id": "missing"}) + "\n")
    with pytest.raises(ValueError):
        phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=orphan)


def test_declared_run_ceiling_rebinds_protocol_rates_and_model_before_build(tmp_path):
    db, rates, ledger = _db(tmp_path), _rates(tmp_path), _ledger(tmp_path / "ledger.jsonl")
    rate_card = json.loads(rates.read_text(encoding="utf-8"))
    rate_card["models"][0].update(max_input_tokens=9, max_output_tokens=5)
    rates.write_text(json.dumps(rate_card), encoding="utf-8")
    declaration = phoenix_report.completed_call_estimate_ceiling(
        rates, provider="provider-a", model="model-a", token_ceiling=10,
    )
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    rows[0]["protocol"] = {"estimated_run_ceiling": declaration}
    payload = {key: value for key, value in rows[0].items() if key != "record_hash"}
    rows[0]["record_hash"] = experiment_contracts.content_hash(payload)
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    bundle = phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger)
    assert bundle["report"]["declared_run_ceilings"] == {"attempt-1": declaration}

    def stale(mutator):
        current_rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        mutator(current_rows[0])
        record = {key: value for key, value in current_rows[0].items() if key != "record_hash"}
        current_rows[0]["record_hash"] = experiment_contracts.content_hash(record)
        ledger.write_text("".join(json.dumps(row) + "\n" for row in current_rows), encoding="utf-8")
        with pytest.raises(ValueError):
            phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger)
        ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    stale(lambda row: row["protocol"]["estimated_run_ceiling"].update(token_ceiling=11))
    rate_card["models"][0]["input_usd_per_token"] = 1.1
    rates.write_text(json.dumps(rate_card), encoding="utf-8")
    with pytest.raises(ValueError):
        phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger)
    rate_card["models"][0]["input_usd_per_token"] = 1.0
    rate_card["models"][0]["max_input_tokens"] = 10
    rates.write_text(json.dumps(rate_card), encoding="utf-8")
    with pytest.raises(ValueError):
        phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger)
    rate_card["models"][0]["max_input_tokens"] = 9
    rates.write_text(json.dumps(rate_card), encoding="utf-8")
    stale(lambda row: row["protocol"]["estimated_run_ceiling"].update(provider="wrong"))

    no_declaration = _ledger(tmp_path / "no-declaration.jsonl")
    historical = phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=no_declaration)["report"]
    assert historical["declared_run_ceilings"] == {"attempt-1": None}
    assert historical["declared_run_ceiling_dispositions"] == {"attempt-1": "unavailable"}


def test_declared_run_ceiling_marks_oversized_and_post_crossing_calls_without_erasing_spend(tmp_path):
    db, rates, ledger = _db(tmp_path), _rates(tmp_path), _ledger(tmp_path / "ledger.jsonl")

    def declare(*, maximum_input, maximum_output, token_ceiling, model_calls=1):
        card = json.loads(rates.read_text(encoding="utf-8"))
        card["models"][0].update(max_input_tokens=maximum_input, max_output_tokens=maximum_output)
        rates.write_text(json.dumps(card), encoding="utf-8")
        ceiling = phoenix_report.completed_call_estimate_ceiling(
            rates, provider="provider-a", model="model-a", token_ceiling=token_ceiling,
        )
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        rows[0]["cost"]["model_call_count"] = model_calls
        rows[0]["protocol"] = {"estimated_run_ceiling": ceiling}
        payload = {key: value for key, value in rows[0].items() if key != "record_hash"}
        rows[0]["record_hash"] = experiment_contracts.content_hash(payload)
        ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return ceiling

    declare(maximum_input=10, maximum_output=4, token_ceiling=10)
    within = phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger)["report"]
    assert within["declared_run_ceiling_dispositions"] == {"attempt-1": "within"}

    declared = declare(maximum_input=1, maximum_output=1, token_ceiling=1)
    violated_bundle = phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger)
    report = violated_bundle["report"]
    assert report["declared_run_ceiling_dispositions"] == {"attempt-1": "violated"}
    assert report["attempt_costs"]["attempt-1"]["monetary_cost_usd"] == pytest.approx(13.9)
    bundle_path = tmp_path / "violated.json"
    accounting_report.write_bundle(bundle_path, violated_bundle)
    with pytest.raises(ValueError, match="violates its declared run ceiling"):
        accounting_report.accounted_campaign_start(
            bundle_path, limits=experiment_contracts.CostLimits(monetary_cost_usd=1_000.0),
            next_attempt_price=experiment_contracts.PriceProvenance(
                source_id="next", route_model_identity="provider-a:model-a", currency="USD", effective_date_or_version="v1",
            ),
        )

    con = sqlite3.connect(db)
    attrs = json.dumps({"llm": {"model_name": "model-a", "provider": "provider-a"}})
    con.execute("INSERT INTO spans VALUES (3, 1, 'span-after-cross', NULL, 'LLM', '3', '4', 'OK', ?, 1, 0)", (attrs,))
    con.commit(); con.close()
    declare(maximum_input=10, maximum_output=4, token_ceiling=1, model_calls=2)
    config = _config(db, rates, method="conservative")
    config["attempts"][0]["expected_model_calls"] = 2
    post_crossing = phoenix_report.build_phoenix_bundle(config, ledger_path=ledger)["report"]
    assert declared["estimated_run_ceiling_usd"] == "3.000000000"
    assert post_crossing["declared_run_ceiling_dispositions"] == {"attempt-1": "violated"}


def test_cli_verify_claims_and_exhausted_campaign_stop_before_client(tmp_path, monkeypatch):
    db, rates, ledger = _db(tmp_path), _rates(tmp_path), _ledger(tmp_path / "ledger.jsonl")
    config = _config(db, rates)
    config_path, output = tmp_path / "config.json", tmp_path / "bundle.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert accounting_cli.main(["phoenix", "--config", str(config_path), "--output", str(output), "--ledger", str(ledger)]) == 0
    verified = reproducible_claims.verify_claims(output)
    assert verified["accounting_only"] is True
    next_provenance = experiment_contracts.PriceProvenance(
        source_id="next", route_model_identity="provider-a:model-a", currency="USD", effective_date_or_version="v1",
    )
    decision = accounting_report.accounted_campaign_start(
        output, limits=experiment_contracts.CostLimits(monetary_cost_usd=20.0), next_attempt_price=next_provenance,
    )
    assert decision.summary.monetary_cost_status == "estimated"
    limits = tmp_path / "limits.json"
    limits.write_text(json.dumps({"monetary_cost_usd": 13.9}), encoding="utf-8")
    next_price = tmp_path / "next-price.json"
    next_price.write_text(json.dumps({"source_id": "next", "artifact_path": "", "artifact_sha256": "", "route_model_identity": "provider-a:model-a", "currency": "USD", "effective_date_or_version": "v1"}), encoding="utf-8")
    monkeypatch.setattr("ai.hillclimb.live_seams.default_mythic_client", lambda: pytest.fail("must stop before client"))
    assert accounting_cli.main(["check-budget", str(output), "--limits", str(limits), "--next-price", str(next_price)]) == 2


def test_estimated_campaign_requires_rate_card_before_any_client(tmp_path, monkeypatch):
    db, rates, ledger = _db(tmp_path), _rates(tmp_path), _ledger(tmp_path / "ledger.jsonl")
    card = json.loads(rates.read_text(encoding="utf-8"))
    card["models"][0].update(max_input_tokens=9, max_output_tokens=5)
    rates.write_text(json.dumps(card), encoding="utf-8")
    bundle_path = tmp_path / "phoenix-bundle.json"
    accounting_report.write_bundle(
        bundle_path, phoenix_report.build_phoenix_bundle(_config(db, rates), ledger_path=ledger),
    )
    next_price = tmp_path / "next-price.json"
    next_price.write_text(json.dumps({
        "source_id": "next", "artifact_path": "", "artifact_sha256": "",
        "route_model_identity": "provider-a:model-a", "currency": "USD", "effective_date_or_version": "v1",
    }), encoding="utf-8")
    opened = []
    monkeypatch.setattr(rgl.live_seams, "default_mythic_client", lambda: opened.append(True))
    base = dict(
        measurement_kind="legacy", policy_mode="llm", model_provider="provider-a", model_id="model-a",
        model_token_budget=10, campaign_id="campaign", campaign_limits=experiment_contracts.CostLimits(monetary_cost_usd=20.0),
        campaign_accounting_bundle=bundle_path, campaign_price_provenance=next_price,
        development_results_path=ledger,
    )
    with pytest.raises(SystemExit, match="estimated campaign budget requires --run-estimate-rates"):
        rgl.run_side(rgl.Config(**base), "bare", "ordinary")
    assert opened == []
    with pytest.raises(SystemExit, match="run estimate requires a bounded raw or controller LLM row"):
        rgl.run_side(rgl.Config(**base, run_estimate_rates=rates), "bare", "ordinary")
    assert opened == []
