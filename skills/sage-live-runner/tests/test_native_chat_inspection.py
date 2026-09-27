from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "native_chat.py"
SPEC = importlib.util.spec_from_file_location("native_chat_inspection", SCRIPT)
native_chat = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(native_chat)


def test_canary_metadata_is_supervised_without_changing_auto_defaults(
    monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "provider=OpenAI\nmodel=test-model\nmax_steps=200\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(native_chat, "SAGE_ENV_PATH", env_file)

    normal = native_chat.default_ai_metadata()
    canary = native_chat.canary_ai_metadata(max_steps=12)

    assert normal["config"]["mode"] == "auto"
    assert normal["config"]["autonomous_solve"] is True
    assert normal["config"]["max_steps"] == 200
    assert canary["config"]["mode"] == "supervised"
    assert canary["config"]["autonomous_solve"] is False
    assert canary["config"]["max_steps"] == 12
    assert canary["config"]["model"] == "test-model"


def test_canary_metadata_uses_runtime_env_precedence(monkeypatch, tmp_path):
    (tmp_path / ".env.local").write_text(
        "provider=OpenAI\nmodel=local-model\nAPI_ENDPOINT=https://local.example/v1\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "provider=anthropic\nmodel=tracked-model\nAPI_ENDPOINT=https://tracked.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(native_chat, "SAGE_ENV_PATH", tmp_path / ".env")
    for name in ("provider", "model", "API_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)

    config = native_chat.canary_ai_metadata(max_steps=12)["config"]
    assert (config["provider"], config["model"], config["API_ENDPOINT"]) == (
        "openai", "local-model", "https://local.example/v1"
    )
    assert config["mode"] == "supervised"
    assert config["autonomous_solve"] is False

    monkeypatch.setenv("provider", "bedrock")
    monkeypatch.setenv("model", "process-model")
    monkeypatch.setenv("API_ENDPOINT", "https://process.example/v1")
    config = native_chat.canary_ai_metadata(max_steps=12)["config"]
    assert (config["provider"], config["model"], config["API_ENDPOINT"]) == (
        "bedrock", "process-model", "https://process.example/v1"
    )


def test_canary_metadata_skips_empty_file_values_and_rejects_missing_model(
    monkeypatch, tmp_path
):
    (tmp_path / ".env.local").write_text(
        "provider=\nmodel=\nAPI_ENDPOINT=\nmodel_name=near-match\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "provider=Anthropic\nmodel=tracked-model\nAPI_ENDPOINT=https://tracked.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(native_chat, "SAGE_ENV_PATH", tmp_path / ".env")
    for name in ("provider", "model", "API_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)

    config = native_chat.canary_ai_metadata(max_steps=12)["config"]
    assert (config["provider"], config["model"], config["API_ENDPOINT"]) == (
        "anthropic", "tracked-model", "https://tracked.example/v1"
    )

    (tmp_path / ".env").write_text("provider=OpenAI\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Sage model is not configured"):
        native_chat.canary_ai_metadata(max_steps=12)


def test_empty_supervised_channel_sends_resolved_runtime_metadata(monkeypatch, tmp_path):
    (tmp_path / ".env.local").write_text(
        "provider=OpenAI\nmodel=local-model\nAPI_ENDPOINT=https://local.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(native_chat, "SAGE_ENV_PATH", tmp_path / ".env")
    for name in ("provider", "model", "API_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAGE_BACKING_APITOKEN_ID", "7")
    monkeypatch.setenv("SAGE_CHAT_CONTAINER_ID", "3")
    sent = []

    async def fake_query(_client, query, variables=None):
        assert query == native_chat.CREATE_CHANNEL_MUTATION
        sent.append(variables)
        return {"chatCreateChannel": {"status": "success", "channel_id": 9}}

    monkeypatch.setattr(native_chat.mythic, "execute_custom_query", fake_query)
    metadata = native_chat.canary_ai_metadata(max_steps=12)
    result = asyncio.run(native_chat.create_locked_channel(object(), metadata=metadata))

    assert result["chat_channel_id"] == 9
    assert len(sent) == 1
    assert sent[0]["metadata"]["config"] == metadata["config"]
    config = sent[0]["metadata"]["config"]
    assert (config["provider"], config["model"], config["API_ENDPOINT"]) == (
        "openai", "local-model", "https://local.example/v1"
    )


def _supervised_prepare_case(monkeypatch, tmp_path, *, existing=False, stored_change=None):
    endpoint = "https://provider.example/v1/private"
    (tmp_path / ".env").write_text(
        "provider=openai\nmodel=test-model\nAPI_ENDPOINT=" + endpoint + "\nAPI_KEY=secret-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(native_chat, "SAGE_ENV_PATH", tmp_path / ".env")
    for key in ("provider", "model", "API_ENDPOINT", "API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SAGE_BACKING_APITOKEN_ID", "7")
    monkeypatch.setenv("SAGE_CHAT_CONTAINER_ID", "3")
    client = type("Client", (), {"current_operation_id": 4})()
    calls = []
    created_metadata = {}

    async def fake_query(_client, query, variables=None):
        calls.append((query, variables))
        if query == native_chat.SUPERVISED_CHANNEL_NAME_QUERY:
            return {"chat_channel": [{"id": 6}] if existing else []}
        if query == native_chat.CREATE_CHANNEL_MUTATION:
            created_metadata.update(variables["metadata"])
            return {"chatCreateChannel": {"status": "success", "channel_id": 9}}
        if query == native_chat.SUPERVISED_CHANNEL_READBACK_QUERY:
            row = {
                "id": 9, "name": "HUMAN TRIAL 20260924", "channel_type": "ai",
                "locked": True, "archived": False, "last_message_id": None,
                "ai_metadata": created_metadata,
            }
            if stored_change:
                stored_change(row)
            return {"chat_channel": [row]}
        raise AssertionError("unexpected query")

    monkeypatch.setattr(native_chat.mythic, "execute_custom_query", fake_query)
    kwargs = {
        "channel_name": "HUMAN TRIAL 20260924", "max_steps": 12,
        "expect_provider": "openai", "expect_model": "test-model",
        "expect_endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
    }
    return client, calls, kwargs, endpoint


def test_prepare_supervised_creates_and_reads_back_empty_channel(monkeypatch, tmp_path):
    client, calls, kwargs, endpoint = _supervised_prepare_case(monkeypatch, tmp_path)
    result = asyncio.run(native_chat.prepare_supervised_channel(client, **kwargs))
    assert [query for query, _ in calls] == [
        native_chat.SUPERVISED_CHANNEL_NAME_QUERY,
        native_chat.CREATE_CHANNEL_MUTATION,
        native_chat.SUPERVISED_CHANNEL_READBACK_QUERY,
    ]
    assert calls[0][1] == {"operationId": 4, "name": kwargs["channel_name"]}
    assert calls[2][1] == {"operationId": 4, "channelId": 9}
    assert result == {
        "chat_channel_id": 9, "chat_channel_name": kwargs["channel_name"],
        "provider": "openai", "model": "test-model",
        "endpoint_sha256": kwargs["expect_endpoint_sha256"], "mode": "supervised",
        "autonomous_solve": False, "max_steps": 12, "empty": True,
    }
    assert "secret-key" not in json.dumps(result)
    assert endpoint not in json.dumps(result)
    sent = calls[1][1]["metadata"]["config"]
    assert sent["API_KEY"] == "secret-key"
    assert sent["mode"] == "supervised" and sent["autonomous_solve"] is False


@pytest.mark.parametrize("mismatch", ["expect_provider", "expect_model", "expect_endpoint_sha256"])
def test_prepare_supervised_refuses_local_contract_mismatch_before_creation(
    monkeypatch, tmp_path, mismatch
):
    client, calls, kwargs, _ = _supervised_prepare_case(monkeypatch, tmp_path)
    kwargs[mismatch] = "0" * 64 if mismatch == "expect_endpoint_sha256" else "wrong"
    with pytest.raises(RuntimeError, match="local Sage provider, model, or endpoint"):
        asyncio.run(native_chat.prepare_supervised_channel(client, **kwargs))
    assert calls == []


def test_prepare_supervised_refuses_duplicate_name_before_creation(monkeypatch, tmp_path):
    client, calls, kwargs, _ = _supervised_prepare_case(monkeypatch, tmp_path, existing=True)
    with pytest.raises(RuntimeError, match="visible channel name already exists"):
        asyncio.run(native_chat.prepare_supervised_channel(client, **kwargs))
    assert [query for query, _ in calls] == [native_chat.SUPERVISED_CHANNEL_NAME_QUERY]


@pytest.mark.parametrize("name", ["", "Sage", "TRIAL", "  HUMAN TRIAL 20260924  "])
def test_prepare_supervised_requires_distinct_visible_name(monkeypatch, tmp_path, name):
    client, calls, kwargs, _ = _supervised_prepare_case(monkeypatch, tmp_path)
    kwargs["channel_name"] = name
    with pytest.raises(ValueError, match="--channel-name"):
        asyncio.run(native_chat.prepare_supervised_channel(client, **kwargs))
    assert calls == []


@pytest.mark.parametrize("change", [
    lambda row: row.update(id=10),
    lambda row: row.update(name="wrong"),
    lambda row: row.update(locked=False),
    lambda row: row.update(last_message_id=1),
    lambda row: row["ai_metadata"]["config"].update(mode="auto"),
    lambda row: row["ai_metadata"]["config"].update(autonomous_solve=True),
    lambda row: row["ai_metadata"]["config"].update(max_steps=13),
    lambda row: row["ai_metadata"]["config"].update(API_ENDPOINT="https://wrong.example"),
])
def test_prepare_supervised_refuses_stored_drift_with_created_id(
    monkeypatch, tmp_path, change
):
    client, calls, kwargs, endpoint = _supervised_prepare_case(
        monkeypatch, tmp_path, stored_change=change
    )
    with pytest.raises(RuntimeError, match="created channel 9 failed server readback") as exc:
        asyncio.run(native_chat.prepare_supervised_channel(client, **kwargs))
    assert endpoint not in str(exc.value)
    assert "secret-key" not in str(exc.value)
    assert calls[-1][0] == native_chat.SUPERVISED_CHANNEL_READBACK_QUERY


def test_prepare_supervised_parser_requires_contract_and_name():
    parser = native_chat.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["prepare-supervised", "--channel-name", "TRIAL"])
    args = parser.parse_args([
        "prepare-supervised", "--channel-name", "TRIAL 20260924",
        "--expect-provider", "openai", "--expect-model", "test-model",
        "--expect-endpoint-sha256", "a" * 64,
    ])
    assert args.max_steps == 12
    assert args.command == "prepare-supervised"


def test_prepare_supervised_cli_prints_only_verified_fields(monkeypatch, tmp_path, capsys):
    client, _calls, kwargs, endpoint = _supervised_prepare_case(monkeypatch, tmp_path)

    async def fake_login(**_kwargs):
        return client

    monkeypatch.setattr(native_chat, "login", fake_login)
    args = native_chat.build_parser().parse_args([
        "prepare-supervised", "--channel-name", kwargs["channel_name"],
        "--expect-provider", kwargs["expect_provider"],
        "--expect-model", kwargs["expect_model"],
        "--expect-endpoint-sha256", kwargs["expect_endpoint_sha256"],
    ])
    assert asyncio.run(native_chat._run(args)) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["empty"] is True
    assert endpoint not in output
    assert "secret-key" not in output


def test_resolve_latest_request_is_scoped_to_sage_channels(monkeypatch):
    calls = []

    async def fake_query(_client, query, variables=None):
        calls.append((query, variables))
        if query == native_chat.SAGE_CHANNEL_IDS_QUERY:
            return {"chat_channel": [{"id": 7}, {"id": 5}]}
        return {"chat_request": [{"id": 41, "channel_id": 7}]}

    monkeypatch.setattr(
        native_chat.mythic, "execute_custom_query", fake_query
    )

    request_id = asyncio.run(
        native_chat.resolve_request_selector(object(), latest=True)
    )

    assert request_id == 41
    assert calls[-1][1] == {"channelIds": [7, 5]}


def test_resolve_latest_rejects_non_sage_channel(monkeypatch):
    async def fake_query(_client, _query, variables=None):
        assert variables is None
        return {"chat_channel": [{"id": 7}]}

    monkeypatch.setattr(
        native_chat.mythic, "execute_custom_query", fake_query
    )

    with pytest.raises(RuntimeError, match="not an active Sage"):
        asyncio.run(
            native_chat.resolve_request_selector(
                object(), latest=True, channel_id=99
            )
        )


@pytest.mark.parametrize(
    ("channel_rows", "request_rows"),
    [
        ([{"id": True}], [{"id": 41}]),
        ([{"id": 7.0}], [{"id": 41}]),
        ([{"id": 7}], [{"id": True}]),
        ([{"id": 7}], [{"id": 41.0}]),
    ],
)
def test_resolve_latest_rejects_coerced_result_identity(
    monkeypatch, channel_rows, request_rows
):
    async def fake_query(_client, query, variables=None):
        if query == native_chat.SAGE_CHANNEL_IDS_QUERY:
            return {"chat_channel": channel_rows}
        return {"chat_request": request_rows}

    monkeypatch.setattr(
        native_chat.mythic, "execute_custom_query", fake_query
    )

    with pytest.raises(RuntimeError, match="exact integer"):
        asyncio.run(
            native_chat.resolve_request_selector(object(), latest=True)
        )


def test_fetch_snapshot_sorts_messages_and_builds_full_transcript(monkeypatch):
    async def fake_query(_client, query, variables=None):
        if query == native_chat.REQUEST_QUERY:
            assert variables == {"requestId": 9}
            return {
                "chat_request": [
                    {
                        "id": 9,
                        "channel_id": 4,
                        "request_message_id": 1,
                        "status": "complete",
                        "error": "",
                    }
                ],
                "chat_message": [
                    {
                        "id": 3,
                        "channel_id": 4,
                        "chat_request_id": 9,
                        "message": "final",
                        "metadata": {
                            "container_metadata": {
                                "control_transitions": [
                                    {
                                        "event_id": "control:one",
                                        "kind": "control_transition",
                                        "phase": "request_installed",
                                        "content": "request contract installed",
                                    },
                                    {
                                        "event_id": "control:two",
                                        "kind": "control_transition",
                                        "phase": "request_terminal",
                                        "content": "complete",
                                    },
                                ]
                            }
                        },
                    },
                ],
            }
        assert query == native_chat.REQUEST_MESSAGE_QUERY
        assert variables == {"messageId": 1}
        return {
            "chat_message": [
                {
                    "id": 1,
                    "channel_id": 4,
                    "chat_request_id": None,
                    "message": "prompt",
                    "metadata": {},
                },
            ]
        }

    monkeypatch.setattr(
        native_chat.mythic, "execute_custom_query", fake_query
    )

    snapshot = asyncio.run(
        native_chat.fetch_request_snapshot(object(), 9)
    )
    transcript = native_chat.build_transcript_export(snapshot)

    assert [message["id"] for message in transcript["messages"]] == [1, 3]
    assert transcript["schema"] == "sage-native-chat-transcript-v1"
    assert transcript["chat_channel_id"] == 4
    assert transcript["chat_request_id"] == 9
    assert transcript["status"] == "complete"
    assert [
        event["phase"]
        for event in transcript["control_transitions"]
    ] == ["request_installed", "request_terminal"]


@pytest.mark.parametrize(
    "operator_rows",
    (
        [],
        [
            {
                "id": 1,
                "channel_id": 4,
                "chat_request_id": 9,
                "message": "prompt",
                "metadata": {},
            }
        ],
        [
            {
                "id": 2,
                "channel_id": 4,
                "chat_request_id": None,
                "message": "near match",
                "metadata": {},
            }
        ],
    ),
)
def test_fetch_snapshot_rejects_missing_or_wrong_operator_identity(
    monkeypatch,
    operator_rows,
):
    async def fake_query(_client, query, variables=None):
        if query == native_chat.REQUEST_QUERY:
            return {
                "chat_request": [
                    {
                        "id": 9,
                        "channel_id": 4,
                        "request_message_id": 1,
                        "status": "complete",
                    }
                ],
                "chat_message": [
                    {
                        "id": 3,
                        "channel_id": 4,
                        "chat_request_id": 9,
                        "message": "response only",
                        "metadata": {},
                    }
                ],
            }
        assert query == native_chat.REQUEST_MESSAGE_QUERY
        assert variables == {"messageId": 1}
        return {"chat_message": operator_rows}

    monkeypatch.setattr(
        native_chat.mythic,
        "execute_custom_query",
        fake_query,
    )

    with pytest.raises(RuntimeError, match="operator message"):
        asyncio.run(native_chat.fetch_request_snapshot(object(), 9))


@pytest.mark.parametrize("returned_id", [10, True, 9.0, "9"])
def test_fetch_snapshot_rejects_wrong_or_coerced_request_identity(
    monkeypatch, returned_id
):
    async def fake_query(_client, _query, variables=None):
        assert variables == {"requestId": 9}
        return {
            "chat_request": [
                {
                    "id": returned_id,
                    "channel_id": 4,
                    "status": "complete",
                }
            ],
            "chat_message": [],
        }

    monkeypatch.setattr(
        native_chat.mythic, "execute_custom_query", fake_query
    )

    with pytest.raises(RuntimeError, match="request id"):
        asyncio.run(native_chat.fetch_request_snapshot(object(), 9))


@pytest.mark.parametrize("channel_id", [True, 4.0, "4"])
def test_fetch_snapshot_rejects_coerced_channel_identity(
    monkeypatch, channel_id
):
    async def fake_query(_client, _query, variables=None):
        assert variables == {"requestId": 9}
        return {
            "chat_request": [
                {
                    "id": 9,
                    "channel_id": channel_id,
                    "status": "complete",
                }
            ],
            "chat_message": [],
        }

    monkeypatch.setattr(
        native_chat.mythic, "execute_custom_query", fake_query
    )

    with pytest.raises(RuntimeError, match="channel id"):
        asyncio.run(native_chat.fetch_request_snapshot(object(), 9))


def test_transcript_export_rejects_message_identity_drift():
    with pytest.raises(RuntimeError, match="chat_request_id"):
        native_chat.build_transcript_export(
            {
                "request": {
                    "id": 9,
                    "channel_id": 4,
                    "request_message_id": 1,
                    "status": "complete",
                    "error": "",
                },
                "messages": [
                    {
                        "id": 1,
                        "channel_id": 4,
                        "chat_request_id": None,
                        "message": "prompt",
                        "metadata": {},
                    },
                    {
                        "id": 2,
                        "channel_id": 4,
                        "chat_request_id": 10,
                        "message": "response",
                        "metadata": {},
                    },
                ],
            }
        )


def test_transcript_export_rejects_missing_message_identity():
    with pytest.raises(RuntimeError, match="chat_request_id"):
        native_chat.build_transcript_export(
            {
                "request": {
                    "id": 9,
                    "channel_id": 4,
                    "request_message_id": 1,
                    "status": "complete",
                    "error": "",
                },
                "messages": [
                    {
                        "id": 1,
                        "channel_id": 4,
                        "chat_request_id": None,
                        "message": "prompt",
                        "metadata": {},
                    },
                    {
                        "id": 2,
                        "channel_id": 4,
                        "message": "response",
                        "metadata": {},
                    }
                ],
            }
        )


def test_transcript_export_rejects_response_only_near_match():
    with pytest.raises(RuntimeError, match="exact operator message"):
        native_chat.build_transcript_export(
            {
                "request": {
                    "id": 9,
                    "channel_id": 4,
                    "request_message_id": 1,
                    "status": "complete",
                    "error": "",
                },
                "messages": [
                    {
                        "id": 2,
                        "channel_id": 4,
                        "chat_request_id": 9,
                        "message": "same text is not the operator row",
                        "metadata": {},
                    },
                ],
            }
        )


@pytest.mark.parametrize(
    "control_transitions",
    (
        {"event_id": "not-a-list"},
        [{"event_id": "missing-fields"}],
        [
            {
                "event_id": "control:one",
                "kind": "tool",
                "phase": "request_terminal",
                "content": "complete",
            }
        ],
        [
            {
                "event_id": "control:one",
                "kind": "control_transition",
                "phase": "request_installed",
                "content": "installed",
            },
            {
                "event_id": "control:one",
                "kind": "control_transition",
                "phase": "request_terminal",
                "content": "complete",
            },
        ],
    ),
)
def test_transcript_export_rejects_invalid_typed_control_events(
    control_transitions,
):
    snapshot = {
        "request": {
            "id": 9,
            "channel_id": 4,
            "request_message_id": 1,
            "status": "complete",
            "error": "",
        },
        "messages": [
            {
                "id": 1,
                "channel_id": 4,
                "chat_request_id": None,
                "message": "prompt",
                "metadata": {},
            },
            {
                "id": 2,
                "channel_id": 4,
                "chat_request_id": 9,
                "message": "response",
                "metadata": {
                    "control_transitions": control_transitions,
                },
            },
        ],
    }

    with pytest.raises(RuntimeError, match="control transition"):
        native_chat.build_transcript_export(snapshot)


@pytest.mark.parametrize(
    "control_transitions",
    (
        [],
        [
            {
                "event_id": "control:bogus",
                "kind": "control_transition",
                "phase": "not_a_typed_phase",
                "content": "nonsense",
            }
        ],
        [
            {
                "event_id": "control:terminal",
                "kind": "control_transition",
                "phase": "request_terminal",
                "content": "complete",
            },
            {
                "event_id": "control:install",
                "kind": "control_transition",
                "phase": "request_installed",
                "content": "request contract installed",
            },
        ],
    ),
)
def test_transcript_export_rejects_incomplete_or_semantically_invalid_controls(
    control_transitions,
):
    snapshot = {
        "request": {
            "id": 9,
            "channel_id": 4,
            "request_message_id": 1,
            "status": "complete",
            "error": "",
        },
        "messages": [
            {
                "id": 1,
                "channel_id": 4,
                "chat_request_id": None,
                "message": "prompt",
                "metadata": {},
            },
            {
                "id": 2,
                "channel_id": 4,
                "chat_request_id": 9,
                "message": "response",
                "metadata": {
                    "control_transitions": control_transitions,
                },
            },
        ],
    }

    with pytest.raises(RuntimeError, match="control transition"):
        native_chat.build_transcript_export(snapshot)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", True),
        ("chat_request_id", True),
        ("chat_request_id", 9.0),
        ("channel_id", 4.0),
    ],
)
def test_transcript_export_rejects_coerced_message_identity(field, value):
    message = {
        "id": 1,
        "channel_id": 4,
        "chat_request_id": 9,
        "message": "prompt",
        "metadata": {},
    }
    message[field] = value

    with pytest.raises(RuntimeError, match="exact integer"):
        native_chat.build_transcript_export(
            {
                "request": {
                    "id": 9,
                    "channel_id": 4,
                    "status": "complete",
                    "error": "",
                },
                "messages": [message],
            }
        )


def test_find_prepared_channel_requires_auto_autonomous_metadata(
    monkeypatch,
):
    async def fake_query(_client, query, variables=None):
        assert query == native_chat.PREPARED_CHANNEL_QUERY
        assert variables is None
        return {
            "chat_channel": [
                {
                    "id": 9,
                    "name": native_chat.DEFAULT_PREPARED_CHANNEL_NAME,
                    "chat_container_id": 2,
                    "apitokens_id": 3,
                    "ai_metadata": {
                        "prepared_for": native_chat.PREPARED_CHANNEL_MARKER,
                        "config": {
                            "mode": "supervised",
                            "autonomous_solve": False,
                        },
                    },
                },
                {
                    "id": 8,
                    "name": native_chat.DEFAULT_PREPARED_CHANNEL_NAME,
                    "chat_container_id": 2,
                    "apitokens_id": 3,
                    "ai_metadata": {
                        "prepared_for": native_chat.PREPARED_CHANNEL_MARKER,
                        "config": {
                            "mode": "auto",
                            "autonomous_solve": True,
                        },
                    },
                },
            ]
        }

    monkeypatch.setattr(
        native_chat.mythic, "execute_custom_query", fake_query
    )

    prepared = asyncio.run(
        native_chat.find_prepared_channel(object())
    )

    assert prepared is not None
    assert prepared["chat_channel_id"] == 8
    assert prepared["prepared_policy"]["mode"] == "auto"
    assert prepared["prepared_policy"]["autonomous_solve"] is True


def test_find_prepared_channel_rejects_boolean_like_autonomy(monkeypatch):
    async def fake_query(_client, query, variables=None):
        assert query == native_chat.PREPARED_CHANNEL_QUERY
        assert variables is None
        return {
            "chat_channel": [
                {
                    "id": 9,
                    "name": native_chat.DEFAULT_PREPARED_CHANNEL_NAME,
                    "chat_container_id": 2,
                    "apitokens_id": 3,
                    "ai_metadata": {
                        "prepared_for": native_chat.PREPARED_CHANNEL_MARKER,
                        "config": {
                            "mode": "auto",
                            "autonomous_solve": "true",
                        },
                    },
                }
            ]
        }

    monkeypatch.setattr(
        native_chat.mythic, "execute_custom_query", fake_query
    )

    assert asyncio.run(
        native_chat.find_prepared_channel(object())
    ) is None


def test_run_revalidates_prepared_channel_policy_before_use(monkeypatch):
    created = []

    async def fake_find(_client):
        return {
            "chat_channel_id": 9,
            "chat_channel_name": "wrong",
            "prepared_policy": {
                "mode": "supervised",
                "autonomous_solve": False,
            },
        }

    async def fake_create(_client, **_kwargs):
        created.append(True)
        return {
            "chat_channel_id": 10,
            "chat_channel_name": "fresh-auto",
            "chat_runtime_identity": {},
        }

    async def fake_message(_client, channel_id, _prompt):
        assert channel_id == 10
        return {"chat_message_id": 20, "chat_request_id": 30}

    async def fake_wait(_client, request_id, **_kwargs):
        assert request_id == 30
        return {
            "request": {"id": 30, "channel_id": 10, "status": "complete"},
            "messages": [],
        }

    monkeypatch.setattr(native_chat, "find_prepared_channel", fake_find)
    monkeypatch.setattr(native_chat, "create_locked_channel", fake_create)
    monkeypatch.setattr(native_chat, "create_message", fake_message)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    result = asyncio.run(
        native_chat.run_native_chat_turn(object(), "status")
    )

    assert created == [True]
    assert result["chat_channel_id"] == 10


@pytest.mark.parametrize(
    "prepared_extra",
    [{}, {"prepared_policy": "auto"}],
)
def test_run_refuses_prepared_channel_without_typed_policy(
    monkeypatch, prepared_extra
):
    created = []

    async def fake_find(_client):
        return {
            "chat_channel_id": 9,
            "chat_channel_name": "unverified",
            **prepared_extra,
        }

    async def fake_create(_client, **_kwargs):
        created.append(True)
        return {
            "chat_channel_id": 10,
            "chat_channel_name": "fresh-auto",
            "chat_runtime_identity": {},
        }

    async def fake_message(_client, channel_id, _prompt):
        assert channel_id == 10
        return {"chat_message_id": 20, "chat_request_id": 30}

    async def fake_wait(_client, request_id, **_kwargs):
        assert request_id == 30
        return {
            "request": {"id": 30, "channel_id": 10, "status": "complete"},
            "messages": [],
        }

    monkeypatch.setattr(native_chat, "find_prepared_channel", fake_find)
    monkeypatch.setattr(native_chat, "create_locked_channel", fake_create)
    monkeypatch.setattr(native_chat, "create_message", fake_message)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    result = asyncio.run(
        native_chat.run_native_chat_turn(object(), "status")
    )

    assert created == [True]
    assert result["chat_channel_id"] == 10


def test_wait_for_request_stops_at_native_input_request(monkeypatch):
    async def fake_snapshot(_client, request_id):
        assert request_id == 12
        return {
            "request": {"id": 12, "status": "processing"},
            "messages": [
                {
                    "id": 8,
                    "metadata": {
                            "container_metadata": {
                                "special_type": "input_requested",
                                "input_requested": {
                                    "title": "Approve",
                                    "status": "pending",
                                },
                            }
                        },
                }
            ],
        }

    monkeypatch.setattr(
        native_chat, "fetch_request_snapshot", fake_snapshot
    )

    result = asyncio.run(
        native_chat.wait_for_request(
            object(),
            12,
            timeout_seconds=5,
            stop_on_input_requested=True,
        )
    )

    assert result["halt_reason"] == "operator_input_requested"


def test_wait_for_request_treats_terminal_status_before_stale_input_card(
    monkeypatch,
):
    async def fake_snapshot(_client, request_id):
        assert request_id == 12
        return {
            "request": {"id": 12, "channel_id": 3, "status": "complete"},
            "messages": [
                {
                    "id": 8,
                    "deleted": False,
                    "metadata": {
                        "special_type": "input_requested",
                        "input_requested": {"title": "Approve"},
                    },
                }
            ],
        }

    monkeypatch.setattr(
        native_chat, "fetch_request_snapshot", fake_snapshot
    )

    result = asyncio.run(
        native_chat.wait_for_request(
            object(),
            12,
            timeout_seconds=5,
            stop_on_input_requested=True,
        )
    )

    assert result["request"]["status"] == "complete"
    assert "halt_reason" not in result


def test_deleted_input_card_does_not_count_as_active_hitl():
    assert not native_chat._has_input_requested(
        [
            {
                "deleted": True,
                "metadata": {
                    "special_type": "input_requested",
                    "input_requested": {"title": "Approve"},
                },
            }
        ]
    )


def test_canary_cli_never_reuses_prepared_channel(monkeypatch, capsys):
    observed = {}
    monkeypatch.setenv("model", "inert-test-model")

    async def fake_login(**_kwargs):
        return object()

    async def fake_run(_client, prompt, **kwargs):
        observed["prompt"] = prompt
        observed.update(kwargs)
        return {
            "chat_channel_id": 3,
            "chat_request_id": 4,
            "status": "processing",
            "halt_reason": "operator_input_requested",
            "messages": [],
            "runtime_telemetry": {},
        }

    monkeypatch.setattr(native_chat, "login", fake_login)
    monkeypatch.setattr(native_chat, "run_native_chat_turn", fake_run)

    args = native_chat.build_parser().parse_args(
        ["canary", "--prompt", "Run pwd on callback 1", "--max-steps", "8"]
    )
    assert asyncio.run(native_chat._run(args)) == 0

    assert observed["use_prepared_channel"] is False
    assert observed["stop_on_input_requested"] is True
    assert observed["metadata"]["config"]["mode"] == "supervised"
    assert observed["metadata"]["config"]["autonomous_solve"] is False
    assert "operator_input_requested" in capsys.readouterr().out


@pytest.mark.parametrize(
    "result",
    [
        {
            "chat_channel_id": 3,
            "chat_request_id": 4,
            "status": "error",
            "error": "model failure",
            "messages": [],
        },
        {
            "chat_channel_id": 3,
            "chat_request_id": 4,
            "status": "complete",
            "error": "",
            "messages": [],
        },
    ],
)
def test_canary_cli_fails_without_actual_input_request(
    monkeypatch, capsys, result
):
    monkeypatch.setenv("model", "inert-test-model")
    async def fake_login(**_kwargs):
        return object()

    async def fake_run(_client, _prompt, **_kwargs):
        return result

    monkeypatch.setattr(native_chat, "login", fake_login)
    monkeypatch.setattr(native_chat, "run_native_chat_turn", fake_run)

    args = native_chat.build_parser().parse_args(
        ["canary", "--prompt", "Run pwd on callback 1"]
    )

    assert asyncio.run(native_chat._run(args)) == 1
    capsys.readouterr()


def test_parser_requires_explicit_request_selector():
    parser = native_chat.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["status"])

    status = parser.parse_args(["status", "--request-id", "5"])
    follow = parser.parse_args(["follow", "--latest", "--channel-id", "7"])
    transcript = parser.parse_args(["transcript", "--latest"])
    request_snapshot = parser.parse_args(["snapshot", "--request-id", "9"])

    assert status.request_id == 5
    assert follow.latest is True
    assert follow.channel_id == 7
    assert transcript.command == "transcript"
    assert request_snapshot.request_id == 9


def test_paused_request_snapshot_preserves_card_without_terminal_grammar():
    snapshot = {
        "request": {
            "id": 9,
            "channel_id": 4,
            "request_message_id": 1,
            "status": "streaming",
            "error": "",
        },
        "messages": [
            {
                "id": 3,
                "channel_id": 4,
                "chat_request_id": 9,
                "message": "approve getprivs",
                "metadata": {
                    "special_type": "input_requested",
                    "input_requested": {"status": "pending", "type": "tool_approval"},
                },
            },
            {
                "id": 1,
                "channel_id": 4,
                "chat_request_id": None,
                "message": "Check privileges",
                "metadata": {},
            },
        ],
    }

    captured = native_chat.build_request_snapshot_export(snapshot)
    assert captured["schema"] == "sage-native-chat-request-snapshot-v1"
    assert captured["capture_state"] == "pending_operator_input"
    assert captured["chat_request_id"] == 9
    assert captured["chat_channel_id"] == 4
    assert [row["id"] for row in captured["messages"]] == [1, 3]
    assert captured["messages"][1]["metadata"]["input_requested"]["status"] == "pending"
    with pytest.raises(RuntimeError, match="control transition sequence"):
        native_chat.build_transcript_export(snapshot)

    snapshot["messages"][0]["channel_id"] = 5
    with pytest.raises(RuntimeError, match="channel_id"):
        native_chat.build_request_snapshot_export(snapshot)


def test_snapshot_command_writes_private_paused_capture(tmp_path, monkeypatch, capsys):
    async def fake_login(**_kwargs):
        return object()

    async def fake_fetch(_client, request_id):
        assert request_id == 9
        return {
            "request": {
                "id": 9, "channel_id": 4, "request_message_id": 1,
                "status": "streaming", "error": "",
            },
            "messages": [
                {"id": 1, "channel_id": 4, "chat_request_id": None,
                 "message": "prompt", "metadata": {}},
                {"id": 3, "channel_id": 4, "chat_request_id": 9,
                 "message": "card", "metadata": {
                     "special_type": "input_requested",
                     "input_requested": {"status": "pending"},
                 }},
            ],
        }

    monkeypatch.setattr(native_chat, "login", fake_login)
    monkeypatch.setattr(native_chat, "fetch_request_snapshot", fake_fetch)
    output = tmp_path / "paused.json"
    args = native_chat.build_parser().parse_args(
        ["snapshot", "--request-id", "9", "--output", str(output)]
    )
    assert asyncio.run(native_chat._run(args)) == 0
    assert json.loads(output.read_text())["capture_state"] == "pending_operator_input"
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(capsys.readouterr().out)["export_path"] == str(output)


def test_transcript_writer_is_atomic_json(tmp_path):
    output = tmp_path / "transcript.json"
    native_chat.write_transcript_export(
        output,
        {
            "schema": "sage-native-chat-transcript-v1",
            "messages": [{"id": 1}],
        },
    )

    assert json.loads(output.read_text())["messages"] == [{"id": 1}]
    assert output.stat().st_mode & 0o777 == 0o600
    assert not output.with_suffix(".json.tmp").exists()


def test_default_transcript_path_is_private_durable_and_manifested(
    tmp_path, monkeypatch
):
    durable = tmp_path / "history"
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(durable))

    output = native_chat.default_transcript_export_path(42)
    native_chat.write_transcript_export(
        output,
        {
            "schema": "sage-native-chat-transcript-v1",
            "chat_request_id": 42,
            "messages": [],
        },
    )
    retention = native_chat.record_transcript_export(output, 42)

    assert output.is_relative_to(durable)
    assert retention["retention_class"] == "durable-private"
    assert retention["manifested"] is True
    assert (durable / "manifest.jsonl").is_file()
