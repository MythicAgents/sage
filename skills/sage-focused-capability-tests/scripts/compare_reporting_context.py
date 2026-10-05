"""Replay frozen paired reporting contexts with no executable tools.

Default is offline validation. The caller supplies a prospective plan, owns paid
authority/accounting, and independently judges the retained answers. This measures
report generation, not assembled-agent action safety or retrieval effectiveness.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import time

import httpx


def load_helpers():
    path = Path(__file__).with_name("run_real_model_human_led_case.py")
    spec = importlib.util.spec_from_file_location("reporting_provider_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate_plan(plan):
    if not isinstance(plan.get("cases"), list) or not plan["cases"]:
        raise ValueError("nonempty cases required")
    seen = set()
    for case in plan["cases"]:
        if not isinstance(case.get("id"), str) or case["id"] in seen:
            raise ValueError("unique case IDs required")
        seen.add(case["id"])
        if set(case.get("arms", {})) != {"full", "minimal"}:
            raise ValueError("both reporting arms required")
        for messages in case["arms"].values():
            if not messages or any(set(m) != {"role", "content"} or
                    m["role"] not in {"system", "user", "assistant"} or
                    not isinstance(m["content"], str) for m in messages):
                raise ValueError("plain-text reporting messages required")
        # Identical final source packet/question is a checked comparison invariant.
        if case["arms"]["full"][-1] != case["arms"]["minimal"][-1]:
            raise ValueError("paired arms must share the final evidence/question packet")
    settings = plan.get("generation", {})
    if set(settings) - {"temperature", "top_p"}:
        raise ValueError("unsupported generation setting")


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


async def run(plan, args, helpers, profile):
    route = {"provider": profile["provider"], "model": profile["model"],
             "endpoint_sha256": hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest()}
    if route["model"] != args.expected_model or route["endpoint_sha256"] != args.expected_endpoint_sha256:
        raise ValueError("route binding mismatch")
    if args.output.exists():
        raise ValueError("output already exists; preserve prior attempts")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {"plan_sha256": digest(plan), "route": route, "attempts": [],
              "started_at": datetime.now(timezone.utc).isoformat(),
              "status": "running", "observed_tokens": 0, "provider_requests": 0,
              "limits": {k: getattr(args, k) for k in
                         ("max_calls", "max_tokens", "max_wall", "output_tokens", "repeats")}}
    write_json(args.output, result)
    started = time.monotonic()
    endpoint = profile["API_ENDPOINT"].rstrip("/") + "/chat/completions"
    transport = httpx.AsyncHTTPTransport(verify=helpers.sage_tls_context(), retries=0)
    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=60,
                                follow_redirects=False) as client:
        for repeat in range(args.repeats):
            for case in plan["cases"]:
                for arm in (("full", "minimal") if repeat % 2 == 0 else ("minimal", "full")):
                    remaining = args.max_wall - (time.monotonic() - started)
                    if (remaining <= 0 or result["provider_requests"] >= args.max_calls or
                            result["observed_tokens"] >= args.max_tokens):
                        result["status"] = "resource_limit"
                        write_json(args.output, result)
                        return result
                    body = {"model": profile["model"], "messages": case["arms"][arm],
                            "max_completion_tokens": args.output_tokens, "stream": False,
                            **plan.get("generation", {})}
                    row = {"case": case["id"], "repeat": repeat + 1, "arm": arm,
                           "request": body, "status": "started"}
                    result["attempts"].append(row)
                    result["provider_requests"] += 1
                    write_json(args.output, result)
                    call_started = time.monotonic()
                    try:
                        async with asyncio.timeout(min(60, remaining)):
                            response = await client.post(endpoint, json=body,
                                headers={"Authorization": "Bearer " + profile["API_KEY"]})
                        row["http_status"] = response.status_code
                        response.raise_for_status()
                        data = response.json()
                        row["response"] = data
                        tokens = data.get("usage", {}).get("total_tokens")
                        if type(tokens) is not int or tokens < 0:
                            raise ValueError("unknown usage")
                        result["observed_tokens"] += tokens
                        row["reported_response_cost_usd"] = response.headers.get("x-litellm-response-cost")
                        row["status"] = "observed"
                    except Exception as exc:
                        row.update(status="error", error_type=type(exc).__name__)
                        result["status"] = "stopped_unknown_usage_or_provider_error"
                    finally:
                        row["elapsed_seconds"] = round(time.monotonic() - call_started, 3)
                        write_json(args.output, result)
                    if row["status"] != "observed":
                        return result
    result.update(status="complete", elapsed_seconds=round(time.monotonic() - started, 3))
    write_json(args.output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--expected-model")
    parser.add_argument("--expected-endpoint-sha256")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=200000)
    parser.add_argument("--max-wall", type=int, default=900)
    parser.add_argument("--output-tokens", type=int, default=2048)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text())
    validate_plan(plan)
    if any(getattr(args, name) <= 0 for name in
           ("repeats", "max_calls", "max_tokens", "max_wall", "output_tokens")):
        parser.error("limits must be positive")
    calls = len(plan["cases"]) * 2 * args.repeats
    if calls > args.max_calls:
        parser.error("planned calls exceed limit")
    if not args.run:
        print(json.dumps({"status": "offline_validated", "calls": calls, "plan_sha256": digest(plan)}))
        return 0
    if not args.config or not args.expected_model or not args.expected_endpoint_sha256:
        parser.error("paid execution needs config and exact route bindings")
    helpers = load_helpers()
    result = asyncio.run(run(plan, args, helpers, helpers.load_profile(args.config)))
    print(json.dumps({key: result[key] for key in
                      ("status", "provider_requests", "observed_tokens")}))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
