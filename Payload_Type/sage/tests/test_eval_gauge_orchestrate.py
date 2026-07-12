import subprocess
import importlib.util
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


def test_foothold_launch_env_maps_alternate_password_source(monkeypatch):
    monkeypatch.setenv("SAGE_RUN_AS_PASSWORD", "samwell-password")
    monkeypatch.setenv("SAGE_MEEREEN_JORAH_PASSWORD", "jorah-password")

    foothold = orchestrate.FootholdSpec(password_env="SAGE_MEEREEN_JORAH_PASSWORD")

    assert foothold.launch_env()["SAGE_RUN_AS_PASSWORD"] == "jorah-password"


def test_run_side_pins_same_netbios_map_for_all_policy_arms(monkeypatch):
    seen = {}

    monkeypatch.setattr(orchestrate, "_run", lambda *args, **kwargs: None)

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold):
        del snapshot, retained_callback_config, foothold
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
