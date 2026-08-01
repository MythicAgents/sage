"""The relaunch helper must record the identity Sage will actually run under.

`_sage_relaunch.py` execs `main.py`, which calls `load_sage_dotenv()` afterwards — so settings in
`Payload_Type/sage/.env` are not in the exec environment. Recording identity from that environment
alone made readiness report `provider is not recorded` on every local run configured the documented
way, while Sage was correctly configured the whole time. A gate that cries wolf trains you past it.

Lives here rather than beside the other readiness tests because the maintained offline suite collects
only `Payload_Type/sage/tests` and `tests/repo_hygiene`; a guard outside that is a guard that does not
run. `test_bootstrap_payloads_script.py` sets the precedent for skill-script tests in this directory.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SAGE_ROOT = REPO_ROOT / "Payload_Type" / "sage"
RELAUNCH_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "_sage_relaunch.py"


def _relaunch_module():
    spec = importlib.util.spec_from_file_location("sage_relaunch_under_test", RELAUNCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sage_dir(tmp_path: Path) -> Path:
    """A throwaway Sage directory carrying the real dotenv_bootstrap, so the precedence rules
    under test are Sage's own rather than a copy that can drift."""
    shutil.copy(SAGE_ROOT / "dotenv_bootstrap.py", tmp_path / "dotenv_bootstrap.py")
    return tmp_path


def _write_env(directory: Path, body: str) -> None:
    (directory / ".env").write_text(body, encoding="utf-8")


def test_dotenv_settings_reach_the_recorded_identity(sage_dir: Path):
    _write_env(
        sage_dir,
        "provider=openai\nmodel=bedrock-claude-4-6-sonnet\nAPI_ENDPOINT=https://example.invalid:7443\n",
    )
    merged = _relaunch_module().identity_env({"PATH": "/usr/bin"}, str(sage_dir))
    assert merged["provider"] == "openai"
    assert merged["model"] == "bedrock-claude-4-6-sonnet"
    assert merged["API_ENDPOINT"] == "https://example.invalid:7443"
    assert merged["PATH"] == "/usr/bin", "exec environment entries must survive the merge"


def test_local_override_reaches_the_recorded_identity(sage_dir: Path):
    """Local development configures `.env.local`, so identity blind to it is identity blind, period.

    This is the regression the fix would quietly suffer if the recorder kept its own file list: the
    values simply move to a file it never learned to read, and readiness goes back to reporting a
    correctly-configured Sage as unconfigured.
    """
    _write_env(sage_dir, "provider=anthropic\nmodel=shipped\n")
    (sage_dir / ".env.local").write_text("provider=openai\n", encoding="utf-8")

    merged = _relaunch_module().identity_env({}, str(sage_dir))
    assert merged["provider"] == "openai", ".env.local must win in the identity record too"
    assert merged["model"] == "shipped"


def test_exec_environment_wins_over_the_dotenv_file(sage_dir: Path):
    """Mythic injects configuration into the container, and a stale file must never shadow it.
    That is the precedence Sage itself applies, so identity must record the same winner."""
    _write_env(sage_dir, "provider=openai\n")
    merged = _relaunch_module().identity_env({"provider": "bedrock"}, str(sage_dir))
    assert merged["provider"] == "bedrock"


def test_empty_dotenv_value_sets_nothing(sage_dir: Path):
    _write_env(sage_dir, "provider=\n")
    merged = _relaunch_module().identity_env({}, str(sage_dir))
    assert "provider" not in merged


def test_caller_environment_is_not_mutated(sage_dir: Path):
    """The exec environment must stay byte-identical; only the recorded copy gains the settings."""
    _write_env(sage_dir, "provider=openai\n")
    exec_env = {"PATH": "/usr/bin"}
    merged = _relaunch_module().identity_env(exec_env, str(sage_dir))
    assert exec_env == {"PATH": "/usr/bin"}
    assert "provider" in merged


@pytest.mark.parametrize(
    "breakage",
    [
        pytest.param(lambda d: (d / ".env").unlink(), id="missing_dotenv_file"),
        pytest.param(lambda d: (d / "dotenv_bootstrap.py").unlink(), id="missing_bootstrap"),
        pytest.param(
            lambda d: (d / "dotenv_bootstrap.py").write_text("raise RuntimeError('boom')\n"),
            id="bootstrap_raises",
        ),
    ],
)
def test_identity_recording_never_blocks_a_relaunch(sage_dir: Path, breakage):
    """Sage coming back up matters more than bookkeeping, so every failure degrades to the exec env.

    Covered as a class rather than one example: three releases of a different upstream renderer each
    shipped a different exception type from the same block, which an example-shaped test would miss.
    """
    _write_env(sage_dir, "provider=openai\n")
    breakage(sage_dir)
    merged = _relaunch_module().identity_env({"PATH": "/usr/bin"}, str(sage_dir))
    assert merged == {"PATH": "/usr/bin"}
