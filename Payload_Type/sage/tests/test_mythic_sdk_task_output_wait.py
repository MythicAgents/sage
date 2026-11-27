"""The Mythic SDK's task-output wait against real websockets and HTTP servers speaking Mythic's protocol.

Sage waits for every issued task with ``mythic.waitfor_for_task_output`` (``ai/langgraph/mythic_tools.py``). The SDK
opens a GraphQL subscription over gql's websockets transport, then looks up subtasks over HTTP GraphQL on the same
host and port. Nothing here stands in for either: a real ``websockets`` server answers the upgrade with the
subprotocol Mythic's endpoint negotiates (``graphql-ws``, the Apollo protocol, when offered gql's default pair), a
real aiohttp server answers the subtask queries, and a byte relay puts both behind one port as Mythic's nginx does.

Rows: a task that completes, one that errors, and one that never finishes within the wait's timeout. Each checks the
output Sage receives, the token and task the subscription carried, and that the subscription was shut down before the
SDK went on to its HTTP lookups. The SDK closes its subscription explicitly from 0.3.0rc10 on; earlier releases left
it for the garbage collector after the loop ended.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from mythic import mythic, mythic_classes
from websockets.asyncio.server import serve

TOKEN = "fixture-api-token"
TASK_DISPLAY_ID = 41
TASK_ID = 7


def _response(text: str) -> dict[str, str]:
    return {"response_text": base64.b64encode(text.encode()).decode(), "timestamp": "2026-11-25T00:00:00"}


@dataclass
class _Mythic:
    """One GraphQL endpoint: websocket subscriptions and HTTP queries on one port, as nginx serves Mythic."""

    frames: list[dict[str, Any]]
    events: list[str] = field(default_factory=list)
    subscription: dict[str, Any] = field(default_factory=dict)
    upgrade_headers: dict[str, str] = field(default_factory=dict)
    negotiated: str | None = None

    async def _websocket(self, connection) -> None:
        self.negotiated = connection.subprotocol
        self.upgrade_headers = {name.lower(): value for name, value in connection.request.headers.raw_items()}
        try:
            async for raw in connection:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "connection_init":
                    await connection.send(json.dumps({"type": "connection_ack"}))
                elif kind == "start":
                    self.subscription = message
                    self.events.append("ws:start")
                    for frame in self.frames:
                        await connection.send(json.dumps(
                            {"type": "data", "id": message["id"], "payload": {"data": {"task_stream": [frame]}}}
                        ))
                elif kind == "stop":
                    self.events.append("ws:stop")
                    await connection.send(json.dumps({"type": "complete", "id": message["id"]}))
                elif kind == "connection_terminate":
                    self.events.append("ws:terminate")
        finally:
            self.events.append("ws:closed")

    async def _http(self, request: web.Request) -> web.Response:
        body = await request.json()
        query = body.get("query", "")
        self.events.append("http:" + ("task-id" if "taskIdFromDisplayID" in query else "subtasks"))
        assert request.headers.get("Authorization") == f"Bearer {TOKEN}"
        if "taskIdFromDisplayID" in query:
            assert body["variables"] == {"task_display_id": TASK_DISPLAY_ID}
            return web.json_response({"data": {"task": [{"id": TASK_ID}]}})
        assert body["variables"] == {"task_id": TASK_ID}
        return web.json_response({"data": {"task": []}})

    async def run(self, call) -> Any:
        websocket_server = await serve(self._websocket, "127.0.0.1", 0, subprotocols=["graphql-ws"])
        websocket_port = websocket_server.sockets[0].getsockname()[1]
        app = web.Application()
        app.router.add_post("/graphql/", self._http)
        runner = web.AppRunner(app)
        await runner.setup()
        http_site = web.TCPSite(runner, "127.0.0.1", 0)
        await http_site.start()
        http_port = http_site._server.sockets[0].getsockname()[1]

        async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            first = await reader.read(65536)
            port = websocket_port if first.startswith(b"GET ") else http_port
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
            upstream_writer.write(first)

            async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
                try:
                    while chunk := await source.read(65536):
                        sink.write(chunk)
                        await sink.drain()
                except (ConnectionError, asyncio.CancelledError):
                    pass
                finally:
                    sink.close()

            await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))

        front = await asyncio.start_server(relay, "127.0.0.1", 0)
        port = front.sockets[0].getsockname()[1]
        client = mythic_classes.Mythic(apitoken=TOKEN, server_ip="127.0.0.1", server_port=port, ssl=False)
        try:
            return await call(client)
        finally:
            front.close()
            websocket_server.close()
            await runner.cleanup()


def _run(frames: list[dict[str, Any]], *, timeout: int | None = None) -> tuple[bytes, _Mythic]:
    server = _Mythic(frames)

    async def call(client):
        return await mythic.waitfor_for_task_output(mythic=client, task_display_id=TASK_DISPLAY_ID, timeout=timeout)

    result = asyncio.run(asyncio.wait_for(server.run(call), 30))
    return result, server


ROWS = {
    "completed": (
        [
            {"status": "processing", "completed": False, "responses": [_response("first ")]},
            {"status": "completed", "completed": True, "responses": [_response("first "), _response("second")]},
        ],
        None,
        b"first second",
    ),
    "error": (
        [{"status": "error: access denied", "completed": False, "responses": [_response("denied")]}],
        None,
        b"denied",
    ),
    "timeout": (
        [{"status": "processing", "completed": False, "responses": [_response("partial")]}],
        1,
        b"partial",
    ),
}


@pytest.mark.parametrize("row", sorted(ROWS))
def test_task_output_wait_returns_the_task_output_over_mythics_subprotocol(row):
    frames, timeout, expected = ROWS[row]

    output, server = _run(frames, timeout=timeout)

    assert output == expected
    assert server.negotiated == "graphql-ws"
    assert server.upgrade_headers.get("authorization") == f"Bearer {TOKEN}"
    assert server.subscription["payload"]["variables"] == {"task_display_id": TASK_DISPLAY_ID}
    assert "task_stream" in server.subscription["payload"]["query"]
    assert [event for event in server.events if event.startswith("http:")] == ["http:task-id", "http:subtasks"]


@pytest.mark.parametrize("row", sorted(ROWS))
def test_task_output_wait_shuts_its_subscription_before_the_subtask_lookup(row):
    frames, timeout, _ = ROWS[row]

    _, server = _run(frames, timeout=timeout)

    first_http = server.events.index("http:task-id")
    shutdown = [index for index, event in enumerate(server.events) if event in {"ws:stop", "ws:closed"}]
    assert shutdown and shutdown[0] < first_http, server.events


# The recorded wait result of a supervised whoami (gateway live check, 2026-11-26, request 85, task 22), with the lab
# account name replaced as in skills/sage-focused-capability-tests/tests/fixtures/task_result_request85.json. Mythic's
# server rows were not captured, so the split into two rows (the first 30 bytes, then the rest) is synthetic.
RECORDED_RAW = "Primary Identity: LAB\\lab.user\nImpersonation Identity: LAB\\lab.user"


def _recorded_rows(order: tuple[int, ...]) -> list[dict[str, Any]]:
    data = RECORDED_RAW.encode()
    chunks = (data[:30].decode(), data[30:].decode())
    return [{"status": "completed", "completed": True, "responses": [_response(chunks[index]) for index in order]}]


@pytest.mark.parametrize("order, holds", [((0, 1), True), ((0,), False), ((1, 0), False)],
                         ids=["full pair", "second row dropped", "rows reversed"])
def test_the_recorded_wait_result_arrives_whole_and_in_order(order, holds):
    """Sage decodes what the SDK returns (``_task_output_text``) and shows it on the task's lifecycle card; the value
    must be every row, in order. Dropping or reversing a row is the control that the comparison can fail."""
    from ai.langgraph.mythic_tools import _task_output_text

    output, server = _run(_recorded_rows(order))

    assert (_task_output_text(output) == RECORDED_RAW) is holds
    assert server.negotiated == "graphql-ws"


def test_sages_login_arguments_keep_mythic_tls_unverified_as_before():
    """rc10 made certificate verification opt-in (``verify_ssl``/``ssl_ca_file``). Sage logs in with only the token,
    host, port and ``ssl`` (``MythicTools.login``), so HTTP GraphQL keeps ``ssl=False`` and the websocket keeps a
    non-verifying context, as rc8 did. Opting in is a separate, reviewed decision."""
    import ssl

    from mythic import mythic_utilities

    client = mythic_classes.Mythic(apitoken=TOKEN, server_ip="mythic.invalid", server_port=7443, ssl=True)
    assert getattr(client, "verify_ssl", False) is False
    assert mythic_utilities.get_ssl(client) is False
    context = mythic_utilities.get_ssl(client, websocket=True)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_NONE and context.check_hostname is False
