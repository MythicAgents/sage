"""
prompt_loader — externalized agent system prompts (ROADMAP Phase 3, T3.1-T3.4).

Each Sage agent's system prompt lives in `Payload_Type/sage/prompts/<agent>.md` as a
markdown file with a YAML frontmatter block:

    ---
    name: Mythic_Operator
    description: Drives all Mythic C2 operations and offensive tradecraft.
    tools:
      - get_all_active_callbacks
      - issue_task_and_waitfor_task_output
      - ...
    ---
    <the system prompt body>

The body is a Python ``str.format()`` template: runtime values are single-braced
placeholders (e.g. ``{commands_text}``) and any LITERAL brace in the prompt text MUST be
doubled (``{{`` / ``}}``) — exactly the same escaping a Python f-string uses, because these
bodies were lifted verbatim from the original f-strings in model.py.

Design notes (see Plans/PROMPT_FORMAT.md):
- Files are read at agent-build time (each ``_*_agent()`` call), so editing a prompt file +
  restarting Sage changes behavior with no code edit (T3.3).
- ``load_prompt`` renders the template and ``.strip()``s the result — this matches today's
  ``SystemMessage(content=prompt.strip())`` byte-for-byte; the only delta vs the old
  ``create_agent(system_prompt=prompt)`` sink is leading/trailing whitespace, which is inert.
- A malformed operator edit (stray single brace, wrong placeholder) does NOT crash Sage:
  rendering falls back to the raw body and logs at ERROR.
- Frontmatter is parsed from the LEADING ``---`` block only — prompt bodies routinely contain
  ``---`` markdown rules, so we never ``split('---')``.
"""

from pathlib import Path

try:  # match model.py's logger so warnings surface in Sage logs
    from mythic_container.logging import logger
except Exception:  # pragma: no cover - fallback for standalone/unit-test contexts
    import logging
    logger = logging.getLogger(__name__)

import yaml

# sage/ai/langgraph/prompt_loader.py -> sage/prompts  (CWD-independent)
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _read(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Parses ONLY the leading ---...--- block."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        for i in range(1, len(lines)):
            if lines[i].rstrip("\r\n") == "---":
                fm_text = "".join(lines[1:i])
                body = "".join(lines[i + 1:])
                try:
                    meta = yaml.safe_load(fm_text) or {}
                except Exception as e:  # pragma: no cover - defensive
                    logger.error(f"prompt_loader: frontmatter YAML parse failed: {e}")
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                return meta, body
    # No frontmatter present — whole file is body.
    return {}, text


def load_prompt(name: str, **subs) -> str:
    """Load, render, and strip an agent prompt.

    :param name: prompt file stem (e.g. "mythic_operator").
    :param subs: substitutions for single-braced placeholders in the body.
    :return: the rendered, stripped system prompt.
    """
    _, body = _split_frontmatter(_read(name))
    try:
        rendered = body.format(**subs)
    except (KeyError, IndexError, ValueError) as e:
        logger.error(
            f"prompt_loader: template render FAILED for '{name}' ({e}); using RAW body. "
            f"Check for an unescaped single brace (use '{{{{' / '}}}}') or a wrong placeholder name."
        )
        rendered = body
    return rendered.strip()


def load_prompt_meta(name: str) -> dict:
    """Return just the frontmatter dict for a prompt file."""
    meta, _ = _split_frontmatter(_read(name))
    return meta


def get_prompt_tools(name: str) -> list[str]:
    """Return the frontmatter 'tools' list (empty list if absent)."""
    tools = load_prompt_meta(name).get("tools") or []
    return [str(t) for t in tools]


def filter_tools_by_frontmatter(name: str, candidate_tools: list) -> list:
    """Keep only the candidate tools whose ``.name`` is listed in the prompt frontmatter.

    Order is preserved (iterates candidates, not the frontmatter list). Tools that are
    runtime-discovered and cannot be enumerated in frontmatter (e.g. MCP server tools)
    should NOT be passed here — keep them in a separate list and concatenate, e.g.::

        tools = mcp_tools + filter_tools_by_frontmatter("mcp_manager", static_tools)

    Logs a warning on any frontmatter↔code mismatch so ISC-31 (frontmatter matches actual
    tool set) is self-checking.
    """
    allowed = set(get_prompt_tools(name))
    candidate_names = {getattr(t, "name", None) for t in candidate_tools}

    for n in allowed:
        if n not in candidate_names:
            logger.warning(
                f"prompt_loader[{name}]: frontmatter lists tool '{n}' that is not in the code's "
                f"candidate tools — it will have no effect."
            )

    kept = []
    for t in candidate_tools:
        tn = getattr(t, "name", None)
        if tn in allowed:
            kept.append(t)
        else:
            logger.warning(
                f"prompt_loader[{name}]: tool '{tn}' present in code but absent from frontmatter "
                f"'tools' — REMOVED from this agent's tool set."
            )
    return kept
