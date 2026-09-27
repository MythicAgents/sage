from __future__ import annotations

import asyncio
import base64
from concurrent import futures
import json

import aio_pika.exchange
import aiormq.channel
import grpc
from yarl import URL
from google.protobuf.wrappers_pb2 import BytesValue
import pytest
import mythic_container
import mythic_container.rabbitmq as rabbitmq_module
from types import SimpleNamespace

from ai import observation_channel, resource_budget, resource_rpc


@pytest.fixture(autouse=True)
def restore_rpc_methods():
    publish = aiormq.channel.Channel.basic_publish
    insecure = grpc.insecure_channel
    secure = grpc.secure_channel
    send_rpc = rabbitmq_module.rabbitmqConnectionClass.SendRPCMessage
    reply_rpc = rabbitmq_module.rabbitmqConnectionClass.ReplyMessage
    try:
        yield
    finally:
        aiormq.channel.Channel.basic_publish = publish
        grpc.insecure_channel = insecure
        grpc.secure_channel = secure
        rabbitmq_module.rabbitmqConnectionClass.SendRPCMessage = send_rpc
        rabbitmq_module.rabbitmqConnectionClass.ReplyMessage = reply_rpc


def _budget(monkeypatch, tmp_path, limit=8):
    config = tmp_path / "config.json"
    ledger = tmp_path / "ledger.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {"control_requests": {"limit": limit, "cleanup_reserve": 0}},
        "calendar_seconds": 1000,
        "cleanup_seconds": 1,
    }))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", str(config))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_LEDGER", str(ledger))
    return resource_budget.ResourceLedger(config, ledger)


def test_outer_exchange_and_direct_reply_converge_on_one_charge_each(monkeypatch, tmp_path):
    ledger = _budget(monkeypatch, tmp_path, 2)
    calls = []

    async def original(_channel, body, **kwargs):
        calls.append((body, kwargs))
        return "ack"

    monkeypatch.setattr(resource_rpc, "_ORIGINAL_BASIC_PUBLISH", original)
    assert resource_rpc.install() is True
    underlay = object.__new__(aiormq.channel.Channel)

    class Channel:
        async def get_underlay_channel(self):
            return underlay

    exchange = type("Exchange", (), {
        "internal": False, "name": "mythic_exchange", "channel": Channel(),
    })()
    message = aio_pika.Message(body=b"outer-body")
    assert asyncio.run(aio_pika.exchange.Exchange.publish(exchange, message, "outer")) == "ack"
    assert asyncio.run(underlay.basic_publish(body=b"reply-body", routing_key="direct")) == "ack"
    assert [(body, kwargs["routing_key"]) for body, kwargs in calls] == [
        (b"outer-body", "outer"), (b"reply-body", "direct"),
    ]
    assert ledger.status()["effective_resource_charges"]["control_requests"] == 2


def test_basic_publish_precharges_repeated_failed_and_denied_calls(monkeypatch, tmp_path):
    ledger = _budget(monkeypatch, tmp_path, 3)
    calls = []

    async def original(_channel, body, **kwargs):
        calls.append((body, kwargs["routing_key"], kwargs))
        if kwargs["routing_key"] == "error":
            raise ConnectionError("private detail")
        return "ack"

    monkeypatch.setattr(resource_rpc, "_ORIGINAL_BASIC_PUBLISH", original)
    assert resource_rpc.install() is True
    channel = object.__new__(aiormq.channel.Channel)
    assert asyncio.run(channel.basic_publish(body=b"exact-body", routing_key="first")) == "ack"
    assert asyncio.run(channel.basic_publish(body=b"exact-body", routing_key="second")) == "ack"
    with pytest.raises(ConnectionError):
        asyncio.run(channel.basic_publish(body=b"exact-body", routing_key="error"))
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        asyncio.run(channel.basic_publish(body=b"denied", routing_key="denied"))
    assert [row[:2] for row in calls] == [
        (b"exact-body", "first"), (b"exact-body", "second"), (b"exact-body", "error"),
    ]
    assert ledger.status()["effective_resource_charges"]["control_requests"] == 3


def test_basic_publish_capture_exact_body_and_exception_type_only(monkeypatch, tmp_path):
    ledger = _budget(monkeypatch, tmp_path, 1)
    events = []
    monkeypatch.setenv(observation_channel.ENV_NAME, "/configured")
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))

    async def original(*_args, **_kwargs):
        raise TimeoutError("private raw exception")

    monkeypatch.setattr(resource_rpc, "_ORIGINAL_BASIC_PUBLISH", original)
    resource_rpc.install()
    body = b"binary\x00body"
    channel = object.__new__(aiormq.channel.Channel)
    with pytest.raises(TimeoutError):
        asyncio.run(channel.basic_publish(body=body, routing_key="route"))
    request, response = events
    assert request[0] == "control_request"
    assert request[1]["body_base64"] == base64.b64encode(body).decode()
    assert request[1]["routing_key"] == "route"
    assert response[0] == "control_response" and response[1]["request_id"] == request[1]["request_id"]
    assert response[1]["outcome"] == "error" and response[1]["exception_type"] == "TimeoutError"
    reservations = [event for line in ledger.ledger_path.read_text().splitlines()
                    for event in [json.loads(line)] if event.get("type") == "reservation"]
    assert response[1]["reservation_id"] == reservations[0]["reservation_id"]


def test_actual_unary_rpc_counts_calls_caps_retries_and_captures(monkeypatch, tmp_path):
    ledger = _budget(monkeypatch, tmp_path, 2)
    events = []
    monkeypatch.setenv(observation_channel.ENV_NAME, "/configured")
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    server_calls = []

    def handler(request, _context):
        server_calls.append(request.value)
        return BytesValue(value=b"private response")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(
        "meter.Service", {"Call": grpc.unary_unary_rpc_method_handler(
            handler, request_deserializer=BytesValue.FromString,
            response_serializer=BytesValue.SerializeToString,
        )}),))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    seen_options = []
    original = resource_rpc._ORIGINAL_INSECURE_CHANNEL

    def make_channel(target, options=None, compression=None):
        seen_options.append(tuple(options or ()))
        return original(target, options=options, compression=compression)

    monkeypatch.setattr(resource_rpc, "_ORIGINAL_INSECURE_CHANNEL", make_channel)
    resource_rpc.install()
    channel = grpc.insecure_channel(
        f"127.0.0.1:{port}", options=(("grpc.primary_user_agent", "test"), ("grpc.enable_retries", 1)),
    )
    call = channel.unary_unary(
        "/meter.Service/Call", request_serializer=BytesValue.SerializeToString,
        response_deserializer=BytesValue.FromString,
    )
    try:
        assert call(BytesValue(value=b"one")).value == b"private response"
        assert call(BytesValue(value=b"two")).value == b"private response"
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            call(BytesValue(value=b"denied"))
    finally:
        channel.close()
        server.stop(None)
    assert server_calls == [b"one", b"two"]
    assert ("grpc.primary_user_agent", "test") in seen_options[0]
    assert ("grpc.enable_retries", 0) in seen_options[0]
    assert ("grpc.enable_retries", 1) not in seen_options[0]
    requests = [payload for kind, payload in events if kind == "control_request"]
    assert [row["body_base64"] for row in requests] == [
        base64.b64encode(BytesValue(value=b"one").SerializeToString()).decode(),
        base64.b64encode(BytesValue(value=b"two").SerializeToString()).decode(),
        base64.b64encode(BytesValue(value=b"denied").SerializeToString()).decode(),
    ]
    assert all(row["rpc_method"] == "/meter.Service/Call" for row in requests)
    responses = [payload for kind, payload in events if kind == "control_response"]
    assert len(responses) == 3
    assert responses[-1]["outcome"] == "denied_before_dispatch" and "reservation_id" not in responses[-1]
    ids = [event["reservation_id"] for line in ledger.ledger_path.read_text().splitlines()
           for event in [json.loads(line)] if event.get("type") == "reservation"]
    assert [row["reservation_id"] for row in responses[:2]] == ids and len(set(ids)) == 2
    assert ledger.status()["effective_resource_charges"]["control_requests"] == 2


def test_amqp_denial_remains_an_observed_attempt_without_dispatch(monkeypatch, tmp_path):
    ledger = _budget(monkeypatch, tmp_path, 1)
    events, dispatched = [], []
    monkeypatch.setenv(observation_channel.ENV_NAME, "/configured")
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))

    async def original(*_args, **_kwargs):
        dispatched.append(True)
        return "ack"

    monkeypatch.setattr(resource_rpc, "_ORIGINAL_BASIC_PUBLISH", original)
    resource_rpc.install()
    channel = object.__new__(aiormq.channel.Channel)
    asyncio.run(channel.basic_publish(body=b"first", routing_key="route"))
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        asyncio.run(channel.basic_publish(body=b"denied", routing_key="route"))
    assert dispatched == [True]
    request, response = events[-2:]
    assert request[0] == "control_request"
    assert base64.b64decode(request[1]["body_base64"]) == b"denied"
    assert response == ("control_response", {
        "request_id": request[1]["request_id"], "transport": "amqp",
        "outcome": "denied_before_dispatch", "exception_type": "ResourceBudgetExceeded",
    })
    assert ledger.status()["effective_resource_charges"]["control_requests"] == 1


def test_unary_failure_consumes_charge_and_other_cardinality_is_denied(monkeypatch, tmp_path):
    ledger = _budget(monkeypatch, tmp_path, 3)
    events = []
    monkeypatch.setenv(observation_channel.ENV_NAME, "/configured")
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    resource_rpc.install()
    channel = grpc.insecure_channel("127.0.0.1:1")
    try:
        with pytest.raises(grpc.RpcError):
            channel.unary_unary("/missing.Service/Call")(b"request", timeout=0.1)
        with pytest.raises(resource_budget.ResourceBudgetError, match="unary-unary"):
            next(channel.unary_stream("/missing.Service/Stream")(iter((b"request",)), timeout=0.1))
    finally:
        channel.close()
    assert ledger.status()["effective_resource_charges"]["control_requests"] == 1
    response = next(payload for kind, payload in events if kind == "control_response")
    assert response["outcome"] == "error"
    assert response["exception_type"] == "_InactiveRpcError"


def test_install_without_config_preserves_native_methods(monkeypatch):
    monkeypatch.delenv("SAGE_RESOURCE_BUDGET_CONFIG", raising=False)
    monkeypatch.delenv("SAGE_RESOURCE_BUDGET_LEDGER", raising=False)
    publish = aiormq.channel.Channel.basic_publish
    insecure = grpc.insecure_channel
    secure = grpc.secure_channel
    send_rpc = rabbitmq_module.rabbitmqConnectionClass.SendRPCMessage
    assert resource_rpc.install() is False
    assert aiormq.channel.Channel.basic_publish is publish
    assert grpc.insecure_channel is insecure
    assert grpc.secure_channel is secure
    assert rabbitmq_module.rabbitmqConnectionClass.SendRPCMessage is send_rpc


def test_amqp_endpoint_fields_are_sanitized_on_failure_and_denial(monkeypatch, tmp_path):
    _budget(monkeypatch, tmp_path, 1); events = []
    monkeypatch.setenv(observation_channel.ENV_NAME, "/configured")
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    async def original(*_args, **_kwargs): raise TimeoutError
    monkeypatch.setattr(resource_rpc, "_ORIGINAL_BASIC_PUBLISH", original)
    connection = type("Connection", (), {"url": URL("amqps://user:embedded-secret@broker.example:5671/measured?token=embedded-secret"),
                                           "vhost": "/measured"})()
    channel = type("Channel", (), {"connection": connection})()
    with pytest.raises(TimeoutError):
        asyncio.run(resource_rpc._basic_publish(channel, b"body", exchange="exchange", routing_key="route"))
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        asyncio.run(resource_rpc._basic_publish(channel, b"denied", exchange="exchange", routing_key="route"))
    requests = [payload for kind, payload in events if kind == "control_request"]
    assert all(row["endpoint"] == {"scheme": "amqps", "host": "broker.example", "port": 5671,
                                    "vhost": "/measured"} for row in requests)
    assert all(row["exchange"] == "exchange" and row["routing_key"] == "route" for row in requests)
    assert "embedded-secret" not in json.dumps(events)


def test_actual_grpc_endpoints_differ_and_malformed_is_unavailable(monkeypatch, tmp_path):
    _budget(monkeypatch, tmp_path, 3); events = []
    monkeypatch.setenv(observation_channel.ENV_NAME, "/configured")
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))

    def server():
        instance = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
        instance.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("meter.Endpoint", {
            "Call": grpc.unary_unary_rpc_method_handler(lambda request, context: request,
                request_deserializer=BytesValue.FromString, response_serializer=BytesValue.SerializeToString)}),))
        port = instance.add_insecure_port("127.0.0.1:0"); instance.start(); return instance, port
    first, p1 = server(); second, p2 = server(); resource_rpc.install()
    try:
        for target in (f"127.0.0.1:{p1}", f"dns:///127.0.0.1:{p2}"):
            channel = grpc.insecure_channel(target)
            try:
                assert channel.unary_unary("/meter.Endpoint/Call",
                    request_serializer=BytesValue.SerializeToString,
                    response_deserializer=BytesValue.FromString)(BytesValue(value=b"same"), timeout=1).value == b"same"
            finally: channel.close()
    finally:
        first.stop(None); second.stop(None)
    endpoints = [payload["endpoint"] for kind, payload in events if kind == "control_request"]
    assert endpoints == [{"resolver": "direct", "host": "127.0.0.1", "port": p1, "secure": False},
                         {"resolver": "dns", "host": "127.0.0.1", "port": p2, "secure": False}]
    assert resource_rpc._grpc_endpoint("malformed/path", False) == {"endpoint_unavailable": True}


def test_actual_rpc_semantic_result_captures_token_transient_with_correlation(monkeypatch, tmp_path):
    _budget(monkeypatch, tmp_path, 2); events=[]
    monkeypatch.setenv(observation_channel.ENV_NAME,"/configured")
    monkeypatch.setattr(observation_channel,"emit",lambda kind,payload: events.append((kind,payload)) or True)
    async def publish(*_args,**_kwargs):return "ack"
    monkeypatch.setattr(resource_rpc,"_ORIGINAL_BASIC_PUBLISH",publish)
    async def rpc(self,queue,body,retry_policy=0):
        channel=type("Channel",(),{"connection":SimpleNamespace(url=SimpleNamespace(scheme="amqp",host="127.0.0.1",port=5672,path="/v",),vhost="v")})()
        await resource_rpc._basic_publish(channel,body,exchange="mythic_exchange",routing_key=queue,
            properties=SimpleNamespace(correlation_id="correlation",reply_to="reply"))
        return {"success":True,"apitoken":"TRANSIENT_TOKEN_SENTINEL"}
    monkeypatch.setattr(resource_rpc,"_ORIGINAL_SEND_RPC_MESSAGE",rpc);resource_rpc.install()
    connection=object.__new__(rabbitmq_module.rabbitmqConnectionClass);connection.conn=SimpleNamespace(url=SimpleNamespace(scheme="amqp",host="127.0.0.1",port=5672,path="/v"))
    result=asyncio.run(connection.SendRPCMessage("mythic_rpc_apitoken_create",b"{}"))
    event=next(payload for kind,payload in events if kind=="tool_event")
    assert result["apitoken"]=="TRANSIENT_TOKEN_SENTINEL" and event["routing_key"]=="mythic_rpc_apitoken_create"
    assert event["attempts"][0]["correlation_id"]=="correlation" and event["attempts"][0]["reply_to"]=="reply"
    request=next(payload for kind,payload in events if kind=="control_request")
    assert event["endpoint"]==request["endpoint"]
    assert "TRANSIENT_TOKEN_SENTINEL" not in repr([(kind,payload) for kind,payload in events if kind!="tool_event"])


def test_actual_reply_parent_links_nested_publish_and_malformed_stays_unknown(monkeypatch,tmp_path):
    _budget(monkeypatch,tmp_path,3);events=[];monkeypatch.setenv(observation_channel.ENV_NAME,"/configured")
    monkeypatch.setattr(observation_channel,"emit",lambda kind,payload:events.append((kind,payload)) or True)
    async def publish(*_args,**_kwargs):return "ack"
    monkeypatch.setattr(resource_rpc,"_ORIGINAL_BASIC_PUBLISH",publish)
    async def reply(self,response,message):
        channel=SimpleNamespace(connection=SimpleNamespace(url=SimpleNamespace(scheme="amqp",host="127.0.0.1",port=5672,path="/v"),vhost="v"))
        return await resource_rpc._basic_publish(channel,response,exchange="",routing_key=message.reply_to,
            properties=SimpleNamespace(correlation_id=message.correlation_id))
    monkeypatch.setattr(resource_rpc,"_ORIGINAL_REPLY_MESSAGE",reply);resource_rpc.install()
    connection=object.__new__(rabbitmq_module.rabbitmqConnectionClass);connection.conn=SimpleNamespace(url=SimpleNamespace(scheme="amqp",host="127.0.0.1",port=5672,path="/v"))
    message=SimpleNamespace(reply_to="dynamic-reply",correlation_id="corr",routing_key="incoming",exchange="",app_id=None)
    assert asyncio.run(connection.ReplyMessage(b"result",message))=="ack"
    parent=next(payload for kind,payload in events if kind=="tool_event")
    request=next(payload for kind,payload in events if kind=="control_request")
    assert parent["available"] is True and request["reply_parent_event_id"]==parent["parent_id"]
    assert request["routing_key"]=="dynamic-reply" and request["correlation_id"]=="corr"
    assert parent["producer_name"]=="rabbitmqConnectionClass.ReplyMessage" and parent["incoming_exchange"]==""
    events.clear();message.reply_to=None;message.correlation_id=None;asyncio.run(connection.ReplyMessage(b"result",message))
    assert events[0][1]["available"] is False and {"reply_to","correlation_id"}<=set(events[0][1]["missing_fields"])
    assert "reply_parent_event_id" not in next(payload for kind,payload in events if kind=="control_request")
