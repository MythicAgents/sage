"""Operator CLI behavior; container and protocol checks live with the broker."""

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect_artifact.py"


def _load():
    spec = importlib.util.spec_from_file_location("inspection_cli_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(tmp_path, profile="static-v1", recipe="metadata"):
    return [str(tmp_path / "artifact.exe"), "--sha256", "a" * 64,
            "--image-id", "sha256:" + "b" * 64, "--profile", profile,
            "--recipe", recipe, "--cache-dir", str(tmp_path / "cache")]


def test_parser_error_is_preserved_as_observation(tmp_path, monkeypatch, capsys):
    cli = _load()
    captured = {}

    def inspect(path, **kwargs):
        captured.update(kwargs)
        return {"status": "nonzero_exit", "exit_code": 64, "stdout": "usage",
                "stderr": "unknown option", "cache_hit": False}

    monkeypatch.setattr(cli, "inspect_artifact", inspect)
    assert cli.main(_args(tmp_path, "certify-help-v1", "request")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["exit_code"] == 64
    assert result["status"] == "nonzero_exit"
    assert captured["limits"] == cli.WINE_LIMITS
    assert captured["expected_sha256"] == "a" * 64


def test_timeout_uses_profile_limits_and_writes_new_result(tmp_path, monkeypatch):
    cli = _load()
    output = tmp_path / "result.json"
    captured = {}

    def inspect(path, **kwargs):
        captured.update(kwargs)
        return {"status": "timeout", "exit_code": None, "timed_out": True}

    monkeypatch.setattr(cli, "inspect_artifact", inspect)
    assert cli.main(_args(tmp_path) + ["--timeout", "5", "--output", str(output)]) == 1
    assert json.loads(output.read_text())["timed_out"] is True
    assert captured["limits"].wall_seconds == 5
    assert captured["limits"].memory_mb == cli.Limits().memory_mb


def test_existing_output_refuses_before_workload(tmp_path, monkeypatch):
    cli = _load()
    output = tmp_path / "result.json"
    output.write_text("original")
    monkeypatch.setattr(cli, "inspect_artifact", lambda *a, **k: pytest.fail("workload started"))
    assert cli.main(_args(tmp_path) + ["--output", str(output)]) == 2
    assert output.read_text() == "original"


def test_unavailable_image_does_not_become_success(tmp_path, monkeypatch, capsys):
    cli = _load()

    def unavailable(*args, **kwargs):
        raise cli.InspectionError("inspection image unavailable")

    monkeypatch.setattr(cli, "inspect_artifact", unavailable)
    assert cli.main(_args(tmp_path)) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "unavailable"


def test_inert_fixture_profile_is_not_operator_artifact_interface(tmp_path):
    cli = _load()
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(_args(tmp_path, "fixture-v1", "native"))
    assert error.value.code == 2
