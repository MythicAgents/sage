import json
from mythic_container.MythicCommandBase import TaskArguments, CommandBase, CommandParameter, ParameterType, ParameterGroupInfo, PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse
from mythic_container.MythicRPC import MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate, MythicRPCCallbackUpdateMessage, SendMythicRPCCallbackUpdate, SendMythicRPCTaskUpdate, MythicRPCTaskUpdateMessage
from mythic_container.logging import logger
from ai.mcp import MCPManager


class MCPCallArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)

        tool_name = CommandParameter(
            name="tool_name",
            display_name="Tool Name",
            cli_name="tool",
            type=ParameterType.String,
            description="Name of the MCP tool to invoke",
            parameter_group_info=[ParameterGroupInfo(required=True, ui_position=1)]
        )

        server_name = CommandParameter(
            name="server_name",
            display_name="Server Name",
            cli_name="server",
            type=ParameterType.String,
            description="Name of MCP server to use (required if multiple servers have tools with the same name)",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=0)]
        )

        tool_args = CommandParameter(
            name="tool_args",
            display_name="Tool Arguments",
            cli_name="args",
            type=ParameterType.Array,
            description="List of tool arguments as alternating key-value pairs (e.g., 'agent_id', '34', 'include_archived', 'false')",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=2)]
        )

        self.args = [tool_name, server_name, tool_args]

    async def parse_arguments(self):
        if len(self.command_line) == 0:
            raise ValueError("Tool name is required")
        parts = self.command_line.split(None, 1)
        self.add_arg("tool_name", parts[0])

    async def parse_dictionary(self, dictionary_arguments):
        if "tool_name" not in dictionary_arguments:
            raise ValueError("Missing 'tool_name' argument")
        for arg in dictionary_arguments:
            self.add_arg(arg, dictionary_arguments[arg])


class MCPCallCommand(CommandBase):
    cmd = "mcp-call"
    needs_admin = False
    help_cmd = "mcp-call -tool <tool_name> [-server <server_name>] -args key1 -args value1 -args key2 -args value2"
    description = "Directly invoke an MCP (Model Context Protocol) tool"
    version = 1
    author = "@Ne0nd0g"
    argument_class = MCPCallArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )

        try:
            tool_name = taskData.args.get_arg("tool_name")
            if not tool_name:
                response.Success = False
                response.Error = "Tool name is required"
                return response

            server_name = taskData.args.get_arg("server_name")

            # Parse tool arguments from array of alternating key-value pairs
            # e.g., ["agent_id", "34", "include_archived", "false"]
            tool_args = {}
            tool_args_array = taskData.args.get_arg("tool_args")
            if tool_args_array:
                if not isinstance(tool_args_array, list):
                    response.Success = False
                    response.Error = f"Tool arguments must be a list/array, got {type(tool_args_array).__name__}"
                    return response

                if len(tool_args_array) % 2 != 0:
                    response.Success = False
                    response.Error = f"Tool arguments must be in key-value pairs (even number of entries). Got {len(tool_args_array)} entries. Format: [param_name, value, param_name2, value2, ...]. Use 'mcp-list -verbose' to see parameter names for each tool."
                    return response

                # Process pairs: index 0,2,4... are keys, index 1,3,5... are values
                for i in range(0, len(tool_args_array), 2):
                    key = str(tool_args_array[i]).strip()
                    value = str(tool_args_array[i + 1]).strip()

                    # Try to parse value as JSON for complex types (numbers, booleans, objects, arrays)
                    try:
                        parsed_value = json.loads(value)
                        tool_args[key] = parsed_value
                    except json.JSONDecodeError:
                        # Keep as string if not valid JSON
                        tool_args[key] = value

            display_server = f" (server: {server_name})" if server_name else ""
            response.DisplayParams = f"{tool_name}{display_server} {json.dumps(tool_args)}"

            # Check if any MCP servers are connected
            connected_servers = MCPManager.get_connected_servers()
            if not connected_servers:
                response.Success = False
                response.Error = "No MCP servers connected. Use 'mcp-connect' first."
                return response

            # Check for tool name conflicts
            if MCPManager.has_tool_conflict(tool_name):
                if not server_name:
                    servers_with_tool = MCPManager.get_servers_with_tool(tool_name)
                    response.Success = False
                    response.Error = f"Tool '{tool_name}' exists on multiple servers: {', '.join(servers_with_tool)}. Please specify -server to disambiguate."
                    return response

            # Get the tool (optionally from a specific server)
            tool = await MCPManager.get_tool_by_name(tool_name, server_name)
            if not tool:
                if server_name:
                    # Tool not found on specified server
                    server_tools = MCPManager.get_tools_by_server(server_name)
                    tool_names = [t.name for t in server_tools]
                    response.Success = False
                    response.Error = f"Tool '{tool_name}' not found on server '{server_name}'. Available tools on this server: {', '.join(tool_names) if tool_names else 'none'}"
                    return response
                else:
                    # List available tools to help the user
                    all_tools = MCPManager.get_all_tools()
                    tool_names = [t.name for t in all_tools]
                    response.Success = False
                    response.Error = f"Tool '{tool_name}' not found. Available tools: {', '.join(tool_names) if tool_names else 'none'}"
                    return response

            logger.info(f"Invoking MCP tool '{tool_name}' with args: {tool_args}")

            # Update task status to show processing
            await SendMythicRPCTaskUpdate(MythicRPCTaskUpdateMessage(TaskID=taskData.Task.ID, UpdateStatus="processing"))

            # Invoke the tool
            result = await tool.ainvoke(tool_args)

            # Format the result
            if isinstance(result, (dict, list)):
                result_str = json.dumps(result, indent=2)
            else:
                result_str = str(result)

            result_message = f"Tool: {tool_name}\n"
            result_message += f"Arguments: {json.dumps(tool_args)}\n"
            result_message += "=" * 50 + "\n"
            result_message += f"Result:\n{result_str}"

            logger.info(f"MCP tool '{tool_name}' completed successfully")

        except Exception as e:
            logger.error(f"Error invoking MCP tool: {e}")
            response.Success = False
            response.Error = f"Error invoking MCP tool: {str(e)}"
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
