"""Production-path behavioral constitution and post-cutover regression matrix."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace

from tests.conversation_contract import (
    CASES,
    ConversationCase,
    run_case,
)


def _run(case: ConversationCase):
    return asyncio.run(run_case(case))


def test_behavioral_constitution_is_versioned_and_complete():
    assert len(CASES) >= 20
    assert len({case.case_id for case in CASES}) == len(CASES)
    assert {field.name for field in fields(ConversationCase)} >= {
        "case_id",
        "source_class",
        "prompt",
        "terminal_state",
        "required_events",
        "forbidden_events",
        "expected_control_plane",
    }
    for case in CASES:
        assert case.case_id.startswith("C")
        assert case.source_class
        assert case.prompt
        assert case.terminal_state in {
            "complete",
            "blocked",
            "stopped",
            "awaiting_approval",
        }
        assert case.required_events
        assert "request.terminal" in case.required_events
        assert case.forbidden_events


def test_each_case_enters_sage_chat_and_records_one_terminal():
    for case in CASES:
        result = _run(case)
        assert result.entered_sage_chat is True
        terminals = [
            emission
            for emission in result.emissions
            if emission.get("complete_request")
        ]
        assert len(terminals) == 1, (case.case_id, result.emissions)
        assert "operator.input" in result.events
        assert "request.metadata" in result.events
        assert result.events[-1] == "request.terminal"


def test_protocol_cases_capture_declared_lifecycle_and_final_state():
    for case in CASES:
        if case.driver != "protocol":
            continue
        result = _run(case)
        assert result.terminal_state == case.terminal_state
        assert result.first_divergence == "", (
            case.case_id,
            result.first_divergence,
            result.events,
            result.control_plane,
        )


def test_valid_authority_near_matches_satisfy_the_ideal_contract():
    for case in CASES:
        if case.driver != "authority":
            continue
        result = _run(case)
        assert result.terminal_state == case.terminal_state
        assert result.first_divergence == "", (
            case.case_id,
            result.first_divergence,
            result.events,
            result.control_plane,
        )


def test_red_before_regressions_converge_after_typed_cutover():
    observed = {
        case.case_id: _run(case).first_divergence
        for case in CASES
        if case.case_id in {
            "C05-positive-with-safety-suffix",
            "C06-two-actions-one-prohibited",
        }
    }
    assert observed == {
        "C05-positive-with-safety-suffix": "",
        "C06-two-actions-one-prohibited": "",
    }


def test_non_goad_entity_renaming_preserves_every_case_disposition():
    substitutions = {
        "citadel.test": "orion.example",
        "callback 7": "callback 42",
        "callback 8": "callback 93",
        "CASTELBLACK": "VEGA",
        "samwell.tarly": "avery.quinn",
    }

    def renamed_text(value: str) -> str:
        for old, new in substitutions.items():
            value = value.replace(old, new)
        return value

    for case in CASES:
        renamed = replace(
            case,
            prompt=renamed_text(case.prompt),
            stored_objective=renamed_text(case.stored_objective),
            pending_objective=renamed_text(case.pending_objective),
        )
        original_result = _run(case)
        renamed_result = _run(renamed)
        assert renamed_result.terminal_state == original_result.terminal_state
        assert renamed_result.first_divergence == original_result.first_divergence
        assert renamed_result.events == original_result.events
        assert renamed_result.control_plane == original_result.control_plane
