package openai

import (
	// Standard
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net/http"

	// 3rd Party
	oai "github.com/sashabaranov/go-openai"

	// Internal
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/env"

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

func Chat(task *structs.PTTaskMessageAllData, prompt string) (output string, err error) {
	// Get the model
	model, err := env.Get(task, "model")
	if err != nil {
		return "", err
	}

	// Get the OPENAI_API_ENDPOINT
	OPENAI_API_ENDPOINT, err := env.Get(task, "API_ENDPOINT")
	if err != nil {
		return "", err
	}

	c, err := GetClient(task)
	if err != nil {
		return
	}

	ctx := context.Background()

	req := oai.ChatCompletionRequest{
		Model: model,
		Messages: []oai.ChatCompletionMessage{
			{
				Role:    oai.ChatMessageRoleUser,
				Content: prompt,
			},
		},
	}

	logging.LogInfo(fmt.Sprintf("Using OpenAI provider, calling model: %s, endpoint: %s", model, OPENAI_API_ENDPOINT))
	resp, err := c.CreateChatCompletion(ctx, req)
	if err != nil {
		err = fmt.Errorf("ChatCompletion error: %v", err)
		return
	}
	logging.LogDebug(fmt.Sprintf("ChatCompletion Response: %s", resp.Choices[0].Message.Content))
	output = resp.Choices[0].Message.Content
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
