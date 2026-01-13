from langchain_core.runnables.config import RunnableConfig
from langchain.chat_models import init_chat_model
from .agent import Agent
from ..state.state import State, AgentState
from ..mythic import MythicAPIClient
from ..tools import UnifiedToolManager
from ..mcp import MCPManager
from mythic_container.logging import logger
from typing import Dict, List
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, START
from langgraph.prebuilt import tools_condition, ToolNode
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, BaseMessage
import asyncio

class OperatorState(State):
    """
    Represents the state of the Operator agent, including its messages and other relevant data.
    Inherits from State to manage message history and additional operator-specific attributes.
    """
    # Additional attributes specific to the operator can be added here if needed.
    pass

class Operator(Agent):
    """
    Operator is responsible for interfacing with Mythic's command and control (C2) agents.
    It issues raw commands directly to active C2 callbacks via the Mythic API. This agent does not
    interact with other Mythic subsystems such as artifacts or files.

    This agent is typically invoked when direct operator-level tasking is required for implants
    or payloads controlled by the red team during post-exploitation.

    Example use cases:
    - Tasking an agent to download and execute a payload
    - Instructing an agent to enumerate processes or privileges
    - Sending a lateral movement or persistence command
    """
    name = "Operator"
    description = "Handles direct operator-level tasking for Mythic command-and-control implants, agents, or payloads controlled by the red team. Use this for issuing raw commands to active C2 callbacks via the Mythic API."
    agent_task_id: str  # Mythic task ID for the agent
    system_prompt = """
        You are the Operator agent, responsible for interfacing with Mythic's command and control (C2) agents.
        Your job is to issue raw commands directly to active C2 callbacks via the Mythic API. You do not interact with other Mythic subsystems such as artifacts or files.
    """

    def __init__(self, provider: str, model: str, config: dict, agent_task_id: str):
        super().__init__(provider, model, config)
        if agent_task_id:
            self.agent_task_id = agent_task_id
        else:
            raise ValueError("Agent task ID must be provided for the Operator agent.")
    
    def agent(self, state: AgentState) -> AgentState:
        # Remove thread_id from config if it exists
        if self.config and "configurable" in self.config:
            config = RunnableConfig(
                configurable={
                    k: v 
                    for k, v in self.config["configurable"].items()
                    if k not in ["thread_id"]  # Remove thread_id if present
                }
            )
        else:
            config = None
       
        if config:
           resp = self.llm.invoke([SystemMessage(content=self.system_prompt)] + [AIMessage(content=state["agent_prompt"])], config=config)
        else:
            resp = self.llm.invoke(self.system_prompt + state["agent_prompt"])
        for message in resp.content[0]:
            state["messages"].append(AIMessage(content=message))
        return state

    async def _get_tools(self):
        """
        Returns the tools available to the operator agent.
        This method is used to retrieve the tools that can be invoked by the agent.
        """
        # Initialize the unified tool manager
        self.tool_manager = UnifiedToolManager(MCPManager)
        
        # Initialize Mythic tools
        await self.tool_manager.initialize_mythic_tools(self.agent_task_id)
        
        # Get tools
        all_tools: List[BaseTool] = []
        tools = ["get_all_commands_for_payloadtype", "issue_task_and_waitfor_task_output"]
        for tool in tools:
            t = self.tool_manager.get_tool_by_name(tool)
            if t:
                all_tools.append(t)
        
        # For backward compatibility, keep the mythic_client reference
        self.mythic_client = self.tool_manager.mythic_client
        return all_tools
    
    def get_graph(self) -> StateGraph:
        """Returns the state graph for the operator agent."""

        # Get the specific tools for the operator agent
        tools = asyncio.run(self._get_tools())

        # Create the state graph for the operator agent
        graph = StateGraph(OperatorState)
        graph.add_node(self.name, self.agent)
        graph.add_node("tools", ToolNode(tools))
        graph.add_edge(START, self.name)
        graph.add_conditional_edges(self.name, tools_condition)
        graph.add_edge("tools", self.name)

        return graph