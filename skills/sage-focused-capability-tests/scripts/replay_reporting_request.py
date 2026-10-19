"""Validate or replay the frozen four-call reporting request comparison.

Validation is offline by default. ``--run`` makes only provider chat-completion
requests; returned tool calls are retained as response bytes and never dispatched.
The caller owns paid authority, reservation, and external ledger reconciliation.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time

import httpx


ROOT = Path(__file__).resolve().parents[3]
ORDER = ("original", "qualified_prior_prose", "qualified_prior_prose", "original")
LIMITS = {"max_provider_calls": 4, "max_observed_tokens": 60000,
          "max_wall_seconds": 300, "max_call_seconds": 60,
          "automatic_retries": 0, "max_completion_tokens": 2048}
MAX_RESPONSE_BYTES = 4_000_000
HANDBACK_TOOL = "handback_to_supervisor"
HANDBACK_CHOICE = {"tool_choice": {"type": "function",
                                   "function": {"name": HANDBACK_TOOL}}}
HANDBACK_OUTCOMES = {"progress", "handoff", "blocked", "complete"}


def load_helpers():
    path = Path(__file__).with_name("run_real_model_human_led_case.py")
    spec = importlib.util.spec_from_file_location("reporting_replay_provider_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value: object) -> str:
    wire = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def validate_packet(packet: object, *, root: Path | None = None) -> dict:
    root = root or ROOT
    if not isinstance(packet, dict):
        raise ValueError("invalid packet")
    source_info = packet.get("source")
    route = packet.get("route")
    limits = packet.get("limits")
    arms = packet.get("arms")
    common_override = packet.get("common_request_override")
    if (not isinstance(source_info, dict) or not isinstance(route, dict) or
            not isinstance(arms, dict) or not isinstance(limits, dict)):
        raise ValueError("missing packet bindings")
    source_hash = source_info.get("file_sha256")
    request_hash = source_info.get("request_sha256")
    route_model = route.get("model")
    endpoint_hash = route.get("endpoint_sha256")
    replacements = packet.get("replacements")
    if (not all(isinstance(value, str) and len(value) == 64 and
                all(char in "0123456789abcdef" for char in value)
                for value in (source_hash, request_hash, endpoint_hash)) or
            not isinstance(route_model, str) or not route_model or
            route.get("provider") != "openai" or set(route) !=
            {"provider", "model", "endpoint_sha256"} or
            limits != LIMITS or packet.get("changed_field") != "/messages/7/content" or
            packet.get("order") != list(ORDER) or
            not isinstance(replacements, list) or len(replacements) != 3 or
            any(not isinstance(item, dict) or set(item) !=
                {"from", "to", "expected_count"} or item["expected_count"] != 1 or
                not isinstance(item["from"], str) or not item["from"] or
                not isinstance(item["to"], str) or not item["to"] or
                item["from"] == item["to"] for item in replacements)):
        raise ValueError("packet identity or bounds changed")
    pointer = source_info.get("request_pointer")
    if (not isinstance(pointer, str) or not pointer.startswith("/provider_requests/") or
            not pointer.endswith("/request")):
        raise ValueError("invalid captured request pointer")
    index_text = pointer[len("/provider_requests/"):-len("/request")]
    if not index_text.isdecimal() or str(int(index_text)) != index_text:
        raise ValueError("invalid captured request index")
    index = int(index_text)
    path = source_info.get("path")
    if not isinstance(path, str) or Path(path).is_absolute():
        raise ValueError("invalid source path")
    source_path = (root / path).resolve(strict=True)
    if not source_path.is_relative_to((root / ".sage_history").resolve()):
        raise ValueError("source outside private history")
    source_bytes = source_path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != source_hash:
        raise ValueError("source file hash mismatch")
    source = json.loads(source_bytes)
    try:
        entry = source["provider_requests"][index]
        original = entry["request"]
    except (TypeError, KeyError, IndexError) as exc:
        raise ValueError("captured request unavailable") from exc
    if (not isinstance(original, dict) or digest(original) != request_hash or
            entry.get("status") != "returned" or entry.get("http_status") != 200):
        raise ValueError("captured request provenance mismatch")
    if ("common_request_overrides" in packet or
            common_override is not None and
            common_override not in ({"tool_choice": "none"}, HANDBACK_CHOICE)):
        raise ValueError("unsupported common request override")
    if "tool_choice" in original and common_override is not None:
        raise ValueError("captured request already has tool_choice")
    expected_original = json.loads(json.dumps(original))
    if common_override is not None:
        expected_original.update(common_override)
    if (set(arms) != {"original", "qualified_prior_prose"} or
            arms["original"] != expected_original):
        raise ValueError("original arm changed")
    messages = original.get("messages")
    if (original.get("model") != route_model or original.get("stream") is not False or
            original.get("max_completion_tokens") != 2048 or
            not isinstance(messages, list) or len(messages) != 9 or
            not isinstance(messages[7].get("content"), str) or
            messages[7].get("role") != "assistant" or
            messages[8].get("role") != "user" or
            not isinstance(original.get("tools"), list) or
            len(original["tools"]) != 25):
        raise ValueError("captured wire shape changed")
    if common_override == HANDBACK_CHOICE and sum(
            isinstance(tool, dict) and tool.get("type") == "function" and
            isinstance(tool.get("function"), dict) and
            tool["function"].get("name") == HANDBACK_TOOL
            for tool in original["tools"]) != 1:
        raise ValueError("named handback tool absent or ambiguous in capture")
    text = messages[7]["content"]
    for replacement in replacements:
        old, new = replacement["from"], replacement["to"]
        if text.count(old) != 1:
            raise ValueError("reviewed source phrase unavailable")
        text = text.replace(old, new, 1)
    candidate = json.loads(json.dumps(expected_original))
    candidate["messages"][7]["content"] = text
    if arms["qualified_prior_prose"] != candidate:
        raise ValueError("qualified arm changed outside reviewed phrases")
    return packet


def validate_route(packet: dict, profile: dict[str, str]) -> None:
    if (profile.get("provider") != "openai" or profile.get("model") != packet["route"]["model"] or
            hashlib.sha256(profile.get("API_ENDPOINT", "").encode()).hexdigest()
            != packet["route"]["endpoint_sha256"] or not profile.get("API_KEY")):
        raise ValueError("provider route mismatch")


def _append(fd: int, value: dict) -> None:
    data = (json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n").encode()
    os.write(fd, data)
    os.fsync(fd)


def _handback_status(choices: object) -> str:
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return "missing_or_multiple_choices"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return "missing_message"
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return "missing_or_multiple_calls"
    call = calls[0]
    function = call.get("function")
    if (call.get("type") not in (None, "function") or
            not isinstance(function, dict) or function.get("name") != HANDBACK_TOOL):
        return "wrong_tool"
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        return "invalid_arguments"
    try:
        values = json.loads(arguments)
    except ValueError:
        return "invalid_arguments"
    if (not isinstance(values, dict) or
            any(not isinstance(values.get(key), str) or not values[key].strip()
                for key in ("reason", "summary")) or
            not isinstance(values.get("outcome"), str) or
            values["outcome"] not in HANDBACK_OUTCOMES or
            "next_owner" in values and not isinstance(values["next_owner"], str)):
        return "invalid_arguments"
    return "valid"


def _response_metadata(raw: bytes, *, require_handback: bool = False) -> dict:
    try:
        response = json.loads(raw)
    except (UnicodeError, ValueError):
        return {"usage_status": "unknown",
                "handback_status": "invalid_json" if require_handback else None}
    if not isinstance(response, dict):
        return {"usage_status": "unknown",
                "handback_status": "invalid_json" if require_handback else None}
    usage = response.get("usage")
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    choices = response.get("choices")
    valid_choices = (isinstance(choices, list) and bool(choices) and
        all(isinstance(choice, dict) and isinstance(choice.get("message"), dict) and
            (isinstance(choice["message"].get("content"), str) or
             isinstance(choice["message"].get("refusal"), str) or
             isinstance(choice["message"].get("tool_calls"), list) and
             bool(choice["message"]["tool_calls"])) for choice in choices))
    return {"usage": usage, "usage_status": "reported" if type(total) is int and total >= 0
            else "unknown", "observed_tokens": total if type(total) is int and total >= 0 else None,
            "reported_model": response.get("model"),
            "answer_status": "interpretable" if valid_choices else "malformed_choices",
            "handback_status": _handback_status(choices) if require_handback else None,
            "finish_reasons": [c.get("finish_reason") for c in choices if isinstance(c, dict)]
            if isinstance(choices, list) else None,
            "returned_tool_calls": any(isinstance(c, dict) and
                isinstance(c.get("message"), dict) and c["message"].get("tool_calls")
                for c in choices) if isinstance(choices, list) else False}


async def run(packet: dict, profile: dict[str, str], output: Path, *,
              transport: httpx.AsyncBaseTransport | None = None,
              tls_context=None) -> dict:
    validate_packet(packet)
    validate_route(packet, profile)
    if not output.parent.is_dir():
        raise ValueError("output parent must exist")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    result = {"status": "running", "provider_requests": 0, "observed_tokens": 0}
    started = time.monotonic()
    endpoint = profile["API_ENDPOINT"].rstrip("/") + "/chat/completions"
    try:
        _append(fd, {"event": "start", "packet_sha256": digest(packet),
                     "route": packet["route"], "limits": LIMITS,
                     "common_request_override": packet.get("common_request_override"),
                     "started_at_utc": datetime.now(timezone.utc).isoformat()})
        if transport is None:
            if tls_context is None:
                raise ValueError("verified TLS context required")
            transport = httpx.AsyncHTTPTransport(verify=tls_context, retries=0)
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                     follow_redirects=False, timeout=60) as client:
            for sequence, arm in enumerate(ORDER, start=1):
                remaining = LIMITS["max_wall_seconds"] - (time.monotonic() - started)
                if remaining <= 0 or result["observed_tokens"] >= LIMITS["max_observed_tokens"]:
                    result["status"] = "resource_limit"
                    break
                body = packet["arms"][arm]
                wire = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode()
                _append(fd, {"event": "attempt", "sequence": sequence, "arm": arm,
                             "request_sha256": digest(body),
                             "wire_body_sha256": hashlib.sha256(wire).hexdigest(),
                             "started_at_utc": datetime.now(timezone.utc).isoformat()})
                result["provider_requests"] += 1
                call_started = time.monotonic()
                try:
                    request = client.build_request("POST", endpoint, content=wire,
                        headers={"Authorization": "Bearer " + profile["API_KEY"],
                                 "Content-Type": "application/json"})
                    if request.method != "POST" or str(request.url) != endpoint:
                        raise ValueError("unexpected provider route")
                    async with asyncio.timeout(min(60, remaining)):
                        response = await client.send(request, stream=True)
                        try:
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > MAX_RESPONSE_BYTES:
                                    raise ValueError("response exceeds capture limit")
                        finally:
                            await response.aclose()
                    metadata = _response_metadata(bytes(raw),
                        require_handback=packet.get("common_request_override") == HANDBACK_CHOICE)
                    row = {"event": "response", "sequence": sequence, "arm": arm,
                           "http_status": response.status_code,
                           "response_body_base64": base64.b64encode(raw).decode(),
                           "response_sha256": hashlib.sha256(raw).hexdigest(),
                           "reported_response_cost_usd": response.headers.get("x-litellm-response-cost"),
                           "elapsed_seconds": round(time.monotonic() - call_started, 3),
                           **metadata}
                    _append(fd, row)
                    if metadata.get("observed_tokens") is not None:
                        result["observed_tokens"] += metadata["observed_tokens"]
                    if result["observed_tokens"] > LIMITS["max_observed_tokens"]:
                        result["status"] = "resource_limit"
                        break
                    if (packet.get("common_request_override") == {"tool_choice": "none"} and
                            metadata.get("returned_tool_calls")):
                        result["status"] = "stopped_tool_choice_violation"
                        break
                    if (response.status_code != 200 or metadata["usage_status"] != "reported" or
                            metadata.get("reported_model") != packet["route"]["model"]):
                        result["status"] = "stopped_provider_or_usage_or_route"
                        break
                    if packet.get("common_request_override") == HANDBACK_CHOICE:
                        if metadata.get("handback_status") != "valid":
                            result["status"] = "stopped_report_format_violation"
                            break
                    elif metadata.get("answer_status") != "interpretable":
                        result["status"] = "stopped_provider_or_usage_or_route"
                        break
                except Exception as exc:
                    _append(fd, {"event": "error", "sequence": sequence, "arm": arm,
                                 "error_type": type(exc).__name__,
                                 "elapsed_seconds": round(time.monotonic() - call_started, 3),
                                 "usage_status": "unknown"})
                    result["status"] = "stopped_provider_or_usage_or_route"
                    break
            else:
                result["status"] = "complete"
        _append(fd, {"event": "finish", **result,
                     "elapsed_seconds": round(time.monotonic() - started, 3),
                     "ended_at_utc": datetime.now(timezone.utc).isoformat()})
        return result
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-packet-sha256")
    parser.add_argument("--expected-model")
    parser.add_argument("--expected-endpoint-sha256")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    packet = validate_packet(json.loads(args.packet.read_text(encoding="utf-8")))
    if args.expected_packet_sha256 and digest(packet) != args.expected_packet_sha256:
        parser.error("packet digest mismatch")
    if not args.run:
        print(json.dumps({"status": "offline_validated", "calls": len(ORDER),
                          "packet_sha256": digest(packet),
                          "source_sha256": packet["source"]["file_sha256"],
                          "request_sha256": packet["source"]["request_sha256"],
                          "endpoint_sha256": packet["route"]["endpoint_sha256"],
                          "model": packet["route"]["model"],
                          "tools": len(packet["arms"]["original"]["tools"]) }))
        return 0
    if (args.config is None or args.output is None or not args.expected_packet_sha256 or
            args.expected_model != packet["route"]["model"] or
            args.expected_endpoint_sha256 != packet["route"]["endpoint_sha256"]):
        parser.error("--run requires config, output, and exact packet/model/endpoint bindings")
    helpers = load_helpers()
    profile = helpers.load_profile(args.config)
    validate_route(packet, profile)
    result = asyncio.run(run(packet, profile, args.output, tls_context=helpers.sage_tls_context()))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
