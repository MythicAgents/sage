from .agent import Agent
from .generalist import Generalist
from .operator import Operator

def get_all_agents(provider: str, model: str, config: dict) -> list[Agent]:
    """Returns a list of all available agents."""
    return [
        Generalist(provider, model, config),
        Operator(provider, model, config, agent_task_id="1"),
    ]