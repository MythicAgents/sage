"""Opt-in request accounting at native application RPC publish seams."""
from __future__ import annotations

import base64
import ipaddress
import os
import re
import json
from contextvars import ContextVar
import mythic_container.rabbitmq as _rabbitmq
from typing import Any
from uuid import uuid4

import aiormq.channel
import grpc

from . import resource_budget


_ORIGINAL_BASIC_PUBLISH = aiormq.channel.Channel.basic_publish
_ORIGINAL_INSECURE_CHANNEL = grpc.insecure_channel
_ORIGINAL_SECURE_CHANNEL = grpc.secure_channel
_ORIGINAL_SEND_RPC_MESSAGE = _rabbitmq.rabbitmqConnectionClass.SendRPCMessage
_ORIGINAL_REPLY_MESSAGE = _rabbitmq.rabbitmqConnectionClass.ReplyMessage
_rpc_attempts: ContextVar[list[dict[str, Any]] | None] = ContextVar("sage_rpc_attempts", default=None)
_reply_parent: ContextVar[str | None] = ContextVar("sage_reply_parent", default=None)

_DIRECT_TARGET = re.compile(r"^(?:([A-Za-z0-9._-]+)|\[([0-9A-Fa-f:.]+)\]):([0-9]{1,5})$")


def _grpc_endpoint(target: Any, secure: bool) -> dict[str, Any]:
    if not isinstance(target, str):
        return {"endpoint_unavailable": True}
    resolver, rendered = ("dns", target[7:]) if target.startswith("dns:///") else ("direct", target)
    if "://" in rendered or any(char in rendered for char in "/?#@"):
        return {"endpoint_unavailable": True}
    match = _DIRECT_TARGET.fullmatch(rendered)
    if match is None:
        return {"endpoint_unavailable": True}
    hostname = match.group(1) or match.group(2)
    try:
        port = int(match.group(3))
        if not 0 < port < 65536:
            raise ValueError
        if match.group(2):
            ipaddress.IPv6Address(hostname)
    except ValueError:
        return {"endpoint_unavailable": True}
    return {"endpoint": {"resolver": resolver, "host": hostname, "port": port, "secure": secure}}


def _amqp_endpoint(channel: Any) -> dict[str, Any]:
    try:
        connection = getattr(channel, "connection", None)
        url = getattr(connection, "url", None)
        scheme, host, port = (getattr(url, name, None) for name in ("scheme", "host", "port"))
        vhost = getattr(connection, "vhost", None)
        if not isinstance(vhost, str):
            path = getattr(url, "path", None)
            if path in ("", "/"):
                vhost = "/"
            elif isinstance(path, str) and path.startswith("/"):
                vhost = path[1:].replace("%2F", "/")
    except Exception:
        return {"endpoint_unavailable": True}
    if scheme not in {"amqp", "amqps"} or not isinstance(host, str) or not host \
            or type(port) is not int or not 0 < port < 65536 or not isinstance(vhost, str):
        return {"endpoint_unavailable": True}
    return {"endpoint": {"scheme": scheme, "host": host, "port": port, "vhost": vhost}}


def _ledger_reserve() -> str | None:
    ledger = resource_budget.configured_ledger()
    if ledger is not None:
        return ledger.reserve({"control_requests": 1}, "control-rpc-application-request")
    return None


def _capture(kind: str, payload: dict[str, Any]) -> None:
    from . import observation_channel

    if os.environ.get(observation_channel.ENV_NAME, ""):
        observation_channel.emit(kind, payload)


def _reserve_captured(request_id: str, transport: str) -> str | None:
    try:
        return _ledger_reserve()
    except resource_budget.ResourceBudgetError as exc:
        _capture("control_response", {
            "request_id": request_id, "transport": transport,
            "outcome": "denied_before_dispatch", "exception_type": type(exc).__name__,
        })
        raise


async def _basic_publish(
    self, body: bytes, *, exchange: str = "", routing_key: str = "",
    properties=None, mandatory: bool = False, immediate: bool = False,
    timeout: float | int | None = None, wait: bool = True,
):
    request_id = uuid4().hex
    endpoint = _amqp_endpoint(self)
    properties_payload = {}
    for name in ("correlation_id", "reply_to"):
        value = getattr(properties, name, None)
        if isinstance(value, str) and value:
            properties_payload[name] = value
    if _reply_parent.get() is not None:
        properties_payload["reply_parent_event_id"] = _reply_parent.get()
    _capture("control_request", {
        "request_id": request_id, "transport": "amqp",
        "body_base64": base64.b64encode(body).decode("ascii"),
        "exchange": exchange, "routing_key": routing_key, **properties_payload, **endpoint,
    })
    if _rpc_attempts.get() is not None:
        _rpc_attempts.get().append({"request_id": request_id, **properties_payload})
    reservation = _reserve_captured(request_id, "amqp")
    try:
        result = await _ORIGINAL_BASIC_PUBLISH(
            self, body, exchange=exchange, routing_key=routing_key,
            properties=properties, mandatory=mandatory, immediate=immediate,
            timeout=timeout, wait=wait,
        )
    except BaseException as exc:
        _capture("control_response", {
            "request_id": request_id, "transport": "amqp", "outcome": "error",
            "exception_type": type(exc).__name__,
            **({"reservation_id": reservation} if reservation is not None else {}),
        })
        raise
    _capture("control_response", {
        "request_id": request_id, "transport": "amqp", "outcome": "returned",
        **({"reservation_id": reservation} if reservation is not None else {}),
    })
    return result


async def _reply_message(self, response: bytes, message):
    if resource_budget.configured_ledger() is None or not os.environ.get("SAGE_OBSERVATION_DIRECTORY", ""):
        return await _ORIGINAL_REPLY_MESSAGE(self, response, message)
    parent_id=uuid4().hex
    values={"reply_to":getattr(message,"reply_to",None),"correlation_id":getattr(message,"correlation_id",None),
            "incoming_routing_key":getattr(message,"routing_key",None),"incoming_exchange":getattr(message,"exchange",None)}
    missing=sorted(name for name,value in values.items() if not isinstance(value,str) or name!="incoming_exchange" and not value)
    connection=type("RPCConnection",(),{"connection":getattr(self,"conn",None)})()
    endpoint=_amqp_endpoint(connection)
    if "endpoint" not in endpoint:missing.append("endpoint")
    payload={"event":"amqp_reply_parent","parent_id":parent_id,"transport":"amqp",
             "producer_name":"rabbitmqConnectionClass.ReplyMessage",**endpoint}
    app_id=getattr(message,"app_id",None)
    if isinstance(app_id,str) and app_id:payload["incoming_app_id"]=app_id
    if missing:payload.update(available=False,missing_fields=missing)
    else:payload.update(available=True,**values)
    _capture("tool_event",payload)
    token=_reply_parent.set(parent_id if not missing else None)
    try:return await _ORIGINAL_REPLY_MESSAGE(self,response,message)
    finally:_reply_parent.reset(token)


async def _send_rpc_message(self, queue: str, body: bytes, retry_policy=_rabbitmq.RPC_RETRY_POLICY_RETRY_ON_TIMEOUT):
    if resource_budget.configured_ledger() is None or not os.environ.get("SAGE_OBSERVATION_DIRECTORY", ""):
        return await _ORIGINAL_SEND_RPC_MESSAGE(self, queue, body, retry_policy=retry_policy)
    rpc_call_id = uuid4().hex; attempts: list[dict[str, Any]] = []; token = _rpc_attempts.set(attempts)
    try:
        result = await _ORIGINAL_SEND_RPC_MESSAGE(self, queue, body, retry_policy=retry_policy)
    finally:
        _rpc_attempts.reset(token)
    endpoint = _amqp_endpoint(type("RPCConnection", (), {"connection": getattr(self, "conn", None)})())
    observation_channel_payload = {"event": "amqp_rpc_result", "rpc_call_id": rpc_call_id,
        "transport": "amqp", "routing_key": queue, "body_base64": base64.b64encode(
            json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).decode("ascii"),
        "attempts": attempts, **endpoint}
    _capture("tool_event", observation_channel_payload)
    return result


class _ClientInterceptor(
    grpc.UnaryUnaryClientInterceptor,
    grpc.UnaryStreamClientInterceptor,
    grpc.StreamUnaryClientInterceptor,
    grpc.StreamStreamClientInterceptor,
):
    def __init__(self, endpoint: dict[str, Any]):
        self._endpoint = endpoint

    def intercept_unary_unary(self, continuation, client_call_details, request):
        request_id = uuid4().hex
        request_payload: dict[str, Any] = {
            "request_id": request_id, "transport": "grpc",
            "rpc_method": str(client_call_details.method), **self._endpoint,
        }
        serializer = getattr(request, "SerializeToString", None)
        if callable(serializer):
            try:
                body = serializer()
            except Exception:
                request_payload["body_unavailable"] = True
            else:
                if isinstance(body, bytes):
                    request_payload["body_base64"] = base64.b64encode(body).decode("ascii")
                else:
                    request_payload["body_unavailable"] = True
        else:
            request_payload["body_unavailable"] = True
        _capture("control_request", request_payload)
        reservation = _reserve_captured(request_id, "grpc")
        try:
            result = continuation(client_call_details, request)
        except BaseException as exc:
            _capture("control_response", {
                "request_id": request_id, "transport": "grpc", "outcome": "error",
                "exception_type": type(exc).__name__,
                **({"reservation_id": reservation} if reservation is not None else {}),
            })
            raise
        def record_done(call) -> None:
            try:
                exception = call.exception()
            except BaseException as exc:
                exception = exc
            payload = {"request_id": request_id, "transport": "grpc",
                       **({"reservation_id": reservation} if reservation is not None else {})}
            if exception is None:
                payload["outcome"] = "returned"
            else:
                payload.update(outcome="error", exception_type=type(exception).__name__)
            _capture("control_response", payload)

        done_callback = getattr(result, "add_done_callback", None)
        if callable(done_callback):
            done_callback(record_done)
        else:
            _capture("control_response", {
                "request_id": request_id, "transport": "grpc", "outcome": "returned",
                **({"reservation_id": reservation} if reservation is not None else {}),
            })
        return result

    @staticmethod
    def _unsupported() -> None:
        raise resource_budget.ResourceBudgetError(
            "metered RPC channel supports application unary-unary calls only"
        )

    def intercept_unary_stream(self, continuation, client_call_details, request):
        self._unsupported()

    def intercept_stream_unary(self, continuation, client_call_details, request_iterator):
        self._unsupported()

    def intercept_stream_stream(self, continuation, client_call_details, request_iterator):
        self._unsupported()


def _metered_options(options: Any) -> tuple[Any, ...]:
    preserved = tuple(
        option for option in (options or ())
        if not (isinstance(option, tuple) and option and option[0] == "grpc.enable_retries")
    )
    return preserved + (("grpc.enable_retries", 0),)


def _insecure_channel(target, options=None, compression=None):
    channel = _ORIGINAL_INSECURE_CHANNEL(
        target, options=_metered_options(options), compression=compression,
    )
    return grpc.intercept_channel(channel, _ClientInterceptor(_grpc_endpoint(target, False)))


def _secure_channel(target, credentials, options=None, compression=None):
    channel = _ORIGINAL_SECURE_CHANNEL(
        target, credentials, options=_metered_options(options), compression=compression,
    )
    return grpc.intercept_channel(channel, _ClientInterceptor(_grpc_endpoint(target, True)))


def install() -> bool:
    """Install idempotent process hooks only for an explicitly configured ledger."""
    if resource_budget.configured_ledger() is None:
        return False
    aiormq.channel.Channel.basic_publish = _basic_publish
    grpc.insecure_channel = _insecure_channel
    grpc.secure_channel = _secure_channel
    _rabbitmq.rabbitmqConnectionClass.SendRPCMessage = _send_rpc_message
    _rabbitmq.rabbitmqConnectionClass.ReplyMessage = _reply_message
    return True
