"""Channel-scoped ACP coding-agent connection controls (`/acp`) without any model call.

The recorded shapes come from the real Codex ACP adapter 2.9.0 (Codex 0.171.0) over a Unix socket between two
network-disabled containers, with a public non-secret sentinel as its API-key input and no prompt sent
(`tests/fixtures/acp/codex_acp_2_9_0.json`). A scripted agent replays them on a real Unix socket, so each case
runs through Sage's real transport, the SDK `Connection`, Sage's decoder and the native slash dispatcher.
The scripted agent proves Sage's handling of those shapes; it does not prove what the adapter would choose.
Shapes marked "derived" are recorded responses with one named field changed.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

import pytest
from mythic_container.ChatBase import ChatSlashCommandInvocation

import ai.acp as acp_module
import sage_chat.service as service
import sage_chat.session as chat_session
from sage_chat.headless import HeadlessSageChat, build_chat_request

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "acp" / "codex_acp_2_9_0.json").read_text())
RECORDED = FIXTURE["sentinel_session"]
RECORDED_DEFAULT = "gpt-6.2-sol"
RECORDED_OTHER = "gpt-6.2-astra"


class _NoModelChat(HeadlessSageChat):
    async def _get_or_create_model(self, request):
        raise AssertionError("/acp constructed a Sage model")

    async def _restore_channel_session(self, request):
        raise AssertionError("/acp restored a Sage model")


def _request(argument="", *, name="acp", operation_id=7, channel_id=24, request_id=23, model="Sage",
             config=None):
    request = build_chat_request("", operation_id=operation_id, channel_id=channel_id, request_id=request_id,
                                 model=model, config=config)
    request.SlashCommand = ChatSlashCommandInvocation(name=name, argument=argument)
    return request


@pytest.fixture
def runtime_db(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.delenv("SAGE_ACP_SOCKET", raising=False)
    monkeypatch.setattr(acp_module, "ACP_MANAGER", acp_module.AcpConnectionManager())
    return tmp_path / "sage.db"


@pytest.fixture
def socket_dir():
    # AF_UNIX paths are limited to ~108 bytes; pytest's tmp_path can exceed that.
    path = Path(tempfile.mkdtemp(prefix="acp-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def _send(chat, argument="", **kwargs):
    before = len(chat.terminal_emissions)
    await chat.chat(_request(argument, **kwargs))
    emitted = chat.terminal_emissions[before:]
    assert len(emitted) == 1, emitted
    assert emitted[0]["complete_request"] is True
    return emitted[0]["content"]


class ScriptedAgent:
    """Replays recorded adapter responses on a Unix socket and records every message it receives."""

    def __init__(self, path: Path, **behaviour):
        self.path = path
        self.behaviour = behaviour
        self.connections = 0
        self.received: list[tuple[int, dict]] = []
        self.client_replies: list[dict] = []
        self.closed: list[float] = []
        self._writers: list[asyncio.StreamWriter] = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path), limit=4 * 1024 * 1024)
        return self

    async def stop(self):
        for writer in self._writers:
            writer.close()
        self.server.close()
        await self.server.wait_closed()

    def methods(self, conn=None):
        return [m.get("method") for c, m in self.received if "method" in m and (conn is None or c == conn)]

    async def drop_all(self):
        for writer in self._writers:
            writer.close()

    async def _serve(self, reader, writer):
        self.connections += 1
        conn = self.connections
        self._writers.append(writer)
        session_id = f"session-{conn}"
        options = copy.deepcopy(RECORDED["session_new"]["configOptions"])

        async def send(message):
            writer.write(json.dumps(message).encode() + b"\n")
            await writer.drain()

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                message = json.loads(line)
                self.received.append((conn, message))
                if "method" not in message:
                    self.client_replies.append(message)
                    continue
                method, rid, params = message["method"], message.get("id"), message.get("params") or {}
                hook = self.behaviour.get(method)
                if hook == "hang":
                    continue
                if isinstance(hook, dict) and "error" in hook:
                    await send({"jsonrpc": "2.0", "id": rid, "error": hook["error"]})
                    continue
                if method == "initialize":
                    result = copy.deepcopy(RECORDED["initialize"])
                    result.update(self.behaviour.get("initialize_override", {}))
                    if self.behaviour.get("no_auth_status"):
                        result["agentCapabilities"]["auth"].pop("status")
                    if self.behaviour.get("ask_permission"):
                        await send({"jsonrpc": "2.0", "id": "agent-1", "method": "session/request_permission",
                                    "params": {"sessionId": "x", "toolCall": {"toolCallId": "t"}, "options": []}})
                        await send({"jsonrpc": "2.0", "id": "agent-2", "method": "fs/read_text_file",
                                    "params": {"sessionId": "x", "path": "/etc/hostname"}})
                    if self.behaviour.get("oversize"):
                        writer.write(b'{"pad":"' + b"x" * (1024 * 1024 + 10) + b'"}\n')
                        await writer.drain()
                        continue
                elif method == "authenticate":
                    result = RECORDED["authenticate"]
                elif method == "auth/status":
                    result = self.behaviour.get("auth_status", RECORDED["auth_status"])
                elif method == "session/new":
                    result = copy.deepcopy(RECORDED["session_new"])
                    result["sessionId"] = session_id
                    result["configOptions"] = options
                elif method == "session/set_mode":
                    result = {}
                elif method == "session/set_config_option":
                    target = self.behaviour.get("report_model", params["value"])
                    for option in options:
                        if option["id"] == params["configId"]:
                            option["currentValue"] = target
                    await send({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": session_id,
                        "update": {"sessionUpdate": "config_option_update", "configOptions": options}}})
                    result = {"configOptions": copy.deepcopy(options)}
                    if self.behaviour.get("foreign_update"):
                        # Derived: the recorded update, addressed to another session, after the response.
                        await send({"jsonrpc": "2.0", "id": rid, "result": result})
                        foreign = copy.deepcopy(options)
                        foreign[0]["currentValue"] = "gpt-5.5"
                        await send({"jsonrpc": "2.0", "method": "session/update", "params": {
                            "sessionId": "another-session",
                            "update": {"sessionUpdate": "config_option_update", "configOptions": foreign}}})
                        continue
                elif method == "session/close":
                    result = {}
                else:
                    await send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})
                    continue
                await send({"jsonrpc": "2.0", "id": rid, "result": result})
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.closed.append(time.monotonic())
            writer.close()


def _run(coro):
    return asyncio.run(coro)


def test_acp_status_is_a_native_slash_command_that_builds_no_model(runtime_db):
    async def scenario():
        chat = _NoModelChat()
        return await _send(chat, "status")

    text = _run(scenario())
    assert "Unknown Sage slash command" not in text
    assert "**Coding agent connection**" in text
    assert "unconfigured" in text
    assert "SAGE_ACP_SOCKET" in text
    assert "Provider access" in text and "unverified" in text


def test_recorded_session_connects_selects_a_model_and_persists_only_the_selection(
        runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        connected = await _send(chat, "connect codex")
        models = await _send(chat, "models")
        changed = await _send(chat, f"model {RECORDED_OTHER}")
        status = await _send(chat, "")
        binding = await chat_session.load_acp_binding(7, 24)
        await agent.stop()
        return agent, connected, models, changed, status, binding

    agent, connected, models, changed, status, binding = _run(scenario())
    assert "| Transport | `connected` |" in connected
    assert "| Protocol | ACP v1 |" in connected
    assert "@agentclientprotocol/codex-acp 2.9.0" in connected
    assert "| Authentication (observed) | `authenticated` |" in connected
    assert "does not prove the provider accepts it" in connected
    assert f"| Effective model | `{RECORDED_DEFAULT}` |" in connected
    assert "| `gpt-6.2-astra` | 6.2 Astra |" in models and models.count("| `") == 11
    assert f"| `{RECORDED_DEFAULT}` | 6.2 Sol | yes |" in models
    assert f"| Effective model | `{RECORDED_OTHER}` |" in changed
    assert f"| Saved model | `{RECORDED_OTHER}` |" in status
    assert binding == {"profile": "codex", "model": RECORDED_OTHER}

    sent = [m for _c, m in agent.received if "method" in m]
    init = next(m for m in sent if m["method"] == "initialize")["params"]
    assert init["protocolVersion"] == 1
    assert init["clientCapabilities"]["fs"] == {"readTextFile": False, "writeTextFile": False}
    assert init["clientCapabilities"]["terminal"] is False
    new = next(m for m in sent if m["method"] == "session/new")["params"]
    assert new == {"cwd": "/workspace", "mcpServers": []}
    mode = next(m for m in sent if m["method"] == "session/set_mode")["params"]
    assert mode == {"sessionId": "session-1", "modeId": "read-only"}
    assert agent.methods() == ["initialize", "authenticate", "auth/status", "session/new", "session/set_mode",
                               "session/set_config_option"]
    assert agent.connections == 1


def test_two_channels_hold_independent_sessions_and_disconnect_one(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        await _send(chat, "connect codex", channel_id=1)
        await _send(chat, f"model {RECORDED_OTHER}", channel_id=1)
        b = await _send(chat, "connect codex", channel_id=2, config={"mode": "supervised"})
        a_after_b = await _send(chat, "status", channel_id=1)
        bye = await _send(chat, "disconnect", channel_id=1)
        await asyncio.sleep(0.05)
        b_after = await _send(chat, "status", channel_id=2, config={"mode": "supervised"})
        a_after = await _send(chat, "status", channel_id=1)
        rows = (await chat_session.load_acp_binding(7, 1), await chat_session.load_acp_binding(7, 2))
        closed_while_running = len(agent.closed)
        await agent.stop()
        return agent, b, a_after_b, bye, b_after, a_after, rows, closed_while_running

    agent, b, a_after_b, bye, b_after, a_after, rows, closed_while_running = _run(scenario())
    assert agent.connections == 2
    assert f"| Effective model | `{RECORDED_DEFAULT}` |" in b
    assert f"| Effective model | `{RECORDED_OTHER}` |" in a_after_b
    assert "Disconnected" in bye and "cleared" in bye
    assert "session/close" in agent.methods(conn=1) and "session/close" not in agent.methods(conn=2)
    assert closed_while_running == 1
    assert "| Transport | `connected` |" in b_after and f"`{RECORDED_DEFAULT}`" in b_after
    assert "| Transport | `disconnected` |" in a_after and "| Saved model | agent default |" in a_after
    assert rows == (None, {"profile": "codex", "model": ""})


@pytest.mark.parametrize("value,hint", [
    ("gpt-9-unknown", ""),
    ("6.2 Astra", "use `gpt-6.2-astra`"),       # display label, not the ID
    ("gpt-6.2-astra[low]", ""),                 # legacy `models.availableModels` id, not a config value
    ("GPT-6.2-ASTRA", ""),                      # IDs compare exactly
])
def test_unadvertised_model_is_rejected_before_any_setting_is_sent(
        runtime_db, socket_dir, monkeypatch, value, hint):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        await _send(chat, "connect codex")
        text = await _send(chat, f"model {value}") if " " not in value else None
        if text is None:  # a display label with a space reaches the usage reply, also without a setting
            text = await _send(chat, f"model {value}")
        status = await _send(chat, "status")
        binding = await chat_session.load_acp_binding(7, 24)
        await agent.stop()
        return agent, text, status, binding

    agent, text, status, binding = _run(scenario())
    assert "session/set_config_option" not in agent.methods()
    assert f"| Effective model | `{RECORDED_DEFAULT}` |" in status
    assert binding == {"profile": "codex", "model": ""}
    if " " in value:
        assert text.startswith("Usage:")
    else:
        assert "is not a model this agent advertises" in text
        assert hint in text


def test_display_label_without_spaces_points_at_the_exact_id(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        await _send(chat, "connect codex")
        text = await _send(chat, "model 5.5")   # recorded label of value `gpt-5.5`
        await agent.stop()
        return agent, text

    agent, text = _run(scenario())
    assert "`5.5` is a display name; use `gpt-5.5`" in text
    assert "session/set_config_option" not in agent.methods()


@pytest.mark.parametrize("behaviour,expected", [
    ({"session/set_config_option": {"error": {"code": -32602, "message": "model not allowed"}}},
     "refused the model change: model not allowed"),
    ({"report_model": "gpt-5.5"}, "reports `gpt-5.5` after the change"),   # derived: currentValue changed
])
def test_failed_model_update_keeps_the_saved_selection(runtime_db, socket_dir, monkeypatch, behaviour, expected):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock, **behaviour).start()
        chat = _NoModelChat()
        await chat_session.save_acp_binding(7, 24, profile="codex", model="")
        await _send(chat, "connect codex")
        text = await _send(chat, f"model {RECORDED_OTHER}")
        binding = await chat_session.load_acp_binding(7, 24)
        await agent.stop()
        return text, binding

    text, binding = _run(scenario())
    assert expected in text
    assert binding == {"profile": "codex", "model": ""}


def test_notification_for_another_session_does_not_change_the_effective_model(
        runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock, foreign_update=True).start()
        chat = _NoModelChat()
        await _send(chat, "connect codex")
        await _send(chat, f"model {RECORDED_OTHER}")
        await asyncio.sleep(0.05)
        status = await _send(chat, "status")
        await agent.stop()
        return status

    assert f"| Effective model | `{RECORDED_OTHER}` |" in _run(scenario())


def test_unknown_profile_and_missing_configuration_open_nothing(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        unknown = await _send(chat, "connect claude")
        unconfigured = await _send(chat, "connect codex")
        await agent.stop()
        return agent, unknown, unconfigured

    agent, unknown, unconfigured = _run(scenario())
    assert "Unknown coding-agent profile `claude`" in unknown and "Nothing was started" in unknown
    assert "set SAGE_ACP_SOCKET" in unconfigured and "| Transport | `unconfigured` |" in unconfigured
    assert agent.connections == 0


def test_socket_path_over_the_unix_limit_is_a_configuration_error(runtime_db, monkeypatch):
    monkeypatch.setenv("SAGE_ACP_SOCKET", "/" + "s" * 107)
    text = _run(_send(_NoModelChat(), "connect codex"))
    assert "longer than the 107-byte Unix socket path limit" in text


def test_absent_worker_socket_is_a_bounded_failure(runtime_db, socket_dir, monkeypatch):
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(socket_dir / "absent.sock"))

    async def scenario():
        chat = _NoModelChat()
        started = time.monotonic()
        text = await _send(chat, "connect codex")
        return text, time.monotonic() - started

    text, elapsed = _run(scenario())
    assert "| Transport | `failed` |" in text
    assert "worker is unavailable (FileNotFoundError: No such file or directory)" in text
    assert elapsed < 5


def test_missing_key_reports_no_usable_credential(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(
            sock,
            authenticate={"error": {"code": -32000, "message": "Authentication required",
                                    "data": {"message": "CODEX_API_KEY is not set"}}},
            auth_status=FIXTURE["no_key_session"]["auth_status"],
            # Real adapter answer without a key (observed against the built worker, 2026-11-21).
            **{"session/new": {"error": {"code": -32000, "message": "Authentication required"}}},
        ).start()
        chat = _NoModelChat()
        text = await _send(chat, "connect codex")
        await agent.stop()
        return text

    text = _run(scenario())
    assert "| Authentication (observed) | `unknown` |" in text
    assert "No usable API-key configuration in the worker" in text
    assert "CODEX_API_KEY is not set" in text
    assert "`authenticated`" not in text
    assert "unverified" in text
    assert "| Effective model | none (no session) |" in text and "No session: Authentication required" in text


def test_absent_auth_status_capability_stays_unknown(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock, no_auth_status=True).start()
        text = await _send(_NoModelChat(), "connect codex")
        await agent.stop()
        return agent, text

    agent, text = _run(scenario())
    assert "auth/status" not in agent.methods()
    assert "| Authentication (observed) | `unknown` |" in text


def test_wrong_protocol_version_is_refused_and_closed(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock, initialize_override={"protocolVersion": 2}).start()
        text = await _send(_NoModelChat(), "connect codex")
        await asyncio.sleep(0.05)
        await agent.stop()
        return agent, text

    agent, text = _run(scenario())
    assert "offered ACP protocol 2; Sage supports only 1" in text
    assert agent.methods() == ["initialize"] and len(agent.closed) == 1


def test_agent_permission_and_filesystem_requests_are_refused(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock, ask_permission=True).start()
        text = await _send(_NoModelChat(), "connect codex")
        await asyncio.sleep(0.05)
        await agent.stop()
        return agent, text

    agent, text = _run(scenario())
    assert "| Transport | `connected` |" in text
    replies = {r["id"]: r for r in agent.client_replies}
    assert replies["agent-1"]["result"] == {"outcome": {"outcome": "cancelled"}}
    assert replies["agent-2"]["error"]["code"] == -32601
    assert "session/prompt" not in agent.methods()


def test_worker_eof_shows_disconnected_and_explicit_connect_reapplies_the_saved_model(
        runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        await _send(chat, "connect codex")
        await _send(chat, f"model {RECORDED_OTHER}")
        await agent.drop_all()
        await asyncio.sleep(0.1)
        lost = await _send(chat, "status")
        connections_before = agent.connections
        again = await _send(chat, "connect codex")
        await agent.stop()
        return agent, lost, connections_before, again

    agent, lost, connections_before, again = _run(scenario())
    assert "| Transport | `disconnected` |" in lost and "Connection lost" in lost
    assert f"| Saved model | `{RECORDED_OTHER}` |" in lost
    assert connections_before == 1        # nothing reconnected on its own
    assert agent.connections == 2
    assert f"Reapplied saved model `{RECORDED_OTHER}`" in again
    assert f"| Effective model | `{RECORDED_OTHER}` |" in again


def test_process_restart_keeps_the_selection_but_not_the_connection(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def first_process():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        await _send(chat, "connect codex")
        await _send(chat, f"model {RECORDED_OTHER}")
        await acp_module.ACP_MANAGER.close_all()
        await agent.stop()

    async def second_process():
        monkeypatch.setattr(acp_module, "ACP_MANAGER", acp_module.AcpConnectionManager())
        monkeypatch.setattr(service, "claim_channel_marker", _seen_before)
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        status = await _send(chat, "status")
        again = await _send(chat, "connect codex")
        await agent.stop()
        return status, again

    _run(first_process())
    status, again = _run(second_process())
    assert "| Transport | `disconnected` |" in status
    assert f"| Saved model | `{RECORDED_OTHER}` |" in status and "(saved for this channel)" in status
    assert f"Reapplied saved model `{RECORDED_OTHER}`" in again


async def _seen_before(_request):
    return False


def test_acp_on_a_channel_seen_before_never_restores_a_model(runtime_db, monkeypatch):
    monkeypatch.setattr(service, "claim_channel_marker", _seen_before)
    text = _run(_send(_NoModelChat(), "status"))
    assert "**Coding agent connection**" in text


def test_connect_steps_share_the_connect_deadline(runtime_db, socket_dir, monkeypatch):
    # The real adapter answers `session/new` after ~15 s with networking disabled (its model refresh retries),
    # so connect steps are bounded by the connect deadline, not the management-request deadline.
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))
    monkeypatch.setattr(acp_module, "CONNECT_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(acp_module, "REQUEST_DEADLINE_SECONDS", 0.1)

    async def scenario():
        agent = await ScriptedAgent(sock, **{"session/new": "hang"}).start()
        started = time.monotonic()
        text = await _send(_NoModelChat(), "connect codex")
        elapsed = time.monotonic() - started
        await asyncio.sleep(0.05)
        await agent.stop()
        return agent, text, elapsed

    agent, text, elapsed = _run(scenario())
    assert "Connecting took longer than 0.5 seconds" in text
    assert "| Transport | `failed` |" in text
    assert 0.45 < elapsed < 3 and len(agent.closed) == 1


def test_management_request_timeout_keeps_the_selection(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))
    monkeypatch.setattr(acp_module, "REQUEST_DEADLINE_SECONDS", 0.3)

    async def scenario():
        agent = await ScriptedAgent(sock, **{"session/set_config_option": "hang"}).start()
        chat = _NoModelChat()
        await _send(chat, "connect codex")
        text = await _send(chat, f"model {RECORDED_OTHER}")
        binding = await chat_session.load_acp_binding(7, 24)
        await agent.stop()
        return text, binding

    text, binding = _run(scenario())
    assert "did not answer `session/set_config_option` within 0.3 seconds" in text
    assert binding == {"profile": "codex", "model": ""}


def test_oversized_agent_message_fails_the_connection(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock, oversize=True).start()
        text = await _send(_NoModelChat(), "connect codex")
        await agent.stop()
        return text

    text = _run(scenario())
    assert "| Transport | `failed` |" in text
    assert "message over 1048576 bytes" in text


def test_stop_closes_the_coding_agent_connection_without_a_sage_session(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        await _send(chat, "connect codex")
        await _send(chat, f"model {RECORDED_OTHER}")
        started = time.monotonic()
        stopped = await _send(chat, "", name="stop")
        for _ in range(50):
            if agent.closed:
                break
            await asyncio.sleep(0.02)
        closed_after = agent.closed[0] - started if agent.closed else None
        status = await _send(chat, "status")
        binding = await chat_session.load_acp_binding(7, 24)
        await agent.stop()
        return stopped, closed_after, status, binding

    stopped, closed_after, status, binding = _run(scenario())
    assert stopped.startswith("No running session to stop on this channel.")
    assert "Closed this channel's coding-agent connection; its saved selection remains." in stopped
    assert closed_after is not None and closed_after < 5
    assert "| Transport | `disconnected` |" in status
    assert binding == {"profile": "codex", "model": RECORDED_OTHER}


def test_stop_without_a_coding_agent_keeps_its_existing_wording(runtime_db):
    text = _run(_send(_NoModelChat(), "", name="stop"))
    assert text == "No running session to stop on this channel."


def test_watcher_neither_advertises_nor_accepts_acp(runtime_db):
    from sage_chat.slash import SAGE_SLASH_COMMANDS, WATCHER_SLASH_COMMANDS

    assert "acp" in {c.Name for c in SAGE_SLASH_COMMANDS}
    assert "acp" not in {c.Name for c in WATCHER_SLASH_COMMANDS}
    text = _run(_send(_NoModelChat(), "status", model="Sage Watcher"))
    assert "`/acp` is not available to Sage Watcher" in text


def test_connection_ceiling_refuses_a_fifth_channel(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        for channel in range(1, 5):
            await _send(chat, "connect codex", channel_id=channel)
        fifth = await _send(chat, "connect codex", channel_id=5)
        await acp_module.ACP_MANAGER.close_all()
        await agent.stop()
        return agent, fifth

    agent, fifth = _run(scenario())
    assert agent.connections == 4
    assert "already holds 4 coding-agent connections" in fifth


def test_repeated_connect_is_idempotent(runtime_db, socket_dir, monkeypatch):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        chat = _NoModelChat()
        await _send(chat, "connect codex")
        again = await _send(chat, "connect codex")
        await agent.stop()
        return agent, again

    agent, again = _run(scenario())
    assert agent.connections == 1 and "Already connected." in again


@pytest.mark.parametrize("argument", ["connect", "connect codex extra", "model", "frobnicate",
                                      'connect {"command":"sh"}', "disconnect now", "status --json"])
def test_other_arguments_return_usage_and_open_nothing(runtime_db, socket_dir, monkeypatch, argument):
    sock = socket_dir / "acp.sock"
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(sock))

    async def scenario():
        agent = await ScriptedAgent(sock).start()
        text = await _send(_NoModelChat(), argument)
        await agent.stop()
        return agent, text

    agent, text = _run(scenario())
    assert agent.connections == 0
    assert text.startswith("Usage: `/acp`") or "Unknown coding-agent profile" in text


def test_grouped_select_options_flatten_to_exact_values():
    # Synthetic grouped shape (ACP SessionConfigSelectGroup); the recorded adapter uses flat options.
    option = {"id": "model", "category": "model", "type": "select", "currentValue": "b",
              "options": [{"group": "fast", "name": "Fast", "options": [{"value": "a", "name": "A"}]},
                          {"value": "b", "name": "B"}]}
    assert acp_module.model_option([{"id": "x", "category": "mode", "type": "select"}, option]) is option
    assert acp_module._flatten_select(option) == [
        {"value": "a", "name": "A", "group": "Fast"}, {"value": "b", "name": "B", "group": ""}]
    recorded = acp_module.model_option(RECORDED["session_new"]["configOptions"])
    assert len(acp_module._flatten_select(recorded)) == 11
