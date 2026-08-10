"""Sage's committed `.env` and the loader that reads it.

The file is tracked on purpose so a Mythic operator can edit it in place from the web UI container
file browser rather than needing a shell to copy an example. That makes two properties safety
critical rather than cosmetic:

* it must ship setting NOTHING, or cloning Sage would silently reconfigure someone's container; and
* it must never contain a real credential, because it is committed to a public repository.

Both are asserted here against the actual file, not against a fixture.

Mirrors the repo's no-pytest-asyncio convention. No filesystem writes outside tmp_path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv_bootstrap import apply_dotenv, dotenv_paths, load_sage_dotenv  # noqa: E402

SAGE_ROOT = Path(__file__).resolve().parents[1]
DOTENV = SAGE_ROOT / ".env"

# The one class of value allowed to ship SET rather than commented: a security default whose absence
# is itself the unsafe state. LangGraph defaults LANGGRAPH_STRICT_MSGPACK to false, and false means a
# callable stored in checkpoint data is imported and executed on load, so a commented line here would
# ship the vulnerable configuration. Everything else in the file stays inert.
#
# This is an allowlist, not a relaxation: the value is pinned exactly, so a second uncommented
# variable, a different value for this one, or any credential still fails.
DOTENV_ALLOWED_ACTIVE = {"LANGGRAPH_STRICT_MSGPACK": "true"}


def test_existing_environment_wins():
    """Mythic injects RABBITMQ_HOST and friends; a stale file must never shadow them."""
    environ = {"RABBITMQ_HOST": "mythic_rabbitmq"}
    applied = apply_dotenv({"RABBITMQ_HOST": "127.0.0.1"}, environ)

    assert environ["RABBITMQ_HOST"] == "mythic_rabbitmq", "the container environment must win"
    assert applied == []


def test_unset_variables_are_filled():
    environ: dict[str, str] = {}
    applied = apply_dotenv({"BLOODHOUND_DOMAIN": "bh.range.local"}, environ)

    assert environ == {"BLOODHOUND_DOMAIN": "bh.range.local"}
    assert applied == ["BLOODHOUND_DOMAIN"]


def test_empty_values_set_nothing():
    """`KEY=` must not create an empty variable.

    Downstream reads treat presence as "configured", so an empty string would disable a fallback
    instead of leaving it alone — an operator uncommenting a line and leaving it blank would break
    the very setting they were enabling.
    """
    environ: dict[str, str] = {}
    applied = apply_dotenv({"BLOODHOUND_DOMAIN": "", "BLOODHOUND_PORT": None}, environ)

    assert environ == {}, "an empty or valueless entry must set nothing at all"
    assert applied == []


def test_missing_file_is_a_no_op(tmp_path):
    assert load_sage_dotenv(str(tmp_path)) == []


def test_loader_reads_a_real_file(tmp_path):
    (tmp_path / ".env").write_text(
        "# comment\nBLOODHOUND_DOMAIN=bh.range.local\nBLOODHOUND_PORT=\n", encoding="utf-8"
    )
    import os

    for key in ("BLOODHOUND_DOMAIN", "BLOODHOUND_PORT"):
        os.environ.pop(key, None)
    try:
        applied = load_sage_dotenv(str(tmp_path))
        assert applied == ["BLOODHOUND_DOMAIN"], "comments and empty values must be skipped"
        assert os.environ["BLOODHOUND_DOMAIN"] == "bh.range.local"
        assert "BLOODHOUND_PORT" not in os.environ
    finally:
        for key in ("BLOODHOUND_DOMAIN", "BLOODHOUND_PORT"):
            os.environ.pop(key, None)


def test_local_override_is_searched_before_the_committed_file(tmp_path):
    (tmp_path / ".env").touch()
    (tmp_path / ".env.local").touch()

    assert [Path(p).name for p in dotenv_paths(str(tmp_path))] == [".env.local", ".env"], (
        "load order IS the precedence rule; reversing it silently hands the tracked template "
        "authority over an operator's local configuration"
    )


def test_missing_files_are_omitted_from_the_search(tmp_path):
    assert dotenv_paths(str(tmp_path)) == []

    (tmp_path / ".env").touch()
    assert [Path(p).name for p in dotenv_paths(str(tmp_path))] == [".env"]


def test_local_override_wins_over_the_committed_file(tmp_path):
    """The whole point of the split: `.env.local` carries local/secret values, `.env` ships inert."""
    (tmp_path / ".env").write_text("provider=anthropic\nmodel=shipped\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("provider=openai\n", encoding="utf-8")

    import os

    for key in ("provider", "model"):
        os.environ.pop(key, None)
    try:
        load_sage_dotenv(str(tmp_path))
        assert os.environ["provider"] == "openai", ".env.local must win on keys it sets"
        assert os.environ["model"] == "shipped", ".env must still fill keys .env.local omits"
    finally:
        for key in ("provider", "model"):
            os.environ.pop(key, None)


def test_real_environment_still_beats_both_files(tmp_path):
    """Mythic's injected configuration outranks every file, `.env.local` included."""
    (tmp_path / ".env").write_text("provider=anthropic\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("provider=openai\n", encoding="utf-8")

    import os

    os.environ["provider"] = "injected-by-mythic"
    try:
        applied = load_sage_dotenv(str(tmp_path))
        assert os.environ["provider"] == "injected-by-mythic"
        assert "provider" not in applied
    finally:
        os.environ.pop("provider", None)


def test_local_override_file_is_gitignored():
    """It holds real credentials, so a rule change that made it committable must fail loudly."""
    import shutil
    import subprocess

    if shutil.which("git") is None or not (SAGE_ROOT.parents[1] / ".git").exists():
        pytest.skip("not a git checkout; nothing to assert about ignore rules")

    result = subprocess.run(
        ["git", "check-ignore", "-v", "Payload_Type/sage/.env.local"],
        cwd=SAGE_ROOT.parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and result.stdout.strip(), (
        "Payload_Type/sage/.env.local is NOT gitignored; it is the documented home for API keys "
        "and machine-specific paths, so it must never be committable"
    )


def test_committed_dotenv_sets_nothing():
    """Cloning Sage must not reconfigure anything. Every line ships commented out."""
    assert DOTENV.is_file(), "the .env template is committed on purpose and must exist in a clone"

    from dotenv import dotenv_values

    values = dotenv_values(DOTENV)
    assert values == DOTENV_ALLOWED_ACTIVE, (
        "the committed .env must define nothing beyond the security-hardening allowlist "
        f"{DOTENV_ALLOWED_ACTIVE}, found: {dict(sorted(values.items()))}"
    )

    environ: dict[str, str] = {}
    assert apply_dotenv(values, environ) == list(DOTENV_ALLOWED_ACTIVE)
    assert environ == DOTENV_ALLOWED_ACTIVE


def test_committed_dotenv_carries_no_credentials():
    """It is committed to a public repository, so a real value here is a published secret."""
    text = DOTENV.read_text(encoding="utf-8")

    allowed_lines = {f"{key}={value}" for key, value in DOTENV_ALLOWED_ACTIVE.items()}
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped in allowed_lines:
            continue
        assert not stripped or stripped.startswith("#"), (
            f".env line {line_number} is uncommented ({stripped!r}); the committed template must "
            "ship inert apart from the security-hardening allowlist "
            f"({sorted(allowed_lines)}) — an operator uncomments what they need after cloning"
        )

    # Shapes that indicate a pasted credential rather than documentation.
    for marker in ("AKIA", "ASIA", "sk-", "BEGIN PRIVATE KEY", "BEGIN RSA"):
        assert marker not in text, f"possible credential material in the committed .env: {marker}"


def test_committed_dotenv_omits_mythic_managed_keys_from_the_active_section():
    """Mythic-managed keys belong in the local-development section, clearly marked.

    They cannot take effect in a container anyway (the environment wins), but documenting them
    beside the settings that DO work would invite an operator to set them and wonder why nothing
    happened.
    """
    text = DOTENV.read_text(encoding="utf-8")
    assert "Local development only" in text

    local_section = text.split("Local development only", 1)[1]
    for key in ("RABBITMQ_HOST", "MYTHIC_SERVER_HOST", "RABBITMQ_PASSWORD", "DEBUG_LEVEL"):
        assert f"#{key}=" in local_section, f"{key} must be documented in the local-dev section"
        assert text.count(f"#{key}=") == 1, f"{key} appears outside the local-dev section too"
