import asyncio
import os
import re
import inspect
import json
import threading
import logging
from contextlib import contextmanager
from typing import Dict
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
        logger.debug(f"Initializing MythicAPIClient with task ID: {task_id}")
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
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            results =  await mythic.get_all_commands_for_payloadtype(self.client, payload, attr)
            return json.dumps(results)
        except Exception as e:
            return f"Error getting commands for payload type {payload}: {e}"
    
    @mythic_tool
    async def get_all_active_callbacks(self) -> str:
        """Executes a graphql query to get information about all currently active callbacks. The default set of attributes returned in the dictionary can be found at graphql_queries.callback_fragment. If you want to use your own `custom_return_attributes` string to identify what information you want back, you have to include the `id` field, everything else is optional.

        Args:
            custom_return_attributes: Optional string of attributes to return. If not provided, the default set of attributes will be used.
        Returns:
            str: JSON string of the callbacks and their attributes.
        """
        if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
        resp = await mythic.get_all_active_callbacks(self.client)
        return json.dumps(resp)

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

