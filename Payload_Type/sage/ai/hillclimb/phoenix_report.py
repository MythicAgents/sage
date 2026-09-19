"""Offline Phoenix usage estimates, using the existing evaluation ledger interfaces.

An estimate is complete only for its declared, settled trace capture. It is never
described as a provider bill or proof that uninstrumented retries were captured.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import math
from pathlib import Path
from typing import Any, Mapping

from . import experiment_contracts as contracts
from .phoenix_accounting import read_phoenix_usage

SCHEMA = "sage.eval.phoenix-accounting.v1"
RATE_SCHEMA = "sage.eval.phoenix-rates.v1"
_RATE_FIELDS = {"input_usd_per_token", "output_usd_per_token",
                "cache_read_usd_per_token", "cache_write_usd_per_token"}
_MODEL_LIMITS = {"max_input_tokens", "max_output_tokens"}


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Phoenix accounting identity unavailable")
    return value.strip()


def load_rates(path: Path) -> dict:
    from . import accounting_report as reports

    value = reports.load_json(path)
    if not isinstance(value, dict) or set(value) != {"schema", "price_provenance", "models"}:
        raise ValueError("invalid Phoenix rate card")
    if value["schema"] != RATE_SCHEMA or not isinstance(value["models"], list) or not value["models"]:
        raise ValueError("invalid Phoenix rate card")
    reports.validate_price_provenance(contracts.PriceProvenance(**value["price_provenance"]))
    seen = set()
    for model in value["models"]:
        if not isinstance(model, dict) or set(model) not in (
            {"model", "provider", *_RATE_FIELDS}, {"model", "provider", *_RATE_FIELDS, *_MODEL_LIMITS},
        ):
            raise ValueError("invalid Phoenix model rates")
        identity = (_text(model["provider"]), _text(model["model"]))
        if identity in seen:
            raise ValueError("duplicate Phoenix model rates")
        seen.add(identity)
        for name in _RATE_FIELDS:
            rate = model[name]
            if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
                raise ValueError("Phoenix token rate unavailable")
        for name in _MODEL_LIMITS.intersection(model):
            if type(model[name]) is not int or model[name] <= 0:
                raise ValueError("Phoenix model token maximum unavailable")
    return value


def completed_call_estimate_ceiling(pricing_path: Path, *, provider: str, model: str,
                                    token_ceiling: int) -> dict:
    """Bound a sequential logical-response stream stopped after crossing C tokens.

    Existing raw/controller guards check before and after each completed call.
    The last call may consume its full model input/output maxima. Internal SDK
    or gateway retries without logical response usage are outside this estimate.
    Callers must establish that this is the actual guarded execution path.
    """
    from . import accounting_report as reports

    if type(token_ceiling) is not int or token_ceiling <= 0:
        raise ValueError("completed-call token ceiling unavailable")
    rates = load_rates(pricing_path)
    matches = [item for item in rates["models"] if (item["provider"], item["model"]) == (provider, model)]
    if len(matches) != 1 or not _MODEL_LIMITS <= matches[0].keys():
        raise ValueError("exact model pricing and request token maxima required")
    rate = matches[0]
    q_in = max(Decimal(str(rate[name])) for name in _RATE_FIELDS if name != "output_usd_per_token")
    q_out = Decimal(str(rate["output_usd_per_token"]))
    crossing = rate["max_input_tokens"] * q_in + rate["max_output_tokens"] * q_out
    ceiling = ((token_ceiling - 1) * max(q_in, q_out) + crossing).quantize(
        Decimal("0.000000001"), rounding=ROUND_CEILING)
    return {"monetary_cost_status": "estimated", "estimated_run_ceiling_usd": str(ceiling),
            "token_ceiling": token_ceiling, "max_input_tokens": rate["max_input_tokens"],
            "max_output_tokens": rate["max_output_tokens"], "crossing_call_usd": str(crossing),
            "provider": provider, "model": model,
            "pricing_path": str(pricing_path.resolve()), "pricing_sha256": reports._hash_file(pricing_path),
            "price_provenance": rates["price_provenance"],
            "max_in_flight_logical_calls": 1,
            "enforced_by": "completed_call_token_ceiling",
            "accounting_scope": "completed_logical_response_estimate",
            "provider_billing_hard_cap": False}


def estimate_row(row: Mapping[str, Any], rates: Mapping[str, Any], method: str) -> Decimal | None:
    """Price aggregate counts conservatively, or exact retained cache categories.

    Conservative mode prices every prompt token at the largest input/cache rate. It needs
    no fabricated cache split. Cache-aware mode uses inclusive prompt counts and
    requires both cache categories; reasoning is already part of completion.
    """
    if (row.get("provider"), row.get("model")) != (rates["provider"], rates["model"]):
        return None
    prompt, completion = row.get("prompt_tokens"), row.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
        return None
    if method == "conservative":
        return (Decimal(prompt) * max(Decimal(str(rates[name])) for name in _RATE_FIELDS
                                     if name != "output_usd_per_token")
                + Decimal(completion) * Decimal(str(rates["output_usd_per_token"])))
    if method != "cache_aware":
        raise ValueError("unknown Phoenix estimate method")
    read, write = row.get("cache_read_tokens"), row.get("cache_write_tokens")
    if type(read) is not int or type(write) is not int or min(read, write) < 0 or read + write > prompt:
        return None
    return (Decimal(prompt - read - write) * Decimal(str(rates["input_usd_per_token"]))
            + Decimal(read) * Decimal(str(rates["cache_read_usd_per_token"]))
            + Decimal(write) * Decimal(str(rates["cache_write_usd_per_token"]))
            + Decimal(completion) * Decimal(str(rates["output_usd_per_token"])))


def build_phoenix_bundle(config: Mapping[str, Any], *, ledger_path: Path | None = None,
                         ledger_kind: str = "attempts") -> dict:
    from . import accounting_report as reports

    if not isinstance(config, Mapping) or set(config) != {
        "campaign_id", "database_path", "pricing_path", "method", "capture_complete", "attempts",
    }:
        raise ValueError("invalid Phoenix accounting configuration")
    campaign = _text(config["campaign_id"])
    database = Path(_text(config["database_path"])).resolve()
    pricing = Path(_text(config["pricing_path"])).resolve()
    method = config["method"]
    if method not in {"conservative", "cache_aware"} or type(config["capture_complete"]) is not bool:
        raise ValueError("invalid Phoenix estimate declaration")
    attempts = config["attempts"]
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("Phoenix attempt selection required")
    ids, traces, selected = set(), set(), []
    for attempt in attempts:
        if not isinstance(attempt, Mapping) or set(attempt) != {"attempt_id", "trace_ids", "expected_model_calls"}:
            raise ValueError("invalid Phoenix attempt selection")
        attempt_id = _text(attempt["attempt_id"])
        count = attempt["expected_model_calls"]
        trace_ids = attempt["trace_ids"]
        if attempt_id in ids or type(count) is not int or count < 1:
            raise ValueError("ambiguous Phoenix attempt selection")
        if not isinstance(trace_ids, list) or not trace_ids:
            raise ValueError("Phoenix trace IDs required")
        clean_ids = [_text(item) for item in trace_ids]
        if len(set(clean_ids)) != len(clean_ids) or traces.intersection(clean_ids):
            raise ValueError("Phoenix trace assigned more than once")
        ids.add(attempt_id)
        traces.update(clean_ids)
        selected.append({"attempt_id": attempt_id, "trace_ids": sorted(clean_ids),
                         "expected_model_calls": count})
    rates = load_rates(pricing)
    rate_map = {(item["provider"], item["model"]): item for item in rates["models"]}
    snapshot = read_phoenix_usage(database, trace_ids=sorted(traces))
    manifest = {**dict(config), "campaign_id": campaign, "database_path": str(database),
                "pricing_path": str(pricing), "attempts": sorted(selected, key=lambda item: item["attempt_id"])}
    price = contracts.PriceProvenance(**rates["price_provenance"])
    # The exact estimate rate card is the monetary provenance; it retains its own
    # declared upstream source/provenance without pretending to observe a route.
    estimate_price = contracts.PriceProvenance(
        source_id="phoenix-token-estimate:" + method, artifact_path=str(pricing),
        artifact_sha256=reports._hash_file(pricing), route_model_identity=price.route_model_identity,
        currency="USD", effective_date_or_version=price.effective_date_or_version,
    )
    ledger = None
    ledger_rows = []
    if ledger_path is not None:
        ledger_rows = reports._ledger_rows(ledger_path)
        reports._validate_ledger_scope(ledger_rows, ledger_kind, campaign, ids)
        ledger = {"path": str(ledger_path.resolve()), "sha256": reports._hash_file(ledger_path), "kind": ledger_kind}
    known_calls = {row["attempt_id"]: row.get("cost", {}).get("model_call_count")
                   for row in reports._scoped_attempt_rows(ledger_rows, campaign)
                   if not row.get("supersedes_attempt_id")}
    reasons = []
    if not config["capture_complete"]:
        reasons.append("trace_capture_not_declared_complete")
    if not snapshot["coverage"]["countable"]:
        reasons.append("trace_usage_incomplete_or_ambiguous")
    costs, details, ceilings, ceiling_dispositions = {}, {}, {}, {}
    subtotal, priced_rows = Decimal(0), 0
    for item in manifest["attempts"]:
        rows = [row for row in snapshot["rows"] if row["trace_id"] in item["trace_ids"]]
        attempt_reasons = list(reasons)
        if len(rows) != item["expected_model_calls"]:
            attempt_reasons.append("model_call_count_mismatch")
        recorded_calls = known_calls.get(item["attempt_id"])
        if recorded_calls is not None and recorded_calls != item["expected_model_calls"]:
            attempt_reasons.append("ledger_model_call_count_mismatch")
        amount, row_details = Decimal(0), []
        for row in rows:
            rate = rate_map.get((row["provider"], row["model"]))
            estimate = estimate_row(row, rate, method) if rate is not None else None
            if estimate is None:
                attempt_reasons.append("model_rates_or_token_categories_unavailable")
            else:
                amount += estimate
                subtotal += estimate
                priced_rows += 1
            row_details.append({"trace_id": row["trace_id"], "span_id": row["span_id"],
                                "estimated_cost_usd": float(estimate) if estimate is not None else None})
        def total(field):
            values = [row[field] for row in rows]
            return sum(values) if values and all(v is not None for v in values) else None
        costs[item["attempt_id"]] = asdict(contracts.CostMeasurement.from_dict({
            "model_call_count": len(rows), "input_tokens": total("prompt_tokens"),
            "output_tokens": total("completion_tokens"), "total_tokens": total("total_tokens"),
            "monetary_cost_usd": None if attempt_reasons else float(amount),
            "monetary_cost_status": "unavailable" if attempt_reasons else "estimated",
            "price_provenance": asdict(estimate_price),
        }))
        details[item["attempt_id"]] = {"rows": row_details, "unavailable_reasons": sorted(set(attempt_reasons))}
        original_rows = [row for row in ledger_rows if row.get("attempt_id") == item["attempt_id"]]
        declaration = original_rows[0].get("protocol", {}).get("estimated_run_ceiling") if original_rows else None
        if declaration is not None:
            rebound = completed_call_estimate_ceiling(
                pricing, provider=declaration["provider"], model=declaration["model"],
                token_ceiling=declaration["token_ceiling"],
            )
            if (declaration != rebound or any((row["provider"], row["model"]) !=
                    (declaration["provider"], declaration["model"]) for row in rows)):
                raise ValueError("Phoenix run ceiling differs from its protocol or model evidence")
            ceilings[item["attempt_id"]] = rebound
            # Preserve spent cost even when the declared execution contract is
            # contradicted. A ceiling violation is a separate stopping signal.
            disposition = "unavailable" if attempt_reasons or any(not row["start_time"] for row in rows) else "within"
            before = 0
            for row in sorted(rows, key=lambda value: (value["start_time"] or "", value["span_id"])):
                if row["prompt_tokens"] is None or row["completion_tokens"] is None:
                    continue
                if (row["prompt_tokens"] > rebound["max_input_tokens"]
                        or row["completion_tokens"] > rebound["max_output_tokens"]
                        or before >= rebound["token_ceiling"]):
                    disposition = "violated"
                before += row["total_tokens"]
            if amount > Decimal(rebound["estimated_run_ceiling_usd"]):
                disposition = "violated"
            ceiling_dispositions[item["attempt_id"]] = disposition
        else:
            ceilings[item["attempt_id"]] = None
            ceiling_dispositions[item["attempt_id"]] = "unavailable"
    reasons = sorted({reason for detail in details.values() for reason in detail["unavailable_reasons"]})
    complete = not reasons
    report = {"provider": "phoenix", "accounting_scope": "phoenix_traced_model_estimate",
              "estimate_method": method, "complete": complete,
              "complete_total_usd": float(subtotal) if complete else None,
              "priced_subtotal_usd": float(subtotal) if priced_rows else None,
              "monetary_cost_status": "estimated" if complete else "unavailable",
              "attempt_costs": costs, "attempt_details": details, "unavailable_reasons": reasons,
              "declared_run_ceilings": ceilings,
              "declared_run_ceiling_dispositions": ceiling_dispositions,
              "provider_billing_reconciled": False, "in_flight_dollar_enforcement": False,
              "scope_note": "Declared settled Phoenix traces only; uninstrumented provider retries are not established.",
              "estimate_note": ("Prompt tokens use the largest input/cache rate; completion includes reasoning. Rates describe exclusive token categories; unknown cache splits remain unknown."
                                if method == "conservative" else "Configured category rates; inclusive prompt counts; reasoning included in completion.")}
    return {"schema": SCHEMA, "evaluator_bindings": reports.evaluator_bindings(),
            "manifest": manifest, "ledger": ledger,
            "pricing": {"path": str(pricing), "sha256": reports._hash_file(pricing)},
            "snapshot": snapshot, "report": report}


def verify_phoenix_bundle(bundle: Mapping[str, Any]) -> dict:
    from . import accounting_report as reports

    if set(bundle) != {"schema", "evaluator_bindings", "manifest", "ledger", "pricing", "snapshot", "report"}:
        raise ValueError("invalid Phoenix accounting bundle")
    if bundle["schema"] != SCHEMA or bundle["evaluator_bindings"] != reports.evaluator_bindings():
        raise ValueError("Phoenix accounting evaluator changed")
    pricing = bundle["pricing"]
    if reports._hash_file(Path(pricing["path"])) != pricing["sha256"]:
        raise ValueError("Phoenix rate card changed")
    ledger = bundle["ledger"]
    if ledger is not None and reports._hash_file(Path(ledger["path"])) != ledger["sha256"]:
        raise ValueError("Phoenix accounting ledger changed")
    rebuilt = build_phoenix_bundle(bundle["manifest"], ledger_path=Path(ledger["path"]) if ledger else None,
                                   ledger_kind=ledger["kind"] if ledger else "attempts")
    if contracts.canonical_json(rebuilt) != contracts.canonical_json(bundle):
        raise ValueError("Phoenix accounting does not reproduce")
    return rebuilt
