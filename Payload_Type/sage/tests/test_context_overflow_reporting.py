"""Context-window overflow is named for what it is (ISA 9F, ISC-43).

Sage's graph-execution handler catches everything and renders one generic operator message ending in
"adjust your approach". That advice is unactionable for a context overflow: the operator needs to know
the conversation outgrew the model, not that something unspecified failed.

The load-bearing test here is `test_provider_errors_are_context_overflow_subclasses`. The branch in
`model.py` is only reachable because langchain-openai and langchain-anthropic raise provider
subclasses of `ContextOverflowError`. If a future release stops doing that, the branch becomes dead
code that still looks correct, and this test is what notices.

Mirrors the repo's no-pytest-asyncio convention. Source-level assertions are used for the branch
itself because it lives inside `Model.invoke`'s exception handler, which cannot be driven without a
live model and Mythic client.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from langchain_core.exceptions import ContextOverflowError

SAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SAGE_ROOT))

MODEL_PY = SAGE_ROOT / "ai" / "langgraph" / "model.py"
MODEL_SOURCE = MODEL_PY.read_text(encoding="utf-8")


def test_provider_errors_are_context_overflow_subclasses():
    """The branch is reachable only while providers keep translating their own errors.

    Covers both paths Sage actually runs: the LiteLLM proxy (which is langchain-openai's ChatOpenAI)
    and native Anthropic.
    """
    from langchain_anthropic.chat_models import AnthropicContextOverflowError
    from langchain_openai.chat_models.base import (
        OpenAIAPIContextOverflowError,
        OpenAIContextOverflowError,
    )

    for exc_type in (
        OpenAIContextOverflowError,
        OpenAIAPIContextOverflowError,
        AnthropicContextOverflowError,
    ):
        assert issubclass(exc_type, ContextOverflowError), (
            f"{exc_type.__name__} no longer subclasses ContextOverflowError; the overflow branch in "
            "model.py is now dead code"
        )


def _translates_context_overflow(module_name: str) -> bool:
    """Does this provider package define its own ContextOverflowError subclass?"""
    import importlib

    root = Path(importlib.import_module(module_name).__file__).parent
    return any(
        "ContextOverflowError)" in path.read_text(encoding="utf-8", errors="ignore")
        for path in root.rglob("*.py")
    )


def test_provider_coverage_is_exactly_what_model_py_claims():
    """Pins which of the six installed providers actually translate, so the claim cannot go stale.

    Measured 2026-08-10: openai and anthropic do; aws, groq, ollama and xai do not. The covered pair
    is what matters in practice — Sage's configured path is provider `openai` pointed at a LiteLLM
    proxy, and native Anthropic is the other real one. The four uncovered providers fall through to
    the generic guidance, which is correct behaviour rather than a bug, but it must stay written down.

    The covered assertions double as the positive control: if this probe silently stopped seeing
    subclasses, they would fail rather than letting the "not covered" half read as reassuring.
    """
    covered = {"langchain_openai", "langchain_anthropic"}
    uncovered = {"langchain_aws", "langchain_groq", "langchain_ollama", "langchain_xai"}

    for name in sorted(covered):
        assert _translates_context_overflow(name), (
            f"{name} no longer translates context overflows; Sage's overflow guidance has silently "
            "stopped reaching a path it used to cover"
        )

    for name in sorted(uncovered):
        assert not _translates_context_overflow(name), (
            f"{name} now translates context overflows; model.py's comment listing it as uncovered is "
            "stale and the guidance should be widened"
        )


def _invoke_handler_source() -> str:
    tree = ast.parse(MODEL_SOURCE)
    invoke = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "invoke"
    )
    return ast.dump(invoke)


def test_invoke_branches_on_context_overflow():
    """The guidance must actually branch, not merely import the exception."""
    body = _invoke_handler_source()

    assert "ContextOverflowError" in body, (
        "Model.invoke no longer distinguishes a context overflow from any other failure"
    )
    assert "next_steps" in body, "the operator guidance is no longer parameterised by failure type"


def test_the_two_guidance_texts_are_actually_different():
    """A branch that renders identical text would pass the AST check and help nobody."""
    assert "outgrew the model's context window" in MODEL_SOURCE
    assert "Adjust your approach" in MODEL_SOURCE
    assert MODEL_SOURCE.count("**Next Steps:**") == 1, (
        "the error template was duplicated; both branches should feed one template"
    )


def test_overflow_guidance_does_not_blame_a_tool_or_target():
    """The whole point: an overflow is not a tool failure and must not read like one."""
    start = MODEL_SOURCE.index("outgrew the model's context window")
    guidance = MODEL_SOURCE[start : start + 400]

    assert "not a tool or target failure" in guidance
