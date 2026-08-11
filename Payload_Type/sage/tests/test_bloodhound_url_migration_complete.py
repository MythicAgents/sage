"""Nobody is left reading a retired key, and the operator surface really is one address.

`BLOODHOUND_DOMAIN`, `BLOODHOUND_PORT` and `BLOODHOUND_SCHEME` expressed one fact — where BloodHound
is — across three fields, and `DOMAIN` was a HOSTNAME, which is a confusing thing to call it inside a
tool whose whole subject is Active Directory domains. They are now one `BLOODHOUND_URL`, expanded
back into those three at the subprocess boundary because the MCP server still reads them.

That split is the whole hazard: the three names must survive in exactly one place (what Sage hands
the subprocess) and vanish everywhere an operator or a document can see them. A migration that moves
the code and forgets a doc leaves a reader configuring a key nothing consumes, with no error.

This guard therefore checks the CLASS, not the instances I happened to fix — the repo's own rule
after a sweep that "completed" while five occurrences survived in a different spelling. It walks the
real operator-facing files and fails on any live mention, with a floor assertion so it cannot pass by
inspecting nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.bloodhound_config import (  # noqa: E402
    BLOODHOUND_CREDENTIAL_KEYS,
    BLOODHOUND_OPERATOR_CONFIG_KEYS,
    BLOODHOUND_URL_KEY,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SAGE_ROOT = REPO_ROOT / "Payload_Type" / "sage"

RETIRED_KEYS = ("BLOODHOUND_DOMAIN", "BLOODHOUND_PORT", "BLOODHOUND_SCHEME")

#: Everything an operator reads or edits. Not the whole repo: the three names legitimately survive in
#: the resolver, the allowlist and the tests, which is where the expansion lives.
OPERATOR_FACING = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "configuration" / "BLOODHOUND.md",
    REPO_ROOT / "documentation-payload" / "sage" / "bloodhound.md",
    SAGE_ROOT / "ttps" / "bloodhound-mcp.md",
    SAGE_ROOT / ".env",
    SAGE_ROOT / "Dockerfile",
)


def test_the_files_under_guard_exist_and_are_read():
    """Floor assertion: a guard that inspects nothing reports success forever."""
    missing = [p for p in OPERATOR_FACING if not p.is_file()]
    assert not missing, f"guard is pointed at files that do not exist: {missing}"
    total = sum(len(p.read_text(encoding="utf-8")) for p in OPERATOR_FACING)
    assert total > 5000, "read implausibly little of the operator-facing surface"


@pytest.mark.parametrize("path", OPERATOR_FACING, ids=lambda p: p.name)
def test_no_operator_facing_file_still_asks_for_a_retired_key(path: Path):
    text = path.read_text(encoding="utf-8")
    found = [key for key in RETIRED_KEYS if key in text]
    assert not found, (
        f"{path.relative_to(REPO_ROOT)} still names {found}, which an operator can no longer set. "
        f"Use {BLOODHOUND_URL_KEY}."
    )


@pytest.mark.parametrize("path", OPERATOR_FACING, ids=lambda p: p.name)
def test_the_replacement_is_actually_offered(path: Path):
    """The other half: removing the old keys without naming the new one is not a migration.

    Only checked for files that discuss BloodHound credentials at all — the Dockerfile and README
    both do, so an empty exemption list here is deliberate rather than an oversight waiting to grow.
    """
    text = path.read_text(encoding="utf-8")
    if "BLOODHOUND_TOKEN_ID" not in text:
        pytest.skip(f"{path.name} does not discuss BloodHound credentials")
    assert BLOODHOUND_URL_KEY in text


def test_the_two_key_lists_are_deliberately_different():
    """The expansion is the point: what a human sets is not what the subprocess receives."""
    assert BLOODHOUND_URL_KEY in BLOODHOUND_OPERATOR_CONFIG_KEYS
    assert BLOODHOUND_URL_KEY not in BLOODHOUND_CREDENTIAL_KEYS, (
        "BLOODHOUND_URL must never enter the MCP subprocess environment; the server does not read "
        "it, and the allowlist is what keeps un-consumed keys out"
    )
    for key in RETIRED_KEYS:
        assert key in BLOODHOUND_CREDENTIAL_KEYS, "the server still reads the expanded triple"
        assert key not in BLOODHOUND_OPERATOR_CONFIG_KEYS, "no operator sets these any more"


def test_the_chat_config_offers_one_address_field():
    from sage_chat.models import SAGE_MODELS, _CONFIG_OPTIONS

    names = {opt.Name for opt in _CONFIG_OPTIONS}
    secrets = set(SAGE_MODELS[0].Metadata.OptionalUserSecrets)

    assert BLOODHOUND_URL_KEY in names and BLOODHOUND_URL_KEY in secrets
    for key in RETIRED_KEYS:
        assert key not in names, f"{key} is still a chat-configuration field"
        assert key not in secrets, f"{key} is still declared as a user secret"
