"""In-process Phoenix stays responsive under Sage's real tracer registration, and Sage's spans still arrive.

Runs Phoenix and ``register_sage_tracing`` in a child interpreter, because Phoenix's server threads cannot
be stopped cleanly inside the test process. The child probes its own Phoenix over HTTP and reports JSON.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

SAGE_ROOT = Path(__file__).resolve().parents[1]

_CHILD = r"""
import json, os, sys, time, urllib.request

import phoenix as px

mode = sys.argv[1]
port = os.environ["PHOENIX_PORT"]
base = f"http://127.0.0.1:{port}"

try:
    px.launch_app(use_temp_dir=False)
except RuntimeError:
    if mode == "warmup":  # Phoenix allows its server 5 s to start; creating a fresh database can exceed it
        os._exit(0)
    raise
if mode == "warmup":
    os._exit(0)

if mode == "sage":
    from ai.phoenix_tracing import register_sage_tracing
    provider = register_sage_tracing()
else:  # the pre-fix registration, kept to show the probe can see the deadlock
    from phoenix.otel import register
    provider = register(project_name="Sage", auto_instrument=False, batch=False)

from langchain_core.runnables import RunnableLambda
from ai.langgraph import kernel_tracing

with kernel_tracing.kernel_span("probe.kernel_step"):
    RunnableLambda(lambda x: x + 1).invoke(1)


def probe(path, body=None):
    request = urllib.request.Request(base + path, data=body,
                                     headers={"Content-Type": "application/json"} if body else {})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return {"status": response.status, "seconds": round(time.monotonic() - started, 3),
                    "body": response.read().decode()}
    except Exception as exc:
        return {"status": None, "error": type(exc).__name__}


health = probe("/healthz")
query = json.dumps({"query": "{ projects { edges { node { name traceCount } } } }"}).encode()
counts = {}
deadline = time.monotonic() + 15  # Phoenix writes received spans to its database in periodic batches
while True:
    graphql = probe("/graphql", query)
    if graphql.get("status") == 200:
        counts = {edge["node"]["name"]: edge["node"]["traceCount"]
                  for edge in json.loads(graphql["body"])["data"]["projects"]["edges"]}
    if graphql.get("status") != 200 or counts.get("Sage", 0) >= 1 or time.monotonic() > deadline:
        break
    time.sleep(0.5)
print(json.dumps({"health": health.get("status"), "graphql": graphql.get("status"), "trace_counts": counts}),
      flush=True)
os._exit(0)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(mode: str, tmp_path: Path) -> dict:
    env = {
        **os.environ,
        "PHOENIX_WORKING_DIR": str(tmp_path / "phoenix"),
        "PHOENIX_PORT": str(_free_port()),
        "PHOENIX_GRPC_PORT": str(_free_port()),
        "PHOENIX_HOST": "127.0.0.1",
        "SAGE_KERNEL_TRACING": "1",
    }
    env.pop("PHOENIX_COLLECTOR_ENDPOINT", None)
    # Migrate the fresh database first so the measured launch fits Phoenix's startup window.
    subprocess.run(
        [sys.executable, "-c", _CHILD, "warmup"], cwd=SAGE_ROOT, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=180, check=False,
    )
    for _attempt in range(3):  # a busy machine can miss Phoenix's own 5 s startup window
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, mode], cwd=SAGE_ROOT, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, check=False,
        )
        if "server took too long to start" not in result.stderr:
            break
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, f"child produced no report (exit {result.returncode}): {result.stderr[-2000:]}"
    return json.loads(lines[-1])


def test_phoenix_answers_and_receives_sage_spans_with_sage_tracing(tmp_path):
    report = _run("sage", tmp_path)

    assert report["health"] == 200
    assert report["graphql"] == 200
    # The kernel span and the LangChain run under it form one trace in the Sage project; Phoenix's own
    # request spans no longer route through Sage's exporter.
    assert report["trace_counts"].get("Sage", 0) >= 1


def test_probe_detects_the_deadlock_of_a_global_synchronous_provider(tmp_path):
    report = _run("global", tmp_path)

    assert report["health"] is None
