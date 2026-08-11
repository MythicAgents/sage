"""A deploy must not delete the credentials an operator set through the Mythic UI.

Sage ships `Payload_Type/sage/.env` **tracked and inert** for one reason: a Mythic operator can open
it on the installed-services page, fill in credentials, save, and restart — no shell, no `docker cp`,
no sudo. That file in the installed copy is therefore operator-owned state, exactly like the live
`sage.db` the sync already refuses to touch.

`sage_deployment.py deploy` mirrors the working tree with `rsync -a --delete`, and the repo copy is
fully commented out. Without an exclusion it overwrites the operator's file with a blank one, and it
does so **silently**: the container comes back healthy, chat still answers, and BloodHound has simply
vanished with nothing in the log to say why. That is the failure this file exists to prevent, and it
is the same shape as the defect the rest of this ISA is about — a real loss with no signal.

The seeding half matters as much as the exclusion. The shipped `.env` is not merely a template: it
carries `LANGGRAPH_STRICT_MSGPACK=true`, a security default that is unsafe when ABSENT rather than
merely unset. So a first deploy into an empty install must still receive it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_deployment.py"


@pytest.fixture(scope="module")
def deployment():
    """Load the real script, with its own directory importable for its sibling imports."""
    sys.path.insert(0, str(DEPLOY_PATH.parent))
    spec = importlib.util.spec_from_file_location("sage_deployment_under_test", DEPLOY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_env_files_are_excluded_from_the_sync(deployment):
    """The exclusion itself. `rsync --delete` does not remove excluded files on the receiver."""
    assert ".env" in deployment._SYNC_EXCLUDES
    assert ".env.local" in deployment._SYNC_EXCLUDES


def test_repo_env_would_have_blanked_an_operator_file(deployment):
    """The control for the test above: prove the shipped file is the destructive payload.

    If the tracked `.env` ever started carrying real values, the exclusion would be protecting
    nothing and this suite would be asserting a property that no longer matters — while also meaning
    credentials had been committed to a public repository.
    """
    shipped = (REPO_ROOT / "Payload_Type" / "sage" / ".env").read_text(encoding="utf-8")
    live_bloodhound_lines = [
        line for line in shipped.splitlines() if line.startswith("BLOODHOUND_") and line.split("=", 1)[1].strip()
    ]
    assert not live_bloodhound_lines, "the tracked .env must ship credential-free"


def test_missing_env_is_seeded_on_a_first_deploy(deployment, tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / ".env").write_text("LANGGRAPH_STRICT_MSGPACK=true\n", encoding="utf-8")

    seeded = deployment._seed_missing_env_files(source, destination)

    assert seeded == [".env"]
    assert "LANGGRAPH_STRICT_MSGPACK=true" in (destination / ".env").read_text(encoding="utf-8")


def test_an_existing_env_is_never_overwritten(deployment, tmp_path: Path):
    """The whole point: the operator's values outlive every deploy."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / ".env").write_text("#BLOODHOUND_DOMAIN=\n", encoding="utf-8")
    (destination / ".env").write_text("BLOODHOUND_DOMAIN=bh.operator.example\n", encoding="utf-8")

    seeded = deployment._seed_missing_env_files(source, destination)

    assert seeded == []
    assert "bh.operator.example" in (destination / ".env").read_text(encoding="utf-8")
