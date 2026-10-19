import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/stage_runtime_tools.py"
SPEC = importlib.util.spec_from_file_location("stage_runtime_tools", SCRIPT)
staging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staging)


@pytest.fixture
def roots(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    for root in (source, target):
        (root / "ai/langgraph").mkdir(parents=True)
        (root / "ai/langgraph/ttp_library.py").write_text("")
        (root / "tools").mkdir()
        (root / "tools/.keep").write_text("tracked placeholder")
    (source / "tools/Example.exe").write_bytes(b"inert fixture")
    return source, target


def test_cli_detects_archive_omission_then_stages_and_rechecks(roots):
    source, target = roots
    cmd = [sys.executable, str(SCRIPT), "--source-sage-root", str(source),
           "--destination-sage-root", str(target), "--required", "Example.exe"]
    before = subprocess.run(cmd, capture_output=True, text=True)
    assert before.returncode == 1
    assert json.loads(before.stdout)["missing"] == ["Example.exe"]
    copied = subprocess.run([*cmd, "--apply"], capture_output=True, text=True)
    assert copied.returncode == 0, copied.stdout
    assert (target / "tools/Example.exe").read_bytes() == b"inert fixture"
    assert subprocess.run(cmd, capture_output=True).returncode == 0
    assert staging.stage(source, target, required=[], apply=True)["copied"] == []


def test_conflict_is_detected_before_any_copy(roots):
    source, target = roots
    (source / "tools/Another.exe").write_bytes(b"another inert fixture")
    (target / "tools/Example.exe").write_bytes(b"operator change")
    with pytest.raises(ValueError, match="Destination conflicts"):
        staging.stage(source, target, required=[], apply=True)
    assert not (target / "tools/Another.exe").exists()
    assert (target / "tools/Example.exe").read_bytes() == b"operator change"


def test_empty_tool_is_rejected(roots):
    (roots[0] / "tools/Example.exe").write_bytes(b"")
    with pytest.raises(ValueError, match="Empty tool"):
        staging.stage(*roots, required=[], apply=True)


@pytest.mark.parametrize("name", ["Missing.exe", "example.exe", "../Example.exe"])
def test_required_missing_near_match_and_path_are_rejected(roots, name):
    with pytest.raises(ValueError):
        staging.stage(*roots, required=[name], apply=True)
    assert not (roots[1] / "tools/Example.exe").exists()


@pytest.mark.parametrize("side", [0, 1])
def test_symlink_entries_are_rejected(roots, side):
    (roots[side] / "tools/link.exe").symlink_to(roots[0] / "tools/Example.exe")
    with pytest.raises(ValueError, match="Unsupported tools entry"):
        staging.stage(*roots, required=[], apply=True)
