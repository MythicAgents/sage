"""Regression: empty `system` content block crashes Bedrock (bug 2, 2026-07-10).

THE BUG: `ValidationException: system: text content blocks must be non-empty`.
In chat mode `system_prompt` defaults to "" (sage_chat/config.py) and `Model.__init__` seeded
`SystemMessage(content="")` as the FIRST message of the supervisor channel. `_sanitize_messages` KEPT
that empty first system message, so an empty `system` block reached Bedrock. The only sanitizer,
`_apply_bedrock_patch`, (a) rescued empty content only for role=="assistant" (never an empty system
string) and (b) monkeypatched langchain_openai, which the native `init_chat_model(model_provider="bedrock")`
(langchain-aws) provider never touches — so it was a no-op on the live path.

PRE-FIX BEHAVIOR: `_sanitize_messages([SystemMessage(content=""), HumanMessage("hi")])` returned the
empty SystemMessage unchanged (kept as the first system message) -> empty `system` forwarded -> crash.

THE FIX (provider-agnostic, at the message boundary):
  1. `_nonempty_system` normalizes a blank top-level system prompt to a minimal default at construction.
  2. `_sanitize_messages` keeps only the first NON-EMPTY SystemMessage and drops blank ones (Bedrock treats
     `system` as optional, so dropping a blank entirely is valid); blank text blocks in list content stripped.

Run: cd Payload_Type/sage && ../../.venv/bin/python -m pytest tests/test_empty_system_sanitize.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph.model import (  # noqa: E402
    Model, _nonempty_system, _content_has_text, _strip_blank_text_blocks, _DEFAULT_SYSTEM_PROMPT,
)
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage  # noqa: E402


def _bare() -> Model:
    return object.__new__(Model)


# ---- construction-site guard --------------------------------------------------------------------
def test_nonempty_system_backfills_blank():
    assert _nonempty_system("") == _DEFAULT_SYSTEM_PROMPT
    assert _nonempty_system("   \n\t ") == _DEFAULT_SYSTEM_PROMPT
    assert _nonempty_system(None) == _DEFAULT_SYSTEM_PROMPT
    assert _nonempty_system("You are Sage. Do X.") == "You are Sage. Do X."
    # the guard never yields a blank SystemMessage
    assert SystemMessage(content=_nonempty_system("")).content.strip() != ""


# ---- sanitizer: drop the empty system block -----------------------------------------------------
def test_sanitize_drops_empty_system_message():
    m = _bare()
    out = m._sanitize_messages([SystemMessage(content=""), HumanMessage(content="hi")])
    systems = [x for x in out if isinstance(x, SystemMessage)]
    assert systems == []  # the empty system block is dropped entirely (system is optional to Bedrock)
    assert any(isinstance(x, HumanMessage) for x in out)


def test_sanitize_drops_whitespace_only_system_message():
    m = _bare()
    out = m._sanitize_messages([SystemMessage(content="  \n\t "), HumanMessage(content="hi")])
    assert [x for x in out if isinstance(x, SystemMessage)] == []


def test_sanitize_keeps_first_nonempty_system_message():
    m = _bare()
    out = m._sanitize_messages([SystemMessage(content="You are Sage."), HumanMessage(content="hi")])
    systems = [x for x in out if isinstance(x, SystemMessage)]
    assert len(systems) == 1
    assert systems[0].content == "You are Sage."


def test_sanitize_skips_empty_then_keeps_real_system_message():
    m = _bare()
    out = m._sanitize_messages([
        SystemMessage(content=""),            # empty -> dropped, does not consume the "first system" slot
        SystemMessage(content="Real prompt"),  # this one must survive
        HumanMessage(content="hi"),
    ])
    systems = [x for x in out if isinstance(x, SystemMessage)]
    assert len(systems) == 1 and systems[0].content == "Real prompt"


def test_sanitize_strips_blank_text_blocks_in_list_system_content():
    m = _bare()
    content = [{"type": "text", "text": ""}, {"type": "text", "text": "real"}]
    out = m._sanitize_messages([SystemMessage(content=content), HumanMessage(content="hi")])
    systems = [x for x in out if isinstance(x, SystemMessage)]
    assert len(systems) == 1
    assert all(str(b.get("text", "")).strip() for b in systems[0].content)


def test_anti_no_empty_system_block_survives_sanitize():
    """Anti-criterion: after sanitize, no SystemMessage carries blank content."""
    m = _bare()
    cases = [
        [SystemMessage(content=""), HumanMessage(content="hi")],
        [SystemMessage(content="   "), HumanMessage(content="hi")],
        [SystemMessage(content=[{"type": "text", "text": ""}]), HumanMessage(content="hi")],
        [SystemMessage(content="ok"), HumanMessage(content="hi")],
    ]
    for msgs in cases:
        out = m._sanitize_messages(msgs)
        for x in out:
            if isinstance(x, SystemMessage):
                assert _content_has_text(x.content), f"empty system survived: {x.content!r}"


# ---- helper units ------------------------------------------------------------------------------
def test_sanitize_preserves_cache_control_on_kept_system_block():
    """Anthropic-on-Bedrock prompt caching rides on cache_control breakpoints; stripping blank text blocks must
    not drop cache_control on a REAL text block (Forge finding)."""
    m = _bare()
    content = [{"type": "text", "text": "You are Sage", "cache_control": {"type": "ephemeral"}}]
    out = m._sanitize_messages([SystemMessage(content=content), HumanMessage(content="hi")])
    systems = [x for x in out if isinstance(x, SystemMessage)]
    assert len(systems) == 1
    assert systems[0].content[0].get("cache_control") == {"type": "ephemeral"}


def test_content_has_text_counts_typeless_dict_with_text():
    """A dict block carrying non-blank text counts even without an explicit 'type' (agrees with
    _strip_blank_text_blocks, which only drops type=='text' blank blocks)."""
    assert _content_has_text([{"text": "hello"}]) is True
    assert _content_has_text([{"text": ""}]) is False


def test_nonempty_system_falls_back_on_non_string():
    """A non-str (e.g. list content passed by mistake) must NOT str()-ify to garbage — it falls to the default."""
    assert _nonempty_system([{"type": "text", "text": "x"}]) == _DEFAULT_SYSTEM_PROMPT
    assert _nonempty_system({"text": "x"}) == _DEFAULT_SYSTEM_PROMPT


def test_content_has_text():
    assert _content_has_text("hi") is True
    assert _content_has_text("") is False
    assert _content_has_text("  \n ") is False
    assert _content_has_text([{"type": "text", "text": "x"}]) is True
    assert _content_has_text([{"type": "text", "text": ""}]) is False
    assert _content_has_text([{"type": "tool_use", "name": "t"}]) is True  # non-text block counts


def test_strip_blank_text_blocks():
    assert _strip_blank_text_blocks([{"type": "text", "text": ""}, {"type": "text", "text": "y"}]) == \
        [{"type": "text", "text": "y"}]
    # dropping everything -> return original (caller decides)
    only_blank = [{"type": "text", "text": ""}]
    assert _strip_blank_text_blocks(only_blank) == only_blank
    assert _strip_blank_text_blocks("plain") == "plain"


def test_sanitize_preserves_assistant_message_behavior_unchanged():
    """Existing rescue path for assistant content is out of scope for this fix — do not alter it."""
    m = _bare()
    ai = AIMessage(content="did the thing")
    out = m._sanitize_messages([SystemMessage(content="s"), HumanMessage(content="hi"), ai])
    # AIMessage content is preserved verbatim; a trailing-AI nudge is appended (provider needs a user turn)
    assert any(isinstance(x, AIMessage) and x.content == "did the thing" for x in out)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all empty-system-sanitize tests passed")
