"""Pure-function library for Sage's TTP and Mythic-agent knowledge files.

No Mythic dependency: every function here reads the markdown files under
``Payload_Type/sage/{ttps,mythic_agents}/`` so the parsing/matching logic can be
unit-tested offline. ``MythicTools`` wraps these with the runtime Mythic-agent
join and file-store upload (those parts need a live Mythic client).

Schemas:
  - TTP files          -> see the schema documented in this module
  - Mythic-agent files -> see the schema documented in this module

Progressive disclosure (mirrors Claude Code's SKILL.md pattern):
  - ``get_ttp_guidance``      returns frontmatter + body UP TO "## Full Reference"
  - ``get_ttp_full_reference`` returns ONLY the "## Full Reference" section
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import datetime
import yaml

# Stdlib logger only — this module stays dependency-free so it can be unit-tested
# without Mythic. A single malformed TTP file (autonomous-authored or operator-edited
# via the Mythic paperclip) must never take down guidance for every other file.
logger = logging.getLogger(__name__)

# This file lives at Payload_Type/sage/ai/langgraph/ttp_library.py.
# parents[2] == Payload_Type/sage/ (the per-payload-type root that Mythic mounts).
_SAGE_ROOT = Path(__file__).resolve().parents[2]
TTP_DIR = _SAGE_ROOT / "ttps"
MYTHIC_AGENTS_DIR = _SAGE_ROOT / "mythic_agents"
TOOLS_DIR = _SAGE_ROOT / "tools"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_FULL_REFERENCE_HEADING = "## Full Reference"


def _json_safe(obj: Any) -> Any:
    """Recursively convert YAML date/datetime values to ISO strings.

    YAML safe_load turns unquoted dates like `last_updated: 2026-05-29` into
    datetime.date objects, which json.dumps cannot serialize. Frontmatter flows into
    tool responses (get_ttp_guidance) and the LangGraph state checkpoint, so we
    sanitize at parse time to guarantee it is always JSON-serializable.
    (datetime is a subclass of date — check it first.)
    """
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def parse_markdown(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown doc into (frontmatter dict, body str). No frontmatter -> ({}, text)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    frontmatter = yaml.safe_load(match.group(1)) or {}
    if not isinstance(frontmatter, dict):
        frontmatter = {}
    return _json_safe(frontmatter), match.group(2)


def load_ttp(slug: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load one TTP file by slug. Returns (frontmatter, body) or (None, None) if missing/malformed."""
    path = TTP_DIR / f"{slug}.md"
    if not path.is_file():
        return None, None
    try:
        return parse_markdown(path.read_text(encoding="utf-8"))
    except Exception as exc:  # malformed YAML frontmatter, etc.
        logger.warning("Failed to parse TTP file %s: %s", path.name, exc)
        return None, None


def guidance_body(body: str) -> str:
    """Body up to (not including) the '## Full Reference' section."""
    idx = body.find(_FULL_REFERENCE_HEADING)
    return (body[:idx] if idx != -1 else body).rstrip()


def full_reference(body: str) -> str:
    """The '## Full Reference' section only, stopping at the next '## ' heading. '' if absent."""
    idx = body.find(_FULL_REFERENCE_HEADING)
    if idx == -1:
        return ""
    after_heading = body[idx + len(_FULL_REFERENCE_HEADING):]
    next_heading = re.search(r"\n##\s", after_heading)
    end = next_heading.start() if next_heading else len(after_heading)
    return (_FULL_REFERENCE_HEADING + after_heading[:end]).rstrip()


def iter_ttps():
    """Yield (slug, frontmatter, body) for every TTP file (skips _-prefixed files)."""
    if not TTP_DIR.is_dir():
        return
    for path in sorted(TTP_DIR.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            frontmatter, body = parse_markdown(path.read_text(encoding="utf-8"))
        except Exception as exc:  # one malformed file must not break the whole catalog
            logger.warning("Skipping malformed TTP file %s: %s", path.name, exc)
            continue
        yield path.stem, frontmatter, body


def list_categories() -> dict[str, list[dict[str, str]]]:
    """{category: [{slug, name}, ...]} across all TTP files."""
    catalog: dict[str, list[dict[str, str]]] = {}
    for slug, frontmatter, _ in iter_ttps():
        category = frontmatter.get("category", "uncategorized")
        catalog.setdefault(category, []).append(
            {"slug": slug, "name": frontmatter.get("name", slug)}
        )
    return catalog


def _haystack(slug: str, frontmatter: dict[str, Any]) -> str:
    """Lowercased searchable text for a TTP: slug, name, category, tags, example descriptions."""
    parts: list[str] = [slug, frontmatter.get("name", ""), frontmatter.get("category", "")]
    parts += frontmatter.get("subcategories", []) or []
    parts += frontmatter.get("tradecraft_tags", []) or []
    for example in frontmatter.get("usage_examples", []) or []:
        if isinstance(example, dict):
            parts.append(example.get("description", ""))
    return " ".join(str(p) for p in parts).lower()


_NAME_MATCH_BONUS = 100


def match_goal(goal: str, limit: int = 3) -> list[tuple[str, int]]:
    """Rank TTPs by keyword overlap with the goal text. Returns [(slug, score), ...] desc.

    A TTP the goal *explicitly names* (its slug or name appears as a goal token, or its
    multi-word name appears verbatim in the goal) gets a decisive bonus. Without this,
    keyword counting alone lets a tool that merely *mentions* another in its tags/examples
    (e.g. RustHound's `sharphound-alternative` tag) tie or beat the named tool — and stable
    sort then favors whichever sorts first alphabetically. The bonus makes "run SharpHound"
    return the `sharphound` TTP, not `rusthound`.
    """
    goal_lower = goal.lower()
    tokens = {t for t in re.split(r"\W+", goal_lower) if len(t) > 2}
    scored: list[tuple[str, int]] = []
    for slug, frontmatter, _ in iter_ttps():
        if not frontmatter:
            continue
        haystack = _haystack(slug, frontmatter)
        score = sum(1 for token in tokens if token in haystack)
        if not score:
            continue
        name = str(frontmatter.get("name", "")).lower()
        named = (
            slug.lower() in tokens
            or (name and name in tokens)
            or (name and len(name) > 3 and name in goal_lower)
        )
        if named:
            score += _NAME_MATCH_BONUS
        scored.append((slug, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def load_mythic_agent(payload_type: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Load mythic_agents/<payload_type>.md. Returns (frontmatter, body) or (None, None)."""
    if not payload_type:
        return None, None
    path = MYTHIC_AGENTS_DIR / f"{payload_type.lower()}.md"
    if not path.is_file():
        return None, None
    try:
        return parse_markdown(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse Mythic-agent file %s: %s", path.name, exc)
        return None, None


def execution_hint(ttp_frontmatter: dict[str, Any] | None, agent_frontmatter: dict[str, Any] | None) -> str:
    """Describe how to run a TTP's binary_type on a given Mythic agent (the C2-agnostic join)."""
    if not ttp_frontmatter or not agent_frontmatter:
        return ""
    binary_type = ttp_frontmatter.get("binary_type")
    agent_name = agent_frontmatter.get("name", "the agent")
    entry = (agent_frontmatter.get("binary_type_execution") or {}).get(binary_type)
    if not entry:
        return f"No execution mapping for binary_type '{binary_type}' on {agent_name}."
    command = entry.get("command")
    if command is None:
        return f"{agent_name} cannot run '{binary_type}' directly. Fallback: {entry.get('fallback', 'n/a')}"
    lines = [f"On {agent_name}, run this {binary_type} via the `{command}` command."]
    if entry.get("upload_required"):
        binary = ttp_frontmatter.get("binary_filename", "the binary")
        lines.append(
            f"`{binary}` must be in Mythic's file store first "
            f"(ensure_tool_uploaded / get_all_uploaded_files), then pass its file UUID."
        )
    template = entry.get("parameters_template")
    if template:
        lines.append(f"Parameter shape: {template}")
    return "\n".join(lines)
