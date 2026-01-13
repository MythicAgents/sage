import os
import json
from typing import Annotated, List, Dict, TypedDict
from mythic import mythic, mythic_classes
from mythic_container.MythicGoRPC import SendMythicRPCAPITokenCreate, MythicRPCAPITokenCreateMessage
from mythic_container.logging import logger
import rigging as rg

class MythicTools:
    client: mythic_classes.Mythic
    task_id: str
    def __init__(self, task_id: str):
        self.task_id = task_id
        logger.debug(f"Initializing MythicAPIClient with task ID: {task_id}")

    async def login(self):
        logger.warning(f"Calling MythicRPCAPITokenCreateMessage with: {self.task_id}")
        resp = await SendMythicRPCAPITokenCreate(MythicRPCAPITokenCreateMessage(AgentTaskID=self.task_id))
        if resp.Success:
            api_key = resp.APIToken
        else:
            raise Exception(f"Failed to get API token for AgentTaskID {self.task_id}: {resp.Error}")
        
        ip = os.environ.get("NGINX_HOST", "127.0.0.1")
        port = int(os.environ.get("NGINX_PORT", 7443))
        ssl = True if os.environ.get("NGINX_SSL", "true").lower() in ['true', '1', 'yes'] else False
        self.client = await mythic.login(apitoken=api_key, server_ip=ip, server_port=port, ssl=ssl)
    
    async def get_all_tools(self):
        return [self.get_all_active_callbacks()]
    
    @rg.tool_method()
    async def get_all_active_callbacks(self) -> str:
        """Get information about all currently active agent callbacks.
        
        This tool retrieves details about all active agent connections (callbacks) in the Mythic framework,
        including callback IDs, agent information, and connection status.

        Returns:
            str: JSON string containing information about all active callbacks
        """
        if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
        resp = await mythic.get_all_active_callbacks(self.client)
        return json.dumps(resp)
    
    @rg.tool_method()
    async def get_all_payload_info(self) -> str:
        query = """
            query PayloadInfo {
                payloadtype {
                    agent_type
                    name
                    supported_os
                    buildparameters {
                        id
                        name
                        parameter_type
                        choices
                        default_value
                        description
                    }
                }
            }
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            results = await mythic.execute_custom_query(self.client, query,)
            return json.dumps(results)
        except Exception as e:
            return f"Error executing query: {e}"

    @rg.tool_method()
    async def get_c2_profiles_for_payload(self, payload: Annotated[str, "The name of the payload type to retrieve C2 profiles for such as 'merlin', 'apollo', etc."]):
        """Get C2 profiles for a specific payload type.
        Args:
            payload (str): The name of the payload type to retrieve C2 profiles for such as "merlin", "apollo", etc.
        Returns:
            str: JSON string containing C2 profile information for the specified payload type.
        """

        query = """
            query PayloadC2Profiles {
                payloadtypec2profile(where: {payloadtype: {name: {_eq: "merlin"}}}) {
                    payloadtype {
                    name
                    }
                    c2profile {
                    name
                    description
                    is_p2p
                    c2profileparameters {
                        name
                        description
                        parameter_type
                        required
                        default_value
                        choices
                    }
                    }
                }
            }
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            results = await mythic.execute_custom_query(self.client, query,)
            return json.dumps(results)
        except Exception as e:
            return f"Error executing query: {e}"

    @rg.tool_method()
    async def get_all_commands_for_payloadtype(self, payload: Annotated[str, "The name of the payload type (e.g., 'sage', 'apollo', 'poseidon') to get its available commands"]) -> str:
        """Get all available commands for a specific payload type (agent).
        
        This tool retrieves information about all commands available for a given payload type,
        including command parameters, descriptions, and requirements.

        Args:
            payload: The name of the payload type (e.g., 'sage', 'apollo', 'poseidon')
        Returns:
            str: JSON string containing all commands and their detailed information
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
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            results =  await mythic.get_all_commands_for_payloadtype(self.client, payload, attr)
            return json.dumps(results)
        except Exception as e:
            return f"Error getting commands for payload type {payload}: {e}"

    @rg.tool_method()
    async def create_payload(
        self,
        payload_type_name: str,
        filename: str,
        operating_system: str,
        c2_profiles: Annotated[List[Dict[str, str | Dict[str, str]]], "List of C2 profiles where each dict contains 'c2_profile' and 'c2_profile_parameters' keys"],
        build_parameters: Annotated[List[dict[str,str]], "List of build parameters where each dict contains 'name' and 'value' keys"],
        description: str = "",
    ) -> str:
        """Create a new payload with the specified parameters.

        Args:
            payload_type_name: The name of the payload type (e.g., 'sage', 'apollo').
            filename: The name of the output file from the created payload.
            operating_system: The operating system for which the payload is built (e.g., 'linux', 'windows').
            c2_profiles: A list of dictionaries where each dictionary holds the following information:
                {
                    "c2_profile": "http",
                    "c2_profile_parameters": {
                        "parameter name": "parameter value",
                        "parameter name 2": "parameter value 2"
                    }
                }
            build_parameters: a list of dictionaries where each dictionary holds the following payload build parameter information:
                {
                    "name": "build parameter name", "value": "build parameter value"
                }
            description: Optional description for the payload.

        Returns:
            str: JSON string containing the created payload information.
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call create() first.")
        resp = await mythic.create_payload(
            self.client,
            payload_type_name=payload_type_name,
            filename=filename,
            operating_system=operating_system,
            c2_profiles=c2_profiles,
            build_parameters=build_parameters,
            description=description,
            include_all_commands= True,  # Include all commands in the payload
        )
        return json.dumps(resp)

    @rg.tool_method()
    async def issue_task_and_waitfor_task_output(self, command: str, parameters: str|dict, callback_display_id: int, token_id: int | None = None, timeout: int | None = None) -> str:
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
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            results = await mythic.issue_task_and_waitfor_task_output(self.client, command, parameters, callback_display_id, token_id, timeout)
            if results is None:
                return "No results returned from task."
            else:
                return str(results)
        except Exception as e:
            return f"Error issuing command '{command}' to agent {callback_display_id}: {e}"
        
async def get_mythic_client(task_id: str) -> mythic_classes.Mythic:
    """Get a Mythic client instance."""
    logger.warning(f"Initializing MythicAPIClient with task ID: {task_id}")
    resp = await SendMythicRPCAPITokenCreate(MythicRPCAPITokenCreateMessage(AgentTaskID=task_id))
    if resp.Success:
        api_key = resp.APIToken
    else:
        raise Exception(f"Failed to get API token: {resp.Error}")
    
    ip = os.environ.get("NGINX_HOST", "127.0.0.1")
    port = int(os.environ.get("NGINX_PORT", 7443))
    ssl = True if os.environ.get("NGINX_SSL", "true").lower() in ['true', '1', 'yes'] else False
    client = await mythic.login(apitoken=api_key, server_ip=ip, server_port=port, ssl=ssl)
    return client