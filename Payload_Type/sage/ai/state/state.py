from langgraph.graph import MessagesState
from typing import Annotated, Optional
import operator
from langgraph.graph import MessagesState
from langchain_core.messages import MessageLikeRepresentation

def override_reducer(current_value, new_value):
    if isinstance(new_value, dict) and new_value.get("type") == "override":
        return new_value.get("value", new_value)
    else:
        return operator.add(current_value, new_value)

class State(MessagesState):
    """
    Represents the state of the AI system, including its messages and other relevant data.
    Inherits from MessagesState to manage message history.
    """
    

class AgentState(MessagesState):
    """
    Represents the state of an agent, including its messages and other relevant data.
    Inherits from MessagesState to manage message history.
    """
    orchestrator_messages: Annotated[list[MessageLikeRepresentation], override_reducer]
    agent_prompt: str
