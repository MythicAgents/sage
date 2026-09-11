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
    monkeypatch.setattr(foothold_launch._RUN_AS_RESOLVER, "SKILL_ENV", skill_env)
    monkeypatch.setattr(
        foothold_launch._RUN_AS_RESOLVER, "RUNTIME_ENV", tmp_path / "missing-runtime.env"
    )

    assert foothold_launch._env()["SAGE_RUN_AS_PASSWORD"] == "skill-secret"


def test_process_environment_wins_over_local_files(foothold_launch, monkeypatch, tmp_path):
    skill_env = tmp_path / "skill.env"
    runtime_env = tmp_path / "runtime.env"
    skill_env.write_text("SAGE_RUN_AS_PASSWORD=skill-secret\n", encoding="utf-8")
    runtime_env.write_text("SAGE_RUN_AS_PASSWORD=runtime-secret\n", encoding="utf-8")
    monkeypatch.setenv("SAGE_RUN_AS_PASSWORD", "process-secret")
    monkeypatch.setattr(foothold_launch._RUN_AS_RESOLVER, "SKILL_ENV", skill_env)
    monkeypatch.setattr(foothold_launch._RUN_AS_RESOLVER, "RUNTIME_ENV", runtime_env)

    assert foothold_launch._env()["SAGE_RUN_AS_PASSWORD"] == "process-secret"


def test_skill_local_file_wins_over_runtime_fallback(foothold_launch, monkeypatch, tmp_path):
    skill_env = tmp_path / "skill.env"
    runtime_env = tmp_path / "runtime.env"
    skill_env.write_text("SAGE_RUN_AS_PASSWORD=skill-secret\n", encoding="utf-8")
    runtime_env.write_text("SAGE_RUN_AS_PASSWORD=runtime-secret\n", encoding="utf-8")
    monkeypatch.delenv("SAGE_RUN_AS_PASSWORD", raising=False)
    monkeypatch.setattr(foothold_launch._RUN_AS_RESOLVER, "SKILL_ENV", skill_env)
    monkeypatch.setattr(foothold_launch._RUN_AS_RESOLVER, "RUNTIME_ENV", runtime_env)

    assert foothold_launch._env()["SAGE_RUN_AS_PASSWORD"] == "skill-secret"


def test_empty_skill_value_falls_back_without_injecting_empty(foothold_launch, monkeypatch, tmp_path):
    skill_env = tmp_path / "skill.env"
    runtime_env = tmp_path / "runtime.env"
    skill_env.write_text("SAGE_RUN_AS_PASSWORD=\n", encoding="utf-8")
    runtime_env.write_text('SAGE_RUN_AS_PASSWORD="runtime-secret"\n', encoding="utf-8")
    monkeypatch.delenv("SAGE_RUN_AS_PASSWORD", raising=False)
    monkeypatch.setattr(foothold_launch._RUN_AS_RESOLVER, "SKILL_ENV", skill_env)
    monkeypatch.setattr(foothold_launch._RUN_AS_RESOLVER, "RUNTIME_ENV", runtime_env)

    assert foothold_launch._env()["SAGE_RUN_AS_PASSWORD"] == "runtime-secret"


def test_missing_or_empty_sources_leave_password_unset(foothold_launch, monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# SAGE_RUN_AS_PASSWORD is intentionally unset\n", encoding="utf-8")
    monkeypatch.delenv("SAGE_RUN_AS_PASSWORD", raising=False)
    monkeypatch.setattr(foothold_launch._RUN_AS_RESOLVER, "SKILL_ENV", empty_env)
    monkeypatch.setattr(
        foothold_launch._RUN_AS_RESOLVER, "RUNTIME_ENV", tmp_path / "missing-runtime.env"
    )

    assert "SAGE_RUN_AS_PASSWORD" not in foothold_launch._env()


def test_blocked_callback_readiness_reaches_no_launch_mutation(foothold_launch, monkeypatch):
    args = foothold_launch.build_parser().parse_args(["launch"])
    calls = []
    monkeypatch.setattr(
        foothold_launch._READINESS,
        "poll_readiness",
        lambda *_args, **_kwargs: {
            "schema": "sage-readiness-decision-v1",
            "lane": "callback",
            "ready": False,
            "launch_released": False,
            "blockers": ["clock: not ready"],
        },
    )
    monkeypatch.setattr(foothold_launch, "inspect_foothold", lambda: calls.append("inspect"))
    monkeypatch.setattr(
        foothold_launch,
        "grant_batch_logon_right",
        lambda *_args: calls.append("grant"),
    )
    monkeypatch.setattr(foothold_launch, "run", lambda *_args, **_kwargs: calls.append("deploy"))

    result = foothold_launch.do_launch(args)

    assert result["verdict"] == "readiness_blocked"
    assert calls == []


def test_rebuild_rejects_unsafe_timeout_before_rollback(foothold_launch, monkeypatch):
    args = foothold_launch.build_parser().parse_args(
        ["rebuild", "--boot-wait-seconds", "35"]
    )
    calls = []
    monkeypatch.setattr(foothold_launch, "run", lambda *_args, **_kwargs: calls.append("run"))

    with pytest.raises(ValueError, match="strictly greater"):
        foothold_launch.do_rebuild(args)

    assert calls == []


def test_rebuild_blocked_boot_reaches_no_clock_or_launch(foothold_launch, monkeypatch):
    args = foothold_launch.build_parser().parse_args(["rebuild"])
    runs = []
    launches = []
    monkeypatch.setattr(
        foothold_launch._READINESS,
        "poll_readiness",
        lambda *_args, **_kwargs: {"ready": False, "blockers": ["missing IP"]},
    )
    monkeypatch.setattr(
        foothold_launch,
        "run",
        lambda cmd, **_kwargs: runs.append(cmd) or {"rc": 0, "stdout": ""},
    )
    monkeypatch.setattr(
        foothold_launch,
        "do_launch",
        lambda _args: launches.append("launch"),
    )

    result = foothold_launch.do_rebuild(args)

    assert result["verdict"] == "readiness_blocked"
    assert len(runs) == 2
    assert launches == []


def test_rebuild_no_json_clock_result_reaches_no_launch(foothold_launch, monkeypatch):
    args = foothold_launch.build_parser().parse_args(["rebuild"])
    runs = []
    launches = []
    monkeypatch.setattr(
        foothold_launch._READINESS,
        "poll_readiness",
        lambda *_args, **_kwargs: {"ready": True, "blockers": []},
    )

    def fake_run(cmd, **_kwargs):
        runs.append(cmd)
        return {"rc": 0, "stdout": "", "json": None}

    monkeypatch.setattr(foothold_launch, "run", fake_run)
    monkeypatch.setattr(
        foothold_launch,
        "do_launch",
        lambda _args: launches.append("launch"),
    )

    result = foothold_launch.do_rebuild(args)

    assert result["verdict"] == "readiness_blocked"
    assert len(runs) == 3
    assert launches == []
