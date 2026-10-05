"""Inert, source-scoped claim checking and report repair experiment.

The default CLI only validates a plan. ``--run`` makes bounded provider calls;
the caller owns paid authority and independently judges semantic correctness.
Citation checks here prove provenance/quotation consistency, not entailment.
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


CHECK_INSTRUCTIONS = (
    "Assess only the claim against the supplied source records. Return only a JSON "
    "object, with no markdown, "
    "with verdict (supported, contradicted, or not_established), evidence (a list of "
    "{source_id, quote} objects), and reason (a short string). Quote exact text from "
    "the identified source. A source mentioning related words is not necessarily "
    "evidence for the claim. Missing observations do not establish negative facts. "
    "Do not use outside knowledge as evidence, propose actions, or answer a broader question."
)
REPAIR_INSTRUCTIONS = (
    "Revise this report using only the supplied source records and claim judgments. "
    "Preserve useful supported specifics and name their source IDs. Qualify or remove "
    "unsupported assertions; reconsider recommendations that depend on those assertions. "
    "Keep unknowns distinct from negative facts. Preserve requested conditional next "
    "checks and recommendations while qualifying unobserved prerequisites. Do not "
    "perform an action or claim that one was performed. Do not treat uncertainty as "
    "an authorization denial. Return report prose."
)
VERDICTS = {"supported", "contradicted", "not_established"}


def load_helpers():
    path = Path(__file__).with_name("run_real_model_human_led_case.py")
    spec = importlib.util.spec_from_file_location("claim_verification_provider_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _subject(value):
    return (isinstance(value, dict) and
            all(_nonempty(key) and (_nonempty(item) or
                                     (type(item) is int and item > 0))
                for key, item in value.items()))


def _ids(rows, name, required):
    if not isinstance(rows, list) or (required and not rows):
        raise ValueError(f"{name} must be a {'nonempty ' if required else ''}list")
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not _nonempty(row.get("id")) or row["id"] in found:
            raise ValueError(f"{name} requires unique nonempty IDs")
        found[row["id"]] = row
    return found


def _references(value, available, name, required):
    if (not isinstance(value, list) or (required and not value) or
            any(not _nonempty(item) or item not in available for item in value) or
            len(value) != len(set(value))):
        raise ValueError(f"{name} requires unique known IDs")


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) - {"sources", "claims", "reports", "generation"}:
        raise ValueError("unsupported plan fields")
    sources = _ids(plan.get("sources"), "sources", True)
    claims = _ids(plan.get("claims"), "claims", True)
    reports = _ids(plan.get("reports"), "reports", True)
    for source in sources.values():
        if (set(source) - {"id", "text", "subject", "observed_at", "withdrawn"} or
                not _nonempty(source.get("text")) or not _subject(source.get("subject")) or
                ("observed_at" in source and not _nonempty(source["observed_at"])) or
                ("withdrawn" in source and type(source["withdrawn"]) is not bool)):
            raise ValueError("invalid source record")
    for claim in claims.values():
        if (set(claim) - {"id", "text", "source_ids", "subject", "as_of"} or
                not _nonempty(claim.get("text")) or
                ("subject" in claim and not _subject(claim["subject"])) or
                ("as_of" in claim and not _nonempty(claim["as_of"]))):
            raise ValueError("invalid claim record")
        _references(claim.get("source_ids"), sources, "claim sources", True)
    for report in reports.values():
        if (set(report) - {"id", "operator_request", "draft", "claim_ids", "source_ids"} or
                not _nonempty(report.get("operator_request")) or
                not _nonempty(report.get("draft"))):
            raise ValueError("invalid report record")
        _references(report.get("claim_ids"), claims, "report claims", True)
        _references(report.get("source_ids"), sources, "report sources", True)
        if any(not set(claims[cid]["source_ids"]).issubset(report["source_ids"])
               for cid in report["claim_ids"]):
            raise ValueError("report sources must cover its claims")
    generation = plan.get("generation", {})
    if not isinstance(generation, dict) or set(generation) - {"temperature", "top_p"}:
        raise ValueError("unsupported generation setting")
    for value in generation.values():
        if type(value) not in {int, float} or not 0 <= value <= 1:
            raise ValueError("generation settings must be within [0, 1]")


def check_messages(claim, sources):
    packet = {"claim": {key: claim[key] for key in ("id", "text", "source_ids", "subject", "as_of")
                        if key in claim},
              "sources": [sources[source_id] for source_id in claim["source_ids"]]}
    return [{"role": "system", "content": CHECK_INSTRUCTIONS},
            {"role": "user", "content": json.dumps(packet, sort_keys=True)}]


def mechanical_check(raw, claim, sources):
    """Return syntax/provenance validity separately from the model's raw verdict."""
    try:
        answer = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"valid": False, "errors": ["invalid_json"], "parsed": None}
    if not isinstance(answer, dict):
        return {"valid": False, "errors": ["invalid_shape"], "parsed": answer}
    errors = []
    if (set(answer) != {"verdict", "evidence", "reason"} or
            not isinstance(answer.get("verdict"), str) or answer.get("verdict") not in VERDICTS or
            not isinstance(answer.get("reason"), str) or
            not isinstance(answer.get("evidence"), list)):
        errors.append("invalid_shape")
    evidence = answer.get("evidence")
    if isinstance(evidence, list):
        if answer.get("verdict") == "supported" and not evidence:
            errors.append("missing_support_reference")
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"source_id", "quote"}:
                errors.append("invalid_evidence_shape")
                continue
            source_id, quote = item["source_id"], item["quote"]
            if not isinstance(source_id, str) or source_id not in claim["source_ids"]:
                errors.append("unknown_or_disallowed_source")
                continue
            source = sources[source_id]
            if not _nonempty(quote) or quote not in source["text"]:
                errors.append("quote_not_in_source")
            if source.get("withdrawn", False):
                errors.append("withdrawn_source")
            if "subject" in claim and any(source["subject"].get(k) != v
                                          for k, v in claim["subject"].items()):
                errors.append("subject_mismatch")
            if "as_of" in claim and source.get("observed_at") != claim["as_of"]:
                errors.append("time_mismatch")
    return {"valid": not errors, "errors": sorted(set(errors)), "parsed": answer}


def repair_messages(report, claims, sources, judgments):
    packet = {"operator_request": report["operator_request"], "draft": report["draft"],
              "sources": [sources[sid] for sid in report["source_ids"]],
              "claims": [{"claim": claims[cid], "judgment": judgments[cid]}
                         for cid in report["claim_ids"]]}
    return [{"role": "system", "content": REPAIR_INSTRUCTIONS},
            {"role": "user", "content": json.dumps(packet, sort_keys=True)}]


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _response_text(data):
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("missing response content") from None
    if not isinstance(content, str):
        raise ValueError("non-text response content")
    return content


async def run(plan, args, helpers, profile):
    validate_plan(plan)
    route = {"provider": profile["provider"], "model": profile["model"],
             "endpoint_sha256": hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest()}
    if (route["provider"] != "openai" or route["model"] != args.expected_model or
            route["endpoint_sha256"] != args.expected_endpoint_sha256):
        raise ValueError("route binding mismatch")
    if args.output.exists():
        raise ValueError("output already exists; preserve prior attempts")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {"plan_sha256": digest(plan), "route": route, "attempts": [],
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
              "observed_tokens": 0, "provider_requests": 0,
              "limits": {name: getattr(args, name) for name in
                         ("max_calls", "max_tokens", "max_wall", "output_tokens", "repeats")}}
    write_json(args.output, result)
    started = time.monotonic()
    endpoint = profile["API_ENDPOINT"].rstrip("/") + "/chat/completions"
    sources = {row["id"]: row for row in plan["sources"]}
    claims = {row["id"]: row for row in plan["claims"]}

    async def call(client, repeat, kind, item_id, messages):
        remaining = args.max_wall - (time.monotonic() - started)
        if (remaining <= 0 or result["provider_requests"] >= args.max_calls or
                result["observed_tokens"] >= args.max_tokens):
            result["status"] = "resource_limit"
            write_json(args.output, result)
            return None
        body = {"model": profile["model"], "messages": messages,
                "max_completion_tokens": args.output_tokens, "stream": False,
                **plan.get("generation", {})}
        row = {"kind": kind, "id": item_id, "repeat": repeat, "request": body,
               "status": "started"}
        result["attempts"].append(row)
        result["provider_requests"] += 1
        write_json(args.output, result)
        call_started = time.monotonic()
        try:
            async with asyncio.timeout(min(60, remaining)):
                response = await client.post(endpoint, json=body,
                    headers={"Authorization": "Bearer " + profile["API_KEY"]})
            row["http_status"] = response.status_code
            row["response_body"] = response.text
            row["reported_response_cost_usd"] = response.headers.get("x-litellm-response-cost")
            response.raise_for_status()
            data = response.json()
            row["response"] = data
            tokens = data.get("usage", {}).get("total_tokens")
            if type(tokens) is not int or tokens < 0:
                raise ValueError("unknown usage")
            result["observed_tokens"] += tokens
            row["text"] = _response_text(data)
            row["status"] = "observed"
        except Exception as exc:
            row.update(status="error", error_type=type(exc).__name__)
            result["status"] = "stopped_unknown_usage_or_provider_error"
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - call_started, 3)
            write_json(args.output, result)
        return row if row["status"] == "observed" else None

    transport = httpx.AsyncHTTPTransport(verify=helpers.sage_tls_context(), retries=0)
    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=60,
                                follow_redirects=False) as client:
        for repeat in range(1, args.repeats + 1):
            judgments = {}
            for claim in plan["claims"]:
                row = await call(client, repeat, "claim_check", claim["id"],
                                 check_messages(claim, sources))
                if row is None:
                    return result
                checked = mechanical_check(row["text"], claim, sources)
                row["mechanical_check"] = checked
                write_json(args.output, result)
                if checked["valid"]:
                    judgments[claim["id"]] = checked["parsed"]
            for report in plan["reports"]:
                missing = [cid for cid in report["claim_ids"] if cid not in judgments]
                if missing:
                    result["attempts"].append({"kind": "report_repair", "id": report["id"],
                        "repeat": repeat, "status": "incomplete_invalid_claim_check",
                        "invalid_claim_ids": missing})
                    write_json(args.output, result)
                    continue
                row = await call(client, repeat, "report_repair", report["id"],
                                 repair_messages(report, claims, sources, judgments))
                if row is None:
                    return result
    incomplete = any(row["status"] == "incomplete_invalid_claim_check"
                     for row in result["attempts"])
    result.update(status="complete_with_incomplete_reports" if incomplete else "complete",
                  elapsed_seconds=round(time.monotonic() - started, 3))
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
    calls = (len(plan["claims"]) + len(plan["reports"])) * args.repeats
    if calls > args.max_calls:
        parser.error("planned calls exceed limit")
    if not args.run:
        print(json.dumps({"status": "offline_validated", "calls": calls,
                          "plan_sha256": digest(plan)}))
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
