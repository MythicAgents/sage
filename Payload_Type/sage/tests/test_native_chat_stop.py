import asyncio
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-live-runner" / "scripts" / "native_chat.py"
SPEC = importlib.util.spec_from_file_location("native_chat_stop_runner", SCRIPT)
native_chat = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(native_chat)


def _snapshot(request_id, status):
    return {"request": {"id": request_id, "channel_id": 7, "status": status}, "messages": []}


def test_stop_native_chat_request_sends_exactly_one_stop_and_confirms_both_terminals(monkeypatch):
    sent = []

    async def fake_create(_client, channel_id, prompt):
        sent.append((channel_id, prompt))
        return {"chat_request_id": 91}

    async def fake_wait(_client, request_id, **_kwargs):
        return _snapshot(request_id, "completed" if request_id == 91 else "cancelled")

    monkeypatch.setattr(native_chat, "create_message", fake_create)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    result = asyncio.run(native_chat.stop_native_chat_request(
        object(), channel_id=7, request_id=44, timeout_seconds=10,
    ))

    assert sent == [(7, "/stop")]
    assert result["stop_request_id"] == 91
    assert result["stop_request_terminal"] is True
    assert result["original_request_terminal"] is True
    assert result["remote_stop_confirmed"] is True


def test_stop_native_chat_request_does_not_infer_confirmation_from_stop_delivery(monkeypatch):
    async def fake_create(_client, _channel_id, _prompt):
        return {"chat_request_id": 91}

    async def fake_wait(_client, request_id, **_kwargs):
        if request_id == 91:
            return _snapshot(request_id, "completed")
        raise native_chat.RequestWaitTimeout(request_id, 1, _snapshot(request_id, "processing"))

    monkeypatch.setattr(native_chat, "create_message", fake_create)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    result = asyncio.run(native_chat.stop_native_chat_request(
        object(), channel_id=7, request_id=44, timeout_seconds=10,
    ))

    assert result["stop_sent"] is True
    assert result["stop_request_terminal"] is True
    assert result["original_request_terminal"] is False
    assert result["remote_stop_confirmed"] is False


def test_stop_native_chat_request_rejects_nonpositive_timeout(monkeypatch):
    async def fail_create(*_args, **_kwargs):
        raise AssertionError("must fail before a control-plane call")

    monkeypatch.setattr(native_chat, "create_message", fail_create)
    try:
        asyncio.run(native_chat.stop_native_chat_request(
            object(), channel_id=7, request_id=44, timeout_seconds=0,
        ))
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("zero timeout was accepted")
