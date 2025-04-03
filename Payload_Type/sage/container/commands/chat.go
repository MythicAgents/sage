package commands

import (
	// Standard
	"fmt"
	"strings"
	"sync"

	// Internal
	b "github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/bedrock"
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/env"
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/openai"

	// Mythic
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/agent_structs/InteractiveTask"
	"github.com/MythicMeta/MythicContainer/logging"
	"github.com/MythicMeta/MythicContainer/mythicrpc"
)

func chat() structs.Command {
	attr := structs.CommandAttribute{
		SupportedOS: []string{"sage"},
	}

	provider := structs.CommandParameter{
		Name:                                    "provider",
		ModalDisplayName:                        "Provider",
		CLIName:                                 "provider",
		ParameterType:                           structs.COMMAND_PARAMETER_TYPE_CHOOSE_ONE,
		Description:                             "The model provider to chat with",
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

	model := structs.CommandParameter{
		Name:                                    "model",
		ModalDisplayName:                        "model",
		CLIName:                                 "model",
		ParameterType:                           structs.COMMAND_PARAMETER_TYPE_STRING,
		Description:                             "The model to use for inference from the selected provider",
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
		Description:                             "The prompt to send to the model for inference",
		DefaultValue:                            "",
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
		},
	}

	command := structs.Command{
		Name:                           "chat",
		NeedsAdminPermissions:          false,
		HelpString:                     "chat",
		Description:                    "Interactive chat with selected provider and model",
		Version:                        0,
		SupportedUIFeatures:            []string{"task:process_interactive_tasks", "task_response:interactive"},
		Author:                         "@Ne0nd0g",
		MitreAttackMappings:            []string{},
		ScriptOnlyCommand:              false,
		CommandAttributes:              attr,
		CommandParameters:              []structs.CommandParameter{provider, model, prompt, apiEndpoint, apiKey, awsAccessKey, awsSecretAccessKey, awsSessionToken, awsRegion},
		AssociatedBrowserScript:        nil,
		TaskFunctionOPSECPre:           nil,
		TaskFunctionCreateTasking:      chatCreateTask,
		TaskFunctionProcessResponse:    nil,
		TaskFunctionOPSECPost:          nil,
		TaskFunctionParseArgString:     taskFunctionParseArgString,
		TaskFunctionParseArgDictionary: taskFunctionParseArgDictionary,
		TaskCompletionFunctions:        nil,
	}

	return command
}

func chatCreateTask(task *structs.PTTaskMessageAllData) (resp structs.PTTaskCreateTaskingMessageResponse) {
	pkg := "mythic/container/commands/query/chatCreateTask()"
	resp.TaskID = task.Task.ID

	var err error
	var chat Chat
	var provider string
	var model string
	var prompt string
	var output string

	// Handle interactive tasks (everything after the first task)
	if task.Task.IsInteractiveTask {
		logging.LogDebug("got interactive message", "type", task.Task.InteractiveTaskType, "msg", task.Args.GetCommandLine())

		// Get the parent Task
		searchResp, err := mythicrpc.SendMythicRPCTaskSearch(
			mythicrpc.MythicRPCTaskSearchMessage{
				TaskID:       task.Task.ID,
				SearchTaskID: &task.Task.ParentTaskID,
			},
		)
		if err != nil {
			err = fmt.Errorf("there was an error getting the parent task: %s", err)
			resp.Error = err.Error()
			resp.Success = false
			logging.LogError(err, "returning with error")
			return
		}
		if len(searchResp.Tasks) == 0 {
			err = fmt.Errorf("there was an error getting the parent task: %s", err)
			resp.Error = err.Error()
			resp.Success = false
			logging.LogError(err, "returning with error")
			return
		}
		parentTask := searchResp.Tasks[0]
		logging.LogDebug("got parent task", "task_id", parentTask.ID, "task_name", parentTask.CommandName)

		resp.TaskID = parentTask.ID

		// Unmarshal parentTask.Params in to Chat struct
		/*
			fmt.Printf("parentTask.OriginalParams: %s\n", parentTask.OriginalParams)
			err = json.Unmarshal([]byte(parentTask.OriginalParams), &chatParams)
			if err != nil {
				err = fmt.Errorf("there was an error unmarshalling the parent task params: %s", err)
				resp.Error = err.Error()
				resp.Success = false
				logging.LogError(err, "returning with error")
				return
			}
		*/

		// Get the session from the repository
		chatParams, ok := sessions.Get(parentTask.ID)
		if !ok {
			err = fmt.Errorf("there was an error getting the chat session: %s", err)
			resp.Error = err.Error()
			resp.Success = false
			logging.LogError(err, "returning with error")
			return
		}

		// Update the task args with chatParams
		task.Args.SetArgValue("provider", chatParams.Provider)
		task.Args.SetArgValue("model", chatParams.Model)
		task.Args.SetArgValue("prompt", chatParams.Prompt)
		task.Args.SetArgValue("API_ENDPOINT", chatParams.Endpoint)
		task.Args.SetArgValue("API_KEY", chatParams.Key)
		task.Args.SetArgValue("AWS_ACCESS_KEY_ID", chatParams.AWSAccessKeyID)
		task.Args.SetArgValue("AWS_SECRET_ACCESS_KEY", chatParams.AWSSecretAccessKey)
		task.Args.SetArgValue("AWS_SESSION_TOKEN", chatParams.AWSSessionToken)
		task.Args.SetArgValue("AWS_DEFAULT_REGION", chatParams.AWSDefaultRegion)

		provider = chatParams.Provider

		switch InteractiveTask.MessageType(task.Task.InteractiveTaskType) {
		case InteractiveTask.Input:
			// Handle input messages
			prompt = fmt.Sprintf("%sUser> %s", chatParams.Prompt, task.Args.GetCommandLine())
			sessions.UpdatePrompt(resp.TaskID, fmt.Sprintf("\nUser> %s\n", task.Args.GetCommandLine()))
		case InteractiveTask.Exit:
			sessions.Delete(parentTask.ID)
			resp.Success = true
			t := true
			resp.Completed = &t
			return
		default:
			// Handle other message types
		}
	} else {
		chat, err = NewChat(task)
		if err != nil {
			resp.Error = err.Error()
			resp.Success = false
			logging.LogError(err, "returning with error")
			return
		}
		// Store the chat session in the repository
		sessions.Add(task.Task.ID, chat)

		provider = chat.Provider
		model = chat.Model
		prompt = chat.Prompt
	}

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
			err = fmt.Errorf("there was an error with the openai chat: %s", err)
			resp.Error = err.Error()
			resp.Success = false
			logging.LogError(err, "returning with error")
			return
		}
	default:
		resp.Error = fmt.Sprintf("Unknown provider: %s", provider)
		resp.Success = false
		logging.LogError(fmt.Errorf("unknown provider: %s", provider), pkg)
		return
	}
	output = fmt.Sprintf("AI> %s\n\n", output)

	sessions.UpdatePrompt(resp.TaskID, output)

	// If this is the first message in an interactive task, add the prompt to the output
	if !task.Task.IsInteractiveTask {
		output = fmt.Sprintf("%s%s", prompt, output)
	}

	msg := mythicrpc.MythicRPCResponseCreateMessage{
		TaskID:   resp.TaskID,
		Response: []byte(output),
	}

	r, err := mythicrpc.SendMythicRPCResponseCreate(msg)
	if err != nil {
		resp.Error = fmt.Sprintf("Failed to send response: %s", err.Error())
		resp.Success = r.Success
		logging.LogError(err, pkg)
		return
	}

	resp.Success = true

	// Mark the interactive task as completed with a green check mark
	if task.Task.IsInteractiveTask {
		// Currently disabled because when the task updates it moves the prompt to show after the response
		//t := true
		//resp.Completed = &t
		//resp.TaskID = task.Task.ID
	} else {
		disp := fmt.Sprintf("with %s:%s", provider, model)
		resp.DisplayParams = &disp
	}

	return
}

type Chat struct {
	Provider           string `json:"provider"`
	Model              string `json:"model"`
	Prompt             string `json:"prompt"`
	Endpoint           string `json:"API_ENDPOINT"`
	Key                string `json:"API_KEY"`
	AWSAccessKeyID     string `json:"AWS_ACCESS_KEY_ID"`
	AWSSecretAccessKey string `json:"AWS_SECRET_ACCESS_KEY"`
	AWSSessionToken    string `json:"AWS_SESSION_TOKEN"`
	AWSDefaultRegion   string `json:"AWS_DEFAULT_REGION"`
}

func NewChat(task *structs.PTTaskMessageAllData) (chat Chat, err error) {
	chat.Provider, err = env.Get(task, "provider")
	if err != nil {
		return
	}
	chat.Model, err = env.Get(task, "model")
	if err != nil {
		return
	}
	chat.Prompt, err = task.Args.GetStringArg("prompt")
	if err != nil {
		return
	}
	chat.Prompt = fmt.Sprintf("User> %s\n", chat.Prompt)

	// If the key is empty, an error will be returned. It is OK if the key is empty for some providers
	chat.Endpoint, _ = env.Get(task, "API_ENDPOINT")
	chat.Key, _ = env.Get(task, "API_KEY")
	chat.AWSAccessKeyID, _ = env.Get(task, "AWS_ACCESS_KEY_ID")
	chat.AWSSecretAccessKey, _ = env.Get(task, "AWS_SECRET_ACCESS_KEY")
	chat.AWSSessionToken, _ = env.Get(task, "AWS_SESSION_TOKEN")
	chat.AWSDefaultRegion, _ = env.Get(task, "AWS_DEFAULT_REGION")

	return chat, nil
}

type Repository struct {
	chats map[int]Chat
	sync.Mutex
}

var sessions = &Repository{chats: make(map[int]Chat)}

func (r *Repository) Add(taskID int, chat Chat) {
	r.Lock()
	defer r.Unlock()
	r.chats[taskID] = chat
}

func (r *Repository) Get(taskID int) (Chat, bool) {
	r.Lock()
	defer r.Unlock()
	chat, ok := r.chats[taskID]
	return chat, ok
}

func (r *Repository) Delete(taskID int) {
	r.Lock()
	defer r.Unlock()
	delete(r.chats, taskID)
}

func (r *Repository) UpdatePrompt(taskID int, prompt string) {
	r.Lock()
	defer r.Unlock()
	chat, ok := r.chats[taskID]
	if ok {
		chat.Prompt += prompt
		r.chats[taskID] = chat
	}
}
