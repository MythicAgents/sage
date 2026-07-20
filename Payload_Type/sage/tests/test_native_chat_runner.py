import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-live-runner" / "scripts" / "native_chat.py"
SPEC = importlib.util.spec_from_file_location("native_chat_runner", SCRIPT)
native_chat = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(native_chat)

RESTART_SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-live-runner" / "scripts" / "restart_sage_process.py"
RESTART_SPEC = importlib.util.spec_from_file_location("restart_sage_process", RESTART_SCRIPT)
restart_sage_process = importlib.util.module_from_spec(RESTART_SPEC)
assert RESTART_SPEC and RESTART_SPEC.loader
RESTART_SPEC.loader.exec_module(restart_sage_process)


def test_select_chat_resources_accepts_wildcard_token():
    container, token = native_chat.select_chat_resources(
        {
            "consuming_container": [
                {"id": 1, "container_running": True, "deleted": False}
            ],
            "apitokens": [
                {"id": 3, "active": True, "deleted": False, "scopes": ["*"]}
            ],
        }
    )

    assert container["id"] == 1
    assert token["id"] == 3


def test_select_chat_resources_fails_without_required_scope():
    with pytest.raises(RuntimeError, match="wildcard scope"):
        native_chat.select_chat_resources(
            {
                "consuming_container": [
                    {"id": 1, "container_running": True, "deleted": False}
                ],
                "apitokens": [
                    {
                        "id": 2,
                        "active": True,
                        "deleted": False,
                        "scopes": ["apitoken.write", "chat-ai.write"],
                    }
                ],
            }
        )


def test_ensure_api_token_creates_wildcard_for_autonomous_operations(monkeypatch):
    calls = []

    async def fake_query(client, query, variables=None):
        calls.append(variables)
        if variables is None:
            return {
                "consuming_container": [],
                "apitokens": [
                    {
                        "id": 2,
                        "active": True,
                        "deleted": False,
                        "scopes": ["apitoken.write", "chat-ai.write"],
                    }
                ],
            }
        return {
            "createAPIToken": {
                "id": 3,
                "name": "Sage native chat",
                "scopes": ["*"],
                "status": "success",
                "error": "",
            }
        }

    monkeypatch.setattr(native_chat.mythic, "execute_custom_query", fake_query)

    result = asyncio.run(native_chat.ensure_api_token(object()))

    assert result["created"] is True
    assert calls[-1]["scopes"] == ["*"]


def test_default_ai_metadata_is_autonomous(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'export provider="OpenAI"\nexport model="test-model"\nexport API_ENDPOINT="http://localhost/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(native_chat, "SAGE_ENV_PATH", env_file)

    metadata = native_chat.default_ai_metadata({"seed": 4})

    assert metadata["seed"] == 4
    assert metadata["config"]["mode"] == "auto"
    assert metadata["config"]["autonomous_solve"] is True
    assert metadata["config"]["max_steps"] == 0
    assert metadata["config"]["model"] == "test-model"
    assert metadata["config"]["provider"] == "openai"


def test_default_env_paths_derive_from_workspace_root():
    assert native_chat.DEFAULT_ENV_PATHS == (
        native_chat.WORKSPACE_ROOT / "mythic_v4" / ".env",
        native_chat.WORKSPACE_ROOT / "mythic" / ".env",
    )


def test_create_locked_channel_returns_sanitized_effective_chat_identity(monkeypatch):
    observed = {}

    async def fake_query(client, query, variables=None):
        if variables is None:
            return {
                "consuming_container": [{"id": 1, "container_running": True, "deleted": False}],
                "apitokens": [{"id": 2, "active": True, "deleted": False, "scopes": ["*"]}],
            }
        observed["metadata"] = variables["metadata"]
        return {"chatCreateChannel": {"status": "success", "error": "", "channel_id": 10}}

    monkeypatch.setattr(native_chat.mythic, "execute_custom_query", fake_query)

    result = asyncio.run(
        native_chat.create_locked_channel(
            object(),
            metadata={
                "config": {
                    "provider": "Bedrock",
                    "model": "test-model",
                    "API_ENDPOINT": "http://user:pass@127.0.0.1:8100/v1?secret=1",
                    "API_KEY": "secret",
                    "AWS_SECRET_ACCESS_KEY": "secret",
                }
            },
        )
    )

    assert observed["metadata"]["config"]["API_KEY"] == "secret"
    assert result["chat_runtime_identity"] == {
        "provider": "bedrock",
        "model": "test-model",
        "route": "http://127.0.0.1:8100/v1",
    }
    assert "secret" not in json.dumps(result["chat_runtime_identity"], sort_keys=True)


def test_run_native_chat_turn_returns_channel_and_request(monkeypatch):
    async def fake_channel(*args, **kwargs):
        return {"chat_channel_id": 10, "chat_channel_name": "seed", "api_token_id": 2}

    async def fake_message(*args, **kwargs):
        return {"chat_message_id": 20, "chat_request_id": 30}

    async def fake_wait(*args, **kwargs):
        return {
            "request": {"status": "complete", "error": ""},
            "messages": [{"message": "done"}],
        }

    async def no_prepared_channel(*args, **kwargs):
        return None

    monkeypatch.setattr(native_chat, "create_locked_channel", fake_channel)
    monkeypatch.setattr(native_chat, "find_prepared_channel", no_prepared_channel)
    monkeypatch.setattr(native_chat, "create_message", fake_message)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    result = asyncio.run(native_chat.run_native_chat_turn(object(), "objective"))

    assert result["chat_channel_id"] == 10
    assert result["chat_request_id"] == 30
    assert result["status"] == "complete"
    assert result["runtime_telemetry"] == {}


def test_run_native_chat_turn_emits_request_identity_and_progress(monkeypatch):
    events = []

    async def fake_channel(*args, **kwargs):
        return {"chat_channel_id": 10, "chat_channel_name": "seed", "api_token_id": 2}

    async def fake_message(*args, **kwargs):
        return {"chat_message_id": 20, "chat_request_id": 30}

    async def fake_wait(*args, **kwargs):
        kwargs["progress_sink"]({
            "event": "request_progress",
            "chat_request_id": 30,
            "status": "running",
            "message_count": 1,
        })
        return {
            "request": {"status": "complete", "error": ""},
            "messages": [{"message": "done"}],
        }

    async def no_prepared_channel(*args, **kwargs):
        return None

    monkeypatch.setattr(native_chat, "create_locked_channel", fake_channel)
    monkeypatch.setattr(native_chat, "find_prepared_channel", no_prepared_channel)
    monkeypatch.setattr(native_chat, "create_message", fake_message)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    asyncio.run(
        native_chat.run_native_chat_turn(
            object(),
            "objective",
            progress_sink=events.append,
        )
    )

    assert events[0] == {
        "event": "request_started",
        "chat_channel_id": 10,
        "chat_channel_name": "seed",
        "chat_request_id": 30,
    }
    assert events[1]["event"] == "request_progress"
    assert events[-1] == {
        "event": "request_terminal",
        "chat_channel_id": 10,
        "chat_request_id": 30,
        "status": "complete",
    }


def test_extract_runtime_telemetry_reads_terminal_metadata():
    telemetry = {
        "policy_mode": "llm",
        "semantic_transaction_count": 2,
        "authorized_transaction_count": 2,
        "semantic_policy_coverage": 1.0,
    }
    messages = [
        {"metadata": {"tool_use": {"status": "completed"}}},
        {"metadata": {"runtime_telemetry": telemetry}},
    ]

    assert native_chat.extract_runtime_telemetry(messages) == telemetry


def test_extract_runtime_telemetry_reads_error_wrapped_metadata():
    telemetry = {"policy_mode": "symbolic"}
    messages = [{
        "metadata": {
            "container_metadata": {
                "runtime_telemetry": telemetry,
            }
        }
    }]

    assert native_chat.extract_runtime_telemetry(messages) == telemetry


def test_native_chat_full_output_mode_remains_default():
    args = native_chat.build_parser().parse_args(["run"])

    assert args.output_mode == "full"


def test_native_chat_eval_view_omits_messages_and_raw_metadata():
    marker = "SAGE_TEST_SECRET_DO_NOT_USE"
    result = {
        "chat_channel_id": 10,
        "chat_channel_name": marker,
        "chat_request_id": 20,
        "status": "complete",
        "error": marker,
        "messages": [{"message": marker, "metadata": {"tool_use": {"arguments": marker}}}],
        "runtime_telemetry": {
            "policy_mode": "symbolic",
            "configured_policy_mode": "symbolic",
            "policy_identity_valid": True,
            "model_calls": 0,
            "semantic_transaction_count": 14,
            "authorized_transaction_count": 14,
            "kernel_singleton_count": 14,
            "decisions": [{"raw_response": marker}],
        },
    }

    view = native_chat.evaluator_result_view(result)

    assert view["schema"] == "native-chat-evaluator-result-v1"
    assert view["chat_channel_id"] == "10"
    assert view["chat_request_id"] == "20"
    assert view["error_present"] is True
    assert view["evaluator_evidence"]["runtime_telemetry"]["kernel_singleton_count"] == 14
    assert "messages" not in view
    assert marker not in json.dumps(view, sort_keys=True)


def test_build_demo_manifest_redacts_secrets_and_binds_artifact_hash(tmp_path):
    artifact = tmp_path / "proof.json"
    artifact.write_text('{"ok": true}\n', encoding="utf-8")
    result = {
        "chat_channel_id": 10,
        "chat_channel_name": "demo",
        "chat_request_id": 20,
        "status": "complete",
        "runtime_telemetry": {
            "transactions": [{
                "transaction_id": "tx-1",
                "child_tasks": [{"task_id": 7, "command": "whoami"}],
            }],
            "proof_lineage": [{"proof_id": "proof-1"}],
        },
        "chat_runtime_identity": {
            "provider": "openai",
            "model": "channel-model",
            "route": "http://127.0.0.1:8100/v1",
        },
    }

    manifest = native_chat.build_demo_manifest(
        result,
        runtime_identity={
            "provider": "openai",
            "model": "test-model",
            "route": "http://127.0.0.1:8100",
            "api_key": "secret-value",
        },
        startup_identity={
            "provider": "openai",
            "model": "test-model",
            "route": "http://127.0.0.1:8100",
            "api_key": "secret-value",
        },
        range_state={"identity": "range-4", "state": "callback-ready"},
        snapshot="clean-baseline",
        callback={"display_id": 7, "token": "secret"},
        artifact_paths=[artifact],
        readiness_snapshot={"ready": True},
    )

    assert manifest["schema"] == "sage-native-chat-demo-manifest-v1"
    assert manifest["chat"]["channel_id"] == 10
    assert manifest["range"]["snapshot"] == "clean-baseline"
    assert manifest["runtime_identity"]["model"] == "channel-model"
    assert manifest["startup_identity"]["model"] == "test-model"
    assert manifest["semantic_transactions"][0]["transaction_id"] == "tx-1"
    assert manifest["tasks"] == [{"task_id": 7, "command": "whoami"}]
    assert manifest["proofs"] == [{"proof_id": "proof-1"}]
    assert manifest["run_status_evidence"]["readiness_ready"] is True
    assert manifest["artifacts"][0]["sha256"]
    serialized = json.dumps(manifest, sort_keys=True)
    assert "secret-value" not in serialized
    assert "\"token\": \"<redacted>\"" in serialized


def test_build_demo_manifest_fails_closed_on_missing_artifact(tmp_path):
    with pytest.raises(FileNotFoundError):
        native_chat.build_demo_manifest(
            {"runtime_telemetry": {}},
            artifact_paths=[tmp_path / "missing.json"],
        )


def test_wait_for_request_emits_safe_heartbeat_and_metadata_changes(monkeypatch):
    calls = 0
    events = []
    clock = {"value": 0.0}

    def fake_monotonic():
        return clock["value"]

    async def fake_sleep(seconds):
        clock["value"] += seconds

    async def fake_query(client, query, variables=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "chat_request": [{"status": "running", "updated_at": "t1"}],
                "chat_message": [{
                    "metadata": {
                        "tool_use": {"tool_name": "collect_graph", "retry_count": 1},
                        "runtime_telemetry": {"current_operation": "collect-graph"},
                    }
                }],
            }
        if calls == 2:
            return {
                "chat_request": [{"status": "running", "updated_at": "t2"}],
                "chat_message": [{
                    "metadata": {
                        "tool_use": {"tool_name": "collect_graph", "retry_count": 1},
                        "runtime_telemetry": {"current_operation": "collect-graph"},
                    }
                }],
            }
        if calls == 3:
            return {
                "chat_request": [{"status": "running", "updated_at": "t3"}],
                "chat_message": [{
                    "metadata": {
                        "tool_use": {"tool_name": "collect_graph", "retry_count": 1},
                        "runtime_telemetry": {"current_operation": "collect-graph"},
                    }
                }],
            }
        if calls == 4:
            return {
                "chat_request": [{"status": "running", "updated_at": "t4"}],
                "chat_message": [{
                    "metadata": {
                        "tool_use": {"tool_name": "grant_rights", "retry_count": 2},
                        "runtime_telemetry": {"current_operation": "grant-rights"},
                    }
                }],
            }
        return {
            "chat_request": [{"status": "complete", "updated_at": "t5"}],
            "chat_message": [],
        }

    monkeypatch.setattr(native_chat.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(native_chat.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(native_chat.mythic, "execute_custom_query", fake_query)

    asyncio.run(
        native_chat.wait_for_request(
            object(),
            7,
            timeout_seconds=180,
            poll_interval_seconds=30,
            heartbeat_interval_seconds=60,
            progress_sink=events.append,
        )
    )

    assert events[0]["event"] == "request_progress"
    assert events[0]["current_operation"] == "collect-graph"
    assert events[0]["tool_name"] == "collect_graph"
    assert events[0]["retry_count"] == 1
    assert events[1]["event"] == "request_heartbeat"
    assert events[1]["elapsed_seconds"] == 60
    assert events[2]["event"] == "request_progress"
    assert events[2]["current_operation"] == "grant-rights"
    serialized = json.dumps(events, sort_keys=True)
    assert "arguments" not in serialized
    assert "result" not in serialized


def test_manifest_preflight_failure_blocks_message_creation(monkeypatch, tmp_path):
    async def fake_inspect(*args, **kwargs):
        return {"ready": False, "blockers": ["not ready"]}

    async def fail_create_message(*args, **kwargs):
        raise AssertionError("message creation must not run after readiness failure")

    monkeypatch.setattr(native_chat, "inspect_readiness", fake_inspect)
    monkeypatch.setattr(native_chat, "create_message", fail_create_message)

    with pytest.raises(RuntimeError, match="preflight failed"):
        asyncio.run(
            native_chat.run_native_chat_turn(
                object(),
                "objective",
                manifest_path=tmp_path / "manifest.json",
                runtime_dbs_archived=True,
            )
        )


def test_native_chat_parser_accepts_repeatable_artifact_paths():
    args = native_chat.build_parser().parse_args([
        "run",
        "--artifact-path",
        "one.json",
        "--artifact-path",
        "two.json",
    ])

    assert args.artifact_path == ["one.json", "two.json"]


def test_inspect_command_returns_nonzero_when_readiness_is_false(monkeypatch):
    observed = {}

    async def fake_login(**_kwargs):
        return object()

    async def fake_inspect(_client, *, api_token_id=None, runtime_dbs_archived=False):
        observed["runtime_dbs_archived"] = runtime_dbs_archived
        return {"ready": False, "blockers": ["not ready"]}

    monkeypatch.setattr(native_chat, "login", fake_login)
    monkeypatch.setattr(native_chat, "inspect_readiness", fake_inspect)

    args = native_chat.build_parser().parse_args(["inspect", "--runtime-dbs-archived"])
    rc = asyncio.run(native_chat._run(args))

    assert rc == 1
    assert observed["runtime_dbs_archived"] is True


def test_prepare_locked_channel_reuses_empty_prepared_channel(monkeypatch):
    async def fake_find(client):
        return {
            "chat_channel_id": 10,
            "chat_channel_name": "Sage GOAD Ready",
            "prepared": True,
            "reused": True,
        }

    async def fail_create(*args, **kwargs):
        raise AssertionError("existing prepared channel must be reused")

    monkeypatch.setattr(native_chat, "find_prepared_channel", fake_find)
    monkeypatch.setattr(native_chat, "create_locked_channel", fail_create)

    result = asyncio.run(native_chat.prepare_locked_channel(object()))

    assert result["chat_channel_id"] == 10
    assert result["reused"] is True


def test_run_native_chat_turn_prefers_prepared_channel(monkeypatch):
    calls = []

    async def fake_find(client):
        return {
            "chat_channel_id": 11,
            "chat_channel_name": "Sage GOAD Ready",
            "prepared": True,
            "reused": True,
        }

    async def fail_create(*args, **kwargs):
        raise AssertionError("prepared channel must be used")

    async def fake_message(client, channel_id, prompt):
        calls.append((channel_id, prompt))
        return {"chat_message_id": 20, "chat_request_id": 30}

    async def fake_wait(*args, **kwargs):
        return {"request": {"status": "complete", "error": ""}, "messages": []}

    monkeypatch.setattr(native_chat, "find_prepared_channel", fake_find)
    monkeypatch.setattr(native_chat, "create_locked_channel", fail_create)
    monkeypatch.setattr(native_chat, "create_message", fake_message)
    monkeypatch.setattr(native_chat, "wait_for_request", fake_wait)

    result = asyncio.run(native_chat.run_native_chat_turn(object(), "objective"))

    assert result["chat_channel_id"] == 11
    assert calls == [(11, "objective")]


def test_restart_sage_process_delegates_to_canonical_launcher(monkeypatch):
    calls = []

    class _Completed:
        returncode = 0

    def fake_run(argv, check=False):
        calls.append((argv, check))
        return _Completed()

    monkeypatch.setattr(restart_sage_process.subprocess, "run", fake_run)

    rc = restart_sage_process.main(["SAGE_ENGAGEMENT_GATE=1"])

    assert rc == 0
    assert calls == [(
        [
            "/bin/bash",
            str(restart_sage_process.CANONICAL_RESTART),
            "SAGE_ENGAGEMENT_GATE=1",
        ],
        False,
    )]
