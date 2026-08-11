"""Sage ships no BloodHound address, and says so plainly when it has none.

ISC-24 / D10. The tempting alternative was a default like `http://127.0.0.1:8083` shipped live in the
tracked `.env`. Rejected for three reasons, and this file is what keeps the rejection true:

* The tokens are mandatory anyway, so an operator edits the file regardless. A default saves nothing.
* A present-but-wrong address converts a clear "not configured" into a connection failure, which is
  strictly worse diagnostics — the subject of this entire ISA.
* `127.0.0.1` is only correct under host networking. On a bridge network it points the container at
  itself, and the parser is deliberately neutral about reachability.

So: a COMMENTED example is documentation and welcome; an uncommented value is a shipped guess. The
distinction is the whole test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.bloodhound_config import BLOODHOUND_URL_KEY, credential_diagnostic  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
TRACKED_ENV = REPO_ROOT / "Payload_Type" / "sage" / ".env"

#: Any host:port or scheme://host that would function if uncommented.
_LIVE_ADDRESS = re.compile(r"^\s*BLOODHOUND_URL\s*=\s*\S+", re.MULTILINE)


def test_the_tracked_env_ships_no_live_address():
    """A commented example is documentation; an uncommented one is a guess shipped to every operator."""
    text = TRACKED_ENV.read_text(encoding="utf-8")
    assert BLOODHOUND_URL_KEY in text, "the example line is missing, so operators get no shape to copy"

    live = [m.group(0).strip() for m in _LIVE_ADDRESS.finditer(text)]

    assert not live, f"the tracked .env ships a live BloodHound address: {live}"


def test_the_chat_configuration_field_ships_blank():
    """A committed DefaultValue would push one deployment's layout onto every operator."""
    from sage_chat.models import _CONFIG_OPTIONS

    url_options = [o for o in _CONFIG_OPTIONS if o.Name == BLOODHOUND_URL_KEY]
    assert len(url_options) == 1, "expected exactly one BloodHound URL field"

    default = getattr(url_options[0], "DefaultValue", None)
    assert not default, f"the chat-config field ships a default address: {default!r}"


def test_zero_configuration_fails_closed_naming_the_variable():
    """The behaviour the absent default buys: a refusal that says what to set, not a silent attempt.

    Same shape as the `SAGE_BLOODHOUND_MCP_DIR` resolver, which also refuses and names its variable
    rather than guessing a checkout.
    """
    diagnostic = credential_diagnostic({})

    assert BLOODHOUND_URL_KEY in diagnostic
    assert "NONE" in diagnostic, "must state that nothing resolved, not merely what is missing"
    for retired in ("BLOODHOUND_DOMAIN", "BLOODHOUND_PORT", "BLOODHOUND_SCHEME"):
        assert retired not in diagnostic, f"named {retired}, which an operator can no longer set"


@pytest.mark.parametrize("path", ["ai/bloodhound_config.py", "sage_chat/config.py", "sage_chat/models.py"])
def test_no_module_hardcodes_a_bloodhound_address(path):
    """The code default D10 rejected. Example addresses in help text and comments are fine."""
    source = (REPO_ROOT / "Payload_Type" / "sage" / path).read_text(encoding="utf-8")

    assignments = re.findall(r"BLOODHOUND_URL_KEY\s*[:,]?\s*=\s*[\"'](https?://[^\"']+)[\"']", source)
    assert not assignments, f"{path} assigns a hardcoded BloodHound address: {assignments}"
