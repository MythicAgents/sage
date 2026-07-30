"""Repository hygiene — portability, privacy, and build-context guarantees.

These are contributor-facing, not maintainer-only: they fail on *anyone's* hardcoded home directory,
dead documentation link, or leaked private tooling, which is why they belong in the repository rather
than in someone's local setup. They were split out of `Payload_Type/sage/tests/` because nothing here
tests Sage's runtime behaviour — that suite keeps the two genuine product-boundary tests.

Contract they enforce: AGENTS.md § Publishable-By-Default Contract.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import re
import subprocess
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[2]
SAGE_ROOT = REPO_ROOT / "Payload_Type" / "sage"
RUNNER_PATH = (
    REPO_ROOT
    / "skills"
    / "sage-focused-capability-tests"
    / "scripts"
    / "run_offline_suite.py"
)


def _product_python_files() -> list[Path]:
    files = [SAGE_ROOT / "main.py", SAGE_ROOT / "ai" / "mcp.py", SAGE_ROOT / "ai" / "bloodhound_config.py"]
    for directory in (SAGE_ROOT / "sage_chat", SAGE_ROOT / "ai" / "langgraph"):
        files.extend(directory.rglob("*.py"))
    return sorted(path for path in files if path.is_file())


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return imported


def _load_runner():
    spec = importlib.util.spec_from_file_location("sage_offline_suite", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docker_context_excludes_development_state_and_archived_databases():
    ignored = {
        line.strip()
        for line in (SAGE_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        ".git",
        ".env",
        ".phoenix",
        ".sage_engagement",
        ".trajectory",
        ".hillclimb",
        "sage*.db*",
        "ai/hillclimb",
        "evals",
        "tests",
        "Payload_Type",
    }
    assert required <= ignored


def test_offline_suite_runs_the_whole_tree_with_no_hidden_exclusions():
    """The runner must not silently drop suites.

    Replaces an earlier test that asserted the runner excluded exactly four rejected
    successor-portfolio suites. Those portfolios moved to
    `.sage_history/evaluation/architecture-policy/rejected-successor-portfolios/` — rejected
    evaluation evidence belongs in the retention store, not the product tree — so the exclusion
    mechanism is gone rather than merely unused. This asserts it stays gone: an exclusion that
    creeps back in makes a green run stop meaning the tree is green.
    """
    runner = _load_runner()
    assert not hasattr(runner, "RETIRED_SUITES")
    command = runner.command_for(["--collect-only", "-q"])
    assert not any(item.startswith("--ignore=") for item in command)
    assert command[-2:] == ["--collect-only", "-q"]


def test_rejected_successor_portfolios_are_absent_from_the_product_tree():
    """Regression: 28k lines of rejected candidates must not return to Payload_Type/."""
    strays = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "Payload_Type" / "sage").rglob("*successor*portfolio*.py")
    ]
    assert strays == []


def test_enabled_codex_agent_profile_references_resolve_atomically():
    config_path = REPO_ROOT / ".codex" / "config.toml"
    if not config_path.exists():
        return
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    for name, definition in (config.get("agents") or {}).items():
        if not isinstance(definition, dict) or not definition.get("config_file"):
            continue
        referenced = (config_path.parent / str(definition["config_file"])).resolve()
        assert referenced.is_file(), f"enabled Codex profile {name!r} is missing {referenced}"

    agents_guide = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    if "skills/sage-cyber-runner/" in agents_guide:
        assert (REPO_ROOT / "skills" / "sage-cyber-runner" / "SKILL.md").is_file()


def test_repo_carries_no_maintainer_private_codex_tooling():
    """The public repo must not ship the maintainer's personal Codex agents.

    `sage_cyber_executor` pins `gpt-5.5-cyber-preview` and `sage-cyber-runner` exists to drive it; both encode
    how one maintainer works rather than how Sage is built, so they live in `~/.codex/` instead. Codex has no
    gitignored project-local config layer (openai/codex#24961), so nothing but this test stops them drifting
    back in the next time someone edits `.codex/config.toml`.
    """
    assert not (REPO_ROOT / "skills" / "sage-cyber-runner").exists()
    assert not (REPO_ROOT / ".codex" / "agents").exists()

    config_path = REPO_ROOT / ".codex" / "config.toml"
    if config_path.exists():
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        named_agents = {
            name
            for name, definition in (config.get("agents") or {}).items()
            if isinstance(definition, dict)
        }
        assert named_agents == set(), f"private agent profiles leaked back in: {sorted(named_agents)}"


def test_public_guide_does_not_point_into_the_private_plans_directory():
    """`Plans/` is gitignored, so a pointer into it is a dead link for everyone but the maintainer.

    Rules *about* `Plans/` ("don't put tools there", "evidence belongs in .sage_history") are fine and expected.
    What this rejects is `AGENTS.md` telling a reader to go *read* a file they cannot have.
    """
    for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in text.splitlines()
            if "Read `Plans/" in line or "Plans/RESUME.md" in line or "Plans/Archived/SAGE" in line
        ]
        assert offenders == [], f"{name} points a reader into gitignored Plans/: {offenders}"


def test_maintained_operator_helpers_have_no_personal_absolute_paths():
    maintained_helpers = (
        "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py",
        "skills/sage-live-runner/scripts/native_chat.py",
        "skills/sage-goad-reset/scripts/mythic_reset.sh",
        "skills/sage-goad-reset/scripts/ludus.py",
        "skills/sage-goad-reset/scripts/mcp_check.py",
        "skills/sage-goad-reset/scripts/bh_reset.py",
        "skills/sage-goad-reset/scripts/check_cross_forest_laps_bridge.py",
        "skills/sage-goad-reset/scripts/liveness.py",
        "skills/sage-goad-reset/scripts/sync_range_time.py",
    )

    offenders = []
    for relative_path in maintained_helpers:
        path = REPO_ROOT / relative_path
        assert path.is_file(), f"maintained helper is missing: {relative_path}"
        if _LAYOUT_PATH_RE.search(path.read_text(encoding="utf-8")):
            offenders.append(relative_path)

    assert not offenders, "personal absolute paths remain in: " + ", ".join(offenders)


# A path that encodes ONE developer's SOURCE-CHECKOUT location. `~/dev/...` and `$HOME/dev/...` are
# the same defect as `/home/<user>/dev/...` wearing a portable-looking hat — a sweep for
# `/home/john` alone passes them, which is exactly how five survived a cleanup pass.
#
# Deliberately NOT "any /home/ path": the repo legitimately contains target-side paths that only
# look similar. `ttps/` documents victim-host tradecraft (`/home/user/.ssh/id_rsa`), and several
# tests feed a synthetic `/home/alice/...` in to prove the redactor strips it. Anchoring on a
# checkout directory rather than on `/home` separates those from the real defect without needing a
# per-file exemption for each one.
#
# Known limit: only the `dev/` convention is matched. A checkout under `~/code/` or `~/src/` would
# slip through. Widen this the day that happens rather than guessing at it now.
_LAYOUT_PATH_RE = re.compile(
    r"/home/[a-z][a-z0-9_-]*/dev/|~/dev/|\$HOME/dev\b|/Users/[a-z][a-z0-9_-]*/(?:dev|Documents)/"
)

# One-off forensics scripts named after specific 2026 task IDs and runs (poll_1960, watch_2215,
# diag_ingest_run4, …). They still hardcode the maintainer's layout. They are excluded here rather
# than silently skipped so the debt stays countable: the assertion below fails if this list grows,
# and the list should only ever shrink. Fixing or archiving them is tracked separately.
_LAYOUT_PATH_DEBT = 13

# Legitimate occurrences. Not debt, and must never be "fixed":
#   test_hillclimb_operator_replay_benchmark.py — feeds a personal path in to prove redaction strips it
#   test_repository_boundaries.py               — this file; the pattern is the thing being searched for
_LAYOUT_PATH_EXEMPT = {
    "Payload_Type/sage/tests/test_hillclimb_operator_replay_benchmark.py",
    # This file. It must name the forbidden shapes in order to search for them, and it moved here
    # from Payload_Type/sage/tests/test_repository_boundaries.py when the hygiene suite was split
    # out — the stale entry made the guard flag itself, which is how the move got caught.
    "tests/repo_hygiene/test_repo_hygiene.py",
}


def test_no_layout_encoding_paths_outside_the_known_one_off_scripts():
    """Docs, product code, and tests must not encode one machine's directory layout.

    Scope is everything tracked except `skills/*/scripts/` one-offs, which carry a counted debt.
    A green run means a fresh clone reads documentation and product code that work anywhere.
    """
    # The tracked set is exactly what a contributor sees in a clone, so `git ls-files` is the right
    # source of truth — a filesystem walk instead pulls in local runtime state (archived engagement
    # ledgers, `.claude/settings.local.json`) that no clone ever contains, and needs an
    # ever-growing skip-list to suppress.
    #
    # The floor assertion is the point: `git ls-files` returns nothing in a tarball export or a
    # freshly-initialised repo, and without it this assertion passed while inspecting ZERO files.
    # A guard that can pass vacuously is worse than no guard.
    listing = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    tracked = listing.stdout.split() if listing.returncode == 0 else []
    assert len(tracked) >= 300, (
        f"git ls-files returned {len(tracked)} paths (rc={listing.returncode}); this test needs a "
        "real git checkout, and a green result without one would prove nothing"
    )

    offenders, one_off_debt = [], []
    for relative_path in tracked:
        if relative_path in _LAYOUT_PATH_EXEMPT:
            continue
        path = REPO_ROOT / relative_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not _LAYOUT_PATH_RE.search(text):
            continue
        bucket = one_off_debt if "/scripts/" in relative_path else offenders
        bucket.append(relative_path)
    assert not offenders, (
        "layout-encoding paths outside the one-off scripts: " + ", ".join(sorted(offenders))
    )
    assert len(one_off_debt) <= _LAYOUT_PATH_DEBT, (
        f"one-off-script debt grew to {len(one_off_debt)} (cap {_LAYOUT_PATH_DEBT}); "
        "new scripts must not hardcode a home directory: " + ", ".join(sorted(one_off_debt))
    )
