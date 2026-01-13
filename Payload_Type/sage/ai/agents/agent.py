from langchain_core.runnables.config import RunnableConfig
from langchain_core.language_models.chat_models import BaseChatModel
from typing import Any
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, MessagesState

from ..state.state import State, AgentState

class Agent:
    """A base agent class for Sage that can be extended for specific behaviors."""

    name: str # The name of the agent
    description: str # A description of the agent's purpose passed to a model to decide if it should be used.
    system_prompt: str # The system prompt for the agent
    provider: str # The LLM provider, e.g., "openai"
    model: str # The LLM model, e.g., "gpt-3.
    config: RunnableConfig | None # Configuration for the agent, such as API keys or URLs
    llm: BaseChatModel | Any # The language model used by the agent

    def __init__(self, provider: str, model: str, config: dict | None):
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
            config = None
       
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

    def agent(self, state: AgentState) -> AgentState:
        """The main method to be implemented by subclasses to handle the agent's logic."""
        raise NotImplementedError("Subclasses must implement the agent method.")
    
    def get_name(self) -> str:
        """Returns the name of the agent."""
        return self.name
    
    def get_description(self) -> str:
        """Returns the description of the agent."""
        return self.description
    
    def get_graph(self) -> StateGraph:
        """Returns the state graph for the agent."""
        return StateGraph(State)
