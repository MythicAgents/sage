"""A small real MCP server for Sage's MCP client tests.

    probe_server.py stdio [bloodhound]
    probe_server.py sse|http <port> [<certfile> <keyfile>]

`bloodhound` adds the three tools BloodHound admission requires (`domain_info`, `cypher_query`, `file_upload`)
with the patched server's ingest-status contract. `PROBE_STATUS_MODE` picks the status answer: `not_found`
(default, the job-0 readiness answer), `wrong_job`, `error_flag` or `slow`.

Every process start appends `start <pid>`, and every tool call `<tool> <pid>`, to `$PROBE_LOG` when it is set, so
a test can tell which server process answered and whether a call reached or started any server at all.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ToolAnnotations

mcp = FastMCP("sage-probe")


def _record(name: str) -> None:
    log = os.environ.get("PROBE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"{name} {os.getpid()}\n")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_status(job_id: int) -> str:
    """Inert read: the ingest-status envelope for one job id."""
    _record("read_status")
    return json.dumps({"info_type": "status", "data": {"job_id": job_id, "status_message": "not_found"}})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def wipe_graph(confirm: bool) -> str:
    """A tool whose server annotations declare it destructive."""
    _record("wipe_graph")
    return "wiped"


@mcp.tool()
def echo_context(name: str) -> dict:
    """Structured content: one environment variable and the request's probe header."""
    _record("echo_context")
    headers = get_http_headers(include={"x-probe-header"})
    return {"env": os.environ.get(name), "header": headers.get("x-probe-header"), "pid": os.getpid()}


@mcp.tool()
async def slow(seconds: float) -> str:
    """Answer after a delay."""
    _record("slow")
    await asyncio.sleep(seconds)
    return "done"


def _add_bloodhound_tools() -> None:
    @mcp.tool()
    def domain_info(info_type: str) -> str:
        """BloodHound domain read."""
        _record("domain_info")
        return json.dumps({"domains": [{"name": "PROBE.LOCAL"}]})

    @mcp.tool()
    def cypher_query(query: str) -> str:
        """BloodHound Cypher read."""
        _record("cypher_query")
        return json.dumps({"rows": []})

    @mcp.tool()
    async def file_upload(
        info_type: str,
        job_id: int | None = None,
        file_name: str | None = None,
        file_bytes_base64: str | None = None,
    ) -> str:
        """BloodHound file upload and ingest-status read."""
        _record("file_upload")
        mode = os.environ.get("PROBE_STATUS_MODE", "not_found")
        if mode == "slow":
            await asyncio.sleep(30)
        data: dict = {"job_id": job_id, "status_message": "not_found"}
        if mode == "wrong_job":
            data["job_id"] = (job_id or 0) + 1
        envelope: dict = {"info_type": info_type, "data": data}
        if mode == "error_flag":
            envelope["error"] = "ingest status unavailable"
        return json.dumps(envelope)


def main(argv: list[str]) -> None:
    _record("start")
    transport = argv[1]
    if transport == "stdio":
        if argv[2:3] == ["bloodhound"]:
            _add_bloodhound_tools()
        mcp.run(transport="stdio", show_banner=False)
        return
    import uvicorn

    port = int(argv[2])
    ssl = {"ssl_certfile": argv[3], "ssl_keyfile": argv[4]} if len(argv) > 4 else {}
    app = mcp.http_app(transport="sse" if transport == "sse" else "streamable-http")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", **ssl)


if __name__ == "__main__":
    main(sys.argv)
