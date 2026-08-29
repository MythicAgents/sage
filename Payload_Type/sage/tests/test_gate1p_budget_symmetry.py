"""Gate 1P budget symmetry: a comparison is only valid if both arms run under the same constraint.

WHY THIS FILE EXISTS
--------------------
Pair 024, the campaign's only comparison-countable pair, recorded:

    shipping_sage   capability 1.000   2 calls,      3,203 tokens, 8 tasks
    true_naive      capability 0.000   token_budget_overshoot at 260,079 tokens

The winning arm used 1/81st of what the losing arm was allowed to burn before being killed. The
naive arm did not attempt the objective and fail; it was terminated by a resource cap. A
right-censored observation was then scored as a failure event, which are different objects.

An external review found the same pattern across the campaign: every documented arm-specific loss
removed a BARE-MODEL trial (Pair 022 budget termination at 13 calls / 254,163 tokens; Pair 023 the
same class; Pair 018 never produced a mate). Under arm-correlated censoring the estimate is biased
toward the hypothesis being promoted.

The mechanism is structural, not chance. `TOKEN_ADMISSION_CEILING` is an aggregate cap over a
sequence of model completions. That is the bare runner's execution shape by construction: its whole
disadvantage is that it has no planner, no capability catalog and no deterministic builders, so it
must explore in raw completions. The harness arm executes capability plans against Mythic instead.
A cap written in one arm's shape does not censor at random; it censors that arm. No sample size
fixes it.

WHAT THIS TEST ASSERTS, AND WHY IT FAILS TODAY
---------------------------------------------
`test_scoring_function_is_arm_agnostic` passes: the disposition function itself is symmetric.

`test_both_arms_run_under_the_same_ceiling` FAILS, deliberately, and documents the defect. As of
2026-08-29 there is exactly one `TokenBudget` construction in `ai/hillclimb/` and it is inside
`BareModelRunner`. No shipping-arm runner accepts a `token_ceiling` at all, and no shipping-arm
runner module exists. Only one of the two arms can be stopped by the budget.

This is a failing test on purpose. It is the falsifier for "is Gate 1P salvageable as specified,"
and it is written as a test rather than a document so it cannot quietly stop being true. Delete it
only when the budget is genuinely symmetric, never to make the suite green.

HOW TO MAKE IT PASS (three options, in preference order)
--------------------------------------------------------
1. Remove the cap as a TERMINATION trigger for both arms. Let each arm reach a self-determined
   terminal state (success or give-up) and record token spend as a separate cost coordinate. This
   is the option the review preferred: the 81x spread is the largest real effect in the dataset and
   the binary metric threw it away.
2. Cap on a resource both arms consume comparably -- objective-directed action count, or wall
   clock -- and record a cap hit as `burned` (a censored observation), never as capability 0.0.
   The ISA's own vocabulary already has the right word.
3. Keep the token cap but enforce it identically on both arms, including Sage's own LLM calls.

Until one of those lands, no pair is countable under this budget spec and Pair 024's +1.000 delta
may not be promoted to a comparison result.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from ai.hillclimb import bare_runner, gate1p_stagea

CEILING = gate1p_stagea.TOKEN_ADMISSION_CEILING


def _usage(*totals: int) -> list[dict]:
    """A response stream with the given per-call total_tokens."""
    return [
        {
            "response_id": f"resp-{index}",
            "total_tokens": total,
            "input_tokens": total // 2,
            "output_tokens": total - total // 2,
        }
        for index, total in enumerate(totals)
    ]


def test_ceiling_is_the_single_declared_constant() -> None:
    """Floor assertion: if this drifts, every other assertion here is measuring the wrong thing."""
    assert CEILING == 250_000
    assert gate1p_stagea.ARMS == ("shipping_sage", "true_naive")


def test_scoring_function_is_arm_agnostic() -> None:
    """The disposition function is symmetric. The defect is in enforcement, not in scoring."""
    signature = inspect.signature(gate1p_stagea.completed_call_budget_disposition)
    assert "arm" not in signature.parameters, (
        "completed_call_budget_disposition must not branch on arm; if it grows an arm parameter, "
        "the asymmetry has moved into the scorer as well."
    )


@pytest.mark.parametrize("arm", gate1p_stagea.ARMS)
def test_both_arms_run_under_the_same_ceiling(arm: str) -> None:
    """FAILS for shipping_sage by design. See the module docstring.

    A comparison in which only one arm can be terminated by the budget is not a measurement of the
    treatment; it is a measurement of which arm the budget was shaped around.
    """
    hillclimb_dir = Path(bare_runner.__file__).parent
    constructions = {
        path.name
        for path in hillclimb_dir.glob("*.py")
        if "TokenBudget(" in path.read_text(errors="replace")
    }

    runners_with_a_ceiling = {
        name: obj
        for name, obj in vars(bare_runner).items()
        if inspect.isclass(obj)
        and "token_ceiling" in inspect.signature(obj.__init__).parameters
    }

    if arm == "true_naive":
        assert "bare_runner.py" in constructions
        assert runners_with_a_ceiling, "the naive arm lost its budget enforcement"
        return

    assert constructions - {"bare_runner.py"}, (
        f"arm {arm!r} runs under no token budget. Exactly one module constructs a TokenBudget "
        f"({sorted(constructions)}), and it enforces the ceiling only on the bare model. "
        f"Pair 024: shipping_sage completed on 3,203 tokens while true_naive was terminated at "
        f"260,079 -- an 81x spread in what each arm was permitted to spend. Every documented "
        f"arm-specific loss in the campaign removed a bare-model trial, so the censoring is "
        f"arm-correlated and biased toward the hypothesis under test. Fix the budget, not this "
        f"test; see this module's docstring for the three accepted options."
    )
