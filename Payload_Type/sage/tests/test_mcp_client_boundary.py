"""Sage's MCP client against real MCP servers, over every transport Sage configures.

No stand-in replaces the client here: each test starts `fixtures/mcp_servers/probe_server.py` as a real server
process (stdio) or a real TLS HTTP server (SSE, Streamable HTTP) and drives it through `MCPServerManager`, the
native slash commands, and the BloodHound readiness reads. The server logs `<tool> <pid>` for every call and
`start <pid>` when a process starts, so a test can prove a denied call reached no server and started none.
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from langchain_core.messages import ToolCall

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai import bloodhound_config as bh  # noqa: E402
from ai import mcp as mcpmod  # noqa: E402
from ai.mcp import MCPServerManager  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402

SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_servers" / "probe_server.py"
PROBE_TOOLS = {"read_status", "wipe_graph", "echo_context", "slow"}


def _log_lines(log: Path) -> list[tuple[str, int]]:
    if not log.exists():
        return []
    return [(name, int(pid)) for name, pid in (line.split() for line in log.read_text().splitlines())]


def _calls(log: Path) -> list[tuple[str, int]]:
    """Tool calls the server answered, as (tool, server pid)."""
    return [entry for entry in _log_lines(log) if entry[0] != "start"]


def _starts(log: Path) -> list[int]:
    """Server processes that started."""
    return [pid for name, pid in _log_lines(log) if name == "start"]


def _process_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z":
                return True
        except OSError:
            return True
        time.sleep(0.1)
    return False


def _stdio_config(name: str, log: Path, **overrides):
    config = mcpmod.create_stdio_config(
        name=name,
        command=sys.executable,
        args=[str(SERVER), "stdio"],
        env={"PROBE_LOG": str(log), "PROBE_MARKER": "from-config"},
        cwd=str(SERVER.parent),
        encoding=overrides.pop("encoding", None),
        encoding_error_handler=None,
        session_kwargs=overrides.pop("session_kwargs", None),
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
    )
    assert not overrides
    return config


# --- stdio --------------------------------------------------------------------------------------------------


def test_stdio_server_is_discovered_called_and_ended(tmp_path):
    log = tmp_path / "calls.log"

    async def scenario():
        manager = MCPServerManager()
        assert await manager.connect_server(_stdio_config("Probe", log)) == (True, None)
        tools = {tool.name: tool for tool in manager.get_tools_by_server("Probe")}
        status = await tools["read_status"].ainvoke({"job_id": 0})
        message = await tools["echo_context"].ainvoke(
            ToolCall(name="echo_context", args={"name": "PROBE_MARKER"}, id="call-7", type="tool_call")
        )
        path_message = await tools["echo_context"].ainvoke({"name": "PATH"})
        assert await manager.disconnect_server("Probe") is True
        return set(tools), status, message, path_message, manager

    names, status, message, path_message, manager = asyncio.run(scenario())

    assert names == PROBE_TOOLS
    # A plain read returns the content blocks the ingest decoder accepts.
    assert bh.decode_ingest_response(status, info_type="status", expected_job_id=0) == {
        "job_id": 0,
        "status_message": "not_found",
    }
    # A tool call keeps its id, status and structured result; the config's env reached the process.
    assert message.tool_call_id == "call-7"
    assert message.status == "success"
    structured = message.artifact["structured_content"]
    assert structured["env"] == "from-config" and structured["header"] is None
    # The SDK's safe default environment is still merged under the configured env.
    assert json.loads(path_message[0]["text"])["env"]
    assert manager.get_connected_servers() == []
    assert _process_gone(structured["pid"])


def test_a_tool_held_after_disconnect_is_denied_before_any_server_io(tmp_path):
    log = tmp_path / "calls.log"

    async def scenario():
        manager = MCPServerManager()
        assert await manager.connect_server(_stdio_config("Probe", log)) == (True, None)
        held = next(tool for tool in manager.get_tools_by_server("Probe") if tool.name == "read_status")
        await held.ainvoke({"job_id": 0})
        assert await manager.disconnect_server("Probe") is True
        before = _log_lines(log)
        with pytest.raises(PermissionError, match="source connection is no longer active"):
            await held.ainvoke({"job_id": 0})
        return before

    before = asyncio.run(scenario())
    # The converted tool would reconnect on its own; the denial happened before it could start a process.
    assert _log_lines(log) == before
    assert len(_starts(log)) == 1


def test_a_tool_held_across_a_rebind_is_denied_and_the_new_tool_answers(tmp_path):
    log = tmp_path / "calls.log"

    async def scenario():
        manager = MCPServerManager()
        assert await manager.connect_server(_stdio_config("Probe", log)) == (True, None)
        old = next(tool for tool in manager.get_tools_by_server("Probe") if tool.name == "read_status")
        await old.ainvoke({"job_id": 0})
        assert await manager.connect_server(_stdio_config("Probe", log)) == (True, None)
        new = next(tool for tool in manager.get_tools_by_server("Probe") if tool.name == "read_status")
        with pytest.raises(PermissionError, match="source connection is no longer active"):
            await old.ainvoke({"job_id": 0})
        await new.ainvoke({"job_id": 0})
        await manager.close_all_connections()

    asyncio.run(scenario())
    calls = _calls(log)
    assert [name for name, _ in calls] == ["read_status", "read_status"]
    first_pid, second_pid = (pid for _, pid in calls)
    assert first_pid != second_pid
    assert _process_gone(first_pid) and _process_gone(second_pid)


def test_a_server_that_dies_is_dropped_so_the_next_turn_reconnects(tmp_path):
    log = tmp_path / "calls.log"

    async def scenario():
        manager = MCPServerManager()
        assert await manager.connect_server(_stdio_config("Probe", log)) == (True, None)
        tool = next(tool for tool in manager.get_tools_by_server("Probe") if tool.name == "read_status")
        await tool.ainvoke({"job_id": 0})
        os.kill(_calls(log)[-1][1], signal.SIGKILL)
        await asyncio.sleep(0.5)
        with pytest.raises(MCPError, match="Connection closed"):
            await tool.ainvoke({"job_id": 0})
        return manager

    manager = asyncio.run(scenario())
    assert manager.get_connected_servers() == []
    assert manager.get_tools_by_server("Probe") == []
    assert "Probe" not in manager.configs


def test_session_read_timeout_bounds_a_slow_call(tmp_path):
    log = tmp_path / "calls.log"

    async def scenario():
        manager = MCPServerManager()
        config = _stdio_config("Probe", log, session_kwargs={"read_timeout_seconds": 1})
        assert await manager.connect_server(config) == (True, None)
        tool = next(tool for tool in manager.get_tools_by_server("Probe") if tool.name == "slow")
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            await tool.ainvoke({"seconds": 10})
        elapsed = time.monotonic() - started
        await manager.close_all_connections()
        return elapsed, caught.value

    elapsed, error = asyncio.run(scenario())
    assert elapsed < 5, elapsed
    assert "timed out" in str(error).lower() or "timeout" in type(error).__name__.lower()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"session_kwargs": {"sampling_callback": "x"}}, "unsupported session_kwargs"),
        ({"session_kwargs": {"read_timeout_seconds": "soon"}}, "must be a positive number"),
        ({"encoding": "latin-1"}, "UTF-8 only"),
    ],
)
def test_unsupported_stdio_options_are_refused_before_a_process_starts(tmp_path, overrides, message):
    log = tmp_path / "calls.log"
    ok, error = asyncio.run(MCPServerManager().connect_server(_stdio_config("Probe", log, **overrides)))
    assert ok is False and message in str(error)
    assert _log_lines(log) == []


def test_streamable_http_without_session_termination_is_refused_before_connect():
    config = mcpmod.create_streamable_http_config(
        name="Remote",
        url="https://127.0.0.1:9/mcp",
        terminate_on_close=False,
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
    )
    ok, error = asyncio.run(MCPServerManager().connect_server(config))
    assert ok is False and "terminate_on_close=false is not supported" in str(error)


# --- SSE and Streamable HTTP over TLS -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory):
    """A private CA and a server certificate for 127.0.0.1 it signed."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory = tmp_path_factory.mktemp("tls")
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sage probe CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name).public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    paths = {"ca": directory / "ca.pem", "cert": directory / "server.pem", "key": directory / "server.key"}
    paths["ca"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    paths["cert"].write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    paths["key"].write_bytes(server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    return paths


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(params=["sse", "http"])
def tls_server(request, tls_material, tmp_path):
    transport = request.param
    port = _free_port()
    log = tmp_path / "calls.log"
    process = subprocess.Popen(
        [sys.executable, str(SERVER), transport, str(port), str(tls_material["cert"]), str(tls_material["key"])],
        env={**os.environ, "PROBE_LOG": str(log)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"probe {transport} server did not start")
                time.sleep(0.1)
        path = "/sse" if transport == "sse" else "/mcp"
        yield {"transport": transport, "url": f"https://127.0.0.1:{port}{path}", "log": log}
    finally:
        process.terminate()
        process.wait(timeout=10)


def _http_config(server, **overrides):
    factory = mcpmod.create_sse_config if server["transport"] == "sse" else mcpmod.create_streamable_http_config
    return factory(
        name="Remote",
        url=server["url"],
        headers={"X-Probe-Header": "header-from-config"},
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
        **overrides,
    )


def _clear_trust_env(monkeypatch):
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)


def test_http_transports_trust_the_selected_ca_bundle_and_carry_headers(tls_server, tls_material, monkeypatch):
    _clear_trust_env(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_material["ca"]))

    async def scenario():
        manager = MCPServerManager()
        assert await manager.connect_server(_http_config(tls_server)) == (True, None)
        tools = {tool.name: tool for tool in manager.get_tools_by_server("Remote")}
        message = await tools["echo_context"].ainvoke(
            ToolCall(name="echo_context", args={"name": "PROBE_LOG"}, id="call-h", type="tool_call")
        )
        status = await tools["read_status"].ainvoke({"job_id": 0})
        assert await manager.disconnect_server("Remote") is True
        return set(tools), message, status, manager

    names, message, status, manager = asyncio.run(scenario())
    assert names == PROBE_TOOLS
    assert message.artifact["structured_content"]["header"] == "header-from-config"
    assert bh.decode_ingest_response(status, info_type="status", expected_job_id=0)["status_message"] == "not_found"
    assert manager.get_connected_servers() == []


def test_http_transports_reject_a_certificate_outside_the_trust_store(tls_server, monkeypatch):
    _clear_trust_env(monkeypatch)
    ok, error = asyncio.run(MCPServerManager().connect_server(_http_config(tls_server)))
    assert ok is False
    assert "certificate" in str(error).lower() or "ssl" in str(error).lower(), error
    assert _calls(tls_server["log"]) == []


def test_http_transports_connect_without_verification_when_configured(tls_server, monkeypatch):
    _clear_trust_env(monkeypatch)

    async def scenario():
        manager = MCPServerManager()
        assert await manager.connect_server(_http_config(tls_server, ssl_verify=False)) == (True, None)
        tool = next(tool for tool in manager.get_tools_by_server("Remote") if tool.name == "read_status")
        result = await tool.ainvoke({"job_id": 0})
        await manager.close_all_connections()
        return result

    assert bh.decode_ingest_response(asyncio.run(scenario()), info_type="status", expected_job_id=0)


# --- Native slash commands through the real client ------------------------------------------------------------


def test_mcp_call_denies_a_server_annotated_destructive_tool_even_when_allowlisted(tmp_path):
    from sage_chat.slash import _handle_mcp

    log = tmp_path / "calls.log"
    spec = json.dumps({
        "type": "stdio",
        "name": "Probe",
        "command": sys.executable,
        "args": [str(SERVER), "stdio"],
        "env": {"PROBE_LOG": str(log)},
        "cwd": str(SERVER.parent),
        "sage_execution_class": mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
        "read_only_tools": ["read_status", "wipe_graph"],
    })

    async def scenario():
        try:
            connected = await _handle_mcp(f"connect {spec}")
            denied = await _handle_mcp('call Probe wipe_graph {"confirm": true}')
            allowed = await _handle_mcp('call Probe read_status {"job_id": 0}')
            return connected, denied, allowed
        finally:
            await mcpmod.MCPManager.disconnect_server("Probe")

    connected, denied, allowed = asyncio.run(scenario())
    assert connected == "Connected MCP server `Probe`."
    assert "explicit non-read-only annotation" in denied
    assert "not_found" in allowed
    assert [name for name, _ in _calls(log)] == ["read_status"]


@pytest.fixture
def canonical_bloodhound(tmp_path, monkeypatch):
    """Make the probe server the canonical BloodHound launcher: the pinned directory and command."""
    directory = tmp_path / "bloodhound_mcp"
    directory.mkdir()
    log = tmp_path / "calls.log"
    launcher = tmp_path / "bloodhound-launcher"

    def configure(mode: str = "not_found") -> Path:
        launcher.write_text(
            "#!/bin/sh\n"
            f"PROBE_LOG='{log}' PROBE_STATUS_MODE='{mode}' exec '{sys.executable}' '{SERVER}' stdio bloodhound\n"
        )
        launcher.chmod(0o755)
        return log

    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", str(directory))
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_COMMAND", str(launcher))
    monkeypatch.delenv("UV_NO_SYNC", raising=False)
    bh.reset_bloodhound_connect_cache()
    yield configure
    asyncio.run(mcpmod.MCPManager.close_all_connections())
    bh.reset_bloodhound_connect_cache()


def test_bloodhound_slash_reads_ingest_status_and_lists_tools_through_the_client(canonical_bloodhound):
    from sage_chat.slash import _handle_bloodhound, _handle_mcp

    log = canonical_bloodhound("not_found")

    async def scenario():
        connection = await _handle_bloodhound(None, "force")
        listing = await _handle_mcp("tools BloodHound")
        return connection, listing, bh.bloodhound_tool_admission()

    connection, listing, admission = asyncio.run(scenario())
    assert "Connected to BloodHound MCP" in connection
    assert "**Ingest:** Ready. The connected BloodHound MCP answered an exact-job ingest status read." in connection
    for name in ("file_upload", "domain_info", "cypher_query"):
        assert listing.count(f"- `{name}`") == 1
    assert admission["ready"] is True
    assert [name for name, _ in _calls(log)] == ["domain_info", "file_upload"]


@pytest.mark.parametrize(
    ("mode", "kind"),
    [("wrong_job", bh.INGEST_ERROR_MISMATCH), ("error_flag", bh.INGEST_ERROR_TOOL), ("slow", bh.INGEST_ERROR_TRANSIENT)],
)
def test_bloodhound_ingest_readiness_fails_on_a_wrong_job_an_error_or_a_timeout(canonical_bloodhound, mode, kind):
    canonical_bloodhound(mode)

    async def scenario():
        connected, _ = await bh.ensure_bloodhound_connected(force=True)
        assert connected is True
        return await bh.probe_ingest_contract(timeout=1.0)

    contract = asyncio.run(scenario())
    assert contract.ready is False
    assert contract.kind == kind
    assert contract.timed_out is (mode == "slow")
