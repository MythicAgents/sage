from mythic_container.MythicCommandBase import TaskArguments, CommandBase, CommandParameter, ParameterType, ParameterGroupInfo, SupportedUIFeature, PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse
from mythic_container.MythicRPC import MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate, MythicRPCCallbackUpdateMessage, SendMythicRPCCallbackUpdate, SendMythicRPCTaskUpdate, MythicRPCTaskUpdateMessage 
from mythic_container.logging import logger
from .utils import get_secret
from ai.model import Model, add_session, get_session, remove_session

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
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=3)]
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
        # Tools
        tools = CommandParameter(
            name="tools",
            display_name="Tools",
            cli_name="tools",
            type=ParameterType.Boolean,
            default_value=True,
            description="Use tools to enhance the model's capabilities",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=1)]
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
        # API Endpoint
        api_endpoint = CommandParameter(
            name="api_endpoint",
            display_name="API Endpoint",
            cli_name="api-endpoint",
            type=ParameterType.String,
            description="[OPTIONAL] The API endpoint to use for the selected provider",
            parameter_group_info=[ParameterGroupInfo(required=False,ui_position=5)]
        )
        # API Key
        api_key = CommandParameter(
            name="api_key",
            display_name="API Key",
            cli_name="api-key",
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

        # Add all the parameters
        self.args = [provider, model, prompt, tools, verbose, api_endpoint, api_key, aws_access_key, aws_secret_access_key, aws_session_token, aws_region]

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
            # If the task is exit, return
            elif taskData.Task.InteractiveTaskType == 3:  # Exit task
                resp = await SendMythicRPCTaskUpdate(
                    MythicRPCTaskUpdateMessage(
                        TaskID=taskData.Task.ParentTaskID,
                        UpdateStatus="completed",
                        UpdateTaskIsComplete=True
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
            
            tools = taskData.args.get_arg("tools")
            verbose = taskData.args.get_arg("verbose")

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
            llm = Model(provider=provider.lower(), model=model.lower(), system_prompt=system_prompt, config=config)
            if verbose:
                llm.set_verbose(True)
            if tools:
                await llm.with_tools(str(taskData.Task.AgentTaskID))
            await add_session(str(taskData.Task.ID), llm)

        llm_resp = await llm.invoke(prompt)
        llm_resp += "\n👤> "  # Add the user prompt to the response for context

        id = response.TaskID if not taskData.Task.IsInteractiveTask else taskData.Task.ParentTaskID
        resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(id, llm_resp.encode()))
        if not resp.Success:
            response.Success = False
            response.Error = resp.Error
            return response

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

        return response
