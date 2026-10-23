from __future__ import annotations

import json
from pathlib import Path
import sys

SAGE_ROOT = Path(__file__).resolve().parents[1]
# agent_capabilities.json lives at the repo root, not in the service dir.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(SAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(SAGE_ROOT))

import _version  # noqa: E402


def test_reads_canonical_version_from_agent_capabilities(monkeypatch):
    monkeypatch.delenv("SAGE_AGENT_VERSION", raising=False)  # force the dev-checkout read path
    caps = json.loads((REPO_ROOT / "agent_capabilities.json").read_text(encoding="utf-8"))
    assert _version.agent_version() == str(caps["agent_version"])
    assert _version.mythic_version() == str(caps["mythic_version"])


def test_installer_remote_image_matches_agent_version():
    # mythic-cli install pulls config.json's remote_images entry instead of building, so the pinned
    # image must be the release that agent_version declares; a version bump must move it too.
    config = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    caps = json.loads((REPO_ROOT / "agent_capabilities.json").read_text(encoding="utf-8"))
    assert config["remote_images"] == {"sage": f"ghcr.io/mythicagents/sage:{caps['agent_version']}"}


def test_baked_agent_version_env_wins():
    # A built image stamps SAGE_AGENT_VERSION; it must win over any file read.
    import os

    os.environ["SAGE_AGENT_VERSION"] = "9.9-test"
    try:
        assert _version.agent_version() == "9.9-test"
    finally:
        del os.environ["SAGE_AGENT_VERSION"]


def test_chat_container_semver_is_the_sage_release_version(monkeypatch):
    # Mythic shows a container's semver as that container's own version, so SageChat must register
    # Sage's release version from agent_capabilities.json, never the Mythic framework version.
    monkeypatch.delenv("SAGE_AGENT_VERSION", raising=False)
    from sage_chat.service import SageChat

    assert SageChat.semver == _version.agent_version()
    assert SageChat.semver != _version.mythic_version().lstrip("v")


def test_build_commit_reads_env(monkeypatch):
    monkeypatch.delenv("SAGE_GIT_COMMIT", raising=False)
    assert _version.build_commit() == "unknown"
    monkeypatch.setenv("SAGE_GIT_COMMIT", "a" * 40)
    assert _version.build_commit() == "a" * 40


def test_source_fingerprint_is_deterministic_hex():
    fp1 = _version.source_fingerprint()
    fp2 = _version.source_fingerprint()
    assert fp1 == fp2 and fp1 != "unknown"
    assert len(fp1) == 12
    int(fp1, 16)  # must be hex


def test_source_fingerprint_changes_with_content(tmp_path):
    root = tmp_path / "svc"
    (root / "pkg").mkdir(parents=True)
    (root / "main.py").write_text("print('a')\n")
    (root / "pkg" / "m.py").write_text("x = 1\n")
    before = _version.source_fingerprint(root)
    (root / "pkg" / "m.py").write_text("x = 2\n")
    assert _version.source_fingerprint(root) != before


def test_source_fingerprint_ignores_volatile_and_non_python(tmp_path):
    root = tmp_path / "svc"
    root.mkdir()
    (root / "main.py").write_text("print('a')\n")
    before = _version.source_fingerprint(root)
    # A runtime DB dir, bytecode cache, and a non-.py file must not move the fingerprint.
    (root / ".phoenix").mkdir()
    (root / ".phoenix" / "phoenix.db").write_text("binary-ish\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "main.cpython-314.pyc").write_text("cache\n")
    (root / "notes.txt").write_text("not python\n")
    assert _version.source_fingerprint(root) == before


def test_startup_banner_leads_with_commit_and_fingerprint():
    banner = _version.startup_banner()
    for field in ("build_commit=", "source_fp=", "version="):
        assert field in banner
    # The discipline-free facts come first; the hand-maintained version follows.
    assert banner.index("build_commit=") < banner.index("version=")
    assert banner.index("source_fp=") < banner.index("version=")


def test_startup_announcement_prints_the_banner_to_stdout(capsys):
    # Mythic's default DEBUG_LEVEL ("warning") hides INFO logging, so the banner is printed instead.
    banner = _version.announce_startup()
    assert capsys.readouterr().out == banner + "\n"
    assert banner.startswith("Sage build_commit=")
