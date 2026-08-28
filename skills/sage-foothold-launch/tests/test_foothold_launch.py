from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "foothold_launch.py"


@pytest.fixture
def foothold_launch():
    spec = importlib.util.spec_from_file_location("foothold_launch_under_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_local_run_as_password_is_loaded(foothold_launch, monkeypatch, tmp_path):
    skill_env = tmp_path / "skill.env"
    skill_env.write_text("SAGE_RUN_AS_PASSWORD='skill-secret'\n", encoding="utf-8")
    monkeypatch.delenv("SAGE_RUN_AS_PASSWORD", raising=False)
    monkeypatch.setattr(foothold_launch, "SKILL_ENV", skill_env)
    monkeypatch.setattr(foothold_launch, "SAGE_ENV", tmp_path / "missing-runtime.env")

    assert foothold_launch._env()["SAGE_RUN_AS_PASSWORD"] == "skill-secret"


def test_process_environment_wins_over_local_files(foothold_launch, monkeypatch, tmp_path):
    skill_env = tmp_path / "skill.env"
    runtime_env = tmp_path / "runtime.env"
    skill_env.write_text("SAGE_RUN_AS_PASSWORD=skill-secret\n", encoding="utf-8")
    runtime_env.write_text("SAGE_RUN_AS_PASSWORD=runtime-secret\n", encoding="utf-8")
    monkeypatch.setenv("SAGE_RUN_AS_PASSWORD", "process-secret")
    monkeypatch.setattr(foothold_launch, "SKILL_ENV", skill_env)
    monkeypatch.setattr(foothold_launch, "SAGE_ENV", runtime_env)

    assert foothold_launch._env()["SAGE_RUN_AS_PASSWORD"] == "process-secret"


def test_skill_local_file_wins_over_runtime_fallback(foothold_launch, monkeypatch, tmp_path):
    skill_env = tmp_path / "skill.env"
    runtime_env = tmp_path / "runtime.env"
    skill_env.write_text("SAGE_RUN_AS_PASSWORD=skill-secret\n", encoding="utf-8")
    runtime_env.write_text("SAGE_RUN_AS_PASSWORD=runtime-secret\n", encoding="utf-8")
    monkeypatch.delenv("SAGE_RUN_AS_PASSWORD", raising=False)
    monkeypatch.setattr(foothold_launch, "SKILL_ENV", skill_env)
    monkeypatch.setattr(foothold_launch, "SAGE_ENV", runtime_env)

    assert foothold_launch._env()["SAGE_RUN_AS_PASSWORD"] == "skill-secret"


def test_empty_skill_value_falls_back_without_injecting_empty(foothold_launch, monkeypatch, tmp_path):
    skill_env = tmp_path / "skill.env"
    runtime_env = tmp_path / "runtime.env"
    skill_env.write_text("SAGE_RUN_AS_PASSWORD=\n", encoding="utf-8")
    runtime_env.write_text('SAGE_RUN_AS_PASSWORD="runtime-secret"\n', encoding="utf-8")
    monkeypatch.delenv("SAGE_RUN_AS_PASSWORD", raising=False)
    monkeypatch.setattr(foothold_launch, "SKILL_ENV", skill_env)
    monkeypatch.setattr(foothold_launch, "SAGE_ENV", runtime_env)

    assert foothold_launch._env()["SAGE_RUN_AS_PASSWORD"] == "runtime-secret"


def test_missing_or_empty_sources_leave_password_unset(foothold_launch, monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# SAGE_RUN_AS_PASSWORD is intentionally unset\n", encoding="utf-8")
    monkeypatch.delenv("SAGE_RUN_AS_PASSWORD", raising=False)
    monkeypatch.setattr(foothold_launch, "SKILL_ENV", empty_env)
    monkeypatch.setattr(foothold_launch, "SAGE_ENV", tmp_path / "missing-runtime.env")

    assert "SAGE_RUN_AS_PASSWORD" not in foothold_launch._env()
