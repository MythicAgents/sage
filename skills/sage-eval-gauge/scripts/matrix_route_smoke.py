#!/usr/bin/env python3
"""Produce private matrix route evidence with one explicit, bounded provider smoke.

Without --execute this command only checks local configuration and prints a recipe.
The provider query and model invocation never touch Mythic or the range.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[3]
PAYLOAD = ROOT / "Payload_Type" / "sage"
SCRIPTS = Path(__file__).resolve().parent
if str(PAYLOAD) not in sys.path:
    sys.path.insert(0, str(PAYLOAD))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import orchestrate
import run_trust_walker_pilot as pilot
from ai.hillclimb import live_seams
from ssl_bootstrap import configure_sage_ssl_cert_file

DEFAULT_CONTRACT = pilot.DEFAULT_MATRIX_CONTRACT
PRIVATE_ROOT = ROOT / ".sage_history"
RECEIPT_NAME = "route-smoke.json"
PROBE_NAME = "matrix-runtime-route-probe.json"
REQUEST_TIMEOUT_SECONDS = 10
MAX_OUTPUT_TOKENS = 8


class RouteSmokeError(RuntimeError):
    pass


def configured_route(contract_file: Path, route_id: str) -> tuple[Any, dict[str, str]]:
    selected = pilot.load_matrix_contract(contract_file).route_for(route_id)
    # Uses exactly the pilot's dotenv precedence and route equality guard.
    route = pilot._route(orchestrate, selected)
    if route["provider"] != "openai":
        raise RouteSmokeError("route probe currently supports the OpenAI-compatible adapter only")
    return selected, route


def _catalog_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/models", "", ""))


def probe_catalog(route: Mapping[str, str]) -> dict[str, Any]:
    """Check the configured endpoint's advertised model without an inference call."""
    import httpx

    parsed = urlsplit(route["api_endpoint"])
    is_openrouter = parsed.hostname == "openrouter.ai"
    record: dict[str, Any] = {
        "provider": route["provider"],
        "model": route["model"],
        "endpoint_host": parsed.hostname,
        "endpoint_port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "endpoint_path": parsed.path,
        "reachable": False,
        "listed": False,
        "inference_calls": 0,
        "zdr_query": is_openrouter,
    }
    configure_sage_ssl_cert_file()
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = client.get(
                _catalog_url(route["api_endpoint"]),
                headers={"Authorization": f"Bearer {route['api_key']}"},
                params={"zdr": "true"} if is_openrouter else None,
            )
        if response.status_code != 200:
            record["error"] = "catalog_http_status"
            record["http_status"] = response.status_code
            return record
        record["reachable"] = True
        body = response.json()
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            record["error"] = "catalog_shape"
            return record
        record["listed"] = any(
            isinstance(item, dict) and item.get("id") == route["model"] for item in rows
        )
        if not record["listed"]:
            record["error"] = "model_not_listed"
    except (httpx.HTTPError, ValueError) as exc:
        record["error"] = type(exc).__name__
    return record


def run_smoke(route: Mapping[str, str]) -> dict[str, Any]:
    """Use the pilot's production model factory and response-derived metadata."""
    ok, value = live_seams.model_route_auth_smoke(
        route["provider"], route["model"],
        api_key=route["api_key"], base_url=route["api_endpoint"],
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    if not ok:
        # The helper returns an allowlisted description today. Keep only a fixed
        # failure class here so future adapter changes cannot persist provider prose.
        return {"ok": False, "error": "model_route_request_failed"}
    try:
        detail = json.loads(value)
    except (TypeError, ValueError):
        return {"ok": False, "error": "smoke_detail_malformed"}
    backend = detail.get("observed_backend") if isinstance(detail, dict) else None
    usage = detail.get("usage") if isinstance(detail, dict) else None
    expected_backend = f"{route['provider']}:{route['model']}"
    accepted = (
        isinstance(backend, dict)
        and backend.get("effective_backend") == expected_backend
        and backend.get("observed_provider") == route["provider"]
        and backend.get("observed_model") == route["model"]
        and isinstance(usage, dict)
        and type(usage.get("total_tokens")) is int
        and usage["total_tokens"] > 0
    )
    record: dict[str, Any] = {"ok": bool(accepted), "detail": detail}
    if not accepted:
        record["error"] = "backend_or_usage_mismatch"
    return record


def execute(contract_file: Path, route_id: str, output_dir: Path) -> dict[str, Any]:
    selected, route = configured_route(contract_file, route_id)
    output_dir = output_dir.expanduser().resolve()
    try:
        output_dir.relative_to(PRIVATE_ROOT.resolve())
    except ValueError as exc:
        raise RouteSmokeError("route evidence must be under the private .sage_history directory") from exc
    receipt_path = output_dir / RECEIPT_NAME
    probe_path = output_dir / PROBE_NAME
    if receipt_path.exists() or probe_path.exists():
        raise RouteSmokeError("route receipt already exists; choose a new output directory")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(output_dir.stat().st_mode) & 0o077:
        raise RouteSmokeError("route evidence output directory must have private permissions")
    probe = probe_catalog(route)
    if probe["reachable"] and probe["listed"]:
        receipt = run_smoke(route)
    else:
        receipt = {"ok": False, "error": "catalog_probe_failed"}
    receipt["created_at"] = datetime.now(UTC).isoformat()
    # The catalog and inference evidence are separate so the pilot can reject either.
    with os.fdopen(os.open(probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
        json.dump(probe, file, indent=2, sort_keys=True)
        file.write("\n")
    with os.fdopen(os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
        json.dump(receipt, file, indent=2, sort_keys=True)
        file.write("\n")
    return {
        "route_id": selected.route_id,
        "ok": receipt.get("ok") is True,
        "catalog_reachable": probe["reachable"],
        "model_listed": probe["listed"],
        "zdr_query": probe["zdr_query"],
        "created_at": receipt["created_at"],
        "route_smoke": str(receipt_path),
        "catalog_probe": str(probe_path),
        "usage": receipt.get("detail", {}).get("usage", {}) if isinstance(receipt.get("detail"), dict) else {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--contract-file", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="make one provider catalog query and one model request")
    args = parser.parse_args(argv)
    try:
        selected, _ = configured_route(args.contract_file, args.route_id)
        if not args.execute:
            result = {
                "mode": "offline",
                "route_id": selected.route_id,
                "catalog_query": "GET models; OpenRouter adds zdr=true",
                "provider_calls": 0,
                "planned_model_requests": 1,
                "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "output_dir": str(args.output_dir.expanduser().resolve()),
                "execute_flag_required": True,
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        result = execute(args.contract_file, args.route_id, args.output_dir)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 2
    except (pilot.PilotError, RouteSmokeError, OSError) as exc:
        # The low-level request path suppresses provider response bodies and credentials.
        print(f"route probe failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
