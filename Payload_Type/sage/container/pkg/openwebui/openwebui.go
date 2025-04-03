package openwebui

// https://docs.openwebui.com/getting-started/api-endpoints/

import (
	// Standard
	"bytes"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"

	// Internal
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/env"

	// Mythic
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"
)

type Meta struct {
	ProfileImageURL string   `json:"profile_image_url"`
	Description     string   `json:"description"`
	ModelIDs        []string `json:"model_ids"`
}

type Info struct {
	Meta Meta `json:"meta"`
}

type OpenAI struct {
	ID      string `json:"id"`
	Object  string `json:"object"`
	Created int64  `json:"created"`
	OwnedBy string `json:"owned_by"`
}

type Model struct {
	ID      string   `json:"id"`
	Name    string   `json:"name"`
	Info    *Info    `json:"info,omitempty"`
	Object  string   `json:"object"`
	Created int64    `json:"created"`
	OwnedBy string   `json:"owned_by"`
	Arena   bool     `json:"arena"`
	Actions []string `json:"actions"`
	URLIdx  *int     `json:"urlIdx,omitempty"`
	OpenAI  *OpenAI  `json:"openai,omitempty"`
}

type Response struct {
	Data []Model `json:"data"`
}

type RequestMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// ResponseMessage represents the message object inside a choice.
type ResponseMessage struct {
	Content      string       `json:"content"`
	Role         string       `json:"role"`
	ToolCalls    *interface{} `json:"tool_calls"`
	FunctionCall *interface{} `json:"function_call"`
}

type Completion struct {
	Model   string           `json:"model"`
	Message []RequestMessage `json:"messages"`
}

// ChatCompletion represents the root JSON structure.
type ChatCompletion struct {
	ID                string   `json:"id"`
	Choices           []Choice `json:"choices"`
	Created           int64    `json:"created"`
	Model             string   `json:"model"`
	Object            string   `json:"object"`
	SystemFingerprint *string  `json:"system_fingerprint"`
	Usage             Usage    `json:"usage"`
}

// Choice represents a choice object in the response.
type Choice struct {
	FinishReason string          `json:"finish_reason"`
	Index        int             `json:"index"`
	Message      ResponseMessage `json:"message"`
}

// Usage represents the token usage statistics.
type Usage struct {
	CompletionTokens int `json:"completion_tokens"`
	PromptTokens     int `json:"prompt_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

func Chat(task *structs.PTTaskMessageAllData, prompt string) (output string, err error) {
	// Get the OPEN_WEBUI_API_KEY
	OPEN_WEBUI_API_KEY, err := env.Get(task, "API_KEY")
	if err != nil {
		return "", err
	}

	// Get the OPEN_WEBUI_API_ENDPOINT
	OPEN_WEBUI_API_ENDPOINT, err := env.Get(task, "API_ENDPOINT")
	if err != nil {
		return "", err
	}

	// Get the model
	modelID, err := env.Get(task, "model")
	if err != nil {
		return "", err
	}

	endpoint := "api/chat/completions"

	request := Completion{
		Model: modelID,
		Message: []RequestMessage{
			{
				Role:    "user",
				Content: prompt,
			},
		},
	}

	// Marshal request into JSON
	reqBody, err := json.Marshal(request)
	if err != nil {
		return "", fmt.Errorf("failed to marshal request into JSON: %s", err)
	}

	parsedBase, err := url.Parse(OPEN_WEBUI_API_ENDPOINT)
	if err != nil {
		return "", fmt.Errorf("invalid base URL: %w", err)
	}

	parsedEndpoint, err := url.Parse(endpoint)
	if err != nil {
		return "", fmt.Errorf("invalid endpoint: %w", err)
	}

	// Ensure the endpoint is properly joined with the base URL
	parsedBase.Path = path.Join(parsedBase.Path, parsedEndpoint.Path)

	// Create the POST request
	req, err := http.NewRequest("POST", parsedBase.String(), bytes.NewBuffer(reqBody))
	if err != nil {
		return "", fmt.Errorf("failed to create POST request: %s", err)
	}

	// Set the request headers
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+OPEN_WEBUI_API_KEY)

	// Create the HTTP client
	client := &http.Client{
		// allow insecure tls
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
		},
	}

	logging.LogDebug(fmt.Sprintf("Sending POST request to %s", parsedBase.String()))
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("failed to send POST request: %s", err)
	}
	defer resp.Body.Close()

	// Read the response body
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("failed to read response body: %w", err)
	}

	// Unmarshal JSON response into struct
	var chatCompletion ChatCompletion
	err = json.Unmarshal(body, &chatCompletion)
	if err != nil {
		return "", fmt.Errorf("failed to unmarshal JSON: %w", err)
	}
	logging.LogDebug(fmt.Sprintf("ChatCompletion: %+v", chatCompletion))

	output = chatCompletion.Choices[0].Message.Content

	return
}

func List(task *structs.PTTaskMessageAllData) (output string, err error) {
	// Get the OPEN_WEBUI_API_KEY
	OPEN_WEBUI_API_KEY, err := env.Get(task, "API_KEY")
	if err != nil {
		return "", err
	}

	// Get the OPEN_WEBUI_API_ENDPOINT
	OPEN_WEBUI_API_ENDPOINT, err := env.Get(task, "API_ENDPOINT")
	if err != nil {
		return "", err
	}

	endpoint := "api/models"

	parsedBase, err := url.Parse(OPEN_WEBUI_API_ENDPOINT)
	if err != nil {
		return "", fmt.Errorf("invalid base URL: %w", err)
	}

	parsedEndpoint, err := url.Parse(endpoint)
	if err != nil {
		return "", fmt.Errorf("invalid endpoint: %w", err)
	}

	// Ensure the endpoint is properly joined with the base URL
	parsedBase.Path = path.Join(parsedBase.Path, parsedEndpoint.Path)

	// Create the GET request
	req, err := http.NewRequest("GET", parsedBase.String(), nil)
	if err != nil {
		return "", fmt.Errorf("failed to create GET request: %s", err)
	}

	// Set the request headers
	req.Header.Set("Authorization", "Bearer "+OPEN_WEBUI_API_KEY)

	// Create the HTTP client
	client := &http.Client{
		// allow insecure tls
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
		},
	}

	logging.LogDebug(fmt.Sprintf("Sending GET request to %s", parsedBase.String()))
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("failed to send GET request: %s", err)
	}
	defer resp.Body.Close()

	// Read the response body
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("failed to read response body: %w", err)
	}

	// Unmarshal JSON response into struct
	var response Response
	err = json.Unmarshal(body, &response)
	if err != nil {
		return "", fmt.Errorf("failed to unmarshal JSON: %w", err)
	}
	logging.LogDebug(fmt.Sprintf("Data: %+v", response))

	for _, d := range response.Data {
		output += fmt.Sprintf("ID: %s\n", d.ID)
	}
	return
}
