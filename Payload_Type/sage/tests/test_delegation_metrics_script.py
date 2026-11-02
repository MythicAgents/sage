import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-trace-analysis" / "scripts" / "delegation_metrics.py"
SPEC = importlib.util.spec_from_file_location("delegation_metrics", SCRIPT)
metrics = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(metrics)

PROMPT = "From your current foothold, obtain administrative control of essos"
REQUEST = {"id": 1, "prompt": PROMPT, "created_at": "2026-10-30T00:00:00", "completed_at": "2026-10-30T01:00:00"}


def _llm(minute, agent, *, prompt_text=""):
    return {"name": "ChatOpenAI", "kind": "LLM", "minute": minute, "agent": agent,
            "llm": {"input_messages": [{"message": {"role": "user", "content": prompt_text}}]}}


def _tool(minute, name, *, input_value="", output_value="", agent="Mythic_Operator", status="OK"):
    return {"name": name, "kind": "TOOL", "minute": minute, "agent": agent,
            "input": input_value, "output": output_value, "status": status}


def _task(minute, task_id, command="whoami"):
    return _tool(minute, "issue_task_and_waitfor_task_output", input_value=json.dumps({"command": command}),
                 output_value=f"[Mythic task issuance identity] task_display_id={task_id} callback_display_id=1")


def _phoenix(path: Path, spans: list[dict]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY, name TEXT, span_kind TEXT, start_time TEXT, "
                 "end_time TEXT, status_code TEXT, llm_token_count_prompt INTEGER, "
                 "llm_token_count_completion INTEGER, attributes TEXT)")
    for span in spans:
        attrs = {"metadata": {"lc_agent_name": span["agent"]}}
        if "input" in span:
            attrs["input"] = {"value": span["input"]}
            attrs["output"] = {"value": span["output"]}
        if "llm" in span:
            attrs["llm"] = span["llm"]
        start = f"2026-10-30 00:{span['minute']:02d}:00.000000"
        conn.execute("INSERT INTO spans (name, span_kind, start_time, end_time, status_code, llm_token_count_prompt, "
                     "llm_token_count_completion, attributes) VALUES (?, ?, ?, ?, ?, 10, 2, ?)",
                     (span["name"], span["kind"], start, start, span.get("status", "OK"), json.dumps(attrs)))
    conn.commit()
    conn.close()
    return path


def _report(tmp_path, spans, request=REQUEST):
    db = _phoenix(tmp_path / "phoenix.db", spans)
    return metrics.request_metrics(metrics.load_spans(db, request["created_at"], request["completed_at"]), request)


def test_title_only_handoff_and_a_derived_brief_are_told_apart(tmp_path):
    report = _report(tmp_path, [
        _llm(1, "Supervisor"),
        _tool(2, "transfer_to_Mythic_Operator", input_value="List active callbacks", agent="Supervisor"),
        _llm(3, "Supervisor"),
        _tool(4, "transfer_to_Mythic_Operator", agent="Supervisor", input_value=json.dumps({
            "handoff_title": "Enumerate essos", "handoff_instruction": "Objective: " + PROMPT + "\nSteps: enumerate"})),
        _tool(5, "transfer_to_BloodHound", agent="Supervisor", input_value=json.dumps({
            "handoff_title": "Paths", "handoff_instruction": "Find a path from samwell to essos domain admins"})),
    ])

    first, second, third = report["delegation_detail"]
    assert first["title_only"] and first["prompt_coverage"] is None
    assert not second["title_only"] and second["prompt_coverage"] == 1.0
    assert 0 < third["prompt_coverage"] < 0.5  # near-match: shares "essos" and "from" only
    assert report["delegation_targets"] == {"Mythic_Operator": 2, "BloodHound": 1}


def test_delegation_window_ends_at_the_supervisors_next_call(tmp_path):
    report = _report(tmp_path, [
        _llm(1, "Supervisor"),
        _tool(2, "transfer_to_Mythic_Operator", input_value="go", agent="Supervisor"),
        _llm(3, "Mythic_Operator"), _task(4, 1), _llm(5, "Mythic_Operator"), _task(6, 2),
        _llm(10, "Supervisor"),
        _task(11, 3),  # issued after control returned: must not count toward the delegation
    ])

    (delegation,) = report["delegation_detail"]
    assert delegation["returned"] is True
    assert delegation["worker_model_calls"] == 2
    assert delegation["tool_calls"] == 2
    assert delegation["issued_task_ids"] == [1, 2]
    assert delegation["wall_seconds"] == 8 * 60
    assert report["returns_to_supervisor"] == 1
    assert report["max_seconds_between_supervisor_calls"] == 9 * 60


def test_a_delegation_that_never_returns_runs_to_the_request_end(tmp_path):
    report = _report(tmp_path, [
        _llm(1, "Supervisor"),
        _tool(2, "transfer_to_Mythic_Operator", input_value="go", agent="Supervisor"),
        _task(3, 1),
    ])

    (delegation,) = report["delegation_detail"]
    assert delegation["returned"] is False
    assert delegation["handback_visible_task_ids"] is None
    assert delegation["wall_seconds"] == 58 * 60


@pytest.mark.parametrize("handback, visible", [
    ('{"task_id": 1, "task_status": "success"}', [1]),          # observation envelope JSON
    ('[{\\"task_id\\": 2}]', [2]),                               # envelope JSON nested in a string
    ("Task #1 finished; task 2 output pending", [1, 2]),         # prose
    ("task_display_id=2 callback_display_id=1", [2]),            # issuance header
    ("Callback 1 and 2 are alive; the worker made progress.", []),  # near-match: numbers, but no task
])
def test_handback_visibility_counts_only_issued_tasks_the_supervisor_can_see(tmp_path, handback, visible):
    report = _report(tmp_path, [
        _llm(1, "Supervisor"),
        _tool(2, "transfer_to_Mythic_Operator", input_value="go", agent="Supervisor"),
        _task(3, 1), _task(4, 2),
        _llm(5, "Supervisor", prompt_text=handback),
    ])

    assert report["delegation_detail"][0]["handback_visible_task_ids"] == visible
    assert report["handback_task_visibility"] == round(len(visible) / 2, 3)


def test_only_identical_tool_calls_count_as_repeats(tmp_path):
    report = _report(tmp_path, [
        _tool(1, "transfer_to_Mythic_Operator", input_value="go", agent="Supervisor"),
        _task(2, 1, command="whoami"), _task(3, 1, command="whoami"),  # same input twice
        _task(4, 2, command="net_dclist"),                              # different input
        _tool(5, "list_callbacks", input_value="{}", status="ERROR"),
    ])

    (delegation,) = report["delegation_detail"]
    assert delegation["repeat_tool_calls"] == 1
    assert delegation["tool_errors"] == 1


def test_approval_cards_report_status_and_median_decision_time():
    cards = [
        {"request_id": 1, "created_at": "2026-10-30T00:00:00", "resolved_at": "2026-10-30T00:00:10Z", "status": "accepted"},
        {"request_id": 1, "created_at": "2026-10-30T00:01:00", "resolved_at": "2026-10-30T00:01:30Z", "status": "rejected"},
        {"request_id": 1, "created_at": "2026-10-30T00:02:00", "resolved_at": "2026-10-30T00:02:20Z", "status": "accepted"},
    ]

    assert metrics.card_metrics(cards) == {
        "approval_cards": 3,
        "approval_cards_by_status": {"accepted": 2, "rejected": 1},
        "median_decision_seconds": 20.0,
    }


def test_cli_writes_json_and_markdown_and_opens_phoenix_read_only(tmp_path):
    db = _phoenix(tmp_path / "phoenix.db", [
        _llm(1, "Supervisor"),
        _tool(2, "transfer_to_Mythic_Operator", input_value="go", agent="Supervisor"),
        _task(3, 1),
    ])
    db.chmod(0o444)
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"requests": [REQUEST], "cards": []}))

    assert metrics.main(["--phoenix", str(db), "--mythic-export", str(export),
                         "--json-out", str(tmp_path / "m.json"), "--markdown-out", str(tmp_path / "m.md")]) == 0

    report = json.loads((tmp_path / "m.json").read_text())
    assert report["requests"][0]["issued_tasks"] == 1
    assert report["requests"][0]["approval_cards"] == 0
    assert "| 1 | 1 → Mythic_Operator | go | sage_run | True |" in (tmp_path / "m.md").read_text()


def _rendered_step_outcome(referenced: list[int]) -> str:
    """The Sage-authored step outcome exactly as the Supervisor reads it, referencing the given tasks."""
    from ai.langgraph import step_bounds, step_references

    record = step_bounds.StepRecord("r", 1, "Mythic_Operator", 0.0)
    outcome = step_bounds.step_outcome(record, step_bounds.StepActivity(), now=1.0, tasks_issued={"1", "2"})
    outcome["references"]["tasks"] = [
        step_references.task_reference(task_id, {"status": "success", "completed": True}, []) for task_id in referenced]
    return step_bounds.render_outcome(outcome)


@pytest.mark.parametrize("referenced, visible", [
    ([1, 2], [1, 2]),   # every issued task referenced
    ([1], [1]),         # negative: an issued task missing from the references stays invisible
    ([], []),           # near-match: the bare "Mythic tasks issued: 1, 2" list is not a reference
])
def test_handback_visibility_reads_the_step_outcomes_task_references(tmp_path, referenced, visible):
    report = _report(tmp_path, [
        _llm(1, "Supervisor"),
        _tool(2, "transfer_to_Mythic_Operator", input_value="go", agent="Supervisor"),
        _task(3, 1), _task(4, 2),
        _llm(5, "Supervisor", prompt_text=_rendered_step_outcome(referenced)),
    ])

    assert report["delegation_detail"][0]["handback_visible_task_ids"] == visible


def _handoff(minute, *, graph, title="Step", goto=None):
    """A transfer_to_* span whose output is the handoff Command as Phoenix records it."""
    if goto is None:
        goto = "Mythic_Operator" if graph == "__parent__" else []
    return _tool(minute, "transfer_to_Mythic_Operator", agent="Supervisor",
                 input_value=json.dumps({"handoff_title": title, "handoff_instruction": "do it"}),
                 output_value=json.dumps({"graph": graph, "goto": goto, "update": {"messages": []}}))


def test_a_rejected_brief_is_not_a_delegation(tmp_path):
    report = _report(tmp_path, [
        _llm(1, "Supervisor"),
        _handoff(2, graph=None, title="Rejected"),          # brief failed its checks: reached no worker
        _llm(3, "Supervisor"),
        _handoff(4, graph="__parent__", title="Accepted"),
        _llm(5, "Mythic_Operator"),
        _llm(6, "Supervisor"),
    ])

    assert report["delegations"] == 1
    assert report["rejected_briefs"] == 1
    assert report["returns_to_supervisor"] == 1
    assert report["delegation_targets"] == {"Mythic_Operator": 1}
    assert [d["step_kind"] for d in report["delegation_detail"]] == ["not_dispatched", "model"]


def test_a_second_rejection_that_ends_the_step_is_not_a_delegation(tmp_path):
    """Near-match: this rejection returns to the parent graph, but to end the step rather than reach the worker."""
    report = _report(tmp_path, [
        _llm(1, "Supervisor"),
        _handoff(2, graph=None),
        _llm(3, "Supervisor"),
        _handoff(4, graph="__parent__", goto="__end__"),
    ])

    assert report["delegations"] == 0
    assert report["rejected_briefs"] == 2


def test_a_handoff_output_without_the_graph_field_counts_as_dispatched(tmp_path):
    """Near-match: runs from before the brief check recorded plain text, and every handoff was dispatched."""
    report = _report(tmp_path, [
        _tool(1, "transfer_to_Mythic_Operator", input_value="go", agent="Supervisor",
              output_value="Delegated to Mythic_Operator with instruction: go"),
        _llm(2, "Mythic_Operator"),
    ])

    assert report["delegations"] == 1 and report["rejected_briefs"] == 0


def test_tasks_a_procedure_issued_are_counted_from_the_mythic_export(tmp_path):
    export_tasks = [
        {"display_id": 18, "timestamp": "2026-10-30T00:03:30"},   # inside the window, issued by the procedure
        {"display_id": 1, "timestamp": "2026-10-30T00:04:00"},    # also seen in a tool output: counted once
        {"display_id": 19, "timestamp": "2026-10-30T00:12:00"},   # after control returned
        {"display_id": 20, "timestamp": "2026-10-29T23:59:00"},   # before the handoff
    ]
    db = _phoenix(tmp_path / "phoenix.db", [
        _llm(1, "Supervisor"),
        _handoff(2, graph="__parent__"),
        _tool(3, "execute_capability", input_value='{"capability": "collect-graph"}', output_value="ok"),
        _task(4, 1),
        _llm(10, "Supervisor", prompt_text="task 18 completed"),
    ])
    report = metrics.request_metrics(
        metrics.load_spans(db, REQUEST["created_at"], REQUEST["completed_at"]), REQUEST, None, export_tasks)

    (delegation,) = report["delegation_detail"]
    assert delegation["issued_task_ids"] == [1, 18]
    assert delegation["handback_visible_task_ids"] == [18]
    assert report["issued_tasks"] == 2


def test_without_export_tasks_only_tool_outputs_count(tmp_path):
    report = _report(tmp_path, [_handoff(1, graph="__parent__"), _task(2, 5), _llm(3, "Supervisor")])

    assert report["delegation_detail"][0]["issued_task_ids"] == [5]


@pytest.mark.parametrize("worker_spans, kind", [
    ([_llm(3, "Mythic_Operator"), _task(4, 1)], "model"),
    ([_tool(3, "execute_capability"), _tool(4, "get_task_output"), _tool(5, "get_task_output")], "sage_run"),
    ([], "ended_before_model"),
])
def test_step_kind_tells_model_work_from_sage_run_procedures_and_dead_steps(tmp_path, worker_spans, kind):
    report = _report(tmp_path, [_llm(1, "Supervisor"), _handoff(2, graph="__parent__"), *worker_spans,
                                _llm(9, "Supervisor")])

    assert report["delegation_detail"][0]["step_kind"] == kind
    assert report["step_kinds"] == {kind: 1}
