from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

common = importlib.import_module("arch_governor_common")
review_lease = importlib.import_module("review_lease")
hook = importlib.import_module("pre_tool_use_arch_gate")


@pytest.fixture(autouse=True)
def _use_repository_index(monkeypatch):
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=tmp_path,
        check=True,
    )
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def _freeze_args(root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        repo=str(root),
        paths=["source.py"],
        protected=[],
        review_stage="source_candidate",
        review_domain="conversation_behavior",
        independence_class="internal_subagent",
        mechanism_id="turn-authority",
        review_round=1,
        governing_gate="approved-gate",
    )


def test_review_lease_verifies_and_detects_drift(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)

    assert review_lease.freeze(_freeze_args(root)) == 0
    assert review_lease.verify(argparse.Namespace(repo=str(root))) == 0
    lease = json.loads(common.review_lease_path(root).read_text())

    source.write_text("value = 3\n", encoding="utf-8")
    assert review_lease.verify(argparse.Namespace(repo=str(root))) == 3
    assert "invalidated_candidate_drift" in capsys.readouterr().out

    assert (
        review_lease.close(
            argparse.Namespace(
                repo=str(root),
                lease_id=lease["lease_id"],
                disposition="invalidated",
            )
        )
        == 0
    )
    assert not common.review_lease_path(root).exists()


def test_close_keeps_active_lease_temporary_and_archives_receipt_durably(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path)
    lease_dir = tmp_path / "temporary-leases"
    history = tmp_path / "private-history"
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(lease_dir))
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(history))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)

    assert review_lease.freeze(_freeze_args(root)) == 0
    lease_path = common.review_lease_path(root)
    assert lease_path.is_relative_to(lease_dir)
    lease = json.loads(lease_path.read_text())
    capsys.readouterr()

    assert (
        review_lease.close(
            argparse.Namespace(
                repo=str(root),
                lease_id=lease["lease_id"],
                disposition="accepted",
            )
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    archive = Path(output["archive"])
    assert not lease_path.exists()
    assert archive.is_relative_to(history)
    assert archive.name.endswith(".closed.json")
    assert archive.stat().st_mode & 0o777 == 0o600
    assert (history / "manifest.jsonl").is_file()


def test_review_lease_detects_unrelated_index_drift(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
    unrelated = root / "unrelated.txt"
    unrelated.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=root, check=True)

    assert review_lease.freeze(_freeze_args(root)) == 0
    capsys.readouterr()
    unrelated.write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=root, check=True)

    assert review_lease.verify(argparse.Namespace(repo=str(root))) == 3
    output = json.loads(capsys.readouterr().out)
    assert any(row["field"] == "index_sha256" for row in output["drift"])


def test_freeze_refuses_unstaged_and_split_candidate_bytes(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"

    source.write_text("value = 2\n", encoding="utf-8")
    assert review_lease.freeze(_freeze_args(root)) == 2

    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
    source.write_text("value = 3\n", encoding="utf-8")
    assert review_lease.freeze(_freeze_args(root)) == 2


def test_hook_blocks_write_to_active_review_lease(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
    assert review_lease.freeze(_freeze_args(root)) == 0

    patch = (
        "*** Begin Patch\n"
        "*** Update File: source.py\n"
        "@@\n"
        "-value = 2\n"
        "+value = 3\n"
        "*** End Patch\n"
    )
    _code, payload = hook.run_hook(
        {"tool_name": "apply_patch", "tool_input": {"command": patch}}
    )
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "active Sage review lease" in payload["hookSpecificOutput"][
        "permissionDecisionReason"
    ]

    _code, index_payload = hook.run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git add --all"},
        }
    )
    assert index_payload["hookSpecificOutput"]["permissionDecision"] == "deny"

    for command in (
        "echo changed>source.py",
        "printf changed >>source.py",
        "command 2>source.py",
        f'echo changed>"{source}"',
    ):
        _code, redirect_payload = hook.run_hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
        )
        assert (
            redirect_payload["hookSpecificOutput"]["permissionDecision"]
            == "deny"
        )


def test_hook_blocks_explicit_shell_writers_and_parent_removal(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    frozen_dir = root / "frozen"
    frozen_dir.mkdir()
    source = root / "source.py"
    source.rename(frozen_dir / "source.py")
    protected = frozen_dir / "protected.txt"
    protected.write_text("do not change\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    args = _freeze_args(root)
    args.paths = ["frozen/source.py"]
    args.protected = ["frozen/protected.txt"]
    assert review_lease.freeze(args) == 0

    for command in (
        "printf changed|tee frozen/source.py",
        "printf changed | tee -a ./frozen/protected.txt",
        "truncate -s 0 frozen/source.py",
        "dd if=/dev/zero of=frozen/protected.txt bs=1 count=1",
        "rm frozen",
    ):
        _code, payload = hook.run_hook(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_shell_writer_positive_controls_do_not_claim_source_arguments(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
    assert review_lease.freeze(_freeze_args(root)) == 0

    for command in (
        "printf source.py",
        "printf changed | tee unrelated.txt",
        "truncate --reference=source.py unrelated.txt",
        "dd if=source.py of=unrelated.txt",
        "rm unrelated",
        "cp source.py unrelated.txt",
        "cat ./source.py",
    ):
        _code, payload = hook.run_hook(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )
        assert payload == {}


def test_authority_and_chat_lifecycle_paths_are_high_risk(tmp_path):
    paths = common.high_risk_paths(
        [
            "Payload_Type/sage/ai/langgraph/turn_authority.py",
            "Payload_Type/sage/ai/langgraph/objective_contract.py",
            "Payload_Type/sage/sage_chat/service.py",
        ],
        tmp_path,
    )
    assert paths == [
        "Payload_Type/sage/ai/langgraph/objective_contract.py",
        "Payload_Type/sage/ai/langgraph/turn_authority.py",
        "Payload_Type/sage/sage_chat/service.py",
    ]


def test_dot_prefixed_repo_paths_are_preserved(tmp_path):
    assert (
        common.normalize_repo_path(
            ".codex/agents/sage_eval_reviewer.toml", tmp_path
        )
        == ".codex/agents/sage_eval_reviewer.toml"
    )
    assert common.matches_any(
        ".codex/agents/sage_eval_reviewer.toml",
        [".codex/agents/**"],
    )
