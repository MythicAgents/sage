"""The operator-facing execution-event status label must not contradict Mythic state.

Observed live: Mythic recorded a `certutil -TCAInfo` task as
`run success`/completed while the operator-facing card said "Tool `shell` failed." — because Sage's
output classification read the command output (`CertUtil: -TCAInfo command FAILED: ...`) as a
failure. Three different facts were conflated into one status word: Mythic's task terminal state,
the command's own exit semantics, and Sage's output classification. The card label is the one the
operator scans; it renders Mythic's state, and the classification nuance moves into the preview.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import Model, stable_event_id  # noqa: E402


def _model_shell(observed_sink: list[dict[str, Any]]):
    """A Model shell exposing only what _emit_execution_event touches."""
    model = Model.__new__(Model)

    def _noop_lineage(_event: dict[str, Any]) -> None:
        return None

    def _observe(event: dict[str, Any]) -> None:
        observed_sink.append(event)

    model._controller_update_transaction_task_lineage = _noop_lineage
    model._observe_supervised_execution_event = _observe
    ledger = SimpleNamespace(request_id="chat:1:request:1")
    model._ensure_request_event_ledger = lambda: ledger
    cards: list[dict[str, Any]] = []

    async def card(**kwargs: Any) -> None:
        cards.append(kwargs)

    model._emit_tool_use_card = card
    return model, cards


def _base_event(**overrides: Any) -> dict[str, Any]:
    event = {
        "event_id": "mythic-task:1:45",
        "source": "mythic",
        "tool_name": "shell",
        "callback_id": 1,
        "task_id": 45,
        "parameters": "certutil -TCAInfo",
    }
    event.update(overrides)
    return event


def test_mythic_success_task_is_not_labeled_as_tool_failure():
    """The task-45 class: Mythic success + failure-classified output renders completed, not failed."""
    observed: list[dict[str, Any]] = []
    model, cards = _model_shell(observed)
    event = _base_event(
        status="error",
        task_completed=True,
        task_status="success",
        result_preview="CertUtil: -TCAInfo command FAILED: 0x80092004",
        output="No CA's listed in the domain.\nCertUtil: -TCAInfo command FAILED: 0x80092004",
    )
    asyncio.run(model._emit_execution_event(event))
    assert len(cards) == 1, cards
    card = cards[0]
    assert card["status"] == "completed", (
        f"the card label must match Mythic's task state, not the output classification: {card}"
    )
    assert card["complete"] is True
    # The classification nuance is preserved as a note, never dropped.
    preview = str(card.get("result_preview") or "")
    assert "completed successfully" in preview
    assert "read the command output as a failure" in preview
    assert "CertUtil: -TCAInfo command FAILED" in preview
    # The observation hook still saw the ORIGINAL event (evidence semantics unchanged).
    assert observed and observed[0]["status"] == "error"


def test_mythic_success_without_preview_still_carries_the_note():
    model, cards = _model_shell([])
    event = _base_event(status="error", task_completed=True, task_status="completed")
    asyncio.run(model._emit_execution_event(event))
    card = cards[0]
    assert card["status"] == "completed"
    preview = str(card.get("result_preview") or "")
    assert ("completed successfully" in preview
            and "read the command output as a failure" in preview)


def test_error_without_mythic_truth_keeps_the_error_label():
    """No Mythic task truth in the event (e.g., an observation exception) keeps the error label."""
    observed: list[dict[str, Any]] = []
    model, cards = _model_shell(observed)
    event = _base_event(
        status="error",
        task_completed=None,
        task_status="",
        result_preview="TimeoutError: subscription timed out",
    )
    asyncio.run(model._emit_execution_event(event))
    card = cards[0]
    assert card["status"] == "error", card
    assert card.get("result_preview") == "TimeoutError: subscription timed out"


def test_mythic_explicit_failure_keeps_the_error_label():
    """Mythic saying the task itself failed is exactly when the error label belongs."""
    model, cards = _model_shell([])
    event = _base_event(
        status="error",
        task_completed=True,
        task_status="error",
        result_preview="exit code 0x1",
    )
    asyncio.run(model._emit_execution_event(event))
    assert cards[0]["status"] == "error"


def test_mcp_events_are_not_affected():
    model, cards = _model_shell([])
    event = {
        "event_id": "mcp:ev:1", "source": "mcp", "tool_name": "cypher_query",
        "status": "error", "task_completed": True, "task_status": "success",
    }
    asyncio.run(model._emit_execution_event(event))
    assert cards[0]["status"] == "error"
