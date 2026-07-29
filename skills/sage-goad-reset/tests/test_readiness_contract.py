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
            "SAGE_BLOODHOUND_MCP_DIR": "/home/john/dev/bloodhound_mcp",
            "API_KEY": "secret-value",
        },
        pid=123,
        cwd="/home/john/dev/sage/Payload_Type/sage",
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
