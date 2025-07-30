from langchain_core.runnables.config import RunnableConfig
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import tools_condition, ToolNode
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, BaseMessage
from pydantic import BaseModel
from typing import List, Dict, Any
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from aiosqlite import connect
from uuid import uuid4
from langchain_core.language_models.chat_models import BaseChatModel

from .agent import Agent
from .utils import get_all_agents
from ..state.state import State, AgentState


class OrchestratorState(State):
    """
    Represents the state of the orchestrator agent, including its messages and other relevant data.
    Inherits from State to manage message history and additional orchestrator-specific attributes.
    """
    # Additional attributes specific to the orchestrator can be added here if needed.
    pass

class AgentPrompt(BaseModel):
    agent: str
    prompt: str

class OrchestratorOutput(BaseModel):
    """
    Represents the structured output of the orchestrator agent.
    Contains a mapping of agent names to their respective prompts.
    """
    agents: List[AgentPrompt]  # List of dictionaries with agent names and prompts

class Orchestrator():
    """An orchestrator agent that manages the execution of other agents based on the current state."""

    name = "Orchestrator"
    description = "Orchestrates the execution of agents based on the current state and available agents."
    system_prompt = """
        1. You are the orchestrator of the AI system, managing the execution of agents based on the current state and their capabilities.

        2. Here is a list of available agents and their descriptions:
        {agents_list}

        3. Generate a detailed plan to accomplish the user's prompt and break down the plan by responding with a list of agents and the prompt you want to send to each agent. The generated plan is provided to another agent, not to the user.
        """
    agents: list[Agent]
    memory: AsyncSqliteSaver
    config: RunnableConfig | None # Configuration for the agent, such as API keys or URLs
    llm: BaseChatModel | Any # The language model used by the agent

    def __init__(self, provider: str, model: str, config: dict, agent_task_id: str):
        self.provider = provider
        self.model = model
        api_key = None
        base_url = None
        if config and config.get("configurable"):
            self.config = RunnableConfig(
                configurable={
                    k: v 
                    for k, v in config.get("configurable", {}).items()
                    if k not in ["thread_id"]  # Remove thread_id if present
                }
            )
            if config["configurable"].get("api_key"):
                api_key = config["configurable"]["api_key"]
            if config["configurable"].get("base_url"):
                base_url = config["configurable"]["base_url"]



            api_key = config["configurable"]["api_key"] if config and config.get("configurable") else None
            if "base_url" in config["configurable"]:
                base_url = config["configurable"]["base_url"]
        else:
            config = {}
       
        if not api_key:
            raise ValueError("api_key must be provided in the configuration")
        if base_url:
            self.llm = init_chat_model(
                model_provider=provider,
                model=model,
                configurable_fields="any",
                api_key=api_key,
                base_url=base_url,
            )
        else:
            self.llm = init_chat_model(
                model_provider=provider,
                model=model,
                configurable_fields="any",
                api_key=api_key,
            )


        self.agents = get_all_agents(provider, model, config)
        db_path = "/home/john/Dev/sage/Payload_Type/sage/sage.db"  # Path to your SQLite database
        conn = connect(db_path, check_same_thread=False)
        self.memory = AsyncSqliteSaver(conn)
        if self.config and "configurable" in self.config:
            self.config["configurable"]["thread_id"] = str(agent_task_id)
    
    def get_graph(self) -> StateGraph:
        """Returns the state graph for the orchestrator."""
        graph = StateGraph(AgentState, input=State)
        graph.add_node("orchestrator-agent", self.agent)
        graph.add_edge(START, "orchestrator-agent")
        graph.add_edge("orchestrator-agent", END)
        
        return graph

    def agent(self, state: State) -> State: # If I added the config parameter here, it would be passed to the agent's invoke method. It'll have a bunch of LangGraph metadata and causes problems
        """Orchestrates the execution of agents based on the current state."""

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
       
        llm = self.llm.with_structured_output(OrchestratorOutput)

        # Format the system prompt with the list of available agents
        agents_list = ""
        if self.agents:
            for agent in self.agents:
                agents_list += f"- **{agent.get_name()}**: {agent.get_description()}\n"
        state["messages"].insert(0, SystemMessage(content=self.system_prompt.format(agents_list=agents_list)))
        print(f"\nOrchestrator Agent Messages: {state['messages']}\n")
        if config:
            resp = llm.invoke(state["messages"], config=config)
        else:
            resp = llm.invoke(state["messages"])
        print(f"Orchestrator Response: {resp}")
        if isinstance(resp, OrchestratorOutput):
            # Process the structured output to invoke the appropriate agents
            for agent_info in resp.agents:
                if agent_info.agent and agent_info.prompt:
                    # Find the agent by name
                    for agent in self.agents:
                        if agent.get_name() == agent_info.agent:
                            # Invoke the agent with the provided prompt
                            agent_response = agent.agent(AgentState(messages=state["messages"], orchestrator_messages=[], agent_prompt=agent_info.prompt))
                            print(f"Agent {agent.get_name()} Response: {agent_response}")
                            break
                else:
                    raise ValueError("Orchestrator output must contain both agent name and prompt.")
        else:
            raise ValueError("Orchestrator did not return structured output.")
        return state

    async def invoke(self, prompt: str):
        graph = self.get_graph()
        graph = graph.compile(checkpointer=self.memory)

        state = State(messages=[HumanMessage(content=prompt)])
        return await graph.ainvoke(state, self.config)

# Run the script from sage/Payload_Type/sage$ python3 -m ai.agents.orchestrator --prompt "hello"
# export LANGSMITH_TRACING=true
# export LANGSMITH_PROJECT=Sage
# export LANGSMITH_API_KEY=your_langsmith_api_key

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run the orchestrator agent a standalone operation.")
    parser.add_argument("-p", "--provider", type=str, default="anthropic", help="AI model provider, e.g., 'openai'")
    parser.add_argument("-m", "--model", type=str, default="claude-sonnet-4-20250514", help="AI model name, e.g., 'gpt-3.5-turbo'")
    parser.add_argument("--prompt", type=str, help="User request to send to the AI model", required=True)
    parser.add_argument("-k", "--api_key", type=str, help="API key for the AI model provider", required=False)
    parser.add_argument("-u", "--api_url", type=str, help="API URL for the AI model provider", required=False)
    args = parser.parse_args()

    if not args.api_key:
        import os
        args.api_key = os.getenv("API_KEY")
    if not args.api_key:
        import getpass
        args.api_key = getpass.getpass("Enter your LLM Provider API key: ")

    config = {"configurable": {}}
    if args.api_key:
        config["configurable"]["api_key"] = args.api_key
    if args.api_url:
        config["configurable"]["base_url"] = args.api_url
    
    orchestrator = Orchestrator(args.provider, args.model, config, str(uuid4()))
    resp = await orchestrator.invoke(args.prompt)
    print(f"\nOrchestrator Response: {resp}\n") 

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())