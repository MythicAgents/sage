from __future__ import annotations

import ast
import fnmatch
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[3]
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


def _publishable_product_files() -> list[Path]:
    files = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "docs" / "SECURITY_AND_DATA_HANDLING.md",
        REPO_ROOT / "docs" / "releases" / "v0.1.0-beta.md",
        SAGE_ROOT / "Dockerfile",
        SAGE_ROOT / ".env",
        *_product_python_files(),
    ]
    return sorted(path for path in files if path.is_file())


_MACHINE_PATH_PATTERNS = {
    "posix_user_home": re.compile(r"(?<![A-Za-z0-9_])/(?:home|Users)/[A-Za-z0-9._-]+/"),
    "windows_user_home": re.compile(
        r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/])Users[\\/]"
        r"(?!(?:Public|Default|Default User|All Users)(?:[\\/]|$))[^\\/\s:]+[\\/]"
    ),
    "tilde_home": re.compile(r"(?<![A-Za-z0-9_])~[\\/]"),
    "home_variable": re.compile(r"\$(?:HOME\b|\{HOME\})"),
    "windows_userprofile_variable": re.compile(r"(?i)%USERPROFILE%(?:[\\/]|$)"),
    "powershell_userprofile_variable": re.compile(r"(?i)\$env:USERPROFILE(?:[\\/]|$)"),
}


def _machine_path_hits(text: str) -> list[str]:
    return [name for name, pattern in _MACHINE_PATH_PATTERNS.items() if pattern.search(text)]


def _machine_path_violations(paths: list[Path]) -> list[str]:
    violations = []
    for path in paths:
        for kind in _machine_path_hits(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path.name}: {kind}")
    return violations


def _imports(path: Path) -> list[tuple[str, int, int, tuple[str, ...]]]:
    """Return ``(module, line, level, names)`` for every import.

    ``names`` holds a ``from`` import's imported names (empty for ``import x``); ``from . import x`` has module ``""``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[tuple[str, int, int, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend((alias.name, node.lineno, 0, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append((node.module or "", node.lineno, node.level, tuple(a.name for a in node.names)))
    return imported


def _canonical_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _installed_module_owners() -> dict[str, set[str]]:
    """Map each installed module's dotted name to the distributions whose RECORD lists its file.

    Owning files, not top-level names: ``opentelemetry``, ``phoenix``, ``openinference`` and ``langgraph`` are each
    shared by several distributions, so declaring one of them says nothing about the others.
    """
    owners: dict[str, set[str]] = {}
    for distribution in importlib.metadata.distributions():
        name = _canonical_distribution(distribution.metadata["Name"])
        for file in distribution.files or ():
            path = file.as_posix()
            if path.startswith(".."):
                continue
            if path.endswith("/__init__.py"):
                module = path[: -len("/__init__.py")]
            elif path.endswith(".py"):
                module = path[:-3]
            elif path.endswith((".so", ".pyd")):
                head, _, last = path.rpartition("/")
                module = f"{head}/{last.split('.', 1)[0]}" if head else last.split(".", 1)[0]
            else:
                continue
            owners.setdefault(module.replace("/", "."), set()).add(name)
    return owners


def _shipped_runtime_python_files() -> list[Path]:
    """Every .py the Sage image copies: the service root minus .dockerignore and self-built sub-images."""
    patterns = []
    for raw in (SAGE_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith(("#", "!")):
            patterns.append(line.rstrip("/"))

    def ignored(relative: Path) -> bool:
        parts = relative.parts
        for pattern in patterns:
            if pattern.startswith("**/"):
                candidates = ["/".join(parts[i:j]) for i in range(len(parts)) for j in range(i + 1, len(parts) + 1)]
                pattern = pattern[3:]
            else:
                candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
            if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
                return True
        return False

    own_images = [
        directory for directory in (SAGE_ROOT / "container").iterdir()
        if (directory / "Dockerfile").is_file() and any(directory.glob("requirements*.txt"))
    ]
    files = []
    for directory, subdirectories, names in os.walk(SAGE_ROOT):
        base = Path(directory)
        subdirectories[:] = [
            name for name in subdirectories
            if not ignored((base / name).relative_to(SAGE_ROOT)) and base / name not in own_images
        ]
        files.extend(
            base / name for name in names
            if name.endswith(".py") and not ignored((base / name).relative_to(SAGE_ROOT))
        )
    return sorted(files)


def _resolves_to_local_module(name: str, importer: Path) -> bool:
    directories = [importer.parent, *(p for p in importer.parent.parents if p.is_relative_to(SAGE_ROOT))]
    directories.append(SAGE_ROOT / "ai")
    return any((d / f"{name}.py").is_file() or (d / name).is_dir() for d in directories)


def _load_runner():
    spec = importlib.util.spec_from_file_location("sage_offline_suite", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_product_runtime_does_not_import_development_or_range_packages():
    forbidden = ("ai.hillclimb", "evals", "skills", "ludus", "Plans")
    violations: list[str] = []
    for path in _product_python_files():
        for module, _line, _level, _names in _imports(path):
            if module == forbidden or any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert violations == []


def test_shipped_runtime_imports_are_declared():
    """A shipped import must name the stdlib, Sage's own code, or a distribution in requirements.txt.

    A package that arrives only transitively (or only as a constraints.txt pin) can vanish on any upstream bump and
    break the image at import time. Guarded imports count: the image must not rely on a fallback. Each import
    resolves to the distribution owning its most specific module file (``from phoenix.otel import x`` belongs to
    arize-phoenix-otel, not arize-phoenix). A module an installed distribution provides is third-party even when a
    same-named local module exists (``ai/mcp.py``, ``ai/langgraph/``), because ``ai/`` is never on the runtime
    ``sys.path``.
    """
    files = _shipped_runtime_python_files()
    relative = {path.relative_to(SAGE_ROOT).as_posix() for path in files}
    assert {"main.py", "sage_chat/cancel_adapter.py"} <= relative
    assert not any(name.startswith(("tests/", "ai/hillclimb/", "container/inspection/")) for name in relative)

    declared = set()
    for raw in (SAGE_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            declared.add(_canonical_distribution(re.split(r"[\[<>=!~;\s]", line, maxsplit=1)[0]))
    owners = _installed_module_owners()

    undeclared: dict[str, set[str]] = {}
    for path in files:
        for module, line, level, names in _imports(path):
            top = module.split(".", 1)[0]
            if level or top in sys.stdlib_module_names:
                continue
            for imported in [f"{module}.{name}" for name in names] or [module]:
                parts = imported.split(".")
                prefixes = (".".join(parts[:end]) for end in range(len(parts), 0, -1))
                owner = next((owners[prefix] for prefix in prefixes if prefix in owners), set())
                if not owner and _resolves_to_local_module(top, path):
                    continue
                if not owner & declared:
                    key = ", ".join(sorted(owner)) or f"{top} (no installed distribution)"
                    undeclared.setdefault(key, set()).add(f"{path.relative_to(SAGE_ROOT).as_posix()}:{line}")
    assert {key: sorted(sites) for key, sites in undeclared.items()} == {}


def test_reset_and_bootstrap_helpers_do_not_use_callback_task_apis():
    helper_dirs = (
        REPO_ROOT / "skills" / "sage-callback-bootstrap" / "scripts",
        REPO_ROOT / "skills" / "sage-goad-reset" / "scripts",
    )
    forbidden = ("issue_task", "waitfor_for_task_output", "get_all_task_output_by_id")

    offenders = []
    for directory in helper_dirs:
        for path in sorted(directory.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in source:
                    offenders.append(f"{path.relative_to(REPO_ROOT)} uses {token}")

    assert not offenders, "callback task APIs remain in reset/bootstrap helpers: " + ", ".join(offenders)


def test_publishable_product_files_have_no_machine_specific_home_paths():
    assert _machine_path_violations(_publishable_product_files()) == []


def test_machine_path_guard_rejects_complete_platform_class_and_accepts_near_matches(tmp_path):
    defect_class = (
        ("posix", "/" + "home/alice/dev/sage"),
        ("macos", "/" + "Users/alice/dev/sage"),
        ("windows_backslash", "C:" + r"\Users\alice\dev\sage"),
        ("windows_forward_slash", "D:" + "/" + "Users/alice/dev/sage"),
        ("tilde_posix", "~" + "/.config/sage"),
        ("tilde_windows", "~" + r"\.config\sage"),
        ("home_bare", "$" + "HOME/dev/sage"),
        ("home_braced", "$" + "{HOME}/dev/sage"),
        ("userprofile", "%" + r"USERPROFILE%\dev\sage"),
        ("powershell_userprofile", "$" + r"env:USERPROFILE\dev\sage"),
    )
    negatives = (
        "/Mythic/sage.db",
        "Path(__file__).resolve().parent",
        "<mythic>/InstalledServices/sage",
        "C:" + r"\Users\Public\payload.exe",
        "C:" + "/ProgramData/Sage",
        "%" + r"PUBLIC%\Sage",
    )
    assert all(_machine_path_hits(value) for _, value in defect_class)
    assert all(_machine_path_hits(value) == [] for value in negatives)

    planted = tmp_path / "README.md"
    for name, value in defect_class:
        planted.write_text(f"candidate path: {value}\n", encoding="utf-8")
        assert _machine_path_violations([planted]), f"guard missed planted {name} defect"
