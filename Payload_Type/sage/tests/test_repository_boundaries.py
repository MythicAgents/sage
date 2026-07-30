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
