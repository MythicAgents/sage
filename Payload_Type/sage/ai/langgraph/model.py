import copy
import json
import re
import asyncio
import aiosqlite
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
from typing import Any, Callable, Literal
from typing_extensions import NotRequired
from uuid import UUID
from .mythic_tools import MythicTools, GUARDED_TOOLS
from .tool_cache import ToolCache
from .prompt_loader import load_prompt, filter_tools_by_frontmatter
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
_TOON_SENTINEL = "⟦TOON "
_TRUNCATION_MARKER = "[truncated"
_COMPACTION_PROTECTED_TOOLS = frozenset((
    "summarize_and_handback", "request_continuation", "respond_to_user",
    "transfer_to_Supervisor", "transfer_to_Generalist", "transfer_to_Mythic_Operator",
    "transfer_to_Mythic_Payload", "transfer_to_BloodHound", "transfer_to_MCP_Manager",
))


def _is_scalar_tool_value(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


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
    """Apply the Bedrock fix and return the model unchanged."""
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
        connected = any("bloodhound" in s.lower() for s in MCPManager.get_connected_servers())
        if connected:
            return None
        await self._model._notify_bloodhound_not_connected()
        logger.info("🩸 [bloodhound-guard] BloodHound MCP not connected → EventFeed notice + returning steps to user")
        return {"jump_to": "end", "messages": [AIMessage(content=_BLOODHOUND_CONNECT_STEPS)]}


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
            or "single capability" in text
            or "one next grounded capability" in text
            or "one capability" in text
        )
        asks_to_stop = "then stop" in text or "stop after" in text or "retry at most" in text
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


def _terminal_execute_capability_report(payload: dict[str, Any]) -> str:
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

    def __init__(self, agent_name: str, stream_func=None, format_func=None):
        self.agent_name = agent_name
        self.captured_messages: list[AnyMessage] = []
        self._tool_call_to_name: dict[str, str] = {}  # Map tool_call_id to tool name
        self._stream_func = stream_func  # Function to stream formatted messages to Mythic
        self._format_func = format_func  # Function to format messages for streaming
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

                            if should_stream and self._stream_func and self._format_func:
                                formatted = self._format_func(msg, agent_name=self.agent_name)
                                if formatted:
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

    def __init__(self, provider: str, model: str, system_prompt: str, config: dict, task_id: int, agent_task_id: str, mode: str = "auto", autonomous_solve: bool = False, max_steps: int = 200):
        """
        Initialize the Model with provider, model, and configuration.
        :param provider: The model provider (e.g., 'anthropic', 'bedrock').
        :param model: The model string (e.g., 'claude-3-5-sonnet-latest').
        :param system_prompt: The system prompt to use for the model.
        :param config: A dictionary containing configuration options for the model {"configurable": {}}.
        """
        self.provider = provider
        self.model = model
        self.mode = mode if mode in ("auto", "supervised") else "auto"
        self._autonomous_solve = bool(autonomous_solve)
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
        # Initialize dynamic data cache
        self._payload_names = None
        self._c2_profiles = None
        self._cached_commands = None
        self._dynamic_data_loaded = False
        db_path = "sage.db"  # Path to your SQLite database
        self.tool_cache = ToolCache(db_path)
        conn = aiosqlite.connect(db_path, check_same_thread=False)
        self.memory = AsyncSqliteSaver(conn)
        self.system_message = SystemMessage(content=system_prompt)
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
        elif not self.config.get("configurable"):
            logger.error("Model configuration is missing 'configurable' settings.")
            return None
        else:
            cfg = self.config.get("configurable")

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
        elif cfg is not None and cfg.get("api_key"):
            if cfg.get("base_url"):
                logger.debug(f"Initializing model with provider={self.provider}, model={self.model}, base_url={cfg.get('base_url')}")
                llm = init_chat_model(model_provider=self.provider, model=self.model, api_key=cfg.get("api_key"), base_url=cfg.get("base_url"))
            else:
                logger.debug(f"Initializing model with provider={self.provider}, model={self.model} and api_key")
                llm = init_chat_model(model_provider=self.provider, model=self.model, api_key=cfg.get("api_key"))
        else:
            logger.debug(f"Initializing model with provider={self.provider}, model={self.model} and no api_key")
            llm = init_chat_model(model_provider=self.provider, model=self.model)

        # Patch model to strip empty text blocks before every LLM call.
        # This catches blank content blocks inside LangChain's internal react loop
        # where our _sanitize_messages can't reach.
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

    async def _stream_message_to_mythic(self, formatted_message: str) -> bool:
        """
        Stream a formatted message chunk to the Mythic task.

        Args:
            formatted_message: Pre-formatted message string (e.g., "🤖[Agent]> response")

        Returns:
            True if successful, False otherwise
        """
        try:
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
                    else:
                        logger.debug(f"📨 [Stream] Suppressing Supervisor respond_to_user message from user output")

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
                # Show agent handoffs: "📋[Task → Mythic_Operator]> Query active callbacks"
                return f"📋[Task → {delegated_to}]> {content}\n"
            else:
                # User prompts: Show for non-interactive tasks, skip for interactive tasks
                # - Non-interactive (first turn): Mythic doesn't echo the prompt, so we show it
                # - Interactive (subsequent turns): Mythic echoes it, so we skip to avoid duplication
                if self.is_interactive:
                    return ""  # Skip - Mythic already shows it
                else:
                    return f"👤> {content}\n"  # Show it

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
                output += f"🤖[{msg_agent_name}]> {text_content}\n"

            # Show tool calls with agent name and tool ID in brackets (only in verbose mode)
            if self.verbose and hasattr(message, "tool_calls") and message.tool_calls:
                for tc in message.tool_calls:
                    tool_name = tc.get("name", "unknown")
                    tool_args = tc.get("args", {})
                    tool_id = tc.get("id", "unknown")
                    output += f"🛠️[{msg_agent_name}:{tool_id}]> Tool Request: '{tool_name}', Args: '{tool_args}'\n"

            return output

        elif isinstance(message, ToolMessage):
            # Show tool execution results (only in verbose mode)
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

        self.mythic_client = MythicTools(agent_task_id=self.agent_task_id)
        await self.mythic_client.login()
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

            # Create callback handler to capture ALL messages during agent execution
            # This captures the first AIMessage (with tool_calls) that LangChain's react agent
            # would otherwise "consume" during its internal tool execution loop
            # Pass streaming functions so messages are streamed immediately as they're captured
            callback_handler = MessageCaptureCallback(
                agent_name=node_name,
                stream_func=self._stream_message_to_mythic,
                format_func=self._format_message_for_streaming
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
                result = await agent_runnable.ainvoke({"messages": _agent_input}, invoke_config)
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
                    # Engagement-state-aware nudge (flag-gated, fail-open): when the engagement gate is
                    # enabled, prepend a FRESH rendered snapshot of the observed engagement state plus a
                    # "don't re-propose achieved hops" directive. This stops the Operator from re-issuing
                    # already-achieved hops dozens of times (which the gate would only SKIP, burning the
                    # step budget). With the flag OFF this is a byte-for-byte no-op (plain base nudge).
                    _nudge_text = _base_nudge_text
                    try:
                        try:
                            from . import mythic_tools as _mt
                        except ImportError:
                            import mythic_tools as _mt
                        if bool(getattr(_mt, "ENGAGEMENT_GATE_ENABLED", False)):
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
                    final_msg = AIMessage(
                        content=_terminal_execute_capability_report(terminal_payload),
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
        """Best-effort build of the current EngagementState the same way the gate does.

        Returns an ``engagement_state.EngagementState`` or ``None`` on ANY error — never raises.
        Mirrors ``MythicTools._engagement_gate``: reconcile live footholds (fail-open to []) and
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

            return engagement_state.EngagementState(
                objective=objective,
                footholds=footholds,
                hops=hops,
            )
        except Exception:
            return None

    def _render_engagement_state_for_injection(self) -> str | None:
        """Cheap, synchronous, in-memory render of the observed engagement state for PER-TURN injection
        by `_EngagementStateMiddleware`. Returns None — and the middleware injects nothing — when:
        the gate is off, this is not an autonomous solve, there is no mythic_client, or there is no
        observed state yet. NO network: reads the in-memory incremental hop ledger (the anti-loop
        signal) plus the footholds the gate cached. Never raises (caller also guards)."""
        try:
            if not bool(getattr(self, "_autonomous_solve", False)):
                return None
            try:
                from . import mythic_tools as _mt
            except ImportError:
                import mythic_tools as _mt
            if not bool(getattr(_mt, "ENGAGEMENT_GATE_ENABLED", False)):
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
            # Include the cached graph facts (refreshed after each verified ingest) so the render's forward
            # planner can surface NEXT GROUNDED ACTIONS. Suggestion-only — the gate's enforcement state is
            # built separately (mythic_tools._engagement_gate) and intentionally omits these.
            graph_facts = list(getattr(mythic_client, "_engagement_graph_facts", []) or [])
            state = _es.EngagementState(
                objective=objective, footholds=footholds, hops=hops, graph_facts=graph_facts
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
        graph facts that the engagement gate/per-turn injection maintain, so the stop check cannot mint new
        facts; it can only notice that already-recorded proof satisfies the current objective.
        """
        try:
            if require_autonomous and not bool(getattr(self, "_autonomous_solve", False)):
                return None
            try:
                from . import mythic_tools as _mt
            except ImportError:
                import mythic_tools as _mt
            if not bool(getattr(_mt, "ENGAGEMENT_GATE_ENABLED", False)):
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
            return _es.EngagementState(
                objective=objective,
                footholds=footholds,
                hops=hops,
                graph_facts=list(getattr(mythic_client, "_engagement_graph_facts", []) or []),
            )
        except Exception:
            return None

    def _autonomous_handoff_step_redirect(
        self,
        agent_name: str,
        handoff_instruction: str,
        state: dict,
    ) -> tuple[str, str] | None:
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
                        return "Mythic_Operator", instruction
                phase = str(_es.engagement_phase(snapshot))
                if phase.startswith("BLOCKED"):
                    if _recent_bloodhound_blocker_observed(state):
                        instruction = _compiled_autonomous_blocked_report(
                            snapshot,
                            handoff_instruction=handoff_instruction,
                            requested_agent=agent_name,
                        )
                        if instruction:
                            return "__terminal__", instruction
                    if agent_name == "Mythic_Operator":
                        instruction = _compiled_autonomous_blocked_bloodhound_instruction(
                            snapshot,
                            handoff_instruction=handoff_instruction,
                            requested_agent=agent_name,
                        )
                        if instruction:
                            return "BloodHound", instruction
            except Exception:
                return None
            return None
        action = actions[0]
        instruction = _compiled_autonomous_capability_instruction(
            action,
            snapshot,
            handoff_instruction=handoff_instruction,
            requested_agent=agent_name,
        )
        return "Autonomous_Executor", instruction

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
            candidates = _es.objective_completion_candidates(state)
            if not candidates:
                return None
            objective = str(getattr(state, "objective", "") or "").strip()
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
            result_text = await self.mythic_client.execute_capability(action_payload, inputs_payload)

        tool_msg = ToolMessage(
            content=result_text,
            name="execute_capability",
            tool_call_id=tool_call_id,
        )
        _tag_msg(tool_msg, next_seq)
        next_seq += 1

        terminal = _terminal_execute_capability_payload([tool_msg])
        summary_text = (
            _terminal_execute_capability_report(terminal)
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
        """Whether a non-autonomous prompt is explicitly asking to continue/report the engagement objective."""
        if bool(getattr(self, "_autonomous_solve", False)):
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
                "materialize_capability_inputs",
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
            handback_to_supervisor_tool = _create_handback_to_supervisor_tool(self.mythic_client)

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
        bh_servers = [s for s in MCPManager.get_connected_servers() if "bloodhound" in s.lower()]
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
        other_servers = [s for s in MCPManager.get_connected_servers() if "bloodhound" not in s.lower()]
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
            system_prompt=prompt,
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
            2. Keep only the FIRST SystemMessage, remove all others (prevents "multiple non-consecutive system messages" error)
            Note: Bedrock blank text field fix is handled at the payload level by _patch_model_for_bedrock.
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
                    # Only keep the first SystemMessage
                    if not seen_system_message:
                        cleaned.append(m)
                        seen_system_message = True
                    # else drop additional system messages
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

    async def handle_hitl_resume(self, response: str, thread_id: str) -> str:
        """Resume a graph paused on a guarded-tool approval interrupt with a DEFAULT-DENY decision map.

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
        decision_word = "approve" if approved else "deny"

        # One audit line + one Decision per interrupted tool call.
        decisions: list[dict] = []
        if action_requests:
            for ar in action_requests:
                tool_name = ar.get("name", "unknown") if isinstance(ar, dict) else "unknown"
                tool_args = ar.get("args", {}) if isinstance(ar, dict) else {}
                self._write_hitl_audit(tool_name, tool_args, decision_word)
                if approved:
                    decisions.append({"type": "approve"})
                else:
                    decisions.append({"type": "reject",
                                      "message": f"[DENIED by operator] {tool_name} was not executed."})
        else:
            # No structured action_requests recovered — still resume default-deny safely with a single
            # decision so the graph does not hang. Audit it as an unknown-tool deny/approve.
            self._write_hitl_audit("unknown", {}, decision_word)
            if approved:
                decisions.append({"type": "approve"})
            else:
                decisions.append({"type": "reject", "message": "[DENIED by operator]"})

        logger.info(f"HITL resume on thread {thread_id}: {decision_word} for {len(decisions)} tool call(s)")

        # Resume the paused graph with the decision payload the installed middleware expects.
        async for event in self.graph.astream(
            Command(resume={"decisions": decisions}),
            {"configurable": {"thread_id": thread_id}, "recursion_limit": 250}
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

        thread_id = f"{self.agent_task_id}-{self.task_id}"
        if await self._hitl_interrupt_pending(thread_id):
            logger.info(f"HITL interrupt pending on thread {thread_id} — routing operator reply to approve/deny resume")
            return await self.handle_hitl_resume(prompt, thread_id)

        # Check if we're responding to a recursion limit continuation request
        # If so, delegate to handle_continuation_response() instead of normal flow
        was_recursion_requested = self.state.get("recursion_summary_requested", False)
        if was_recursion_requested:
            logger.info(f"Detected continuation response: '{prompt}' - delegating to handle_continuation_response()")
            # Reset the flag before handling
            self.state["recursion_summary_requested"] = False
            return await self.handle_continuation_response(prompt)

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

        try:
            # Recursion limit 250 for multi-hop autonomous solves (e.g. the GOAD Trust Walker is many agent
            # hops — foothold→essos DA exceeded 150); RemainingSteps + handback still terminate gracefully,
            # and the global step cap (_max_steps, default 200 / 300 via the solve driver) backstops runaways.
            logger.debug(f"🚀 Before astream: self.state._message_seq={self.state.get('_message_seq')}, Model._message_seq={self._message_seq}")

            # Stream graph execution and process events incrementally
            hitl_interrupted = False
            async for event in self.graph.astream(
                self.state,
                {"configurable": {"thread_id": f"{self.agent_task_id}-{self.task_id}"}, "recursion_limit": 250}
            ):
                # Cooperative kill switch: an operator `exit`/stop set _stop_requested on this
                # Model; halt before driving the next super-step so the session can't run away.
                if self._stop_requested:
                    logger.info("🛑 Stop requested — terminating graph execution (main loop)")
                    try:
                        await self._stream_message_to_mythic("\n🛑> Session stopped by operator.\n")
                    except Exception:
                        pass
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
            stop_message = "\n🛑> Session stopped by operator.\n"
            logger.info("🛑 Operator stop cancelled active invoke task — terminating session")
            try:
                await self._stream_message_to_mythic(stop_message)
            except Exception:
                pass
            return ""
        except _OperatorStopRequested:
            # Kill-switch fired inside an agent turn (finer-grained than the between-super-steps
            # check). End the session cleanly instead of surfacing it as an error.
            if getattr(self, "_global_step_limit_hit", False):
                stop_message = (
                    f"\n🛑> Halted: global step limit ({self._max_steps}) reached; "
                    "the run may be looping without progress.\n"
                )
                logger.info(
                    f"🛑 Global step limit stop honored inside agent loop after "
                    f"{self._global_step_count} model steps"
                )
            else:
                stop_message = "\n🛑> Session stopped by operator.\n"
                logger.info("🛑 Operator stop honored inside agent loop — terminating session")
            try:
                await self._stream_message_to_mythic(stop_message)
            except Exception:
                pass
            return ""
        except GraphRecursionError as e:
            # Catch recursion limit error and return progress made so far
            logger.warning(f"Recursion limit hit: {e}")

            # CRITICAL: When recursion limit hits, astream() only yielded events for COMPLETED nodes.
            # The current node (e.g., Mythic_Operator) was terminated mid-execution, so its messages
            # are NOT in self.state. We MUST restore from checkpoint to get partial progress.

            thread_id = f"{self.agent_task_id}-{self.task_id}"
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

**Status:** Hit the system's iteration limit of 250 steps. All work and context have been preserved in each agent's conversation history.

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
            thread_id = f"{self.agent_task_id}-{self.task_id}"
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

        thread_id = f"{self.agent_task_id}-{self.task_id}"
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
                    # Stream continuation with raised recursion limit (250)
                    async for event in self.graph.astream(
                        self.state,
                        {"configurable": {"thread_id": thread_id}, "recursion_limit": 250}
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
                    # Stream new task direction with raised recursion limit (250)
                    async for event in self.graph.astream(
                        self.state,
                        {"configurable": {"thread_id": thread_id}, "recursion_limit": 250}
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


def _create_handback_to_supervisor_tool(mythic_client=None):
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
        esl = ""
        try:
            try:
                from . import mythic_tools as _mt
            except ImportError:
                import mythic_tools as _mt
        except ImportError:
            _mt = None
        if _mt is not None and getattr(_mt, "ENGAGEMENT_GATE_ENABLED", False):
            try:
                esl = _build_esl_summary(mythic_client)
            except Exception:
                esl = ""  # fail-open: never break handback
        msg = ToolMessage(
            content=f"🔄 **Handback to Supervisor** — {reason}\n\n{summary}" + (f"\n\n{esl}" if esl else ""),
            name="handback_to_supervisor",
            tool_call_id=runtime.tool_call_id,
        )
        updated_state = {**runtime.state}
        updated_state["supervisor_messages"] = [msg]
        updated_state["messages"] = [msg]
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
    autonomous_redirect: Callable[[str, str, dict], tuple[str, str] | None] | None = None,
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
    description = description or f"Delegate a task to {agent_name}. Provide a single 'handoff_instruction' argument containing the complete task."

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
        handoff_instruction: Annotated[str, "The complete, self-contained instruction for the target agent: a full sentence stating exactly what to do, with NO pronouns and NO references to 'it'/'that'/'the previous task'. Example: 'List all active Mythic callbacks and report each host, user, and integrity level.' This is the ONLY argument for this tool — do not invent positional or placeholder argument names (e.g. a, b, c)."],
    ) -> Command:
        redirect = None
        if autonomous_redirect is not None:
            try:
                redirect = autonomous_redirect(agent_name, handoff_instruction, runtime.state)
            except Exception:
                redirect = None
        if redirect is None:
            redirect = _autonomous_handoff_redirect(agent_name, handoff_instruction, runtime.state)
        terminal_redirect = bool(redirect and redirect[0] == "__terminal__")
        actual_agent_name = "Supervisor" if terminal_redirect else (redirect[0] if redirect else agent_name)
        actual_instruction = redirect[1] if redirect else handoff_instruction
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


def _capability_action_payload(action: Any) -> dict[str, Any]:
    return {
        "name": _jsonable_value(getattr(action, "name", "")),
        "target": _jsonable_value(getattr(action, "target", "")),
        "preconditions": _jsonable_value(list(getattr(action, "preconditions", []) or [])),
        "effects": _jsonable_value(list(getattr(action, "effects", []) or [])),
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


def _autonomous_capability_inputs(action: Any, engagement_snapshot: Any) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    callback_id = _autonomous_callback_id_for_action(action, engagement_snapshot)
    if callback_id:
        inputs["callback_id"] = callback_id
    return inputs


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
    achieved = set()
    try:
        achieved = set(engagement_snapshot.achieved_effects())
    except Exception:
        achieved = set()
    if domain:
        prefix = f"kerberos-context:{domain}@callback:"
        for effect in sorted(achieved):
            text = str(effect or "").strip().casefold()
            if text.startswith(prefix):
                return text[len(prefix):].split(None, 1)[0].strip()

    footholds = list(getattr(engagement_snapshot, "footholds", []) or [])
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


def _autonomous_handoff_redirect(agent_name: str, handoff_instruction: str, state: dict) -> tuple[str, str] | None:
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
                return (
                    "Mythic_Operator",
                    "Do not perform SharpHound collection confirmation, ZIP discovery, download, or BloodHound "
                    "ingest for this access context. Tool-result evidence in the current run already shows "
                    f"`graph_verified=true`. Continue from the observed graph and recover from the latest "
                    f"terminal capability result: `{capability}` returned `{verdict}`"
                    f"{tasks}; reason: {reason}. Inspect the referenced task output if needed, repair and retry "
                    "that capability only when the error is recoverable, or replan from the verified graph. "
                    "Do not regress to collection work."
                )
            return (
                "BloodHound",
                "Tool-result evidence in the current run already shows `graph_verified=true` for the current "
                "access context. Do not ask Mythic_Operator to confirm SharpHound completion, list ZIPs, "
                "download collections, or ingest again. Analyze the verified BloodHound graph and return the "
                "next concrete graph-supported hop plus the exact Mythic action needed next.",
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
    return (
        "BloodHound",
        "Analyze the current BloodHound graph for the next concrete sevenkingdoms.local -> essos.local hop "
        f"from proven sevenkingdoms administrative control on callback {callback_id}. Do not repeat the "
        "STARKWALLPAPER/GPO hop, do not rerun sevenkingdoms krbtgt DCSync after the recorded 0x20f7/8439 "
        "failures, and return the exact next traversable principal/group/edge plus the Mythic action needed next.",
    )


def _redirect_stale_gpo_handoff_from_observed_effects(instruction: str, state: dict) -> tuple[str, str] | None:
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
            return (
                "Mythic_Operator",
                f"Observed execute_capability results already prove the STARKWALLPAPER/GPO chain is past the "
                f"GPO hop: `krbtgt-hash:{domain}` is achieved. Do not repeat GPO abuse, Domain Admins "
                f"membership polling, Kerberos PAC refresh, or NORTH DCSync. Execute the next capability now: "
                f"call `build_capability_commands` for `forge-golden-ticket` with `domain={domain}`, "
                f"`target_domain={parent}`, and callback {callback_id}; then issue the returned structured "
                "commands exactly without editing SID/key/domain fields. Verify administrative control over "
                f"`{parent}` before any ESSOS trust hop.",
            )
        return (
            "Mythic_Operator",
            f"Observed execute_capability results already prove `krbtgt-hash:{domain}` is achieved. Do not "
            "repeat GPO abuse or membership/PAC checks. Replan from the achieved krbtgt hash and execute the "
            "next non-GPO capability toward the objective.",
        )

    for domain, callback_id in sorted(contexts.items()):
        netbios = _netbios_from_domain(domain)
        user = f"{netbios}\\krbtgt" if netbios else f"{domain}\\krbtgt"
        return (
            "Mythic_Operator",
            f"Observed execute_capability results already prove `kerberos-context:{domain}@callback:{callback_id}`. "
            "Do not repeat STARKWALLPAPER/GPO abuse, Domain Admins membership polling, or PAC refresh. Execute "
            f"NORTH DCSync now from callback {callback_id}: DCSync `{user}` against `{domain}` and record "
            f"`krbtgt-hash:{domain}` from real secret material.",
        )

    for domain in sorted(_domains_with_effect_prefix(effects, "da:")):
        return (
            "Mythic_Operator",
            f"Observed execute_capability results already prove `da:{domain}`. Do not repeat STARKWALLPAPER/GPO "
            "abuse or Domain Admins membership polling. Execute `ensure-kerberos-context` for that domain on the "
            "live callback, then proceed to DCSync only after the context effect is recorded.",
        )

    system_exec = sorted(_gpo_system_exec_effects(effects))
    if system_exec:
        gpo, domain = system_exec[0]
        return (
            "Mythic_Operator",
            f"Observed execute_capability results already prove `system-exec:gpo:{gpo}@{domain}`. Do not repeat "
            "the GPO write. Verify/record the durable domain-admin effect if missing, then continue to Kerberos "
            "context refresh and DCSync.",
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


def _redirect_stale_handoff_after_capability_progress(instruction: str, state: dict) -> tuple[str, str] | None:
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
            return (
                "Mythic_Operator",
                f"Tool-result evidence in this run already recorded `kerberos-context:{domain}@callback:{callback_id}`. "
                "Do not repeat Domain Admins membership checks, klist/PAC refresh, or C$ proof for that same "
                f"context. Execute the next capability now: DCSync `{user}` from callback {callback_id} against "
                f"`{domain}` using the payload-native `dcsync` command or `execute_capability` for "
                f"`dcsync-krbtgt` if available. Record `krbtgt-hash:{domain}` from the real secret material "
                "before ticket forging or any parent/forest hop. If DCSync fails with 8439, fix DN/DC targeting; "
                "if it fails with 8453 after the recorded Kerberos context, surface that as a rights/context "
                "blocker instead of re-running the completed Kerberos proof.",
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
