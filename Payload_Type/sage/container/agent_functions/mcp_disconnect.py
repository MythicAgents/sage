from mythic_container.MythicCommandBase import TaskArguments, CommandBase, CommandParameter, ParameterType, ParameterGroupInfo, PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse
from mythic_container.MythicRPC import MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate, MythicRPCCallbackUpdateMessage, SendMythicRPCCallbackUpdate
from mythic_container.logging import logger
from ai.mcp import MCPManager


class MCPDisconnectArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)

        name = CommandParameter(
            name="name",
            display_name="Server Name",
            cli_name="name",
            type=ParameterType.String,
            description="Name of the MCP server to disconnect",
            parameter_group_info=[ParameterGroupInfo(required=True, ui_position=0)]
        )

        self.args = [name]

    async def parse_arguments(self):
        if len(self.command_line) == 0:
            raise ValueError("Server name is required")
        self.add_arg("name", self.command_line.split()[0])

    async def parse_dictionary(self, dictionary_arguments):
        if "name" not in dictionary_arguments:
            raise ValueError("Missing 'name' argument")
        for arg in dictionary_arguments:
            self.add_arg(arg, dictionary_arguments[arg])


class MCPDisconnectCommand(CommandBase):
    cmd = "mcp-disconnect"
    needs_admin = False
    help_cmd = "mcp-disconnect -name <server_name>"
    description = "Disconnect from an MCP (Model Context Protocol) server"
    version = 1
    author = "@Ne0nd0g"
    argument_class = MCPDisconnectArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )

        try:
            name = taskData.args.get_arg("name")
            if not name:
                response.Success = False
                response.Error = "Server name is required"
                return response

            response.DisplayParams = name

            # Check if server is connected
            connected_servers = MCPManager.get_connected_servers()
            if name not in connected_servers:
                response.Success = False
                response.Error = f"MCP server '{name}' is not connected. Connected servers: {', '.join(connected_servers) if connected_servers else 'none'}"
                return response

            # Attempt to disconnect
            success = await MCPManager.disconnect_server(name)

            if success:
                result_message = f"Successfully disconnected from MCP server '{name}'"
                logger.info(result_message)
            else:
                response.Success = False
                response.Error = f"Failed to disconnect from MCP server '{name}'"
                return response

        except Exception as e:
            logger.error(f"Error disconnecting from MCP server: {e}")
            response.Success = False
            response.Error = f"Error disconnecting from MCP server: {str(e)}"
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
