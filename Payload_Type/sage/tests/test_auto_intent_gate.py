"""Regression: auto-mode initiation gate (bug 1, 2026-07-10).

THE BUG: in `auto` mode the deterministic AutonomousController was handed control on the FIRST
non-interactive turn regardless of message content — a bare "hello" launched offensive execution off
the pre-collected range state, with no objective. `_should_use_controller` had no content check; the
existing conservative objective detector `_looks_like_explicit_objective_prompt` was wired only to the
supervised-HITL path.

PRE-FIX BEHAVIOR (what these tests would have shown against the old code):
    m.mode = "auto"; m._autonomous_solve = True
    m._should_use_controller(is_interactive=False)            # old: True  (no prompt arg existed)
    -> the controller ran on ANY first turn, including "hello".

THE FIX: auto mode is DEFAULT-DENY — the controller initiates only when the turn reads as an explicit
engagement objective. A non-objective first turn (greeting, question, negative instruction) falls through
to normal conversational handling. The gate is deterministic on purpose (LLM guards are injectable/DoS-able;
a false negative costs a clarifying turn, a false positive fires offensive actions).

Uses object.__new__(Model) to exercise the unit without the heavy __init__ (sqlite + chat model).
Run: cd Payload_Type/sage && ../../.venv/bin/python -m pytest tests/test_auto_intent_gate.py -q
"""
import os
import sys
from pathlib import Path

from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph import model as model_mod  # noqa: E402
from ai.langgraph.model import Model, _coerce_prompt_text  # noqa: E402


def _auto_model() -> Model:
    """Bare autonomous-auto Model with the controller flag at its default (enabled)."""
    m = object.__new__(Model)
    m._autonomous_solve = True
    m.mode = "auto"
    m.command_name = "chat"
    return m


def _with_default_flags(fn):
    saved_c = os.environ.get("SAGE_AUTONOMOUS_CONTROLLER")
    saved_h = os.environ.get("SAGE_CONTROLLER_HITL")
    os.environ.pop("SAGE_AUTONOMOUS_CONTROLLER", None)
    os.environ.pop("SAGE_CONTROLLER_HITL", None)
    try:
        fn()
    finally:
        for k, v in (("SAGE_AUTONOMOUS_CONTROLLER", saved_c), ("SAGE_CONTROLLER_HITL", saved_h)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_greeting_does_not_initiate_controller_in_auto_mode():
    def _case():
        m = _auto_model()
        assert m._should_use_controller(is_interactive=False, prompt="hello") is False
        assert m._should_use_controller(is_interactive=False, prompt="hi there") is False
        assert m._should_use_controller(is_interactive=False, prompt="") is False
    _with_default_flags(_case)


def test_casual_greetings_use_terminal_tool_free_route():
    m = _auto_model()
    for prompt in ("hello", "Hello!", "hi there", "Hey Sage", "good morning"):
        assert m._looks_like_casual_greeting(prompt) is True
    for prompt in ("list callbacks", "hello and run whoami", "compromise the corp domain"):
        assert m._looks_like_casual_greeting(prompt) is False


def test_casual_greeting_emits_generalist_answer_to_main_chat():
    m = _auto_model()
    m.channel_id = 3
    m.state = {
        "messages": [],
        "supervisor_messages": [],
        "generalist_messages": [],
        "_message_seq": 0,
    }
    m._message_seq = 0
    m.verbose = True
    streamed = []

    async def _agent(state, config):
        answer = AIMessage(content="Hello! How can I help?", name="Generalist")
        return {
            "messages": [answer],
            "generalist_messages": [answer],
            "supervisor_messages": [],
        }

    async def _stream(message):
        streamed.append(message)
        return True

    m._generalist_agent = lambda: _agent
    m._graph_run_config = lambda thread_id: {}
    m._session_thread_id = lambda: "3"
    m._stream_message_to_mythic = _stream

    import asyncio
    asyncio.run(m._run_generalist_only_turn("hello"))

    assert streamed == ["Hello! How can I help?\n"]


def test_questions_and_negatives_do_not_initiate_in_auto_mode():
    def _case():
        m = _auto_model()
        assert m._should_use_controller(is_interactive=False, prompt="what callbacks are active?") is False
        assert m._should_use_controller(is_interactive=False, prompt="summarize the domain") is False
        assert m._should_use_controller(is_interactive=False, prompt="don't compromise anything") is False
    _with_default_flags(_case)


def test_explicit_objective_still_initiates_in_auto_mode():
    def _case():
        m = _auto_model()
        assert m._should_use_controller(
            is_interactive=False, prompt="compromise the north.sevenkingdoms.local domain") is True
        assert m._should_use_controller(
            is_interactive=False, prompt="achieve domain admin on sevenkingdoms.local") is True
        assert m._should_use_controller(
            is_interactive=False, prompt="obtain administrative control of child.lab.local") is True
    _with_default_flags(_case)


def test_interactive_followups_never_initiate_a_fresh_solve():
    def _case():
        m = _auto_model()
        # even with an objective, an interactive (reused-channel) turn falls through to the normal path
        assert m._should_use_controller(
            is_interactive=True, prompt="compromise the corp domain") is False
    _with_default_flags(_case)


def test_gate_handles_content_block_list_prompt_shape():
    """A prompt may arrive as a list of content blocks, not a plain string; the gate must normalize it."""
    def _case():
        m = _auto_model()
        obj = [{"type": "text", "text": "compromise the corp domain"}]
        greet = [{"type": "text", "text": "hello"}]
        assert m._should_use_controller(is_interactive=False, prompt=obj) is True
        assert m._should_use_controller(is_interactive=False, prompt=greet) is False
    _with_default_flags(_case)


def test_rollback_flag_disables_controller_regardless_of_objective():
    def _case():
        m = _auto_model()
        os.environ["SAGE_AUTONOMOUS_CONTROLLER"] = "0"
        assert m._should_use_controller(
            is_interactive=False, prompt="compromise the corp domain") is False
    _with_default_flags(_case)


def test_padded_and_leading_negation_do_not_initiate():
    """Cato finding: a bounded-window bypass ('please do not, <60 chars of filler>, compromise ...') must not
    fire the safety gate. The leading-prohibition check closes it (default-deny direction)."""
    def _case():
        m = _auto_model()
        assert m._should_use_controller(
            is_interactive=False,
            prompt="please do not, and I mean under no circumstances whatsoever right now, "
                   "compromise the corp domain",
        ) is False
        assert m._should_use_controller(
            is_interactive=False, prompt="do not compromise the corp domain") is False
        assert m._should_use_controller(
            is_interactive=False, prompt="never escalate to domain admin") is False
        # a genuine objective with NO leading prohibition still initiates
        assert m._should_use_controller(
            is_interactive=False, prompt="compromise the corp domain") is True
    _with_default_flags(_case)


def test_coerce_prompt_text_normalization():
    assert _coerce_prompt_text("hello") == "hello"
    assert _coerce_prompt_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a b"
    assert _coerce_prompt_text([{"type": "image", "url": "x"}]) == ""
    assert _coerce_prompt_text(None) == ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all auto-intent-gate tests passed")
