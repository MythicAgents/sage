import subprocess
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
PY = ROOT / ".venv" / "bin" / "python"
SCRIPT = ROOT / "skills" / "sage-eval-gauge" / "scripts" / "orchestrate.py"
SPEC = importlib.util.spec_from_file_location("sage_eval_orchestrate", SCRIPT)
orchestrate = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(orchestrate)


def test_dry_run_uses_staged_snapshot_retained_config_and_existing_apollo():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "essos-da",
            "--side",
            "harness",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert "rollback sage-seed-apollo-staged-20260710 --yes" in output
    assert "--use-retained-callback" in output
    assert "apollo_callback_config.json" in output
    assert "launch_apollo_foothold.sh 10.4.10.22 NORTH\\samwell.tarly" in output
    assert "fresh-interactive-apollo" not in output


def test_dry_run_accepts_hybrid_policy_treatment():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "essos-da",
            "--side",
            "harness",
            "--policy-mode",
            "hybrid",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "--policy-mode hybrid" in result.stdout
    assert "Sage policy mode -> hybrid" in result.stdout


def test_dry_run_accepts_alternate_foothold_spec():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "direct-laps-objective",
            "--side",
            "harness",
            "--foothold-host",
            "MEEREEN",
            "--foothold-ip",
            "10.4.10.12",
            "--foothold-user",
            r"ESSOS\jorah.mormont",
            "--foothold-callback-user",
            "jorah.mormont",
            "--foothold-password-env",
            "SAGE_MEEREEN_JORAH_PASSWORD",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert "launch_apollo_foothold.sh 10.4.10.12 ESSOS\\jorah.mormont" in output
    assert "--target-host MEEREEN" in output
    assert "--callback-host MEEREEN" in output
    assert "--callback-user jorah.mormont" in output
    assert "--foothold-host MEEREEN" in output
    assert "--foothold-user-match jorah.mormont" in output
    assert "password source=SAGE_MEEREEN_JORAH_PASSWORD" in output


def test_dry_run_accepts_distinct_ludus_and_callback_hosts():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "replication-purpose-range-visible-cost",
            "--side",
            "harness",
            "--foothold-host",
            "SAGEREPL-WS01",
            "--foothold-callback-host",
            "WS01",
            "--foothold-ip",
            "10.7.10.31",
            "--foothold-user",
            r"REPLICATION\user1",
            "--foothold-callback-user",
            "user1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert "--target-host SAGEREPL-WS01" in output
    assert "--callback-host WS01" in output
    assert "--foothold-host WS01" in output
    assert "--foothold-user-match user1" in output


def test_foothold_launch_env_maps_alternate_password_source(monkeypatch):
    monkeypatch.setenv("SAGE_RUN_AS_PASSWORD", "samwell-password")
    monkeypatch.setenv("SAGE_MEEREEN_JORAH_PASSWORD", "jorah-password")

    foothold = orchestrate.FootholdSpec(password_env="SAGE_MEEREEN_JORAH_PASSWORD")

    assert foothold.launch_env()["SAGE_RUN_AS_PASSWORD"] == "jorah-password"


def test_run_side_pins_same_netbios_map_for_all_policy_arms(monkeypatch):
    seen = {}

    monkeypatch.setattr(orchestrate, "_run", lambda *args, **kwargs: None)

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del snapshot, retained_callback_config, foothold, ludus_range_id
        seen[restart_env["SAGE_POLICY_MODE"]] = dict(restart_env)
        return None, 7

    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    for policy_mode in ("symbolic", "llm", "hybrid"):
        orchestrate.run_side(
            "cross-forest-objective",
            "harness",
            go=False,
            solve_timeout=1,
            policy_mode=policy_mode,
        )

    assert set(seen) == {"symbolic", "llm", "hybrid"}
    assert {
        values["SAGE_ENGAGEMENT_NETBIOS_MAP"]
        for values in seen.values()
    } == {orchestrate.DEFAULT_ENGAGEMENT_NETBIOS_MAP}
    assert {
        values["SAGE_AUTONOMOUS_CONTROLLER"]
        for values in seen.values()
    } == {"1"}
    assert {
        values["SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS"]
        for values in seen.values()
    } == {"1"}


def test_run_side_uses_purpose_range_map_and_passes_range_id(monkeypatch):
    seen = {}

    monkeypatch.setattr(orchestrate, "_run", lambda *args, **kwargs: None)

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del snapshot, retained_callback_config, foothold
        seen["restart_env"] = dict(restart_env)
        seen["range_id"] = ludus_range_id
        return None, 7

    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    orchestrate.run_side(
        "purpose-range-visible-cost",
        "harness",
        go=False,
        solve_timeout=1,
        policy_mode="hybrid",
        ludus_range_id="SAGEPOLICY20260712",
    )

    assert seen["range_id"] == "SAGEPOLICY20260712"
    assert seen["restart_env"]["SAGE_ENGAGEMENT_NETBIOS_MAP"] == orchestrate.DEFAULT_PURPOSE_RANGE_NETBIOS_MAP
    assert seen["restart_env"]["SAGE_GPO_PROOF_SHARE_NAME"] == "SageProof"
    assert seen["restart_env"]["SAGE_GPO_PROOF_LOCAL_ROOT"] == r"C:\SageProof"
    assert seen["restart_env"]["SAGE_GPO_WAIT_SECONDS"] == "120"


def test_run_side_uses_replication_range_map_and_gpo_proof_env(monkeypatch):
    seen = {}

    monkeypatch.setattr(orchestrate, "_run", lambda *args, **kwargs: None)

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del snapshot, retained_callback_config, foothold
        seen["restart_env"] = dict(restart_env)
        seen["range_id"] = ludus_range_id
        return None, 7

    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    orchestrate.run_side(
        "replication-purpose-range-visible-cost",
        "harness",
        go=False,
        solve_timeout=1,
        policy_mode="hybrid",
        ludus_range_id="SAGEREPLICATION20260712",
    )

    assert seen["range_id"] == "SAGEREPLICATION20260712"
    assert (
        seen["restart_env"]["SAGE_ENGAGEMENT_NETBIOS_MAP"]
        == orchestrate.DEFAULT_REPLICATION_PURPOSE_RANGE_NETBIOS_MAP
    )
    assert seen["restart_env"]["SAGE_GPO_PROOF_SHARE_NAME"] == "SageProof"
    assert seen["restart_env"]["SAGE_GPO_PROOF_LOCAL_ROOT"] == r"C:\SageProof"
    assert seen["restart_env"]["SAGE_GPO_WAIT_SECONDS"] == "120"
    assert "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON" not in seen["restart_env"]


def test_purpose_range_recovery_adds_explicit_gpo_blocker_only_for_recovery_variant():
    visible = orchestrate._scenario_restart_env("purpose-range-visible-cost")
    recovery = orchestrate._scenario_restart_env("purpose-range-recovery")

    assert "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON" not in visible
    blocker = json.loads(recovery["SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON"])
    assert blocker["capability"] == "gpo-controlled-system-exec"
    assert blocker["target_contains"] == "gpo=srv02-policy;domain=range.local"
    assert blocker["reason"] == "endpoint protection blocked the staged GPO payload on srv02"
    assert blocker["probe"]["defender_blocked"] is True


def test_direct_laps_ca_export_recovery_adds_late_blocker_and_scoped_enrollment_hint():
    visible = orchestrate._scenario_restart_env("direct-laps-objective")
    recovery = orchestrate._scenario_restart_env("direct-laps-ca-export-recovery")

    assert visible == {}
    blocker = json.loads(recovery["SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON"])
    assert blocker["capability"] == "adcs-ca-private-key-export"
    assert blocker["target_contains"] == "target=braavos;target_domain=essos.local"
    assert blocker["reason"] == "key not exportable"
    assert blocker["probe"]["key_not_exportable"] is True
    hints = json.loads(recovery["SAGE_EVAL_ADCS_ESC_ENROLLMENT_HINTS_JSON"])
    assert hints == [{
        "ca_host": "braavos",
        "ca_name": r"braavos.essos.local\ESSOS-CA",
        "domain": "essos.local",
        "esc_type": "esc1",
        "template": "ESC1",
    }]


def test_purpose_range_ca_export_replanning_forces_prefix_and_releases_repairable_blocker():
    visible = orchestrate._scenario_restart_env("purpose-range-visible-cost")
    replanning = orchestrate._scenario_restart_env("purpose-range-ca-export-replanning")

    assert "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON" not in visible
    prefix = json.loads(replanning["SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON"])
    assert [item["capability"] for item in prefix] == [
        "read-managed-local-admin-secret",
        "use-managed-local-admin-secret",
        "execute-as-local-admin",
        "adcs-ca-private-key-export",
    ]
    assert prefix[-1]["release_on_failure"] is True
    blocker = json.loads(replanning["SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON"])
    assert blocker["capability"] == "adcs-ca-private-key-export"
    assert blocker["target_contains"] == "target=ca01;target_domain=range.local"
    assert blocker["failure_class"] == "transient"
    assert blocker["skip_if_achieved_effect"] == "endpoint-protection-adjusted:ca01@range.local"
    assert blocker["probe"]["tool_execution_failed"] is True
    assert blocker["probe"]["defender_blocked"] is True


def test_treatment_route_rejects_loopback_proxy(tmp_path):
    route = tmp_path / ".env.local"
    route.write_text(
        "SAGE_EVAL_PROVIDER=openai\n"
        "SAGE_EVAL_API_ENDPOINT=http://127.0.0.1:8100/v1\n"
        "SAGE_EVAL_API_KEY=secret\n"
        "SAGE_EVAL_SONNET_MODEL=sonnet\n"
    )

    with pytest.raises(SystemExit, match="may not use the loopback proxy"):
        orchestrate.load_treatment_route(route, "sonnet")


def test_treatment_route_loads_selected_model_without_exposing_secret(tmp_path):
    route = tmp_path / ".env.local"
    route.write_text(
        "SAGE_EVAL_PROVIDER=openai\n"
        "SAGE_EVAL_API_ENDPOINT=https://bedrock-proxy.example/v1\n"
        "SAGE_EVAL_API_KEY=secret\n"
        "SAGE_EVAL_SONNET_MODEL=sonnet\n"
        "SAGE_EVAL_HAIKU_MODEL=haiku\n"
    )

    loaded = orchestrate.load_treatment_route(route, "haiku")

    assert loaded == {
        "provider": "openai",
        "model": "haiku",
        "api_endpoint": "https://bedrock-proxy.example/v1",
        "api_key": "secret",
    }
