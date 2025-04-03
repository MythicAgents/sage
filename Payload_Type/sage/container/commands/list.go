package commands

import (
	// Standard
	"fmt"
	"strings"

	// Internal
	b "github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/bedrock"
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/env"
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/ollama"
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/openai"
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/openwebui"

	// Mythic

	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"
	"github.com/MythicMeta/MythicContainer/mythicrpc"
)

func list() structs.Command {
	attr := structs.CommandAttribute{
		SupportedOS: []string{"sage"},
	}

	provider := structs.CommandParameter{
		Name:                                    "provider",
		ModalDisplayName:                        "Provider",
		CLIName:                                 "provider",
		ParameterType:                           structs.COMMAND_PARAMETER_TYPE_CHOOSE_ONE,
		Description:                             "The model provider you want to list models from",
		Choices:                                 append([]string{""}, env.ProvidersString()...),
		DefaultValue:                            "",
		SupportedAgents:                         nil,
		SupportedAgentBuildParameters:           nil,
		ChoicesAreAllCommands:                   false,
		ChoicesAreLoadedCommands:                false,
		FilterCommandChoicesByCommandAttributes: nil,
		DynamicQueryFunction:                    nil,
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   false,
				GroupName:             "Default",
				UIModalPosition:       0,
				AdditionalInformation: nil,
			},
		},
	}

	apiEndpoint := structs.CommandParameter{
		Name:             "API_ENDPOINT",
		ModalDisplayName: "API Endpoint",
		CLIName:          "API-ENDPOINT",
		ParameterType:    structs.COMMAND_PARAMETER_TYPE_STRING,
		DefaultValue:     "",
		Description:      "[OPTIONAL] The API endpoint to use for the selected provider",
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   false,
				GroupName:             "Default",
				UIModalPosition:       1,
				AdditionalInformation: nil,
			},
		},
	}

	apiKey := structs.CommandParameter{
		Name:             "API_KEY",
		ModalDisplayName: "API Key",
		CLIName:          "API-KEY",
		ParameterType:    structs.COMMAND_PARAMETER_TYPE_STRING,
		DefaultValue:     "",
		Description:      "[OPTIONAL] The API key to use for the selected provider",
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   false,
				GroupName:             "Default",
				UIModalPosition:       2,
				AdditionalInformation: nil,
			},
		},
	}

	awsAccessKey := structs.CommandParameter{
		Name:             "AWS_ACCESS_KEY_ID",
		ModalDisplayName: "AWS Access Key ID",
		CLIName:          "AWS-ACCESS-KEY-ID",
		ParameterType:    structs.COMMAND_PARAMETER_TYPE_STRING,
		DefaultValue:     "",
		Description:      "[OPTIONAL] The AWS Access Key ID (AWS_ACCESS_KEY_ID) to use for Bedrock",
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   false,
				GroupName:             "Default",
				UIModalPosition:       3,
				AdditionalInformation: nil,
			},
		},
	}

	awsSecretAccessKey := structs.CommandParameter{
		Name:             "AWS_SECRET_ACCESS_KEY",
		ModalDisplayName: "AWS Secret Access Key",
		CLIName:          "AWS-SECRET-ACCESS-KEY",
		ParameterType:    structs.COMMAND_PARAMETER_TYPE_STRING,
		DefaultValue:     "",
		Description:      "[OPTIONAL] The AWS Secret Access Key (AWS_SECRET_ACCESS_KEY) to use for Bedrock",
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   false,
				GroupName:             "Default",
				UIModalPosition:       4,
				AdditionalInformation: nil,
			},
		},
	}

	awsSessionToken := structs.CommandParameter{
		Name:             "AWS_SESSION_TOKEN",
		ModalDisplayName: "AWS Session Token",
		CLIName:          "AWS-SESSION-TOKEN",
		ParameterType:    structs.COMMAND_PARAMETER_TYPE_STRING,
		DefaultValue:     "",
		Description:      "[OPTIONAL] The AWS Session Token (AWS_SESSION_TOKEN) to use for Bedrock",
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   false,
				GroupName:             "Default",
				UIModalPosition:       5,
				AdditionalInformation: nil,
			},
		},
	}

	awsRegion := structs.CommandParameter{
		Name:             "AWS_DEFAULT_REGION",
		ModalDisplayName: "AWS Default Region",
		CLIName:          "AWS-DEFAULT-REGION",
		ParameterType:    structs.COMMAND_PARAMETER_TYPE_STRING,
		DefaultValue:     "",
		Description:      "[OPTIONAL] The AWS Region (AWS_DEFAULT_REGION) to use for Bedrock",
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   false,
				GroupName:             "Default",
				UIModalPosition:       6,
				AdditionalInformation: nil,
			},
		},
	}

	command := structs.Command{
		Name:                           "list",
		NeedsAdminPermissions:          false,
		HelpString:                     "list",
		Description:                    "List all available from the selected provider",
		Version:                        0,
		SupportedUIFeatures:            nil,
		Author:                         "@Ne0nd0g",
		MitreAttackMappings:            []string{},
		ScriptOnlyCommand:              false,
		CommandAttributes:              attr,
		CommandParameters:              []structs.CommandParameter{provider, apiEndpoint, apiKey, awsAccessKey, awsSecretAccessKey, awsSessionToken, awsRegion},
		AssociatedBrowserScript:        nil,
		TaskFunctionOPSECPre:           nil,
		TaskFunctionParseArgString:     taskFunctionParseArgString,
		TaskFunctionParseArgDictionary: taskFunctionParseArgDictionary,
		TaskFunctionCreateTasking:      listCreateTask,
		TaskFunctionProcessResponse:    nil,
		TaskFunctionOPSECPost:          nil,
		TaskCompletionFunctions:        nil,
	}

	return command
}

func listCreateTask(task *structs.PTTaskMessageAllData) (resp structs.PTTaskCreateTaskingMessageResponse) {
	resp.TaskID = task.Task.ID

	provider, err := env.Get(task, "provider")
	if err != nil {
		err = fmt.Errorf("there was an error getting the 'provider' argument: %s", err)
		resp.Error = err.Error()
		resp.Success = false
		logging.LogError(err, "returning with error")
		return
	}

	var stdout string

	switch strings.ToLower(provider) {
	case "bedrock":
		bedrockModels, err := b.ListFoundationalModels(task)
		if err != nil {
			resp.Error = fmt.Sprintf("Failed to list Bedrock models: %s", err.Error())
			resp.Success = false
			logging.LogError(err, "there was an error listing bedrock models")
			return
		}

		for _, m := range bedrockModels {
			stdout += fmt.Sprintf("%s\n", m)
		}
	case "openai":
		stdout, err = openai.List(task)
		if err != nil {
			resp.Error = fmt.Sprintf("Failed to list OpenAI models: %s", err.Error())
			resp.Success = false
			logging.LogError(err, "there was an error listing OpenAI models")
			return
		}
	case "ollama":
		stdout, err = ollama.List(task)
		if err != nil {
			resp.Error = fmt.Sprintf("Failed to list ollama models: %s", err.Error())
			resp.Success = false
			logging.LogError(err, "there was an error listing ollama models")
			return
		}
	case "openwebui":
		stdout, err = openwebui.List(task)
		if err != nil {
			resp.Error = fmt.Sprintf("Failed to list OpenWebUI models: %s", err.Error())
			resp.Success = false
			logging.LogError(err, "there was an error listing openwebui models")
			return
		}
	}

	msg := mythicrpc.MythicRPCResponseCreateMessage{
		TaskID:   task.Task.ID,
		Response: []byte(stdout),
	}

	r, err := mythicrpc.SendMythicRPCResponseCreate(msg)
	if err != nil {
		resp.Error = fmt.Sprintf("Failed to send response: %s", err.Error())
		resp.Success = false
		logging.LogError(err, "there was an error sending the Mythic RPC Response Create message")
		return
	}

	resp.DisplayParams = &provider
	resp.Success = true
	resp.Completed = &r.Success
	return
}
