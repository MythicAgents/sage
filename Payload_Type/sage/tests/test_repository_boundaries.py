from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
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
        if "/home/john" in path.read_text(encoding="utf-8"):
            offenders.append(relative_path)

    assert not offenders, "personal absolute paths remain in: " + ", ".join(offenders)


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
