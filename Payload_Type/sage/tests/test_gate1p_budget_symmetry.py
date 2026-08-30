"""Gate 1P completed-call budget symmetry at the real arm boundaries.

The shipping arm enforces the boundary in ``ai.langgraph.policy.LLMPolicy``; the true-naive arm
enforces it in ``ai.hillclimb.bare_runner.BareModelRunner``. These tests execute both mechanisms.
Comments, docstrings, helper names, and unrelated constructors cannot satisfy the guard.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from ai.hillclimb import bare_runner, gate1p_stagea, run_gauge_live
from ai.langgraph import policy


class _Action:
    def __init__(self):
        self.name = "candidate"
        self.target = "target"
        self.preconditions = []
        self.effects = ["effect:candidate"]
        self.reason = ""


class _State:
    def achieved_effects(self):
        return set()


class _CrossingResponse:
    content = json.dumps({
        "disposition": "select",
        "capability": "candidate",
        "target": "target",
        "rationale": "cross the boundary",
    })
    response_metadata = {"model_provider": "openai", "model_name": "test-model"}
    usage_metadata = {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11}


def _valid_request(*, side: str, arm_index: int, token_ceiling=10) -> dict:
    return {
        "protocol_id": gate1p_stagea.PROTOCOL_ID,
        "pair_id": gate1p_stagea.PAIR_ID,
        "scenario": gate1p_stagea.SCENARIO,
        "side": side,
        "arm_index": arm_index,
        "provider": gate1p_stagea.MODEL_PROVIDER,
        "model": gate1p_stagea.MODEL_ID,
        "policy_mode": gate1p_stagea.POLICY_MODE,
        "solve_timeout": gate1p_stagea.WALL_SECONDS,
        "token_ceiling": token_ceiling,
        "model_context_limit": 200_000,
        "max_output_tokens": 8_192,
        "source_version": "tree:test",
        "api_endpoint": "https://provider.invalid/v1",
    }


@pytest.mark.parametrize(
    ("side", "arm_index", "expected_arm"),
    (("harness", 0, "shipping_sage"), ("bare", 1, "true_naive")),
)
def test_both_arms_require_the_same_protocol_ceiling(side, arm_index, expected_arm):
    request = _valid_request(
        side=side,
        arm_index=arm_index,
        token_ceiling=gate1p_stagea.TOKEN_ADMISSION_CEILING,
    )
    assert gate1p_stagea.validate_request(**request) == expected_arm

    with pytest.raises(gate1p_stagea.ProtocolError, match="token_ceiling"):
        gate1p_stagea.validate_request(**{**request, "token_ceiling": 250_001})


def test_assembled_driver_defaults_to_the_frozen_shared_ceiling():
    cfg = run_gauge_live.Config(measurement_kind="row")
    invariants = gate1p_stagea.pair_invariants(
        provider=gate1p_stagea.MODEL_PROVIDER,
        model=gate1p_stagea.MODEL_ID,
        api_endpoint="https://provider.invalid/v1",
        source_version="tree:test",
        model_context_limit=200_000,
        max_output_tokens=8_192,
    )

    assert cfg.model_token_budget == gate1p_stagea.TOKEN_ADMISSION_CEILING
    assert invariants["token_admission_ceiling"] == gate1p_stagea.TOKEN_ADMISSION_CEILING
    assert invariants["token_budget_boundary"] == "completed_model_call_v1"


def _run_naive_crossing(*, ceiling):
    decisions = iter([
        {
            "tool": "noop",
            "args": {},
            "_usage": {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
        },
        {
            "final": "done",
            "_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    ])
    dispatched: list[dict] = []
    runner = bare_runner.BareModelRunner(
        lambda _system, _tools, _history: next(decisions),
        lambda call: dispatched.append(call) or {"ok": True},
        tools_spec=[],
        token_ceiling=ceiling,
        require_complete_usage=True,
    )
    return runner.run("cross the boundary"), dispatched


def _run_shipping_crossing(*, ceiling):
    async def decide(_request):
        return _CrossingResponse()

    return asyncio.run(policy.LLMPolicy(
        decide,
        provider="openai",
        model_id="test-model",
        catalog=[{"name": "candidate", "description": "candidate"}],
        completed_call_token_ceiling=ceiling,
    ).select(
        episode_id="episode-budget-symmetry",
        objective="cross the boundary",
        state=_State(),
        candidates=[_Action()],
        history=[],
    ))


def test_both_arms_suppress_the_boundary_crossing_action():
    naive, dispatched = _run_naive_crossing(ceiling=10)
    shipping = _run_shipping_crossing(ceiling=10)

    assert naive.stopped == "token_budget_overshoot"
    assert naive.model_calls == 1
    assert dispatched == []
    assert shipping.disposition == "stop"
    assert shipping.raw_disposition == "select"
    assert shipping.model_response_observed is True
    assert shipping.selected_capability == ""
    assert shipping.token_usage == {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11}


def test_negative_control_proves_removing_the_ceiling_releases_both_actions():
    naive, dispatched = _run_naive_crossing(ceiling=None)
    shipping = _run_shipping_crossing(ceiling=None)

    assert naive.stopped == "done"
    assert len(dispatched) == 1
    assert shipping.disposition == "select"
    assert shipping.selected_capability == "candidate"


def test_crossing_evidence_is_admitted_identically_after_both_runners_stop():
    record = {
        "response_id": "r1",
        "effective_backend": "openai:test-model",
        "backend_provenance_source": "response_metadata.model_name",
        "input_tokens": 9,
        "output_tokens": 2,
        "total_tokens": 11,
        "usage_complete": True,
    }
    dispositions = [
        gate1p_stagea.completed_call_budget_disposition(
            [record],
            model_calls=1,
            action_response_ids=[],
            token_ceiling=10,
            model_context_limit=100,
            max_output_tokens=10,
        )
        for _arm in gate1p_stagea.ARMS
    ]

    assert dispositions[0] == dispositions[1]
    assert dispositions[0]["countable"] is True
    assert dispositions[0]["boundary_reached"] is True
    assert dispositions[0]["overshoot"] is True
    assert dispositions[0]["terminal_response_id"] == "r1"


def test_scoring_function_has_no_arm_branch():
    signature = inspect.signature(gate1p_stagea.completed_call_budget_disposition)
    assert "arm" not in signature.parameters
