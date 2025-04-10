package commands

import (
	// Standard
	"fmt"

	// Internal
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/mcp"

	// Mythic
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"
	"github.com/MythicMeta/MythicContainer/mythicrpc"
)

func mcpConnect() structs.Command {
	attr := structs.CommandAttribute{
		SupportedOS: []string{"sage"},
	}

	mcpCommand := structs.CommandParameter{
		Name:                                    "command",
		ModalDisplayName:                        "command",
		CLIName:                                 "command",
		ParameterType:                           structs.COMMAND_PARAMETER_TYPE_STRING,
		Description:                             "The command or program to start the MCP Server",
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
				UIModalPosition:       0,
				AdditionalInformation: nil,
			},
		},
	}

	mcpArgs := structs.CommandParameter{
		Name:             "args",
		ModalDisplayName: "Arguments",
		CLIName:          "args",
		ParameterType:    structs.COMMAND_PARAMETER_TYPE_ARRAY,
		DefaultValue:     []string{},
		Description:      "Arguments to pass to the command",
		ParameterGroupInformation: []structs.ParameterGroupInfo{
			{
				ParameterIsRequired:   false,
				GroupName:             "Default",
				UIModalPosition:       1,
				AdditionalInformation: nil,
			},
		},
	}

	command := structs.Command{
		Name:                           "mcp-connect",
		NeedsAdminPermissions:          false,
		HelpString:                     "mcp-connect -command <command> -args <args>",
		Description:                    "Start and connect to a local Stdio MCP server",
		Version:                        0,
		SupportedUIFeatures:            nil,
		Author:                         "@Ne0nd0g",
		MitreAttackMappings:            []string{},
		ScriptOnlyCommand:              false,
		CommandAttributes:              attr,
		CommandParameters:              []structs.CommandParameter{mcpCommand, mcpArgs},
		AssociatedBrowserScript:        nil,
		TaskFunctionOPSECPre:           nil,
		TaskFunctionParseArgString:     taskFunctionParseArgString,
		TaskFunctionParseArgDictionary: taskFunctionParseArgDictionary,
		TaskFunctionCreateTasking:      mcpConnectCreateTask,
		TaskFunctionProcessResponse:    nil,
		TaskFunctionOPSECPost:          nil,
		TaskCompletionFunctions:        nil,
	}

	return command
}

func mcpConnectCreateTask(task *structs.PTTaskMessageAllData) (resp structs.PTTaskCreateTaskingMessageResponse) {
	resp.TaskID = task.Task.ID

	command, err := task.Args.GetStringArg("command")
	if err != nil {
		err = fmt.Errorf("there was an error getting the 'command' argument: %s", err)
		resp.Error = err.Error()
		resp.Success = false
		logging.LogError(err, "returning with error")
		return
	}

	args, err := task.Args.GetArrayArg("args")
	if err != nil {
		err = fmt.Errorf("there was an error getting the 'args' argument: %s", err)
		resp.Error = err.Error()
		resp.Success = false
		logging.LogError(err, "returning with error")
		return
	}

	stdout, err := mcp.NewClient(command, args)
	if err != nil {
		err = fmt.Errorf("there was an error creating the MCP client: %s", err)
		resp.Error = err.Error()
		resp.Success = false
		logging.LogError(err, "returning with error")
		return
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

	resp.Success = true
	resp.Completed = &r.Success
	return
}
