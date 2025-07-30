from langchain_core.runnables.config import RunnableConfig
from langchain.chat_models import init_chat_model
from .agent import Agent
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, BaseMessage
from ..state.state import State, AgentState

class Generalist(Agent):
    """
    GeneralistAgent is a fallback LLM agent responsible for handling tasks that are not explicitly routed
    to a specialized agent. It acts as a catch-all responder for general-purpose queries, such as 
    basic questions, open-ended prompts, or any requests that do not match a defined specialization.

    This agent ensures the system remains responsive and capable even when no targeted agent is assigned
    to the task. It can handle a wide range of inputs, from simple factual questions to generic reasoning
    tasks.

    Example use cases:
    - "What is 4 + 4?"
    - "Summarize this paragraph."
    - "Write a generic thank-you message."

    If task routing fails or no agent is assigned, GeneralistAgent is used by default.
    """


    name = "Generalist"
    description = "handles broad, general-purpose tasks that don't match any specialized agent. Use this for simple questions, open-ended prompts, or uncategorized inputs (e.g., “What is 4+4?”, “Write a short poem”, “Summarize this text”)."
    system_prompt = """
        You are the Generalist agent, a capable and efficient language model agent responsible for handling tasks that do not fall under any specialized category. Your job is to respond clearly, concisely, and helpfully to a wide range of prompts, including factual questions, reasoning tasks, summaries, basic instructions, or creative writing.

        Always aim to complete the user's request directly without referring the task elsewhere. If the input is ambiguous, respond with your best interpretation. If the task requires deeper expertise that you cannot provide, offer a reasonable response based on general knowledge. Avoid deferring the task to another agent.

        Format your answers in plain, readable text unless otherwise instructed. Respond in a helpful and professional tone.
    """

    def __init__(self, provider: str, model: str, config: dict):
        super().__init__(provider, model, config)
    
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

        generalist_messages = [SystemMessage(content=self.system_prompt), HumanMessage(content=state["agent_prompt"])]
        print(f"\nGeneralist Agent State: {state}\n")
        if config:
            resp = self.llm.invoke(generalist_messages, config=config)
        else:
            resp = self.llm.invoke(generalist_messages)
        print(f"Generalist Response: {resp}")
        state["messages"].append(AIMessage(content=resp.content))
        return state