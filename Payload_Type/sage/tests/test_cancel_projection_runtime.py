"""Offline Mythic SDK → Sage request-ledger cancellation projection probes."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import mythic_container
from mythic_container import ChatBase, chat_utils, mythic_service

from ai.langgraph.model import Model
from ai.langgraph.request_events import stable_event_id
from sage_chat.streaming import ChatStreamEmitter
from sage_chat.service import SageChat, _CHANNEL_TURN_LOCKS
from sage_chat.session import get_channel_session, put_channel_session
from sage_chat.headless import HeadlessSageChat


def _model(emitter, request_id):
    model = Model.__new__(Model)
    model._response_emitter = emitter
    model._active_delegations = {}
    model.verbose = False
    model.begin_visibility_turn(
        request_id,
        operator_prompt="operator prompt",
        native_request_id="41",
        logical_request_id=request_id,
    )
    model._request_event_ledger.record_once(
        event_id=stable_event_id(request_id, "control_transition", "contract-installed"),
        kind="control_transition",
        phase="request_installed",
        content="request contract installed",
    )
    return model


def test_sdk_sync_binds_sage_cancel_wrapper_without_network(monkeypatch):
    bound = []

    async def no_shared_services(_name):
        pass

    async def sync_rpc(**_kwargs):
        return {"success": True}

    async def receive_direct(*, queue, routing_key, handler):
        bound.append((queue, routing_key, handler))

    async def receive_rpc(*, queue, routing_key, handler):
        bound.append((queue, routing_key, handler))

    monkeypatch.setattr(mythic_service, "startSharedServices", no_shared_services)
    monkeypatch.setattr(mythic_service, "payloadQueueTasks", [])
    monkeypatch.setattr(mythic_container.RabbitmqConnection, "SendRPCDictMessage", sync_rpc)
    monkeypatch.setattr(mythic_container.RabbitmqConnection, "ReceiveFromMythicDirectExchange", receive_direct)
    monkeypatch.setattr(mythic_container.RabbitmqConnection, "ReceiveFromRPCQueue", receive_rpc)

    class ChatDefinition:
        name = "sage"

        def get_sync_message(self):
            return {"name": "sage"}

    async def scenario():
        await mythic_service.syncChatData(ChatDefinition())
        await asyncio.gather(*mythic_service.payloadQueueTasks)

    asyncio.run(scenario())
    cancel_route = mythic_service.getRoutingKey("sage", mythic_container.CHAT_CANCEL_ROUTING_KEY)
    matching = [entry for entry in bound if entry[0] == cancel_route]
    assert len(matching) == 1
    assert matching[0][2] is chat_utils.ChatCancelHandler
    assert getattr(matching[0][2], "_sage_paused_cancel_wrapper", False) is True


def test_sdk_active_cancel_projects_one_cancelled_response_and_terminal_evidence(monkeypatch):
    responses = []

    async def capture(response):
        responses.append(response)

    monkeypatch.setattr(ChatBase, "SendMythicRPCChatResponse", capture)
    monkeypatch.setattr(ChatBase, "chatServices", {})
    monkeypatch.setattr(ChatBase, "chatRequestTasks", {})
    monkeypatch.setattr(ChatBase, "chatCancelledRequests", set())
    request = ChatBase.ChatRequest(
        container_name="sage-cancel-probe", operation_id=1, channel_id=2,
        request_id=41, prompt="probe",
    )
    entered = asyncio.Event()
    model = None

    class ProbeChat(ChatBase.Chat):
        name = "sage-cancel-probe"

        async def chat(self, active_request):
            nonlocal model
            model = _model(ChatStreamEmitter(self, active_request), "request:41")
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await model._emit_operator_stop("Session cancelled.", status="cancelled")
                raise

    ChatBase.chatServices[ProbeChat.name] = ProbeChat()

    async def scenario():
        running = asyncio.create_task(chat_utils.ChatRequestHandler(json.dumps(request.to_json()).encode()))
        await entered.wait()
        await chat_utils.ChatCancelHandler(json.dumps({
            "container_name": ProbeChat.name, "operation_id": 1,
            "channel_id": 2, "request_id": 41,
        }).encode())
        await running

    asyncio.run(scenario())

    evidence = [item for item in responses if item.Metadata.get("event_id")]
    assert len(evidence) == 1
    assert evidence[0].Status == "cancelled"
    assert evidence[0].Complete is True
    assert evidence[0].CompleteRequest is False
    assert evidence[0].ResponseKey.startswith("event:final_response:")
    assert [item.Status for item in responses] == ["cancelled", "cancelled"]
    assert [item.phase for item in model._request_event_ledger.actual_events(
        kind="control_transition",
    )] == ["request_installed", "request_terminal"]
    assert asyncio.run(model.finalize_visibility_turn(require_final=True))["ok"] is True


def test_cancelled_final_projection_retries_same_event_without_streaming(monkeypatch):
    responses = []

    async def fail_once(response):
        responses.append(response)
        if len(responses) == 1:
            raise RuntimeError("publish failed")

    monkeypatch.setattr(ChatBase, "SendMythicRPCChatResponse", fail_once)
    chat = ChatBase.Chat()
    request = ChatBase.ChatRequest(operation_id=1, channel_id=2, request_id=41)
    model = _model(ChatStreamEmitter(chat, request), "request:failed-cancel")

    asyncio.run(model._emit_operator_stop("Session cancelled.", status="cancelled"))
    assert len(responses) == 1
    assert asyncio.run(model.finalize_visibility_turn(require_final=True))["ok"] is False
    asyncio.run(model._emit_operator_stop("Later text is inert.", status="cancelled"))
    assert len(responses) == 2, [(r.Status, r.Content, r.ResponseKey) for r in responses]
    assert model._request_event_ledger.phase_count(
        stable_event_id("request:failed-cancel", "final_response", "terminal"),
        "final_response", "emitted", projected=True,
    ) == 1
    asyncio.run(model._emit_operator_stop("Duplicate is inert.", status="cancelled"))
    assert asyncio.run(model.finalize_visibility_turn(require_final=True))["ok"] is True
    assert len(responses) == 2, [
        (item.ResponseKey, item.Status, item.Content) for item in responses
    ]
    assert {(item.ResponseKey, item.Content, item.Status, item.Complete, item.CompleteRequest)
            for item in responses} == {
                (responses[0].ResponseKey, "Session cancelled.", "cancelled", True, False)
            }


def test_non_cancelled_final_keeps_existing_streaming_projection(monkeypatch):
    responses = []

    async def capture(response):
        responses.append(response)

    monkeypatch.setattr(ChatBase, "SendMythicRPCChatResponse", capture)
    chat = ChatBase.Chat()
    request = ChatBase.ChatRequest(operation_id=1, channel_id=2, request_id=41)
    model = _model(ChatStreamEmitter(chat, request), "request:stop")
    asyncio.run(model._emit_operator_stop("Session stopped.", status="stopped"))

    assert len(responses) == 1
    assert responses[0].Status == "streaming"
    assert responses[0].Complete is False
    assert asyncio.run(model.finalize_visibility_turn(require_final=True))["ok"] is True


def test_sdk_inactive_cancel_closes_exact_paused_hitl_request(monkeypatch):
    responses = []

    async def capture(response):
        responses.append(response)

    monkeypatch.setattr(ChatBase, "SendMythicRPCChatResponse", capture)
    monkeypatch.setattr(ChatBase, "chatServices", {"sage": SageChat()})
    monkeypatch.setattr(ChatBase, "chatRequestTasks", {})
    monkeypatch.setattr(ChatBase, "chatCancelledRequests", set())
    request = ChatBase.ChatRequest(
        container_name="sage", operation_id=771, channel_id=772,
        request_id=773, apitokens_id=774,
    )
    model = _model(ChatStreamEmitter(ChatBase.chatServices["sage"], request), "request:paused")
    model.operation_id = request.OperationID
    model.channel_id = request.ChannelID
    model.apitoken_id = request.APITokenID
    model.task_id = 0
    model.provider = "offline"
    model.model = "offline"
    model._running_tasks = set()
    model._request_contract = SimpleNamespace(request_id="request:paused", digest="bound-digest")
    model._pending_approval_context = {
        "request_id": "request:paused", "request_contract_digest": "bound-digest",
        "operation_id": "771", "apitoken_id": "774",
    }
    model._hitl_card_pending = True
    model._controller_hitl_pending = None

    async def scenario():
        await put_channel_session(request, model)
        for changed in (
            {"operation_id": 999},
            {"channel_id": 999},
            {"request_id": 999},
            {"container_name": "other"},
        ):
            near = {
                "container_name": "sage", "operation_id": 771,
                "channel_id": 772, "request_id": 773,
                **changed,
            }
            await chat_utils.ChatCancelHandler(json.dumps(near).encode())
            assert await get_channel_session(request) is model
            assert responses == []
        model._pending_approval_context["request_contract_digest"] = "wrong"
        await chat_utils.ChatCancelHandler(json.dumps({
            "container_name": "sage", "operation_id": 771,
            "channel_id": 772, "request_id": 773,
        }).encode())
        assert await get_channel_session(request) is model
        assert responses == []
        model._pending_approval_context["request_contract_digest"] = "bound-digest"
        model.apitoken_id = 999
        await chat_utils.ChatCancelHandler(json.dumps({
            "container_name": "sage", "operation_id": 771,
            "channel_id": 772, "request_id": 773,
        }).encode())
        assert await get_channel_session(request) is model
        assert responses == []
        model.apitoken_id = 774
        message = json.dumps({
            "container_name": "sage", "operation_id": 771,
            "channel_id": 772, "request_id": 773,
        }).encode()
        original_emitter = model._response_emitter
        lock = _CHANNEL_TURN_LOCKS.setdefault("772", asyncio.Lock())
        async with lock:
            racing_cancel = asyncio.create_task(chat_utils.ChatCancelHandler(message))
            await asyncio.sleep(0)
            replacement = ChatBase.ChatRequest(
                container_name="sage", operation_id=771, channel_id=772,
                request_id=900, apitokens_id=774,
            )
            model._response_emitter = ChatStreamEmitter(ChatBase.chatServices["sage"], replacement)
        await racing_cancel
        assert await get_channel_session(request) is model
        assert responses == []
        model._response_emitter = original_emitter
        await chat_utils.ChatCancelHandler(message)
        await chat_utils.ChatCancelHandler(message)
        return await get_channel_session(request)

    assert asyncio.run(scenario()) is None
    assert model._pending_approval_context is None
    assert model._hitl_card_pending is False
    assert model._stop_reason == "operator"
    assert len(responses) == 1
    assert responses[0].Status == "cancelled"
    assert responses[0].Complete is True
    assert responses[0].CompleteRequest is False
    assert asyncio.run(model.finalize_visibility_turn(require_final=True))["ok"] is True

    runner_path = Path(__file__).resolve().parents[3] / "skills/sage-live-runner/scripts/native_chat.py"
    spec = importlib.util.spec_from_file_location("native_chat_cancel_probe", runner_path)
    runner = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(runner)
    snapshot = {
        "request": {
            "id": 773, "channel_id": 772, "request_message_id": 775,
            "status": "cancelled", "error": "Cancelled by operator",
        },
        "messages": [
            {
                "id": 775, "channel_id": 772, "chat_request_id": None,
                "author_type": "operator", "metadata": {}, "content": "approve?",
            },
            {
                "id": 776, "channel_id": 772, "chat_request_id": 773,
                "author_type": "ai", "metadata": responses[0].Metadata,
                "content": responses[0].Content,
            },
        ],
    }
    assert runner.request_cancellation_acknowledged(snapshot) is True
    exported = runner.build_transcript_export(snapshot)
    assert exported["control_transitions"][-1]["phase"] == "request_terminal"
    assert exported["control_transitions"][-1]["content"] == "cancelled"


def test_sdk_releases_paused_service_task_then_cancel_reaches_session(monkeypatch):
    import sage_chat.service as service

    class PausingModel:
        provider = "offline"
        model = "offline"

        def __init__(self):
            self.operation_id = 881
            self.channel_id = 882
            self.apitoken_id = 884
            self.stops = []

        def set_verbose(self, _value):
            pass

        async def _hitl_interrupt_pending(self, _thread_id):
            return False

        def install_request_contract(self, contract):
            self._request_contract = contract

        async def invoke(self, _prompt, **_kwargs):
            contract = self._request_contract
            self._pending_approval_context = {
                "request_id": contract.request_id,
                "request_contract_digest": contract.digest,
                "operation_id": "881", "apitoken_id": "884",
            }
            self._hitl_card_pending = True
            return ""

        def request_stop(self, reason):
            self.stops.append(reason)

        async def _emit_operator_stop(self, content, *, status):
            self.stops.append(status)

        async def finalize_visibility_turn(self, *, require_final):
            return {"ok": True}

    class PausingChat(HeadlessSageChat):
        async def _get_or_create_model(self, request):
            await put_channel_session(request, model)
            return model, False

    async def first_claim(_request):
        return True

    monkeypatch.setattr(service, "claim_channel_marker", first_claim)
    monkeypatch.setattr(ChatBase, "chatServices", {})
    monkeypatch.setattr(ChatBase, "chatRequestTasks", {})
    monkeypatch.setattr(ChatBase, "chatCancelledRequests", set())
    model = PausingModel()
    chat = PausingChat()
    ChatBase.chatServices["sage"] = chat
    request = ChatBase.ChatRequest(
        container_name="sage", operation_id=881, channel_id=882,
        request_id=883, apitokens_id=884, prompt="request approval",
    )

    async def scenario():
        await chat_utils.ChatRequestHandler(json.dumps(request.to_json()).encode())
        assert 883 not in ChatBase.chatRequestTasks
        assert await get_channel_session(request) is model
        assert chat.terminal_emissions == []
        await chat_utils.ChatCancelHandler(json.dumps({
            "container_name": "sage", "operation_id": 881,
            "channel_id": 882, "request_id": 883,
        }).encode())
        return await get_channel_session(request)

    assert asyncio.run(scenario()) is None
    assert model.stops == ["operator", "cancelled"]
