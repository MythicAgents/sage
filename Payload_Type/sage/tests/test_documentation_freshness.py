from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_public_readme_describes_current_native_chat_lifecycle():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "Mythic v4",
        "native chat",
        "archive_runtime_dbs.py",
        "sage_restart.sh",
        "native_chat.py",
        "SAGE_BLOODHOUND_MCP_DIR",
    )
    assert all(value in readme for value in required)
    assert "Mythic v3" not in readme
    assert not re.search(r"(?mi)^\s*rm\s+.*sage(?:_.*)?\.db", readme)


def test_public_readme_does_not_advertise_removed_payload_commands():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert not re.search(r"(?mi)^###\s+(chat|query|mcp-connect|mcp-call)\s*$", readme)
    assert "create-sage" not in readme
    assert "Sage payload type" not in readme


def test_repository_boundary_document_states_one_way_dependency():
    boundary = (REPO_ROOT / "docs" / "architecture" / "REPOSITORY_BOUNDARIES.md").read_text(
        encoding="utf-8"
    )
    assert "The arrow does not reverse" in boundary
    assert "`Payload_Type/sage/ai/hillclimb/`" in boundary
    assert "No" in boundary


def test_runtime_packaging_comments_match_native_chat_and_pinned_sdk():
    chat_init = (REPO_ROOT / "Payload_Type" / "sage" / "sage_chat" / "__init__.py").read_text(
        encoding="utf-8"
    )
    dockerfile = (REPO_ROOT / "Payload_Type" / "sage" / "Dockerfile").read_text(encoding="utf-8")
    requirements = (REPO_ROOT / "Payload_Type" / "sage" / "requirements.txt").read_text(encoding="utf-8")
    assert "alongside ``container``" not in chat_init
    assert "first query" not in dockerfile

    # The invariant is that the prose in requirements.txt names the version actually pinned there —
    # not that the pin is any particular version. Derived rather than hardcoded because the previous
    # form asserted a literal `mythic==0.3.0rc5`, so every SDK bump failed this test on the pin
    # itself rather than on the thing it exists to catch. The 0.3.0rc6/rc9 bump landed with the tree
    # red and both comments still saying rc5, which is exactly the stale prose this guards against.
    pinned = re.search(r"^mythic==(\S+)", requirements, re.MULTILINE)
    assert pinned, "requirements.txt must pin the mythic SDK explicitly"
    pinned_version = pinned.group(1)

    commented = set(re.findall(r"\bmythic (\d[\w.]*)", requirements))
    assert commented, "the SDK comments must name a version, so drift is detectable"
    stale = commented - {pinned_version}
    assert not stale, (
        f"requirements.txt comments name mythic {sorted(stale)} but the pin is {pinned_version} — "
        "update the prose alongside the pin"
    )


def test_reset_docs_distinguish_logical_baseline_from_snapshot_identity():
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    reset_skill = (
        REPO_ROOT / "skills" / "sage-goad-reset" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "rollback clean-baseline --yes" not in agents
    assert "sage-seed-baseline-20260710" in agents
    assert "rollback <snapshot-name> --yes" in reset_skill
    assert re.search(
        r"not guaranteed to be the literal Ludus snapshot\s+name",
        reset_skill,
    )
