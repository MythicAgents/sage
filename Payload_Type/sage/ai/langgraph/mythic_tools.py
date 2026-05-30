import os
import json
import asyncio
import base64
from typing import Annotated, List, Dict, TypedDict
from mythic import mythic, mythic_classes
from mythic_container.MythicGoRPC import SendMythicRPCAPITokenCreate, MythicRPCAPITokenCreateMessage
from mythic_container.logging import logger
from langchain.tools import tool, BaseTool
from langchain_core.tools import StructuredTool

try:
    from . import ttp_library
except ImportError:  # allow running this module directly for manual testing
    import ttp_library

class MythicTools:
    """A class to manage Mythic API tools for LangChain agents.

    Attributes:
        client (mythic_classes.Mythic): The Mythic API client instance.
        agent_task_id (str): The agent task ID, from Mythic's taskData.Task.AgentTaskID, associated with the agent.

    Do not use the LangChain @tool decorator because it will cause a conflict with 'self' argument in class methods
    Tools should follow LangChain StructuredTool format and must contain a doc string for the description field.
        - https://python.langchain.com/docs/how_to/custom_tools/#structuredtool
        - https://python.langchain.com/api_reference/core/tools/langchain_core.tools.structured.StructuredTool.html
    Use annotated typing for arguments to provide additional context for the tool description.
    Create args_schema where possible to provide more detailed argument information. The schema should be a class that inherits from BaseModel.
        - https://docs.langchain.com/oss/python/langchain/tools#advanced-schema-definition
    """
    client: mythic_classes.Mythic | None
    agent_task_id: str

    def __init__(self, agent_task_id: str):
        """Initialize the MythicTools with the Mythic taskData.Task.AgentTaskID. Call create() to establish connection."""
        logger.debug(f"Initializing MythicAPIClient with task ID: {agent_task_id}")
        self.agent_task_id = agent_task_id
        self.client = None

    async def login(self):
        """Create the Mythic API client connection asynchronously."""
        logger.info(f"Calling MythicRPCAPITokenCreateMessage with: {self.agent_task_id}")

        resp = await SendMythicRPCAPITokenCreate(MythicRPCAPITokenCreateMessage(AgentTaskID=self.agent_task_id))
        if resp.Success:
            api_key = resp.APIToken
        else:
            raise Exception(f"Failed to get API token for AgentTaskID {self.agent_task_id}: {resp.Error}")

        ip = os.environ.get("NGINX_HOST", "127.0.0.1")
        port = int(os.environ.get("NGINX_PORT", 7443))
        ssl = True if os.environ.get("NGINX_SSL", "true").lower() in ['true', '1', 'yes'] else False
        self.client = await mythic.login(apitoken=api_key, server_ip=ip, server_port=port, ssl=ssl)
    
    def get_tools(self, method_names: list[str]) -> list[StructuredTool]:
        """Get Mythic tools by method names and return them as LangChain StructuredTool instances.

        Do not use the LangChain @tool decorator because it will cause a conflict with 'self' argument in class methods
        """

        tools = []
        for method_name in method_names:
            if hasattr(self, method_name):
                method = getattr(self, method_name)
                if asyncio.iscoroutinefunction(method):
                    tools.append(StructuredTool.from_function(
                        coroutine=method,
                        name=method_name,
                        description=method.__doc__ or f"Execute {method_name}"
                    ))
                else:
                    tools.append(StructuredTool.from_function(
                        func=method,
                        name=method_name,
                        description=method.__doc__ or f"Execute {method_name}"
                    ))
        return tools

    async def get_all_active_callbacks(self) -> str:

        """
        Retrieve detailed information about all active Mythic agents.

        This tool provides comprehensive details about all active Mythic agents, including their operating systems, network information, 
        process details, and user contexts. The returned information includes:

        - **architecture**: The architecture of the operating system (e.g., 386, amd64, arm, mips).
        - **description**: The description used when the Mythic Agent payload was created.
        - **domain**: The Windows domain associated with the host or user.
        - **external_ip**: The internet-facing IP address from the agent.
        - **host**: The host name where the agent is running.
        - **id**: The Mythic callback ID.
        - **integrity_level**: The Windows integrity level (1: low, 2: medium, 3: high).
        - **ip**: A list of local IP addresses on the host where the agent is running.
        - **pid**: The process ID for the Mythic agent.
        - **os**: The operating system the Mythic agent is running on.
        - **user**: The username that the Mythic agent is running as.
        - **process_name**: The name and file path of the process the Mythic agent is running as.
        - **sleep_info**: Information about the agent's sleep time.

        Returns:
            str: JSON string containing the agent's detailed information.
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug("🛠️ Calling get_all_active_callbacks tool")
        resp = await mythic.get_all_active_callbacks(self.client)
        return json.dumps(resp, sort_keys=True)

    async def get_all_payload_info(self) -> str:
        """ Get information about ALL payload types in Mythic. """
        query = """
            query PayloadInfo {
                payloadtype(where: { name: { _neq: "sage" } }) {
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
            logger.debug("🛠️ Calling get_all_payload_info tool")
            results = await mythic.execute_custom_query(self.client, query,)
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error executing query: {e}"
        
    async def get_payload_names(self) -> List[str]:
        """Get a list of all payload type names."""
        query = """
            query SagePayloadNames {
                payloadtype(where: { name: { _neq: "sage" } }) {
                    name
                }
            }
            """
        if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
        logger.debug("🛠️ Calling get_payload_names tool")
        resp = await mythic.execute_custom_query(self.client, query)
        # Payload names response: {'payloadtype': [{'name': 'sage'}, {'name': 'merlin'}]}, type: <class 'dict'>
        return [p['name'] for p in resp['payloadtype']]

    async def get_c2_profile_names(self) -> List[dict[str, str]]:
        """Get a list of all C2 profile names."""
        query = """
            query C2ProfileNames {
                c2profile {
                    name
                    description
                }
            }
            """
        if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
        logger.debug("🛠️ Calling get_c2_profile_names tool")
        resp = await mythic.execute_custom_query(self.client, query)
        # C2 profile names response: {'c2profile': [{'name': 'http', 'description': 'HTTP/S C2 Profile'}, {'name': 'websocket', 'description': 'WebSocket C2 Profile'}, {'name': 'dns', 'description': 'DNS C2 Profile'}]}, type: <class 'dict'>
        return [{'name': c['name'], 'description': c['description']} for c in resp['c2profile']] if resp.get('c2profile') else []
    
    async def get_c2_profiles_for_payload(self, payload: Annotated[str, "The name of the payload type to retrieve C2 profiles for such as 'merlin', 'apollo', etc."]):
        """Get C2 profiles for a specific payload type.
        Args:
            payload (str): The name of the payload type to retrieve C2 profiles for such as "merlin", "apollo", etc.
        Returns:
            str: JSON string containing C2 profile information for the specified payload type.
        """

        query = """
            query PayloadC2Profiles {
                payloadtypec2profile(where: {payloadtype: {name: {_eq: "PLACEHOLDER"}}}) {
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
        query = query.replace("PLACEHOLDER", payload)
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling get_c2_profiles_for_payload tool for: {payload}")
            results = await mythic.execute_custom_query(self.client, query,)
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error executing query: {e}"

    async def get_all_command_names_for_payloadtype(self, payload: Annotated[str, "The name of the payload type (e.g., 'sage', 'apollo', 'poseidon') to get its available commands"]) -> str:
        """Get all available command names for a specific payload type (agent).
        
        This tool retrieves all command names available for a given payload type.

        Args:
            payload: The name of the payload type (e.g., 'sage', 'apollo', 'poseidon')
        Returns:
            str: JSON string containing all commands and their detailed information
        """
        query = """
            query SageCommandNames {
                command(where: {payloadtype: {name: {_eq: "PLACEHOLDER"}}}) {
                    cmd
                    description
                }
            }
        """
        query = query.replace("PLACEHOLDER", payload)
        attr = """
        cmd
        description
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling get_all_command_names_for_payloadtype tool for: {payload}")
            results =  await mythic.execute_custom_query(self.client, query)
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error getting commands for payload type {payload}: {e}"

    async def get_all_command_args_for_payloadtype(
            self, 
            payload: Annotated[str, "The name of the payload type (e.g., 'sage', 'apollo', 'poseidon') to get its available commands"],
            command: Annotated[str, "The name of the command to get its arguments (e.g., 'ls', 'pwd', 'whoami')"]) -> str:
        """Get all of a command's arguments for a specific payload type (agent).
        
        This tool retrieves all information about a command's arguments available for a given payload type.

        Args:
            payload: The name of the payload type (e.g., 'sage', 'apollo', 'poseidon')
            command: The name of the command to get its arguments (e.g., 'ls', 'pwd', 'whoami')
        Returns:
            str: JSON string containing all commands and their detailed information
        """
        query = """
            query SageCommandArgs {
                command(where: {cmd: {_eq: "COMMAND"}, payloadtype: {name: {_eq: "PAYLOAD"}}}) {
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
                    help_cmd
                    needs_admin
                }
            }
        """
        query = query.replace("COMMAND", command).replace("PAYLOAD", payload)
 
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling get_all_command_args_for_payloadtype tool for: {payload}")
            results =  await mythic.execute_custom_query(self.client, query)
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error getting command {command} args for payload type {payload}: {e}"
    
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
            logger.debug(f"🛠️ Calling get_all_commands_for_payloadtype tool for: {payload}")
            results =  await mythic.get_all_commands_for_payloadtype(self.client, payload, attr)
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error getting commands for payload type {payload}: {e}"

    async def create_payload(
        self,
        payload_type_name: str,
        filename: str,
        operating_system: str,
        c2_profiles: Annotated[List[Dict[str, str | Dict[str, str]]], "List of C2 profiles where each dict contains 'c2_profile' and 'c2_profile_parameters' keys"],
        build_parameters: Annotated[List[dict[str,str]], "List of build parameters where each dict contains 'name' and 'value' keys"],
        description: str = "",
    ) -> str:
        """Create a new Mythic payload (also known as a Mythic agent) with the specified parameters.
        Returns the created payload information as a JSON string.

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
        # uuid is the Payload UUID not to be confused with the Mythic file UUID
        custom_attributes = """
        build_phase
        uuid
        build_stdout
        build_stderr
        build_message
        id
        filemetum {
            agent_file_id
        }
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call create() first.")
        logger.debug(f"🛠️ Calling create_payload tool for: {payload_type_name}, filename: {filename}")
        resp = await mythic.create_payload(
            self.client,
            payload_type_name=payload_type_name,
            filename=filename,
            operating_system=operating_system,
            c2_profiles=c2_profiles,
            build_parameters=build_parameters,
            description=description,
            include_all_commands= True,  # Include all commands in the payload
            custom_return_attributes=custom_attributes,
        )
        return json.dumps(resp, sort_keys=True)

    async def issue_task_and_waitfor_task_output(self, command: str, parameters: str|dict, callback_display_id: int, token_id: int | None = None, timeout: int | None = None) -> str:
        """
        Issue a task to execute 'command' on the specified agent and wait for the agent to checkin, execute the task, and return the results.
        **IMPORTANT**: When a command has a parameter type of "File" (e.g., "type": "File"), you must pass in the Mythic file UUID (not the filename).

        Args:
            command: The command name to execute from the "cmd" field from the get_all_commands_for_payloadtype tool. Validate the agent's operating system and the supported_os match.
            parameters: The command's parameters or arguments. Prefer a JSON string that leverages the commandparameters "name" value (e.g. {"arguments": "value"}). Alternatively, use a non-JSON string that has dash with the "cli_name" field (e.g. -path /etc/issue).
            callback_display_id: The callback_display_id of the target agent to run the command on.
            token_id: Optional Mythic identifier for tracked Windows user access tokens to use for impersonation.
            timeout: Optional timeout in seconds for the task to complete.
        Returns:
            str: Command output (binary output coerced to string).
        """
        if timeout is None:
            timeout = 300  # Default timeout of 5 minutes
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling issue_task_and_waitfor_task_output tool for command: {command} on callback_display_id: {callback_display_id}")
            results = await mythic.issue_task_and_waitfor_task_output(mythic=self.client, command_name=command, parameters=parameters, callback_display_id=callback_display_id, timeout=timeout) #token_id=token_id
            if results is None:
                return "No results returned from task."
            else:
                return str(results)
        except Exception as e:
            return f"Error issuing command '{command}' to agent {callback_display_id}: {e}"

    async def get_task_history_for_callback(self, callback_display_id: Annotated[int, "The callback_display_id of the target agent to retrieve task history for"]) -> str:
        """Get the task history of commands issued for a specific agent (callback).

        This tool retrieves detailed information about all tasks issued to a specific Mythic agent, including the following fields:

        - **id**: The ID associated with the task.
        - **operator**: The Mythic operator who issued the command.
        - **status**: The status of the task (e.g., success, completed, agent_processing, error).
        - **completed**: Whether the task is completed (True/False).
        - **original_params**: The original parameters or arguments issued with the command.
        - **timestamp**: The timestamp when the command was issued.
        - **command_name**: The name of the command issued to the Mythic agent.

        Args:
            callback_display_id: The callback_display_id of the target agent to retrieve task history for.
        Returns:
            str: JSON string containing the task history for the specified agent.
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling get_task_history_for_callback tool on callback_display_ids: {callback_display_id}")
        resp = await mythic.get_all_tasks(mythic=self.client, callback_display_id=callback_display_id)
        return json.dumps(resp, sort_keys=True)
    
    async def get_all_task_output_by_task_id(self, task_id: Annotated[int, "The Mythic task ID to retrieve output for"]) -> str:
        """Get all output associated with a specific Mythic task ID.

        This tool retrieves all output generated by a specific Mythic task, including standard output, error messages, and any other relevant information produced during the execution of the task.

        The response_text field will be automatically decoded from base64 if possible, making it easier for the LLM to process.

        Args:
            task_id: The Mythic task ID to retrieve output for.
        Returns:
            str: JSON string containing all output for the specified task ID, with response_text decoded from base64.
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling get_all_task_output_by_task_id tool for task IDs: {task_id}")
        resp = await mythic.get_all_task_output_by_id(mythic=self.client, task_display_id=task_id)

        # Decode base64 response_text fields for easier LLM processing
        if isinstance(resp, list):
            for item in resp:
                if isinstance(item, dict) and "response_text" in item:
                    try:
                        # Try to decode base64
                        decoded_bytes = base64.b64decode(item["response_text"])
                        # Try to decode as UTF-8 text
                        decoded_text = decoded_bytes.decode('utf-8')
                        item["response_text"] = decoded_text
                        logger.debug(f"Successfully decoded base64 response_text for task output {item.get('id', 'unknown')}")
                    except (Exception, UnicodeDecodeError) as e:
                        # If decode fails, keep the original base64 string
                        logger.debug(f"Failed to decode base64 response_text for task output {item.get('id', 'unknown')}: {e}")
                        pass

        return json.dumps(resp, sort_keys=True)
    
    async def get_all_uploaded_files(self) -> str:
        """
        Get a list of all files uploaded to Mythic.
        Uploaded files can include, but not limited to, additional tools, scripts, or binaries that operators have uploaded for use with Mythic agents.
        Excludes files downloaded by Mythic agents, screenshots, and Mythic payload files.
        Call the download_file() method to download a specific file by its Mythic file UUID.
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug("🛠️ Calling get_all_uploaded_files tool")
        resp = mythic.get_all_uploaded_files(mythic=self.client)
        data = [item async for item in resp]
        return json.dumps(data, sort_keys=True)

    async def upload_file_by_file_uuid(
            self,
            command: Annotated[str, "The name of the command for a Mythic agent that has upload functionality, typically the \"upload\" command, to execute on the target agent"], 
            parameters: Annotated[str|dict, "Parameters for the upload command"], 
            file_uuid: Annotated[str, "The Mythic file UUID to upload to the target agent"], 
            callback_display_id: Annotated[int, "The callback_display_id of the target agent to upload the file to"],
            token_id: Annotated[int | None, "Optional token ID for authentication"] = None, 
            timeout: Annotated[int | None, "Optional timeout for the upload operation"] = None,
            ) -> str:
        """Upload a file stored in Mythic to a specific Mythic agent by the Mythic file UUID.

        This tool uploads a file, identified by its Mythic file UUID, to a specified Mythic agent. The file will be transferred to the agent associated with the provided callback_display_id.

        **IMPORTANT**: The command's parameter must be of type "File" (e.g., "type": "File"). DO NOT USE PARAMETER TYPE "STRING" TO UPLOAD FILES.
        For the Merlin agent, use the "upload" command with the "file" parameter set to the Mythic file UUID and the "path" parameter set to the destination path and file name on the target system.
        
        Args:
            command: The name of the command for a Mythic agent that has upload functionality, typically the "upload" command.
            parameters: Parameters a Mythic agent's upload command.
            file_uuid: The Mythic file UUID to upload to the target agent.
            callback_display_id: The callback_display_id of the target agent to upload the file to.
            token_id: Optional token ID for authentication.
            timeout: Optional timeout for the upload operation.
        Returns:
            str: Command output (binary output coerced to string) after the upload operation."""
        
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling upload_file_by_file_id tool for file ID {file_uuid} and callback_display_id {callback_display_id}")
        file = await mythic.download_file(mythic=self.client, file_uuid=file_uuid)
        if file is None or len(file) == 0:
            raise Exception(f"Failed to download file with UUID: {file_uuid}")
        resp = await self.issue_task_and_waitfor_task_output(
            command=command,
            parameters=parameters,
            callback_display_id=callback_display_id,
            token_id=token_id,
            timeout=timeout
        ) 
        return resp

    async def download_file(self, file_uuid: Annotated[str, "The Mythic file UUID of the file to download from Mythic"]) -> str:
        # Not sure what I'm going to use this for because I don't want to send the file data back to the LLM
        """Download a file from Mythic by its Mythic file UUID.

        This tool downloads a file stored in Mythic, identified by its Mythic file UUID. 
        The file content is returned as a base64-encoded string.

        Args:
            file_uuid: The Mythic file UUID to download the file for.
        Returns:
            str: Base64-encoded string of the downloaded file content.
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling download_file tool for file UUID: {file_uuid}")
        file_content = await mythic.download_file(mythic=self.client, file_uuid=file_uuid)
        if file_content is None:
            raise Exception(f"Failed to download file with UUID: {file_uuid}")
        # Encode the binary content to base64 string for easier transport
        encoded_content = base64.b64encode(file_content).decode('utf-8')
        return encoded_content

    async def get_operations(self) -> str:
        """Get a list of all operations in Mythic."""
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug("🛠️ Calling get_operations tool")
        resp = await mythic.get_operations(mythic=self.client)
        return json.dumps(resp, sort_keys=True)

    async def _resolve_payload_type(self, callback_display_id: int) -> str | None:
        """Best-effort lookup of a callback's Mythic payload type name (e.g. 'apollo').

        Returns None if it can't be resolved; TTP guidance still works without it,
        just without the agent-specific execution join.
        """
        if self.client is None:
            return None
        try:
            callbacks = await mythic.get_all_active_callbacks(self.client)
            for cb in callbacks or []:
                if cb.get("display_id") == callback_display_id or cb.get("id") == callback_display_id:
                    payload = cb.get("payload")
                    if isinstance(payload, dict):
                        ptype = payload.get("payloadtype")
                        if isinstance(ptype, dict) and ptype.get("name"):
                            return ptype["name"]
                    return cb.get("payload_type") or cb.get("payloadtype")
        except Exception as e:
            logger.debug(f"Could not resolve payload type for callback {callback_display_id}: {e}")
        return None

    async def get_ttp_guidance(
        self,
        goal: Annotated[str, "The tradecraft goal in plain language, e.g. 'enumerate the domain', 'dump LSASS', 'abuse a GPO', 'request an ADCS cert'."],
        callback_display_id: Annotated[int, "The callback_display_id of the agent you intend to run the tradecraft on. Used to tailor execution guidance to that Mythic agent."],
    ) -> str:
        """Get C2-agnostic tradecraft guidance for a goal, joined to how the target agent runs it.

        This is the FIRST tool to consult before reaching for a specific offensive tool. It
        matches your goal to Sage's TTP knowledge library and returns the tool's frontmatter
        (including `common_args` and `usage_examples`) plus the prose guidance UP TO the
        "## Full Reference" section, along with an `execution_on_agent` hint describing how the
        callback's Mythic agent runs that binary type.

        Progressive disclosure: rely on `common_args` and `usage_examples` first. Only call
        `get_ttp_full_reference(slug)` when you need an uncommon flag, exact output format, or
        version-specific behavior the guidance doesn't cover.

        Args:
            goal: Plain-language tradecraft goal.
            callback_display_id: Target agent's callback_display_id (tailors execution guidance).
        Returns:
            str: JSON with matched slug, frontmatter, guidance prose, execution_on_agent, and
                 whether a Full Reference is available.
        """
        logger.debug(f"🛠️ Calling get_ttp_guidance tool (goal={goal!r}, callback={callback_display_id})")
        matches = ttp_library.match_goal(goal)
        if not matches:
            categories = ttp_library.list_categories()
            return json.dumps({
                "status": "no_match",
                "goal": goal,
                "available_categories": sorted(categories.keys()),
                "hint": "Call list_ttp_categories for the full catalog, or restate the goal using tradecraft terms.",
            }, sort_keys=True)

        slug, _score = matches[0]
        frontmatter, body = ttp_library.load_ttp(slug)
        payload_type = await self._resolve_payload_type(callback_display_id)
        agent_frontmatter, _ = ttp_library.load_mythic_agent(payload_type)
        if agent_frontmatter:
            execution = ttp_library.execution_hint(frontmatter, agent_frontmatter)
        elif payload_type:
            execution = (f"No capability file for Mythic agent '{payload_type}' under mythic_agents/. "
                         f"Use get_all_commands_for_payloadtype('{payload_type}') to discover its command surface.")
        else:
            execution = ("Could not resolve the callback's payload type. Use get_all_active_callbacks "
                         "then get_all_commands_for_payloadtype to map execution.")

        return json.dumps({
            "slug": slug,
            "alternatives": [s for s, _ in matches[1:]],
            "payload_type": payload_type,
            "frontmatter": frontmatter,
            "guidance": ttp_library.guidance_body(body),
            "execution_on_agent": execution,
            "full_reference_available": ttp_library._FULL_REFERENCE_HEADING in (body or ""),
            "next": "Use common_args + usage_examples first; call get_ttp_full_reference(slug) only for uncommon flags, output format, or version specifics.",
        }, sort_keys=True)

    async def get_ttp_full_reference(
        self,
        slug: Annotated[str, "The TTP slug (filename without .md), e.g. 'sharphound', returned as 'slug' by get_ttp_guidance."],
    ) -> str:
        """Get the full '## Full Reference' section for a TTP (comprehensive args, output, versions).

        Call this only after get_ttp_guidance when its `common_args`/`usage_examples` don't cover
        the flag, output format, or version-specific behavior you need. This is the deep, expensive
        tier of the progressive-disclosure pattern.

        Args:
            slug: The TTP slug returned by get_ttp_guidance.
        Returns:
            str: JSON with the full reference text, or a not_found / no_full_reference status.
        """
        logger.debug(f"🛠️ Calling get_ttp_full_reference tool (slug={slug!r})")
        frontmatter, body = ttp_library.load_ttp(slug)
        if body is None:
            available = [s for s, _, _ in ttp_library.iter_ttps()]
            return json.dumps({"status": "not_found", "slug": slug, "available_ttps": available}, sort_keys=True)
        reference = ttp_library.full_reference(body)
        if not reference:
            return json.dumps({
                "status": "no_full_reference",
                "slug": slug,
                "note": "This TTP has no Full Reference yet; rely on common_args and usage_examples from get_ttp_guidance.",
            }, sort_keys=True)
        return json.dumps({"slug": slug, "full_reference": reference}, sort_keys=True)

    async def list_ttp_categories(self) -> str:
        """List Sage's TTP knowledge library grouped by category.

        Use this to discover what tradecraft Sage has structured guidance for before forming a
        plan. Each category lists the tools (slug + name) available under it.

        Returns:
            str: JSON mapping each category to a list of {slug, name}, or an empty status.
        """
        logger.debug("🛠️ Calling list_ttp_categories tool")
        categories = ttp_library.list_categories()
        if not categories:
            return json.dumps({
                "status": "empty",
                "note": "No TTP files present yet at Payload_Type/sage/ttps/.",
            }, sort_keys=True)
        return json.dumps(categories, sort_keys=True)

    async def ensure_tool_uploaded(
        self,
        binary_filename: Annotated[str, "The tool binary filename, e.g. 'SharpHound.exe' (matches a TTP's binary_filename)."],
    ) -> str:
        """Ensure a tool binary is in Mythic's file store, uploading it from the tools/ drop zone if needed.

        Workflow: (1) check Mythic's file store by name; (2) if absent, look for the file in the
        operator drop zone at Payload_Type/sage/tools/<binary_filename>; (3) if found, register it
        with Mythic via register_file. Returns the Mythic file UUID to pass as the File parameter of
        a subsequent issue_task_and_waitfor_task_output call (e.g. assembly_file for inline_assembly).

        Args:
            binary_filename: The tool binary filename (matches a TTP's binary_filename).
        Returns:
            str: JSON with status and, when available, the Mythic file UUID.
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling ensure_tool_uploaded tool (binary={binary_filename!r})")
        # 1. Already in Mythic's file store?
        try:
            existing = await mythic.get_latest_uploaded_file_by_name(self.client, filename=binary_filename)
            if existing:
                uuid = existing.get("agent_file_id") or existing.get("id")
                if uuid:
                    return json.dumps({"status": "already_present", "binary_filename": binary_filename, "file_uuid": uuid}, sort_keys=True)
        except Exception as e:
            logger.debug(f"get_latest_uploaded_file_by_name failed for {binary_filename}: {e}")
        # 2. Operator drop zone
        local_path = ttp_library.TOOLS_DIR / binary_filename
        if not local_path.is_file():
            return json.dumps({
                "status": "missing",
                "binary_filename": binary_filename,
                "note": f"Not in Mythic's file store and not found at tools/{binary_filename}. "
                        f"Operator must drop the binary into Payload_Type/sage/tools/ or upload it to Mythic first.",
            }, sort_keys=True)
        # 3. Register the local file with Mythic
        try:
            file_uuid = await mythic.register_file(self.client, filename=binary_filename, contents=local_path.read_bytes())
            return json.dumps({"status": "uploaded", "binary_filename": binary_filename, "file_uuid": file_uuid}, sort_keys=True)
        except Exception as e:
            return json.dumps({"status": "error", "binary_filename": binary_filename, "error": str(e)}, sort_keys=True)

    def _run_sandbox_sync(self, command: list[str], image: str, timeout: int,
                          mem_limit: str, pids_limit: int, work_size: str) -> dict:
        """Blocking sandbox run (offloaded via asyncio.to_thread). Returns a result dict.

        All isolation is applied here at create time: no privileges, all caps dropped,
        no-new-privileges, read-only rootfs, size-capped tmpfs work dir, network disabled,
        mem/pids caps, non-root user, hard timeout + force-remove. See Plans/SANDBOX_DESIGN.md.
        """
        import docker
        from docker.errors import ImageNotFound

        client = docker.from_env()
        # Ensure the sandbox image exists; build from container/sandbox/ on first use.
        try:
            client.images.get(image)
        except ImageNotFound:
            # __file__ = .../sage/ai/langgraph/mythic_tools.py -> up 3 dirs = .../sage/
            sage_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            sandbox_ctx = os.path.join(sage_root, "container", "sandbox")
            logger.info(f"Building sandbox image '{image}' from {sandbox_ctx} (first use)")
            client.images.build(path=sandbox_ctx, tag=image, rm=True)

        container = client.containers.run(
            image=image,
            command=command,
            detach=True,
            network_disabled=True,
            read_only=True,
            tmpfs={"/sandbox/work": f"size={work_size},uid=10001"},
            working_dir="/sandbox/work",
            user="10001",
            mem_limit=mem_limit,
            pids_limit=pids_limit,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            auto_remove=False,
        )
        timed_out = False
        exit_code = -1
        try:
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", -1)
            except Exception:
                # docker-py raises on wait timeout (ReadTimeout/ConnectionError) -> kill it
                timed_out = True
                try:
                    container.kill()
                except Exception:
                    pass
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
        finally:
            try:
                container.remove(force=True)  # ephemeral — never leave it behind
            except Exception:
                pass

        cap = 20000  # truncate large output before it reaches the LLM
        return {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": stdout[:cap],
            "stderr": stderr[:cap],
            "truncated": len(stdout) > cap or len(stderr) > cap,
        }

    async def sandbox_exec(
        self,
        code_or_command: Annotated[str, "The code or shell command to run in the isolated sandbox."],
        language: Annotated[str, "'shell' (sh -c) or 'python' (python3 -c). Default 'shell'."] = "shell",
        timeout: Annotated[int | None, "Wall-clock timeout in seconds; default 30, clamped to 1-120."] = None,
    ) -> str:
        """Run untrusted code in an isolated, ephemeral sandbox container, returning stdout/stderr/exit.

        The code runs in a throwaway `sage-sandbox` container with NO access to the Sage container's
        filesystem (Mythic tokens, sage.db, TTP files), NO host mounts, NO network, dropped capabilities,
        a read-only rootfs + small tmpfs work dir, memory/pid caps, a non-root user, and a hard timeout.
        Use this for ad-hoc parsing/scripting that should NOT run in the Sage container or on the host.

        # HITL: guarded  (code execution — supervised mode must gate this; Phase 2 reads this tag)

        Args:
            code_or_command: The code/command to execute.
            language: 'shell' or 'python'.
            timeout: Seconds (default 30, max 120).
        Returns:
            str: JSON {status, exit_code, stdout, stderr, timed_out, truncated} or {status:error,...}.
        """
        try:
            import docker  # noqa: F401  (lazy: module must import even when the SDK is absent)
        except ImportError:
            return json.dumps({"status": "error", "error": "docker SDK not installed in the Sage container; add 'docker' to requirements and rebuild."}, sort_keys=True)

        timeout = 30 if timeout is None else max(1, min(int(timeout), 120))
        if language == "python":
            command = ["python3", "-c", code_or_command]
        elif language == "shell":
            command = ["sh", "-c", code_or_command]
        else:
            return json.dumps({"status": "error", "error": f"unsupported language '{language}'; use 'shell' or 'python'."}, sort_keys=True)

        logger.debug(f"🛠️ Calling sandbox_exec (language={language}, timeout={timeout}s)")
        try:
            result = await asyncio.to_thread(
                self._run_sandbox_sync, command, "sage-sandbox:latest", timeout, "512m", 256, "128m"
            )
            result["status"] = "ok"
            return json.dumps(result, sort_keys=True)
        except Exception as e:
            return json.dumps({"status": "error", "error": f"sandbox execution failed: {e}"}, sort_keys=True)

# Create a main function with arguments so that I can test the methods in this class manually
if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Test MythicTools class methods.")
    parser.add_argument("agent_task_id", type=str, help="The Mythic agent task ID to initialize the client.")
    parser.add_argument("method", type=str, help="The method to test (e.g., get_payload_names, get_all_active_callbacks).")
    args = parser.parse_args()

    async def main():
        if "RABBITMQ_PASSWORD" not in os.environ or "RABBITMQ_HOST" not in os.environ:
            print("Error: RABBITMQ_PASSWORD and RABBITMQ_HOST environment variables must be set.")
            return
        mythic_tools = MythicTools(agent_task_id=args.agent_task_id)
        await mythic_tools.login()
        method = getattr(mythic_tools, args.method, None)
        if method and asyncio.iscoroutinefunction(method):
            result = await method()
            print(f"Result from {args.method}:\n{result}")
        else:
            print(f"Method {args.method} not found or is not asynchronous.")

    asyncio.run(main())