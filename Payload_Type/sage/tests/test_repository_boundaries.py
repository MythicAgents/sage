from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import re
import subprocess
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


def test_product_runtime_does_not_import_development_or_range_packages():
    forbidden = ("ai.hillclimb", "evals", "skills", "ludus", "Plans")
    violations: list[str] = []
    for path in _product_python_files():
        for module in _imports(path):
            if module == forbidden or any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert violations == []


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
