from mythic_container.MythicCommandBase import TaskArguments, CommandBase, PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse
from mythic_container.MythicRPC import MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate, MythicRPCCallbackUpdateMessage, SendMythicRPCCallbackUpdate
from mythic_container.logging import logger
from ai.mcp import MCPManager


class MCPListArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)
        self.args = []

    async def parse_arguments(self):
        pass

    async def parse_dictionary(self, dictionary_arguments):
        pass


class MCPListCommand(CommandBase):
    cmd = "mcp-list"
    needs_admin = False
    help_cmd = "mcp-list"
    description = "List all connected MCP (Model Context Protocol) servers and their tools"
    version = 1
    author = "@Ne0nd0g"
    argument_class = MCPListArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )

        try:
            # Get connected servers
            connected_servers = MCPManager.get_connected_servers()

            if not connected_servers:
                result_message = "No MCP servers currently connected.\n\nUse 'mcp-connect' to connect to an MCP server."
            else:
                summary = MCPManager.get_tools_summary()
                result_message = f"Connected MCP Servers: {len(connected_servers)}\n"
                result_message += f"Total Tools Available: {summary.get('total_tools', 0)}\n"
                result_message += "=" * 50 + "\n\n"

                for server_name in connected_servers:
                    server_info = MCPManager.get_server_info(server_name)
                    server_summary = summary.get("server_summaries", {}).get(server_name, {})

                    result_message += f"Server: {server_name}\n"
                    if server_info:
                        result_message += f"  Connection Type: {server_info.get('connection_type', 'unknown')}\n"
                    result_message += f"  Tools: {server_summary.get('tool_count', 0)}\n"

                    # Get full tool details with descriptions and parameters
                    tools = MCPManager.get_tools_by_server(server_name)
                    if tools:
                        result_message += "  Available Tools:\n"
                        for tool in tools:
                            result_message += f"    - {tool.name}\n"
                            if tool.description:
                                # Truncate long descriptions
                                desc = tool.description[:200] + "..." if len(tool.description) > 200 else tool.description
                                result_message += f"      Description: {desc}\n"

                            # Try multiple ways to get parameter schema
                            properties = {}
                            required = []

                            # Method 1: Use tool.args property (returns dict directly)
                            if hasattr(tool, 'args') and tool.args:
                                properties = tool.args
                                # args doesn't include required info, try to get from args_schema
                                if hasattr(tool, 'args_schema') and tool.args_schema:
                                    try:
                                        if hasattr(tool.args_schema, 'model_json_schema'):
                                            schema = tool.args_schema.model_json_schema()
                                        elif hasattr(tool.args_schema, 'schema'):
                                            schema = tool.args_schema.schema()
                                        else:
                                            schema = {}
                                        required = schema.get('required', [])
                                    except Exception:
                                        pass

                            # Method 2: Try args_schema directly if args didn't work
                            if not properties and hasattr(tool, 'args_schema') and tool.args_schema:
                                try:
                                    if hasattr(tool.args_schema, 'model_json_schema'):
                                        schema = tool.args_schema.model_json_schema()
                                    elif hasattr(tool.args_schema, 'schema'):
                                        schema = tool.args_schema.schema()
                                    else:
                                        schema = {}
                                    properties = schema.get('properties', {})
                                    required = schema.get('required', [])
                                except Exception:
                                    pass

                            if properties:
                                result_message += f"      Parameters:\n"
                                for param_name, param_info in properties.items():
                                    if isinstance(param_info, dict):
                                        param_type = param_info.get('type', 'any')
                                        param_desc = param_info.get('description', '')
                                    else:
                                        param_type = str(param_info)
                                        param_desc = ''
                                    req_marker = "*" if param_name in required else ""
                                    result_message += f"        - {param_name}{req_marker} ({param_type})"
                                    if param_desc:
                                        result_message += f": {param_desc[:100]}"
                                    result_message += "\n"

                    result_message += "\n"

            logger.info(f"Listed {len(connected_servers)} MCP server(s)")

        except Exception as e:
            logger.error(f"Error listing MCP servers: {e}")
            response.Success = False
            response.Error = f"Error listing MCP servers: {str(e)}"
            return response

        # Send response back to Mythic
        resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(taskData.Task.ID, result_message.encode()))
        if not resp.Success:
            response.Success = False
            response.Error = resp.Error
            return response

        # Update callback timestamp
        resp = await SendMythicRPCCallbackUpdate(
            MythicRPCCallbackUpdateMessage(
                TaskID=taskData.Task.ID,
                UpdateLastCheckinTime=True,
                UpdateLastCheckinTimeViaC2Profile=""
            )
        )
        if not resp.Success:
            logger.error(f"Failed to update callback for task {taskData.Task.ID}: {resp.Error}")

        response.Completed = True
        return response
