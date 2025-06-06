from mythic_container.MythicCommandBase import TaskArguments, CommandBase, SupportedUIFeature, PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse

class ExitArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)
        self.args = []

    async def parse_arguments(self):
        pass

class ExitCommand(CommandBase):
    cmd = "exit"
    needs_admin = False
    help_cmd = "exit"
    description = "Does nothing, but make Mythic happy"
    version = 1
    author = "@Ne0nd0g"
    supported_ui_features = [SupportedUIFeature.SUPPORTED_UI_FEATURE_CALLBACK_TABLE_EXIT]
    argument_class = ExitArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )
        return response