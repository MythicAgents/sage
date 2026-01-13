import argparse
import getpass
import os
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage


class State(TypedDict):
    # Messages have the type "list". The `add_messages` function
    # in the annotation defines how this state key should be updated
    # (in this case, it appends messages to the list, rather than overwriting them)
    count: int
    messages: Annotated[list[HumanMessage | AIMessage | SystemMessage], add_messages]


def _set_env(var: str):
    if not os.environ.get(var):
        os.environ[var] = getpass.getpass(f"{var}: ")

graph_builder = StateGraph(State)

llm: BaseChatModel
# llm = init_chat_model("anthropic:claude-3-5-sonnet-latest")

def chatbot(state: State):
    return {"messages": [llm.invoke(state["messages"])]}

def get_model(provider: str, model:str) -> BaseChatModel:
    return init_chat_model(model_provider=provider,model=model,configurable_fields="any")

def prompt(provider: str, model:str):
    # The first argument is the unique node name
    # The second argument is the function or object that will be called whenever
    # the node is used.
    global llm
    llm = init_chat_model(model_provider=provider,model=model)
    graph_builder.add_node("chatbot", chatbot)
    graph_builder.add_edge(START, "chatbot")
    graph_builder.add_edge("chatbot", END)
    graph = graph_builder.compile()

    def stream_graph_updates(user_input: str):
        for event in graph.stream({"messages": [{"role": "user", "content": user_input}]}):
            for value in event.values():
                print("🤖> ", value["messages"][-1].content)

    while True:
        try:
            user_input = input("👤> ")
            if user_input.lower() in ["quit", "exit", "q"]:
                print("Goodbye!")
                break
            stream_graph_updates(user_input)
        except:
            print("there was an exception!")
            break

if __name__ == "__main__":
    # Added CA to anaconda3/lib/python3.12/site-packages/certifi/cacert.pem
    #cafile = certifi.where()
    #with open('certicate.pem', 'rb') as infile:
    #    customca = infile.read()
    #with open(cafile, 'ab') as outfile:
    #    outfile.write(customca)
    parser = argparse.ArgumentParser(description="Mythic Sage AI Assistant")
    parser.add_argument(
        "--provider", type=str, help="Model provider (e.g., 'anthropic')", default="anthropic", required=True
    )
    parser.add_argument(
        "--model", type=str, help="Model string (e.g., 'claude-3-5-sonnet-latest')", default="claude-3-5-sonnet-latest", required=True
    )
    parser.add_argument(
        "--api-key", type=str, help="API key for the model provider", default=None, required=True
    )
    parser.add_argument(
        "--api-endpoint", type=str, help="API endpoint for the model provider", default=None, required=False
    )
    parser.add_argument(
        "--aws-access-key", type=str, help="AWS access key", default=None, required=False
    )
    parser.add_argument(
        "--aws-secret-access-key", type=str, help="AWS secret access key", default=None, required=False
    )
    parser.add_argument(
        "--aws-session-token", type=str, help="AWS session token", default=None, required=False
    )
    parser.add_argument(
        "--aws-region", type=str, help="AWS region", default=None, required=False
    )
    parser.add_argument(
        "--system-prompt", type=str, help="System prompt for the model", default=None, required=False
    )
    parser.add_argument(
        "--tools", action="store_true", help="Use tools to enhance the model's capabilities", required=False
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show verbose output of all User & AI messages", required=False
    )
    parser.add_argument("--prompt", type=str, help="User model prompt", default="What is the meaning of life?", required=True)
    parser.add_argument(
        "--agent-task-id", type=str, help="Agent Task ID (a UUID) for the Mythic task", required=False
    )
    args = parser.parse_args()
    print(f"Using provider: {args.provider}, model: {args.model}, prompt: '{args.prompt}', system_prompt: '{args.system_prompt}', tools: {args.tools}, verbose: {args.verbose}")
