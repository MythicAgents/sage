from __future__ import annotations

import argparse
import asyncio
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
        assert query == native_chat.REQUEST_QUERY
        assert variables == {"requestId": 9}
        return {
            "chat_request": [
                {
                    "id": 9,
                    "channel_id": 4,
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
                    "metadata": {},
                },
                {
                    "id": 1,
                    "channel_id": 4,
                    "chat_request_id": 9,
                    "message": "prompt",
                    "metadata": {},
                },
            ],
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
                    "status": "complete",
                    "error": "",
                },
                "messages": [
                    {
                        "id": 1,
                        "channel_id": 4,
                        "chat_request_id": 10,
                        "message": "prompt",
                        "metadata": {},
                    }
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
                    "status": "complete",
                    "error": "",
                },
                "messages": [
                    {
                        "id": 1,
                        "channel_id": 4,
                        "message": "prompt",
                        "metadata": {},
                    }
                ],
            }
        )


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

    assert status.request_id == 5
    assert follow.latest is True
    assert follow.channel_id == 7
    assert transcript.command == "transcript"


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
    assert not output.with_suffix(".json.tmp").exists()
