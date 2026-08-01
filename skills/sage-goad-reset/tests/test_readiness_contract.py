from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "readiness_contract.py"
SPEC = importlib.util.spec_from_file_location("readiness_contract", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_build_readiness_report_requires_every_section_and_redacts_secrets(tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("payload", encoding="utf-8")
    report = MODULE.build_readiness_report(
        sage_deployment={"ready": True, "blockers": []},
        runtime_identity={
            "ready": True,
            "provider": "openai",
            "model": "test-model",
            "route": "http://127.0.0.1:8100",
            "api_key": "secret",
            "credential_text": "secret-credential",
            "private_key": "secret-key",
            "token_id": "retain-me",
            "blockers": [],
        },
        runtime_databases={"ready": True, "blockers": []},
        ludus={"ready": True, "blockers": []},
        clock={"ready": True, "blockers": []},
        bloodhound_api={"ready": True, "blockers": []},
        bloodhound_mcp={"ready": True, "blockers": []},
        mythic_chat={"ready": True, "blockers": []},
        foothold={"ready": True, "blockers": []},
        channel={"ready": True, "blockers": []},
    )

    assert report["schema"] == "sage-readiness-contract-v1"
    assert report["ready"] is True
    assert report["runtime_identity"]["api_key"] == "<redacted>"
    assert report["runtime_identity"]["credential_text"] == "<redacted>"
    assert report["runtime_identity"]["private_key"] == "<redacted>"
    assert report["runtime_identity"]["token_id"] == "retain-me"
    assert MODULE.hash_file(artifact)["sha256"]


def test_ludus_status_requires_exact_six_vm_ips():
    report = MODULE.ludus_status({
        "rangeNumber": 4,
        "rangeState": "SUCCESS",
        "VMs": [
            {"name": "router", "poweredOn": True, "ip": "10.4.10.254"},
            {"name": "DC01", "poweredOn": True, "ip": "10.4.10.10"},
            {"name": "DC02", "poweredOn": True, "ip": "10.4.10.11"},
            {"name": "DC03", "poweredOn": True, "ip": "10.4.10.12"},
            {"name": "CASTELBLACK", "poweredOn": True, "ip": "10.4.10.22"},
            {"name": "BRAAVOS", "poweredOn": True, "ip": "10.4.10.23"},
        ],
    })

    assert report["ready"] is True
    assert report["expected_ips"]["srv02"] == "10.4.10.22"


def test_ludus_status_accepts_structured_prefixed_names_from_live_goad_range():
    report = MODULE.ludus_status({
        "rangeNumber": 4,
        "rangeState": "SUCCESS",
        "VMs": [
            {"name": "GOADf255df-router-debian11-x64", "poweredOn": True, "ip": "10.4.10.254"},
            {"name": "GOADf255df-GOAD-DC01", "poweredOn": True, "ip": "10.4.10.10"},
            {"name": "GOADf255df-GOAD-DC02", "poweredOn": True, "ip": "10.4.10.11"},
            {"name": "GOADf255df-GOAD-DC03", "poweredOn": True, "ip": "10.4.10.12"},
            {"name": "GOADf255df-GOAD-SRV02", "poweredOn": True, "ip": "10.4.10.22"},
            {"name": "GOADf255df-GOAD-SRV03", "poweredOn": True, "ip": "10.4.10.23"},
        ],
    })

    assert report["ready"] is True
    assert [row["canonical_name"] for row in report["vms"]] == [
        "router",
        "dc01",
        "dc02",
        "dc03",
        "srv02",
        "srv03",
    ]


def test_ludus_status_rejects_suffix_collision_names_without_substring_admission():
    report = MODULE.ludus_status({
        "rangeNumber": 4,
        "rangeState": "SUCCESS",
        "VMs": [
            {"name": "GOADf255df-router-debian11-x64", "poweredOn": True, "ip": "10.4.10.254"},
            {"name": "GOADf255df-GOAD-DC011", "poweredOn": True, "ip": "10.4.10.10"},
            {"name": "GOADf255df-GOAD-DC02", "poweredOn": True, "ip": "10.4.10.11"},
            {"name": "GOADf255df-GOAD-DC03", "poweredOn": True, "ip": "10.4.10.12"},
            {"name": "GOADf255df-GOAD-SRV02", "poweredOn": True, "ip": "10.4.10.22"},
            {"name": "GOADf255df-GOAD-SRV03", "poweredOn": True, "ip": "10.4.10.23"},
        ],
    })

    assert report["ready"] is False
    assert "missing Ludus VMs: dc01" in report["blockers"]
    assert "unexpected Ludus VM names: GOADf255df-GOAD-DC011" in report["blockers"]


def test_ludus_status_rejects_duplicate_canonical_vm_identities():
    report = MODULE.ludus_status({
        "rangeNumber": 4,
        "rangeState": "SUCCESS",
        "VMs": [
            {"name": "GOADf255df-router-debian11-x64", "poweredOn": True, "ip": "10.4.10.254"},
            {"name": "dc01", "poweredOn": True, "ip": "10.4.10.10"},
            {"name": "GOADf255df-GOAD-DC01", "poweredOn": True, "ip": "10.4.10.10"},
            {"name": "GOADf255df-GOAD-DC02", "poweredOn": True, "ip": "10.4.10.11"},
            {"name": "GOADf255df-GOAD-DC03", "poweredOn": True, "ip": "10.4.10.12"},
            {"name": "GOADf255df-GOAD-SRV02", "poweredOn": True, "ip": "10.4.10.22"},
            {"name": "GOADf255df-GOAD-SRV03", "poweredOn": True, "ip": "10.4.10.23"},
        ],
    })

    assert report["ready"] is False
    assert "duplicate Ludus VM identities: dc01" in report["blockers"]


def test_bloodhound_mcp_status_requires_exact_tool_names(tmp_path):
    missing = MODULE.bloodhound_mcp_status(
        tmp_path,
        ["file_upload", "domain_info", "cypher-query"],
    )
    assert missing["ready"] is False
    assert missing["missing_tools"] == ["cypher_query"]

    ready = MODULE.bloodhound_mcp_status(
        tmp_path,
        ["file_upload", "domain_info", "cypher_query"],
    )
    assert ready["ready"] is True


def test_write_startup_identity_records_only_safe_route_and_required_env(tmp_path):
    path = tmp_path / "startup.json"
    written = MODULE.write_startup_identity(
        path,
        {
            "provider": "openai",
            "model": "test-model",
            "API_ENDPOINT": "http://127.0.0.1:8100/v1?secret=1",
            "SAGE_ENGAGEMENT_GATE": "1",
            "SAGE_BLOODHOUND_MCP_DIR": "/opt/bloodhound_mcp",
            "API_KEY": "secret-value",
        },
        pid=123,
        cwd="/srv/sage/Payload_Type/sage",
        recorded_at="2026-07-20T00:00:00+00:00",
    )

    assert written["route"] == "http://127.0.0.1:8100/v1"
    assert written["required_env"] == {
        "SAGE_ENGAGEMENT_GATE": True,
        "SAGE_BLOODHOUND_MCP_DIR": True,
    }
    assert "secret-value" not in path.read_text(encoding="utf-8")


def test_startup_identity_requires_valid_running_process_probe(tmp_path):
    identity = tmp_path / "startup.json"
    MODULE.write_startup_identity(
        identity,
        {
            "provider": "openai",
            "model": "test-model",
            "API_ENDPOINT": "http://user:pass@127.0.0.1:8100/v1?secret=1#frag",
            "SAGE_ENGAGEMENT_GATE": "1",
            "SAGE_BLOODHOUND_MCP_DIR": "/tmp/bloodhound",
        },
        pid=4321,
        cwd="/repo/Payload_Type/sage",
        recorded_at="2026-07-20T00:00:00+00:00",
    )

    status = MODULE.startup_identity_from_env(
        {"provider": "ambient-only", "model": "ambient-only"},
        identity_path=identity,
        process_probe=lambda pid: {
            "exists": pid == 4321,
            "cmdline": "/repo/.venv/bin/python -u main.py",
            "cwd": "/repo/Payload_Type/sage",
        },
    )

    assert status["ready"] is True
    assert status["provider"] == "openai"
    assert status["model"] == "test-model"
    assert status["route"] == "http://127.0.0.1:8100/v1"
    assert status["pid"] == 4321
    assert status["cwd"] == "/repo/Payload_Type/sage"


def test_startup_identity_rejects_dead_or_stale_pid(tmp_path):
    identity = tmp_path / "startup.json"
    MODULE.write_startup_identity(
        identity,
        {
            "provider": "openai",
            "model": "test-model",
            "SAGE_ENGAGEMENT_GATE": "1",
            "SAGE_BLOODHOUND_MCP_DIR": "/tmp/bloodhound",
        },
        pid=1,
        cwd="/repo/Payload_Type/sage",
    )

    status = MODULE.startup_identity_from_env(
        identity_path=identity,
        process_probe=lambda _pid: {"exists": False, "cmdline": "", "cwd": ""},
    )

    assert status["ready"] is False
    assert any("not running" in blocker for blocker in status["blockers"])


def test_startup_identity_rejects_cwd_and_cmdline_mismatch(tmp_path):
    identity = tmp_path / "startup.json"
    MODULE.write_startup_identity(
        identity,
        {
            "provider": "openai",
            "model": "test-model",
            "SAGE_ENGAGEMENT_GATE": "1",
            "SAGE_BLOODHOUND_MCP_DIR": "/tmp/bloodhound",
        },
        pid=22,
        cwd="/repo/Payload_Type/sage",
    )

    status = MODULE.startup_identity_from_env(
        identity_path=identity,
        process_probe=lambda _pid: {
            "exists": True,
            "cmdline": "/repo/.venv/bin/python worker.py",
            "cwd": "/repo/elsewhere",
        },
    )

    assert status["ready"] is False
    assert "recorded Sage process cmdline does not contain main.py" in status["blockers"]
    assert "recorded Sage process cwd does not match startup identity" in status["blockers"]


def test_startup_identity_does_not_fall_back_to_ambient_env(tmp_path):
    status = MODULE.startup_identity_from_env(
        {"provider": "ambient-only", "model": "ambient-only"},
        identity_path=tmp_path / "missing.json",
    )

    assert status["ready"] is False
    assert status["provider"] == ""
    assert status["model"] == ""


def test_channel_status_requires_prepared_channel():
    assert MODULE.channel_status({
        "chat_channel_id": 7,
        "chat_channel_name": "not-prepared",
        "prepared": False,
    })["ready"] is False
    assert MODULE.channel_status({
        "chat_channel_id": 7,
        "chat_channel_name": "prepared",
        "prepared": True,
    })["ready"] is True


def test_channel_status_can_defer_chat_creation_to_operator():
    status = MODULE.channel_status(None, required=False)

    assert status["ready"] is True
    assert status["required"] is False
    assert status["prepared"] is False
    assert status["blockers"] == []


# --- Sage deployment mode: exactly one Sage may serve Mythic's `sage` queue -------------------
#
# `mythic-cli start` starts every registered service, so a Mythic reset brings the Sage container
# up alongside a tmux Sage. Both register as `sage`, one wins the RabbitMQ queue, and requests are
# answered by whichever won. On 2026-08-01 that was the container, running its baked image instead
# of the working tree and with no BloodHound MCP directory, while readiness reported `ready: true`.


def _deployment(**kwargs):
    kwargs.setdefault("container_running", False)
    kwargs.setdefault("local_running", False)
    return MODULE.sage_deployment_status(**kwargs)


def test_local_mode_is_ready_only_with_the_local_sage_alone():
    assert _deployment(mode="local", local_running=True)["ready"] is True


def test_local_mode_blocks_when_the_container_is_also_up():
    status = _deployment(mode="local", local_running=True, container_running=True)
    assert status["ready"] is False
    assert any("container is running in local mode" in b for b in status["blockers"])


def test_container_mode_blocks_when_a_local_sage_is_also_up():
    status = _deployment(mode="container", container_running=True, local_running=True)
    assert status["ready"] is False
    assert any("local Sage process is running in container mode" in b for b in status["blockers"])


def test_container_mode_is_ready_with_the_container_alone():
    assert _deployment(mode="container", container_running=True)["ready"] is True


def test_missing_intended_sage_blocks_by_default_but_not_in_conflict_only():
    """Mid-reset, Mythic restarts long before Sage does; demanding a live Sage there fails resets."""
    assert _deployment(mode="local")["ready"] is False
    assert _deployment(mode="local", require_intended_running=False)["ready"] is True
    assert _deployment(mode="container", require_intended_running=False)["ready"] is True


def test_conflict_only_still_blocks_the_actual_conflict():
    status = _deployment(mode="local", container_running=True, require_intended_running=False)
    assert status["ready"] is False


def test_undetectable_container_state_fails_closed():
    """`cannot tell` must never collapse into `no container` — that is the whole failure mode."""
    status = _deployment(mode="local", container_running=None, local_running=True)
    assert status["ready"] is False
    assert any("could not determine" in b for b in status["blockers"])


def test_unknown_mode_is_refused_not_coerced():
    status = _deployment(mode="kubernetes", local_running=True)
    assert status["ready"] is False
    assert any("unknown SAGE_DEPLOYMENT_MODE" in b for b in status["blockers"])


def test_local_sage_probe_ignores_the_calling_shell(tmp_path, monkeypatch):
    """`pgrep -f main.py` matches the shell that ran it; the /proc scan must not."""
    proc = tmp_path / "proc"
    (proc / "10").mkdir(parents=True)
    (proc / "10" / "cmdline").write_bytes(b"/bin/bash\0-c\0grep main.py\0")
    assert MODULE.probe_local_sage_running(Path("/repo"), proc) is False
    (proc / "20").mkdir()
    (proc / "20" / "cmdline").write_bytes(b"/repo/.venv/bin/python\0-u\0main.py\0")
    assert MODULE.probe_local_sage_running(Path("/repo"), proc) is True
