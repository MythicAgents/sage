from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


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
        authentication={"ready": True, "blockers": []},
        bloodhound_api={"ready": True, "blockers": []},
        bloodhound_mcp={"ready": True, "blockers": []},
        mythic_chat={"ready": True, "blockers": []},
        foothold={"ready": True, "blockers": []},
        channel={"ready": True, "blockers": []},
    )

    assert report["schema"] == "sage-readiness-contract-v1"
    assert report["ready"] is True
    assert report["readiness"]["lane"] == "full"
    assert report["readiness"]["launch_released"] is True
    assert report["runtime_identity"]["api_key"] == "<redacted>"
    assert report["runtime_identity"]["credential_text"] == "<redacted>"
    assert report["runtime_identity"]["private_key"] == "<redacted>"
    assert report["runtime_identity"]["token_id"] == "retain-me"
    assert MODULE.hash_file(artifact)["sha256"]


def _ready_sections(lane: str) -> dict:
    names = MODULE.READINESS_LANE_SECTIONS[lane]
    return {name: {"ready": True, "blockers": []} for name in names}


@pytest.mark.parametrize("lane", ["range_boot", "callback", "full"])
def test_typed_readiness_requires_each_lane_section_as_literal_true(lane):
    sections = _ready_sections(lane)
    ready = MODULE.evaluate_readiness(
        lane=lane,
        sections=sections,
        observation_id="fresh-1",
    )
    assert ready["ready"] is True
    assert ready["state"] == "ready"
    assert ready["launch_released"] is True
    assert ready["capture_attempt_consumed"] is False

    for name in MODULE.READINESS_LANE_SECTIONS[lane]:
        invalid = dict(sections)
        invalid[name] = {"ready": 1, "blockers": []}
        blocked = MODULE.evaluate_readiness(
            lane=lane,
            sections=invalid,
            observation_id=f"bad-{name}",
        )
        assert blocked["ready"] is False
        assert blocked["launch_released"] is False
        assert any(name in item for item in blocked["blockers"])


def test_callback_lane_allows_intentionally_absent_sage_but_full_lane_does_not():
    callback = MODULE.evaluate_readiness(
        lane="callback",
        sections=_ready_sections("callback"),
        observation_id="callback-1",
    )
    full_sections = _ready_sections("full")
    full_sections["sage_deployment"] = {
        "ready": False,
        "blockers": ["intentionally absent"],
    }
    full_sections["mythic_chat"] = {
        "ready": False,
        "blockers": ["intentionally absent"],
    }
    full = MODULE.evaluate_readiness(
        lane="full",
        sections=full_sections,
        observation_id="full-1",
    )

    assert callback["ready"] is True
    assert full["ready"] is False


@pytest.mark.parametrize(
    ("outer", "child", "grace"),
    [(300, 300, 0), (330, 300, 30), (299, 300, 0)],
)
def test_timeout_budget_must_be_strictly_greater_than_child_plus_grace(
    outer, child, grace
):
    with pytest.raises(ValueError, match="strictly greater"):
        MODULE.validate_timeout_budget(
            outer_timeout_seconds=outer,
            child_timeout_seconds=child,
            grace_seconds=grace,
        )

    valid = MODULE.validate_timeout_budget(
        outer_timeout_seconds=331,
        child_timeout_seconds=300,
        grace_seconds=30,
    )
    assert valid["valid"] is True


def test_poll_readiness_reprobes_null_ip_until_fresh_hydrated_observation():
    calls = []
    observations = iter(
        [
            {
                "observation_id": "range-1",
                "sections": {"ludus": {"ready": False, "blockers": ["null ip"]}},
            },
            {
                "observation_id": "range-2",
                "sections": {"ludus": {"ready": True, "blockers": []}},
            },
        ]
    )

    def probe():
        calls.append("probe")
        return next(observations)

    decision = MODULE.poll_readiness(
        probe,
        lane="range_boot",
        outer_timeout_seconds=10,
        child_timeout_seconds=1,
        grace_seconds=1,
        poll_seconds=0,
        sleep=lambda _seconds: None,
    )

    assert decision["ready"] is True
    assert decision["observation_id"] == "range-2"
    assert decision["poll_count"] == 2
    assert calls == ["probe", "probe"]


def test_planted_stale_observation_cannot_release_readiness():
    observations = iter(
        [
            {
                "observation_id": "cached-range",
                "sections": {"ludus": {"ready": False, "blockers": ["null ip"]}},
            },
            {
                "observation_id": "cached-range",
                "sections": {"ludus": {"ready": True, "blockers": []}},
            },
        ]
    )

    decision = MODULE.poll_readiness(
        lambda: next(observations),
        lane="range_boot",
        outer_timeout_seconds=10,
        child_timeout_seconds=1,
        grace_seconds=1,
        poll_seconds=0,
        sleep=lambda _seconds: None,
    )

    assert decision["ready"] is False
    assert decision["state"] == "blocked"
    assert decision["launch_released"] is False
    assert any("stale observation_id" in item for item in decision["blockers"])


def test_poll_never_starts_child_without_timeout_plus_grace_remaining():
    calls = []
    moments = iter((0.0, 0.0, 2.0))

    decision = MODULE.poll_readiness(
        lambda: calls.append("probe")
        or {
            "observation_id": "still-booting",
            "sections": {"ludus": {"ready": False, "blockers": ["null ip"]}},
        },
        lane="range_boot",
        outer_timeout_seconds=4,
        child_timeout_seconds=2,
        grace_seconds=1,
        poll_seconds=0,
        sleep=lambda _seconds: None,
        now=lambda: next(moments),
    )

    assert calls == ["probe"]
    assert decision["state"] == "blocked"
    assert decision["capture_attempt_consumed"] is False
    assert any("safe child probe" in item for item in decision["blockers"])


@pytest.mark.parametrize("raw", ["", "not-json", "[]", '{"ready": true}', '{"ready": 1}'])
def test_planted_malformed_output_cannot_release_launch(raw):
    decision = MODULE.parse_release_output(raw, expected_lane="full")

    assert decision["ready"] is False
    assert decision["state"] == "blocked"
    assert decision["launch_released"] is False


def test_canonical_full_output_is_the_only_release_shape():
    report = {
        "schema": "sage-readiness-contract-v1",
        "ready": True,
        "observation_id": "full-output-1",
        "readiness": MODULE.evaluate_readiness(
            lane="full",
            sections=_ready_sections("full"),
            observation_id="full-output-1",
        ),
        **_ready_sections("full"),
    }

    parsed = MODULE.parse_release_output(json.dumps(report), expected_lane="full")

    assert parsed["ready"] is True
    assert parsed["launch_released"] is True


def test_partial_canonical_output_cannot_release_launch():
    sections = _ready_sections("full")
    decision = MODULE.evaluate_readiness(
        lane="full", sections=sections, observation_id="partial-output"
    )
    report = {
        "schema": MODULE.READINESS_CONTRACT_SCHEMA,
        "ready": True,
        "observation_id": "partial-output",
        "readiness": decision,
        **{name: section for name, section in sections.items() if name != "authentication"},
    }

    parsed = MODULE.parse_release_output(report, expected_lane="full")

    assert parsed["ready"] is False
    assert parsed["launch_released"] is False


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


def test_fixture_inventory_accepts_only_declared_bsl_router_interfaces():
    common = [
        {"name": "router", "poweredOn": True, "ip": "10.16.20.254"},
        {"name": "dc01", "poweredOn": True, "ip": "10.16.10.10"},
        {"name": "srv01", "poweredOn": True, "ip": "10.16.10.11"},
        {"name": "srv02", "poweredOn": True, "ip": "10.16.10.12"},
        {"name": "srv03", "poweredOn": True, "ip": "10.16.10.13"},
        {"name": "elastic", "poweredOn": True, "ip": "10.16.20.2"},
        {"name": "kali", "poweredOn": True, "ip": "10.16.99.1"},
    ]
    valid = MODULE.ludus_status(
        {"rangeNumber": 16, "VMs": common},
        "bsl-ad-elastic",
        qualification_observation=True,
    )
    wrong_router = copy.deepcopy(common)
    wrong_router[0]["ip"] = "10.16.20.253"
    invalid = MODULE.ludus_status(
        {"rangeNumber": 16, "VMs": wrong_router},
        "bsl-ad-elastic",
        qualification_observation=True,
    )

    assert valid["ready"] is True
    assert valid["live_ready"] is False
    assert valid["expected_ips"]["router"] == "10.16.10.254"
    assert valid["accepted_ips"]["router"] == [
        "10.16.10.254", "10.16.20.254", "10.16.99.254",
    ]
    assert invalid["ready"] is False
    assert "unexpected Ludus VM IPs: router" in invalid["blockers"]


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


def test_live_qualified_profile_drives_non_goad_inventory():
    report = MODULE.ludus_status({
        "rangeNumber": 7,
        "rangeState": "SUCCESS",
        "VMs": [
            {"name": "SAGEREPL-router-debian11-x64", "poweredOn": True, "ip": "10.7.10.254"},
            {"name": "SAGEREPL-DC01", "poweredOn": True, "ip": "10.7.10.10"},
            {"name": "SAGEREPL-SRV02", "poweredOn": True, "ip": "10.7.10.22"},
            {"name": "SAGEREPL-WS01", "poweredOn": True, "ip": "10.7.10.31"},
        ],
    }, "sage-replication-range")

    assert report["ready"] is True
    assert report["expected_ips"]["ws01"] == "10.7.10.31"
    assert report["range_profile"]["id"] == "sage-replication-range"
    assert report["blockers"] == []


def _bsl_shaped_profile() -> dict:
    return {
        "id": "bsl-vlan-synthetic",
        "source": {"kind": "external", "path": "bsl-synthetic-source"},
        "qualification": {"state": "defined_only", "receipt": ""},
        "network": {"first_octet": 10, "vlan": 10, "range_number_octet": True},
        "vms": [
            {"role": "dc01", "aliases": ["dc01"], "suffixes": [], "last_octet": 10, "os": "windows"},
            {"role": "dc02", "aliases": ["dc02"], "suffixes": [], "last_octet": 11, "os": "windows"},
            {"role": "srv02", "aliases": ["srv02"], "suffixes": [], "last_octet": 12, "os": "windows"},
            {"role": "srv03", "aliases": ["srv03"], "suffixes": [], "last_octet": 13, "os": "windows"},
            {"role": "elastic", "aliases": ["elastic"], "suffixes": [], "last_octet": 2, "vlan": 20, "os": "linux"},
            {"role": "kali", "aliases": ["kali"], "suffixes": [], "last_octet": 1, "vlan": 99, "os": "linux"},
        ],
        "topology": {"capabilities": ["collect-directory-graph"], "branches": ["synthetic"]},
        "foothold": {
            "role": "dc01", "principal": "SYNTHETIC\\operator", "callback_host": "DC01",
            "callback_user": "operator", "password_env": "SYNTHETIC_OPERATOR_PASSWORD",
        },
        "bindings": [],
    }


def test_ludus_status_uses_effective_vm_vlans_and_keeps_defined_only_profile_blocked():
    profile = _bsl_shaped_profile()
    state = {
        "rangeNumber": 7,
        "rangeState": "SUCCESS",
        "VMs": [
            {"name": "dc01", "poweredOn": True, "ip": "10.7.10.10"},
            {"name": "dc02", "poweredOn": True, "ip": "10.7.10.11"},
            {"name": "srv02", "poweredOn": True, "ip": "10.7.10.12"},
            {"name": "srv03", "poweredOn": True, "ip": "10.7.10.13"},
            {"name": "elastic", "poweredOn": True, "ip": "10.7.20.2"},
            {"name": "kali", "poweredOn": True, "ip": "10.7.99.1"},
        ],
    }

    accepted_inventory = MODULE.ludus_status(state, profile)
    assert accepted_inventory["expected_ips"]["elastic"] == "10.7.20.2"
    assert accepted_inventory["expected_ips"]["kali"] == "10.7.99.1"
    assert accepted_inventory["ready"] is False
    assert accepted_inventory["blockers"] == [
        "range profile bsl-vlan-synthetic is defined_only, not live-qualified"
    ]

    wrong_vlan = copy.deepcopy(state)
    wrong_vlan["VMs"][4]["ip"] = "10.7.10.2"
    rejected_inventory = MODULE.ludus_status(wrong_vlan, profile)
    assert rejected_inventory["ready"] is False
    assert "unexpected Ludus VM IPs: elastic" in rejected_inventory["blockers"]


def test_profile_drives_authenticated_windows_guest_set():
    report = MODULE.authentication_status({
        "hosts": [
            {"inventory_hostname": "SAGEREPL-DC01"},
            {"inventory_hostname": "SAGEREPL-SRV02"},
            {"inventory_hostname": "SAGEREPL-WS01"},
        ],
        "errors": [],
    }, "sage-replication-range")

    assert report["expected_hosts"] == ["dc01", "srv02", "ws01"]
    assert report["observed_hosts"] == ["dc01", "srv02", "ws01"]
    assert report["ready"] is True
    assert report["blockers"] == []


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


def _point_runtime_env_at(monkeypatch, directory: Path) -> None:
    """Redirect only WHERE the shared resolver looks; its precedence logic runs for real."""
    monkeypatch.delenv("SAGE_BLOODHOUND_MCP_DIR", raising=False)
    monkeypatch.setattr(MODULE._runtime_env(), "RUNTIME_DIR", directory)


def test_unresolved_bloodhound_mcp_dir_is_a_named_blocker_not_a_sibling_guess(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "bloodhound_mcp").mkdir(parents=True)
    runtime = workspace / "sage" / "Payload_Type" / "sage"
    runtime.mkdir(parents=True)
    (runtime / ".env").write_text("#SAGE_BLOODHOUND_MCP_DIR=\n", encoding="utf-8")
    _point_runtime_env_at(monkeypatch, runtime)

    status = MODULE.bloodhound_mcp_status(None, ["file_upload", "domain_info", "cypher_query"])
    probed = asyncio.run(MODULE.probe_bloodhound_mcp_tools())

    for section in (status, probed):
        assert section["ready"] is False
        assert section["directory"] is None
        assert any("SAGE_BLOODHOUND_MCP_DIR is not set" in b for b in section["blockers"])


def test_bloodhound_mcp_dir_follows_env_local_before_env(tmp_path, monkeypatch):
    local_dir, tracked_dir = tmp_path / "from-local", tmp_path / "from-env"
    local_dir.mkdir()
    tracked_dir.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".env.local").write_text(f"SAGE_BLOODHOUND_MCP_DIR={local_dir}\n", encoding="utf-8")
    (runtime / ".env").write_text(f"SAGE_BLOODHOUND_MCP_DIR={tracked_dir}\n", encoding="utf-8")
    _point_runtime_env_at(monkeypatch, runtime)

    status = MODULE.bloodhound_mcp_status(None, ["file_upload", "domain_info", "cypher_query"])

    assert status["ready"] is True
    assert status["directory"] == str(local_dir)


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
# answered by whichever won — possibly a container running its baked image instead of the working
# tree and with no BloodHound MCP directory, while readiness reports `ready: true`.


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


# --- Ludus addresses a range by its ID, never by its number ----------------------------------------------------

def _stub_range_helpers(monkeypatch, *, env=None, resolved=None):
    """Replace only the Ludus and clock helpers the probes load, recording the range each request is scoped to."""
    scoped: list = []
    asked: list = []
    if env is None:
        monkeypatch.delenv("SAGE_LUDUS_RANGE_ID", raising=False)
    else:
        monkeypatch.setenv("SAGE_LUDUS_RANGE_ID", env)
    MODULE._resolve_range_number.cache_clear()
    original = MODULE._load_module

    def resolve_cli_range_id(value=None, mcp_server=None):
        asked.append(value)
        return (resolved or {})[value]

    def call(method, path, body=None, range_id=None, mcp_server=None):
        scoped.append(("ludus", range_id))
        return 200, {"rangeID": range_id, "rangeNumber": 4, "rangeState": "SUCCESS", "VMs": []}

    def load_inventory(mcp_path, range_id=None, mcp_server=None):
        scoped.append(("clock", range_id))
        return {}

    stubs = {
        "ludus.py": SimpleNamespace(resolve_cli_range_id=resolve_cli_range_id, _call=call),
        "sync_range_time.py": SimpleNamespace(
            DEFAULT_MCP_PATH=Path("/unused"), DEFAULT_MAX_SKEW_SECONDS=60.0, load_inventory=load_inventory,
            windows_hosts=lambda inventory: [], check_clocks=lambda hosts, skew: {"ready": False, "hosts": []}),
    }
    monkeypatch.setattr(MODULE, "_load_module",
                        lambda name, path: stubs.get(Path(path).name) or original(name, path))
    return scoped, asked


@pytest.mark.parametrize("argument, env", [("4", None), (None, "4")])
def test_every_range_probe_uses_the_range_id_for_a_range_number(monkeypatch, argument, env):
    scoped, asked = _stub_range_helpers(monkeypatch, env=env, resolved={"4": "GOADf255df"})

    for _poll in range(3):     # a readiness poll repeats the probes; the number is looked up once
        MODULE.range_boot_observation(argument)
        MODULE.callback_readiness_observation(argument)
    MODULE.range_fixture_inventory_observation(argument)

    assert asked == ["4"]
    assert scoped and all(range_id == "GOADf255df" for _probe, range_id in scoped)
    assert {probe for probe, _range_id in scoped} == {"ludus", "clock"}


def test_a_range_probe_uses_a_range_id_or_the_default_range_without_a_lookup(monkeypatch):
    scoped, asked = _stub_range_helpers(monkeypatch)
    MODULE.callback_readiness_observation("GOADf255df")
    MODULE.callback_readiness_observation()

    assert asked == []
    assert scoped == [("clock", "GOADf255df"), ("ludus", "GOADf255df"), ("clock", None), ("ludus", None)]


def test_collected_readiness_resolves_through_the_probes_and_not_when_observations_are_supplied(monkeypatch):
    scoped, asked = _stub_range_helpers(monkeypatch, env="4", resolved={"4": "GOADf255df"})
    common = dict(callback_summary={}, chat_containers=[], api_token=None, channel=None,
                  bloodhound_api_observation={"ready": True}, bloodhound_mcp_observation={"ready": True},
                  sage_deployment_observation={"ready": True})

    asyncio.run(MODULE.collect_operator_readiness(**common, ludus_observation={"ready": True},
                                                  clock_observation={"ready": True}))
    assert asked == [] and scoped == []

    asyncio.run(MODULE.collect_operator_readiness(**common))
    assert asked == ["4"] and all(range_id == "GOADf255df" for _probe, range_id in scoped)



# --- Runtime identity and databases follow the deployment mode -----------------------------------------

def test_container_identity_comes_from_parity_not_the_tmux_startup_file(tmp_path):
    """2026-11-03/04: readiness read /tmp/sage_startup_identity.json, which only the tmux launcher
    writes, so a correctly deployed container reported `ready: false`."""
    stale = tmp_path / "startup.json"
    stale.write_text('{"pid": 999999999, "provider": "openai", "model": "old"}', encoding="utf-8")

    ready = MODULE.runtime_identity_status(
        mode="container", identity_path=stale,
        parity_probe=lambda: {"ready": True, "blockers": [], "repo_digest": "abc", "container_digest": "abc"},
    )
    stale_code = MODULE.runtime_identity_status(
        mode="container", identity_path=stale,
        parity_probe=lambda: {"ready": False, "blockers": ["the container's code differs"], "repo_digest": "a"},
    )

    assert ready["ready"] is True and ready["source"] == "container-parity"
    assert stale_code["ready"] is False
    assert any("differs" in b for b in stale_code["blockers"])


def test_local_identity_still_reads_the_startup_file(tmp_path):
    def must_not_run():
        raise AssertionError("parity is not the local identity source")

    status = MODULE.runtime_identity_status(
        mode="local", identity_path=tmp_path / "absent.json", parity_probe=must_not_run,
    )
    assert status["source"] == "local-startup-identity"


def test_container_runtime_databases_are_the_installed_copy(tmp_path):
    installed = tmp_path / "mythic" / "InstalledServices" / "sage"
    (installed / ".phoenix").mkdir(parents=True)
    (installed / "sage.db").write_text("")
    repo = tmp_path / "repo"
    (repo / "Payload_Type" / "sage").mkdir(parents=True)

    status = MODULE.runtime_db_status(repo, database_dir=installed)

    assert status["ready"] is False, "an unarchived installed database must block"
    assert status["existing_required"] == [str(installed / "sage.db")]



def test_container_identity_runs_the_real_parity_and_reports_its_failure(tmp_path, monkeypatch):
    """No injected probe: readiness loads sage_deployment itself and asks the container."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\necho 'Error response from daemon: No such container: sage' >&2\nexit 1\n")
    docker.chmod(0o755)
    import os
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    status = MODULE.runtime_identity_status(mode="container")

    assert status["ready"] is False and status["source"] == "container-parity"
    assert any("No such container" in b for b in status["blockers"]), status



def test_published_identity_comes_from_the_image_check(tmp_path):
    status = MODULE.runtime_identity_status(
        mode="published", identity_path=tmp_path / "absent.json",
        published_probe=lambda: {"ready": False, "blockers": ["the running container is not the published image"]},
    )
    assert status["source"] == "published-image" and status["ready"] is False
    assert MODULE.sage_deployment_status(mode="published", container_running=True, local_running=False)["ready"]


def test_published_runtime_databases_are_the_installed_copy(tmp_path, monkeypatch):
    mythic = tmp_path / "mythic"
    installed = mythic / "InstalledServices" / "sage"
    installed.mkdir(parents=True)
    (installed / "sage.db").write_text("")
    monkeypatch.setenv("MYTHIC_ENV_PATH", str(mythic / ".env"))

    status = MODULE.runtime_databases_status(tmp_path, mode="published")
    assert status["existing_required"] == [str(installed / "sage.db")]



def test_published_identity_keeps_the_image_evidence(tmp_path):
    status = MODULE.runtime_identity_status(
        mode="published", identity_path=tmp_path / "absent.json",
        published_probe=lambda: {"ready": True, "blockers": [], "image": "ghcr.io/x/sage:1",
                                 "image_id": "sha256:a", "running_image_id": "sha256:a",
                                 "image_digest": "d", "container_digest": "d"},
    )
    assert status["ready"] is True
    assert (status["image"], status["image_id"], status["running_image_id"], status["image_digest"]) == (
        "ghcr.io/x/sage:1", "sha256:a", "sha256:a", "d")
