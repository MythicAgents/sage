"""Regression: controller initiation is owned by typed transport/session state.

Prompt shape, wording, content-block encoding, negation, and channel reuse are inert at this
boundary. Casual greetings still terminate through the earlier tool-free route in ``Model.invoke``.
"""
import os
import sys
from pathlib import Path

from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph import model as model_mod  # noqa: E402
from ai.langgraph.model import Model, _coerce_prompt_text  # noqa: E402
from ai.langgraph.request_contract import build_request_contract  # noqa: E402
from ai.langgraph.turn_authority import authority_from_request_contract  # noqa: E402


def _typed_model(mode: str, *, autonomous_solve: bool = False) -> Model:
    m = object.__new__(Model)
    m._autonomous_solve = autonomous_solve
    m.mode = mode
    m.command_name = "chat"
    m._supervised_objective_active = False
    m._request_contract = build_request_contract(
        request_id=f"{mode}-{autonomous_solve}",
        channel_id="channel",
        operation_id="operation",
        mode=mode,
        autonomous_solve=autonomous_solve,
    )
    m._turn_authority = authority_from_request_contract(m._request_contract)
    return m


def _auto_model() -> Model:
    return _typed_model("auto")


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


def test_auto_lane_authority_is_independent_of_greeting_prose():
    def _case():
        m = _auto_model()
        assert m._should_use_controller(is_interactive=False, prompt="hello") is True
        assert m._should_use_controller(is_interactive=False, prompt="hi there") is True
        assert m._should_use_controller(is_interactive=False, prompt="") is True
        conversation = _typed_model("conversation")
        assert conversation._should_use_controller(
            is_interactive=False,
            prompt="compromise the domain",
        ) is False
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


def test_questions_and_negatives_cannot_change_typed_lane():
    def _case():
        m = _auto_model()
        assert m._should_use_controller(is_interactive=False, prompt="what callbacks are active?") is True
        assert m._should_use_controller(is_interactive=False, prompt="summarize the domain") is True
        assert m._should_use_controller(is_interactive=False, prompt="don't compromise anything") is True
        supervised = _typed_model("supervised")
        assert supervised._should_use_controller(
            is_interactive=False,
            prompt="compromise everything",
        ) is False
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


def test_explicit_remote_exec_proof_objective_initiates_in_auto_mode():
    """All prose variants share the same disposition under one typed autonomous lane."""
    def _case():
        m = _auto_model()
        assert m._should_use_controller(
            is_interactive=False,
            prompt="From the current foothold, prove bounded remote execution on EAST-OPS01.",
        ) is True
        assert m._should_use_controller(
            is_interactive=False,
            prompt="How would you prove bounded remote execution on EAST-OPS01?",
        ) is True
        assert m._should_use_controller(
            is_interactive=False,
            prompt="Avoid proving bounded remote execution on EAST-OPS01.",
        ) is True
    _with_default_flags(_case)


def test_reused_channel_flag_cannot_change_typed_auto_lane():
    def _case():
        m = _auto_model()
        assert m._should_use_controller(
            is_interactive=True, prompt="compromise the corp domain") is True
    _with_default_flags(_case)


def test_content_block_encoding_cannot_change_typed_lane():
    def _case():
        m = _auto_model()
        obj = [{"type": "text", "text": "compromise the corp domain"}]
        greet = [{"type": "text", "text": "hello"}]
        assert m._should_use_controller(is_interactive=False, prompt=obj) is True
        assert m._should_use_controller(is_interactive=False, prompt=greet) is True
    _with_default_flags(_case)


def test_rollback_flag_disables_controller_regardless_of_objective():
    def _case():
        m = _auto_model()
        os.environ["SAGE_AUTONOMOUS_CONTROLLER"] = "0"
        assert m._should_use_controller(
            is_interactive=False, prompt="compromise the corp domain") is False
    _with_default_flags(_case)


def test_padded_and_leading_negation_cannot_change_typed_lane():
    def _case():
        m = _auto_model()
        assert m._should_use_controller(
            is_interactive=False,
            prompt="please do not, and I mean under no circumstances whatsoever right now, "
                   "compromise the corp domain",
        ) is True
        assert m._should_use_controller(
            is_interactive=False, prompt="do not compromise the corp domain") is True
        assert m._should_use_controller(
            is_interactive=False, prompt="never escalate to domain admin") is True
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
