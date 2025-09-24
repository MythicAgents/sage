import aiosqlite
from langgraph.graph import StateGraph, START, MessagesState, END
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt import tools_condition
from langchain.agents import create_agent
from langchain.agents.tool_node import ToolNode
from langgraph.types import Command
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables.config import RunnableConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, BaseMessage, AnyMessage
from mythic_container.logging import logger
from typing import Any
from .mythic import MythicTools
from langchain.tools import tool
from typing import Annotated
from langchain_core.tools import tool, InjectedToolCallId
from langchain.agents.tool_node import InjectedState

class SageState(MessagesState):
    count: int

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
    state: SageState
    graph: CompiledStateGraph | None

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
        self.counter = 0
        self.messages = []
        self.agent_task_id = agent_task_id
        self.task_id = task_id
        db_path = "sage.db"  # Path to your SQLite database
        conn = aiosqlite.connect(db_path, check_same_thread=False)
        self.memory = AsyncSqliteSaver(conn)
        self.system_message = SystemMessage(content=system_prompt)
        if system_prompt:
            self.messages.insert(0, SystemMessage(content=system_prompt))
        self.state = SageState(messages=self.messages, count=self.counter)
        if config:
            self.config = RunnableConfig(
                configurable={
                    k: v 
                    for k, v in config.get("configurable", {}).items()
                    if k not in ["thread_id"]  # Remove thread_id if present
                }
            )
        self.llm = init_chat_model(model_provider=provider,model=model, api_key=config["configurable"]["api_key"] if config and config.get("configurable") else None)

    async def initialize(self):
        """ Initialize the model's graph and Mythic client."""
        self.mythic_client = MythicTools(agent_task_id=self.agent_task_id)
        await self.mythic_client.login()
        if not self.graph:
            # Build and compile the graph
            self.graph = (
            StateGraph(SageState)
            .add_node(self._supervisor_agent())
            .add_node(self._generalist_agent())
            .add_node(self._mythic_operator_agent())
            .add_node(self._mythic_payload_agent())
            .add_edge(START, "Supervisor")
            .add_edge("Generalist", "Supervisor")
            .add_edge("Mythic_Operator", "Supervisor")
            .add_edge("Mythic_Payload", "Supervisor")
            .compile(checkpointer=self.memory, name="Sage")
        )

    def set_verbose(self, verbose: bool):
        """
        Set the verbosity of the model.
        :param verbose: If True, the model will print all User & AI messages.
        """
        self.verbose = verbose

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
        tools = []
        return create_agent(
            model=self.llm,
            tools=tools,
            name=name,
            prompt=SystemMessage(content=prompt),
        )
    
    def _mythic_operator_agent(self):
        name = "Mythic_Operator" # Note: name must match the agent_name in _create_handoff_tool and cannot have spaces
        prompt = """
        You are a Mythic Operator Agent responsible for handling prompts or tasks issued to Mythic from a human operator interacting with Mythic. 
        Your primary role is to take actions within Mythic based on the operator's requests, ensuring that tasks are executed accurately and efficiently.

        Responsibilities:
        - Interpret and execute commands related to Mythic operations, such as managing callbacks, issuing Mythic tasks, and monitoring their status.
        - Provide updates on the status of operations and any relevant information to the operator.
        - Ensure that all actions taken within Mythic are logged and traceable.

        Guidelines:
        - Always confirm the operator's intent before executing any critical commands.
        - Use tools that provide the requested information from Mythic, such as get_all_active_callbacks for issuing commands to the Mythic agent with the issue_task_and_waitfor_task_output tool.
        - Maintain a clear and professional tone in all communications.
        - Prioritize accuracy and efficiency in executing tasks.
        - If a command is unclear or outside your scope, ask for clarification or suggest consulting another agent.

        Your goal is to assist the human operator effectively, ensuring that their requests are fulfilled accurately within the Mythic environment.
        """
        # Tools
        if self.mythic_client is not None:
            tools = self.mythic_client.get_tools([
                "get_all_active_callbacks",
                "get_all_commands_for_payloadtype",
                "issue_task_and_waitfor_task_output",
                "get_task_history_for_callback",
                "get_all_task_output_by_task_id",
                "get_operations",
            ])
        else:
            raise ValueError("Mythic client not initialized for Mythic Operator Agent.")
        return create_agent(
            model=self.llm,
            tools=tools,
            name=name,
            prompt=SystemMessage(content=prompt),
        )

    def _mythic_payload_agent(self):
        name = "Mythic_Payload"
        prompt = """
        You are the Mythic Payload Agent, an AI/LLM-based assistant designed to help users create Mythic Payloads within the Mythic C2 framework. Always remember and clearly distinguish that Mythic agents refer to the software components or payload types in the Mythic C2 system (e.g., Apollo, Poseidon, Apfell, Merlin)—these are wildly different from AI/LLM agents like yourself, which are language models for conversational tasks.

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
        - **Step-by-Step Process**: When sufficient info is provided, outline the payload creation steps clearly, including any Mythic agent-specific configurations from the build container. Reference supported features like task queuing, opsec checks, or browser scripting if relevant.
        - **Validation**: Always validate compatibility (e.g., "Apollo supports Windows with http and websocket profiles").
        - **Documentation Reference**: Direct users to official docs for details: https://docs.mythic-c2.net/operational-pieces/payload-types. If needed, suggest checking agent repos at https://github.com/MythicAgents for source code and features.
        - **No Assumptions**: Do not assume details or create payloads without explicit user confirmation. If a query is ambiguous, clarify.
        - **Edge Cases**: For advanced features (e.g., wrappers like scarecrow_wrapper or AI-integrated agents like sage), explain limitations and requirements.
        - **Tone**: Be professional, helpful, and concise. Avoid jargon unless explaining it, and focus on operational safety.

        Common Mythic Agents for Reference (based on community and official sources; always verify latest via docs):
        - Apollo: Windows (.NET), supports http, websocket.
        - Poseidon: Linux/macOS (Golang), supports http, websocket, dns.
        - Merlin: Windows, Linux, macOS, freebsd (Golang), supports http.
        - Apfell: macOS (JXA), supports http.
        - Thanatos: Linux/Windows (Rust), supports http, websocket.
        - Medusa: Cross-platform (Python), supports multiple profiles.
        - Others: Kharon (evasion-focused), Xenon (C for Windows), etc.

        If a user attempts to confuse Mythic agents with AI agents, correct them immediately (e.g., "Mythic agents are C2 implants, not AI systems like me.").
        """
        # Tools
        if self.mythic_client:
            tools = self.mythic_client.get_tools([
                "get_payload_names",
                "create_payload",
                "get_all_payload_info",
                "get_all_commands_for_payloadtype",
                "get_c2_profiles_for_payload",
                "issue_task_and_waitfor_task_output",
            ])
        else:
            raise ValueError("Mythic client not initialized for Mythic Payload Agent.")
        return create_agent(
            model=self.llm,
            tools=tools,
            name=name,
            prompt=SystemMessage(content=prompt),
        )

    def _supervisor_agent(self):
        name = "Supervisor"
        prompt = """
            You are a Supervisor Agent responsible for managing and coordinating multiple specialized agents. 
            Your primary role is to ensure that tasks are delegated effectively, progress is monitored, and results are integrated seamlessly. 
            You have access to the following agents, each with their own expertise:

            1. **Generalist Agent**: Handles general inquiries and tasks that do not fit for other agents.
            2. **Mythic Operator Agent**: Handles prompts or tasks issued to Mythic from a human operator interacting with Mythic and to take actions within Mythic.

            Your responsibilities include:
            - Understanding the user's high-level goals and breaking them into smaller, manageable tasks.
            - Assigning tasks to the appropriate agent based on their expertise.
            - Monitoring the progress of each agent and ensuring timely completion of tasks.
            - Integrating the outputs from all agents into a cohesive response for the user.
            - Providing clear and concise updates to the user about the status of tasks.

            When interacting with the agents:
            - Clearly specify the task, context, and expected output.
            - Use structured communication to ensure clarity and avoid misunderstandings.
            - Handle any errors or unexpected behavior by reassigning tasks or consulting the Error Handling Agent.

            When responding to the user:
            - Summarize the progress and results of all agents.
            - Provide actionable insights or next steps based on the outputs of the agents.
            - Maintain a professional and concise tone.

            Always prioritize efficiency, accuracy, and clarity in your management and communication.
        """
        # Handoffs
        assign_to_generalist_agent = _create_handoff_tool(
            agent_name="Generalist",
            description="Assign task to a generalist agent.",
        )

        assign_to_mythic_operator_agent = _create_handoff_tool(
                agent_name="Mythic_Operator",
                description="Assign task to a mythic operator agent.",
            )
        
        assign_to_mythic_payload_agent = _create_handoff_tool(
                agent_name="Mythic_Payload",
                description="Assign task to a mythic payload agent.",
            )
        
        # Tools
        tools = [
            assign_to_generalist_agent,
            assign_to_mythic_operator_agent,
            assign_to_mythic_payload_agent,
        ]

        return create_agent(
            model=self.llm,
            tools=tools,
            name=name,
            prompt=SystemMessage(content=prompt),
        )

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

    def _generate_mythic_output(self, resp) -> str:
        """
        Generate a string representation of the model's messages for Mythic output.
        :return: A string containing the messages formatted for Mythic.
        """

        return_message = ""
        # check that the messages key is in the resp or throw an error
        if "messages" not in resp:
            raise ValueError("No messages found in the response from the graph invocation.")
        for i, message in enumerate(resp["messages"]):
            # print(f"Processing message {i}: {message}")
            if i < self.counter:
                continue
            if isinstance(message, HumanMessage):
                return_message += f"👤> {message.content}\n"
            elif isinstance(message, AIMessage):
                is_last_message = i == len(resp["messages"]) - 1
                show_content = is_last_message or self.verbose
                
                if show_content:
                    if isinstance(message.content, str):
                        if message.content and message.name:
                            return_message += f"🤖 ({message.name})> {message.content}\n"
                        elif message.content:
                            return_message += f"🤖> {message.content}\n"
                        else:
                            return_message += "🤖> NO CONTENT"
                    elif isinstance(message.content, list):
                        if len(message.content) > 0:
                            return_message += self._process_ai_content_list(message.content)
                        elif is_last_message and not self.verbose and not message.content:
                            logger.debug("Last message has empty content list and not verbose, searching for last AIMessage with content list") 
                            # If last message has empty content list and not verbose, find the last AIMessage with content
                            for j in range(len(resp["messages"]) - 2, -1, -1):
                                prev_message = resp["messages"][j]
                                if isinstance(prev_message, AIMessage):
                                    if isinstance(prev_message.content, str) and prev_message.content:
                                        if prev_message.name:
                                            return_message += f"🤖 ({prev_message.name})> {prev_message.content}\n"
                                        else:
                                            return_message += f"🤖> {prev_message.content}\n"
                                        break
                                    elif isinstance(prev_message.content, list) and len(prev_message.content) > 0:
                                        return_message += self._process_ai_content_list(prev_message.content)
                                        break
            elif isinstance(message, SystemMessage):
                pass
            elif isinstance(message, ToolMessage):
                if self.verbose:
                    return_message += f"🛠️> Tool Call ID: '{message.tool_call_id}', Content: '{message.content}'\n"
            else:
                return_message += f"❓> Unknown message type: {type(message)} with content: {message}\n"
            
            self.counter += 1

        return return_message
    
    async def invoke(self, prompt: str) -> str:
        """
        Invoke the model with a prompt and return the response.
        :param prompt: The prompt to send to the model.
        :return: The model's response as a string.
        """
        logger.debug(f"Invoking LLM with provider: '{self.provider}', model: '{self.model}', prompt: '{prompt}'")
        
        if "messages" not in self.state:
            self.state["messages"] = []
        self.state["messages"].append(HumanMessage(content=prompt))
        
        if self.graph:
            resp = await self.graph.ainvoke(self.state, {"configurable": {"thread_id": self.task_id}}) # Default {"recursion_limit": 25}
        else:
            raise ValueError("No graph defined for the model. Ensure the model's initialize() method has been called.")

        return self._generate_mythic_output(resp)


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
    description = description or f"Ask {agent_name} for help."

    @tool(name, description=description)
    def handoff_tool(
        state: Annotated[MessagesState, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        tool_message = ToolMessage(
            content=f"Successfully transferred to {agent_name}",
            name=name,
            tool_call_id=tool_call_id,
        )
        return Command(
            goto=agent_name,  
            update={**state, "messages": state["messages"] + [tool_message]},  
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