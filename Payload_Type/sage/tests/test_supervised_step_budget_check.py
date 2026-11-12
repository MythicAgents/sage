"""The supervised step-budget provider check, run offline: its stand-in answers behind the bounded transport and no
socket may open. The readiness run proves the check exercises all three bounds through Sage's own code and that its
verdict fails when the final call is offered tools."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from ai.langgraph import model as model_module

_PATH = Path(__file__).resolve().parents[3] / "skills/sage-focused-capability-tests/scripts/check_supervised_step_budget.py"
_SPEC = importlib.util.spec_from_file_location("check_supervised_step_budget", _PATH)
CHECK = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = CHECK  # so pydantic resolves the module's annotations
_SPEC.loader.exec_module(CHECK)

ENDPOINT = "http://127.0.0.1:9/v1"


def _readiness(tmp_path: Path) -> tuple[int, dict]:
    config = tmp_path / "profile.env"
    config.write_text(f"provider=openai\nmodel=offline-model\nAPI_ENDPOINT={ENDPOINT}\nAPI_KEY=offline-only\n")
    out = tmp_path / "out"
    code = CHECK.main(["--config", str(config), "--output-dir", str(out), "--expected-model", "offline-model",
                       "--expected-endpoint-sha256", hashlib.sha256(ENDPOINT.encode()).hexdigest()])
    return code, json.loads((out / "readiness.json").read_text())


def test_offline_readiness_exercises_each_bound_with_one_tool_free_final_call(tmp_path):
    code, record = _readiness(tmp_path)

    assert (code, record["status"], record["provider_requests"]) == (0, "dry_run_ready", 3)
    assert [result["scenario"] for result in record["results"]] == ["tool_calls", "active_time",
                                                                   "consecutive_errors"]
    for result in record["results"]:
        assert all(result["checks"].values()), result["checks"]
        [wire] = result["wire"]
        assert "tools" not in wire["request"]
        assert result["forwarded"] == [CHECK.OFFLINE_ANSWER]
    assert record["usefulness"] == {"answered": 3, "fallback": 0}


def test_the_verdict_fails_when_the_final_call_is_offered_tools(tmp_path, monkeypatch):
    # Without the wrap-up branch the bounded step's next call is an ordinary one, with the worker's tool menu.
    monkeypatch.setattr(model_module._SupervisedStepBoundMiddleware, "_pending_wrap_up", lambda self, request: None)
    code, record = _readiness(tmp_path)

    assert (code, record["status"]) == (1, "not_ready")
    assert not record["results"][0]["checks"]["empty_wire_menu"]
