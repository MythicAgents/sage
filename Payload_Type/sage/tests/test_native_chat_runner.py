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
    with pytest.raises(RuntimeError, match="required scopes"):
        native_chat.select_chat_resources(
            {
                "consuming_container": [
                    {"id": 1, "container_running": True, "deleted": False}
                ],
                "apitokens": [
                    {"id": 2, "active": True, "deleted": False, "scopes": ["auth.read"]}
                ],
            }
        )


def test_default_ai_metadata_is_autonomous(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'export provider="openai"\nexport model="test-model"\nexport API_ENDPOINT="http://localhost/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(native_chat, "SAGE_ENV_PATH", env_file)

    metadata = native_chat.default_ai_metadata({"seed": 4})

    assert metadata["seed"] == 4
    assert metadata["config"]["mode"] == "auto"
    assert metadata["config"]["autonomous_solve"] is True
    assert metadata["config"]["max_steps"] == 0
    assert metadata["config"]["model"] == "test-model"


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

    monkeypatch.setattr(native_chat, "create_locked_channel", fake_channel)
    monkeypatch.setattr(native_chat, "create_message", fake_message)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    result = asyncio.run(native_chat.run_native_chat_turn(object(), "objective"))

    assert result["chat_channel_id"] == 10
    assert result["chat_request_id"] == 30
    assert result["status"] == "complete"
