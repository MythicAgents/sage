from __future__ import annotations

import pytest

from ai.hillclimb.experiment_contracts import PriceProvenance
from ai.hillclimb.provider_accounting import (
    AccountingBinding,
    AccountingProvider,
    AttemptSpendExpectation,
    LiteLLMAccountingProvider,
    ProviderAccountingConflict,
    ProviderAccountingError,
    SpendExportCoverage,
    create_accounting_provider,
    reconcile_provider_spend,
)

KEY_HASH = "a" * 64


def _provenance() -> PriceProvenance:
    return PriceProvenance(
        source_id="litellm-spend-export", route_model_identity="declared:route-a:model-a",
        currency="USD", effective_date_or_version="2026-09-18",
    )


def _binding(**changes) -> AccountingBinding:
    values = {
        "expected_model": "provider/model-a", "expected_model_id": "model-a",
        "route_model_identity": "declared:route-a:model-a", "price_provenance": _provenance(),
        "expected_api_base": "https://gateway.example.test",
    }
    values.update(changes)
    return AccountingBinding(**values)


def _coverage(**changes) -> SpendExportCoverage:
    values = {
        "complete": True, "start_time": "2026-09-18T00:00:00Z",
        "end_time": "2026-09-18T02:00:00Z", "source_hash": "sha256:export",
    }
    values.update(changes)
    return SpendExportCoverage(**values)


def _row(request_id: str = "req-1", **changes):
    values = {
        "request_id": request_id, "model": "provider/model-a", "model_id": "model-a",
        "model_group": "route-a", "api_base": "https://gateway.example.test",
        "api_key": KEY_HASH, "spend": 0.25, "prompt_tokens": 10,
        "completion_tokens": 5, "total_tokens": 15, "status": "failed",
        "startTime": "2026-09-18T00:30:00Z", "endTime": "2026-09-18T00:31:00Z",
    }
    values.update(changes)
    return values


def test_exact_request_ids_count_failed_and_explicit_zero_with_provenance():
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row(spend=0.0)], expectations={"burned-attempt": ("req-1",)},
        binding=_binding(), coverage=_coverage(),
    )

    assert report.complete is True
    assert report.complete_total_usd == 0.0
    assert report.priced_subtotal_usd == 0.0
    cost = report.attempt_costs["burned-attempt"]
    assert cost.monetary_cost_status == "measured"
    assert cost.monetary_cost_usd == 0.0
    assert cost.price_provenance == _provenance()
    assert cost.model_call_count == 1
    assert cost.wall_time_seconds is None


def test_report_replays_from_scrubbed_rows_without_sensitive_text():
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(),
        [_row(prompt="do not retain", response="do not retain", metadata={"secret": "do not retain"})],
        expectations={"attempt-1": ("req-1",)}, binding=_binding(), coverage=_coverage(),
    )
    serialized = report.to_dict()
    assert "do not retain" not in str(serialized)
    restored = type(report).from_dict(serialized)
    replay = reconcile_provider_spend(
        LiteLLMAccountingProvider(),
        [row.to_provider_row() for row in restored.sanitized_rows], expectations=restored.expectations,
        binding=restored.binding, coverage=restored.coverage,
    )
    assert replay.to_dict() == report.to_dict()
    serialized["sanitized_rows"][0]["prompt"] = "must reject"
    with pytest.raises(ProviderAccountingError, match="unknown fields"):
        type(report).from_dict(serialized)


def test_missing_or_orphaned_records_make_total_unavailable_but_keep_priced_subtotal():
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row(), _row("orphan", spend=0.75)],
        expectations={"attempt-1": ("req-1", "missing")}, binding=_binding(), coverage=_coverage(),
    )
    assert report.complete is False
    assert report.complete_total_usd is None
    assert report.priced_subtotal_usd == 0.25
    assert set(report.unavailable_reasons) == {"missing_provider_record", "unassigned_provider_record"}
    assert report.attempt_costs["attempt-1"].monetary_cost_status == "unavailable"
    assert report.attempt_costs["attempt-1"].model_call_count is None


def test_unknown_or_null_spend_is_not_free_and_incomplete_export_cannot_complete():
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row(spend="unknown")], expectations={"attempt-1": ("req-1",)},
        binding=_binding(), coverage=_coverage(complete=False),
    )
    assert report.complete is False
    assert report.complete_total_usd is None
    assert report.priced_subtotal_usd == 0.0
    assert set(report.unavailable_reasons) == {"export_not_complete", "provider_spend_unavailable"}


def test_model_or_optional_api_base_mismatch_fails_binding_without_fabricating_a_route():
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row(api_base=None)], expectations={"attempt-1": ("req-1",)},
        binding=_binding(), coverage=_coverage(),
    )
    assert report.complete is False
    assert report.complete_total_usd is None
    assert report.priced_subtotal_usd == 0.25
    assert report.unavailable_reasons == ("model_or_route_binding_mismatch",)
    assert report.sanitized_rows[0].api_base_sha256 == ""


def test_observed_api_base_is_fingerprinted_without_retaining_a_credential_path():
    marker = "SECRET-CREDENTIAL-MARKER"
    base = f"https://gateway.example/openai/{marker}"
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row(api_base=base)], expectations={"attempt-1": ("req-1",)},
        binding=_binding(expected_api_base="https://gateway.example/openai"), coverage=_coverage(),
    )
    serialized = report.to_dict()
    assert report.complete is False
    assert report.sanitized_rows[0].api_base_sha256.startswith("sha256:")
    assert marker not in str(serialized)
    assert marker not in str(report.sanitized_rows[0].to_provider_row())
    replay = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [row.to_provider_row() for row in report.sanitized_rows],
        report.expectations, report.binding, report.coverage,
    )
    assert replay.to_dict() == serialized


def test_decimal_money_sums_at_the_float_boundary_without_binary_rounding_drift():
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row("first", spend=0.7), _row("second", spend=0.1)],
        expectations={"attempt-1": ("first", "second")}, binding=_binding(), coverage=_coverage(),
    )
    assert report.complete is True
    assert report.complete_total_usd == 0.8
    assert report.priced_subtotal_usd == 0.8
    assert report.attempt_costs["attempt-1"].monetary_cost_usd == 0.8


def test_key_window_scope_needs_explicit_count_and_assigns_all_retries():
    expectation = AttemptSpendExpectation(
        attempt_id="attempt-1", api_key_hash=KEY_HASH, start_time="2026-09-18T00:00:00Z",
        end_time="2026-09-18T01:00:00Z", expected_request_count=2,
    )
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row("retry-1", spend=0.1), _row("retry-2", spend=0.2)],
        expectations=(expectation,), binding=_binding(), coverage=_coverage(),
    )
    assert report.complete is True
    assert report.complete_total_usd == pytest.approx(0.3)
    assert report.attempt_costs["attempt-1"].model_call_count == 2

    unknown_count = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row()],
        expectations=(AttemptSpendExpectation(
            attempt_id="unknown", api_key_hash=KEY_HASH, start_time="2026-09-18T00:00:00Z",
            end_time="2026-09-18T01:00:00Z",
        ),), binding=_binding(), coverage=_coverage(),
    )
    assert unknown_count.complete is False
    assert "expected_request_count_unavailable" in unknown_count.unavailable_reasons


def test_scope_windows_cannot_overlap_and_windows_must_be_covered():
    first = AttemptSpendExpectation("a", api_key_hash=KEY_HASH, start_time="2026-09-18T00:00:00Z", end_time="2026-09-18T01:00:00Z", expected_request_count=1)
    overlap = AttemptSpendExpectation("b", api_key_hash=KEY_HASH, start_time="2026-09-18T00:30:00Z", end_time="2026-09-18T01:30:00Z", expected_request_count=1)
    with pytest.raises(ProviderAccountingError, match="windows overlap"):
        reconcile_provider_spend(LiteLLMAccountingProvider(), [], expectations=(first, overlap), binding=_binding(), coverage=_coverage())

    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row()], expectations=(first,), binding=_binding(),
        coverage=_coverage(start_time="2026-09-18T00:15:00Z"),
    )
    assert "export_window_not_covered" in report.unavailable_reasons


def test_duplicate_pagination_rows_dedupe_but_conflicts_and_bad_numbers_fail():
    exact = _row()
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [exact, dict(exact)], expectations={"attempt-1": ("req-1",)},
        binding=_binding(), coverage=_coverage(),
    )
    assert len(report.sanitized_rows) == 1

    with pytest.raises(ProviderAccountingConflict):
        reconcile_provider_spend(
            LiteLLMAccountingProvider(), [exact, _row(spend=0.3)], expectations={"attempt-1": ("req-1",)},
            binding=_binding(), coverage=_coverage(),
        )
    for bad in (True, -0.1, float("nan"), "0.1"):
        with pytest.raises(ProviderAccountingError, match="numeric"):
            LiteLLMAccountingProvider().normalize_rows([_row(spend=bad)])
    with pytest.raises(ProviderAccountingError, match="token totals conflict"):
        LiteLLMAccountingProvider().normalize_rows([_row(total_tokens=99)])
    with pytest.raises(ProviderAccountingError, match="key hash"):
        LiteLLMAccountingProvider().normalize_rows([_row(api_key="sk-not-a-hash")])


def test_raw_naive_litellm_datetime_is_declared_utc_but_scopes_are_explicit_utc():
    normalized = LiteLLMAccountingProvider().normalize_rows([_row(startTime="2026-09-18T00:30:00")])
    assert normalized[0].start_time == "2026-09-18T00:30:00Z"
    with pytest.raises(ProviderAccountingError, match="time"):
        AttemptSpendExpectation("a", api_key_hash=KEY_HASH, start_time="2026-09-18T00:00:00", end_time="2026-09-18T01:00:00", expected_request_count=1)


def test_exact_id_and_key_window_cannot_compete_for_a_row():
    exact = AttemptSpendExpectation("id-arm", request_ids=("req-1",))
    scoped = AttemptSpendExpectation("window-arm", api_key_hash=KEY_HASH, start_time="2026-09-18T00:00:00Z", end_time="2026-09-18T01:00:00Z", expected_request_count=1)
    report = reconcile_provider_spend(
        LiteLLMAccountingProvider(), [_row()], expectations=(exact, scoped), binding=_binding(), coverage=_coverage(),
    )
    assert report.complete is False
    assert "ambiguous_provider_record_assignment" in report.unavailable_reasons
    assert all(cost.monetary_cost_status == "unavailable" for cost in report.attempt_costs.values())


def test_factory_rejects_unknown_and_a_second_protocol_provider_works():
    class DummyProvider:
        name = "dummy"

        def normalize_rows(self, rows):
            return LiteLLMAccountingProvider().normalize_rows(rows)

    assert isinstance(DummyProvider(), AccountingProvider)
    report = reconcile_provider_spend(
        DummyProvider(), [_row()], expectations={"attempt-1": ("req-1",)}, binding=_binding(), coverage=_coverage(),
    )
    assert report.provider == "dummy"
    assert create_accounting_provider().name == "litellm"
    with pytest.raises(ProviderAccountingError, match="unknown"):
        create_accounting_provider("future-provider")
