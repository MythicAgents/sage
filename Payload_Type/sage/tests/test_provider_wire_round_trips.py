"""Battery rows: recorded provider bodies replayed through Sage's real model factory, the OpenAI SDK and the adapter.

Each row in ``fixtures/model_input_boundaries/provider_wire_round_trips.json`` is a redacted provider response body
recorded on 2026-11-22 together with what the pre-upgrade SDK and adapter (``provenance.oracle_versions``) made of
it, projected onto what Sage's consumers read (``tests/provider_wire_replay.py``). The installed SDK and adapter must
produce the same projections on both client paths Sage uses, and a checkpoint saved under the oracle versions must
restore to the same history. Stand-ins, labelled: the network transport (answers with the recorded body), the replay
prompt and the tool result.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage

from ai import provider_retry
from tests import provider_wire_replay as replay

FIXTURE = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/provider_wire_round_trips.json")
                     .read_text(encoding="utf-8"))
ROWS = FIXTURE["rows"]
ANSWERED = [row for row in ROWS if "message" in row["expected"]]
REJECTED = [row for row in ROWS if "error" in row["expected"]]
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(autouse=True)
def _no_environment_proxy(monkeypatch):
    # With a proxy variable set the adapter builds its default client without an explicit transport and mounts the
    # proxy, which the replay would not replace. Deployment sets none; keep the test's environment the same.
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _accepted(row: dict, view: str) -> dict:
    """The frozen view with the differences an operator accepted for this row (each carries its reason)."""
    accepted = row.get("accepted_differences") or {}
    if accepted:
        assert accepted["reason"].startswith("Operator decision"), row["id"]
    return {**row["expected"][view], **accepted.get(view, {})}


def _expected_request_path(row: dict) -> str:
    return "/v1/responses" if row["route"]["api"] == "responses" else "/v1/chat/completions"


def test_provenance_names_its_oracle_substitutions_and_gaps():
    provenance = FIXTURE["provenance"]
    assert provenance["kind"].startswith("recorded provider output")
    assert set(provenance["oracle_versions"]) == {"openai", "langchain-openai", "langchain-core"}
    assert "Chat Completions success" in provenance["gap"] and "Anthropic" in provenance["gap"]
    shapes = {(row["route"]["api"], row["status"]) for row in ROWS}
    assert {("responses", 200), ("responses", 400), ("chat_completions", 400)} <= shapes
    assert any("reasoning" in row["expected"]["message"]["content_types"] for row in ANSWERED)
    assert any(row["expected"]["message"]["content_types"] == ["text"] for row in ANSWERED)
    # An accepted difference may only replace the SDK's own error text, never what Sage's consumers decide on.
    for row in ROWS:
        accepted = row.get("accepted_differences")
        if accepted:
            assert set(accepted) == {"reason", "versions", "error"} and set(accepted["error"]) <= {
                "message", "operator_text"}, row["id"]


@pytest.mark.parametrize("path", replay.PATHS)
@pytest.mark.parametrize("row", ROWS, ids=[row["id"] for row in ROWS])
def test_recorded_body_produces_the_frozen_consumer_view(row, path):
    llm, client, requests = replay.build_model(row, path)
    if path == "default":
        # The client the adapter builds itself: the SDK's default client class (httpx2 from openai 3).
        assert isinstance(client, openai.DefaultHttpxClient)
    else:
        assert type(client) is httpx.Client
        assert isinstance(client._transport, provider_retry.RetryingHTTPTransport)

    result = replay.outcome(llm)

    assert requests == [_expected_request_path(row)]
    expected = row["expected"]
    if "error" in expected:
        assert isinstance(result, openai.APIStatusError), repr(result)
        assert replay.error_view(result, row) == _accepted(row, "error")
    else:
        assert isinstance(result, AIMessage), repr(result)
        assert replay.message_view(result) == expected["message"]
        assert replay.history_view(replay.history(result), configured_model=row["model"]) == expected["history"]


def test_the_default_path_runs_on_the_library_the_installed_sdk_defaults_to():
    import httpx2

    _llm, client, _requests = replay.build_model(ANSWERED[0], "default")
    sdk_major = int(openai.__version__.split(".")[0])
    assert isinstance(client, httpx2.Client) is (sdk_major >= 3)


@pytest.mark.parametrize("row", ANSWERED, ids=[row["id"] for row in ANSWERED])
def test_a_checkpoint_saved_under_the_oracle_versions_restores_to_the_same_history(row):
    messages = replay.restore_checkpoint(row["expected"]["checkpoint"])

    assert [type(message).__name__ for message in messages] == (
        ["HumanMessage", "AIMessage"] + ["ToolMessage"] * len(row["expected"]["message"]["tool_calls"]))
    assert replay.message_view(messages[1]) == row["expected"]["message"]
    assert replay.history_view(messages, configured_model=row["model"]) == row["expected"]["history"]


# Red: each guard fails when a consumer-relevant field of the recorded body changes.

def _row(predicate) -> dict:
    return next(row for row in ROWS if predicate(row))


def _replay_body(row: dict, body: dict):
    llm, _client, _requests = replay.build_model(row, "default", body=json.dumps(body).encode())
    return replay.outcome(llm)


def test_a_changed_call_id_fails_its_row():
    row = _row(lambda r: "message" in r["expected"] and r["expected"]["message"]["tool_calls"])
    body = copy.deepcopy(row["body"])
    call = next(item for item in body["output"] if item["type"] == "function_call")
    call["call_id"] = call["call_id"] + "x"

    assert replay.message_view(_replay_body(row, body)) != row["expected"]["message"]


def test_changed_encrypted_reasoning_fails_the_responses_replay():
    row = _row(lambda r: "message" in r["expected"] and "reasoning" in r["expected"]["message"]["content_types"])
    body = copy.deepcopy(row["body"])
    reasoning = next(item for item in body["output"] if item["type"] == "reasoning")
    reasoning["encrypted_content"] = reasoning["encrypted_content"][::-1]

    result = _replay_body(row, body)
    assert replay.history_view(replay.history(result), configured_model=row["model"])["responses_input"] != (
        row["expected"]["history"]["responses_input"])


def test_a_changed_rejection_parameter_fails_its_row():
    row = _row(lambda r: "error" in r["expected"] and r["route"]["api"] == "responses")
    body = copy.deepcopy(row["body"])
    body["error"]["param"] = "reasoning.summary"

    assert replay.error_view(_replay_body(row, body), row) != _accepted(row, "error")
