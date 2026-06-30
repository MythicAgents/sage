from mythic_container.MythicCommandBase import TaskArguments, CommandBase, CommandParameter, ParameterType, ParameterGroupInfo, SupportedUIFeature, PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse
from mythic_container.MythicRPC import MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate, MythicRPCCallbackUpdateMessage, SendMythicRPCCallbackUpdate, SendMythicRPCTaskUpdate, MythicRPCTaskUpdateMessage, SendMythicRPCTaskUpdate, MythicRPCTaskUpdateMessage 
from mythic_container.logging import logger
from .utils import get_secret, ensure_bloodhound_task_preflight
from ai.langgraph.model import Model, add_session, get_session, remove_session

class ChatArguments(TaskArguments):
    def __init__(self, command_line, **kwargs):
        super().__init__(command_line, **kwargs)

        # Provider
        provider = CommandParameter(
            name="provider",
            display_name="Provider",
            cli_name="provider",
            type=ParameterType.String,
            description="The model provider to interact with (e.g. Anthropic, Bedrock, OpenAI)",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=1)]
        )

        # Model
        model = CommandParameter(
            name="model",
            display_name="Model",
            cli_name="model",
            type=ParameterType.String,
            description="The model to use for inference from the selected provider",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=4)]
        )
        # Prompt
        prompt = CommandParameter(
            name="prompt",
            display_name="Prompt",
            cli_name="prompt",
            type=ParameterType.String,
            description="The prompt to send to the model for inference",
            parameter_group_info=[ParameterGroupInfo(required=True, ui_position=0)]
        )
        # Verbose
        verbose = CommandParameter(
            name="verbose",
            display_name="Verbose",
            cli_name="verbose",
            type=ParameterType.Boolean,
            default_value=False,
            description="Show verbose output of all User & AI messages",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=2)]
        )
        # Autonomous Solve
        autonomous_solve = CommandParameter(
            name="autonomous_solve",
            display_name="Autonomous Solve",
            cli_name="autonomous_solve",
            type=ParameterType.Boolean,
            default_value=False,
            description="Opt-in autonomous mode: drive an objective through multi-hop solving without per-step operator confirmation. Off (default) = scoped, confirm-first behavior. Leave off for evals/normal ops.",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=3)]
        )
        # Max Steps
        max_steps = CommandParameter(
            name="max_steps",
            display_name="Max Steps",
            cli_name="max_steps",
            type=ParameterType.Number,
            default_value=200,
            description="Global cap on model steps for this run; halts a runaway loop. 0 = unlimited.",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=4)]
        )
        # API Endpoint
        api_endpoint = CommandParameter(
            name="API_ENDPOINT",
            display_name="API Endpoint",
            cli_name="API_ENDPOINT",
            type=ParameterType.String,
            description="[OPTIONAL] The API endpoint to use for the selected provider",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=5)]
        )
        # API Key
        api_key = CommandParameter(
            name="API_KEY",
            display_name="API Key",
            cli_name="API_KEY",
            type=ParameterType.String,
            description="[OPTIONAL] The API key to use for the selected provider",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=6)]
        )
        # AWS Access Key ID
        aws_access_key = CommandParameter(
            name="AWS_ACCESS_KEY_ID",
            display_name="AWS_ACCESS_KEY_ID",
            cli_name="AWS_ACCESS_KEY_ID",
            type=ParameterType.String,
            description="[OPTIONAL] The AWS Access Key ID (AWS_ACCESS_KEY_ID) to use for Bedrock",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=7)]
        )
        # AWS Secret Access Key
        aws_secret_access_key = CommandParameter(
            name="AWS_SECRET_ACCESS_KEY",
            display_name="AWS_SECRET_ACCESS_KEY",
            cli_name="AWS_SECRET_ACCESS_KEY",
            type=ParameterType.String,
            description="[OPTIONAL] The AWS Secret Access Key (AWS_SECRET_ACCESS_KEY) to use for Bedrock",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=8)]
        )
        # AWS Session Token
        aws_session_token = CommandParameter(
            name="AWS_SESSION_TOKEN",
            display_name="AWS_SESSION_TOKEN",
            cli_name="AWS_SESSION_TOKEN",
            type=ParameterType.String,
            description="[OPTIONAL] The AWS Session Token (AWS_SESSION_TOKEN) to use for Bedrock",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=9)]
        )
        # AWS Region
        aws_region = CommandParameter(
            name="AWS_DEFAULT_REGION",
            display_name="AWS_DEFAULT_REGION",
            cli_name="AWS_DEFAULT_REGION",
            type=ParameterType.String,
            description="[OPTIONAL] The AWS Region (AWS_DEFAULT_REGION) to use for Bedrock",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=10)]
        )
        # Mode (HITL)
        mode = CommandParameter(
            name="mode",
            display_name="Mode",
            cli_name="mode",
            type=ParameterType.ChooseOne,
            choices=["auto", "supervised"],
            default_value="supervised",
            description="auto = run unattended (evals/automation); supervised (default) = require operator approve/deny on guarded tool calls — safe for live ops",
            parameter_group_info=[ParameterGroupInfo(required=False, ui_position=11)]
        )

        # Add all the parameters
        self.args = [provider, model, prompt, verbose, autonomous_solve, max_steps, api_endpoint, api_key, aws_access_key, aws_secret_access_key, aws_session_token, aws_region, mode]

    async def parse_arguments(self):
        if len(self.command_line) == 0:
            raise ValueError("Need to specify a prompt")
        self.add_arg("prompt", self.command_line)

    async def parse_dictionary(self, dictionary_arguments):
        if "prompt" not in dictionary_arguments:
            raise ValueError("Missing 'prompt' argument")
        for arg in dictionary_arguments:
            self.add_arg(arg, dictionary_arguments[arg])

class ChatCommand(CommandBase):
    cmd = "chat"
    needs_admin = False
    help_cmd = "chat -prompt <prompt>"
    description = "Multi-turn chat with a model"
    version = 1
    author = "@Ne0nd0g"
    argument_class = ChatArguments
    supported_ui_features = [SupportedUIFeature.SUPPORTED_UI_FEATURE_TASK_PROCESS_INTERACTIVE_TASKS, SupportedUIFeature.SUPPORTED_UI_FEATURE_TASK_RESPONSE_INTERACTIVE] 

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )

        prompt = None
        llm = None
        aws_access_key_id: str | None = None
        aws_secret_access_key: str | None = None
        aws_session_token: str | None = None
        aws_region: str | None = None

        # Handle interactive tasks (everything after the initial/first task)
        if taskData.Task.IsInteractiveTask:
            if taskData.Task.InteractiveTaskType == 0: # Input`` task
                # Get the task's parent task ID
                '''
                resp = await SendMythicRPCTaskSearch(MythicRPCTaskSearchMessage(TaskID=taskData.Task.ID, SearchTaskID=taskData.Task.ParentTaskID))
                if not resp.Success:
                    response.Success = False
                    response.Error = resp.Error
                    return response
                if len(resp.Tasks) != 1:
                    response.Success = False
                    response.Error = f"expected 1 task with SendMythicRPCTaskSearch, got {len(resp.Tasks)}"
                    return response
                '''
                #response.TaskID = taskData.Task.ParentTaskID
                llm = await get_session(str(taskData.Task.ParentTaskID))
                if llm is None:
                    response.Success = False
                    response.Error = f"unable to find LLM session for task {taskData.Task.ParentTaskID}"
                    return response
                prompt = taskData.args.get_command_line()
            # If the task is exit, stop the running session and return
            elif taskData.Task.InteractiveTaskType == 3:  # Exit task
                # Signal the running graph to stop BEFORE tearing down the session. Marking the
                # parent task completed and removing the session dict entry does NOT cancel the
                # already-running invoke()/astream coroutine — it holds its own ref to the Model
                # and keeps issuing tasks. request_stop() sets a cooperative flag the graph loops
                # check between steps, so `exit` actually terminates a running/runaway session.
                llm = await get_session(str(taskData.Task.ParentTaskID))
                if llm is not None:
                    llm.request_stop()
                resp = await SendMythicRPCTaskUpdate(
                    MythicRPCTaskUpdateMessage(
                        TaskID=taskData.Task.ParentTaskID,
                        UpdateStatus="completed",
                        UpdateCompleted=True
                    )
                )
                if not resp.Success:
                    logger.error(f"Failed to update task {taskData.Task.ParentTaskID} status to completed: {resp.Error}")
                response.Success = True
                response.Completed = True
                response.TaskStatus = "completed"
                await remove_session(str(taskData.Task.ParentTaskID))
                return response
            else:
                response.Success = False
                response.Error = f"unexpected InteractiveTaskType {taskData.Task.InteractiveTaskType} for task {taskData.Task.ID}"
                return response
            
        # Handle initial task (the first task)
        else:
            provider = get_secret(taskData=taskData, key="provider")
            if provider is None:
                response.Success = False
                response.Error = "unable to find the 'provider' key in the task, user secrets, payload build parameters, or in the payload container's environment variables"
                return response
            
            model = get_secret(taskData=taskData, key="model")
            if model is None:
                response.Success = False
                response.Error = "unable to find the 'model' key in the task, user secrets, payload build parameters, or in the payload container's environment variables"
                return response
            
            # Verify the prompt argument was provided and is not None
            prompt = taskData.args.get_arg(key="prompt")
            if prompt is None or not prompt:
                response.Success = False
                response.Error = "the 'prompt' argument must be provided and must not be blank"
                return response
            
            verbose = taskData.args.get_arg("verbose")
            mode = taskData.args.get_arg("mode") or "supervised"
            autonomous_solve = taskData.args.get_arg("autonomous_solve") or False
            max_steps = taskData.args.get_arg("max_steps")
            max_steps = int(max_steps) if max_steps not in (None, "") else 200

            # These can be None as they are optional
            api_endpoint = get_secret(taskData=taskData, key="API_ENDPOINT")
            api_key = get_secret(taskData=taskData, key="API_KEY")
            if provider.lower() == "bedrock":
                aws_access_key_id = get_secret(taskData=taskData, key="AWS_ACCESS_KEY_ID")
                aws_secret_access_key = get_secret(taskData=taskData, key="AWS_SECRET_ACCESS_KEY")
                aws_session_token = get_secret(taskData=taskData, key="AWS_SESSION_TOKEN")
                aws_region = get_secret(taskData=taskData, key="AWS_DEFAULT_REGION")
            sys = get_secret(taskData=taskData, key="system_prompt")
            system_prompt = sys if sys or sys is not None else ""

            response.DisplayParams = f'{provider} {model} {prompt}'
            config = {"configurable":{}}
            if api_key is not None:
                config["configurable"]["api_key"] = api_key
            if api_endpoint is not None:
                config["configurable"]["base_url"] = api_endpoint
            if provider.lower() == "bedrock":
                if aws_access_key_id is not None:
                    config["configurable"]["aws_access_key_id"] = aws_access_key_id
                if aws_secret_access_key is not None:
                    config["configurable"]["aws_secret_access_key"] = aws_secret_access_key
                if aws_session_token is not None:
                    config["configurable"]["aws_session_token"] = aws_session_token
                if aws_region is not None:
                    config["configurable"]["region"] = aws_region
            # Initial chat sessions build the same graph-backed runtime as query. Connect BloodHound before
            # Model.initialize() so deterministic capability enrichment can resolve graph facts and SIDs.
            await ensure_bloodhound_task_preflight(taskData.Task.ID)
            llm = Model(provider=provider.lower(), model=model.lower(), system_prompt=system_prompt, config=config, task_id=taskData.Task.ID, agent_task_id=taskData.Task.AgentTaskID, mode=mode, autonomous_solve=autonomous_solve, max_steps=max_steps)
            llm.command_name = "chat"
            llm.task_display_id = (
                getattr(taskData.Task, "DisplayID", None)
                or getattr(taskData.Task, "DisplayId", None)
                or getattr(taskData.Task, "display_id", None)
            )
            await llm.initialize()
            if verbose:
                llm.set_verbose(True)
            await add_session(str(taskData.Task.ID), llm)

        if taskData.Task.IsInteractiveTask:
            await SendMythicRPCTaskUpdate(MythicRPCTaskUpdateMessage(TaskID=taskData.Task.ParentTaskID, UpdateStatus="LLM Processing..."))
        else:
            await SendMythicRPCTaskUpdate(MythicRPCTaskUpdateMessage(TaskID=taskData.Task.ID, UpdateStatus="LLM Processing..."))

        # Invoke LLM (streaming happens internally within invoke())
        try:
            await llm.invoke(prompt, is_interactive=taskData.Task.IsInteractiveTask)
        except Exception as e:
            # Error occurred - update task status and stream error to UI
            response.Success = False
            response.Error = f"{type(e).__name__}: {str(e)}"
            logger.error(f"LLM invocation failed: {response.Error}")

            # Update task status to show error (not stuck on "LLM Processing...")
            id = response.TaskID if not taskData.Task.IsInteractiveTask else taskData.Task.ParentTaskID
            await SendMythicRPCTaskUpdate(MythicRPCTaskUpdateMessage(TaskID=id, UpdateStatus="error"))

            # Stream error message to task output so user can see it
            error_msg = f"\n❌> Error: {response.Error}\n"
            resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(id, error_msg.encode()))
            if not resp.Success:
                logger.error(f"Failed to stream error message to task: {resp.Error}")

            # Add prompt indicator so user knows they can retry
            resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(id, "\n👤> ".encode()))
            if not resp.Success:
                logger.error(f"Failed to add prompt indicator after error: {resp.Error}")

            return response

        # Add user prompt indicator for next turn
        id = response.TaskID if not taskData.Task.IsInteractiveTask else taskData.Task.ParentTaskID
        if getattr(llm, "_stop_requested", False):
            await SendMythicRPCTaskUpdate(MythicRPCTaskUpdateMessage(
                TaskID=id,
                UpdateStatus="stopped",
                UpdateCompleted=True,
            ))
            response.Completed = True
            response.TaskStatus = "stopped"
            await remove_session(str(id))
            return response

        resp = await SendMythicRPCResponseCreate(
            MythicRPCResponseCreateMessage(id, "\n👤> ".encode())
        )
        if not resp.Success:
            logger.error(f"Failed to add prompt indicator: {resp.Error}")

        # Not checking for success or errors because this is just a callback to update the last checkin time
        resp = await SendMythicRPCCallbackUpdate(
            MythicRPCCallbackUpdateMessage(
                TaskID=id,
                UpdateLastCheckinTime=True,
                UpdateLastCheckinTimeViaC2Profile=""
            )
        )
        if not resp.Success:
            logger.error(f"Failed to update callback for task {id}: {resp.Error}")

        if taskData.Task.IsInteractiveTask:
            response.Completed = True
            await SendMythicRPCTaskUpdate(MythicRPCTaskUpdateMessage(TaskID=taskData.Task.ParentTaskID, UpdateStatus="success"))
        return response
