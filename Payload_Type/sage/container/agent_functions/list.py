from mythic_container.MythicCommandBase import TaskArguments, CommandBase, CommandParameter, ParameterType, ParameterGroupInfo, SupportedUIFeature, PTTaskMessageAllData, PTTaskCreateTaskingMessageResponse, PTTaskMessageTaskData
from mythic_container.MythicRPC import MythicRPCResponseCreateMessage, SendMythicRPCResponseCreate, MythicRPCCallbackUpdateMessage, SendMythicRPCCallbackUpdate, SendMythicRPCTaskUpdate, MythicRPCTaskUpdateMessage 
from mythic_container.logging import logger
from .utils import get_secret
from ai.model import Model, add_session, get_session, remove_session
import requests
from urllib.parse import urljoin

class ListArguments(TaskArguments):
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
        self.args = [provider, api_endpoint, api_key, aws_access_key, aws_secret_access_key, aws_session_token, aws_region]

    async def parse_arguments(self):
        pass

    async def parse_dictionary(self, dictionary_arguments):
        pass

class ListCommand(CommandBase):
    cmd = "list"
    needs_admin = False
    help_cmd = "list"
    description = "List all available models for the selected provider."
    version = 1
    author = "@Ne0nd0g"
    argument_class = ListArguments

    async def create_go_tasking(self, taskData: PTTaskMessageAllData) -> PTTaskCreateTaskingMessageResponse:
        response = PTTaskCreateTaskingMessageResponse(
            TaskID=taskData.Task.ID,
            Success=True,
        )

        provider = get_secret(taskData=taskData, key="provider")
        if provider is None:
            response.Success = False
            response.Error = "unable to find the 'provider' key in the task, user secrets, payload build parameters, or in the payload container's environment variables"
            return response
        
                # These can be None as they are optional
        api_endpoint = get_secret(taskData=taskData, key="API_ENDPOINT")
        api_key = get_secret(taskData=taskData, key="API_KEY")
        aws_access_key_id = get_secret(taskData=taskData, key="AWS_ACCESS_KEY_ID")
        aws_secret_access_key = get_secret(taskData=taskData, key="AWS_SECRET_ACCESS_KEY")
        aws_session_token = get_secret(taskData=taskData, key="AWS_SESSION_TOKEN")
        aws_region = get_secret(taskData=taskData, key="AWS_DEFAULT_REGION")

        if (provider.lower() == "openai" or provider.lower() == "anthropic") and not api_key:
            response.Error = "An API key was not found in the task, user secrets, payload build parameters, or in the payload container's environment variables. Please set the API key."
            response.Success = False
            return response

        response.DisplayParams = f'available models for {provider}'
 
        models_list : str = ""
        if provider.lower() == "openai":
            if not api_endpoint:
                # response.Error = "An API endpoint was not found in the task, user secrets, payload build parameters, or in the payload container's environment variables. Please set the API endpoint."
                api_endpoint = "https://api.openai.com"

            resp = requests.get(
                urljoin(api_endpoint, "v1/models"),
                headers={"Authorization": f"Bearer {api_key}"}
            )
            if resp.status_code != 200:
                response.Error = f"Failed to list models: {resp.text}"
                response.Success = False
                return response
            data = resp.json()
            for model in data["data"]:
                models_list += f"{model['id']}\n"
        elif provider.lower() == "anthropic":
            if not api_endpoint:
                api_endpoint = "https://api.anthropic.com"
            url = urljoin(api_endpoint, "v1/models")
            headers = {"x-api-key": f"{api_key}", "anthropic-version": "2023-06-01"}
            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                response.Error = f"Failed to list models: {resp.text}"
                response.Success = False
                return response
            data = resp.json()
            for model in data["data"]:
                models_list += f"{model['id']}\n"
        elif provider.lower() == "bedrock":
            models_list = "Listing models for Bedrock is not supported yet. Please use the AWS CLI or SDK to list models."
        else:
            response.Error = f"Listing models for provider '{provider}' is not supported yet."
            response.Success = False
            return response
        
        resp = await SendMythicRPCResponseCreate(MythicRPCResponseCreateMessage(taskData.Task.ID, models_list.encode()))
        if not resp.Success:
            response.Success = False
            response.Error = resp.Error
        
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