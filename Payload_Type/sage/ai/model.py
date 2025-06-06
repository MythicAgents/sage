import json
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_function
from .mythic import MythicAPIClient
from mythic_container.logging import logger


class State(TypedDict):
    # Messages have the type "list". The `add_messages` function
    # in the annotation defines how this state key should be updated
    # (in this case, it appends messages to the list, rather than overwriting them)
    count: int
    messages: Annotated[list[HumanMessage | AIMessage | SystemMessage], add_messages]

graph_builder = StateGraph(State)

llm: BaseChatModel

class Model:
    """A class to represent a model with its configuration."""
    def __init__(self, provider: str, model: str, system_prompt: str, config: dict):
        """
        Initialize the Model with provider, model, and configuration.
        :param provider: The model provider (e.g., 'anthropic', 'bedrock').
        :param model: The model string (e.g., 'claude-3-5-sonnet-latest').
        :param system_prompt: The system prompt to use for the model.
        :param config: A dictionary containing configuration options for the model {"configurable": {}}.
        """
        self.provider = provider
        self.model = model
        self.config = config if config and config.get("configurable") else None
        self.verbose = False
        self.mythic_client = None
        self.counter = 0
        self.messages = []
        if system_prompt:
            self.messages.insert(0, SystemMessage(content=system_prompt))
        self.llm = init_chat_model(model_provider=provider,model=model,configurable_fields="any")
        if self.config:
                self.llm = self.llm.with_config(self.config["configurable"])

    async def with_tools(self, agent_task_id: str):
        """
        Bind all available tools to the model.
        :param agent_task_id: The agent task ID, a UUID, of the agent task interacting with the model.
        """
        self.mythic_client = await MythicAPIClient.create(agent_task_id)
        tool_definitions = self.mythic_client.get_tool_definitions_for_llm()
        if self.provider.lower() == "openai":
            tool_definitions = [convert_to_openai_function(tool) for tool in tool_definitions]
        self.llm = self.llm.bind_tools(tool_definitions) # type: ignore

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

    async def invoke(self, prompt: str) -> str:
        """
        Invoke the model with the given prompt.
        :param prompt: The user prompt to send to the model.
        :return: The model's response as a string.
        """
        logger.debug(f"Invoking model {self.provider} {self.model} with prompt: {prompt}")

        # If this is a subsequent call, increment the counter
        if self.counter > 0:
            self.counter += 1
        if prompt:
            self.messages.append(HumanMessage(content=prompt))

        done = False
        while not done:
            try:
                if self.config is not None and self.config.get("configurable"):
                    logger.debug(f"Invoking model 2 {self.provider} {self.model} with prompt: {prompt}")
                    resp = self.llm.invoke(self.messages, self.config["configurable"])
                else:
                    logger.debug(f"Invoking model 3 {self.provider} {self.model} with prompt: {prompt}")
                    resp = self.llm.invoke(self.messages)
                self.messages.append(resp)
                logger.debug(f"🤖> Invoke response: {resp}")
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
                logger.debug("Model indicated end of turn, stopping.")
                break
            tool_calls = []
            if hasattr(resp, "tool_calls") and resp.tool_calls:
                tool_calls = resp.tool_calls

            for tool_call in tool_calls:
                logger.debug(f"🛠️> Name: '{tool_call.get('name', '')}', Arguments: '{tool_call.get('input', '')}', ID: '{tool_call.get('id', '')}'")
                if self.verbose:
                    pass
                    #return_message += f"🛠️> Name: '{tool_call.get('name', '')}', Arguments: '{tool_call.get('input', '')}', ID: '{tool_call.get('id', '')}'\n"
                if not self.mythic_client:
                    logger.error("Mythic client is not initialized, cannot execute tool.")
                    return "❗> Mythic client is not initialized, cannot execute tool.\n"
                result = await self.mythic_client.execute_tool(
                    tool_name=tool_call.get("name", ""),
                    **(tool_call.get("input", {}) if isinstance(tool_call.get("input", {}), dict) else {})
                )
                tool_message = ToolMessage(
                    content=json.dumps(result) if isinstance(result, dict) else str(result),
                    tool_call_id=tool_call.get('id', '')
                )
                self.messages.append(tool_message)
                logger.debug(f"🛠️> Result: {result}")
        
        return self._generate_mythic_output()

sessions: dict[str, Model] = {}

async def get_session(session_id: str) -> Model|None:
    try:
        return sessions.get(session_id)
    except KeyError:
        logger.error(f"Session {session_id} not found.")
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