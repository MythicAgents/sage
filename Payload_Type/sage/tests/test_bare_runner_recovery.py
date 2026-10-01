"""Disk-backed bare-arm recovery checks; model and tool boundaries are injected fakes."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
import bare_runner as br  # noqa: E402


class Crash(BaseException):
    pass


USAGE = {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
BINDING = {"route": "raw", "source": "tree-1", "start": {"snapshot": "one"}}
TOOLS = [{"name": "issue_command"}]


def runner(path, model, tool, *, resume=False, reconcile=None, binding=BINDING, **kwargs):
    return br.BareModelRunner(
        model, tool, tools_spec=TOOLS, checkpoint_path=path,
        checkpoint_binding=binding, resume=resume,
        reconcile_pending_tool=reconcile, require_complete_usage=True,
        **kwargs,
    )


def test_saved_response_resumes_without_repeating_model_call(tmp_path):
    path = tmp_path / "private" / "arm.json"
    calls = []

    def first_model(*_):
        calls.append("model-1")
        return {"tool": "issue_command", "args": {"command": "whoami"}, "_usage": USAGE}

    def crash_after_response(event):
        if event["event"] == "model_response_retained":
            raise Crash()

    first = runner(path, first_model, lambda _: pytest.fail("tool ran"), event_sink=crash_after_response)
    with pytest.raises(Crash):
        first.run("objective")
    assert json.loads(path.read_text())["phase"] == "response_saved"
    assert path.stat().st_mode & 0o777 == 0o600

    def next_model(_system, _tools, history):
        calls.append("model-2")
        assert history == [{
            "response_id": "bare-model-call-1",
            "call": {"tool": "issue_command", "args": {"command": "whoami"}},
            "obs": "known observation",
        }]
        return {"final": "OBJECTIVE_MET", "_usage": USAGE}

    tool_calls = []
    resumed = runner(path, next_model, lambda call: tool_calls.append(call) or "known observation", resume=True)
    result = resumed.run("objective")
    assert calls == ["model-1", "model-2"]
    assert len(tool_calls) == 1
    assert result.stopped == "done" and result.steps == 1
    assert result.model_attempts == result.model_calls == 2
    assert result.total_tokens == 10 and result.token_usage_complete
    assert result.action_response_ids == ["bare-model-call-1"]
    assert runner(path, lambda *_: pytest.fail("model replay"), lambda _: pytest.fail("tool replay"), resume=True).run("objective") == result


def test_pending_tool_requires_exact_known_reconciliation(tmp_path):
    path = tmp_path / "arm.json"
    selected = {"tool": "issue_command", "args": {"command": "hostname"}}
    holder = {}

    def tool(call):
        assert call == selected
        holder["runner"].record_pending_task_id(42)
        raise Crash()

    holder["runner"] = runner(path, lambda *_: {**selected, "_usage": USAGE}, tool)
    with pytest.raises(Crash):
        holder["runner"].run("objective")
    saved = json.loads(path.read_text())
    assert saved["phase"] == "tool_pending"
    assert saved["pending_call"] == selected and saved["pending_task_id"] == 42

    denied = runner(path, lambda *_: pytest.fail("model replay"), lambda _: pytest.fail("tool replay"), resume=True)
    with pytest.raises(br.BareRunnerRecoveryError, match="requires reconciliation"):
        denied.run("objective")
    unresolved = runner(path, lambda *_: pytest.fail("model replay"), lambda _: pytest.fail("tool replay"),
                        resume=True, reconcile=lambda call: None)
    with pytest.raises(br.BareRunnerRecoveryError, match="no known completed observation"):
        unresolved.run("objective")
    assert json.loads(path.read_text())["phase"] == "tool_pending"

    seen = []
    resumed = None

    def reconcile(call):
        seen.append((call, resumed.pending_task_id))
        return "known completed output"

    resumed = runner(path, lambda *_: {"final": "STUCK", "_usage": USAGE},
                     lambda _: pytest.fail("tool replay"), resume=True, reconcile=reconcile)
    result = resumed.run("objective")
    assert seen == [(selected, 42)]
    assert result.transcript[0]["obs"] == "known completed output"
    assert result.total_tokens == 10 and result.steps == 1


def test_elapsed_floor_expires_resumed_arm_before_any_new_model_call(tmp_path):
    path = tmp_path / "arm.json"
    first = runner(path, lambda *_: {"final": "STUCK", "_usage": USAGE},
                   lambda _: pytest.fail("no tool"), timeout=3600,
                   event_sink=lambda event: (_ for _ in ()).throw(Crash())
                   if event["event"] == "model_response_retained" else None)
    with pytest.raises(Crash):
        first.run("objective")
    assert json.loads(path.read_text())["phase"] == "response_saved"
    resumed = runner(path, lambda *_: pytest.fail("no new model request"),
                     lambda _: pytest.fail("no tool"), resume=True,
                     timeout=3600, elapsed_floor_seconds=3601)
    result = resumed.run("objective")
    assert result.termination["cause"] == "wall_time"
    assert result.stopped == "timeout"


def test_pending_tool_reconciliation_crossing_deadline_ends_as_time_limit(tmp_path):
    path = tmp_path / "arm.json"
    selected = {"tool": "issue_command", "args": {"command": "hostname"}}
    first = runner(path, lambda *_: {**selected, "_usage": USAGE},
                   lambda _: (_ for _ in ()).throw(Crash()), timeout=0.02)
    with pytest.raises(Crash):
        first.run("objective")
    assert json.loads(path.read_text())["phase"] == "tool_pending"

    def late_reconcile(_):
        time.sleep(0.03)
        raise TimeoutError("existing Mythic task still pending")

    resumed = runner(path, lambda *_: pytest.fail("no new model request"),
                     lambda _: pytest.fail("no duplicate dispatch"), resume=True,
                     timeout=0.02, reconcile=late_reconcile)
    result = resumed.run("objective")
    assert result.termination["cause"] == "wall_time"


def test_bounded_provider_timeout_before_wall_is_a_model_error():
    result = br.BareModelRunner(
        lambda *_: (_ for _ in ()).throw(TimeoutError("provider request timed out")),
        lambda _: pytest.fail("no tool"), timeout=3600,
        provider_timeout_is_wall=False,
    ).run("objective")
    assert result.stopped == "error"
    assert result.termination["cause"] != "wall_time"


def test_inflight_model_is_explicitly_unrecoverable(tmp_path):
    path = tmp_path / "arm.json"

    def crash_model(*_):
        raise Crash()

    with pytest.raises(Crash):
        runner(path, crash_model, lambda _: pytest.fail("tool"), max_steps=1).run("objective")
    assert json.loads(path.read_text())["phase"] == "model_inflight"
    with pytest.raises(br.BareRunnerRecoveryError, match="usage is unknown"):
        runner(path, lambda *_: pytest.fail("model retry"), lambda _: pytest.fail("tool"),
               resume=True, max_steps=1).run("objective")


@pytest.mark.parametrize("change", [
    {"binding": {"route": "harness", "source": "tree-1", "start": {"snapshot": "one"}}},
    {"tools_spec": [{"name": "other"}]},
    {"max_steps": 2},
    {"token_ceiling": 9},
    {"system_template": "changed {objective} {tools}"},
])
def test_resume_rejects_changed_arm_binding(tmp_path, change):
    path = tmp_path / "arm.json"
    runner(path, lambda *_: {"final": "STUCK", "_usage": USAGE}, lambda _: "unused",
           max_steps=1).run("objective")
    kwargs = {"max_steps": 1, **change}
    binding = kwargs.pop("binding", BINDING)
    tools_spec = kwargs.pop("tools_spec", TOOLS)
    resumed = br.BareModelRunner(
        lambda *_: pytest.fail("model replay"), lambda _: pytest.fail("tool replay"),
        checkpoint_path=path, checkpoint_binding=binding, resume=True,
        tools_spec=tools_spec, require_complete_usage=True, **kwargs,
    )
    with pytest.raises(br.BareRunnerRecoveryError, match="binding differs"):
        resumed.run("objective")


def test_terminal_response_survives_crash_before_result_return(tmp_path):
    path = tmp_path / "arm.json"

    def crash_final(message):
        if message.startswith("■ FINAL"):
            raise Crash()

    with pytest.raises(Crash):
        runner(path, lambda *_: {"final": "OBJECTIVE_MET", "_usage": USAGE},
               lambda _: pytest.fail("tool"), logger=crash_final).run("objective")
    result = runner(path, lambda *_: pytest.fail("model replay"), lambda _: pytest.fail("tool"),
                    resume=True).run("objective")
    assert result.stopped == "done" and result.objective_recognized
    assert result.model_calls == 1 and result.total_tokens == 5
    assert json.loads(path.read_text())["phase"] == "completed"
