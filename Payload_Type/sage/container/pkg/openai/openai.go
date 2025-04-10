package openai

import (
	// Standard
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	// 3rd Party
	"github.com/mark3labs/mcp-go/mcp"
	oai "github.com/sashabaranov/go-openai"

	// Internal
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/env"
	sageMCP "github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/mcp"
	sageMessage "github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/message"

	// Mythic
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"
	"github.com/MythicMeta/MythicContainer/mythicrpc"
)

func GetClient(task *structs.PTTaskMessageAllData) (client *oai.Client, err error) {
	// Get the OPENAI_API_ENDPOINT
	OPENAI_API_ENDPOINT, err := env.Get(task, "API_ENDPOINT")
	if err != nil {
		return nil, err
	}

	// Get the OPENAI_API_KEY
	OPENAI_API_KEY, _ := env.Get(task, "API_KEY")

	cfg := oai.DefaultConfig(OPENAI_API_KEY)
	cfg.BaseURL = OPENAI_API_ENDPOINT
	cfg.HTTPClient = &http.Client{
		// allow insecure tls
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
		},
	}
	client = oai.NewClientWithConfig(cfg)
	return
}

func ChatWithStream(task *structs.PTTaskMessageAllData) (output string, err error) {
	// Get the model
	model, err := env.Get(task, "model")
	if err != nil {
		return "", err
	}

	// Get the prompt
	prompt, err := env.Get(task, "prompt")
	if err != nil {
		return "", err
	}

	c, err := GetClient(task)
	if err != nil {
		return
	}

	ctx := context.Background()

	req := oai.ChatCompletionRequest{
		Model:     model,
		MaxTokens: 20,
		Messages: []oai.ChatCompletionMessage{
			{
				Role:    oai.ChatMessageRoleUser,
				Content: prompt,
			},
		},
		Stream: true,
	}

	var stream *oai.ChatCompletionStream
	stream, err = c.CreateChatCompletionStream(ctx, req)
	if err != nil {
		err = fmt.Errorf("ChatCompletionStream error: %v", err)
		return
	}
	defer stream.Close()

	for {
		response, errStream := stream.Recv()
		if errors.Is(errStream, io.EOF) {
			logging.LogDebug("Stream finished EOF")
			return
		}

		if err != nil {
			err = fmt.Errorf("there was an error with the ChatCompletionStream: %v", err)
			return
		}
		logging.LogDebug(fmt.Sprintf("Stream Response: %s", response.Choices[0].Delta.Content))

		var resp *mythicrpc.MythicRPCResponseCreateMessageResponse
		resp, err = mythicrpc.SendMythicRPCResponseCreate(mythicrpc.MythicRPCResponseCreateMessage{
			TaskID:   task.Task.ID,
			Response: []byte(response.Choices[0].Delta.Content),
		})
		logging.LogDebug(fmt.Sprintf("MythicRPC SendMythicRPCResponse Create Response: %v", resp))
		if err != nil {
			return
		}
	}
}

func Chat(task *structs.PTTaskMessageAllData, msgs []sageMessage.Message, useTools bool, verbose bool) (response []sageMessage.Message, err error) {
	// Get the model
	model, err := env.Get(task, "model")
	if err != nil {
		return response, err
	}

	// Get the OPENAI_API_ENDPOINT
	OPENAI_API_ENDPOINT, err := env.Get(task, "API_ENDPOINT")
	if err != nil {
		return response, err
	}

	c, err := GetClient(task)
	if err != nil {
		return
	}

	var messages []oai.ChatCompletionMessage
	for _, m := range msgs {
		var mp oai.ChatCompletionMessage
		if m.Role == sageMessage.User {
			mp.Role = oai.ChatMessageRoleUser
		} else if m.Role == sageMessage.Assistant {
			mp.Role = oai.ChatMessageRoleAssistant
		}
		mp.Content = m.Content
		messages = append(messages, mp)
	}

	req := oai.ChatCompletionRequest{
		Model:  model,
		Stream: false,
	}

	// Get MCP Tools
	if useTools {
		// Add the tools to the request body
		req.Tools = mcpTooltoOAITool(sageMCP.GetAllTools())
	}

	logging.LogInfo(fmt.Sprintf("Using OpenAI provider, calling model: %s, endpoint: %s", model, OPENAI_API_ENDPOINT))

	done := false
	for !done {
		var resp oai.ChatCompletionResponse
		req.Messages = messages
		//logging.LogDebug("Chat Completion Request", "Request", req)
		resp, err = c.CreateChatCompletion(context.Background(), req)
		if err != nil {
			err = fmt.Errorf("chatCompletion error: %v", err)
			return
		}
		if len(resp.Choices) <= 0 {
			done = true
			break
		}
		for i, choice := range resp.Choices {
			logging.LogDebug(fmt.Sprintf("Choice (%d): %+v", i, choice))
			switch choice.FinishReason {
			case oai.FinishReasonStop:
				done = true
				logging.LogDebug("FinishResonStop", choice.Message.Content)
			case oai.FinishReasonLength:
				done = true
				logging.LogDebug("FinishResonLength", choice.Message.Content)
			case oai.FinishReasonToolCalls:
				messages = append(messages, choice.Message)
				for _, toolCall := range choice.Message.ToolCalls {
					response = append(response, sageMessage.Message{
						Role:    sageMessage.Assistant,
						Content: fmt.Sprintf("🛠️ Tool Call - ID: %s, Type: %s, Name: %s, Arguments: %s", toolCall.ID, toolCall.Type, toolCall.Function.Name, toolCall.Function.Arguments),
					})
					var toolResponse oai.ChatCompletionMessage
					toolResponse, err = toolUse(toolCall)
					if err != nil {
						err = fmt.Errorf("toolUse error: %v", err)
						return
					}
					// Add the tool responses to the messages
					messages = append(messages, toolResponse)
					response = append(response, sageMessage.Message{
						Role:    sageMessage.Assistant,
						Content: fmt.Sprintf("🛠️ Tool Call Result: %s", toolResponse.Content),
					})
				}
			case oai.FinishReasonContentFilter:
				done = true
				logging.LogDebug("FinishResonContentFilter", choice.Message.Content)
			case oai.FinishReasonNull:
				done = true
				logging.LogDebug("FinishResonNull", choice.Message.Content)
			default:
				done = true
				logging.LogDebug("FinishResonUnknown", choice.Message.Content)
			}
		}
		if len(resp.Choices) > 0 {
			for _, choice := range resp.Choices {
				if choice.FinishReason != oai.FinishReasonToolCalls && choice.Message.Content != "" {
					response = append(response, sageMessage.Message{
						Role:    sageMessage.Assistant,
						Content: choice.Message.Content,
					})
				}
			}
		}
	}

	//logging.LogDebug(fmt.Sprintf("ChatCompletion Response (%d): %s", len(response), response))
	logging.LogDebug("OpenAI Chat Completion Response", "Count", len(response))
	return
}

func List(task *structs.PTTaskMessageAllData) (output string, err error) {
	c, err := GetClient(task)
	if err != nil {
		return
	}

	ctx := context.Background()

	models, err := c.ListModels(ctx)
	if err != nil {
		err = fmt.Errorf("ListModels error: %v", err)
		return
	}

	for _, m := range models.Models {
		output += fmt.Sprintf("%s\n", m.ID)
	}

	logging.LogDebug(fmt.Sprintf("ListModels Response: %s", output))
	return
}

// mcpTooltoOAITool converts MCP tools to OpenAI tools format
func mcpTooltoOAITool(mcpTools []mcp.Tool) (tools []oai.Tool) {
	for _, tool := range mcpTools {
		fd := oai.FunctionDefinition{
			Name:        tool.Name,
			Description: tool.Description,
			Parameters:  tool.InputSchema,
		}
		//fmt.Printf("Tool: %s, Description: %s, Parameters: %s\n", tool.Name, tool.Description, tool.InputSchema)

		t := oai.Tool{
			Type:     oai.ToolTypeFunction,
			Function: &fd,
		}
		tools = append(tools, t)
	}
	return
}

func toolUse(call oai.ToolCall) (message oai.ChatCompletionMessage, err error) {
	// Convert JSON to map
	var args map[string]interface{}
	err = json.Unmarshal([]byte(call.Function.Arguments), &args)
	if err != nil {
		return
	}
	var toolResponse string
	toolResponse, err = sageMCP.ExecuteTool(call.Function.Name, args)
	if err != nil {
		return
	}
	if toolResponse == "" {
		toolResponse = "success"
	}
	message = oai.ChatCompletionMessage{
		Role:       oai.ChatMessageRoleTool,
		ToolCallID: call.ID,
		Content:    toolResponse,
	}
	//logging.LogDebug(fmt.Sprintf("Tool Response: %+v", message))

	return
}
