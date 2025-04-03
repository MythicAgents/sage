package commands

import (
	// Standard
	"fmt"
	"strings"

	// Internal
	b "github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/bedrock"
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/env"
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/openai"

	// Mythic
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"
	"github.com/MythicMeta/MythicContainer/mythicrpc"
)

func query() structs.Command {
	attr := structs.CommandAttribute{
		SupportedOS: []string{"sage"},
	}

	provider := structs.CommandParameter{
		Name:                                    "provider",
		ModalDisplayName:                        "Provider",
		CLIName:                                 "provider",
		ParameterType:                           structs.COMMAND_PARAMETER_TYPE_CHOOSE_ONE,
		Description:                             "The model provider to interact with",
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
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       0,
				AdditionalInformation: nil,
			},
		},
	}

	model := structs.CommandParameter{
		Name:                                    "model",
		ModalDisplayName:                        "model",
		CLIName:                                 "model",
		ParameterType:                           structs.COMMAND_PARAMETER_TYPE_STRING,
		DefaultValue:                            "",
		Description:                             "The model to use for inference from the selected provider",
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
				UIModalPosition:       1,
				AdditionalInformation: nil,
			},
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       1,
				AdditionalInformation: nil,
			},
		},
	}

	prompt := structs.CommandParameter{
		Name:                                    "prompt",
		ModalDisplayName:                        "prompt",
		CLIName:                                 "prompt",
		ParameterType:                           structs.COMMAND_PARAMETER_TYPE_STRING,
		DefaultValue:                            "",
		Description:                             "The prompt to send to the model for inference",
		SupportedAgents:                         nil,
		SupportedAgentBuildParameters:           nil,
		ChoicesAreAllCommands:                   false,
		ChoicesAreLoadedCommands:                false,
		FilterCommandChoicesByCommandAttributes: nil,
		DynamicQueryFunction:                    nil,
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   true,
				GroupName:             "Default",
				UIModalPosition:       2,
				AdditionalInformation: nil,
			},
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       2,
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
				UIModalPosition:       3,
				AdditionalInformation: nil,
			},
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       3,
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
				UIModalPosition:       4,
				AdditionalInformation: nil,
			},
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       4,
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
				UIModalPosition:       5,
				AdditionalInformation: nil,
			},
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       5,
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
				UIModalPosition:       6,
				AdditionalInformation: nil,
			},
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       6,
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
				UIModalPosition:       7,
				AdditionalInformation: nil,
			},
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       7,
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
				UIModalPosition:       8,
				AdditionalInformation: nil,
			},
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       8,
				AdditionalInformation: nil,
			},
		},
	}

	filename := structs.CommandParameter{
		Name:                                    "filename",
		ModalDisplayName:                        "Filename",
		CLIName:                                 "filename",
		ParameterType:                           structs.COMMAND_PARAMETER_TYPE_CHOOSE_ONE,
		Description:                             "The filename to send to the model for inference",
		Choices:                                 []string{""},
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
				UIModalPosition:       9,
				AdditionalInformation: nil,
			},
		},
	}

	file := structs.CommandParameter{
		Name:                                    "file",
		ModalDisplayName:                        "File",
		CLIName:                                 "file",
		ParameterType:                           structs.COMMAND_PARAMETER_TYPE_FILE,
		DefaultValue:                            "",
		Description:                             "The file to send to the model for inference",
		Choices:                                 nil,
		SupportedAgents:                         nil,
		SupportedAgentBuildParameters:           nil,
		ChoicesAreAllCommands:                   false,
		ChoicesAreLoadedCommands:                false,
		FilterCommandChoicesByCommandAttributes: nil,
		DynamicQueryFunction:                    nil,
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   false,
				GroupName:             "New File",
				UIModalPosition:       9,
				AdditionalInformation: nil,
			},
		},
	}

	command := structs.Command{
		Name:                           "query",
		NeedsAdminPermissions:          false,
		HelpString:                     "query -prompt \"<prompt>\"",
		Description:                    "Send a single query to a model and get a single response",
		Version:                        0,
		SupportedUIFeatures:            nil,
		Author:                         "@Ne0nd0g",
		MitreAttackMappings:            []string{},
		ScriptOnlyCommand:              false,
		CommandAttributes:              attr,
		CommandParameters:              []structs.CommandParameter{provider, model, prompt, apiEndpoint, apiKey, awsAccessKey, awsSecretAccessKey, awsSessionToken, awsRegion, file, filename},
		AssociatedBrowserScript:        nil,
		TaskFunctionOPSECPre:           nil,
		TaskFunctionCreateTasking:      queryCreateTask,
		TaskFunctionProcessResponse:    nil,
		TaskFunctionOPSECPost:          nil,
		TaskFunctionParseArgString:     taskFunctionParseArgString,
		TaskFunctionParseArgDictionary: taskFunctionParseArgDictionary,
		TaskCompletionFunctions:        nil,
	}

	return command
}

func queryCreateTask(task *structs.PTTaskMessageAllData) (resp structs.PTTaskCreateTaskingMessageResponse) {
	pkg := "mythic/container/commands/query/queryCreateTask()"
	resp.TaskID = task.Task.ID

	//data, filename, err := GetFile(task)
	//if err != nil {
	//	logging.LogError(err, "there was an error getting the file")
	//}
	//logging.LogDebug(fmt.Sprintf("Filename: %s", filename))

	provider, err := env.Get(task, "provider")
	if err != nil {
		err = fmt.Errorf("there was an error getting the 'provider' argument: %s", err)
		resp.Error = err.Error()
		resp.Success = false
		logging.LogError(err, "returning with error")
		return
	}

	model, err := env.Get(task, "model")
	if err != nil {
		err = fmt.Errorf("%s: there was an error getting the 'model' argument: %s", pkg, err)
		resp.Error = err.Error()
		resp.Success = false
		logging.LogError(err, "returning with error")
		return
	}

	prompt, err := task.Args.GetStringArg("prompt")
	if err != nil {
		err = fmt.Errorf("%s: there was an error getting the 'prompt' argument: %s", pkg, err)
		resp.Error = err.Error()
		resp.Success = false
		logging.LogError(err, "returning with error")
		return
	}

	var output string

	switch strings.ToLower(provider) {
	case "bedrock":
		output, err = b.Chat(task, prompt)
		if err != nil {
			resp.Error = fmt.Sprintf("Failed to invoke model: %s", err.Error())
			resp.Success = false
			logging.LogError(err, pkg)
			return
		}
	case "openai":
		output, err = openai.Chat(task, prompt)
		if err != nil {
			resp.Error = fmt.Sprintf("Failed to invoke model: %s", err.Error())
			resp.Success = false
			logging.LogError(err, pkg)
			return
		}
	default:
		resp.Error = fmt.Sprintf("Unknown provider: %s", provider)
		resp.Success = false
		logging.LogError(fmt.Errorf("unknown provider: %s", provider), pkg)
		return
	}

	output = prompt + "\n\n" + output

	msg := mythicrpc.MythicRPCResponseCreateMessage{
		TaskID:   task.Task.ID,
		Response: []byte(output),
	}

	r, err := mythicrpc.SendMythicRPCResponseCreate(msg)
	if err != nil {
		resp.Error = fmt.Sprintf("Failed to send response: %s", err.Error())
		resp.Success = false
		logging.LogError(err, pkg)
		return
	}

	disp := fmt.Sprintf("with %s:%s", provider, model)
	resp.DisplayParams = &disp

	resp.Success = true
	resp.Completed = &r.Success

	return
}
