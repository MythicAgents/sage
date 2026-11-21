"""BloodHound availability is a per-turn state on the chip, explained by the BloodHound agent only when it matters.

Drives the real ``HeadlessSageChat`` handler from ``tests.openai_responses_harness`` (real Model, graph, checkpointer
and provider client over a recorded-shape wire) without that harness's BloodHound stub, so every turn runs the real
``_ensure_bloodhound_connected``, ``ensure_bloodhound_connected``, admission check, state observation, channel
metadata and BloodHound node.

The BloodHound MCP server is registered in the real ``MCPManager`` registry with its canonical config. Its three
admitted tools answer with the shapes the server's own source returns: a CE failure is
``{"error": "Connection error: Failed to connect to BloodHound API: <requests error>"}`` (bloodhound_mcp ``main.py:64``,
``lib/bloodhound_api.py:154``), and the unpatched base rejects ``status`` with the error recorded on 2026-11-03.
The ``Connection error`` shapes come from source, not a recording: these cases prove Sage's handling of them
(wiring), and the line's live check is their evidence. Where a case needs a connect attempt, only
``MCPManager.connect_server`` is replaced: it registers (or fails to register) what the real one registers, without
starting a subprocess. The scripted Supervisor in the guard cases proves wiring only, not the live model's routing.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.tools import StructuredTool

import sage_chat.service as service
from ai import bloodhound_config as bh
from ai.mcp import MCPManager
from sage_chat import session as chat_session
from tests.openai_responses_harness import (
    CHANNEL,
    CYBER_RESPONSES,
    Process,
    ProviderWire,
    call_item,
    install_wire,
    request,
    route_config,
    text_item,
    texts,
)

PROMPT = "What is 2+2?"
GRAPH_PROMPT = "Use BloodHound to list the domains in the graph."
OTHER_CHANNEL = CHANNEL + 1
TOKEN_ID = "placeholder-token-id-offline"
TOKEN_KEY = "placeholder-token-key-offline"
BLOODHOUND_SETTINGS = {
    "BLOODHOUND_URL": "http://127.0.0.1:9",
    "BLOODHOUND_TOKEN_ID": TOKEN_ID,
    "BLOODHOUND_TOKEN_KEY": TOKEN_KEY,
}
MODEL_B = "gpt-5.6-sol"
B_RESPONSES = {"model": MODEL_B, "SAGE_OPENAI_API": "responses", "SAGE_OPENAI_ACCESS_PROGRAM": "standard",
               "SAGE_OPENAI_SAFETY_IDENTIFIER": CYBER_RESPONSES["SAGE_OPENAI_SAFETY_IDENTIFIER"]}

# Server-source shapes (bloodhound_mcp e84fb36). Synthetic values, real envelopes.
CONNECTION_ERROR = (
    "Connection error: Failed to connect to BloodHound API: HTTPConnectionPool(host='127.0.0.1', port=9): Max "
    "retries exceeded with url: /api/v2/available-domains (Caused by NewConnectionError('<urllib3.connection."
    "HTTPConnection object at 0x7f00>: Failed to establish a new connection: [Errno 111] Connection refused'))"
)
UNKNOWN_STATUS = (
    "Unknown info_type 'status'. Valid options: end_job, start_job, upload, upload_bytes, "
    "upload_bytes_to_job, upload_to_job"
)
_PROCESSES: list[Process] = []


def _block(payload) -> list[dict]:
    return [{"type": "text", "text": json.dumps(payload)}]


class OfflineBloodHound:
    """The server's three admitted tools for one CE condition: ``up``, ``down`` (refused) or ``hang``."""

    def __init__(self, ce: str = "up", *, patched: bool = True):
        self.ce = ce
        self.patched = patched
        self.calls: list[tuple[str, dict]] = []

    def _failure(self):
        return _block({"error": CONNECTION_ERROR})

    def tools(self) -> list[StructuredTool]:
        server = self

        async def domain_info(info_type: str, domain_id: str = "") -> list[dict]:
            server.calls.append(("domain_info", {"info_type": info_type}))
            if server.ce == "down":
                return server._failure()
            if server.ce == "hang":
                await asyncio.sleep(5)
            return _block({"info_type": info_type, "data": [{"name": "OFFLINE.LOCAL", "id": "S-1-5-21-0"}]})

        async def file_upload(info_type: str, job_id: int = 0, file_name: str = "", file_bytes_base64: str = "",
                              file_path: str = "", content_type: str = "") -> list[dict]:
            server.calls.append(("file_upload", {"info_type": info_type, "job_id": job_id}))
            if server.ce == "down":
                return server._failure()
            if server.ce == "hang":
                await asyncio.sleep(5)
            if info_type == "status" and server.patched:
                return _block({"info_type": "status", "data": {"job_id": job_id, "status_message": "not_found"}})
            return _block({"error": UNKNOWN_STATUS})

        async def cypher_query(query: str) -> list[dict]:
            server.calls.append(("cypher_query", {"query": query}))
            return server._failure() if server.ce == "down" else _block({"data": {"nodes": {}, "edges": []}})

        return [
            StructuredTool.from_function(coroutine=domain_info, name="domain_info", description="Domain info."),
            StructuredTool.from_function(coroutine=file_upload, name="file_upload", description="Upload."),
            StructuredTool.from_function(coroutine=cypher_query, name="cypher_query", description="Cypher."),
        ]


def _register(server: OfflineBloodHound, config=None) -> None:
    """Commit a BloodHound session to the real registry exactly as ``connect_server`` does."""
    config = config or bh.bloodhound_mcp_config(None, None)
    MCPManager.configs["BloodHound"] = config
    MCPManager.sessions["BloodHound"] = object()
    MCPManager.connections["BloodHound"] = object()
    MCPManager.tools["BloodHound"] = [
        MCPManager._wrap_tool_for_visibility("BloodHound", tool) for tool in server.tools()
    ]


def _connects(monkeypatch, server: OfflineBloodHound | None, error: str = "Connection closed") -> list[dict]:
    """Replace only the subprocess start. A server registers its session (as the real one does); None fails."""
    attempts: list[dict] = []

    async def connect_server(config):
        attempts.append(dict(config.env or {}))
        if server is None:
            return False, error
        _register(server, config)
        return True, None

    monkeypatch.setattr(MCPManager, "connect_server", connect_server)
    return attempts


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    directory = tmp_path / "bloodhound_mcp"
    directory.mkdir()
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", str(directory))
    for key in ("BLOODHOUND_URL", "BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY", "BLOODHOUND_DOMAIN",
                "BLOODHOUND_PORT", "BLOODHOUND_SCHEME"):
        monkeypatch.delenv(key, raising=False)
    MCPManager._forget_server("BloodHound")
    bh.reset_bloodhound_connect_cache()
    for channel in (CHANNEL, OTHER_CHANNEL):
        asyncio.run(chat_session.remove_session(str(channel)))
    yield

    async def close():
        for channel in (CHANNEL, OTHER_CHANNEL):
            await chat_session.remove_session(str(channel))
        for process in _PROCESSES:
            await process.close()

    asyncio.run(close())
    _PROCESSES.clear()
    service._CHANNEL_TURN_LOCKS.clear()
    MCPManager._forget_server("BloodHound")
    bh.reset_bloodhound_connect_cache()


class BloodHoundProcess(Process):
    """The harness process with the real BloodHound connect path (the harness stubs it to ``False``)."""

    def __init__(self):
        super().__init__()
        del type(self.chat)._ensure_bloodhound_connected


def _process() -> BloodHoundProcess:
    process = BloodHoundProcess()
    _PROCESSES.append(process)
    return process


def _config(*, bloodhound: bool = False, **extra) -> dict:
    return route_config(**{**CYBER_RESPONSES, **(BLOODHOUND_SETTINGS if bloodhound else {}), **extra})


def _rows(chat, since: int = 0) -> list[dict]:
    return [e for e in chat.emissions[since:] if str(e.get("response_key") or "").startswith("bloodhound_")]


def _chip(chat) -> dict:
    assert chat.channel_metadata_updates, "no channel metadata was published"
    [item] = [i for i in chat.channel_metadata_updates[-1]["items"] if i["key"] == "bloodhound"]
    return item


def _assert_chip(chat, state: str, color: str) -> None:
    item = _chip(chat)
    assert (item["value"], item["color"]) == (state, color), item
    assert item["display_value"] == state.replace("_", " "), item
    assert item["click"] == "/bloodhound"


def _no_secret(chat) -> None:
    published = json.dumps([chat.emissions, chat.channel_metadata_updates], default=str)
    assert TOKEN_ID not in published and TOKEN_KEY not in published


# --- Channel 32's shape: no notice on any rebuild path when BloodHound is not configured -------------------------


def test_an_unconfigured_chat_sends_no_bloodhound_row_through_every_rebuild_path(monkeypatch):
    """Recorded rows 80-84: a first turn, a reused turn, a route change, a model switch and a restart restore."""
    install_wire(monkeypatch, ProviderWire())

    async def scenario():
        first = _process()
        await first.chat.chat(request(PROMPT, 1, _config()))
        await first.chat.chat(request(PROMPT, 2, _config()))
        await first.chat.chat(request(PROMPT, 3, _config(SAGE_OPENAI_ACCESS_PROGRAM="standard")))
        await first.chat.chat(request(PROMPT, 4, route_config(**B_RESPONSES)))
        await first.close()
        await chat_session.remove_session(str(CHANNEL))
        service._CHANNEL_TURN_LOCKS.clear()
        second = _process()
        await second.chat.chat(request(PROMPT, 5, route_config(**B_RESPONSES)))
        return first, second

    first, second = asyncio.run(scenario())
    assert len(first.models) >= 3, "the route change and model switch must each have rebuilt the Model"
    assert len(second.models) == 1, "the restart must have restored a Model"
    for process in (first, second):
        assert _rows(process.chat) == [], _rows(process.chat)
        _assert_chip(process.chat, "not_configured", "neutral")


# --- Each state: no row, and the chip says which -------------------------------------------------------------------


def test_configured_but_not_started_reads_unavailable(monkeypatch):
    install_wire(monkeypatch, ProviderWire())
    attempts = _connects(monkeypatch, None)

    async def scenario():
        process = _process()
        await process.chat.chat(request(PROMPT, 1, _config(bloodhound=True)))
        return process

    process = asyncio.run(scenario())
    assert attempts, "the configured chat must have attempted a connect"
    assert _rows(process.chat) == []
    _assert_chip(process.chat, "unavailable", "warning")
    assert "Connection closed" in _chip(process.chat)["tooltip"]
    _no_secret(process.chat)


def test_a_started_server_whose_ce_refuses_reads_unavailable_on_every_turn(monkeypatch):
    """F1's shape: the failed reachability probe leaves the session registered, yet it never reads as usable."""
    install_wire(monkeypatch, ProviderWire())
    server = OfflineBloodHound("down")
    _connects(monkeypatch, server)

    async def scenario():
        process = _process()
        marks = []
        for request_id in (1, 2, 3):
            await process.chat.chat(request(PROMPT, request_id, _config(bloodhound=True)))
            marks.append(_chip(process.chat))
        return process, marks

    process, chips = asyncio.run(scenario())
    assert "BloodHound" in MCPManager.sessions, "precondition: F1 leaves the session registered"
    assert _rows(process.chat) == []
    assert [chip["value"] for chip in chips] == ["unavailable"] * 3
    assert all(chip["color"] == "warning" for chip in chips)
    assert "Nothing accepted a connection" in chips[-1]["tooltip"]
    _no_secret(process.chat)


def test_an_ingest_read_that_times_out_reads_unavailable_without_a_second_read(monkeypatch):
    install_wire(monkeypatch, ProviderWire())
    monkeypatch.setattr(bh, "INGEST_CALL_TIMEOUT_SECONDS", 0.05)
    server = OfflineBloodHound("hang")
    _register(server)

    async def scenario():
        process = _process()
        await process.chat.chat(request(PROMPT, 1, _config(bloodhound=True)))
        return process

    process = asyncio.run(scenario())
    assert _rows(process.chat) == []
    _assert_chip(process.chat, "unavailable", "warning")
    assert [name for name, _args in server.calls] == ["file_upload"], server.calls


def test_a_server_that_reads_but_cannot_report_ingest_reads_degraded(monkeypatch):
    install_wire(monkeypatch, ProviderWire())
    server = OfflineBloodHound("up", patched=False)
    _register(server)

    async def scenario():
        process = _process()
        await process.chat.chat(request(PROMPT, 1, _config(bloodhound=True)))
        return process

    process = asyncio.run(scenario())
    assert _rows(process.chat) == []
    _assert_chip(process.chat, "degraded", "warning")
    assert "Unknown info_type 'status'" in _chip(process.chat)["tooltip"]


def test_a_healthy_server_reads_connected(monkeypatch):
    install_wire(monkeypatch, ProviderWire())
    server = OfflineBloodHound("up")
    _register(server)

    async def scenario():
        process = _process()
        await process.chat.chat(request(PROMPT, 1, _config(bloodhound=True)))
        return process

    process = asyncio.run(scenario())
    assert _rows(process.chat) == []
    _assert_chip(process.chat, "connected", "success")


# --- Loss of a connection that worked ------------------------------------------------------------------------------


def test_a_lost_connection_is_noticed_once_per_channel_and_re_arms(monkeypatch):
    install_wire(monkeypatch, ProviderWire())
    server = OfflineBloodHound("up")
    _register(server)

    async def scenario():
        process = _process()
        chat = process.chat
        await chat.chat(request(PROMPT, 1, _config(bloodhound=True)))
        assert _rows(chat) == [], "connected says nothing"
        server.ce = "down"
        await chat.chat(request(PROMPT, 2, _config(bloodhound=True)))
        await chat.chat(request(PROMPT, 3, _config(bloodhound=True)))
        await chat.chat(request(PROMPT, 4, _config(bloodhound=True, SAGE_OPENAI_ACCESS_PROGRAM="standard")))
        after_loss = list(_rows(chat))
        # A channel that never saw it connected is told nothing.
        await chat.chat(request(PROMPT, 5, _config(bloodhound=True), channel=OTHER_CHANNEL))
        never_connected = [r for r in _rows(chat) if r["response_key"].endswith(f":{OTHER_CHANNEL}")]
        server.ce = "up"
        await chat.chat(request(PROMPT, 6, _config(bloodhound=True)))
        server.ce = "down"
        await chat.chat(request(PROMPT, 7, _config(bloodhound=True)))
        return process, after_loss, never_connected

    process, after_loss, never_connected = asyncio.run(scenario())
    assert [r["response_key"] for r in after_loss] == [f"bloodhound_lost:{CHANNEL}"], after_loss
    assert "no longer available" in after_loss[0]["content"]
    assert "BloodHound CE did not answer" in after_loss[0]["content"]
    assert never_connected == []
    assert [r["response_key"] for r in _rows(process.chat)] == [f"bloodhound_lost:{CHANNEL}"] * 2, "re-armed once"
    _no_secret(process.chat)


def test_each_channel_that_saw_it_connected_gets_its_own_loss_notice(monkeypatch):
    install_wire(monkeypatch, ProviderWire())
    server = OfflineBloodHound("up")
    _register(server)

    async def scenario():
        process = _process()
        chat = process.chat
        await chat.chat(request(PROMPT, 1, _config(bloodhound=True)))
        await chat.chat(request(PROMPT, 2, _config(bloodhound=True), channel=OTHER_CHANNEL))
        server.ce = "down"
        await chat.chat(request(PROMPT, 3, _config(bloodhound=True)))
        await chat.chat(request(PROMPT, 4, _config(bloodhound=True), channel=OTHER_CHANNEL))
        return process

    process = asyncio.run(scenario())
    assert sorted(r["response_key"] for r in _rows(process.chat)) == sorted(
        [f"bloodhound_lost:{CHANNEL}", f"bloodhound_lost:{OTHER_CHANNEL}"]
    )


def test_a_connected_server_that_loses_ingest_is_a_loss_and_says_reads_still_work(monkeypatch):
    install_wire(monkeypatch, ProviderWire())
    server = OfflineBloodHound("up")
    _register(server)

    async def scenario():
        process = _process()
        await process.chat.chat(request(PROMPT, 1, _config(bloodhound=True)))
        server.patched = False
        await process.chat.chat(request(PROMPT, 2, _config(bloodhound=True)))
        return process

    process = asyncio.run(scenario())
    [row] = _rows(process.chat)
    assert "Graph reads still work" in row["content"]
    _assert_chip(process.chat, "degraded", "warning")


# --- The guard, through the real BloodHound node ------------------------------------------------------------------

ROUTE = [call_item("transfer_to_BloodHound", {
    "handoff_title": "List domains", "handoff_instruction": "List the domains in the BloodHound graph."}, "route-1")]


def _bloodhound_agent_ran(wire: ProviderWire) -> bool:
    """True when a provider request carried the BloodHound agent's MCP tools, i.e. the agent's model ran."""
    for body in wire.responses():
        names = {str(t.get("name") or "") for t in body.get("tools") or []}
        if "domain_info" in names:
            return True
    return False


def _graph_turn(monkeypatch, config: dict, *, setup=None):
    wire = ProviderWire([ROUTE, [text_item("Here is what BloodHound reported.", 2)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        if setup is not None:
            setup(process)
        await process.chat.chat(request(GRAPH_PROMPT, 1, config))
        return process

    process = asyncio.run(scenario())
    return process, wire, "\n".join(texts(process.chat))


def test_the_guard_explains_an_unconfigured_bloodhound(monkeypatch):
    process, wire, joined = _graph_turn(monkeypatch, _config())
    assert "BloodHound is not configured for this chat" in joined, joined
    assert "did not read any graph data" in joined
    assert "BLOODHOUND_URL" in joined and "/bloodhound" in joined
    assert "mcp-connect" not in joined
    assert not _bloodhound_agent_ran(wire)


def test_the_guard_explains_a_configured_bloodhound_that_did_not_connect(monkeypatch):
    _connects(monkeypatch, None)
    process, wire, joined = _graph_turn(monkeypatch, _config(bloodhound=True))
    assert "BloodHound is configured but not connected" in joined, joined
    assert "did not read any graph data" in joined
    assert "Connection closed" in joined
    assert "mcp-connect" not in joined
    assert not _bloodhound_agent_ran(wire)
    _no_secret(process.chat)


def test_a_configured_connect_failure_is_named_on_every_turn(monkeypatch):
    """Live attempt 1 (2026-11-21): turn 1 named the failed start; turns 2 and 3 hit the failed-credential skip, whose
    outcome named no failure, so the chip and the guard stopped saying what failed. ``McpError: Connection closed`` is
    the recorded failed-start shape (Sage container log, 15:41 UTC). Only the subprocess start is stubbed."""
    wire = ProviderWire()
    install_wire(monkeypatch, wire)
    attempts = _connects(monkeypatch, None, error="McpError: Connection closed")

    async def scenario():
        process = _process()
        chips = []
        for request_id in (1, 2):
            await process.chat.chat(request(PROMPT, request_id, _config(bloodhound=True)))
            chips.append(_chip(process.chat))
        wire.script[:] = [ROUTE, [text_item("Here is what BloodHound reported.", 2)]]
        mark = len(process.chat.emissions)
        await process.chat.chat(request(GRAPH_PROMPT, 3, _config(bloodhound=True)))
        chips.append(_chip(process.chat))
        return process, chips, "\n".join(texts(process.chat)[mark:])

    process, chips, joined = asyncio.run(scenario())
    assert len(attempts) == 1, "an unchanged failed credential set must still not be retried"
    assert _rows(process.chat) == []
    assert [chip["value"] for chip in chips] == ["unavailable"] * 3
    for turn, chip in enumerate(chips, 1):
        assert "Failed to connect to BloodHound MCP" in chip["tooltip"], (turn, chip["tooltip"])
    assert "BloodHound is configured but not connected" in joined, joined
    assert "did not read any graph data" in joined
    assert "Failed to connect to BloodHound MCP" in joined, joined
    assert "BloodHound is not configured" not in joined, "must not fall back to the setup text"
    assert not _bloodhound_agent_ran(wire)
    _no_secret(process.chat)


def test_the_guard_fires_on_a_live_session_whose_ce_refuses(monkeypatch):
    server = OfflineBloodHound("down")
    _connects(monkeypatch, server)
    process, wire, joined = _graph_turn(monkeypatch, _config(bloodhound=True))
    assert "BloodHound is connected, but BloodHound CE did not answer a read" in joined, joined
    assert "Nothing accepted a connection" in joined
    assert "BloodHound is not configured" not in joined, "must not fall back to the setup text"
    assert "mcp-connect" not in joined
    assert not _bloodhound_agent_ran(wire)
    assert all(name != "cypher_query" for name, _args in server.calls), "no graph query was made"
    _no_secret(process.chat)


@pytest.mark.parametrize("patched", [True, False], ids=["connected", "degraded"])
def test_the_guard_lets_a_reading_server_through(monkeypatch, patched):
    _register(OfflineBloodHound("up", patched=patched))
    process, wire, joined = _graph_turn(monkeypatch, _config(bloodhound=True))
    assert "did not read any graph data" not in joined, joined
    assert _bloodhound_agent_ran(wire), "the BloodHound agent's model must run with its tools"


def test_without_a_turn_state_the_guard_keeps_the_pinned_session_rule(monkeypatch):
    """A Model whose turn computed no state (observation failed or never ran) keeps today's rule."""
    async def no_state(self, model, request):
        model._bloodhound_turn_state = None

    monkeypatch.setattr(service.SageChat, "_observe_bloodhound_turn", no_state)
    process, wire, joined = _graph_turn(monkeypatch, _config())
    assert "BloodHound is not connected, so I did not read any graph data" in joined, joined
    assert "mcp-connect" not in joined
    assert not _bloodhound_agent_ran(wire)
    item = _chip(process.chat)
    assert (item["value"], item["display_value"], item["color"]) == (False, "off", "neutral"), item
