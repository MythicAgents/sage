#!/usr/bin/env python3
"""Test-only, one-response Certify `forge` (unenrolled, no alias) seed for a bound native Sage chat request.

The generated interpreter shim is selected through SAGE_VENV_PY for one local restart.
It passes ordinary Python invocations through and intercepts only `-u main.py`.
No product module, prompt, or provider configuration is edited.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import sys
import tempfile
import time

import httpx


SAGE_ROOT = Path(__file__).resolve().parents[3] / "Payload_Type/sage"
REAL_PYTHON = Path(__file__).resolve().parents[3] / ".venv/bin/python"
REQUEST_ID_RE = re.compile(r"\bchat:([1-9][0-9]*):request:([1-9][0-9]*)\b")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _private_json(path: Path) -> dict:
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("seed descriptor path must be absolute and regular")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        raw = os.read(fd, 65_537)
    finally:
        os.close(fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or not raw or len(raw) > 65_536):
        raise RuntimeError("seed descriptor must be a private bounded file")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("seed descriptor must be an object")
    return value


def _write_once(path: Path, value: dict, *, mode: int = 0o600) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("seed output path must be absolute")
    if path.parent.stat().st_mode & 0o077:
        raise RuntimeError("seed output directory is not private")
    fd, temporary_name = tempfile.mkstemp(prefix=".seed-record-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # link() fails if the destination already exists, and readers see only
        # the fully written file. rename() would silently replace a prior arm.
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _config(path: Path) -> dict:
    config = _private_json(path)
    if set(config) != {"schema", "expected_model", "endpoint_sha256", "prompt_sha256",
                       "callback_display_id", "arm_path", "seed_event_path", "arm_wait_seconds"}:
        raise RuntimeError("seed configuration keys differ from fixed contract")
    if config["schema"] != "sage-native-certify-seed-v1":
        raise RuntimeError("seed configuration schema differs")
    if not isinstance(config["expected_model"], str) or not config["expected_model"]:
        raise RuntimeError("seed model binding is missing")
    if not all(isinstance(config[key], str) and SHA_RE.fullmatch(config[key])
               for key in ("endpoint_sha256", "prompt_sha256")):
        raise RuntimeError("seed hash binding is invalid")
    if type(config["callback_display_id"]) is not int or config["callback_display_id"] <= 0:
        raise RuntimeError("seed callback identity is invalid")
    if type(config["arm_wait_seconds"]) is not int or not 1 <= config["arm_wait_seconds"] <= 30:
        raise RuntimeError("seed arm wait is outside the bound")
    for key in ("arm_path", "seed_event_path"):
        target = Path(config[key])
        if not target.is_absolute() or target.parent.stat().st_mode & 0o077:
            raise RuntimeError("seed scratch path is not private")
    if config["arm_path"] == config["seed_event_path"]:
        raise RuntimeError("seed arm and event paths overlap")
    return config


def _arm(config: dict) -> dict:
    value = _private_json(Path(config["arm_path"]))
    if set(value) != {"schema", "channel_id", "request_id", "prompt_sha256"}:
        raise RuntimeError("seed arm keys differ from fixed contract")
    if value["schema"] != "sage-native-certify-seed-arm-v1":
        raise RuntimeError("seed arm schema differs")
    if (type(value["channel_id"]) is not int or value["channel_id"] <= 0
            or type(value["request_id"]) is not int or value["request_id"] <= 0
            or value["prompt_sha256"] != config["prompt_sha256"]):
        raise RuntimeError("seed arm request or prompt identity differs")
    return value


def _request_binding(body: dict, request: httpx.Request, config: dict, arm: dict) -> str:
    if request.method != "POST" or not str(request.url).endswith("/chat/completions"):
        raise RuntimeError("seed provider transport boundary differs")
    endpoint = str(request.url)[:-len("/chat/completions")]
    if _sha(endpoint.encode()) != config["endpoint_sha256"] or body.get("model") != config["expected_model"]:
        raise RuntimeError("seed provider route or model differs")
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise RuntimeError("seed provider messages are malformed")
    expected_request = f"chat:{arm['channel_id']}:request:{arm['request_id']}"
    system = [str(m.get("content") or "") for m in messages
              if isinstance(m, dict) and m.get("role") == "system"]
    user = [str(m.get("content") or "") for m in messages
            if isinstance(m, dict) and m.get("role") == "user"]
    observed_ids = set(REQUEST_ID_RE.findall(" ".join(system)))
    # The native supervised projection contains the exact turn_id as text. Guard
    # against a different request even when the system prompt format evolves.
    if expected_request not in " ".join(system):
        raise RuntimeError("seed native request identity is absent from provider wire")
    if not any(_sha(text.encode()) == config["prompt_sha256"] for text in user):
        raise RuntimeError("seed operator prompt identity is absent from provider wire")
    if observed_ids != {(str(arm["channel_id"]), str(arm["request_id"]))}:
        raise RuntimeError("seed native request identity differs")
    operator_role = any("You are the **Mythic Operator**" in text for text in system)
    offered = {tool.get("function", {}).get("name") for tool in body.get("tools", [])
               if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
    if "issue_task_and_waitfor_task_output" in offered and not operator_role:
        raise RuntimeError("seed Operator role marker is absent from guarded tool wire")
    return "Mythic_Operator" if operator_role else "other"


def _seed_response(request: httpx.Request, model: str, stream: bool,
                   callback_display_id: int) -> httpx.Response:
    seed_args = {"callback_display_id": callback_display_id,
                 "command": "execute_assembly",
                 "parameters": {"assembly_name": "Certify.exe", "assembly_arguments": "forge"}}
    tool = {"id": "synthetic-invalid-certify-forge", "type": "function",
            "function": {"name": "issue_task_and_waitfor_task_output",
                         "arguments": json.dumps(seed_args, sort_keys=True)}}
    if stream:
        chunks = [
            {"id": "synthetic-native-certify-forge", "object": "chat.completion.chunk",
             "created": 0, "model": model, "choices": [{"index": 0,
                "delta": {"role": "assistant", "tool_calls": [{"index": 0, **tool}]},
                "finish_reason": None}]},
            {"id": "synthetic-native-certify-forge", "object": "chat.completion.chunk",
             "created": 0, "model": model, "choices": [{"index": 0, "delta": {},
                "finish_reason": "tool_calls"}]},
            {"id": "synthetic-native-certify-forge", "object": "chat.completion.chunk",
             "created": 0, "model": model, "choices": [],
             "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
        ]
        content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(200, request=request,
                              content=(content + "data: [DONE]\n\n").encode(),
                              headers={"content-type": "text/event-stream"})
    return httpx.Response(200, request=request, json={
        "id": "synthetic-native-certify-forge", "object": "chat.completion",
        "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": None,
                                 "tool_calls": [tool]}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})


class OneCertifySeed(httpx.AsyncBaseTransport):
    """A pre-meter response gate; after one seed the original meter owns every call."""

    def __init__(self, original_meter: httpx.AsyncBaseTransport, config: dict):
        self.original_meter = original_meter
        self.config = config
        self.seeded = Path(config["seed_event_path"]).exists()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(await request.aread())
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("seed provider request is not JSON") from exc
        deadline = time.monotonic() + self.config["arm_wait_seconds"]
        while not Path(self.config["arm_path"]).exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if not Path(self.config["arm_path"]).exists():
            raise RuntimeError("seed arm record was not installed before provider dispatch")
        arm = _arm(self.config)
        role = _request_binding(body, request, self.config, arm)
        if role != "Mythic_Operator" or self.seeded:
            return await self.original_meter.handle_async_request(request)
        offered = {tool.get("function", {}).get("name") for tool in body.get("tools", [])
                   if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
        if "issue_task_and_waitfor_task_output" not in offered:
            raise RuntimeError("seed guarded issue tool is not on Operator wire")
        stream = body.get("stream") is True
        if body.get("stream") not in (True, False, None):
            raise RuntimeError("seed provider stream mode is malformed")
        _write_once(Path(self.config["seed_event_path"]), {
            "schema": "sage-native-certify-seed-event-v1", "kind": "local_fault_injection",
            "channel_id": arm["channel_id"], "request_id": arm["request_id"],
            "prompt_sha256": self.config["prompt_sha256"],
            "model": body["model"], "endpoint_sha256": self.config["endpoint_sha256"],
            "tool": "issue_task_and_waitfor_task_output",
            "arguments": {"callback_display_id": self.config["callback_display_id"],
                          "command": "execute_assembly",
                          "parameters": {"assembly_name": "Certify.exe",
                                         "assembly_arguments": "forge"}},
            "stream": stream, "controller_monotonic_ns": time.monotonic_ns(),
            "provider_reservation": None,
        })
        self.seeded = True
        return _seed_response(request, body["model"], stream,
                              self.config["callback_display_id"])

    async def aclose(self) -> None:
        await self.original_meter.aclose()


def install(config: dict) -> None:
    from ai import resource_http
    original = resource_http.MeteredAsyncHTTPTransport

    def construct(*args, **kwargs):
        meter = original(*args, **kwargs)
        return OneCertifySeed(meter, config) if args and args[0] == "provider" else meter

    resource_http.MeteredAsyncHTTPTransport = construct


def launch(config_path: Path, *, main_path: Path = SAGE_ROOT / "main.py") -> None:
    config = _config(config_path)
    if Path(config["seed_event_path"]).exists():
        raise RuntimeError("seed event already exists before Sage launch")
    if Path.cwd() != SAGE_ROOT or main_path.resolve() != SAGE_ROOT / "main.py":
        raise RuntimeError("native seed launcher must run the unchanged Sage main.py")
    sys.path.insert(0, str(SAGE_ROOT))
    install(config)
    sys.argv = ["main.py"]
    runpy.run_path(str(main_path), run_name="__main__")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    shim = sub.add_parser("make-shim")
    shim.add_argument("--config", type=Path, required=True)
    shim.add_argument("--output", type=Path, required=True)
    arm = sub.add_parser("arm")
    arm.add_argument("--config", type=Path, required=True)
    arm.add_argument("--channel-id", type=int, required=True)
    arm.add_argument("--request-id", type=int, required=True)
    arm.add_argument("--prompt-file", type=Path, required=True)
    start = sub.add_parser("launch")
    start.add_argument("--config", type=Path, required=True)
    start.add_argument("main_marker", choices=("main.py",))
    args = parser.parse_args()
    if args.action == "make-shim":
        _config(args.config)
        script = (f"#!{REAL_PYTHON}\nimport os,sys\n"
                  f"real={str(REAL_PYTHON)!r}\n"
                  f"if sys.argv[1:]==['-u','main.py']:\n"
                  f" os.execv(real,[real,'-u',{str(Path(__file__).resolve())!r},'launch','--config',{str(args.config)!r},'main.py'])\n"
                  f"os.execv(real,[real,*sys.argv[1:]])\n")
        if not args.output.is_absolute() or args.output.parent.stat().st_mode & 0o077:
            raise RuntimeError("shim must be created in private absolute directory")
        fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(script)
            stream.flush()
            os.fsync(stream.fileno())
        return 0
    if args.action == "arm":
        config = _config(args.config)
        if (type(args.channel_id) is not int or args.channel_id <= 0
                or type(args.request_id) is not int or args.request_id <= 0):
            raise RuntimeError("seed channel/request identity is invalid")
        prompt = args.prompt_file.read_text(encoding="utf-8").strip()
        if _sha(prompt.encode()) != config["prompt_sha256"]:
            raise RuntimeError("seed prompt file differs from configured prompt")
        _write_once(Path(config["arm_path"]), {
            "schema": "sage-native-certify-seed-arm-v1", "channel_id": args.channel_id,
            "request_id": args.request_id, "prompt_sha256": config["prompt_sha256"],
        })
        return 0
    launch(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
