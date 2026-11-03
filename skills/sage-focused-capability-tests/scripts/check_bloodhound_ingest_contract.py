#!/usr/bin/env python3
"""Check a built Sage image's baked BloodHound MCP serves the ingest contract Sage depends on.

Usage:
    check_bloodhound_ingest_contract.py --image <image-id-or-tag> [--json <result.json>]

The image is never trusted by tag alone: the result records the resolved image ID, the MCP base commit, and the
SHA-256 of the status patch baked at /opt/sage-patches. A throwaway container runs with `--network none`, no
operator configuration or data mounts, and the normal Sage entrypoint bypassed; only this file is mounted, read-only.
Inside it, a loopback fake BloodHound API answers the MCP, and Sage's own MCP manager launches the baked MCP through
its normal `uv run --no-sync` stdio path with inert credentials. Discovery, the readiness read, a byte upload of a
generated collection ZIP, and exact-job status reads all go through the installed dependency and are decoded by
Sage's own ingest-contract helpers.

This checks the packaged producer/consumer contract only. It does not prove real BloodHound ingestion or live
reliability, and it has no live-target mode. Exit status: 0 all checks passed, 1 a check failed, 2 the check could
not run (Docker or the image unavailable) — a blocker, never a pass.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

CONTAINER_SCRIPT = "/sage-check/check_bloodhound_ingest_contract.py"
CHECK_TIMEOUT_SECONDS = 300
MCP_DIR = "/opt/bloodhound_mcp"
PATCH_PATH = "/opt/sage-patches/bloodhound-mcp-ingest-status.patch"
RESULT_MARKER = "SAGE_INGEST_CHECK_RESULT="
#: Job ids the fake API reserves for scenarios.
OLD_JOB_COUNT = 60
AUTH_FAILURE_JOB = 401


# --- host side -----------------------------------------------------------------------------------------------


def docker_command(image: str, name: str, script: Path) -> list[str]:
    """The throwaway container: no network, no operator mounts, entrypoint bypassed, this file read-only."""
    return [
        "docker", "run", "--rm", "--name", name, "--network", "none",
        "--entrypoint", "python3",
        "-v", f"{script.resolve()}:{CONTAINER_SCRIPT}:ro",
        image, "-I", CONTAINER_SCRIPT, "--inside",
    ]


def resolve_image_id(image: str, runner=subprocess.run) -> str | None:
    completed = runner(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True, text=True, timeout=30,
    )
    image_id = completed.stdout.strip() if completed.returncode == 0 else ""
    return image_id or None


def parse_inside_output(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                return json.loads(line[len(RESULT_MARKER):])
            except ValueError:
                return None
    return None


def run_check(image: str, *, runner=subprocess.run, which=shutil.which, timeout: int = CHECK_TIMEOUT_SECONDS) -> tuple[int, dict]:
    """Return (exit status, result record)."""
    if which("docker") is None:
        return 2, {"status": "blocked", "reason": "docker is not available; the image check did not run"}
    image_id = resolve_image_id(image, runner=runner)
    if image_id is None:
        return 2, {"status": "blocked", "reason": f"image {image!r} is not present locally"}
    name = f"sage-ingest-check-{uuid.uuid4().hex[:12]}"
    command = docker_command(image_id, name, Path(__file__))
    try:
        completed = runner(command, capture_output=True, text=True, timeout=min(timeout, CHECK_TIMEOUT_SECONDS))
    except subprocess.TimeoutExpired:
        runner(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)
        return 1, {"status": "failed", "image_id": image_id, "reason": "the image check exceeded its time limit"}
    finally:
        # --rm removes a container that exited; this covers one that a signal or a client crash left behind.
        runner(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)
    inside = parse_inside_output(completed.stdout)
    if inside is None:
        return 1, {
            "status": "failed", "image_id": image_id, "exit_code": completed.returncode,
            "reason": "the container produced no check result",
            "stderr_tail": completed.stderr[-2000:],
        }
    inside["image_id"] = image_id
    inside["image_ref"] = image
    passed = bool(inside.get("checks")) and all(check.get("ok") for check in inside["checks"])
    inside["status"] = "passed" if passed else "failed"
    return (0 if passed else 1), inside


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", help="Image ID or local tag to check (required).")
    parser.add_argument("--json", type=Path, help="Also write the result record to this file.")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.inside:
        return inside_main()
    if not args.image:
        parser.error("--image is required")
    status, result = run_check(args.image)
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    return status


# --- container side (stdlib plus the image's own installed packages) ----------------------------------------


class FakeBloodHound:
    """The BloodHound CE endpoints the MCP touches, with synthetic jobs. Records every request."""

    def __init__(self):
        import threading

        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.jobs: dict[int, dict] = {
            job_id: self._row(job_id, 2, "Complete") for job_id in range(1, OLD_JOB_COUNT + 1)
        }
        self.next_id = OLD_JOB_COUNT + 1
        self.upload_bytes: list[int] = []

    @staticmethod
    def _row(job_id: int, status: int, message: str) -> dict:
        return {
            "id": job_id, "user_id": "00000000-0000-0000-0000-000000000000", "user_email_address": None,
            "status": status, "status_message": message, "start_time": "2026-11-03T00:00:00Z",
            "end_time": "0001-01-01T00:00:00Z", "last_ingest": "2026-11-03T00:00:00Z",
            "total_files": 1, "failed_files": 0, "partial_failed_files": 0,
        }

    def set_job(self, job_id: int, status: int, message: str) -> None:
        with self.lock:
            self.jobs[job_id] = self._row(job_id, status, message)

    def handle(self, method: str, raw_path: str, body: bytes) -> tuple[int, dict | None]:
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(raw_path)
        query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
        with self.lock:
            self.requests.append({"method": method, "path": parts.path, "query": query})
            if method == "GET" and parts.path == "/api/v2/self":
                return 200, {"data": {"id": "inert-check-user"}}
            if method == "POST" and parts.path == "/api/v2/file-upload/start":
                job_id = self.next_id
                self.next_id += 1
                self.jobs[job_id] = self._row(job_id, 1, "")
                return 201, {"data": {"id": job_id}}
            segments = parts.path.strip("/").split("/")
            if method == "POST" and segments[:3] == ["api", "v2", "file-upload"] and len(segments) in (4, 5):
                job_id = int(segments[3])
                if job_id not in self.jobs:
                    return 404, {"errors": [{"message": "no such job"}]}
                if len(segments) == 4:
                    self.upload_bytes.append(len(body))
                    return 202, None
                if segments[4] == "end":
                    self.jobs[job_id]["status"] = 6
                    return 200, None
            if method == "GET" and parts.path == "/api/v2/file-upload":
                limit = int(query.get("limit", "10"))
                id_filter = query.get("id", "")
                if id_filter.startswith("eq:"):
                    job_id = int(id_filter[3:])
                    if job_id == AUTH_FAILURE_JOB:
                        return 401, {"errors": [{"message": "authentication is invalid"}]}
                    rows = [dict(self.jobs[job_id])] if job_id in self.jobs else []
                else:
                    rows = [dict(self.jobs[key]) for key in sorted(self.jobs, reverse=True)]
                rows = rows[:limit]
                return 200, {"count": len(rows), "skip": 0, "limit": limit, "data": rows}
        return 404, {"errors": [{"message": "not found"}]}


def _serve(fake: FakeBloodHound):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = fake.handle(method, self.path, body)
            data = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - http.server API
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802 - http.server API
            self._dispatch("POST")

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _inert_collection_zip() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("20261103000000_domains.json", json.dumps({
            "data": [], "meta": {"type": "domains", "count": 0, "version": 6, "methods": 0},
        }))
    return buffer.getvalue()


def _identity() -> dict:
    import hashlib

    identity = {"base_sha": "", "patch_sha256": "absent"}
    try:
        identity["base_sha"] = subprocess.run(
            ["git", "-C", MCP_DIR, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        identity["base_sha"] = f"unreadable: {type(exc).__name__}"
    if os.path.exists(PATCH_PATH):
        with open(PATCH_PATH, "rb") as handle:
            identity["patch_sha256"] = hashlib.sha256(handle.read()).hexdigest()
    return identity


async def _scenario(fake: FakeBloodHound, port: int) -> list[dict]:
    import asyncio
    import base64

    sys.path.insert(0, "/Mythic")
    from ai import bloodhound_config as bh
    from ai.mcp import MCPManager

    checks: list[dict] = []

    def check(name: str, ok: bool, detail="") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:500]})
        return bool(ok)

    def requests_since(mark: int) -> list[dict]:
        with fake.lock:
            return list(fake.requests[mark:])

    async def status(tool, job_id):
        raw = await asyncio.wait_for(tool.ainvoke({"info_type": "status", "job_id": job_id}), timeout=15)
        try:
            data = bh.decode_ingest_response(raw, info_type="status", expected_job_id=job_id)
            return bh.classify_ingest_status(data), data
        except bh.IngestContractError as exc:
            return f"error:{exc.kind}", {"detail": exc.detail}

    env = {
        "BLOODHOUND_DOMAIN": "127.0.0.1", "BLOODHOUND_PORT": str(port), "BLOODHOUND_SCHEME": "http",
        "BLOODHOUND_TOKEN_ID": "inert-check-token-id", "BLOODHOUND_TOKEN_KEY": "inert-check-token-key",
    }
    config = bh.bloodhound_mcp_config(MCP_DIR, env)
    connected, error = await asyncio.wait_for(MCPManager.connect_server(config), timeout=120)
    if not check("baked MCP starts through Sage's stdio launcher", connected, error or ""):
        return checks
    try:
        mark = len(fake.requests)
        contract = await bh.probe_ingest_contract()
        readiness = requests_since(mark)
        check("readiness read answers typed not_found for job 0", contract.ready, contract.operator_text())
        check(
            "readiness performs one exact-ID GET only",
            [(r["method"], r["path"], r["query"].get("id"), r["query"].get("limit")) for r in readiness]
            == [("GET", "/api/v2/file-upload", "eq:0", "2")],
            readiness,
        )
        if not contract.ready:
            return checks
        tool = contract.tool

        mark = len(fake.requests)
        payload = _inert_collection_zip()
        raw = await asyncio.wait_for(tool.ainvoke({
            "info_type": "upload_bytes", "file_name": "bloodhound_check.zip",
            "file_bytes_base64": base64.b64encode(payload).decode("ascii"),
        }), timeout=30)
        try:
            job_id = bh.upload_job_id(bh.decode_ingest_response(raw, info_type="upload_bytes"))
        except bh.IngestContractError as exc:
            check("byte upload returns a positive job id", False, exc.detail)
            return checks
        uploaded = requests_since(mark)
        check("byte upload returns a positive job id", job_id > OLD_JOB_COUNT, job_id)
        check(
            "byte upload performs one start/upload/end sequence",
            [(r["method"], r["path"]) for r in uploaded] == [
                ("POST", "/api/v2/file-upload/start"),
                ("POST", f"/api/v2/file-upload/{job_id}"),
                ("POST", f"/api/v2/file-upload/{job_id}/end"),
            ] and fake.upload_bytes == [len(payload)],
            uploaded,
        )

        mark = len(fake.requests)
        state, data = await status(tool, job_id)
        check("an ingesting job reads as pending", state == "pending" and data.get("status") == 6, data)
        fake.set_job(job_id, 2, "Complete")
        state, data = await status(tool, job_id)
        check("the uploaded job's Complete reads as complete", state == "complete", data)
        state, data = await status(tool, 999)
        check("an unlisted job reads as typed not_found", state == "pending" and data.get("status_message") == "not_found", data)
        for offset, (number, message, label) in enumerate((
            (5, "All files failed to ingest as JSON Content", "Failed"),
            (8, "1 File(s) failed to ingest as JSON Content", "Partially Complete"),
            (4, "Ingest timeout: No ingest activity observed in 20.0 minutes. Upload incomplete.", "Timed Out"),
            (3, "", "Canceled"),
        ), start=1):
            terminal_id = job_id + offset
            fake.set_job(terminal_id, number, message)
            state, data = await status(tool, terminal_id)
            check(
                f"CE status {number} reads as terminal failure with its documented label",
                state == "terminal_failure" and data.get("status_message") == label
                and data.get("detail") == message,
                data,
            )
        state, data = await status(tool, AUTH_FAILURE_JOB)
        check("an authentication failure is an error, not pending", state == f"error:{bh.INGEST_ERROR_AUTH}", data)
        state, data = await status(tool, 1)
        check("a job older than fifty newer jobs is found by exact ID", state == "complete" and data.get("job_id") == 1, data)
        state, data = await status(tool, -1)
        check("a negative job id is rejected", state.startswith("error:"), data)
        status_requests = requests_since(mark)
        check(
            "status reads are exact-ID GETs only",
            all(r["method"] == "GET" and r["path"] == "/api/v2/file-upload"
                and r["query"].get("id", "").startswith("eq:") and r["query"].get("limit") == "2"
                for r in status_requests) and len(status_requests) == 9,
            status_requests,
        )
        with fake.lock:
            posts = [r for r in fake.requests if r["method"] == "POST"]
        check("the whole check started, uploaded, and ended exactly one job", len(posts) == 3, posts)
    finally:
        try:
            await asyncio.wait_for(MCPManager.disconnect_server(bh.BLOODHOUND_SERVER_NAME), timeout=20)
        except Exception:  # noqa: BLE001 - the container is discarded either way
            pass
    return checks


def inside_main() -> int:
    import asyncio

    fake = FakeBloodHound()
    server = _serve(fake)
    record = _identity()
    try:
        record["checks"] = asyncio.run(asyncio.wait_for(_scenario(fake, server.server_address[1]), timeout=240))
    except Exception as exc:  # noqa: BLE001 - a crash is a failed check with its reason
        record["checks"] = [{"name": "check harness completed", "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:500]}]
    finally:
        server.shutdown()
    print(RESULT_MARKER + json.dumps(record, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
