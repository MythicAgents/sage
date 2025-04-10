package anthropic

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	// Internal
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/env"
	sageMCP "github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/mcp"
	sageMessage "github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/message"

	// Mythic
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"

	// 3rd Party
	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/bedrock"
	"github.com/anthropics/anthropic-sdk-go/option"
	"github.com/anthropics/anthropic-sdk-go/packages/param"
	"github.com/anthropics/anthropic-sdk-go/shared/constant"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/mark3labs/mcp-go/mcp"
)

func Chat(task *structs.PTTaskMessageAllData, msgs []sageMessage.Message, useTools bool, verbose bool) (response []sageMessage.Message, err error) {
	var messages []anthropic.MessageParam

	// Get the model string
	modelID, err := env.Get(task, "model")
	if err != nil {
		return response, err
	}

	// Get the model provider
	provider, err := env.Get(task, "provider")
	if err != nil {
		return response, err
	}

	var opt option.RequestOption
	if strings.ToLower(provider) == "bedrock" {
		// Get the AWS config
		cfg, err := GetAWSConfig(task)
		if err != nil {
			return response, err
		}
		opt = bedrock.WithConfig(cfg)
	} else {
		ANTHROPIC_API_KEY, err := env.Get(task, "API_KEY")
		if err == nil {
			opt = option.WithAPIKey(ANTHROPIC_API_KEY)
		} else {
			ANTHROPIC_API_KEY = os.Getenv("ANTHROPIC_API_KEY")
			if ANTHROPIC_API_KEY != "" {
				opt = option.WithAPIKey(ANTHROPIC_API_KEY)
			} else {
				ANTHROPIC_API_KEY = os.Getenv("ANTHROPIC_AUTH_TOKEN")
				if ANTHROPIC_API_KEY != "" {
					opt = option.WithAuthToken(ANTHROPIC_API_KEY)
				} else {
					return response, errors.New("unable to find API_KEY, ANTHROPIC_API_KEY, or ANTHROPIC_AUTH_TOKEN in task, secrets, or environment variables")
				}
			}
		}
	}

	client := anthropic.NewClient(opt)

	for _, msg := range msgs {
		tbp := anthropic.TextBlockParam{
			Text: msg.Content,
		}
		var mp anthropic.MessageParam
		if msg.Role == sageMessage.User {
			mp.Role = anthropic.MessageParamRoleUser
		} else if msg.Role == sageMessage.Assistant {
			mp.Role = anthropic.MessageParamRoleAssistant
		}
		mp.Content = []anthropic.ContentBlockParamUnion{
			{
				OfRequestTextBlock: &tbp,
			},
		}
		messages = append(messages, mp)
	}

	// Create the request body
	body := anthropic.MessageNewParams{
		Model:     modelID,
		MaxTokens: 1024,
		Messages:  messages,
	}

	// Get MCP Tools
	if useTools {
		// Add the tools to the request body
		body.Tools = mcpTooltoAnthropicTool(sageMCP.GetAllTools())
	}

	// Send the initial request and iterate over all response messages until we reach a stopping point
	done := false
	for !done {
		var message *anthropic.Message
		message, err = client.Messages.New(context.TODO(), body)
		if err != nil {
			err = fmt.Errorf("😡 Failed to create message: %w", err)
			break
		}
		logging.LogDebug("🐞 Anthropic Response Message", "Message", message)
		switch message.StopReason {
		case anthropic.MessageStopReasonEndTurn: // the model reached a natural stopping point
			done = true
			messages = append(messages, message.ToParam())
		case anthropic.MessageStopReasonMaxTokens: // we exceeded the requested max_tokens or the model's maximum
			done = true
			messages = append(messages, message.ToParam())
		case anthropic.MessageStopReasonStopSequence: // one of your provided custom stop_sequences was generated
			done = true
			messages = append(messages, message.ToParam())
		case anthropic.MessageStopReasonToolUse: // the model invoked one or more tools
			var trbp anthropic.ToolResultBlockParam
			trbp, err = toolUse(message.Content, &messages)
			if err != nil {
				err = fmt.Errorf("😡 Failed to execute tool: %w", err)
				break
			}
			cbpu := anthropic.ContentBlockParamUnion{
				OfRequestToolResultBlock: &trbp,
			}
			mp := anthropic.MessageParam{
				Role:    anthropic.MessageParamRoleUser,
				Content: []anthropic.ContentBlockParamUnion{cbpu},
			}
			messages = append(messages, mp)
			body.Messages = messages
		default:
			err = fmt.Errorf("😡 Unknown Anthropic stop reason: %v", message.StopReason)
		}
		if err != nil {
			break
		}
	}

	// Loop through the messages and return the new ones
	for i := len(msgs); i < len(messages); i++ {
		m := messages[i]
		for _, c := range m.Content {
			r := sageMessage.Message{
				Role: sageMessage.Assistant,
			}
			if c.OfRequestTextBlock != nil {
				r.Content = c.OfRequestTextBlock.Text
			}
			if c.OfRequestToolUseBlock != nil {
				var input string
				if _, ok := c.OfRequestToolUseBlock.Input.(json.RawMessage); ok {
					var result map[string]interface{}
					json.Unmarshal(c.OfRequestToolUseBlock.Input.(json.RawMessage), &result)
					input = fmt.Sprintf("%+v", result)
				} else {
					input = fmt.Sprintf("%v", c.OfRequestToolUseBlock.Input)
				}
				r.Content = fmt.Sprintf("🛠️ Tool Use Block - ID: %s, Tool: %s, Input: %+v", c.OfRequestToolUseBlock.ID, c.OfRequestToolUseBlock.Name, input)
			}
			if c.OfRequestToolResultBlock != nil {
				r.Content = fmt.Sprintf("🛠️ Tool Result Block - ID: %s, Result:\n%s", c.OfRequestToolResultBlock.ToolUseID, c.OfRequestToolResultBlock.Content[0].OfRequestTextBlock.Text)
			}
			if c.OfRequestImageBlock != nil {
				r.Content = "⚠️ Unhandled Message Type: Image Block"
			}
			if c.OfRequestThinkingBlock != nil {
				r.Content = fmt.Sprintf("<🤔 Thinking - Signature: %s>\n%s</🤔 Thinking>", c.OfRequestThinkingBlock.Signature, c.OfRequestThinkingBlock.Thinking)
			}
			if c.OfRequestRedactedThinkingBlock != nil {
				r.Content = fmt.Sprintf("<🔒 Redacted Thinking>\n%s</🔒 Redacted Thinking>", c.OfRequestRedactedThinkingBlock.Data)
			}
			response = append(response, r)
		}
	}
	return
}

func toolUse(content []anthropic.ContentBlockUnion, messages *[]anthropic.MessageParam) (trb anthropic.ToolResultBlockParam, err error) {
	for _, m := range content {
		switch variant := m.AsAny().(type) {
		case anthropic.TextBlock:
			logging.LogDebug("🤖 Tool Use TextBlock", "Text", variant.Text)
			msg := anthropic.MessageParam{
				Role: anthropic.MessageParamRoleAssistant,
			}
			x := variant.ToParam()
			tbp := anthropic.ContentBlockParamUnion{
				OfRequestTextBlock: &x,
			}
			msg.Content = append(msg.Content, tbp)
			*messages = append(*messages, msg)
		case anthropic.ToolUseBlock:
			//fmt.Printf("🛠️ ToolUseBlock - ID: %s, Tool: %s\n", variant.ID, variant.Name)
			logging.LogDebug("🛠️ Tool Use ToolUseBlock", m, "Input", variant.Input)
			msg := anthropic.MessageParam{
				Role: anthropic.MessageParamRoleAssistant,
			}
			x := variant.ToParam()
			tbp := anthropic.ContentBlockParamUnion{
				OfRequestToolUseBlock: &x,
			}
			msg.Content = append(msg.Content, tbp)
			*messages = append(*messages, msg)
			trb, err = ExecuteTools(variant)
		case anthropic.ThinkingBlock:
			err = fmt.Errorf("⚠️ Unhandled ContentBlockUnion Variant (ThinkingBlock): %+v", variant)
		case anthropic.RedactedThinkingBlock:
			err = fmt.Errorf("⚠️ Unhandled ContentBlockUnion Variant (RedactedThinkingBlock): %+v", variant)
		default:
			err = fmt.Errorf("⚠️ Unhandled ContentBlockUnion Variant (%T): %+v", variant, variant)
		}
	}
	return
}

func ExecuteTools(tub anthropic.ToolUseBlock) (trb anthropic.ToolResultBlockParam, err error) {
	// Convert JSON to map
	var args map[string]interface{}
	err = json.Unmarshal(tub.Input, &args)
	if err != nil {
		return
	}

	trb.IsError = param.NewOpt(false)

	var response string
	response, err = sageMCP.ExecuteTool(tub.Name, args)
	if err != nil {
		trb.IsError = param.NewOpt(true)
	}

	resp := anthropic.ToolResultBlockParamContentUnion{
		OfRequestTextBlock: &anthropic.TextBlockParam{Text: response},
	}

	trb.ToolUseID = tub.ID
	trb.Content = []anthropic.ToolResultBlockParamContentUnion{resp}

	return
}

// mcpTooltoAnthropicTool converts MCP tools to Anthropics tools format
func mcpTooltoAnthropicTool(mcpTools []mcp.Tool) (tools []anthropic.ToolUnionParam) {
	for _, tool := range mcpTools {
		newTool := anthropic.ToolParam{
			Name:        tool.Name,
			Description: param.NewOpt(tool.Description),
			InputSchema: anthropic.ToolInputSchemaParam{
				Type:       constant.Object(tool.InputSchema.Type),
				Properties: tool.InputSchema.Properties},
		}
		tools = append(tools, anthropic.ToolUnionParam{
			OfTool: &newTool,
		})
	}
	return
}

func GetAWSConfig(task *structs.PTTaskMessageAllData) (cfg aws.Config, err error) {
	// Get the AWS_ACCESS_KEY_ID
	AWS_ACCESS_KEY_ID, err := env.Get(task, "AWS_ACCESS_KEY_ID")
	if err != nil {
		return cfg, fmt.Errorf("there was an error getting the 'AWS_ACCESS_KEY_ID' argument: %s", err)
	}

	// Get the AWS_SECRET_ACCESS_KEY
	AWS_SECRET_ACCESS_KEY, err := env.Get(task, "AWS_SECRET_ACCESS_KEY")
	if err != nil {
		return cfg, fmt.Errorf("there was an error getting the 'AWS_SECRET_ACCESS_KEY' argument: %s", err)
	}

	// Get the AWS_SESSION_TOKEN
	AWS_SESSION_TOKEN, err := env.Get(task, "AWS_SESSION_TOKEN")
	if err != nil {
		return cfg, fmt.Errorf("there was an error getting the 'AWS_SESSION_TOKEN' argument: %s", err)
	}

	// Get the AWS_DEFAULT_REGION from the User's secrets
	AWS_DEFAULT_REGION, err := env.Get(task, "AWS_DEFAULT_REGION")
	if err != nil {
		return cfg, fmt.Errorf("there was an error getting the 'AWS_DEFAULT_REGION' argument: %s", err)
	}

	// Setup the AWS Config
	ctx := context.Background()
	cfg, err = config.LoadDefaultConfig(
		ctx,
		config.WithCredentialsProvider(
			credentials.NewStaticCredentialsProvider(
				AWS_ACCESS_KEY_ID,
				AWS_SECRET_ACCESS_KEY,
				AWS_SESSION_TOKEN,
			),
		),
		config.WithRegion(AWS_DEFAULT_REGION),
	)
	if err != nil {
		err = fmt.Errorf("configuration error, %v", err)
	}

	return
}
