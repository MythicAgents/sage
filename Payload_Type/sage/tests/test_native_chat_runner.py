import asyncio
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-live-runner" / "scripts" / "native_chat.py"
SPEC = importlib.util.spec_from_file_location("native_chat_runner", SCRIPT)
native_chat = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(native_chat)


def test_select_chat_resources_accepts_wildcard_token():
    container, token = native_chat.select_chat_resources(
        {
            "consuming_container": [
                {"id": 1, "container_running": True, "deleted": False}
            ],
            "apitokens": [
                {"id": 3, "active": True, "deleted": False, "scopes": ["*"]}
            ],
        }
    )

    assert container["id"] == 1
    assert token["id"] == 3


def test_select_chat_resources_fails_without_required_scope():
    with pytest.raises(RuntimeError, match="wildcard scope"):
        native_chat.select_chat_resources(
            {
                "consuming_container": [
                    {"id": 1, "container_running": True, "deleted": False}
                ],
                "apitokens": [
                    {
                        "id": 2,
                        "active": True,
                        "deleted": False,
                        "scopes": ["apitoken.write", "chat-ai.write"],
                    }
                ],
            }
        )


def test_ensure_api_token_creates_wildcard_for_autonomous_operations(monkeypatch):
    calls = []

    async def fake_query(client, query, variables=None):
        calls.append(variables)
        if variables is None:
            return {
                "consuming_container": [],
                "apitokens": [
                    {
                        "id": 2,
                        "active": True,
                        "deleted": False,
                        "scopes": ["apitoken.write", "chat-ai.write"],
                    }
                ],
            }
        return {
            "createAPIToken": {
                "id": 3,
                "name": "Sage native chat",
                "scopes": ["*"],
                "status": "success",
                "error": "",
            }
        }

    monkeypatch.setattr(native_chat.mythic, "execute_custom_query", fake_query)

    result = asyncio.run(native_chat.ensure_api_token(object()))

    assert result["created"] is True
    assert calls[-1]["scopes"] == ["*"]


def test_default_ai_metadata_is_autonomous(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'export provider="OpenAI"\nexport model="test-model"\nexport API_ENDPOINT="http://localhost/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(native_chat, "SAGE_ENV_PATH", env_file)

    metadata = native_chat.default_ai_metadata({"seed": 4})

    assert metadata["seed"] == 4
    assert metadata["config"]["mode"] == "auto"
    assert metadata["config"]["autonomous_solve"] is True
    assert metadata["config"]["max_steps"] == 0
    assert metadata["config"]["model"] == "test-model"
    assert metadata["config"]["provider"] == "openai"


def test_run_native_chat_turn_returns_channel_and_request(monkeypatch):
    async def fake_channel(*args, **kwargs):
        return {"chat_channel_id": 10, "chat_channel_name": "seed", "api_token_id": 2}

    async def fake_message(*args, **kwargs):
        return {"chat_message_id": 20, "chat_request_id": 30}

    async def fake_wait(*args, **kwargs):
        return {
            "request": {"status": "complete", "error": ""},
            "messages": [{"message": "done"}],
        }

    async def no_prepared_channel(*args, **kwargs):
        return None

    monkeypatch.setattr(native_chat, "create_locked_channel", fake_channel)
    monkeypatch.setattr(native_chat, "find_prepared_channel", no_prepared_channel)
    monkeypatch.setattr(native_chat, "create_message", fake_message)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    result = asyncio.run(native_chat.run_native_chat_turn(object(), "objective"))

    assert result["chat_channel_id"] == 10
    assert result["chat_request_id"] == 30
    assert result["status"] == "complete"
    assert result["runtime_telemetry"] == {}


def test_extract_runtime_telemetry_reads_terminal_metadata():
    telemetry = {
        "policy_mode": "llm",
        "semantic_transaction_count": 2,
        "authorized_transaction_count": 2,
        "semantic_policy_coverage": 1.0,
    }
    messages = [
        {"metadata": {"tool_use": {"status": "completed"}}},
        {"metadata": {"runtime_telemetry": telemetry}},
    ]

    assert native_chat.extract_runtime_telemetry(messages) == telemetry


def test_extract_runtime_telemetry_reads_error_wrapped_metadata():
    telemetry = {"policy_mode": "symbolic"}
    messages = [{
        "metadata": {
            "container_metadata": {
                "runtime_telemetry": telemetry,
            }
        }
    }]

    assert native_chat.extract_runtime_telemetry(messages) == telemetry


def test_prepare_locked_channel_reuses_empty_prepared_channel(monkeypatch):
    async def fake_find(client):
        return {
            "chat_channel_id": 10,
            "chat_channel_name": "Sage GOAD Ready",
            "prepared": True,
            "reused": True,
        }

    async def fail_create(*args, **kwargs):
        raise AssertionError("existing prepared channel must be reused")

    monkeypatch.setattr(native_chat, "find_prepared_channel", fake_find)
    monkeypatch.setattr(native_chat, "create_locked_channel", fail_create)

    result = asyncio.run(native_chat.prepare_locked_channel(object()))

    assert result["chat_channel_id"] == 10
    assert result["reused"] is True


def test_run_native_chat_turn_prefers_prepared_channel(monkeypatch):
    calls = []

    async def fake_find(client):
        return {
            "chat_channel_id": 11,
            "chat_channel_name": "Sage GOAD Ready",
            "prepared": True,
            "reused": True,
        }

    async def fail_create(*args, **kwargs):
        raise AssertionError("prepared channel must be used")

    async def fake_message(client, channel_id, prompt):
        calls.append((channel_id, prompt))
        return {"chat_message_id": 20, "chat_request_id": 30}

    async def fake_wait(*args, **kwargs):
        return {"request": {"status": "complete", "error": ""}, "messages": []}

    monkeypatch.setattr(native_chat, "find_prepared_channel", fake_find)
    monkeypatch.setattr(native_chat, "create_locked_channel", fail_create)
    monkeypatch.setattr(native_chat, "create_message", fake_message)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    result = asyncio.run(native_chat.run_native_chat_turn(object(), "objective"))

    assert result["chat_channel_id"] == 11
    assert calls == [(11, "objective")]
