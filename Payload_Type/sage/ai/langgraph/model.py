import copy
import json
import ntpath
import os
import re
import asyncio
import aiosqlite
from dataclasses import dataclass, replace
from langgraph.graph import StateGraph, START, MessagesState, END
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt import tools_condition
from langgraph.managed.is_last_step import RemainingSteps
from langchain.agents import create_agent
from langchain.agents.middleware import ContextEditingMiddleware, ClearToolUsesEdit, SummarizationMiddleware, HumanInTheLoopMiddleware, InterruptOnConfig, AgentMiddleware, hook_config
from langgraph.types import Command
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables.config import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, BaseMessage, AnyMessage
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import ChatGeneration, LLMResult
from mythic_container.logging import logger
from mythic_container.MythicRPC import (
    MythicRPCResponseCreateMessage,
    SendMythicRPCResponseCreate,
    MythicRPCTaskUpdateMessage,
    SendMythicRPCTaskUpdate
)
from typing import Any, Awaitable, Callable, Literal
from typing_extensions import NotRequired
from uuid import UUID, uuid4
from .mythic_tools import MythicTools, GUARDED_TOOLS
from .tool_cache import ToolCache
from .prompt_loader import load_prompt, load_prompt_meta, filter_tools_by_frontmatter
from . import prompt_context
from ai.mcp import MCPManager

# Import logging fix - handle both relative and absolute imports
try:
    from .logging_fix import ensure_logger_initialized, force_flush_all_handlers
except ImportError:
    from logging_fix import ensure_logger_initialized, force_flush_all_handlers
from typing import Annotated
from langchain_core.tools import tool
from langchain.tools import ToolRuntime
from langgraph.errors import GraphRecursionError
import operator

SUPERVISOR_COPY_TOOL_RESULT_CAP = 2000  # max chars of a ToolMessage's string content when copied to OTHER agents' channels
_AUTONOMOUS_OPERATOR_CONTINUE_CAP = 6  # max autonomous re-invocations of Mythic_Operator per node entry
# Halt the autonomous solve after this many consecutive capability steps that add NO new achieved effect
# (the ledger doesn't grow) — a stall detector so a dead/unsatisfiable hop (e.g. a dcsync that keeps failing)
# halts with a report instead of looping forever and burning tokens.
_AUTONOMOUS_STALL_LIMIT = 6
_DEFAULT_GRAPH_RECURSION_LIMIT = 250
_TOON_SENTINEL = "⟦TOON "
_TRUNCATION_MARKER = "[truncated"
_COMPACTION_PROTECTED_TOOLS = frozenset((
    "summarize_and_handback", "request_continuation", "respond_to_user",
    "transfer_to_Supervisor", "transfer_to_Generalist", "transfer_to_Mythic_Operator",
    "transfer_to_Mythic_Payload", "transfer_to_BloodHound", "transfer_to_MCP_Manager",
))


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def _controller_flag_enabled() -> bool:
    """Use deterministic control by default for autonomous solves, with an explicit legacy-path rollback."""
    value = os.environ.get("SAGE_AUTONOMOUS_CONTROLLER")
    if value is None or not value.strip():
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


def _controller_hitl_flag_enabled() -> bool:
    """Use controller-native HITL by default for supervised autonomous chat, with an explicit rollback."""
    value = os.environ.get("SAGE_CONTROLLER_HITL")
    if value is None or not value.strip():
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


# Minimal non-empty fallback for a top-level system prompt. Anthropic-on-Bedrock rejects an empty/blank
# `system` content block with `ValidationException: system: text content blocks must be non-empty`, and the
# system field is optional — so a blank must be normalized (or omitted) before it reaches the provider. This
# is the construction-site guard; _sanitize_messages is the belt-and-suspenders drop at the invoke boundary.
_DEFAULT_SYSTEM_PROMPT = "You are Sage, an autonomous offensive security operator."


def _nonempty_system(text: Any) -> str:
    """Return a non-blank system prompt: the given text if it carries content, else a minimal default.

    Only a real string counts as content — a non-str (e.g. a list of content blocks passed by mistake) would
    otherwise str()-ify to non-empty garbage, so it falls back to the default instead."""
    t = (text if isinstance(text, str) else "").strip()
    return t if t else _DEFAULT_SYSTEM_PROMPT


def _coerce_prompt_text(prompt: Any) -> str:
    """Normalize a chat turn to plain text for the deterministic objective gate.

    A prompt is usually a str, but LangChain content can be a list of blocks ([{'type':'text','text':...}]).
    The objective detector is a regex over text, so join the text parts and ignore non-text blocks rather
    than str()-ing a list (which would smuggle '[{...}]' into the match)."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parts = []
        for b in prompt:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(parts)
    return str(prompt or "")


def _content_has_text(content: Any) -> bool:
    """True if a message's content carries at least one non-blank piece of text or a non-text block.

    Used to decide whether a SystemMessage is 'real' or an empty block that would trip Bedrock's
    'text content blocks must be non-empty' validation."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                # A dict block with non-blank text counts regardless of an explicit "type" (keeps this in
                # agreement with _strip_blank_text_blocks, which only drops type=="text" blank blocks).
                if str(b.get("text", "")).strip():
                    return True
                if b.get("type") and b.get("type") != "text":
                    return True  # tool_use / image / etc. count as content
            elif isinstance(b, str) and b.strip():
                return True
        return False
    return content is not None


def _strip_blank_text_blocks(content: Any) -> Any:
    """Drop empty/whitespace-only text blocks from list content, preserving non-text blocks.

    Returns the original content unchanged if it is not a list, or if stripping would remove every block
    (the caller decides what to do with a now-empty message). Mirrors _fix_payload_empty_content's list
    handling so the langchain-aws provider path — which never touches the langchain_openai patch — is also
    protected against blank text blocks."""
    if not isinstance(content, list):
        return content
    kept = [
        b for b in content
        if not (isinstance(b, dict) and b.get("type") == "text" and not str(b.get("text", "")).strip())
    ]
    return kept if kept else content


def _sanitize_model_messages(msgs: list) -> tuple[list, bool]:
    """Normalize a message list so NO empty/blank content block reaches any provider. Returns (messages, changed).

    Scope is the empty-block class ONLY — drop empty SystemMessages, strip blank text blocks from list content,
    and backfill an AIMessage left with no textual content (empty string, or an assistant turn carrying only a
    tool_use block would still be valid, but an empty text block alongside it is not). It deliberately does NOT
    reorder or drop tool_use/tool_result pairs — that is `_sanitize_messages`' job on the channel path. This is
    the provider-agnostic core shared by `_MessageSanitizerMiddleware`, which reaches create_agent's internal
    react loop where `_sanitize_messages` and the langchain_openai monkeypatch cannot."""
    out: list = []
    changed = False
    for m in msgs:
        content = getattr(m, "content", None)
        # Drop an empty/blank top-level system block (Bedrock: "system: text content blocks must be non-empty";
        # system is optional so dropping is valid).
        if isinstance(m, SystemMessage) and not _content_has_text(content):
            changed = True
            continue
        # Strip blank text blocks from any list content, preserving non-text (tool_use / image / …) blocks.
        if isinstance(content, list):
            stripped = _strip_blank_text_blocks(content)
            if stripped != content:
                m = m.model_copy(update={"content": stripped})
                content = stripped
                changed = True
        # An assistant turn must not carry empty content (empty string, or a now-empty block set). A tool_use
        # block counts as content (_content_has_text handles that), so this only fires on genuinely empty ones.
        if isinstance(m, AIMessage) and not _content_has_text(content):
            m = m.model_copy(update={"content": "."})
            changed = True
        out.append(m)
    return out, changed


_AUTONOMOUS_UNBOUNDED_GRAPH_RECURSION_LIMIT = _env_positive_int(
    "SAGE_AUTONOMOUS_UNBOUNDED_GRAPH_RECURSION_LIMIT",
    100000,
)


@dataclass(frozen=True)
class _ControllerCollectionRequest:
    foothold: Any
    scope_domain: str = ""
    reason: str = ""
    collection_key: str = ""
    support: str = ""


@dataclass(frozen=True)
class _HandoffDirective:
    """One delegated task: where it goes, what the card says, and what the worker receives.

    `__iter__` preserves the historical `(agent, instruction)` unpacking used by tests and older helpers while
    giving runtime redirects a first-class place to own the visible title after rewriting a handoff.
    """

    agent_name: str
    title: str
    instruction: str

    def __iter__(self):
        yield self.agent_name
        yield self.instruction


_HANDOFF_TITLE_MAX_CHARS = 72


def _normalize_handoff_title(title: Any, instruction: Any, agent_name: str = "") -> str:
    """Return a short one-line card title, falling back deterministically when older calls omit one."""
    raw_title = re.sub(r"\s+", " ", str(title or "").strip())
    if not raw_title:
        raw_instruction = re.sub(r"\s+", " ", _message_content_as_text(instruction).strip())
        if raw_instruction:
            raw_title = re.split(r"(?<=[.!?])\s+", raw_instruction, maxsplit=1)[0].rstrip(".!?")
        else:
            raw_title = f"Delegate to {agent_name}" if agent_name else "Delegated task"
    if len(raw_title) > _HANDOFF_TITLE_MAX_CHARS:
        raw_title = raw_title[: _HANDOFF_TITLE_MAX_CHARS - 3].rstrip() + "..."
    return raw_title


def _handoff_directive(agent_name: str, instruction: Any, title: Any = "") -> _HandoffDirective:
    instruction_text = _message_content_as_text(instruction).strip()
    return _HandoffDirective(
        agent_name=str(agent_name or "").strip(),
        title=_normalize_handoff_title(title, instruction_text, str(agent_name or "").strip()),
        instruction=instruction_text,
    )


def _coerce_handoff_directive(
    value: Any,
    *,
    fallback_agent_name: str,
    fallback_instruction: str,
    fallback_title: str = "",
) -> _HandoffDirective:
    """Accept new directives plus legacy tuple/dict redirect shapes during the transition."""
    if isinstance(value, _HandoffDirective):
        return _handoff_directive(value.agent_name, value.instruction, value.title)
    if isinstance(value, dict):
        return _handoff_directive(
            value.get("agent_name") or value.get("agent") or fallback_agent_name,
            value.get("instruction") or fallback_instruction,
            value.get("title") or "",
        )
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return _handoff_directive(value[0], value[1], "")
    return _handoff_directive(fallback_agent_name, fallback_instruction, fallback_title)


def _is_scalar_tool_value(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _capability_name_from_tool_arguments(arguments: Any) -> str:
    """Extract a semantic capability name from controller or LangChain tool-call args."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("action", "capability"):
        candidate = arguments.get(key)
        if isinstance(candidate, dict):
            name = candidate.get("name") or candidate.get("capability")
        else:
            name = candidate
        text = str(name or "").strip()
        if text:
            return text
    return ""


def _stringify_toon_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _slim_tool_description(desc: Any) -> Any:
    try:
        if not desc or not isinstance(desc, str):
            return desc
        match = re.search(
            r"^[^\S\r\n]*(?:Args|Arguments|Returns|Return|Raises|Yields|Examples|Example|Attributes):[^\S\r\n]*$",
            desc,
            re.MULTILINE,
        )
        if not match:
            return desc
        slim = desc[:match.start()].rstrip()
        if not slim:
            return desc
        return slim
    except Exception:
        return desc


def _encode_toon_table(data: Any) -> str | None:
    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(row, dict) for row in data):
        return None
    if not all(_is_scalar_tool_value(value) for row in data for value in row.values()):
        return None

    keys = []
    for row in data:
        for key in row:
            if key not in keys:
                keys.append(key)

    rows = []
    for row in data:
        cells = [_stringify_toon_cell(row.get(key)) for key in keys]
        if any("\t" in cell or "\n" in cell for cell in cells):
            return None
        rows.append("\t".join(cells))

    key_header = "\t".join(str(key) for key in keys)
    header = f"⟦TOON rows={len(data)} keys={key_header}⟧"
    return "\n".join([header, *rows])


def _cap_compacted_tool_result(chosen: str, *, ceiling: int) -> str:
    if chosen.startswith(_TOON_SENTINEL):
        lines = chosen.split("\n")
        header = lines[0]
        rows = lines[1:]
        kept = []
        for row in rows:
            candidate = "\n".join([header, *kept, row])
            if len(candidate) > ceiling:
                break
            kept.append(row)
        body = "\n".join([header, *kept])
        return (
            body
            + f"\n[truncated: showing {len(kept)} of {len(rows)} rows — re-query narrower for the rest]"
        )

    head = chosen[:ceiling]
    return (
        head
        + f"\n[truncated: {len(head)} of {len(chosen)} chars — full result not retained; re-query narrower]"
    )


def _compact_tool_result_str(s: str, *, trigger: int = 4000, ceiling: int = 16000) -> str:
    try:
        if len(s) <= trigger:
            return s
        if s.startswith(_TOON_SENTINEL) or _TRUNCATION_MARKER in s:
            return s

        chosen = s
        try:
            data = json.loads(s)
        except Exception:
            data = None
        else:
            toon = _encode_toon_table(data)
            if toon is not None and len(toon) < len(s):
                chosen = toon
            else:
                compact_json = json.dumps(data, separators=(",", ":"), sort_keys=True)
                chosen = compact_json if len(compact_json) < len(s) else s

        if len(chosen) > ceiling:
            return _cap_compacted_tool_result(chosen, ceiling=ceiling)
        return chosen
    except Exception:
        return s


def _transform_content(content: Any) -> Any:
    if isinstance(content, str):
        return _compact_tool_result_str(content)
    if isinstance(content, list):
        transformed = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                new_item = dict(item)
                new_item["text"] = _compact_tool_result_str(item["text"])
                transformed.append(new_item)
            else:
                transformed.append(item)
        return transformed
    return content


def _digest_cleared_tool_content(name, content) -> str:
    try:
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        else:
            text = str(content)
        orig_len = len(text)
        preview = " ".join(text[:140].split())
        return f"[cleared {name or 'tool'} result · {orig_len} chars] {preview}…"[:180]
    except Exception:
        return f"[cleared {name or 'tool'} result]"


class _DigestToolUsesEdit(ClearToolUsesEdit):
    """Like ClearToolUsesEdit but preserves a compact breadcrumb for cleared tool results."""

    def apply(self, messages, *, count_tokens) -> None:
        from langchain_core.messages import ToolMessage

        originals = {}
        for m in messages:
            if isinstance(m, ToolMessage) and not (
                    m.response_metadata.get("context_editing", {}).get("cleared")):
                originals[m.tool_call_id] = (m.name, m.content)
        super().apply(messages, count_tokens=count_tokens)
        if not originals:
            return
        for i, m in enumerate(messages):
            if (isinstance(m, ToolMessage)
                    and m.content == self.placeholder
                    and m.response_metadata.get("context_editing", {}).get("cleared")
                    and m.tool_call_id in originals):
                name, orig_content = originals[m.tool_call_id]
                try:
                    digest = _digest_cleared_tool_content(name, orig_content)
                    messages[i] = m.model_copy(update={"content": digest})
                except Exception:
                    pass


def _cap_message_for_copy(msg, cap=SUPERVISOR_COPY_TOOL_RESULT_CAP):
    """Return a copied ToolMessage with oversized string content capped for cross-agent channel copies.

    Non-ToolMessage messages, non-string ToolMessage content, and ToolMessages at
    or below the cap are returned unchanged with the same object identity.
    """
    if not isinstance(msg, ToolMessage) or not isinstance(msg.content, str) or len(msg.content) <= cap:
        return msg

    original_len = len(msg.content)
    preview = (
        msg.content[:cap]
        + f"\n…[truncated {original_len - cap} of {original_len} chars — full result retained in the executing agent's channel]"
    )
    return msg.model_copy(update={"content": preview})

def _fix_payload_empty_content(payload: dict) -> dict:
    """Fix OpenAI-format payload dicts to prevent Bedrock blank text field errors.

    When litellm converts OpenAI format to Bedrock/Anthropic format, it adds
    {"type": "text", "text": ""} blocks to assistant messages that have
    tool_calls but null/empty content. Bedrock rejects these blank text fields.

    This function operates on the DICT-level payload (after LangChain's message
    conversion), right before it's sent to the OpenAI client / litellm proxy.
    """
    if "messages" not in payload:
        return payload

    for msg in payload["messages"]:
        if not isinstance(msg, dict):
            continue

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            content = msg.get("content")
            # Case 1: content is None or empty string — litellm will add blank text block
            if content is None or content == "" or content == []:
                msg["content"] = "."
            # Case 2: content is a list with empty text blocks
            elif isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and
                            block.get("type") == "text" and
                            not block.get("text", "").strip()):
                        block["text"] = "."
        # Also fix non-assistant messages with blank text blocks in list content
        elif isinstance(msg.get("content"), list):
            msg["content"] = [
                block for block in msg["content"]
                if not (isinstance(block, dict) and
                        block.get("type") == "text" and
                        not block.get("text", "").strip())
            ] or msg["content"]  # fallback to original if all blocks removed

    return payload


_bedrock_patch_applied = False

def _apply_bedrock_patch():
    """Patch _convert_message_to_dict at MODULE level in langchain_openai.

    This is a module-level function (not a class method), so patching the
    module attribute reliably intercepts ALL calls. The function is referenced
    inline in _get_request_payload's list comprehension, which uses the module
    global, so replacing langchain_openai.chat_models.base._convert_message_to_dict
    catches every invocation regardless of class hierarchy or instance copying.
    """
    global _bedrock_patch_applied
    if _bedrock_patch_applied:
        return

    try:
        import langchain_openai.chat_models.base as openai_base
        original_convert = openai_base._convert_message_to_dict
    except (ImportError, AttributeError):
        return

    def _patched_convert_message_to_dict(message, *args, **kwargs):
        result = original_convert(message, *args, **kwargs)
        # Fix: Bedrock rejects blank text fields in ANY message content.
        # Litellm converts empty strings to [{"type":"text","text":""}] for Bedrock.
        # This affects assistant messages (with or without tool_calls) and can
        # affect any role where content is empty or contains blank text blocks.
        if isinstance(result, dict):
            role = result.get("role", "")
            content = result.get("content")

            # Case 1: content is None or empty string
            if content is None or (isinstance(content, str) and not content.strip()):
                if role == "assistant":
                    result["content"] = result["content"]  # keep None for tool_calls (OpenAI expects it)
                    # But if there are tool_calls, None is fine for OpenAI but litellm adds blank text
                    if result.get("tool_calls"):
                        result["content"] = "."
                    elif content == "":
                        # Empty string with no tool_calls — litellm converts to blank text block
                        result["content"] = "."
                        logger.debug(f"BEDROCK_FIX: Replaced empty assistant content with '.'")

            # Case 2: content is a list with blank text blocks
            elif isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and
                            block.get("type") == "text" and
                            not block.get("text", "").strip()):
                        block["text"] = "."
                        logger.debug(f"BEDROCK_FIX: Replaced blank text block with '.' in {role} message")
        return result

    # Patch the module-level function
    openai_base._convert_message_to_dict = _patched_convert_message_to_dict
    _bedrock_patch_applied = True
    logger.info("Applied Bedrock empty content fix to _convert_message_to_dict")


def _patch_model_for_bedrock(model: BaseChatModel) -> BaseChatModel:
    """Apply the langchain_openai empty-content monkeypatch and return the model unchanged.

    IMPORTANT — scope: `_apply_bedrock_patch` only patches `langchain_openai.chat_models.base.
    _convert_message_to_dict`, so it takes effect ONLY when the live model is `ChatOpenAI` (provider
    "openai" or an OpenAI-compatible / LiteLLM proxy that fronts Bedrock). For the NATIVE Bedrock provider —
    `init_chat_model(model_provider="bedrock")` → langchain-aws `ChatBedrock`/`ChatBedrockConverse` — this
    patch is a NO-OP (that path never imports langchain_openai). The authoritative, provider-agnostic
    empty-`system`/blank-block guard for the native path is `_sanitize_messages` plus the `_nonempty_system`
    construction-site default; do not rely on this monkeypatch for the langchain-aws provider."""
    _apply_bedrock_patch()
    return model


def _max_seq_reducer(a: int, b: int) -> int:
    """Reducer that always takes the maximum sequence value to prevent collisions."""
    return max(a or 0, b or 0)


# HITL approve/deny: DEFAULT-DENY. Only an explicit, exact-match approval word approves; every other
# input — empty, "no", "deny", "stop", ambiguous, anything unrecognized — denies. This is a safety
# boundary for an autonomous OFFENSIVE agent: a misclassification that approves a destructive call is
# exactly the failure supervised mode exists to prevent, so we never use an LLM tiebreak to UPGRADE to
# approve. Deliberately NOT _classify_continuation_intent (that is continue/stop semantics, not approve/deny).
_HITL_APPROVE_WORDS = frozenset({"approve", "approved", "yes", "y", "ok", "okay", "go", "proceed"})


def _hitl_is_approved(text: str) -> bool:
    """True only if the operator reply is an exact-match approval token (case-insensitive). Default-deny."""
    if not text:
        return False
    return text.strip().lower() in _HITL_APPROVE_WORDS


def _collect_hitl_action_requests(snapshot) -> list:
    """Return the pending HumanInTheLoop action_requests for a graph snapshot, counted ONCE.

    The middleware requires EXACTLY one resume decision per hanging tool call. `snapshot.interrupts`
    already aggregates interrupts across tasks, and `snapshot.tasks[].interrupts` repeats the same
    objects — unioning them double-counts (the 2026-06-01 task-598 bug: "Number of human decisions (2)
    does not match number of hanging tool calls (1)"). Use the aggregated list when present, fall back
    to per-task only when it is empty, and dedupe defensively by interrupt id.
    """
    interrupt_objs = list(getattr(snapshot, "interrupts", None) or ())
    if not interrupt_objs:
        for task in (getattr(snapshot, "tasks", None) or ()):
            interrupt_objs.extend(getattr(task, "interrupts", None) or ())
    seen = set()
    action_requests: list = []
    for itr in interrupt_objs:
        iid = getattr(itr, "id", None)
        key = iid if iid is not None else id(itr)
        if key in seen:
            continue
        seen.add(key)
        val = getattr(itr, "value", None)
        if isinstance(val, dict) and isinstance(val.get("action_requests"), list):
            action_requests.extend(val["action_requests"])
    return action_requests


class _OperatorStopRequested(Exception):
    """Raised INSIDE an agent's create_agent loop when the operator kill-switch fired.

    The outer graph.astream only checks Model._stop_requested between top-level super-steps
    (Supervisor↔specialist handoffs). A specialist mid-turn — e.g. Mythic_Operator looping over
    many tool calls, or blocked in issue_task_and_waitfor_task_output — never yields to that check,
    so `exit` appeared to do nothing until the whole turn finished (task-626: exit issued twice, no
    stop, manual kill; the log shows request_stop DID fire). This exception, raised by
    _StopCheckMiddleware at each model/tool boundary inside the agent, breaks out promptly; the outer
    invoke() try/except catches it and ends the session cleanly.
    """


class _ControllerHitlPause(BaseException):
    """Escape the controller loop without classifying a pending approval as a capability failure.

    AutonomousController intentionally catches ordinary Exceptions at its injected seams and converts them into
    diagnostic blockers. A pending operator approval is neither a blocker nor a failure, so it must escape that
    catch boundary and return control to Mythic without touching loop-breaker/no-progress state.
    """


class _StopCheckMiddleware(AgentMiddleware):
    """Honors the operator kill-switch at every model call and every tool call inside an agent.

    Closes over the owning Model so it can read the live _stop_requested flag. Checking before each
    model call (before_model) and before each tool executes (awrap_tool_call) means a stop takes
    effect at the next boundary within the agent turn, not just at top-level handoffs. The Model also
    tracks the active invoke() asyncio task so request_stop() can hard-cancel a tool already blocked
    mid-await, such as wait_for_seconds or issue_task_and_waitfor_task_output.
    """
    def __init__(self, model: "Model"):
        super().__init__()
        self._model = model

    def before_model(self, state, runtime):
        self._model._global_step_count = getattr(self._model, "_global_step_count", 0) + 1
        if (getattr(self._model, "_max_steps", 0)
                and self._model._global_step_count > self._model._max_steps
                and not getattr(self._model, "_stop_requested", False)):
            self._model._global_step_limit_hit = True
            self._model._stop_requested = True
            logger.warning(
                f"🛑 Global step limit ({self._model._max_steps}) reached after "
                f"{self._model._global_step_count} model steps — halting to prevent a runaway loop."
            )
        if getattr(self._model, "_stop_requested", False):
            raise _OperatorStopRequested()
        return None

    async def awrap_tool_call(self, request, handler):
        if getattr(self._model, "_stop_requested", False):
            raise _OperatorStopRequested()
        return await handler(request)

    def wrap_tool_call(self, request, handler):
        if getattr(self._model, "_stop_requested", False):
            raise _OperatorStopRequested()
        return handler(request)


_BLOODHOUND_CONNECT_STEPS = (
    "BloodHound is NOT connected, so I can't ingest the collection or run attack-path analysis yet — and "
    "BloodHound is central to Sage's graph features. To enable it (if you want graph-driven analysis):\n"
    "1. Make sure BloodHound CE is running (web/API + neo4j) and reachable from the Sage host.\n"
    "2. Create a BloodHound API token (BloodHound CE → Administration → API tokens) and put the Token ID "
    "and Token Key in the BloodHound MCP server's .env.\n"
    "3. Connect it with `mcp-connect` (name \"BloodHound\", a stdio command that runs the BloodHound MCP "
    "server); verify with `mcp-list`.\n"
    "Full steps are in the Sage payload documentation → \"Connecting BloodHound to Sage\". Re-run your "
    "request once BloodHound is connected."
)


class _BloodHoundConnectionGuardMiddleware(AgentMiddleware):
    """BloodHound agent only: if the BloodHound MCP is NOT connected, do not silently fail. Emit a Mythic
    EventFeed WARNING with connect steps (once per run) and END the turn with a user-facing message, so the
    Supervisor returns it to the operator — who can connect BloodHound if they wish, then re-run."""
    def __init__(self, model: "Model"):
        super().__init__()
        self._model = model

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        connected = any(MCPManager.is_bloodhound_server(s) for s in MCPManager.get_connected_servers())
        if connected:
            return None
        await self._model._notify_bloodhound_not_connected()
        # create_agent drops a before_model-injected message from the node's returned channel when we jump
        # straight to "end" (no LLM call → the capture callback records nothing), which left the BloodHound
        # node returning 0 messages and the Supervisor re-delegating forever. Stash the message on the Model
        # so _ainvoke surfaces it as the node's result (seq-tagged + copied to the Supervisor channel) → the
        # operator sees the connect steps and the turn terminates instead of looping.
        guard_msg = AIMessage(content=_BLOODHOUND_CONNECT_STEPS, name="BloodHound")
        self._model._pending_guard_message = guard_msg
        logger.info("🩸 [bloodhound-guard] BloodHound MCP not connected → EventFeed notice + returning steps to user")
        return {"jump_to": "end", "messages": [guard_msg]}


def _runtime_engagement_scope(mythic_client) -> tuple[str, bool]:
    """Return strict runtime proof scope only for a real MythicTools-like client."""
    try:
        resolver = getattr(mythic_client, "_eng_key", None)
        if not callable(resolver):
            return "", False
        engagement_id = str(resolver() or "").strip()
        return (engagement_id, bool(engagement_id))
    except Exception:
        return "", False


# Imperative directive paired with the observed-state block. A bare status LIST is informational; the
# operator can still favor an overlay checklist that lists an already-achieved hop. This makes the
# instruction explicit ("ACHIEVED = DONE, do NOT re-issue"). Shared by the per-turn middleware injection
# and the continue-loop nudge so both speak with one voice.
_ENGAGEMENT_STATE_DIRECTIVE = (
    "The ENGAGEMENT STATE above is authoritative. Any hop whose effect is listed as ACHIEVED is "
    "DONE — do NOT re-issue it (the gate will only SKIP it, wasting steps). If a hop is achieved "
    "but its expected follow-on did not occur (e.g. no new callback returned), that is a SEPARATE "
    "blocker — report it and move on; do NOT re-run the achieved primitive. Advance to the next "
    "viable hop from the observed state, or call respond_to_user with the blocker if no traversable "
    "hop remains."
)


class _EngagementStateMiddleware(AgentMiddleware):
    """Inject a FRESH observed engagement-state block into the operator's per-turn model context on
    EVERY model call (flag-gated, autonomous-only, fail-open).

    This is the per-turn fix for the mis-wired continue-loop-only injection: the 156x re-proposal of
    already-achieved hops happens INSIDE a single react run (many model calls, each ending in a tool
    call), so the `=== ENGAGEMENT STATE` block must be visible at each model call — not only when the
    operator ends a turn plainly (the rare path the continue-loop nudge covered). Injection is via
    `request.override(messages=...)`, which is EPHEMERAL for this call only — it is NOT a graph-state
    update, so the block never accumulates (otherwise 156 calls would append 156 state messages).
    """
    def __init__(self, model: "Model"):
        super().__init__()
        self._model = model
        self._last_rendered = None  # for change-only logging (proof-of-injection without log spam)

    def _augment(self, request):
        try:
            rendered = self._model._render_engagement_state_for_injection()
            if not rendered:
                return request
            # Proof-of-injection log, deduped to fire only when the observed state CHANGES (a hop newly
            # achieved, or the first injection). This is the reliable observation surface: the injected
            # block rides at the TAIL of a ~300KB+ prompt, past Phoenix's 128KB span-attr truncation, so
            # it would not show in spans or Mythic task output. A handful of greppable lines instead.
            if rendered != self._last_rendered:
                self._last_rendered = rendered
                _first = rendered.splitlines()[0] if rendered else ""
                logger.info(f"🧭 [engagement-state] injected into operator per-turn context | {_first}")
            return request.override(messages=list(request.messages) + [HumanMessage(content=rendered)])
        except Exception:
            return request  # fail-open: never break a model call over the state block

    def wrap_model_call(self, request, handler):
        augmented = self._augment(request)
        if augmented is request:
            return handler(request)
        try:
            return handler(augmented)
        except Exception:
            return handler(request)  # fail-open: a bad injection must never abort the turn

    async def awrap_model_call(self, request, handler):
        augmented = self._augment(request)
        if augmented is request:
            return await handler(request)
        try:
            return await handler(augmented)
        except Exception:
            return await handler(request)  # fail-open: a bad injection must never abort the turn


class _MessageSanitizerMiddleware(AgentMiddleware):
    """Provider-agnostic empty/blank content-block guard on EVERY model call inside create_agent's react loop.

    WHY THIS EXISTS (2026-07-10): the reported `ValidationException: system: text content blocks must be
    non-empty` fired on a native `ChatBedrock` (InvokeModel) call under the Supervisor. Two prior defenses do
    NOT cover that path: (1) `_sanitize_messages` only cleans the channel lists the graph passes in — it never
    sees the messages create_agent assembles internally across a multi-step react loop; (2) the
    `langchain_openai._convert_message_to_dict` monkeypatch only affects the ChatOpenAI/LiteLLM-proxy provider,
    so it is a NO-OP for langchain-aws (`ChatBedrock`/`ChatBedrockConverse`) and for every other native provider
    (ollama, anthropic, google_genai, …). This middleware fires at `wrap_model_call`, which wraps the actual
    model invocation for ALL providers regardless of class, and normalizes the OUTGOING request so no empty
    `system` prompt, blank text block, or empty assistant turn reaches the provider. Appended INNERMOST so it
    sees the final request after all other middleware (engagement-state injection, summarization). Fail-open: a
    sanitizer error must never abort a model call."""
    def __init__(self, model: "Model"):
        super().__init__()
        self._model = model

    def _sanitize(self, request):
        try:
            overrides: dict[str, Any] = {}
            # A blank top-level system prompt (create_agent's `system_prompt=`) → minimal non-empty default.
            sysp = getattr(request, "system_prompt", None)
            if isinstance(sysp, str) and not sysp.strip():
                overrides["system_prompt"] = _DEFAULT_SYSTEM_PROMPT
            msgs = getattr(request, "messages", None)
            if isinstance(msgs, list):
                cleaned, mchanged = _sanitize_model_messages(msgs)
                if mchanged:
                    overrides["messages"] = cleaned
            if not overrides:
                return request
            return request.override(**overrides)
        except Exception:
            return request  # fail-open: never break a model call over sanitization

    def wrap_model_call(self, request, handler):
        return handler(self._sanitize(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._sanitize(request))


def _tool_name_from_request(request: Any) -> str:
    """Extract the tool name from a langgraph ToolCallRequest (dataclass with
    tool_call: ToolCall TypedDict {'name': str}, tool: BaseTool | None). Never raises."""
    try:
        tool_call = getattr(request, "tool_call", None)
        if isinstance(tool_call, dict):
            try:
                name = tool_call["name"]
            except Exception:
                name = None
            if isinstance(name, str) and name:
                return name
        tool = getattr(request, "tool", None)
        name = getattr(tool, "name", None)
        if isinstance(name, str) and name:
            return name
        name = getattr(request, "name", None)
        if isinstance(name, str) and name:
            return name
    except Exception:
        pass
    return ""


class _BoundedExecuteCapabilityStopMiddleware(AgentMiddleware):
    """Treat execute_capability as an atomic operator boundary.

    The graph wrapper already has special handling for explicitly bounded one-action
    requests. Live runs showed that prompt-gated handling is too narrow: a react loop
    can execute a terminal capability and then launch another unrelated tool before
    returning to the Supervisor/state reconciler. This middleware enforces the generic
    contract inside create_agent:

    - a tool batch containing execute_capability may execute only the first
      execute_capability call;
    - once a terminal execute_capability result is in the agent state, the inner
      react loop jumps to the agent boundary before another model/tool step.

    Explicit bounded requests still get the stronger graph-END behavior in the
    wrapper below.
    """

    def __init__(self, model: "Model"):
        super().__init__()
        self._model = model

    @staticmethod
    def _is_bounded_state(state: Any) -> tuple[bool, list[AnyMessage]]:
        messages = _agent_state_messages(state)
        return _is_bounded_one_action_capability_request(messages), messages

    @staticmethod
    def _tool_call_id(request: Any) -> str:
        try:
            tool_call = getattr(request, "tool_call", None)
            if isinstance(tool_call, dict):
                value = tool_call.get("id")
                if value:
                    return str(value)
        except Exception:
            pass
        return "bounded-execute-capability-stop"

    @staticmethod
    def _blocked_tool_message(request: Any, reason: str) -> ToolMessage:
        name = _tool_name_from_request(request) or "unknown_tool"
        payload = {
            "ok": False,
            "verdict": "blocked",
            "capability": "execute-capability-boundary",
            "reason": reason,
        }
        return ToolMessage(
            content=json.dumps(payload, sort_keys=True),
            name=name,
            tool_call_id=_BoundedExecuteCapabilityStopMiddleware._tool_call_id(request),
        )

    def _before_model_update(self, state: Any) -> dict[str, Any] | None:
        _bounded, messages = self._is_bounded_state(state)
        terminal_payload = _terminal_execute_capability_payload(messages)
        if terminal_payload is None:
            return None
        # Control-state P0: feed the terminal capability outcome to the loop-breaker. The fresh execute_capability
        # tool-call id is the per-turn dedup key (re-delegations get a new id, so cross-turn repeats still count).
        try:
            _calls = _latest_ai_tool_calls(messages)
            _eid = next((str(tc.get("id")) for tc in _calls
                         if (tc.get("name") or "") == "execute_capability" and tc.get("id")), "")
            self._model._note_capability_outcome(terminal_payload, _eid)
        except Exception:
            pass
        try:
            logger.info(
                "✅ [execute-capability-boundary] terminal execute_capability result "
                "observed inside create_agent; ending agent loop before another model call"
            )
        except Exception:
            pass
        return {"jump_to": "end"}

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        return self._before_model_update(state)

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        return self._before_model_update(state)

    def _pre_tool_block_reason(self, request: Any) -> str | None:
        bounded, messages = self._is_bounded_state(getattr(request, "state", None))

        if _terminal_execute_capability_payload(messages) is not None:
            return (
                "execute_capability already has a terminal "
                "result; skipped this follow-on tool without executing it"
            )

        tool_name = _tool_name_from_request(request)
        latest_calls = _latest_ai_tool_calls(messages)
        execute_ids = [
            str(tc.get("id"))
            for tc in latest_calls
            if (tc.get("name") or "") == "execute_capability" and tc.get("id")
        ]
        request_id = self._tool_call_id(request)

        if execute_ids and tool_name != "execute_capability":
            return (
                "execute_capability is an atomic transaction boundary; skipped sibling "
                f"tool `{tool_name or 'unknown_tool'}` from the same tool batch without executing it"
            )

        if tool_name != "execute_capability":
            if not bounded:
                return None
            if execute_ids:
                return (
                    "bounded one-action capability request allows only the "
                    "execute_capability call from this tool batch; skipped sibling "
                    f"tool `{tool_name or 'unknown_tool'}` without executing it"
                )
            return (
                "bounded one-action capability request requires execute_capability; "
                f"skipped `{tool_name or 'unknown_tool'}` without executing it"
            )

        if execute_ids and request_id != execute_ids[0]:
            return (
                "execute_capability is an atomic transaction boundary and already selected one "
                "execute_capability call in this tool batch; skipped duplicate "
                "execute_capability without executing it"
            )
        return None

    async def awrap_tool_call(self, request, handler):
        reason = self._pre_tool_block_reason(request)
        if reason:
            try:
                logger.info(f"🛑 [execute-capability-boundary] {reason}")
            except Exception:
                pass
            return self._blocked_tool_message(request, reason)
        return await handler(request)

    def wrap_tool_call(self, request, handler):
        reason = self._pre_tool_block_reason(request)
        if reason:
            try:
                logger.info(f"🛑 [execute-capability-boundary] {reason}")
            except Exception:
                pass
            return self._blocked_tool_message(request, reason)
        return handler(request)


class _ToolResultCompactionMiddleware(AgentMiddleware):
    """Caps oversized tool RESULTS at the proven awrap_tool_call seam (fires on every tool call
    inside create_agent), so large structured results (BloodHound cypher ~131k, Mythic blobs 57-76k)
    don't flood the worker's per-call prompt. Reuses the densify/cap encoder (_transform_content).
    Skips protected control-flow tools. Passes Command / non-ToolMessage results through untouched.
    Never breaks a tool call: try/except around the COMPACTION only -> returns the original result;
    a handler's own exception is NOT caught here and propagates normally."""

    def __init__(self, model: "Model"):
        super().__init__()
        self._model = model

    def _compact_result(self, request, result):
        try:
            if not isinstance(result, ToolMessage):
                return result  # Command (handoff) etc. -> untouched
            name = _tool_name_from_request(request)
            if name in _COMPACTION_PROTECTED_TOOLS:
                return result
            new_content = _transform_content(result.content)  # handles str + list-of-blocks
            if new_content is result.content or new_content == result.content:
                return result
            try:
                before = len(result.content) if isinstance(result.content, str) else None
                logger.info(f"🗜️ [compaction] fired tool={name} -> capped (before_chars={before})")
            except Exception:
                pass
            return result.model_copy(update={"content": new_content})
        except Exception:
            return result

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)  # handler exceptions propagate (not caught)
        return self._compact_result(request, result)

    def wrap_tool_call(self, request, handler):
        result = handler(request)  # handler exceptions propagate (not caught)
        return self._compact_result(request, result)


class _ToolSchemaSlimMiddleware(AgentMiddleware):
    """Trims duplicated Google-docstring sections from per-call tool schemas without
    mutating source tools. Parameter details remain in each tool's JSON schema."""

    def __init__(self, model: "Model"):
        super().__init__()
        self._model = model

    def _slim_dict_tool(self, t):
        function = t.get("function")
        if isinstance(function, dict):
            desc = function.get("description")
            if isinstance(desc, str):
                slim = _slim_tool_description(desc)
                if slim != desc:
                    new_tool = copy.deepcopy(t)
                    new_tool["function"]["description"] = slim
                    return new_tool
        desc = t.get("description")
        if isinstance(desc, str):
            slim = _slim_tool_description(desc)
            if slim != desc:
                new_tool = copy.deepcopy(t)
                new_tool["description"] = slim
                return new_tool
        return t

    def _slim_request(self, request):
        try:
            tools = getattr(request, "tools", None)
            if not tools:
                return request
            new_tools = []
            changed_count = 0
            for t in tools:
                if isinstance(t, dict):
                    slimmed = self._slim_dict_tool(t)
                    new_tools.append(slimmed)
                    if slimmed is not t:
                        changed_count += 1
                    continue
                desc = getattr(t, "description", None)
                if isinstance(desc, str):
                    slim = _slim_tool_description(desc)
                    if slim != desc:
                        new_tools.append(t.model_copy(update={"description": slim}))
                        changed_count += 1
                        continue
                new_tools.append(t)
            if changed_count == 0:
                return request
            logger.info(f"✂️ [schema-slim] trimmed {changed_count} tool descriptions")
            return request.override(tools=new_tools)
        except Exception:
            return request

    def wrap_model_call(self, request, handler):
        return handler(self._slim_request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._slim_request(request))


class SageState(MessagesState):
    count: int
    remaining_steps: RemainingSteps
    mode: NotRequired[Literal["auto", "supervised"]]
    next_owner: NotRequired[str]
    recursion_summary_requested: bool
    recursion_handback: bool
    supervisor_messages: Annotated[list[AnyMessage], operator.add]
    generalist_messages: Annotated[list[AnyMessage], operator.add]
    mythic_operator_messages: Annotated[list[AnyMessage], operator.add]
    mythic_payload_messages: Annotated[list[AnyMessage], operator.add]
    mcp_manager_messages: Annotated[list[AnyMessage], operator.add]
    bloodhound_messages: Annotated[list[AnyMessage], operator.add]
    autonomous_executor_messages: Annotated[list[AnyMessage], operator.add]
    _message_seq: Annotated[int, _max_seq_reducer]  # Global sequence counter with max reducer


def _get_seq(msg: AnyMessage) -> int:
    """Get sequence number from message, defaulting to 0 for untagged messages."""
    return msg.additional_kwargs.get("_seq", 0)


def _tag_msg(msg: AnyMessage, seq: int) -> AnyMessage:
    """Tag a message with a sequence number for ordering."""
    if "_seq" not in msg.additional_kwargs:
        msg.additional_kwargs["_seq"] = seq
    return msg


def _is_control_tool(tool_name: str) -> bool:
    """Internal orchestration/handoff tools that must not render as operator-facing tool cards."""
    if not tool_name:
        return False
    if tool_name.startswith("transfer_to_"):
        return True
    return tool_name in {
        "handback_to_supervisor",
        "summarize_and_handback",
        "request_continuation",
        "respond_to_user",
    }


def _tool_result_is_error(content: str) -> bool:
    """Best-effort detection of a FAILED tool result, for the card's Finished/Failed badge.

    Catches both string errors ("Error: ...") AND structured results whose TOP-LEVEL shape signals
    failure — a dict with status == "error" or a non-empty "error" field. Mythic tools commonly return
    e.g. {"status": "error", "error": "..."} which does NOT start with "error", so the old startswith
    check tagged it green/Finished. Conservative: only the top-level object is inspected (parsed JSON
    first, then a python-repr fallback), so a SUCCESS payload that merely lists nested errors isn't
    misflagged.
    """
    s = (content or "").strip()
    if not s:
        return False
    if s.lower().startswith("error"):
        return True
    if s[:1] in ("{", "["):
        obj = None
        try:
            import json
            obj = json.loads(s)
        except Exception:
            try:
                import ast
                obj = ast.literal_eval(s)
            except Exception:
                obj = None
        if obj is not None:
            def _is_err(d: Any) -> bool:
                return isinstance(d, dict) and (
                    str(d.get("status", "")).strip().lower() == "error" or bool(d.get("error"))
                )
            # ONLY the top-level object signals tool failure. A top-level LIST is a data listing
            # (e.g. get_task_history_for_callback returns many task records, and a historical task
            # legitimately having status "error" does NOT mean THIS call failed) — inspecting list
            # elements here misflagged successful listings as Failed. Mythic's error envelope is a
            # dict ({"status":"error",...}), never a bare list, so a list is never an error by shape.
            if _is_err(obj):
                return True
    return False


def _is_internal_human_message(msg: AnyMessage) -> bool:
    """True for provider/control nudges that must not be treated as operator input."""
    return isinstance(msg, HumanMessage) and bool(msg.additional_kwargs.get("_hide_from_stream"))


def _message_content_as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _is_bounded_one_action_capability_request(messages: list[AnyMessage]) -> bool:
    """Detect delegated operator tasks that explicitly ask for one capability action then stop."""
    for msg in reversed(messages):
        if not isinstance(msg, HumanMessage) or _is_internal_human_message(msg):
            continue
        text = _message_content_as_text(msg.content).casefold()
        if not text:
            continue
        asks_for_capability = "execute_capability" in text or "capability action" in text
        asks_for_one = (
            "exactly one" in text
            or "exactly once" in text
            or "single capability" in text
            or "one next grounded capability" in text
            or "one capability" in text
        )
        asks_to_stop = (
            "then stop" in text
            or "and stop" in text
            or "stop after" in text
            or "retry at most" in text
        )
        return bool(asks_for_capability and asks_for_one and asks_to_stop)
    return False


def _terminal_execute_capability_payload(messages: list[AnyMessage]) -> dict[str, Any] | None:
    """Return the latest terminal execute_capability JSON payload, if one is present."""
    terminal_verdicts = {"achieved", "failed", "blocked", "partial"}
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if (getattr(msg, "name", "") or "") != "execute_capability":
            continue
        raw = _message_content_as_text(msg.content).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        verdict = str(payload.get("verdict") or "").casefold()
        ok = payload.get("ok") is True
        if not (ok or verdict in terminal_verdicts):
            continue
        capability = str(payload.get("capability") or "").strip()
        action = payload.get("action")
        if not capability and isinstance(action, dict):
            capability = str(action.get("name") or "").strip()
        if not capability:
            continue
        normalized = dict(payload)
        normalized["capability"] = capability
        normalized["verdict"] = verdict or ("achieved" if ok else "unknown")
        return normalized
    return None


def _terminal_execute_capability_report(
    payload: dict[str, Any],
    *,
    bounded_one_action: bool = False,
) -> str:
    capability = str(payload.get("capability") or "capability").strip()
    verdict = str(payload.get("verdict") or "unknown").strip()
    reason = str(payload.get("reason") or "").strip()
    issued = payload.get("issued") if isinstance(payload.get("issued"), list) else []
    recorded = payload.get("recorded_effects") if isinstance(payload.get("recorded_effects"), list) else []
    achieved = payload.get("achieved_effects") if isinstance(payload.get("achieved_effects"), list) else []
    proof_chain = payload.get("proof_chain") if isinstance(payload.get("proof_chain"), list) else []

    task_parts: list[str] = []
    for item in issued[:8]:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        task_id = item.get("task_id")
        if task_id and command:
            task_parts.append(f"{task_id} `{command}`")
        elif task_id:
            task_parts.append(str(task_id))
        elif command:
            task_parts.append(f"`{command}`")

    lines = [
        f"Executor verdict: `{verdict}` for `{capability}`.",
    ]
    if reason:
        lines.append(f"Reason: {reason}")
    lines.append("Task IDs: " + (", ".join(task_parts) if task_parts else "none issued"))
    lines.append(
        "Recorded effects: "
        + (", ".join(f"`{effect}`" for effect in recorded) if recorded else "none")
    )
    if proof_chain:
        lines.append("Proof chain:")
        for item in proof_chain[:8]:
            if not isinstance(item, dict):
                continue
            effect = str(item.get("effect") or "").strip()
            task_id = str(item.get("task_id") or "").strip()
            callback_id = str(item.get("callback_id") or "").strip()
            detail = f"`{effect}`" if effect else "(effect not recorded)"
            if task_id:
                detail += f" task={task_id}"
            if callback_id:
                detail += f" cb={callback_id}"
            lines.append(f"- {detail}")
    if achieved:
        preview = ", ".join(f"`{effect}`" for effect in achieved[:10])
        if len(achieved) > 10:
            preview += f", ... ({len(achieved)} total)"
        lines.append(f"Achieved effects now include: {preview}")
    if bounded_one_action:
        lines.append("This was a bounded one-action capability request, so Sage stopped instead of re-delegating.")
    return "\n".join(lines)


def _agent_state_messages(state: Any) -> list[AnyMessage]:
    """Return create_agent's internal message list from middleware state."""
    try:
        if isinstance(state, dict):
            messages = state.get("messages")
        else:
            messages = getattr(state, "messages", None)
        return list(messages) if isinstance(messages, list) else []
    except Exception:
        return []


def _latest_ai_tool_calls(messages: list[AnyMessage]) -> list[dict[str, Any]]:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            calls = getattr(msg, "tool_calls", None) or []
            return [tc for tc in calls if isinstance(tc, dict)]
    return []


def _msg_id(msg: AnyMessage) -> str:
    """Generate unique ID for deduplication based on sequence + type + key content."""
    seq = _get_seq(msg)
    msg_type = type(msg).__name__
    # For ToolMessages, include tool_call_id to distinguish different tool results
    if isinstance(msg, ToolMessage):
        return f"{msg_type}:{seq}:{getattr(msg, 'tool_call_id', '')}"
    # For AIMessages, include tool_call IDs (if any) to distinguish tool-calling messages
    if isinstance(msg, AIMessage):
        tool_calls = getattr(msg, 'tool_calls', None) or []
        if tool_calls:
            # Use tool call IDs for uniqueness - critical for messages with no text content
            tool_ids = ",".join(tc.get('id', '') for tc in tool_calls)
            return f"{msg_type}:{seq}:tools:{tool_ids}"
        content_preview = str(msg.content)[:50] if msg.content else ""
        return f"{msg_type}:{seq}:{hash(content_preview)}"
    # For HumanMessages, distinguish delegated tasks from real user input
    # This ensures we don't deduplicate away the delegated version (which has _delegated_to)
    if isinstance(msg, HumanMessage):
        delegated_to = msg.additional_kwargs.get("_delegated_to", "")
        if delegated_to:
            return f"{msg_type}:{seq}:delegated:{delegated_to}"
    return f"{msg_type}:{seq}"


class MessageCaptureCallback(AsyncCallbackHandler):
    """Callback handler that captures all messages during agent execution.

    This captures AIMessages from LLM calls and ToolMessages from tool executions,
    ensuring we see ALL messages including the first tool-calling AIMessage that
    LangChain's react agent "consumes" during its internal loop.

    Now also streams messages to Mythic in real-time as they're captured.
    """

    def __init__(
        self,
        agent_name: str,
        stream_func=None,
        format_func=None,
        tool_use_func=None,
        tool_source_func=None,
        agent_text_func=None,
        handback_summary_func=None,
        delegation_id: str | None = None,
        delegation_name: str | None = None,
    ):
        self.agent_name = agent_name
        self.delegation_id = delegation_id
        self.delegation_name = delegation_name
        self.captured_messages: list[AnyMessage] = []
        self._tool_call_to_name: dict[str, str] = {}  # Map tool_call_id to tool name
        self._tool_call_to_args: dict[str, Any] = {}  # Map tool_call_id to its request args (for the card)
        self._stream_func = stream_func  # Function to stream formatted messages to Mythic
        self._format_func = format_func  # Function to format messages for streaming
        self._tool_use_func = tool_use_func  # Function to stream tool-use cards to Mythic chat
        self._tool_source_func = tool_source_func
        self._agent_text_func = agent_text_func  # Function to stream delegated specialist text to its drill-down
        self._handback_summary_func = handback_summary_func  # Function to retain control-tool handback summaries
        # Track run_ids for SummarizationMiddleware's internal model.invoke calls.
        # Those produce a summary AIMessage that must NOT be captured or streamed:
        # capturing it would leak the summary to Mythic as fake agent output and
        # inject it into the persisted channel. Populated in on_chat_model_start/
        # on_llm_start when metadata lc_source == "summarization"; consumed in on_llm_end.
        self._summarization_run_ids: set = set()

    def clear(self):
        """Clear captured messages for reuse."""
        self.captured_messages = []
        self._tool_call_to_name = {}
        self._tool_call_to_args = {}
        self._summarization_run_ids = set()

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Flag SummarizationMiddleware's internal model call so its summary AIMessage is dropped."""
        if metadata and metadata.get("lc_source") == "summarization":
            self._summarization_run_ids.add(run_id)
            logger.debug(f"📨 [Callback:{self.agent_name}] Flagging summarization run_id={run_id} (chat_model_start)")

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Defensive twin of on_chat_model_start for models that surface as bare LLM runs."""
        if metadata and metadata.get("lc_source") == "summarization":
            self._summarization_run_ids.add(run_id)
            logger.debug(f"📨 [Callback:{self.agent_name}] Flagging summarization run_id={run_id} (llm_start)")

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Capture AIMessage after each LLM call and stream it immediately."""
        # Drop SummarizationMiddleware's internal summary call: never capture or stream it.
        if run_id in self._summarization_run_ids:
            self._summarization_run_ids.discard(run_id)
            logger.debug(f"📨 [Callback:{self.agent_name}] Dropping summarization summary AIMessage for run_id={run_id}")
            return
        try:
            logger.debug(f"📨 [Callback:{self.agent_name}] on_llm_end called with {len(response.generations)} generation(s)")
            # LLMResult contains generations which are lists of ChatGeneration
            for generation_list in response.generations:
                for generation in generation_list:
                    # Check if it's a ChatGeneration with a message
                    if isinstance(generation, ChatGeneration) and hasattr(generation, 'message') and generation.message:
                        msg = generation.message
                        if isinstance(msg, AIMessage):
                            # Tag with agent name for display
                            msg.name = self.agent_name
                            self.captured_messages.append(msg)

                            # Track tool calls for later matching with ToolMessages
                            for tc in getattr(msg, 'tool_calls', []) or []:
                                tc_id = tc.get('id')
                                tc_name = tc.get('name')
                                if tc_id and tc_name:
                                    self._tool_call_to_name[tc_id] = tc_name
                                    self._tool_call_to_args[tc_id] = tc.get('args')

                            logger.debug(f"📨 [Callback:{self.agent_name}] Captured AIMessage: "
                                       f"content={str(msg.content)[:50]!r}, "
                                       f"tool_calls={len(getattr(msg, 'tool_calls', []) or [])}")

                            # Stream message immediately to Mythic
                            # Filter: Suppress ALL Supervisor messages from streaming.
                            # The Supervisor is an internal orchestration component — users
                            # interact with specialist agents only. Without this, the Supervisor's
                            # AIMessage (which contains both text AND tool_calls like respond_to_user)
                            # would leak duplicate content to the user.
                            should_stream = True
                            if self.agent_name == "Supervisor":
                                logger.debug(f"📨 [Callback:Supervisor] Suppressing Supervisor message from streaming (internal orchestrator)")
                                should_stream = False

                            if should_stream and self._tool_use_func:
                                for tc in getattr(msg, 'tool_calls', []) or []:
                                    tc_id = tc.get('id')
                                    tc_name = tc.get('name')
                                    if not (tc_id and tc_name):
                                        continue
                                    if _is_control_tool(tc_name):
                                        if (
                                            self.delegation_id is not None
                                            and self._handback_summary_func is not None
                                            and tc_name in ("handback_to_supervisor", "summarize_and_handback", "respond_to_user")
                                        ):
                                            try:
                                                args = tc.get('args')
                                                if isinstance(args, dict):
                                                    summary = next(
                                                        (
                                                            value.strip()
                                                            for key in ("summary", "final_response", "progress_summary", "reason", "text")
                                                            for value in [args.get(key)]
                                                            if isinstance(value, str) and value.strip()
                                                        ),
                                                        "",
                                                    )
                                                    if summary:
                                                        await self._handback_summary_func(self.delegation_name, summary)
                                            except Exception as e:
                                                logger.debug(f"handback summary capture failed (non-fatal): {e}")
                                        continue
                                    if (
                                        self._tool_source_func is not None
                                        and self._tool_source_func(tc_name) == "mcp"
                                    ):
                                        continue
                                    try:
                                        await self._tool_use_func(
                                            tool_call_id=tc_id,
                                            tool_name=tc_name,
                                            status="started",
                                            complete=False,
                                            arguments_present=bool(tc.get('args')),
                                            arguments=tc.get('args'),
                                            delegation_id=self.delegation_id,
                                            delegation_name=self.delegation_name,
                                        )
                                    except Exception as e:
                                        logger.debug(f"tool_use started card failed (non-fatal): {e}")

                            if should_stream and self._format_func:
                                formatted = self._format_func(msg, agent_name=self.agent_name)
                                if formatted:
                                    if self.delegation_id is not None and self._agent_text_func is not None:
                                        await self._agent_text_func(
                                            content=formatted,
                                            delegation_id=self.delegation_id,
                                            delegation_name=self.delegation_name,
                                        )
                                    elif self._stream_func:
                                        await self._stream_func(formatted)
                    else:
                        # Log what we got if it's not a ChatGeneration
                        logger.debug(f"📨 [Callback:{self.agent_name}] Got generation type: {type(generation).__name__}")
        except Exception as e:
            logger.warning(f"⚠️  [Callback:{self.agent_name}] Error in on_llm_end: {e}", exc_info=True)

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Capture ToolMessage after each tool execution and stream it immediately."""
        try:
            # The output might be a ToolMessage or raw output
            if isinstance(output, ToolMessage):
                output.name = output.name or self._tool_call_to_name.get(output.tool_call_id, "unknown_tool")
                self.captured_messages.append(output)
                logger.debug(f"📨 [Callback:{self.agent_name}] Captured ToolMessage: "
                           f"tool={output.name}, tool_call_id={output.tool_call_id}")

                if (
                    self._tool_use_func
                    and self.agent_name != "Supervisor"
                    and not _is_control_tool(output.name)
                    and not (
                        self._tool_source_func is not None
                        and self._tool_source_func(output.name) == "mcp"
                    )
                ):
                    content_str = output.content if isinstance(output.content, str) else str(output.content)
                    # Structured-first error detection (not a brittle string match):
                    #   1. ToolMessage.status — LangChain's first-class flag, set to "error" when a tool RAISES.
                    #   2. Sage's Mythic tools mostly RETURN a structured {"status":"error","error":...} payload
                    #      instead of raising (so .status stays "success") — _tool_result_is_error reads those
                    #      top-level fields. The startswith("error") case is only the last-resort fallback for a
                    #      genuinely unstructured plain-string tool error.
                    errored = getattr(output, "status", None) == "error" or _tool_result_is_error(content_str)
                    # Phase 3 — keep the card lean. Small results stay fully inline as result_preview; large
                    # ones show a short preview and ship the full raw result via tool_use.output, which Mythic
                    # stores separately (chat_message.tool_output), strips from metadata, and serves lazily via
                    # "View output" — so a big result never inflates the chat message / page rendering.
                    # (`output` here is the ToolMessage; the lazy full result is `full_output`.)
                    _INLINE_CAP = 4000
                    _OUTPUT_CAP = 1_000_000  # 1 MB DB backstop on lazy output; real tool output is far under
                    if len(content_str) <= _INLINE_CAP:
                        preview = content_str
                        full_output = None
                    else:
                        preview = (
                            content_str[:_INLINE_CAP]
                            + f"\n…[{len(content_str):,} chars total — open “View output” for the full result]"
                        )
                        full_output = content_str if len(content_str) <= _OUTPUT_CAP else (
                            content_str[:_OUTPUT_CAP]
                            + f"\n…[truncated {len(content_str) - _OUTPUT_CAP:,} of {len(content_str):,} chars]"
                        )
                    try:
                        await self._tool_use_func(
                            tool_call_id=output.tool_call_id,
                            tool_name=output.name,
                            status="error" if errored else "completed",
                            complete=True,
                            arguments_present=bool(self._tool_call_to_args.get(output.tool_call_id)),
                            arguments=self._tool_call_to_args.get(output.tool_call_id),
                            result_preview=preview,
                            output=full_output,
                            delegation_id=self.delegation_id,
                            delegation_name=self.delegation_name,
                        )
                    except Exception as e:
                        logger.debug(f"tool_use finished card failed (non-fatal): {e}")

                # Stream message immediately to Mythic
                if self._stream_func and self._format_func:
                    formatted = self._format_func(output, agent_name=self.agent_name)
                    if formatted:
                        await self._stream_func(formatted)
            # If it's a string or other output, we'll get it via the agent's return value
        except Exception as e:
            logger.warning(f"⚠️  [Callback:{self.agent_name}] Error in on_tool_end: {e}")


class Model:
    """A class to represent an LLM model with its configuration for use with Mythic commands.
    
    """
    provider: str # The model provider (e.g., 'anthropic', 'bedrock', 'openai', 'ollama', etc.)
    model: str # The model string (e.g., 'claude-3-5-sonnet-latest', 'gpt-4-turbo', etc.)
    verbose: bool # Return verbose output of all User & AI messages as opposed to just the final response
    mythic_client: MythicTools | None # an instance of mythic_classes.Mythic with Mythic LLM tools to interact with the Mythic API
    counter: int # A counter to keep track of the number of messages processed
    agent_task_id: str # The Mythic taskData.Task.AgentTaskID associated with the agent using this model; used to obtain Mythic API token for authentication
    task_id: int # The Mythic task ID, taskData.Task.ID, used for logging LLM interactions into the LangChain SQLite database in sage.db
    config: RunnableConfig | None # The LangChain configuration options for the model {"configurable": {}}
    llm: BaseChatModel | Any # The initialized LangChain BaseChatModel instance for the specified provider and model
    messages: list[AnyMessage]
    system_message: SystemMessage
    memory: AsyncSqliteSaver # The LangChain AsyncSqliteSaver instance for saving messages to the SQLite database in sage.db
    state: dict[str, Any]
    graph: CompiledStateGraph | None
    # Tool cache for flexible data caching
    tool_cache: ToolCache
    # Dynamic data cache for agent prompts
    _payload_names: list[str] | None
    _c2_profiles: list[dict[str, str]] | None
    _cached_commands: dict[str, Any] | None
    _dynamic_data_loaded: bool

    def __init__(self, provider: str, model: str, system_prompt: str, config: dict, task_id: int, agent_task_id: str, mode: str = "supervised", autonomous_solve: bool = False, policy_mode: str = "", max_steps: int = 200, response_emitter: "Callable[[str], Awaitable[bool]] | None" = None, operation_id: int | None = None, channel_id: int | None = None, apitoken_id: int = 0, mythic_preauth_client: Any = None, policy_mode_resolution: str = "", policy_mode_requested: str = "", eval_force_capability_prefix_json: str | None = None):
        """
        Initialize the Model with provider, model, and configuration.
        :param provider: The model provider (e.g., 'anthropic', 'bedrock').
        :param model: The model string (e.g., 'claude-3-5-sonnet-latest').
        :param system_prompt: The system prompt to use for the model.
        :param config: A dictionary containing configuration options for the model {"configurable": {}}.
        :param response_emitter: Optional async sink for outbound formatted messages. The native chat
            container (sage_chat) injects a ChatTurnContext-backed emitter here so streaming egress goes
            to Mythic's chat_response queue instead of the PayloadType task RPC. When None (the legacy
            PayloadType path), _stream_message_to_mythic falls back to SendMythicRPCResponseCreate.
        :param operation_id: Chat-path OperationID, threaded explicitly since a chat request has no task
            to infer it from (Section 7). None on the legacy task path, where operation is task-derived.
        """
        # Native-chat streaming seam (Section 7): the single egress (_stream_message_to_mythic) prefers
        # this when set. Kept as a plain attribute so a chat turn can swap it per-request if ever needed.
        self._response_emitter = response_emitter
        self._active_delegations: dict[str, dict[str, Any]] = {}
        self._delegation_seq: int = 0
        self._delegation_scope: str = ""
        self._execution_activity_seq: int = 0
        self._visibility_expected: set[str] = set()
        self._visibility_rendered: set[str] = set()
        self._visibility_failed: set[str] = set()
        self.operation_id = operation_id
        # Chat-path Mythic auth context (Section 7 / 8A-P0): the numeric channel id and the per-channel
        # bot API token id, threaded so MythicTools can mint a channel-scoped token via
        # ChatAPITokenProvider instead of the task's AgentTaskID. None/0 on the legacy task path.
        self.channel_id = channel_id
        self.apitoken_id = apitoken_id
        self._mythic_preauth_client = mythic_preauth_client  # headless/eval: pre-authenticated mythic client
        # Chat-path checkpointer thread key (Section 7 / 8A-P1). The legacy task path derives the
        # LangGraph thread_id from f"{agent_task_id}-{task_id}", but a chat channel has no task and must
        # persist multi-turn state under a channel-stable key (str(ChannelID)). sage_chat sets this so
        # _session_thread_id() returns it; None keeps the task-derived key for the PayloadType path.
        self._thread_id_override = None
        self.provider = provider
        self.model = model
        self.mode = mode if mode in ("auto", "supervised") else "supervised"
        self._autonomous_solve = bool(autonomous_solve)
        try:
            from .policy import resolve_policy_mode
        except ImportError:
            from policy import resolve_policy_mode
        self.policy_mode, inferred_resolution = resolve_policy_mode(policy_mode)
        self._policy_mode_requested = str(policy_mode_requested or policy_mode or "")
        self._policy_mode_resolution = str(policy_mode_resolution or inferred_resolution)
        # Eval-only per-session override. Native chat executes inside the already-running Sage process,
        # so post-reset exact-target fixtures cannot rely on the gauge subprocess environment reaching the
        # controller. None preserves the legacy process-env behavior for headless and task-backed paths.
        self._eval_force_capability_prefix_json = eval_force_capability_prefix_json
        self._policy_model_calls = 0
        self._policy_episode_id = ""
        self._controller_runtime_telemetry: dict[str, Any] = {}
        self._controller_observed_decisions: list[dict[str, Any]] = []
        self._controller_observed_transactions: list[dict[str, Any]] = []
        self.graph = None
        self.verbose = False
        self.is_interactive = False
        self.mythic_client = None
        self.tool_manager = None
        self._message_seq = 1  # Sequence counter for message ordering (starts at 1, 0 reserved for system)
        self.messages = []
        self.agent_task_id = agent_task_id
        self.task_id = task_id
        # Cooperative kill switch. The exit command marks the parent task completed and removes
        # the session dict entry, but the running invoke()/astream coroutine holds its own ref to
        # this Model and keeps issuing tasks. request_stop() sets this flag; every graph.astream
        # loop checks it between super-steps and breaks. The active invoke() task is also tracked
        # and cancelled so a stop interrupts long awaits inside tools before they can issue delayed
        # follow-up tasks.
        self._stop_requested = False
        self._running_tasks = set()
        self._max_steps = int(max_steps) if max_steps else 0  # 0 = unlimited
        self._global_step_count = 0
        self._global_step_limit_hit = False
        self._objective_completion_report_streamed = False
        self._controller_hitl_pending = None
        self._controller_hitl_approved_key = ""
        self._controller_hitl_approved_pending = None
        self._controller_hitl_objective = ""
        # A supervised chat turn can opt into the policy-selected execution kernel without globally enabling
        # autonomous_solve. This flag stays true across controller-native approval pauses, then is reset
        # before the next fresh operator turn.
        self._supervised_objective_active = False
        # Autonomous-solve stall detector state (reset per solve in invoke() so counters never cross objectives).
        self._autonomous_stall_progress = None
        self._autonomous_stall_count = 0
        self._autonomous_stall_sig = None
        # Control-state loop-breaker (P0): see _note_capability_outcome. The per-solve state object is (re)created
        # in invoke() so a prior objective's blockers never leak into the next solve (a Sage session reuses one
        # Model across invoke() calls — the same reuse hazard as the stall counters).
        self._loop_breaker = None
        # Initialize dynamic data cache
        self._payload_names = None
        self._c2_profiles = None
        self._cached_commands = None
        self._dynamic_data_loaded = False
        db_path = "sage.db"  # Path to your SQLite database
        self.tool_cache = ToolCache(db_path)
        conn = aiosqlite.connect(db_path, check_same_thread=False)
        self.memory = AsyncSqliteSaver(conn)
        # Guard against an empty top-level system prompt. In chat mode `system_prompt` defaults to "" and this
        # SystemMessage is seeded as the FIRST message of the supervisor channel (below); an empty system block
        # makes Bedrock raise `ValidationException: system: text content blocks must be non-empty`. Normalize a
        # blank to a minimal real default so messages[0] stays a valid, non-empty SystemMessage (no index shift).
        self.system_message = SystemMessage(content=_nonempty_system(system_prompt))
        _tag_msg(self.system_message, 0)  # System message gets sequence 0
        self.state = {
            "messages": self.messages,          # legacy combined channel
            "count": 0,  # Legacy field, not used for output tracking
            "mode": self.mode,
            "_message_seq": self._message_seq,  # Shared sequence counter
            "supervisor_messages": [self.system_message],
            "generalist_messages": [],
            "mythic_operator_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
            "autonomous_executor_messages": [],
            "recursion_summary_requested": False,
            "recursion_handback": False,
        }
        # Note: LangChain instrumentation is initialized globally in main.py
        # Note: remaining_steps and recursion_summary_requested will be managed by LangGraph
        if config:
            self.config = RunnableConfig(
                configurable={
                    k: v 
                    for k, v in config.get("configurable", {}).items()
                    if k not in ["thread_id"]  # Remove thread_id if present
                }
            )
        self.llm = self._get_base_chat_model()
        if not self.llm:
            raise ValueError("Failed to initialize the BaseChatModel with the provided configuration.")

    def _graph_recursion_limit(self) -> int:
        if self._autonomous_solve and self._max_steps == 0:
            return _AUTONOMOUS_UNBOUNDED_GRAPH_RECURSION_LIMIT
        return _DEFAULT_GRAPH_RECURSION_LIMIT

    def _graph_run_config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self._graph_recursion_limit(),
        }

    def _session_thread_id(self) -> str:
        """The LangGraph checkpointer thread key for this session.

        Task path: f"{agent_task_id}-{task_id}" (unchanged). Chat path: the channel-stable override
        set by sage_chat (str(ChannelID)), so multi-turn SageState survives across one-shot chat
        requests (Section 7). Centralizing this is the P1 "thread key" re-source from Section 8A.
        """
        if getattr(self, "_thread_id_override", None):
            return self._thread_id_override
        return f"{self.agent_task_id}-{self.task_id}"

    def _next_seq(self) -> int:
        """Get next sequence number and increment counter. Also syncs to state."""
        seq = self._message_seq
        self._message_seq += 1
        self.state["_message_seq"] = self._message_seq
        logger.debug(f"🔢 Model._next_seq: returned seq={seq}, state now has _message_seq={self._message_seq}")
        return seq

    def _get_base_chat_model(self) -> BaseChatModel | None:
        """Initialize and return the BaseChatModel based on provider and model."""
        ensure_logger_initialized()
        if not self.config:
            logger.error("Model configuration is missing a config.")
            return None
        cfg = self.config.get("configurable")
        if cfg is None:
            logger.error("Model configuration is missing 'configurable' settings.")
            return None

        llm = None
        if self.provider.lower() == "bedrock" and cfg is not None:
            # Set region to cfg.get("region") if it exists, else default to "us-east-1"
            region = cfg.get("region")
            if region is None:
                region = "us-east-1"
            aws_access_key_id = cfg.get("aws_access_key_id")
            if aws_access_key_id is None:
                logger.error("Bedrock model configuration is missing 'aws_access_key_id'.")
                return None
            aws_secret_access_key = cfg.get("aws_secret_access_key")
            if aws_secret_access_key is None:
                logger.error("Bedrock model configuration is missing 'aws_secret_access_key'.")
                return None
            aws_session_token = cfg.get("aws_session_token")
            if aws_session_token is None:
                logger.error("Bedrock model configuration is missing 'aws_session_token'.")
                return None
            logger.debug(f"Initializing Bedrock model with provider={self.provider}, model={self.model}, aws_region={region}")
            llm = init_chat_model(model_provider=self.provider, model=self.model, region=region, aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key, aws_session_token=aws_session_token)
        else:
            model_kwargs: dict[str, Any] = {}
            if cfg.get("api_key"):
                model_kwargs["api_key"] = cfg["api_key"]
            if cfg.get("base_url"):
                model_kwargs["base_url"] = cfg["base_url"]
            logger.debug(
                f"Initializing model with provider={self.provider}, model={self.model}, "
                f"api_key={'configured' if 'api_key' in model_kwargs else 'provider default'}, "
                f"base_url={model_kwargs.get('base_url', 'provider default')}"
            )
            llm = init_chat_model(
                model_provider=self.provider,
                model=self.model,
                **model_kwargs,
            )

        # Legacy: monkeypatch langchain_openai's message conversion to strip blank text blocks. NOTE this only
        # affects the ChatOpenAI / LiteLLM-proxy provider path — it is a NO-OP for native langchain-aws
        # (ChatBedrock/ChatBedrockConverse) and every other native provider. The provider-agnostic guard that
        # actually reaches create_agent's internal react loop for ALL providers is `_MessageSanitizerMiddleware`
        # (added in `_context_middleware`); this patch is retained only as belt-and-suspenders for the proxy path.
        if llm is not None:
            llm = _patch_model_for_bedrock(llm)
        return llm
        
    async def _fetch_dynamic_data(self):
        """Fetch dynamic data from Mythic APIs for use in agent prompts."""
        ensure_logger_initialized()
        logger.info("🔄 Starting _fetch_dynamic_data() ")
        force_flush_all_handlers()

        try:
            if self.mythic_client is None:
                logger.warning("Mythic client not available, using default values for agent prompts")
                self._payload_names = ["merlin"]  # Default fallback
                self._c2_profiles = [{"name": "http", "description": "HTTP/S C2 Profile"}]  # Default fallback
                self._cached_commands = {}
                self._dynamic_data_loaded = True
                return

            # Fetch payload names
            try:
                self._payload_names = await self.mythic_client.get_payload_names()
                logger.debug(f"Fetched {len(self._payload_names)} payload names: {self._payload_names}")
                force_flush_all_handlers()
            except Exception as e:
                logger.warning(f"Failed to fetch payload names: {e}, using defaults")
                self._payload_names = ["merlin"]

            # Fetch C2 profiles
            try:
                self._c2_profiles = await self.mythic_client.get_c2_profile_names()
                logger.debug(f"Fetched {len(self._c2_profiles)} C2 profiles: {[p['name'] for p in self._c2_profiles]}")
                force_flush_all_handlers()
            except Exception as e:
                logger.warning(f"Failed to fetch C2 profiles: {e}, using defaults")
                self._c2_profiles = [{"name": "http", "description": "HTTP/S C2 Profile"}]

            # Pre-load commands for all payloads EXCEPT 'sage'
            # (sage is the running program and doesn't need to call itself)
            # This ensures they're cached and available in agent prompts
            self._cached_commands = {}
            # Pre-load all available payloads except 'sage'
            common_payloads = [p for p in self._payload_names if p.lower() != "sage"]

            logger.info(f"📦 Pre-loading commands for payloads: {common_payloads}")
            logger.debug(f"Available payload names: {self._payload_names}")
            force_flush_all_handlers()

            for payload in common_payloads:
                if payload in self._payload_names:
                    try:
                        logger.info(f"🔄 Pre-loading commands for payload '{payload}'...")
                        force_flush_all_handlers()
                        # Use 24-hour TTL since commands rarely change
                        commands = await self.get_commands_for_payload_cached(payload, ttl_seconds=86400)
                        self._cached_commands[payload] = commands

                        # Log summary of what was cached
                        if isinstance(commands, dict):
                            cmd_count = len(commands.get('commands', commands.get('command', [])))
                            logger.info(f"✅ Pre-loaded {cmd_count} commands for payload '{payload}'")
                        elif isinstance(commands, list):
                            logger.info(f"✅ Pre-loaded {len(commands)} commands for payload '{payload}'")
                        else:
                            logger.warning(f"⚠️  Pre-loaded commands for payload '{payload}' has unexpected type: {type(commands).__name__}")
                        force_flush_all_handlers()
                    except Exception as e:
                        logger.error(f"❌ Failed to pre-load commands for payload '{payload}': {e}", exc_info=True)
                        force_flush_all_handlers()
                else:
                    logger.warning(f"⚠️  Payload '{payload}' not in available payloads: {self._payload_names}")

            self._dynamic_data_loaded = True
            logger.info("Dynamic data successfully loaded for agent prompts")
            force_flush_all_handlers()

        except Exception as e:
            logger.error(f"Error fetching dynamic data: {e}", exc_info=True)
            force_flush_all_handlers()
            # Set defaults to ensure agents still work
            self._payload_names = ["merlin"]
            self._c2_profiles = [{"name": "http", "description": "HTTP/S C2 Profile"}]
            self._cached_commands = {}
            self._dynamic_data_loaded = True

    def _classify_tool_source(self, tool_name: str) -> str:
        """Classify a tool as MCP-backed or Sage's default Mythic tool source."""
        try:
            for server in MCPManager.get_connected_servers():
                for tool_item in MCPManager.get_tools_by_server(server):
                    if getattr(tool_item, "name", None) == tool_name:
                        return "mcp"
        except Exception as e:
            logger.debug(f"tool source classification failed (non-fatal): {e}")
        return "mythic"

    async def _emit_tool_use_card(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        status: str,
        complete: bool,
        arguments_present: bool = False,
        arguments: Any = None,
        result_preview: str | None = None,
        output: str | None = None,
        delegation_id: str | None = None,
        delegation_name: str | None = None,
        tool_source: str | None = None,
        preserve_arguments: bool = False,
    ) -> bool:
        """Bridge a captured tool call/result to the chat emitter as a collapsible card.

        The card's `content` (rendered in the UI Details) carries the tool REQUEST — the tool name and
        its arguments — so the operator sees what was called alongside the `result_preview` response.
        `output`, when set, is the full raw result shipped lazily (Mythic's "View output").
        """
        emitter = self._response_emitter
        if emitter is None or not hasattr(emitter, "emit_tool_use"):
            return False
        try:
            raw_name = tool_name or "unknown_tool"
            source = tool_source or self._classify_tool_source(raw_name)
            source_label = "MCP" if source == "mcp" else "Mythic"
            capability_name = _capability_name_from_tool_arguments(arguments) if raw_name == "execute_capability" else ""
            name = capability_name or raw_name
            args_str = ""
            if arguments:
                try:
                    import json as _json
                    args_str = _json.dumps(arguments, default=str, ensure_ascii=False)
                except Exception:
                    args_str = str(arguments)
                if not preserve_arguments and len(args_str) > 4000:
                    args_str = args_str[:4000] + "…"
            request_line = f"Request: {raw_name}({args_str})" if args_str else ""
            if status == "started":
                content = f"Using {source_label} tool `{name}`..."
                if request_line:
                    content += f"\n{request_line}"
            elif status == "error":
                content = request_line + ("\n\n" if request_line else "") + f"Tool `{name}` failed."
            else:
                content = request_line or f"Tool `{name}` finished."
            emitted = await emitter.emit_tool_use(
                tool_call_id=tool_call_id or "",
                tool_name=name,
                tool_source=source,
                status=status,
                content=content,
                complete=complete,
                arguments_present=arguments_present or bool(args_str),
                arguments=args_str or None,
                result_preview=result_preview,
                output=output,
                delegation_id=delegation_id,
                delegation_name=delegation_name,
            )
            if emitted is False:
                return False
            if delegation_id is not None and delegation_name is not None and status == "started":
                await self._bump_delegation_progress(delegation_name)
            return True
        except Exception as e:
            logger.debug(f"_emit_tool_use_card failed (non-fatal): {e}")
            return False

    def begin_visibility_turn(self, delegation_scope: str = "") -> None:
        """Reset request-scoped visibility accounting before execution starts."""
        self._visibility_expected = set()
        self._visibility_rendered = set()
        self._visibility_failed = set()
        self._delegation_scope = str(delegation_scope or "").strip()

    async def _emit_execution_event(self, event: dict[str, Any]) -> None:
        """Render one boundary-owned Mythic or MCP lifecycle event."""
        if not isinstance(event, dict):
            return
        self._controller_update_transaction_task_lineage(event)
        event_id = str(event.get("event_id") or "").strip()
        source = str(event.get("source") or "").strip().casefold()
        tool_name = str(event.get("tool_name") or event.get("command") or "").strip()
        status = str(event.get("status") or "").strip().casefold()
        if not event_id or source not in {"mythic", "mcp"} or not tool_name:
            return
        if status == "started":
            self._visibility_expected.add(event_id)

        activity = event.get("activity") if isinstance(event.get("activity"), dict) else {}
        arguments = event.get("arguments")
        if source == "mythic":
            arguments = {
                "callback_id": event.get("callback_id"),
                "parameters": event.get("parameters"),
                "task_id": event.get("task_id"),
            }
            capability_name = str(event.get("capability") or "").strip()
            if capability_name:
                arguments["capability"] = capability_name
            purpose = str(event.get("purpose") or "").strip()
            if purpose:
                arguments["purpose"] = purpose

        rendered = await self._emit_tool_use_card(
            tool_call_id=event_id,
            tool_name=tool_name,
            status=status or "completed",
            complete=status != "started",
            arguments_present=arguments not in (None, "", {}),
            arguments=arguments,
            result_preview=str(event.get("result_preview") or "") or None,
            output=str(event.get("output") or "") or None,
            delegation_id=str(activity.get("id") or "") or None,
            delegation_name=str(activity.get("name") or "") or None,
            tool_source=source,
            preserve_arguments=True,
        )
        if rendered:
            self._visibility_rendered.add(event_id)
        else:
            self._visibility_failed.add(event_id)

    async def finalize_visibility_turn(self) -> dict[str, Any]:
        """Reconcile boundary events against rendered cards and surface degraded visibility."""
        expected = set(getattr(self, "_visibility_expected", set()) or set())
        rendered = set(getattr(self, "_visibility_rendered", set()) or set())
        failed = set(getattr(self, "_visibility_failed", set()) or set())
        missing = sorted(expected - rendered)
        degraded = sorted(set(missing) | failed)
        summary = {
            "expected": len(expected),
            "rendered": len(expected & rendered),
            "failed": len(degraded),
            "missing_event_ids": degraded,
        }
        if degraded:
            preview = ", ".join(degraded[:5])
            suffix = "" if len(degraded) <= 5 else f" (+{len(degraded) - 5} more)"
            try:
                await self._stream_message_to_mythic(
                    "**Visibility degraded**\n"
                    f"{len(degraded)} execution event(s) could not be rendered: {preview}{suffix}.\n"
                )
            except Exception as exc:
                logger.warning(f"Visibility reconciliation warning failed: {exc}")
        return summary

    async def _emit_capability_command_card(self, event: dict[str, Any]) -> None:
        """Render non-task capability lifecycle prose; accepted tasks use the shared boundary."""
        if not isinstance(event, dict):
            return
        command_name = str(event.get("command") or "").strip()
        trace_id = str(event.get("trace_id") or "").strip()
        if not command_name or not trace_id:
            return
        if command_name != "wait_for_seconds":
            return
        parameters = event.get("parameters") if isinstance(event.get("parameters"), dict) else {}
        seconds = int(parameters.get("seconds") or 300)
        reason = str(parameters.get("reason") or "wait for propagation").strip()
        status = str(event.get("status") or "").strip().casefold()
        duration = (
            f"{seconds // 60} minute{'s' if seconds // 60 != 1 else ''}"
            if seconds >= 60 and seconds % 60 == 0
            else f"{seconds} seconds"
        )
        if status == "started":
            message = (
                "**Waiting for propagation**\n"
                f"Sage is sleeping for {duration} while the external effect propagates, then it will resume verification.\n"
                f"Reason: {reason}\n"
                "No operator action is required.\n"
            )
        elif status == "progress":
            preview = str(event.get("result_preview") or "").strip()
            message = (
                "**Propagation wait in progress**\n"
                f"Sage is still sleeping before verification{f': {preview}' if preview else '.'}\n"
                "No operator action is required.\n"
            )
        else:
            message = (
                "**Propagation wait complete**\n"
                "Sage finished the bounded wait and is continuing with effect validation.\n"
            )
        await self._stream_message_to_mythic(message)

    async def _emit_subagent_status(
        self,
        *,
        title: str,
        delegation_id: str,
        delegation_name: str,
        status: str = "running",
        tool_count: int | None = None,
        tool_total: int | None = None,
        icon: str = "",
        icon_color: str = "",
        content: str = "",
        complete: bool = False,
    ) -> None:
        emitter = self._response_emitter
        if emitter is None or not hasattr(emitter, "emit_subagent_status"):
            return
        try:
            await emitter.emit_subagent_status(
                title=title,
                delegation_id=delegation_id,
                delegation_name=delegation_name,
                status=status,
                tool_count=tool_count,
                tool_total=tool_total,
                icon=icon,
                icon_color=icon_color,
                content=content,
                complete=complete,
            )
        except Exception as e:
            logger.debug(f"_emit_subagent_status failed (non-fatal): {e}")

    async def _emit_agent_text(
        self,
        *,
        content: str,
        delegation_id: str,
        delegation_name: str,
    ) -> None:
        emitter = getattr(self, "_response_emitter", None)
        if emitter is None or not hasattr(emitter, "emit_agent_text"):
            return
        try:
            await emitter.emit_agent_text(
                content=content,
                delegation_id=delegation_id,
                delegation_name=delegation_name,
            )
            if content.strip():
                delegation = getattr(self, "_active_delegations", {}).get(delegation_name)
                if delegation is not None:
                    delegation["last_text"] = content.strip()
        except Exception as e:
            logger.debug(f"_emit_agent_text failed (non-fatal): {e}")

    @staticmethod
    def _delegation_icon(agent_name: str) -> str:
        """Sub-agent card icon. rc5 lets this be a Font-Awesome icon NAME (rendered as the glyph);
        operator-editable via the prompt frontmatter ``icon:`` (like ``color:``), with a per-agent fa
        default, then a 2-letter code for an unknown agent."""
        fallback = {
            "BloodHound": "dog",            # bloodhound = the dog
            "Mythic_Operator": "user-secret",
            "Mythic_Payload": "box-open",
            "Generalist": "robot",
            "MCP_Manager": "plug",
            "Execution": "gears",
            "Collection": "database",
        }
        try:
            icon = load_prompt_meta(agent_name.lower()).get("icon")
            if isinstance(icon, str) and icon.strip():
                return icon.strip()
        except Exception as e:
            logger.debug(f"_delegation_icon frontmatter read failed for {agent_name} (non-fatal): {e}")
        return fallback.get(agent_name, agent_name[:2].upper())

    @staticmethod
    def _delegation_color(agent_name: str) -> str:
        """Deterministic per-agent sub-agent-card color (CSS color text).

        Resolution order: the agent's prompt-file frontmatter ``color:`` (operator-editable),
        then a fixed fallback palette, then ``""`` — which lets Mythic auto-derive a color.
        Auto-derivation is the OLD behavior: it hashes per-card, so every card for the same
        agent came out a different color. Returning one deterministic color per agent_name is
        what pins each specialist to a single, stable color (e.g. BloodHound = red).
        """
        fallback = {
            "BloodHound": "#E5484D",       # red (operator request)
            "Mythic_Operator": "#3B82F6",  # blue
            "Mythic_Payload": "#A855F7",   # purple
            "Generalist": "#10B981",       # green
            "MCP_Manager": "#F59E0B",      # amber
            "Execution": "#3B82F6",
            "Collection": "#F59E0B",
        }
        try:
            color = load_prompt_meta(agent_name.lower()).get("color")
            if isinstance(color, str) and color.strip():
                return color.strip()
        except Exception as e:
            logger.debug(f"_delegation_color frontmatter read failed for {agent_name} (non-fatal): {e}")
        return fallback.get(agent_name, "")

    def current_delegation_id(self, agent_name: str) -> str | None:
        # Fail-soft: a Model built via __new__ (tests, partial harnesses) never ran __init__, so
        # _active_delegations may be absent. Missing state simply means "no active delegation".
        delegation = getattr(self, "_active_delegations", {}).get(agent_name)
        if delegation is None:
            return None
        delegation_id = delegation.get("id")
        return delegation_id if isinstance(delegation_id, str) else None

    async def _open_execution_activity(
        self,
        activity_name: str,
        *,
        title: str,
        instruction: str,
    ) -> dict[str, str] | None:
        """Open a virtual delegation used to group deterministic execution detail."""
        self._execution_activity_seq = int(getattr(self, "_execution_activity_seq", 0) or 0) + 1
        await self._open_delegation(
            activity_name,
            instruction,
            self._execution_activity_seq,
            title=title,
        )
        delegation_id = self.current_delegation_id(activity_name)
        if delegation_id is None:
            return None
        return {"id": delegation_id, "name": activity_name}

    async def _close_execution_activity(
        self,
        activity: dict[str, str] | None,
        *,
        content: str = "",
        status: str = "finished",
    ) -> None:
        if not isinstance(activity, dict):
            return
        activity_name = str(activity.get("name") or "")
        if activity_name:
            await self._close_delegation(activity_name, content=content, status=status)

    def _single_active_delegation(self) -> tuple[str, str] | None:
        try:
            active = getattr(self, "_active_delegations", {}) or {}
            if len(active) != 1:
                return None
            delegation = next(iter(active.values()))
            delegation_id = delegation.get("id")
            delegation_name = delegation.get("name")
            if isinstance(delegation_id, str) and isinstance(delegation_name, str):
                return (delegation_id, delegation_name)
            return None
        except Exception:
            return None

    async def _capture_delegation_final_summary(self, agent_name: str | None, summary: str) -> None:
        try:
            delegation = getattr(self, "_active_delegations", {}).get(agent_name)
            if delegation is not None and isinstance(summary, str) and summary.strip():
                delegation["final_summary"] = summary.strip()
        except Exception as e:
            logger.debug(f"_capture_delegation_final_summary failed (non-fatal): {e}")

    async def _open_delegation(
        self,
        agent_name: str,
        instruction: str,
        source_seq: int,
        title: str = "",
    ) -> None:
        emitter = getattr(self, "_response_emitter", None)
        if emitter is None or not hasattr(emitter, "emit_subagent_status") or agent_name == "Supervisor":
            return
        try:
            existing = self._active_delegations.get(agent_name)
            if existing is not None and existing.get("source_seq") == source_seq:
                return
            if existing is not None:
                await self._close_delegation(agent_name)
            self._delegation_seq += 1
            scope = str(getattr(self, "_delegation_scope", "") or "").strip()
            delegation_id = (
                f"{agent_name.lower()}:{scope}:{self._delegation_seq}"
                if scope
                else f"{agent_name.lower()}:{self._delegation_seq}"
            )
            icon = self._delegation_icon(agent_name)
            icon_color = self._delegation_color(agent_name)
            card_title = _normalize_handoff_title(title, instruction, agent_name)
            self._active_delegations[agent_name] = {
                "id": delegation_id,
                "name": agent_name,
                "title": card_title,
                "instruction": instruction,
                "tool_count": 0,
                "icon": icon,
                "icon_color": icon_color,
                "source_seq": source_seq,
                "last_text": "",
                "final_summary": "",
            }
            await self._emit_subagent_status(
                title=card_title,
                delegation_id=delegation_id,
                delegation_name=agent_name,
                status="running",
                tool_count=0,
                icon=icon,
                icon_color=icon_color,
                content="",
            )
        except Exception as e:
            logger.debug(f"_open_delegation failed (non-fatal): {e}")

    async def _bump_delegation_progress(self, agent_name: str) -> None:
        try:
            delegation = self._active_delegations.get(agent_name)
            if delegation is None:
                return
            tool_count = int(delegation.get("tool_count", 0)) + 1
            delegation["tool_count"] = tool_count
            await self._emit_subagent_status(
                title=str(delegation.get("title", "")),
                delegation_id=str(delegation.get("id", "")),
                delegation_name=str(delegation.get("name", agent_name)),
                status="running",
                tool_count=tool_count,
                icon=str(delegation.get("icon", "")),
                icon_color=str(delegation.get("icon_color", "")),
                content="",
            )
        except Exception as e:
            logger.debug(f"_bump_delegation_progress failed (non-fatal): {e}")

    async def _close_delegation(self, agent_name: str, content: str = "", status: str = "finished") -> None:
        try:
            delegation = self._active_delegations.pop(agent_name, None)
            if delegation is None:
                return
            final_summary = str(delegation.get("final_summary") or "").strip()
            explicit_content = str(content or "").strip()
            last_text = str(delegation.get("last_text") or "").strip()
            # Mythic automatically persists non-empty terminal card content as a
            # subagent_final_output drill-down message. Text already emitted through emit_agent_text
            # must therefore not be repeated as card-close content.
            content = explicit_content or final_summary
            if content and last_text and content == last_text:
                content = ""
            await self._emit_subagent_status(
                title=str(delegation.get("title", "")),
                delegation_id=str(delegation.get("id", "")),
                delegation_name=str(delegation.get("name", agent_name)),
                status=status,
                tool_count=int(delegation.get("tool_count", 0)),
                icon=str(delegation.get("icon", "")),
                icon_color=str(delegation.get("icon_color", "")),
                content=content,
                complete=True,
            )
        except Exception as e:
            logger.debug(f"_close_delegation failed (non-fatal): {e}")

    async def _close_all_delegations(self, status: str = "finished") -> None:
        """Close every still-open sub-agent card with ``status``.

        Called on operator stop so a card that was mid-run does not stay stuck on the
        "running" badge (it becomes "stopped"). Safe to call repeatedly and from multiple
        stop paths — _close_delegation pops each entry, so a second call is a no-op.
        """
        names = list(getattr(self, "_active_delegations", {}))
        logger.info(f"🛑 _close_all_delegations(status={status!r}): closing {len(names)} open card(s): {names}")
        for agent_name in names:
            await self._close_delegation(agent_name, status=status)

    async def _emit_operator_stop(self, stop_message: str) -> None:
        """Stream the operator-stop notice and mark every open sub-agent card 'stopped'.

        Grouped into one coroutine so the hard-cancel path can drive it under
        ``asyncio.shield``: when the stop arrives as a task cancel, these emits run *after*
        we've caught the CancelledError while the task is still being torn down, and an
        un-shielded emit can be cut off before it reaches Mythic — leaving a card stuck on
        "running". Shielding lets the whole notice+close sequence complete on the loop.
        """
        try:
            await self._stream_message_to_mythic(stop_message)
        except Exception:
            pass
        await self._close_all_delegations(status="stopped")

    async def _run_operator_stop_shielded(self, stop_message: str) -> None:
        """Run _emit_operator_stop to completion even if the caller's task is being cancelled."""
        cleanup = asyncio.ensure_future(self._emit_operator_stop(stop_message))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Cancelled again while the shielded cleanup ran; wait it out so its emits land
            # BEFORE we return and the request terminal is sent.
            try:
                await cleanup
            except asyncio.CancelledError:
                pass

    async def _stream_message_to_mythic(self, formatted_message: str) -> bool:
        """
        Stream a formatted message chunk to the Mythic task.

        Args:
            formatted_message: Pre-formatted message string (e.g., "🤖[Agent]> response")

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.verbose:
                # Verbose copy for the Sage stdout/tmux pane (the logger adds its own timestamp for the
                # pane). The message streamed to Mythic is NOT stamped: Mythic's chat UI renders its own
                # native timestamp, so Sage must not prepend its own [HH:MM:SS] to the outbound content.
                logger.info(f"📤 {formatted_message.rstrip()}")
            # Native-chat seam (Section 7): when a response_emitter is injected (sage_chat path), route
            # this — the ONLY egress — to the chat_response queue instead of the PayloadType task RPC.
            # Same empty-guard so a blank block never streams (mirrors the "actual bytes" RPC guard).
            if self._response_emitter is not None:
                if not formatted_message:
                    return False
                try:
                    return await self._response_emitter(formatted_message)
                except Exception as e:
                    logger.error(f"Exception streaming to chat emitter: {e}")
                    return False
            encoded = formatted_message.encode()
            if not encoded:
                logger.warning(f"⚠️  Skipping empty response to Mythic task {self.task_id} (would cause 'Response must have actual bytes' error)")
                return False
            resp = await SendMythicRPCResponseCreate(
                MythicRPCResponseCreateMessage(
                    self.task_id,
                    encoded
                )
            )
            if not resp.Success:
                logger.error(f"Failed to stream message to task {self.task_id}: {resp.Error}")
                return False
            return True
        except Exception as e:
            logger.error(f"Exception streaming to task {self.task_id}: {e}")
            return False

    async def _process_stream_event(self, event: dict[str, Any]) -> None:
        """
        Process a streaming event from graph.astream() and stream messages to Mythic.

        NOTE: AIMessages and ToolMessages are now streamed immediately by MessageCaptureCallback
        during agent execution. This method only needs to stream HumanMessages (handoff messages)
        that are created by handoff tools and not captured by the callback.

        Args:
            event: Dictionary containing node name as key and updated state as value
                  Example: {"Supervisor": {"supervisor_messages": [...]}}
        """
        # Extract node name and state from event
        # Events are formatted as: {node_name: updated_state_dict}
        for node_name, state_update in event.items():
            if node_name in ["__start__", "__end__"]:
                continue  # Skip internal nodes

            # Extract new messages from state update
            new_messages = await self._extract_new_messages_from_event(state_update)

            for msg in new_messages:
                # Stream HumanMessages (handoff/delegation messages)
                if isinstance(msg, HumanMessage):
                    formatted = self._format_message_for_streaming(msg, agent_name=node_name)
                    if formatted:
                        await self._stream_message_to_mythic(formatted)
                # Supervisor AIMessages are normally internal routing/orchestration messages and
                # are suppressed. The respond_to_user tool tags its final report so this path can
                # stream exactly one closing executive summary after the specialist live stream.
                elif isinstance(msg, AIMessage) and getattr(msg, 'name', None) == "Supervisor":
                    if msg.additional_kwargs.get("_is_final_report"):
                        # Keep the agent prefix first (nothing before it); the "Final Report" header
                        # goes AFTER the prefix: 🤖[Supervisor]> 📊 **Final Report**\n<content>
                        report_msg = msg.model_copy(update={"content": f"📊 **Final Report**\n{msg.content}"})
                        formatted = self._format_message_for_streaming(report_msg, agent_name="Supervisor")
                        if formatted:
                            await self._stream_message_to_mythic(f"\n\n{formatted}")
                    elif str(msg.content).strip() and not (getattr(msg, "tool_calls", None) or []):
                        # Supervisor DIRECT answer: plain text, no tool call, not routed through
                        # respond_to_user. This happens when the Supervisor handles a trivial prompt inline
                        # instead of delegating — previously suppressed here (and in on_llm_end), so the
                        # operator saw nothing. Surface it as the turn's response. Routing/handoff/
                        # respond_to_user messages (all carry tool_calls or the _is_final_report tag) are
                        # unaffected, so the delegation path does not double-stream.
                        formatted = self._format_message_for_streaming(msg, agent_name="Supervisor")
                        if formatted:
                            await self._stream_message_to_mythic(formatted)
                    else:
                        logger.debug(f"📨 [Stream] Suppressing Supervisor routing/respond_to_user message from user output")

    async def _extract_new_messages_from_event(self, state_update: dict) -> list[BaseMessage]:
        """
        Extract messages from a stream event's state update.

        LangGraph events only contain the NEW messages added by that node,
        so we can stream everything without deduplication logic.
        """
        new_messages = []

        # Check all message channels
        for channel_name in [
            "supervisor_messages",
            "generalist_messages",
            "mythic_operator_messages",
            "mythic_payload_messages",
            "mcp_manager_messages", "bloodhound_messages",
            "autonomous_executor_messages",
        ]:
            if channel_name in state_update:
                messages = state_update[channel_name]
                if not isinstance(messages, list):
                    continue

                # Add all messages from this event (they're all new)
                new_messages.extend(messages)

        # Sort by sequence number to maintain chronological order
        new_messages.sort(key=lambda m: m.additional_kwargs.get("_seq", 0))

        return new_messages

    def _format_message_for_streaming(self, message: BaseMessage, agent_name: str = None) -> str:
        """
        Format a single message for streaming output.

        This is a streamlined version of _generate_mythic_output() logic that handles
        one message at a time instead of batching.

        Args:
            message: The message to format
            agent_name: The name of the agent/node that produced this message (from stream event)

        Returns:
            Formatted string with emoji prefix, or empty string if message should be skipped
        """
        # Skip system messages
        if isinstance(message, SystemMessage):
            return ""

        # Format based on message type
        if isinstance(message, HumanMessage):
            if _is_internal_human_message(message):
                return ""
            # Get content
            content = str(message.content).strip() if message.content else ""
            if not content:
                return ""  # Skip empty messages

            # Check if this is a DELEGATED task (agent handoff)
            delegated_to = message.additional_kwargs.get("_delegated_to")

            if delegated_to:
                if getattr(self, "channel_id", None) is not None:
                    return ""  # chat path: the sub-agent card replaces this delegation line
                # Show agent handoffs: "📋[Task → Mythic_Operator]> Query active callbacks"
                return f"📋[Task → {delegated_to}]> {content}\n"
            else:
                # User prompts:
                # - Chat container (first-class): Mythic ALWAYS renders the operator's own message in the
                #   channel, so we must never echo it back. channel_id is set only on the chat path.
                # - Interactive PayloadType task (subsequent turns): Mythic echoes it → skip to avoid dupes.
                # - Non-interactive PayloadType task (first turn): Mythic doesn't echo → show it.
                if getattr(self, "channel_id", None) is not None or self.is_interactive:
                    return ""  # Mythic already shows the operator's message
                return f"👤> {content}\n"  # PayloadType first turn only

        elif isinstance(message, AIMessage):
            # Handle AIMessages with content and/or tool calls
            output = ""

            # Get agent name from message or parameter (matches existing format)
            msg_agent_name = getattr(message, 'name', None) or agent_name or "AI"

            # Extract text content
            text_content = ""
            if isinstance(message.content, str):
                text_content = message.content.strip()
            elif isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_content += block.get("text", "").strip() + " "
                text_content = text_content.strip()

            # Show text content with agent name in brackets (matches existing format)
            if text_content:
                if getattr(self, "channel_id", None) is not None:
                    output += f"{text_content}\n"
                else:
                    output += f"🤖[{msg_agent_name}]> {text_content}\n"

            # Show tool calls as text (verbose PayloadType task path only). On the chat path the
            # collapsible tool-use card renders each request+response, so suppress the redundant
            # `🛠️ Tool Request` text here — otherwise the operator sees the card AND duplicate text.
            _chat_path = getattr(self, "channel_id", None) is not None
            if self.verbose and not _chat_path and hasattr(message, "tool_calls") and message.tool_calls:
                for tc in message.tool_calls:
                    tool_name = tc.get("name", "unknown")
                    tool_args = tc.get("args", {})
                    tool_id = tc.get("id", "unknown")
                    output += f"🛠️[{msg_agent_name}:{tool_id}]> Tool Request: '{tool_name}', Args: '{tool_args}'\n"

            return output

        elif isinstance(message, ToolMessage):
            # Tool results: the chat path renders these inside the tool-use card's Details (result_preview),
            # so suppress the redundant `🔧 Tool Response` text there. Task path keeps the verbose line.
            if getattr(self, "channel_id", None) is not None:
                return ""
            if not self.verbose:
                return ""
            tool_id = message.tool_call_id or "unknown"
            content = str(message.content)

            # Use agent_name from the event (which node produced this ToolMessage)
            display_agent = agent_name or "unknown"
            return f"🔧[{display_agent}:{tool_id}]> Tool Response: {content}\n"

        else:
            # Unknown message type
            logger.warning(f"Unknown message type for streaming: {type(message)}")
            return ""

    async def initialize(self):
        """ Initialize the model's graph and Mythic client."""
        # Ensure logger has handlers before any logging calls
        ensure_logger_initialized()
        #logger.info("🚀 Starting Model initialization...")
        force_flush_all_handlers()

        # Initialize tool cache
        await self.tool_cache.initialize()

        self.mythic_client = MythicTools(
            agent_task_id=self.agent_task_id,
            operation_id=self.operation_id,
            channel_id=self.channel_id,
            apitoken_id=self.apitoken_id,
            preauth_client=self._mythic_preauth_client,
        )
        self.mythic_client.set_mechanic_repair_resolver(self._resolve_capability_mechanic)
        self.mythic_client.set_capability_command_observer(self._emit_capability_command_card)
        self.mythic_client.set_execution_observer(self._emit_execution_event)
        await self.mythic_client.login()
        # Scope preflight (Section 8A P1): learn the bot token's granted scopes and disable guarded tools
        # it can't use BEFORE the graph attaches them (get_tools skips disabled ones). No-op on the task
        # path and whenever scopes are unknown (whoami_scopes returns None). Never blocks initialize().
        try:
            granted = await self.mythic_client.whoami_scopes()
            self.mythic_client.apply_scope_gating(granted)
        except Exception as e:
            logger.debug(f"Scope preflight skipped during initialization: {e}")
        try:
            await self.mythic_client._ensure_engagement_key()
        except Exception as e:
            logger.debug(f"Engagement ledger key resolution skipped during initialization: {e}")

        # CRITICAL: After RPC call, check if logger still has handlers
        ensure_logger_initialized()
        logger.info("✅ Mythic client logged in, starting dynamic data fetch")
        force_flush_all_handlers()

        # Fetch dynamic data for agent prompts before building graph
        await self._fetch_dynamic_data()

        ensure_logger_initialized()
        logger.info("✅ Dynamic data fetch completed, building graph")
        force_flush_all_handlers()

        if not self.graph:
            # Build and compile the graph
            self.graph = (
            StateGraph(SageState)
            .add_node("Supervisor", self._supervisor_agent())
            .add_node("Generalist", self._generalist_agent())
            .add_node("Mythic_Operator", self._mythic_operator_agent())
            .add_node("Mythic_Payload", self._mythic_payload_agent())
            .add_node("BloodHound", self._bloodhound_agent())
            .add_node("MCP_Manager", self._mcp_manager_agent())
            .add_node("Autonomous_Executor", self._autonomous_executor_node)
            .add_edge(START, "Supervisor")
            .add_edge("Generalist", "Supervisor")
            .add_edge("Mythic_Payload", "Supervisor")
            .add_edge("BloodHound", "Supervisor")
            .add_edge("MCP_Manager", "Supervisor")
            # The Operator ingests collections IN-PROCESS (ingest_collection → in-memory upload to BloodHound),
            # so there is no cross-agent ingest handoff to force — the Operator returns to the Supervisor as
            # normal, which then routes to the BloodHound agent for attack-path ANALYSIS.
            .add_edge("Mythic_Operator", "Supervisor")
            .add_edge("Autonomous_Executor", "Supervisor")
            .compile(checkpointer=self.memory, name="Sage")
        )

    async def _resolve_capability_mechanic(self, request: dict) -> dict | None:
        """Propose one payload mechanic substitute for an already-fixed capability operation."""
        if self.llm is None:
            return None
        try:
            from . import mechanic_repair
        except ImportError:
            import mechanic_repair
        try:
            response = await self.llm.ainvoke([
                SystemMessage(content=(
                    "You are a bounded payload mechanic resolver. The capability, generic operation, artifact "
                    "contract, and verifier are fixed. Return one JSON substitute from the supplied live command "
                    "surface or an empty command when no substitute exists. Do not add steps or change intent."
                )),
                HumanMessage(content=mechanic_repair.build_prompt(request)),
            ])
            return mechanic_repair.parse_candidate(response)
        except Exception as exc:
            logger.info(f"🧭 [mechanic-repair] model resolver failed: {exc}")
            return None

    def set_verbose(self, verbose: bool):
        """
        Set the verbosity of the model.
        :param verbose: If True, the model will print all User & AI messages.
        """
        self.verbose = verbose

    # Cache-aware wrapper methods for Mythic tools
    async def get_commands_for_payload_cached(self, payload: str, ttl_seconds: int = 3600) -> Any:
        """Get commands for a payload type with caching.

        Args:
            payload: The payload type name (e.g., "sage", "merlin")
            ttl_seconds: Cache TTL in seconds (default: 1 hour)

        Returns:
            Command data for the payload (parsed as dict/list, not JSON string)
        """
        logger.debug(f"get_commands_for_payload_cached called for payload '{payload}'")

        # Check cache first
        cached = await self.tool_cache.get("get_all_commands_for_payloadtype", payload)
        if cached is not None:
            # Defensive: If cache contains old string data (from before JSON parsing was added),
            # parse it now to ensure consistent return type
            if isinstance(cached, str):
                logger.warning(f"⚠️  Cached data for '{payload}' is a JSON string (old format), parsing now")
                try:
                    cached = json.loads(cached)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse cached JSON string for '{payload}': {e}")
                    # Invalidate bad cache entry
                    await self.tool_cache.invalidate("get_all_commands_for_payloadtype", payload)
                    # Fall through to fetch fresh data below
                    cached = None

            if cached is not None:
                logger.info(f"✅ CACHE HIT: Using cached commands for payload '{payload}'")
                force_flush_all_handlers()
                return cached

        # Cache miss - fetch from API
        logger.info(f"❌ CACHE MISS: Fetching commands for '{payload}' from Mythic API")
        force_flush_all_handlers()
        if self.mythic_client is None:
            raise ValueError("Mythic client not initialized")

        result_json_str = await self.mythic_client.get_all_commands_for_payloadtype(payload)

        # Parse JSON string to Python object for better caching and display
        try:
            result = json.loads(result_json_str) if isinstance(result_json_str, str) else result_json_str
            logger.debug(f"Parsed commands result type: {type(result)}")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse commands JSON for payload '{payload}': {e}")
            result = result_json_str  # Fall back to string if parsing fails

        # Cache the parsed result
        await self.tool_cache.set("get_all_commands_for_payloadtype", payload, result, ttl_seconds)
        logger.info(f"💾 Cached commands for payload '{payload}' with TTL {ttl_seconds}s")
        force_flush_all_handlers()

        return result

    async def get_callbacks_cached(self, ttl_seconds: int = 60) -> Any:
        """Get active callbacks with caching.

        Args:
            ttl_seconds: Cache TTL in seconds (default: 60 seconds for more frequent updates)

        Returns:
            Active callbacks data
        """
        # Check cache first
        cached = await self.tool_cache.get("get_all_active_callbacks")
        if cached is not None:
            logger.info("Using cached active callbacks")
            return cached

        # Cache miss - fetch from API
        logger.info("Cache miss for active callbacks - fetching from API")
        if self.mythic_client is None:
            raise ValueError("Mythic client not initialized")

        result = await self.mythic_client.get_all_active_callbacks()

        # Cache the result
        await self.tool_cache.set("get_all_active_callbacks", None, result, ttl_seconds)

        return result

    async def invalidate_tool_cache(self, tool_name: str, params: Any = None):
        """Invalidate cache for a specific tool or all entries for that tool.

        Args:
            tool_name: Name of the tool to invalidate
            params: Optional parameters to match (if None, invalidates all entries for this tool)
        """
        await self.tool_cache.invalidate(tool_name, params)

    def _wrap_create_agent(self, agent_runnable, state_key: str, node_name: str):
        """
        Wrap a create_agent runnable so it only sees & updates its own message list
        while keeping a global 'messages' list for tool compatibility.

        CRITICAL: Worker agents (non-Supervisor) will copy their responses back to the
        Supervisor's channel so the Supervisor can see what work was completed.
        """
        async def _ainvoke(state: SageState | dict, config=None):
            if isinstance(state, dict):
                channel = state.get(state_key)
                if channel is None:
                    channel = []
                    state[state_key] = channel
            else:
                channel = getattr(state, state_key, []) or []

            # Store original channel length to detect new messages
            original_channel_length = len(channel)

            # Ensure at least one message (Anthropic Bedrock requires non-empty messages list)
            if len(channel) == 0:
                channel.append(SystemMessage(content=f"{node_name} context start"))

            # Remove orphan tool_result blocks
            seen_tool_use_ids = set()
            cleaned_channel = []
            for msg in channel:
                if isinstance(msg, AIMessage):
                    for tc in (getattr(msg, "tool_calls", None) or []):
                        tc_id = tc.get("id")
                        if tc_id:
                            seen_tool_use_ids.add(tc_id)
                    cleaned_channel.append(msg)
                elif isinstance(msg, ToolMessage):
                    tc_id = getattr(msg, "tool_call_id", None)
                    if tc_id and tc_id in seen_tool_use_ids:
                        cleaned_channel.append(msg)
                else:
                    cleaned_channel.append(msg)
            channel = cleaned_channel

            try:
                if node_name == "Supervisor":
                    for active_agent in list(self._active_delegations):
                        await self._close_delegation(active_agent)
                elif (
                    state_key != "supervisor_messages"
                    and self._response_emitter is not None
                    and hasattr(self._response_emitter, "emit_subagent_status")
                ):
                    delegated_message = next(
                        (
                            msg
                            for msg in reversed(channel)
                            if isinstance(msg, HumanMessage)
                            and msg.additional_kwargs.get("_delegated_to") == node_name
                        ),
                        None,
                    )
                    if delegated_message is not None:
                        instruction = _message_content_as_text(delegated_message.content).strip()
                        title = str(delegated_message.additional_kwargs.get("_handoff_title") or "").strip()
                        source_seq = _get_seq(delegated_message)
                        await self._open_delegation(node_name, instruction, source_seq, title=title)
            except Exception as e:
                logger.debug(f"delegation lifecycle entry failed (non-fatal): {e}")

            # Create callback handler to capture ALL messages during agent execution
            # This captures the first AIMessage (with tool_calls) that LangChain's react agent
            # would otherwise "consume" during its internal tool execution loop
            # Pass streaming functions so messages are streamed immediately as they're captured
            delegation_id = self.current_delegation_id(node_name)
            callback_handler = MessageCaptureCallback(
                agent_name=node_name,
                stream_func=self._stream_message_to_mythic,
                format_func=self._format_message_for_streaming,
                tool_use_func=self._emit_tool_use_card,
                tool_source_func=self._classify_tool_source,
                agent_text_func=self._emit_agent_text,
                handback_summary_func=self._capture_delegation_final_summary,
                delegation_id=delegation_id,
                delegation_name=node_name if delegation_id is not None else None,
            )

            # Merge callback into config - handle both list and CallbackManager types
            invoke_config = dict(config) if config else {}
            existing_callbacks = invoke_config.get("callbacks")
            if existing_callbacks is None:
                invoke_config["callbacks"] = [callback_handler]
            elif isinstance(existing_callbacks, list):
                invoke_config["callbacks"] = existing_callbacks + [callback_handler]
            else:
                # existing_callbacks is a CallbackManager - just use our callback
                # The outer config callbacks will still be called at the graph level
                invoke_config["callbacks"] = [callback_handler]

            # Sanitize messages before invoking agent to prevent "multiple non-consecutive system messages" error
            sanitized_channel = self._sanitize_messages(channel)

            # Autonomous keep-going: in an autonomous solve, the Mythic_Operator must not yield control to the
            # Supervisor by accident (a react agent ends its turn whenever the LLM emits no tool call). If the
            # Operator finishes a react run WITHOUT an explicit yield (no recursion_handback flag, no transfer_*/
            # summarize_and_handback tool call), re-invoke it with a continuation nudge — bounded by a cap — so the
            # only ways out are an explicit handback, a cross-agent transfer, or the cap. Base mode and every other
            # agent are unaffected (the loop runs exactly once and breaks immediately = current behavior).
            _mythic_operator = node_name == "Mythic_Operator"
            _autonomous_operator = bool(self._autonomous_solve) and _mythic_operator
            _bounded_one_action_request = (
                _is_bounded_one_action_capability_request(channel)
                if _mythic_operator else False
            )
            _continue_count = 0
            _agent_input = sanitized_channel
            updated_channel = channel  # safe default if we halt before the first invocation
            while True:
                # Cooperative kill switch INSIDE the autonomous continue-loop: an operator `stop`/`exit` set
                # _stop_requested, but the outer astream only checks it between top-level super-steps — so
                # without this guard `stop` keeps issuing real offensive Mythic tasks until the continue-cap
                # (6) drains. Checked before each (re-)invocation: any in-flight call finishes, then we halt.
                if getattr(self, "_stop_requested", False):
                    break
                activity_token = None
                if delegation_id is not None:
                    activity_token = MCPManager.set_execution_activity({
                        "id": delegation_id,
                        "name": node_name,
                    })
                try:
                    result = await agent_runnable.ainvoke({"messages": _agent_input}, invoke_config)
                finally:
                    if activity_token is not None:
                        MCPManager.reset_execution_activity(activity_token)
                updated_channel = result.get("messages", channel)
                if not _autonomous_operator:
                    break
                if result.get("recursion_handback"):
                    break  # explicit handback — let upstream flag handling end/route
                _new_msgs = updated_channel[original_channel_length:]
                if _bounded_one_action_request and _terminal_execute_capability_payload(_new_msgs):
                    break  # the delegated task explicitly said one capability action, then stop
                _explicit_yield = any(
                    isinstance(m, AIMessage) and any(
                        ((tc.get("name") or "").startswith("transfer_to_")) or ((tc.get("name") or "") in ("summarize_and_handback", "handback_to_supervisor"))
                        for tc in (getattr(m, "tool_calls", None) or [])
                    )
                    for m in _new_msgs
                )
                if _explicit_yield:
                    break  # cross-agent transfer or explicit handback already routed
                _last_ai = next((m for m in reversed(_new_msgs) if isinstance(m, AIMessage)), None)
                _finished_plainly = _last_ai is not None and not (getattr(_last_ai, "tool_calls", None))
                if _finished_plainly and _continue_count < _AUTONOMOUS_OPERATOR_CONTINUE_CAP:
                    _continue_count += 1
                    _base_nudge_text = (
                        "[autonomous-continue] You ended your turn without reaching the objective and without an explicit "
                        "handback. In autonomous mode you must NOT stop after a sub-goal — execute the NEXT "
                        "action from your own REMAINING list now. To hand off you MUST call a tool (a plain stop just loops "
                        "you back here): call `handback_to_supervisor(reason, summary)` when the next step needs another "
                        "agent (BloodHound for graph work, Mythic_Payload for a build) or the objective is complete — the "
                        "Supervisor will route and the solve continues. Use `summarize_and_handback` ONLY at the recursion "
                        "limit (it pauses for the operator). Do not stop silently."
                    )
                    # Engagement-state-aware nudge (fail-open): prepend a FRESH rendered snapshot of the
                    # observed engagement state plus a "don't re-propose achieved hops" directive.
                    _nudge_text = _base_nudge_text
                    try:
                        _rendered_state = None
                        try:
                            _state = await self._build_current_engagement_state()
                            if _state is not None:
                                try:
                                    from . import engagement_state as _es
                                except ImportError:
                                    import engagement_state as _es
                                _rendered_state = _es.render_engagement_state(_state)
                        except Exception:
                            _rendered_state = None  # fail-open to the plain base nudge
                        _nudge_text = self._autonomous_nudge_content(_base_nudge_text, _rendered_state)
                    except Exception:
                        _nudge_text = _base_nudge_text  # fail-open: never break the continue-loop
                    _nudge = HumanMessage(
                        content=_nudge_text,
                        additional_kwargs={
                            "_synthetic_nudge": "autonomous_operator_continue",
                            "_hide_from_stream": True,
                        },
                    )
                    _agent_input = self._sanitize_messages(list(updated_channel) + [_nudge])
                    continue
                break

            # With operator.add reducer, we only pass the NEW messages, not the full list
            returned_messages = [
                msg for msg in updated_channel[original_channel_length:]
                if not _is_internal_human_message(msg)
            ]

            # Merge captured messages (from callback) with returned messages
            # The callback captures ALL messages including the first tool-calling AIMessage
            # that LangChain's react agent doesn't return
            captured = callback_handler.captured_messages
            logger.info(f"🎯 [{node_name}] Callback captured {len(captured)} messages, agent returned {len(returned_messages)}")

            # Build a unified message list:
            # - Use captured messages as the primary source (they're in chronological order)
            # - Add any returned messages that weren't captured (rare, but possible)
            #
            # Deduplication: For AIMessages, match by tool_call IDs if present; for ToolMessages, match by tool_call_id
            seen_tool_call_ids: set[str] = set()  # AIMessage tool_call IDs
            seen_tool_result_ids: set[str] = set()  # ToolMessage tool_call_ids

            # First pass: collect IDs from captured messages
            for msg in captured:
                if isinstance(msg, AIMessage):
                    for tc in getattr(msg, 'tool_calls', []) or []:
                        tc_id = tc.get('id')
                        if tc_id:
                            seen_tool_call_ids.add(tc_id)
                elif isinstance(msg, ToolMessage):
                    tc_id = getattr(msg, 'tool_call_id', None)
                    if tc_id:
                        seen_tool_result_ids.add(tc_id)

            # Add any returned messages that weren't captured (shouldn't happen often)
            new_messages_from_agent = list(captured)

            # Build content hash set for captured AIMessages (for deduplication of messages without tool calls)
            captured_ai_content_hashes = set()
            for msg in captured:
                if isinstance(msg, AIMessage):
                    # Hash the content for comparison
                    content_str = str(msg.content) if msg.content else ""
                    captured_ai_content_hashes.add(hash(content_str))

            for msg in returned_messages:
                is_duplicate = False
                if isinstance(msg, AIMessage):
                    tool_calls = getattr(msg, 'tool_calls', []) or []
                    if tool_calls:
                        # Check if all tool_call IDs are already seen
                        msg_tc_ids = {tc.get('id') for tc in tool_calls if tc.get('id')}
                        if msg_tc_ids and msg_tc_ids.issubset(seen_tool_call_ids):
                            is_duplicate = True
                    else:
                        # For AIMessages without tool calls, check content hash
                        content_str = str(msg.content) if msg.content else ""
                        if hash(content_str) in captured_ai_content_hashes:
                            is_duplicate = True
                            logger.debug(f"🔁 [{node_name}] Skipping duplicate AIMessage (same content as captured)")
                elif isinstance(msg, ToolMessage):
                    tc_id = getattr(msg, 'tool_call_id', None)
                    if tc_id and tc_id in seen_tool_result_ids:
                        is_duplicate = True

                if not is_duplicate:
                    new_messages_from_agent.append(msg)
                    logger.debug(f"➕ [{node_name}] Added non-captured message: {type(msg).__name__}")

            # A guard middleware (e.g. BloodHound-not-connected) can short-circuit BEFORE the model runs:
            # nothing is captured and create_agent does not return its before_model-injected message, so the
            # node would otherwise produce 0 messages and the Supervisor would re-delegate forever. If a node
            # stashed a pending guard message and produced nothing, surface it here as the node's result so it
            # gets seq-tagged and copied to the Supervisor channel below — breaking the loop.
            _guard_msg = getattr(self, "_pending_guard_message", None)
            if _guard_msg is not None:
                self._pending_guard_message = None
                if not new_messages_from_agent:
                    new_messages_from_agent = [_guard_msg]
                    logger.info(f"🩸 [{node_name}] surfaced pending guard message (agent produced no messages)")

            # Tag new messages with sequence numbers for chronological ordering
            # Compute from max of existing messages to avoid collisions with handoff-created messages
            max_seq = 0
            for ch_key in ["supervisor_messages", "generalist_messages", "mythic_operator_messages", "mythic_payload_messages", "mcp_manager_messages", "bloodhound_messages"]:
                for existing_msg in state.get(ch_key, []):
                    seq = _get_seq(existing_msg)
                    if seq > max_seq:
                        max_seq = seq
            # Also check the new messages we're about to tag (some may already have seq from elsewhere)
            for msg in new_messages_from_agent:
                seq = _get_seq(msg)
                if seq > max_seq:
                    max_seq = seq
            next_seq = max_seq + 1
            logger.debug(f"🔢 [{node_name}] Computed next_seq={next_seq} from max_seq={max_seq}")
            for msg in new_messages_from_agent:
                if "_seq" not in msg.additional_kwargs:
                    _tag_msg(msg, next_seq)
                    next_seq += 1
            # Update Model's counter to stay in sync
            self._message_seq = next_seq
            self.state["_message_seq"] = next_seq

            # Debug: log what messages we got from the agent
            logger.info(f"📦 [{node_name}] returned {len(new_messages_from_agent)} new messages:")
            for idx, msg in enumerate(new_messages_from_agent):
                msg_type = type(msg).__name__
                tool_calls = getattr(msg, 'tool_calls', None) or []
                content_preview = str(msg.content)[:50] if msg.content else "(empty)"
                logger.info(f"  [{idx}] {msg_type}: content='{content_preview}', tool_calls={len(tool_calls)}")
            force_flush_all_handlers()

            update: dict[str, Any] = {
                state_key: new_messages_from_agent,  # Only new messages for operator.add
                "messages": new_messages_from_agent,  # keep legacy/global mirror
            }

            if _mythic_operator and _bounded_one_action_request:
                terminal_payload = _terminal_execute_capability_payload(new_messages_from_agent)
                if terminal_payload is not None:
                    terminal_report = _terminal_execute_capability_report(
                        terminal_payload,
                        bounded_one_action=True,
                    )
                    final_msg = AIMessage(
                        content=terminal_report,
                        name="Supervisor",
                        additional_kwargs={
                            "_is_final_report": True,
                            "_terminal_execute_capability": True,
                        },
                    )
                    _tag_msg(final_msg, next_seq)
                    next_seq += 1
                    self._message_seq = next_seq
                    self.state["_message_seq"] = next_seq
                    terminal_update: dict[str, Any] = {
                        state_key: new_messages_from_agent,
                        "messages": new_messages_from_agent + [final_msg],
                        "supervisor_messages": [final_msg],
                        "_message_seq": next_seq,
                    }
                    for flag in ("recursion_summary_requested", "recursion_handback"):
                        if flag in result:
                            terminal_update[flag] = result[flag]
                    terminal_update["recursion_summary_requested"] = False
                    terminal_update["recursion_handback"] = True
                    logger.info(
                        f"✅ [{node_name}] bounded one-action execute_capability result is terminal; ending graph"
                    )
                    await self._close_delegation(node_name, content=terminal_report, status="finished")
                    return Command(goto=END, update=terminal_update)

            # CRITICAL FIX: If this is a worker agent (not Supervisor), copy its response
            # back to the Supervisor's channel AND to the calling agent's channel (if worker-to-worker handoff)
            if node_name != "Supervisor" and state_key != "supervisor_messages":
                if new_messages_from_agent:
                    # Filter to only include substantive responses
                    # Include AIMessages with text content OR tool_calls (for visibility)
                    # Include ToolMessages except handoff confirmations
                    substantive_messages = []
                    for msg in new_messages_from_agent:
                        if isinstance(msg, AIMessage):
                            has_tool_calls = bool(getattr(msg, 'tool_calls', None))
                            has_text_content = False
                            if isinstance(msg.content, str) and msg.content.strip():
                                has_text_content = True
                            elif isinstance(msg.content, list):
                                has_text_content = any(
                                    item.get("type") == "text" and item.get("text", "").strip()
                                    for item in msg.content if isinstance(item, dict)
                                )
                            # Include if has text OR has tool_calls (so tool requests are visible)
                            if has_text_content or has_tool_calls:
                                substantive_messages.append(msg)
                        elif isinstance(msg, ToolMessage):
                            # Include tool results that aren't just handoff confirmations
                            if not msg.name or not msg.name.startswith("transfer_to_"):
                                substantive_messages.append(msg)

                    if substantive_messages:
                        capped_messages = [_cap_message_for_copy(m) for m in substantive_messages]
                        # Create a header message to show which agent responded
                        # Mark with _is_completion_header for semantic filtering (vs string matching)
                        response_header = AIMessage(
                            content=f"[{node_name} completed task]",
                            name=node_name,
                            additional_kwargs={"_is_completion_header": True}
                        )
                        _tag_msg(response_header, self._next_seq())

                        def _message_text_content(msg: BaseMessage) -> str:
                            if isinstance(msg.content, str):
                                return msg.content
                            if isinstance(msg.content, list):
                                return "\n".join(
                                    item.get("text", "")
                                    for item in msg.content
                                    if isinstance(item, dict)
                                    and item.get("type") == "text"
                                    and item.get("text", "").strip()
                                )
                            return ""

                        terminal_payload = _terminal_execute_capability_payload(new_messages_from_agent)
                        if terminal_payload is not None:
                            summary_text = _terminal_execute_capability_report(terminal_payload)
                        else:
                            worker_text_parts = []
                            for msg in substantive_messages:
                                if isinstance(msg, AIMessage):
                                    msg_text = _message_text_content(msg).strip()
                                    if msg_text:
                                        worker_text_parts.append(msg_text)
                            summary_text = "\n\n".join(worker_text_parts).strip()

                        tool_contents = [
                            msg.content
                            for msg in new_messages_from_agent
                            if isinstance(msg, ToolMessage)
                            and isinstance(msg.content, str)
                            and (not msg.name or not msg.name.startswith("transfer_to_"))
                        ]
                        joined_tool_contents = "\n\n".join(tool_contents)

                        if not summary_text and self.llm is not None:
                            truncated_tool_contents = joined_tool_contents[:12000]
                            synthesis_prompt = (
                                "You are condensing a sub-agent's work for the orchestrator. From the tool results below, "
                                "write a concise, SELF-CONTAINED summary of the concrete findings (include actual "
                                "names/values/results, not just 'done'). 5-10 sentences. Tool results:\n"
                                f"{truncated_tool_contents}"
                            )
                            try:
                                resp = await self.llm.ainvoke(synthesis_prompt)
                                resp_content = resp.content if hasattr(resp, 'content') else str(resp)
                                summary_text = (resp_content or "").strip()
                            except Exception as e:
                                logger.debug(f"⚠️  Summary synthesis failed for {node_name}: {e}")
                                summary_text = ""

                        if not summary_text:
                            preview = joined_tool_contents[:1500].strip()
                            if preview:
                                summary_text = f"[summary synthesis unavailable — raw tool output preview]\n{preview}"
                            else:
                                summary_text = "[summary synthesis unavailable — raw tool output preview]\n[no tool output captured]"

                        await self._close_delegation(node_name, content=summary_text, status="finished")
                        summary_ai_msg = AIMessage(content=summary_text, name=node_name)
                        _tag_msg(summary_ai_msg, self._next_seq())

                        # ALWAYS copy to Supervisor channel (only the NEW messages with operator.add)
                        update["supervisor_messages"] = [response_header, summary_ai_msg]
                        logger.info(f"✅ Copied summary from {node_name} to Supervisor channel ({len(summary_text)} chars)")

                        # ALSO copy to calling agent channel if this was a worker-to-worker handoff
                        calling_agent = state.get("_last_calling_agent")
                        if calling_agent and calling_agent != "Supervisor":
                            channel_map = {
                                "Mythic_Operator": "mythic_operator_messages",
                                "Mythic_Payload": "mythic_payload_messages",
                                "Generalist": "generalist_messages",
                                "BloodHound": "bloodhound_messages",
                                "MCP_Manager": "mcp_manager_messages",
                            }
                            calling_agent_channel_key = channel_map.get(calling_agent)
                            if calling_agent_channel_key:
                                # With operator.add, only provide the new messages to append
                                update[calling_agent_channel_key] = [response_header] + capped_messages
                                logger.info(f"✅ Copied {len(substantive_messages)} substantive messages from {node_name} to {calling_agent} channel (worker-to-worker handoff)")

                        force_flush_all_handlers()
                    else:
                        logger.debug(f"⏭️  No substantive messages from {node_name} to copy to Supervisor")

            for flag in ("recursion_summary_requested", "recursion_handback"):
                if flag in result:
                    update[flag] = result[flag]
            return update
        _ainvoke.__name__ = node_name
        return _ainvoke

    async def _build_current_engagement_state(self):
        """Best-effort build of the current EngagementState the same way the issue hook does.

        Returns an ``engagement_state.EngagementState`` or ``None`` on ANY error — never raises.
        Mirrors ``MythicTools._engagement_issue_hook``: reconcile live footholds (fail-open to []) and
        snapshot the recorded hops. Graph facts are intentionally skipped (optional).
        """
        try:
            mythic_client = self.mythic_client
            if mythic_client is None:
                return None
            try:
                from . import access_reconciler, engagement_state
            except ImportError:
                import access_reconciler
                import engagement_state

            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()

            try:
                footholds = await access_reconciler.reconcile_access(mythic_client, now)
            except Exception:
                footholds = []  # fail-open: state without footholds is still useful

            hops = list(getattr(mythic_client, "_engagement_hops", []) or [])

            try:
                objective = mythic_client._engagement_objective()
            except Exception:
                objective = "sage-engagement"
            engagement_id, runtime_scope = _runtime_engagement_scope(mythic_client)

            return engagement_state.EngagementState(
                objective=objective,
                footholds=footholds,
                hops=hops,
                engagement_id=engagement_id,
                runtime_scope=runtime_scope,
            )
        except Exception:
            return None

    def _render_engagement_state_for_injection(self) -> str | None:
        """Cheap, synchronous, in-memory render of the observed engagement state for PER-TURN injection
        by `_EngagementStateMiddleware`. Returns None — and the middleware injects nothing — when this is not
        an autonomous solve, there is no mythic_client, or there is no observed state yet. NO network: reads the
        in-memory incremental hop ledger plus cached footholds. Never raises (caller also guards)."""
        try:
            if not bool(getattr(self, "_autonomous_solve", False)):
                return None
            mythic_client = getattr(self, "mythic_client", None)
            if mythic_client is None:
                return None
            hops = list(getattr(mythic_client, "_engagement_hops", []) or [])
            footholds = list(getattr(mythic_client, "_engagement_footholds", []) or [])
            if not hops and not footholds:
                return None
            try:
                from . import engagement_state as _es
            except ImportError:
                import engagement_state as _es
            try:
                objective = mythic_client._engagement_objective()
            except Exception:
                objective = "sage-engagement"
            engagement_id, runtime_scope = _runtime_engagement_scope(mythic_client)
            # Include cached graph facts so durable-hop corroboration markers can be computed.
            graph_facts = list(getattr(mythic_client, "_engagement_graph_facts", []) or [])
            state = _es.EngagementState(
                objective=objective, footholds=footholds, hops=hops, graph_facts=graph_facts,
                engagement_id=engagement_id, runtime_scope=runtime_scope,
            )
            rendered = _es.render_engagement_state(state)
            if not rendered:
                return None
            # Pair the observed-state block with the imperative directive so the per-turn injection is
            # an instruction ("ACHIEVED = DONE"), not just a status list the operator can talk past.
            return f"{rendered}\n\n{_ENGAGEMENT_STATE_DIRECTIVE}"
        except Exception:
            return None

    def _current_engagement_state_snapshot(self, *, require_autonomous: bool = True):
        """Cheap in-memory EngagementState snapshot for terminal objective checks.

        This intentionally does not poll Mythic or BloodHound. It uses the same cached footholds, hops, and
        graph facts that the issue hook/per-turn injection maintain, so the stop check cannot mint new
        facts; it can only notice that already-recorded proof satisfies the current objective.
        """
        try:
            if require_autonomous and not bool(getattr(self, "_autonomous_solve", False)):
                return None
            mythic_client = getattr(self, "mythic_client", None)
            if mythic_client is None:
                return None
            hops = list(getattr(mythic_client, "_engagement_hops", []) or [])
            footholds = list(getattr(mythic_client, "_engagement_footholds", []) or [])
            if not hops or not footholds:
                return None
            try:
                from . import engagement_state as _es
            except ImportError:
                import engagement_state as _es
            try:
                objective = mythic_client._engagement_objective()
            except Exception:
                objective = "sage-engagement"
            engagement_id, runtime_scope = _runtime_engagement_scope(mythic_client)
            return _es.EngagementState(
                objective=objective,
                footholds=footholds,
                hops=hops,
                graph_facts=list(getattr(mythic_client, "_engagement_graph_facts", []) or []),
                engagement_id=engagement_id,
                runtime_scope=runtime_scope,
            )
        except Exception:
            return None

    def _autonomous_handoff_step_redirect(
        self,
        agent_name: str,
        handoff_instruction: str,
        state: dict,
    ) -> _HandoffDirective | None:
        """Compile autonomous handoffs from the ledger-selected next capability.

        Specialist handbacks and Supervisor routing are useful for coordination, but
        they are not allowed to choose executable tradecraft when the engagement
        ledger already exposes a grounded next capability. This synchronous gate
        reads only cached footholds/hops/graph facts and rewrites Mythic/BloodHound
        handoffs to a concrete execute_capability request.
        """
        if agent_name not in {"Mythic_Operator", "BloodHound"}:
            return None
        if not bool(getattr(self, "_autonomous_solve", False)):
            return None
        snapshot = self._current_engagement_state_snapshot(require_autonomous=True)
        if snapshot is None:
            return None
        try:
            try:
                from . import capabilities
            except ImportError:
                import capabilities
            actions = capabilities.actions_from_state(snapshot)
        except Exception:
            actions = []
        if not actions:
            try:
                try:
                    from . import engagement_state as _es
                except ImportError:
                    import engagement_state as _es
                if _es.current_access_collection_missing(snapshot):
                    instruction = _compiled_autonomous_collection_instruction(
                        snapshot,
                        handoff_instruction=handoff_instruction,
                        requested_agent=agent_name,
                    )
                    if instruction:
                        return _handoff_directive("Mythic_Operator", instruction, "Collect BloodHound graph")
                phase = str(_es.engagement_phase(snapshot))
                if phase.startswith("BLOCKED"):
                    if _recent_bloodhound_blocker_observed(state):
                        instruction = _compiled_autonomous_blocked_report(
                            snapshot,
                            handoff_instruction=handoff_instruction,
                            requested_agent=agent_name,
                        )
                        if instruction:
                            return _handoff_directive("__terminal__", instruction, "Report blocked objective")
                    if agent_name == "Mythic_Operator":
                        instruction = _compiled_autonomous_blocked_bloodhound_instruction(
                            snapshot,
                            handoff_instruction=handoff_instruction,
                            requested_agent=agent_name,
                        )
                        if instruction:
                            return _handoff_directive("BloodHound", instruction, "Analyze graph blocker")
            except Exception:
                return None
            return None
        action = actions[0]
        # Stall detector: halt if the SAME hop is re-selected with no ledger progress for N consecutive steps
        # (a dead hop, e.g. a dcsync whose precondition/execution keeps failing) — recover instead of looping
        # and burning tokens. Skip when the objective is already a completion candidate, so a just-completed
        # run reports completion, not "stalled".
        try:
            _progress = len(snapshot.achieved_effects())
        except Exception:
            _progress = 0
        try:
            from . import engagement_state as _es_stall
        except ImportError:
            import engagement_state as _es_stall
        try:
            _complete = str(_es_stall.engagement_phase(snapshot)).startswith("COMPLETE-CANDIDATE")
        except Exception:
            _complete = False
        if not _complete and self._autonomous_stall_halt(_progress, _action_signature(action)):
            return _handoff_directive("__terminal__", _autonomous_stall_report(snapshot), "Report stalled objective")
        instruction = _compiled_autonomous_capability_instruction(
            action,
            snapshot,
            handoff_instruction=handoff_instruction,
            requested_agent=agent_name,
        )
        return _handoff_directive(
            "Autonomous_Executor",
            instruction,
            _autonomous_capability_handoff_title(action),
        )

    def _note_capability_outcome(self, payload, turn_key: str = "") -> None:
        """Observe a terminal execute_capability result (control-state P0 loop-breaker).

        When the SAME blocker (capability + reason) recurs with NO progress since last seen, request a clean
        stop instead of letting the supervisor re-delegate the identical blocked action — the 1116 461K-token
        loop. Keyed on the worker's blocker, so it is immune to the supervisor's paraphrasing (which defeated the
        action-signature stall detector). `turn_key` dedups multiple middleware fires WITHIN one worker turn (the
        execute_capability tool-call id is fresh per re-delegation, so cross-turn repeats still count). Fail-open:
        never raises into the agent loop."""
        try:
            if not isinstance(payload, dict) or self._loop_breaker is None:
                return
            try:
                from . import worker_outcome as _wo
            except ImportError:
                import worker_outcome as _wo
            # All dedup (incl. the empty-turn_key guard), epoch, and decision logic lives in the pure
            # observe_capability_outcome (unit-tested) — this stays a thin, fail-open shell.
            should_halt = _wo.observe_capability_outcome(
                self._loop_breaker, str(payload.get("capability") or ""), payload, turn_key)
            if should_halt and not getattr(self, "_stop_requested", False):
                self._stop_requested = True
                logger.warning(
                    "🛑 [terminal-blocker] same blocker recurred with no progress — halting the solve instead of "
                    "re-delegating it (control-state P0; kills the supervisor↔worker loop).")
        except Exception:
            pass

    def _autonomous_stall_halt(self, progress: int, action_sig=None) -> bool:
        """Track progress across autonomous capability steps. `progress` is the count of achieved effects;
        `action_sig` identifies the selected hop. Returns True only when the SAME hop has been re-selected with
        NO new progress for _AUTONOMOUS_STALL_LIMIT consecutive steps — the dead-loop signature. Real progress
        (count grows) OR moving to a DIFFERENT hop resets the counter, so a solve that keeps advancing — or that
        legitimately works through distinct multi-step precondition hops — never trips; only a genuinely stuck
        re-selection of one dead hop does. Pure counter; never raises."""
        last_progress = getattr(self, "_autonomous_stall_progress", None)
        last_sig = getattr(self, "_autonomous_stall_sig", None)
        progressed = last_progress is not None and progress > last_progress
        if progressed or action_sig != last_sig:
            self._autonomous_stall_count = 0
        else:
            self._autonomous_stall_count = getattr(self, "_autonomous_stall_count", 0) + 1
        self._autonomous_stall_progress = progress
        self._autonomous_stall_sig = action_sig
        if self._autonomous_stall_count >= _AUTONOMOUS_STALL_LIMIT:
            self._autonomous_stall_count = 0  # reset after signalling so a resumed solve gets a fresh window
            return True
        return False

    def _objective_completion_report(self, *, require_autonomous: bool = True) -> str | None:
        """Return a terminal objective-complete report when the observed state proves it."""
        state = self._current_engagement_state_snapshot(require_autonomous=require_autonomous)
        if state is None:
            return None
        try:
            from . import engagement_state as _es
        except ImportError:
            import engagement_state as _es
        try:
            if not str(_es.engagement_phase(state)).startswith("COMPLETE-CANDIDATE"):
                return None
            objective = str(getattr(state, "objective", "") or "").strip()
            credential_targets = _es._objective_credential_targets(objective)
            if credential_targets and _es.objective_effects_complete(state):
                achieved = set(state.achieved_effects())
                evidence = []
                for account, domain in sorted(credential_targets):
                    for effect in sorted(achieved):
                        if _es._credential_effect_satisfied(account, domain, {effect}):
                            evidence.append(effect)
                            break
                lines = ["Objective complete: verified credential material is recorded."]
                if objective:
                    lines.append(f"Objective: {objective}")
                if evidence:
                    lines.append("Proof:")
                    lines.extend(f"- `{effect}`" for effect in evidence)
                lines.append(
                    "Sage is stopping because the target objective is satisfied; "
                    "no further capability will be executed."
                )
                return "\n".join(lines)
            candidates = _es.objective_completion_candidates(state)
            if not candidates:
                return None
            targets = {str(t).casefold() for t in _es._objective_target_domains(objective)}
            if targets:
                candidates = [
                    c for c in candidates
                    if str(c.get("domain", "")).strip().casefold() in targets
                ]
            if not candidates:
                return None
            candidate = candidates[0]
            domain = str(candidate.get("domain") or "").strip()
            lines = [
                f"Objective complete: administrative-control proof is recorded for `{domain}`.",
            ]
            if objective:
                lines.append(f"Objective: {objective}")
            lines.append("Proof chain:")
            proof_items = [
                ("admin", candidate.get("admin_effect"), candidate.get("admin_task_id"), ""),
                ("access", candidate.get("access_effect"), candidate.get("access_task_id"), candidate.get("callback_id")),
                ("auth", candidate.get("auth_effect"), candidate.get("auth_task_id"), ""),
                ("key", candidate.get("key_effect"), candidate.get("key_task_id"), ""),
            ]
            for label, effect, task_id, callback_id in proof_items:
                effect_text = str(effect or "").strip()
                if not effect_text:
                    continue
                detail = f"- {label}: `{effect_text}`"
                if task_id:
                    detail += f" task={task_id}"
                if callback_id:
                    detail += f" cb={callback_id}"
                lines.append(detail)
            lines.append("Sage is stopping because the target objective is satisfied; no further capability will be executed.")
            return "\n".join(lines)
        except Exception:
            return None

    @staticmethod
    def _looks_like_explicit_objective_prompt(prompt: str) -> bool:
        """Conservatively detect a chat turn that asks Sage to pursue an engagement objective.

        This is routing only, not planning. It deliberately recognizes generic operator phrasing such as
        "Compromise the CORP domain" and "From the foothold, achieve administrative control of child.lab.local"
        while leaving read-only/scoped questions on the normal supervisor graph.
        """
        text = re.sub(r"\s+", " ", str(prompt or "").strip().casefold())
        if not text:
            return False

        # Read-only questions and negative instructions must not silently become execution requests.
        if re.match(
            r"^(?:why|what|when|where|who|which|how|explain|describe|summarize|show|list|inspect|"
            r"analy[sz]e|report|did|does|is|are|was|were|should)\b",
            text,
        ):
            return False
        if re.search(
            r"\b(?:do not|don't|never|avoid|stop|without)\b.{0,48}"
            r"\b(?:compromise|pwn|own|take over|achieve|obtain|gain|reach|escalate|continue|"
            r"prov(?:e|ing)|demonstrat(?:e|ing)|establish(?:ing)?|remote[- ]exec(?:ution)?)\b",
            text,
        ):
            return False

        # Common request wrappers should not hide an otherwise imperative objective.
        candidate = re.sub(
            r"^(?:(?:please|kindly)\s+|(?:can|could|would)\s+you\s+|(?:i\s+want\s+you\s+to)\s+)+",
            "",
            text,
        )
        # Leading prohibition: an instruction that OPENS with a hard negation is a prohibition regardless of how
        # much filler pads it before the objective verb. This closes the bounded-window bypass of the proximity
        # check above (e.g. "please do not, and I mean under no circumstances, compromise the domain") — for a
        # default-deny safety gate, over-declining a negated instruction is the safe direction.
        if re.match(r"^(?:do not|don'?t|never|no)\b", candidate):
            return False

        # Effect-backed bounded remote-execution objectives are explicit objectives too. Reuse the same generic
        # parser that completion uses instead of widening the gate with a host/topology regex. This keeps vague
        # "prove" requests denied while allowing the sealed form "prove bounded remote execution on HOST".
        try:
            try:
                from . import engagement_state as _es
            except ImportError:
                import engagement_state as _es
            if _es._objective_remote_exec_targets(candidate) and re.search(
                r"\b(?:compromise|pwn|own|take over|achieve|obtain|gain|reach|escalate|continue|"
                r"prove|demonstrate|establish)\b",
                candidate,
            ):
                return True
        except Exception:
            pass

        if not re.search(
            r"\b(?:compromise|pwn|own|take over|achieve|obtain|gain|reach|escalate|continue)\b",
            candidate,
        ):
            return False

        try:
            try:
                from . import engagement_state as _es
            except ImportError:
                import engagement_state as _es
            if _es._objective_target_domains(candidate):
                return True
        except Exception:
            pass

        if re.search(
            r"\b(?:domain admin(?:istrator)?s?|enterprise admin(?:istrator)?s?|"
            r"administrative control|engagement objective)\b",
            candidate,
        ):
            return True
        return bool(
            re.search(r"\b(?:compromise|pwn|own|take over)\b", candidate)
            and re.search(r"\b(?:domain|forest|enterprise)\b", candidate)
        )

    def _supervised_objective_controller_enabled_for_prompt(self, prompt: str) -> bool:
        """Whether this fresh supervised chat turn should use controller-native HITL."""
        return bool(
            getattr(self, "mode", "auto") == "supervised"
            and getattr(self, "command_name", "") == "chat"
            and _controller_flag_enabled()
            and _controller_hitl_flag_enabled()
            and self._looks_like_explicit_objective_prompt(prompt)
        )

    @staticmethod
    def _looks_like_casual_greeting(prompt: Any) -> bool:
        """Recognize short greetings that must remain a tool-free conversational turn."""
        text = re.sub(r"\s+", " ", _coerce_prompt_text(prompt).strip().lower())
        text = re.sub(r"[!.?]+$", "", text).strip()
        return bool(re.fullmatch(
            r"(?:hello|hi|hey|hello there|hi there|hey there|"
            r"good morning|good afternoon|good evening)(?: sage)?",
            text,
        ))

    async def _run_generalist_only_turn(self, prompt: Any) -> str:
        """Run one tool-free Generalist inference and end the turn.

        This is the provider-independent terminal route for casual greetings. It deliberately bypasses the
        Supervisor because a provider may otherwise continue routing after the Generalist has already answered.
        The Generalist callback writes into its trace card, so this route must also emit the final response to
        the main chat before completing the turn.
        """
        generalist = self._generalist_agent()
        delegated = HumanMessage(
            content=_coerce_prompt_text(prompt),
            additional_kwargs={
                "_delegated_to": "Generalist",
                "_handoff_title": "Conversation",
                "_hide_from_stream": True,
            },
        )
        _tag_msg(delegated, self._next_seq())
        self.state.setdefault("generalist_messages", []).append(delegated)
        update = await generalist(self.state, self._graph_run_config(self._session_thread_id()))
        final_message = None
        if isinstance(update, dict):
            final_message = next(
                (
                    msg
                    for msg in reversed(update.get("generalist_messages", []))
                    if isinstance(msg, AIMessage)
                    and not (getattr(msg, "tool_calls", None) or [])
                    and _message_content_as_text(msg.content).strip()
                ),
                None,
            )
            for channel in (
                "messages",
                "supervisor_messages",
                "generalist_messages",
                "_message_seq",
            ):
                if channel not in update:
                    continue
                if channel == "_message_seq":
                    self._message_seq = update[channel]
                    self.state[channel] = update[channel]
                else:
                    self.state.setdefault(channel, []).extend(
                        msg for msg in update[channel] if not _is_internal_human_message(msg)
                    )
        if final_message is None:
            final_message = AIMessage(
                content="I’m sorry, but I couldn’t produce a response for that turn.",
                name="Generalist",
            )
        formatted = self._format_message_for_streaming(final_message, agent_name="Generalist")
        if formatted:
            await self._stream_message_to_mythic(formatted)
        return ""

    def _controller_owned_solve(self) -> bool:
        """Whether the bounded execution kernel owns the current solve."""
        return bool(
            getattr(self, "_autonomous_solve", False)
            or getattr(self, "_supervised_objective_active", False)
        )

    def _should_use_controller(self, is_interactive: bool, prompt: Any = "") -> bool:
        """Whether the policy-selected execution kernel should handle this solve.

        Existing autonomous solves keep their first-turn behavior. A supervised chat objective turn is the
        exception: it may begin on an already-open chat session because `is_interactive` there means "reused
        channel", not "approval response". Pending approvals are routed before this method is reached.

        AUTO-MODE INITIATION GATE (default-deny): the execution kernel drives offensive execution off
        the observed range state (`capabilities.actions_from_state`), so ceding control to it on a turn that is
        NOT an explicit objective means the ENVIRONMENT — not an authorized operator objective — starts the
        attack. A greeting ("hello") on a pre-collected range would otherwise begin executing. We therefore
        require the same conservative, deterministic objective detector the supervised path already uses before
        initiating in auto mode. A non-objective first turn falls through to normal conversational handling; the
        operator can restate. The gate is deliberately deterministic (not an LLM classifier on the hot path):
        an LLM guard is itself prompt-injectable/DoS-able, and a false negative here costs only a clarifying
        turn while a false positive fires offensive actions.
        """
        if not self._controller_owned_solve() or not _controller_flag_enabled():
            return False
        if getattr(self, "_supervised_objective_active", False):
            return self._controller_hitl_enabled()
        if is_interactive:
            return False
        if getattr(self, "mode", "auto") == "auto":
            if self._looks_like_explicit_objective_prompt(_coerce_prompt_text(prompt)):
                return True
            # Observable decline (not a silent fallthrough): the turn is not an explicit objective, so the
            # execution kernel does NOT initiate; the normal supervisor graph handles it conversationally
            # and can still ask the operator to restate the objective.
            logger.info(
                "Auto-mode initiation gate DECLINED: first turn is not an explicit engagement objective; "
                "handling via the supervisor graph instead of the execution kernel."
            )
            return False
        return bool(self._controller_hitl_enabled())

    def _controller_hitl_enabled(self) -> bool:
        """Whether supervised controller-owned chat should pause moves for operator approval."""
        return bool(
            self._controller_owned_solve()
            and getattr(self, "mode", "auto") == "supervised"
            and getattr(self, "command_name", "") == "chat"
            and _controller_hitl_flag_enabled()
        )

    def _controller_hitl_key(self, kind: str, args: dict[str, Any]) -> str:
        """Stable fingerprint for the exact controller move shown to the operator.

        Policy provenance is intentionally excluded: a resumed controller rebuilds its
        episode/decision IDs, but those audit fields do not change the capability the
        operator approved. Concrete target, preconditions, effects, callback, and inputs
        remain bound into the fingerprint.
        """
        def approval_identity(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    str(key): approval_identity(item)
                    for key, item in value.items()
                    if str(key) != "policy_decision"
                }
            if isinstance(value, (list, tuple, set)):
                return [approval_identity(item) for item in value]
            return _jsonable_value(value)

        return json.dumps(
            {"kind": str(kind or ""), "args": approval_identity(args or {})},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _controller_hitl_capability_request(
        self,
        payload: dict[str, Any],
        inputs: dict[str, Any],
        objective: str,
    ) -> dict[str, Any]:
        args = {
            "capability": _jsonable_value(payload or {}),
            "inputs": _jsonable_value(inputs or {}),
        }
        return {
            "kind": "capability",
            "key": self._controller_hitl_key("capability", args),
            "tool": "execute_capability",
            "display_name": str(payload.get("name") or "execute_capability"),
            "args": args,
            "objective": str(objective or ""),
        }

    def _controller_hitl_collection_request(
        self,
        request: Any,
        objective: str,
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        foothold = getattr(request, "foothold", None)
        args = {
            "collection_key": str(getattr(request, "collection_key", "") or ""),
            "scope_domain": str(getattr(request, "scope_domain", "") or ""),
            "reason": str(getattr(request, "reason", "") or ""),
            "support": str(getattr(request, "support", "") or ""),
            "callback_id": str(getattr(foothold, "callback_id", "") or ""),
            "host": str(getattr(foothold, "host", "") or ""),
            "agent": str(getattr(foothold, "agent", "") or ""),
        }
        if decision:
            args["policy_decision"] = _jsonable_value(decision)
        return {
            "kind": "collection",
            "key": self._controller_hitl_key("collection", args),
            "tool": "collect_graph",
            "args": args,
            "objective": str(objective or ""),
        }

    async def _surface_controller_hitl_request(self, pending: dict[str, Any]) -> None:
        """Stream the same default-deny operator UX for a controller-owned pending move."""
        kind = str(pending.get("kind") or "")
        args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
        if getattr(self, "_hitl_card_emitter", None) is not None:
            try:
                await self._hitl_card_emitter([{
                    "name": str(pending.get("tool") or "guarded_controller_action"),
                    "display_name": str(pending.get("display_name") or ""),
                    "args": args,
                }])
                self._hitl_card_pending = True
            except Exception as e:
                logger.warning(f"HITL: failed to emit controller confirmation card ({e})")
            logger.info(f"HITL controller interrupt surfaced as native card ({kind or 'unknown'})")
            return

        lines: list[str] = []
        if kind == "capability":
            payload = args.get("capability") if isinstance(args.get("capability"), dict) else {}
            inputs = args.get("inputs") if isinstance(args.get("inputs"), dict) else {}
            lines = [
                f"  * `{pending.get('display_name') or 'execute_capability'}`",
                f"    capability: `{payload.get('name', '')}`",
                f"    target: `{payload.get('target', '')}`",
                f"    callback: `{inputs.get('callback_id', '')}`",
                f"    preconditions: `{json.dumps(payload.get('preconditions', []), default=str)}`",
                f"    expected effects: `{json.dumps(payload.get('effects', []), default=str)}`",
            ]
        elif kind == "collection":
            lines = [
                "  * `collect_graph`",
                f"    callback: `{args.get('callback_id', '')}`",
                f"    host: `{args.get('host', '')}`",
                f"    agent: `{args.get('agent', '')}`",
                f"    scope: `{args.get('scope_domain', '') or 'current-forest'}`",
                f"    reason: `{args.get('reason', '')}`",
                f"    support: `{args.get('support', '')}`",
            ]
        body = "\n".join(lines) if lines else "  * (a guarded action)"
        msg = (
            "⏸️ **Approval required — supervised mode**\n\n"
            "Sage wants to run the following guarded action:\n"
            f"{body}\n\n"
            "Reply **`approve`** to run it, or **`deny`** to skip it. "
            "Anything other than an explicit approval is treated as a denial."
        )
        try:
            await self._stream_message_to_mythic(msg)
        except Exception as e:
            logger.warning(f"HITL: failed to stream controller approval prompt ({e})")
        logger.info(f"HITL controller interrupt surfaced to operator ({kind or 'unknown'}); awaiting approve/deny")

    async def _require_controller_hitl_approval(self, pending: dict[str, Any]) -> None:
        """Consume one matching approval token or pause before the controller move executes."""
        if not self._controller_hitl_enabled():
            return
        key = str(pending.get("key") or "")
        if key and getattr(self, "_controller_hitl_approved_key", "") == key:
            self._controller_hitl_approved_key = ""
            self._controller_hitl_approved_pending = None
            self._controller_hitl_pending = None
            logger.info(f"HITL controller approval consumed for {pending.get('tool', 'unknown')}")
            return

        # A stale approval must never authorize a different freshly-selected action.
        self._controller_hitl_approved_key = ""
        self._controller_hitl_approved_pending = None
        self._controller_hitl_pending = pending
        await self._flush_controller_verbose_events()
        await self._surface_controller_hitl_request(pending)
        raise _ControllerHitlPause()

    async def handle_controller_hitl_resume(self, response: str) -> str:
        """Resume or deny a controller-owned pending move with the existing default-deny semantics."""
        pending = getattr(self, "_controller_hitl_pending", None)
        if not isinstance(pending, dict):
            return ""

        approved = _hitl_is_approved(response)
        decision_word = "approve" if approved else "deny"
        tool = str(pending.get("tool") or "unknown")
        display_name = str(pending.get("display_name") or tool)
        args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
        self._write_hitl_audit(tool, args, decision_word)

        objective = str(pending.get("objective") or getattr(self, "_controller_hitl_objective", "") or "")
        key = str(pending.get("key") or "")
        self._controller_hitl_pending = None

        if not approved:
            self._controller_hitl_approved_key = ""
            self._controller_hitl_approved_pending = None
            self._controller_hitl_objective = ""
            self._supervised_objective_active = False
            self._controller_refresh_runtime_policy_telemetry(
                status="halted_denied",
                reason=f"operator denied {display_name}",
                objective_recognized=False,
            )
            msg = (
                "**Execution stopped**\n"
                f"Operator denied `{display_name}`. Sage stopped before execution.\n"
            )
            try:
                await self._stream_message_to_mythic(msg)
            except Exception as e:
                logger.warning(f"HITL: failed to stream controller deny result ({e})")
            logger.info(f"HITL controller resume: deny for {tool}")
            return ""

        self._controller_hitl_approved_key = key
        self._controller_hitl_approved_pending = pending
        self._controller_hitl_objective = objective
        self._seed_autonomous_objective(objective)
        logger.info(f"HITL controller resume: approve for {tool}")
        return await self._run_autonomous_controller(objective)

    @staticmethod
    def _controller_inline_values(values: Any) -> str:
        """Render compact controller metadata for operator-facing progress messages."""
        if values in (None, "", []):
            return "none"
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        rendered = [str(item).strip() for item in values if str(item).strip()]
        return ", ".join(f"`{item}`" for item in rendered) if rendered else "none"

    @staticmethod
    def _controller_result_payload(result: Any) -> dict[str, Any]:
        """Best-effort parse of a capability result for presentation only."""
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @classmethod
    def _controller_selected_action_progress(cls, payload: dict[str, Any], inputs: dict[str, Any]) -> str:
        name = str(payload.get("name") or "unknown capability")
        target = str(payload.get("target") or "").strip()
        callback_id = str(inputs.get("callback_id") or "").strip()
        lines = [f"Sage selected `{name}`" + (f" for `{target}`." if target else ".")]
        if callback_id:
            lines.append(f"Callback: `{callback_id}`")
        reason = str(payload.get("reason") or "").strip()
        if reason:
            lines.append(f"Reason: {reason}")
        lines.append(f"Preconditions: {cls._controller_inline_values(payload.get('preconditions'))}")
        lines.append(f"Expected effects: {cls._controller_inline_values(payload.get('effects'))}")
        return "**Selected action**\n" + "\n".join(lines)

    @classmethod
    def _controller_capability_result_progress(cls, payload: dict[str, Any], result: Any) -> str:
        name = str(payload.get("name") or "unknown capability")
        parsed = cls._controller_result_payload(result)
        lines = [f"Sage received the result for `{name}` and will verify it against observed state."]
        status_bits = []
        for key in ("ok", "verdict", "status"):
            if key in parsed and parsed.get(key) not in (None, ""):
                status_bits.append(f"{key}={parsed.get(key)}")
        if status_bits:
            lines.append("Result: " + ", ".join(f"`{bit}`" for bit in status_bits))
        reason = str(parsed.get("reason") or parsed.get("error") or "").strip()
        if reason:
            lines.append(f"Reason: {reason}")
        return "**Capability result**\n" + "\n".join(lines)

    @staticmethod
    def _controller_collection_selection_progress(request: Any) -> str:
        foothold = getattr(request, "foothold", None)
        callback_id = str(getattr(foothold, "callback_id", "") or "").strip()
        host = str(getattr(foothold, "host", "") or "").strip()
        scope_domain = str(getattr(request, "scope_domain", "") or "").strip() or "current forest"
        reason = str(getattr(request, "reason", "") or "").strip()
        support = str(getattr(request, "support", "") or "").strip()
        lines = [f"Sage selected graph collection for `{scope_domain}`."]
        if callback_id:
            lines.append(f"Callback: `{callback_id}`" + (f" on `{host}`" if host else ""))
        if reason:
            lines.append(f"Reason: {reason}")
        if support:
            lines.append(f"Support: {support}")
        return "**Selected collection**\n" + "\n".join(lines)

    @staticmethod
    def _controller_operator_progress_from_raw(message: str) -> str:
        """Translate stable controller fire-log messages into Sage-owned operator updates."""
        text = str(message or "").strip()
        match = re.match(
            r"^cycle (?P<cycle>\d+): (?P<phase>\S+) (?P<action>[^ ]+)->(?P<target>.*?) "
            r"ok=(?P<ok>\S+) progressed=(?P<progressed>\S+) new_effects=(?P<effects>.*)$",
            text,
        )
        if match:
            action = match.group("action")
            target = match.group("target")
            ok = match.group("ok").casefold() == "true"
            progressed = match.group("progressed").casefold() == "true"
            effects = match.group("effects").strip()
            lines = [
                f"Sage verified `{action}` for `{target}`: "
                f"execution {'reported success' if ok else 'did not report success'}; "
                f"{'verified progress' if progressed else 'no new expected effect was verified'}."
            ]
            lines.append(f"New verified effects: `{effects}`" if effects and effects != "[]" else "New verified effects: none.")
            return "**Verification**\n" + "\n".join(lines)
        if text == "route_discovery candidate failed precondition check; rejected":
            return (
                "**Route discovery**\n"
                "Sage rejected a proposed route-discovery action because its preconditions were not satisfied."
            )
        if text.startswith("collect: "):
            return "**Collection detail**\n" + text.removeprefix("collect: ")
        return "**Execution detail**\n" + text

    def _queue_controller_verbose_event(self, message: str, *, operator_message: str | None = None) -> None:
        """Stream Sage-owned progress without making deterministic control depend on Mythic response RPCs.

        Controller seams are intentionally synchronous logger callbacks, so they cannot await the existing Mythic
        stream function directly. Chain background sends behind one tail task instead: events stay ordered for the
        watcher, but a failed response send cannot change action selection, tasking, or verifier behavior.
        """
        if not getattr(self, "verbose", False):
            return

        formatted = f"{operator_message or self._controller_operator_progress_from_raw(message)}\n"
        previous = getattr(self, "_controller_verbose_stream_tail", None)
        activity = MCPManager.current_execution_activity()

        async def _send_after_previous() -> None:
            if previous is not None:
                try:
                    await previous
                except Exception:
                    pass
            try:
                if isinstance(activity, dict) and activity.get("id") and activity.get("name"):
                    await self._emit_agent_text(
                        content=formatted,
                        delegation_id=str(activity["id"]),
                        delegation_name=str(activity["name"]),
                    )
                else:
                    await self._stream_message_to_mythic(formatted)
            except Exception as e:
                logger.debug(f"Autonomous controller verbose stream failed: {e}")

        try:
            self._controller_verbose_stream_tail = asyncio.create_task(_send_after_previous())
        except RuntimeError:
            # The runtime controller always runs inside an event loop. This only protects atypical direct callers.
            logger.debug("Autonomous controller verbose stream skipped: no running event loop")

    async def _flush_controller_verbose_events(self) -> None:
        """Wait for queued controller progress before emitting the terminal report."""
        tail = getattr(self, "_controller_verbose_stream_tail", None)
        if tail is None:
            return
        try:
            await tail
        except Exception as e:
            logger.debug(f"Autonomous controller verbose flush failed: {e}")
        finally:
            if getattr(self, "_controller_verbose_stream_tail", None) is tail:
                self._controller_verbose_stream_tail = None

    async def _controller_llm_policy_decide(self, request: dict[str, Any]) -> Any:
        """Ask the configured model for exactly one semantic capability decision."""
        if self.llm is None:
            raise RuntimeError("configured LLM is unavailable")
        self._policy_model_calls = int(getattr(self, "_policy_model_calls", 0) or 0) + 1
        if str(request.get("selection_contract") or "") == "hybrid-full-frontier-v2":
            instruction = (
                "Choose exactly one candidate from the supplied complete deterministic admissible frontier for "
                "the stated objective. Return JSON only. Use disposition=select with candidate_id, or stop/ask. "
                "Do not invent candidates, commands, or additional steps."
            )
        else:
            instruction = (
                "Choose the next semantic capability and target for the stated objective using the normalized "
                "state, capability catalog, and current_admissible_actions. Return JSON only. Use "
                "disposition=select with a capability and target from current_admissible_actions, or stop/ask. "
                "Collection is a normal semantic action when collect-graph appears there. Deterministic code "
                "will reject proposals that are not currently admissible. Do not emit commands or additional "
                "steps."
            )
        return await self.llm.ainvoke([
            SystemMessage(content=instruction),
            HumanMessage(content=json.dumps(request, sort_keys=True)),
        ])

    @staticmethod
    def _controller_effective_backend_requests(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return one response-derived backend record for each observed model response."""
        requests: list[dict[str, Any]] = []
        for decision in decisions:
            if not isinstance(decision, dict) or decision.get("model_response_observed") is not True:
                continue
            requests.append({
                "decision_id": str(decision.get("decision_id") or ""),
                "policy_mode": str(decision.get("policy_mode") or ""),
                "effective_backend": str(decision.get("effective_backend") or ""),
                "effective_model_provider": str(decision.get("effective_model_provider") or ""),
                "effective_model_id": str(decision.get("effective_model_id") or ""),
                "backend_provenance_source": str(decision.get("backend_provenance_source") or ""),
                "response_metadata": dict(decision.get("response_metadata") or {}),
            })
        return requests

    @staticmethod
    def _controller_decision_dict(decision: Any | None) -> dict[str, Any]:
        if decision is None:
            return {}
        if hasattr(decision, "to_dict"):
            value = decision.to_dict()
        elif isinstance(decision, dict):
            value = dict(decision)
        else:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _controller_pending_policy_decision(pending: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(pending, dict):
            return {}
        args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
        decision = args.get("policy_decision")
        if isinstance(decision, dict):
            return dict(decision)
        inputs = args.get("inputs") if isinstance(args.get("inputs"), dict) else {}
        decision = inputs.get("policy_decision")
        if isinstance(decision, dict):
            return dict(decision)
        capability = args.get("capability") if isinstance(args.get("capability"), dict) else {}
        intent = capability.get("intent") if isinstance(capability.get("intent"), dict) else {}
        decision = intent.get("policy_decision")
        return dict(decision) if isinstance(decision, dict) else {}

    def _controller_record_policy_decision(self, decision: Any | None) -> dict[str, Any]:
        """Keep one episode-level copy of every semantic decision across HITL pauses."""
        data = self._controller_decision_dict(decision)
        if not data:
            return {}
        observed = list(getattr(self, "_controller_observed_decisions", []) or [])
        decision_id = str(data.get("decision_id") or "")
        if decision_id:
            for existing in observed:
                if str(existing.get("decision_id") or "") == decision_id:
                    return existing
        elif data in observed:
            return data
        observed.append(data)
        self._controller_observed_decisions = observed
        self._controller_refresh_runtime_policy_telemetry()
        return data

    def _controller_record_semantic_transaction(
        self,
        *,
        kind: str,
        capability: str,
        target: str,
        decision: Any | None,
        callback_id: str = "",
        parent_transaction_id: str = "",
    ) -> dict[str, Any]:
        """Record only authorized semantic moves, before the side-effecting seam starts."""
        data = {
            "transaction_id": f"transaction-{uuid4().hex}",
            "parent_transaction_id": str(parent_transaction_id or ""),
            "kind": str(kind or ""),
            "capability": str(capability or ""),
            "target": str(target or ""),
            "callback_id": str(callback_id or ""),
            "child_tasks": [],
            "verifier_ids": [],
            "proof_envelope_ids": [],
            "proof_lineage": [],
            "wait_count": 0,
            "retry_count": 0,
            **self._controller_decision_dict(decision),
        }
        observed = list(getattr(self, "_controller_observed_transactions", []) or [])
        observed.append(data)
        self._controller_observed_transactions = observed
        self._controller_refresh_runtime_policy_telemetry()
        return data

    def _controller_update_transaction_task_lineage(self, event: dict[str, Any]) -> None:
        """Join one observed Mythic task lifecycle event onto its semantic transaction."""
        if not isinstance(event, dict):
            return
        transaction_id = str(event.get("transaction_id") or "").strip()
        task_id = str(event.get("task_id") or "").strip()
        if not transaction_id or not task_id:
            return
        status = str(event.get("status") or "").strip().casefold()
        terminal_status = str(event.get("terminal_status") or "").strip().casefold()
        if not terminal_status and status not in {"", "started"}:
            terminal_status = "completed" if status == "completed" else "failed"
        command = str(event.get("tool_name") or event.get("command") or "").strip()
        callback_id = str(event.get("callback_id") or "").strip()
        observed = list(getattr(self, "_controller_observed_transactions", []) or [])
        changed = False
        for transaction in observed:
            if str(transaction.get("transaction_id") or "") != transaction_id:
                continue
            if callback_id and not str(transaction.get("callback_id") or ""):
                transaction["callback_id"] = callback_id
                changed = True
            child_tasks = list(transaction.get("child_tasks") or [])
            existing = next(
                (item for item in child_tasks if str(item.get("task_id") or "") == task_id),
                None,
            )
            if existing is None:
                existing = {
                    "task_id": task_id,
                    "command": command,
                    "terminal_status": terminal_status,
                    "artifact_ids": [],
                }
                child_tasks.append(existing)
                changed = True
            else:
                if command and str(existing.get("command") or "") != command:
                    existing["command"] = command
                    changed = True
                if terminal_status and str(existing.get("terminal_status") or "") != terminal_status:
                    existing["terminal_status"] = terminal_status
                    changed = True
            transaction["child_tasks"] = child_tasks
            break
        if changed:
            self._controller_observed_transactions = observed
            self._controller_refresh_runtime_policy_telemetry()

    @staticmethod
    def _controller_proof_lineage_row(proof: dict[str, Any], evidence: dict[str, Any], engagement_id: str) -> dict[str, Any]:
        """Return one exact proof join row from a persisted hop/fact proof envelope."""
        if not isinstance(proof, dict) or not proof:
            return {}
        try:
            from . import proof_boundary
        except ImportError:
            import proof_boundary
        envelope = proof_boundary.ProofEnvelope.from_dict(proof)
        if envelope is None or not envelope.transaction_id:
            return {}
        admission = proof_boundary.admit_runtime_envelope(
            envelope,
            current_engagement_id=str(engagement_id or ""),
        )
        proof_id = str(evidence.get("proof_hash") or "").strip() if isinstance(evidence, dict) else ""
        if not proof_id:
            proof_id = envelope.hash
        return {
            "proof_envelope_id": proof_id,
            "transaction_id": envelope.transaction_id,
            "task_id": envelope.task_id,
            "verifier_id": envelope.verifier_id,
            "verifier_hash": envelope.verifier_hash,
            "scope": envelope.scope,
            "origin": envelope.origin,
            "admissible_for_runtime_achievement": admission.admitted,
        }

    def _controller_refresh_transaction_proof_lineage(self, transaction_id: str = "") -> None:
        """Join persisted verifier/proof rows back onto their semantic transactions."""
        observed = list(getattr(self, "_controller_observed_transactions", []) or [])
        if not observed:
            return
        client = getattr(self, "mythic_client", None)
        if client is None:
            return
        engagement_id = ""
        try:
            engagement_id = str(client._eng_key() or "")
        except Exception:
            engagement_id = ""
        sources = [
            *list(getattr(client, "_engagement_hops", []) or []),
            *list(getattr(client, "_engagement_graph_facts", []) or []),
        ]
        changed = False
        for source in sources:
            evidence = getattr(source, "evidence", {}) if isinstance(getattr(source, "evidence", {}), dict) else {}
            proof = getattr(source, "proof_envelope", {}) or evidence.get("proof_envelope") or {}
            row = self._controller_proof_lineage_row(proof, evidence, engagement_id)
            if not row:
                continue
            if transaction_id and row["transaction_id"] != transaction_id:
                continue
            for transaction in observed:
                if str(transaction.get("transaction_id") or "") != row["transaction_id"]:
                    continue
                child_tasks = list(transaction.get("child_tasks") or [])
                child_task = next(
                    (item for item in child_tasks if str(item.get("task_id") or "") == row["task_id"]),
                    None,
                )
                if row["task_id"] and child_task is None:
                    child_task = {
                        "task_id": row["task_id"],
                        "command": str(proof.get("command") or ""),
                        "terminal_status": str(proof.get("terminal_status") or ""),
                        "artifact_ids": [str(proof.get("artifact_id"))] if proof.get("artifact_id") else [],
                    }
                    child_tasks.append(child_task)
                    transaction["child_tasks"] = child_tasks
                    changed = True
                elif child_task is not None and proof.get("artifact_id"):
                    artifact_ids = list(child_task.get("artifact_ids") or [])
                    artifact_id = str(proof.get("artifact_id") or "")
                    if artifact_id and artifact_id not in artifact_ids:
                        artifact_ids.append(artifact_id)
                        child_task["artifact_ids"] = artifact_ids
                        transaction["child_tasks"] = child_tasks
                        changed = True
                verifier_ids = list(transaction.get("verifier_ids") or [])
                if row["verifier_id"] and row["verifier_id"] not in verifier_ids:
                    verifier_ids.append(row["verifier_id"])
                    transaction["verifier_ids"] = verifier_ids
                    changed = True
                proof_ids = list(transaction.get("proof_envelope_ids") or [])
                if row["proof_envelope_id"] and row["proof_envelope_id"] not in proof_ids:
                    proof_ids.append(row["proof_envelope_id"])
                    transaction["proof_envelope_ids"] = proof_ids
                    changed = True
                proof_lineage = list(transaction.get("proof_lineage") or [])
                if row["proof_envelope_id"] and not any(
                    str(item.get("proof_envelope_id") or "") == row["proof_envelope_id"]
                    for item in proof_lineage
                ):
                    proof_lineage.append(row)
                    transaction["proof_lineage"] = proof_lineage
                    changed = True
                break
        if changed:
            self._controller_observed_transactions = observed
            self._controller_refresh_runtime_policy_telemetry()

    @staticmethod
    def _controller_policy_switches(
        policy_mode: str,
        decisions: list[dict[str, Any]],
        transactions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        switches: list[dict[str, Any]] = []
        for source, items in (("decision", decisions), ("transaction", transactions)):
            for index, item in enumerate(items):
                observed = str(item.get("policy_mode") or "")
                if observed != policy_mode:
                    switches.append({
                        "source": source,
                        "index": index,
                        "configured_policy_mode": policy_mode,
                        "observed_policy_mode": observed,
                    })
        return switches

    def _controller_refresh_runtime_policy_telemetry(
        self,
        *,
        status: str | None = None,
        reason: str | None = None,
        objective_recognized: bool | None = None,
    ) -> dict[str, Any]:
        """Refresh episode telemetry from observations that survive controller HITL pauses."""
        telemetry = dict(getattr(self, "_controller_runtime_telemetry", {}) or {})
        if not telemetry:
            return {}
        decisions = list(getattr(self, "_controller_observed_decisions", []) or [])
        transactions = list(getattr(self, "_controller_observed_transactions", []) or [])
        proof_lineage = [
            dict(item)
            for transaction in transactions
            for item in list(transaction.get("proof_lineage") or [])
            if isinstance(item, dict)
        ]
        policy_mode = str(telemetry.get("configured_policy_mode") or getattr(self, "policy_mode", "") or "")
        effective_backend_requests = self._controller_effective_backend_requests(decisions)
        effective_backends = sorted({
            str(item.get("effective_backend") or "")
            for item in effective_backend_requests
            if str(item.get("effective_backend") or "")
        })
        model_calls = int(getattr(self, "_policy_model_calls", 0) or 0)
        policy_switches = self._controller_policy_switches(policy_mode, decisions, transactions)
        branch_opportunity_count = sum(int(item.get("branch_opportunity_count") or 0) for item in decisions)
        model_owned_decision_count = sum(int(item.get("model_owned_decision_count") or 0) for item in decisions)
        model_owned_branch_count = sum(
            1
            for item in decisions
            if str(item.get("decision_owner") or "") == "model_branch"
            and int(item.get("branch_opportunity_count") or 0) > 0
        )
        kernel_singleton_count = sum(int(item.get("kernel_singleton_count") or 0) for item in decisions)
        causally_decisive_decision_count = sum(
            int(item.get("causally_decisive_decision_count") or 0)
            for item in decisions
        )
        decision_owner_counts: dict[str, int] = {}
        for item in decisions:
            owner = str(item.get("decision_owner") or "")
            if owner:
                decision_owner_counts[owner] = decision_owner_counts.get(owner, 0) + 1
        authorized = sum(
            1
            for item in transactions
            if item.get("decision_id") and str(item.get("policy_mode") or "") == policy_mode
        )
        telemetry.update({
            "model_calls": model_calls,
            "effective_backend_requests": effective_backend_requests,
            "effective_backends": effective_backends,
            "backend_provenance_complete": (
                len(effective_backend_requests) == model_calls
                and all(
                    str(item.get("effective_backend") or "")
                    and str(item.get("backend_provenance_source") or "") not in {"", "unavailable"}
                    for item in effective_backend_requests
                )
            ),
            "policy_identity_valid": not policy_switches,
            "policy_switches": policy_switches,
            "semantic_transaction_count": len(transactions),
            "authorized_transaction_count": authorized,
            "semantic_policy_coverage": authorized / len(transactions) if transactions else 1.0,
            "branch_opportunity_count": branch_opportunity_count,
            "model_owned_decision_count": model_owned_decision_count,
            "kernel_singleton_count": kernel_singleton_count,
            "model_branch_coverage": (
                model_owned_branch_count / branch_opportunity_count
                if branch_opportunity_count
                else 0.0
            ),
            "causally_decisive_decision_count": causally_decisive_decision_count,
            "decision_owner_counts": decision_owner_counts,
            "decisions": decisions,
            "transactions": transactions,
            "proof_lineage": proof_lineage,
        })
        if status is not None:
            telemetry["controller_status"] = status
        if reason is not None:
            telemetry["controller_terminal_reason"] = reason
        if objective_recognized is not None:
            telemetry["objective_recognized"] = bool(objective_recognized)
        self._controller_runtime_telemetry = telemetry
        return telemetry

    def controller_runtime_telemetry(self) -> dict[str, Any]:
        """Return the latest observed policy/controller telemetry for this session."""
        self._controller_refresh_runtime_policy_telemetry()
        return dict(getattr(self, "_controller_runtime_telemetry", {}) or {})

    async def _run_autonomous_controller(self, prompt: str) -> str:
        """Run the policy-selected AutonomousController execution kernel.

        The selected policy owns semantic capability choice. Deterministic code owns observe, execute, verify,
        budgets, retries, and stop handling. Returns a terminal report (also streamed to Mythic).

        Collection is deterministic too: initial current-forest collection runs when no verified graph exists for
        that domain, then later collection is allowed only when the real capability frontier is empty and there is
        a concrete reason: a new graph-visible authority epoch or an uncollected trusted domain that supports the
        objective.
        """
        try:
            from . import autonomous_controller as _ctrl
            from . import capabilities as _cap
            from . import engagement_state as _es
            from . import policy as _policy
        except ImportError:
            import autonomous_controller as _ctrl
            import capabilities as _cap
            import engagement_state as _es
            import policy as _policy

        if self._controller_hitl_enabled():
            self._controller_hitl_objective = str(prompt or "")
        self._controller_verbose_stream_tail = None
        resumed_after_approval = bool(getattr(self, "_controller_hitl_approved_key", ""))
        approved_pending = (
            dict(getattr(self, "_controller_hitl_approved_pending", {}) or {})
            if resumed_after_approval
            else {}
        )

        def fire(msg: str, *, operator_message: str | None = None) -> None:
            logger.info(f"[autonomous-controller] {msg}")
            self._queue_controller_verbose_event(msg, operator_message=operator_message)

        snap = {"state": None, "collection_request": None}

        async def observe():
            state = await self._build_current_engagement_state()
            # _build_current_engagement_state intentionally omits graph_facts, but actions_from_state derives
            # GPO/credential/managed-secret/ADCS actions FROM graph_facts — without them the frontier is falsely
            # empty at exactly those walls (Forge HIGH). Refresh-if-stale (so post-collect ingestion is visible)
            # then attach the cached facts. Fail-open: graph-fact errors must not break the loop.
            if state is not None and self.mythic_client is not None:
                try:
                    from datetime import datetime, timezone
                    await self.mythic_client._refresh_graph_facts_if_stale(datetime.now(timezone.utc).isoformat())
                    state.graph_facts = list(getattr(self.mythic_client, "_engagement_graph_facts", []) or [])
                except Exception:
                    pass
            snap["state"] = state
            hops = len(getattr(state, "hops", []) or []) if state is not None else -1
            gf = len(getattr(state, "graph_facts", []) or []) if state is not None else -1
            if state is None:
                progress = "**Observed state**\nSage could not build the current engagement state."
            else:
                progress = f"**Observed state**\nSage observed {hops} ledger hops and {gf} graph facts."
            fire(
                f"observe -> {'None' if state is None else f'{hops} hops, {gf} graph_facts'}",
                operator_message=progress,
            )
            return state

        async def execute(action, decision=None):
            payload = _capability_action_payload(action)
            inputs = _autonomous_capability_inputs(action, snap["state"])
            decision_context = (
                decision.to_dict() if hasattr(decision, "to_dict")
                else dict(decision) if isinstance(decision, dict)
                else {}
            )
            if decision_context.get("model_response_observed") is False:
                approved_decision = self._controller_pending_policy_decision(approved_pending)
                if approved_decision.get("policy_mode"):
                    decision_context = approved_decision
            self._controller_record_policy_decision(decision_context)
            if decision_context:
                payload["intent"] = dict(payload.get("intent") or {})
                payload["intent"]["policy_decision"] = decision_context
                inputs["policy_decision"] = decision_context
            pending = self._controller_hitl_capability_request(payload, inputs, prompt)
            if (
                not self._controller_hitl_enabled()
                or str(getattr(self, "_controller_hitl_approved_key", "") or "") != str(pending.get("key") or "")
            ):
                self._queue_controller_verbose_event(
                    f"selected {payload.get('name')} -> {payload.get('target')} cb={inputs.get('callback_id', '')}",
                    operator_message=self._controller_selected_action_progress(payload, inputs),
                )
            await self._require_controller_hitl_approval(pending)
            transaction = self._controller_record_semantic_transaction(
                kind="capability",
                capability=str(payload.get("name") or ""),
                target=str(payload.get("target") or ""),
                decision=decision_context,
                callback_id=str(inputs.get("callback_id") or ""),
            )
            inputs["transaction_id"] = transaction["transaction_id"]
            payload["intent"] = dict(payload.get("intent") or {})
            payload["intent"]["transaction_id"] = transaction["transaction_id"]
            capability_name = str(payload.get("name") or "capability")
            target = str(payload.get("target") or "").strip()
            activity = await self._open_execution_activity(
                "Execution",
                title=f"Execute {capability_name}",
                instruction=(
                    f"Execute `{capability_name}`"
                    + (f" for `{target}`." if target else ".")
                ),
            )
            activity_token = MCPManager.set_execution_activity(activity) if activity is not None else None
            result = None
            activity_status = "finished"
            try:
                fire(
                    f"execute {payload.get('name')} -> {payload.get('target')} cb={inputs.get('callback_id', '')}",
                    operator_message=(
                        "**Executing action**\n"
                        f"Sage started `{payload.get('name')}`"
                        + (f" for `{payload.get('target')}`." if payload.get("target") else ".")
                    ),
                )
                result = await self.mythic_client.execute_capability(payload, inputs)
                self._controller_refresh_transaction_proof_lineage(transaction["transaction_id"])
                kind = type(result).__name__
                size = len(result) if isinstance(result, str) else "n/a"
                fire(
                    f"execute returned {kind} (len={size})",
                    operator_message=self._controller_capability_result_progress(payload, result),
                )  # PROVE the real string-return boundary fired
                return result
            except BaseException:
                activity_status = "error"
                raise
            finally:
                await self._flush_controller_verbose_events()
                if activity_token is not None:
                    MCPManager.reset_execution_activity(activity_token)
                await self._close_execution_activity(
                    activity,
                    content=(
                        self._controller_capability_result_progress(payload, result)
                        if result is not None
                        else f"`{capability_name}` did not complete."
                    ),
                    status=activity_status,
                )

        def policy_frontier(state):
            return _eval_forced_capability_prefix_frontier(
                _autonomous_policy_candidates(_cap.actions_from_state(state)),
                state,
                raw_override=getattr(self, "_eval_force_capability_prefix_json", None),
            )

        def needs_collection(state):
            # N2: signal collection ONLY when a SUPPORTED collector-profile foothold actually needs it — aligned with
            # _controller_collection_target — so an unsupported-agent missing foothold doesn't trigger a
            # collect()->no_target slot burn. (current_access_collection_missing counts ALL agents.)
            try:
                frontier_empty = not bool(policy_frontier(state))
                if not frontier_empty:
                    snap["collection_request"] = None
                    return False
                request = self._controller_collection_request(
                    state,
                    include_trusted_scope=True,
                    include_optional_recollection=True,
                )
                snap["collection_request"] = request
                return request
            except Exception:
                snap["collection_request"] = None
                return False

        async def collect(state, decision=None):
            request = snap.get("collection_request")
            snap["collection_request"] = None
            decision_context = (
                decision.to_dict() if hasattr(decision, "to_dict")
                else dict(decision) if isinstance(decision, dict)
                else {}
            )
            if decision_context.get("model_response_observed") is False:
                approved_decision = self._controller_pending_policy_decision(approved_pending)
                if approved_decision.get("policy_mode"):
                    decision_context = approved_decision
            self._controller_record_policy_decision(decision_context)
            pending = self._controller_hitl_collection_request(request, prompt, decision_context)
            if (
                not self._controller_hitl_enabled()
                or str(getattr(self, "_controller_hitl_approved_key", "") or "") != str(pending.get("key") or "")
            ):
                self._queue_controller_verbose_event(
                    "selected collect_graph",
                    operator_message=self._controller_collection_selection_progress(request),
                )
            await self._require_controller_hitl_approval(pending)
            transaction = self._controller_record_semantic_transaction(
                kind="collection",
                capability="collect-graph",
                target=str(getattr(request, "collection_key", "") or ""),
                decision=decision_context,
                callback_id=str(getattr(request, "callback_id", "") or ""),
            )
            activity = await self._open_execution_activity(
                "Collection",
                title="Collect and ingest graph data",
                instruction="Run the selected collection and ingest its graph data.",
            )
            activity_token = MCPManager.set_execution_activity(activity) if activity is not None else None
            result = None
            activity_status = "finished"
            visibility_token = None
            try:
                if decision_context:
                    try:
                        from . import mythic_tools as _mt
                    except ImportError:
                        import mythic_tools as _mt
                    visibility_token = _mt._task_visibility_context.set({
                        "capability": "collect-graph",
                        "purpose": str(getattr(request, "reason", "") or ""),
                        "policy_decision": decision_context,
                        "transaction_id": transaction["transaction_id"],
                    })
                result = await self._controller_collect(state, request=request)
                self._controller_refresh_transaction_proof_lineage(transaction["transaction_id"])
                return result
            except BaseException:
                activity_status = "error"
                raise
            finally:
                if visibility_token is not None:
                    _mt._task_visibility_context.reset(visibility_token)
                await self._flush_controller_verbose_events()
                if activity_token is not None:
                    MCPManager.reset_execution_activity(activity_token)
                await self._close_execution_activity(
                    activity,
                    content=str(result or "Collection did not complete."),
                    status=activity_status,
                )

        def objective_met(state):
            try:
                if _es.objective_effects_complete(state):
                    return True
                if not str(_es.engagement_phase(state)).startswith("COMPLETE-CANDIDATE"):
                    return False
                cands = _es.objective_completion_candidates(state)
                targets = list(_es._objective_target_domains(getattr(state, "objective", "") or ""))
                if targets:
                    # domain-equivalence (lab == lab.example.local), matching real completion semantics — raw
                    # casefold equality would drop a valid candidate and MISS completion -> over-reach (Forge MEDIUM).
                    cands = [c for c in cands
                             if any(_es._domains_equivalent(str(c.get("domain", "")), t) for t in targets)]
                return bool(cands)
            except Exception:
                return False

        start = asyncio.get_event_loop().time()

        def clock():
            return asyncio.get_event_loop().time() - start

        cfg = _ctrl.ControllerConfig(
            seam_timeout_s=float(_env_positive_int("SAGE_CONTROLLER_SEAM_TIMEOUT_S", 900)),
            wall_clock_budget_s=float(_env_positive_int("SAGE_CONTROLLER_WALL_S", 2700)),
            token_budget=_env_positive_int("SAGE_CONTROLLER_TOKEN_BUDGET", 3_000_000),
            max_cycles=_env_positive_int("SAGE_CONTROLLER_MAX_CYCLES", 60),
        )

        objective_text = str(prompt or "").strip()
        continuing_episode = resumed_after_approval and bool(getattr(self, "_policy_episode_id", ""))
        if not continuing_episode:
            self._policy_episode_id = _policy.new_episode_id()
            self._policy_model_calls = 0
            self._controller_observed_decisions = []
            self._controller_observed_transactions = []
        policy_mode, inferred_policy_mode_resolution = _policy.resolve_policy_mode(
            getattr(self, "policy_mode", ""),
            default=_policy.POLICY_DEFAULT,
        )
        self.policy_mode = policy_mode
        if not str(getattr(self, "_policy_mode_resolution", "") or ""):
            self._policy_mode_resolution = inferred_policy_mode_resolution
        if not str(getattr(self, "_policy_mode_requested", "") or ""):
            self._policy_mode_requested = str(getattr(self, "policy_mode", "") or "")
        if policy_mode == _policy.POLICY_SYMBOLIC:
            policy_backend = _policy.SymbolicPolicy()
        else:
            approved_selection_consumed = False

            async def policy_decide(request: dict[str, Any]) -> Any:
                nonlocal approved_selection_consumed
                if approved_pending and not approved_selection_consumed:
                    approved_selection_consumed = True
                    candidates = request.get("candidates") if isinstance(request.get("candidates"), list) else []
                    kind = str(approved_pending.get("kind") or "")
                    args = approved_pending.get("args") if isinstance(approved_pending.get("args"), dict) else {}
                    match_index = None
                    match_candidate_id = ""
                    if kind == "capability":
                        approved = args.get("capability") if isinstance(args.get("capability"), dict) else {}
                        if policy_mode == _policy.POLICY_LLM:
                            candidates = [
                                {
                                    "index": index,
                                    "name": str(getattr(candidate, "name", "") or ""),
                                    "target": str(getattr(candidate, "target", "") or ""),
                                    "preconditions": list(getattr(candidate, "preconditions", None) or []),
                                    "effects": list(getattr(candidate, "effects", None) or []),
                                }
                                for index, candidate in enumerate(policy_frontier(snap.get("state")))
                            ]
                        for candidate in candidates:
                            if not isinstance(candidate, dict):
                                continue
                            if (
                                str(candidate.get("name") or "") == str(approved.get("name") or "")
                                and str(candidate.get("target") or "") == str(approved.get("target") or "")
                                and list(candidate.get("preconditions") or []) == list(approved.get("preconditions") or [])
                                and list(candidate.get("effects") or []) == list(approved.get("effects") or [])
                            ):
                                match_index = candidate.get("index")
                                match_candidate_id = str(candidate.get("candidate_id") or "")
                                break
                    elif kind == "collection":
                        collection_key = str(args.get("collection_key") or "")
                        if policy_mode == _policy.POLICY_LLM:
                            return {
                                "disposition": "select",
                                "capability": "collect-graph",
                                "target": collection_key,
                                "rationale": "resume the exact operator-approved collection after revalidation",
                                "confidence": 1.0,
                                "expected_evidence": "verified graph collection and ingest",
                                "_policy_model_response_observed": False,
                                "_policy_replay_decision": self._controller_pending_policy_decision(approved_pending),
                            }
                        for candidate in candidates:
                            if (
                                isinstance(candidate, dict)
                                and str(candidate.get("name") or "") == "collect-graph"
                                and str(candidate.get("target") or "") == collection_key
                            ):
                                match_index = candidate.get("index")
                                match_candidate_id = str(candidate.get("candidate_id") or "")
                                break
                    if isinstance(match_index, int):
                        if policy_mode == _policy.POLICY_LLM:
                            matched = candidates[match_index]
                            return {
                                "disposition": "select",
                                "capability": str(matched.get("name") or ""),
                                "target": str(matched.get("target") or ""),
                                "rationale": "resume the exact operator-approved action after deterministic revalidation",
                                "confidence": 1.0,
                                "expected_evidence": "the approved capability's declared effects",
                                "_policy_model_response_observed": False,
                                "_policy_replay_decision": self._controller_pending_policy_decision(approved_pending),
                            }
                        return {
                            "disposition": "select",
                            "candidate_id": match_candidate_id,
                            "rationale": "resume the exact operator-approved action after deterministic revalidation",
                            "confidence": 1.0,
                            "expected_evidence": "the approved capability's declared effects",
                            "_policy_model_response_observed": False,
                            "_policy_replay_decision": self._controller_pending_policy_decision(approved_pending),
                        }
                    if policy_mode == _policy.POLICY_HYBRID and match_candidate_id:
                        return {
                            "disposition": "select",
                            "candidate_id": match_candidate_id,
                            "rationale": "resume the exact operator-approved action after deterministic revalidation",
                            "confidence": 1.0,
                            "expected_evidence": "the approved capability's declared effects",
                            "_policy_model_response_observed": False,
                            "_policy_replay_decision": self._controller_pending_policy_decision(approved_pending),
                        }
                    return {
                        "disposition": "stop",
                        "rationale": "the operator-approved action is no longer admissible in the observed state",
                        "_policy_model_response_observed": False,
                    }
                return await self._controller_llm_policy_decide(request)

            policy_class = (
                _policy.HybridPolicy
                if policy_mode == _policy.POLICY_HYBRID
                else _policy.LLMPolicy
            )
            policy_backend = policy_class(
                policy_decide if getattr(self, "llm", None) is not None else None,
                provider=getattr(self, "provider", ""),
                model_id=getattr(self, "model", ""),
                catalog=_cap.capability_catalog() if policy_mode == _policy.POLICY_LLM else None,
            )
        policy_backend = _EvalForcedInterventionPolicy(policy_backend)
        self._controller_runtime_telemetry = {
            "episode_id": self._policy_episode_id,
            "policy_mode": str(getattr(policy_backend, "mode", "") or ""),
            "configured_policy_mode": policy_mode,
            "policy_mode_requested": str(getattr(self, "_policy_mode_requested", "") or ""),
            "policy_mode_resolution": str(getattr(self, "_policy_mode_resolution", "") or ""),
            "policy_identity_valid": True,
            "policy_switches": [],
            "model_provider": str(getattr(self, "provider", "") or ""),
            "model_id": str(getattr(self, "model", "") or ""),
            "model_calls": 0,
            "effective_backend_requests": [],
            "effective_backends": [],
            "backend_provenance_complete": True,
            "controller_status": "running",
            "controller_terminal_reason": "",
            "controller_cycle_count": 0,
            "controller_cycles": [],
            "controller_blocker": None,
            "achieved_effects": [],
            "objective_recognized": False,
            "semantic_transaction_count": 0,
            "authorized_transaction_count": 0,
            "semantic_policy_coverage": 1.0,
            "branch_opportunity_count": 0,
            "model_owned_decision_count": 0,
            "kernel_singleton_count": 0,
            "model_branch_coverage": 0.0,
            "causally_decisive_decision_count": 0,
            "decision_owner_counts": {},
        }
        if resumed_after_approval:
            start_progress = (
                "**Execution resumed**\n"
                f"Sage resumed deterministic execution for `{objective_text}` after the latest approval."
            )
        else:
            approval_policy = (
                "with operator approvals" if self._controller_hitl_enabled() else "without per-step approvals"
            )
            start_progress = (
                "**Execution started**\n"
                f"Sage is pursuing `{objective_text}` using `{self.policy_mode}` policy selection and "
                f"deterministic execution {approval_policy}."
            )
        fire(
            f"START (flagged) objective_seed='{(prompt or '')[:80]}' "
            f"seam_timeout={cfg.seam_timeout_s}s wall={cfg.wall_clock_budget_s}s",
            operator_message=start_progress,
        )
        controller = _ctrl.AutonomousController(
            observe=observe,
            execute=execute,
            objective_met=objective_met,
            needs_collection=needs_collection,
            collect=collect,
            frontier_fn=policy_frontier,
            policy_backend=policy_backend,
            objective=objective_text,
            episode_id=self._policy_episode_id,
            clock=clock,
            should_abort=lambda: bool(getattr(self, "_stop_requested", False)),  # operator kill switch between cycles
            config=cfg,
            logger=fire,
        )
        try:
            result = await controller.run()
        except _ControllerHitlPause:
            self._controller_refresh_runtime_policy_telemetry()
            await self._flush_controller_verbose_events()
            return ""
        result_data = result.to_dict()
        for decision in result_data["decisions"]:
            self._controller_record_policy_decision(decision)
        self._controller_runtime_telemetry = {
            "episode_id": result.episode_id,
            "policy_mode": result.policy_mode,
            "configured_policy_mode": policy_mode,
            "policy_mode_requested": str(getattr(self, "_policy_mode_requested", "") or ""),
            "policy_mode_resolution": str(getattr(self, "_policy_mode_resolution", "") or ""),
            "policy_identity_valid": True,
            "policy_switches": [],
            "model_provider": str(getattr(self, "provider", "") or ""),
            "model_id": str(getattr(self, "model", "") or ""),
            "model_calls": 0,
            "effective_backend_requests": [],
            "effective_backends": [],
            "backend_provenance_complete": True,
            "controller_status": result.status,
            "controller_terminal_reason": result.reason,
            "controller_cycle_count": int(result_data.get("cycle_count", 0) or 0),
            "controller_cycles": list(result_data.get("cycles") or []),
            "controller_blocker": result_data.get("blocker"),
            "achieved_effects": list(result_data.get("achieved_effects") or []),
            "objective_recognized": result.status == _ctrl.STATUS_COMPLETE,
            "semantic_transaction_count": 0,
            "authorized_transaction_count": 0,
            "semantic_policy_coverage": 1.0,
            "branch_opportunity_count": 0,
            "model_owned_decision_count": 0,
            "kernel_singleton_count": 0,
            "model_branch_coverage": 0.0,
            "causally_decisive_decision_count": 0,
            "decision_owner_counts": {},
        }
        self._controller_refresh_runtime_policy_telemetry(
            status=result.status,
            reason=result.reason,
            objective_recognized=result.status == _ctrl.STATUS_COMPLETE,
        )
        fire(
            f"DONE status={result.status} cycles={result.cycle_count} "
            f"effects={len(result.achieved_effects)} reason={result.reason}",
            operator_message=(
                "**Execution finished**\n"
                f"Sage finished deterministic execution with status `{result.status}`. "
                f"Verified effects: {len(result.achieved_effects)}. Reason: {result.reason}"
            ),
        )

        report = None
        if result.status == _ctrl.STATUS_COMPLETE:
            report = self._objective_completion_report(require_autonomous=False)
        if not report:
            report = self._controller_terminal_report(result)
        # Persist the assistant turn to state so a reused Model on a later interactive turn does not see a
        # human prompt with no recorded reply (Forge MEDIUM: dangling-turn hazard).
        report_msg = AIMessage(content=report, name="Supervisor")
        try:
            _tag_msg(report_msg, self._next_seq())
            self.state.setdefault("messages", []).append(report_msg)
            self.state.setdefault("supervisor_messages", []).append(report_msg)
        except Exception:
            pass
        try:
            await self._flush_controller_verbose_events()
            formatted = self._format_message_for_streaming(report_msg, agent_name="Supervisor")
            if formatted:
                await self._stream_message_to_mythic(formatted)
        except Exception:
            pass
        if self._controller_hitl_enabled():
            self._controller_hitl_pending = None
            self._controller_hitl_approved_key = ""
            self._controller_hitl_approved_pending = None
            self._controller_hitl_objective = ""
            self._supervised_objective_active = False
        return report

    def _controller_terminal_report(self, result) -> str:
        """Render a terminal controller result as an operator-facing report. Range-agnostic: lists status,
        reason, the precise blocker (if any), and achieved effects. Scenario 'wall' mapping is the eval layer's
        job (ai/hillclimb/wall_checkpoints.py), not the runtime's."""
        lines = [f"Sage halted deterministic execution: **{result.status}** — {result.reason}.",
                 f"Cycles: {result.cycle_count}. Verified effects: {len(result.achieved_effects)}."]
        blocker = result.blocker or {}
        if blocker:
            if blocker.get("capability"):
                lines.append(f"Blocker: `{blocker.get('capability')}` — {blocker.get('reason') or 'blocked'}.")
                if blocker.get("suggested_capability"):
                    lines.append(f"Suggested prerequisite: `{blocker.get('suggested_capability')}`.")
            elif blocker.get("reason"):
                lines.append(f"Blocker: {blocker.get('reason')}.")
        if result.achieved_effects:
            lines.append("Achieved effects:")
            lines.extend(f"- `{e}`" for e in result.achieved_effects[:40])
        return "\n".join(lines)

    def _controller_collection_target(self, state):
        """The SPECIFIC live SUPPORTED-AGENT foothold whose CURRENT authority epoch has no verified collection.
        Aligned with `current_access_collection_missing` (scans ALL footholds, not just the first) so we don't
        re-collect an already-covered authority epoch forever while the missing one is never collected (Forge H3) — but
        ADDITIONALLY filtered to payloads with a collector profile. Selecting an
        unsupported-agent foothold and then rejecting it would burn collection slots and starve a collectable
        foothold (Forge N2). Returns a Foothold or None."""
        try:
            from . import engagement_state as _es
        except ImportError:
            import engagement_state as _es
        try:
            for fh in self._controller_ordered_supported_footholds(state):
                if not _es.graph_collection_covers_foothold(state, fh):
                    return fh
        except Exception:
            return None
        return None

    def _controller_collection_adapter(self, foothold):
        """Return the payload's collector profile, or None when the collector has no contract for it."""
        try:
            from . import mythic_capability_adapter as _adapter
        except ImportError:
            import mythic_capability_adapter as _adapter
        try:
            return _adapter.collection_adapter_for_payload_type(getattr(foothold, "agent", ""))
        except Exception:
            return None

    def _controller_ordered_supported_footholds(self, state) -> list[Any]:
        """Return collectable footholds with the latest proven callback lane first."""
        try:
            from . import capabilities as _cap
            from . import engagement_state as _es
        except ImportError:
            import capabilities as _cap
            import engagement_state as _es
        candidates = [
            foothold
            for foothold in (getattr(state, "footholds", []) or [])
            if _es._is_live_target_foothold(foothold)
            and self._controller_collection_adapter(foothold) is not None
        ]
        if not candidates:
            return []
        live_callback_ids = {
            _cap._normalize_callback_id(getattr(foothold, "callback_id", ""))
            for foothold in candidates
            if _cap._normalize_callback_id(getattr(foothold, "callback_id", ""))
        }
        preferred_callback_id = _cap._preferred_live_callback_id(state, live_callback_ids)
        if not preferred_callback_id:
            return candidates
        return sorted(
            candidates,
            key=lambda foothold: (
                _cap._normalize_callback_id(getattr(foothold, "callback_id", "")) != preferred_callback_id,
            ),
        )

    def _controller_latest_capability_failure_is_retryable(self, state) -> bool:
        """Do not turn a repairable capability failure into an unrelated graph collection.

        The executor records construction/transient failures for traceability, but marks
        them non-terminal so the same grounded action can be repaired or retried. If the
        latest capability hop has that marker, an empty frontier is a repair/control
        problem, not evidence that BloodHound coverage is missing.
        """
        try:
            for hop in reversed(list(getattr(state, "hops", []) or [])):
                technique = str(getattr(hop, "technique", "") or "").strip().casefold()
                if not technique.startswith("capability:"):
                    continue
                status = str(getattr(hop, "status", "") or "").strip().casefold()
                evidence = getattr(hop, "evidence", {})
                return (
                    status in {"failed", "blocked"}
                    and isinstance(evidence, dict)
                    and evidence.get("terminal_failure") is False
                )
        except Exception:
            return False
        return False

    def _controller_collection_request(
        self,
        state,
        include_trusted_scope: bool = False,
        include_optional_recollection: bool = False,
    ):
        """Return the next deterministic collection request.

        Ordering is objective-driven:

        1. A domain with no verified collection at all gets one baseline collection.
        2. Once baseline graph exists, an objective-target trusted domain that is visible but uncollected wins.
        3. Only when no grounded action remains may a new modeled authority epoch recollect the current forest.
        4. Other trusted-domain expansion remains available after the objective-target and authority cases.

        This prevents raw credential/ticket churn from preempting the route the graph already exposes."""
        try:
            from . import engagement_state as _es
        except ImportError:
            import engagement_state as _es
        supported_footholds = self._controller_ordered_supported_footholds(state)
        if not supported_footholds:
            return None

        def request_for(foothold, *, scope_domain: str = "", reason: str, support: str):
            return _ControllerCollectionRequest(
                foothold=foothold,
                scope_domain=scope_domain,
                reason=reason,
                collection_key=_es.collection_target_key(state, foothold, scope_domain),
                support=support,
            )

        # Initial coverage is not optional. Without one verified graph for this domain, there is no reliable
        # basis for capability selection or trust expansion.
        for candidate in supported_footholds:
            if _es.graph_collection_covers_foothold(state, candidate):
                continue
            forest = str(getattr(candidate, "forest", "") or "").strip().casefold()
            if not _es.graph_domain_has_verified_collection(state, forest):
                return request_for(
                    candidate,
                    reason="baseline",
                    support=f"no verified collection exists for {forest or 'the foothold forest'}",
                )

        if self._controller_latest_capability_failure_is_retryable(state):
            return None

        trusted_domains = _es.trusted_uncollected_domains(state) if include_trusted_scope else []
        objective_targets = list(_es._objective_target_domains(getattr(state, "objective", "") or ""))

        def objective_scope(domain: str) -> bool:
            return any(_es._domains_equivalent(domain, target) for target in objective_targets)

        objective_coverage_complete = bool(objective_targets) and all(
            _es.graph_domain_has_verified_collection(state, target)
            for target in objective_targets
        )

        # If BloodHound already exposes the objective domain as reachable but uncollected, collect that scope
        # before spending a cycle on an optional current-forest recollection.
        for scope_domain in trusted_domains:
            if not objective_scope(scope_domain):
                continue
            for candidate in supported_footholds:
                if not _es.graph_collection_covers_scope(state, candidate, scope_domain):
                    return request_for(
                        candidate,
                        scope_domain=scope_domain,
                        reason="objective-scope-expansion",
                        support=f"objective domain {scope_domain} is trusted and uncollected",
                    )

        # A new authority epoch is only an optional collection reason while the objective still lacks verified
        # domain coverage. Once the objective domain is already collected, an empty frontier is a real blocker or
        # capability gap, not a reason to run SharpHound again under a different token.
        if include_optional_recollection and not objective_coverage_complete:
            for candidate in supported_footholds:
                if _es.graph_collection_covers_foothold(state, candidate):
                    continue
                forest = str(getattr(candidate, "forest", "") or "").strip().casefold()
                if _es.graph_domain_has_verified_collection(state, forest):
                    return request_for(
                        candidate,
                        reason="authority-change",
                        support=f"new collection authority epoch is not covered for {forest or 'the foothold forest'}",
                    )

        for scope_domain in trusted_domains:
            if objective_scope(scope_domain):
                continue
            for candidate in supported_footholds:
                if not _es.graph_collection_covers_scope(state, candidate, scope_domain):
                    return request_for(
                        candidate,
                        scope_domain=scope_domain,
                        reason="trusted-scope-expansion",
                        support=f"trusted domain {scope_domain} is visible and uncollected",
                    )
        return None

    async def _controller_collect(self, state, request=None) -> dict:
        """Deterministic collect-once for the controller: run SharpHound -> download the ZIP
        -> ingest the FRESH artifact. Polling/waiting is deterministic (issue_task_and_waitfor_task_output), OFF
        the model path (the 'remove polling from model reasoning' P1 win). Range-agnostic: canonical SharpHound
        2.x args + canonical output path; no range literals.

        FAIL-CLOSED on a failed collection: issue_task_and_waitfor_task_output returns a failure STRING without
        raising, so we never trust that SharpHound ran. SharpHound prepends a timestamp to `--ZipFilename`, so
        the on-disk name is NOT predictable — instead we give it a UNIQUE per-run token name (opsec + anchor),
        then DISCOVER the real path via the payload's `ls` output filtered to our token, and download
        THAT exact path. If no token-bearing ZIP exists, SharpHound failed -> no artifact ingested. Ingest is by
        the resolved `file_uuid` (the exact artifact), with `callback_display_id` so `_record_graph_built` flips
        the collection gate. `ok` is driven by `graph_verified` (Forge H2): only `ingested`/`already_ingested`
        set it True; `ingest_failed`/`uploaded_pending_ingest`/`error` are NOT success, so the collection stays
        'missing' and the controller's per-request retry budget may re-run it (self-heal by composition)."""
        try:
            from . import mythic_tools as _mt
        except ImportError:
            import mythic_tools as _mt

        def fire(msg: str, *, operator_message: str | None = None) -> None:
            logger.info(f"[autonomous-controller:collect] {msg}")
            self._queue_controller_verbose_event(f"collect: {msg}", operator_message=operator_message)

        request = request or self._controller_collection_request(state)
        foothold = getattr(request, "foothold", None)
        scope_domain = str(getattr(request, "scope_domain", "") or "").strip().casefold()
        collection_reason = str(getattr(request, "reason", "") or "").strip()
        collection_key = str(getattr(request, "collection_key", "") or "").strip()
        if not collection_key and foothold is not None:
            try:
                from . import engagement_state as _es
            except ImportError:
                import engagement_state as _es
            collection_key = _es.collection_target_key(state, foothold, scope_domain)

        def outcome(ok: bool, status: str, reason: str = "") -> dict:
            result = {
                "ok": ok,
                "status": status,
                "collection_reason": collection_reason,
                "collection_key": collection_key,
                "scope_domain": scope_domain,
            }
            if reason:
                result["reason"] = reason
            return result

        if foothold is None:
            # No supported foothold needs collection. (An unsupported-agent foothold that is missing
            # collection is deliberately not selected — see _controller_collection_target / Forge N2.)
            return outcome(False, "no_target", "no supported foothold needs collection")
        adapter = self._controller_collection_adapter(foothold)
        if adapter is None:
            return outcome(False, "no_target", "no supported foothold needs collection")
        cb = str(getattr(foothold, "callback_id", "") or "").strip()
        try:
            cb_int = int(cb.lstrip("#").removeprefix("cb"))
        except (TypeError, ValueError):
            return outcome(False, "bad_callback", f"non-numeric callback id {cb!r}")

        host = str(getattr(foothold, "host", "") or "").strip()
        forest = str(getattr(foothold, "forest", "") or "").strip()
        try:
            context = await self.mythic_client.probe_authentication_context(
                cb_int,
                host=host,
                adapter=adapter,
                known_domain_authorities={forest} if forest else set(),
            )
        except Exception as e:
            return outcome(False, "identity_probe_failed", f"{type(e).__name__}: {e}")

        if not context.domain_capable:
            fire(
                f"callback authentication context is local-only ({context.evidence}); restoring process context",
                operator_message=(
                    "**Collection preparation**\n"
                    "Sage found a local-only callback authentication context and is restoring process context "
                    "before graph collection.\n"
                    f"Evidence: {context.evidence}"
                ),
            )
            try:
                revert_command = _collection_profile_text(adapter, "collection_revert_command", "rev2self")
                await self.mythic_client.issue_task_and_waitfor_task_output(revert_command, "", cb_int)
                context = await self.mythic_client.probe_authentication_context(
                    cb_int,
                    host=host,
                    adapter=adapter,
                    known_domain_authorities={forest} if forest else set(),
                )
            except Exception as e:
                return outcome(False, "identity_restore_failed", f"{type(e).__name__}: {e}")
            if not context.domain_capable:
                return outcome(
                    False,
                    "no_domain_identity",
                    f"SharpHound requires domain authentication; observed {context.evidence}",
                )
        fire(
            f"verified callback authentication context luid={context.current_luid or 'unknown'} "
            f"evidence={context.evidence}",
            operator_message=(
                "**Collection preparation**\n"
                "Sage verified the callback authentication context needed for graph collection.\n"
                f"LUID: `{context.current_luid or 'unknown'}`\n"
                f"Evidence: {context.evidence}"
            ),
        )

        # UNIQUE per-run ZIP name: good opsec (controlled name) + a random anchor so discovery cannot match any
        # other file. Range-agnostic (random token, not a range literal).
        import secrets as _secrets
        token = _secrets.token_hex(8)  # 64-bit anchor (Forge LOW: avoid any same-token stale-row collision)
        zip_name = f"bloodhound_{token}.zip"
        args = _mt.build_sharphound_arguments(zip_filename=zip_name, domain=scope_domain)
        out_dir = _mt.SHARPHOUND_CANONICAL_OUTPUT_DIRECTORY
        runner_command = _collection_profile_text(adapter, "dotnet_runner_command", "execute_assembly")
        runner_tool_param = _collection_profile_text(adapter, "dotnet_tool_param", "assembly_name")
        runner_args_param = _collection_profile_text(adapter, "dotnet_args_param", "assembly_arguments")
        ls_command = _collection_profile_text(adapter, "collection_ls_command", "ls")
        ls_path_param = _collection_profile_text(adapter, "collection_ls_path_param", "path")
        download_command = _collection_profile_text(adapter, "collection_download_command", "download")
        download_path_param = _collection_profile_text(adapter, "collection_download_path_param", "path")
        try:
            scope_note = f" scope={scope_domain}" if scope_domain else " scope=current-forest"
            reason_note = f" reason={collection_reason}" if collection_reason else ""
            fire(
                f"SharpHound {runner_command} on cb={cb_int}:{scope_note}{reason_note} {args}",
                operator_message=(
                    "**Collection started**\n"
                    f"Sage started SharpHound collection on callback `{cb_int}` for "
                    f"`{scope_domain or 'current forest'}`.\n"
                    + (f"Reason: {collection_reason}\n" if collection_reason else "")
                    + f"Arguments: `{args}`"
                ),
            )
            runner_output = await self.mythic_client.issue_task_and_waitfor_task_output(
                runner_command,
                {runner_tool_param: "SharpHound.exe", runner_args_param: args},
                cb_int,
            )
            if str(runner_output or "").startswith(_mt._REGISTERED_FILE_PREFLIGHT_PREFIX):
                fire(
                    f"SharpHound tool preflight failed: {runner_output}",
                    operator_message=(
                        "**Collection failed**\n"
                        "Sage could not start SharpHound because the tool preflight failed.\n"
                        f"Result: {runner_output}"
                    ),
                )
                return outcome(False, "tool_preflight_failed", str(runner_output))
            # DISCOVER the real on-disk path (SharpHound prepends a timestamp; never predict the name). `ls` the
            # output dir, find the file carrying our token. Bounded retry for filesystem latency.
            real_path = ""
            for _attempt in range(4):
                ls_out = await self.mythic_client.issue_task_and_waitfor_task_output(
                    ls_command, {ls_path_param: out_dir}, cb_int)
                real_path = _find_token_zip_path(ls_out, token)
                if real_path:
                    break
                await asyncio.sleep(2)
            if not real_path:
                fire(
                    "no token-bearing SharpHound ZIP found in the output dir -> collection failed (not ingesting)",
                    operator_message=(
                        "**Collection failed**\n"
                        "Sage did not find a fresh SharpHound ZIP for this run, so it will not ingest stale data."
                    ),
                )
                return outcome(False, "no_collection_artifact",
                               "SharpHound produced no collection ZIP carrying this run's token")
            fire(
                f"discovered collection artifact: {real_path}",
                operator_message=(
                    "**Collection artifact**\n"
                    f"Sage found the fresh collection artifact `{real_path}`."
                ),
            )
            await self.mythic_client.issue_task_and_waitfor_task_output(
                download_command, {download_path_param: real_path}, cb_int,
            )
            # Resolve the Mythic download by our token -> file_uuid (bounded retry; completed-download filemeta is
            # not always visible immediately).
            row = None
            for _attempt in range(4):
                row = await self.mythic_client._latest_download_for_callback(cb_int, token)
                if isinstance(row, dict) and row.get("agent_file_id"):
                    break
                await asyncio.sleep(2)
            file_uuid = row.get("agent_file_id") if isinstance(row, dict) else None
            if not file_uuid:
                fire(
                    "downloaded artifact not visible in Mythic filemeta -> not ingesting",
                    operator_message=(
                        "**Collection failed**\n"
                        "Sage downloaded the collection artifact but could not find it in Mythic file metadata, "
                        "so it will not ingest it."
                    ),
                )
                return outcome(False, "no_fresh_collection",
                               "the downloaded collection ZIP was not found in Mythic")
            file_name = (row.get("filename_utf8") if isinstance(row, dict) else "") or zip_name
            fire(
                f"ingest_collection(file_uuid={file_uuid}, callback_display_id={cb_int})",
                operator_message=(
                    "**Collection ingest**\n"
                    f"Sage is ingesting `{file_name}` from callback `{cb_int}` into BloodHound."
                ),
            )
            # file_uuid wins for RESOLUTION (the exact discovered artifact); callback_display_id lets
            # _record_graph_built flip the collection gate (it early-returns on a None callback).
            ingest_raw = await self.mythic_client.ingest_collection(
                file_uuid=file_uuid,
                callback_display_id=cb_int,
                file_name=file_name,
                collection_scope_domain=scope_domain,
            )
        except Exception as e:
            fire(
                f"collect failed: {type(e).__name__}: {e}",
                operator_message=(
                    "**Collection failed**\n"
                    f"Sage hit `{type(e).__name__}` while collecting graph data: {e}"
                ),
            )
            return outcome(False, "error", f"{type(e).__name__}: {e}")

        try:
            parsed = json.loads(ingest_raw) if isinstance(ingest_raw, str) else (ingest_raw or {})
        except Exception:
            parsed = {}
        ok = bool(parsed.get("graph_verified")) if isinstance(parsed, dict) else False
        status = str(parsed.get("status", "")) if isinstance(parsed, dict) else ""
        fire(
            f"ingest status={status} graph_verified={ok}",
            operator_message=(
                "**Collection verified**\n"
                f"Sage finished graph ingest with status `{status or 'unknown'}` and "
                f"`graph_verified={str(ok).lower()}`."
            ),
        )
        return outcome(ok, status or "unknown")

    async def _autonomous_executor_node(self, state: SageState | dict, config=None):
        """Execute a compiled autonomous capability step without another LLM handoff."""
        del config
        if isinstance(state, dict):
            channel = list(state.get("autonomous_executor_messages", []) or [])
        else:
            channel = list(getattr(state, "autonomous_executor_messages", []) or [])
        request = next(
            (
                msg for msg in reversed(channel)
                if isinstance(msg, HumanMessage) and not _is_internal_human_message(msg)
            ),
            None,
        )
        action_payload: dict[str, Any] | None = None
        inputs_payload: dict[str, Any] = {}
        if request is not None:
            action_payload, inputs_payload = _parse_compiled_autonomous_capability_instruction(
                _message_content_as_text(request.content)
            )

        state_dict = state if isinstance(state, dict) else {}
        max_seq = 0
        for ch_key in [
            "supervisor_messages",
            "generalist_messages",
            "mythic_operator_messages",
            "mythic_payload_messages",
            "mcp_manager_messages",
            "bloodhound_messages",
            "autonomous_executor_messages",
            "messages",
        ]:
            value = state_dict.get(ch_key, []) if isinstance(state_dict, dict) else []
            if isinstance(value, list):
                for msg in value:
                    max_seq = max(max_seq, _get_seq(msg))
        next_seq = max_seq + 1
        tool_call_id = f"autonomous-executor-{next_seq}"

        if not action_payload:
            result_text = json.dumps({
                "ok": False,
                "verdict": "failed",
                "capability": "autonomous-step-driver",
                "reason": "compiled autonomous capability instruction was missing or unparsable",
                "issued": [],
                "recorded_effects": [],
            }, sort_keys=True)
        elif getattr(self, "mythic_client", None) is None:
            result_text = json.dumps({
                "ok": False,
                "verdict": "failed",
                "capability": action_payload.get("name") or "autonomous-step-driver",
                "reason": "Mythic client is not initialized",
                "issued": [],
                "recorded_effects": [],
            }, sort_keys=True)
        else:
            capability_name = str(action_payload.get("name") or "capability")
            activity = await self._open_execution_activity(
                "Execution",
                title=f"Execute {capability_name}",
                instruction=f"Execute `{capability_name}` through the autonomous executor.",
            )
            activity_token = MCPManager.set_execution_activity(activity) if activity is not None else None
            activity_status = "finished"
            activity_content = f"`{capability_name}` execution completed."
            try:
                result_text = await self.mythic_client.execute_capability(action_payload, inputs_payload)
            except BaseException:
                activity_status = "error"
                activity_content = f"`{capability_name}` execution did not complete."
                raise
            finally:
                if activity_token is not None:
                    MCPManager.reset_execution_activity(activity_token)
                await self._close_execution_activity(
                    activity,
                    content=activity_content,
                    status=activity_status,
                )

        tool_msg = ToolMessage(
            content=result_text,
            name="execute_capability",
            tool_call_id=tool_call_id,
        )
        _tag_msg(tool_msg, next_seq)
        next_seq += 1

        terminal = _terminal_execute_capability_payload([tool_msg])
        summary_text = (
            _terminal_execute_capability_report(terminal, bounded_one_action=True)
            if terminal is not None
            else "Autonomous executor returned a non-terminal execute_capability result."
        )
        summary_msg = AIMessage(
            content=summary_text,
            name="Autonomous_Executor",
            additional_kwargs={"_autonomous_executor_result": True},
        )
        _tag_msg(summary_msg, next_seq)
        next_seq += 1

        self._message_seq = next_seq
        self.state["_message_seq"] = next_seq

        update = {
            "messages": [tool_msg, summary_msg],
            "autonomous_executor_messages": [tool_msg, summary_msg],
            "supervisor_messages": [summary_msg],
            "_message_seq": next_seq,
            "recursion_summary_requested": False,
            "recursion_handback": False,
        }
        return Command(goto="Supervisor", update=update)

    async def _refresh_footholds_for_objective_completion(self) -> None:
        """Populate cached live footholds once for a pre-graph terminal objective check.

        A fresh one-shot query has the durable hop ledger loaded, but no cached footholds until the
        Mythic_Operator runs a tool/gate. This refresh uses Mythic callback metadata only; it does not issue
        payload tasks. Fail-open so missing Mythic state never blocks normal graph execution.
        """
        try:
            mythic_client = getattr(self, "mythic_client", None)
            if mythic_client is None:
                return
            if list(getattr(mythic_client, "_engagement_footholds", []) or []):
                return
            try:
                from . import access_reconciler
            except ImportError:
                import access_reconciler
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            footholds = await access_reconciler.reconcile_access(mythic_client, now)
            try:
                mythic_client._engagement_footholds = list(footholds)
            except Exception:
                pass
        except Exception:
            return

    async def _maybe_stream_objective_completion_stop(
        self,
        *,
        refresh_footholds: bool = False,
        require_autonomous: bool = True,
    ) -> bool:
        """Stream the terminal objective-complete report once and tell the graph loop to stop."""
        if refresh_footholds:
            await self._refresh_footholds_for_objective_completion()
        report = self._objective_completion_report(require_autonomous=require_autonomous)
        if not report:
            return False
        self.state["recursion_handback"] = True
        if getattr(self, "_objective_completion_report_streamed", False):
            return True
        self._objective_completion_report_streamed = True
        try:
            logger.info("✅ Objective completion detected from engagement state; halting autonomous solve")
        except Exception:
            pass
        try:
            await self._stream_message_to_mythic(f"\n{report}\n")
        except Exception:
            pass
        return True

    def _objective_completion_preflight_allowed(self, prompt: str) -> bool:
        """Whether this prompt should check for already-recorded objective completion before more work."""
        if self._controller_owned_solve():
            return True
        text = str(prompt or "").casefold()
        if not text:
            return False
        if "autonomous" in text and "objective" in text:
            return True
        if "observed engagement state" in text and ("continue" in text or "satisfied" in text or "proof chain" in text):
            return True
        if "objective" in text and ("already satisfied" in text or "proof chain" in text or "continue" in text):
            return True
        return False

    def _autonomous_nudge_content(self, base_nudge_text: str, rendered_state: str | None) -> str:
        """Compose the autonomous-continue nudge, optionally prefixed with observed state.

        When ``rendered_state`` is falsy (gate off, or building/rendering failed) the base nudge is
        returned UNCHANGED — byte-for-byte. Otherwise the rendered engagement-state block and the
        "don't re-issue achieved hops" directive are PREPENDED so the Operator advances from the
        observed state instead of re-proposing already-achieved hops.
        """
        if not rendered_state:
            return base_nudge_text
        return f"{rendered_state}\n\n{_ENGAGEMENT_STATE_DIRECTIVE}\n\n{base_nudge_text}"

    def _context_middleware(
            self,
            inject_engagement_state: bool = False,
            bounded_execute_stop: bool = False,
    ) -> list:
        """Bounded-context middleware for every create_agent.
        Strategy: ClearToolUsesEdit does the cheap, routine bounding every step (no LLM call);
        SummarizationMiddleware is a SAFETY NET that only fires on genuine context overflow.
        Note: the per-call floor (system prompt + tool schemas) is ~75k tokens, so the summarization
        trigger MUST sit well above it (we run bedrock-claude-4-6-sonnet via LiteLLM, ~200k context) —
        a trigger near the floor makes summarization fire every step and thrash (~10s/call) for no gain."""
        # Protect routing/handoff tool results from clearing — they drive graph control.
        _PROTECTED_TOOLS = (
            "summarize_and_handback", "handback_to_supervisor", "request_continuation", "respond_to_user",
            "transfer_to_Supervisor", "transfer_to_Generalist", "transfer_to_Mythic_Operator",
            "transfer_to_Mythic_Payload", "transfer_to_BloodHound", "transfer_to_MCP_Manager",
        )
        # Static, run-constant schema the Mythic_Operator needs constantly — protect its tool output from the
        # DIGEST so it is fetched ONCE per payload type and kept, not elided-and-re-fetched (run 2058: 16×).
        # NOT added to compaction protection on purpose: the >4k compaction cap (~16k ceiling) keeps the
        # retained copy bounded instead of flooding every call with the full ~30k schema. The cheap
        # names+summaries index already lives un-trimmed in the prompt ({commands_text}); this keeps the
        # on-demand PARAMETER schema from being re-fetched.
        _STATIC_SCHEMA_TOOLS = ("get_all_commands_for_payloadtype",)
        mw = [
            # Kill-switch honored INSIDE each agent loop (not just at top-level handoffs) — see
            # _StopCheckMiddleware / task-626. Listed first so it gates before the model call.
            _StopCheckMiddleware(self),
            # Cap oversized tool results BEFORE the context-editing size trigger evaluates them,
            # at the awrap_tool_call seam that provably fires (unlike the v1 tool wrapper).
            _ToolResultCompactionMiddleware(self),
            # Trim duplicated docstring Args:/Returns: blocks from tool schemas to cut the per-call floor.
            _ToolSchemaSlimMiddleware(self),
            ContextEditingMiddleware(
                edits=[_DigestToolUsesEdit(
                    trigger=50000,
                    # keep is a lab-tunable knob: higher = fewer re-fetches (less churn) but more
                    # retained context/call. Raised 3→5 after the 2026-06-01 trace showed the agent
                    # re-fetching cleared task output (task 301 x3, 296 x2). Re-measure tokens/call
                    # after a fresh run before tuning further.
                    keep=5,
                    clear_tool_inputs=False,
                    exclude_tools=_PROTECTED_TOOLS + _STATIC_SCHEMA_TOOLS,
                    placeholder="[earlier tool output elided to conserve context. Do NOT re-fetch it unless you need a specific detail from THIS task — re-fetching cleared output just re-fills the context you are trying to save.]",
                )],
                token_count_method="approximate",
            ),
        ]
        if bounded_execute_stop:
            # Mythic_Operator treats execute_capability as an atomic transaction boundary.
            # Explicit bounded one-action requests still get a graph-END final report in
            # _wrap_create_agent; unbounded autonomous solves return to Supervisor/state
            # reconciliation before choosing another action.
            mw.insert(1, _BoundedExecuteCapabilityStopMiddleware(self))
        summ_model = self._get_base_chat_model()
        if summ_model is not None:
            mw.append(SummarizationMiddleware(
                model=summ_model,
                # Safety net only — sits well above the ~75k system-prompt+tool-schema floor so it does
                # not fire every step. Raised from 55000 (which thrashed) after trace evidence. ~200k ctx.
                trigger=("tokens", 150000),
                keep=("messages", 12),
            ))
        # HITL (supervised mode only): gate guarded tool calls behind an operator approve/deny
        # interrupt. AUTO mode appends nothing here, so its behavior is byte-identical to before.
        if getattr(self, "mode", "auto") == "supervised":
            mw.append(HumanInTheLoopMiddleware(
                interrupt_on={
                    t: InterruptOnConfig(allowed_decisions=["approve", "reject"])
                    for t in GUARDED_TOOLS
                },
                description_prefix="Sage supervised mode — approve or deny this guarded tool call",
            ))
        # Per-turn engagement-state injection (Mythic_Operator only, autonomous + gate-on, fail-open).
        # Appended LAST so it is the INNERMOST wrapper: the rendered block is added AFTER all the
        # context-editing/summarization middleware run, so it is never trimmed before reaching the model.
        if inject_engagement_state:
            mw.append(_EngagementStateMiddleware(self))
        # Provider-agnostic empty/blank-block guard — appended LAST so it is the INNERMOST wrapper and sees the
        # final request (after engagement-state injection) on every model call inside create_agent's react loop,
        # for ALL providers. This is the create_agent-internal-loop counterpart to the channel-path
        # `_sanitize_messages`; together they close the empty-`system`/blank-block class on every code path.
        mw.append(_MessageSanitizerMiddleware(self))
        return mw
    
    # Agent definitions
    def _generalist_agent(self):
        name = "Generalist"
        prompt = load_prompt("generalist")
        if not self.state["generalist_messages"]:
            self.state["generalist_messages"].append(SystemMessage(content=prompt))
        tools = []
        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for Mythic Operator Agent.")
        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            middleware=self._context_middleware(),
        )
        return self._wrap_create_agent(agent, "generalist_messages", name)
    
    def _mythic_operator_agent(self):
        name = "Mythic_Operator" # Note: name must match the agent_name in _create_handoff_tool and cannot have spaces

        commands_text = prompt_context.commands_text(self)

        prompt = load_prompt("mythic_operator", commands_text=commands_text)
        if not self.state["mythic_operator_messages"]:
            self.state["mythic_operator_messages"].append(SystemMessage(content=prompt))
        # Tools
        if self.mythic_client is not None:
            mythic_tools = self.mythic_client.get_tools([
                "list_callbacks",
                "get_all_commands_for_payloadtype",
                "wait_for_seconds",
                "issue_task_and_waitfor_task_output",
                "get_task_history_for_callback",
                "get_all_task_output_by_task_id",
                "upload_file_by_file_uuid",
                "get_all_uploaded_files",
                "get_operations",
                "read_credentials",
                "add_credential",
                "execute_capability",
                "build_capability_commands",
                "get_ttp_guidance",
                "get_ttp_full_reference",
                "list_ttp_categories",
                "ensure_tool_uploaded",
                "download_tool",
                "ingest_collection",
            ])
            # Add the handback tool for recursion limit management
            handback_tool = _create_summarize_handback_tool()
            # Explicit autonomous handback to the Supervisor (routes to Supervisor, does NOT end the run) —
            # the continue-loop consumes plain turn-ends, so this is the Operator's path to cross-agent routing.
            handback_to_supervisor_tool = _create_handback_to_supervisor_tool(
                self.mythic_client,
                autonomous=bool(self._autonomous_solve),
            )

            # Add handoff to Mythic_Payload for payload creation needs
            transfer_to_payload = _create_handoff_tool(
                agent_name="Mythic_Payload",
                description="Delegate payload creation task to Mythic_Payload agent. Use when privilege escalation, lateral movement, or persistence requires a new payload. Always include the source/reference callback display_id in handoff_instruction so Mythic_Payload can inherit working C2 config, e.g. 'inherit C2 config from reference callback 22'."
            )

            tools = mythic_tools + [handback_tool, handback_to_supervisor_tool, transfer_to_payload]
            tools = filter_tools_by_frontmatter("mythic_operator", tools)
        else:
            raise ValueError("Mythic client not initialized for Mythic Operator Agent.")
        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for Mythic Operator Agent.")
        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            middleware=self._context_middleware(
                inject_engagement_state=True,
                bounded_execute_stop=True,
            ),
        )
        return self._wrap_create_agent(agent, "mythic_operator_messages", name)

    def _mythic_payload_agent(self):
        name = "Mythic_Payload"

        installed_payloads_text = prompt_context.installed_payloads_text(self)
        installed_c2_profiles_text = prompt_context.installed_c2_profiles_text(self)

        prompt = load_prompt("mythic_payload", installed_payloads_text=installed_payloads_text, installed_c2_profiles_text=installed_c2_profiles_text)
        if not self.state["mythic_payload_messages"]:
            self.state["mythic_payload_messages"].append(SystemMessage(content=prompt))
        # Tools
        if self.mythic_client:
            mythic_tools = self.mythic_client.get_tools([
                "get_payload_names",
                "create_payload",
                "get_all_payload_info",
                "get_all_payloads",
                "get_c2_profiles_for_payload",
                "get_callback_c2_config",
                "get_payload_c2_config",
                "download_payload",
                "delete_payload",
            ])
            # Add the handback tool for recursion limit management
            handback_tool = _create_summarize_handback_tool()
            tools = mythic_tools + [handback_tool]
            tools = filter_tools_by_frontmatter("mythic_payload", tools)
        else:
            raise ValueError("Mythic client not initialized for Mythic Payload Agent.")
        
        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for Mythic Operator Agent.")
        
        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            middleware=self._context_middleware(),
        )
        return self._wrap_create_agent(agent, "mythic_payload_messages", name)

    async def _notify_bloodhound_not_connected(self):
        """Emit a ONE-TIME Mythic EventFeed WARNING (idempotent per run) telling the operator that
        BloodHound is needed but not connected, and how to connect it. Fail-soft."""
        if getattr(self, "_bh_notconnected_notified", False):
            return
        self._bh_notconnected_notified = True
        client = getattr(self.mythic_client, "client", None) if getattr(self, "mythic_client", None) else None
        if client is None:
            return
        try:
            from mythic import mythic as _mythic
            await _mythic.send_event_log_message(
                client,
                message=(
                    "Sage needs BloodHound but its MCP server is NOT connected — attack-graph ingest and "
                    "analysis are unavailable. Connect BloodHound CE via the `mcp-connect` command (see the "
                    "Sage payload documentation: \"Connecting BloodHound to Sage\" for the exact parameters), "
                    "then re-run the request."
                ),
                level="warning",
                source="sage_bloodhound",
            )
            logger.info("🩸 [bloodhound-guard] emitted Mythic EventFeed warning (BloodHound MCP not connected)")
        except Exception as e:
            logger.debug(f"BloodHound not-connected EventFeed notify failed: {e}")

    def _bloodhound_agent(self):
        # Dedicated BloodHound agent: owns the BloodHound graph lifecycle (ingest -> verify -> query) on its
        # OWN message channel ("bloodhound_messages"). Scoped to ONLY the BloodHound MCP server's tools so it
        # stays distinct from the general-purpose MCP_Manager (which owns any OTHER connected MCP servers).
        name = "BloodHound"

        servers_text = prompt_context.servers_text(self)

        prompt = load_prompt("bloodhound", servers_text=servers_text)

        if not self.state["bloodhound_messages"]:
            self.state["bloodhound_messages"].append(SystemMessage(content=prompt))

        # Scope to the BloodHound MCP server's tools only (file_upload, domain_info, data_quality,
        # graph_analysis, cypher_query, adcs_info, etc.) — match by name so case/registration variants work.
        bh_servers = [s for s in MCPManager.get_connected_servers() if MCPManager.is_bloodhound_server(s)]
        mcp_tools = []
        for s in bh_servers:
            mcp_tools += MCPManager.get_tools_by_server(s)

        # Add handback tool for recursion limit management
        handback_tool = _create_summarize_handback_tool()

        # Sage TTP knowledge tools (read-only local files) + the stage->ingest bridge so the BloodHound
        # agent can self-serve standup guidance, the attack-path-loop playbook, and stage a staged file UUID.
        ttp_tools = []
        if self.mythic_client is not None:
            ttp_tools = self.mythic_client.get_tools([
                "get_ttp_guidance",
                "get_ttp_full_reference",
                "list_ttp_categories",
            ])

        tools = mcp_tools + filter_tools_by_frontmatter("bloodhound", ttp_tools + [handback_tool])

        # Handle case when BloodHound MCP not connected — the agent still loads (with only its TTP/handback
        # tools) so it can NOTIFY the user how to connect BloodHound rather than silently failing.
        if not mcp_tools:
            logger.warning("BloodHound agent initialized with NO BloodHound MCP tools (BloodHound MCP not connected)")

        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for BloodHound Agent.")

        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            # Guard FIRST so a not-connected BloodHound MCP notifies the user (EventFeed + steps) and ends
            # the turn before any model call, instead of silently failing with no graph tools.
            middleware=[_BloodHoundConnectionGuardMiddleware(self)] + self._context_middleware(),
        )
        return self._wrap_create_agent(agent, "bloodhound_messages", name)

    def _mcp_manager_agent(self):
        # General-purpose MCP manager: lets users connect ARBITRARY MCP servers to Sage and use their tools.
        # Scoped to every connected MCP server EXCEPT BloodHound (which has its own dedicated agent), so the
        # two never overlap. If only BloodHound is connected, this agent simply has no MCP tools (that's fine).
        name = "MCP_Manager"

        servers_text = prompt_context.servers_text(self)

        prompt = load_prompt("mcp_manager", servers_text=servers_text)

        if not self.state["mcp_manager_messages"]:
            self.state["mcp_manager_messages"].append(SystemMessage(content=prompt))

        # All connected MCP servers EXCEPT BloodHound (owned by the dedicated BloodHound agent).
        other_servers = [s for s in MCPManager.get_connected_servers() if not MCPManager.is_bloodhound_server(s)]
        mcp_tools = []
        for s in other_servers:
            mcp_tools += MCPManager.get_tools_by_server(s)

        handback_tool = _create_summarize_handback_tool()
        tools = mcp_tools + filter_tools_by_frontmatter("mcp_manager", [handback_tool])

        if not mcp_tools:
            logger.info("MCP_Manager agent initialized with no non-BloodHound MCP tools (no other MCP servers connected)")

        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for MCP Manager Agent.")

        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            middleware=self._context_middleware(),
        )
        return self._wrap_create_agent(agent, "mcp_manager_messages", name)

    def _supervisor_agent(self):
        name = "Supervisor"
        prompt = load_prompt("supervisor")
        if not self.state["supervisor_messages"]:
            self.state["supervisor_messages"].append(SystemMessage(content=prompt))

        # Handoffs
        assign_to_generalist_agent = _create_handoff_tool(
            agent_name="Generalist",
            description="Assign task to Generalist for general questions, explanations, advice, and tasks that don't require Mythic operations or external tools.",
            autonomous_redirect=self._autonomous_handoff_step_redirect,
        )

        assign_to_mythic_operator_agent = _create_handoff_tool(
                agent_name="Mythic_Operator",
                description="Assign task to Mythic Operator for ALL Mythic C2 operations: callbacks, agents, tasks, commands, files, reconnaissance. ALWAYS use this for Mythic-related queries instead of the BloodHound agent.",
                autonomous_redirect=self._autonomous_handoff_step_redirect,
            )

        assign_to_mythic_payload_agent = _create_handoff_tool(
                agent_name="Mythic_Payload",
                description="Assign task to Mythic Payload for creating Mythic payloads, configuring C2 profiles, and build options.",
                autonomous_redirect=self._autonomous_handoff_step_redirect,
            )

        assign_to_bloodhound_agent = _create_handoff_tool(
                agent_name="BloodHound",
                description="Assign to the BloodHound agent for the BloodHound attack-graph: INGEST a staged SharpHound/AzureHound collection (file_upload) then VERIFY it, and attack-path ANALYSIS (shortest path, ADCS/ESC paths, Cypher, object detail). NOTE: the Operator auto-hands-off freshly-staged collections to BloodHound; route here for any BloodHound/graph work, to re-attempt a failed ingest, or for path analysis. Do NOT route BloodHound work to Mythic_Operator.",
                autonomous_redirect=self._autonomous_handoff_step_redirect,
            )

        assign_to_mcp_manager_agent = _create_handoff_tool(
                agent_name="MCP_Manager",
                description="Assign to the general-purpose MCP Manager for tools from ARBITRARY third-party MCP servers a user has connected (web fetching, external APIs, non-Mythic integrations) — anything that is NOT BloodHound, NOT Mythic C2, and NOT a payload build. For BloodHound/graph work use the BloodHound agent; for Mythic operations use Mythic_Operator.",
                autonomous_redirect=self._autonomous_handoff_step_redirect,
            )

        # Completion tool - use when task is done
        respond_to_user_tool = _create_respond_to_user_tool()

        # Recursion limit management tool
        request_continuation_tool = _create_recursion_summary_tool()

        # Tools
        tools = [
            assign_to_generalist_agent,
            assign_to_mythic_operator_agent,
            assign_to_mythic_payload_agent,
            assign_to_bloodhound_agent,
            assign_to_mcp_manager_agent,
            respond_to_user_tool,
            request_continuation_tool,
        ]
        tools = filter_tools_by_frontmatter("supervisor", tools)

        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for Mythic Operator Agent.")
        # NOTE: unlike the worker agents, the Supervisor's channel is pre-seeded with
        # self.system_message (the overarching "You are Sage" prompt) at build time, so the
        # `if not self.state["supervisor_messages"]` guard above is False and supervisor.md is
        # NEVER appended to the channel. supervisor.md therefore MUST come from create_agent's
        # system_prompt — removing it (as for the workers) drops the Supervisor's routing prompt
        # entirely and it loops. This is NOT a duplicate: the channel has only self.system_message.
        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            # Guard the ONE system-block site the message-boundary sanitizer can't reach: create_agent prepends
            # this as a system block inside its own react loop, downstream of _wrap_create_agent's
            # _sanitize_messages. supervisor.md is non-empty today, but a blank render would otherwise send an
            # empty `system` block to Bedrock (bug 2). Normalize here too, consistently with __init__.
            system_prompt=_nonempty_system(prompt),
            middleware=self._context_middleware(),
        )
        return self._wrap_create_agent(agent, "supervisor_messages", name)

    def _process_ai_content_list(self, content_list, agent_name=None):
        """Helper method to process AI message content lists"""
        result = ""
        logger.info(f"🔍 _process_ai_content_list called with {len(content_list)} items, agent_name={agent_name}")
        for m in content_list:
            if m.get("type") == "text":
                logger.debug(f"  Processing text content: {m.get('text', '')[:50]}...")
                if agent_name:
                    result += f"🤖[{agent_name}]> {m.get('text', '')}\n"
                else:
                    result += f"🤖> {m.get('text', '')}\n"
            elif m.get("type") == "tool_use":
                tool_name = m.get('name', '')
                logger.info(f"  ✅ Processing tool_use: name={tool_name}, id={m.get('id', '')}")
                result += f"🛠️[{m.get('id', '')}> Tool Request: '{tool_name}', Args: '{m.get('input', '')}'\n"
            else:
                logger.warning(f"  ❓ Unknown message type: {m.get('type', 'unknown')}")
                result += f"❓> Unknown message type: {m.get('type', 'unknown')} with content: {m}\n"
        logger.info(f"🔍 _process_ai_content_list returning {len(result)} chars")
        force_flush_all_handlers()
        return result

    def _cleanup_dangling_tool_calls(self):
        """
        Clean up ANY dangling tool_use blocks throughout the entire conversation state.

        Anthropic's API requires that every tool_use block must have a corresponding
        tool_result block. This function scans ALL messages to find tool_use blocks
        that don't have matching tool_results and injects synthetic ToolMessages.

        This is critical when resuming from a recursion limit hit or checkpoint restore,
        where execution may have stopped after tool_use blocks but before their results.
        """
        if "messages" not in self.state or not self.state["messages"]:
            return

        messages = self.state["messages"]
        logger.info(f"Scanning {len(messages)} messages for dangling tool calls...")

        # Track which tool_call_ids have been fulfilled with ToolMessages
        fulfilled_tool_call_ids = set()

        # First pass: collect all fulfilled tool_call_ids
        for msg in messages:
            if isinstance(msg, ToolMessage):
                tool_call_id = getattr(msg, "tool_call_id", None)
                if tool_call_id:
                    fulfilled_tool_call_ids.add(tool_call_id)

        logger.info(f"Found {len(fulfilled_tool_call_ids)} fulfilled tool calls")

        # Second pass: find dangling tool_use blocks
        dangling_tool_calls = []
        for i, msg in enumerate(messages):
            if isinstance(msg, AIMessage):
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    for tool_call in tool_calls:
                        tool_call_id = tool_call.get("id")
                        tool_name = tool_call.get("name", "unknown")

                        if tool_call_id and tool_call_id not in fulfilled_tool_call_ids:
                            logger.warning(f"Found dangling tool_use at message {i}: id={tool_call_id}, name={tool_name}")
                            dangling_tool_calls.append((i, tool_call_id, tool_name))

        if dangling_tool_calls:
            logger.warning(f"Found {len(dangling_tool_calls)} dangling tool_use blocks - injecting synthetic results")

            # CRITICAL: Insert synthetic ToolMessages RIGHT AFTER their corresponding AIMessages
            # Process in REVERSE order to avoid index shifting issues
            for msg_index, tool_call_id, tool_name in reversed(dangling_tool_calls):
                logger.info(f"Creating synthetic ToolMessage for tool_use id={tool_call_id}, name={tool_name}")

                synthetic_tool_message = ToolMessage(
                    content="[Tool execution cancelled - recursion limit or checkpoint restore. User requested continuation.]",
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )

                # Insert RIGHT AFTER the AIMessage (at msg_index + 1)
                insert_position = msg_index + 1
                self.state["messages"].insert(insert_position, synthetic_tool_message)
                logger.info(f"Inserted synthetic ToolMessage at position {insert_position} (right after message {msg_index})")

            logger.info(f"Successfully injected {len(dangling_tool_calls)} synthetic ToolMessages at correct positions")
        else:
            logger.info("No dangling tool calls found - state is clean")

    def _sanitize_messages(self, msgs: list[AnyMessage]) -> list[AnyMessage]:
            """
            Sanitize message list for LLM invocation:
            1. Remove orphan ToolMessages whose tool_call_id was never introduced by a preceding AIMessage
            2. Keep only the FIRST NON-EMPTY SystemMessage; drop empty/blank ones and all later ones (prevents
               both "multiple non-consecutive system messages" AND the Bedrock
               "system: text content blocks must be non-empty" ValidationException — an empty first system
               block was previously kept and forwarded verbatim). Bedrock treats `system` as optional, so
               dropping a blank system message entirely is valid.
            Note: this is the provider-agnostic empty-block guard on the live invoke path. The legacy
            `_patch_model_for_bedrock` / `_apply_bedrock_patch` monkeypatch ONLY affects the
            langchain_openai (OpenAI-compatible / LiteLLM proxy) path and is a no-op for the native
            `init_chat_model(model_provider="bedrock")` (langchain-aws) provider — so it cannot be relied on here.
            """
            seen_tool_use_ids = set()
            seen_system_message = False
            cleaned = []
            for m in msgs:
                if isinstance(m, AIMessage):
                    for tc in (getattr(m, "tool_calls", None) or []):
                        tc_id = tc.get("id")
                        if tc_id:
                            seen_tool_use_ids.add(tc_id)
                    cleaned.append(m)
                elif isinstance(m, ToolMessage):
                    tc_id = getattr(m, "tool_call_id", None)
                    if tc_id and tc_id in seen_tool_use_ids:
                        cleaned.append(m)
                    # else drop orphan
                elif isinstance(m, SystemMessage):
                    # Keep only the FIRST NON-EMPTY SystemMessage; drop blank ones (an empty `system` block is
                    # rejected by Bedrock) and drop all later ones (provider "single system" rule). Strip any
                    # blank text blocks from a kept list-form system message so no empty block slips through.
                    if not seen_system_message and _content_has_text(m.content):
                        cleaned.append(m.model_copy(update={"content": _strip_blank_text_blocks(m.content)})
                                       if isinstance(m.content, list) else m)
                        seen_system_message = True
                    # else drop empty / duplicate system messages
                elif isinstance(m, HumanMessage):
                    cleaned.append(m)
                # ignore other types silently

            # Ensure conversation doesn't end with an AIMessage.
            # Bedrock rejects "assistant message prefill" — the conversation must end
            # with a user message. This happens when worker agent responses (AIMessages)
            # are copied back to the Supervisor's channel before re-invocation.
            if cleaned and isinstance(cleaned[-1], AIMessage):
                cleaned.append(HumanMessage(
                    content="Based on the above, decide your next action.",
                    additional_kwargs={
                        "_synthetic_nudge": "provider_requires_user_turn",
                        "_hide_from_stream": True,
                    },
                ))

            return cleaned

    def _fix_message_sequence_for_bedrock(self, msgs: list[AnyMessage]) -> list[AnyMessage]:
        """
        Validate and fix message sequences for strict LLM provider requirements.

        Some providers (like AWS Bedrock) require that every AIMessage with tool_calls
        is IMMEDIATELY followed by ToolMessage(s) with matching tool_call_ids.

        After rebuilding agent channels, this requirement might be violated because
        messages from different timepoints get mixed together.

        Strategy: Remove tool_calls from AIMessages that don't have immediate
        ToolMessage responses. This preserves the text content (which describes
        what was done) while removing the structured metadata that causes validation errors.

        This is provider-agnostic and safe because:
        - Text content usually describes tool usage
        - ToolMessages still exist in history showing results
        - Only structured metadata is removed
        """
        fixed = []

        for i, msg in enumerate(msgs):
            if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
                # Get the tool_call IDs from this message
                tool_call_ids = set(tc.get('id') for tc in msg.tool_calls if tc.get('id'))

                # Check if next message(s) are ToolMessages with matching IDs
                has_immediate_results = False
                if i + 1 < len(msgs):
                    next_msg = msgs[i + 1]
                    if isinstance(next_msg, ToolMessage):
                        # Check if tool_call_id matches any of our tool_calls
                        if hasattr(next_msg, 'tool_call_id') and next_msg.tool_call_id in tool_call_ids:
                            has_immediate_results = True

                if not has_immediate_results:
                    # Strip tool_calls from this AIMessage
                    msg_copy = msg.copy()
                    msg_copy.tool_calls = []

                    # Only include if it has text content
                    has_content = False
                    if isinstance(msg_copy.content, str) and msg_copy.content.strip():
                        has_content = True
                    elif isinstance(msg_copy.content, list) and any(
                        block.get('type') == 'text' and block.get('text', '').strip()
                        for block in msg_copy.content if isinstance(block, dict)
                    ):
                        has_content = True

                    if has_content:
                        fixed.append(msg_copy)
                    # If no content, skip this message entirely
                else:
                    # Has immediate tool results, keep as-is
                    fixed.append(msg)
            else:
                # Not an AIMessage with tool_calls, keep as-is
                fixed.append(msg)

        return fixed

    def _render_combined(self, messages):
        out = ""
        for m in messages:
            if isinstance(m, HumanMessage):
                if _is_internal_human_message(m):
                    continue
                out += f"👤> {m.content}\n"
            elif isinstance(m, AIMessage):
                out += f"🤖[{getattr(m,'name','Agent')}]> {m.content if isinstance(m.content,str) else ''}\n"
        return out

    async def _generate_per_agent_summaries(self) -> str:
        """
        Generate summaries from each participating agent about their own work.
        Returns formatted string with per-agent summaries.
        """
        agent_channels = {
            "Supervisor": "supervisor_messages",
            "Mythic_Operator": "mythic_operator_messages",
            "Mythic_Payload": "mythic_payload_messages",
            "Generalist": "generalist_messages",
            "BloodHound": "bloodhound_messages",
            "MCP_Manager": "mcp_manager_messages"
        }

        summaries = []

        for agent_name, channel_name in agent_channels.items():
            channel_messages = self.state.get(channel_name, [])

            # Skip if channel is empty or only has system/handoff messages
            # Count substantive messages (AIMessages with content or tool calls, ToolMessages)
            substantive_count = sum(1 for msg in channel_messages
                                   if isinstance(msg, (AIMessage, ToolMessage)))

            if substantive_count == 0:
                logger.debug(f"Skipping {agent_name} - no substantive work done")
                continue

            logger.info(f"Generating summary for {agent_name} ({substantive_count} substantive messages)")

            # Create agent-specific summary prompt with strong anti-hallucination instructions
            summary_prompt = HumanMessage(content=f"""You are {agent_name}. Review YOUR complete conversation history above and summarize YOUR work.

CRITICAL: Only reference information that ACTUALLY appears in the messages above. DO NOT make up, infer, or hallucinate ANY data. If you executed a tool, cite the EXACT results from the ToolMessage.

1. **Tools Called**: What tools/functions did YOU execute? (Include tool call IDs if visible)
2. **Data Gathered**: What SPECIFIC information did YOU collect? (Quote exact callback IDs, hostnames, usernames from tool outputs)
3. **Tasks Incomplete**: What tasks were YOU working on that are NOT yet complete?

Be specific and accurate (3-5 bullet points). Only summarize YOUR OWN actions based on messages in YOUR conversation history above.""")

            try:
                # Use the agent's own channel history (capped) to summarize. Raised alongside
                # the recursion bump (T1.4: 150 -> 250) so long solves don't drop early tool results.
                max_messages = 250
                if len(channel_messages) > max_messages:
                    logger.warning(f"{agent_name} has {len(channel_messages)} messages, using last {max_messages} for summary")
                    history = channel_messages[-max_messages:]
                else:
                    history = list(channel_messages)

                # Flatten the conversation to PLAIN TEXT for the summary call. Passing message objects
                # that still carry toolUse/toolResult content blocks makes Bedrock demand a toolConfig
                # ("The toolConfig field must be defined when using toolUse and toolResult content blocks")
                # — and the summary deliberately runs WITHOUT tools, so every summary 400s. THIS was the
                # "summary generation failed" bug: the prior cleanup only cleared AIMessage.tool_calls and
                # left ToolMessages (toolResult blocks) intact, then bound an empty tool list. Rendering
                # the history as text keeps the tool *data* the summary must cite while dropping the
                # structured tool blocks that trigger the requirement.
                def _text_of(content) -> str:
                    if isinstance(content, str):
                        return content.strip()
                    if isinstance(content, list):
                        parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                                parts.append(block["text"].strip())
                            elif isinstance(block, str) and block.strip():
                                parts.append(block.strip())
                        return "\n".join(parts)
                    return ""

                transcript_lines = []
                for msg in history:
                    if isinstance(msg, SystemMessage):
                        continue
                    text = _text_of(getattr(msg, "content", ""))
                    if isinstance(msg, ToolMessage):
                        if text:
                            transcript_lines.append(f"[Tool result — {getattr(msg, 'name', None) or 'tool'}]: {text}")
                    elif isinstance(msg, AIMessage):
                        calls = ""
                        if getattr(msg, "tool_calls", None):
                            calls = " (called: " + ", ".join(tc.get("name", "?") for tc in msg.tool_calls) + ")"
                        if text or calls:
                            transcript_lines.append(f"[{agent_name}{calls}]: {text}".rstrip())
                    elif text:
                        transcript_lines.append(f"[instruction]: {text}")

                transcript = "\n".join(transcript_lines) if transcript_lines else "(no prior content captured)"
                logger.info(f"Summary transcript for {agent_name}: {len(transcript_lines)} lines from {len(history)} messages")

                # Plain text only → no toolUse/toolResult blocks → no toolConfig requirement on any
                # provider. self.llm is the base chat model (no tools bound), so this is a clean call.
                summary_input = [
                    SystemMessage(content=f"You are {agent_name}, a specialized agent. Summarize YOUR OWN work concisely from the transcript below. Cite exact data (callback IDs, hostnames, usernames, task IDs); do NOT invent anything."),
                    HumanMessage(content=f"Conversation transcript:\n{transcript}\n\n---\n{summary_prompt.content}"),
                ]

                summary_resp = await self.llm.ainvoke(summary_input)

                logger.info(f"DEBUG: {agent_name} generated summary: {summary_resp.content[:500]}...")
                summary_content = summary_resp.content if hasattr(summary_resp, 'content') else str(summary_resp)

                summaries.append(f"**{agent_name}:**\n{summary_content}")

            except Exception as e:
                logger.warning(f"Could not generate summary for {agent_name}: {e}")
                summaries.append(f"**{agent_name}:** Work in progress (summary generation failed)")

        if not summaries:
            return "No agent summaries available - work may have just started."

        return "\n\n".join(summaries)

    def request_stop(self) -> None:
        """Kill switch for a running Sage session.

        Sets the cooperative flag checked by middleware/astream loops and cancels any registered
        invoke() task so long-running tool awaits cannot survive after Mythic marks the run stopped.
        """
        logger.info(f"🛑 Stop requested for session task_id={self.task_id}")
        self._stop_requested = True
        current_task = None
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        cancelled = 0
        for task in list(getattr(self, "_running_tasks", set()) or set()):
            if task is None or task.done() or task is current_task:
                continue
            try:
                task.cancel()
                cancelled += 1
            except Exception as exc:
                logger.warning(f"Failed to cancel running Sage task for session {self.task_id}: {exc}")
        if cancelled:
            logger.info(f"🛑 Cancelled {cancelled} running invoke task(s) for session task_id={self.task_id}")

    def _register_running_task(self, task: asyncio.Task | None = None) -> None:
        """Track the active invoke() task so operator stop can interrupt long awaits."""
        if task is None:
            return
        running = getattr(self, "_running_tasks", None)
        if running is None:
            running = set()
            self._running_tasks = running
        running.add(task)
        task.add_done_callback(lambda done_task: running.discard(done_task))

    async def _hitl_interrupt_pending(self, thread_id: str) -> bool:
        """True if the graph for this thread is paused on a HumanInTheLoopMiddleware interrupt.

        Detected via the checkpointer: aget_state surfaces pending interrupts on the StateSnapshot
        (top-level .interrupts and per-task .interrupts). Only relevant in supervised mode; auto mode
        never installs the middleware so this stays False and the path below is never taken.
        """
        if getattr(self, "mode", "auto") != "supervised" or not self.graph:
            return False
        try:
            snapshot = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
        except Exception as e:
            logger.warning(f"HITL: aget_state failed for thread {thread_id} ({e}); treating as no pending interrupt")
            return False
        if getattr(snapshot, "interrupts", None):
            return True
        for task in (getattr(snapshot, "tasks", None) or ()):
            if getattr(task, "interrupts", None):
                return True
        return False

    async def handle_hitl_resume(self, response: str, thread_id: str, operator_message: str = "") -> str:
        """Resume a graph paused on a guarded-tool approval interrupt with a DEFAULT-DENY decision map.

        Steering (Phase 3): when ``operator_message`` is non-empty (the operator hit Respond/Select with
        free-text), the guarded action is still rejected — never blind-run — but the operator's text becomes
        the rejection message, so the agent replans WITH the guidance instead of seeing a bare denial.

        Reads the pending interrupt to learn how many tool calls were interrupted (the middleware
        requires exactly one decision per interrupted tool call, else it raises ValueError), classifies
        the operator reply with _hitl_is_approved (default-deny), writes one audit line per decision, then
        resumes via Command(resume={"decisions": [...]}) on the SAME thread_id. The middleware re-executes
        the tool node on resume (replay-safe: the real side effect runs only after the resume value is read),
        so we add NO side effects here beyond the audit log.
        """
        config = RunnableConfig(configurable={"thread_id": thread_id})
        snapshot = await self.graph.aget_state(config)

        # Collect the pending HITLRequest action_requests, counted ONCE (single authoritative source +
        # dedupe). Unioning snapshot.interrupts with snapshot.tasks[].interrupts double-counts and breaks
        # the middleware's one-decision-per-hanging-tool-call check (task-598 ValueError).
        action_requests = _collect_hitl_action_requests(snapshot)

        approved = _hitl_is_approved(response)
        steer = (operator_message or "").strip()
        decision_word = "approve" if approved else ("steer" if steer else "deny")

        # One audit line + one Decision per interrupted tool call. On a steer, the guarded action is still
        # rejected (never blind-run), but the operator's text becomes the rejection message.
        decisions: list[dict] = []
        if action_requests:
            for ar in action_requests:
                tool_name = ar.get("name", "unknown") if isinstance(ar, dict) else "unknown"
                tool_args = ar.get("args", {}) if isinstance(ar, dict) else {}
                self._write_hitl_audit(tool_name, tool_args, decision_word)
                if approved:
                    decisions.append({"type": "approve"})
                else:
                    message = (f"[Operator steering] {steer}" if steer
                               else f"[DENIED by operator] {tool_name} was not executed.")
                    decisions.append({"type": "reject", "message": message})
        else:
            # No structured action_requests recovered — still resume safely with a single decision so the
            # graph does not hang. Audit it as an unknown-tool deny/steer/approve.
            self._write_hitl_audit("unknown", {}, decision_word)
            if approved:
                decisions.append({"type": "approve"})
            else:
                message = f"[Operator steering] {steer}" if steer else "[DENIED by operator]"
                decisions.append({"type": "reject", "message": message})

        logger.info(f"HITL resume on thread {thread_id}: {decision_word} for {len(decisions)} tool call(s)")

        # Resume the paused graph with the decision payload the installed middleware expects.
        async for event in self.graph.astream(
            Command(resume={"decisions": decisions}),
            self._graph_run_config(thread_id)
        ):
            if self._stop_requested:
                logger.info("🛑 Stop requested — terminating graph execution (HITL resume)")
                break
            # A subsequent guarded tool call in the same supervised run interrupts again — surface
            # the next approve/deny prompt and pause rather than silently halting.
            if isinstance(event, dict) and "__interrupt__" in event:
                await self._surface_hitl_interrupt(event)
                break
            await self._process_stream_event(event)

        return ""

    def _write_hitl_audit(self, tool: str, args: dict, decision: str) -> None:
        """Append one JSON line to MEMORY/audit.jsonl recording an approve/deny decision. Best-effort:
        a failure to write the audit log must never crash the resume path."""
        try:
            from pathlib import Path
            from datetime import datetime, timezone
            audit_dir = Path("MEMORY")
            audit_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "operator": getattr(self, "operator", None) or "unknown",
                "tool": tool,
                "args": args,
                "decision": "approve" if decision == "approve" else "deny",
                "mode": "supervised",
            }
            with (audit_dir / "audit.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except Exception as e:
            logger.warning(f"HITL: failed to write audit line ({e})")

    async def _surface_hitl_interrupt(self, event: dict) -> bool:
        """If an astream event carries a HumanInTheLoopMiddleware approval interrupt, stream a clear
        approve/deny prompt to the operator and return True. The graph stays paused (checkpointed) so
        the operator's next message resumes it via handle_hitl_resume. Without this, supervised mode
        halts on the raw tool-call request with no prompt (the 2026-06-01 task-595 symptom: 'stop had
        no text back to the user, last text showing as the tool call request')."""
        interrupts = event.get("__interrupt__") if isinstance(event, dict) else None
        if not interrupts:
            return False
        # Chat path (Option C): a native confirmation card replaces the text approve/deny prompt. sage_chat
        # injects _hitl_card_emitter per turn; it emits an input_requested card with complete_request=False,
        # releasing the channel while the graph waits on disk. _hitl_card_pending tells the chat handler a
        # card already released the request, so it must NOT send a terminal completion.
        if getattr(self, "_hitl_card_emitter", None) is not None:
            action_requests = []
            for itr in interrupts:
                val = getattr(itr, "value", None)
                if isinstance(val, dict):
                    for ar in (val.get("action_requests") or []):
                        if isinstance(ar, dict):
                            action_requests.append(ar)
            try:
                await self._hitl_card_emitter(action_requests)
                self._hitl_card_pending = True
            except Exception as e:
                logger.warning(f"HITL: failed to emit confirmation card ({e})")
            logger.info(f"HITL interrupt surfaced as native card ({len(action_requests)} action(s))")
            return True
        lines = []
        for itr in interrupts:
            val = getattr(itr, "value", None)
            if isinstance(val, dict):
                for ar in (val.get("action_requests") or []):
                    if isinstance(ar, dict):
                        name = ar.get("name", "a guarded tool")
                        args = ar.get("args", {})
                        try:
                            args_str = json.dumps(args, default=str, sort_keys=True)
                        except Exception:
                            args_str = str(args)
                        lines.append(f"  • `{name}`  {args_str[:400]}")
        body = "\n".join(lines) if lines else "  • (a guarded tool call)"
        msg = (
            "⏸️ **Approval required — supervised mode**\n\n"
            "Sage wants to run the following guarded action(s):\n"
            f"{body}\n\n"
            "Reply **`approve`** to run it, or **`deny`** to skip it. "
            "Anything other than an explicit approval is treated as a denial."
        )
        try:
            await self._stream_message_to_mythic(msg)
        except Exception as e:
            logger.warning(f"HITL: failed to stream approval prompt ({e})")
        logger.info(f"HITL interrupt surfaced to operator ({len(lines)} action(s)); awaiting approve/deny")
        return True

    def _seed_autonomous_objective(self, prompt: str) -> None:
        """For a controller-owned solve the prompt IS the mission: seed it on the client so
        MythicTools._engagement_objective() adopts it when no operator/env/ledger objective exists. Clearing
        the one-shot persist latch on a NEW prompt lets a reused client re-adopt per solve (an operator-set
        objective stays sticky — the persist path refuses to clobber it). Then a LOUD guard: if the objective
        STILL resolves opaque (blank/opaque prompt AND no operator/env objective), warn once — in that case
        completion-recognition (engagement_state._objective_is_complete) is unreachable and the solve would
        silently over-reach until the stall detector halts it. No-op unless this is a controller-owned solve with a
        live client. Fail-open: never breaks the solve."""
        if not self._controller_owned_solve() or self.mythic_client is None:
            return
        try:
            if self.mythic_client._autonomous_objective_seed != prompt:
                self.mythic_client._autonomous_objective_seed = prompt
                self.mythic_client._autonomous_objective_persisted = False
            if str(self.mythic_client._engagement_objective() or "").startswith("sage-engagement"):
                logger.warning(
                    "⚠️ autonomous solve has no resolvable objective (opaque sage-engagement:* fallback) — "
                    "completion-recognition is UNREACHABLE; set SAGE_ENGAGEMENT_OBJECTIVE or run "
                    "`state objective <text>`. The solve will run but cannot recognize completion."
                )
        except Exception:
            pass

    async def invoke(self, prompt: str, is_interactive: bool = False) -> str:
        """
        Invoke the model with a prompt and return the response.
        :param prompt: The prompt to send to the model.
        :param is_interactive: Whether this is an interactive task (subsequent turn in conversation).
        :return: The model's response as a string.
        """
        # Store for use in streaming formatter
        self.is_interactive = is_interactive
        try:
            self._register_running_task(asyncio.current_task())
        except RuntimeError:
            pass
        if not self.graph:
            raise ValueError("No graph defined for the model. Ensure the model's initialize() method has been called.")

        thread_id = self._session_thread_id()
        if isinstance(getattr(self, "_controller_hitl_pending", None), dict):
            logger.info("HITL controller approval pending — routing operator reply to controller resume")
            return await self.handle_controller_hitl_resume(prompt)
        if await self._hitl_interrupt_pending(thread_id):
            logger.info(f"HITL interrupt pending on thread {thread_id} — routing operator reply to approve/deny resume")
            return await self.handle_hitl_resume(prompt, thread_id)

        # Fresh turn only: supervised chat objective prompts use the bounded execution kernel while scoped
        # questions remain on the normal supervisor graph. Leave the flag alive across a controller-native HITL
        # pause so the next approval can resume the exact pending move.
        self._supervised_objective_active = self._supervised_objective_controller_enabled_for_prompt(prompt)

        # Fresh stall-detector window per solve — never carry a prior objective's counters into this one
        # (a Sage session may reuse one Model across invoke() calls).
        self._autonomous_stall_progress = None
        self._autonomous_stall_count = 0
        self._autonomous_stall_sig = None
        # Fresh control-state loop-breaker per solve (same Model-reuse hazard as the stall counters above) so a
        # prior objective's blockers never carry into this solve (Forge-caught cross-solve leak).
        try:
            from . import worker_outcome as _wo_init
        except ImportError:
            import worker_outcome as _wo_init
        self._loop_breaker = _wo_init.LoopBreakerState()
        # Self-describe the mission: for a controller-owned solve the prompt IS the objective, so seed it on the
        # client. _engagement_objective() adopts it as the engagement objective when none is set, so
        # completion-recognition has a parseable target instead of the opaque sage-engagement:<task> fallback
        # (which is never completable) — the root cause of post-objective over-reach. Generic to any caller;
        # never overwrites an operator/env objective. No I/O here (deferred to the resolved-key write).
        self._seed_autonomous_objective(prompt)
        logger.debug(f"Invoking LLM with provider: '{self.provider}', model: '{self.model}', prompt: '{prompt}'")

        # Ensure per-agent channels exist
        for ch in [
            "supervisor_messages",
            "generalist_messages",
            "mythic_operator_messages",
            "mythic_payload_messages",
            "mcp_manager_messages", "bloodhound_messages",
        ]:
            if ch not in self.state:
                self.state[ch] = []

        if "messages" not in self.state:
            self.state["messages"] = []

        # Check if we're responding to a recursion limit continuation request
        # If so, delegate to handle_continuation_response() instead of normal flow
        was_recursion_requested = self.state.get("recursion_summary_requested", False)
        if was_recursion_requested:
            logger.info(f"Detected continuation response: '{prompt}' - delegating to handle_continuation_response()")
            # Reset the flag before handling
            self.state["recursion_summary_requested"] = False
            return await self.handle_continuation_response(prompt)
        try:
            if self.mythic_client is not None:
                self.mythic_client.begin_operator_turn(prompt)
        except Exception:
            pass

        # CRITICAL: Reset recursion flags before each invocation
        # Without this, the graph sees stale flags from previous runs and
        # immediately returns with recursion_summary_requested=True, causing
        # the recursion summary to be shown again instead of continuing
        if "recursion_summary_requested" in self.state:
            logger.debug("Resetting recursion_summary_requested flag before invocation")
            self.state["recursion_summary_requested"] = False

        # CRITICAL: Clean up any dangling tool_use blocks before adding the user's message
        # This prevents Anthropic API errors when resuming from recursion limit
        # Note: We don't load from checkpoint here because self.state already contains
        # the full conversation history from the graph's checkpointing mechanism
        #self._cleanup_dangling_tool_calls()

        user_msg = HumanMessage(content=prompt)
        user_msg_seq = self._next_seq()
        _tag_msg(user_msg, user_msg_seq)
        self.state["supervisor_messages"].append(user_msg)

        # Manually stream the user prompt for non-interactive tasks
        # (Interactive tasks have Mythic echo the prompt, so we skip)
        if not is_interactive:
            formatted_prompt = self._format_message_for_streaming(user_msg, agent_name=None)
            if formatted_prompt:
                await self._stream_message_to_mythic(formatted_prompt)

        if self._objective_completion_preflight_allowed(prompt) and await self._maybe_stream_objective_completion_stop(
            refresh_footholds=True,
            require_autonomous=False,
        ):
            return ""

        if self._looks_like_casual_greeting(prompt):
            logger.info("Casual greeting routed to one tool-free Generalist turn")
            return await self._run_generalist_only_turn(prompt)

        # Policy-selected execution kernel. For autonomous auto one-shots, supervised autonomous chat, or an explicit
        # supervised chat objective turn with controller-native HITL, OWN the
        # observe->policy-select->execute->verify->stop cycle in bounded code, bypassing the
        # Supervisor/worker astream negotiation entirely. SAGE_AUTONOMOUS_CONTROLLER=0 is the rollback path.
        # SAFETY GATE: supervised controller execution is chat-only and pauses inside the controller seams before
        # any capability or collection move; query remains one-shot and falls through to the legacy graph HITL
        # path because it has no interactive approve/deny transport. Interactive approval replies are handled
        # above before a fresh solve is seeded.
        if self._should_use_controller(is_interactive, prompt):
            try:
                return await self._run_autonomous_controller(prompt)
            except asyncio.CancelledError:
                logger.info("🛑 Autonomous controller cancelled — clean stop")
                try:
                    await self._stream_message_to_mythic("\n🛑 Session stopped by operator.\n")
                except Exception:
                    pass
                raise
            except Exception as e:
                logger.error(f"Autonomous controller failed: {e}", exc_info=True)
                return f"Sage deterministic execution error: {type(e).__name__}: {e}"

        try:
            # Use a central graph recursion budget so operator-facing max_steps=0 can mean
            # unbounded autonomous budget while LangGraph still receives a valid positive limit.
            logger.debug(f"🚀 Before astream: self.state._message_seq={self.state.get('_message_seq')}, Model._message_seq={self._message_seq}")

            # Stream graph execution and process events incrementally
            hitl_interrupted = False
            async for event in self.graph.astream(
                self.state,
                self._graph_run_config(self._session_thread_id())
            ):
                # Cooperative kill switch: an operator `exit`/stop set _stop_requested on this
                # Model; halt before driving the next super-step so the session can't run away.
                if self._stop_requested:
                    logger.info("🛑 Stop requested — terminating graph execution (main loop)")
                    try:
                        await self._stream_message_to_mythic("\n🛑 Session stopped by operator.\n")
                    except Exception:
                        pass
                    # Any sub-agent card still open would otherwise stay stuck on "running".
                    await self._close_all_delegations(status="stopped")
                    break

                # HITL: in supervised mode a guarded tool call interrupts here. The outer graph
                # (which holds the checkpointer) emits a clean __interrupt__ event; surface the
                # approve/deny prompt and pause — the graph state is checkpointed for resume.
                if isinstance(event, dict) and "__interrupt__" in event:
                    await self._surface_hitl_interrupt(event)
                    hitl_interrupted = True
                    break

                # DEBUG: Log what's IN the event
                for node_name, state_update in event.items():
                    if node_name not in ["__start__", "__end__"]:
                        channel_keys = list(state_update.keys()) if isinstance(state_update, dict) else []
                        logger.debug(f"DEBUG: Event from node '{node_name}' has channels: {channel_keys}")
                        if "mythic_operator_messages" in channel_keys:
                            logger.debug(f"  mythic_operator_messages has {len(state_update['mythic_operator_messages'])} messages")

                # Check for recursion_handback flag in the event BEFORE processing further.
                # When a specialist calls summarize_and_handback, the flag appears in the
                # stream event. We must break immediately to prevent the Supervisor from
                # running another turn and ignoring the handback.
                handback_detected = False
                for node_name, state_update in event.items():
                    if isinstance(state_update, dict) and state_update.get("recursion_handback"):
                        logger.info(f"Handback detected in event from node '{node_name}' — stopping astream to wait for user")
                        self.state["recursion_handback"] = True
                        handback_detected = True
                if handback_detected:
                    # Stream any messages from this event before breaking
                    await self._process_stream_event(event)
                    break

                # Process each streaming event and stream messages to Mythic
                await self._process_stream_event(event)

                # Update self.state with new values from event
                for node_name, state_update in event.items():
                    if node_name in ["__start__", "__end__"]:
                        continue
                    for ch in [
                        "supervisor_messages",
                        "generalist_messages",
                        "mythic_operator_messages",
                        "mythic_payload_messages",
                        "mcp_manager_messages", "bloodhound_messages",
                        "autonomous_executor_messages",
                        "_message_seq"
                    ]:
                        if ch in state_update:
                            if ch == "_message_seq":
                                self._message_seq = state_update[ch]
                                self.state[ch] = state_update[ch]
                            else:
                                # Append new messages to state (operator.add behavior)
                                if ch not in self.state:
                                    self.state[ch] = []

                                # DEBUG: Log what we're extending
                                old_len = len(self.state[ch])
                                self.state[ch].extend(
                                    msg for msg in state_update[ch]
                                    if not _is_internal_human_message(msg)
                                )
                                new_len = len(self.state[ch])
                                if new_len > old_len:
                                    logger.debug(f"  Extended {ch}: {old_len} -> {new_len} messages")

                if await self._maybe_stream_objective_completion_stop():
                    break

            logger.debug(f"📥 After astream: self.state._message_seq={self.state.get('_message_seq')}")

            # HITL: a guarded tool call paused the graph for operator approval. The approve/deny prompt
            # was already streamed; return cleanly (do NOT merge the proposed tool-call AIMessage as the
            # final answer). The operator's next message resumes via handle_hitl_resume.
            if hitl_interrupted:
                return ""

            # Create resp variable for compatibility with downstream code
            resp = self.state

            # Merge all channels, deduplicate by message ID, and sort by sequence
            all_messages = []
            seen_ids = set()
            for ch in ["supervisor_messages", "generalist_messages", "mythic_operator_messages", "mythic_payload_messages", "mcp_manager_messages", "bloodhound_messages", "autonomous_executor_messages"]:
                ch_msgs = self.state.get(ch, [])
                logger.info(f"📊 Channel {ch}: {len(ch_msgs)} messages")
                for idx, msg in enumerate(ch_msgs):
                    mid = _msg_id(msg)
                    msg_type = type(msg).__name__
                    seq = _get_seq(msg)
                    tool_calls = len(getattr(msg, 'tool_calls', None) or [])
                    if mid not in seen_ids:
                        all_messages.append(msg)
                        seen_ids.add(mid)
                        logger.debug(f"  [{idx}] ADDED {msg_type} seq={seq} tools={tool_calls} id={mid[:50]}")
                    else:
                        logger.debug(f"  [{idx}] SKIP (dup) {msg_type} seq={seq} tools={tool_calls} id={mid[:50]}")
            force_flush_all_handlers()

            # Sort by sequence number for chronological order
            # Secondary sort: when sequences are equal, HumanMessages before AIMessages
            # This ensures delegated tasks appear before the agent's response
            def _sort_key(m):
                seq = _get_seq(m)
                # Priority: HumanMessage=0, ToolMessage=1, AIMessage=2, other=3
                if isinstance(m, HumanMessage):
                    type_priority = 0
                elif isinstance(m, ToolMessage):
                    type_priority = 1
                elif isinstance(m, AIMessage):
                    type_priority = 2
                else:
                    type_priority = 3
                return (seq, type_priority)

            all_messages.sort(key=_sort_key)
            logger.info(f"📊 Merged total: {len(all_messages)} unique messages")

            synthetic_resp = {
                "messages": all_messages,
                "recursion_summary_requested": resp.get("recursion_summary_requested", False),
                "recursion_handback": resp.get("recursion_handback", False),
            }
            # Check if we got a recursion summary request or handback from specialist
            if synthetic_resp.get("recursion_summary_requested", False):
                logger.info("Recursion limit approached - user continuation requested")
                return ""  # All output already streamed to Mythic
            elif synthetic_resp.get("recursion_handback", False):
                logger.info("Recursion handback received from specialist agent")
                return ""  # All output already streamed to Mythic
        except asyncio.CancelledError:
            # request_stop() cancels the active invoke task to interrupt long-running tool awaits
            # immediately. Treat that as an operator stop, not as an invocation failure.
            self._stop_requested = True
            stop_message = "\n🛑 Session stopped by operator.\n"
            logger.info("🛑 Operator stop cancelled active invoke task — terminating session")
            # Hard cancel: shield the stop notice + card-close so the tearing-down task can't cut
            # the emits off before they reach Mythic (which left the sub-agent card stuck "running").
            await self._run_operator_stop_shielded(stop_message)
            return ""
        except _OperatorStopRequested:
            # Kill-switch fired inside an agent turn (finer-grained than the between-super-steps
            # check). End the session cleanly instead of surfacing it as an error.
            if getattr(self, "_global_step_limit_hit", False):
                stop_message = (
                    f"\n🛑 Halted: global step limit ({self._max_steps}) reached; "
                    "the run may be looping without progress.\n"
                )
                logger.info(
                    f"🛑 Global step limit stop honored inside agent loop after "
                    f"{self._global_step_count} model steps"
                )
            else:
                stop_message = "\n🛑 Session stopped by operator.\n"
                logger.info("🛑 Operator stop honored inside agent loop — terminating session")
            try:
                await self._stream_message_to_mythic(stop_message)
            except Exception:
                pass
            await self._close_all_delegations(status="stopped")
            return ""
        except GraphRecursionError as e:
            # Catch recursion limit error and return progress made so far
            logger.warning(f"Recursion limit hit: {e}")

            # CRITICAL: When recursion limit hits, astream() only yielded events for COMPLETED nodes.
            # The current node (e.g., Mythic_Operator) was terminated mid-execution, so its messages
            # are NOT in self.state. We MUST restore from checkpoint to get partial progress.

            thread_id = self._session_thread_id()
            config = RunnableConfig(configurable={"thread_id": thread_id})

            # DEBUG: Log what's in self.state BEFORE checkpoint recovery
            logger.info(f"DEBUG: In-memory state BEFORE checkpoint recovery:")
            for ch in ["messages", "supervisor_messages", "mythic_operator_messages",
                      "mythic_payload_messages", "generalist_messages", "mcp_manager_messages", "bloodhound_messages"]:
                ch_msgs = self.state.get(ch, [])
                logger.info(f"  {ch}: {len(ch_msgs)} messages")

            # DEBUG: List ALL checkpoints to understand structure
            logger.info(f"DEBUG: Listing ALL checkpoints for thread_id='{thread_id}':")
            checkpoint_list_count = 0
            try:
                async for cp_tuple in self.memory.alist(config, limit=50):
                    checkpoint_list_count += 1
                    if cp_tuple and cp_tuple.checkpoint:
                        ns = cp_tuple.metadata.get("checkpoint_ns", "")
                        cp_id = cp_tuple.checkpoint.get("id", "unknown")
                        channel_values = cp_tuple.checkpoint.get("channel_values", {})
                        channel_keys = list(channel_values.keys())

                        # Show message counts for each channel
                        channel_info = {}
                        for key, value in channel_values.items():
                            if isinstance(value, list):
                                channel_info[key] = len(value)
                            else:
                                channel_info[key] = f"{type(value).__name__}"

                        logger.info(f"  Checkpoint #{checkpoint_list_count}: namespace='{ns}', id={cp_id[:20]}..., channels={channel_info}")
            except Exception as e:
                logger.warning(f"Could not list checkpoints: {e}")

            logger.info(f"Found {checkpoint_list_count} total checkpoints for this thread")

            # CRITICAL FIX: Most recent checkpoints only have 'messages', not agent-specific channels.
            # Agent channels (mythic_operator_messages, etc.) are only in earlier checkpoints
            # from before the node started executing. We need to:
            # 1. Get global 'messages' from LATEST checkpoint (has all accumulated messages)
            # 2. Get agent-specific channels from checkpoint that has them (usually earlier)

            latest_messages = None
            best_agent_state = {}
            try:
                async for checkpoint_tuple in self.memory.alist(config, limit=50):
                    if checkpoint_tuple and checkpoint_tuple.checkpoint:
                        channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})

                        # Get global messages from first checkpoint (latest)
                        if latest_messages is None and "messages" in channel_values:
                            latest_messages = channel_values["messages"]
                            logger.info(f"Found latest 'messages' channel with {len(latest_messages)} messages")

                        # Look for agent-specific channels
                        for channel_name in ["supervisor_messages", "mythic_operator_messages",
                                            "mythic_payload_messages", "generalist_messages",
                                            "mcp_manager_messages", "bloodhound_messages"]:
                            if channel_name in channel_values:
                                current_len = len(channel_values[channel_name]) if isinstance(channel_values[channel_name], list) else 0
                                existing_len = len(best_agent_state.get(channel_name, [])) if isinstance(best_agent_state.get(channel_name), list) else 0

                                # Keep the version with more messages
                                if current_len > existing_len:
                                    best_agent_state[channel_name] = channel_values[channel_name]
                                    logger.info(f"Found better {channel_name} with {current_len} messages")

                # Now restore the best versions we found
                if latest_messages:
                    self.state["messages"] = latest_messages
                    logger.info(f"Restored messages: {len(latest_messages)} messages from latest checkpoint")

                    # Check if agent channels already have substantive content (from previous recursion limit)
                    # If they do, DON'T rebuild from checkpoint - preserve the existing content
                    channels_need_rebuild = False
                    for ch in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages",
                              "generalist_messages", "mcp_manager_messages", "bloodhound_messages"]:
                        existing = self.state.get(ch, [])
                        substantive_count = sum(1 for msg in existing if isinstance(msg, (AIMessage, ToolMessage)))

                        if substantive_count == 0:
                            # Channel is empty or only has system/human messages
                            channels_need_rebuild = True
                            break

                    if channels_need_rebuild:
                        # CRITICAL: Rebuild agent-specific channels from global messages
                        # The global messages channel has ALL the work, but agent channels don't
                        # because the node hasn't returned yet. Reconstruct them from message metadata.
                        logger.info("Rebuilding agent-specific channels from global messages...")

                        # Initialize channels if not already present
                        for ch in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages",
                                  "generalist_messages", "mcp_manager_messages", "bloodhound_messages"]:
                            if ch not in self.state:
                                self.state[ch] = []
                    else:
                        logger.info("Agent channels already have content from previous run - skipping rebuild to preserve state")
                        # Still need to ensure channels exist
                        for ch in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages",
                                  "generalist_messages", "mcp_manager_messages", "bloodhound_messages"]:
                            if ch not in self.state:
                                self.state[ch] = []
                        # Skip the message sorting below
                        channels_need_rebuild = False

                    # Sort messages into their respective channels based on metadata
                    if channels_need_rebuild:
                        for msg in latest_messages:
                            # Check message.name attribute (set by MessageCaptureCallback)
                            agent_name = getattr(msg, 'name', None)

                            # Also check _delegated_to in additional_kwargs (for handoff messages)
                            delegated_to = msg.additional_kwargs.get('_delegated_to') if hasattr(msg, 'additional_kwargs') else None

                            # Map agent names to channels
                            if agent_name == "Supervisor" or delegated_to == "Supervisor":
                                if msg not in self.state["supervisor_messages"]:
                                    self.state["supervisor_messages"].append(msg)
                            elif agent_name == "Mythic_Operator" or delegated_to == "Mythic_Operator":
                                if msg not in self.state["mythic_operator_messages"]:
                                    self.state["mythic_operator_messages"].append(msg)
                            elif agent_name == "Mythic_Payload" or delegated_to == "Mythic_Payload":
                                if msg not in self.state["mythic_payload_messages"]:
                                    self.state["mythic_payload_messages"].append(msg)
                            elif agent_name == "Generalist" or delegated_to == "Generalist":
                                if msg not in self.state["generalist_messages"]:
                                    self.state["generalist_messages"].append(msg)
                            elif agent_name == "BloodHound" or delegated_to == "BloodHound":
                                if msg not in self.state["bloodhound_messages"]:
                                    self.state["bloodhound_messages"].append(msg)
                            elif agent_name == "MCP_Manager" or delegated_to == "MCP_Manager":
                                if msg not in self.state["mcp_manager_messages"]:
                                    self.state["mcp_manager_messages"].append(msg)

                        # Log rebuilt channel sizes
                        for ch in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages",
                                  "generalist_messages", "mcp_manager_messages", "bloodhound_messages"]:
                            logger.info(f"Rebuilt {ch}: {len(self.state[ch])} messages")

                        # CRITICAL: Validate and fix message sequences for Bedrock compatibility
                        # Bedrock requires that every AIMessage with tool_calls is IMMEDIATELY
                        # followed by ToolMessage(s) with matching tool_call_ids
                        # After rebuilding channels, this requirement might be violated
                        logger.info("Validating message sequences for LLM provider compatibility...")
                        for ch in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages",
                                  "generalist_messages", "mcp_manager_messages", "bloodhound_messages"]:
                            self.state[ch] = self._fix_message_sequence_for_bedrock(self.state[ch])

                if not latest_messages:
                    logger.warning("No latest messages found in checkpoints!")
                    # Fall back to best agent state if available
                    for channel_name, channel_data in best_agent_state.items():
                        self.state[channel_name] = channel_data
                        logger.info(f"Restored {channel_name}: {len(channel_data)} messages from best checkpoint (fallback)")

            except Exception as checkpoint_error:
                logger.warning(f"Could not retrieve checkpoint after recursion limit: {checkpoint_error}")

            # DEBUG: Log what's in self.state AFTER checkpoint recovery
            logger.info(f"DEBUG: In-memory state AFTER checkpoint recovery:")
            for ch in ["messages", "supervisor_messages", "mythic_operator_messages",
                      "mythic_payload_messages", "generalist_messages", "mcp_manager_messages", "bloodhound_messages"]:
                ch_msgs = self.state.get(ch, [])
                logger.info(f"  {ch}: {len(ch_msgs)} messages")

                # Sample mythic_operator_messages
                if ch == "mythic_operator_messages" and len(ch_msgs) > 0:
                    logger.info(f"  DEBUG: Sampling first 5 {ch} messages:")
                    for idx, msg in enumerate(ch_msgs[:5]):
                        msg_type = type(msg).__name__
                        content_preview = str(msg.content)[:150] if hasattr(msg, 'content') else "N/A"
                        logger.info(f"    [{idx}] {msg_type}: {content_preview}...")

            # Generate per-agent summaries for accurate state capture
            # Each agent that did work summarizes its own work
            agent_summaries = await self._generate_per_agent_summaries()

            # Create continuation message with per-agent summaries
            summary_text = agent_summaries if agent_summaries else "Work in progress across multiple agents."

            continuation_message = AIMessage(content=f"""🔄 **Recursion Limit Reached**

**Progress by Agent:**
{summary_text}

**Status:** Hit the system's iteration limit of {self._graph_recursion_limit()} steps. All work and context have been preserved in each agent's conversation history.

**Your Options:**
• Reply **"continue"** to increase the limit and keep going from where we left off
• Reply **"stop"** to end the current task
• Provide specific instructions to redirect the approach

**What would you like to do?**"""
            )

            # Add continuation message to state
            # CRITICAL: Add to BOTH messages AND supervisor_messages so Supervisor can see the summary
            continuation_message_seq = self._next_seq()
            _tag_msg(continuation_message, continuation_message_seq)
            self.state["messages"].append(continuation_message)
            self.state["supervisor_messages"].append(continuation_message)
            self.state["recursion_summary_requested"] = True

            # Stream the continuation message to Mythic
            formatted_continuation = self._format_message_for_streaming(continuation_message, agent_name="System")
            if formatted_continuation:
                await self._stream_message_to_mythic(formatted_continuation)

            logger.info(f"Returning recursion summary with continuation prompt")
            return ""  # All output already streamed to Mythic

        except Exception as e:
            # Catch any other errors (API errors, context limit exceeded, etc.)
            logger.error(f"Error during graph execution: {e}", exc_info=True)

            # Collect partial work from state/checkpoints
            thread_id = self._session_thread_id()
            config = RunnableConfig(configurable={"thread_id": thread_id})

            all_messages = []
            try:
                # Try to get messages from checkpoint
                checkpoint = await self.memory.aget_tuple(config)
                if checkpoint and checkpoint.checkpoint:
                    saved_state = checkpoint.checkpoint.get("channel_values", {})

                    # Merge all agent channels
                    for ch in ["messages", "supervisor_messages", "generalist_messages",
                               "mythic_operator_messages", "mythic_payload_messages", "mcp_manager_messages", "bloodhound_messages"]:
                        if ch in saved_state:
                            ch_messages = saved_state[ch]
                            for msg in ch_messages:
                                if msg not in all_messages:
                                    all_messages.append(msg)

                    logger.info(f"Collected {len(all_messages)} messages from checkpoint after error")
            except Exception as checkpoint_error:
                logger.warning(f"Could not retrieve checkpoint after error: {checkpoint_error}")

            # If we couldn't get checkpoint messages, use what's in current state
            if not all_messages:
                all_messages = self.state.get("messages", [])
                logger.info(f"Using {len(all_messages)} messages from current state")

            # Create error message that includes partial work
            error_msg = str(e)
            error_type = type(e).__name__

            error_message = AIMessage(content=f"""❌ **Error: {error_type}**

**Error Details:**
{error_msg}

**Partial Work Preserved:**
The conversation history below shows all work completed before the error occurred. This has been saved and you can continue from here.

**Next Steps:**
• Review the conversation history to see what was accomplished
• Adjust your approach (e.g., use a smaller scope, different model, or break into smaller tasks)
• Try again with modified parameters
"""
            )

            # Add error message to state
            all_messages.append(error_message)
            self.state["messages"] = all_messages

            # Stream the error message to Mythic
            formatted_error = self._format_message_for_streaming(error_message, agent_name="System")
            if formatted_error:
                await self._stream_message_to_mythic(formatted_error)

            # Re-raise the exception so chat.py can handle setting response.Success = False
            # The error message has already been streamed to the user
            raise

        return ""  # All output already streamed to Mythic

    async def _classify_continuation_intent(self, response: str) -> str:
        """Classify an operator's recursion-continuation reply as CONTINUE, STOP, or REDIRECT.

        The old dispatch used a 3-word exact match and treated ANYTHING that wasn't literally
        'continue'/'stop' as a new task to RUN — so 'Don't run any tasks, just give me a summary'
        fell through to the run-the-graph branch and the system ran away. This classifies intent
        so natural-language stop/inhibit instructions are honored. Fast exact-match first, then a
        cheap PLAIN-TEXT LLM call (tiny input, no tools → no Bedrock toolConfig issue). Falls back
        to REDIRECT on any failure (the Supervisor prompt-precedence rule is the second safety net).
        """
        text = response.lower().strip()
        if text in ["continue", "yes", "keep going", "go", "proceed", "y"]:
            return "CONTINUE"
        if text in ["stop", "no", "end", "quit", "halt", "cancel", "abort", "n"]:
            return "STOP"
        if self.llm is None:
            return "REDIRECT"
        try:
            classification_prompt = [
                SystemMessage(content=(
                    "You classify an operator's instruction to an autonomous offensive-security agent "
                    "that is paused mid-task. Reply with EXACTLY ONE word:\n"
                    "CONTINUE = keep going with the current task.\n"
                    "STOP = halt and run NO more tasks/commands (e.g. 'stop', \"don't run anything\", "
                    "'hold off', 'pause', 'wait', 'just give me a summary', 'no more tasks', 'only report').\n"
                    "REDIRECT = a NEW task or change of direction that requires running commands.\n"
                    "If the instruction forbids running tasks or asks only to summarize/report, it is STOP. "
                    "When unsure between STOP and REDIRECT, answer STOP."
                )),
                HumanMessage(content=f"Operator instruction: {response}\n\nOne word (CONTINUE / STOP / REDIRECT):"),
            ]
            resp = await self.llm.ainvoke(classification_prompt)
            out = (resp.content if hasattr(resp, "content") else str(resp)).strip().upper()
            for label in ("CONTINUE", "STOP", "REDIRECT"):
                if label in out:
                    logger.info(f"Continuation intent for '{response[:60]}' classified as {label}")
                    return label
            return "REDIRECT"
        except Exception as e:
            logger.warning(f"Continuation intent classification failed ({e}); defaulting to REDIRECT")
            return "REDIRECT"

    async def handle_continuation_response(self, response: str) -> str:
        """
        Handle user response to recursion limit continuation request.
        :param response: User's response ('continue', 'stop', 'redirect', or specific instruction)
        :return: The model's response after handling the continuation
        """
        logger.debug(f"Handling continuation response: '{response}'")

        # Reset the recursion flags
        if "recursion_summary_requested" in self.state:
            self.state["recursion_summary_requested"] = False
        if "recursion_handback" in self.state:
            self.state["recursion_handback"] = False

        thread_id = self._session_thread_id()
        config = RunnableConfig(configurable={"thread_id": thread_id})

        # Classify intent so natural-language stop/inhibit instructions ("don't run any tasks,
        # just give me a summary") are honored as STOP instead of being treated as a new task
        # to run. Replaces the old 3-word exact match that caused the post-handback runaway.
        intent = await self._classify_continuation_intent(response)

        if intent == "CONTINUE":
            # Increase recursion limit and continue
            logger.info("User requested to continue - increasing recursion limit")

            # Add continuation message to supervisor channel (not the literal "continue" user typed)
            # Make it explicit that this is a continuation building on prior work
            continuation_msg = HumanMessage(content="""Review the progress summary above and continue with the task from where we left off.

IMPORTANT:
- DO NOT repeat any work that was already completed
- Build upon the information and results already gathered
- Reference previous tool outputs instead of re-running the same tools
- Focus on the remaining tasks identified in the summary

Continue now.""")
            continuation_msg_seq = self._next_seq()
            _tag_msg(continuation_msg, continuation_msg_seq)
            self.state["supervisor_messages"].append(continuation_msg)
            self.state["messages"].append(continuation_msg)

            # Stream the continuation instruction to show what we're telling the LLM
            # (Always stream this, even for interactive tasks, because it's our replacement message)
            formatted = self._format_message_for_streaming(continuation_msg, agent_name=None)
            if formatted:
                await self._stream_message_to_mythic(formatted)

            if self.graph:
                try:
                    # Stream continuation with the configured graph recursion budget.
                    async for event in self.graph.astream(
                        self.state,
                        self._graph_run_config(thread_id)
                    ):
                        if self._stop_requested:
                            logger.info("🛑 Stop requested — terminating graph execution (continue branch)")
                            break
                        await self._process_stream_event(event)

                        # Update state with new values from event (extend for lists, assign for scalars)
                        for node_name, state_update in event.items():
                            if node_name in ["__start__", "__end__"]:
                                continue
                            for ch in ["supervisor_messages", "generalist_messages", "mythic_operator_messages",
                                      "mythic_payload_messages", "mcp_manager_messages", "bloodhound_messages", "_message_seq"]:
                                if ch in state_update:
                                    if ch == "_message_seq":
                                        self._message_seq = state_update[ch]
                                        self.state[ch] = state_update[ch]
                                    else:
                                        if ch not in self.state:
                                            self.state[ch] = []
                                        self.state[ch].extend(state_update[ch])
                            # Check for recursion flags
                            if "recursion_summary_requested" in state_update:
                                self.state["recursion_summary_requested"] = state_update["recursion_summary_requested"]
                            if "recursion_handback" in state_update:
                                self.state["recursion_handback"] = state_update["recursion_handback"]

                        if await self._maybe_stream_objective_completion_stop():
                            break

                    # Check if recursion summary was requested during streaming
                    if self.state.get("recursion_summary_requested", False):
                        return ""  # All output already streamed to Mythic

                except GraphRecursionError as e:
                    # Hit recursion limit again even with increased limit
                    logger.warning(f"Recursion limit hit again: {e}")

                    # Restore from checkpoint (same reason as first recursion hit)
                    logger.info(f"Restoring from checkpoint after second recursion limit:")
                    try:
                        async for checkpoint_tuple in self.memory.alist(config, limit=1):
                            if checkpoint_tuple and checkpoint_tuple.checkpoint:
                                nested_state = checkpoint_tuple.checkpoint.get("channel_values", {})
                                for channel_name in ["messages", "supervisor_messages", "generalist_messages",
                                                    "mythic_operator_messages", "mythic_payload_messages",
                                                    "mcp_manager_messages", "bloodhound_messages", "_message_seq"]:
                                    if channel_name in nested_state:
                                        self.state[channel_name] = nested_state[channel_name]
                                        if channel_name != "_message_seq":
                                            logger.info(f"  Restored {channel_name}: {len(nested_state[channel_name])} messages")
                                if "_message_seq" in nested_state:
                                    self._message_seq = nested_state["_message_seq"]
                    except Exception as checkpoint_error:
                        logger.warning(f"Could not retrieve checkpoint: {checkpoint_error}")

                    # Generate per-agent summaries
                    agent_summaries = await self._generate_per_agent_summaries()
                    summary_text = agent_summaries if agent_summaries else "Work in progress."

                    # Generate summary again
                    continuation_message = AIMessage(content=f"""🔄 **Recursion Limit Reached Again**

**Progress by Agent:**
{summary_text}

**Status:** Hit the increased iteration limit. The task appears to be very complex or open-ended.

**Your Options:**
• Reply **"continue"** to try again with an even higher limit
• Reply **"stop"** to end and review what's been done
• Provide more specific instructions to narrow the scope

**What would you like to do?**""")

                    # Add to BOTH messages AND supervisor_messages so Supervisor can see it
                    continuation_message_seq = self._next_seq()
                    _tag_msg(continuation_message, continuation_message_seq)
                    self.state["messages"].append(continuation_message)
                    self.state["supervisor_messages"].append(continuation_message)
                    self.state["recursion_summary_requested"] = True

                    # Stream the continuation message to Mythic
                    formatted_continuation = self._format_message_for_streaming(continuation_message, agent_name="System")
                    if formatted_continuation:
                        await self._stream_message_to_mythic(formatted_continuation)

                    logger.info(f"Recursion limit hit again, streaming continuation prompt")
                    return ""  # All output already streamed to Mythic
            else:
                raise ValueError("No graph defined for the model.")

        elif intent == "STOP":
            # User wants to stop (or inhibit further tasking — e.g. "don't run anything, just summarize")
            logger.info("User requested to stop the task")
            stop_message = AIMessage(content="✅ Task stopped as requested. The session remains active for new tasks.")
            self.state["messages"].append(stop_message)

            # Stream the stop confirmation
            formatted = self._format_message_for_streaming(stop_message, agent_name="System")
            if formatted:
                await self._stream_message_to_mythic(formatted)

            return ""  # All output already streamed

        else:
            # User provided new instructions or redirection
            logger.info("User provided new instructions for continuation")
            try:
                if self.mythic_client is not None:
                    self.mythic_client.begin_operator_turn(response)
            except Exception:
                pass

            # Add user's custom instruction to supervisor channel
            redirect_msg = HumanMessage(content=response)
            redirect_msg_seq = self._next_seq()
            _tag_msg(redirect_msg, redirect_msg_seq)
            self.state["supervisor_messages"].append(redirect_msg)
            self.state["messages"].append(redirect_msg)

            # Stream the custom instruction (always show since it's explicit redirection)
            formatted = self._format_message_for_streaming(redirect_msg, agent_name=None)
            if formatted:
                await self._stream_message_to_mythic(formatted)

            if self.graph:
                try:
                    # Stream new task direction with the configured graph recursion budget.
                    async for event in self.graph.astream(
                        self.state,
                        self._graph_run_config(thread_id)
                    ):
                        if self._stop_requested:
                            logger.info("🛑 Stop requested — terminating graph execution (redirect branch)")
                            break
                        await self._process_stream_event(event)

                        # Update state with new values from event (extend for lists, assign for scalars)
                        for node_name, state_update in event.items():
                            if node_name in ["__start__", "__end__"]:
                                continue
                            for ch in ["supervisor_messages", "generalist_messages", "mythic_operator_messages",
                                      "mythic_payload_messages", "mcp_manager_messages", "bloodhound_messages", "_message_seq"]:
                                if ch in state_update:
                                    if ch == "_message_seq":
                                        self._message_seq = state_update[ch]
                                        self.state[ch] = state_update[ch]
                                    else:
                                        if ch not in self.state:
                                            self.state[ch] = []
                                        self.state[ch].extend(state_update[ch])
                            # Check for recursion flags
                            if "recursion_summary_requested" in state_update:
                                self.state["recursion_summary_requested"] = state_update["recursion_summary_requested"]
                            if "recursion_handback" in state_update:
                                self.state["recursion_handback"] = state_update["recursion_handback"]

                        if await self._maybe_stream_objective_completion_stop():
                            break

                    # Check if recursion summary was requested during streaming
                    if self.state.get("recursion_summary_requested", False):
                        return ""  # All output already streamed to Mythic

                except GraphRecursionError as e:
                    # Handle recursion error for new direction too
                    logger.warning(f"Recursion limit hit on redirect: {e}")

                    # Restore from checkpoint (same reason as first recursion hit)
                    logger.info(f"Restoring from checkpoint after redirect recursion limit:")
                    try:
                        async for checkpoint_tuple in self.memory.alist(config, limit=1):
                            if checkpoint_tuple and checkpoint_tuple.checkpoint:
                                nested_state = checkpoint_tuple.checkpoint.get("channel_values", {})
                                for channel_name in ["messages", "supervisor_messages", "generalist_messages",
                                                    "mythic_operator_messages", "mythic_payload_messages",
                                                    "mcp_manager_messages", "bloodhound_messages", "_message_seq"]:
                                    if channel_name in nested_state:
                                        self.state[channel_name] = nested_state[channel_name]
                                        if channel_name != "_message_seq":
                                            logger.info(f"  Restored {channel_name}: {len(nested_state[channel_name])} messages")
                                if "_message_seq" in nested_state:
                                    self._message_seq = nested_state["_message_seq"]
                    except Exception as checkpoint_error:
                        logger.warning(f"Could not retrieve checkpoint: {checkpoint_error}")

                    # Generate per-agent summaries
                    agent_summaries = await self._generate_per_agent_summaries()
                    summary_text = agent_summaries if agent_summaries else "Work in progress."

                    continuation_message = AIMessage(content=f"""🔄 **Recursion Limit Reached (Redirect)**

**Progress by Agent:**
{summary_text}

**Status:** Hit the iteration limit while pursuing the redirected task.

**Your Options:**
• Reply **"continue"** to keep going with an increased limit
• Reply **"stop"** to end and review progress
• Provide more specific instructions

**What would you like to do?**""")

                    # Add to BOTH messages AND supervisor_messages so Supervisor can see it
                    continuation_message_seq = self._next_seq()
                    _tag_msg(continuation_message, continuation_message_seq)
                    self.state["messages"].append(continuation_message)
                    self.state["supervisor_messages"].append(continuation_message)
                    self.state["recursion_summary_requested"] = True

                    # Stream the continuation message to Mythic
                    formatted_continuation = self._format_message_for_streaming(continuation_message, agent_name="System")
                    if formatted_continuation:
                        await self._stream_message_to_mythic(formatted_continuation)

                    logger.info(f"Recursion limit hit again, streaming continuation prompt")
                    return ""  # All output already streamed to Mythic
            else:
                raise ValueError("No graph defined for the model.")

        return ""  # All output already streamed to Mythic

def _create_summarize_handback_tool():
    """
    Create a tool that allows specialist agents to hand back control to the Supervisor
    when approaching recursion limits, with a progress summary.
    """
    @tool("summarize_and_handback")
    def summarize_and_handback(
        runtime: ToolRuntime,
        progress_summary: Annotated[str, "Summary of work completed so far"],
        tasks_remaining: Annotated[str, "Description of tasks that still need to be completed"],
        key_findings: Annotated[str, "Important findings or results discovered"] = "",
    ) -> Command:
        """Hand back control to Supervisor when approaching recursion limit with progress summary."""

        handback_message = f"""🔄 **Approaching Recursion Limit - Progress Handback**

**Work Completed:**
{progress_summary}

**Key Findings:**
{key_findings if key_findings else "No specific findings to report yet."}

**Remaining Tasks:**
{tasks_remaining}

**Status:** Handing back to Supervisor to avoid hitting recursion limit and allow user to decide how to proceed."""

        tool_message = ToolMessage(
            content=handback_message,
            name="summarize_and_handback",
            tool_call_id=runtime.tool_call_id,
        )

        # Mark that we've had a recursion handback from a specialist
        updated_state = {**runtime.state, "recursion_handback": True}

        # CRITICAL FIX: Copy handback summary to supervisor_messages so Supervisor
        # knows where the worker left off when user says "continue"
        # With operator.add reducer, only provide the NEW message to append
        updated_state["supervisor_messages"] = [tool_message]

        # Also update global messages for legacy compatibility (only new message)
        updated_state["messages"] = [tool_message]

        return Command(
            goto="__end__",              # stop graph, wait for user
            update=updated_state,
            graph=Command.PARENT,
        )

    return summarize_and_handback


def _build_esl_summary(mythic_client) -> str:
    if mythic_client is None:
        return ""
    hops = list(getattr(mythic_client, "_engagement_hops", []) or [])
    achieved = [
        hop for hop in hops
        if str(getattr(hop, "status", "")).casefold() == "achieved"
    ]
    if not achieved:
        return ""

    lines = ["📊 **Engagement State Ledger (ESL)**"]
    for hop in achieved[-8:]:
        technique = str(getattr(hop, "technique", "") or "unknown")
        effect = str(getattr(hop, "effect", "") or getattr(hop, "target", "") or "")
        status = str(getattr(hop, "status", "") or "")
        evidence = getattr(hop, "evidence", {}) or {}
        preview = ""
        if isinstance(evidence, dict):
            preview = str(evidence.get("result_preview") or evidence.get("task_id") or "")[:80]
        detail = f"{technique} -> {effect} ({status})"
        if preview:
            detail += f": {preview}"
        lines.append(detail)
    cached_footholds = getattr(mythic_client, "_engagement_footholds", None)
    if cached_footholds is not None:
        lines.append(f"live footholds observed: {len(cached_footholds)}")
    return "\n".join(lines[:12])


def _deterministic_post_ingest_owner(mythic_client) -> _HandoffDirective | None:
    """Route verified post-ingest work without asking the Supervisor to classify prose."""
    if mythic_client is None:
        return None
    try:
        try:
            from . import capabilities
            from . import engagement_state as _es
        except ImportError:
            import capabilities
            import engagement_state as _es
        engagement_id, runtime_scope = _runtime_engagement_scope(mythic_client)
        snapshot = _es.EngagementState(
            objective=mythic_client._engagement_objective(),
            footholds=list(getattr(mythic_client, "_engagement_footholds", []) or []),
            hops=list(getattr(mythic_client, "_engagement_hops", []) or []),
            graph_facts=list(getattr(mythic_client, "_engagement_graph_facts", []) or []),
            engagement_id=engagement_id,
            runtime_scope=runtime_scope,
        )
        if capabilities.actions_from_state(snapshot):
            return None
        phase = str(_es.engagement_phase(snapshot))
        if phase.startswith("COMPLETE-CANDIDATE") or _es.current_access_collection_missing(snapshot):
            return None
        verified_keys = []
        for foothold in snapshot.footholds:
            if not _es._is_live_target_foothold(foothold):
                continue
            key = _es.access_context_key(snapshot, foothold)
            if key and _es.graph_collection_covers_foothold(snapshot, foothold):
                verified_keys.append(key)
        if not verified_keys:
            return None
        objective = str(snapshot.objective or "the engagement objective").strip()
        instruction = (
            "AUTONOMOUS POST-INGEST ROUTER: The authoritative engagement ledger proves a verified "
            "BloodHound collection for the current access context, no grounded executable capability is "
            "available, and the objective is not complete. Analyze the existing graph now; do not route back "
            "to Mythic_Operator for callback reconnaissance, collection confirmation, ZIP handling, download, "
            f"or ingest. Objective: {objective}. Return the next concrete graph-supported hop and its required "
            "Mythic capability, or a specific graph-coverage blocker."
        )
        return _handoff_directive("BloodHound", instruction, "Analyze verified graph")
    except Exception:
        return None


def _create_handback_to_supervisor_tool(mythic_client=None, *, autonomous: bool = False):
    """
    Let a specialist yield control to the Supervisor WITHOUT ending the run, so the Supervisor can route
    to another agent (BloodHound for graph work, Mythic_Payload for a build) or finalize. This is the
    explicit autonomous handback path: with the keep-going continue-loop a plain turn-end no longer reaches
    the Supervisor, so the Operator MUST call this to hand off across agents. Distinct from
    summarize_and_handback (which ends the run and waits for the user at the recursion limit).
    """
    @tool("handback_to_supervisor")
    def handback_to_supervisor(
        runtime: ToolRuntime,
        reason: Annotated[str, "Why you are handing back NOW: the capability/agent needed next (e.g. 'BloodHound ingestion -> route to BloodHound', 'payload build -> Mythic_Payload') or that the objective is complete."],
        summary: Annotated[str, "Structured DONE / FAILED / BLOCKER / REMAINING summary with concrete values (hashes, SIDs, file UUIDs, exact errors)."],
    ) -> Command:
        """Yield control to the Supervisor WITHOUT ending the run so it can route to another agent
        (BloodHound for graph work, Mythic_Payload for builds) or finalize the objective.
        Call this the moment the NEXT step needs a capability you do not own, or the objective is reached.
        Plain completion = keep going; summarize_and_handback = pause for the user at the recursion limit only."""
        msg = ToolMessage(
            content=f"🔄 **Handback to Supervisor** — {reason}\n\n{summary}",
            name="handback_to_supervisor",
            tool_call_id=runtime.tool_call_id,
        )
        post_ingest_route = (
            _deterministic_post_ingest_owner(mythic_client)
            if autonomous else None
        )
        if post_ingest_route is not None:
            owner, instruction = post_ingest_route
            delegated = HumanMessage(
                content=instruction,
                additional_kwargs={
                    "_delegated_to": owner,
                    "_handoff_title": post_ingest_route.title,
                },
            )
            return Command(
                goto=owner,
                update={
                    "messages": [msg, delegated],
                    "supervisor_messages": [msg],
                    "bloodhound_messages": [msg, delegated],
                    "next_owner": owner,
                    "_last_calling_agent": "Mythic_Operator",
                    "_last_target_agent": owner,
                },
                graph=Command.PARENT,
            )
        updated_state = {**runtime.state}
        updated_state["supervisor_messages"] = [msg]
        updated_state["messages"] = [msg]
        updated_state["next_owner"] = "Supervisor"
        updated_state["_last_calling_agent"] = "Mythic_Operator"
        return Command(
            goto="Supervisor",
            update=updated_state,
            graph=Command.PARENT,
        )

    return handback_to_supervisor


def _create_respond_to_user_tool():
    """
    Create a tool that allows the Supervisor to explicitly indicate it's done
    delegating and wants to deliver the user-facing final report: a complete,
    well-formatted markdown synthesis of concrete specialist findings (names,
    values, IPs, paths, counts), not raw JSON and not a thin acknowledgment.
    """
    @tool("respond_to_user")
    def respond_to_user(
        runtime: ToolRuntime,
        final_response: Annotated[str, "The complete, user-facing final report: a well-formatted markdown synthesis of the concrete findings discovered by the specialists (names, values, IPs, paths, counts), formatted for a human operator, not raw JSON, not a thin \"task complete\"."],
    ) -> Command:
        """Call this when the task is complete and you want to deliver the user-facing final synthesized report. The final_response must be complete, well-formatted markdown containing the concrete findings from specialists (names, values, IPs, paths, counts), not raw JSON and not a thin acknowledgment. DO NOT delegate again after calling this."""

        # Create a final AI message with the response
        response_message = AIMessage(
            content=final_response,
            name="Supervisor",
            additional_kwargs={"_is_final_report": True},
        )

        update_state = {**runtime.state}
        update_state["messages"] = [response_message]
        update_state["supervisor_messages"] = [response_message]

        return Command(
            goto="__end__",  # Explicitly end the graph
            update=update_state,
            graph=Command.PARENT,
        )

    return respond_to_user

def _create_recursion_summary_tool():
    """
    Create a tool that allows the supervisor to handle recursion limit situations
    by asking the user if they want to continue.
    """
    @tool("request_continuation")
    def request_continuation(
        runtime: ToolRuntime,
        summary: Annotated[str, "Summary of progress made so far"],
    ) -> Command:
        """Request user input on whether to continue when approaching recursion limit."""

        continuation_message = f"""🔄 **Recursion Limit Approaching**

**Progress Summary:**
{summary}

**Status:** We've been working through a complex task and are approaching the system's iteration limit.

**Your Options:**
• Reply **"continue"** to increase the limit and keep going with the current approach
• Reply **"stop"** to end the current task
• Reply **"redirect"** to give new specific instructions
• Ask a specific question to help focus the next steps

**What would you like to do?**"""

        tool_message = ToolMessage(
            content=continuation_message,
            name="request_continuation",
            tool_call_id=runtime.tool_call_id,
        )

        # Mark that we've requested a recursion summary - this will help detect the response
        update_state = {**runtime.state}
        # With operator.add, only provide NEW messages to append
        update_state["messages"] = [tool_message]
        update_state["supervisor_messages"] = [tool_message]
        update_state["recursion_summary_requested"] = True

        return Command(
            goto="__end__",  # End the graph execution and wait for user input
            update=update_state,
            graph=Command.PARENT,
        )

    return request_continuation

def _create_handoff_tool(
    *,
    agent_name: str,
    description: str | None = None,
    autonomous_redirect: Callable[[str, str, dict], _HandoffDirective | tuple[str, str] | dict[str, str] | None] | None = None,
):
    """
    Create a handoff tool to transfer control to another agent.
    https://docs.langchain.com/oss/python/langchain/multi-agent#handoffs
    https://langchain-ai.github.io/langgraph/how-tos/multi_agent/#handoffs

    :param agent_name: The name of the agent to transfer control to.
    :param description: An optional description for the tool.
    :return: A tool function that performs the handoff.
    """
    name = f"transfer_to_{agent_name}"
    description = description or (
        f"Delegate a task to {agent_name}. Provide `handoff_title` for the short operator-facing card label "
        "and `handoff_instruction` for the complete worker task."
    )

    channel_map = {
        "Supervisor": "supervisor_messages",
        "Generalist": "generalist_messages",
        "Mythic_Operator": "mythic_operator_messages",
        "Mythic_Payload": "mythic_payload_messages",
        "BloodHound": "bloodhound_messages",
        "MCP_Manager": "mcp_manager_messages",
        "Autonomous_Executor": "autonomous_executor_messages",
    }
    target_channel_key = channel_map.get(agent_name)

    @tool(name, description=description)
    def handoff_tool(
        runtime: ToolRuntime,
        handoff_instruction: Annotated[str, "The complete, self-contained instruction for the target agent: a full sentence stating exactly what to do, with NO pronouns and NO references to 'it'/'that'/'the previous task'. Example: 'List all active Mythic callbacks and report each host, user, and integrity level.'"],
        handoff_title: Annotated[str, "A short operator-facing title for the sub-agent card, usually 3-8 words and never the full instruction. Example: 'List active callbacks'."] = "",
    ) -> Command:
        requested = _handoff_directive(agent_name, handoff_instruction, handoff_title)
        redirect = None
        if autonomous_redirect is not None:
            try:
                redirect = autonomous_redirect(agent_name, handoff_instruction, runtime.state)
            except Exception:
                redirect = None
        if redirect is None:
            redirect = _autonomous_handoff_redirect(agent_name, handoff_instruction, runtime.state)
        directive = _coerce_handoff_directive(
            redirect,
            fallback_agent_name=requested.agent_name,
            fallback_instruction=requested.instruction,
            fallback_title=requested.title,
        )
        terminal_redirect = directive.agent_name == "__terminal__"
        actual_agent_name = "Supervisor" if terminal_redirect else directive.agent_name
        actual_instruction = directive.instruction
        actual_title = directive.title
        actual_target_channel_key = channel_map.get(actual_agent_name)

        # Compute sequence from max of existing messages in all channels
        # This is more reliable than state._message_seq which may not persist across checkpoints
        max_seq = 0
        for ch_key in ["supervisor_messages", "generalist_messages", "mythic_operator_messages", "mythic_payload_messages", "mcp_manager_messages", "bloodhound_messages", "autonomous_executor_messages", "messages"]:
            for msg in runtime.state.get(ch_key, []):
                seq = _get_seq(msg)
                if seq > max_seq:
                    max_seq = seq
        current_seq = max_seq + 1
        logger.debug(f"🔄 Handoff to {agent_name}: computed next seq={current_seq} (max in channels was {max_seq})")

        # ToolMessage confirming delegation
        if terminal_redirect:
            ack_prefix = "Terminal autonomous report"
        else:
            ack_prefix = f"Redirected to {actual_agent_name}" if redirect else f"Delegated to {actual_agent_name}"
        acknowledgment = ToolMessage(
            content=f"{ack_prefix} with instruction: {actual_instruction}",
            name=name,
            tool_call_id=runtime.tool_call_id,
        )
        _tag_msg(acknowledgment, current_seq)
        current_seq += 1

        # HumanMessage representing the actual task for the target agent
        # Mark as delegated so it displays differently from real user input
        injected_human = HumanMessage(content=actual_instruction)
        injected_human.additional_kwargs["_delegated_to"] = actual_agent_name
        injected_human.additional_kwargs["_handoff_title"] = actual_title
        _tag_msg(injected_human, current_seq)
        current_seq += 1

        # With operator.add reducers, provide only NEW messages. Returning a full state copy here
        # re-appends old channel contents on every handoff and can trap autonomous runs in loops.
        update_state = {
            "messages": [acknowledgment, injected_human],
            "_message_seq": current_seq,
        }
        if terminal_redirect:
            update_state["recursion_handback"] = True

        # Inject into target channel (only new messages with operator.add)
        if actual_target_channel_key:
            update_state[actual_target_channel_key] = [acknowledgment, injected_human]

        # CRITICAL: Track who is calling this agent so responses can be copied back
        # Store the calling agent's name in state for response routing
        # We need to detect the current agent from the message history
        current_agent = None
        for channel_name, channel_key in channel_map.items():
            if runtime.state.get(channel_key) and len(runtime.state.get(channel_key, [])) > 0:
                # Check if this channel has recent activity (last message is not too old)
                # This is a heuristic - the agent that just called a tool is the calling agent
                if channel_key in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages", "generalist_messages", "mcp_manager_messages", "bloodhound_messages", "autonomous_executor_messages"]:
                    # Simple approach: assume the tool was called from whichever non-target channel exists
                    if channel_name != actual_agent_name:
                        current_agent = channel_name
                        break

        # Store calling agent info for response routing
        update_state["_last_calling_agent"] = current_agent
        update_state["_last_target_agent"] = actual_agent_name

        return Command(
            goto=actual_agent_name,
            update=update_state,
            graph=Command.PARENT,
        )

    return handoff_tool


def _autonomous_capability_handoff_title(action: Any) -> str:
    payload = _capability_action_payload(action)
    name = str(payload.get("name") or "capability").strip()
    target = str(payload.get("target") or "").strip()
    title = f"Execute {name}"
    if target:
        title += f" on {target}"
    return _normalize_handoff_title(title, title)


def _compiled_autonomous_capability_instruction(
    action: Any,
    engagement_snapshot: Any,
    *,
    handoff_instruction: str,
    requested_agent: str,
) -> str:
    action_payload = _capability_action_payload(action)
    inputs_payload = _autonomous_capability_inputs(action, engagement_snapshot)
    original = _message_content_as_text(handoff_instruction).strip()
    if len(original) > 600:
        original = original[:597].rstrip() + "..."
    action_json = json.dumps(action_payload, sort_keys=True)
    inputs_json = json.dumps(inputs_payload, sort_keys=True)
    lines = [
        "AUTONOMOUS STEP DRIVER: The engagement ledger and cached BloodHound facts were re-evaluated "
        "at the handoff boundary. The selected capability below supersedes the delegated prose from "
        f"{requested_agent}.",
        "Call `execute_capability` now with these arguments:",
        f"`action={action_json}`",
        f"`inputs={inputs_json}`",
        "Do not re-plan from task history, do not repeat achieved hops, and do not call BloodHound before "
        "this capability. After `execute_capability` returns a terminal JSON result, allow Sage to return "
        "to Supervisor/state reconciliation so the next step is recomputed from the updated ledger.",
    ]
    if original:
        lines.append(f"Superseded handoff text: {original}")
    return "\n".join(lines)


def _compiled_autonomous_collection_instruction(
    engagement_snapshot: Any,
    *,
    handoff_instruction: str,
    requested_agent: str,
) -> str:
    foothold = _autonomous_collection_foothold(engagement_snapshot)
    if foothold is None:
        return ""
    callback_id = str(getattr(foothold, "callback_id", "") or "").strip()
    if not callback_id:
        return ""
    try:
        try:
            from . import engagement_state as _es
        except ImportError:
            import engagement_state as _es
        access_key = _es.access_context_key(engagement_snapshot, foothold)
    except Exception:
        access_key = ""
    original = _message_content_as_text(handoff_instruction).strip()
    if len(original) > 600:
        original = original[:597].rstrip() + "..."
    host = str(getattr(foothold, "host", "") or "").strip() or "the live foothold"
    identity = str(getattr(foothold, "identity", "") or "").strip() or "the current user"
    lines = [
        "AUTONOMOUS COLLECTION DRIVER: The engagement ledger was re-evaluated at the handoff boundary. "
        f"No executable capability is currently available, and the current access context on callback {callback_id} "
        "has no verified BloodHound collection. This supersedes the delegated prose from "
        f"{requested_agent}.",
        f"Target callback: {callback_id} ({host} as {identity}).",
    ]
    if access_key:
        lines.append(f"Required access-context key: `{access_key}`.")
    lines.extend([
        "Run exactly one NEW SharpHound collection task on that callback now. Do not use task history, task 5, "
        "task 7, any previous file UUID, or any previous ZIP to satisfy this collection requirement.",
        "Use Apollo `execute_assembly` with registered `SharpHound.exe` and arguments "
        "`-c All --SearchForest --CollectAllProperties --OutputDirectory C:\\Users\\Public`.",
        "After the collection task completes, list `C:\\Users\\Public`, download the BloodHound ZIP produced by "
        "this new collection task, then call `ingest_collection` with `callback_display_id="
        f"{callback_id}` and `name_contains=\"zip\"` (or the exact new file UUID plus callback_display_id="
        f"{callback_id}).",
        "Do not run GPO abuse, DCSync, RBCD, ticket forging, or any other attack action during this collection "
        "driver. After `ingest_collection` returns `graph_verified=true`, hand back so Sage can recompute the "
        "next step from the updated ledger.",
    ])
    if original:
        lines.append(f"Superseded handoff text: {original}")
    return "\n".join(lines)


def _compiled_autonomous_blocked_bloodhound_instruction(
    engagement_snapshot: Any,
    *,
    handoff_instruction: str,
    requested_agent: str,
) -> str:
    original = _message_content_as_text(handoff_instruction).strip()
    if len(original) > 600:
        original = original[:597].rstrip() + "..."
    phase = _engagement_phase_text(engagement_snapshot)
    lines = [
        "AUTONOMOUS BLOCKED-STATE GRAPH ANALYSIS: The engagement ledger was re-evaluated at the handoff "
        f"boundary and no Mythic-executable hop is modeled. This supersedes the delegated prose from "
        f"{requested_agent}.",
        f"Current phase: {phase or 'BLOCKED'}.",
        "Analyze BloodHound graph coverage and path facts from the proven current control state. Do not ask "
        "Mythic_Operator to rerun GPO abuse, DCSync, RBCD, ticket forging, collection, or any achieved hop.",
        "Return exactly one of: a concrete graph-supported next capability/hop with target object and "
        "preconditions, or a blocker stating which required target-side objects/edges are absent from the graph.",
    ]
    _append_control_milestones(lines, engagement_snapshot)
    if original:
        lines.append(f"Superseded handoff text: {original}")
    return "\n".join(lines)


def _action_signature(action) -> tuple:
    """A stable identity for a selected capability action (handles dict or object), used by the stall detector
    to tell "re-selecting the same dead hop" from "advancing through distinct hops". Never raises."""
    try:
        if isinstance(action, dict):
            return (action.get("name"), action.get("target"), action.get("effect"))
        return (getattr(action, "name", None), getattr(action, "target", None), getattr(action, "effect", None))
    except Exception:
        return (None, None, None)


def _autonomous_stall_report(snapshot) -> str:
    """Terminal report when the autonomous solve stalls (no ledger progress for _AUTONOMOUS_STALL_LIMIT steps).
    Self-contained string surfaced to the operator via the __terminal__ handback. Never raises."""
    try:
        objective = str(getattr(snapshot, "objective", "") or "")
        achieved = sorted(snapshot.achieved_effects()) if snapshot is not None else []
    except Exception:
        objective, achieved = "", []
    return (
        f"AUTONOMOUS SOLVE HALTED — no new objective progress in {_AUTONOMOUS_STALL_LIMIT} consecutive "
        f"capability steps: the selected hop is not advancing the engagement ledger (likely an unmet "
        f"precondition or a repeatedly-failing execution). Stopping to avoid a token-burning loop.\n"
        f"Objective: {objective}\n"
        f"Achieved so far: {achieved}\n"
        f"Operator: inspect why the next capability is not progressing, then resume manually."
    )


def _compiled_autonomous_blocked_report(
    engagement_snapshot: Any,
    *,
    handoff_instruction: str,
    requested_agent: str,
) -> str:
    original = _message_content_as_text(handoff_instruction).strip()
    if len(original) > 600:
        original = original[:597].rstrip() + "..."
    phase = _engagement_phase_text(engagement_snapshot)
    objective = str(getattr(engagement_snapshot, "objective", "") or "").strip()
    lines = [
        "AUTONOMOUS BLOCKED REPORT: Sage is stopping this run because the authoritative engagement state has "
        "no modeled next hop after current-access collection and BloodHound analysis.",
    ]
    if objective:
        lines.append(f"Objective: {objective}")
    lines.append(f"Current phase: {phase or 'BLOCKED — no modeled hop available'}.")
    _append_control_milestones(lines, engagement_snapshot)
    lines.extend([
        "No reset is indicated by this state. The missing input is graph/capability coverage for the next "
        "sevenkingdoms.local -> essos.local hop, not a clean range.",
        "Do not repeat achieved NORTH or SEVENKINGDOMS GPO, golden-ticket, Kerberos-context, DCSync, or account "
        "harvesting steps. Continue only after adding the missing graph facts/capability for the ESSOS route or "
        "after obtaining a new access context.",
    ])
    if original:
        lines.append(f"Suppressed handoff text: {original}")
    return "\n".join(lines)


def _engagement_phase_text(engagement_snapshot: Any) -> str:
    try:
        try:
            from . import engagement_state as _es
        except ImportError:
            import engagement_state as _es
        return str(_es.engagement_phase(engagement_snapshot))
    except Exception:
        return ""


def _append_control_milestones(lines: list[str], engagement_snapshot: Any) -> None:
    try:
        try:
            from . import engagement_state as _es
        except ImportError:
            import engagement_state as _es
        candidates = _es.objective_completion_candidates(engagement_snapshot)
    except Exception:
        candidates = []
    if not candidates:
        return
    lines.append("Recorded administrative-control milestones:")
    for candidate in candidates[:6]:
        domain = str(candidate.get("domain") or "").strip()
        if not domain:
            continue
        pieces = [f"- {domain}"]
        for key in ("admin_effect", "access_effect", "key_effect", "auth_effect"):
            value = str(candidate.get(key) or "").strip()
            if value:
                pieces.append(f"{key.removesuffix('_effect')}={value}")
        lines.append(" | ".join(pieces))


def _recent_bloodhound_blocker_observed(state: dict) -> bool:
    text = _safe_lower(_recent_channel_text(state, ("bloodhound_messages", "supervisor_messages", "messages"), limit=18))
    if "blocker / missing capability" not in text:
        return False
    if "essos.local" not in text:
        return False
    return any(marker in text for marker in (
        "collected=false",
        "users: `0`",
        "groups: `0`",
        "computers: `0`",
        "no modeled hop",
        "domain admins@essos.local",
        "target-side",
    ))


def _autonomous_collection_foothold(engagement_snapshot: Any) -> Any | None:
    footholds = list(getattr(engagement_snapshot, "footholds", []) or [])
    for foothold in footholds:
        try:
            if not bool(getattr(foothold, "alive", False)):
                continue
            if str(getattr(foothold, "agent", "") or "").strip().casefold() == "sage":
                continue
            return foothold
        except Exception:
            continue
    return None


def _parse_compiled_autonomous_capability_instruction(text: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if "AUTONOMOUS STEP DRIVER" not in str(text or ""):
        return None, {}
    action = _json_backtick_payload(text, "action")
    inputs = _json_backtick_payload(text, "inputs")
    if not isinstance(action, dict):
        return None, {}
    if not isinstance(inputs, dict):
        inputs = {}
    return action, inputs


def _json_backtick_payload(text: str, label: str) -> Any:
    pattern = rf"`{re.escape(label)}=(.*?)`"
    match = re.search(pattern, str(text or ""), re.DOTALL)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        return json.loads(raw)
    except Exception:
        return None


def _collection_profile_text(config: dict[str, Any], key: str, default: str) -> str:
    if isinstance(config, dict) and key in config:
        return str(config.get(key) or "").strip()
    return default


def _find_token_zip_path(ls_output: str, token: str) -> str:
    """Find the real on-disk path of THIS collection's ZIP in a supported `ls` result. SharpHound prepends a
    timestamp to `--ZipFilename` (e.g. `<ts>_bloodhound_<token>.zip`), so the on-disk name is NOT predictable —
    we discover it instead. Anchored on the per-run random `token` so it cannot match any other file. Apollo
    streams one-or-more structured JSON objects; Merlin emits a tab-delimited native directory listing. Returns
    the exact full path or ""."""
    dec = json.JSONDecoder()
    s = ls_output or ""
    i, n = 0, len(s)
    tok = (token or "").lower()
    if not tok:
        return ""
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(s, i)
        except ValueError:
            i += 1
            continue
        i = end
        if not isinstance(obj, dict):
            continue
        for f in (obj.get("files") or []):
            if not isinstance(f, dict):
                continue
            name = str(f.get("name", "")).lower()
            if f.get("is_file") is True and tok in name and name.endswith(".zip"):
                full = str(f.get("full_name") or "").strip()
                if full:
                    return full
    directory_match = re.search(r"(?im)^\s*Directory listing for:\s*(.+?)\s*$", s)
    directory = directory_match.group(1).strip() if directory_match else ""
    if not directory:
        return ""
    for line in s.splitlines():
        fields = line.rstrip().split("\t")
        if len(fields) < 4:
            continue
        name = fields[-1].strip()
        lowered = name.casefold()
        if tok in lowered and lowered.endswith(".zip"):
            return ntpath.join(directory, name)
    return ""


def _capability_action_payload(action: Any) -> dict[str, Any]:
    return {
        "name": _jsonable_value(getattr(action, "name", "")),
        "target": _jsonable_value(getattr(action, "target", "")),
        "preconditions": _jsonable_value(list(getattr(action, "preconditions", []) or [])),
        "effects": _jsonable_value(list(getattr(action, "effects", []) or [])),
        "operational_cost": _jsonable_value(getattr(action, "operational_cost", {}) or {}),
        "intent": _jsonable_value(getattr(action, "intent", {}) or {}),
        "verifier": _jsonable_value(getattr(action, "verifier", {}) or {}),
        "reason": _jsonable_value(getattr(action, "reason", "") or ""),
        "source_facts": _jsonable_value(list(getattr(action, "source_facts", []) or [])),
    }


def _jsonable_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable_value(item) for item in value]
    return str(value)


_AUTONOMOUS_GPO_WAIT_ALIASES = (
    "wait_seconds",
    "gpo_wait_seconds",
    "gp_refresh_wait_seconds",
    "dc_refresh_wait_seconds",
    "delay_seconds",
)


def _autonomous_gpo_wait_seconds(action: Any = None) -> int:
    """Return the bounded GPO wait window used by both policy metadata and execution inputs."""
    override = _env_positive_int("SAGE_GPO_WAIT_SECONDS", 0)
    if override:
        return min(override, 600)
    intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
    for alias in _AUTONOMOUS_GPO_WAIT_ALIASES:
        value = intent.get(alias)
        if value is None or value == "":
            continue
        try:
            return max(0, min(int(value), 600))
        except (TypeError, ValueError):
            continue
    return 300


def _autonomous_policy_candidates(actions: list[Any]) -> list[Any]:
    try:
        from . import capabilities as _cap
    except ImportError:
        import capabilities as _cap
    return [
        _cap.with_operational_cost(action, gpo_wait_seconds=_autonomous_gpo_wait_seconds(action))
        for action in (actions or [])
    ]


def _eval_forced_capability_prefix_candidates(
    actions: list[Any],
    state: Any,
    *,
    raw_override: str | None = None,
) -> list[Any]:
    """Restrict an eval run to one declared capability prefix until it completes or releases."""
    raw = str(
        raw_override
        if raw_override is not None
        else os.environ.get("SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON") or ""
    ).strip()
    if not raw:
        return list(actions or [])
    try:
        decoded = json.loads(raw)
    except Exception:
        return list(actions or [])
    if not isinstance(decoded, list):
        return list(actions or [])

    def _name(value: Any) -> str:
        text = str(value or "").strip().casefold()
        return text.rsplit(":", 1)[-1] if ":" in text else text

    def _matches(value: Any, spec: dict[str, Any]) -> bool:
        if _name(getattr(value, "name", None) or getattr(value, "technique", "")) != _name(spec.get("capability")):
            return False
        exact_target = str(spec.get("exact_target") or spec.get("target") or "").strip()
        target = str(getattr(value, "target", "") or "").strip()
        if exact_target and target != exact_target:
            return False
        target_contains = str(spec.get("target_contains") or "").strip().casefold()
        target_cf = target.casefold()
        return not target_contains or target_contains in target_cf

    def _annotate_exact_intervention(value: Any, spec: dict[str, Any], index: int) -> Any:
        exact_target = str(spec.get("exact_target") or spec.get("target") or "").strip()
        if not exact_target:
            return value
        intent = getattr(value, "intent", {}) if isinstance(getattr(value, "intent", {}), dict) else {}
        intervention = {
            "forced": True,
            "intervention_id": str(spec.get("intervention_id") or f"forced-prefix-{index}"),
            "exact_target": exact_target,
            "credit_policy_win": False,
            "label_only": True,
        }
        try:
            return replace(value, intent={**dict(intent), "eval_intervention": intervention})
        except Exception:
            return value

    hops = list(getattr(state, "hops", []) or [])
    candidates = list(actions or [])
    for index, spec in enumerate(decoded):
        if not isinstance(spec, dict) or not _name(spec.get("capability")):
            continue
        if any(_matches(hop, spec) and str(getattr(hop, "status", "") or "").strip().casefold() == "achieved" for hop in hops):
            continue
        if spec.get("release_on_failure") is True and any(
            _matches(hop, spec)
            and str(getattr(hop, "status", "") or "").strip().casefold() in {"failed", "blocked"}
            for hop in hops
        ):
            continue
        matched = [action for action in candidates if _matches(action, spec)]
        if str(spec.get("exact_target") or spec.get("target") or "").strip() and len(matched) == 1:
            return [_annotate_exact_intervention(matched[0], spec, index)]
        return matched
    return candidates


def _eval_exact_target_intervention(value: Any) -> dict[str, Any]:
    """Return one valid exact-target eval intervention attached to a candidate, if present."""
    intent = getattr(value, "intent", {}) if isinstance(getattr(value, "intent", {}), dict) else {}
    intervention = intent.get("eval_intervention")
    intervention = intervention if isinstance(intervention, dict) else {}
    exact_target = str(intervention.get("exact_target") or "").strip()
    if (
        intervention.get("forced") is not True
        or intervention.get("credit_policy_win") is not False
        or not exact_target
        or exact_target != str(getattr(value, "target", "") or "").strip()
    ):
        return {}
    return intervention


def _eval_same_capability_action(left: Any, right: Any) -> bool:
    """Return whether two action objects name the same executable semantic action."""
    return (
        str(getattr(left, "name", "") or "") == str(getattr(right, "name", "") or "")
        and str(getattr(left, "target", "") or "") == str(getattr(right, "target", "") or "")
        and list(getattr(left, "preconditions", None) or []) == list(getattr(right, "preconditions", None) or [])
        and list(getattr(left, "effects", None) or []) == list(getattr(right, "effects", None) or [])
    )


def _eval_forced_capability_prefix_frontier(
    actions: list[Any],
    state: Any,
    *,
    raw_override: str | None = None,
) -> list[Any]:
    """Preserve the pre-intervention frontier for exact-target forced eval rows.

    Legacy prefix fixtures use `target_contains` to narrow the actual frontier and keep
    their historical behavior. Phase 6 exact-target rows need a different contract:
    the policy packet must still contain the real admissible frontier, while the
    executed action is a label-only forced intervention. The existing prefix helper
    already identifies and annotates that one exact action; this function only
    re-inserts the annotation into the original frontier before policy capture.
    """
    candidates = list(actions or [])
    filtered = _eval_forced_capability_prefix_candidates(
        candidates,
        state,
        raw_override=raw_override,
    )
    if len(filtered) != 1 or not _eval_exact_target_intervention(filtered[0]):
        return filtered
    forced = filtered[0]
    replaced = False
    frontier: list[Any] = []
    for candidate in candidates:
        if not replaced and _eval_same_capability_action(candidate, forced):
            frontier.append(forced)
            replaced = True
        else:
            frontier.append(candidate)
    return frontier if replaced else filtered


def _eval_forced_intervention_index(candidates: list[Any]) -> int | None:
    """Return the unique exact-target intervention index in one policy frontier."""
    matches = [
        index
        for index, candidate in enumerate(candidates or [])
        if _eval_exact_target_intervention(candidate)
    ]
    return matches[0] if len(matches) == 1 else None


def _eval_apply_forced_intervention_decision(decision: Any, candidates: list[Any]) -> Any:
    """Override only execution selection for an exact-target eval intervention.

    The delegated policy still builds the packet/hash from the full pre-intervention
    frontier. This replacement updates only the selected action lineage and marks it
    as non-creditable forced execution.
    """
    forced_index = _eval_forced_intervention_index(candidates)
    if decision is None or forced_index is None:
        return decision
    try:
        from . import policy as _policy
    except ImportError:
        import policy as _policy
    selected = candidates[forced_index]
    identity_fields = _policy._decision_identity_fields(
        mode=str(getattr(decision, "policy_mode", "") or ""),
        selection_contract=str(getattr(decision, "selection_contract", "") or ""),
        candidates=list(candidates or []),
        selected_index=forced_index,
        decision_owner="forced_intervention",
    )
    return replace(
        decision,
        disposition="select",
        selected_index=forced_index,
        selected_capability=str(getattr(selected, "name", "") or ""),
        selected_target=str(getattr(selected, "target", "") or ""),
        rationale="exact-target eval intervention overrides policy selection; no policy-win credit",
        candidate_count=len(candidates or []),
        selected_family=_policy.capability_family(getattr(selected, "name", "")),
        selected_is_first_admissible=forced_index == 0,
        **identity_fields,
    )


class _EvalForcedInterventionPolicy:
    """Eval-only policy wrapper that separates packet capture from forced execution."""

    def __init__(self, delegate: Any):
        self._delegate = delegate
        self.mode = str(getattr(delegate, "mode", "") or "")
        self.selection_contract = str(getattr(delegate, "selection_contract", "") or "")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def select(self, **kwargs: Any) -> Any:
        decision = await self._delegate.select(**kwargs)
        return _eval_apply_forced_intervention_decision(
            decision,
            list(kwargs.get("candidates") or []),
        )


def _autonomous_capability_inputs(action: Any, engagement_snapshot: Any) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    callback_id = _autonomous_callback_id_for_action(action, engagement_snapshot)
    if callback_id:
        inputs["callback_id"] = callback_id
    # Deterministic capability builders that make a durable domain change (e.g. gpo-controlled-system-exec
    # adding us to Domain Admins) need to know WHICH principal we control — the foothold identity. The old
    # LLM path supplied this implicitly; the controller must. Range-agnostic (the live foothold user); builders
    # that don't need it ignore it.
    identity = _autonomous_controlled_identity(engagement_snapshot, callback_id)
    if identity:
        inputs["controlled_principal"] = identity
        inputs["current_user"] = identity
    intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
    if (
        str(getattr(action, "name", "") or "").strip().casefold() == "gpo-controlled-system-exec"
        and str(intent.get("preferred_effect") or "").strip().casefold() == "system-exec-proof"
    ):
        inputs["allow_proof_only"] = True
    action_name = str(getattr(action, "name", "") or "").strip().casefold()
    if action_name in {"gpo-controlled-system-exec", "grant-directory-rights"}:
        inputs["gpo_wait_seconds"] = _autonomous_gpo_wait_seconds(action)
    return inputs


def _autonomous_controlled_identity(engagement_snapshot: Any, callback_id: str = "") -> str:
    """The identity (DOMAIN\\user or user@domain) of the live foothold we control — preferring the one on the
    action's callback, else any live foothold. Used to parameterize self-escalation capabilities."""
    footholds = list(getattr(engagement_snapshot, "footholds", []) or [])
    cb = str(callback_id or "").strip()
    if cb:
        for fh in footholds:
            if not _is_live_tradecraft_foothold(fh):
                continue
            if str(getattr(fh, "callback_id", "") or "").strip() == cb:
                ident = str(getattr(fh, "identity", "") or "").strip()
                if ident:
                    return ident
    for fh in footholds:
        if not _is_live_tradecraft_foothold(fh):
            continue
        ident = str(getattr(fh, "identity", "") or "").strip()
        if ident:
            return ident
    return ""


def _autonomous_callback_id_for_action(action: Any, engagement_snapshot: Any) -> str:
    target_fields = _target_fields_from_action(action)
    intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
    explicit = str(
        intent.get("callback_id")
        or intent.get("callback")
        or intent.get("callback_display_id")
        or target_fields.get("callback")
        or target_fields.get("callback_id")
        or ""
    ).strip()
    if explicit:
        return explicit.casefold().lstrip("#").removeprefix("cb")

    for value in _action_text_values(action):
        match = re.search(r"@callback:(\d+)\b", value, re.IGNORECASE)
        if match:
            return match.group(1)

    domain = str(
        intent.get("domain")
        or intent.get("source_domain")
        or target_fields.get("domain")
        or target_fields.get("source_domain")
        or ""
    ).strip().casefold()
    footholds = list(getattr(engagement_snapshot, "footholds", []) or [])
    live_callback_ids = {
        str(getattr(foothold, "callback_id", "") or "").strip().casefold().lstrip("#").removeprefix("cb")
        for foothold in footholds
        if _is_live_tradecraft_foothold(foothold)
    }
    achieved = set()
    try:
        achieved = set(engagement_snapshot.achieved_effects())
    except Exception:
        achieved = set()
    if domain:
        callback_id = _latest_live_kerberos_context_callback(
            domain,
            engagement_snapshot,
            live_callback_ids,
        )
        if callback_id:
            return callback_id
        prefix = f"kerberos-context:{domain}@callback:"
        for effect in sorted(achieved):
            text = str(effect or "").strip().casefold()
            if text.startswith(prefix):
                callback_id = text[len(prefix):].split(None, 1)[0].strip().lstrip("#").removeprefix("cb")
                if callback_id in live_callback_ids:
                    return callback_id

    if domain:
        for foothold in footholds:
            if not _is_live_tradecraft_foothold(foothold):
                continue
            forest = str(getattr(foothold, "forest", "") or "").strip().casefold()
            if forest == domain:
                callback = str(getattr(foothold, "callback_id", "") or "").strip()
                if callback:
                    return callback
    for foothold in footholds:
        if not _is_live_tradecraft_foothold(foothold):
            continue
        callback = str(getattr(foothold, "callback_id", "") or "").strip()
        if callback:
            return callback
    return ""


def _latest_live_kerberos_context_callback(
    domain: str,
    engagement_snapshot: Any,
    live_callback_ids: set[str],
) -> str:
    """Return the newest live callback whose latest context proof matches ``domain``."""
    domain = str(domain or "").strip().casefold()
    if not domain or not live_callback_ids:
        return ""
    seen_callbacks: set[str] = set()
    for hop in reversed(list(getattr(engagement_snapshot, "hops", []) or [])):
        if str(getattr(hop, "status", "") or "").strip().casefold() != "achieved":
            continue
        effects = list(getattr(hop, "satisfied_effects", []) or [])
        if not effects:
            effects = [getattr(hop, "effect", "")]
        for effect in effects:
            match = re.match(
                r"^kerberos-context:([^@\s]+)@callback:(\d+)$",
                str(effect or "").strip(),
                re.IGNORECASE,
            )
            if not match:
                continue
            callback_id = match.group(2).strip().casefold().lstrip("#").removeprefix("cb")
            if callback_id not in live_callback_ids or callback_id in seen_callbacks:
                continue
            seen_callbacks.add(callback_id)
            if match.group(1).strip().casefold() == domain:
                return callback_id
    return ""


def _target_fields_from_action(action: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        target = str(getattr(action, "target", "") or "")
        for part in target.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().casefold()
            value = value.strip()
            if key and value:
                fields[key] = value
    except Exception:
        return {}
    return fields


def _action_text_values(action: Any) -> list[str]:
    out: list[str] = []
    for name in ("target", "preconditions", "effects", "source_facts"):
        value = getattr(action, name, None)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple, set)):
            out.extend(str(item or "") for item in value)
    intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
    out.extend(str(item or "") for item in intent.values() if isinstance(item, (str, int, float)))
    return out


def _is_live_tradecraft_foothold(foothold: Any) -> bool:
    if getattr(foothold, "alive", False) is not True:
        return False
    agent = str(getattr(foothold, "agent", "") or "").strip().casefold()
    return agent != "sage"


def _autonomous_handoff_redirect(agent_name: str, handoff_instruction: str, state: dict) -> _HandoffDirective | None:
    """Deterministically block autonomous handoff regressions from observed progress.

    This runs before a delegated task reaches the target agent. It intentionally uses tool-result evidence from
    the graph state, not prompt text, so stale Supervisor narration cannot send the Operator backwards after a
    verified collection or terminal capability result.
    """
    if agent_name != "Mythic_Operator":
        return None
    instruction = _safe_lower(handoff_instruction)

    progress_redirect = _redirect_stale_handoff_after_capability_progress(instruction, state)
    if progress_redirect:
        return progress_redirect
    gpo_progress_redirect = _redirect_stale_gpo_handoff_from_observed_effects(instruction, state)
    if gpo_progress_redirect:
        return gpo_progress_redirect

    if _requests_collection_confirmation(instruction):
        facts = _observed_handoff_facts(state)
        if facts.get("graph_verified"):
            terminal = facts.get("terminal_capability") or {}
            if terminal and str(terminal.get("verdict") or "").casefold() in {"failed", "blocked", "partial"}:
                capability = str(terminal.get("capability") or "the latest capability").strip()
                verdict = str(terminal.get("verdict") or "failed").strip()
                reason = str(terminal.get("reason") or "no reason supplied").strip()
                tasks = _terminal_capability_task_summary(terminal)
                return _handoff_directive(
                    "Mythic_Operator",
                    "Do not perform SharpHound collection confirmation, ZIP discovery, download, or BloodHound "
                    "ingest for this access context. Tool-result evidence in the current run already shows "
                    f"`graph_verified=true`. Continue from the observed graph and recover from the latest "
                    f"terminal capability result: `{capability}` returned `{verdict}`"
                    f"{tasks}; reason: {reason}. Inspect the referenced task output if needed, repair and retry "
                    "that capability only when the error is recoverable, or replan from the verified graph. "
                    "Do not regress to collection work.",
                    f"Recover {capability}",
                )
            return _handoff_directive(
                "BloodHound",
                "Tool-result evidence in the current run already shows `graph_verified=true` for the current "
                "access context. Do not ask Mythic_Operator to confirm SharpHound completion, list ZIPs, "
                "download collections, or ingest again. Analyze the verified BloodHound graph and return the "
                "next concrete graph-supported hop plus the exact Mythic action needed next.",
                "Analyze verified graph",
            )

    if not (
        "starkwallpaper" in instruction
        and "gpo" in instruction
        and "domain admins" in instruction
        and ("sharpgpoabuse" in instruction or "net group" in instruction)
    ):
        return None
    recent = _recent_channel_text(state, ("supervisor_messages", "messages"), limit=8)
    low = _safe_lower(recent)
    if not (
        "handback to supervisor" in low
        and "done (do not repeat)" in low
        and "starkwallpaper" in low
        and "domain admins" in low
        and "graph-supported" in low
        and "bloodhound" in low
        and "essos" in low
    ):
        return None
    callback_id = _extract_callback_id(recent) or "the live CASTELBLACK callback"
    return _handoff_directive(
        "BloodHound",
        "Analyze the current BloodHound graph for the next concrete sevenkingdoms.local -> essos.local hop "
        f"from proven sevenkingdoms administrative control on callback {callback_id}. Do not repeat the "
        "STARKWALLPAPER/GPO hop, do not rerun sevenkingdoms krbtgt DCSync after the recorded 0x20f7/8439 "
        "failures, and return the exact next traversable principal/group/edge plus the Mythic action needed next.",
        "Analyze next graph hop",
    )


def _redirect_stale_gpo_handoff_from_observed_effects(instruction: str, state: dict) -> _HandoffDirective | None:
    """Advance stale GPO handoffs when tool-result effects prove the GPO chain is already past that hop."""
    if not _requests_gpo_domain_admin_add(instruction):
        return None
    effects = _observed_effects_from_execute_capability_results(state)
    if not effects:
        return None

    contexts = _kerberos_contexts_from_effects(effects)
    for domain in sorted(_domains_with_effect_prefix(effects, "krbtgt-hash:")):
        callback_id = contexts.get(domain) or _extract_callback_id(_recent_channel_text(
            state, ("mythic_operator_messages", "supervisor_messages", "messages"), limit=12
        )) or "the live callback"
        parent = _parent_domain(domain)
        if parent:
            return _handoff_directive(
                "Mythic_Operator",
                f"Observed execute_capability results already prove the STARKWALLPAPER/GPO chain is past the "
                f"GPO hop: `krbtgt-hash:{domain}` is achieved. Do not repeat GPO abuse, Domain Admins "
                f"membership polling, Kerberos PAC refresh, or NORTH DCSync. Execute the next capability now: "
                f"call `build_capability_commands` for `forge-golden-ticket` with `domain={domain}`, "
                f"`target_domain={parent}`, and callback {callback_id}; then issue the returned structured "
                "commands exactly without editing SID/key/domain fields. Verify administrative control over "
                f"`{parent}` before any ESSOS trust hop.",
                "Forge golden ticket",
            )
        return _handoff_directive(
            "Mythic_Operator",
            f"Observed execute_capability results already prove `krbtgt-hash:{domain}` is achieved. Do not "
            "repeat GPO abuse or membership/PAC checks. Replan from the achieved krbtgt hash and execute the "
            "next non-GPO capability toward the objective.",
            "Advance from krbtgt hash",
        )

    for domain, callback_id in sorted(contexts.items()):
        netbios = _netbios_from_domain(domain)
        user = f"{netbios}\\krbtgt" if netbios else f"{domain}\\krbtgt"
        return _handoff_directive(
            "Mythic_Operator",
            f"Observed execute_capability results already prove `kerberos-context:{domain}@callback:{callback_id}`. "
            "Do not repeat STARKWALLPAPER/GPO abuse, Domain Admins membership polling, or PAC refresh. Execute "
            f"NORTH DCSync now from callback {callback_id}: DCSync `{user}` against `{domain}` and record "
            f"`krbtgt-hash:{domain}` from real secret material.",
            f"DCSync {domain}",
        )

    for domain in sorted(_domains_with_effect_prefix(effects, "da:")):
        return _handoff_directive(
            "Mythic_Operator",
            f"Observed execute_capability results already prove `da:{domain}`. Do not repeat STARKWALLPAPER/GPO "
            "abuse or Domain Admins membership polling. Execute `ensure-kerberos-context` for that domain on the "
            "live callback, then proceed to DCSync only after the context effect is recorded.",
            "Refresh Kerberos context",
        )

    system_exec = sorted(_gpo_system_exec_effects(effects))
    if system_exec:
        gpo, domain = system_exec[0]
        return _handoff_directive(
            "Mythic_Operator",
            f"Observed execute_capability results already prove `system-exec:gpo:{gpo}@{domain}`. Do not repeat "
            "the GPO write. Verify/record the durable domain-admin effect if missing, then continue to Kerberos "
            "context refresh and DCSync.",
            "Verify domain admin effect",
        )

    return None


def _requests_gpo_domain_admin_add(instruction: str) -> bool:
    if not instruction:
        return False
    has_gpo = "gpo" in instruction or "starkwallpaper" in instruction or "sharpgpoabuse" in instruction
    has_group_add = (
        "domain admins" in instruction
        or "net group" in instruction
        or "addcomputertask" in instruction
        or "gpo-controlled-system-exec" in instruction
    )
    return bool(has_gpo and has_group_add)


def _observed_effects_from_execute_capability_results(state: dict) -> set[str]:
    effects: set[str] = set()
    for msg in _state_messages(state):
        text = _message_content_as_text(getattr(msg, "content", ""))
        effects.update(_effect_tokens_from_text(text))
        effects.update(_dcsync_krbtgt_effects_from_task_text(text))
        if not isinstance(msg, ToolMessage):
            continue
        if (getattr(msg, "name", "") or "") != "execute_capability":
            continue
        raw = text.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("achieved_effects", "recorded_effects", "satisfied_effects"):
            values = payload.get(key)
            if isinstance(values, list):
                effects.update(str(item).strip().casefold() for item in values if str(item).strip())
    return effects


def _effect_tokens_from_text(text: str) -> set[str]:
    if not text:
        return set()
    patterns = (
        r"\bkrbtgt-hash:[a-z0-9.-]+",
        r"\bda:[a-z0-9.-]+",
        r"\bea:[a-z0-9.-]+",
        r"\bds-replication-rights:[a-z0-9.-]+",
        r"\bkerberos-context:[a-z0-9.-]+@callback:\d+",
        r"\bsystem-exec:gpo:[^@\s`,]+@[a-z0-9.-]+",
    )
    effects: set[str] = set()
    for pattern in patterns:
        effects.update(match.group(0).strip("`'\".,;").casefold() for match in re.finditer(pattern, text, re.IGNORECASE))
    return effects


def _dcsync_krbtgt_effects_from_task_text(text: str) -> set[str]:
    if not text:
        return set()
    low = text.casefold()
    if "dcsync" not in low or "krbtgt" not in low:
        return set()
    success_markers = (
        '"status": "success"',
        "'status': 'success'",
        '"status":"success"',
        "hash ntlm:",
        "aes256_hmac",
        "[*] process exited",
    )
    if not any(marker in low for marker in success_markers):
        return set()
    effects: set[str] = set()
    for match in re.finditer(
        r"lsadump::dcsync\s+/domain:([a-z0-9.-]+)[^\r\n\"']*/user:(?:[a-z0-9_.-]+\\+)?krbtgt\b",
        text,
        re.IGNORECASE,
    ):
        domain = match.group(1).strip().casefold()
        if domain:
            effects.add(f"krbtgt-hash:{domain}")
    return effects


def _domains_with_effect_prefix(effects: set[str], prefix: str) -> set[str]:
    prefix_key = prefix.casefold()
    return {
        effect[len(prefix_key):].strip()
        for effect in effects
        if effect.startswith(prefix_key) and effect[len(prefix_key):].strip()
    }


def _kerberos_contexts_from_effects(effects: set[str]) -> dict[str, str]:
    contexts: dict[str, str] = {}
    for effect in effects:
        parsed = _parse_kerberos_context_effect_text(effect)
        if not parsed:
            continue
        domain, callback_id = parsed
        contexts[domain] = callback_id
    return contexts


def _gpo_system_exec_effects(effects: set[str]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    prefix = "system-exec:gpo:"
    for effect in effects:
        if not effect.startswith(prefix):
            continue
        tail = effect[len(prefix):]
        gpo, sep, domain = tail.partition("@")
        if sep and gpo.strip() and domain.strip():
            out.add((gpo.strip(), domain.strip()))
    return out


def _parent_domain(domain: str) -> str:
    parts = [part for part in str(domain or "").strip().split(".") if part]
    if len(parts) <= 2:
        return ""
    return ".".join(parts[1:])


def _redirect_stale_handoff_after_capability_progress(instruction: str, state: dict) -> _HandoffDirective | None:
    """Rewrite stale autonomous handoffs after a capability recorded a new effect.

    The supervisor often phrases the next delegation by replaying the previous user prompt. If a just-finished
    capability already recorded the sub-effect named in that prompt, replaying it sends the operator back to a
    completed proof loop. Use the latest execute_capability tool result as the source of truth and advance to
    the next deterministic capability.
    """
    if not instruction:
        return None
    terminal = _terminal_execute_capability_payload(_state_messages(state))
    if not terminal or str(terminal.get("verdict") or "").casefold() != "achieved":
        return None
    recorded = _terminal_recorded_effects(terminal)
    if not recorded:
        return None

    for effect in recorded:
        parsed_context = _parse_kerberos_context_effect_text(effect)
        if parsed_context:
            domain, callback_id = parsed_context
            if not _handoff_is_stale_context_or_dcsync_request(instruction, domain):
                continue
            netbios = _netbios_from_domain(domain)
            user = f"{netbios}\\krbtgt" if netbios else f"{domain}\\krbtgt"
            return _handoff_directive(
                "Mythic_Operator",
                f"Tool-result evidence in this run already recorded `kerberos-context:{domain}@callback:{callback_id}`. "
                "Do not repeat Domain Admins membership checks, klist/PAC refresh, or C$ proof for that same "
                f"context. Execute the next capability now: DCSync `{user}` from callback {callback_id} against "
                f"`{domain}` using the payload-native `dcsync` command or `execute_capability` for "
                f"`dcsync-krbtgt` if available. Record `krbtgt-hash:{domain}` from the real secret material "
                "before ticket forging or any parent/forest hop. If DCSync fails with 8439, fix DN/DC targeting; "
                "if it fails with 8453 after the recorded Kerberos context, surface that as a rights/context "
                "blocker instead of re-running the completed Kerberos proof.",
                f"DCSync {domain}",
            )

    return None


def _terminal_recorded_effects(payload: dict[str, Any]) -> list[str]:
    effects: list[str] = []
    for key in ("recorded_effects", "satisfied_effects"):
        values = payload.get(key)
        if isinstance(values, list):
            effects.extend(str(item).strip() for item in values if str(item).strip())
    action = payload.get("action")
    if isinstance(action, dict):
        values = action.get("effects")
        if isinstance(values, list) and effects:
            # Action effects are plan-shaped; include them only when the terminal payload also recorded an effect.
            effects.extend(str(item).strip() for item in values if str(item).strip())
    return list(dict.fromkeys(effects))


def _parse_kerberos_context_effect_text(effect: str) -> tuple[str, str] | None:
    match = re.match(r"^kerberos-context:([^@\s]+)@callback:(\d+)$", str(effect or "").strip(), re.IGNORECASE)
    if not match:
        return None
    return match.group(1).casefold(), match.group(2)


def _handoff_is_stale_context_or_dcsync_request(instruction: str, domain: str) -> bool:
    domain_key = _safe_lower(domain)
    netbios = _safe_lower(_netbios_from_domain(domain))
    mentions_domain = domain_key in instruction or (netbios and netbios in instruction)
    if not mentions_domain:
        return False
    progress_terms = ("continue", "autonomous", "solve", "objective", "essos", "dcsync", "krbtgt")
    stale_terms = (
        "kerberos",
        "ticket",
        "pac",
        "klist",
        "c$",
        "domain admins",
        "membership",
        "context",
        "dcsync",
        "krbtgt",
    )
    return any(term in instruction for term in progress_terms) and any(term in instruction for term in stale_terms)


def _netbios_from_domain(domain: str) -> str:
    first = str(domain or "").split(".", 1)[0].strip()
    return first.upper()


def _state_messages(state: dict, channel_keys: tuple[str, ...] | None = None) -> list[AnyMessage]:
    keys = channel_keys or (
        "messages",
        "supervisor_messages",
        "mythic_operator_messages",
        "mythic_payload_messages",
        "bloodhound_messages",
        "mcp_manager_messages",
        "generalist_messages",
        "autonomous_executor_messages",
    )
    messages: list[AnyMessage] = []
    for key in keys:
        value = state.get(key, []) if isinstance(state, dict) else []
        if isinstance(value, list):
            messages.extend(msg for msg in value if isinstance(msg, BaseMessage))
    return messages


def _observed_handoff_facts(state: dict) -> dict[str, Any]:
    messages = _state_messages(state)
    tool_text = "\n".join(
        _message_content_as_text(msg.content)
        for msg in messages
        if isinstance(msg, ToolMessage)
    )
    graph_verified = bool(re.search(
        r"(?:graph[_ -]?verified)[\"'\s:=]+true\b",
        tool_text,
        re.IGNORECASE,
    ))
    return {
        "graph_verified": graph_verified,
        "terminal_capability": _terminal_execute_capability_payload(messages),
    }


def _requests_collection_confirmation(instruction: str) -> bool:
    if not instruction:
        return False
    collection_terms = (
        "sharphound",
        "bloodhound zip",
        "ingest_collection",
        "graph_verified",
        "domain_info was empty",
    )
    phase_terms = (
        "zip",
        "download",
        "ingest",
        "completion",
        "collection",
        "list c:\\users\\public",
        "do not rerun",
        "do not re-run",
    )
    return any(term in instruction for term in collection_terms) and any(
        term in instruction for term in phase_terms
    )


def _terminal_capability_task_summary(payload: dict[str, Any]) -> str:
    issued = payload.get("issued") if isinstance(payload.get("issued"), list) else []
    task_ids: list[str] = []
    for item in issued:
        if not isinstance(item, dict):
            continue
        task_id = item.get("task_id") or item.get("mythic_task_id")
        if task_id:
            task_ids.append(str(task_id))
    if not task_ids:
        return ""
    return f" on Mythic task(s) {', '.join(task_ids[:6])}"


def _recent_channel_text(state: dict, channel_keys: tuple[str, ...], limit: int = 8) -> str:
    parts: list[str] = []
    for key in channel_keys:
        for msg in list(state.get(key, []) or [])[-limit:]:
            content = getattr(msg, "content", "")
            if content:
                parts.append(str(content))
    return "\n".join(parts[-limit:])


def _safe_lower(value: Any) -> str:
    return str(value or "").casefold()


def _extract_callback_id(text: str) -> str:
    for pattern in (r"callback[:\s]+(\d+)", r"callback@?:(\d+)", r"callback\s+\*\*(\d+)\*\*"):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""

sessions: dict[str, Model] = {}

async def get_session(session_id: str) -> Model|None:
    logger.debug(f"Retrieving session {session_id}")
    try:
        return sessions.get(session_id)
    except KeyError:
        logger.warning(f"Session {session_id} not found. Start a new Chat session.")
        return None

async def add_session(session_id: str, model: Model):
    logger.debug(f"Adding session {session_id} with model {model.provider} {model.model}")
    sessions[session_id] = model

async def list_sessions() -> dict[str, Model]:
    return dict(sessions)

async def request_stop_for_sessions(session_id: str | None = None) -> dict[str, Model]:
    target = str(session_id or "").strip()
    stopped: dict[str, Model] = {}
    for key, model in list(sessions.items()):
        model_task_id = str(getattr(model, "task_id", "") or "")
        model_display_id = str(getattr(model, "task_display_id", "") or "")
        if target and target not in {str(key), model_task_id, model_display_id}:
            continue
        try:
            model.request_stop()
            stopped[str(key)] = model
        except Exception as exc:
            logger.warning(f"Failed to request stop for session {key}: {exc}")
    return stopped

async def remove_session(session_id: str):
    logger.debug(f"Removing session {session_id}")
    sessions.pop(session_id, None)
