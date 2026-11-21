"""The real Codex worker image (bridge + pinned adapter) driven by Sage's real ACP manager and slash dispatcher.

Opt-in: set ``SAGE_ACP_CONTAINER_TESTS=1`` with Docker available and the worker image built
(``docker build -t sage-codex-acp:2.9.0 Payload_Type/sage/container/codex``; override the tag with
``SAGE_ACP_WORKER_IMAGE``). Each worker runs with networking disabled, as a non-root UID matching this process,
and a public non-secret sentinel as its API-key input. No prompt is sent; nothing reaches a model provider.

Substitution: this test process stands in for the Sage container on the client side of the socket; the plan's
native check exercises the real Sage container on the feature deployment.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import pytest
from mythic_container.ChatBase import ChatSlashCommandInvocation

import ai.acp as acp_module
import sage_chat.session as chat_session
from sage_chat.headless import HeadlessSageChat, build_chat_request

IMAGE = os.environ.get("SAGE_ACP_WORKER_IMAGE", "sage-codex-acp:2.9.0")
SENTINEL = "public-sentinel-not-a-credential-" + uuid.uuid4().hex[:8]

pytestmark = pytest.mark.skipif(
    os.environ.get("SAGE_ACP_CONTAINER_TESTS") != "1" or shutil.which("docker") is None,
    reason="real worker container tests are opt-in (SAGE_ACP_CONTAINER_TESTS=1, Docker, built image)",
)


class _NoModelChat(HeadlessSageChat):
    async def _get_or_create_model(self, request):
        raise AssertionError("/acp constructed a Sage model")

    async def _restore_channel_session(self, request):
        raise AssertionError("/acp restored a Sage model")


def _docker(*args, check=True, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


class Worker:
    def __init__(self, root: Path, *, key: bool = True):
        self.root = root
        self.key = key
        self.name = f"sage-codex-test-{uuid.uuid4().hex[:10]}"
        (root / "transport").mkdir(mode=0o700)
        self.socket = root / "transport" / "acp.sock"
        if key:
            (root / "key").write_text(SENTINEL + "\n")
            (root / "key").chmod(0o600)

    def start(self):
        uid, gid = os.getuid(), os.getgid()
        args = ["run", "-d", "--name", self.name, "--init", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", f"{uid}:{gid}",
                "--tmpfs", f"/state:mode=0700,uid={uid},gid={gid}", "--tmpfs", "/tmp:mode=1777",
                "-v", f"{self.root / 'transport'}:/transport"]
        if self.key:
            args += ["-v", f"{self.root / 'key'}:/run/secrets/codex_api_key:ro"]
        _docker(*args, IMAGE)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and '"event":"listening"' not in self.logs():
            time.sleep(0.1)
        assert '"event":"listening"' in self.logs(), self.logs()

    def restart(self):
        _docker("restart", "-t", "10", self.name)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.logs().count('"event":"listening"') < 2:
            time.sleep(0.1)

    def stop(self):
        _docker("stop", "-t", "10", self.name, check=False)

    def remove(self):
        _docker("rm", "-f", self.name, check=False)

    def logs(self) -> str:
        result = _docker("logs", self.name, check=False)
        return result.stdout + result.stderr

    def events(self) -> list[dict]:
        rows = []
        for line in self.logs().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def adapters(self) -> list[str]:
        """Live adapter/Codex processes in the worker, observed by Docker, not by the bridge."""
        result = _docker("top", self.name, "-o", "pid,args", check=False)
        return [line for line in result.stdout.splitlines()[1:]
                if "bridge.mjs" not in line and "docker-init" not in line]

    def state_entries(self) -> list[str]:
        return _docker("exec", self.name, "ls", "-A", "/state").stdout.split()


@pytest.fixture
def worker_root():
    path = Path(tempfile.mkdtemp(prefix="acpw-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def workers(worker_root):
    started: list[Worker] = []

    def make(name="w", **kwargs):
        root = worker_root / name
        root.mkdir()
        worker = Worker(root, **kwargs)
        worker.start()
        started.append(worker)
        return worker

    yield make
    for worker in started:
        worker.remove()


@pytest.fixture
def runtime_db(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(acp_module, "ACP_MANAGER", acp_module.AcpConnectionManager())


def _request(argument, *, channel_id, name="acp"):
    request = build_chat_request("", operation_id=7, channel_id=channel_id, request_id=channel_id * 100)
    request.SlashCommand = ChatSlashCommandInvocation(name=name, argument=argument)
    return request


async def _send(chat, argument, *, channel_id, name="acp"):
    before = len(chat.terminal_emissions)
    await chat.chat(_request(argument, channel_id=channel_id, name=name))
    emitted = chat.terminal_emissions[before:]
    assert len(emitted) == 1
    return emitted[0]["content"]


async def _until(predicate, seconds=6.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


def test_two_channels_restart_and_stop_against_the_real_worker(runtime_db, workers, monkeypatch):
    worker = workers()
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(worker.socket))
    outputs: list[str] = []

    async def scenario():
        chat = _NoModelChat()
        say = lambda *a, **k: _send(chat, *a, **k)  # noqa: E731
        a1 = await say("connect codex", channel_id=1)
        models = await say("models", channel_id=1)
        choices = [line.split("`")[1] for line in models.splitlines() if line.startswith("| `")]
        default = next(line.split("`")[1] for line in models.splitlines() if line.endswith("| yes |"))
        other = next(c for c in choices if c != default)
        a2 = await say(f"model {other}", channel_id=1)
        b1 = await say("connect codex", channel_id=2)
        a3 = await say("model not-a-real-model", channel_id=1)
        a4 = await say("status", channel_id=1)
        assert len(worker.adapters()) >= 2
        await say("disconnect", channel_id=1)
        a_gone = await _until(lambda: sum(e.get("event") == "cleanup" for e in worker.events()) >= 1)
        b2 = await say("status", channel_id=2)
        worker.restart()
        b3 = await say("status", channel_id=2)
        b4 = await say("connect codex", channel_id=2)
        b5 = await say(f"model {other}", channel_id=2)
        started = time.monotonic()
        stop = await say("", channel_id=2, name="stop")
        b_closed = await _until(lambda: not worker.adapters(), 5.0)
        stop_seconds = time.monotonic() - started
        b6 = await say("status", channel_id=2)
        outputs.extend([a1, models, a2, b1, a3, a4, b2, b3, b4, b5, stop, b6])
        return default, other, a1, a2, b1, a3, a4, a_gone, b2, b3, b4, b5, stop, b_closed, stop_seconds, b6

    (default, other, a1, a2, b1, a3, a4, a_gone, b2, b3, b4, b5, stop, b_closed, stop_seconds,
     b6) = asyncio.run(scenario())
    assert "| Transport | `connected` |" in a1 and "| Protocol | ACP v1 |" in a1
    assert "@agentclientprotocol/codex-acp 2.9.0" in a1
    assert "unverified" in a1 and "does not prove the provider accepts it" in a1
    assert f"| Effective model | `{other}` |" in a2
    assert f"| Effective model | `{default}` |" in b1
    assert "is not a model this agent advertises" in a3 and f"| Effective model | `{other}` |" in a4
    assert a_gone
    assert "| Transport | `connected` |" in b2 and f"| Effective model | `{default}` |" in b2
    assert "| Transport | `disconnected` |" in b3
    assert "| Transport | `connected` |" in b4
    assert f"| Effective model | `{other}` |" in b5
    assert "Closed this channel's coding-agent connection" in stop
    assert b_closed and stop_seconds < 5
    assert "| Transport | `disconnected` |" in b6 and f"| Saved model | `{other}` |" in b6
    assert worker.state_entries() == []
    methods = [e.get("method") for e in worker.events() if e.get("event") == "message"]
    assert "session/prompt" not in methods
    assert not [m for m in methods if m and m.startswith(("fs/", "terminal/", "session/request_permission"))]
    assert SENTINEL not in worker.logs()
    assert not any(SENTINEL in text for text in outputs)


def test_worker_without_a_key_reports_no_usable_credential(runtime_db, workers, monkeypatch):
    worker = workers(key=False)
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(worker.socket))

    async def scenario():
        chat = _NoModelChat()
        connected = await _send(chat, "connect codex", channel_id=3)
        status = await _send(chat, "status", channel_id=3)
        bye = await _send(chat, "disconnect", channel_id=3)
        return connected, status, bye

    connected, status, bye = asyncio.run(scenario())
    assert "| Transport | `connected` |" in connected
    assert "`authenticated`" not in connected
    assert "| Authentication (observed) | `unknown` |" in connected or \
        "| Authentication (observed) | `unauthenticated` |" in connected
    assert "unverified" in status
    assert "cleared" in bye
    accept = next(e for e in worker.events() if e.get("event") == "accept")
    assert accept["api_key_provisioned"] is False


def test_stopped_worker_is_a_bounded_failure_without_fallback(runtime_db, workers, monkeypatch):
    worker = workers()
    monkeypatch.setenv("SAGE_ACP_SOCKET", str(worker.socket))
    worker.stop()

    async def scenario():
        started = time.monotonic()
        text = await _send(_NoModelChat(), "connect codex", channel_id=4)
        return text, time.monotonic() - started

    text, elapsed = asyncio.run(scenario())
    assert "| Transport | `failed` |" in text and "worker is unavailable" in text
    assert elapsed < 5


def _raw_initialize(path: Path, request_id: int) -> socket.socket:
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(20)
    client.connect(str(path))
    client.sendall(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "initialize",
                               "params": {"protocolVersion": 1, "clientCapabilities": {}}}).encode() + b"\n")
    return client


def _read_line(client: socket.socket) -> dict:
    data = b""
    while not data.endswith(b"\n"):
        chunk = client.recv(65536)
        if not chunk:
            break
        data += chunk
    return json.loads(data.splitlines()[0]) if data else {}


def test_fifth_connection_gets_a_busy_error_and_oversize_lines_end_the_connection(workers):
    worker = workers()
    held = [_raw_initialize(worker.socket, n) for n in range(4)]
    for client in held:
        assert _read_line(client)["result"]["protocolVersion"] == 1
    fifth = _raw_initialize(worker.socket, 99)
    busy = _read_line(fifth)
    assert busy["id"] == 99 and "busy: 4 connections" in busy["error"]["message"]
    for client in held:
        client.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and sum(e.get("event") == "cleanup" for e in worker.events()) < 4:
        time.sleep(0.1)
    assert sum(e.get("event") == "cleanup" for e in worker.events()) == 4

    oversize = socket.socket(socket.AF_UNIX)
    oversize.connect(str(worker.socket))
    oversize.sendall(b'{"pad":"' + b"x" * (1024 * 1024 + 16) + b'"}\n')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not any(
            e.get("reason") == "oversize message from client" for e in worker.events()):
        time.sleep(0.1)
    assert any(e.get("event") == "cleanup" and e.get("reason") == "oversize message from client"
               for e in worker.events())
    assert worker.adapters() == [] and worker.state_entries() == []
