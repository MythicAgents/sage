"""Reproducible provider-cost sidecars for evaluation, never runtime imports.

Original outcome ledgers remain immutable. Accounting views supplement resource
measurements only; they do not amend outcomes or establish live budget enforcement.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from . import experiment_contracts as contracts
from .provider_accounting import (
    AccountingBinding, AttemptSpendExpectation, SpendExportCoverage,
    SanitizedSpendRow, create_accounting_provider, reconcile_provider_spend,
)

SCHEMA = "sage.eval.provider-accounting.v1"


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluator_bindings() -> dict[str, str]:
    root = Path(__file__).parent
    return {name: _hash_file(root / name) for name in (
        "accounting_report.py", "provider_accounting.py", "experiment_contracts.py",
        "accounting_cli.py", "litellm_accounting.py", "phoenix_accounting.py", "phoenix_report.py",
        "run_gauge_live.py", "bare_runner.py", "live_seams.py",
        "../langgraph/policy.py", "../langgraph/autonomous_controller.py", "../langgraph/model.py",
    )}


def load_json(path: Path) -> Any:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-finite JSON number")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates,
                      parse_constant=reject_constant)


def _manifest(value: Mapping[str, Any]):
    if not isinstance(value, Mapping) or set(value) != {
        "provider", "campaign_id", "binding", "coverage", "attempts",
    }:
        raise ValueError("invalid accounting manifest fields")
    if not isinstance(value["campaign_id"], str) or not value["campaign_id"].strip():
        raise ValueError("accounting campaign identity required")
    if not isinstance(value["attempts"], list):
        raise ValueError("accounting attempt selectors must be a list")
    provider = create_accounting_provider(value["provider"])
    binding = AccountingBinding.from_dict(value["binding"])
    provenance = binding.price_provenance
    if provenance.artifact_path:
        artifact = Path(provenance.artifact_path)
        if not artifact.is_absolute():
            raise ValueError("pricing artifact reference must be absolute")
        if _hash_file(artifact) != provenance.artifact_sha256.removeprefix("sha256:"):
            raise ValueError("pricing artifact changed")
    coverage = SpendExportCoverage.from_dict(value["coverage"])
    expectations = tuple(AttemptSpendExpectation.from_dict(item) for item in value["attempts"])
    safe = {"provider": value["provider"], "campaign_id": value["campaign_id"],
            "binding": asdict(binding), "coverage": asdict(coverage),
            "attempts": [asdict(item) for item in expectations]}
    # Convert tuples and other JSON-compatible dataclass values to one canonical shape.
    safe = json.loads(contracts.canonical_json(safe))
    return provider, binding, coverage, expectations, safe


def _ledger_rows(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("ledger must contain JSON objects")
    return rows


def _scoped_attempt_rows(rows: list[dict], campaign_id: str) -> list[dict]:
    return [row for row in rows if row.get("campaign_id") == campaign_id and (
        row.get("attempt_id") or row.get("supersedes_attempt_id")
        or row.get("replacement_for_attempt_id") or row.get("record_type") == "result")]


def _validate_ledger_scope(rows: list[dict], kind: str, campaign_id: str,
                           attempt_ids: set[str]) -> None:
    if kind == "attempts":
        scoped = _scoped_attempt_rows(rows, campaign_id)
        resolution = contracts.resolve_attempt_records(scoped)
        if not resolution.valid:
            raise ValueError("invalid original attempt ledger")
        # Amendments share their original attempt's resource facts; replacements spent anew.
        expected = {row["attempt_id"] for row in scoped if not row.get("supersedes_attempt_id")}
    elif kind == "pilot":
        starts = [row for row in rows if row.get("record_type") == "arm_started"]
        expected = {row.get("arm") for row in starts}
        if any(not isinstance(arm, str) or not arm for arm in expected) or len(expected) != len(starts):
            raise ValueError("ambiguous pilot arm accounting")
        results = [row.get("arm") for row in rows if row.get("record_type") == "arm_result"]
        if len(results) != len(set(results)) or set(results) - expected:
            raise ValueError("orphaned or duplicate pilot result")
    else:
        raise ValueError("unknown accounting ledger kind")
    if expected != attempt_ids:
        raise ValueError("accounting selectors do not cover all spent attempts")


def build_bundle(manifest: Mapping[str, Any], raw_rows, *, ledger_path: Path | None = None,
                 ledger_kind: str = "attempts") -> dict[str, Any]:
    provider, binding, coverage, expectations, safe_manifest = _manifest(manifest)
    report = reconcile_provider_spend(provider, raw_rows, expectations, binding, coverage)
    ledger = None
    if ledger_path is not None:
        rows = _ledger_rows(ledger_path)
        _validate_ledger_scope(rows, ledger_kind, safe_manifest["campaign_id"],
                               {item.attempt_id for item in expectations})
        ledger = {"path": str(ledger_path.resolve()), "sha256": _hash_file(ledger_path),
                  "kind": ledger_kind}
    return {"schema": SCHEMA, "evaluator_bindings": evaluator_bindings(),
            "manifest": safe_manifest, "ledger": ledger,
            "spend_rows": [row.to_dict() for row in report.sanitized_rows],
            "report": report.to_dict()}


def verify_bundle(path: Path) -> dict[str, Any]:
    bundle = load_json(path)
    from . import phoenix_report
    if isinstance(bundle, dict) and bundle.get("schema") == phoenix_report.SCHEMA:
        return phoenix_report.verify_phoenix_bundle(bundle)
    if not isinstance(bundle, dict) or set(bundle) != {
        "schema", "evaluator_bindings", "manifest", "ledger", "spend_rows", "report",
    } or bundle["schema"] != SCHEMA or bundle["evaluator_bindings"] != evaluator_bindings():
        raise ValueError("accounting schema or evaluator source changed")
    ledger = bundle["ledger"]
    ledger_path = None
    if ledger is not None:
        if not isinstance(ledger, dict) or set(ledger) != {"path", "sha256", "kind"}:
            raise ValueError("invalid accounting ledger reference")
        ledger_path = Path(ledger["path"])
        if _hash_file(ledger_path) != ledger["sha256"]:
            raise ValueError("accounting ledger changed")
    raw = [SanitizedSpendRow.from_dict(row).to_provider_row() for row in bundle["spend_rows"]]
    reproduced = build_bundle(bundle["manifest"], raw, ledger_path=ledger_path,
                              ledger_kind=ledger["kind"] if ledger else "attempts")
    if contracts.canonical_json(reproduced) != contracts.canonical_json(bundle):
        raise ValueError("accounting report does not reproduce")
    return bundle


def validate_price_provenance(price: contracts.PriceProvenance) -> None:
    if not price.valid or price.currency != "USD":
        raise ValueError("prospective USD pricing unavailable")
    if price.artifact_path and (
        not Path(price.artifact_path).is_absolute()
        or _hash_file(Path(price.artifact_path)) != price.artifact_sha256.removeprefix("sha256:")
    ):
        raise ValueError("prospective price artifact unavailable or changed")


def accounted_campaign_start(path: Path, *, limits: contracts.CostLimits,
                             next_attempt_price: contracts.PriceProvenance,
                             required_axes: frozenset[str] = frozenset(),
                             expected_ledger_path: Path | None = None,
                             expected_campaign_id: str | None = None):
    """Apply existing stop-loss arithmetic to a verified, in-memory resource view.

    This is a pre-attempt check, not a reservation or an in-flight spending guard.
No original ledger row, hash, result, or amendment is written or reclassified.
    """
    validate_price_provenance(next_attempt_price)
    bundle = verify_bundle(path)
    ledger = bundle["ledger"]
    if not ledger or ledger["kind"] != "attempts":
        raise ValueError("campaign budget requires a typed attempt ledger")
    ledger_path = Path(ledger["path"])
    campaign_id = bundle["manifest"]["campaign_id"]
    if expected_ledger_path is not None and ledger_path.resolve() != expected_ledger_path.resolve():
        raise ValueError("accounting belongs to another ledger")
    if expected_campaign_id is not None and campaign_id != expected_campaign_id:
        raise ValueError("accounting belongs to another campaign")
    report = bundle["report"]
    if not report["complete"]:
        raise ValueError("campaign accounting is incomplete")
    if "violated" in report.get("declared_run_ceiling_dispositions", {}).values():
        raise ValueError("retained attempt violates its declared run ceiling")
    rows = _scoped_attempt_rows(_ledger_rows(ledger_path), campaign_id)
    roots: dict[str, str] = {}
    derived = []
    for row in rows:
        attempt_id = row["attempt_id"]
        roots[attempt_id] = roots[row["supersedes_attempt_id"]] if row.get("supersedes_attempt_id") else attempt_id
        imported = contracts.CostMeasurement.from_dict(report["attempt_costs"][roots[attempt_id]])
        original = contracts.CostMeasurement.from_dict(row.get("cost"), schema_version=row.get("schema_version", 1))
        if original.monetary_cost_countable and original.monetary_cost_usd != imported.monetary_cost_usd:
            raise ValueError("imported charge conflicts with recorded cost")
        # Model usage is an additional provider measure. Runtime token/task/time
        # axes keep their original semantics (upstream retries may differ).
        cost = original if original.monetary_cost_countable else replace(
            original, monetary_cost_usd=imported.monetary_cost_usd,
            monetary_cost_status=imported.monetary_cost_status,
            price_provenance=imported.price_provenance, not_applicable_reason="")
        view = {key: value for key, value in row.items() if key != "record_hash"}
        view["cost"] = asdict(cost)
        view["record_hash"] = contracts.content_hash(view)
        derived.append(view)
    return contracts.campaign_start_disposition(derived, limits=limits, required_axes=required_axes,
                                                 next_attempt_price=next_attempt_price)


def write_bundle(path: Path, value: Mapping[str, Any]) -> None:
    """Create a private artifact once; never overwrite existing evidence."""
    import os
    text = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
