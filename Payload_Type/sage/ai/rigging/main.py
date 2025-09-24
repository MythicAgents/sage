import rigging as rg
from .mythic import get_mythic_client, MythicTools
from mythic import mythic, mythic_classes
from rigging.logging import configure_logging
from loguru import logger as loguru_logger

loguru_logger.enable('rigging')
configure_logging(
    'info',      # stderr level
    'rigging.log',   # log file (optional)
    'trace'      # log file level
)

@rg.tool
def add_numbers(a: int, b: int) -> int:
    """Adds two numbers together."""
    return a + b

class Agent:
    provider: str
    model: str
    verbose: bool
    mythic_client: mythic_classes.Mythic | None
    agent_task_id: str
    api_key: str
    base_url: str
    generator = rg.Generator

    def __init__(self, provider: str, model: str, system_prompt: str, config: dict, agent_task_id: str):
        self.provider = provider
        self.model = model
        self.system_prompt = system_prompt
        self.config = config
        self.agent_task_id = agent_task_id
        # self.mythic_client = get_mythic_client(str(agent_task_id))
        self.api_key = config["configurable"]["api_key"] if config and config.get("configurable") else ""
        #self.base_url = config["configurable"]["base_url"] if config and config.get("configurable") else ""
        #self.generator = rg.get_generator(f"{self.provider}/{self.model}")
        #if self.api_key:
        #    self.generator.api_key = self.api_key

    async def invoke(self, prompt: str):
        generator = rg.get_generator(f"{self.provider}/{self.model}")
        if self.api_key:
            generator.api_key = self.api_key
        
        # Get Mythic Client and Tools
        mythic_client = MythicTools(str(self.agent_task_id))
        await mythic_client.login()
        tools = [
            mythic_client.get_all_active_callbacks,
            mythic_client.get_all_payload_info,
            mythic_client.get_c2_profiles_for_payload,
            mythic_client.get_all_commands_for_payloadtype,
            #mythic_client.issue_task_and_waitfor_task_output,
            #mythic_client.create_payload,
        ]
        #tools = add_numbers

        pipeline = generator.chat(
            [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ]
        ).using(tools)

        chat = await pipeline.run()
        print(chat.all)
        return await generate_mythic_output(chat.all)

async def generate_mythic_output(messages: list[rg.Message]) -> str:
    """Generate output from Mythic based on the provided messages."""
    response = ""
    for message in messages:
        if message.role == "assistant":
            if message.content:
                response += f"🤖> {message.content}\n"
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    response += f"🔧> ID: {tool_call.id}, Type: {tool_call.type}, Name: {tool_call.function.name} Args: {tool_call.function.arguments}\n"
        elif message.role == "user":
            response += f"👤> {message.content}\n"
        elif message.role == "tool":
            response += f"🔧> ID: {message.tool_call_id}, Response: {message.content}\n"
        elif message.role == "system":
            pass
        else:
            response += f"❓[{message.role}]> {message.content}\n"
    return response

async def main():
    # From the Payload_Type directory run python3 -m sage.ai.rigging.main
    # Example usage
    agent = Agent(provider="anthropic", model="claude-sonnet-4-20250514", system_prompt="You are a helpful assistant", config={}, agent_task_id="6461f40c-ce82-41da-85ac-adb5d8172308")
    resp = await agent.invoke("What is 4 + 4?")
    print(resp)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())