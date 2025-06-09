from mythic_container.MythicCommandBase import TaskArguments, CommandBase, CommandParameter, ParameterType, ParameterGroupInfo, PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse
from mythic_container.MythicRPC import MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate, MythicRPCCallbackUpdateMessage, SendMythicRPCCallbackUpdate 
from mythic_container.logging import logger
from ai.mcp import MCPManager, create_stdio_config, create_sse_config, create_streamable_http_config, ConnectionType
from typing import List

class MCPConnectArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)

        # Server Name
        name = CommandParameter(
            name="name",
            display_name="Server Name",
            cli_name="name",
            type=ParameterType.String,
            description="Unique name for the MCP server connection",
            parameter_group_info=[ParameterGroupInfo(required=True, ui_position=0)]
        )

        # Connection Type
        connection_type = CommandParameter(
            name="connection_type",
            display_name="Connection Type",
            cli_name="connection_type",
            type=ParameterType.ChooseOne,
            choices=["stdio", "sse", "streamable_http"],
            default_value="stdio",
            description="Type of MCP connection to establish",
            parameter_group_info=[ParameterGroupInfo(required=True, ui_position=1)]
        )

        # STDIO Parameters
        command = CommandParameter(
            name="command",
            display_name="Command",
            cli_name="command",
            type=ParameterType.String,
            description="[STDIO] Command to execute for STDIO MCP server (e.g., 'uv')",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=2)]
        )

        arguments = CommandParameter(
            name="arguments",
            display_name="Arguments",
            cli_name="arguments",
            type=ParameterType.Array,
            description="[STDIO] Array of command arguments",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=3)]
        )

        cwd = CommandParameter(
            name="cwd",
            display_name="Working Directory",
            cli_name="cwd",
            type=ParameterType.String,
            description="[STDIO] Working directory for the command",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=4)]
        )

        # SSE/HTTP Parameters
        url = CommandParameter(
            name="url",
            display_name="URL",
            cli_name="url",
            type=ParameterType.String,
            description="[SSE/HTTP] URL for SSE or HTTP streaming connection",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=6)]
        )

        headers = CommandParameter(
            name="headers",
            display_name="Headers",
            cli_name="headers",
            type=ParameterType.Array,
            description="[SSE/HTTP] List of HTTP headers with key-value pairs (e.g., Content-Type: application/json)",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=7)]
        )

        timeout = CommandParameter(
            name="timeout",
            display_name="Timeout",
            cli_name="timeout",
            type=ParameterType.Number,
            description="[SSE/HTTP] Connection timeout in seconds",
            default_value=30.0,
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=8)]
        )

        sse_read_timeout = CommandParameter(
            name="sse_read_timeout",
            display_name="SSE Read Timeout",
            cli_name="sse_read_timeout",
            type=ParameterType.Number,
            description="[SSE/HTTP] SSE read timeout in seconds",
            default_value=300.0,
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=9)]
        )

        terminate_on_close = CommandParameter(
            name="terminate_on_close",
            display_name="Terminate on Close",
            cli_name="terminate_on_close",
            type=ParameterType.Boolean,
            description="[HTTP] Whether to terminate the connection when closed",
            default_value=True,
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=10)]
        )

        # Add all the parameters
        self.args = [name, connection_type, command, arguments, cwd, url, headers, timeout, sse_read_timeout, terminate_on_close]

    async def parse_arguments(self):
        if len(self.command_line) == 0:
            raise ValueError("Need to specify server name and connection details")
        parts = self.command_line.split(" ", 1)
        self.add_arg("name", parts[0])

    async def parse_dictionary(self, dictionary_arguments):
        if "name" not in dictionary_arguments:
            raise ValueError("Missing 'name' argument")
        for arg in dictionary_arguments:
            self.add_arg(arg, dictionary_arguments[arg])

class MCPConnectCommand(CommandBase):
    cmd = "mcp-connect"
    needs_admin = False
    help_cmd = "mcp-connect -name <server_name> -connection_type <stdio|sse|streamable_http> [options]"
    description = "Connect to an MCP (Model Context Protocol) server"
    version = 1
    author = "@Ne0nd0g"
    argument_class = MCPConnectArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )

        try:
            # Get required parameters
            name = taskData.args.get_arg("name")
            if not name:
                response.Success = False
                response.Error = "Server name is required"
                return response

            connection_type_str = taskData.args.get_arg("connection_type")
            if not connection_type_str:
                response.Success = False
                response.Error = "Connection type is required"
                return response

            # Parse connection type
            try:
                connection_type = ConnectionType(connection_type_str.lower())
            except ValueError:
                response.Success = False
                response.Error = f"Invalid connection type: {connection_type_str}. Must be stdio, sse, or streamable_http"
                return response

            response.DisplayParams = f'{name} ({connection_type.value})'

            # Create configuration based on connection type
            config = None
            if connection_type == ConnectionType.STDIO:
                command = taskData.args.get_arg("command")
                if not command:
                    response.Success = False
                    response.Error = "Command is required for STDIO connections"
                    return response

                # Get arguments as list (Array)
                args: List[str] = []
                args_list = taskData.args.get_arg("arguments")
                if isinstance(args_list, list):
                    args = args_list
                else:
                    response.Success = False
                    response.Error = f"Arguments must be a list/array, got {type(args_list).__name__}"
                    return response

                cwd = taskData.args.get_arg("cwd")

                config = create_stdio_config(
                    name=name,
                    command=command,
                    args=args,
                    env={},
                    cwd=cwd,
                    encoding=None,
                    encoding_error_handler=None,
                    session_kwargs=None
                )

            elif connection_type == ConnectionType.SSE:
                url = taskData.args.get_arg("url")
                if not url:
                    response.Success = False
                    response.Error = "URL is required for SSE connections"
                    return response

                # Get headers as dict (Dictionary)
                headers_dict = {}
                headers_array = taskData.args.get_arg("headers")
                if headers_array and not isinstance(headers_array, list):
                    response.Success = False
                    response.Error = f"Headers must be a list/array, got {type(headers_array).__name__}"
                    return response
                if headers_array:
                    for header in headers_array:
                        key, value = header.split(":", 1)
                        headers_dict[key.strip()] = value.strip()
                
                timeout: float
                timeout_arg = taskData.args.get_arg("timeout") or 30.0
                if not isinstance(timeout_arg, (int, float)):
                    response.Success = False
                    response.Error = f"Timeout must be a number, got {type(timeout_arg).__name__}"
                    return response
                timeout = float(timeout_arg)

                sse_read_timeout: float
                sse_read_timeout_arg = taskData.args.get_arg("sse_read_timeout") or 300.0
                if not isinstance(sse_read_timeout_arg, (int, float)):
                    response.Success = False
                    response.Error = f"SSE Read Timeout must be a number, got {type(sse_read_timeout_arg).__name__}"
                    return response
                sse_read_timeout = float(sse_read_timeout_arg)

                config = create_sse_config(
                    name=name,
                    url=url,
                    headers=headers_dict,
                    timeout=timeout,
                    sse_read_timeout=sse_read_timeout,
                    session_kwargs=None
                )

            elif connection_type == ConnectionType.STREAMABLE_HTTP:
                url = taskData.args.get_arg("url")
                if not url:
                    response.Success = False
                    response.Error = "URL is required for HTTP streaming connections"
                    return response

                headers_dict = {}
                headers_array = taskData.args.get_arg("headers")
                if headers_array and not isinstance(headers_array, list):
                    response.Success = False
                    response.Error = f"Headers must be a list/array, got {type(headers_array).__name__}"
                    return response
                if headers_array:
                    for header in headers_array:
                        key, value = header.split(":", 1)
                        headers_dict[key.strip()] = value.strip()

                timeout: float
                timeout_arg = taskData.args.get_arg("timeout") or 30.0
                if not isinstance(timeout_arg, (int, float)):
                    response.Success = False
                    response.Error = f"Timeout must be a number, got {type(timeout_arg).__name__}"
                    return response
                timeout = float(timeout_arg)

                sse_read_timeout: float
                sse_read_timeout_arg = taskData.args.get_arg("sse_read_timeout") or 300.0
                if not isinstance(sse_read_timeout_arg, (int, float)):
                    response.Success = False
                    response.Error = f"SSE Read Timeout must be a number, got {type(sse_read_timeout_arg).__name__}"
                    return response
                sse_read_timeout = float(sse_read_timeout_arg)

                terminate_on_close = True
                terminate_on_close_arg = taskData.args.get_arg("terminate_on_close")
                if terminate_on_close_arg is not None:
                    if not isinstance(terminate_on_close_arg, bool):
                        response.Success = False
                        response.Error = f"Terminate on close must be a boolean, got {type(terminate_on_close_arg).__name__}"
                        return response
                    terminate_on_close = terminate_on_close_arg

                config = create_streamable_http_config(
                    name=name,
                    url=url,
                    headers=headers_dict,
                    timeout=timeout,
                    sse_read_timeout=sse_read_timeout,
                    terminate_on_close=terminate_on_close,
                    session_kwargs=None
                )

            if not config:
                response.Success = False
                response.Error = f"Failed to create configuration for connection type: {connection_type.value}"
                return response

            # Attempt to connect

            success = await MCPManager.connect_server(config)

            if success:
                # Get server info and tools
                server_info = MCPManager.get_server_info(name)
                tools = MCPManager.get_tools_by_server(name)

                result_message = f"✅ Successfully connected to MCP server '{name}'\n"
                result_message += f"Connection Type: {connection_type.value}\n"
                result_message += f"Tools Available: {len(tools)}\n"
                
                if tools:
                    result_message += "\nAvailable Tools:\n"
                    for tool in tools:
                        result_message += f"  • {tool.name}: {tool.description}\n"
                
                logger.info(f"Successfully connected to MCP server '{name}' with {len(tools)} tools")
                
            else:
                response.Success = False
                response.Error = f"Failed to connect to MCP server '{name}'. Check server configuration and availability."
                return response

        except Exception as e:
            logger.error(f"Error connecting to MCP server: {e}")
            response.Success = False
            response.Error = f"Error connecting to MCP server: {str(e)}"
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

        return response