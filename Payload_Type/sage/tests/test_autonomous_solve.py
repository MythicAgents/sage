import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import prompt_loader  # noqa: E402


def _load_model_class():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        mod = importlib.import_module("ai.langgraph.model")
    except Exception as e:
        pytest.skip(f"model.py runtime unavailable: {e}")
    return mod.Model


def test_supervisor_overlay_contains_autonomous_attack_path_solve():
    section = prompt_loader.load_autonomous_overlay("Supervisor")
    assert section
    assert "AUTONOMOUS ATTACK-PATH SOLVE" in section


def test_mythic_operator_overlay_contains_autonomous_execution():
    section = prompt_loader.load_autonomous_overlay("Mythic_Operator")
    assert section
    assert "AUTONOMOUS EXECUTION" in section


def test_unknown_overlay_role_raises_value_error():
    with pytest.raises(ValueError):
        prompt_loader.load_autonomous_overlay("Generalist")


def test_autonomous_overlay_sections_are_distinct():
    supervisor = prompt_loader.load_autonomous_overlay("Supervisor")
    mythic_operator = prompt_loader.load_autonomous_overlay("Mythic_Operator")
    assert supervisor != mythic_operator


def test_apply_overlay_disabled_is_byte_identical():
    Model = _load_model_class()
    m = Model.__new__(Model)
    m._autonomous_solve = False
    original = "BASE SUPERVISOR PROMPT"
    out = m._apply_autonomous_overlay(original, "Supervisor")
    assert out is original


def test_apply_overlay_enabled_appends_section():
    Model = _load_model_class()
    m = Model.__new__(Model)
    m._autonomous_solve = True
    base = "BASE SUPERVISOR PROMPT"
    section = prompt_loader.load_autonomous_overlay("Supervisor")
    out = m._apply_autonomous_overlay(base, "Supervisor")
    assert out == base + "\n\n" + section
    assert len(out) > len(base)
    assert "AUTONOMOUS ATTACK-PATH SOLVE" in out
