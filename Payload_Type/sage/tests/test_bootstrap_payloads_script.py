import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-callback-bootstrap" / "scripts" / "bootstrap_payloads.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_payloads", SCRIPT)
bootstrap_payloads = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bootstrap_payloads)

SAGE_TASK_SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-live-runner" / "scripts" / "sage_task.py"
SAGE_TASK_SPEC = importlib.util.spec_from_file_location("sage_task", SAGE_TASK_SCRIPT)
sage_task = importlib.util.module_from_spec(SAGE_TASK_SPEC)
assert SAGE_TASK_SPEC and SAGE_TASK_SPEC.loader
SAGE_TASK_SPEC.loader.exec_module(sage_task)

RUN_ESSOS_SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-live-runner" / "scripts" / "run_essos_da.py"
RUN_ESSOS_SPEC = importlib.util.spec_from_file_location("run_essos_da", RUN_ESSOS_SCRIPT)
run_essos_da = importlib.util.module_from_spec(RUN_ESSOS_SPEC)
assert RUN_ESSOS_SPEC and RUN_ESSOS_SPEC.loader
RUN_ESSOS_SPEC.loader.exec_module(run_essos_da)


def test_sage_build_parameters_skip_empty_values():
    args = argparse.Namespace(
        provider="Bedrock",
        model="",
        api_endpoint="",
        api_key="",
        aws_access_key_id="AKIA",
        aws_secret_access_key="secret",
        aws_session_token="",
        aws_default_region="us-east-1",
    )

    assert bootstrap_payloads.sage_build_parameters(args) == [
        {"name": "provider", "value": "Bedrock"},
        {"name": "AWS_ACCESS_KEY_ID", "value": "AKIA"},
        {"name": "AWS_SECRET_ACCESS_KEY", "value": "secret"},
        {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"},
    ]


def test_skill_env_loader_sets_sage_defaults_without_overriding(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# local Sage payload defaults",
                "SAGE_PROVIDER=OpenAI",
                "SAGE_MODEL=gpt-5.5-cyber-preview",
                "SAGE_API_ENDPOINT=http://127.0.0.1:8100/v1",
                "SAGE_API_KEY='dummy-key'",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("SAGE_PROVIDER", raising=False)
    monkeypatch.delenv("SAGE_API_ENDPOINT", raising=False)
    monkeypatch.delenv("SAGE_API_KEY", raising=False)
    monkeypatch.setenv("SAGE_MODEL", "shell-override")

    loaded = bootstrap_payloads.load_env_file(env_file)

    assert loaded == {
        "SAGE_PROVIDER": "OpenAI",
        "SAGE_MODEL": "gpt-5.5-cyber-preview",
        "SAGE_API_ENDPOINT": "http://127.0.0.1:8100/v1",
        "SAGE_API_KEY": "dummy-key",
    }
    assert os.environ["SAGE_PROVIDER"] == "OpenAI"
    assert os.environ["SAGE_MODEL"] == "shell-override"
    assert os.environ["SAGE_API_ENDPOINT"] == "http://127.0.0.1:8100/v1"
    assert os.environ["SAGE_API_KEY"] == "dummy-key"


def test_sage_arg_defaults_use_loaded_skill_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SAGE_PROVIDER=OpenAI\n"
        "SAGE_MODEL=gpt-5.5-cyber-preview\n"
        "SAGE_API_ENDPOINT=http://127.0.0.1:8100/v1\n"
        "SAGE_API_KEY=dummy-key\n",
        encoding="utf-8",
    )
    for key in ("SAGE_PROVIDER", "SAGE_MODEL", "SAGE_API_ENDPOINT", "SAGE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    bootstrap_payloads.load_env_file(env_file)
    parser = argparse.ArgumentParser()

    bootstrap_payloads.add_sage_args(parser)
    args = parser.parse_args([])

    assert bootstrap_payloads.sage_build_parameters(args)[:4] == [
        {"name": "provider", "value": "OpenAI"},
        {"name": "model", "value": "gpt-5.5-cyber-preview"},
        {"name": "API_ENDPOINT", "value": "http://127.0.0.1:8100/v1"},
        {"name": "API_KEY", "value": "dummy-key"},
    ]


def test_apollo_arg_defaults_use_loaded_skill_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APOLLO_CALLBACK_HOST=http://100.64.0.1\n"
        "APOLLO_CALLBACK_PORT=8080\n"
        "APOLLO_CALLBACK_INTERVAL=5\n"
        "APOLLO_CALLBACK_JITTER=17\n"
        "APOLLO_AESPSK=none\n"
        "APOLLO_GET_URI=hello\n"
        "APOLLO_POST_URI=submit\n"
        "APOLLO_QUERY_PATH_NAME=id\n"
        "APOLLO_OUTPUT_TYPE=Shellcode\n"
        "APOLLO_ADJUST_FILENAME=true\n"
        "APOLLO_DEBUG=true\n"
        "APOLLO_DOWNLOAD_DIR=/payloads\n",
        encoding="utf-8",
    )
    keys = [
        "APOLLO_CALLBACK_HOST",
        "APOLLO_CALLBACK_PORT",
        "APOLLO_CALLBACK_INTERVAL",
        "APOLLO_CALLBACK_JITTER",
        "APOLLO_AESPSK",
        "APOLLO_GET_URI",
        "APOLLO_POST_URI",
        "APOLLO_QUERY_PATH_NAME",
        "APOLLO_OUTPUT_TYPE",
        "APOLLO_ADJUST_FILENAME",
        "APOLLO_DEBUG",
        "APOLLO_DOWNLOAD_DIR",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    bootstrap_payloads.load_env_file(env_file)
    parser = argparse.ArgumentParser()

    bootstrap_payloads.add_apollo_args(parser)
    args = parser.parse_args([])

    assert args.callback_host == "http://100.64.0.1"
    assert args.callback_port == 8080
    assert args.callback_interval == 5
    assert args.callback_jitter == 17
    assert args.aespsk == "none"
    assert args.get_uri == "hello"
    assert args.post_uri == "submit"
    assert args.query_path_name == "id"
    assert args.output_type == "Shellcode"
    assert args.adjust_filename is True
    assert args.debug is True
    assert args.download_dir == "/payloads"


def test_apollo_arg_default_filename_is_apollo_exe(monkeypatch):
    monkeypatch.delenv("APOLLO_FILENAME", raising=False)
    parser = argparse.ArgumentParser()

    bootstrap_payloads.add_apollo_args(parser)
    args = parser.parse_args([])

    assert args.apollo_filename == "apollo.exe"


def test_callback_config_export_document_round_trips_with_owner_only_permissions(tmp_path):
    path = tmp_path / "apollo_callback_config.json"

    document = bootstrap_payloads.write_callback_config(
        path,
        {
            "status": "success",
            "error": None,
            "agent_callback_id": "callback-uuid",
            "config": '{"uuid":"payload-uuid","key":"secret"}',
        },
    )

    assert document["agent_callback_id"] == "callback-uuid"
    assert bootstrap_payloads.load_callback_config(path) == {
        "uuid": "payload-uuid",
        "key": "secret",
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_export_callback_config_resolves_display_id_and_uses_graphql_variables(monkeypatch):
    calls = []

    async def fake_query(client, query, variables=None):
        calls.append((query, variables))
        if "CallbackIdentity" in query:
            return {
                "callback": [
                    {"display_id": 7, "agent_callback_id": "callback-uuid"}
                ]
            }
        return {
            "exportCallbackConfig": {
                "status": "success",
                "error": None,
                "agent_callback_id": "callback-uuid",
                "config": '{"uuid":"payload-uuid"}',
            }
        }

    monkeypatch.setattr(bootstrap_payloads.mythic, "execute_custom_query", fake_query)

    result = asyncio.run(bootstrap_payloads.export_callback_config(object(), "7"))

    assert result["agent_callback_id"] == "callback-uuid"
    assert calls[0][1] == {"displayId": 7}
    assert calls[1][1] == {"agentCallbackId": "callback-uuid"}


def test_import_callback_config_passes_jsonb_object(monkeypatch):
    observed = {}

    async def fake_query(client, query, variables=None):
        observed["query"] = query
        observed["variables"] = variables
        return {
            "importCallbackConfig": {
                "status": "success",
                "error": None,
            }
        }

    monkeypatch.setattr(bootstrap_payloads.mythic, "execute_custom_query", fake_query)

    result = asyncio.run(
        bootstrap_payloads.import_callback_config(
            object(),
            '{"uuid":"payload-uuid","key":"secret"}',
        )
    )

    assert result == {"status": "success", "error": None}
    assert observed["variables"] == {
        "config": {"uuid": "payload-uuid", "key": "secret"}
    }
    assert "$config: jsonb!" in observed["query"]


def test_callback_config_payload_type_reads_exported_payload_type():
    assert bootstrap_payloads.callback_config_payload_type({
        "payload_type": {"name": "Merlin"},
    }) == "merlin"
    assert bootstrap_payloads.callback_config_payload_type({"payload_type": {}}) is None


def test_bootstrap_reset_imports_baked_apollo_only_when_explicitly_requested(
    monkeypatch,
    tmp_path,
    capsys,
):
    config_path = tmp_path / "apollo_callback_config.json"
    config_path.write_text(
        json.dumps({"config": {"uuid": "payload-uuid", "key": "secret"}}),
        encoding="utf-8",
    )
    calls = []

    async def fake_login(args):
        return object()

    async def fake_import(client, config):
        calls.append(("import", config))
        return {"status": "success", "error": None}

    async def fake_create_sage(client, args):
        calls.append(("sage", None))
        return {"build_phase": "success", "uuid": "sage-uuid"}

    async def fail_create_apollo(client, args):
        raise AssertionError("Apollo payload creation must be skipped")

    async def fake_preflight(client, *, timeout_seconds, max_skew_seconds):
        calls.append(("preflight", timeout_seconds, max_skew_seconds))
        return {"ready": True}

    async def fake_query(client, query, variables=None):
        return {
            "callback": [],
            "consuming_container": [
                {"id": 1, "container_running": True, "deleted": False}
            ],
        }

    monkeypatch.setattr(bootstrap_payloads, "login", fake_login)
    monkeypatch.setattr(bootstrap_payloads, "import_callback_config", fake_import)
    monkeypatch.setattr(bootstrap_payloads, "create_sage", fake_create_sage)
    monkeypatch.setattr(bootstrap_payloads, "create_apollo", fail_create_apollo)
    monkeypatch.setattr(bootstrap_payloads, "post_callback_preflight", fake_preflight)
    monkeypatch.setattr(bootstrap_payloads.mythic, "execute_custom_query", fake_query)

    asyncio.run(
        bootstrap_payloads.command_bootstrap_reset(
            argparse.Namespace(
                callback_config=str(config_path),
                use_baked_apollo=True,
                post_callback_timeout=180,
                max_clock_skew_seconds=60.0,
            )
        )
    )
    output = json.loads(capsys.readouterr().out)

    assert calls == [
        ("import", {"uuid": "payload-uuid", "key": "secret"}),
        ("preflight", 180, 60.0),
    ]
    assert output["mode"] == "legacy-imported-baked-apollo"
    assert output["post_callback_preflight"]["ready"] is True
    assert "apollo" not in output


def test_bootstrap_reset_imports_retained_merlin_without_creating_apollo(
    monkeypatch,
    tmp_path,
    capsys,
):
    config_path = tmp_path / "merlin_callback_config.json"
    config_path.write_text(
        json.dumps({
            "config": {
                "uuid": "payload-uuid",
                "key": "secret",
                "payload_type": {"name": "merlin"},
            }
        }),
        encoding="utf-8",
    )
    calls = []

    async def fake_login(args):
        return object()

    async def fake_import(client, config):
        calls.append(("import", config))
        return {"status": "success", "error": None}

    async def fake_create_sage(client, args):
        calls.append(("sage", None))
        return {"build_phase": "success", "uuid": "sage-uuid"}

    async def fail_create_apollo(client, args):
        raise AssertionError("Apollo payload creation must be skipped")

    async def fail_preflight(client, *, timeout_seconds, max_skew_seconds):
        raise AssertionError("Apollo post-callback preflight must be skipped")

    async def fake_query(client, query, variables=None):
        return {
            "callback": [],
            "consuming_container": [
                {"id": 1, "container_running": True, "deleted": False}
            ],
        }

    monkeypatch.setattr(bootstrap_payloads, "login", fake_login)
    monkeypatch.setattr(bootstrap_payloads, "import_callback_config", fake_import)
    monkeypatch.setattr(bootstrap_payloads, "create_sage", fake_create_sage)
    monkeypatch.setattr(bootstrap_payloads, "create_apollo", fail_create_apollo)
    monkeypatch.setattr(bootstrap_payloads, "post_callback_preflight", fail_preflight)
    monkeypatch.setattr(bootstrap_payloads.mythic, "execute_custom_query", fake_query)

    asyncio.run(
        bootstrap_payloads.command_bootstrap_reset(
            argparse.Namespace(
                callback_config=None,
                retained_callback_config=str(config_path),
                use_baked_apollo=False,
                use_retained_callback=True,
            )
        )
    )
    output = json.loads(capsys.readouterr().out)

    assert calls == [
        ("import", {
            "uuid": "payload-uuid",
            "key": "secret",
            "payload_type": {"name": "merlin"},
        }),
    ]
    assert output["mode"] == "imported-retained-callback"
    assert output["retained_payload_type"] == "merlin"
    assert output["retained_callback_config"] == str(config_path)
    assert output["retained_callback_bootstrap"]["payload_type"] == "merlin"
    assert "apollo" not in output


def test_bootstrap_reset_rejects_conflicting_retained_and_baked_modes(monkeypatch):
    async def fail_login(args):
        raise AssertionError("Validation must fail before Mythic login")

    monkeypatch.setattr(bootstrap_payloads, "login", fail_login)

    with pytest.raises(ValueError, match="mutually exclusive"):
        asyncio.run(
            bootstrap_payloads.command_bootstrap_reset(
                argparse.Namespace(
                    callback_config=None,
                    retained_callback_config=None,
                    use_baked_apollo=True,
                    use_retained_callback=True,
                )
            )
        )


def test_bootstrap_reset_creates_fresh_interactive_apollo_by_default_even_when_config_exists(
    monkeypatch,
    tmp_path,
    capsys,
):
    config_path = tmp_path / "apollo_callback_config.json"
    config_path.write_text(
        json.dumps({"config": {"uuid": "payload-uuid", "key": "secret"}}),
        encoding="utf-8",
    )
    calls = []

    async def fake_login(args):
        return object()

    async def fake_create_apollo(client, args):
        calls.append(("apollo", None))
        return {"build_phase": "success", "uuid": "apollo-uuid"}

    async def fake_download(client, payload, download_dir):
        calls.append(("download", download_dir))
        return {"downloaded": True, "path": "/payloads/apollo.exe"}

    async def fake_create_sage(client, args):
        calls.append(("sage", None))
        return {"build_phase": "success", "uuid": "sage-uuid"}

    async def fail_import(client, config):
        raise AssertionError("Baked Apollo import must be opt-in")

    async def fake_query(client, query, variables=None):
        return {
            "callback": [],
            "consuming_container": [
                {"id": 1, "container_running": True, "deleted": False}
            ],
        }

    monkeypatch.setattr(bootstrap_payloads, "login", fake_login)
    monkeypatch.setattr(bootstrap_payloads, "create_apollo", fake_create_apollo)
    monkeypatch.setattr(bootstrap_payloads, "maybe_download_payload", fake_download)
    monkeypatch.setattr(bootstrap_payloads, "create_sage", fake_create_sage)
    monkeypatch.setattr(bootstrap_payloads, "import_callback_config", fail_import)
    monkeypatch.setattr(bootstrap_payloads.mythic, "execute_custom_query", fake_query)

    asyncio.run(
        bootstrap_payloads.command_bootstrap_reset(
            argparse.Namespace(
                callback_config=str(config_path),
                use_baked_apollo=False,
                download_dir="/payloads",
            )
        )
    )
    output = json.loads(capsys.readouterr().out)

    assert calls == [
        ("apollo", None),
        ("download", "/payloads"),
    ]
    assert output["mode"] == "fresh-interactive-apollo"
    assert output["apollo"]["uuid"] == "apollo-uuid"
    assert output["apollo_bootstrap"]["method"] == "interactive-rdp-scheduled-task"
    assert output["apollo_bootstrap"]["payload_uuid"] == "apollo-uuid"


def test_sage_task_password_resolver_prefers_environment(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MYTHIC_ADMIN_PASSWORD=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MYTHIC_ADMIN_PASSWORD", "from-env")

    assert sage_task.resolve_password(env_file) == "from-env"


def test_sage_task_password_resolver_reads_local_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# local Mythic secrets\nOTHER=value\nMYTHIC_ADMIN_PASSWORD='from-file'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MYTHIC_ADMIN_PASSWORD", raising=False)

    assert sage_task.resolve_password(env_file) == "from-file"


def test_sage_task_password_resolver_requires_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("MYTHIC_ADMIN_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="MYTHIC_ADMIN_PASSWORD"):
        sage_task.resolve_password(tmp_path / "missing.env")


def test_sage_task_forces_verbose_for_query_and_chat():
    assert sage_task.normalize_task_parameters("query", {"prompt": "status"}) == {
        "prompt": "status",
        "verbose": True,
    }
    assert sage_task.normalize_task_parameters("chat", {"prompt": "status", "verbose": False}) == {
        "prompt": "status",
        "verbose": True,
    }
    assert sage_task.normalize_task_parameters("query", "status") == {
        "prompt": "status",
        "verbose": True,
    }
    assert sage_task.normalize_task_parameters("state", {"action": "show"}) == {"action": "show"}


def test_sage_task_accepts_explicit_verbose_flag_for_operator_commands():
    cleaned, explicit = sage_task.strip_verbose_args([
        "task-callback",
        "1",
        "query",
        '{"prompt":"status"}',
        "90",
        "--verbose",
        "true",
    ])

    assert cleaned == ["task-callback", "1", "query", '{"prompt":"status"}', "90"]
    assert explicit is True


def test_sage_task_query_chat_remain_verbose_even_if_flag_is_false():
    cleaned, explicit = sage_task.strip_verbose_args([
        "task",
        "query",
        '{"prompt":"status"}',
        "--verbose=false",
    ])

    assert cleaned == ["task", "query", '{"prompt":"status"}']
    assert explicit is False
    assert sage_task.normalize_task_parameters(
        "query",
        {"prompt": "status"},
        explicit_verbose=explicit,
    ) == {"prompt": "status", "verbose": True}


def test_run_essos_auto_selects_latest_live_sage_and_castelblack_apollo():
    callbacks = [
        {
            "display_id": 1,
            "active": True,
            "host": "SAGE",
            "user": "Sage",
            "payload": {"payloadtype": {"name": "sage"}},
        },
        {
            "display_id": 2,
            "active": False,
            "host": "CASTELBLACK",
            "user": "NORTH\\samwell.tarly",
            "payload": {"payloadtype": {"name": "apollo"}},
        },
        {
            "display_id": 3,
            "active": True,
            "host": "CASTELBLACK",
            "user": "NORTH\\samwell.tarly",
            "payload": {"payloadtype": {"name": "apollo"}},
        },
        {
            "display_id": 4,
            "active": True,
            "host": "SAGE",
            "user": "Sage",
            "payload": {"payloadtype": {"name": "sage"}},
        },
    ]

    assert run_essos_da.select_run_callbacks(callbacks) == (4, 3)


def test_run_essos_guidance_prefers_sharpgpoabuse_before_powerpick_fallback():
    objective = run_essos_da.build_objective(9)

    assert "without forcing the PowerShell/GPP fallback method" in objective
    assert "`SharpGPOAbuse.exe`" in objective
    assert "`--AddComputerTask --Force`" in objective
    assert "Apollo `execute_assembly`" in objective
    assert "only if the primary SharpGPOAbuse/execute_assembly path is unavailable" in objective


def test_run_essos_guidance_includes_structured_route_facts():
    objective = run_essos_da.build_objective(9)
    state_objective = run_essos_da.build_state_objective()

    expected_laps = (
        "can-read-managed-local-admin-secret:"
        "account=cersei.lannister;account_domain=sevenkingdoms.local;target=braavos;target_domain=essos.local"
    )
    assert expected_laps in objective
    assert expected_laps in state_objective
    assert "certificate-auth-target:administrator@essos.local" in objective
    assert "certificate-auth-target:administrator@essos.local" in state_objective


def test_apollo_payload_defaults_match_goad_rehearsal():
    args = argparse.Namespace(
        callback_host="https://10.4.10.1",
        callback_port=80,
        callback_interval=3,
        callback_jitter=23,
        aespsk="aes256_hmac",
        get_uri="index",
        post_uri="data",
        query_path_name="q",
        output_type="WinExe",
        adjust_filename=False,
        debug=False,
    )

    assert bootstrap_payloads.apollo_build_parameters(args) == [
        {"name": "output_type", "value": "WinExe"},
        {"name": "shellcode_format", "value": "Binary"},
        {"name": "shellcode_bypass", "value": "Continue on fail"},
        {"name": "adjust_filename", "value": "false"},
        {"name": "debug", "value": "false"},
    ]
    assert bootstrap_payloads.apollo_c2_profiles(args) == [
        {
            "c2_profile": "http",
            "c2_profile_parameters": {
                "callback_host": "https://10.4.10.1",
                "callback_port": "80",
                "callback_interval": "3",
                "callback_jitter": "23",
                "AESPSK": "aes256_hmac",
                "encrypted_exchange_check": "true",
                "get_uri": "index",
                "post_uri": "data",
                "query_path_name": "q",
            },
        }
    ]


def test_runtime_db_status_blocks_required_dbs_without_deleting(tmp_path):
    sage_root = tmp_path / "Payload_Type" / "sage"
    phoenix_root = sage_root / ".phoenix"
    phoenix_root.mkdir(parents=True)
    (sage_root / "sage.db").write_text("checkpoint", encoding="utf-8")
    (phoenix_root / "phoenix.db").write_text("trace", encoding="utf-8")
    (sage_root / "sage_20260613.db").write_text("session", encoding="utf-8")
    (phoenix_root / "phoenix_20260613-1200.db").write_text("trace archive", encoding="utf-8")

    status = bootstrap_payloads.runtime_db_status(tmp_path)

    assert status["ready"] is False
    assert status["existing_required"] == [
        "Payload_Type/sage/sage.db",
        "Payload_Type/sage/.phoenix/phoenix.db",
    ]
    assert status["existing_session"] == ["Payload_Type/sage/sage_20260613.db"]
    assert status["existing_archives"] == [
        "Payload_Type/sage/sage_20260613.db",
        "Payload_Type/sage/.phoenix/phoenix_20260613-1200.db",
    ]
    assert (sage_root / "sage.db").exists()
    assert (phoenix_root / "phoenix.db").exists()


def test_runtime_db_status_allows_recreated_dbs_after_archive_confirmation(tmp_path):
    sage_root = tmp_path / "Payload_Type" / "sage"
    phoenix_root = sage_root / ".phoenix"
    phoenix_root.mkdir(parents=True)
    (sage_root / "sage.db").write_text("fresh checkpoint", encoding="utf-8")
    (phoenix_root / "phoenix.db").write_text("fresh trace", encoding="utf-8")

    status = bootstrap_payloads.runtime_db_status(
        tmp_path,
        runtime_dbs_archived=True,
    )

    assert status["ready"] is True
    assert status["runtime_dbs_archived"] is True
    assert status["operator_db_cleanup_confirmed"] is True
    assert status["existing_required"] == [
        "Payload_Type/sage/sage.db",
        "Payload_Type/sage/.phoenix/phoenix.db",
    ]
    assert (sage_root / "sage.db").exists()
    assert (phoenix_root / "phoenix.db").exists()


def test_callback_readiness_selects_fresh_live_sage_and_castelblack_apollo():
    callbacks = [
        {
            "display_id": 3,
            "host": "SAGE",
            "user": "Sage",
            "active": True,
            "payload": {"payloadtype": {"name": "sage"}},
        },
        {
            "display_id": 7,
            "host": "SAGE",
            "user": "Sage",
            "active": True,
            "payload": {"payloadtype": {"name": "sage"}},
        },
        {
            "display_id": 8,
            "host": "CASTELBLACK",
            "user": "samwell.tarly",
            "active": True,
            "payload": {"payloadtype": {"name": "apollo"}},
        },
        {
            "display_id": 9,
            "host": "CASTELBLACK",
            "user": "samwell.tarly",
            "active": True,
            "payload": {"payloadtype": {"name": "apollo"}},
        },
    ]
    liveness = {
        3: {"alive": True, "reason": "old but live"},
        7: {"alive": True, "reason": "fresh"},
        8: {"alive": True, "reason": "old but live"},
        9: {"alive": True, "reason": "fresh"},
    }

    status = bootstrap_payloads.summarize_callback_readiness(
        callbacks,
        liveness,
        chat_containers=[{"id": 1, "container_running": True, "deleted": False}],
    )

    assert status["ready"] is True
    assert status["selected_sage_cb"] is None
    assert status["selected_chat_container_id"] == 1
    assert status["selected_foothold_cb"] == 9
    assert status["selected_apollo_cb"] == 9


def test_callback_readiness_selects_merlin_when_requested():
    callbacks = [
        {
            "display_id": 1,
            "host": "SAGE",
            "user": "Sage",
            "active": True,
            "payload": {"payloadtype": {"name": "sage"}},
        },
        {
            "display_id": 2,
            "host": "CASTELBLACK",
            "user": "NORTH\\samwell.tarly",
            "active": True,
            "payload": {"payloadtype": {"name": "merlin"}},
        },
        {
            "display_id": 3,
            "host": "CASTELBLACK",
            "user": "NORTH\\samwell.tarly",
            "active": True,
            "payload": {"payloadtype": {"name": "apollo"}},
        },
    ]
    liveness = {
        1: {"alive": True, "reason": "fresh"},
        2: {"alive": True, "reason": "fresh"},
        3: {"alive": True, "reason": "fresh"},
    }

    status = bootstrap_payloads.summarize_callback_readiness(
        callbacks,
        liveness,
        foothold_payload_type="merlin",
        chat_containers=[{"id": 1, "container_running": True, "deleted": False}],
    )

    assert status["ready"] is True
    assert status["foothold_payload_type"] == "merlin"
    assert status["selected_sage_cb"] is None
    assert status["selected_foothold_cb"] == 2
    assert status["selected_apollo_cb"] is None


def test_callback_readiness_rejects_dead_or_wrong_foothold():
    callbacks = [
        {
            "display_id": 1,
            "host": "SAGE",
            "user": "Sage",
            "active": True,
            "payload": {"payloadtype": {"name": "sage"}},
        },
        {
            "display_id": 2,
            "host": "WINTERFELL",
            "user": "samwell.tarly",
            "active": True,
            "payload": {"payloadtype": {"name": "apollo"}},
        },
    ]
    liveness = {
        1: {"alive": False, "reason": "dead"},
        2: {"alive": True, "reason": "fresh but wrong host"},
    }

    status = bootstrap_payloads.summarize_callback_readiness(callbacks, liveness)

    assert status["ready"] is False
    assert status["selected_sage_cb"] is None
    assert status["selected_apollo_cb"] is None
