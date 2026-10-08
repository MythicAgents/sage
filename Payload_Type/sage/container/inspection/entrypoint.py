"""Fixed inspection workload protocol. Runs only inside the isolated image."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from static_metadata import classify


MAX_HEADER = 8192
MAX_TOTAL = 32 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
CERTIFY_SHA256 = "1383e8bca90e4be70ff2224b7e2af465a8f3d5bede1fde60f1ece71e3b02d346"
RECIPES = {
    "top": ["help"],
    "enum-cas": ["enum-cas", "--help"],
    "enum-templates": ["enum-templates", "--help"],
    "request": ["request", "--help"],
    "request-download": ["request-download", "--help"],
}
FIXTURES = {
    "native": "/opt/inspection/fixtures/native.exe",
    "managed-com": "/opt/inspection/fixtures/managed-com.exe",
    "network-attempt": "/opt/inspection/fixtures/network-attempt.exe",
    "output-flood": "/opt/inspection/fixtures/output-flood.exe",
    "hang": "/opt/inspection/fixtures/hang.exe",
    "child-hang": "/opt/inspection/fixtures/child-hang.exe",
}


def receive(stream, work: Path) -> list[dict]:
    header = stream.readline(MAX_HEADER + 1)
    if len(header) > MAX_HEADER or not header.endswith(b"\n"):
        raise ValueError("invalid protocol header length")
    try:
        manifest = json.loads(header)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid protocol JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"artifacts"} or not isinstance(manifest["artifacts"], list):
        raise ValueError("invalid artifact manifest")
    artifacts = manifest["artifacts"]
    if not artifacts or len(artifacts) > 16 or not isinstance(artifacts[0], dict) or artifacts[0].get("name") != "artifact.exe":
        raise ValueError("missing primary artifact")
    names: set[str] = set()
    total = 0
    for entry in artifacts:
        if not isinstance(entry, dict) or set(entry) != {"name", "size", "sha256"}:
            raise ValueError("invalid artifact entry")
        name, size, digest = entry["name"], entry["size"], entry["sha256"]
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in {".", ".."}:
            raise ValueError("invalid artifact name")
        if name.casefold() in names:
            raise ValueError("duplicate artifact name")
        names.add(name.casefold())
        if type(size) is not int or size < 0 or size > MAX_TOTAL or total + size > MAX_TOTAL:
            raise ValueError("artifact size exceeds limit")
        total += size
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid artifact digest")
    work.mkdir(mode=0o700, exist_ok=True)
    for entry in artifacts:
        remaining = entry["size"]
        hasher = hashlib.sha256()
        with (work / entry["name"]).open("xb") as out:
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("truncated artifact")
                out.write(chunk)
                hasher.update(chunk)
                remaining -= len(chunk)
        if hasher.hexdigest() != entry["sha256"]:
            raise ValueError("artifact digest mismatch")
    if stream.read(1):
        raise ValueError("trailing protocol data")
    return artifacts


def run(profile: str, recipe: str, stream, work: Path) -> int:
    if profile == "static-v1" and recipe == "metadata":
        pass
    elif profile == "certify-help-v1" and recipe in RECIPES:
        pass
    elif profile == "fixture-v1" and recipe in FIXTURES:
        pass
    else:
        raise ValueError("unsupported inspection profile or recipe")
    artifacts = receive(stream, work)
    if profile == "static-v1":
        facts = classify((work / "artifact.exe").read_bytes())
        print(json.dumps(facts, sort_keys=True), flush=True)
        return 0
    if profile == "fixture-v1" and (len(artifacts) != 1 or artifacts[0]["size"] != 0
                                     or artifacts[0]["sha256"] != hashlib.sha256(b"").hexdigest()):
        raise ValueError("fixture profile accepts only empty artifact")
    if profile == "certify-help-v1" and len(artifacts) != 1:
        raise ValueError("Certify help profile does not accept dependencies")
    if profile == "certify-help-v1" and artifacts[0]["sha256"] != CERTIFY_SHA256:
        raise ValueError("help profile requires enrolled artifact hash")
    # The image owns this fixed executable path. No caller-supplied argv or shell.
    wine = "/usr/lib/wine/wine64"
    runtime = work / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    environment = {"HOME": str(work), "WINEPREFIX": str(work / "prefix"),
                   "WINEARCH": "win64", "WINEDEBUG": "-all", "PATH": "/usr/bin:/bin",
                   "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": str(runtime), "TMPDIR": str(runtime)}
    command = ([wine, FIXTURES[recipe]] if profile == "fixture-v1" else
               [wine, str(work / "artifact.exe"), *RECIPES[recipe]])
    completed = subprocess.run(command,
                               cwd=work, env=environment, check=False)
    return completed.returncode


def main() -> int:
    try:
        if len(sys.argv) != 3:
            raise ValueError("profile and recipe required")
        return run(sys.argv[1], sys.argv[2], sys.stdin.buffer, Path("/inspection/work"))
    except (OSError, ValueError) as exc:
        print(f"inspection_error: {exc}", file=sys.stderr, flush=True)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
