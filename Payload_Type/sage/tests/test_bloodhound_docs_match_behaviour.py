"""The two operator-facing documents must describe the behaviour Sage actually has, and agree.

Sage has two audiences for the same facts: `README.md` on GitHub, and `documentation-payload/sage/`,
which Mythic serves inside the UI and is what an operator reads while actually running Sage. A
behaviour change documented in one and not the other is worse than documenting neither, because the
stale page still reads as authoritative.

`AGENTS.md` already warns that `README.md` has been stale before and that safety claims in it must be
verified against source. This is that warning turned into a test for the claims this ISA changed:
degraded-without-BloodHound behaviour, the single `BLOODHOUND_URL`, where credentials can be set, and
what an autonomous channel does when the graph is unavailable.

Deliberately checks CLAIMS, not prose. It asserts that each document states a fact the code makes
true, so rewording is free and dropping a fact is not. What it cannot catch is a document that states
the fact and gets the detail wrong; nothing short of a human reading it can.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.bloodhound_config import BLOODHOUND_URL_KEY  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
MYTHIC_DOC = REPO_ROOT / "documentation-payload" / "sage" / "bloodhound.md"
OPERATOR_DOCS = (README, MYTHIC_DOC)

#: (label, one or more spellings that would satisfy the claim). A claim is met if ANY spelling
#: appears, so the documents can phrase things differently without failing.
REQUIRED_CLAIMS = (
    ("the single URL field", (BLOODHOUND_URL_KEY,)),
    ("ordinary chat survives without BloodHound", ("Ordinary chat is unaffected",)),
    ("autonomous fails closed", ("Autonomous solves fail closed", "fail closed")),
    ("the UI-editable .env is a credential home", ("installed-services page",)),
    ("the baked MCP directory is not durable", ("lost on the\nnext rebuild", "lost on the next rebuild")),
    ("the diagnostic names missing keys", ("Missing (required)",)),
)


@pytest.mark.parametrize("doc", OPERATOR_DOCS, ids=lambda p: p.name)
def test_documents_exist_and_are_substantial(doc: Path):
    """Floor assertion: a guard that reads an empty file reports success forever."""
    assert doc.is_file(), f"{doc} is missing"
    assert len(doc.read_text(encoding="utf-8")) > 2000


@pytest.mark.parametrize("doc", OPERATOR_DOCS, ids=lambda p: p.name)
@pytest.mark.parametrize("label,spellings", REQUIRED_CLAIMS, ids=[c[0] for c in REQUIRED_CLAIMS])
def test_each_operator_document_states_the_claim(doc: Path, label: str, spellings: tuple):
    text = doc.read_text(encoding="utf-8")
    assert any(s in text for s in spellings), (
        f"{doc.relative_to(REPO_ROOT)} does not state: {label}. An operator reading only this page "
        "would not learn it."
    )


def test_the_two_documents_do_not_disagree_on_the_configuration_surface():
    """ISC-26's falsifier: the Mythic page and the README describing different configuration.

    Compared on the retired key names rather than on wording, because that is the disagreement that
    actually misleads — one page telling an operator to set a variable the other retired.
    """
    readme = README.read_text(encoding="utf-8")
    mythic = MYTHIC_DOC.read_text(encoding="utf-8")

    for retired in ("BLOODHOUND_DOMAIN", "BLOODHOUND_PORT", "BLOODHOUND_SCHEME"):
        assert retired not in readme, f"README still asks for the retired {retired}"
        assert retired not in mythic, f"the Mythic documentation page still asks for {retired}"

    assert (BLOODHOUND_URL_KEY in readme) == (BLOODHOUND_URL_KEY in mythic) is True
