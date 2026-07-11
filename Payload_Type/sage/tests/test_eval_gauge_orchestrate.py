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
