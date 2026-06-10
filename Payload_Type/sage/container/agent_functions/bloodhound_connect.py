from mythic_container.MythicCommandBase import (
    TaskArguments, CommandBase, CommandParameter, ParameterType, ParameterGroupInfo,
    PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse,
)
from mythic_container.MythicRPC import MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate
from mythic_container.logging import logger

from ai.bloodhound_config import ensure_bloodhound_connected, BLOODHOUND_SETUP_STEPS


class BloodHoundConnectArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)
        directory = CommandParameter(
            name="directory",
            display_name="BloodHound MCP Directory",
            cli_name="directory",
            type=ParameterType.String,
            description="Path to the BloodHound MCP server directory. Defaults to the SAGE_BLOODHOUND_MCP_DIR env var.",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=0)],
        )
        self.args = [directory]

    async def parse_arguments(self):
        # Bare command line (if any) is treated as the directory path.
        if self.command_line:
            self.add_arg("directory", self.command_line.strip())

    async def parse_dictionary(self, dictionary_arguments):
        for arg in dictionary_arguments:
            self.add_arg(arg, dictionary_arguments[arg])


class BloodHoundConnectCommand(CommandBase):
    cmd = "bloodhound-connect"
    needs_admin = False
    help_cmd = "bloodhound-connect [-directory <path>]"
    description = (
        "Connect Sage's dedicated BloodHound MCP server (uses SAGE_BLOODHOUND_MCP_DIR, or -directory). "
        "BloodHound is first-class in Sage and has its own agent — this is distinct from `mcp-connect`, "
        "which connects arbitrary third-party MCP servers."
    )
    version = 1
    author = "@Ne0nd0g"
    argument_class = BloodHoundConnectArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(TaskID=taskData.Task.ID, Success=True)
        directory = taskData.args.get_arg("directory") or None
        response.DisplayParams = directory or "(configured default)"
        try:
            connected, msg = await ensure_bloodhound_connected(directory)
        except Exception as e:
            logger.error(f"bloodhound-connect error: {e}")
            response.Success = False
            response.Error = f"bloodhound-connect error: {e}"
            return response

        out = msg if connected else (msg + "\n\n" + BLOODHOUND_SETUP_STEPS)
        resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(taskData.Task.ID, out.encode()))
        if not resp.Success:
            response.Success = False
            response.Error = resp.Error
            return response

        response.Completed = True
        return response
