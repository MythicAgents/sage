import asyncio
import importlib.util
import json
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "sage-live-runner"
    / "scripts"
    / "run_unguided_essos_da.py"
)
SPEC = importlib.util.spec_from_file_location("run_unguided_essos_da", SCRIPT)
run_unguided = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(run_unguided)


def _callbacks():
    return [
        {
            "display_id": 1,
            "host": "SAGE",
            "user": "Sage",
            "payload": {"payloadtype": {"name": "sage"}},
        },
        {
            "display_id": 2,
            "host": "CASTELBLACK",
            "user": "NORTH\\samwell.tarly",
            "payload": {"payloadtype": {"name": "apollo"}},
        },
        {
            "display_id": 3,
            "host": "CASTELBLACK",
            "user": "NORTH\\samwell.tarly",
            "payload": {"payloadtype": {"name": "apollo"}},
        },
        {
            "display_id": 4,
            "host": "SAGE",
            "user": "Sage",
            "payload": {"payloadtype": {"name": "sage"}},
        },
    ]


async def _liveness(_client, display_id):
    return {"alive": display_id in {3, 4}}


def _launch():
    issued = []

    async def issue_task(**kwargs):
        issued.append(kwargs)
        return {"display_id": 41}

    result = asyncio.run(
        run_unguided.launch_unguided_solve(
            object(),
            callbacks=_callbacks(),
            issue_task_fn=issue_task,
            assess_liveness_fn=_liveness,
        )
    )
    return result, issued


def test_strict_runner_never_issues_state_task():
    _result, issued = _launch()

    assert all(call["command_name"] != "state" for call in issued)


def test_strict_runner_issues_exactly_one_query_task():
    result, issued = _launch()

    assert result["sage_cb"] == 4
    assert result["apollo_cb"] == 3
    assert [call["command_name"] for call in issued] == ["query"]
    assert issued[0]["callback_display_id"] == 4


def test_strict_runner_prompt_contains_no_guided_route_facts():
    prompt = run_unguided.build_objective()
    params = run_unguided.build_query_parameters()

    assert prompt == "From the current foothold, achieve administrative control of essos.local."
    assert params["prompt"] == prompt
    assert params["verbose"] is True
    assert params["autonomous_solve"] is True
    assert params["max_steps"] == 0
    assert params["mode"] == "auto"
    assert "can-read-managed-local-admin-secret" not in prompt
    assert "certificate-auth-target" not in prompt
    assert "starkwallpaper" not in prompt.casefold()
    assert "cersei" not in prompt.casefold()
    assert "braavos" not in prompt.casefold()
    assert "sevenkingdoms" not in prompt.casefold()


def test_strict_runner_callback_overrides_still_require_live_matching_callbacks():
    selected = asyncio.run(
        run_unguided.select_run_callbacks(
            object(),
            _callbacks(),
            sage_cb=4,
            apollo_cb=3,
            assess_liveness_fn=_liveness,
        )
    )

    assert selected == (4, 3)


def test_strict_runner_report_counts_apollo_subtasks_and_sharphound_collections():
    subtasks = [
        {
            "display_id": 42,
            "command_name": "execute_assembly",
            "original_params": json.dumps(
                {"assembly_name": "SharpHound.exe", "assembly_arguments": "-c All"}
            ),
            "callback": {
                "display_id": 3,
                "payload": {"payloadtype": {"name": "apollo"}},
            },
        },
        {
            "display_id": 43,
            "command_name": "shell",
            "original_params": "whoami",
            "callback": {
                "display_id": 3,
                "payload": {"payloadtype": {"name": "apollo"}},
            },
        },
        {
            "display_id": 44,
            "command_name": "query",
            "original_params": "{}",
            "callback": {
                "display_id": 4,
                "payload": {"payloadtype": {"name": "sage"}},
            },
        },
    ]

    report = run_unguided.build_report(
        solve_task_id=41,
        elapsed_seconds=123,
        subtasks=subtasks,
    )

    assert report == {
        "solve_task_id": 41,
        "elapsed_seconds": 123,
        "apollo_subtask_count": 2,
        "sharphound_collection_count": 1,
        "only_one_sharphound_collection": True,
    }
