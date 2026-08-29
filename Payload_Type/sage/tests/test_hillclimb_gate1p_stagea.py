from __future__ import annotations

import copy
from dataclasses import asdict
import threading
import time
import json
import itertools
from pathlib import Path

import pytest

from ai.hillclimb import gate1p_stagea as g1p
from ai.hillclimb.fitness import ScoreCard


def _stagea_authority_evidence(rows, *, initial_callback_display_id=7, mythic_operation_id=41):
    bound_rows = []
    for row in rows:
        bound = dict(row)
        bound.setdefault("operation_id", mythic_operation_id)
        bound_rows.append(bound)
    return g1p.stagea_authority_evidence(
        bound_rows,
        initial_callback_display_id=initial_callback_display_id,
        mythic_operation_id=mythic_operation_id,
    )


def _valid_request(**overrides):
    values = {
        "protocol_id": g1p.PROTOCOL_ID,
        "pair_id": g1p.PAIR_ID,
        "scenario": g1p.SCENARIO,
        "side": "harness",
        "arm_index": 0,
        "provider": g1p.MODEL_PROVIDER,
        "model": g1p.MODEL_ID,
        "policy_mode": g1p.POLICY_MODE,
        "solve_timeout": g1p.WALL_SECONDS,
        "token_ceiling": g1p.TOKEN_ADMISSION_CEILING,
        "model_context_limit": 200_000,
        "max_output_tokens": 8_192,
        "source_version": "tree:test",
        "api_endpoint": "https://provider.invalid/v1",
    }
    values.update(overrides)
    return values


def test_validate_request_accepts_only_the_declared_arm_mapping():
    assert g1p.validate_request(**_valid_request()) == "shipping_sage"
    assert g1p.validate_request(
        **_valid_request(side="bare", arm_index=1)
    ) == "true_naive"
    with pytest.raises(g1p.ProtocolError, match="requires side"):
        g1p.validate_request(**_valid_request(side="bare", arm_index=0))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol_id", "other"),
        ("pair_id", "other"),
        ("scenario", "single-hop-system"),
        ("provider", "bedrock"),
        ("model", "other"),
        ("policy_mode", "symbolic"),
        ("arm_index", True),
        ("solve_timeout", 1799),
        ("solve_timeout", True),
        ("token_ceiling", 250_001),
        ("token_ceiling", True),
        ("source_version", ""),
        ("api_endpoint", ""),
        ("api_endpoint", "https://user:secret@provider.invalid/v1"),
        ("api_endpoint", "https://provider.invalid/v1?token=secret"),
        ("model_context_limit", 0),
        ("model_context_limit", True),
        ("max_output_tokens", 0),
        ("max_output_tokens", True),
    ],
)
def test_validate_request_rejects_every_mismatched_protocol_field(field, value):
    with pytest.raises(g1p.ProtocolError):
        g1p.validate_request(**_valid_request(**{field: value}))


def test_token_usage_is_complete_only_for_reconciling_input_output_total():
    complete = g1p.normalize_usage(
        {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
        response_id="r1",
    )
    missing = g1p.normalize_usage({"total_tokens": 12}, response_id="r2")
    wrong = g1p.normalize_usage(
        {"input_tokens": 7, "output_tokens": 5, "total_tokens": 13},
        response_id="r3",
    )
    assert complete["usage_complete"] is True
    assert missing["usage_complete"] is False
    assert wrong["usage_complete"] is False


@pytest.mark.parametrize(
    "raw",
    [
        {"input_tokens": "9", "output_tokens": "2", "total_tokens": "11"},
        {"input_tokens": 9.9, "output_tokens": 2.9, "total_tokens": 11.9},
        {"input_tokens": True, "output_tokens": 2, "total_tokens": 3},
    ],
)
def test_normalize_usage_rejects_coercible_non_integer_values(raw):
    usage = g1p.normalize_usage(raw, response_id="malformed")

    assert usage["usage_complete"] is False


def test_token_budget_distinguishes_exact_exhaustion_from_overshoot():
    budget = g1p.TokenBudget(ceiling=10)
    budget.add({"input_tokens": 4, "output_tokens": 6}, response_id="r1")
    assert budget.total_tokens == 10
    assert budget.exhausted is True
    assert budget.overshoot is False
    budget.add({"input_tokens": 1, "output_tokens": 0}, response_id="r2")
    assert budget.overshoot is True


def _budget_response(response_id, input_tokens, output_tokens):
    return {
        "response_id": response_id,
        "effective_backend": "openai:model",
        "backend_provenance_source": "response_metadata.model_name",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "usage_complete": True,
    }


def test_completed_call_budget_accepts_one_bounded_crossing_without_action():
    records = [_budget_response("r1", 4, 2), _budget_response("r2", 4, 1)]

    disposition = g1p.completed_call_budget_disposition(
        records,
        model_calls=2,
        action_response_ids=["r1"],
        token_ceiling=10,
        model_context_limit=100,
        max_output_tokens=10,
    )

    assert disposition == {
        "countable": True,
        "reason": "one bounded completed call reached the token boundary without a downstream action",
        "boundary_reached": True,
        "overshoot": True,
        "prior_total_tokens": 6,
        "total_tokens": 11,
        "terminal_response_id": "r2",
    }


@pytest.mark.parametrize(
    ("records", "action_ids", "reason"),
    [
        (
            [_budget_response("r1", 4, 2), _budget_response("r2", 4, 1)],
            ["r1", "r2"],
            "the boundary-crossing response caused a downstream action",
        ),
        (
            [_budget_response("r1", 9, 1), _budget_response("r2", 1, 0)],
            [],
            "a model call occurred after the token boundary",
        ),
    ],
)
def test_completed_call_budget_rejects_post_boundary_activity(records, action_ids, reason):
    disposition = g1p.completed_call_budget_disposition(
        records,
        model_calls=len(records),
        action_response_ids=action_ids,
        token_ceiling=10,
        model_context_limit=100,
        max_output_tokens=10,
    )

    assert disposition == {"countable": False, "reason": reason}


def test_completed_call_budget_accepts_exact_boundary_and_normal_below_boundary():
    exact = g1p.completed_call_budget_disposition(
        [_budget_response("r1", 6, 4)],
        model_calls=1,
        action_response_ids=[],
        token_ceiling=10,
        model_context_limit=100,
        max_output_tokens=10,
    )
    below = g1p.completed_call_budget_disposition(
        [_budget_response("r1", 4, 2)],
        model_calls=1,
        action_response_ids=[],
        token_ceiling=10,
        model_context_limit=100,
        max_output_tokens=10,
    )

    assert exact["countable"] is True
    assert exact["boundary_reached"] is True
    assert exact["overshoot"] is False
    assert below["countable"] is True
    assert below["boundary_reached"] is False


def test_runtime_summary_projects_shipping_transaction_decision_ids_as_action_lineage():
    disposition = g1p.runtime_summary_disposition({
        "controller_cycle_count": 2,
        "achieved_effects": ["effect:b", "effect:a"],
        "transactions": [
            {"decision_id": "response-1", "capability": "first"},
            {"decision_id": "response-2", "capability": "second"},
        ],
    })

    assert disposition["countable"] is True
    assert disposition["canonical_summary"] == {
        "schema": "gate1p-stagea-runtime-summary-v2",
        "controller_cycle_count": 2,
        "achieved_effects": ["effect:a", "effect:b"],
        "action_response_ids": ["response-1", "response-2"],
    }


def _facts_only_observer(*, arm="shipping_sage", objective=True, status="done", tasks=None,
                         task_window_observed=True, wrapper_recognized=None):
    events = [{
        "event_id": f"{g1p.PAIR_ID}:{arm}:coordinator:1",
        "source": "coordinator",
        "kind": "arm_started",
        "sequence": 1,
        "monotonic_ns": 1,
    }]
    if objective:
        events.append({
            "event_id": f"{g1p.PAIR_ID}:{arm}:oracle:2",
            "source": "oracle",
            "kind": "objective_positive",
            "sequence": 2,
            "monotonic_ns": 2,
        })
    sequence = len(events) + 1
    events.append({
        "event_id": f"{g1p.PAIR_ID}:{arm}:wrapper:{sequence}",
        "source": "wrapper",
        "kind": "terminal",
        "sequence": sequence,
        "monotonic_ns": sequence,
        "status": status,
    })
    canonical_tasks = []
    for task in tasks or []:
        callback = task.get("callback") or {}
        canonical_tasks.append({
            "operation_id": task.get("operation_id"),
            "display_id": task.get("display_id"),
            "completed": task.get("completed"),
            "callback": {
                "display_id": callback.get("display_id"),
                "host": callback.get("host"),
                "domain": callback.get("domain"),
                "user": callback.get("user"),
            },
        })
    return {
        "observer_version": "gate1p-stagea-observer-facts-v22",
        "oracle": {
            "success": objective,
            "proof_class": "directory_delta" if objective else None,
            "summary": {
                "domain": "north.sevenkingdoms.local",
                "baseline_member_count": 1,
                "final_member_count": 2 if objective else 1,
                "proof_states": {
                    "directory_delta": objective,
                    "certificate_control": False,
                    "ticket_control": False,
                },
            },
            "artifact_pointer": ".sage_history/example.json",
        },
        "task_window_observed": task_window_observed,
        "wrapper_objective_recognized": objective if wrapper_recognized is None else wrapper_recognized,
        "tasks": canonical_tasks,
        "events": events,
    }


def _typed_oracle_observation(success):
    return _facts_only_observer(objective=success)["oracle"]


def _mark_task_window_unavailable(observer, *, reason="producer_deadline"):
    observer["task_window_observed"] = False
    sequence = len(observer["events"]) + 1
    arm = "shipping_sage"
    observer["events"].append({
        "event_id": f"{g1p.PAIR_ID}:{arm}:observer:{sequence}",
        "source": "observer",
        "kind": "lane_unavailable",
        "sequence": sequence,
        "monotonic_ns": sequence,
        "lane": "task",
        "reason": reason,
    })


def _usage_records(total=15):
    return [{
        "response_id": "response-1",
        "effective_backend": "openai:effective",
        "backend_provenance_source": "response_metadata.model_name",
        "input_tokens": total - 5,
        "output_tokens": 5,
        "total_tokens": total,
        "usage_complete": True,
    }]


def test_facts_only_outcome_derives_budget_and_terminal_without_duplicate_claims():
    usage = g1p.response_usage_disposition(_usage_records(), model_calls=1)
    assert usage["countable"] is True
    outcome = g1p.episode_outcome_disposition(
        _facts_only_observer(),
        arm="shipping_sage",
        total_tokens=usage["total_tokens"],
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )
    assert outcome == {
        "countable": True,
        "reason": "raw observer facts derive objective and stop outcomes",
        "objective_success": True,
        "stop_countable": True,
        "task_quiescent": True,
        "token_budget_exhausted": False,
        "token_budget_overshoot": False,
        "post_objective_task_ids": [],
        "clean_stop": True,
        "objective_recognized": True,
        "objective_clean_stop": True,
    }


def test_facts_only_outcome_rejects_all_predecessor_duplicate_semantic_keys():
    forbidden = (
        "objective_success", "task_quiescent", "clean_stop", "objective_recognized",
        "objective_clean_stop", "wrapper_status", "wrapper_normal_terminal",
        "token_budget_exhausted", "token_budget_overshoot",
        "post_objective_task_ids",
    )
    for field in forbidden:
        observer = _facts_only_observer()
        observer[field] = False
        assert g1p.episode_outcome_disposition(
            observer,
            arm="shipping_sage",
            total_tokens=15,
            token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
        )["countable"] is False


def test_facts_only_outcome_derives_exact_ceiling_and_wrapper_timeout_fail_closed():
    exact = g1p.episode_outcome_disposition(
        _facts_only_observer(),
        arm="shipping_sage",
        total_tokens=g1p.TOKEN_ADMISSION_CEILING,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )
    assert exact["countable"] is True
    assert exact["token_budget_exhausted"] is True
    assert exact["token_budget_overshoot"] is False
    assert exact["clean_stop"] is False

    timeout = g1p.episode_outcome_disposition(
        _facts_only_observer(status="timeout"),
        arm="shipping_sage",
        total_tokens=15,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )
    assert timeout["countable"] is True
    assert timeout["clean_stop"] is False


def test_facts_only_response_usage_rejects_boolean_counts_and_malformed_usage():
    assert g1p.response_usage_disposition(_usage_records(), model_calls=True)["countable"] is False
    malformed = _usage_records()
    malformed[0]["input_tokens"] = True
    assert g1p.response_usage_disposition(malformed, model_calls=1)["countable"] is False


def test_canonical_response_projection_accepts_both_producers_and_removes_aliases():
    shipping = [{
        "decision_id": "response-1",
        "effective_backend": "openai:effective",
        "backend_provenance_source": "response_metadata.model_name",
        "token_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }]
    bare = [{
        "decision_id": "response-1",
        "response_id": "response-1",
        "effective_backend": "openai:effective",
        "backend_provenance_source": "response_metadata.model_name",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "usage_complete": True,
    }]
    expected = [{
        "response_id": "response-1",
        "effective_backend": "openai:effective",
        "backend_provenance_source": "response_metadata.model_name",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "usage_complete": True,
    }]
    assert g1p.response_usage_disposition(shipping, model_calls=1)["canonical_records"] == expected
    assert g1p.response_usage_disposition(bare, model_calls=1)["canonical_records"] == expected


def test_canonical_response_projection_rejects_conflicting_identity_aliases():
    records = _usage_records()
    records[0]["decision_id"] = "conflicting-response"
    assert g1p.response_usage_disposition(records, model_calls=1)["countable"] is False


@pytest.mark.parametrize(
    "container",
    ["event", "oracle", "oracle_summary", "proof_states", "task", "callback"],
)
def test_canonical_observer_tree_rejects_nested_semantic_extra_fields(container):
    observer = _facts_only_observer(
        tasks=[{"display_id": 1, "completed": True}],
    )
    if container == "event":
        observer["events"][-1]["clean_stop"] = False
    elif container == "oracle":
        observer["oracle"]["clean_stop"] = False
    elif container == "oracle_summary":
        observer["oracle"]["summary"]["clean_stop"] = False
    elif container == "proof_states":
        observer["oracle"]["summary"]["proof_states"]["clean_stop"] = False
    elif container == "task":
        observer["tasks"][0]["clean_stop"] = False
    else:
        observer["tasks"][0]["callback"]["clean_stop"] = False
    assert g1p.episode_outcome_disposition(
        observer,
        arm="shipping_sage",
        total_tokens=15,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )["countable"] is False


@pytest.mark.parametrize(
    ("observer", "expected"),
    [
        (_facts_only_observer(objective=False), (False, True, False, False)),
        (_facts_only_observer(objective=True), (True, True, True, True)),
        (
            _facts_only_observer(
                objective=True,
                tasks=[{"display_id": 1, "completed": False}],
            ),
            (True, False, True, False),
        ),
    ],
)
def test_facts_only_outcome_accepts_valid_success_failure_and_nonquiescent_outcomes(observer, expected):
    outcome = g1p.episode_outcome_disposition(
        observer,
        arm="shipping_sage",
        total_tokens=15,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )
    assert outcome["countable"] is True
    assert (
        outcome["objective_success"],
        outcome["clean_stop"],
        outcome["objective_recognized"],
        outcome["objective_clean_stop"],
    ) == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observer_version", None),
        ("task_window_observed", None),
        ("wrapper_objective_recognized", 1),
    ],
)
def test_facts_only_outcome_rejects_missing_or_nonboolean_raw_fields(field, value):
    observer = _facts_only_observer()
    observer[field] = value
    assert g1p.episode_outcome_disposition(
        observer,
        arm="shipping_sage",
        total_tokens=15,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )["countable"] is False


def _derived_observer_outcome(result, *, arm="shipping_sage", total_tokens=15):
    return g1p.episode_outcome_disposition(
        result,
        arm=arm,
        total_tokens=total_tokens,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )


def test_observer_clean_give_up_is_clean_but_not_objective_clean():
    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(False),
        task_fetcher=lambda: [],
        objective_settle_seconds=0.02,
        objective_poll_seconds=0.005,
        task_quiescence_seconds=0.02,
        task_poll_seconds=0.005,
    )
    observer.start()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=False,
    )
    assert g1p.observer_facts_disposition(result, arm="shipping_sage")["countable"] is True
    outcome = _derived_observer_outcome(result)
    assert outcome["objective_success"] is False
    assert outcome["clean_stop"] is True
    assert outcome["objective_clean_stop"] is False


def test_observer_rejects_incomplete_task_at_fixed_quiescence_deadline():
    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="true_naive",
        objective_probe=lambda: _typed_oracle_observation(False),
        task_fetcher=lambda: [{"display_id": 1, "completed": False, "status": "processing"}],
        objective_settle_seconds=0.02,
        objective_poll_seconds=0.005,
        task_quiescence_seconds=0.01,
        task_poll_seconds=0.002,
    )
    observer.start()
    result = observer.finish(
        wrapper_status="done",
        wrapper_objective_recognized=False,
    )
    assert g1p.observer_facts_disposition(result, arm="true_naive")["countable"] is True
    outcome = _derived_observer_outcome(result, arm="true_naive")
    assert outcome["task_quiescent"] is False
    assert outcome["clean_stop"] is False


def test_observer_stays_active_for_complete_task_quiescence_window():
    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(True),
        task_fetcher=lambda: [],
        objective_settle_seconds=0.01,
        objective_poll_seconds=0.002,
        task_quiescence_seconds=0.03,
        task_poll_seconds=0.002,
    )
    observer.start()
    started = time.monotonic()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=True,
    )
    assert time.monotonic() - started >= 0.025
    assert _derived_observer_outcome(result)["clean_stop"] is True


def test_observer_freezes_task_quiescence_at_deadline_while_oracle_keeps_polling():
    origin = time.monotonic()

    def task_fetcher():
        return [{
            "display_id": 4,
            "completed": time.monotonic() - origin >= 0.04,
            "status": "processing",
        }]

    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(False),
        task_fetcher=task_fetcher,
        objective_settle_seconds=0.06,
        objective_poll_seconds=0.002,
        task_quiescence_seconds=0.02,
        task_poll_seconds=0.002,
    )
    observer.start()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=False,
    )

    outcome = _derived_observer_outcome(result)
    assert outcome["objective_success"] is False
    assert outcome["task_quiescent"] is False
    assert result["tasks"][0]["display_id"] == 4
    assert result["tasks"][0]["completed"] is False
    assert set(result["tasks"][0]) == {"operation_id", "display_id", "completed", "callback"}


def test_observer_discards_task_poll_that_returns_after_exact_deadline():
    origin = time.monotonic()

    def delayed_fetcher():
        time.sleep(0.02)
        return [{
            "display_id": 8,
            "completed": time.monotonic() - origin >= 0.015,
            "status": "completed",
        }]

    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(False),
        task_fetcher=delayed_fetcher,
        objective_settle_seconds=0.2,
        objective_poll_seconds=0.002,
        task_quiescence_seconds=0.01,
        task_poll_seconds=0.02,
    )
    observer.start()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=False,
    )

    assert result["task_window_observed"] is False
    outcome = _derived_observer_outcome(result)
    assert outcome["countable"] is True, outcome
    assert outcome["stop_countable"] is False
    assert outcome["clean_stop"] is None


def test_observer_post_objective_tasks_defeat_checkpoint_recognition():
    objective_seen = threading.Event()

    def objective_probe():
        objective_seen.set()
        return _typed_oracle_observation(True)

    def task_fetcher():
        if objective_seen.is_set():
            return [{"display_id": 9, "completed": True, "status": "completed"}]
        return []

    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="true_naive",
        objective_probe=objective_probe,
        task_fetcher=task_fetcher,
        objective_settle_seconds=0.01,
        objective_poll_seconds=0.002,
        task_quiescence_seconds=0.03,
        task_poll_seconds=0.002,
    )
    observer.start()
    assert objective_seen.wait(timeout=0.1)
    result = observer.finish(
        wrapper_status="done",
        wrapper_objective_recognized=True,
    )
    outcome = _derived_observer_outcome(result, arm="true_naive")
    assert outcome["objective_success"] is True
    assert outcome["post_objective_task_ids"] == [9]
    assert outcome["objective_recognized"] is False
    assert outcome["objective_clean_stop"] is False


def test_observer_does_not_infer_recognition_from_oracle_success_and_clean_terminal():
    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="true_naive",
        objective_probe=lambda: _typed_oracle_observation(True),
        task_fetcher=lambda: [],
        objective_settle_seconds=0.01,
        objective_poll_seconds=0.002,
        task_quiescence_seconds=0.01,
        task_poll_seconds=0.002,
    )
    observer.start()
    result = observer.finish(
        wrapper_status="done",
        wrapper_objective_recognized=False,
    )
    outcome = _derived_observer_outcome(result, arm="true_naive")
    assert outcome["objective_success"] is True
    assert outcome["clean_stop"] is True
    assert outcome["objective_recognized"] is False
    assert outcome["objective_clean_stop"] is False


def test_observer_fails_closed_when_wrapper_recognition_is_unavailable():
    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(True),
        task_fetcher=lambda: [],
        objective_settle_seconds=0.01,
        objective_poll_seconds=0.002,
        task_quiescence_seconds=0.01,
        task_poll_seconds=0.002,
    )
    observer.start()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=None,
    )
    assert g1p.observer_facts_disposition(result, arm="shipping_sage")["countable"] is True
    outcome = _derived_observer_outcome(result)
    assert outcome["countable"] is True
    assert outcome["stop_countable"] is True
    assert outcome["objective_success"] is True
    assert outcome["clean_stop"] is True
    assert outcome["objective_recognized"] is None
    assert outcome["objective_clean_stop"] is None
    assert "objective_success" not in result
    assert "objective_recognized" not in result
    assert "objective_clean_stop" not in result
    assert not any(event.get("source") == "observer" for event in result["events"])


def test_observer_error_is_unavailable_not_objective_false():
    def broken_tasks():
        raise RuntimeError("query failed with credential=must-not-leak")

    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(False),
        task_fetcher=broken_tasks,
        objective_settle_seconds=0.01,
        objective_poll_seconds=0.002,
        task_quiescence_seconds=0.01,
        task_poll_seconds=0.002,
    )
    observer.start()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=False,
    )
    assert g1p.observer_facts_disposition(result, arm="shipping_sage")["countable"] is True
    outcome = _derived_observer_outcome(result)
    assert outcome["countable"] is True
    assert outcome["objective_success"] is False
    assert outcome["stop_countable"] is False
    assert outcome["clean_stop"] is None
    assert "objective_success" not in result
    assert "clean_stop" not in result
    unavailable = [
        event for event in result["events"]
        if event.get("source") == "observer" and event.get("kind") == "lane_unavailable"
    ]
    assert len(unavailable) == 1
    assert set(unavailable[0]) == {
        "event_id", "source", "kind", "sequence", "monotonic_ns", "lane", "reason",
    }
    assert unavailable[0]["lane"] == "task"
    assert unavailable[0]["reason"] == "producer_error"
    assert "must-not-leak" not in json.dumps(result)


def test_observer_rejects_unavailable_task_window_without_one_typed_reason():
    observer = _facts_only_observer()
    observer["task_window_observed"] = False

    disposition = g1p.observer_facts_disposition(observer, arm="shipping_sage")

    assert disposition == {
        "countable": False,
        "reason": "unavailable task window lacks one typed reason",
    }


def test_blocked_task_producer_fails_locally_without_owning_observer_deadline():
    release = threading.Event()

    def blocked_tasks():
        release.wait()
        return []

    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(False),
        task_fetcher=blocked_tasks,
        objective_settle_seconds=0.02,
        objective_poll_seconds=0.005,
        task_quiescence_seconds=0.02,
        task_poll_seconds=0.005,
    )
    observer.start()
    started = time.monotonic()
    try:
        result = observer.finish(
            wrapper_status="completed",
            wrapper_objective_recognized=False,
        )
    finally:
        release.set()

    assert time.monotonic() - started < 0.25
    disposition = g1p.episode_outcome_disposition(
        result,
        arm="shipping_sage",
        total_tokens=15,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )
    assert disposition["countable"] is True
    assert disposition["objective_success"] is False
    assert disposition["stop_countable"] is False
    assert disposition["clean_stop"] is None
    assert result["task_window_observed"] is False
    assert result["tasks"] == []
    frozen_events = list(result["events"])
    time.sleep(0.02)
    assert result["tasks"] == []
    assert result["events"] == frozen_events


def test_sleeping_task_poller_is_not_misclassified_as_a_blocked_producer():
    observed = threading.Event()

    def healthy_tasks():
        observed.set()
        return []

    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(True),
        task_fetcher=healthy_tasks,
        objective_settle_seconds=0.05,
        objective_poll_seconds=0.01,
        task_quiescence_seconds=0.05,
        task_poll_seconds=0.20,
    )
    observer.start()
    assert observed.wait(timeout=0.1)
    time.sleep(0.01)
    started = time.monotonic()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=True,
    )

    assert time.monotonic() - started < 0.2
    assert result["task_window_observed"] is True
    assert result["tasks"] == []


def test_blocked_objective_producer_is_unavailable_not_objective_false():
    release = threading.Event()

    def blocked_objective():
        release.wait()
        return _typed_oracle_observation(False)

    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=blocked_objective,
        task_fetcher=lambda: [],
        objective_settle_seconds=0.02,
        objective_poll_seconds=0.005,
        task_quiescence_seconds=0.02,
        task_poll_seconds=0.005,
    )
    observer.start()
    started = time.monotonic()
    try:
        result = observer.finish(
            wrapper_status="completed",
            wrapper_objective_recognized=False,
        )
    finally:
        release.set()

    assert time.monotonic() - started < 0.25
    disposition = _derived_observer_outcome(result)
    assert disposition["countable"] is False
    assert "objective" in disposition["reason"]
    assert "objective_success" not in disposition
    assert result["task_window_observed"] is True


def test_sleeping_negative_objective_poller_retains_its_on_time_observation():
    observed = threading.Event()

    def healthy_negative_objective():
        observed.set()
        return _typed_oracle_observation(False)

    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=healthy_negative_objective,
        task_fetcher=lambda: [],
        objective_settle_seconds=0.05,
        objective_poll_seconds=0.20,
        task_quiescence_seconds=0.01,
        task_poll_seconds=0.002,
    )
    observer.start()
    assert observed.wait(timeout=0.1)
    time.sleep(0.01)
    started = time.monotonic()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=False,
    )

    assert time.monotonic() - started < 0.2
    assert result["oracle"]["success"] is False
    assert not any(event.get("source") == "observer" for event in result["events"])
    assert _derived_observer_outcome(result)["objective_success"] is False


def test_preterminal_objective_probe_returning_just_after_deadline_is_unavailable():
    entered = threading.Event()
    release = threading.Event()

    def just_late_objective():
        entered.set()
        release.wait()
        return _typed_oracle_observation(False)

    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=just_late_objective,
        task_fetcher=lambda: [],
        objective_settle_seconds=0.05,
        objective_poll_seconds=0.005,
        task_quiescence_seconds=0.01,
        task_poll_seconds=0.002,
    )
    observer.start()
    assert entered.wait(timeout=0.1)
    timer = threading.Timer(0.065, release.set)
    timer.start()
    try:
        result = observer.finish(
            wrapper_status="completed",
            wrapper_objective_recognized=False,
        )
    finally:
        release.set()
        timer.join(timeout=0.2)

    disposition = _derived_observer_outcome(result)
    assert disposition["countable"] is False
    assert "objective" in disposition["reason"]
    assert "objective_success" not in disposition
    assert any(event.get("source") == "observer" for event in result["events"])


def test_known_authority_violation_remains_a_global_outcome_blocker():
    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(False),
        task_fetcher=lambda: [{"display_id": 7, "completed": True}],
        authority_checker=lambda _rows: {"available": True, "valid": False},
        objective_settle_seconds=0.02,
        objective_poll_seconds=0.005,
        task_quiescence_seconds=0.02,
        task_poll_seconds=0.005,
    )
    observer.start()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=False,
    )

    disposition = _derived_observer_outcome(result)
    assert disposition["countable"] is False
    assert any(
        event.get("source") == "observer"
        and "authority observer reported a violation" in event.get("error", "")
        for event in result["events"]
    )


def test_observer_rejects_boolean_task_identity_without_an_authority_checker():
    observer = g1p.LiveEpisodeObserver(
        pair_id=g1p.PAIR_ID,
        arm="shipping_sage",
        objective_probe=lambda: _typed_oracle_observation(False),
        task_fetcher=lambda: [{"display_id": True, "completed": True, "status": "completed"}],
        objective_settle_seconds=0.01,
        objective_poll_seconds=0.002,
        task_quiescence_seconds=0.01,
        task_poll_seconds=0.002,
    )
    observer.start()
    result = observer.finish(
        wrapper_status="completed",
        wrapper_objective_recognized=False,
    )

    assert g1p.observer_facts_disposition(result, arm="shipping_sage")["countable"] is True
    outcome = _derived_observer_outcome(result)
    assert outcome["countable"] is True
    assert outcome["stop_countable"] is False
    assert outcome["clean_stop"] is None


def _scorecard(*, total_tokens):
    return asdict(ScoreCard(
        scenario=g1p.SCENARIO,
        verifier_hash="test",
        furthest_milestone="OBJECTIVE",
        capability=1.0,
        milestones_reached=1,
        milestones={"OBJECTIVE": True},
        substring_score=1.0,
        metric_capability_gap=0.0,
        tool_calls=0,
        model_calls=1,
        productive_action_ratio=0.0,
        turn_efficiency=1.0,
        status="done",
        recursion_deaths=0,
        error_count=0,
        total_tokens=total_tokens,
        wall_seconds=1.0,
        objective_clean_stop=True,
        request_completed=True,
        objective_recognized=True,
        objective_proven=True,
        clean_stop=True,
    ))


def _row(
    arm,
    history_root,
    *,
    outcome=True,
    overshoot=False,
    backend="openai:bedrock-claude-4-6-sonnet",
):
    side = "harness" if arm == "shipping_sage" else "bare"
    arm_index = g1p.ARMS.index(arm)
    protocol = {
        "protocol_id": g1p.PROTOCOL_ID,
        "pair_id": g1p.PAIR_ID,
        "arm": arm,
        "arm_index": arm_index,
        "wrapper": "shipping_native_sage" if arm == "shipping_sage" else "true_naive_raw_tools",
        "pair_invariants": g1p.pair_invariants(
            provider=g1p.MODEL_PROVIDER,
            model=g1p.MODEL_ID,
            api_endpoint="https://provider.invalid/v1",
            source_version="tree:test",
            model_context_limit=200_000,
            max_output_tokens=8_192,
        ),
        "initial_callback_display_id": 7,
        "mythic_operation_id": 1,
        "reset_readiness": {
            "ready": True,
            "snapshot": g1p.RESET_SNAPSHOT,
            "bloodhound_domain_count": 0,
            "clock_gate": "passed",
            "runtime_databases_archived": True,
            "mythic_reset": "passed",
            "unique_initial_callback": True,
            "pre_arm_task_count": 0,
        },
    }
    artifact_dir = history_root / "evaluation" / "gate1p"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    episode = artifact_dir / f"{arm}-episode.json"
    results = artifact_dir / "results.jsonl"
    event_origin = 100 if arm == "shipping_sage" else 400
    events = [
        {
            "event_id": f"{g1p.PAIR_ID}:{arm}:coordinator:1",
            "source": "coordinator",
            "kind": "arm_started",
            "sequence": 1,
            "monotonic_ns": event_origin,
        },
        {
            "event_id": f"{g1p.PAIR_ID}:{arm}:oracle:2",
            "source": "oracle",
            "kind": "objective_positive",
            "sequence": 2,
            "monotonic_ns": event_origin + 1,
        },
        {
            "event_id": f"{g1p.PAIR_ID}:{arm}:wrapper:3",
            "source": "wrapper",
            "kind": "terminal",
            "sequence": 3,
            "monotonic_ns": event_origin + 2,
            "status": "done",
        },
    ]
    total_tokens = g1p.TOKEN_ADMISSION_CEILING + 1 if overshoot else 15
    model_calls = 2 if overshoot else 1
    response_records = ([
        {
            "response_id": "response-1",
            "effective_backend": backend,
            "backend_provenance_source": "response_metadata.model_name",
            "input_tokens": 189_995,
            "output_tokens": 5,
            "total_tokens": 190_000,
            "usage_complete": True,
        },
        {
            "response_id": "response-2",
            "effective_backend": backend,
            "backend_provenance_source": "response_metadata.model_name",
            "input_tokens": 59_996,
            "output_tokens": 5,
            "total_tokens": 60_001,
            "usage_complete": True,
        },
    ] if overshoot else [{
        "response_id": "response-1",
        "effective_backend": backend,
        "backend_provenance_source": "response_metadata.model_name",
        "input_tokens": total_tokens - 5,
        "output_tokens": 5,
        "total_tokens": total_tokens,
        "usage_complete": True,
    }])
    action_response_ids = ["response-1"] if overshoot else []
    wrapper_status = "token_budget_overshoot" if overshoot else "done"
    events[-1]["status"] = wrapper_status
    row = {
        "record_type": "result",
        "scenario": g1p.SCENARIO,
        "side": side,
        "measurement_kind": "row",
        "protocol": protocol,
        "countability": {
            "outcome": {
                "countable": outcome,
                "objective_success": True,
                "stop_countable": True,
                "clean_stop": not overshoot,
                "objective_recognized": not overshoot,
                "objective_clean_stop": not overshoot,
            },
            "cost": {
                "model_calls": {"countable": True, "value": model_calls},
                "total_tokens": {"countable": True, "value": total_tokens},
            },
        },
        "cost_countable": {
            "model_calls": True,
            "total_tokens": True,
            "environment_actions": True,
            "wrapper_tool_invocations": True,
            "control_plane_reads": False,
            "wall_seconds": True,
        },
        "effective_backends": [backend],
        "model_calls": model_calls,
        "model_response_records": response_records,
        "token_usage_complete": True,
        "request_completed": not overshoot,
        "objective_recognized": not overshoot,
        "objective_proven": True,
        "clean_stop": not overshoot,
        "objective_clean_stop": not overshoot,
        "observer": {
            "observer_version": "gate1p-stagea-observer-facts-v22",
            "task_window_observed": True,
            "wrapper_objective_recognized": True,
            "tasks": [],
            "events": events,
            "oracle": {
                "success": True,
                "proof_class": "directory_delta",
                "summary": {
                    "domain": "north.sevenkingdoms.local",
                    "baseline_member_count": 1,
                    "final_member_count": 2,
                    "proof_states": {
                        "directory_delta": True,
                        "certificate_control": False,
                        "ticket_control": False,
                    },
                },
                "artifact_pointer": str(episode),
            },
        },
        "runtime_summary": {
            "schema": "gate1p-stagea-runtime-summary-v2",
            "controller_cycle_count": 0,
            "achieved_effects": [],
            "action_response_ids": action_response_ids,
        },
        "artifact_capture": {
            "complete": True,
            "episode_artifact": str(episode),
            "result_ledger": str(results),
            "transcript_entries": 1,
        },
        "ts": 2.0 if arm == "shipping_sage" else 5.0,
        "card": _scorecard(total_tokens=total_tokens),
    }
    row["card"].update({
        "model_calls": model_calls,
        "status": wrapper_status,
        "request_completed": not overshoot,
        "objective_recognized": not overshoot,
        "clean_stop": not overshoot,
        "objective_clean_stop": not overshoot,
    })
    transcript_events = ([{"response_id": "response-1", "kind": "tool_dispatch"}]
                         if side == "bare" and overshoot else [])
    row["artifact_capture"]["transcript_entries"] = len(transcript_events) + 1
    episode.write_text(json.dumps({
        "schema": "gate1p-stagea-episode-artifact-v17",
        "protocol": protocol,
        "side": side,
        "arm": arm,
        "evaluator_private": {},
        "oracle": row["observer"]["oracle"],
        "observer": row["observer"],
        "transcript": {"events": transcript_events, "final": "done"},
        "runtime_summary": {
            "schema": "gate1p-stagea-runtime-summary-v2",
            "controller_cycle_count": 0,
            "achieved_effects": [],
            "action_response_ids": action_response_ids,
        },
        "response_evidence": row["model_response_records"],
        "scorecard": row["card"],
        "reset_readiness": protocol["reset_readiness"],
        "bare_model": ({
            "schema": "gate1p-stagea-bare-model-contract-v1",
            "system_template": "true-naive-raw-tools-v1",
            "tool_inventory": ["task"],
        } if side == "bare" else None),
    }), encoding="utf-8")
    with results.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "record_type": "pre_run_protocol_snapshot",
            "measurement_kind": "row",
            "protocol": protocol,
            "ts": 0.5 if side == "harness" else 3.25,
        }) + "\n")
        if side == "bare":
            handle.write(json.dumps({
                "record_type": "pre_model_bare_contract",
                "measurement_kind": "row",
                "protocol": protocol,
                "bare_model": {
                    "schema": "gate1p-stagea-bare-model-contract-v1",
                    "system_template": "true-naive-raw-tools-v1",
                    "tool_inventory": ["task"],
                },
                "ts": 3.5,
            }) + "\n")
        handle.write(json.dumps({
            "record_type": "arm_started",
            "measurement_kind": "row",
            "protocol": protocol,
            "event": events[0],
            "ts": 1.0 if side == "harness" else 4.0,
        }) + "\n")
        handle.write(json.dumps(row) + "\n")
    return row


def _cleanup(result, *, complete=True):
    arm = result["protocol"]["arm"]
    return {
        "record_type": "cleanup",
        "measurement_kind": "row",
        "protocol": {
            "protocol_id": g1p.PROTOCOL_ID,
            "pair_id": g1p.PAIR_ID,
            "arm": arm,
        },
        "result_retained": True,
        "episode_artifact": result["artifact_capture"]["episode_artifact"],
        "episode_artifact_retained": True,
        "temporary_evaluator_material": {
            "created": False,
            "removal_required": False,
            "disposition": "not_applicable",
        },
        "errors": [],
        "steps": [{"name": name, "ok": True} for name in g1p._CLEANUP_STEP_NAMES],
        "complete": complete,
        "ts": 3.0 if arm == "shipping_sage" else 6.0,
    }


def _rewrite_retained_row(row):
    episode_path = row["artifact_capture"]["episode_artifact"]
    artifact = json.loads(open(episode_path, encoding="utf-8").read())
    artifact["observer"] = row["observer"]
    artifact["oracle"] = row["observer"]["oracle"]
    artifact["response_evidence"] = row["model_response_records"]
    artifact["runtime_summary"] = row["runtime_summary"]
    artifact["scorecard"] = row["card"]
    with open(episode_path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle)
    ledger_path = row["artifact_capture"]["result_ledger"]
    retained = [
        json.loads(line)
        for line in open(ledger_path, encoding="utf-8").read().splitlines()
        if line.strip()
    ]
    replaced = False
    for index, record in enumerate(retained):
        if (
            record.get("record_type") == "result"
            and (record.get("protocol") or {}).get("pair_id") == g1p.PAIR_ID
            and (record.get("protocol") or {}).get("arm") == row["protocol"]["arm"]
        ):
            retained[index] = row
            replaced = True
    if not replaced:
        retained.append(row)
    with open(ledger_path, "w", encoding="utf-8") as handle:
        for record in retained:
            handle.write(json.dumps(record) + "\n")


@pytest.mark.parametrize("mutation", ["equal_alias", "conflicting_alias", "extra_key"])
def test_arm_disposition_requires_exact_canonical_response_records(tmp_path, mutation):
    history = tmp_path / ".sage_history"
    row = _row("shipping_sage", history)
    response = row["model_response_records"][0]
    if mutation == "equal_alias":
        response["decision_id"] = response["response_id"]
    elif mutation == "conflicting_alias":
        response["decision_id"] = "other-response"
    else:
        response["clean_stop"] = False
    _rewrite_retained_row(row)

    disposition = g1p.arm_comparison_disposition(row, history_root=history)

    assert disposition["countable"] is False
    assert "response" in disposition["reason"]


@pytest.mark.parametrize(
    "path",
    ["event", "oracle", "oracle_summary", "proof_states"],
)
def test_arm_disposition_rejects_fully_retained_nested_observer_extras(tmp_path, path):
    history = tmp_path / ".sage_history"
    row = _row("shipping_sage", history)
    if path == "event":
        row["observer"]["events"][-1]["clean_stop"] = False
    elif path == "oracle":
        row["observer"]["oracle"]["clean_stop"] = False
    elif path == "oracle_summary":
        row["observer"]["oracle"]["summary"]["clean_stop"] = False
    else:
        row["observer"]["oracle"]["summary"]["proof_states"]["clean_stop"] = False
    _rewrite_retained_row(row)

    disposition = g1p.arm_comparison_disposition(row, history_root=history)

    assert disposition["countable"] is False
    assert "raw observer" in disposition["reason"]


def _pair(shipping, naive):
    rows = [
        {"record_type": "pre_run_protocol_snapshot", "measurement_kind": "row", "protocol": shipping["protocol"], "ts": 0.5},
        {"record_type": "arm_started", "measurement_kind": "row", "protocol": shipping["protocol"], "event": shipping["observer"]["events"][0], "ts": 1.0},
        shipping,
        _cleanup(shipping),
        {"record_type": "pre_run_protocol_snapshot", "measurement_kind": "row", "protocol": naive["protocol"], "ts": 3.25},
        {
            "record_type": "pre_model_bare_contract",
            "measurement_kind": "row",
            "protocol": naive["protocol"],
            "bare_model": {
                "schema": "gate1p-stagea-bare-model-contract-v1",
                "system_template": "true-naive-raw-tools-v1",
                "tool_inventory": ["task"],
            },
            "ts": 3.5,
        },
        {"record_type": "arm_started", "measurement_kind": "row", "protocol": naive["protocol"], "event": naive["observer"]["events"][0], "ts": 4.0},
        naive,
        _cleanup(naive),
    ]
    ledger = shipping["artifact_capture"]["result_ledger"]
    with open(ledger, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return rows


def _write_pair_ledger(records):
    result = next(record for record in records if record.get("record_type") == "result")
    ledger = result["artifact_capture"]["result_ledger"]
    with open(ledger, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_pair_disposition_requires_exactly_one_outcome_countable_row_per_arm(tmp_path):
    history = tmp_path / ".sage_history"
    accepted = g1p.pair_disposition(_pair(_row("shipping_sage", history), _row("true_naive", history)), history_root=history)
    assert accepted["countable"] is True
    assert accepted["comparison_inputs"] == [
        {
            "arm": "shipping_sage",
            "side": "harness",
            "card": _scorecard(total_tokens=15),
            "cost_countable": {
                "model_calls": True,
                "total_tokens": True,
                "environment_actions": True,
                "wrapper_tool_invocations": True,
                "control_plane_reads": False,
                "wall_seconds": True,
            },
        },
        {
            "arm": "true_naive",
            "side": "bare",
            "card": _scorecard(total_tokens=15),
            "cost_countable": {
                "model_calls": True,
                "total_tokens": True,
                "environment_actions": True,
                "wrapper_tool_invocations": True,
                "control_plane_reads": False,
                "wall_seconds": True,
            },
        },
    ]
    assert g1p.pair_disposition([_row("shipping_sage", history)], history_root=history)["countable"] is False
    assert g1p.pair_disposition(_pair(
        _row("shipping_sage", history),
        _row("true_naive", history, outcome=False),
    ), history_root=history)["countable"] is False


@pytest.mark.parametrize("field_name", sorted(g1p._SCORECARD_FIELDS))
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_pair014_rejects_every_nonexact_scorecard_constructor_shape(
    tmp_path,
    field_name,
    mutation,
):
    history = tmp_path / ".sage_history" / mutation / field_name
    shipping = _row("shipping_sage", history)
    if mutation == "missing":
        shipping["card"].pop(field_name)
    else:
        shipping["card"][f"unexpected_{field_name}"] = None
    records = _pair(shipping, _row("true_naive", history))

    disposition = g1p.pair_disposition(records, history_root=history)

    assert disposition["countable"] is False
    assert "scorecard constructor schema" in disposition["reason"]


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("objective_proven", 1),
        ("clean_stop", "true"),
        ("tool_calls", True),
        ("model_calls", 1.0),
        ("total_tokens", -1),
        ("wall_seconds", True),
        ("wall_seconds", -0.1),
        ("wall_seconds", float("inf")),
        ("wall_seconds", float("nan")),
    ],
)
def test_pair014_rejects_unsafe_scorecard_comparison_types(tmp_path, field_name, invalid_value):
    history = tmp_path / ".sage_history" / field_name / str(invalid_value)
    shipping = _row("shipping_sage", history)
    shipping["card"][field_name] = invalid_value
    records = _pair(shipping, _row("true_naive", history))

    disposition = g1p.pair_disposition(records, history_root=history)

    assert disposition["countable"] is False
    assert "scorecard" in disposition["reason"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_scenario",
        "wrong_scenario",
        "wrong_side",
        "missing_cost_metadata",
        "extra_cost_metadata",
        "non_boolean_cost_metadata",
    ],
)
def test_pair014_rejects_scenario_side_and_cost_metadata_drift(tmp_path, mutation):
    history = tmp_path / ".sage_history" / mutation
    shipping = _row("shipping_sage", history)
    if mutation == "missing_scenario":
        shipping.pop("scenario")
    elif mutation == "wrong_scenario":
        shipping["scenario"] = "other-scenario"
    elif mutation == "wrong_side":
        shipping["side"] = "bare"
    elif mutation == "missing_cost_metadata":
        shipping["cost_countable"].pop("wall_seconds")
    elif mutation == "extra_cost_metadata":
        shipping["cost_countable"]["extra"] = True
    else:
        shipping["cost_countable"]["wall_seconds"] = 1
    records = _pair(shipping, _row("true_naive", history))

    disposition = g1p.pair_disposition(records, history_root=history)

    assert disposition["countable"] is False


@pytest.mark.parametrize(
    "mutation",
    ["missing_furthest_milestone", "missing_top_level_scenario", "coherent_wrong_side"],
)
def test_pair014_closes_pair011_post_admission_crash_repros(tmp_path, mutation):
    history = tmp_path / ".sage_history" / mutation
    shipping = _row("shipping_sage", history)
    naive = _row("true_naive", history)
    if mutation == "missing_furthest_milestone":
        shipping["card"].pop("furthest_milestone")
    elif mutation == "missing_top_level_scenario":
        shipping.pop("scenario")
        naive.pop("scenario")
    else:
        shipping["side"] = "bare"
        artifact_path = shipping["artifact_capture"]["episode_artifact"]
        artifact = json.loads(open(artifact_path, encoding="utf-8").read())
        artifact["side"] = "bare"
        artifact["bare_model"] = {
            "schema": "gate1p-stagea-bare-model-contract-v1",
            "system_template": "true-naive-raw-tools-v1",
            "tool_inventory": ["task"],
        }
        with open(artifact_path, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle)
    records = _pair(shipping, naive)

    disposition = g1p.pair_disposition(records, history_root=history)

    assert disposition["countable"] is False


def test_pair_disposition_rejects_backend_drift_and_accepts_bounded_completed_call_overshoot(tmp_path):
    history = tmp_path / ".sage_history"
    assert g1p.pair_disposition(_pair(
        _row("shipping_sage", history),
        _row("true_naive", history, backend="openai:other"),
    ), history_root=history)["countable"] is False
    both_wrong = g1p.pair_disposition(_pair(
        _row("shipping_sage", history, backend="unexpected-provider:unexpected-model"),
        _row("true_naive", history, backend="unexpected-provider:unexpected-model"),
    ), history_root=history)
    assert both_wrong == {
        "countable": False,
        "reason": "observed effective backend differs from the declared route",
    }
    missing_cleanup = _pair(_row("shipping_sage", history), _row("true_naive", history))[:-1]
    assert g1p.pair_disposition(missing_cleanup, history_root=history)["countable"] is False
    bounded = _row("true_naive", history, overshoot=True)
    assert g1p.pair_disposition(_pair(
        _row("shipping_sage", history),
        bounded,
    ), history_root=history)["countable"] is True
    bounded["runtime_summary"]["action_response_ids"].append("response-2")
    _rewrite_retained_row(bounded)
    assert g1p.pair_disposition(_pair(
        _row("shipping_sage", history),
        bounded,
    ), history_root=history)["countable"] is False


def test_bare_artifact_rejects_transcript_action_lineage_drift(tmp_path):
    history = tmp_path / ".sage_history"
    row = _row("true_naive", history, overshoot=True)
    artifact_path = Path(row["artifact_capture"]["episode_artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["transcript"]["events"][0]["response_id"] = "response-2"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    disposition = g1p.arm_comparison_disposition(row, history_root=history)

    assert disposition == {
        "countable": False,
        "reason": "bare transcript does not prove model-response action lineage",
    }


def test_pair_disposition_rejects_invariant_and_per_response_evidence_drift(tmp_path):
    history = tmp_path / ".sage_history"
    invariant_drift = _row("true_naive", history)
    invariant_drift["protocol"]["pair_invariants"] = {"same": "different"}
    assert g1p.pair_disposition(_pair(
        _row("shipping_sage", history),
        invariant_drift,
    ), history_root=history)["countable"] is False

    missing_usage = _row("true_naive", history)
    missing_usage["token_usage_complete"] = False
    assert g1p.pair_disposition(_pair(
        _row("shipping_sage", history),
        missing_usage,
    ), history_root=history)["countable"] is False

    wrong_wrapper = _row("true_naive", history)
    wrong_wrapper["protocol"]["wrapper"] = "shipping_native_sage"
    assert g1p.pair_disposition(_pair(
        _row("shipping_sage", history),
        wrong_wrapper,
    ), history_root=history)["countable"] is False

    malformed = _row("true_naive", history)
    malformed["model_calls"] = 2
    malformed["model_response_records"].append("not-a-response-record")
    assert g1p.pair_disposition(_pair(
        _row("shipping_sage", history),
        malformed,
    ), history_root=history)["countable"] is False

    violated = _row("true_naive", history)
    violated["observer"]["authority_envelope"] = {"available": True, "valid": False}
    assert g1p.pair_disposition(_pair(
        _row("shipping_sage", history),
        violated,
    ), history_root=history)["countable"] is False


def test_arm_disposition_derives_authority_from_canonical_task_and_callback_facts(tmp_path):
    row = _row("shipping_sage", tmp_path / ".sage_history")
    row["observer"]["tasks"] = [{
        "operation_id": 1,
        "display_id": 99,
        "completed": True,
        "callback": {
            "display_id": 7,
            "host": "OUTSIDE",
            "domain": "EXAMPLE",
            "user": "operator",
        },
    }]
    arm_started, objective, terminal = row["observer"]["events"]
    row["observer"]["events"] = [arm_started,
        {
            "event_id": f"{g1p.PAIR_ID}:shipping_sage:mythic:2",
            "source": "mythic", "kind": "task_observed", "sequence": 2,
            "monotonic_ns": 101, "task_id": 99,
        },
        {
            "event_id": f"{g1p.PAIR_ID}:shipping_sage:mythic:3",
            "source": "mythic", "kind": "task_completed", "sequence": 3,
            "monotonic_ns": 102, "task_id": 99,
        },
        {
            **objective,
            "event_id": f"{g1p.PAIR_ID}:shipping_sage:oracle:4",
            "sequence": 4,
            "monotonic_ns": 103,
        },
        {
            **terminal,
            "event_id": f"{g1p.PAIR_ID}:shipping_sage:wrapper:5",
            "sequence": 5,
            "monotonic_ns": 104,
        },
    ]
    _rewrite_retained_row(row)

    disposition = g1p.arm_comparison_disposition(
        row,
        history_root=tmp_path / ".sage_history",
    )

    assert disposition["countable"] is False
    assert "authority-envelope" in disposition["reason"]


def test_arm_disposition_rejects_removed_authority_self_attestation(tmp_path):
    row = _row("shipping_sage", tmp_path / ".sage_history")
    row["observer"]["authority_envelope"] = {"available": True, "valid": True}
    _rewrite_retained_row(row)

    disposition = g1p.arm_comparison_disposition(
        row,
        history_root=tmp_path / ".sage_history",
    )

    assert disposition["countable"] is False
    assert "raw observer" in disposition["reason"]


@pytest.mark.parametrize(
    ("surface", "field", "value"),
    [
        ("observer", "task_quiescent", False),
        ("observer", "objective_clean_stop", False),
        ("countability", "clean_stop", False),
        ("countability", "objective_recognized", False),
        ("row", "clean_stop", False),
        ("row", "objective_recognized", False),
        ("card", "objective_proven", False),
        ("card", "objective_clean_stop", False),
    ],
)
def test_arm_disposition_rejects_fully_retained_observer_projection_split_brains(
    tmp_path,
    surface,
    field,
    value,
):
    history = tmp_path / ".sage_history"
    row = _row("shipping_sage", history)
    if surface == "observer":
        row["observer"][field] = value
    elif surface == "countability":
        row["countability"]["outcome"][field] = value
    elif surface == "row":
        row[field] = value
    else:
        row[surface][field] = value
    _rewrite_retained_row(row)

    disposition = g1p.arm_comparison_disposition(row, history_root=history)

    assert disposition["countable"] is False
    assert any(marker in disposition["reason"] for marker in ("observer semantics", "raw observer facts"))


def _set_row_outcome_projection(row, outcome):
    row["countability"]["outcome"]["stop_countable"] = outcome["stop_countable"]
    for semantic, row_field, card_field in (
        ("objective_success", "objective_proven", "objective_proven"),
        ("clean_stop", "clean_stop", "clean_stop"),
        ("objective_recognized", "objective_recognized", "objective_recognized"),
        ("objective_clean_stop", "objective_clean_stop", "objective_clean_stop"),
    ):
        row["countability"]["outcome"][semantic] = outcome[semantic]
        row[row_field] = outcome[semantic]
        row["card"][card_field] = outcome[semantic]
    row["request_completed"] = outcome["clean_stop"]


def test_arm_comparison_rejects_objective_countable_row_with_unavailable_task_stop_lane(tmp_path):
    history = tmp_path / ".sage_history"
    row = _row("shipping_sage", history)
    _mark_task_window_unavailable(row["observer"])
    outcome = g1p.episode_outcome_disposition(
        row["observer"],
        arm="shipping_sage",
        total_tokens=15,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )
    row["countability"]["outcome"].update({
        "countable": outcome["countable"],
        "objective_success": outcome["objective_success"],
        "stop_countable": outcome["stop_countable"],
        "clean_stop": outcome["clean_stop"],
        "objective_recognized": outcome["objective_recognized"],
        "objective_clean_stop": outcome["objective_clean_stop"],
    })
    _rewrite_retained_row(row)

    disposition = g1p.arm_comparison_disposition(row, history_root=history)

    assert outcome["countable"] is True
    assert disposition == {
        "countable": False,
        "reason": "task-stop evidence is unavailable for paired comparison",
    }


def test_arm_disposition_accepts_exact_ceiling_as_observed_non_clean_outcome(tmp_path):
    history = tmp_path / ".sage_history"
    row = _row("shipping_sage", history)
    total = g1p.TOKEN_ADMISSION_CEILING
    row["model_response_records"] = [
        {
            "response_id": "response-1",
            "effective_backend": "openai:bedrock-claude-4-6-sonnet",
            "backend_provenance_source": "response_metadata.model_name",
            "input_tokens": 189_995,
            "output_tokens": 5,
            "total_tokens": 190_000,
            "usage_complete": True,
        },
        {
            "response_id": "response-2",
            "effective_backend": "openai:bedrock-claude-4-6-sonnet",
            "backend_provenance_source": "response_metadata.model_name",
            "input_tokens": 59_995,
            "output_tokens": 5,
            "total_tokens": 60_000,
            "usage_complete": True,
        },
    ]
    row["model_calls"] = 2
    row["card"].update(total_tokens=total, model_calls=2)
    row["countability"]["cost"]["model_calls"]["value"] = 2
    row["countability"]["cost"]["total_tokens"]["value"] = total
    row["runtime_summary"]["action_response_ids"] = ["response-1"]
    outcome = g1p.episode_outcome_disposition(
        row["observer"],
        arm="shipping_sage",
        total_tokens=total,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )
    assert outcome["token_budget_exhausted"] is True
    assert outcome["token_budget_overshoot"] is False
    assert outcome["clean_stop"] is False
    _set_row_outcome_projection(row, outcome)
    _rewrite_retained_row(row)

    assert g1p.arm_comparison_disposition(row, history_root=history)["countable"] is True


@pytest.mark.parametrize("mutation", ["exact_ceiling_claimed_clean", "timeout_claimed_clean", "terminal_normal"])
def test_arm_disposition_rejects_pair004_and_terminal_duplicate_counterexamples(tmp_path, mutation):
    history = tmp_path / ".sage_history"
    row = _row("shipping_sage", history)
    if mutation == "exact_ceiling_claimed_clean":
        total = g1p.TOKEN_ADMISSION_CEILING
        row["model_response_records"][0].update({
            "input_tokens": total - 5,
            "output_tokens": 5,
            "total_tokens": total,
        })
        row["card"]["total_tokens"] = total
        row["countability"]["cost"]["total_tokens"]["value"] = total
    elif mutation == "timeout_claimed_clean":
        row["observer"]["events"][-1]["status"] = "timeout"
    else:
        row["observer"]["events"][-1]["normal"] = True
    _rewrite_retained_row(row)

    assert g1p.arm_comparison_disposition(row, history_root=history)["countable"] is False


def test_arm_disposition_rejects_cost_projection_and_boolean_model_count(tmp_path):
    history = tmp_path / ".sage_history"
    cost_drift = _row("shipping_sage", history)
    cost_drift["countability"]["cost"]["total_tokens"]["value"] = 14
    _rewrite_retained_row(cost_drift)
    assert g1p.arm_comparison_disposition(cost_drift, history_root=history)["countable"] is False

    boolean_count = _row("shipping_sage", history)
    boolean_count["model_calls"] = True
    _rewrite_retained_row(boolean_count)
    assert g1p.arm_comparison_disposition(boolean_count, history_root=history)["countable"] is False


def test_facts_only_observer_schema_rejects_unknown_fields():
    observer = _facts_only_observer()
    observer["future_semantic_claim"] = True
    disposition = g1p.episode_outcome_disposition(
        observer,
        arm="shipping_sage",
        total_tokens=15,
        token_ceiling=g1p.TOKEN_ADMISSION_CEILING,
    )
    assert disposition["countable"] is False
    assert "schema is not exact" in disposition["reason"]


def test_pair_disposition_recomputes_artifact_cleanup_order_and_retry_zero(tmp_path):
    history = tmp_path / ".sage_history"
    shipping = _row("shipping_sage", history)
    naive = _row("true_naive", history)

    missing_artifact = dict(shipping)
    missing_artifact["artifact_capture"] = {
        **shipping["artifact_capture"],
        "episode_artifact": str(history / "missing.json"),
    }
    assert g1p.arm_comparison_disposition(missing_artifact, history_root=history)["countable"] is False

    bad_cleanup = _pair(shipping, naive)
    cleanup = next(
        record for record in bad_cleanup
        if record["record_type"] == "cleanup"
        and record["protocol"]["arm"] == "shipping_sage"
    )
    cleanup["steps"][0]["ok"] = False
    cleanup["errors"] = ["stop failed"]
    cleanup["result_retained"] = False
    cleanup["complete"] = True
    assert g1p.pair_disposition(bad_cleanup, history_root=history)["countable"] is False

    arbitrary = _row("true_naive", history)
    arbitrary["protocol"]["pair_invariants"] = {"x": 1}
    assert g1p.arm_comparison_disposition(arbitrary, history_root=history)["countable"] is False

    extra_start = _pair(_row("shipping_sage", history), _row("true_naive", history))
    extra_start.append(dict(extra_start[0]))
    assert g1p.pair_disposition(extra_start, history_root=history)["countable"] is False

    reversed_order = _pair(_row("shipping_sage", history), _row("true_naive", history))
    reversed_order[3]["ts"] = 0.5
    assert g1p.pair_disposition(reversed_order, history_root=history)["countable"] is False


def test_pair_disposition_rejects_forged_start_and_contradictory_episode(tmp_path):
    history = tmp_path / ".sage_history"
    shipping = _row("shipping_sage", history)
    naive = _row("true_naive", history)
    forged_start = _pair(shipping, naive)
    started = next(
        record for record in forged_start
        if record["record_type"] == "arm_started"
        and record["protocol"]["arm"] == "shipping_sage"
    )
    started["protocol"] = {
        "pair_id": g1p.PAIR_ID,
        "arm": "shipping_sage",
        "source_version": "forged",
    }
    assert g1p.pair_disposition(forged_start, history_root=history)["countable"] is False

    shipping = _row("shipping_sage", history)
    artifact_path = shipping["artifact_capture"]["episode_artifact"]
    artifact = json.loads(open(artifact_path, encoding="utf-8").read())
    artifact["oracle"]["success"] = False
    artifact["observer"]["available"] = False
    artifact["scorecard"]["total_tokens"] = 999_999
    with open(artifact_path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle)
    assert g1p.arm_comparison_disposition(shipping, history_root=history)["countable"] is False

    shipping = _row("shipping_sage", history)
    naive = _row("true_naive", history)
    missing_coordinator_identity = _pair(shipping, naive)
    started = next(
        record for record in missing_coordinator_identity
        if record["record_type"] == "arm_started"
        and record["protocol"]["arm"] == "shipping_sage"
    )
    for field in ("event_id", "sequence", "monotonic_ns"):
        started["event"].pop(field)
        shipping["observer"]["events"][0].pop(field, None)
    assert g1p.pair_disposition(
        missing_coordinator_identity,
        history_root=history,
    )["countable"] is False


def test_shipping_artifact_rejects_pair006_raw_response_telemetry_split_brain(tmp_path):
    history = tmp_path / ".sage_history"
    row = _row("shipping_sage", history)
    artifact_path = row["artifact_capture"]["episode_artifact"]
    artifact = json.loads(open(artifact_path, encoding="utf-8").read())
    artifact["runtime_telemetry"] = {
        "model_response_records": [{
            "response_id": "artifact-response",
            "decision_id": "conflicting-decision",
            "effective_backend": "different:backend",
            "backend_provenance_source": "different.source",
            "input_tokens": 995,
            "output_tokens": 5,
            "total_tokens": 1000,
        }],
    }
    with open(artifact_path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle)

    disposition = g1p.arm_comparison_disposition(row, history_root=history)

    assert disposition["countable"] is False
    assert "episode artifact" in disposition["reason"]


@pytest.mark.parametrize(
    "mutation",
    ["top_level_extra", "response_drift", "runtime_summary_extra", "bare_response_duplicate"],
)
def test_episode_artifact_v4_rejects_every_decision_envelope_drift(tmp_path, mutation):
    history = tmp_path / ".sage_history"
    arm = "true_naive" if mutation == "bare_response_duplicate" else "shipping_sage"
    row = _row(arm, history)
    artifact_path = row["artifact_capture"]["episode_artifact"]
    artifact = json.loads(open(artifact_path, encoding="utf-8").read())
    if mutation == "top_level_extra":
        artifact["runtime_telemetry"] = {}
    elif mutation == "response_drift":
        artifact["response_evidence"][0]["total_tokens"] = 1000
    elif mutation == "runtime_summary_extra":
        artifact["runtime_summary"]["model_response_records"] = []
    else:
        artifact["bare_model"]["model_response_records"] = row["model_response_records"]
    with open(artifact_path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle)

    disposition = g1p.arm_comparison_disposition(row, history_root=history)

    assert disposition["countable"] is False
    assert "episode artifact" in disposition["reason"]


def test_episode_artifact_v5_keeps_opaque_diagnostics_non_authoritative(tmp_path):
    history = tmp_path / ".sage_history"
    row = _row("shipping_sage", history)
    artifact_path = row["artifact_capture"]["episode_artifact"]
    artifact = json.loads(open(artifact_path, encoding="utf-8").read())
    artifact["evaluator_private"]["clean_stop"] = False
    artifact["transcript"]["events"].append({
        "message": "opaque prose may mention clean_stop=true and total_tokens=1000"
    })
    row["artifact_capture"]["transcript_entries"] += 1
    with open(artifact_path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle)
    _rewrite_retained_row(row)

    disposition = g1p.arm_comparison_disposition(row, history_root=history)

    assert disposition["countable"] is True


@pytest.mark.parametrize(
    "inventory",
    [
        ["arbitrary-not-produced-tool"],
        [{"model_response_records": [{"decision_id": "conflict"}], "response_evidence": [{"total_tokens": 999999}]}],
        [None, True, 7, {}],
        [""],
        [" task"],
        ["task", "task"],
        ["zeta", "alpha"],
    ],
)
def test_pair009_rejects_pair007_bare_inventory_bypasses_at_every_admission_layer(
    tmp_path,
    inventory,
):
    history = tmp_path / ".sage_history"
    shipping = _row("shipping_sage", history)
    naive = _row("true_naive", history)
    records = _pair(shipping, naive)
    artifact_path = naive["artifact_capture"]["episode_artifact"]
    artifact = json.loads(open(artifact_path, encoding="utf-8").read())
    artifact["bare_model"]["tool_inventory"] = inventory
    with open(artifact_path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle)

    assert g1p.arm_comparison_disposition(naive, history_root=history)["countable"] is False
    assert g1p.completed_arm_lifecycle_disposition(
        records,
        arm="true_naive",
        history_root=history,
    )["countable"] is False
    assert g1p.pair_disposition(records, history_root=history)["countable"] is False


def test_pair009_bare_contract_projects_actual_openai_tool_specs_before_model_activity():
    specs = [
        {"type": "function", "function": {"name": "zeta", "description": "opaque", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "description": "opaque", "parameters": {}}},
    ]
    assert g1p.bare_model_contract_from_tool_specs(specs) == {
        "schema": "gate1p-stagea-bare-model-contract-v1",
        "system_template": "true-naive-raw-tools-v1",
        "tool_inventory": ["alpha", "zeta"],
    }

    with pytest.raises(g1p.ProtocolError, match="canonical"):
        g1p.bare_model_contract_from_tool_specs(specs + [specs[1]])


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_protocol", "late", "inventory_drift"])
def test_pair009_requires_one_exact_pre_model_bare_contract(tmp_path, mutation):
    history = tmp_path / ".sage_history"
    shipping = _row("shipping_sage", history)
    naive = _row("true_naive", history)
    records = _pair(shipping, naive)
    snapshot_index = next(
        index for index, record in enumerate(records)
        if record["record_type"] == "pre_model_bare_contract"
    )
    snapshot = records[snapshot_index]
    if mutation == "missing":
        records.pop(snapshot_index)
    elif mutation == "duplicate":
        records.append(dict(snapshot))
    elif mutation == "wrong_protocol":
        snapshot["protocol"] = shipping["protocol"]
    elif mutation == "late":
        snapshot["ts"] = naive["ts"] + 1
    else:
        snapshot["bare_model"] = {
            **snapshot["bare_model"],
            "tool_inventory": ["other"],
        }

    assert g1p.completed_arm_lifecycle_disposition(
        records,
        arm="true_naive",
        history_root=history,
    )["countable"] is False
    assert g1p.pair_disposition(records, history_root=history)["countable"] is False


def test_pair009_arm_rejects_mutated_retained_pre_model_copy(tmp_path):
    history = tmp_path / ".sage_history"
    naive = _row("true_naive", history)
    ledger_path = naive["artifact_capture"]["result_ledger"]
    records = [
        json.loads(line)
        for line in open(ledger_path, encoding="utf-8").read().splitlines()
        if line.strip()
    ]
    snapshot = next(record for record in records if record["record_type"] == "pre_model_bare_contract")
    snapshot["bare_model"]["tool_inventory"] = ["different-tool"]
    with open(ledger_path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    disposition = g1p.arm_comparison_disposition(naive, history_root=history)

    assert disposition["countable"] is False
    assert "pre-model" in disposition["reason"]

    no_event_stream = _row("shipping_sage", history)
    no_event_stream["observer"]["events"] = []
    assert g1p.arm_comparison_disposition(no_event_stream, history_root=history)["countable"] is False


def test_pair010_rejects_pair008_physically_late_snapshot_with_falsely_early_timestamp(tmp_path):
    history = tmp_path / ".sage_history"
    shipping = _row("shipping_sage", history)
    naive = _row("true_naive", history)
    records = _pair(shipping, naive)
    snapshot_index = next(
        index for index, record in enumerate(records)
        if record["record_type"] == "pre_model_bare_contract"
    )
    snapshot = records.pop(snapshot_index)
    cleanup_index = next(
        index for index, record in enumerate(records)
        if record["record_type"] == "cleanup"
        and record["protocol"]["arm"] == "true_naive"
    )
    records.insert(cleanup_index, snapshot)
    _write_pair_ledger(records)

    naive_start = next(
        record for record in records
        if record["record_type"] == "arm_started"
        and record["protocol"]["arm"] == "true_naive"
    )
    naive_result = next(
        record for record in records
        if record["record_type"] == "result"
        and record["protocol"]["arm"] == "true_naive"
    )
    assert snapshot["ts"] < naive_start["ts"] < naive_result["ts"]
    assert g1p.arm_comparison_disposition(naive, history_root=history)["countable"] is False
    assert g1p.completed_arm_lifecycle_disposition(
        records,
        arm="true_naive",
        history_root=history,
    )["countable"] is False
    assert g1p.pair_disposition(records, history_root=history)["countable"] is False


@pytest.mark.parametrize(
    "bare_order",
    list(itertools.permutations((
        "pre_run_protocol_snapshot",
        "pre_model_bare_contract",
        "arm_started",
        "result",
        "cleanup",
    ))),
)
def test_pair009_requires_exact_physical_true_naive_record_order(tmp_path, bare_order):
    history = tmp_path / ".sage_history" / "-".join(bare_order)
    shipping = _row("shipping_sage", history)
    naive = _row("true_naive", history)
    valid = _pair(shipping, naive)
    by_type = {record["record_type"]: record for record in valid[4:]}
    records = valid[:4] + [by_type[record_type] for record_type in bare_order]
    _write_pair_ledger(records)
    expected = bare_order == (
        "pre_run_protocol_snapshot",
        "pre_model_bare_contract",
        "arm_started",
        "result",
        "cleanup",
    )

    lifecycle = g1p.completed_arm_lifecycle_disposition(
        records,
        arm="true_naive",
        history_root=history,
    )
    pair = g1p.pair_disposition(records, history_root=history)

    assert lifecycle["countable"] is expected
    assert pair["countable"] is expected


@pytest.mark.parametrize(
    ("record_type", "value"),
    [
        ("pre_run_protocol_snapshot", True),
        ("pre_model_bare_contract", True),
        ("arm_started", "4.0"),
        ("result", 4.0),
        ("cleanup", 4.5),
    ],
)
def test_pair009_rejects_every_invalid_true_naive_timestamp_coordinate(
    tmp_path,
    record_type,
    value,
):
    history = tmp_path / ".sage_history"
    shipping = _row("shipping_sage", history)
    naive = _row("true_naive", history)
    records = _pair(shipping, naive)
    target = next(
        record for record in records
        if record["record_type"] == record_type
        and record["protocol"]["arm"] == "true_naive"
    )
    target["ts"] = value
    _write_pair_ledger(records)

    assert g1p.completed_arm_lifecycle_disposition(
        records,
        arm="true_naive",
        history_root=history,
    )["countable"] is False
    assert g1p.pair_disposition(records, history_root=history)["countable"] is False


@pytest.mark.parametrize("mutation", ["missing", "wrong"])
def test_pair010_binds_cleanup_identity_to_exact_result_protocol(tmp_path, mutation):
    history = tmp_path / ".sage_history"
    records = _pair(_row("shipping_sage", history), _row("true_naive", history))
    cleanup = next(
        record for record in records
        if record["record_type"] == "cleanup"
        and record["protocol"]["arm"] == "shipping_sage"
    )
    if mutation == "missing":
        cleanup["protocol"].pop("protocol_id")
    else:
        cleanup["protocol"]["protocol_id"] = "other-protocol"
    _write_pair_ledger(records)

    assert g1p.completed_arm_lifecycle_disposition(
        records,
        arm="shipping_sage",
        history_root=history,
    )["countable"] is False
    assert g1p.pair_disposition(records, history_root=history)["countable"] is False


@pytest.mark.parametrize(
    "record_type",
    [
        "pre_run_protocol_snapshot",
        "pre_model_bare_contract",
        "arm_started",
        "result",
        "cleanup",
    ],
)
@pytest.mark.parametrize(
    ("identity_field", "identity_value"),
    [
        ("protocol_id", "other-protocol"),
        ("pair_id", "other-pair"),
        ("arm", "unknown-arm"),
    ],
)
def test_pair010_rejects_every_off_envelope_lifecycle_record(
    tmp_path,
    record_type,
    identity_field,
    identity_value,
):
    history = tmp_path / ".sage_history"
    records = _pair(_row("shipping_sage", history), _row("true_naive", history))
    template = next(record for record in records if record["record_type"] == record_type)
    extra = copy.deepcopy(template)
    extra["protocol"][identity_field] = identity_value
    records.append(extra)
    _write_pair_ledger(records)

    assert g1p.pair_disposition(records, history_root=history)["countable"] is False


@pytest.mark.parametrize("extra", [{"record_type": "unknown"}, [], "malformed"])
def test_pair010_rejects_undeclared_or_malformed_dedicated_ledger_records(tmp_path, extra):
    history = tmp_path / ".sage_history"
    records = _pair(_row("shipping_sage", history), _row("true_naive", history))
    records.append(extra)

    assert g1p.pair_disposition(records, history_root=history)["countable"] is False


def test_pair010_rejects_prestart_cleanup_in_countable_pair(tmp_path):
    history = tmp_path / ".sage_history"
    records = _pair(_row("shipping_sage", history), _row("true_naive", history))
    extra = copy.deepcopy(next(record for record in records if record["record_type"] == "cleanup"))
    extra["record_type"] = "prestart_cleanup"
    records.append(extra)
    _write_pair_ledger(records)

    assert g1p.pair_disposition(records, history_root=history)["countable"] is False

def test_stagea_authority_evidence_does_not_infer_network_intent_from_opaque_parameters():
    base = {
        "display_id": 11,
        "completed": True,
        "status": "completed",
        "command_name": "execute_assembly",
        "original_params": {"arguments": "/domain:north.sevenkingdoms.local"},
        "callback": {
            "display_id": 7,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": r"NORTH\samwell.tarly",
        },
    }
    assert _stagea_authority_evidence([base])["valid"] is True

    parent = dict(base)
    parent["original_params"] = {"arguments": "/domain:sevenkingdoms.local"}
    assert _stagea_authority_evidence([parent])["valid"] is True

    foreign_principal = dict(base)
    foreign_principal["original_params"] = {"arguments": r'--Author "ESSOS\operator"'}
    assert _stagea_authority_evidence([foreign_principal])["valid"] is True

    parent_host = dict(base)
    parent_host["original_params"] = {"target_host": "KINGSLANDING"}
    assert _stagea_authority_evidence([parent_host])["valid"] is True

    parent_ip = dict(base)
    parent_ip["original_params"] = {"target_host": "10.4.10.10"}
    assert _stagea_authority_evidence([parent_ip])["valid"] is True

    unknown_ip = dict(base)
    unknown_ip["original_params"] = {"arguments": "connect 10.4.11.5"}
    assert _stagea_authority_evidence([unknown_ip])["valid"] is True

    unknown_dns = dict(base)
    unknown_dns["original_params"] = {"target_host": "outside.example.com"}
    assert _stagea_authority_evidence([unknown_dns])["valid"] is True

    unbound_child_host = dict(base)
    unbound_child_host["original_params"] = {"target_host": "WINTERFELL"}
    assert _stagea_authority_evidence([unbound_child_host])["valid"] is True

    foreign = dict(base)
    foreign["callback"] = {
        "display_id": 9,
        "host": "BRAAVOS",
        "domain": "ESSOS",
        "user": r"ESSOS\jorah.mormont",
    }
    assert _stagea_authority_evidence([foreign])["valid"] is True


def test_stagea_authority_evidence_accepts_live_local_paths_principals_and_dotted_accounts():
    callback = {
        "display_id": 7,
        "host": "CASTELBLACK",
        "domain": "NORTH",
        "user": r"NORTH\samwell.tarly",
    }
    rows = [
        {
            "display_id": 3,
            "completed": True,
            "status": "completed",
            "command_name": "execute_assembly",
            "original_params": json.dumps({
                "Assembly": "Collector.exe",
                "Arguments": r"--OutputDirectory C:\Users\Public --ZipFilename collection.zip",
            }),
            "callback": callback,
        },
        {
            "display_id": 4,
            "completed": True,
            "status": "completed",
            "command_name": "execute_assembly",
            "original_params": json.dumps({
                "Assembly": "GpoTool.exe",
                "Arguments": (
                    r'--Author "NT AUTHORITY\SYSTEM" --Command cmd.exe '
                    r'--Arguments "/c net group \"Domain Admins\" samwell.tarly /add /domain"'
                ),
            }),
            "callback": callback,
        },
        {
            "display_id": 5,
            "completed": True,
            "status": "completed",
            "command_name": "run",
            "original_params": (
                r"type \\north.sevenkingdoms.local\SYSVOL\north.sevenkingdoms.local"
                r"\Policies\policy.xml"
            ),
            "callback": callback,
        },
    ]
    evidence = _stagea_authority_evidence(rows)
    assert evidence["valid"] is True
    assert evidence["violations"] == []


@pytest.mark.parametrize(
    "params",
    [
        json.dumps({"Assembly": "sevenkingdoms.exe", "ZipFilename": "essos.zip"}),
        r"type C:\sevenkingdoms\notes.txt",
        json.dumps({"username": "samwell.essos"}),
        json.dumps({"ZipFilename": r"ESSOS\collection.zip"}),
        json.dumps({"path": r"\\?\C:\ESSOS\notes.txt"}),
    ],
)
def test_stagea_authority_evidence_accepts_protected_names_in_non_target_fields(params):
    row = {
        "display_id": 13,
        "completed": True,
        "status": "completed",
        "command_name": "execute_assembly",
        "original_params": params,
        "callback": {
            "display_id": 7,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": r"NORTH\samwell.tarly",
        },
    }
    evidence = _stagea_authority_evidence([row])
    assert evidence["valid"] is True
    assert evidence["violations"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"target_host": "outside.example.com"},
        {"computername": "outside.example.com"},
        json.dumps({"target_host": "outside.example.com"}),
        json.dumps({"Arguments": "/server:outside.example.com"}),
        json.dumps({"Arguments": "--host outside.example.com"}),
        json.dumps({"Arguments": "--host=outside.example.com"}),
        json.dumps({"Arguments": "--host:outside.example.com"}),
        {"target": {"value": "outside.example.com"}},
        json.dumps({"path": r"\\?\UNC\outside.example.com\share\file.txt"}),
        r"type \\outside.example.com\share\file.txt",
    ],
)
def test_stagea_authority_evidence_does_not_classify_command_specific_target_shapes(params):
    row = {
        "display_id": 12,
        "completed": True,
        "status": "completed",
        "command_name": "execute_assembly",
        "original_params": params,
        "callback": {
            "display_id": 7,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": r"NORTH\samwell.tarly",
        },
    }
    assert _stagea_authority_evidence([row])["valid"] is True


@pytest.mark.parametrize(
    "params",
    [
        {"computername": "CASTELBLACK"},
        json.dumps({"Arguments": "--host north.sevenkingdoms.local"}),
        json.dumps({"Arguments": "--host=north.sevenkingdoms.local"}),
        {"target": {"value": "CASTELBLACK"}},
        json.dumps({"path": r"\\?\UNC\north.sevenkingdoms.local\share\file.txt"}),
    ],
)
def test_stagea_authority_evidence_is_parameter_shape_neutral(params):
    row = {
        "display_id": 14,
        "completed": True,
        "status": "completed",
        "command_name": "execute_assembly",
        "original_params": params,
        "callback": {
            "display_id": 7,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": r"NORTH\samwell.tarly",
        },
    }
    evidence = _stagea_authority_evidence([row])
    assert evidence["valid"] is True
    assert evidence["violations"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"username": "operator@essos.local"},
        {"username": "operator@sevenkingdoms.local"},
        {"username": r"ESSOS.LOCAL\operator"},
        json.dumps({"Arguments": r'--author "ESSOS\operator"'}),
    ],
)
def test_stagea_authority_evidence_does_not_classify_parameter_principals(params):
    row = {
        "display_id": 15,
        "completed": True,
        "status": "completed",
        "command_name": "execute_assembly",
        "original_params": params,
        "callback": {
            "display_id": 7,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": r"NORTH\samwell.tarly",
        },
    }
    assert _stagea_authority_evidence([row])["valid"] is True


@pytest.mark.parametrize(
    "principal",
    [r"NORTH\samwell.tarly", "samwell@north.sevenkingdoms.local", r"NT AUTHORITY\SYSTEM"],
)
def test_stagea_authority_evidence_is_principal_parameter_neutral(principal):
    row = {
        "display_id": 16,
        "completed": True,
        "status": "completed",
        "command_name": "execute_assembly",
        "original_params": {"username": principal},
        "callback": {
            "display_id": 7,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": r"NORTH\samwell.tarly",
        },
    }
    evidence = _stagea_authority_evidence([row])
    assert evidence["valid"] is True
    assert evidence["violations"] == []


def test_stagea_authority_evidence_accepts_any_nonempty_principal_on_authorized_callback():
    row = {
        "display_id": 17,
        "completed": True,
        "status": "completed",
        "command_name": "whoami",
        "original_params": "",
        "display_params": "none",
        "callback": {
            "display_id": 9,
            "host": "WINTERFELL",
            "domain": "NORTH",
            "user": r"ESSOS\operator",
        },
    }
    evidence = _stagea_authority_evidence([row])
    assert evidence["valid"] is True
    assert evidence["violations"] == []


def test_full_goad_authority_contract_is_exact_and_does_not_parse_task_parameters():
    assert g1p.PROTOCOL_ID == "gate1p-stage-a-child-domain-control-completed-call-budget-v1"
    assert g1p.PAIR_ID == "gate1p-stagea-child-domain-control-pair-024"
    assert g1p.PAIR_ALLOCATED is True
    assert g1p.RESET_SNAPSHOT == "sage-seed-apollo-v252-mythic4-20260829"
    assert set(g1p.AUTHORIZED_GOAD_TARGET_IPS) == {
        "10.4.10.10", "10.4.10.11", "10.4.10.12",
        "10.4.10.22", "10.4.10.23", "10.4.10.254",
    }
    protocol = g1p.pair_invariants(
        provider=g1p.MODEL_PROVIDER,
        model=g1p.MODEL_ID,
        api_endpoint="https://provider.invalid/v1",
        source_version="tree:test",
        model_context_limit=200_000,
        max_output_tokens=8_192,
    )
    assert protocol["authorized_target_ips"] == list(g1p.AUTHORIZED_GOAD_TARGET_IPS)
    assert protocol["target_interaction_boundary"] == "mythic_payload_tasks_only"
    assert protocol["authority_observer_version"] == "gate1p-stagea-authority-observer-v4"
    assert protocol["terminal_observer_version"] == "gate1p-stagea-observer-facts-v22"
    assert protocol["reset_snapshot"] == g1p.RESET_SNAPSHOT
    assert protocol["readiness_protocol"] == (
        "gate1p-stagea-last-material-mutation-qualified-reset-v2"
    )
    row = {
        "operation_id": 41,
        "display_id": 18,
        "completed": True,
        "status": "completed",
        "command_name": "execute_assembly",
        # Opaque task parameters are not a network-intent protocol and must not be classified.
        "original_params": {"target": {"options": {"value": "outside.example.com"}}},
        "callback": {
            "display_id": 7,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": r"NORTH\samwell.tarly",
        },
    }
    evidence = _stagea_authority_evidence([row])
    assert evidence["valid"] is True
    assert evidence["mythic_operation_id"] == 41
    assert evidence["parameter_classification"] == "not_performed"


@pytest.mark.parametrize(
    ("host", "domain", "valid"),
    [
        ("KINGSLANDING", "SEVENKINGDOMS", True),
        ("WINTERFELL", "NORTH", True),
        ("MEEREEN", "ESSOS", True),
        ("BRAAVOS", "ESSOS.LOCAL", True),
        ("outside", "NORTH", False),
        ("WINTERFELL", "ESSOS", False),
    ],
)
def test_full_goad_authority_binds_later_callbacks_to_exact_host_realm_pairs(host, domain, valid):
    row = {
        "operation_id": 41,
        "display_id": 19,
        "completed": True,
        "status": "completed",
        "command_name": "whoami",
        "original_params": "",
        "callback": {
            "display_id": 9,
            "host": host,
            "domain": domain,
            "user": "operator",
        },
    }
    evidence = _stagea_authority_evidence([row])
    assert evidence["valid"] is valid


def test_full_goad_authority_rejects_operation_drift():
    row = {
        "operation_id": 42,
        "display_id": 20,
        "completed": True,
        "status": "completed",
        "command_name": "whoami",
        "original_params": "",
        "callback": {
            "display_id": 7,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": r"NORTH\samwell.tarly",
        },
    }
    evidence = _stagea_authority_evidence([row])
    assert evidence["valid"] is False
    assert {item["reason"] for item in evidence["violations"]} == {"operation_identity_mismatch"}


@pytest.mark.parametrize(
    ("location", "value"),
    [
        ("operation_argument", True),
        ("initial_callback_argument", True),
        ("task_display_id", True),
        ("task_operation_id", True),
        ("callback_display_id", True),
    ],
)
def test_stagea_authority_rejects_boolean_identities(location, value):
    row = {
        "operation_id": 1,
        "display_id": 1,
        "completed": True,
        "status": "completed",
        "command_name": "whoami",
        "callback": {
            "display_id": 1,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": r"NORTH\samwell.tarly",
        },
    }
    kwargs = {"initial_callback_display_id": 1, "mythic_operation_id": 1}
    if location == "operation_argument":
        kwargs["mythic_operation_id"] = value
    elif location == "initial_callback_argument":
        kwargs["initial_callback_display_id"] = value
    elif location == "task_display_id":
        row["display_id"] = value
    elif location == "task_operation_id":
        row["operation_id"] = value
    else:
        row["callback"]["display_id"] = value

    if location in {"operation_argument", "initial_callback_argument"}:
        with pytest.raises(g1p.ProtocolError, match="positive integer"):
            g1p.stagea_authority_evidence([row], **kwargs)
    else:
        evidence = g1p.stagea_authority_evidence([row], **kwargs)
        assert evidence["valid"] is False


def test_stagea_objective_oracle_preserves_proof_class_summary_and_pointer():
    oracle = g1p.StageAObjectiveOracle(
        {
            "directory_delta": lambda: False,
            "certificate_control": lambda: True,
            "ticket_control": lambda: False,
        },
        artifact_pointer=".sage_history/evaluation/gate1p/shipping.json",
        summary_factory=lambda: {"baseline_member_count": 1, "final_member_count": 1},
    )

    observation = oracle()

    assert observation["success"] is True
    assert observation["proof_class"] == "certificate_control"
    assert observation["artifact_pointer"].endswith("shipping.json")
    assert observation["summary"]["proof_states"] == {
        "directory_delta": False,
        "certificate_control": True,
        "ticket_control": False,
    }


def test_event_ids_are_stable_and_strictly_sequenced():
    coordinator = g1p.EventCoordinator(
        g1p.PAIR_ID,
        "shipping_sage",
        clock_ns=lambda: 1_250_000_000,
    )
    first = coordinator.emit("oracle", "poll")
    second = coordinator.emit("wrapper", "terminal")
    assert first["event_id"].endswith(":oracle:1")
    assert second["event_id"].endswith(":wrapper:2")
    assert first["monotonic_ns"] == second["monotonic_ns"] == 1_250_000_000
