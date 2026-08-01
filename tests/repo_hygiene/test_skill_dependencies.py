"""Every third-party module the operator skills import must be a declared dependency.

Split from `test_repo_hygiene.py` because this is a dependency-manifest guarantee rather than a
portability or privacy one. It exists because `pywinrm` went undeclared: `sync_range_time.py` needs
it for the clock sync AGENTS.md calls mandatory after every range rollback, and a clean checkout hit
`ModuleNotFoundError: No module named 'winrm'` on that documented-required command. Installing the
package locally fixes one machine; declaring it fixes everyone's, and only this test keeps the two
in step.

Skills are development/operator tooling, so their dependencies belong in `requirements-dev.txt`
(the Dockerfile installs only `requirements.txt`, so nothing here reaches the shipped image).
Declaring one in the runtime manifest instead would still satisfy this test — that is deliberate,
since the runtime manifest is a strict subset of what the dev environment installs.
"""
from __future__ import annotations

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "skills"
SAGE_ROOT = REPO_ROOT / "Payload_Type" / "sage"
REQUIREMENTS = (SAGE_ROOT / "requirements.txt", SAGE_ROOT / "requirements-dev.txt")

# Floors. A guard that inspects nothing and reports success is worse than no guard, because it
# converts silence into confidence. These are deliberately well below current counts (60 files,
# 9 third-party imports) so ordinary growth never trips them, but zero always does.
MIN_FILES_SCANNED = 30
MIN_THIRD_PARTY_IMPORTS = 4

# Imports satisfied by an external checkout on `sys.path`, not by any PyPI distribution.
# `lib` is the BloodHound MCP server's own package: `bh_reset.py` inserts SAGE_BLOODHOUND_MCP_DIR
# onto sys.path and imports `lib.bloodhound_api` from it. There is no wheel to declare.
EXTERNAL_CHECKOUT_MODULES = frozenset({"lib"})


def _normalize(name: str) -> str:
    """PEP 503 name normalization, so `pywinrm`, `PyWinRM` and `py_winrm` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _skill_python_files() -> list[Path]:
    return sorted(
        path
        for path in SKILLS_ROOT.rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts
    )


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import is local by construction; `node.module` is None for `from . import x`.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _repo_local_module_names() -> set[str]:
    """Top-level names resolvable from inside the repo, which skills reach via sys.path edits.

    Skill scripts import `mythic_tools`, `capabilities`, `engagement_ledger` and friends directly
    after putting `Payload_Type/sage/ai/langgraph` on the path, so those are first-party despite
    looking like bare third-party imports.
    """
    local: set[str] = set()
    for root in (REPO_ROOT, SAGE_ROOT):
        for path in root.iterdir():
            if path.is_dir() and (path / "__init__.py").exists():
                local.add(path.name)
            elif path.suffix == ".py":
                local.add(path.stem)
    for path in SAGE_ROOT.rglob("*.py"):
        if ".venv" not in path.parts:
            local.add(path.stem)
            local.add(path.parent.name)
    for path in SKILLS_ROOT.rglob("*.py"):
        if ".venv" not in path.parts and "__pycache__" not in path.parts:
            local.add(path.stem)
    return local


def _declared_distributions() -> set[str]:
    declared: set[str] = set()
    for manifest in REQUIREMENTS:
        for raw in manifest.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
            if name:
                declared.add(_normalize(name))
    return declared


def _third_party_imports() -> dict[str, set[Path]]:
    """Map each third-party top-level import name to the skill files that use it."""
    stdlib = set(sys.stdlib_module_names)
    local = _repo_local_module_names()
    found: dict[str, set[Path]] = {}
    for path in _skill_python_files():
        for name in _top_level_imports(path):
            if name in stdlib or name in local or name in EXTERNAL_CHECKOUT_MODULES:
                continue
            if name.startswith("_"):
                continue
            found.setdefault(name, set()).add(path)
    return found


def _undeclared(declared: set[str]) -> dict[str, set[Path]]:
    """Third-party imports whose providing distribution is not in `declared`.

    Takes `declared` as a parameter rather than reading the manifests directly so the guard can be
    handed a pruned set and proven to go red — see the control test below.
    """
    provider_map = packages_distributions()
    violations: dict[str, set[Path]] = {}
    for name, users in _third_party_imports().items():
        providers = {_normalize(dist) for dist in provider_map.get(name, ())}
        # An import with no known provider is not installed at all, which is also a failure.
        if not providers or providers.isdisjoint(declared):
            violations[name] = users
    return violations


def test_every_skill_third_party_import_is_declared():
    files = _skill_python_files()
    assert len(files) >= MIN_FILES_SCANNED, (
        f"only {len(files)} skill python files found under {SKILLS_ROOT}; "
        "the guard is not inspecting the tree it thinks it is"
    )

    imports = _third_party_imports()
    assert len(imports) >= MIN_THIRD_PARTY_IMPORTS, (
        f"only {len(imports)} third-party imports detected across {len(files)} files; "
        "import extraction is probably broken"
    )

    declared = _declared_distributions()
    assert declared, "no distributions parsed from the requirements manifests"

    violations = _undeclared(declared)
    assert not violations, "skill imports not declared in any requirements manifest: " + "; ".join(
        f"{name} (used by {', '.join(sorted(str(p.relative_to(REPO_ROOT)) for p in paths))})"
        for name, paths in sorted(violations.items())
    )


def test_dependency_guard_detects_an_undeclared_import():
    """The guard must be able to fail; a green run only means something with a red run available.

    Rather than asserting on a hand-planted violation once and trusting it forever, prune a package
    that IS currently imported by a skill out of the declared set and require the guard to catch it.
    """
    declared = _declared_distributions()
    winrm_providers = {_normalize(d) for d in packages_distributions().get("winrm", ())}
    assert winrm_providers & declared, (
        "expected the distribution providing `winrm` to be declared; this control test needs a "
        "real declared-and-imported package to prune"
    )

    pruned = declared - winrm_providers
    violations = _undeclared(pruned)
    assert "winrm" in violations, "guard failed to flag an undeclared import that skills do use"
