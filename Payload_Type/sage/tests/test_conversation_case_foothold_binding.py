"""The frozen corpus must not name a range-specific callback ID.

Case text is supposed to be immutable, but the corpus outlived four rebindings —
``callback 7 -> 1 -> 3 -> 4``. Each was an edit to frozen text forced by the environment moving:
a Ludus rollback or a foothold rebuild silently invalidated the case. ISC-49R S4-01 was launched
against a callback that had been dead for hours, and the resulting livelock was initially
misdiagnosed as a kernel defect.

The corpus now carries a symbol and the runner binds it at freeze time. These tests keep it that
way: the anti-rot test below fails the moment a literal callback number reappears in case text.
"""

from __future__ import annotations

import re

import pytest

from tests.conversation_contract.cases import (
    CASES,
    FOOTHOLD_TOKEN,
    ConversationCase,
    render_prompt,
    requires_foothold,
)

#: "callback 4", "callback  12", "Callback 7" — a bare numeric target in frozen text.
LITERAL_CALLBACK = re.compile(r"\bcallback\s+\d+", re.IGNORECASE)


def _case(prompt: str) -> ConversationCase:
    return ConversationCase(
        "C99-test", "test", prompt, "authority", "complete",
        ("request.terminal",), (),
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_no_case_text_names_a_literal_callback_id(case: ConversationCase):
    """The anti-rot guard. A literal callback number rots the moment the range changes."""
    for field in ("prompt", "stored_objective", "pending_objective"):
        text = getattr(case, field)
        assert not LITERAL_CALLBACK.search(text), (
            f"{case.case_id}.{field} names a literal callback: {text!r}. "
            f"Use {FOOTHOLD_TOKEN} and bind it at freeze time with render_prompt()."
        )


def test_render_prompt_binds_the_live_foothold():
    case = _case(f"Run whoami on callback {FOOTHOLD_TOKEN}.")
    assert render_prompt(case, foothold=4) == "Run whoami on callback 4."
    assert render_prompt(case, foothold="12") == "Run whoami on callback 12."


def test_render_prompt_binds_every_occurrence():
    case = _case(f"Use callback {FOOTHOLD_TOKEN}, then re-check callback {FOOTHOLD_TOKEN}.")
    assert render_prompt(case, foothold=4) == "Use callback 4, then re-check callback 4."


def test_render_prompt_leaves_the_placeholder_nowhere():
    """A literal placeholder reaching Sage would be a nonsense prompt, not a bounded action."""
    for case in CASES:
        assert FOOTHOLD_TOKEN not in render_prompt(case, foothold=4)


def test_render_prompt_rejects_an_empty_binding():
    case = _case(f"Run whoami on callback {FOOTHOLD_TOKEN}.")
    for empty in ("", "   "):
        with pytest.raises(ValueError):
            render_prompt(case, foothold=empty)


def test_render_prompt_is_a_no_op_without_the_token():
    case = _case("Hello Sage.")
    assert render_prompt(case, foothold=4) == "Hello Sage."


def test_requires_foothold_identifies_the_cases_that_need_a_live_target():
    needing = {case.case_id for case in CASES if requires_foothold(case)}
    assert needing, "at least one case targets a callback"
    for case in CASES:
        assert requires_foothold(case) == (FOOTHOLD_TOKEN in case.prompt)
