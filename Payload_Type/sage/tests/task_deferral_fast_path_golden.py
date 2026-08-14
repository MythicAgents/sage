"""Task-deferral golden capture: the fast path must stay byte-identical across the change.

Two records, because the change straddles two surfaces and neither one alone would catch a
regression in the other:

  ``tool_returns``  the exact string ``issue_task_and_waitfor_task_output`` hands back when output
                    arrives inside the grace period. This is the surface being edited, so it is the
                    one a careless edit breaks first.
  ``chat_emissions`` the ordered emission record from ``sage_chat/headless.py`` for every frozen
                    conversation-contract case. A direct call to ``MythicTools`` never reaches the
                    chat path, so tool-use cards and the terminal-status discipline are invisible to
                    the record above.

Capture (must run BEFORE the first production edit):

    PYTHONPATH=Payload_Type/sage .venv/bin/python -m tests.task_deferral_fast_path_golden capture

The companion assertions live in ``test_task_deferral_receipt.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

GOLDEN_PATH = Path(__file__).resolve().parent / "goldens" / "task_deferral_fast_path.json"

# Fast-path cases: output is already present, so the wait never approaches any deadline. Each is a
# (case_id, command, parameters, task_output) tuple driven through the real production entry point.
FAST_PATH_CASES = (
    ("plain-success", "whoami", "", "NORTH\\samwell.tarly"),
    ("empty-params-dict", "rev2self", {}, "Reverted identity to NORTH\\samwell.tarly"),
    ("populated-params", "shell", "hostname", "CASTELBLACK"),
    ("failure-output", "rev2self", "", "[-] failed to parse arguments for rev2self"),
    ("empty-output", "whoami", "", ""),
)


def _capture_tool_returns() -> dict[str, str]:
    """Drive the production entry point once per case with a fresh MythicTools.

    Fresh per case on purpose: the circuit breaker and the loop guard both accumulate across calls
    on one instance, so a shared instance would make the record depend on case ordering.
    """
    from test_circuit_breaker import _make_tools, _split_issue

    returns: dict[str, str] = {}
    for case_id, command, parameters, output in FAST_PATH_CASES:
        tools = _make_tools()
        with _split_issue(output):
            returns[case_id] = asyncio.run(
                tools.issue_task_and_waitfor_task_output(command, parameters, 11)
            )
    return returns


def _capture_chat_emissions() -> dict[str, list]:
    """Snapshot the ordered emission record for every frozen conversation-contract case."""
    from tests.conversation_contract import CASES, run_case

    emissions: dict[str, list] = {}
    for case in CASES:
        result = asyncio.run(run_case(case))
        emissions[case.case_id] = [
            # Sorted keys so the record is stable against dict-construction order, which is not
            # behaviour and would otherwise produce a false diff.
            {key: repr(value) for key, value in sorted(emission.items())}
            for emission in result.emissions
        ]
    return emissions


def capture() -> dict:
    return {
        "tool_returns": _capture_tool_returns(),
        "chat_emissions": _capture_chat_emissions(),
    }


def load() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] != "capture":
        print(__doc__)
        return 2
    record = capture()
    # A golden captured twice in one process must be identical to itself, or it is not a golden and
    # every later comparison is noise. Prove that here rather than discovering it after the edit.
    if capture() != record:
        print("UNSTABLE: two captures in one process differ; this record cannot serve as a golden")
        return 1
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(
        f"captured {len(record['tool_returns'])} tool returns and "
        f"{len(record['chat_emissions'])} chat cases -> {GOLDEN_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
