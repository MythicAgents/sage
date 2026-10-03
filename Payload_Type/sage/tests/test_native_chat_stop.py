import asyncio
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-live-runner" / "scripts" / "native_chat.py"
SPEC = importlib.util.spec_from_file_location("native_chat_stop_runner", SCRIPT)
native_chat = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(native_chat)


def _snapshot(status="streaming", *, ack=False, request_id=44, channel_id=7,
              terminal_content="cancelled", final_request_id=None):
    messages = [{"id": 1, "channel_id": channel_id, "chat_request_id": None,
                 "metadata": {}, "author_type": "operator"}]
    if ack:
        messages.append({
            "id": 2, "channel_id": channel_id,
            "chat_request_id": request_id if final_request_id is None else final_request_id,
            "author_type": "ai", "metadata": {"container_metadata": {
                "event_id": "final_response:one",
                "control_transitions": [
                    {"event_id": "control:install", "kind": "control_transition",
                     "phase": "request_installed", "content": "request contract installed"},
                    {"event_id": "control:terminal", "kind": "control_transition",
                     "phase": "request_terminal", "content": terminal_content},
                ],
            }},
        })
    return {"request": {"id": request_id, "channel_id": channel_id,
                        "request_message_id": 1, "status": status}, "messages": messages}


def _install(monkeypatch, snapshots, action=None):
    observed = {"queries": [], "fetches": 0}
    async def fetch(_client, _request_id):
        index = min(observed["fetches"], len(snapshots) - 1)
        observed["fetches"] += 1
        return snapshots[index]
    async def query(_client, mutation, variables=None):
        observed["queries"].append((mutation, variables))
        return {"chatCancelRequest": action or {"status": "success", "error": None,
                                                "request_id": 44}}
    async def no_create(*_args, **_kwargs):
        raise AssertionError("cancel must not create a chat message")
    monkeypatch.setattr(native_chat, "fetch_request_snapshot", fetch)
    monkeypatch.setattr(native_chat.mythic, "execute_custom_query", query)
    monkeypatch.setattr(native_chat, "create_message", no_create)
    return observed


def _stop(**kwargs):
    return asyncio.run(native_chat.stop_native_chat_request(
        object(), channel_id=7, request_id=44, timeout_seconds=0.04,
        poll_interval_seconds=0.001, **kwargs))


def test_native_cancel_uses_exact_mutation_and_waits_for_late_ack(monkeypatch):
    observed = _install(monkeypatch, [_snapshot(), _snapshot("cancelled"),
                                     _snapshot("cancelled", ack=True)])
    result = _stop()
    assert observed["queries"] == [(native_chat.CANCEL_REQUEST_MUTATION,
                                    {"request_id": 44})]
    assert "chatCancelRequest(request_id: $request_id)" in native_chat.CANCEL_REQUEST_MUTATION
    assert result["cancel_sent"] is True
    assert result["cancel_action_status"] == "success"
    assert result["original_request_status"] == "cancelled"
    assert result["producer_acknowledged"] is True
    assert result["remote_stop_confirmed"] is True
    assert observed["fetches"] == 3


def test_already_cancelled_with_ack_is_idempotent_without_mutation(monkeypatch):
    observed = _install(monkeypatch, [_snapshot("cancelled", ack=True)])
    result = _stop()
    assert result["remote_stop_confirmed"] is True
    assert result["cancel_sent"] is False
    assert observed["queries"] == []


def test_cancelled_without_ack_times_out_unconfirmed(monkeypatch):
    observed = _install(monkeypatch, [_snapshot("cancelled")])
    result = _stop()
    assert result["original_request_terminal"] is True
    assert result["remote_stop_confirmed"] is False
    assert observed["fetches"] > 1
    assert observed["queries"] == []


def test_completion_race_is_natural_terminal_without_mutation(monkeypatch):
    observed = _install(monkeypatch, [_snapshot("complete", ack=True,
                                              terminal_content="complete")])
    result = _stop()
    assert result["original_request_status"] == "complete"
    assert result["remote_stop_confirmed"] is False
    assert observed["queries"] == []


@pytest.mark.parametrize("updates, match", [
    ({"request_id": 45}, "request id"),
    ({"channel_id": 8}, "channel id"),
])
def test_rejects_request_or_channel_mismatch_before_mutation(monkeypatch, updates, match):
    observed = _install(monkeypatch, [_snapshot(**updates)])
    with pytest.raises(RuntimeError, match=match):
        _stop()
    assert observed["queries"] == []


def test_action_error_stops_without_waiting_or_confirmation(monkeypatch):
    observed = _install(monkeypatch, [_snapshot()],
                        {"status": "error", "error": "denied"})
    result = _stop()
    assert len(observed["queries"]) == 1
    assert observed["fetches"] == 1
    assert result["cancel_action_error"] == "denied"
    assert result["remote_stop_confirmed"] is False


def test_action_request_id_mismatch_fails_closed(monkeypatch):
    observed = _install(monkeypatch, [_snapshot()],
                        {"status": "success", "error": None, "request_id": 45})
    with pytest.raises(RuntimeError, match="wrong request id"):
        _stop()
    assert len(observed["queries"]) == 1


def test_action_timeout_is_bounded_and_unconfirmed(monkeypatch):
    observed = _install(monkeypatch, [_snapshot()])
    async def timeout(*_args, **_kwargs):
        raise TimeoutError
    monkeypatch.setattr(native_chat.mythic, "execute_custom_query", timeout)
    result = _stop()
    assert result["cancel_action_error"] == "cancel action timed out"
    assert result["remote_stop_confirmed"] is False
    assert observed["fetches"] == 1


def test_initial_snapshot_timeout_never_mutates(monkeypatch):
    observed = _install(monkeypatch, [_snapshot()])
    async def timeout(*_args, **_kwargs):
        raise TimeoutError
    monkeypatch.setattr(native_chat, "fetch_request_snapshot", timeout)
    result = _stop()
    assert result["observation_error"] == "initial request snapshot timed out"
    assert result["remote_stop_confirmed"] is False
    assert observed["queries"] == []


@pytest.mark.parametrize("snapshot", [
    _snapshot("cancelled", ack=True, terminal_content="complete"),
    _snapshot("cancelled", ack=True, final_request_id=45),
    _snapshot("cancelled"),
])
def test_wrong_or_missing_ack_does_not_confirm(snapshot):
    try:
        acknowledged = native_chat.request_cancellation_acknowledged(snapshot)
    except RuntimeError:
        acknowledged = False
    assert acknowledged is False


@pytest.mark.parametrize("change", [
    lambda message: message.update(author_type="operator"),
    lambda message: message["metadata"]["container_metadata"].update(event_id="other:one"),
    lambda message: message["metadata"]["container_metadata"].update(control_transitions=[]),
    lambda message: message["metadata"]["container_metadata"]["control_transitions"].append(
        message["metadata"]["container_metadata"]["control_transitions"][-1].copy()),
    lambda message: message["metadata"]["container_metadata"]["control_transitions"][0].update(
        content="wrong installation"),
])
def test_malformed_or_non_sage_final_ack_fails_closed(change):
    snapshot = _snapshot("cancelled", ack=True)
    change(snapshot["messages"][1])
    assert native_chat.request_cancellation_acknowledged(snapshot) is False


def test_ack_accepts_event_identity_at_root_with_transitions_in_container_metadata():
    snapshot = _snapshot("cancelled", ack=True)
    metadata = snapshot["messages"][1]["metadata"]
    metadata["event_id"] = metadata["container_metadata"].pop("event_id")
    assert native_chat.request_cancellation_acknowledged(snapshot) is True


def test_rejects_nonpositive_timeout_before_control_plane_call(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("must fail before a control-plane call")
    monkeypatch.setattr(native_chat, "fetch_request_snapshot", forbidden)
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(native_chat.stop_native_chat_request(
            object(), channel_id=7, request_id=44, timeout_seconds=0))
