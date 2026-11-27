"""The Sandbox's executor selection and its confined local executor, on the real kernel.

No mock stands in for Landlock, seccomp or the resource limits: E1 and E2 run the real launcher. On a host without
Landlock ABI 6 (CI runners), those tests skip and E3 asserts the unavailable branch and its reason instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
import platform
import shutil
import socketserver
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.langgraph import sandbox_executor
from ai.langgraph.mythic_tools import MythicTools
from mythic_container.ChatBase import ChatSlashCommandInvocation
from sage_chat.headless import HeadlessSageChat, build_chat_request
from sage_chat.slash import handle_slash
from tests import sandbox_checks

FIXTURE = Path(__file__).parent / "fixtures" / "sandbox" / "request125_sandbox_exec_call.json"
CONFINED = (
    platform.system() == "Linux"
    and platform.machine() in {"x86_64", "aarch64"}
    and sandbox_executor.landlock_abi() >= sandbox_executor.REQUIRED_LANDLOCK_ABI
)
needs_confinement = pytest.mark.skipif(not CONFINED, reason="host lacks Landlock ABI 6 or a supported architecture")


@pytest.fixture(autouse=True)
def _fresh_selection():
    sandbox_executor.select_executor.cache_clear()
    yield
    sandbox_executor.select_executor.cache_clear()


@pytest.fixture
def no_docker(monkeypatch):
    short = tempfile.mkdtemp(prefix="sbx-")
    monkeypatch.setenv("DOCKER_HOST", f"unix://{short}/absent.sock")
    yield
    shutil.rmtree(short, ignore_errors=True)


class _EngineHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - http.server API
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/version"):
            body = json.dumps({"ApiVersion": "1.47", "MinAPIVersion": "1.24", "Version": "27.0.0"}).encode()
            ctype = "application/json"
        elif path.endswith("/_ping"):
            body, ctype = b"OK", "text/plain"
        else:
            body, ctype = b"{}", "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def address_string(self):
        return "unix"

    def log_message(self, *_args):
        return None


class _UnixEngine(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def minimal_engine(monkeypatch):
    short = tempfile.mkdtemp(prefix="sbx-")
    path = os.path.join(short, "docker.sock")
    server = _UnixEngine(path, _EngineHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DOCKER_HOST", f"unix://{path}")
    yield
    server.shutdown()
    server.server_close()
    shutil.rmtree(short, ignore_errors=True)


# --- E1, E2: the launcher on the real kernel ---------------------------------------------------------------------


@needs_confinement
def test_e1_local_executor_confines_files_network_signals_and_environment():
    outcomes = sandbox_checks.confinement_outcomes(sandbox_executor.run_local)
    assert outcomes["probes"] == sandbox_checks.EXPECTED_CONFINEMENT
    # A shell pipeline forks under the process cap (measured against the kernel's own counter), and `/` stays closed.
    assert outcomes["shell"] == ["2", "root-denied"]
    assert outcomes["shell_exit"] == 0


@needs_confinement
def test_e2_local_executor_enforces_timeout_process_output_and_file_limits():
    assert sandbox_checks.limit_outcomes(sandbox_executor.run_local) == sandbox_checks.EXPECTED_LIMITS


@needs_confinement
def test_local_result_keeps_the_fields_the_sandbox_agent_reads():
    result = sandbox_executor.run_local("python", "print(2 + 2)", 10)
    assert set(result) >= {"exit_code", "stdout", "stderr", "timed_out", "truncated"}
    assert (result["exit_code"], result["stdout"], result["timed_out"], result["truncated"]) == (0, "4\n", False, False)


# --- E3: selection ---------------------------------------------------------------------------------------------------


def test_e3_absent_engine_selects_local_when_landlock_is_available(no_docker):
    executor, reason = sandbox_executor.select_executor()
    if CONFINED:
        assert (executor, reason) == ("local", "")
    else:
        assert executor is None
        assert "Landlock" in reason


def test_e3_reachable_engine_selects_container(minimal_engine):
    assert sandbox_executor.select_executor() == ("container", "")


def test_e3_absent_engine_and_too_old_landlock_is_unavailable_with_a_reason(no_docker, monkeypatch):
    monkeypatch.setattr(sandbox_executor, "REQUIRED_LANDLOCK_ABI", sandbox_executor.landlock_abi() + 1)
    executor, reason = sandbox_executor.select_executor()
    assert executor is None
    assert "no Docker engine" in reason and "Landlock" in reason


# --- E4: /sandbox through the real slash dispatch and the real tool -------------------------------------------------


def _sandbox_reply(argument: str) -> str:
    chat = HeadlessSageChat()
    request = build_chat_request("", channel_id=9, request_id=1)
    request.SlashCommand = ChatSlashCommandInvocation(name="sandbox", argument=argument)
    model = SimpleNamespace(mythic_client=MythicTools(agent_task_id=1))  # real tool; Mythic login is not on this path
    assert asyncio.run(handle_slash(chat, request, model, "slash:1")) is True
    return chat.terminal_emissions[-1]["content"]


@needs_confinement
def test_e4_slash_sandbox_runs_on_the_local_executor_without_docker(no_docker):
    text = _sandbox_reply("python print(2 + 2)")
    assert "| Status | `ok` |" in text
    assert "| Executor | `local` |" in text
    assert "```stdout\n4\n\n```" in text


def test_e4_slash_sandbox_reports_unavailable_plainly(no_docker, monkeypatch):
    monkeypatch.setattr(sandbox_executor, "REQUIRED_LANDLOCK_ABI", sandbox_executor.landlock_abi() + 1)
    text = _sandbox_reply("python print(2 + 2)")
    assert "unavailable" in text
    assert "no Docker engine" in text
    assert "Sandbox executors" in text
    assert "DockerException" not in text


# --- R1: the recorded model call through the real consumer ----------------------------------------------------------


@needs_confinement
def test_r1_recorded_sandbox_call_runs_on_the_local_executor(no_docker):
    recorded = json.loads(FIXTURE.read_text())
    raw = asyncio.run(MythicTools(agent_task_id=1).sandbox_exec(**recorded["arguments"]))
    result = json.loads(raw)
    assert (result["status"], result["executor"], result["exit_code"]) == ("ok", "local", 0)
    fib = [1, 1]
    while len(fib) < 25:
        fib.append(fib[-1] + fib[-2])
    assert hashlib.sha256(b"sage-sandbox-check").hexdigest() in result["stdout"]
    assert str(fib[24]) in result["stdout"]


def test_unavailable_sandbox_exec_returns_the_plain_report(no_docker, monkeypatch):
    monkeypatch.setattr(sandbox_executor, "REQUIRED_LANDLOCK_ABI", sandbox_executor.landlock_abi() + 1)
    result = json.loads(asyncio.run(MythicTools(agent_task_id=1).sandbox_exec(code_or_command="true")))
    assert result["status"] == "unavailable"
    assert "no Docker engine" in result["reason"]


def test_sandbox_availability_disables_the_tool_and_survives_scope_gating(no_docker, monkeypatch):
    monkeypatch.setattr(sandbox_executor, "REQUIRED_LANDLOCK_ABI", sandbox_executor.landlock_abi() + 1)
    tools = MythicTools(agent_task_id=1)
    assert asyncio.run(tools.apply_sandbox_availability())
    tools.apply_scope_gating({"chat-ai.write", "apitoken.write"})
    assert "sandbox_exec" in tools.disabled_tools
    assert [t.name for t in tools.get_tools(["sandbox_exec"])] == []


@needs_confinement
def test_available_sandbox_keeps_the_tool(no_docker):
    tools = MythicTools(agent_task_id=1)
    assert asyncio.run(tools.apply_sandbox_availability()) == ""
    tools.apply_scope_gating(None)
    assert [t.name for t in tools.get_tools(["sandbox_exec"])] == ["sandbox_exec"]


# --- E6: opt-in, inside the built Sage image -------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("SAGE_SANDBOX_CONTAINER_TESTS") != "1", reason="opt-in: SAGE_SANDBOX_CONTAINER_TESTS=1")
def test_e6_local_executor_inside_the_sage_image():
    sage_root = Path(__file__).resolve().parents[1]
    image = os.environ.get("SAGE_SANDBOX_IMAGE", "sage:latest")
    proc = subprocess.run(
        [
            "docker", "run", "--rm", "--user", str(os.getuid()), "--network", "host",
            "-v", f"{sage_root}:/src:ro", "--entrypoint", "python3", image,
            "-I", "/src/tests/sandbox_checks.py", "/src",
        ],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["abi"] >= sandbox_executor.REQUIRED_LANDLOCK_ABI
    assert report["confinement"]["probes"] == sandbox_checks.EXPECTED_CONFINEMENT
    assert report["confinement"]["shell"] == ["2", "root-denied"]
    assert report["limits"] == sandbox_checks.EXPECTED_LIMITS
