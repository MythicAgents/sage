"""Battery rows: recorded provider bodies replayed through Sage's real model factory, the OpenAI SDK and the adapter.

Each row in ``fixtures/model_input_boundaries/provider_wire_round_trips.json`` is a redacted provider response body
recorded on 2026-11-22 together with what the pre-upgrade SDK and adapter (``provenance.oracle_versions``) made of
it, projected onto what Sage's consumers read (``tests/provider_wire_replay.py``). The installed SDK and adapter must
produce the same projections on both client paths Sage uses, and a checkpoint saved under the oracle versions must
restore to the same history. Rows added later (``added_by``, with ``provenance.additions``) name the versions they were
frozen under; a rebuilt row says what it was rebuilt from and why its expectation is the folded message. Rejected rows
also freeze the provider-error fields line the container log receives (``log_fields``, added later with its reason).
Stand-ins, labelled: the network transport (answers with the recorded body), the replay prompt and the tool result.
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
    """The frozen view, with the differences an operator accepted for this row (each carries its reason) applied
    only on the versions they were accepted for. Any other versions are held to the frozen view."""
    from importlib.metadata import version

    accepted = row.get("accepted_differences") or {}
    if not accepted:
        return row["expected"][view]
    assert accepted["reason"].startswith("Operator decision"), row["id"]
    if {name: version(name) for name in accepted["versions"]} != accepted["versions"]:
        return row["expected"][view]
    if view == "message":
        frozen = row["expected"]["message"]
        added = accepted.get("message", {}).get("metadata", {})
        return {**frozen, "metadata": {**frozen["metadata"], **added}}
    return {**row["expected"][view], **accepted.get(view, {})}


def _expected_request_path(row: dict) -> str:
    return "/v1/responses" if row["route"]["api"] == "responses" else "/v1/chat/completions"


def test_provenance_names_its_oracle_substitutions_and_gaps():
    provenance = FIXTURE["provenance"]
    assert provenance["kind"].startswith("recorded provider output")
    assert set(provenance["oracle_versions"]) == {"openai", "langchain-openai", "langchain-core"}
    assert "Anthropic" in provenance["gap"]
    shapes = {(row["route"]["api"], row["status"]) for row in ROWS}
    assert {("responses", 200), ("responses", 400), ("chat_completions", 400), ("chat_completions", 200)} <= shapes
    # Rows added after the pre-upgrade freeze name their script and versions; a rebuilt row says so and carries the
    # reason its expectation differs from what the adapter alone makes of its body.
    additions = provenance["additions"]
    assert additions["rows"] == [row["id"] for row in ROWS if row.get("added_by") == additions["added_by"]]
    for row in ROWS:
        if row.get("added_by"):
            assert set(row["oracle_versions"]) == set(provenance["oracle_versions"]), row["id"]
        if row.get("rebuilt"):
            assert row["source"].startswith("rebuilt from Phoenix LLM span"), row["id"]
            assert row["expectation_reason"] == "this plan: one reply split by the gateway", row["id"]
            assert len(row["body"]["choices"]) > 1, row["id"]
    assert any("reasoning" in row["expected"]["message"]["content_types"] for row in ANSWERED)
    assert any(row["expected"]["message"]["content_types"] == ["text"] for row in ANSWERED)
    # An accepted difference may only replace the SDK's own error text, or add a finish_reason whose value no Sage
    # consumer decides on; never what Sage's consumers decide on.
    for row in ROWS:
        accepted = row.get("accepted_differences")
        if not accepted:
            continue
        if "error" in accepted:
            assert set(accepted) == {"reason", "versions", "error"} and set(accepted["error"]) <= {
                "message", "operator_text"}, row["id"]
        else:
            assert set(accepted) == {"reason", "versions", "message"}, row["id"]
            assert set(accepted["message"]) == {"metadata"}, row["id"]
            added = accepted["message"]["metadata"]
            assert set(added) == {"finish_reason"}, row["id"]
            assert "finish_reason" not in row["expected"]["message"]["metadata"], row["id"]
            assert added["finish_reason"].casefold() not in replay.DECIDED_FINISH_REASONS, row["id"]
    # The container-log fields view was added after the freeze, for every rejected row, with the plan's reason.
    for row in ROWS:
        assert ("log_fields" in row["expected"]) is ("error" in row["expected"]), row["id"]
        if "log_fields" in row["expected"]:
            assert row["log_fields_reason"].startswith("this plan: a failed provider request logs"), row["id"]


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

    with replay.captured_log() as logged:
        result = replay.outcome(llm)

    assert requests == [_expected_request_path(row)]
    expected = row["expected"]
    if "error" in expected:
        assert isinstance(result, openai.APIStatusError), repr(result)
        assert replay.error_view(result, row) == _accepted(row, "error")
        assert replay.log_fields_view(logged) == _accepted(row, "log_fields")
    else:
        assert isinstance(result, AIMessage), repr(result)
        assert replay.log_fields_view(logged) == {"lines": []}
        assert replay.message_view(result) == _accepted(row, "message")
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


def _replay_body(row: dict, body: dict, logged: list[str] | None = None):
    llm, _client, _requests = replay.build_model(row, "default", body=json.dumps(body).encode())
    with replay.captured_log() as messages:
        result = replay.outcome(llm)
    if logged is not None:
        logged.extend(messages)
    return result


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
    logged: list[str] = []

    assert replay.error_view(_replay_body(row, body, logged), row) != _accepted(row, "error")
    assert replay.log_fields_view(logged) != _accepted(row, "log_fields")
