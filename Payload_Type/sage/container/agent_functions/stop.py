from mythic_container.MythicCommandBase import (
    CommandBase,
    CommandParameter,
    ParameterGroupInfo,
    ParameterType,
    PTTaskCreateTaskingMessageResponse,
    PTTaskMessageAllData,
    TaskArguments,
)
from mythic_container.MythicRPC import (
    MythicRPCCallbackUpdateMessage,
    MythicRPCResponseCreateMessage,
    MythicRPCTaskUpdateMessage,
    SendMythicRPCCallbackUpdate,
    SendMythicRPCResponseCreate,
    SendMythicRPCTaskUpdate,
)
from mythic_container.logging import logger

from ai.langgraph.model import list_sessions, remove_session, request_stop_for_sessions


class StopArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)
        task_id = CommandParameter(
            name="task_id",
            display_name="Task ID",
            cli_name="task_id",
            type=ParameterType.String,
            default_value="",
            description="Optional internal or display task id to stop. Blank stops every active Sage LLM run.",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=0)],
        )
        self.args = [task_id]

    async def parse_arguments(self):
        if self.command_line and self.command_line.strip():
            self.add_arg("task_id", self.command_line.strip())

    async def parse_dictionary(self, dictionary_arguments):
        for arg in dictionary_arguments:
            self.add_arg(arg, dictionary_arguments[arg])


class StopCommand(CommandBase):
    cmd = "stop"
    needs_admin = False
    help_cmd = "stop [task_id]"
    description = "Stop active Sage LLM processing tasks and mark them stopped."
    version = 1
    author = "@Ne0nd0g"
    argument_class = StopArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(TaskID=taskData.Task.ID, Success=True)
        target = (taskData.args.get_arg("task_id") or "").strip()

        before = await list_sessions()
        stopped = await request_stop_for_sessions(target or None)
        lines = []
        if target:
            lines.append(f"Requested stop for Sage LLM task matching '{target}'.")
        else:
            lines.append("Requested stop for all active Sage LLM tasks.")

        if not stopped:
            active = ", ".join(sorted(before.keys())) if before else "none"
            lines.append(f"No matching active LLM task was found. Active sessions: {active}.")
        else:
            lines.append(f"Stopped {len(stopped)} active LLM task(s): {', '.join(sorted(stopped.keys()))}.")

        for session_id, model in stopped.items():
            task_id = _task_id(model)
            if task_id is None:
                continue
            stop_notice = "\n\n🛑> Stop requested by operator. Sage will not issue further LLM-driven tasks for this run.\n"
            resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(task_id, stop_notice.encode()))
            if not resp.Success:
                logger.error(f"Failed to stream stop notice to task {task_id}: {resp.Error}")
            update = await SendMythicRPCTaskUpdate(MythicRPCTaskUpdateMessage(
                TaskID=task_id,
                UpdateStatus="stopped",
                UpdateCompleted=True,
            ))
            if not update.Success:
                logger.error(f"Failed to update stopped task {task_id}: {update.Error}")
            await remove_session(session_id)

        output = "\n".join(lines) + "\n"
        resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(taskData.Task.ID, output.encode()))
        if not resp.Success:
            logger.error(f"Failed to stream stop command output for task {taskData.Task.ID}: {resp.Error}")

        callback = await SendMythicRPCCallbackUpdate(MythicRPCCallbackUpdateMessage(
            TaskID=taskData.Task.ID,
            UpdateLastCheckinTime=True,
            UpdateLastCheckinTimeViaC2Profile="",
        ))
        if not callback.Success:
            logger.error(f"Failed to update callback for stop task {taskData.Task.ID}: {callback.Error}")

        response.Completed = True
        response.TaskStatus = "success"
        return response


def _task_id(model) -> int | None:
    try:
        return int(getattr(model, "task_id", ""))
    except (TypeError, ValueError):
        return None
