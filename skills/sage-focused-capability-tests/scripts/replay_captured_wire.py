"""Replay one frozen captured Sage provider request without running Sage tools.

Dry-run is the default. A caller supplies a reviewed case bound to a retained
source request and exact provider route; ``--run`` sends one paid request and
keeps the unmodified response bytes in an exclusive private artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[3]
SAGE = ROOT / "Payload_Type" / "sage"
sys.path.insert(0, str(SAGE))
from ssl_bootstrap import configure_sage_ssl_cert_file

OUTPUT_CAP = 2048
TIMEOUT_SECONDS = 90
MAX_RESPONSE_BYTES = 4_000_000
TRANSFORMS = {"none", "compact_specialist_system", "omit_duplicate_supervisor_answer",
              "payload_handoff_completion_last"}
COMPACT_START = "### 4. Lowest detectable footprint, always\n"
COMPACT_END = "**Handback contract (every return to the Supervisor):**"
COMPACT_PREAMBLE = "Five rules govern every action. When they conflict, the lower-numbered one wins.\n\n"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _source_identity(source: dict) -> tuple[object, object, object]:
    """Accept the retained payload case's native identity without rewriting it."""
    if source.get("case") == "payload-handoff-apollo-v2":
        route = source.get("route")
        if not isinstance(route, dict):
            raise ValueError("payload source route missing")
        return source["case"], route.get("provider"), route.get("model")
    return source.get("scenario"), source.get("provider"), source.get("model")


def payload_handoff_completion_last(original: list[dict]) -> list[dict]:
    """Move the repeated operator input before the completed return, then sanitize.

    Use Sage's shipped message sanitizer and LangChain's installed OpenAI wire
    converters. The final neutral user turn is required by the provider; it adds
    no task or authority. Exact round trips fail closed if converter behavior drifts.
    """
    from langchain_openai.chat_models.base import (
        _convert_dict_to_message, _convert_message_to_dict,
    )
    from ai.langgraph.model import Model

    if (len(original) != 7 or
            [message.get("role") for message in original] !=
            ["system", "user", "assistant", "assistant", "tool", "assistant", "user"] or
            original[1] != original[-1] or
            original[2].get("name") != "Mythic_Payload" or
            original[5].get("name") != "Mythic_Payload" or
            not original[3].get("tool_calls") or
            original[4].get("tool_call_id") != original[3]["tool_calls"][0].get("id")):
        raise ValueError("payload return source shape changed")
    messages = [_convert_dict_to_message(message) for message in original]
    if [_convert_message_to_dict(message) for message in messages] != original:
        raise ValueError("payload return cannot round-trip through LangChain")
    reordered = messages[:2] + messages[-1:] + messages[2:-1]
    sanitized = Model._sanitize_messages(Model.__new__(Model), reordered)
    candidate = [_convert_message_to_dict(message) for message in sanitized]
    if (candidate[:-1] != original[:2] + [original[-1]] + original[2:-1] or
            candidate[-1] != {"role": "user", "content":
                "Internal conversation continuation. The operator's request and authorization are unchanged."}):
        raise ValueError("production sanitizer changed the payload return unexpectedly")
    return candidate


def read_profile(path: Path) -> dict[str, str]:
    wanted = {"provider", "model", "API_ENDPOINT", "API_KEY"}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if key in wanted:
            if not separator or key in values:
                raise ValueError("invalid provider profile")
            values[key] = value.strip().strip('"\'')
    if any(not values.get(key) for key in wanted) or values["provider"] != "openai":
        raise ValueError("incomplete OpenAI-compatible provider profile")
    url = urlsplit(values["API_ENDPOINT"])
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or
            url.password or url.query or url.fragment or url.path.rstrip("/") not in {"", "/v1"}):
        raise ValueError("invalid provider endpoint")
    if url.scheme == "http":
        try:
            loopback = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            loopback = url.hostname == "localhost"
        if not loopback:
            raise ValueError("unencrypted non-loopback endpoint")
    return values


def _check_transform(source: dict, frozen: dict, transform: str, *, scenario: str,
                     request_index: int) -> None:
    if set(source) != set(frozen) or any(source[key] != frozen[key]
                                             for key in source if key != "messages"):
        raise ValueError("frozen request changed non-message fields")
    original = source["messages"]
    candidate = frozen["messages"]
    if not isinstance(original, list) or not isinstance(candidate, list):
        raise ValueError("malformed captured messages")
    if transform == "none":
        if candidate != original:
            raise ValueError("baseline request differs from capture")
    elif transform == "compact_specialist_system":
        if scenario not in {"report-accuracy-fresh-netstat-v1",
                            "report-accuracy-historical-withdrawal-v1"} or request_index != 2:
            raise ValueError("specialist transform lacks source binding")
        if (len(original) != len(candidate) or not original or
                original[0].get("role") != "system" or candidate[0].get("role") != "system" or
                set(original[0]) != set(candidate[0]) or
                any(original[0][key] != candidate[0][key]
                    for key in original[0] if key != "content") or
                not isinstance(candidate[0].get("content"), str) or
                candidate[0]["content"] != compact_system_content(original[0].get("content")) or
                candidate[1:] != original[1:]):
            raise ValueError("specialist candidate changed more than system content")
    elif transform == "omit_duplicate_supervisor_answer":
        if (scenario != "report-accuracy-historical-withdrawal-v1" or request_index != 4 or
                len(original) <= 8 or original[4].get("role") != "assistant" or
                original[4].get("name") != "Mythic_Operator" or
                original[7].get("role") != "assistant" or
                original[7].get("name") != "Supervisor" or
                original[7].get("content") != original[4].get("content") or
                candidate != original[:7] + original[8:]):
            raise ValueError("history candidate is not the exact duplicate removal")
    elif transform == "payload_handoff_completion_last":
        if (scenario != "payload-handoff-apollo-v2" or request_index != 0 or
                candidate != payload_handoff_completion_last(original)):
            raise ValueError("payload return candidate differs from production sanitizer")
    else:
        raise ValueError("unknown replay transform")


def compact_system_content(content: object) -> str:
    """Apply the one reviewed subtractive specialist-context ablation."""
    if not isinstance(content, str) or any(content.count(marker) != 1 for marker in
                                            (COMPACT_PREAMBLE, COMPACT_START, COMPACT_END)):
        raise ValueError("specialist system markers changed")
    start = content.index(COMPACT_START)
    end = content.index(COMPACT_END)
    if start >= end or content.index(COMPACT_PREAMBLE) >= start:
        raise ValueError("specialist system marker order changed")
    return (content[:start] + content[end:]).replace(COMPACT_PREAMBLE, "", 1)


def freeze_case(source_path: Path, request_index: int, transform: str,
                profile: dict[str, str]) -> dict:
    """Construct one exact case from a captured source, without provider activity."""
    if type(request_index) is not int or request_index < 0 or transform not in TRANSFORMS:
        raise ValueError("invalid freeze selection")
    source_path = source_path.resolve(strict=True)
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    if not isinstance(source, dict) or not isinstance(source.get("provider_requests"), list):
        raise ValueError("malformed capture source")
    requests = source["provider_requests"]
    if request_index >= len(requests) or not isinstance(requests[request_index], dict):
        raise ValueError("source request index unavailable")
    original = requests[request_index].get("request")
    if not isinstance(original, dict):
        raise ValueError("source request missing")
    # A JSON round trip ensures no shared nested object is accidentally modified.
    frozen = json.loads(json.dumps(original))
    if transform == "compact_specialist_system":
        frozen["messages"][0]["content"] = compact_system_content(
            original["messages"][0]["content"])
    elif transform == "omit_duplicate_supervisor_answer":
        del frozen["messages"][7]
    elif transform == "payload_handoff_completion_last":
        frozen["messages"] = payload_handoff_completion_last(original["messages"])
    scenario, _, _ = _source_identity(source)
    case = {
        "source_path": str(source_path),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_request_sha256": digest(original),
        "frozen_request_sha256": digest(frozen),
        "request_index": request_index,
        "scenario": scenario,
        "endpoint_sha256": hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest(),
        "transform": transform,
        "request": frozen,
    }
    # The same validator guards both freezing and later paid replay.
    _check_case(case, source_bytes, profile)
    return case


def read_case(path: Path, profile: dict[str, str]) -> tuple[dict, dict]:
    case = json.loads(path.read_text(encoding="utf-8"))
    return _check_case(case, None, profile)


def _check_case(case: object, source_bytes: bytes | None,
                profile: dict[str, str]) -> tuple[dict, dict]:
    required = {"source_path", "source_sha256", "source_request_sha256",
                "frozen_request_sha256", "request_index", "scenario", "endpoint_sha256",
                "transform", "request"}
    if not isinstance(case, dict) or set(case) != required:
        raise ValueError("invalid frozen replay case fields")
    if (not isinstance(case["source_path"], str) or
            not Path(case["source_path"]).is_absolute() or
            type(case["request_index"]) is not int or case["request_index"] < 0 or
            not isinstance(case["request"], dict) or
            case["transform"] not in TRANSFORMS or
            not all(isinstance(case[key], str) and case[key]
                    for key in ("source_sha256", "source_request_sha256",
                                "frozen_request_sha256", "scenario", "endpoint_sha256"))):
        raise ValueError("malformed replay case")
    if source_bytes is None:
        source_bytes = Path(case["source_path"]).read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != case["source_sha256"]:
        raise ValueError("source artifact hash mismatch")
    source = json.loads(source_bytes)
    if not isinstance(source, dict):
        raise ValueError("source request provenance invalid")
    requests = source.get("provider_requests") if isinstance(source, dict) else None
    index = case["request_index"]
    source_scenario, source_provider, source_model = _source_identity(source)
    if source_scenario == "payload-handoff-apollo-v2" and (
        source["route"].get("endpoint_sha256") !=
        hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest()
    ):
        raise ValueError("payload source endpoint binding invalid")
    if (not isinstance(source, dict) or source_scenario != case["scenario"] or
            source_provider != profile["provider"] or
            source_model != profile["model"] or source.get("protocol_valid") is not True or
            not isinstance(requests, list) or index >= len(requests) or
            not isinstance(requests[index], dict) or requests[index].get("status") != "returned" or
            requests[index].get("http_status") != 200 or
            not isinstance(requests[index].get("request"), dict)):
        raise ValueError("source request provenance invalid")
    original = requests[index]["request"]
    frozen = case["request"]
    if (digest(original) != case["source_request_sha256"] or
            digest(frozen) != case["frozen_request_sha256"] or
            frozen.get("model") != profile["model"] or
            frozen.get("max_completion_tokens") != OUTPUT_CAP or
            "max_tokens" in frozen or
            not isinstance(frozen.get("stream"), bool) or
            not isinstance(frozen.get("messages"), list) or
            not isinstance(frozen.get("tools"), list) or
            hashlib.sha256(profile["API_ENDPOINT"].encode()).hexdigest() != case["endpoint_sha256"]):
        raise ValueError("frozen request or route binding invalid")
    _check_transform(original, frozen, case["transform"],
                     scenario=case["scenario"], request_index=index)
    identity = {"scenario": case["scenario"], "request_index": index,
                "transform": case["transform"], "source_sha256": case["source_sha256"],
                "source_request_sha256": case["source_request_sha256"],
                "frozen_request_sha256": case["frozen_request_sha256"],
                "endpoint_sha256": case["endpoint_sha256"], "model": profile["model"],
                "stream": frozen["stream"], "output_cap": OUTPUT_CAP,
                "timeout_seconds": TIMEOUT_SECONDS, "max_requests": 1, "max_retries": 0}
    return frozen, identity


def _usage(raw: bytes, *, streaming: bool) -> tuple[object | None, str]:
    try:
        if not streaming:
            value = json.loads(raw)
            usage = value.get("usage") if isinstance(value, dict) else None
        else:
            usage = None
            for line in raw.decode("utf-8").splitlines():
                if line.startswith("data:") and line[5:].strip() != "[DONE]":
                    chunk = json.loads(line[5:].strip())
                    if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
        return usage, "reported" if isinstance(usage, dict) else "unknown"
    except (UnicodeDecodeError, ValueError):
        return None, "unknown"


async def replay_once(body: dict, profile: dict[str, str], *,
                      transport: httpx.AsyncBaseTransport | None = None) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    def timing() -> dict:
        return {"started_at_utc": started_at,
                "ended_at_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.monotonic() - started, 3)}
    endpoint = profile["API_ENDPOINT"].rstrip("/") + "/chat/completions"
    if transport is None:
        ca = configure_sage_ssl_cert_file(certs_dir=SAGE / "certs")
        verify = ssl.create_default_context(cafile=ca or None)
        transport = httpx.AsyncHTTPTransport(verify=verify, retries=0)
    wire = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    request_count = 0
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS,
                                 trust_env=False, follow_redirects=False) as client:
        request = client.build_request("POST", endpoint, content=wire,
                                       headers={"Authorization": "Bearer " + profile["API_KEY"],
                                                "Content-Type": "application/json"})
        if request.method != "POST" or str(request.url) != endpoint:
            raise ValueError("unexpected replay route")
        request_count += 1
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                response = await client.send(request, stream=True)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError("response exceeds capture limit")
                await response.aclose()
            usage, usage_status = _usage(bytes(raw), streaming=body["stream"])
            return {"status": "returned" if response.status_code == 200 else "http_error",
                    "http_status": response.status_code, "provider_requests": request_count,
                    **timing(),
                    "wire_body_sha256": hashlib.sha256(wire).hexdigest(),
                    "response_body_base64": base64.b64encode(raw).decode(),
                    "response_body_sha256": hashlib.sha256(raw).hexdigest(),
                    "usage": usage, "usage_status": usage_status}
        except Exception as exc:
            return {"status": "error", "error_type": type(exc).__name__,
                    "provider_requests": request_count,
                    **timing(),
                    "wire_body_sha256": hashlib.sha256(wire).hexdigest(),
                    "usage": None, "usage_status": "unknown"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path)
    parser.add_argument("--freeze-only", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--request-index", type=int)
    parser.add_argument("--transform", choices=sorted(TRANSFORMS))
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    profile = read_profile(args.profile)
    if args.freeze_only:
        if (args.run or args.case is not None or args.source is None or
                args.request_index is None or args.transform is None or
                args.output is None or not args.output.parent.is_dir()):
            parser.error("freeze-only requires source, request-index, transform, and exclusive output")
        case = freeze_case(args.source, args.request_index, args.transform, profile)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(case, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps({"mode": "frozen", "output": str(args.output),
                          "scenario": case["scenario"], "request_index": case["request_index"],
                          "transform": case["transform"],
                          "source_request_sha256": case["source_request_sha256"],
                          "frozen_request_sha256": case["frozen_request_sha256"]}, sort_keys=True))
        return 0
    if args.case is None or args.source is not None or args.request_index is not None or args.transform is not None:
        parser.error("replay requires --case and no freeze inputs")
    body, identity = read_case(args.case, profile)
    if not args.run:
        print(json.dumps({"mode": "dry_run", **identity}, sort_keys=True))
        return 0
    if args.output is None or not args.output.parent.is_dir():
        parser.error("--run requires --output in an existing private directory")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(json.dumps({"mode": "captured_wire_replay", **identity,
                                 "response": {"status": "started", "usage_status": "unknown"}}) + "\n")
        output.flush()
        os.fsync(output.fileno())
        try:
            response = asyncio.run(replay_once(body, profile))
        except Exception as exc:
            response = {"status": "error", "error_type": type(exc).__name__,
                        "provider_requests": 0, "usage": None,
                        "usage_status": "unknown"}
        output.seek(0)
        output.truncate()
        output.write(json.dumps({"mode": "captured_wire_replay", **identity,
                                 "response": response}, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
    print(json.dumps({"mode": "captured_wire_replay", "status": response["status"],
                      "output": str(args.output), "usage": response["usage"],
                      "usage_status": response["usage_status"]}, sort_keys=True))
    return 0 if response["status"] == "returned" else 1


if __name__ == "__main__":
    raise SystemExit(main())
