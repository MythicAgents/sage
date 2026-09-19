"""Opt-in evaluation accounting commands; offline unless `collect` is selected."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from . import accounting_report as reports
from . import experiment_contracts as contracts


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    phoenix = sub.add_parser("phoenix", help="Offline exact-trace token estimate from a local Phoenix database")
    phoenix.add_argument("--config", type=Path, required=True)
    phoenix.add_argument("--output", type=Path, required=True)
    phoenix.add_argument("--ledger", type=Path)
    phoenix.add_argument("--ledger-kind", choices=("attempts", "pilot"), default="attempts")
    for name in ("reconcile", "collect"):
        command = sub.add_parser(name, help="Offline import" if name == "reconcile" else "Explicit read-only gateway spend export")
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--ledger", type=Path)
        command.add_argument("--ledger-kind", choices=("attempts", "pilot"), default="attempts")
        if name == "reconcile":
            command.add_argument("--spend", type=Path, required=True, help="Complete exported JSON array of spend rows")
        else:
            command.add_argument("--gateway-config", type=Path, required=True,
                                 help="Explicit LiteLLM HTTPS endpoint, credential environment name and bounded UTC window")
    verify = sub.add_parser("verify", help="Reproduce retained accounting without network access")
    verify.add_argument("bundle", type=Path)
    budget = sub.add_parser("check-budget", help="Offline next-attempt stop-loss check; does not execute a run")
    budget.add_argument("bundle", type=Path)
    budget.add_argument("--limits", type=Path, required=True)
    budget.add_argument("--next-price", type=Path, required=True, help="Prospective PriceProvenance JSON")
    budget.add_argument("--required-axis", action="append", choices=tuple(contracts.CostLimits.__dataclass_fields__), default=[])
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            bundle = reports.verify_bundle(args.bundle)
            print(json.dumps({"reproduced": True, "report": bundle["report"]}, sort_keys=True, allow_nan=False))
            return 0 if bundle["report"]["complete"] else 2
        if args.command == "check-budget":
            decision = reports.accounted_campaign_start(
                args.bundle, limits=contracts.CostLimits(**reports.load_json(args.limits)),
                next_attempt_price=contracts.PriceProvenance(**reports.load_json(args.next_price)),
                required_axes=frozenset(args.required_axis),
            )
            scope = reports.load_json(args.bundle)["report"].get("accounting_scope", "provider_model_cost")
            print(json.dumps({"accounting_scope": scope,
                              "in_flight_dollar_enforcement": False,
                              "decision": decision.to_dict()}, sort_keys=True, allow_nan=False))
            return 0 if decision.may_start else 2
        # Check configuration before opening a client. Existing output is never overwritten.
        if args.output.exists():
            raise ValueError("accounting output already exists")
        if args.command == "phoenix":
            from .phoenix_report import build_phoenix_bundle
            bundle = build_phoenix_bundle(reports.load_json(args.config), ledger_path=args.ledger,
                                           ledger_kind=args.ledger_kind)
            reports.write_bundle(args.output, bundle)
            print(json.dumps({"schema": bundle["schema"], "report": bundle["report"]}, sort_keys=True, allow_nan=False))
            return 0 if bundle["report"]["complete"] else 2
        manifest = reports.load_json(args.manifest)
        reports._manifest(manifest)
        if args.command == "collect":
            from .litellm_accounting import SpendLogConfig, fetch_spend_logs
            if manifest["provider"] != "litellm":
                raise ValueError("no collector configured for accounting provider")
            config = SpendLogConfig(**reports.load_json(args.gateway_config))
            # Filtering by model could hide billed retries/fallbacks from the campaign.
            if config.model is not None:
                raise ValueError("complete accounting collection cannot filter by model")
            coverage = manifest["coverage"]
            if (_utc(coverage["start_time"]) < _utc(config.start_date)
                    or _utc(coverage["end_time"]) > _utc(config.end_date)):
                raise ValueError("collection does not cover the declared accounting interval")
            if config.api_key_hash is not None:
                selectors = manifest["attempts"]
                if any(item.get("api_key_hash") != config.api_key_hash for item in selectors):
                    raise ValueError("collection key filter does not cover every accounting selector")
            fetched = fetch_spend_logs(config)
            if not fetched.completed:
                raise ValueError("spend collection incomplete")
            raw_rows = fetched.rows
        else:
            raw_rows = reports.load_json(args.spend)
            # A v2 page is not a complete export, even if its advertised count is small.
            if not isinstance(raw_rows, list):
                raise ValueError("spend import requires a complete exported row array")
        bundle = reports.build_bundle(manifest, raw_rows, ledger_path=args.ledger, ledger_kind=args.ledger_kind)
        reports.write_bundle(args.output, bundle)
        print(json.dumps({"schema": bundle["schema"], "report": bundle["report"]}, sort_keys=True, allow_nan=False))
        return 0 if bundle["report"]["complete"] else 2
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        # Provider bodies and source documents can contain credentials. Only the
        # stable error category crosses this operator boundary, never exception text.
        del exc
        print(json.dumps({"complete": False, "error": "accounting failed; check configuration, source bindings, ledger and spend evidence"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
