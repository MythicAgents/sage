"""Inert producer/transport checks for the one native Certify fault seed."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-focused-capability-tests/scripts/native_certify_seed.py"
SAGE = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("native_certify_seed_test", SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def fixture(tmp_path):
    seed = module()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    prompt = "Diagnostic Certify request unique to this native turn."
    config = {"schema": "sage-native-certify-seed-v1",
              "expected_model": "inert-model",
              "endpoint_sha256": hashlib.sha256(b"https://model.test/v1").hexdigest(),
              "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
              "callback_display_id": 7,
              "arm_path": str(private / "arm.json"),
              "seed_event_path": str(private / "seed-event.json"),
              "arm_wait_seconds": 1}
    config_path = private / "config.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    arm = {"schema": "sage-native-certify-seed-arm-v1", "channel_id": 4,
           "request_id": 5, "prompt_sha256": config["prompt_sha256"]}
    seed._write_once(Path(config["arm_path"]), arm)
    return seed, config, config_path, prompt


def request(prompt, *, role="operator", request_id="chat:4:request:5", stream=False,
            model="inert-model", offered=True):
    system = ("You are the **Mythic Operator**. " if role == "operator" else "You are a Supervisor. ")
    system += f"Native turn_id={request_id}; enforce the exact request."
    body = {"model": model, "stream": stream,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "tools": ([{"type": "function", "function": {
                "name": "issue_task_and_waitfor_task_output", "parameters": {"type": "object"}}}]
                if offered and role != "supervisor" else [])}
    return httpx.Request("POST", "https://model.test/v1/chat/completions", json=body)


class FakeMeter(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []
        self.closed = False

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, request=request, json={"metered": True})

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("stream", [False, True])
def test_exact_operator_response_seeded_once_before_meter_then_delegate(tmp_path, stream):
    seed, config, _, prompt = fixture(tmp_path)
    meter = FakeMeter()
    transport = seed.OneCertifySeed(meter, seed._config(tmp_path / "private/config.json"))

    async def exercise():
        supervisor = await transport.handle_async_request(request(prompt, role="supervisor"))
        first = await transport.handle_async_request(request(prompt, stream=stream))
        later = await transport.handle_async_request(request(prompt, stream=stream))
        await transport.aclose()
        return supervisor, first, later

    supervisor, first, later = asyncio.run(exercise())
    assert supervisor.json() == {"metered": True}
    assert later.json() == {"metered": True}
    assert len(meter.requests) == 2 and meter.closed
    body = first.text
    assert "issue_task_and_waitfor_task_output" in body
    assert '\\"forge\\"' in body
    assert "synthetic-native-certify-forge" in body
    event = seed._private_json(Path(config["seed_event_path"]))
    assert event["channel_id"] == 4 and event["request_id"] == 5
    assert event["arguments"]["callback_display_id"] == 7
    assert event["provider_reservation"] is None
    assert event["stream"] is stream


@pytest.mark.parametrize("change", ["request", "prompt", "model", "missing_tool", "role_marker"])
def test_mismatch_fails_before_seed_or_meter(tmp_path, change):
    seed, config, _, prompt = fixture(tmp_path)
    meter = FakeMeter()
    transport = seed.OneCertifySeed(meter, config)
    kwargs = {}
    if change == "request":
        kwargs["request_id"] = "chat:4:request:6"
    elif change == "prompt":
        prompt += " near match"
    elif change == "model":
        kwargs["model"] = "other-model"
    elif change == "missing_tool":
        kwargs["offered"] = False
    else:
        kwargs["role"] = "unmarked_operator"
        kwargs["offered"] = True
    with pytest.raises(RuntimeError):
        asyncio.run(transport.handle_async_request(request(prompt, **kwargs)))
    assert meter.requests == []
    assert not Path(config["seed_event_path"]).exists()


def test_arm_cli_and_executable_shim_passthrough_are_local(tmp_path):
    seed, config, config_path, prompt = fixture(tmp_path)
    Path(config["arm_path"]).unlink()
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt + "\n")
    env = dict(os.environ)
    arm = subprocess.run([sys.executable, str(SCRIPT), "arm", "--config", str(config_path),
                          "--channel-id", "4", "--request-id", "5",
                          "--prompt-file", str(prompt_file)],
                         capture_output=True, text=True, env=env, check=True)
    assert arm.stdout == ""
    assert seed._private_json(Path(config["arm_path"]))["request_id"] == 5
    shim = tmp_path / "private/python-shim"
    subprocess.run([sys.executable, str(SCRIPT), "make-shim", "--config", str(config_path),
                    "--output", str(shim)], check=True, capture_output=True, env=env)
    result = subprocess.run([str(shim), "-c", "print('inert shim')"], check=True,
                            capture_output=True, text=True, env=env)
    assert result.stdout.strip() == "inert shim"
    assert not Path(config["seed_event_path"]).exists()


def test_seed_record_publishes_complete_bytes_exclusively(tmp_path, monkeypatch):
    seed, config, _, _ = fixture(tmp_path)
    destination = Path(config["seed_event_path"])
    original_dump = seed.json.dump

    def inspect_partial(value, stream, **kwargs):
        stream.write('{"incomplete":')
        stream.flush()
        os.fsync(stream.fileno())
        assert not destination.exists()
        stream.seek(0)
        stream.truncate()
        return original_dump(value, stream, **kwargs)

    monkeypatch.setattr(seed.json, "dump", inspect_partial)
    seed._write_once(destination, {"complete": True})
    assert seed._private_json(destination) == {"complete": True}
    monkeypatch.setattr(seed.json, "dump", original_dump)
    with pytest.raises(FileExistsError):
        seed._write_once(destination, {"complete": False})
    assert seed._private_json(destination) == {"complete": True}
    assert list(destination.parent.glob(".seed-record-*")) == []


def test_bound_launcher_executes_only_inert_local_main_in_subprocess(tmp_path):
    seed, config, config_path, _ = fixture(tmp_path)
    fake_sage = tmp_path / "private/fake-sage"
    fake_sage.mkdir(mode=0o700)
    (fake_sage / "main.py").write_text("print('inert main reached')\n")
    # The test substitutes only the module's root in a child process. The CLI
    # entry point still requires the fixed repository main.py in real use.
    code = ("import importlib.util,pathlib; "
            f"p={str(SCRIPT)!r};s=importlib.util.spec_from_file_location('seed',p);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            f"m.SAGE_ROOT=pathlib.Path({str(fake_sage)!r});"
            f"m.launch(pathlib.Path({str(config_path)!r}),main_path=m.SAGE_ROOT/'main.py')")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SAGE)
    result = subprocess.run([sys.executable, "-c", code], cwd=fake_sage,
                            capture_output=True, text=True, env=env, check=True)
    assert result.stdout.strip() == "inert main reached"
    assert not Path(config["seed_event_path"]).exists()


@pytest.mark.parametrize("stream", [False, True])
def test_openai_sdk_consumes_seeded_native_tool_call_without_network(tmp_path, stream):
    seed, config, _, prompt = fixture(tmp_path)
    meter = FakeMeter()
    transport = seed.OneCertifySeed(meter, config)
    client = httpx.AsyncClient(transport=transport)
    model = ChatOpenAI(model="inert-model", api_key="inert", base_url="https://model.test/v1",
                       http_async_client=client, max_retries=0)
    tool = {"name": "issue_task_and_waitfor_task_output", "description": "Inert schema",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string"}, "callback_display_id": {"type": "integer"},
                "parameters": {"type": "object"}},
                "required": ["command", "callback_display_id", "parameters"]}}
    bound = model.bind_tools([tool])
    messages = [SystemMessage(content="You are the **Mythic Operator**. "
                              "Native turn_id=chat:4:request:5; enforce exact request."),
                HumanMessage(content=prompt)]

    async def exercise():
        if stream:
            chunks = [chunk async for chunk in bound.astream(messages)]
            result = chunks[0]
            for chunk in chunks[1:]:
                result += chunk
        else:
            result = await bound.ainvoke(messages)
        await client.aclose()
        return result

    result = asyncio.run(exercise())
    assert result.tool_calls == [{"name": "issue_task_and_waitfor_task_output",
                                  "args": {"callback_display_id": 7,
                                           "command": "execute_assembly",
                                           "parameters": {"assembly_name": "Certify.exe",
                                                          "assembly_arguments": "forge"}},
                                  "id": "synthetic-invalid-certify-forge", "type": "tool_call"}]
    assert meter.requests == []
    assert Path(config["seed_event_path"]).is_file()
