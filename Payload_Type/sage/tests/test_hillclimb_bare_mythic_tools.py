"""Offline tests for the stripped bare Mythic toolset + the upgraded bare runner loop.

The live Mythic SDK calls are validated on the lab; these pin the pure parts: the tool schema set
(discovery/tasking/tools-folder present, NO hardcoded commands, NO excluded secret sauce), dispatch
routing, tools-folder discovery, and the runner's unlimited-steps / wall-clock / live-watch behavior.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import bare_mythic_tools as bmt  # noqa: E402
import bare_runner  # noqa: E402


def test_bare_tool_specs_are_mythic_not_commands():
    names = {s["function"]["name"] for s in bmt.bare_tool_specs()}
    # Sage-style discovery + tasking + bring-your-own-tooling are present...
    assert {"list_callbacks", "get_payload_types", "get_commands", "issue_command", "get_task_history",
            "get_task_output", "list_tools_folder", "register_tool", "upload_file_to_target",
            "read_credentials", "add_credential"} <= names
    # ...and Apollo commands are NOT tools anymore (the model discovers them via get_commands).
    assert "mimikatz" not in names and "shell" not in names and "powerpick" not in names
    for s in bmt.bare_tool_specs():
        assert s["type"] == "function"
        assert s["function"]["name"] and "parameters" in s["function"]


def test_excluded_secret_sauce_not_exposed():
    names = set(bmt.TOOLS)
    for sauce in ("get_ttp_guidance", "get_ttp_full_reference", "list_ttp_categories",
                  "execute_capability", "build_capability_commands", "ingest_collection",
                  "check_callback_alive", "assess_callback_liveness", "download_tool"):
        assert sauce not in names, f"{sauce} is harness secret sauce and must not reach the bare model"
    assert "list_callbacks" in names   # raw callback listing IS provided (fixes the cb-pivot gap)


def test_dispatcher_unknown_tool_is_graceful():
    ex = bmt.make_mythic_dispatcher(client=None)
    assert "[unknown tool]" in ex({"tool": "does_not_exist", "args": {}})


def test_list_tools_folder_local(tmp_path, monkeypatch):
    (tmp_path / "Rubeus.exe").write_bytes(b"MZ")
    (tmp_path / "SharpHound.exe").write_bytes(b"MZ")
    (tmp_path / ".keep").write_bytes(b"")
    monkeypatch.setattr(bmt, "TOOLS_DIR", tmp_path)
    assert bmt.list_tools_folder_local() == ["Rubeus.exe", "SharpHound.exe"]   # sorted, hidden excluded


def test_runner_unlimited_runs_past_old_cap():
    """max_steps=0 means unlimited — it must run well past the old hardcoded 40-step cap."""
    calls = {"n": 0}
    def model_fn(system, tools, history):
        calls["n"] += 1
        return {"tool": "issue_command", "args": {}} if calls["n"] <= 45 else {"final": "done"}
    r = bare_runner.BareModelRunner(model_fn, lambda d: "obs", max_steps=0).run("obj")
    assert r.stopped == "done" and r.steps == 45


def test_runner_optional_step_cap_still_works():
    r = bare_runner.BareModelRunner(lambda s, t, h: {"tool": "x", "args": {}}, lambda d: "obs",
                                    max_steps=3).run("obj")
    assert r.stopped == "budget" and r.steps == 3


def test_runner_wallclock_timeout_is_not_a_step_cap():
    r = bare_runner.BareModelRunner(lambda s, t, h: {"tool": "x", "args": {}}, lambda d: "obs",
                                    max_steps=0, timeout=0).run("obj")
    assert r.stopped == "timeout" and r.steps == 0


def test_runner_live_logger_captures_action_and_observation():
    logs = []
    def model_fn(s, t, h):
        return {"final": "ok"} if any("ACTION" in m for m in logs) else {"tool": "list_callbacks", "args": {}}
    bare_runner.BareModelRunner(model_fn, lambda d: "OBS-DATA", max_steps=0, logger=logs.append).run("obj")
    assert any("BARE START" in m for m in logs)
    assert any("ACTION: list_callbacks" in m for m in logs)
    assert any("OBSERVATION" in m and "OBS-DATA" in m for m in logs)


def test_runner_graceful_interrupt_on_model_call():
    """Ctrl-C during model thinking -> stop with a partial transcript (the range still gets scored)."""
    def model_fn(s, t, h):
        raise KeyboardInterrupt()
    r = bare_runner.BareModelRunner(model_fn, lambda d: "obs", max_steps=0).run("obj")
    assert r.stopped == "interrupted"


def test_runner_graceful_interrupt_on_tool_call():
    def tool_exec(d):
        raise KeyboardInterrupt()
    r = bare_runner.BareModelRunner(lambda s, t, h: {"tool": "x", "args": {}}, tool_exec, max_steps=0).run("obj")
    assert r.stopped == "interrupted" and r.steps == 0
