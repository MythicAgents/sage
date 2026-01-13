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
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.captured_messages: list[AnyMessage] = []
        self._tool_call_to_name: dict[str, str] = {}  # Map tool_call_id to tool name

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
        """Capture AIMessage after each LLM call."""
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
        """Capture ToolMessage after each tool execution."""
        try:
            # The output might be a ToolMessage or raw output
            if isinstance(output, ToolMessage):
                output.name = output.name or self._tool_call_to_name.get(output.tool_call_id, "unknown_tool")
                self.captured_messages.append(output)
                logger.debug(f"📨 [Callback:{self.agent_name}] Captured ToolMessage: "
                           f"tool={output.name}, tool_call_id={output.tool_call_id}")
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
        self.mythic_client = None
        self.tool_manager = None
        self._shown_messages = set()  # Track shown message IDs for deduplication
        self._message_seq = 1  # Sequence counter for message ordering (starts at 1, 0 reserved for system)
        self._current_turn_prompt_seq = -1  # Track current turn's user prompt (skip in output, Mythic echoes it)
        self._is_first_turn = True  # Track if this is the first turn (show user prompt only on Turn 1)
        logger.debug(f"🆕 Model.__init__: _is_first_turn initialized to {self._is_first_turn}")
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
            return init_chat_model(model_provider=self.provider, model=self.model, region=region, aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key, aws_session_token=aws_session_token)
        elif cfg is not None and cfg.get("api_key"):
            if cfg.get("base_url"):
                logger.debug(f"Initializing model with provider={self.provider}, model={self.model}, base_url={cfg.get('base_url')}")
                return init_chat_model(model_provider=self.provider, model=self.model, api_key=cfg.get("api_key"), base_url=cfg.get("base_url"))
            else:
                logger.debug(f"Initializing model with provider={self.provider}, model={self.model} and api_key")
                return init_chat_model(model_provider=self.provider, model=self.model, api_key=cfg.get("api_key"))
        else:
            logger.debug(f"Initializing model with provider={self.provider}, model={self.model} and no api_key")
            return init_chat_model(model_provider=self.provider, model=self.model)
        
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
            callback_handler = MessageCaptureCallback(agent_name=node_name)

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

            result = await agent_runnable.ainvoke({"messages": channel}, invoke_config)
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
            for msg in returned_messages:
                is_duplicate = False
                if isinstance(msg, AIMessage):
                    tool_calls = getattr(msg, 'tool_calls', []) or []
                    if tool_calls:
                        # Check if all tool_call IDs are already seen
                        msg_tc_ids = {tc.get('id') for tc in tool_calls if tc.get('id')}
                        if msg_tc_ids and msg_tc_ids.issubset(seen_tool_call_ids):
                            is_duplicate = True
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

            When responding to the user:
            - Summarize the progress and results of all agents.
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

    def _generate_mythic_output(self, resp, skip_counter=False) -> str:
        """
        Generate a string representation of the model's messages for Mythic output.
        :param resp: The response containing messages
        :param skip_counter: If True, show all messages regardless of shown tracking (used for recursion recovery)
        :return: A string containing the messages formatted for Mythic.
        """

        return_message = ""
        logger.debug(f"🎯 _generate_mythic_output: _is_first_turn={self._is_first_turn}, _current_turn_prompt_seq={self._current_turn_prompt_seq}")

        # check that the messages key is in the resp or throw an error
        if "messages" not in resp:
            raise ValueError("No messages found in the response from the graph invocation.")

        # Sort by sequence number for chronological order
        messages = sorted(resp["messages"], key=lambda m: _get_seq(m))

        # Track the current active agent by looking at handoff tool messages
        current_agent = "Supervisor"  # Start with supervisor

        logger.info(f"_generate_mythic_output: Processing {len(messages)} total messages, skip_counter={skip_counter}, already_shown={len(self._shown_messages)}")

        for i, message in enumerate(messages):
            # Use message ID for deduplication - skip messages already shown
            mid = _msg_id(message)
            if not skip_counter and mid in self._shown_messages:
                continue
            self._shown_messages.add(mid)

            if isinstance(message, HumanMessage):
                # Skip empty HumanMessages (can happen from framework artifacts)
                content = str(message.content).strip() if message.content else ""
                if not content:
                    continue

                # Check if this is a delegated task (not real user input)
                delegated_to = message.additional_kwargs.get("_delegated_to")
                msg_seq = _get_seq(message)

                if delegated_to:
                    # Delegated tasks always shown with special icon
                    return_message += f"📋[Task → {delegated_to}]> {content}\n"
                elif not self._is_first_turn and msg_seq == self._current_turn_prompt_seq:
                    # Skip current turn's user prompt on Turn 2+ (Mythic UI already echoes it)
                    logger.debug(f"Skipping current turn's user prompt (seq={msg_seq}, _is_first_turn={self._is_first_turn}): {content[:50]}...")
                    continue
                else:
                    # Regular user prompt (show on Turn 1, or for historical context)
                    logger.debug(f"Showing user prompt (seq={msg_seq}, _is_first_turn={self._is_first_turn}): {content[:50]}...")
                    return_message += f"👤> {content}\n"
            elif isinstance(message, AIMessage):
                is_last_message = i == len(messages) - 1

                # Debug: log message attributes to see what's available
                agent_name = getattr(message, 'name', None) or current_agent
                tool_calls = getattr(message, 'tool_calls', None) or []
                has_tool_calls = len(tool_calls) > 0

                # Extract text content for analysis
                text_content = ""
                if isinstance(message.content, str):
                    text_content = message.content.strip()
                elif isinstance(message.content, list):
                    for item in message.content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_content += item.get("text", "").strip() + " "
                    text_content = text_content.strip()

                # Determine what to show based on verbose mode
                show_tool_calls = self.verbose

                # Non-verbose filtering for AI messages:
                # 1. Skip "[Agent completed task]" type messages (framework artifacts)
                # 2. Skip intermediate "thinking" messages (messages that only precede tool calls)
                # 3. Only show substantive final responses
                # Use semantic flag (_is_completion_header) instead of string matching for robustness
                is_completion_header = message.additional_kwargs.get("_is_completion_header", False)
                is_thinking_before_tool = has_tool_calls and text_content  # Has both text and tools (intermediate)

                # In non-verbose mode, skip:
                # - Completion header messages (framework artifacts)
                # - Messages that are just tool call invocations with narration
                skip_in_non_verbose = not self.verbose and (is_completion_header or is_thinking_before_tool)

                logger.info(f"📝 AIMessage #{i}: is_last={is_last_message}, verbose={self.verbose}, "
                           f"agent={agent_name}, has_text={bool(text_content)}, "
                           f"has_tool_calls={has_tool_calls}, is_completion_header={is_completion_header}, "
                           f"skip_in_non_verbose={skip_in_non_verbose}")
                force_flush_all_handlers()

                if skip_in_non_verbose:
                    logger.debug(f"  ⏭️  Skipping AI message in non-verbose mode: completion_header={is_completion_header}, thinking_before_tool={is_thinking_before_tool}")
                    # Still process tool calls if verbose
                    pass
                else:
                    # Show text content
                    if text_content:
                        return_message += f"🤖[{agent_name}]> {text_content}\n"
                    elif not has_tool_calls and is_last_message:
                        logger.warning(f"Last message #{i} has no content and no tool_calls - skipping display")

                # Display tool_calls ONLY when verbose mode is ON
                if show_tool_calls and has_tool_calls:
                    logger.info(f"  ➡️  Processing {len(tool_calls)} tool_calls for message #{i}")
                    for tool_call in tool_calls:
                        tool_name = tool_call.get('name', 'unknown')
                        tool_id = tool_call.get('id', 'unknown')
                        tool_args = tool_call.get('args', {})
                        logger.info(f"    ✅ Tool call: name={tool_name}, id={tool_id}")
                        return_message += f"🛠️[{agent_name}:{tool_id}]> Tool Request: '{tool_name}', Args: '{tool_args}'\n"
                    force_flush_all_handlers()
                elif not show_tool_calls and has_tool_calls:
                    logger.debug(f"  ⏭️  Skipping {len(tool_calls)} tool_calls (verbose=False)")
            elif isinstance(message, SystemMessage):
                pass
            elif isinstance(message, ToolMessage):
                # Display tool response BEFORE updating current_agent for handoffs
                # This ensures the calling agent (e.g., Supervisor) is shown, not the target
                if self.verbose:
                    return_message += f"🔧[{current_agent}:{message.tool_call_id}]> Tool Response: {message.content}\n"

                # Check if this is a handoff tool message to track which agent is active
                # Update AFTER display so the next messages use the new agent
                if message.name and message.name.startswith("transfer_to_"):
                    target_agent = message.name.replace("transfer_to_", "")
                    current_agent = target_agent
                    logger.debug(f"Agent handoff detected: now using {current_agent}")
            else:
                return_message += f"❓> Unknown message type: {type(message)} with content: {message}\n"

        return return_message

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
            Remove orphan ToolMessages (tool_result) whose tool_call_id was never
            introduced by a preceding AIMessage.tool_calls in this sequence.
            """
            seen_tool_use_ids = set()
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
                elif isinstance(m, (HumanMessage, SystemMessage)):
                    cleaned.append(m)
                # ignore other types silently
            return cleaned

    def _render_combined(self, messages):
        out = ""
        for m in messages:
            if isinstance(m, HumanMessage):
                out += f"👤> {m.content}\n"
            elif isinstance(m, AIMessage):
                out += f"🤖[{getattr(m,'name','Agent')}]> {m.content if isinstance(m.content,str) else ''}\n"
        return out
    
    async def invoke(self, prompt: str) -> str:
        """
        Invoke the model with a prompt and return the response.
        :param prompt: The prompt to send to the model.
        :return: The model's response as a string.
        """
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

        # Track the current turn's user prompt sequence so we can skip showing it
        # in output (Mythic UI already echoes the user's input for interactive tasks)
        self._current_turn_prompt_seq = user_msg_seq

        try:
            # Use default recursion limit - RemainingSteps will handle graceful termination
            logger.debug(f"🚀 Before ainvoke: self.state._message_seq={self.state.get('_message_seq')}, Model._message_seq={self._message_seq}")
            resp = await self.graph.ainvoke(self.state, {"configurable": {"thread_id": f"{self.agent_task_id}-{self.task_id}"}, "recursion_limit": 25})
            logger.debug(f"📥 After ainvoke: resp._message_seq={resp.get('_message_seq')}")

            # Merge updated channel values back into self.state
            for ch in [
                "supervisor_messages",
                "generalist_messages",
                "mythic_operator_messages",
                "mythic_payload_messages",
                "mcp_manager_messages",
            ]:
                if ch in resp:
                    self.state[ch] = resp[ch]

            # CRITICAL: Sync sequence counter back from graph state
            # Without this, the Model's counter gets out of sync with messages created
            # during graph execution (e.g., in handoff tools), causing sequence collisions
            if "_message_seq" in resp:
                self._message_seq = resp["_message_seq"]
                self.state["_message_seq"] = resp["_message_seq"]
                logger.debug(f"📊 Synced _message_seq from graph: {self._message_seq}")

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
                result = self._generate_mythic_output(synthetic_resp, skip_counter=False)
                self._is_first_turn = False
                return result
            elif synthetic_resp.get("recursion_handback", False):
                logger.info("Recursion handback received from specialist agent")
                result = self._generate_mythic_output(synthetic_resp, skip_counter=False)
                self._is_first_turn = False
                return result
        except GraphRecursionError as e:
            # Catch recursion limit error and return progress made so far
            logger.warning(f"Recursion limit hit: {e}")

            # First, log what's currently in self.state
            current_msg_count = len(self.state.get("messages", []))
            logger.info(f"Current state has {current_msg_count} messages before checkpoint collection")
            logger.info(f"Already shown {len(self._shown_messages)} messages")

            # Get the latest state from the checkpoint to show all progress
            # The graph execution stopped, but checkpointer has saved all messages
            thread_id = f"{self.agent_task_id}-{self.task_id}"
            config = RunnableConfig(configurable={"thread_id": thread_id})

            # Collect all messages from parent and nested agent checkpoints
            all_messages = []
            checkpoint_count = 0
            try:
                # Get all checkpoints for this thread (including nested agents)
                # LangGraph stores nested agent states with namespace prefixes
                async for checkpoint_tuple in self.memory.alist(config, limit=200):
                    checkpoint_count += 1
                    if checkpoint_tuple and checkpoint_tuple.checkpoint:
                        ns = checkpoint_tuple.metadata.get("checkpoint_ns", "")
                        checkpoint_id = checkpoint_tuple.checkpoint.get("id", "unknown")

                        logger.info(f"Checkpoint {checkpoint_count}: namespace='{ns}', id={checkpoint_id}")

                        nested_state = checkpoint_tuple.checkpoint.get("channel_values", {})
                        if "messages" in nested_state:
                            nested_messages = nested_state["messages"]
                            logger.info(f"  Found {len(nested_messages)} messages in this checkpoint")

                            # Log message types for debugging
                            msg_types = [type(m).__name__ for m in nested_messages]
                            logger.info(f"  Message types: {msg_types}")

                            # Add messages that aren't already in all_messages (avoid duplicates)
                            for msg in nested_messages:
                                if msg not in all_messages:
                                    all_messages.append(msg)

                logger.info(f"Total checkpoints examined: {checkpoint_count}")
                logger.info(f"Total unique messages collected: {len(all_messages)}")

                if all_messages:
                    self.state["messages"] = all_messages
                    logger.info(f"Updated state with {len(all_messages)} messages from checkpoints")
                else:
                    logger.warning("No messages found in any checkpoint!")

            except Exception as checkpoint_error:
                logger.warning(f"Could not retrieve checkpoint after recursion limit: {checkpoint_error}")

            # Ask the LLM to summarize the progress made so far
            # Include recent context from the conversation for the summary
            recent_messages = recent_messages = self.state["messages"][-10:] if len(self.state["messages"]) > 10 else self.state["messages"]
            summary_prompt = HumanMessage(content="""Based on the conversation so far, provide a brief summary of:
                1. What tasks have been completed
                2. What information has been gathered
                3. What still needs to be done

                Keep it concise (3-5 bullet points).""")

            # Create a summary request with context
            try:
                summary_messages_raw = recent_messages + [summary_prompt]
                # Build merged view of ALL agent channels for better context
                merged = []
                for ch in [
                    "supervisor_messages",
                    "generalist_messages",
                    "mythic_operator_messages",
                    "mythic_payload_messages",
                    "mcp_manager_messages",
                ]:
                    merged.extend(self.state.get(ch, []))
                if merged:  # prefer merged if it has data
                    summary_messages_raw = merged[-15:] + [summary_prompt]

                summary_messages = self._sanitize_messages(summary_messages_raw)

                # Ensure at least one SystemMessage (Anthropic Bedrock safer)
                if not any(isinstance(m, SystemMessage) for m in summary_messages):
                    summary_messages.insert(0, SystemMessage(content="You are a summarizer. Produce concise bullet points."))

                # Final guard: must not be empty
                if not summary_messages:
                    summary_messages = [SystemMessage(content="You are a summarizer."), summary_prompt]
                logger.debug(f"Summary invoke sending {len(summary_messages)} messages: {[type(m).__name__ for m in summary_messages]}")
                summary_resp = await self.llm.ainvoke(summary_messages)
                summary_text = summary_resp.content if hasattr(summary_resp, 'content') else str(summary_resp)
            except Exception as summary_error:
                logger.warning(f"Could not generate summary: {summary_error}")
                summary_text = "Multiple reconnaissance and information gathering tasks were in progress."

            # Create continuation message with LLM-generated summary
            continuation_message = AIMessage(content=f"""🔄 **Recursion Limit Reached**

**Progress Summary:**
{summary_text}

**Status:** Hit the system's iteration limit of 25 steps. All work has been preserved in the conversation history.

**Your Options:**
• Reply **"continue"** to increase the limit and keep going from where we left off
• Reply **"stop"** to end the current task
• Provide specific instructions to redirect the approach

**What would you like to do?**"""
            )

            # Add continuation message to state
            self.state["messages"].append(continuation_message)
            self.state["recursion_summary_requested"] = True

            # Return only NEW messages since last output (avoid duplicates)
            # Don't reset counter - it already tracks which messages have been shown
            resp = {"messages": self.state["messages"], "recursion_summary_requested": True}

            # Use skip_counter=False to respect the shown tracking and show only new messages
            # This prevents showing messages that were already displayed before recursion limit
            logger.info(f"Returning recursion summary, {len(self._shown_messages)} messages already shown")
            return self._generate_mythic_output(resp, skip_counter=False)

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

            # Return partial work + error
            resp = {"messages": all_messages}
            logger.info(f"Returning error with {len(all_messages)} messages of partial work")
            return self._generate_mythic_output(resp, skip_counter=False)

        # Generate output FIRST, then mark turn complete
        result = self._generate_mythic_output(synthetic_resp)
        # Mark first turn complete - subsequent turns won't show user prompt (Mythic echoes it)
        self._is_first_turn = False
        return result

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
            self.state["messages"].append(HumanMessage(content="Please continue with the previous task."))

            if self.graph:
                try:
                    resp = await self.graph.ainvoke(self.state, {
                        "configurable": {"thread_id": thread_id},
                        "recursion_limit": 50  # Increased limit for continuation
                    })

                    # Check again for recursion summary request
                    if resp.get("recursion_summary_requested", False):
                        return self._generate_mythic_output(resp)

                except GraphRecursionError as e:
                    # Hit recursion limit again even with increased limit
                    logger.warning(f"Recursion limit hit again: {e}")

                    # Get checkpoint state including nested agents
                    all_messages = []
                    try:
                        checkpoint = await self.memory.aget_tuple(config)
                        if checkpoint and checkpoint.checkpoint:
                            saved_state = checkpoint.checkpoint.get("channel_values", {})
                            if "messages" in saved_state:
                                all_messages.extend(saved_state["messages"])

                        # Get nested agent checkpoints too
                        async for checkpoint_tuple in self.memory.alist(config, limit=100):
                            if checkpoint_tuple and checkpoint_tuple.checkpoint:
                                ns = checkpoint_tuple.metadata.get("checkpoint_ns", "")
                                if ns and ("Mythic_Operator" in ns or "Mythic_Payload" in ns or "Generalist" in ns):
                                    nested_state = checkpoint_tuple.checkpoint.get("channel_values", {})
                                    if "messages" in nested_state:
                                        for msg in nested_state["messages"]:
                                            if msg not in all_messages:
                                                all_messages.append(msg)

                        if all_messages:
                            self.state["messages"] = all_messages
                    except Exception as checkpoint_error:
                        logger.warning(f"Could not retrieve checkpoint: {checkpoint_error}")

                    # Generate summary again
                    continuation_message = AIMessage(content="""🔄 **Recursion Limit Reached Again**

**Status:** Hit the increased iteration limit. The task appears to be very complex or open-ended.

**Your Options:**
• Reply **"continue"** to try again with an even higher limit
• Reply **"stop"** to end and review what's been done
• Provide more specific instructions to narrow the scope

**What would you like to do?**""")

                    self.state["messages"].append(continuation_message)
                    self.state["recursion_summary_requested"] = True
                    resp = {"messages": self.state["messages"], "recursion_summary_requested": True}

                    # Don't reset counter - show only new messages to avoid duplicates
                    logger.info(f"Recursion limit hit again, {len(self._shown_messages)} messages already shown")
                    return self._generate_mythic_output(resp, skip_counter=False)
            else:
                raise ValueError("No graph defined for the model.")

        elif response.lower().strip() in ["stop", "no", "end", "quit"]:
            # User wants to stop
            logger.info("User requested to stop the task")
            return "✅ Task stopped as requested. The session remains active for new tasks."

        else:
            # User provided new instructions or redirection
            logger.info("User provided new instructions for continuation")
            self.state["messages"].append(HumanMessage(content=response))

            if self.graph:
                try:
                    resp = await self.graph.ainvoke(self.state, {
                        "configurable": {"thread_id": thread_id},
                        "recursion_limit": 25  # Reset to default for new task direction
                    })

                    # Check for recursion summary request
                    if resp.get("recursion_summary_requested", False):
                        return self._generate_mythic_output(resp)

                except GraphRecursionError as e:
                    # Handle recursion error for new direction too
                    logger.warning(f"Recursion limit hit on redirect: {e}")

                    # Get checkpoint state including nested agents
                    all_messages = []
                    try:
                        checkpoint = await self.memory.aget_tuple(config)
                        if checkpoint and checkpoint.checkpoint:
                            saved_state = checkpoint.checkpoint.get("channel_values", {})
                            if "messages" in saved_state:
                                all_messages.extend(saved_state["messages"])

                        # Get nested agent checkpoints too
                        async for checkpoint_tuple in self.memory.alist(config, limit=100):
                            if checkpoint_tuple and checkpoint_tuple.checkpoint:
                                ns = checkpoint_tuple.metadata.get("checkpoint_ns", "")
                                if ns and ("Mythic_Operator" in ns or "Mythic_Payload" in ns or "Generalist" in ns):
                                    nested_state = checkpoint_tuple.checkpoint.get("channel_values", {})
                                    if "messages" in nested_state:
                                        for msg in nested_state["messages"]:
                                            if msg not in all_messages:
                                                all_messages.append(msg)

                        if all_messages:
                            self.state["messages"] = all_messages
                    except Exception as checkpoint_error:
                        logger.warning(f"Could not retrieve checkpoint: {checkpoint_error}")

                    continuation_message = AIMessage(content="🔄 Hit recursion limit again. Reply 'continue' to proceed or 'stop' to end.")
                    self.state["messages"].append(continuation_message)
                    self.state["recursion_summary_requested"] = True
                    resp = {"messages": self.state["messages"], "recursion_summary_requested": True}

                    # Don't reset counter - show only new messages to avoid duplicates
                    logger.info(f"Recursion limit hit again, {len(self._shown_messages)} messages already shown")
                    return self._generate_mythic_output(resp, skip_counter=False)
            else:
                raise ValueError("No graph defined for the model.")

        return self._generate_mythic_output(resp)

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