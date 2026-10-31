"""Test modules must not replace importable modules for the whole process when they are imported.

Collection imports every test module, even under `-m serial`, so a module-level `sys.modules["name"] = ...`
reaches every later test in the process. A `mythic_container` mock left at module level in one file broke 31 tests
in other files that imported the real package, but only when both files shared a process. A test that needs a
substitute module patches it inside the test (`monkeypatch.setitem(sys.modules, ...)`), so it is restored.
Registering a script the test itself loaded, under that script's own spec name, adds a new name and is allowed.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
TEST_FILES = sorted({
    *REPO.glob("Payload_Type/sage/tests/**/*.py"),
    *REPO.glob("skills/**/tests/**/*.py"),
    *REPO.glob("tests/**/*.py"),
})


def module_level_replacements(source: str) -> list[str]:
    """Module-level statements that bind or remove `sys.modules[<string literal>]`."""
    found = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            targets = []
            if isinstance(sub, ast.Assign):
                targets = sub.targets
            elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
                targets = [sub.target]
            elif isinstance(sub, ast.Delete):
                targets = sub.targets
            for target in targets:
                if (isinstance(target, ast.Subscript) and ast.unparse(target.value) == "sys.modules"
                        and isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str)):
                    found.append(f"line {sub.lineno}: {ast.unparse(target)}")
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and ast.unparse(sub.func.value) == "sys.modules"
                    and sub.func.attr in {"update", "setdefault", "pop", "clear", "__setitem__", "__delitem__"}):
                found.append(f"line {sub.lineno}: {ast.unparse(sub)[:80]}")
    return found


def test_the_scan_sees_the_test_tree():
    assert len(TEST_FILES) > 300, f"only {len(TEST_FILES)} test files found under {REPO}"


def test_no_test_module_replaces_an_importable_module_at_import_time():
    offenders = {
        str(path.relative_to(REPO)): hits
        for path in TEST_FILES
        if (hits := module_level_replacements(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, f"module-level sys.modules replacement leaks across the test process: {offenders}"


@pytest.mark.parametrize("source, flagged", [
    ('import sys\nsys.modules["mythic_container"] = object()\n', True),
    ('import sys\nif True:\n    sys.modules["x"] = 1\n', True),
    ('import sys\ndel sys.modules["x"]\n', True),
    ('import sys\nsys.modules.update({"x": 1})\n', True),
    ('import sys\nsys.modules[SPEC.name] = module\n', False),
    ('import sys\ndef test_it(monkeypatch):\n    sys.modules["x"] = 1\n', False),
    ('import sys\nvalue = sys.modules.get("x")\n', False),
])
def test_scan_flags_only_module_level_replacement_by_name(source, flagged):
    assert bool(module_level_replacements(source)) is flagged
