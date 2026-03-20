import json
import aiosqlite
from langgraph.graph import StateGraph, START, MessagesState, END
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt import tools_condition
from langgraph.managed.is_last_step import RemainingSteps
from langchain.agents import create_agent
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
from typing import Any
from uuid import UUID
from .mythic_tools import MythicTools
from .tool_cache import ToolCache
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


class SageState(MessagesState):
    count: int
    remaining_steps: RemainingSteps
    recursion_summary_requested: bool
    recursion_handback: bool
    supervisor_messages: Annotated[list[AnyMessage], operator.add]
    generalist_messages: Annotated[list[AnyMessage], operator.add]
    mythic_operator_messages: Annotated[list[AnyMessage], operator.add]
    mythic_payload_messages: Annotated[list[AnyMessage], operator.add]
    mcp_manager_messages: Annotated[list[AnyMessage], operator.add]
    _message_seq: Annotated[int, _max_seq_reducer]  # Global sequence counter with max reducer


def _get_seq(msg: AnyMessage) -> int:
    """Get sequence number from message, defaulting to 0 for untagged messages."""
    return msg.additional_kwargs.get("_seq", 0)


def _tag_msg(msg: AnyMessage, seq: int) -> AnyMessage:
    """Tag a message with a sequence number for ordering."""
    if "_seq" not in msg.additional_kwargs:
        msg.additional_kwargs["_seq"] = seq
    return msg


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

    def clear(self):
        """Clear captured messages for reuse."""
        self.captured_messages = []
        self._tool_call_to_name = {}

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
                            # Filter: Suppress Supervisor text-only messages (no tool_calls).
                            # These are stray acknowledgments/continuations that leak into
                            # the UI after a specialist has already streamed its response.
                            # The Supervisor communicates intentionally via respond_to_user tool.
                            should_stream = True
                            if self.agent_name == "Supervisor":
                                has_tool_calls = bool(getattr(msg, 'tool_calls', None))
                                if not has_tool_calls:
                                    logger.debug(f"📨 [Callback:Supervisor] Suppressing text-only message from streaming (no tool_calls)")
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

    def __init__(self, provider: str, model: str, system_prompt: str, config: dict, task_id: int, agent_task_id: str):
        """
        Initialize the Model with provider, model, and configuration.
        :param provider: The model provider (e.g., 'anthropic', 'bedrock').
        :param model: The model string (e.g., 'claude-3-5-sonnet-latest').
        :param system_prompt: The system prompt to use for the model.
        :param config: A dictionary containing configuration options for the model {"configurable": {}}.
        """
        self.provider = provider
        self.model = model
        self.graph = None
        self.verbose = False
        self.is_interactive = False
        self.mythic_client = None
        self.tool_manager = None
        self._message_seq = 1  # Sequence counter for message ordering (starts at 1, 0 reserved for system)
        self.messages = []
        self.agent_task_id = agent_task_id
        self.task_id = task_id
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
            "_message_seq": self._message_seq,  # Shared sequence counter
            "supervisor_messages": [self.system_message],
            "generalist_messages": [],
            "mythic_operator_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
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
            resp = await SendMythicRPCResponseCreate(
                MythicRPCResponseCreateMessage(
                    self.task_id,
                    formatted_message.encode()
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
                # Stream AIMessages from respond_to_user tool (Supervisor's intentional responses).
                # These are NOT streamed by the callback (which suppresses Supervisor text-only messages)
                # but should be shown since the Supervisor explicitly chose to respond.
                # respond_to_user sets name="Supervisor" and has actual content.
                elif isinstance(msg, AIMessage) and getattr(msg, 'name', None) == "Supervisor":
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    if content.strip() and content.strip() != ".":
                        formatted = self._format_message_for_streaming(msg, agent_name="Supervisor")
                        if formatted:
                            await self._stream_message_to_mythic(formatted)

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
            "mcp_manager_messages"
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
            .add_node("MCP_Manager", self._mcp_manager_agent())
            .add_edge(START, "Supervisor")
            .add_edge("Generalist", "Supervisor")
            .add_edge("Mythic_Operator", "Supervisor")
            .add_edge("Mythic_Payload", "Supervisor")
            .add_edge("MCP_Manager", "Supervisor")
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
            result = await agent_runnable.ainvoke({"messages": sanitized_channel}, invoke_config)
            updated_channel = result.get("messages", channel)

            # With operator.add reducer, we only pass the NEW messages, not the full list
            returned_messages = updated_channel[original_channel_length:]

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
            for ch_key in ["supervisor_messages", "generalist_messages", "mythic_operator_messages", "mythic_payload_messages", "mcp_manager_messages"]:
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
                        # Create a header message to show which agent responded
                        # Mark with _is_completion_header for semantic filtering (vs string matching)
                        response_header = AIMessage(
                            content=f"[{node_name} completed task]",
                            name=node_name,
                            additional_kwargs={"_is_completion_header": True}
                        )
                        _tag_msg(response_header, self._next_seq())

                        # ALWAYS copy to Supervisor channel (only the NEW messages with operator.add)
                        update["supervisor_messages"] = [response_header] + substantive_messages
                        logger.info(f"✅ Copied {len(substantive_messages)} substantive messages from {node_name} to Supervisor channel")

                        # ALSO copy to calling agent channel if this was a worker-to-worker handoff
                        calling_agent = state.get("_last_calling_agent")
                        if calling_agent and calling_agent != "Supervisor":
                            channel_map = {
                                "Mythic_Operator": "mythic_operator_messages",
                                "Mythic_Payload": "mythic_payload_messages",
                                "Generalist": "generalist_messages",
                                "MCP_Manager": "mcp_manager_messages",
                            }
                            calling_agent_channel_key = channel_map.get(calling_agent)
                            if calling_agent_channel_key:
                                # With operator.add, only provide the new messages to append
                                update[calling_agent_channel_key] = [response_header] + substantive_messages
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
    
    # Agent definitions
    def _generalist_agent(self):
        name = "Generalist"
        prompt = """
        You are a Generalist Agent designed to handle a wide range of queries and tasks that do not fall under the expertise of specialized agents. 
        Your primary role is to provide accurate, clear, and concise responses to user queries, leveraging your broad knowledge base and reasoning capabilities.

        Responsibilities:
        - Answer general questions on a variety of topics, including but not limited to technology, science, history, and everyday life.
        - Provide explanations, summaries, or step-by-step instructions as needed.
        - Handle open-ended or creative queries with thoughtful and relevant responses.
        - Ensure clarity and professionalism in all interactions.

        Guidelines:
        - Always prioritize accuracy and relevance in your responses.
        - If a query is outside your scope, acknowledge it politely and suggest consulting a specialized agent or external resource.
        - Maintain a neutral and helpful tone in all communications.
        - Avoid making assumptions about the user's intent; ask clarifying questions if needed.

        Your goal is to assist the user effectively and efficiently, ensuring they leave the interaction with the information or guidance they need.
        """
        if not self.state["generalist_messages"]:
            self.state["generalist_messages"].append(SystemMessage(content=prompt.strip()))
        tools = []
        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for Mythic Operator Agent.")
        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            system_prompt=prompt,
        )
        return self._wrap_create_agent(agent, "generalist_messages", name)
    
    def _mythic_operator_agent(self):
        name = "Mythic_Operator" # Note: name must match the agent_name in _create_handoff_tool and cannot have spaces

        # Build cached commands section for pre-loaded payloads
        commands_text = ""
        if self._cached_commands:
            #logger.debug(f"🎯 Building Mythic_Operator agent with {len(self._cached_commands)} cached payload(s)")
            for payload_name, commands in self._cached_commands.items():
                commands_json = json.dumps(commands, indent=2) if isinstance(commands, (dict, list)) else str(commands)
                #logger.debug(f"Adding {len(commands_json)} chars of commands for '{payload_name}' to prompt")
                commands_text += f"\n### Available Commands for '{payload_name}' Payload:\n{commands_json}\n"
            commands_text += "\n**Note:** Use the get_all_commands_for_payloadtype tool if you need commands for other payload types or want to refresh this data.\n"
            #logger.debug(f"✅ Injected cached commands into Mythic_Operator prompt ({len(commands_text)} chars)")
        else:
            #logger.debug("⚠️  No cached commands available for Mythic_Operator agent prompt")
            pass

        prompt = f"""
        You are a Mythic Operator Agent responsible for handling prompts or tasks issued to Mythic from a human operator interacting with Mythic.
        Your primary role is to take actions within Mythic based on the operator's requests, ensuring that tasks are executed accurately and efficiently.

        Responsibilities:
        - Interpret and execute commands related to Mythic operations, such as managing callbacks, issuing Mythic tasks, and monitoring their status.
        - Provide updates on the status of operations and any relevant information to the operator.
        - Ensure that all actions taken within Mythic are logged and traceable.
        - **CRITICAL**: Monitor the remaining_steps value to prevent hitting recursion limits during complex operations.
        - **IMPORTANT**: You have access to the Mythic_Payload agent for creating new payloads when needed.

        **When to Delegate to Mythic_Payload Agent:**
        You should use the `transfer_to_Mythic_Payload` tool when:
        - Privilege escalation requires a new payload with elevated permissions
        - Lateral movement requires deploying a payload to a different host
        - The operator explicitly requests payload creation or modification
        - You need a specialized payload type that doesn't exist yet
        - Creating a service binary, DLL, or other executable for persistence or execution

        **Example Scenarios:**
        1. **Privilege Escalation**: "I need to escalate privileges on callback 13"
           - Check existing callbacks and determine approach
           - If you need a new service binary or exploit payload → delegate to Mythic_Payload
           - Once payload is created → use it in your privilege escalation commands

        2. **Lateral Movement**: "Move laterally to host 192.168.1.50"
           - Determine target OS and architecture
           - Delegate to Mythic_Payload to create appropriate payload for target
           - Once payload is ready → use WMI/PSExec/SSH commands to deploy it

        Guidelines:
        - Always confirm the operator's intent before executing any critical commands.
        - Use tools that provide the requested information from Mythic, such as get_all_active_callbacks for issuing commands to the Mythic agent with the issue_task_and_waitfor_task_output tool.
        - Maintain a clear and professional tone in all communications.
        - Prioritize accuracy and efficiency in executing tasks.
        - If a command is unclear or outside your scope, ask for clarification or suggest consulting another agent.
        - When delegating to Mythic_Payload, provide clear requirements: payload type, target OS/architecture, and intended use case.

        **CRITICAL: Check Existing Task History BEFORE Issuing New Commands:**
        Before issuing ANY new commands, you MUST follow this workflow:

        1. **Get Active Callbacks**: Use get_all_active_callbacks to identify available agents
        2. **Check Task History**: Use get_task_history_for_callback to see what commands have already been executed
        3. **Review Existing Output**: Use get_all_task_output_by_task_id to retrieve results from relevant past tasks
        4. **Analyze What You Have**: Determine if the requested information already exists in the task history
        5. **Issue New Tasks Only If Needed**: Only run new commands if the required information is missing or outdated

        **Why This Matters:**
        - Operators often have 40+ tasks already executed with valuable reconnaissance data
        - Re-running the same commands wastes time and creates noise
        - Task history contains the answers to most questions - check it FIRST
        - Always prefer retrieving existing data over generating new tasks

        **Example Workflow for "Do host-based recon":**
        1. Get active callbacks → Identify callback #5 (Merlin agent)
        2. Get task history for callback #5 → See tasks: whoami, hostname, ps, ifconfig already executed
        3. Get output for those task IDs → Retrieve the actual reconnaissance results
        4. Analyze the existing data → If complete, present it to the operator
        5. Only if gaps exist → Issue additional commands to fill in missing information

        **Recursion Limit Management:**
        - You have access to a `remaining_steps` value that shows how many more operations can be performed.
        - **Before each major tool call sequence, check remaining_steps**.
        - When remaining_steps is 4 or fewer, you MUST use the `summarize_and_handback` tool instead of continuing.
        - This prevents hitting the recursion limit and allows the Supervisor to ask the user how to proceed.
        - In your summary, include:
          - What tasks you've completed so far
          - What information you've gathered
          - What still needs to be done
          - Any important findings or results

        **Work Prioritization:**
        - For complex multi-step tasks (like comprehensive reconnaissance), break them into phases
        - Complete the most critical information gathering first
        - If approaching recursion limit, prioritize getting essential results over comprehensive coverage

        **IMPORTANT**: When a command has a parameter type of "File" (e.g., "type": "File"), you must pass in the Mythic file UUID (not the filename).
        {commands_text}
        Your goal is to assist the human operator effectively while managing system resources responsibly.
        """
        if not self.state["mythic_operator_messages"]:
            self.state["mythic_operator_messages"].append(SystemMessage(content=prompt.strip()))
        # Tools
        if self.mythic_client is not None:
            mythic_tools = self.mythic_client.get_tools([
                "get_all_active_callbacks",
                "get_all_commands_for_payloadtype",
                "issue_task_and_waitfor_task_output",
                "get_task_history_for_callback",
                "get_all_task_output_by_task_id",
                "upload_file_by_file_uuid",
                "get_all_uploaded_files",
                "get_operations",
            ])
            # Add the handback tool for recursion limit management
            handback_tool = _create_summarize_handback_tool()

            # Add handoff to Mythic_Payload for payload creation needs
            transfer_to_payload = _create_handoff_tool(
                agent_name="Mythic_Payload",
                description="Delegate payload creation task to Mythic_Payload agent. Use when privilege escalation, lateral movement, or persistence requires a new payload."
            )

            tools = mythic_tools + [handback_tool, transfer_to_payload]
        else:
            raise ValueError("Mythic client not initialized for Mythic Operator Agent.")
        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for Mythic Operator Agent.")
        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            system_prompt=prompt,
        )
        return self._wrap_create_agent(agent, "mythic_operator_messages", name)

    def _mythic_payload_agent(self):
        name = "Mythic_Payload"

        # Build dynamic lists from cached data
        installed_payloads_text = ""
        if self._payload_names:
            installed_payloads_text = "\n".join([f"        - {payload}" for payload in self._payload_names])
        else:
            installed_payloads_text = "        - (No payload data available)"

        installed_c2_profiles_text = ""
        if self._c2_profiles:
            installed_c2_profiles_text = "\n".join([f"        - {profile['name']}: {profile['description']}" for profile in self._c2_profiles])
        else:
            installed_c2_profiles_text = "        - (No C2 profile data available)"

        prompt = f"""
        You are the Mythic Payload Agent, an AI/LLM-based assistant designed to help users **create or build** Mythic Payloads within the Mythic C2 framework. Always remember and clearly distinguish that Mythic agents refer to the software components or payload types in the Mythic C2 system (e.g., Apollo, Poseidon, Apfell, Merlin)—these are wildly different from AI/LLM agents like yourself, which are language models for conversational tasks.

        ### Core Responsibilities:
        - Your primary function is to guide users through creating Mythic Payloads. These are executable files (or other formats) that run on a target system to establish a command-and-control (C2) connection back to a Mythic server.
        - Each Mythic Payload is built from a specific Mythic agent (payload type), which has its own Docker-based build container and configuration options.
        - Key required information for building a payload includes:
        - **Mythic Agent (Payload Type)**: The specific agent to use, such as Apollo (.NET for Windows), Poseidon (Golang for Linux/macOS), Apfell (JXA for macOS), Thanatos (Rust for Linux/Windows), Medusa (Python cross-platform), Merlin (Windows, Linux, macOS, freebsd) or others. If unspecified, suggest common ones based on the target's needs.
        - **Target Operating System**: Must match the agent's supported OS (e.g., Windows, Linux, macOS). Agents like Apollo support Windows, Poseidon supports Linux/macOS, Merlin supports Windows/Linux/macOS/freebsd etc.
        - **C2 Profile**: The communication method, such as http, websocket, dns, discord, slack, or dynamic-http. Confirm that the chosen profile is supported by the selected agent (e.g., most agents support http and websocket, but check documentation for specifics like dns or p2p support).
        - Additional optional parameters may include: build options (e.g., encryption, sleep intervals), wrapper types (e.g., scarecrow_wrapper for evasion), or agent-specific features like dynamic loading, socks support, or p2p linking.
        - If the user's query lacks sufficient details (e.g., no OS, no C2 profile, or incompatible choices), do not proceed. Instead, respond politely asking for the missing information, and explain why it's needed (e.g., "To build a compatible executable, please specify the target OS and a supported C2 profile for the Apollo agent.").

        ### Response Guidelines:
        - **Payload Verification**: Only create payloads for installed Mythic agents with the `get_payload_names` tool. If the requested agent is not installed, inform the user and suggest alternatives.
        - ** C2 Profile Verification**: Use the `get_c2_profile_names` tool to list installed C2 profiles. If the requested profile is not available, inform the user and suggest alternatives.
        - **Step-by-Step Process**: When sufficient info is provided, outline the payload creation steps clearly, including any Mythic agent-specific configurations from the build container. Reference supported features like task queuing, opsec checks, or browser scripting if relevant.
        - **Validation**: Always validate compatibility (e.g., "Apollo supports Windows with http and websocket profiles").
        - **Documentation Reference**: Direct users to official docs for details: https://docs.mythic-c2.net/operational-pieces/payload-types. If needed, suggest checking agent repos at https://github.com/MythicAgents for source code and features.
        - **No Assumptions**: Do not assume details or create payloads without explicit user confirmation. If a query is ambiguous, clarify.
        - **Edge Cases**: For advanced features (e.g., wrappers like scarecrow_wrapper or AI-integrated agents like sage), explain limitations and requirements.
        - **Tone**: Be professional, helpful, and concise. Avoid jargon unless explaining it, and focus on operational safety.

        ### Currently Installed Mythic Agents (payloads):
        {installed_payloads_text}
        ### Currently Installed C2 profiles:
        {installed_c2_profiles_text}

        ### Common Mythic Agents for Reference (based on community and official sources; always verify latest via docs):
        - Apollo: Windows (.NET), supports http, websocket.
        - Poseidon: Linux/macOS (Golang), supports http, websocket, dns.
        - Merlin: Windows, Linux, macOS, freebsd (Golang), supports http.
        - Apfell: macOS (JXA), supports http.
        - Thanatos: Linux/Windows (Rust), supports http, websocket.
        - Medusa: Cross-platform (Python), supports multiple profiles.
        - Others: Kharon (evasion-focused), Xenon (C for Windows), etc.

        If a user attempts to confuse Mythic agents with AI agents, correct them immediately (e.g., "Mythic agents are C2 implants, not AI systems like me.").
        """
        if not self.state["mythic_payload_messages"]:
            self.state["mythic_payload_messages"].append(SystemMessage(content=prompt.strip()))
        # Tools
        if self.mythic_client:
            mythic_tools = self.mythic_client.get_tools([
                "get_payload_names",
                "create_payload",
                "get_all_payload_info",
                "get_c2_profiles_for_payload",
            ])
            # Add the handback tool for recursion limit management
            handback_tool = _create_summarize_handback_tool()
            tools = mythic_tools + [handback_tool]
        else:
            raise ValueError("Mythic client not initialized for Mythic Payload Agent.")
        
        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for Mythic Operator Agent.")
        
        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            system_prompt=prompt,
        )
        return self._wrap_create_agent(agent, "mythic_payload_messages", name)

    def _mcp_manager_agent(self):
        name = "MCP_Manager"

        # Build connected servers info for prompt
        servers_text = ""
        connected = MCPManager.get_connected_servers()
        if connected:
            summary = MCPManager.get_tools_summary()
            servers_text = f"\n**Currently Connected MCP Servers:** {len(connected)}\n"
            for server_name in connected:
                server_info = summary.get("server_summaries", {}).get(server_name, {})
                tool_count = server_info.get("tool_count", 0)
                tool_names = server_info.get("tool_names", [])
                tools_preview = ', '.join(tool_names[:5])
                if len(tool_names) > 5:
                    tools_preview += '...'
                servers_text += f"- {server_name}: {tool_count} tools ({tools_preview})\n"
        else:
            servers_text = "\n**No MCP servers currently connected.** Inform the user to use `mcp-connect` command first.\n"

        prompt = f"""
        You are an MCP (Model Context Protocol) Manager Agent responsible for interacting with external tools
        provided by connected MCP servers.

        MCP servers extend Sage's capabilities by providing specialized tools for tasks like:
        - Web fetching and API interactions
        - File system operations
        - Database queries
        - Custom integrations

        {servers_text}

        **Your Responsibilities:**
        - Execute MCP tool calls when delegated tasks that require MCP capabilities
        - Interpret tool results and provide clear summaries
        - Handle tool errors gracefully and suggest alternatives
        - If no MCP servers are connected, inform the user how to connect one using the `mcp-connect` command

        **Guidelines:**
        - Always check which tools are available before attempting to use them
        - Provide context about what each tool does when using it
        - If a tool call fails, explain the error and suggest next steps
        - Monitor remaining_steps and use summarize_and_handback when approaching limits (4 or fewer remaining)

        **Available MCP Tools:**
        Your tools come from connected MCP servers. Tool availability depends on which servers are connected.
        Use the tools naturally based on the task requirements.

        **Recursion Limit Management:**
        - You have access to a `remaining_steps` value that shows how many more operations can be performed.
        - When remaining_steps is 4 or fewer, you MUST use the `summarize_and_handback` tool instead of continuing.
        - In your summary, include what you've accomplished and what still needs to be done.
        """

        if not self.state["mcp_manager_messages"]:
            self.state["mcp_manager_messages"].append(SystemMessage(content=prompt.strip()))

        # Get MCP tools
        mcp_tools = MCPManager.get_all_tools()

        # Add handback tool for recursion limit management
        handback_tool = _create_summarize_handback_tool()

        tools = mcp_tools + [handback_tool]

        # Handle case when no MCP tools available
        if not mcp_tools:
            logger.warning("MCP_Manager agent initialized with no MCP tools available")

        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for MCP Manager Agent.")

        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            system_prompt=prompt,
        )
        return self._wrap_create_agent(agent, "mcp_manager_messages", name)

    def _supervisor_agent(self):
        name = "Supervisor"
        prompt = """
            You are a Supervisor Agent responsible for managing and coordinating multiple specialized agents.
            Your primary role is to ensure that tasks are delegated effectively, progress is monitored, and results are integrated seamlessly.
            You have access to the following agents, each with their own expertise:

            1. **Generalist Agent**: Handles general inquiries and tasks that do not fit for other agents.
            2. **Mythic Operator Agent**: Handles ALL Mythic C2 operations including callbacks, agents, tasks, files, and reconnaissance. Has native tools for get_all_active_callbacks, issue_task, get_task_history, etc.
            3. **Mythic Payload Agent**: Helps create Mythic payloads within the C2 framework.
            4. **MCP Manager Agent**: Handles tasks requiring EXTERNAL tools from connected MCP servers (web fetching, external APIs, third-party integrations). Only use for capabilities NOT provided by other agents.

            **CRITICAL: Agent Routing Priority:**
            Always prefer built-in agents over MCP_Manager when they have relevant capabilities:
            - Callbacks, agents, tasks, commands, files in Mythic → **Mythic_Operator** (NOT MCP_Manager)
            - Payload creation, C2 profiles, build options → **Mythic_Payload** (NOT MCP_Manager)
            - General questions, explanations, advice → **Generalist**
            - ONLY use MCP_Manager for external/third-party tools that other agents cannot handle

            Your responsibilities include:
            - Understanding the user's high-level goals and breaking them into smaller, manageable tasks.
            - Assigning tasks to the appropriate agent based on their expertise.
            - Monitoring the progress of each agent and ensuring timely completion of tasks.
            - Integrating the outputs from all agents into a cohesive response for the user.
            - Providing clear and concise updates to the user about the status of tasks.
            - **CRITICAL**: Monitoring the remaining_steps value to detect when approaching the recursion limit.

            **CRITICAL: Understanding User Input Context:**
            When you receive a new user message, carefully evaluate whether it is:

            1. **Task Continuation** (e.g., "continue", "keep going", "yes")
               - Look for the most recent Progress Handback or agent response in your message history
               - Extract what work was completed and what remains to be done
               - Generate a NEW handoff_instruction that tells the agent to continue from where it left off
               - Example: If Mythic_Operator reported "Completed enumeration, still need privilege escalation",
                 your instruction should be "Continue privilege escalation based on the enumeration results you gathered"

            2. **Task Redirection** (e.g., "Try using the payload agent to create X", "Instead do Y")
               - The user is issuing a DIFFERENT task that may supersede or replace the previous one
               - Select the appropriate agent based on this NEW task
               - Generate a handoff_instruction based on the NEW task objective, NOT the old one
               - Example: If user says "Try using the payload agent to create an apollo service binary" after
                 working on privilege escalation, delegate to Mythic_Payload with instruction
                 "Create an apollo service binary payload"

            3. **Clarification or Meta-comment** (e.g., "What's the status?", "Why did that fail?")
               - User is asking about current state, not requesting new work
               - Provide a summary based on recent agent outputs
               - Do NOT delegate unless explicitly requested

            **Key Rule:** Always base your handoff_instruction on the MOST RECENT user intent, not the original
            task from several messages ago. When continuing a task, incorporate context from the agent's
            progress summary into your instruction.

            **Recursion Limit Management:**
            - You have access to a `remaining_steps` value that shows how many more operations can be performed.
            - When remaining_steps is 3 or fewer, you MUST use the `request_continuation` tool.
            - **Important**: You may receive handbacks from specialist agents (like Mythic_Operator) when they approach recursion limits.
            - When you receive a handback (indicated by messages mentioning "Progress Handback"), you should:
              1. Review the progress summary provided by the specialist agent
              2. Use the `request_continuation` tool to ask the user how to proceed
              3. Include the specialist's findings in your summary to the user
            - This allows the user to decide whether to continue, stop, or redirect the task.

            **CRITICAL: Recognizing Task Completion:**
            When you see a message like "[AgentName completed task]" followed by the agent's results:
            1. **Check if the original user request has been fulfilled**
               - Did the agent provide the requested information/action?
               - Is there a concrete result (payload created, command executed, question answered)?
            2. **If YES - Task is complete:**
               - **USE THE `respond_to_user` TOOL** with a summary of what was accomplished
               - **DO NOT delegate again** - the task is done
               - Include relevant details from the agent's response (IDs, filenames, results)
               - Example: Call respond_to_user with "✅ Payload created successfully. UUID: abc-123, Filename: apollo.bin"
            3. **If NO - More work needed:**
               - Only then use transfer_to_* tools to delegate to another agent OR the same agent with refined instructions
               - Clearly explain what additional work is required

            **Common mistake to avoid:**
            ❌ BAD: Agent creates payload → You see "[Mythic_Payload completed task]" → You call transfer_to_Mythic_Payload again
            ✅ GOOD: Agent creates payload → You see "[Mythic_Payload completed task]" with payload details → You call respond_to_user with the results

            **Tool Selection Rules:**
            - Use `transfer_to_*` tools ONLY when you need an agent to DO work
            - Use `respond_to_user` tool when agents have FINISHED work and you're ready to tell the user
            - Use `request_continuation` tool only when approaching recursion limits

            When interacting with the agents:
            - Clearly specify the task, context, and expected output.
            - Use structured communication to ensure clarity and avoid misunderstandings.
            - Handle any errors or unexpected behavior by reassigning tasks or consulting other agents.
            - When using any transfer_to_* tool, ALWAYS supply a concise handoff_instruction telling the target agent exactly what to do next (no pronouns, be explicit).
            - **CRITICAL**: When user says "continue" after a handback, construct your handoff_instruction by combining:
              1. The original task goal
              2. What the agent already completed (from the handback summary)
              3. What still needs to be done (from "Remaining Tasks" in the handback)

            **CRITICAL: Streaming Output Context:**
            All specialist agent responses are streamed DIRECTLY to the user in real-time as they are generated.
            The user has ALREADY SEEN the specialist's full output by the time you receive control back.
            Therefore:
            - Do NOT repeat, summarize, paraphrase, or extend the specialist's response in your own text.
            - Do NOT continue lists, add options, or append to the specialist's output.
            - When a specialist has finished and you need to respond, use the `respond_to_user` tool.
            - If the specialist's response fully answers the user's question, call `respond_to_user` with a brief acknowledgment only.
            - Your direct text output (without a tool call) is also streamed — any stray text you generate will appear to the user as extra output after the specialist's response.

            When responding to the user:
            - Use the `respond_to_user` tool for all final responses.
            - Provide actionable insights or next steps based on the outputs of the agents.
            - Maintain a professional and concise tone.
            - Always check remaining_steps before delegating to other agents.
            - **If you see completion messages from agents with successful results, respond to the user instead of delegating again.**

            Always prioritize efficiency, accuracy, and clarity in your management and communication.
        """
        if not self.state["supervisor_messages"]:
            self.state["supervisor_messages"].append(SystemMessage(content=prompt.strip()))

        # Handoffs
        assign_to_generalist_agent = _create_handoff_tool(
            agent_name="Generalist",
            description="Assign task to Generalist for general questions, explanations, advice, and tasks that don't require Mythic operations or external tools.",
        )

        assign_to_mythic_operator_agent = _create_handoff_tool(
                agent_name="Mythic_Operator",
                description="Assign task to Mythic Operator for ALL Mythic C2 operations: callbacks, agents, tasks, commands, files, reconnaissance. ALWAYS use this for Mythic-related queries instead of MCP_Manager.",
            )

        assign_to_mythic_payload_agent = _create_handoff_tool(
                agent_name="Mythic_Payload",
                description="Assign task to Mythic Payload for creating Mythic payloads, configuring C2 profiles, and build options.",
            )

        assign_to_mcp_manager_agent = _create_handoff_tool(
                agent_name="MCP_Manager",
                description="Assign task to MCP Manager ONLY for external third-party tools from connected MCP servers (web fetching, external APIs, non-Mythic integrations). Do NOT use for Mythic operations - use Mythic_Operator instead.",
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
            assign_to_mcp_manager_agent,
            respond_to_user_tool,
            request_continuation_tool,
        ]

        llm = self._get_base_chat_model()
        if not llm:
            raise ValueError("Failed to initialize the BaseChatModel for Mythic Operator Agent.")
        agent = create_agent(
            model=llm,
            tools=tools,
            name=name,
            system_prompt=prompt,
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
                # Use ALL agent's channel messages + summary prompt for full context
                # CRITICAL: Use ALL messages, not just last 20, to avoid missing earlier tool results
                # Safety limit: cap at 150 messages to avoid token limits (should be plenty for 25-step recursion)
                max_messages = 150
                if len(channel_messages) > max_messages:
                    logger.warning(f"{agent_name} has {len(channel_messages)} messages, using last {max_messages} for summary")
                    summary_messages_raw = channel_messages[-max_messages:] + [summary_prompt]
                else:
                    summary_messages_raw = channel_messages + [summary_prompt]

                summary_messages = self._sanitize_messages(summary_messages_raw)

                logger.info(f"Summary context for {agent_name}: {len(summary_messages)} messages (from {len(channel_messages)} channel messages)")

                # DEBUG: Log the actual message content being passed to summary LLM
                logger.info(f"DEBUG: Dumping {agent_name} channel messages for summary generation:")
                for idx, msg in enumerate(summary_messages[:20]):  # First 20 messages for debugging
                    msg_type = type(msg).__name__
                    content_preview = str(msg.content)[:200] if hasattr(msg, 'content') else "N/A"
                    logger.info(f"  [{idx}] {msg_type}: {content_preview}...")
                if len(summary_messages) > 20:
                    logger.info(f"  ... ({len(summary_messages) - 20} more messages)")

                # Ensure system message
                if not any(isinstance(m, SystemMessage) for m in summary_messages):
                    summary_messages.insert(0, SystemMessage(content=f"You are {agent_name}, a specialized agent. Summarize YOUR work concisely."))

                if not summary_messages:
                    continue

                logger.debug(f"Invoking {agent_name} for summary with {len(summary_messages)} messages")

                # CRITICAL: Remove tool_calls from AIMessages for summary generation
                # Bedrock requires tool_calls to have corresponding tool_results immediately after
                # But for summarization, we only need the text content, not the tool calls
                # ALSO: Filter out messages with empty content (Bedrock rejects blank content)
                cleaned_summary_messages = []
                for msg in summary_messages:
                    if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
                        # Create a copy without tool_calls
                        msg_copy = msg.copy()
                        msg_copy.tool_calls = []

                        # Check if message has actual text content
                        has_content = False
                        if isinstance(msg_copy.content, str) and msg_copy.content.strip():
                            has_content = True
                        elif isinstance(msg_copy.content, list) and any(
                            block.get('type') == 'text' and block.get('text', '').strip()
                            for block in msg_copy.content if isinstance(block, dict)
                        ):
                            has_content = True

                        # Only append if message has actual content (not just tool calls)
                        if has_content:
                            cleaned_summary_messages.append(msg_copy)
                    else:
                        cleaned_summary_messages.append(msg)

                # Use LLM without tools for summary generation (Bedrock requires this)
                # Create a version of the LLM with no tool bindings
                llm_without_tools = self.llm
                if hasattr(self.llm, 'bind_tools'):
                    try:
                        llm_without_tools = self.llm.bind_tools([])
                    except Exception:
                        # If bind_tools fails, just use the original LLM
                        pass

                summary_resp = await llm_without_tools.ainvoke(cleaned_summary_messages)

                # DEBUG: Log the generated summary
                logger.info(f"DEBUG: {agent_name} generated summary: {summary_resp.content[:500]}...")
                summary_content = summary_resp.content if hasattr(summary_resp, 'content') else str(summary_resp)

                summaries.append(f"**{agent_name}:**\n{summary_content}")

            except Exception as e:
                logger.warning(f"Could not generate summary for {agent_name}: {e}")
                summaries.append(f"**{agent_name}:** Work in progress (summary generation failed)")

        if not summaries:
            return "No agent summaries available - work may have just started."

        return "\n\n".join(summaries)

    async def invoke(self, prompt: str, is_interactive: bool = False) -> str:
        """
        Invoke the model with a prompt and return the response.
        :param prompt: The prompt to send to the model.
        :param is_interactive: Whether this is an interactive task (subsequent turn in conversation).
        :return: The model's response as a string.
        """
        # Store for use in streaming formatter
        self.is_interactive = is_interactive
        if not self.graph:
            raise ValueError("No graph defined for the model. Ensure the model's initialize() method has been called.")
        logger.debug(f"Invoking LLM with provider: '{self.provider}', model: '{self.model}', prompt: '{prompt}'")

        # Ensure per-agent channels exist
        for ch in [
            "supervisor_messages",
            "generalist_messages",
            "mythic_operator_messages",
            "mythic_payload_messages",
            "mcp_manager_messages",
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

        try:
            # Use default recursion limit - RemainingSteps will handle graceful termination
            logger.debug(f"🚀 Before astream: self.state._message_seq={self.state.get('_message_seq')}, Model._message_seq={self._message_seq}")

            # Stream graph execution and process events incrementally
            async for event in self.graph.astream(
                self.state,
                {"configurable": {"thread_id": f"{self.agent_task_id}-{self.task_id}"}, "recursion_limit": 25}
            ):
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
                        "mcp_manager_messages",
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
                                self.state[ch].extend(state_update[ch])
                                new_len = len(self.state[ch])
                                if new_len > old_len:
                                    logger.debug(f"  Extended {ch}: {old_len} -> {new_len} messages")

            logger.debug(f"📥 After astream: self.state._message_seq={self.state.get('_message_seq')}")

            # Create resp variable for compatibility with downstream code
            resp = self.state

            # Merge all channels, deduplicate by message ID, and sort by sequence
            all_messages = []
            seen_ids = set()
            for ch in ["supervisor_messages", "generalist_messages", "mythic_operator_messages", "mythic_payload_messages", "mcp_manager_messages"]:
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
                      "mythic_payload_messages", "generalist_messages", "mcp_manager_messages"]:
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
                                            "mcp_manager_messages"]:
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
                              "generalist_messages", "mcp_manager_messages"]:
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
                                  "generalist_messages", "mcp_manager_messages"]:
                            if ch not in self.state:
                                self.state[ch] = []
                    else:
                        logger.info("Agent channels already have content from previous run - skipping rebuild to preserve state")
                        # Still need to ensure channels exist
                        for ch in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages",
                                  "generalist_messages", "mcp_manager_messages"]:
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
                            elif agent_name == "MCP_Manager" or delegated_to == "MCP_Manager":
                                if msg not in self.state["mcp_manager_messages"]:
                                    self.state["mcp_manager_messages"].append(msg)

                        # Log rebuilt channel sizes
                        for ch in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages",
                                  "generalist_messages", "mcp_manager_messages"]:
                            logger.info(f"Rebuilt {ch}: {len(self.state[ch])} messages")

                        # CRITICAL: Validate and fix message sequences for Bedrock compatibility
                        # Bedrock requires that every AIMessage with tool_calls is IMMEDIATELY
                        # followed by ToolMessage(s) with matching tool_call_ids
                        # After rebuilding channels, this requirement might be violated
                        logger.info("Validating message sequences for LLM provider compatibility...")
                        for ch in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages",
                                  "generalist_messages", "mcp_manager_messages"]:
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
                      "mythic_payload_messages", "generalist_messages", "mcp_manager_messages"]:
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

**Status:** Hit the system's iteration limit of 25 steps. All work and context have been preserved in each agent's conversation history.

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
                               "mythic_operator_messages", "mythic_payload_messages", "mcp_manager_messages"]:
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

        if response.lower().strip() in ["continue", "yes", "keep going"]:
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
                    # Stream continuation with increased recursion limit
                    async for event in self.graph.astream(
                        self.state,
                        {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
                    ):
                        await self._process_stream_event(event)

                        # Update state with new values from event (extend for lists, assign for scalars)
                        for node_name, state_update in event.items():
                            if node_name in ["__start__", "__end__"]:
                                continue
                            for ch in ["supervisor_messages", "generalist_messages", "mythic_operator_messages",
                                      "mythic_payload_messages", "mcp_manager_messages", "_message_seq"]:
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
                                                    "mcp_manager_messages", "_message_seq"]:
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

        elif response.lower().strip() in ["stop", "no", "end", "quit"]:
            # User wants to stop
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
                    # Stream new task direction with default recursion limit
                    async for event in self.graph.astream(
                        self.state,
                        {"configurable": {"thread_id": thread_id}, "recursion_limit": 25}
                    ):
                        await self._process_stream_event(event)

                        # Update state with new values from event (extend for lists, assign for scalars)
                        for node_name, state_update in event.items():
                            if node_name in ["__start__", "__end__"]:
                                continue
                            for ch in ["supervisor_messages", "generalist_messages", "mythic_operator_messages",
                                      "mythic_payload_messages", "mcp_manager_messages", "_message_seq"]:
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
                                                    "mcp_manager_messages", "_message_seq"]:
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

def _create_respond_to_user_tool():
    """
    Create a tool that allows the Supervisor to explicitly indicate it's done
    delegating and wants to respond directly to the user with final results.
    """
    @tool("respond_to_user")
    def respond_to_user(
        runtime: ToolRuntime,
        final_response: Annotated[str, "The final synthesized response to provide to the user"],
    ) -> Command:
        """Call this when the task is complete and you want to respond to the user with final results. DO NOT delegate again after calling this."""

        # Create a final AI message with the response
        response_message = AIMessage(
            content=final_response,
            name="Supervisor"
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

def _create_handoff_tool(*, agent_name: str, description: str | None = None):
    """
    Create a handoff tool to transfer control to another agent.
    https://docs.langchain.com/oss/python/langchain/multi-agent#handoffs
    https://langchain-ai.github.io/langgraph/how-tos/multi_agent/#handoffs

    :param agent_name: The name of the agent to transfer control to.
    :param description: An optional description for the tool.
    :return: A tool function that performs the handoff.
    """
    name = f"transfer_to_{agent_name}"
    description = description or f"Delegate a task to {agent_name}. Provide a clear instruction."

    channel_map = {
        "Supervisor": "supervisor_messages",
        "Generalist": "generalist_messages",
        "Mythic_Operator": "mythic_operator_messages",
        "Mythic_Payload": "mythic_payload_messages",
        "MCP_Manager": "mcp_manager_messages",
    }
    target_channel_key = channel_map.get(agent_name)

    @tool(name, description=description)
    def handoff_tool(
        runtime: ToolRuntime,
        handoff_instruction: Annotated[str, "Explicit task or question for the target agent"],
    ) -> Command:
        # Compute sequence from max of existing messages in all channels
        # This is more reliable than state._message_seq which may not persist across checkpoints
        max_seq = 0
        for ch_key in ["supervisor_messages", "generalist_messages", "mythic_operator_messages", "mythic_payload_messages", "mcp_manager_messages", "messages"]:
            for msg in runtime.state.get(ch_key, []):
                seq = _get_seq(msg)
                if seq > max_seq:
                    max_seq = seq
        current_seq = max_seq + 1
        logger.debug(f"🔄 Handoff to {agent_name}: computed next seq={current_seq} (max in channels was {max_seq})")

        # ToolMessage confirming delegation
        acknowledgment = ToolMessage(
            content=f"Delegated to {agent_name} with instruction: {handoff_instruction}",
            name=name,
            tool_call_id=runtime.tool_call_id,
        )
        _tag_msg(acknowledgment, current_seq)
        current_seq += 1

        # HumanMessage representing the actual task for the target agent
        # Mark as delegated so it displays differently from real user input
        injected_human = HumanMessage(content=handoff_instruction)
        injected_human.additional_kwargs["_delegated_to"] = agent_name
        _tag_msg(injected_human, current_seq)
        current_seq += 1

        # With operator.add reducer, only provide NEW messages to append
        update_state = {**runtime.state}
        update_state["messages"] = [acknowledgment, injected_human]
        update_state["_message_seq"] = current_seq  # Update sequence in state

        # Inject into target channel (only new messages with operator.add)
        if target_channel_key:
            update_state[target_channel_key] = [acknowledgment, injected_human]

        # CRITICAL: Track who is calling this agent so responses can be copied back
        # Store the calling agent's name in state for response routing
        # We need to detect the current agent from the message history
        current_agent = None
        for channel_name, channel_key in channel_map.items():
            if runtime.state.get(channel_key) and len(runtime.state.get(channel_key, [])) > 0:
                # Check if this channel has recent activity (last message is not too old)
                # This is a heuristic - the agent that just called a tool is the calling agent
                if channel_key in ["supervisor_messages", "mythic_operator_messages", "mythic_payload_messages", "generalist_messages", "mcp_manager_messages"]:
                    # Simple approach: assume the tool was called from whichever non-target channel exists
                    if channel_name != agent_name:
                        current_agent = channel_name
                        break

        # Store calling agent info for response routing
        update_state["_last_calling_agent"] = current_agent
        update_state["_last_target_agent"] = agent_name

        return Command(
            goto=agent_name,
            update=update_state,
            graph=Command.PARENT,
        )

    return handoff_tool

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

async def remove_session(session_id: str):
    logger.debug(f"Removing session {session_id}")
    if session_id in sessions:
        del sessions[session_id]
    else:
        logger.error(f"Session {session_id} not found, cannot remove.")