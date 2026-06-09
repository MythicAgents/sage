"""Regression coverage for MythicTools._validate_command_parameters.

The validation layer previously referenced an undefined `payload_type` in its ARGVAL log
lines, so EVERY validation path raised NameError and was swallowed by the fail-open handler —
silently disabling all parameter validation. These tests pin the contract: validation must
resolve, not raise, on both the accept and reject paths (the reject path is what regressed).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage

from ai.langgraph.mythic_tools import MythicTools  # noqa: E402


def _tools_with_schema(monkeypatch_schema):
    tools = MythicTools("agent-task-id")
    tools.client = object()
    # Both helpers are async; stub them so validation runs without a live Mythic.
    tools._resolve_payload_type = AsyncMock(return_value="apollo")
    tools._fetch_command_schema = AsyncMock(return_value=monkeypatch_schema)
    return tools


_SCHEMA = [
    {"name": "host", "cli_name": "host", "type": "String",
     "parameter_group_name": "Default", "required": True, "choices": None},
]


def test_validate_accepts_valid_params_without_raising():
    # Exercises the "validated" log line that references payload_type — must not NameError.
    tools = _tools_with_schema(_SCHEMA)
    result = asyncio.run(tools._validate_command_parameters("ls", {"host": "WINTERFELL"}, 2))
    assert result is None  # valid -> passes validation (None), did NOT fail open via exception


def test_validate_rejects_unknown_param_instead_of_failing_open():
    # The regressed path: an unknown key hits the rejection log that references payload_type.
    # Before the fix this raised NameError and fell through to fail-open (returned None).
    tools = _tools_with_schema(_SCHEMA)
    result = asyncio.run(tools._validate_command_parameters("ls", {"bogus_key": "x"}, 2))
    assert isinstance(result, str)  # a real rejection string, not a silent fail-open None
    assert "bogus_key" in result
