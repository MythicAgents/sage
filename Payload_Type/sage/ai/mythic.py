import asyncio
import os
import re
import inspect
import json
import threading
import logging
from contextlib import contextmanager
from typing import Dict, List
from langchain_core.tools import BaseTool
from mythic_container.MythicRPC import MythicRPCAPITokenCreateMessage
from mythic import mythic
from mythic_container.MythicGoRPC import SendMythicRPCAPITokenCreate, MythicRPCAPITokenCreateMessage
from mythic_container.logging import logger

def mythic_tool(func):
    """Decorator to mark MythicAPIClient methods as available tools"""
    func._is_mythic_tool = True
    return func

class MythicAPIClient:
    """Wrapper for Mythic API interactions"""
    def __init__(self):
        """Initialize the MythicAPIClient with a task ID"""
        self.api_key = None
        self.ip = None
        self.port = None
        self.ssl = None
        self.client = None
    
    @classmethod
    async def create(cls, task_id: str):
        """Async factory method to create and initialize MythicAPIClient"""
        instance = cls()
        await instance._initialize(task_id)
        return instance

    async def _initialize(self, task_id: str):
        """Private async initialization method"""
        logger.warning(f"Initializing MythicAPIClient with task ID: {task_id}")
        resp = await SendMythicRPCAPITokenCreate(MythicRPCAPITokenCreateMessage(AgentTaskID=task_id))
        if resp.Success:
            self.api_key = resp.APIToken
        else:
            raise Exception(f"Failed to get API token: {resp.Error}")
        
        self.ip = os.environ.get("NGINX_HOST", "127.0.0.1")
        self.port = int(os.environ.get("NGINX_PORT", 7443))
        self.ssl = True if os.environ.get("NGINX_SSL", "true").lower() in ['true', '1', 'yes'] else False
        self.client = await mythic.login(
            apitoken=self.api_key,
            server_ip=self.ip, 
            server_port=self.port, 
            ssl=self.ssl
        )
    
    @mythic_tool
    async def get_all_commands_for_payloadtype(self, payload: str) -> str:
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
    
    @mythic_tool
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

    @mythic_tool
    async def get_all_payload_info(self) -> str:
        """Get information about all payload types registered with Mythic.
        This information includes payload names, supported operating systems, and build parameters.

        Returns:
            str: JSON string containing information about all payload types
        """
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
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call create() first.")
        resp = await mythic.execute_custom_query(self.client, query)
        return json.dumps(resp)
    
    @mythic_tool
    async def get_c2_profiles_for_payload(self, payload: str) -> str:
        """Get information about the supported C2 profile for a specific payload type (agent).
        
        This tool retrieves details about all C2 profiles associated with a given payload type,
        including profile names, descriptions, and parameters.

        Args:
            payload: The name of the payload type (e.g., 'merlin', 'sage', 'apollo', 'poseidon')
        Returns:
            str: JSON string containing C2 profile information for the specified payload type
        """
        query = """
        query PayloadC2Profiles($payload: String!) {
          payloadtypec2profile(where: {payloadtype: {name: {_eq: $payload}}}) {
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
        variables = {"payload": payload}
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            results = await mythic.execute_custom_query(self.client, query, variables)
            return json.dumps(results)
        except Exception as e:
            return f"Error executing query: {e}"
    
    @mythic_tool
    async def get_all_payloads(self) -> str:
        """Get information about all payloads currently registered (already built) with Mythic (this includes deleted payloads and autogenerated ones for tasking).

        Returns:
            str: JSON string containing information about all payloads
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call create() first.")
        resp = await mythic.get_all_payloads(self.client)
        return json.dumps(resp)

    @mythic_tool
    async def create_payload(
        self,
        payload_type_name: str,
        filename: str,
        operating_system: str,
        c2_profiles: List[dict],
        build_parameters: List[dict],
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
    
    @mythic_tool
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
        
    @mythic_tool
    async def execute_graphql_query(self, query: str, variables: dict={}) -> str:
        """Execute a Mythic graphql query.

        Args:
            query: The graphql query to execute.
            variables: The variables parameter should contain a JSON object with key-value pairs that correspond to the variable names used in your GraphQL query. Each key should match a variable name defined in the query (without the $ prefix), and each value should match the expected type for that variable. These variables will be passed to the GraphQL server along with the query to provide dynamic values for the operation.
        Returns:
            str: JSON string of the Mythic GraphQL response.
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            results = await mythic.execute_custom_query(self.client, query, variables)
            return json.dumps(results)
        except Exception as e:
            return f"Error executing query: {e}"

    def get_all_tools(self):
        """Returns a simple list of callable tool methods for internal iteration."""
        tools = []
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if callable(attr) and hasattr(attr, '_is_mythic_tool'):
                tools.append({
                    'name': attr_name,
                    'method': attr,
                    'callable': attr
                })
        return tools
    
    def get_tool_definitions_for_llm(self):
        """Returns OpenAI-compatible tool definitions for LLM consumption."""
        tool_definitions = []
        
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if callable(attr) and hasattr(attr, '_is_mythic_tool'):
                tool_def = self._create_tool_definition(attr_name, attr)
                tool_definitions.append(tool_def)
        
        return tool_definitions
    
    def get_langchain_tools(self) -> List[BaseTool]:
        """Returns LangChain BaseTool instances for all Mythic tools."""
        tools = []
        
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if callable(attr) and hasattr(attr, '_is_mythic_tool'):
                tool = self._create_langchain_tool(attr_name, attr)
                tools.append(tool)
        
        return tools
    
    def _create_tool_definition(self, name, method):
        """Creates a single tool definition for the LLM."""
        # Get function signature
        sig = inspect.signature(method)
        
        # Parse docstring for parameter descriptions
        docstring = method.__doc__ or ""
        param_descriptions = self._parse_docstring_params(docstring)
        
        # Build parameters object
        properties = {}
        required = []
        
        for param_name, param in sig.parameters.items():
            # Skip 'self' parameter
            if param_name == 'self':
                continue
                
            # Determine parameter type from annotation
            param_type = self._get_param_type(param.annotation)
            
            properties[param_name] = {
                "type": param_type,
                "description": param_descriptions.get(param_name, f"Parameter {param_name}")
            }
            
            # Check if parameter is required (no default value)
            if param.default == inspect.Parameter.empty:
                required.append(param_name)
        
        return {
            "name": name,
            "description": self._clean_docstring_description(docstring),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    
    def _get_param_type(self, annotation):
        """Convert Python type annotation to JSON schema type."""
        if annotation == inspect.Parameter.empty:
            return "string"  # default
        
        type_mapping = {
            str: "string",
            int: "integer", 
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object"
        }
        
        return type_mapping.get(annotation, "string")
    
    def _create_langchain_tool(self, name: str, method) -> BaseTool:
        """Creates a LangChain BaseTool instance for a Mythic tool method."""
        from langchain_core.tools import tool
        
        # Get docstring
        docstring = method.__doc__ or ""
        
        # Create wrapper function with explicit parameters
        if name == "get_all_commands_for_payloadtype":
            @tool
            async def get_all_commands_for_payloadtype(payload: str) -> str:
                """Get all available commands for a specific payload type (agent).
                
                This tool retrieves information about all commands available for a given payload type,
                including command parameters, descriptions, and requirements.

                Args:
                    payload: The name of the payload type (e.g., 'sage', 'apollo', 'poseidon')
                Returns:
                    str: JSON string containing all commands and their detailed information
                """
                try:
                    result = await self.execute_tool("get_all_commands_for_payloadtype", payload=payload)
                    return str(result)
                except Exception as e:
                    logger.error(f"Error executing get_all_commands_for_payloadtype: {e}")
                    return f"Error executing tool: {str(e)}"
            return get_all_commands_for_payloadtype
            
        elif name == "get_all_active_callbacks":
            @tool
            async def get_all_active_callbacks() -> str:
                """Get information about all currently active agent callbacks.
                
                This tool retrieves details about all active agent connections (callbacks) in the Mythic framework,
                including callback IDs, agent information, and connection status.

                Returns:
                    str: JSON string containing information about all active callbacks
                """
                try:
                    result = await self.execute_tool("get_all_active_callbacks")
                    return str(result)
                except Exception as e:
                    logger.error(f"Error executing get_all_active_callbacks: {e}")
                    return f"Error executing tool: {str(e)}"
            return get_all_active_callbacks
        elif name == "create_payload":
            @tool
            async def create_payload(
                payload_type_name: str,
                filename: str,
                operating_system: str,
                c2_profiles: List[dict],
                build_parameters: List[dict],
                description: str = ""
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
                try:
                    result = await self.execute_tool("create_payload", 
                                                     payload_type_name=payload_type_name, 
                                                     filename=filename, 
                                                     operating_system=operating_system, 
                                                     c2_profiles=c2_profiles, 
                                                     build_parameters=build_parameters, 
                                                     description=description)
                    return str(result)
                except Exception as e:
                    logger.error(f"Error executing create_payload: {e}")
                    return f"Error executing tool: {str(e)}"
            return create_payload
        elif name == "get_c2_profiles_for_payload":
            @tool
            async def get_c2_profiles_for_payload(payload: str) -> str:
                """Get information about the supported C2 profile for a specific payload type (agent).
                
                This tool retrieves details about all C2 profiles associated with a given payload type,
                including profile names, descriptions, and parameters.

                Args:
                    payload: The name of the payload type (e.g., 'merlin', 'sage', 'apollo', 'poseidon')
                Returns:
                    str: JSON string containing C2 profile information for the specified payload type
                """
                try:
                    result = await self.execute_tool("get_c2_profiles_for_payload", payload=payload)
                    return str(result)
                except Exception as e:
                    logger.error(f"Error executing get_c2_profiles_for_payload: {e}")
                    return f"Error executing tool: {str(e)}"
            return get_c2_profiles_for_payload
        elif name == "get_all_payload_info":
            @tool
            async def get_all_payload_info() -> str:
                """Get information about all payload types registered with Mythic.
                This information includes payload names, supported operating systems, and build parameters.

                Returns:
                    str: JSON string containing information about all payload types
                """
                try:
                    result = await self.execute_tool("get_all_payload_info")
                    return str(result)
                except Exception as e:
                    logger.error(f"Error executing get_all_payload_info: {e}")
                    return f"Error executing tool: {str(e)}"
            return get_all_payload_info
        elif name == "get_all_payloads":
            @tool
            async def get_all_payloads() -> str:
                """Get information about all payloads currently registered with Mythic (this includes deleted payloads and autogenerated ones for tasking).

                Returns:
                    str: JSON string containing information about all payloads
                """
                try:
                    result = await self.execute_tool("get_all_payloads")
                    return str(result)
                except Exception as e:
                    logger.error(f"Error executing get_all_payloads: {e}")
                    return f"Error executing tool: {str(e)}"
            return get_all_payloads
            
        elif name == "issue_task_and_waitfor_task_output":
            @tool
            async def issue_task_and_waitfor_task_output(command: str, parameters: str, callback_display_id: int, token_id: int | None = None, timeout: int | None = None) -> str:
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
                    result = await self.execute_tool("issue_task_and_waitfor_task_output", 
                                                    command=command, 
                                                    parameters=parameters, 
                                                    callback_display_id=callback_display_id, 
                                                    token_id=token_id, 
                                                    timeout=timeout)
                    return str(result)
                except Exception as e:
                    logger.error(f"Error executing issue_task_and_waitfor_task_output: {e}")
                    return f"Error executing tool: {str(e)}"
            return issue_task_and_waitfor_task_output
            
        elif name == "execute_graphql_query":
            @tool
            async def execute_graphql_query(query: str, variables: dict = {}) -> str:
                """Execute a Mythic graphql query.

                Args:
                    query: The graphql query to execute.
                    variables: The variables parameter should contain a JSON object with key-value pairs that correspond to the variable names used in your GraphQL query. Each key should match a variable name defined in the query (without the $ prefix), and each value should match the expected type for that variable. These variables will be passed to the GraphQL server along with the query to provide dynamic values for the operation.
                Returns:
                    str: JSON string of the Mythic GraphQL response.
                """
                try:
                    result = await self.execute_tool("execute_graphql_query", 
                                                    query=query, 
                                                    variables=variables)
                    return str(result)
                except Exception as e:
                    logger.error(f"Error executing execute_graphql_query: {e}")
                    return f"Error executing tool: {str(e)}"
            return execute_graphql_query
        
        # Fallback for any other tools - dynamic creation
        else:
            # Get the original method to inspect its signature
            original_method = getattr(self, name)
            sig = inspect.signature(original_method)
            
            # Create wrapper function with proper signature
            async def generic_tool_wrapper(*args, **kwargs):
                try:
                    # Bind arguments to parameters
                    bound_args = sig.bind(self, *args, **kwargs)
                    bound_args.apply_defaults()
                    
                    # Remove 'self' from arguments
                    call_kwargs = dict(bound_args.arguments)
                    call_kwargs.pop('self', None)
                    
                    result = await self.execute_tool(name, **call_kwargs)
                    return str(result)
                except Exception as e:
                    logger.error(f"Error executing {name}: {e}")
                    return f"Error executing tool {name}: {str(e)}"
            
            # Set metadata
            generic_tool_wrapper.__name__ = name
            generic_tool_wrapper.__doc__ = docstring
            
            return tool(generic_tool_wrapper)
    
    def _parse_docstring_params(self, docstring):
        """Extract parameter descriptions from docstring Args section."""
        param_descriptions = {}
        
        # Extract Args section from docstring
        args_match = re.search(r'Args:\s*\n(.*?)(?:\n\s*Returns:|$)', docstring, re.DOTALL)
        if args_match:
            args_section = args_match.group(1)
            # Parse individual parameter descriptions
            param_matches = re.findall(r'(\w+):\s*([^\n]+)', args_section)
            for param_name, desc in param_matches:
                param_descriptions[param_name] = desc.strip()
        
        return param_descriptions
    
    def _clean_docstring_description(self, docstring):
        """Extract the main description from docstring, removing Args/Returns sections."""
        if not docstring:
            return "No description provided"
        
        # Split on Args: or Returns: and take the first part
        main_desc = re.split(r'\n\s*(?:Args|Returns):\s*', docstring)[0]
        return main_desc.strip()
    
    def get_tool_by_name(self, tool_name):
        """Get a specific tool method by name for execution."""
        if hasattr(self, tool_name):
            method = getattr(self, tool_name)
            if callable(method) and hasattr(method, '_is_mythic_tool'):
                return method
        return None
    
    async def execute_tool(self, tool_name, **kwargs):
        """Execute a tool by name with given parameters."""
        tool_method = self.get_tool_by_name(tool_name)
        if tool_method:
            try:
                if asyncio.iscoroutinefunction(tool_method):
                    return await tool_method(**kwargs)
                else:
                    # If it's a regular synchronous function, call it directly
                    return tool_method(**kwargs)
            except Exception as e:
                return f"Error executing {tool_name}: {str(e)}"
        else:
            return f"Tool {tool_name} not found"


class MythicAPIManager:
    """Manages Mythic API clients per TaskID"""
    
    def __init__(self):
        self._clients: Dict[str, MythicAPIClient] = {}
        self._lock = threading.Lock()
        self.logger = logging.getLogger(__name__)
    
    @contextmanager
    def get_client(self, task_id: str):
        """Context manager that provides a Mythic API client for the given TaskID"""
        with self._lock:
            if task_id not in self._clients:
                try:
                    api_key = self._get_api_token(task_id)
                    self._clients[task_id] = MythicAPIClient()
                    self.logger.info(f"Created new Mythic API client for task {task_id}")
                except Exception as e:
                    self.logger.error(f"Failed to create Mythic API client for task {task_id}: {e}")
                    raise
            
            client = self._clients[task_id]
        
        try:
            yield client
        except Exception as e:
            self.logger.error(f"Error using Mythic API client for task {task_id}: {e}")
            raise
        finally:
            # Optionally implement cleanup logic here
            # For now, we keep the client alive for the task's lifetime
            pass

    async def _get_api_token(self, taskID: str) -> str:
        resp = await SendMythicRPCAPITokenCreate(MythicRPCAPITokenCreateMessage(AgentTaskID=taskID))
        if resp.Success:
            return resp.APIToken
            #return mythic.login(apitoken=resp.APIToken, server_ip="127.0.0.1", server_port=7443, ssl=True) # mythic_classes.Mythic
        else:
            raise Exception(f"Failed to get API token: {resp.Error}")
    
    def cleanup_task(self, task_id: str):
        """Manually cleanup a task's API client"""
        with self._lock:
            if task_id in self._clients:
                # Perform any cleanup on the client if needed
                del self._clients[task_id]
                self.logger.info(f"Cleaned up Mythic API client for task {task_id}")

# Global manager instance
mythic_manager = MythicAPIManager()

