"""`sage_deployment.py` container deploy + parity (build-layer tests: no Docker, no Mythic).

Parity answers one question that is invisible to the eye and has failed twice: **is the container
executing the code you just wrote?** The Docker image cannot answer it, because Mythic's generated
compose file bind-mounts the installed service directory over `/Mythic/` and shadows the application
code the image baked in. So the check reads from the container and compares against the working tree.

These tests cover the parts that can be exercised without a running container: the digest that
decides "same code or not", the excludes that keep runtime databases out of the comparison, and the
two fail-closed guards. The Docker-facing paths are deliberately not mocked into a fake pass — a
mock of `docker exec` would assert only that the mock was called.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sage_deployment  # noqa: E402


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A miniature service tree carrying one file of every kind parity cares about."""
    root = tmp_path / "sage"
    (root / "ai" / "langgraph").mkdir(parents=True)
    (root / "sage_chat").mkdir()
    (root / "prompts").mkdir()
    (root / "ai" / "langgraph" / "model.py").write_text("x = 1\n")
    (root / "sage_chat" / "service.py").write_text("y = 2\n")
    (root / "prompts" / "supervisor.md").write_text("be helpful\n")
    (root / "main.py").write_text("run()\n")
    (root / "mcp_tool_policy.json").write_text("{}\n")
    return root


def test_the_digest_is_deterministic(tree: Path):
    """Two reads of one unchanged tree must agree, or every comparison is noise."""
    assert sage_deployment._digest_local(tree)[0] == sage_deployment._digest_local(tree)[0]


def test_the_digest_is_independent_of_location(tree: Path, tmp_path: Path):
    """The repo and the container hold the same code at different paths.

    If the digest folded in absolute paths it would ALWAYS differ and parity would be permanently
    red — which reads as "deploy again" forever rather than as a broken check.
    """
    elsewhere = tmp_path / "another" / "place" / "sage"
    elsewhere.parent.mkdir(parents=True)
    shutil.copytree(tree, elsewhere)
    assert sage_deployment._digest_local(tree)[0] == sage_deployment._digest_local(elsewhere)[0]


def test_the_digest_notices_a_one_line_change(tree: Path):
    """The falsifier. A check that cannot see an edit is worse than no check."""
    before = sage_deployment._digest_local(tree)[0]
    target = tree / "ai" / "langgraph" / "model.py"
    target.write_text(target.read_text() + "# drift\n")
    assert sage_deployment._digest_local(tree)[0] != before


def test_the_digest_ignores_runtime_databases(tree: Path):
    """The container writes its own sage.db into the mounted directory as it runs.

    Counting that would make parity go red on its own accord seconds after a successful deploy.
    """
    before = sage_deployment._digest_local(tree)[0]
    (tree / "sage.db").write_bytes(b"\x00" * 64)
    (tree / "sage_20260101-0000.db.zst").write_bytes(b"\x01" * 64)
    assert sage_deployment._digest_local(tree)[0] == before


def test_the_digest_ignores_tests_and_evals(tree: Path):
    """Only runtime code is compared; a test edit must not read as a stale container."""
    before = sage_deployment._digest_local(tree)[0]
    (tree / "tests").mkdir()
    (tree / "tests" / "test_thing.py").write_text("assert True\n")
    assert sage_deployment._digest_local(tree)[0] == before


def test_the_installed_directory_is_never_guessed(monkeypatch):
    """Publishable-by-default: external locations come from an env var and fail closed.

    Guessing a checkout by name silently deploys into the wrong Mythic on a machine with two.
    """
    monkeypatch.delenv("MYTHIC_ENV_PATH", raising=False)
    with pytest.raises(SystemExit) as caught:
        sage_deployment._installed_service_dir()
    assert "MYTHIC_ENV_PATH" in str(caught.value), caught.value


def test_deploy_refuses_unless_container_mode_is_explicit(monkeypatch, capsys):
    """The safety gate. Container mode is the exception; local tmux is the default workflow.

    A deploy that ran just because the mode variable was unset would overwrite the installed tree
    while the operator believed they were working locally.
    """
    monkeypatch.delenv("SAGE_DEPLOYMENT_MODE", raising=False)
    with pytest.raises(SystemExit) as caught:
        sage_deployment.main(["deploy"])
    message = str(caught.value)
    assert "container" in message and "local" in message, message


def _stub_deploy(monkeypatch, *, local_running: bool) -> list[str]:
    """Run `deploy` with its own side-effecting helpers replaced, recording the order they fire.

    The substitutions are at this module's seams, not at Docker's — the test drives the real
    `main`, real argument parsing and real control flow, and only the four functions that touch the
    world are stubbed. That keeps it a behavioural test rather than an assertion about source text.
    """
    calls: list[str] = []

    def record(name: str, result: dict):
        def _fn(*a, **k):
            calls.append(name)
            return result
        return _fn

    monkeypatch.setattr(sage_deployment, "_sync_into_mythic", record("sync", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_stop_local", record("stop_local", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_restart_container", record("restart", {"returncode": 0}))
    monkeypatch.setattr(
        sage_deployment, "_parity",
        record("parity", {"ready": True, "blockers": [], "repo_digest": "x"}),
    )
    monkeypatch.setattr(
        sage_deployment.readiness_contract, "sage_deployment_status",
        lambda **kw: {"local_process_running": local_running, "blockers": [], "ready": True},
    )
    sage_deployment.main(["deploy", "--mode", "container"])
    return calls


def test_deploy_stops_a_running_local_sage_before_restarting(monkeypatch, capsys):
    """A deploy must not create the split brain the rest of this module exists to prevent.

    Both processes register as the `sage` service and one wins the RabbitMQ queue, so deploying
    while a tmux Sage is up would leave parity measuring a container that is not the process
    answering Mythic. Order matters: the local Sage must stop BEFORE the container returns.
    """
    calls = _stub_deploy(monkeypatch, local_running=True)
    assert "stop_local" in calls, f"deploy did not stop the running local Sage: {calls}"
    assert calls.index("stop_local") < calls.index("restart"), (
        f"local Sage was stopped after the container came back; both were briefly live: {calls}"
    )


def test_deploy_leaves_a_stopped_local_sage_alone(monkeypatch, capsys):
    """The control. If stop fired unconditionally the test above would pass while proving nothing."""
    calls = _stub_deploy(monkeypatch, local_running=False)
    assert "stop_local" not in calls, f"deploy stopped a local Sage that was not running: {calls}"
    assert calls == ["sync", "restart", "parity"], calls


def test_sync_excludes_cover_every_database_spelling():
    """The live database must survive `rsync --delete`.

    rsync protects excluded files on the receiver from deletion, so this list is what stands between
    a deploy and destroying the running container's state. The three spellings all occur in practice.
    """
    for name in ("sage.db", "sage.db-wal", "sage_20260101-0000.db.zst"):
        assert any(
            _matches(name, pattern) for pattern in sage_deployment._SYNC_EXCLUDES
        ), f"{name} is not excluded from the sync"


def _matches(name: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(name, pattern)
