from typing import Any
from mcp.server.fastmcp import FastMCP
from mythic import mythic
import asyncio
import argparse
import json


mcp = FastMCP("mythic_mcp")

api = None

class MythicAPI:
    def __init__(self, username, password, server_ip, server_port):
        self.username = username
        self.password = password
        self.server_ip = server_ip
        self.server_port = server_port

    async def connect(self):
        self.mythic_instance = await mythic.login(
            username=self.username,
            password=self.password,
            server_ip=self.server_ip,
            server_port=self.server_port,
        )

@mcp.tool()
async def get_all_commands_for_payloadtype(payload: str) -> str:
    """Executes a graphql query to get information about all current commands for a payload type. The default set of attributes returned in the dictionary can be found at graphql_queries.commands_fragment. If you want to use your own `custom_return_attributes` string to identify what information you want back, you have to include the `attributes` and `cmd` fields, everything else is optional.

    Args:
        payload: Name of the agent or payload to get commands and their arguments for.
    Returns:
        str: JSON string of the commands and their arguments.
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
        results = await mythic.get_all_commands_for_payloadtype(api.mythic_instance, payload, attr)
        return json.dumps(results)
    except Exception as e:
        return f"Error getting commands for payload type {payload}: {e}"

@mcp.tool()
async def issue_task_and_waitfor_task_output(command: str, parameters: str|dict, callback_display_id: int, token_id: int=None, timeout: int=None) -> str:
    """Issue a task to execute 'command' on the specified agent and wait for the agent to checkin, execute the task, and return the results.

    Args:
        command: The command name to execute from the "cmd" field from the get_all_commands_for_payloadtype tool. Validate the agent's operating system and the supported_os match.
        parameters: The command's parameters or arguments. Prefer a JSON string that leverages the commandparameters "name" value (e.g. {"arguments": "value"}). Alternatively, use a non-JSON string that has dash with the "cli_name" field (e.g. -path /etc/issue).
        callback_display_id: The callback_display_id of the target agent to run the command on.
        token_id: Optional Mythic identifier for tracked Windows user access tokens to use for impersonation.
        timeout: Optional timeout in seconds for the task to complete.
    Returns:
        str: Command output (binary output coerced to string).
    """
    try:
        results = await mythic.issue_task_and_waitfor_task_output(api.mythic_instance, command, parameters, callback_display_id, token_id, timeout)
        if results is None:
            return "No results returned from task."
        else:
            return str(results)
    except Exception as e:
        return f"Error issuing command '{command}' to agent {callback_display_id}: {e}"

@mcp.tool()
async def execute_graphql_query(query: str, variables: dict=None) -> str:
    """Execute a Mythic graphql query.

    Args:
        query: The graphql query to execute.
        variables: The variables parameter should contain a JSON object with key-value pairs that correspond to the variable names used in your GraphQL query. Each key should match a variable name defined in the query (without the $ prefix), and each value should match the expected type for that variable. These variables will be passed to the GraphQL server along with the query to provide dynamic values for the operation.
    Returns:
        str: JSON string of the Mythic GraphQL response.
    """
    try:
        results = await mythic.execute_custom_query(api.mythic_instance, query, variables)
        return json.dumps(results)
    except Exception as e:
        return f"Error executing query: {e}"

@mcp.tool()
async def get_all_active_callbacks(custom_return_attributes: str=None) -> str:
    """Executes a graphql query to get information about all currently active callbacks. The default set of attributes returned in the dictionary can be found at graphql_queries.callback_fragment. If you want to use your own `custom_return_attributes` string to identify what information you want back, you have to include the `id` field, everything else is optional.

    Args:
        custom_return_attributes: Optional string of attributes to return. If not provided, the default set of attributes will be used.
    Returns:
        str: JSON string of the callbacks and their attributes.
    """
    return await mythic.get_all_active_callbacks(api.mythic_instance, custom_return_attributes)

@mcp.tool()
async def get_all_tasks(custom_return_attributes: str=None, callback_display_id: int=None) -> str:
    """Executes a graphql query to get all tasks submitted so far (potentially limited to a single callback). The default set of attributes returned in the dictionary can be found at graphql_queries.task_fragment. If you want to use your own `custom_return_attributes` string to identify what information you want back, you have to include the `id` field, everything else is optional.

    Args:
        custom_return_attributes: Optional string of attributes to return. If not provided, the default set of attributes will be used.
        callback_display_id: The callback_display_id of the target agent to get all the submitted tasks for
    Returns:
        str: JSON string of the tasks and their attributes.
    """
    try:
        if callback_display_id is not None:
            results = await mythic.get_all_tasks(api.mythic_instance, custom_return_attributes, callback_display_id)
        else:
            results = await mythic.get_all_tasks(api.mythic_instance, custom_return_attributes)
        return json.dumps(results)
    except Exception as e:
        return f"Error getting tasks: {e}"


@mcp.tool()
async def get_all_payloads(custom_return_attributes: str=None) -> str:
    """Get information about all payloads currently registered with Mythic (this includes deleted payloads and autogenerated ones for tasking). The default attributes returned for each payload can be found at graphql_queries.payload_data_fragment, but can be modified with thte custom_return_attributes variable.

    Args:
        custom_return_attributes: Optional string of attributes to return. If not provided, the default set of attributes will be used.
    Returns:
        str: JSON string of the payloads and their attributes.
    """
    try:
        results = await mythic.get_all_payloads(api.mythic_instance, custom_return_attributes)
        return json.dumps(results)
    except Exception as e:
        return f"Error getting payloads: {e}"

@mcp.tool()
async def create_payload(payload_type_name: str, filename: str, operating_system: str, c2_profiles: mythic.List[dict], commands: mythic.List[dict], build_parameters: mythic.List[dict], description: str, return_on_complete: bool=True, timeout: int=None, custom_return_attributes: str=None, include_all_commands: bool=True) -> dict:
    """
    This tasks Mythic to create a new payload based on the supplied parameters. If `return_on_complete` is false, then this will return immediately after issuing the task to Mythic.
    If `return_on_complete` is true, then this will do a subsequent subscription to wait for the payload container to finish building.
    c2_profiles is a list of dictionaries where each dictionary holds the following information:
        {
            "c2_profile": "name of the profile, like http",
            "c2_profile_parameters": {
                "parameter name": "parameter value",
                "parameter name 2": "parameter value 2"
            }
        }
    The names of these parameters can be found on the C2 Profile page in Mythic and clicking "build info".
    build_parameters is a list of dictionaries where each dictionary holds the following information:
    {
        "name": "build parameter name", "value": "build parameter value"
    }
    The names of the build parameters page can be found on the Payloads page and clicking for "build information".
    commands is a list of the command names you want included in your payload. If you omit this, set it as None, or as an empty array ( [] ), then Mythic will automatically
        include all builtin and recommended commands for the OS you selected.
    custom_return_attributes only applies when you're using `return_on_complete`.
    Otherwise, you get a dictionary with status, error, and uuid.

    Args:
        payload_type_name: The name of the payload type to create.
        filename: The name of the file to create.
        operating_system: The operating system for the payload.
        c2_profiles: A list of dictionaries containing C2 profile information.
        commands: A list of dictionaries containing command information.
        build_parameters: A list of dictionaries containing build parameter information.
        description: A description of the payload.
        return_on_complete: Whether to return on completion or not.
        timeout: Optional timeout in seconds for the task to complete.
        custom_return_attributes: Optional string of attributes to return. If not provided, the default set of attributes will be used.
        include_all_commands: Whether to include all commands or not.
    Returns:
        dict: JSON string of the payload creation response.
    """
    try:
        results = await mythic.create_payload(
            api.mythic_instance,
            payload_type_name,
            filename,
            operating_system,
            c2_profiles,
            commands,
            build_parameters,
            description,
            return_on_complete,
            timeout,
            custom_return_attributes,
            include_all_commands
        )
        return json.dumps(results)
    except Exception as e:
        return f"Error creating payload: {e}"


@mcp.tool()
async def graphql_introspection_schema() -> str:
    """Executes a graphql introspection query to get information about the Mythic GraphQL schema.

    Returns:
        str: JSON string of the Mythic GraphQL schema.
    """
    query = "query Introspection {__schema {queryType {fields {name description}}}}"
    try:
        results = await mythic.execute_custom_query(api.mythic_instance, query)
        return json.dumps(results)
    except Exception as e:
        return f"Error executing introspection query: {e}"
    
@mcp.tool()
async def graphql_introspection_mutations() -> str:
    """Executes a graphql introspection query to get information about the Mythic GraphQL mutations.

    Returns:
        str: JSON string of the Mythic GraphQL mutations.
    """
    query = """
    query Mutations {
        __schema {
            mutationType {
                fields {
                    name
                    description
                }
            }
        }
    }
    """
    try:
        results = await mythic.execute_custom_query(api.mythic_instance, query)
        return json.dumps(results)
    except Exception as e:
        return f"Error executing introspection query: {e}"

@mcp.tool()
async def graphql_introspection_types() -> str:
    """Executes a graphql introspection query to get information about the Mythic GraphQL types.

    Returns:
        str: JSON string of the Mythic GraphQL types.
    """
    query = """
    query Types {
        __schema {
            types {
                name
                description
            }
        }
    }
    """
    try:
        results = await mythic.execute_custom_query(api.mythic_instance, query)
        return json.dumps(results)
    except Exception as e:
        return f"Error executing introspection query: {e}"

async def main():
    await api.connect()
    await mcp.run_stdio_async()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP for Mythic")
    parser.add_argument(
        "username", type=str, help="Username used to connect to Mythic API"
    )
    parser.add_argument(
        "password", type=str, help="Password used to connect to Mythic API"
    )
    parser.add_argument("host", type=str, help="Host (IP or DNS) of Mythic API server")
    parser.add_argument("port", type=str, help="Port of Mythic server HTTP server")

    args = parser.parse_args()
    api = MythicAPI(args.username, args.password, args.host, args.port)

    asyncio.run(main())
