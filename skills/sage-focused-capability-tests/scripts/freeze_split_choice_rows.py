"""Add Chat Completions battery rows for a reply the provider splits across choices, and for single-choice replies.

Writes rows into ``Payload_Type/sage/tests/fixtures/model_input_boundaries/provider_wire_round_trips.json``. It
replaces the rows it wrote before (``added_by``) and keeps every other row. Two private, read-only sources:

* Sage observation-capture wire files (``model_request``/``model_response`` records). Each distinct single-choice
  Chat Completions 200 body becomes a row, recorded as it arrived. Its expected projection is what the installed
  factory, SDK and adapter make of it today.
* A Phoenix store, with the ids of LLM spans whose one provider call produced several generations: an
  OpenAI-compatible gateway that sends a reply's text and its tool call as separate choices. Their raw bodies were
  not captured, so each body is **rebuilt** from the span's recorded LLMResult (text, call ids, names, the raw
  argument strings, usage, response id) in that gateway's recorded single-choice envelope. Its expected projection
  is what the same factory, SDK and adapter make of **the same reply in one choice**, the shape a fixed gateway sends.
  That oracle does not depend on Sage's fold.

Lab names are replaced from a private substitutions file (``{"substitutions": {"<recorded>": "<replacement>"}}``)
that is never committed. Bodies are substituted before they are replayed. Offline: no network, no provider.

    .venv/bin/python skills/sage-focused-capability-tests/scripts/freeze_split_choice_rows.py \
        --wire <capture>/wire.jsonl --phoenix <phoenix.db> --span 1515 --substitutions <private.json>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import freeze_provider_wire as base  # noqa: E402  (sibling script: wire pairing, substitution, fixture path)

ADDED_BY = "freeze_split_choice_rows.py"
VERSIONS = ("openai", "langchain-openai", "langchain-core")
EXPECTATION_REASON = "this plan: one reply split by the gateway"


def _drop_none(value):
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    return value


def _substitute(value, substitutions: dict[str, str]):
    ordered = dict(sorted(substitutions.items(), key=lambda item: -len(item[0])))
    return json.loads(base._substitute(json.dumps(value, ensure_ascii=False), ordered))


def _choice_shape(body: dict) -> tuple:
    return tuple((bool((choice.get("message") or {}).get("content")),
                  tuple(call["function"]["name"] for call in (choice.get("message") or {}).get("tool_calls") or []))
                 for choice in body["choices"])


def _span_body(connection: sqlite3.Connection, span_id: int) -> tuple[dict, str]:
    """``(rebuilt split body, configured model)`` from one Phoenix LLM span."""
    row = connection.execute("select span_kind, start_time, attributes from spans where id = ?", (span_id,)).fetchone()
    if row is None or row[0] != "LLM":
        raise ValueError(f"span {span_id} is not an LLM span")
    attributes = json.loads(row[2])
    result = json.loads(attributes["output"]["value"])
    generations = result["generations"][0]
    if len(generations) < 2:
        raise ValueError(f"span {span_id} has one generation; it is not a split reply")
    raw_arguments = {
        item["tool_call"]["id"]: item["tool_call"]["function"]["arguments"]
        for message in attributes["llm"]["output_messages"]
        for item in message["message"].get("tool_calls") or []
    }
    choices = []
    for index, generation in enumerate(generations):
        kwargs = generation["message"]["kwargs"]
        message: dict = {"role": "assistant"}
        calls = kwargs.get("tool_calls") or []
        if calls:
            message["tool_calls"] = []
            for position, call in enumerate(calls):
                arguments = raw_arguments[call["id"]]
                if json.loads(arguments) != call["args"]:
                    raise ValueError(f"span {span_id}: recorded arguments for {call['id']} do not match its call")
                message["tool_calls"].append({"index": position, "function": {"arguments": arguments,
                                                                                "name": call["name"]},
                                              "id": call["id"], "type": "function"})
            message["content"] = kwargs.get("content") or None
        else:
            message["content"] = kwargs["content"]
        info = generation.get("generation_info") or {}
        choices.append({"finish_reason": info["finish_reason"], "index": index, "message": message,
                        "logprobs": info.get("logprobs")})
    output = result["llm_output"]
    started = datetime.fromisoformat(row[1]).replace(tzinfo=timezone.utc)
    body = {"id": output["id"], "created": int(started.timestamp()), "model": output["model_name"],
            "object": "chat.completion", "choices": choices, "usage": _drop_none(output["token_usage"])}
    if output.get("service_tier"):
        body["service_tier"] = output["service_tier"]
    model = json.loads(attributes["llm"]["invocation_parameters"])["model"]
    return body, model


def _one_choice(body: dict) -> dict:
    """The same reply as a fixed gateway sends it: every text and call in ``choices[0]``, in choice order."""
    ordered = sorted(body["choices"], key=lambda choice: choice["index"])
    texts = [choice["message"]["content"] for choice in ordered if choice["message"].get("content")]
    calls = [call for choice in ordered for call in choice["message"].get("tool_calls") or []]
    message = {"role": "assistant", "content": "\n\n".join(texts) or None,
               "tool_calls": [{**call, "index": position} for position, call in enumerate(calls)]}
    return {**body, "choices": [{"finish_reason": "tool_calls", "index": 0, "message": message, "logprobs": None}]}


def _views(replay, row: dict, body: dict) -> dict:
    views = []
    for path in replay.PATHS:
        llm, _client, requests = replay.build_model(row, path, body=json.dumps(body).encode())
        result = replay.outcome(llm)
        if requests != ["/v1/chat/completions"] or isinstance(result, BaseException):
            raise ValueError(f"{row['id']} {path}: unexpected replay {requests} {result!r}")
        messages = replay.history(result)
        views.append({"message": replay.message_view(result),
                      "history": replay.history_view(messages, configured_model=row["model"]),
                      "checkpoint": replay.checkpoint_bytes(messages)})
    if views[0]["message"] != views[1]["message"] or views[0]["history"] != views[1]["history"]:
        raise ValueError(f"{row['id']}: the default and injected client paths disagree")
    return views[0]


def _row_id(number: int, model: str, detail: str) -> str:
    return f"r{number:02d}_chat_completions_200_{model.rsplit('-', 1)[-1]}_{detail}".replace(".", "_")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wire", action="append", required=True, type=Path, help="capture wire.jsonl (repeatable)")
    parser.add_argument("--phoenix", required=True, type=Path, help="Phoenix store holding the split spans (read-only)")
    parser.add_argument("--span", action="append", required=True, type=int, help="split LLM span id (repeatable)")
    parser.add_argument("--substitutions", required=True, type=Path, help="private substitutions JSON")
    parser.add_argument("--recorded-on", default="2026-11-26", help="date the captures and spans were recorded")
    parser.add_argument("--out", type=Path, default=base.FIXTURE)
    args = parser.parse_args(argv)

    substitutions = json.loads(args.substitutions.read_text(encoding="utf-8"))["substitutions"]
    sys.path.insert(0, str(base.SAGE))
    from tests import provider_wire_replay as replay

    fixture = json.loads(args.out.read_text(encoding="utf-8"))
    kept = [row for row in fixture["rows"] if row.get("added_by") != ADDED_BY]
    installed = {name: version(name) for name in VERSIONS}
    rows: list[dict] = []

    def add(detail: str, row: dict, replayed: dict) -> None:
        """Name the row (one endpoint per row, so the adapter's client cache never returns a swapped client), then
        freeze what the real factory, SDK and adapter make of ``replayed``."""
        row = {"id": _row_id(len(kept) + len(rows) + 1, row["model"], detail), **row}
        row["expected"] = _views(replay, row, replayed)
        rows.append(row)

    seen: set[tuple] = set()
    for number, wire in enumerate(args.wire, start=1):
        for pair in base._pairs(wire, f"recorded live check {number} of {len(args.wire)} ({args.recorded_on})"):
            if not pair["url"].endswith("/chat/completions") or pair["status"] != 200:
                continue
            body = json.loads(pair["body"])
            if len(body.get("choices") or []) != 1:
                raise ValueError("a captured Chat Completions body has more than one choice; add it as a recorded row")
            body = _substitute({k: v for k, v in body.items() if k not in base.ECHOED_REQUEST_FIELDS}, substitutions)
            model = _substitute(pair["request"]["model"], substitutions)
            shape = (model, _choice_shape(body))
            if shape in seen:
                continue
            seen.add(shape)
            names = "_".join(name for _text, calls in shape[1] for name in calls) or "text"
            row = {"source": pair["source"], "model": model,
                   "route": {"api": "chat_completions", "reasoning_effort": pair["request"].get("reasoning_effort") or ""},
                   "status": 200, "body": body, "added_by": ADDED_BY, "oracle_versions": installed}
            add(f"single_choice_{names}", row, body)

    connection = sqlite3.connect(f"file:{args.phoenix.resolve()}?mode=ro", uri=True)
    try:
        for span_id in args.span:
            body, model = _span_body(connection, span_id)
            body = _substitute(body, substitutions)
            model = _substitute(model, substitutions)
            names = "_".join(call["function"]["name"] for choice in body["choices"]
                             for call in choice["message"].get("tool_calls") or [])
            row = {"source": f"rebuilt from Phoenix LLM span {span_id} ({args.recorded_on}); raw body not captured",
                   "model": model, "route": {"api": "chat_completions", "reasoning_effort": ""}, "status": 200,
                   "body": body, "added_by": ADDED_BY, "oracle_versions": installed,
                   "rebuilt": ("Body rebuilt from the span's recorded LLMResult (each generation's text, call ids, names "
                               "and the raw argument strings; usage; response id) in the gateway's recorded "
                               "single-choice envelope. Envelope fields the span does not record are omitted."),
                   "expected_from": "the same reply in one choice (text and every call in choices[0], in choice order)",
                   "expectation_reason": EXPECTATION_REASON}
            add(f"split_text_then_{names}", row, _one_choice(body))
    finally:
        connection.close()

    provenance = fixture["provenance"]
    provenance["gap"] = ("No Anthropic Messages body was recorded; that route is covered only by synthetic tests and "
                         "the live check. Chat Completions success rows are listed under additions.")
    provenance["additions"] = {
        "added_by": ADDED_BY,
        "rows": [row["id"] for row in rows],
        "description": (
            f"Chat Completions 200 rows recorded on {args.recorded_on}. Single-choice rows are recorded bodies from an "
            "OpenAI-compatible gateway and from OpenAI, frozen with today's projection. Split rows are rebuilt from "
            "Phoenix spans where the gateway sent a reply's text and tool call as separate choices; each expects the "
            "projection of the same reply in one choice. Each row names the versions it was frozen under."),
        "substitutions": ("Lab host and user names renamed to WORKSTATION-A and lab.user; the gateway and OpenAI model "
                          "names renamed to recorded-model-c and recorded-model-d; echoed request parameters removed. "
                          "Substitution happened before replay."),
    }
    fixture["rows"] = kept + rows
    args.out.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} rows ({', '.join(row['id'] for row in rows)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
