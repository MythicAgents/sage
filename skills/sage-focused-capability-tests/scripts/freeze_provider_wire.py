"""Freeze what the installed OpenAI adapter makes of recorded provider bodies, as a battery fixture.

Reads Sage observation-capture wire files (``model_request``/``model_response`` records), keeps one response per
distinct shape, redacts it, replays it through Sage's real model factory, SDK and adapter on both client paths
(``tests/provider_wire_replay.py``) and writes the projections Sage's consumers read to
``Payload_Type/sage/tests/fixtures/model_input_boundaries/provider_wire_round_trips.json``.

The fixture is the oracle for an SDK upgrade, so this refuses to run unless the installed versions are the ones it
freezes (``ORACLE_VERSIONS``). Lab-specific names are replaced from a private substitutions file
(``{"substitutions": {"<recorded text>": "<replacement>", ...}}``) that is never committed; the bodies are
substituted before they are replayed, so a replacement cannot hide a difference. Offline: no network, no provider.

    .venv/bin/python skills/sage-focused-capability-tests/scripts/freeze_provider_wire.py \
        --wire <capture>/wire.jsonl --wire <capture>/wire.jsonl --substitutions <private.json>
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SAGE = ROOT / "Payload_Type" / "sage"
FIXTURE = SAGE / "tests" / "fixtures" / "model_input_boundaries" / "provider_wire_round_trips.json"
ORACLE_VERSIONS = {"openai": "2.54.0", "langchain-openai": "1.4.2", "langchain-core": "1.5.3"}
# Request parameters the Responses API echoes back; no consumer reads them from a response, and the tool schemas
# and identifiers are not needed to replay the output.
ECHOED_REQUEST_FIELDS = ("tools", "instructions", "metadata", "safety_identifier", "user", "prompt_cache_key",
                         "prompt_cache_retention", "access_programs", "billing")


def _pairs(wire: Path, label: str) -> list[dict]:
    pending: dict[str, dict] = {}
    pairs: list[dict] = []
    for line in wire.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        payload = record.get("record", {}).get("payload")
        if record.get("kind") == "model_request":
            pending[payload["request_id"]] = {"payload": payload, "seq": record["seq"]}
        elif record.get("kind") == "model_response":
            request = pending.pop(payload.get("request_id"), None)
            if request is None or payload.get("outcome") != "returned":
                raise ValueError("unpaired or non-returned provider response in capture")
            pairs.append({"request": json.loads(base64.b64decode(request["payload"]["body_base64"])),
                          "url": request["payload"]["url"], "status": payload["status_code"],
                          "body": base64.b64decode(payload["body_base64"]),
                          "source": f"{label}, request seq {request['seq']}"})
    return pairs


def _substitute(text: str, substitutions: dict[str, str]) -> str:
    for original, replacement in substitutions.items():
        text = text.replace(original, replacement)
    leaked = [original for original in substitutions if original in text]
    if leaked:
        raise ValueError("substitution left recorded text in place")
    return text


def _shape(api: str, status: int, model: str, body: dict) -> tuple:
    if status != 200:
        error = body.get("error") or {}
        return (api, status, model, error.get("code"), error.get("param"))
    output = body.get("output", [])
    return (api, status, model, tuple(item.get("type") for item in output),
            tuple(item.get("name") for item in output if item.get("type") == "function_call"))


def _row_id(index: int, shape: tuple) -> str:
    api, status, model = shape[:3]
    detail = "_".join(str(part) for part in shape[3:] if part) or "error"
    detail = detail.replace("(", "").replace(")", "").replace("'", "").replace(",", "").replace(" ", "_")
    return f"r{index:02d}_{api}_{status}_{model.rsplit('-', 1)[-1]}_{detail}".replace(".", "_")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wire", action="append", required=True, type=Path, help="capture wire.jsonl (repeatable)")
    parser.add_argument("--substitutions", required=True, type=Path, help="private substitutions JSON")
    parser.add_argument("--out", type=Path, default=FIXTURE)
    args = parser.parse_args(argv)

    installed = {name: version(name) for name in ORACLE_VERSIONS}
    if installed != ORACLE_VERSIONS:
        print(f"refusing: installed {installed} differ from the oracle versions {ORACLE_VERSIONS}", file=sys.stderr)
        return 2
    substitutions = json.loads(args.substitutions.read_text(encoding="utf-8"))["substitutions"]

    sys.path.insert(0, str(SAGE))
    from tests import provider_wire_replay as replay

    # Differences an operator accepted against an earlier freeze stay with their rows.
    previous = json.loads(args.out.read_text(encoding="utf-8")) if args.out.is_file() else {"rows": []}
    accepted = {row["id"]: row["accepted_differences"] for row in previous["rows"] if row.get("accepted_differences")}
    # Rows another script added (``added_by``) and their provenance stay as they are.
    added = [row for row in previous["rows"] if row.get("added_by")]
    additions = previous.get("provenance", {}).get("additions")
    rows: list[dict] = []
    seen: set[tuple] = set()
    for number, wire in enumerate(args.wire, start=1):
        for pair in _pairs(wire, f"recorded live check {number} of {len(args.wire)} (2026-11-22)"):
            api = "responses" if pair["url"].endswith("/responses") else "chat_completions"
            body = json.loads(pair["body"])
            if pair["status"] == 200:
                body = {key: value for key, value in body.items() if key not in ECHOED_REQUEST_FIELDS}
            body = json.loads(_substitute(json.dumps(body, ensure_ascii=False), substitutions))
            model = _substitute(pair["request"]["model"], substitutions)
            effort = ((pair["request"].get("reasoning") or {}).get("effort") if api == "responses"
                      else pair["request"].get("reasoning_effort"))
            shape = _shape(api, pair["status"], model, body)
            if shape in seen:
                continue
            seen.add(shape)
            row = {"id": _row_id(len(rows) + 1, shape), "source": pair["source"], "model": model,
                   "route": {"api": api, "reasoning_effort": effort or ""}, "status": pair["status"], "body": body}
            views = []
            for path in replay.PATHS:
                llm, _client, requests = replay.build_model(row, path)
                result = replay.outcome(llm)
                expected_path = "/v1/responses" if api == "responses" else "/v1/chat/completions"
                if requests != [expected_path]:
                    raise ValueError(f"{row['id']} {path}: unexpected provider requests {requests}")
                if isinstance(result, BaseException):
                    views.append({"error": replay.error_view(result, row)})
                else:
                    messages = replay.history(result)
                    views.append({"message": replay.message_view(result),
                                  "history": replay.history_view(messages, configured_model=model),
                                  "checkpoint": replay.checkpoint_bytes(messages)})
            if views[0] != views[1]:
                raise ValueError(f"{row['id']}: the default and injected client paths disagree under the oracle")
            row["expected"] = views[0]
            if row["id"] in accepted:
                row["accepted_differences"] = accepted[row["id"]]
            rows.append(row)

    fixture = {
        "provenance": {
            "kind": "recorded provider output (real provider bodies; outputs frozen from the pre-upgrade adapter)",
            "description": (
                "OpenAI Responses and Chat Completions response bodies recorded by Sage's observation capture in two "
                "live checks on 2026-11-22 (24 responses), one row per distinct shape. Each row's expected values "
                "are what Sage's real model factory, the OpenAI SDK and langchain-openai produced from the redacted "
                "body, projected onto what Sage's consumers read, on both client paths (they agreed)."
            ),
            "oracle_versions": ORACLE_VERSIONS,
            "substitutions": (
                "Lab host and user names renamed to WORKSTATION-A and lab.user; the two model names renamed to "
                "recorded-model-a and recorded-model-b (also inside error messages); echoed request parameters "
                f"({', '.join(ECHOED_REQUEST_FIELDS)}) removed from 200 bodies. Substitution happened before "
                "replay. Provider ids, call ids, arguments, text and encrypted reasoning are otherwise unchanged."
            ),
            "synthetic": "The replay prompt and the tool result in each history are synthetic placeholders.",
            "gap": ("No Chat Completions success body and no Anthropic Messages body was recorded; those routes are "
                    "covered only by synthetic tests and the live check."),
            "dedupe": "Rows keep the first response per (API, status, model, output item types, function names, "
                      "error code, error param).",
        },
        "rows": rows + added,
    }
    if additions:
        fixture["provenance"]["gap"] = previous["provenance"]["gap"]
        fixture["provenance"]["additions"] = additions
    args.out.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} rows to {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
