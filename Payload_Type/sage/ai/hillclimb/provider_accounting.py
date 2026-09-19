"""Pure, scrubbed provider-spend reconciliation for evaluation artifacts.

This module deliberately does not call a provider or read credentials.  A caller supplies
already-collected rows and an explicit, complete export assertion.  The retained rows are
an allowlist of accounting fields, so an immutable evaluation artifact can be re-derived
without retaining prompts, responses, API keys, or arbitrary provider metadata.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Iterable, Literal, Mapping, Protocol, Sequence, runtime_checkable

try:
    from .experiment_contracts import CostMeasurement, PriceProvenance
except ImportError:  # Direct neighboring-module use by evaluation scripts.
    from experiment_contracts import CostMeasurement, PriceProvenance


class ProviderAccountingError(ValueError):
    """A non-sensitive provider accounting input error."""


class ProviderAccountingConflict(ProviderAccountingError):
    """The provider supplied non-identical records for one request id."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderAccountingError("provider accounting configuration is malformed")
    return value.strip()


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProviderAccountingError("provider accounting row is malformed")
    return value.strip()


def _api_key_hash(value: Any) -> str:
    """Accept only LiteLLM's stored token digest, never a virtual key."""
    text = _optional_text(value)
    if not text:
        return ""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
        raise ProviderAccountingError("provider accounting key hash is malformed")
    return text.casefold()


def _number(value: Any, *, field: str) -> float:
    if type(value) not in (int, float):
        raise ProviderAccountingError("provider accounting numeric field is malformed")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ProviderAccountingError("provider accounting numeric field is malformed")
    return result


def _token(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ProviderAccountingError("provider accounting token field is malformed")
    return value


def _utc_time(value: Any, *, required: bool, naive_is_utc: bool = False) -> tuple[datetime | None, str]:
    if value is None or value == "":
        if required:
            raise ProviderAccountingError("provider accounting time is malformed")
        return None, ""
    if not isinstance(value, str):
        raise ProviderAccountingError("provider accounting time is malformed")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderAccountingError("provider accounting time is malformed") from exc
    if parsed.tzinfo is None and not naive_is_utc:
        raise ProviderAccountingError("provider accounting time is malformed")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed, parsed.isoformat().replace("+00:00", "Z")


def _safe_api_base(value: Any) -> str:
    text = _optional_text(value)
    # Configuration is operator-authored and non-secret.  Observed provider values
    # are never retained as text (see _observed_api_base_sha256 below).
    if any(part in text for part in ("@", "?", "#")):
        raise ProviderAccountingError("provider accounting route is malformed")
    return text


def _sha256_digest(value: Any) -> str:
    text = _required_text(value, field="sha256").casefold()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise ProviderAccountingError("provider accounting fingerprint is malformed")
    return text


def _optional_sha256_digest(value: Any) -> str:
    return "" if value is None or value == "" else _sha256_digest(value)


def _api_base_sha256(value: Any) -> str:
    """Fingerprint an observed base without retaining its path or credentials."""
    return _hash({"api_base": _optional_text(value)}) if value is not None else ""


def _observed_api_base_sha256(raw: Mapping[str, Any]) -> str:
    """Accept a raw observed base or the typed fingerprint used for scrubbed replay."""
    observed = _api_base_sha256(raw.get("api_base")) if "api_base" in raw else ""
    replay = _optional_sha256_digest(raw["api_base_sha256"]) if "api_base_sha256" in raw else ""
    if observed and replay and observed != replay:
        raise ProviderAccountingError("provider accounting route fingerprint conflicts")
    return observed or replay


def _reject_extra_fields(value: Mapping[str, Any], allowed: set[str]) -> None:
    if set(value) - allowed:
        raise ProviderAccountingError("provider accounting record has unknown fields")


@dataclass(frozen=True)
class SanitizedSpendRow:
    """The non-secret accounting fields retained from one provider spend-log row."""

    request_id: str
    model: str
    model_id: str
    model_group: str
    api_base_sha256: str
    api_key_hash: str
    spend_usd: float | None
    spend_state: Literal["measured", "missing", "null", "unknown"]
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cache_tokens: tuple[tuple[str, int], ...] = ()
    status: str = ""
    start_time: str = ""
    end_time: str = ""
    source_record_sha256: str = ""

    def __post_init__(self) -> None:
        _required_text(self.request_id, field="request_id")
        _optional_sha256_digest(self.api_base_sha256)
        if self.spend_state not in {"measured", "missing", "null", "unknown"}:
            raise ProviderAccountingError("provider accounting row is malformed")
        if self.spend_state == "measured":
            if self.spend_usd is None:
                raise ProviderAccountingError("provider accounting row is malformed")
            _number(self.spend_usd, field="spend")
        elif self.spend_usd is not None:
            raise ProviderAccountingError("provider accounting row is malformed")
        if self.prompt_tokens is not None and self.completion_tokens is not None and self.total_tokens is not None:
            if self.prompt_tokens + self.completion_tokens != self.total_tokens:
                raise ProviderAccountingError("provider accounting token totals conflict")
        for _, amount in self.cache_tokens:
            _token(amount)

    def accounting_fields(self) -> dict[str, Any]:
        """Fields that may influence a reproducible accounting result."""
        return {
            "request_id": self.request_id,
            "model": self.model,
            "model_id": self.model_id,
            "model_group": self.model_group,
            "api_base_sha256": self.api_base_sha256,
            "api_key_hash": self.api_key_hash,
            "spend_usd": self.spend_usd,
            "spend_state": self.spend_state,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_tokens": list(self.cache_tokens),
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.accounting_fields(), "source_record_sha256": self.source_record_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SanitizedSpendRow":
        _reject_extra_fields(value, set(cls.__dataclass_fields__))
        cache = value.get("cache_tokens") or ()
        if not isinstance(cache, Sequence) or isinstance(cache, (str, bytes)):
            raise ProviderAccountingError("provider accounting row is malformed")
        normalized_cache = tuple((str(item[0]), _token(item[1])) for item in cache
                                 if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2)
        if len(normalized_cache) != len(cache) or any(amount is None for _, amount in normalized_cache):
            raise ProviderAccountingError("provider accounting row is malformed")
        row = cls(
            request_id=_required_text(value.get("request_id"), field="request_id"),
            model=_optional_text(value.get("model")), model_id=_optional_text(value.get("model_id")),
            model_group=_optional_text(value.get("model_group")), api_base_sha256=_optional_sha256_digest(value.get("api_base_sha256")),
            api_key_hash=_api_key_hash(value.get("api_key_hash")), spend_usd=value.get("spend_usd"),
            spend_state=value.get("spend_state", "missing"), prompt_tokens=value.get("prompt_tokens"),
            completion_tokens=value.get("completion_tokens"), total_tokens=value.get("total_tokens"),
            cache_tokens=tuple(sorted(normalized_cache)), status=_optional_text(value.get("status")),
            start_time=_utc_time(value.get("start_time"), required=False)[1],
            end_time=_utc_time(value.get("end_time"), required=False)[1],
            source_record_sha256=_optional_text(value.get("source_record_sha256")),
        )
        if row.source_record_sha256 != _hash(row.accounting_fields()):
            raise ProviderAccountingError("accounting row source proof is malformed")
        return row

    def to_provider_row(self) -> dict[str, Any]:
        """Re-create only the scrubbed LiteLLM-compatible fields for deterministic replay."""
        result: dict[str, Any] = {
            "request_id": self.request_id, "model": self.model, "model_id": self.model_id,
            "model_group": self.model_group, "api_base_sha256": self.api_base_sha256, "api_key_hash": self.api_key_hash,
            "status": self.status, "startTime": self.start_time, "endTime": self.end_time,
        }
        if self.spend_state == "measured":
            result["spend"] = self.spend_usd
        elif self.spend_state == "null":
            result["spend"] = None
        elif self.spend_state == "unknown":
            result["spend"] = "unknown"
        if self.prompt_tokens is not None:
            result["prompt_tokens"] = self.prompt_tokens
        if self.completion_tokens is not None:
            result["completion_tokens"] = self.completion_tokens
        if self.total_tokens is not None:
            result["total_tokens"] = self.total_tokens
        result.update(dict(self.cache_tokens))
        return result


@runtime_checkable
class AccountingProvider(Protocol):
    """Adapter boundary for a retained provider export; it must make no network calls."""

    name: str

    def normalize_rows(self, rows: Iterable[Mapping[str, Any]]) -> tuple[SanitizedSpendRow, ...]: ...


class LiteLLMAccountingProvider:
    """Normalizes fields emitted by LiteLLM ``/spend/logs/v2`` exports."""

    name = "litellm"
    _CACHE_FIELDS = (
        "cache_read_input_tokens", "cache_creation_input_tokens", "cache_read_tokens",
        "cache_creation_tokens", "cache_read_input_token_count", "cache_creation_input_token_count",
    )

    def normalize_rows(self, rows: Iterable[Mapping[str, Any]]) -> tuple[SanitizedSpendRow, ...]:
        output: list[SanitizedSpendRow] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise ProviderAccountingError("provider accounting row is malformed")
            request_id = _required_text(raw.get("request_id"), field="request_id")
            if "spend" not in raw:
                spend, state = None, "missing"
            elif raw.get("spend") is None:
                spend, state = None, "null"
            elif isinstance(raw.get("spend"), str) and raw.get("spend").strip().casefold() == "unknown":
                spend, state = None, "unknown"
            else:
                spend, state = _number(raw.get("spend"), field="spend"), "measured"
            cache = tuple(sorted((field, _token(raw[field])) for field in self._CACHE_FIELDS if field in raw and raw[field] is not None))
            row = SanitizedSpendRow(
                request_id=request_id, model=_optional_text(raw.get("model")), model_id=_optional_text(raw.get("model_id")),
                model_group=_optional_text(raw.get("model_group")), api_base_sha256=_observed_api_base_sha256(raw),
                api_key_hash=_api_key_hash(raw.get("api_key_hash", raw.get("api_key"))), spend_usd=spend,
                spend_state=state, prompt_tokens=_token(raw.get("prompt_tokens")),
                completion_tokens=_token(raw.get("completion_tokens")), total_tokens=_token(raw.get("total_tokens")),
                cache_tokens=cache, status=_optional_text(raw.get("status")),
                # Prisma-backed exports can serialize a DateTime without an offset.
                # The export contract declares those database values UTC and normalizes
                # them before persistence; configured scope/coverage times stay explicit.
                start_time=_utc_time(raw.get("startTime", raw.get("start_time")), required=False, naive_is_utc=True)[1],
                end_time=_utc_time(raw.get("endTime", raw.get("end_time")), required=False, naive_is_utc=True)[1],
            )
            output.append(SanitizedSpendRow(**{**row.accounting_fields(), "source_record_sha256": _hash(row.accounting_fields())}))
        return tuple(output)


def create_accounting_provider(provider: str = "litellm") -> AccountingProvider:
    if isinstance(provider, str) and provider.strip().casefold() == "litellm":
        return LiteLLMAccountingProvider()
    raise ProviderAccountingError("unknown accounting provider")


@dataclass(frozen=True)
class AccountingBinding:
    expected_model: str
    expected_model_id: str
    route_model_identity: str
    price_provenance: PriceProvenance | Mapping[str, Any]
    expected_api_base: str = ""

    def __post_init__(self) -> None:
        for name in ("expected_model", "expected_model_id", "route_model_identity"):
            object.__setattr__(self, name, _required_text(getattr(self, name), field=name))
        object.__setattr__(self, "expected_api_base", _safe_api_base(self.expected_api_base))
        provenance = self.price_provenance
        if isinstance(provenance, Mapping):
            provenance = PriceProvenance(**dict(provenance))
        if not isinstance(provenance, PriceProvenance) or not provenance.valid or provenance.currency != "USD":
            raise ProviderAccountingError("price provenance is unavailable")
        if provenance.route_model_identity != self.route_model_identity:
            raise ProviderAccountingError("price provenance does not match configured binding")
        object.__setattr__(self, "price_provenance", provenance)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "price_provenance": asdict(self.price_provenance)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AccountingBinding":
        _reject_extra_fields(value, set(cls.__dataclass_fields__))
        return cls(**dict(value))

    def matches(self, row: SanitizedSpendRow) -> bool:
        return bool(row.model == self.expected_model and row.model_id == self.expected_model_id
                    and (not self.expected_api_base or row.api_base_sha256 == _api_base_sha256(self.expected_api_base)))


@dataclass(frozen=True)
class AttemptSpendExpectation:
    attempt_id: str
    request_ids: tuple[str, ...] = ()
    api_key_hash: str = ""
    start_time: str = ""
    end_time: str = ""
    expected_request_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt_id", _required_text(self.attempt_id, field="attempt_id"))
        ids = tuple(sorted(_required_text(item, field="request_id") for item in self.request_ids))
        if len(set(ids)) != len(ids):
            raise ProviderAccountingError("attempt request ids are ambiguous")
        object.__setattr__(self, "request_ids", ids)
        scoped = bool(self.api_key_hash or self.start_time or self.end_time or self.expected_request_count is not None)
        if bool(ids) == scoped:
            raise ProviderAccountingError("attempt spend selector is malformed")
        if scoped:
            object.__setattr__(self, "api_key_hash", _api_key_hash(self.api_key_hash))
            start, start_text = _utc_time(self.start_time, required=True)
            end, end_text = _utc_time(self.end_time, required=True)
            if start is None or end is None or start >= end:
                raise ProviderAccountingError("attempt spend window is malformed")
            if self.expected_request_count is not None and (
                type(self.expected_request_count) is not int or self.expected_request_count < 0
            ):
                raise ProviderAccountingError("attempt request count is malformed")
            object.__setattr__(self, "start_time", start_text)
            object.__setattr__(self, "end_time", end_text)

    @property
    def scoped(self) -> bool:
        return not self.request_ids

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AttemptSpendExpectation":
        _reject_extra_fields(value, set(cls.__dataclass_fields__))
        payload = dict(value)
        payload["request_ids"] = tuple(payload.get("request_ids") or ())
        return cls(**payload)


@dataclass(frozen=True)
class SpendExportCoverage:
    complete: bool
    start_time: str
    end_time: str
    source_hash: str = ""

    def __post_init__(self) -> None:
        if type(self.complete) is not bool:
            raise ProviderAccountingError("export coverage is malformed")
        start, start_text = _utc_time(self.start_time, required=True)
        end, end_text = _utc_time(self.end_time, required=True)
        if start is None or end is None or start >= end:
            raise ProviderAccountingError("export coverage is malformed")
        object.__setattr__(self, "start_time", start_text)
        object.__setattr__(self, "end_time", end_text)
        object.__setattr__(self, "source_hash", _optional_text(self.source_hash))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SpendExportCoverage":
        _reject_extra_fields(value, set(cls.__dataclass_fields__))
        return cls(**dict(value))


@dataclass(frozen=True)
class ProviderAccountingReport:
    provider: str
    binding: AccountingBinding
    coverage: SpendExportCoverage
    expectations: tuple[AttemptSpendExpectation, ...]
    sanitized_rows: tuple[SanitizedSpendRow, ...]
    attempt_costs: Mapping[str, CostMeasurement]
    complete: bool
    complete_total_usd: float | None
    priced_subtotal_usd: float
    unavailable_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "binding": self.binding.to_dict(), "coverage": self.coverage.to_dict(),
            "expectations": [item.to_dict() for item in self.expectations],
            "sanitized_rows": [item.to_dict() for item in self.sanitized_rows],
            "attempt_costs": {key: asdict(self.attempt_costs[key]) for key in sorted(self.attempt_costs)},
            "complete": self.complete, "complete_total_usd": self.complete_total_usd,
            "priced_subtotal_usd": self.priced_subtotal_usd,
            "unavailable_reasons": list(self.unavailable_reasons),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderAccountingReport":
        _reject_extra_fields(value, set(cls.__dataclass_fields__))
        costs = value.get("attempt_costs") or {}
        if not isinstance(costs, Mapping):
            raise ProviderAccountingError("accounting report is malformed")
        for cost in costs.values():
            if not isinstance(cost, Mapping):
                raise ProviderAccountingError("accounting report is malformed")
            _reject_extra_fields(cost, set(CostMeasurement.__dataclass_fields__))
        return cls(
            provider=_required_text(value.get("provider"), field="provider"),
            binding=AccountingBinding.from_dict(value.get("binding") or {}),
            coverage=SpendExportCoverage.from_dict(value.get("coverage") or {}),
            expectations=tuple(AttemptSpendExpectation.from_dict(item) for item in value.get("expectations") or ()),
            sanitized_rows=tuple(SanitizedSpendRow.from_dict(item) for item in value.get("sanitized_rows") or ()),
            attempt_costs={str(key): CostMeasurement.from_dict(item) for key, item in sorted(costs.items())},
            complete=value.get("complete") is True, complete_total_usd=value.get("complete_total_usd"),
            priced_subtotal_usd=_number(value.get("priced_subtotal_usd"), field="priced_subtotal_usd"),
            unavailable_reasons=tuple(sorted(str(item) for item in value.get("unavailable_reasons") or ())),
        )


def _normalize_expectations(value: Sequence[AttemptSpendExpectation] | Mapping[str, Sequence[str]]) -> tuple[AttemptSpendExpectation, ...]:
    if isinstance(value, Mapping):
        value = tuple(AttemptSpendExpectation(attempt_id=str(key), request_ids=tuple(ids)) for key, ids in value.items())
    expectations = tuple(sorted(value, key=lambda item: item.attempt_id))
    if not expectations or len({item.attempt_id for item in expectations}) != len(expectations):
        raise ProviderAccountingError("attempt expectations are ambiguous")
    seen_ids: set[str] = set()
    scoped: list[AttemptSpendExpectation] = []
    for item in expectations:
        if not isinstance(item, AttemptSpendExpectation):
            raise ProviderAccountingError("attempt expectation is malformed")
        overlap = seen_ids.intersection(item.request_ids)
        if overlap:
            raise ProviderAccountingError("attempt request ids overlap")
        seen_ids.update(item.request_ids)
        if item.scoped:
            scoped.append(item)
    for index, left in enumerate(scoped):
        left_start = _utc_time(left.start_time, required=True)[0]
        left_end = _utc_time(left.end_time, required=True)[0]
        for right in scoped[index + 1:]:
            if left.api_key_hash != right.api_key_hash:
                continue
            right_start = _utc_time(right.start_time, required=True)[0]
            right_end = _utc_time(right.end_time, required=True)[0]
            if left_start < right_end and right_start < left_end:
                raise ProviderAccountingError("attempt spend windows overlap")
    return expectations


def _deduplicate(rows: Sequence[SanitizedSpendRow]) -> tuple[SanitizedSpendRow, ...]:
    by_request: dict[str, SanitizedSpendRow] = {}
    for row in rows:
        prior = by_request.get(row.request_id)
        if prior is None:
            by_request[row.request_id] = row
        elif prior.accounting_fields() != row.accounting_fields():
            raise ProviderAccountingConflict("provider accounting records conflict")
    return tuple(sorted(by_request.values(), key=lambda row: row.request_id))


def _sum_axis(rows: Sequence[SanitizedSpendRow], field: str) -> int | None:
    values = [getattr(row, field) for row in rows]
    return None if any(value is None for value in values) else sum(values)


def reconcile_provider_spend(
    provider: AccountingProvider,
    rows: Iterable[Mapping[str, Any]],
    expectations: Sequence[AttemptSpendExpectation] | Mapping[str, Sequence[str]],
    binding: AccountingBinding,
    coverage: SpendExportCoverage,
) -> ProviderAccountingReport:
    """Reconcile a complete, already-collected provider export without provider I/O.

    ``complete_total_usd`` is null until every retained row assigns to exactly one
    expectation, every expected row is present, price/model binding holds, and the
    caller explicitly asserted complete export coverage.  ``priced_subtotal_usd``
    remains the sum of known assigned charges for diagnosis, never an invented price.
    """
    if not isinstance(provider, AccountingProvider):
        raise ProviderAccountingError("accounting provider is malformed")
    if not isinstance(binding, AccountingBinding) or not isinstance(coverage, SpendExportCoverage):
        raise ProviderAccountingError("provider accounting configuration is malformed")
    expected = _normalize_expectations(expectations)
    normalized = _deduplicate(provider.normalize_rows(rows))
    coverage_start = _utc_time(coverage.start_time, required=True)[0]
    coverage_end = _utc_time(coverage.end_time, required=True)[0]
    reasons: set[str] = set()
    if not coverage.complete:
        reasons.add("export_not_complete")
    assignments: dict[str, list[SanitizedSpendRow]] = {item.attempt_id: [] for item in expected}
    by_request = {request_id: item.attempt_id for item in expected for request_id in item.request_ids}
    scoped = tuple(item for item in expected if item.scoped)
    for row in normalized:
        exact_attempt_id = by_request.get(row.request_id)
        row_start = _utc_time(row.start_time, required=False)[0]
        matches = [] if row_start is None else [
            item for item in scoped
            if item.api_key_hash == row.api_key_hash
            and _utc_time(item.start_time, required=True)[0] <= row_start < _utc_time(item.end_time, required=True)[0]
        ]
        if exact_attempt_id is not None and matches:
            reasons.add("ambiguous_provider_record_assignment")
            continue
        if exact_attempt_id is not None:
            assignments[exact_attempt_id].append(row)
        elif len(matches) == 1:
            assignments[matches[0].attempt_id].append(row)
        else:
            reasons.add("unassigned_provider_record")
    coverage_unavailable_attempts: set[str] = set()
    for item in scoped:
        start = _utc_time(item.start_time, required=True)[0]
        end = _utc_time(item.end_time, required=True)[0]
        if coverage_start > start or coverage_end < end:
            reasons.add("export_window_not_covered")
            coverage_unavailable_attempts.add(item.attempt_id)
    attempt_costs: dict[str, CostMeasurement] = {}
    subtotal = Decimal("0")
    for item in expected:
        selected = tuple(sorted(assignments[item.attempt_id], key=lambda row: row.request_id))
        attempt_reasons: set[str] = set()
        if not coverage.complete:
            attempt_reasons.add("export_not_complete")
        if item.attempt_id in coverage_unavailable_attempts:
            attempt_reasons.add("export_window_not_covered")
        if item.request_ids:
            found = {row.request_id for row in selected}
            if found != set(item.request_ids):
                attempt_reasons.add("missing_provider_record")
        elif item.expected_request_count is None:
            attempt_reasons.add("expected_request_count_unavailable")
        elif len(selected) != item.expected_request_count:
            attempt_reasons.add("unexpected_provider_record_count")
        if not selected:
            attempt_reasons.add("no_provider_spend_records")
        if any(not binding.matches(row) for row in selected):
            attempt_reasons.add("model_or_route_binding_mismatch")
        if any(row.spend_state != "measured" for row in selected):
            attempt_reasons.add("provider_spend_unavailable")
        known = sum((Decimal(str(row.spend_usd)) for row in selected
                     if row.spend_state == "measured" and row.spend_usd is not None), Decimal("0"))
        subtotal += known
        input_tokens = _sum_axis(selected, "prompt_tokens") if selected else None
        output_tokens = _sum_axis(selected, "completion_tokens") if selected else None
        total_tokens = _sum_axis(selected, "total_tokens") if selected else None
        if attempt_reasons:
            attempt_costs[item.attempt_id] = CostMeasurement.from_dict({
                "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens,
                "monetary_cost_usd": None, "monetary_cost_status": "unavailable",
            })
            reasons.update(attempt_reasons)
        else:
            attempt_costs[item.attempt_id] = CostMeasurement.from_dict({
                "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens,
                "model_call_count": len(selected), "monetary_cost_usd": float(known),
                "monetary_cost_status": "measured", "price_provenance": asdict(binding.price_provenance),
            })
    complete = not reasons
    return ProviderAccountingReport(
        provider=provider.name, binding=binding, coverage=coverage, expectations=expected,
        sanitized_rows=normalized, attempt_costs={key: attempt_costs[key] for key in sorted(attempt_costs)},
        complete=complete, complete_total_usd=float(subtotal) if complete else None,
        priced_subtotal_usd=float(subtotal), unavailable_reasons=tuple(sorted(reasons)),
    )
