import json
import aiosqlite
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.message import add_messages
from langgraph.prebuilt import tools_condition, ToolNode
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables.config import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, BaseMessage
from mythic_container.logging import logger
from mythic_container.MythicRPC import MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate
from typing import Any
from .mcp import MCPManager
from .mythic import MythicAPIClient
from .tools import UnifiedToolManager


class State(MessagesState):
    count: int

class Model:
    """A class to represent a model with its configuration."""
    provider: str
    model: str
    verbose: bool
    mythic_client: MythicAPIClient | None
    tool_manager: UnifiedToolManager | None
    counter: int
    agent_task_id: int
    config: RunnableConfig | None
    llm: BaseChatModel | Any
    messages: list[BaseMessage]
    system_message: SystemMessage
    # memory: MemorySaver
    memory: AsyncSqliteSaver
    graph: StateGraph
    agent: CompiledStateGraph | None

    def __init__(self, provider: str, model: str, system_prompt: str, config: dict, agent_task_id: int):
        """
        Initialize the Model with provider, model, and configuration.
        :param provider: The model provider (e.g., 'anthropic', 'bedrock').
        :param model: The model string (e.g., 'claude-3-5-sonnet-latest').
        :param system_prompt: The system prompt to use for the model.
        :param config: A dictionary containing configuration options for the model {"configurable": {}}.
        """
        self.agent = None
        self.provider = provider
        self.model = model
        self.verbose = False
        self.mythic_client = None
        self.tool_manager = None
        self.counter = 0
        self.messages = []
        self.agent_task_id = agent_task_id
        self.graph = StateGraph(State)
        # self.memory = MemorySaver()
        db_path = "sage.db"  # Path to your SQLite database
        conn = aiosqlite.connect(db_path, check_same_thread=False)
        self.memory = AsyncSqliteSaver(conn)
        self.system_message = SystemMessage(content=system_prompt)
        if system_prompt:
            self.messages.insert(0, SystemMessage(content=system_prompt))
        if config:
            self.config = RunnableConfig(
                configurable={
                    k: v 
                    for k, v in config.get("configurable", {}).items()
                    if k not in ["thread_id"]  # Remove thread_id if present
                }
            )
        self.llm = init_chat_model(model_provider=provider,model=model,configurable_fields="any", api_key=config["configurable"]["api_key"] if config and config.get("configurable") else None)
        #if self.config:
        #        self.llm = self.llm.with_config(self.config["configurable"])

    async def with_tools(self, agent_task_id: str):
        """
        Bind all available tools from both Mythic and MCP to the model.
        :param agent_task_id: The agent task ID, a UUID, of the agent task interacting with the model.
        """
        # Initialize the unified tool manager
        self.tool_manager = UnifiedToolManager(MCPManager)
        
        # Initialize Mythic tools
        await self.tool_manager.initialize_mythic_tools(agent_task_id)
        
        # Get all tools (Mythic + MCP) as LangChain BaseTool instances
        all_tools = self.tool_manager.get_all_tools()
        
        # For backward compatibility, keep the mythic_client reference
        self.mythic_client = self.tool_manager.mythic_client
        
        # Bind tools to the LLM
        if all_tools:
            try:
                self.llm = self.llm.bind_tools(all_tools)
                logger.info(f"Bound {len(all_tools)} tools to model (Mythic: {len(self.tool_manager.get_mythic_tools())}, MCP: {len(self.tool_manager.get_mcp_tools())})")
            except AttributeError as e:
                logger.error(f"LLM does not support tool binding: {e}")
                logger.warning("Continuing without tool binding - tools will be available but not bound to LLM")
        else:
            logger.warning("No tools available to bind to model")

    def set_verbose(self, verbose: bool):
        """
        Set the verbosity of the model.
        :param verbose: If True, the model will print all User & AI messages.
        """
        self.verbose = verbose

    def _generate_mythic_output(self) -> str:
        """
        Generate a string representation of the model's messages for Mythic output.
        :return: A string containing the messages formatted for Mythic.
        """
        return_message = ""
        for i, message in enumerate(self.messages):
            if i < self.counter:
                continue
            if isinstance(message, HumanMessage):
                return_message += f"👤> {message.content}\n"
            elif isinstance(message, AIMessage):
                is_last_message = i == len(self.messages) - 1
                show_content = is_last_message or self.verbose
                
                if show_content:
                    if isinstance(message.content, str):
                        if message.content:
                            return_message += f"🤖> {message.content}\n"
                    elif isinstance(message.content, list):
                        return_message += self._process_ai_content_list(message.content)
            elif isinstance(message, SystemMessage):
                pass
            elif isinstance(message, ToolMessage):
                if self.verbose:
                    return_message += f"🛠️> Tool Call ID: '{message.tool_call_id}', Content: '{message.content}'\n"
            else:
                return_message += f"❓> Unknown message type: {type(message)} with content: {message}\n"
            
            self.counter += 1

        return return_message

    def _process_ai_content_list(self, content_list):
        """Helper method to process AI message content lists"""
        result = ""
        for m in content_list:
            if m.get("type") == "text":
                result += f"🤖> {m.get('text', '')}\n"
            elif m.get("type") == "tool_use":
                result += f"🛠️> Name: '{m.get('name', '')}', Arguments: '{m.get('input', '')}', ID: '{m.get('id', '')}'\n"
            else:
                result += f"❓> Unknown message type: {m.get('type', 'unknown')} with content: {m}\n"
        return result

    def _tool_node(self, state: MessagesState):
        return {"messages": [self.llm.invoke(state["messages"])]}

    def _assistant_node(self, state: MessagesState):
        if isinstance(self.config, dict) and "configurable" in self.config:
            config = RunnableConfig(
                    configurable={
                        k: v 
                        for k, v in self.config["configurable"].items()
                        if k not in ["thread_id"]  # Remove thread_id if present
                    }
                )
            return {"messages": [self.llm.invoke([self.system_message] + state["messages"], config=config)]}
        else:
            return {"messages": [self.llm.invoke([self.system_message] + state["messages"])]}
    
    async def _mythic_response_node(self, content: str):
        resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(self.agent_task_id, content.encode()))
        if not resp.Success:
            logger.error(f"Failed to send Mythic response: {resp.Error}")
        
    async def invoke(self, prompt: str) -> str:
        """
        Invoke the model with the given prompt.
        :param prompt: The user prompt to send to the model.
        :return: The model's response as a string.
        """
        logger.warning(f"Invoking LLM with provider: '{self.provider}', model: '{self.model}', prompt: '{prompt}'")

        # If this is a subsequent call, increment the counter
        if self.counter > 0:
            self.counter += 1
        if prompt:
            self.messages.append(HumanMessage(content=prompt))

        done = False
        while not done:
            try:
                if isinstance(self.config, dict) and "configurable" in self.config:
                    resp = self.llm.invoke(self.messages, self.config)
                else:
                    resp = self.llm.invoke(self.messages)
                self.messages.append(resp)
                logger.warning(f"🤖> Invoke response: {resp}")
            except Exception as e:
                logger.error(f"Error invoking model: {e}")
                # return f"❗> Error invoking model: {e}\n"
                raise Exception(f"Error invoking model: {e}")
            
            if not isinstance(resp, AIMessage):
                logger.error(f"Expected AIMessage, got {type(resp)}: {resp}")
                raise TypeError(f"Expected AIMessage, got {type(resp)}: {resp}")
            
            # See if the model is done
            if resp.response_metadata.get("finish_reason") == "stop": # OpenAI
                done = True
            elif resp.response_metadata.get("stop_reason") == "end_turn": # Anthropic
                done = True
            if done:
                logger.warning("Model indicated end of turn, stopping.")
                break
            tool_calls = []
            if hasattr(resp, "tool_calls") and resp.tool_calls:
                tool_calls = resp.tool_calls

            for tool_call in tool_calls:
                # Handle both dict and object tool calls
                if hasattr(tool_call, '__dict__'):
                    # If it's an object, get its attributes
                    tool_call_dict = tool_call.__dict__ if hasattr(tool_call, '__dict__') else {}
                    tool_name = getattr(tool_call, 'name', tool_call_dict.get('name', ''))
                    tool_args = getattr(tool_call, 'args', tool_call_dict.get('args', {}))
                    tool_id = getattr(tool_call, 'id', tool_call_dict.get('id', ''))
                    
                    logger.warning(f"🛠️> Object tool_call - Name: '{tool_name}', Args: '{tool_args}', ID: '{tool_id}'")
                    logger.warning(f"🛠️> Object type: {type(tool_call)}, dict: {tool_call_dict}")
                else:
                    # If it's a dict, access normally
                    tool_name = tool_call.get("name", "")
                    tool_args = (
                        tool_call.get("args") or 
                        tool_call.get("input") or 
                        tool_call.get("arguments") or 
                        tool_call.get("parameters") or
                        {}
                    )
                    tool_id = tool_call.get("id", "")
                    
                    logger.warning(f"🛠️> Dict tool_call - Name: '{tool_name}', Args: '{tool_args}', ID: '{tool_id}'")
                    logger.warning(f"🛠️> Full tool_call structure: {tool_call}")
                
                if self.verbose:
                    pass
                    #return_message += f"🛠️> Name: '{tool_name}', Arguments: '{tool_args}', ID: '{tool_id}'\n"
                
                # Use unified tool manager to execute tools
                if not self.tool_manager:
                    logger.error("Tool manager is not initialized, cannot execute tool.")
                    return "❗> Tool manager is not initialized, cannot execute tool.\n"
                
                # Find the tool by name
                tool = self.tool_manager.get_tool_by_name(tool_name)
                
                if not tool:
                    logger.error(f"Tool '{tool_name}' not found in available tools.")
                    result = f"Error: Tool '{tool_name}' not found"
                else:
                    try:
                        logger.warning(f"🛠️> Final tool_args for execution: {tool_args}")
                        
                        # Ensure tool_args is a dict
                        if not isinstance(tool_args, dict):
                            logger.error(f"Tool args is not a dict: {type(tool_args)} = {tool_args}")
                            tool_args = {}
                        
                        # Use the tool's ainvoke method instead of _arun to handle config properly
                        result = await tool.ainvoke(tool_args)
                    except Exception as e:
                        logger.error(f"Error executing tool '{tool_name}': {e}")
                        result = f"Error executing tool '{tool_name}': {str(e)}"
                
                tool_message = ToolMessage(
                    content=json.dumps(result) if isinstance(result, dict) else str(result),
                    tool_call_id=tool_id
                )
                self.messages.append(tool_message)
                logger.warning(f"🛠️> Result: {result}")
        
        return self._generate_mythic_output()

    async def invoke_graph(self, prompt: str):
        if not self.agent:
            self.graph.add_node("assistant", self._assistant_node)
            if self.tool_manager:
                self.graph.add_node("tools", ToolNode(self.tool_manager.get_all_tools()))
            self.graph.add_edge(START, "assistant")
            self.graph.add_conditional_edges("assistant", tools_condition)
            self.graph.add_edge("tools", "assistant")
            self.agent = self.graph.compile(checkpointer=self.memory)

        if isinstance(self.config, dict) and "configurable" in self.config:
            self.config["configurable"]["thread_id"] = str(self.agent_task_id)
        else:
            self.config = {"configurable": {"thread_id": str(self.agent_task_id)}}
        
        resp = await self.agent.ainvoke({"messages": [HumanMessage(content=prompt)]}, config=self.config)
        logger.warning(f"🤖> Invoke2 response - {self.agent_task_id}: {self.messages}")
        self.messages = resp["messages"]
        return self._generate_mythic_output()

sessions: dict[str, Model] = {}

async def get_session(session_id: str) -> Model|None:
    try:
        return sessions.get(session_id)
    except KeyError:
        logger.error(f"Session {session_id} not found.")
        return None

async def add_session(session_id: str, model: Model):
    logger.warning(f"Adding session {session_id} with model {model.provider} {model.model}")
    sessions[session_id] = model

async def remove_session(session_id: str):
    logger.warning(f"Removing session {session_id}")
    if session_id in sessions:
        del sessions[session_id]
    else:
        logger.error(f"Session {session_id} not found, cannot remove.")