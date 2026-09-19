"""Assembled offline provider import, immutable evidence, and stop-loss probes."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.hillclimb import accounting_cli, accounting_report as reports, experiment_contracts as c
from ai.hillclimb import run_gauge_live as gauge

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-eval-gauge/scripts/accounting.py"


@pytest.fixture
def fixture(tmp_path):
    price = dict(source_id="gateway-price-snapshot", route_model_identity="bedrock:model-v1",
                 effective_date_or_version="gateway-version-1", currency="USD")
    manifest = {
        "provider": "litellm", "campaign_id": "campaign",
        "binding": {"expected_model": "bedrock/model-v1", "expected_model_id": "deployment-1",
                    "route_model_identity": "bedrock:model-v1", "price_provenance": price},
        "coverage": {"complete": True, "start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-02T00:00:00Z"},
        "attempts": [{"attempt_id": "first", "request_ids": ["req-1"]}],
    }
    spend = [{"request_id": "req-1", "model": "bedrock/model-v1", "model_id": "deployment-1",
              "spend": 0.25, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
              "startTime": "2026-01-01T00:01:00Z", "endTime": "2026-01-01T00:02:00Z",
              "metadata": {"user_api_key": "SECRET-MARKER"}, "messages": ["SECRET-MARKER"]}]
    attempt = c.AttemptContract(attempt_id="first", experiment_id="experiment", campaign_id="campaign",
                                treatment_id="shipping", evidence_state="burned",
                                cost=c.CostMeasurement(total_tokens=15))
    ledger = tmp_path / "attempts.jsonl"
    c.append_attempt_record(ledger, attempt)
    bundle = tmp_path / "accounting.json"
    return manifest, spend, ledger, bundle, price, attempt


def _build(fixture):
    manifest, spend, ledger, bundle, *_ = fixture
    value = reports.build_bundle(manifest, spend, ledger_path=ledger)
    reports.write_bundle(bundle, value)
    return value


def test_bundle_reproduces_and_retains_only_accounting_fields(fixture):
    manifest, spend, ledger, bundle, *_ = fixture
    before = ledger.read_bytes()
    _build(fixture)
    reproduced = reports.verify_bundle(bundle)
    assert reproduced["report"]["complete_total_usd"] == 0.25
    assert "SECRET-MARKER" not in bundle.read_text()
    assert ledger.read_bytes() == before
    assert bundle.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        reports.write_bundle(bundle, {})


@pytest.mark.parametrize("tamper", ["report", "ledger", "source", "spend", "missing-ledger"])
def test_changed_missing_or_forged_inputs_cannot_reproduce(fixture, tamper):
    manifest, spend, ledger, bundle, *_ = fixture
    value = _build(fixture)
    if tamper == "report":
        value["report"]["complete_total_usd"] = 0.01
    elif tamper == "source":
        value["evaluator_bindings"]["provider_accounting.py"] = "stale"
    elif tamper == "spend":
        value["spend_rows"][0]["spend_usd"] = 0.01
    elif tamper == "ledger":
        ledger.write_text(ledger.read_text() + "{}\n")
    else:
        ledger.unlink()
    bundle.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError)):
        reports.verify_bundle(bundle)


def test_incomplete_evidence_reproduces_without_becoming_zero(fixture):
    manifest, spend, ledger, bundle, *_ = fixture
    manifest["attempts"][0]["request_ids"].append("missing")
    _build(fixture)
    report = reports.verify_bundle(bundle)["report"]
    assert not report["complete"]
    assert report["complete_total_usd"] is None
    assert report["priced_subtotal_usd"] == 0.25


def test_referenced_price_artifact_must_remain_available_and_unchanged(fixture):
    manifest, spend, ledger, bundle, price, _ = fixture
    artifact = bundle.with_name("price-evidence.json")
    artifact.write_text('{"pricing_version":"frozen"}')
    price.update(artifact_path=str(artifact), artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    _build(fixture)
    assert reports.verify_bundle(bundle)["report"]["complete"]
    artifact.write_text('{"pricing_version":"changed"}')
    with pytest.raises(ValueError, match="pricing artifact changed"):
        reports.verify_bundle(bundle)
    artifact.unlink()
    with pytest.raises(OSError):
        reports.verify_bundle(bundle)


def test_imported_burned_spend_stops_next_attempt_and_never_amends_original(fixture):
    manifest, spend, ledger, bundle, price, _ = fixture
    before = ledger.read_bytes()
    _build(fixture)
    decision = reports.accounted_campaign_start(
        bundle, limits=c.CostLimits(monetary_cost_usd=0.25, total_tokens=100),
        next_attempt_price=c.PriceProvenance(**price))
    assert not decision.may_start
    assert decision.reason == "limit_exhausted"
    assert decision.summary.monetary_cost_usd == 0.25
    assert decision.summary.total_tokens == 15
    assert decision.resolution.burned_attempt_ids == ("first",)
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("same_attempt", [True, False], ids=["request-sum", "campaign-sum"])
def test_decimal_exhaustion_never_leaves_a_false_remaining_balance(fixture, same_attempt):
    manifest, spend, ledger, bundle, price, attempt = fixture
    spend[0]["spend"] = 0.7
    spend.append({**spend[0], "request_id": "req-2", "spend": 0.1})
    if same_attempt:
        manifest["attempts"][0]["request_ids"].append("req-2")
    else:
        c.append_attempt_record(ledger, replace(attempt, attempt_id="second"))
        manifest["attempts"].append({"attempt_id": "second", "request_ids": ["req-2"]})
    _build(fixture)
    decision = reports.accounted_campaign_start(bundle, limits=c.CostLimits(monetary_cost_usd=0.8),
                                               next_attempt_price=c.PriceProvenance(**price))
    assert decision.summary.monetary_cost_usd == 0.8
    assert not decision.may_start
    assert decision.reason == "limit_exhausted"


def test_amendments_count_once_and_replacements_still_charge(fixture):
    manifest, spend, ledger, bundle, price, attempt = fixture
    amendment = replace(attempt, attempt_id="amendment", supersedes_attempt_id="first",
                        amendment_reason="correct retained note")
    c.append_attempt_record(ledger, amendment)
    replacement = replace(attempt, attempt_id="replacement", replacement_for_attempt_id="first",
                          evidence_state="admitted")
    c.append_attempt_record(ledger, replacement)
    manifest["attempts"].append({"attempt_id": "replacement", "request_ids": ["req-2"]})
    spend.append({**spend[0], "request_id": "req-2", "spend": 0.5})
    _build(fixture)
    decision = reports.accounted_campaign_start(bundle, limits=c.CostLimits(monetary_cost_usd=1),
                                               next_attempt_price=c.PriceProvenance(**price))
    assert decision.may_start
    assert decision.summary.monetary_cost_usd == 0.75
    assert decision.summary.total_tokens == 30


def test_omitted_spent_attempt_refuses_sidecar(fixture):
    manifest, spend, ledger, bundle, price, attempt = fixture
    c.append_attempt_record(ledger, replace(attempt, attempt_id="orphan"))
    with pytest.raises(ValueError, match="cover all"):
        _build(fixture)


def test_pilot_cost_sidecar_does_not_change_outcomes_or_resource_completeness(fixture):
    manifest, spend, ledger, bundle, *_ = fixture
    rows = [{"record_type": "arm_started", "arm": "shipping"},
            {"record_type": "arm_result", "arm": "shipping", "execution": {"resource_complete": False}}]
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    manifest["attempts"][0]["attempt_id"] = "shipping"
    before = ledger.read_bytes()
    value = reports.build_bundle(manifest, spend, ledger_path=ledger, ledger_kind="pilot")
    reports.write_bundle(bundle, value)
    assert reports.verify_bundle(bundle)["report"]["complete_total_usd"] == 0.25
    assert ledger.read_bytes() == before
    assert "resource_complete" not in value["report"]


def test_gauge_opt_in_stoploss_runs_before_client_creation(fixture, monkeypatch):
    manifest, spend, ledger, bundle, price, _ = fixture
    _build(fixture)
    price_file = bundle.with_name("price.json")
    price_file.write_text(json.dumps(price))
    cfg = gauge.Config(development_results_path=ledger, campaign_id="campaign",
                       campaign_limits=c.CostLimits(monetary_cost_usd=0.25),
                       campaign_accounting_bundle=bundle, campaign_price_provenance=price_file)
    monkeypatch.setattr(gauge.live_seams, "default_mythic_client", lambda: pytest.fail("client opened"))
    with pytest.raises(SystemExit, match="limit_exhausted"):
        gauge.run_side(cfg, "harness", "scenario")
    # New spend invalidates the sidecar until it is reconciled again.
    ledger.write_text(ledger.read_text() + "{}\n")
    with pytest.raises(SystemExit, match="unavailable or stale"):
        gauge.run_side(cfg, "harness", "scenario")


def test_new_campaign_can_check_frozen_price_without_inventing_historical_spend(fixture):
    manifest, spend, ledger, bundle, price, _ = fixture
    price_file = bundle.with_name("price.json"); price_file.write_text(json.dumps(price))
    new_ledger = ledger.with_name("new-empty-campaign.jsonl")
    cfg = gauge.Config(development_results_path=new_ledger, campaign_id="campaign",
                       campaign_limits=c.CostLimits(monetary_cost_usd=1),
                       campaign_price_provenance=price_file)
    assert gauge._check_campaign_limits(cfg, "scenario") is None
    cfg.development_results_path = ledger
    with pytest.raises(SystemExit, match="required_axis_unavailable"):
        gauge._check_campaign_limits(cfg, "scenario")


def test_script_reconcile_verify_and_budget_exit_statuses(fixture):
    manifest, spend, ledger, bundle, price, _ = fixture
    manifest_path, spend_path = bundle.with_name("manifest.json"), bundle.with_name("spend.json")
    manifest_path.write_text(json.dumps(manifest)); spend_path.write_text(json.dumps(spend))
    invocation = [sys.executable, str(SCRIPT)]
    result = subprocess.run(invocation + ["reconcile", "--manifest", str(manifest_path), "--spend", str(spend_path),
                             "--ledger", str(ledger), "--output", str(bundle)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "SECRET-MARKER" not in result.stdout + result.stderr
    verified = subprocess.run(invocation + ["verify", str(bundle)], text=True, capture_output=True)
    assert verified.returncode == 0, verified.stderr + verified.stdout
    common_verify = subprocess.run([sys.executable, str(SCRIPT.with_name("verify_claims.py")), str(bundle)],
                                   text=True, capture_output=True)
    assert common_verify.returncode == 0, common_verify.stderr + common_verify.stdout
    assert json.loads(common_verify.stdout)["accounting_only"] is True
    assert json.loads(common_verify.stdout)["performance_assertion_allowed"] is False
    limits = bundle.with_name("limits.json"); limits.write_text('{"monetary_cost_usd":0.25}')
    price_file = bundle.with_name("price.json"); price_file.write_text(json.dumps(price))
    blocked = subprocess.run(invocation + ["check-budget", str(bundle), "--limits", str(limits),
                                          "--next-price", str(price_file)], text=True, capture_output=True)
    assert blocked.returncode == 2
    assert json.loads(blocked.stdout)["decision"]["reason"] == "limit_exhausted"


def test_offline_command_does_not_construct_a_collector(fixture, monkeypatch, capsys):
    from ai.hillclimb import litellm_accounting
    monkeypatch.setattr(litellm_accounting, "fetch_spend_logs", lambda *a, **kw: pytest.fail("network attempted"))
    _build(fixture)
    assert accounting_cli.main(["verify", str(fixture[3])]) == 0
    assert json.loads(capsys.readouterr().out)["reproduced"]


def test_collect_command_uses_paginated_transport_and_scrubs_before_writing(fixture, monkeypatch, capsys):
    from ai.hillclimb import litellm_accounting as gateway
    manifest, spend, ledger, bundle, *_ = fixture
    manifest_path = bundle.with_name("manifest.json")
    manifest_path.write_text(json.dumps(manifest))
    config_path = bundle.with_name("gateway.json")
    config_path.write_text(json.dumps({"base_url": "https://gateway.example.test", "api_key_env": "TEST_ACCOUNTING_KEY",
                                      "start_date": "2026-01-01 00:00:00", "end_date": "2026-01-02 00:00:00"}))
    monkeypatch.setenv("TEST_ACCOUNTING_KEY", "SECRET-CREDENTIAL")
    requests = []

    def send(request):
        requests.append(request)
        return gateway.SpendLogResponse(200, {"data": spend if len(requests) == 1 else [],
            "total": 10000, "total_pages": 1, "total_is_capped": True})

    monkeypatch.setattr(gateway, "_stdlib_transport", send)
    assert accounting_cli.main(["collect", "--manifest", str(manifest_path), "--gateway-config", str(config_path),
                               "--ledger", str(ledger), "--output", str(bundle)]) == 0
    assert len(requests) == 2
    assert requests[0].headers["Authorization"] == "Bearer SECRET-CREDENTIAL"
    assert "SECRET" not in bundle.read_text() + capsys.readouterr().out
    assert reports.verify_bundle(bundle)["report"]["complete_total_usd"] == 0.25


def test_collect_failure_is_sanitized_at_cli(fixture, monkeypatch, capsys):
    from ai.hillclimb import litellm_accounting as gateway
    manifest, spend, ledger, bundle, *_ = fixture
    manifest_path = bundle.with_name("manifest.json"); manifest_path.write_text(json.dumps(manifest))
    config_path = bundle.with_name("gateway.json")
    config_path.write_text(json.dumps({"base_url": "https://gateway.example.test", "api_key_env": "TEST_ACCOUNTING_KEY",
                                      "start_date": "2026-01-01 00:00:00", "end_date": "2026-01-02 00:00:00"}))

    def fail(*args, **kwargs):
        raise gateway.SpendLogFetchError("SECRET-MARKER")

    monkeypatch.setattr(gateway, "fetch_spend_logs", fail)
    assert accounting_cli.main(["collect", "--manifest", str(manifest_path), "--gateway-config", str(config_path),
                               "--output", str(bundle)]) == 2
    assert "SECRET" not in capsys.readouterr().out
    assert not bundle.exists()
