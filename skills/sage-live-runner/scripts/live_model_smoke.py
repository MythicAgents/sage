#!/usr/bin/env python3
"""Opt-in two-turn harmless native Sage chat provider smoke."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SAGE_RUNTIME_DIR = REPO_ROOT / "Payload_Type" / "sage"
dotenv_bootstrap = _load("dotenv_bootstrap_live_model_smoke", SAGE_RUNTIME_DIR / "dotenv_bootstrap.py")
native_chat = _load("native_chat_live_model_smoke", Path(__file__).with_name("native_chat.py"))
phoenix_reader = _load(
    "phoenix_reader_live_model_smoke",
    REPO_ROOT / "Payload_Type" / "sage" / "evals" / "phoenix_reader.py",
)
retention = _load(
    "artifact_retention_live_model_smoke",
    REPO_ROOT / "skills" / "sage-artifact-retention" / "scripts" / "artifact_retention.py",
)
PHOENIX_DB = Path(os.environ.get("PHOENIX_DB", REPO_ROOT / "Payload_Type/sage/.phoenix/phoenix.db"))
TASK_STATE_QUERY = """
query SmokeTaskState($operationId: Int!) {
  task_aggregate(where: {operation_id: {_eq: $operationId}}) { aggregate { count } }
  task(where: {operation_id: {_eq: $operationId}}, order_by: {id: desc}, limit: 1) { id }
}
"""


def configured_identity() -> dict[str, Any]:
    return native_chat._chat_runtime_identity_from_metadata(native_chat.canary_ai_metadata(max_steps=8))


def require_identity(identity: dict[str, Any]) -> None:
    provider = str(identity.get("provider") or "").casefold()
    model = str(identity.get("model") or "").casefold()
    sentinels = ("fake", "null", "mock", "test")
    if not provider or not model or any(term in provider or term in model for term in sentinels):
        raise RuntimeError("configured provider/model must be nonempty and non-sentinel")


def _string_leaves(value: Any):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except Exception:
            decoded = None
        if isinstance(decoded, (dict, list)):
            yield from _string_leaves(decoded)
        else:
            yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _string_leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from _string_leaves(child)


def _last_turn_request(input_value: Any) -> str | None:
    last_request: str | None = None
    decoder = json.JSONDecoder()
    for text in _string_leaves(input_value):
        cursor = 0
        while True:
            marker = text.find("[turn-authority]", cursor)
            if marker < 0:
                break
            last_request = None
            opening = text.find("{", marker)
            cursor = marker + 1
            if opening < 0:
                continue
            try:
                envelope, _ = decoder.raw_decode(text[opening:])
            except Exception:
                continue
            if isinstance(envelope, dict) and type(envelope.get("request_id")) is str:
                last_request = envelope["request_id"]
    return last_request


def _is_channel_generation(thread_id: Any, channel_id: int) -> bool:
    if type(thread_id) is not str:
        return False
    prefix = f"{channel_id}:generation:"
    generation = thread_id[len(prefix):] if thread_id.startswith(prefix) else ""
    return len(generation) == 32 and all(char in "0123456789abcdef" for char in generation)


async def task_state(client: Any) -> dict[str, Any]:
    operation_id = getattr(client, "current_operation_id", None)
    if type(operation_id) is not int:
        raise RuntimeError("Mythic operation identity is unavailable")
    result = await native_chat.mythic.execute_custom_query(client, TASK_STATE_QUERY, variables={"operationId": operation_id})
    count = ((result.get("task_aggregate") or {}).get("aggregate") or {}).get("count")
    rows = result.get("task") or []
    max_id = rows[0].get("id") if rows else None
    if type(count) is not int or (max_id is not None and type(max_id) is not int):
        raise RuntimeError("Mythic task state has invalid identity")
    return {"count": count, "max_id": max_id}


def phoenix_request_evidence(
    db_path: Path,
    baseline: int,
    *,
    channel_id: int,
    request_ids: list[int],
    identity: dict[str, Any],
) -> dict[str, Any]:
    expected = {f"chat:{channel_id}:request:{request_id}": str(request_id) for request_id in request_ids}
    per_request = {
        str(request_id): {
            "model_calls": 0,
            "tokens": 0,
            "zero_token_calls": 0,
            "trace_ids": set(),
            "trace_rowids": set(),
            "thread_ids": set(),
            "providers": set(),
            "models": set(),
        }
        for request_id in request_ids
    }
    with phoenix_reader._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT t.rowid, t.trace_id, s.span_kind, s.attributes, "
            "COALESCE(s.llm_token_count_prompt, 0), COALESCE(s.llm_token_count_completion, 0), s.status_code "
            "FROM traces t JOIN spans s ON s.trace_rowid = t.rowid WHERE t.rowid > ? ORDER BY t.rowid, s.id",
            (baseline,),
        ).fetchall()

    error_rowids = {int(rowid) for rowid, _trace, _kind, _attrs, _prompt, _completion, status in rows if str(status or "").upper() == "ERROR"}
    for rowid, trace_id, span_kind, raw_attributes, prompt, completion, _status in rows:
        if str(span_kind or "").upper() != "LLM":
            continue
        try:
            attributes = json.loads(str(raw_attributes or ""))
        except Exception:
            continue
        if not isinstance(attributes, dict):
            continue
        metadata = attributes.get("metadata")
        input_container = attributes.get("input")
        if not isinstance(metadata, dict) or not isinstance(input_container, dict):
            continue
        request_key = expected.get(_last_turn_request(input_container.get("value")) or "")
        thread_id = metadata.get("thread_id")
        if request_key is None or not _is_channel_generation(thread_id, channel_id):
            continue
        item = per_request[request_key]
        token_count = int(prompt or 0) + int(completion or 0)
        item["model_calls"] += 1
        item["tokens"] += token_count
        item["zero_token_calls"] += int(token_count <= 0)
        item["trace_ids"].add(str(trace_id or ""))
        item["trace_rowids"].add(int(rowid))
        item["thread_ids"].add(str(thread_id))
        item["providers"].add(str(metadata.get("ls_provider") or ""))
        item["models"].add(str(metadata.get("ls_model_name") or ""))

    matched_rowids = set().union(*(item["trace_rowids"] for item in per_request.values()))
    result = {
        "model_calls": sum(item["model_calls"] for item in per_request.values()),
        "tokens": sum(item["tokens"] for item in per_request.values()),
        "error_count": len(matched_rowids & error_rowids),
        "per_request": {},
    }
    for request_key, item in per_request.items():
        result["per_request"][request_key] = {
            key: sorted(value) if isinstance(value, set) else value
            for key, value in item.items()
            if key != "trace_rowids"
        }
    return result


def validate_phoenix_evidence(
    evidence: dict[str, Any], *, request_ids: list[int], identity: dict[str, Any]
) -> dict[str, Any]:
    expected_provider = str(identity.get("provider") or "")
    expected_model = str(identity.get("model") or "")
    items = [evidence.get("per_request", {}).get(str(request_id), {}) for request_id in request_ids]
    if any(item.get("model_calls", 0) < 1 or item.get("tokens", 0) <= 0 for item in items):
        raise RuntimeError("Phoenix provider-model proof missing for one or more requests")
    if any(item.get("zero_token_calls", 0) for item in items):
        raise RuntimeError("Phoenix matched a zero-token model call")
    if any(item.get("providers") != [expected_provider] or item.get("models") != [expected_model] for item in items):
        raise RuntimeError("Phoenix observed provider/model does not match configuration")
    thread_ids = set().union(*(set(item.get("thread_ids") or []) for item in items))
    if len(thread_ids) != 1:
        raise RuntimeError("Phoenix request traces do not share one channel generation")
    trace_sets = [set(item.get("trace_ids") or []) for item in items]
    if any("" in traces for traces in trace_sets) or not trace_sets[0].isdisjoint(trace_sets[1]):
        raise RuntimeError("Phoenix request trace identities are missing or shared")
    if evidence.get("error_count", 0):
        raise RuntimeError("Phoenix matched trace contains an error")
    return {
        "model_calls": evidence["model_calls"],
        "tokens": evidence["tokens"],
        "error_count": evidence["error_count"],
        "thread_id": next(iter(thread_ids)),
        "trace_ids": sorted(set().union(*trace_sets)),
        "per_request": evidence["per_request"],
    }


async def wait_for_phoenix_evidence(
    db_path: Path,
    baseline: int,
    *,
    channel_id: int,
    request_ids: list[int],
    identity: dict[str, Any],
    timeout_seconds: float = 15.0,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    previous: dict[str, Any] | None = None
    unchanged_eligible_polls = 0
    while True:
        current = phoenix_request_evidence(db_path, baseline, channel_id=channel_id, request_ids=request_ids, identity=identity)
        eligible = all(
            (current.get("per_request") or {}).get(str(request_id), {}).get("model_calls", 0) >= 1
            and (current.get("per_request") or {}).get(str(request_id), {}).get("tokens", 0) > 0
            for request_id in request_ids
        )
        unchanged_eligible_polls = (
            unchanged_eligible_polls + 1
            if eligible and current == previous
            else 0
        )
        if unchanged_eligible_polls >= 2:
            return validate_phoenix_evidence(current, request_ids=request_ids, identity=identity)
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("Phoenix traces did not settle before timeout")
        previous = current
        await asyncio.sleep(poll_seconds)


def _assistant_text(snapshot: dict[str, Any]) -> str:
    messages: list[str] = []
    for row in snapshot["messages"]:
        if row.get("chat_request_id") is None or row.get("author_type") != "ai" or row.get("deleted") is True:
            continue
        metadata = row.get("metadata")
        containers = [metadata] if isinstance(metadata, dict) else []
        nested = metadata.get("container_metadata") if isinstance(metadata, dict) else None
        if isinstance(nested, dict):
            containers.append(nested)
        if any(
            container.get("special_type") not in (None, "")
            or container.get("tool_use")
            or container.get("input_requested")
            or container.get("delegation_id")
            or container.get("delegation_name")
            for container in containers
        ):
            continue
        messages.append(str(row.get("message") or ""))
    return "\n".join(messages).strip()


def has_delimited_token(text: str, token: str) -> bool:
    cursor = 0
    while token:
        start = text.find(token, cursor)
        if start < 0:
            return False
        end = start + len(token)
        left = text[start - 1] if start else ""
        right = text[end] if end < len(text) else ""
        if (not left or not (left.isalnum() or left in "_-")) and (not right or not (right.isalnum() or right in "_-")):
            return True
        cursor = start + 1
    return False


def _validate(snapshot: dict[str, Any], request_id: int, channel_id: int) -> dict[str, Any]:
    transcript = native_chat.build_transcript_export(snapshot)
    if transcript["chat_request_id"] != request_id or transcript["chat_channel_id"] != channel_id:
        raise RuntimeError("native transcript identity drift")
    if str(transcript["status"]).casefold() not in {"complete", "completed"}:
        raise RuntimeError("native request did not complete")
    if transcript.get("error") or native_chat._pending_input_requested_messages(transcript["messages"]):
        raise RuntimeError("native request error or pending HITL")
    if not _assistant_text(transcript):
        raise RuntimeError("native request returned no assistant text")
    return transcript


def retain_artifacts(transcripts: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    manifested = 0
    for transcript in transcripts:
        path = native_chat.default_transcript_export_path(transcript["chat_request_id"])
        native_chat.write_transcript_export(path, transcript)
        native_chat.record_transcript_export(path, transcript["chat_request_id"])
        manifested += 1
    retention.write_json_artifact("reports/live-model-smoke", "report.json", report, artifact_type="live-model-smoke-report", root=REPO_ROOT)
    return {"manifested": manifested + 1}


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    if not args.live_test:
        raise RuntimeError("live model smoke requires explicit --live-test acknowledgement")
    dotenv_bootstrap.load_sage_dotenv(str(SAGE_RUNTIME_DIR))
    identity = configured_identity()
    require_identity(identity)
    baseline = phoenix_reader.max_trace_rowid(PHOENIX_DB)
    client = await native_chat.login()
    channel = await native_chat.create_locked_channel(client, metadata=native_chat.canary_ai_metadata(max_steps=8))
    channel_id = channel["chat_channel_id"]
    if channel.get("chat_runtime_identity") != identity:
        raise RuntimeError("channel runtime identity does not match configured provider/model/route")
    nonce = args.nonce or f"SAGE-{secrets.token_hex(12)}"
    baseline_tasks = await task_state(client)
    transcripts = []
    requests = []
    prompts = [f"Remember this exact random nonce for my next message: {nonce}", "Reply with the exact nonce I asked you to remember."]
    for prompt in prompts:
        created = await native_chat.create_message(client, channel_id, prompt)
        request_id = created["chat_request_id"]
        if request_id in requests:
            raise RuntimeError("duplicate native request identity")
        snapshot = await native_chat.wait_for_request(client, request_id, timeout_seconds=args.timeout, stop_on_input_requested=True)
        transcript = _validate(snapshot, request_id, channel_id)
        if await task_state(client) != baseline_tasks:
            raise RuntimeError("Mythic task count or maximum id changed")
        requests.append(request_id)
        transcripts.append(transcript)
    if not has_delimited_token(_assistant_text(transcripts[1]), nonce):
        raise RuntimeError("second turn did not recall the exact nonce")
    metrics = await wait_for_phoenix_evidence(
        PHOENIX_DB,
        baseline,
        channel_id=channel_id,
        request_ids=requests,
        identity=identity,
    )
    report = {"schema": "sage-live-model-smoke-v1", "status": "passed", "chat_channel_id": channel_id, "chat_request_ids": requests, "runtime_identity": identity, "phoenix": metrics, "task_state": baseline_tasks}
    retain_artifacts(transcripts, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-test", action="store_true", help="Explicitly acknowledge one real-provider/Mythic smoke.")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--nonce", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(json.dumps(asyncio.run(run_smoke(args)), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"live model smoke refused: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
