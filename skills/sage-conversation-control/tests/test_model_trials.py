from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

# `tests.conversation_contract` lives under Payload_Type/sage, so it resolves only once that is on
# sys.path. The script under test does this before its own import; this module has to as well, or
# it fails at COLLECTION — which is worse than a failing test, because pytest then aborts the whole
# run rather than reporting one red module.
REPO_ROOT = Path(__file__).resolve().parents[3]
SAGE_ROOT = REPO_ROOT / "Payload_Type" / "sage"
if str(SAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(SAGE_ROOT))

from tests.conversation_contract import CASES  # noqa: E402


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_model_trials.py"
)
SPEC = importlib.util.spec_from_file_location("sage_conversation_model_trials", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_five_trial_aggregation_reports_complete_clean_matrix():
    async def generate(case, trial):
        return (
            f"Model display for {case.case_id} trial {trial}: DONE / BLOCKER / handoff.",
            {
                "response_type": "AIMessage",
                "response_id_present": True,
                "response_model": "fake-model",
            },
        )

    report = asyncio.run(MODULE.run_trials(
        CASES,
        trials=5,
        generator=generate,
        concurrency=7,
    ))

    assert report["passed"] is True
    assert report["case_count"] == len(CASES) == 20
    assert report["trial_count"] == 100
    assert report["forbidden_event_count"] == 0
    assert report["duplicate_event_count"] == 0
    assert report["terminal_correct_count"] == 100
    assert all(
        row == {
            "pass_k": 5,
            "required_k": 5,
            "forbidden_event_count": 0,
            "duplicate_event_count": 0,
            "terminal_correct_count": 5,
        }
        for row in report["per_case"].values()
    )


def test_empty_or_unbound_model_response_fails_closed():
    async def generate(_case, _trial):
        return "", {
            "response_type": "AIMessage",
            "response_id_present": False,
            "response_model": "",
        }

    report = asyncio.run(MODULE.run_trials(
        CASES[:1],
        trials=1,
        generator=generate,
        concurrency=1,
    ))

    assert report["passed"] is False
    assert report["failures"][0]["response_nonempty"] is False
    assert report["failures"][0]["response_provenance_bound"] is False


def test_route_label_redacts_credentials_and_query():
    assert MODULE._route_label(
        "http://user:secret@127.0.0.1:8100/v1?token=secret"
    ) == "http://127.0.0.1:8100/v1"
