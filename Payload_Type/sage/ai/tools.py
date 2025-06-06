from langchain_core.tools import tool
from mythic_container.MythicRPC import *
from mythic import mythic, mythic_classes

def get_api(taskID: str) -> mythic_classes.Mythic:
    resp = mythic.SendMythicRPCAPITokenCreate(MythicRPCAPITokenCreateMessage(AgentTaskID=taskID))
    if resp.Success:
        return mythic.login(apitoken=resp.APIToken, server_ip="127.0.0.1", server_port=7443, ssl=True)
    else:
        raise Exception(f"Failed to get API token: {resp.Error}")

def get_all_tools():
    return [get_all_commands_for_payloadtype]

@tool(parse_docstring=True)
async def get_all_commands_for_payloadtype(taskID: str, payload: str) -> str:
    """Executes a graphql query to get information about all current commands for a payload type. The default set of attributes returned in the dictionary can be found at graphql_queries.commands_fragment. If you want to use your own `custom_return_attributes` string to identify what information you want back, you have to include the `attributes` and `cmd` fields, everything else is optional.

    Args:
        taskID: The task ID of the agent to get the commands for.
        payload: Name of the agent or payload to get commands and their arguments for.
    
    Returns:
        A JSON string of the commands and their arguments.
    """
    attr = """
    cmd
    commandparameters {
      cli_name
      name
      type
      description
      default_value
      choices
      required
    }
    description
    help_cmd
    needs_admin
    """
    try:
        api = get_api(payload)
        results = await mythic.get_all_commands_for_payloadtype(api.mythic_instance, payload, attr)
        return json.dumps(results)
    except Exception as e:
        return f"Error getting commands for payload type {payload}: {e}"