package build

import (
	// Standard
	"fmt"

	// Internal
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/env"

	// Mythic
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"
	rpc "github.com/MythicMeta/MythicContainer/mythicrpc"
)

func Build(msg structs.PayloadBuildMessage) (response structs.PayloadBuildResponse) {
	response = structs.PayloadBuildResponse{
		Success: true,
	}

	callback, err := rpc.SendMythicRPCCallbackCreate(
		rpc.MythicRPCCallbackCreateMessage{
			PayloadUUID:    msg.PayloadUUID,
			User:           "Sage",
			Host:           "Sage",
			Ip:             "127.0.0.1",
			IntegrityLevel: 3,
		},
	)

	if err != nil {
		response.Success = false
		logging.LogError(err, "Failed to create callback: "+err.Error())

	} else {
		logging.LogInfo("Created callback: " + callback.CallbackUUID)
	}

	// Send build step response
	resp, err := rpc.SendMythicRPCPayloadUpdateBuildStep(
		rpc.MythicRPCPayloadUpdateBuildStepMessage{
			PayloadUUID: msg.PayloadUUID,
			StepName:    "Create Callback",
			StepSuccess: true,
		},
	)

	if err != nil {
		err = fmt.Errorf("there was an error sending the MythicRPCPayloadUpdateBuildStepMessage RPC message: %s, %s", err, resp.Error)
		logging.LogError(err, "returning with error")
		// Do not return, keep going
	}

	response.BuildStdOut = fmt.Sprintf("Created callback: %s", callback.CallbackUUID)
	response.Success = true
	var data []byte
	response.Payload = &data
	return response
}

func NewPayload() (structs.PayloadType, error) {
	payload := structs.PayloadType{
		Name:                                   "sage",
		FileExtension:                          "",
		Author:                                 "Russel Van Tuyl @Ne0nd0g",
		SupportedOS:                            []string{env.SUPPORTED_OS_SAGE},
		AgentType:                              "service",
		Wrapper:                                false,
		CanBeWrappedByTheFollowingPayloadTypes: []string{},
		SupportsDynamicLoading:                 false,
		Description:                            "A local Mythic agent to interact with AI systems",
		SupportedC2Profiles:                    []string{},
		MythicEncryptsData:                     true,
		MessageFormat:                          structs.MessageFormatJSON,
	}

	// Create the build parameters

	// Provider
	provider := structs.BuildParameter{
		Name:          "provider",
		Description:   "[OPTIONAL] The model provider to use",
		Required:      false,
		ParameterType: structs.BUILD_PARAMETER_TYPE_CHOOSE_ONE,
		Choices:       []string{},
	}
	for _, p := range env.Providers() {
		provider.Choices = append(provider.Choices, p.String())
	}

	// Model
	model := structs.BuildParameter{
		Name:          "model",
		Description:   "[OPTIONAL] The model to use for inference from the selected provider",
		Required:      false,
		ParameterType: structs.BUILD_PARAMETER_TYPE_STRING,
		Choices:       []string{},
	}

	// API Endpoint
	apiEndpoint := structs.BuildParameter{
		Name:          "API_ENDPOINT",
		Description:   "[OPTIONAL] The API endpoint to use for the selected provider",
		Required:      false,
		ParameterType: structs.BUILD_PARAMETER_TYPE_STRING,
	}

	// API Key
	apiKey := structs.BuildParameter{
		Name:          "API_KEY",
		Description:   "[OPTIONAL] The API key to use for the selected provider",
		Required:      false,
		ParameterType: structs.BUILD_PARAMETER_TYPE_STRING,
		DefaultValue:  "",
	}

	// AWS ACCESS KEY
	awsAccessKey := structs.BuildParameter{
		Name:          "AWS_ACCESS_KEY_ID",
		Description:   "[OPTIONAL] The AWS Access Key ID (AWS_ACCESS_KEY_ID) to use for Bedrock",
		Required:      false,
		ParameterType: structs.BUILD_PARAMETER_TYPE_STRING,
		DefaultValue:  "",
	}

	// AWS SECRET ACCESS KEY
	awsSecretAccessKey := structs.BuildParameter{
		Name:          "AWS_SECRET_ACCESS_KEY",
		Description:   "[OPTIONAL] The AWS Secret Access Key (AWS_SECRET_ACCESS_KEY) to use for Bedrock",
		Required:      false,
		ParameterType: structs.BUILD_PARAMETER_TYPE_STRING,
		DefaultValue:  "",
	}

	// AWS SESSION TOKEN
	awsSessionToken := structs.BuildParameter{
		Name:          "AWS_SESSION_TOKEN",
		Description:   "[OPTIONAL] The AWS Session Token (AWS_SESSION_TOKEN) to use for Bedrock",
		Required:      false,
		ParameterType: structs.BUILD_PARAMETER_TYPE_STRING,
		DefaultValue:  "",
	}
	// AWS REGION
	awsRegion := structs.BuildParameter{
		Name:          "AWS_DEFAULT_REGION",
		Description:   "[OPTIONAL] The AWS Region (AWS_DEFAULT_REGION) to use for Bedrock",
		Required:      false,
		ParameterType: structs.BUILD_PARAMETER_TYPE_STRING,
		DefaultValue:  "us-east-1",
	}

	// Add the build parameters to the payload
	payload.BuildParameters = []structs.BuildParameter{
		provider,
		model,
		apiEndpoint,
		apiKey,
		awsAccessKey,
		awsSecretAccessKey,
		awsSessionToken,
		awsRegion,
	}

	// Add build step
	create := structs.BuildStep{
		Name:        "Create Callback",
		Description: "Create Agent Callback",
	}
	payload.BuildSteps = []structs.BuildStep{create}

	return payload, nil
}
