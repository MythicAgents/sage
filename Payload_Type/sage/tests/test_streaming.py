"""
Tests for Sage streaming feature (streaming-chat branch).

Validates that LLM messages, tool calls, and agent handoffs are streamed
to the Mythic web UI in real-time via SendMythicRPCResponseCreate.

Each test class maps to an ISC section from the streaming feature PRD.
Tests use AST inspection and mocking to avoid requiring a live Mythic instance.

Run: cd Payload_Type/sage && python -m pytest tests/test_streaming.py -v
"""

import ast
import asyncio
import inspect
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add the sage package to path for imports
SAGE_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(SAGE_ROOT))

# We need to mock mythic_container before importing model.py since it requires
# a running Mythic server for imports
mythic_container_mock = MagicMock()
mythic_container_mock.logging.logger = MagicMock()
mythic_container_mock.MythicRPC.SendMythicRPCResponseCreate = AsyncMock()
mythic_container_mock.MythicRPC.MythicRPCResponseCreateMessage = MagicMock()
mythic_container_mock.MythicRPC.SendMythicRPCTaskUpdate = AsyncMock()
mythic_container_mock.MythicRPC.MythicRPCTaskUpdateMessage = MagicMock()

sys.modules["mythic_container"] = mythic_container_mock
sys.modules["mythic_container.logging"] = mythic_container_mock.logging
sys.modules["mythic_container.MythicRPC"] = mythic_container_mock.MythicRPC

# Read model.py source for AST-based tests (avoids full import chain)
MODEL_PY = SAGE_ROOT / "ai" / "langgraph" / "model.py"
MODEL_SOURCE = MODEL_PY.read_text()
MODEL_AST = ast.parse(MODEL_SOURCE)

CHAT_PY = SAGE_ROOT / "container" / "agent_functions" / "chat.py"
CHAT_SOURCE = CHAT_PY.read_text()


# ============================================================================
# Helper: find class/method AST nodes
# ============================================================================

def find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def find_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def find_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def method_calls_in(func_node) -> list[str]:
    """Extract all method/function call names from an AST function node."""
    calls = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)
            elif isinstance(node.func, ast.Name):
                calls.append(node.func.id)
    return calls


def get_method_source(method_name: str, class_name: str = "Model") -> str:
    """Get the source text of a specific method."""
    cls = find_class(MODEL_AST, class_name)
    if not cls:
        return ""
    method = find_method(cls, method_name)
    if not method:
        return ""
    return ast.get_source_segment(MODEL_SOURCE, method) or ""


# ============================================================================
# ISC-1 through ISC-7: Core Streaming Functionality
# ============================================================================

class TestCoreStreaming:
    """Tests for ISC-1 through ISC-7: Core streaming functionality."""

    def test_isc1_invoke_uses_astream(self):
        """ISC-1: invoke() uses graph.astream() instead of graph.ainvoke()."""
        invoke_src = get_method_source("invoke")
        assert "astream" in invoke_src, "invoke() must call graph.astream()"
        # Verify ainvoke is NOT used (except possibly in comments)
        lines = [l for l in invoke_src.split("\n") if not l.strip().startswith("#")]
        code_only = "\n".join(lines)
        assert "ainvoke" not in code_only, "invoke() must NOT call graph.ainvoke()"

    def test_isc2_ai_messages_streamed_via_callback(self):
        """ISC-2: AIMessages streamed to Mythic UI via callback on_llm_end."""
        cls = find_class(MODEL_AST, "MessageCaptureCallback")
        assert cls is not None, "MessageCaptureCallback class must exist"
        on_llm_end = find_method(cls, "on_llm_end")
        assert on_llm_end is not None, "on_llm_end method must exist"

        src = ast.get_source_segment(MODEL_SOURCE, on_llm_end) or ""
        assert "_stream_func" in src, "on_llm_end must call _stream_func for streaming"
        assert "_format_func" in src, "on_llm_end must call _format_func for formatting"

    def test_isc3_tool_messages_streamed_via_callback(self):
        """ISC-3: ToolMessages streamed to Mythic UI via callback on_tool_end."""
        cls = find_class(MODEL_AST, "MessageCaptureCallback")
        assert cls is not None
        on_tool_end = find_method(cls, "on_tool_end")
        assert on_tool_end is not None, "on_tool_end method must exist"

        src = ast.get_source_segment(MODEL_SOURCE, on_tool_end) or ""
        assert "_stream_func" in src, "on_tool_end must call _stream_func for streaming"
        assert "_format_func" in src, "on_tool_end must call _format_func for formatting"

    def test_isc4_human_messages_streamed_via_process_stream_event(self):
        """ISC-4: HumanMessages (handoffs) streamed via _process_stream_event."""
        src = get_method_source("_process_stream_event")
        assert src, "_process_stream_event method must exist"
        assert "HumanMessage" in src, "_process_stream_event must handle HumanMessages"
        assert "_stream_message_to_mythic" in src, "Must call _stream_message_to_mythic"

    def test_isc4_only_human_messages_in_process_stream_event(self):
        """ISC-4 (anti-duplicate): _process_stream_event only streams HumanMessages."""
        src = get_method_source("_process_stream_event")
        # Verify it checks for HumanMessage specifically and doesn't stream AI/Tool messages
        assert "isinstance(msg, HumanMessage)" in src, (
            "_process_stream_event must filter to HumanMessage only "
            "(AI/Tool messages are streamed by callback)"
        )

    def test_isc5_stream_message_uses_mythic_rpc(self):
        """ISC-5: _stream_message_to_mythic sends via SendMythicRPCResponseCreate."""
        src = get_method_source("_stream_message_to_mythic")
        assert src, "_stream_message_to_mythic method must exist"
        assert "SendMythicRPCResponseCreate" in src, "Must use SendMythicRPCResponseCreate"
        assert "MythicRPCResponseCreateMessage" in src, "Must use MythicRPCResponseCreateMessage"
        assert "self.task_id" in src, "Must use self.task_id for the target task"
        assert ".encode()" in src, "Must encode message to bytes"

    def test_isc6_user_prompt_shown_for_non_interactive(self):
        """ISC-6: User prompt shown for non-interactive first turn only."""
        invoke_src = get_method_source("invoke")
        assert "not is_interactive" in invoke_src or "not self.is_interactive" in invoke_src, (
            "invoke() must check is_interactive to decide prompt streaming"
        )
        assert "_stream_message_to_mythic" in invoke_src, (
            "invoke() must stream user prompt for non-interactive turns"
        )

    def test_isc7_user_prompt_hidden_for_interactive(self):
        """ISC-7: User prompt NOT shown for interactive turns (Mythic echoes it)."""
        fmt_src = get_method_source("_format_message_for_streaming")
        assert "is_interactive" in fmt_src, (
            "_format_message_for_streaming must check is_interactive"
        )
        # Verify interactive HumanMessages return empty string
        assert 'return ""' in fmt_src, (
            "Must return empty string for interactive user prompts"
        )


# ============================================================================
# ISC-8 through ISC-12: Message Formatting
# ============================================================================

class TestMessageFormatting:
    """Tests for ISC-8 through ISC-12: Message formatting."""

    def _get_format_source(self) -> str:
        return get_method_source("_format_message_for_streaming")

    def test_isc8_ai_text_format(self):
        """ISC-8: AI text responses formatted as robot-emoji[AgentName]> text."""
        src = self._get_format_source()
        # Check for the robot emoji format pattern
        assert "🤖" in src, "Must use robot emoji for AI messages"
        assert "msg_agent_name" in src or "agent_name" in src, (
            "Must include agent name in AI message formatting"
        )

    def test_isc9_tool_request_format(self):
        """ISC-9: Tool requests formatted as wrench-emoji[AgentName:ToolID]> Tool Request..."""
        src = self._get_format_source()
        assert "🛠️" in src or "🛠" in src, "Must use tool emoji for tool requests"
        assert "Tool Request" in src, "Must include 'Tool Request' label"

    def test_isc10_tool_response_format(self):
        """ISC-10: Tool responses formatted as wrench-emoji[AgentName:ToolID]> Tool Response..."""
        src = self._get_format_source()
        assert "🔧" in src, "Must use wrench emoji for tool responses"
        assert "Tool Response" in src, "Must include 'Tool Response' label"

    def test_isc11_agent_handoff_format(self):
        """ISC-11: Agent handoffs formatted as clipboard-emoji[Task -> AgentName]> instruction."""
        src = self._get_format_source()
        assert "📋" in src, "Must use clipboard emoji for handoff messages"
        assert "Task" in src and "→" in src, "Must show 'Task -> AgentName' for handoffs"

    def test_isc12_system_messages_skipped(self):
        """ISC-12: System messages skipped in streaming output."""
        src = self._get_format_source()
        assert "SystemMessage" in src, "Must check for SystemMessage"
        assert 'return ""' in src, "Must return empty string for SystemMessage"

    def test_internal_provider_nudges_not_rendered_as_user_prompts(self):
        """Regression: provider/autonomous control nudges are not shown as Mythic user input."""
        fmt_src = self._get_format_source()
        render_src = get_method_source("_render_combined")
        sanitize_src = get_method_source("_sanitize_messages")
        wrapper_src = get_method_source("_wrap_create_agent")

        assert "_hide_from_stream" in sanitize_src
        assert "_is_internal_human_message" in fmt_src
        assert "_is_internal_human_message" in render_src
        assert "_is_internal_human_message" in wrapper_src
        assert "autonomous_operator_continue" in wrapper_src

    def test_format_handles_list_content(self):
        """Regression: AI messages with list content (Anthropic blocks) handled."""
        src = self._get_format_source()
        assert "isinstance(message.content, list)" in src, (
            "Must handle list-type content (Anthropic content blocks)"
        )

    def test_format_handles_empty_content(self):
        """Regression: Empty content doesn't crash or produce empty output."""
        src = self._get_format_source()
        # Check for empty content handling
        assert "not content" in src or 'content == ""' in src or "not text_content" in src, (
            "Must handle empty content gracefully"
        )


# ============================================================================
# ISC-13 through ISC-15: chat.py Integration
# ============================================================================

class TestChatIntegration:
    """Tests for ISC-13 through ISC-15: chat.py integration."""

    def test_isc13_chat_passes_is_interactive(self):
        """ISC-13: chat.py passes is_interactive parameter to invoke()."""
        assert "is_interactive" in CHAT_SOURCE, (
            "chat.py must pass is_interactive to invoke()"
        )
        # More specifically, check it's in the invoke call
        assert "invoke(prompt, is_interactive=" in CHAT_SOURCE or \
               "invoke(prompt, is_interactive =" in CHAT_SOURCE, (
            "chat.py must pass is_interactive=taskData.Task.IsInteractiveTask to invoke()"
        )

    def test_isc14_chat_appends_prompt_indicator(self):
        """ISC-14: chat.py appends user-prompt-emoji prompt indicator after invoke() returns."""
        assert "👤>" in CHAT_SOURCE, (
            "chat.py must add prompt indicator after invoke()"
        )
        # Verify it's sent via RPC after invoke
        assert "SendMythicRPCResponseCreate" in CHAT_SOURCE, (
            "chat.py must send prompt indicator via RPC"
        )

    def test_isc15_error_handling_streams_then_raises(self):
        """ISC-15: Error handling streams error message then raises exception."""
        # Check that chat.py catches exceptions from invoke and streams error
        assert "except Exception" in CHAT_SOURCE, (
            "chat.py must catch exceptions from invoke()"
        )
        assert "❌" in CHAT_SOURCE or "Error" in CHAT_SOURCE, (
            "chat.py must stream error messages"
        )

    def test_chat_invoke_returns_empty_string(self):
        """Verify: invoke() returns empty string (all output already streamed)."""
        invoke_src = get_method_source("invoke")
        # Check that the normal return path returns empty string
        assert 'return ""' in invoke_src, (
            "invoke() must return empty string (output already streamed)"
        )


# ============================================================================
# ISC-16 through ISC-18: State Management
# ============================================================================

class TestStateManagement:
    """Tests for ISC-16 through ISC-18: State management during streaming."""

    def test_isc16_state_updated_incrementally(self):
        """ISC-16: self.state channels updated incrementally during astream loop."""
        invoke_src = get_method_source("invoke")
        # Must extend channels (not replace) since astream yields deltas
        assert ".extend(" in invoke_src, (
            "invoke() must extend state channels with event data (not replace)"
        )
        # Check all 5 agent channels are updated
        for channel in [
            "supervisor_messages",
            "generalist_messages",
            "mythic_operator_messages",
            "mythic_payload_messages",
            "mcp_manager_messages",
        ]:
            assert channel in invoke_src, f"invoke() must update {channel} channel"

    def test_isc17_message_seq_synchronized(self):
        """ISC-17: _message_seq counter synchronized between Model and state."""
        invoke_src = get_method_source("invoke")
        assert "_message_seq" in invoke_src, (
            "invoke() must update _message_seq from stream events"
        )
        # Also verify the _next_seq method syncs to state
        next_seq_src = get_method_source("_next_seq")
        assert 'self.state["_message_seq"]' in next_seq_src, (
            "_next_seq must sync counter to self.state"
        )

    def test_isc18_deduplication_works(self):
        """ISC-18: Deduplication via _msg_id works across merged channels post-stream."""
        invoke_src = get_method_source("invoke")
        assert "_msg_id" in invoke_src, (
            "invoke() must use _msg_id for deduplication"
        )
        assert "seen_ids" in invoke_src, (
            "invoke() must track seen message IDs"
        )

    def test_continuation_state_extends_not_replaces(self):
        """BUG CHECK: handle_continuation_response must extend, not replace channels."""
        continuation_src = get_method_source("handle_continuation_response")
        assert continuation_src, "handle_continuation_response must exist"

        # Check that list channel updates use .extend(), not bare assignment.
        # Assignment is correct for _message_seq (scalar), but list channels
        # must use .extend() since astream yields deltas with stream_mode='updates'.
        #
        # Strategy: find all `self.state[ch] = state_update[ch]` lines that are NOT
        # inside an `if ch == "_message_seq"` guard (those are scalar, assignment is correct).
        lines = continuation_src.split("\n")
        in_message_seq_guard = False
        channel_assignment_bug = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Track if we're inside a _message_seq guard
            if 'ch == "_message_seq"' in stripped or "ch == '_message_seq'" in stripped:
                in_message_seq_guard = True
            # Reset guard on else/elif (we've left the _message_seq branch)
            elif stripped.startswith("else:") or stripped.startswith("elif"):
                in_message_seq_guard = False

            # Look for bare assignment outside _message_seq guard
            if ("self.state[ch] = state_update[ch]" in stripped and
                    not in_message_seq_guard):
                channel_assignment_bug = True

        assert not channel_assignment_bug, (
            "BUG: handle_continuation_response uses assignment (=) instead of .extend() "
            "for list channel updates. Since astream yields deltas with stream_mode='updates', "
            "assignment loses all prior messages. Must use .extend() for list channels "
            "and = only for scalar values like _message_seq."
        )


# ============================================================================
# ISC-19 through ISC-21: Recursion Handling
# ============================================================================

class TestRecursionHandling:
    """Tests for ISC-19 through ISC-21: Recursion limit handling."""

    def test_isc19_recursion_error_caught_and_streamed(self):
        """ISC-19: GraphRecursionError caught and continuation message streamed."""
        invoke_src = get_method_source("invoke")
        assert "GraphRecursionError" in invoke_src, (
            "invoke() must catch GraphRecursionError"
        )
        assert "Recursion Limit Reached" in invoke_src or "recursion" in invoke_src.lower(), (
            "Must stream continuation message on recursion limit"
        )
        assert "_stream_message_to_mythic" in invoke_src, (
            "Must stream the recursion message to Mythic"
        )

    def test_isc20_continuation_resumes_with_higher_limit(self):
        """ISC-20: handle_continuation_response resumes with configured recursion budget."""
        continuation_src = get_method_source("handle_continuation_response")
        assert continuation_src, "handle_continuation_response must exist"
        assert "_graph_run_config" in continuation_src, (
            "Continuation must use the centralized graph recursion config"
        )
        graph_config_src = get_method_source("_graph_run_config")
        assert "recursion_limit" in graph_config_src, (
            "Central graph config must set recursion_limit"
        )
        assert "astream" in continuation_src, (
            "Continuation must use astream for streaming"
        )

    def test_isc21_checkpoint_recovery_after_recursion(self):
        """ISC-21: Checkpoint recovery restores agent channel state after recursion limit."""
        invoke_src = get_method_source("invoke")
        assert "memory.alist" in invoke_src or "checkpoint" in invoke_src.lower(), (
            "Must access checkpoints for recovery after recursion limit"
        )
        # Should restore agent-specific channels
        for channel in ["supervisor_messages", "mythic_operator_messages"]:
            assert channel in invoke_src, (
                f"Checkpoint recovery must handle {channel}"
            )


# ============================================================================
# ISC-22 through ISC-24: Code Health
# ============================================================================

class TestCodeHealth:
    """Tests for ISC-22 through ISC-24: Code health issues."""

    def test_isc22_is_interactive_initialized_before_use(self):
        """ISC-22: No uninitialized attributes accessed before invoke() is called.

        self.is_interactive is used in _format_message_for_streaming() but only
        set in invoke(). If _format_message_for_streaming is ever called outside
        the invoke() flow, it will crash with AttributeError.
        """
        cls = find_class(MODEL_AST, "Model")
        init_method = find_method(cls, "__init__")
        init_src = ast.get_source_segment(MODEL_SOURCE, init_method) or ""

        # Check if is_interactive is initialized in __init__
        has_init = "self.is_interactive" in init_src

        # This test documents the bug — should FAIL with current code
        assert has_init, (
            "BUG: self.is_interactive is NOT initialized in __init__(). "
            "It's only set in invoke(), but _format_message_for_streaming() "
            "accesses it. This will cause AttributeError if the method is "
            "called before invoke(). Fix: add self.is_interactive = False to __init__()."
        )

    def test_isc23_no_dead_code_is_first_turn(self):
        """ISC-23: Dead code (_is_first_turn) should be cleaned up.

        _is_first_turn is SET at lines 1922, 1926, 2211 but never READ.
        """
        # Find all assignments to _is_first_turn
        assignments = []
        reads = []
        for node in ast.walk(MODEL_AST):
            if isinstance(node, ast.Attribute) and node.attr == "_is_first_turn":
                if isinstance(node.ctx, ast.Store):
                    assignments.append(node.lineno)
                elif isinstance(node.ctx, ast.Load):
                    reads.append(node.lineno)

        has_assignments = len(assignments) > 0
        has_reads = len(reads) > 0

        if has_assignments and not has_reads:
            pytest.fail(
                f"DEAD CODE: self._is_first_turn is assigned at lines {assignments} "
                f"but never read. Should be removed."
            )
        elif not has_assignments:
            pass  # Already cleaned up - good

    def test_isc23_no_dead_code_current_turn_prompt_seq(self):
        """ISC-23: Dead code (_current_turn_prompt_seq) should be cleaned up.

        _current_turn_prompt_seq is SET at line 1814 but never READ.
        """
        assignments = []
        reads = []
        for node in ast.walk(MODEL_AST):
            if isinstance(node, ast.Attribute) and node.attr == "_current_turn_prompt_seq":
                if isinstance(node.ctx, ast.Store):
                    assignments.append(node.lineno)
                elif isinstance(node.ctx, ast.Load):
                    reads.append(node.lineno)

        has_assignments = len(assignments) > 0
        has_reads = len(reads) > 0

        if has_assignments and not has_reads:
            pytest.fail(
                f"DEAD CODE: self._current_turn_prompt_seq is assigned at lines "
                f"{assignments} but never read. Should be removed."
            )

    def test_isc24_verbose_flag_respected(self):
        """ISC-24: verbose flag respected in streaming path (tool visibility control).

        In the main branch, verbose controls whether tool calls are shown.
        In the streaming branch, _format_message_for_streaming always shows tool calls
        regardless of verbose setting.
        """
        fmt_src = get_method_source("_format_message_for_streaming")
        has_verbose_check = "self.verbose" in fmt_src or "verbose" in fmt_src

        # This test documents the behavior change — should FAIL with current code
        assert has_verbose_check, (
            "BEHAVIOR CHANGE: _format_message_for_streaming does NOT check self.verbose. "
            "In the main branch, verbose controls tool call visibility. In the streaming "
            "branch, all tool calls are always shown. If this is intentional, update the "
            "chat command description. If not, add verbose check to skip tool call formatting."
        )


# ============================================================================
# ISC-A-1 through ISC-A-3: Anti-Criteria
# ============================================================================

class TestAntiCriteria:
    """Tests for anti-criteria: things that must NOT happen."""

    def test_isca1_no_duplicate_streaming_paths(self):
        """ISC-A-1: Anti: No duplicate messages appear in Mythic UI output.

        Callbacks stream AI/Tool messages during execution.
        _process_stream_event must NOT also stream AI/Tool messages.
        """
        process_src = get_method_source("_process_stream_event")
        # Verify _process_stream_event only handles HumanMessages
        # It should NOT stream AIMessage or ToolMessage
        assert "AIMessage" not in process_src or "isinstance(msg, HumanMessage)" in process_src, (
            "DUPLICATE RISK: _process_stream_event must only stream HumanMessages. "
            "AI/Tool messages are already streamed by MessageCaptureCallback."
        )

    def test_isca2_session_management_preserved(self):
        """ISC-A-2: Anti: Existing multi-turn conversation persistence not broken."""
        # Verify session management functions still exist and are used
        assert "add_session" in CHAT_SOURCE, "chat.py must use add_session"
        assert "get_session" in CHAT_SOURCE, "chat.py must use get_session"
        assert "remove_session" in CHAT_SOURCE, "chat.py must use remove_session"

        # Verify model.py still exports session management
        assert "add_session" in MODEL_SOURCE, "model.py must define add_session"
        assert "get_session" in MODEL_SOURCE, "model.py must define get_session"

    def test_isca3_no_attribute_errors_on_format(self):
        """ISC-A-3: Anti: No AttributeError from uninitialized instance variables.

        Check that all self.X accessed in _format_message_for_streaming are
        either set in __init__ or always set before the method is called.
        """
        fmt_src = get_method_source("_format_message_for_streaming")

        # Extract all self.X attribute accesses
        cls = find_class(MODEL_AST, "Model")
        fmt_method = find_method(cls, "_format_message_for_streaming")
        if not fmt_method:
            pytest.fail("_format_message_for_streaming not found")

        accessed_attrs = set()
        for node in ast.walk(fmt_method):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "self" and isinstance(node.ctx, ast.Load):
                    accessed_attrs.add(node.attr)

        # Get all attributes initialized in __init__
        init_method = find_method(cls, "__init__")
        init_src = ast.get_source_segment(MODEL_SOURCE, init_method) or ""

        # Check each accessed attribute is initialized
        uninitialized = []
        for attr in accessed_attrs:
            if f"self.{attr}" not in init_src:
                uninitialized.append(attr)

        if uninitialized:
            pytest.fail(
                f"UNINITIALIZED ATTRIBUTES accessed in _format_message_for_streaming: "
                f"{uninitialized}. These must be initialized in __init__() to prevent "
                f"AttributeError if the method is called outside the invoke() flow."
            )


# ============================================================================
# Additional Regression Tests
# ============================================================================

class TestRegressionChecks:
    """Additional regression tests for streaming implementation correctness."""

    def test_callback_receives_streaming_functions(self):
        """Verify MessageCaptureCallback is instantiated with stream_func and format_func."""
        # Find where callback is created in model.py
        assert "stream_func=self._stream_message_to_mythic" in MODEL_SOURCE, (
            "MessageCaptureCallback must receive _stream_message_to_mythic as stream_func"
        )
        assert "format_func=self._format_message_for_streaming" in MODEL_SOURCE, (
            "MessageCaptureCallback must receive _format_message_for_streaming as format_func"
        )

    def test_astream_default_mode_is_updates(self):
        """Verify LangGraph StateGraph compiles with stream_mode='updates' (default).

        This is critical because the code's state update logic (extend, not replace)
        only works correctly with 'updates' mode (incremental deltas).
        With 'values' mode (full state), extend would duplicate everything.
        """
        # Check langgraph source for default stream_mode
        state_graph_py = SAGE_ROOT / ".." / ".." / ".venv" / "lib" / "python3.13" / \
            "site-packages" / "langgraph" / "graph" / "state.py"
        if state_graph_py.exists():
            state_graph_source = state_graph_py.read_text()
            assert 'stream_mode="updates"' in state_graph_source, (
                "LangGraph StateGraph must default to stream_mode='updates'. "
                "If this changes, the entire state update logic in invoke() breaks."
            )
        else:
            pytest.skip("LangGraph source not found at expected path")

    def test_invoke_signature_has_is_interactive_default(self):
        """Verify invoke() has is_interactive parameter with default value."""
        cls = find_class(MODEL_AST, "Model")
        invoke_method = find_method(cls, "invoke")
        assert invoke_method, "invoke method must exist"

        # Check for is_interactive parameter with default
        params = invoke_method.args
        all_args = params.args  # includes 'self'
        has_is_interactive = False

        # params.defaults are right-aligned with args (excluding self)
        # e.g., args=[self, prompt, is_interactive], defaults=[False]
        # means is_interactive has default False, prompt has no default
        non_self_args = all_args[1:]  # skip 'self'
        num_defaults = len(params.defaults)
        # Defaults align to the LAST N args
        for i, arg in enumerate(non_self_args):
            if arg.arg == "is_interactive":
                has_is_interactive = True
                # Check if this arg has a default
                default_index = i - (len(non_self_args) - num_defaults)
                if default_index >= 0:
                    default = params.defaults[default_index]
                    assert isinstance(default, ast.Constant) and default.value is False, (
                        "is_interactive must default to False"
                    )

        # Also check keyword-only args
        for arg, default in zip(params.kwonlyargs, params.kw_defaults):
            if arg.arg == "is_interactive":
                has_is_interactive = True

        assert has_is_interactive, "invoke() must have is_interactive parameter"

    def test_extract_new_messages_checks_all_channels(self):
        """Verify _extract_new_messages_from_event checks all 5 agent channels."""
        src = get_method_source("_extract_new_messages_from_event")
        assert src, "_extract_new_messages_from_event must exist"
        for channel in [
            "supervisor_messages",
            "generalist_messages",
            "mythic_operator_messages",
            "mythic_payload_messages",
            "mcp_manager_messages",
        ]:
            assert channel in src, f"Must check {channel} in event extraction"

    def test_stream_message_handles_rpc_failure(self):
        """Verify _stream_message_to_mythic handles RPC failures gracefully."""
        src = get_method_source("_stream_message_to_mythic")
        assert "except" in src, "Must have exception handling"
        assert "False" in src, "Must return False on failure"
        assert "logger.error" in src, "Must log errors"

    def test_chat_py_no_longer_uses_llm_resp_for_output(self):
        """Verify chat.py doesn't use invoke()'s return value for RPC output.

        In the streaming branch, invoke() returns '' and all output is already
        streamed. chat.py should NOT send the return value as a response.
        """
        # The old pattern was: llm_resp = await llm.invoke(prompt)
        #                      SendMythicRPCResponseCreate(..., llm_resp.encode())
        # New pattern: await llm.invoke(prompt, is_interactive=...)
        #              (no response variable used for output)

        # Check that invoke result is not being sent via RPC
        # Old code: llm_resp = await llm.invoke(prompt)
        #           llm_resp += "\n👤> "
        assert "llm_resp" not in CHAT_SOURCE or "llm_resp.encode" not in CHAT_SOURCE, (
            "chat.py must NOT send invoke() return value as RPC output. "
            "All output is streamed during invoke(). The return value is empty string."
        )


# ============================================================================
# Summary Test: Overall Feature Completeness
# ============================================================================

class TestFeatureCompleteness:
    """High-level tests that the streaming feature is structurally complete."""

    def test_all_streaming_methods_exist(self):
        """Verify all required streaming methods exist on Model class."""
        cls = find_class(MODEL_AST, "Model")
        required_methods = [
            "_stream_message_to_mythic",
            "_process_stream_event",
            "_extract_new_messages_from_event",
            "_format_message_for_streaming",
        ]
        for method_name in required_methods:
            method = find_method(cls, method_name)
            assert method is not None, f"Model.{method_name}() must exist"

    def test_all_streaming_methods_are_async_where_needed(self):
        """Verify async methods are properly async."""
        cls = find_class(MODEL_AST, "Model")
        # These must be async (they await RPC calls)
        async_required = [
            "_stream_message_to_mythic",
            "_process_stream_event",
            "_extract_new_messages_from_event",
            "invoke",
        ]
        for method_name in async_required:
            method = find_method(cls, method_name)
            assert method is not None, f"{method_name} must exist"
            assert isinstance(method, ast.AsyncFunctionDef), (
                f"{method_name} must be async"
            )

        # This can be sync (pure formatting)
        fmt = find_method(cls, "_format_message_for_streaming")
        assert fmt is not None
        assert isinstance(fmt, ast.FunctionDef), (
            "_format_message_for_streaming should be sync (pure formatting)"
        )

    def test_old_batch_output_method_removed(self):
        """Verify _generate_mythic_output (old batch method) is removed."""
        cls = find_class(MODEL_AST, "Model")
        old_method = find_method(cls, "_generate_mythic_output")
        assert old_method is None, (
            "_generate_mythic_output (old batch output) should be removed. "
            "Streaming replaces it entirely."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
