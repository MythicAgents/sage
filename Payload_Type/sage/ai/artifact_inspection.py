"""Deterministic, offline artifact inspection through a pinned Docker image.

This module is an operator/control-plane broker, not an LLM tool. Output from a
workload is untrusted text; a help response is not proof of command semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Sequence


SCHEMA = 1
MAX_TOTAL = 32 * 1024 * 1024
CERTIFY_SHA256 = "1383e8bca90e4be70ff2224b7e2af465a8f3d5bede1fde60f1ece71e3b02d346"
RECIPES = {
    "static-v1": frozenset({"metadata"}),
    "certify-help-v1": frozenset({"top", "enum-cas", "enum-templates", "request", "request-download"}),
    "fixture-v1": frozenset({"native", "managed-com", "network-attempt", "output-flood",
                              "hang", "child-hang"}),
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class InspectionError(ValueError):
    """Caller contract or required runner capability is unavailable."""


@dataclass(frozen=True)
class Dependency:
    name: str
    content: bytes | Path
    sha256: str


@dataclass(frozen=True)
class Limits:
    wall_seconds: int = 30
    output_bytes: int = 131072  # per stream
    memory_mb: int = 512
    cpu: float = 1.0
    pids: int = 64
    tmpfs_mb: int = 64

    def validated(self) -> "Limits":
        if not (1 <= self.wall_seconds <= 120 and 1024 <= self.output_bytes <= 131072
                and 64 <= self.memory_mb <= 2048 and 0.25 <= self.cpu <= 2
                and 1 <= self.pids <= 128 and 16 <= self.tmpfs_mb <= 1024):
            raise InspectionError("inspection limits exceed reviewed bounds")
        return self


WINE_LIMITS = Limits(wall_seconds=120, output_bytes=131072, memory_mb=2048,
                     cpu=2, pids=128, tmpfs_mb=1024)


def _read(content: bytes | Path) -> bytes:
    if isinstance(content, bytes):
        data = content
    elif isinstance(content, Path):
        with content.open("rb") as artifact_file:
            data = artifact_file.read(MAX_TOTAL + 1)
    else:
        raise InspectionError("artifact must be bytes or a trusted Path")
    if len(data) > MAX_TOTAL:
        raise InspectionError("artifact exceeds 32 MiB")
    return data


def _prepare(artifact: bytes | Path, expected_sha256: str,
             dependencies: Sequence[Dependency]) -> tuple[list[dict], bytes]:
    if not _DIGEST.fullmatch(expected_sha256):
        raise InspectionError("expected_sha256 must be lowercase SHA256")
    entries = [("artifact.exe", artifact, expected_sha256)]
    if len(dependencies) > 15:
        raise InspectionError("too many dependencies")
    entries.extend((item.name, item.content, item.sha256) for item in dependencies)
    manifest: list[dict] = []
    chunks: list[bytes] = []
    seen: set[str] = set()
    total = 0
    for name, content, digest in entries:
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in {".", ".."}:
            raise InspectionError("invalid artifact name")
        if name.casefold() in seen:
            raise InspectionError("duplicate artifact name")
        seen.add(name.casefold())
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise InspectionError("invalid artifact digest")
        data = _read(content)
        total += len(data)
        if total > MAX_TOTAL:
            raise InspectionError("artifact bundle exceeds 32 MiB")
        if hashlib.sha256(data).hexdigest() != digest:
            raise InspectionError("artifact digest mismatch")
        manifest.append({"name": name, "size": len(data), "sha256": digest})
        chunks.append(data)
    header = json.dumps({"artifacts": manifest}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(header) > 8192:
        raise InspectionError("artifact manifest exceeds protocol limit")
    return manifest, header + b"".join(chunks)


def _docker(args: list[str], *, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], text=True, capture_output=True,
                          check=False, timeout=timeout)


def _check_image(image_id: str) -> str:
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise InspectionError("image must be an immutable sha256 image ID")
    try:
        proc = _docker(["image", "inspect", "--format", "{{.Id}} {{.Architecture}}", image_id])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError("Docker image inspection unavailable") from exc
    if proc.returncode:
        raise InspectionError("inspection image unavailable")
    parts = proc.stdout.strip().split()
    if len(parts) != 2 or parts[0] != image_id:
        raise InspectionError("inspection image identity mismatch")
    if parts[1] not in {"amd64", "arm64"}:
        raise InspectionError("unsupported inspection image architecture")
    try:
        daemon = _docker(["info", "--format", "{{.Architecture}}"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError("Docker daemon architecture unavailable") from exc
    if daemon.returncode or daemon.stdout.strip() not in {"amd64", "x86_64", "arm64", "aarch64"}:
        raise InspectionError("Docker daemon architecture unavailable")
    # Cross-architecture support belongs to the configured daemon. The actual
    # isolated container launch verifies whether a registered handler works.
    return parts[1]


def _capture(pipe, limit: int, output: bytearray, overflow: threading.Event) -> None:
    try:
        while True:
            chunk = pipe.read(16384)
            if not chunk:
                break
            remaining = limit - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
    finally:
        pipe.close()


def _run(image_id: str, architecture: str, profile: str, recipe: str, payload: bytes,
         limits: Limits) -> dict:
    create = ["create", "-i", "--platform", f"linux/{architecture}",
              "--network", "none", "--read-only", "--user", "10001:10001",
              "--log-driver", "none",
              "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
              "--tmpfs", f"/inspection/work:rw,exec,nosuid,nodev,size={limits.tmpfs_mb}m,uid=10001,gid=10001",
              "--workdir", "/inspection/work", "--memory", f"{limits.memory_mb}m",
              "--cpus", str(limits.cpu), "--pids-limit", str(limits.pids),
              image_id, "/usr/bin/python3", "/opt/inspection/entrypoint.py", profile, recipe]
    cid = None
    child = None
    threads: list[threading.Thread] = []
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    cleanup_error = None
    result = {"status": "launch_error", "exit_code": None, "timed_out": False,
              "truncated": False, "stdout": "", "stderr": ""}
    try:
        proc = _docker(create)
        if proc.returncode or not re.fullmatch(r"[0-9a-f]{64}\n?", proc.stdout):
            return result
        cid = proc.stdout.strip()
        child = subprocess.Popen(["docker", "start", "-ai", cid], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert child.stdin and child.stdout and child.stderr
        out_thread = threading.Thread(target=_capture, args=(child.stdout, limits.output_bytes, stdout, overflow), daemon=True)
        err_thread = threading.Thread(target=_capture, args=(child.stderr, limits.output_bytes, stderr, overflow), daemon=True)
        threads.extend((out_thread, err_thread))
        out_thread.start()
        err_thread.start()

        def send() -> None:
            try:
                child.stdin.write(payload)
                child.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        writer = threading.Thread(target=send, daemon=True)
        threads.append(writer)
        writer.start()
        deadline = time.monotonic() + limits.wall_seconds
        while child.poll() is None and not overflow.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        result["timed_out"] = child.poll() is None and not overflow.is_set()
        result["truncated"] = overflow.is_set()
        if child.poll() is None:
            _docker(["kill", cid], timeout=5)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        out_thread.join(timeout=2)
        err_thread.join(timeout=2)
        writer.join(timeout=2)
        # A short-lived process can exit before reader threads observe its
        # buffered flood. Record the limit after draining as well.
        result["truncated"] = overflow.is_set()
        result["stdout"] = stdout.decode("utf-8", "replace")
        result["stderr"] = stderr.decode("utf-8", "replace")
        inspect = _docker(["inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", cid])
        container_state = None
        if inspect.returncode == 0:
            try:
                container_state, code = inspect.stdout.strip().split()
                result["exit_code"] = int(code)
            except (ValueError, TypeError):
                pass
        if result["timed_out"]:
            result["status"] = "timeout"
        elif result["truncated"]:
            result["status"] = "output_limit"
        elif (container_state != "exited" or result["exit_code"] is None
              or (child.returncode and result["exit_code"] == 0)):
            result["status"] = "launch_error"
        elif result["exit_code"] == 0:
            result["status"] = "completed"
        else:
            result["status"] = "nonzero_exit"
    except (OSError, subprocess.TimeoutExpired):
        result["status"] = "launch_error"
    finally:
        if child is not None:
            if child.poll() is None:
                try:
                    child.terminate()
                    child.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        child.kill()
                        child.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        cleanup_error = "Docker attach process could not be stopped"
            for thread in threads:
                thread.join(timeout=1)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None and not pipe.closed:
                    try:
                        pipe.close()
                    except OSError:
                        cleanup_error = "Docker attach pipe could not be closed"
            for thread in threads:
                thread.join(timeout=1)
                if thread.is_alive():
                    cleanup_error = "Docker attach stream did not drain"
        result["stdout"] = stdout.decode("utf-8", "replace")
        result["stderr"] = stderr.decode("utf-8", "replace")
        result["truncated"] = result["truncated"] or overflow.is_set()
        if cid:
            try:
                removal = _docker(["rm", "-f", cid], timeout=5)
                if removal.returncode:
                    cleanup_error = "container removal failed"
            except (OSError, subprocess.TimeoutExpired):
                cleanup_error = "container removal failed"
        if cleanup_error:
            result["cleanup_error"] = cleanup_error
            if cid:
                result["orphan_container_id"] = cid
            result["status"] = "cleanup_error"
    return result


def _cache_key(manifest: list[dict], image_id: str, profile: str,
               recipe: str, limits: Limits) -> tuple[str, dict]:
    identity = {"schema": SCHEMA, "artifacts": manifest, "image_id": image_id,
                "profile": profile, "recipe": recipe, "limits": limits.__dict__}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return key, identity


def _valid_cached_result(result: object, manifest: list[dict], image_id: str,
                         profile: str, recipe: str, limits: Limits) -> bool:
    if not isinstance(result, dict):
        return False
    required = {"status", "exit_code", "timed_out", "truncated", "stdout", "stderr",
                "artifacts", "image_id", "image_architecture", "profile", "recipe",
                "evidence", "cache_hit"}
    if set(result) != required:
        return False
    status, exit_code = result["status"], result["exit_code"]
    if status not in {"completed", "nonzero_exit"} or type(exit_code) is not int:
        return False
    if (status == "completed") != (exit_code == 0):
        return False
    if result["timed_out"] is not False or result["truncated"] is not False:
        return False
    if any(not isinstance(result[field], str) or len(result[field]) > limits.output_bytes
           for field in ("stdout", "stderr")):
        return False
    return (result["artifacts"] == manifest and result["image_id"] == image_id
            and result["image_architecture"] in {"amd64", "arm64"}
            and result["profile"] == profile and result["recipe"] == recipe
            and result["evidence"] == "untrusted_inspection_output"
            and result["cache_hit"] is False)


def inspect_artifact(artifact: bytes | Path, *, expected_sha256: str, profile: str,
                     recipe: str, image_id: str, cache_dir: Path,
                     dependencies: Sequence[Dependency] = (), limits: Limits | None = None) -> dict:
    """Inspect exact bytes with a reviewed recipe; return untrusted bounded output.

    The cache only avoids rerunning an identical immutable image/byte bundle. Cache
    integrity is checked, but a local writer is not authenticated by this cache.
    """
    if profile not in RECIPES or recipe not in RECIPES[profile]:
        raise InspectionError("unsupported inspection profile or recipe")
    limits = (limits or (WINE_LIMITS if profile in {"certify-help-v1", "fixture-v1"} else Limits())).validated()
    manifest, payload = _prepare(artifact, expected_sha256, dependencies)
    if profile == "fixture-v1" and (manifest != [{"name": "artifact.exe", "size": 0,
                                                   "sha256": hashlib.sha256(b"").hexdigest()}]):
        raise InspectionError("fixture profile accepts only empty artifact and no dependencies")
    if profile == "certify-help-v1" and dependencies:
        raise InspectionError("Certify help profile does not accept external dependencies")
    if profile == "certify-help-v1" and manifest[0]["sha256"] != CERTIFY_SHA256:
        raise InspectionError("help profile requires enrolled Certify hash")
    architecture = _check_image(image_id)
    key, identity = _cache_key(manifest, image_id, profile, recipe, limits)
    cache_dir = Path(cache_dir)
    cache_file = cache_dir / f"{key}.json"
    try:
        with cache_file.open("rb") as cached:
            body = cached.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("oversized cache entry")
        envelope = json.loads(body)
        if (set(envelope) == {"identity", "result", "integrity"}
                and envelope["identity"] == identity
                and _valid_cached_result(envelope["result"], manifest, image_id,
                                         profile, recipe, limits)
                and envelope["integrity"] == hashlib.sha256(json.dumps(
                    {"identity": identity, "result": envelope["result"]},
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()):
            return {**envelope["result"], "cache_hit": True}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    result = _run(image_id, architecture, profile, recipe, payload, limits)
    result.update(artifacts=manifest, image_id=image_id, image_architecture=architecture,
                  profile=profile, recipe=recipe, evidence="untrusted_inspection_output", cache_hit=False)
    if result["status"] in {"completed", "nonzero_exit"}:
        cache_dir.mkdir(parents=True, exist_ok=True)
        core = {"identity": identity, "result": result}
        envelope = {**core, "integrity": hashlib.sha256(json.dumps(
            core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        fd, temporary = tempfile.mkstemp(prefix=f".{key}.", dir=cache_dir)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(envelope, out, sort_keys=True)
                out.write("\n")
            os.replace(temporary, cache_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return result
