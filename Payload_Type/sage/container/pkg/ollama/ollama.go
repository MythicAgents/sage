package ollama

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

func List(task *structs.PTTaskMessageAllData) (output string, err error) {
	// Get the OLLAMA_API_ENDPOINT
	OLLAMA_API_ENDPOINT, err := env.Get(task, "API_ENDPOINT")
	if err != nil {
		return "", err
	}

	endpoint := "api/tags"

	parsedBase, err := url.Parse(OLLAMA_API_ENDPOINT)
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
	var response ModelList
	err = json.Unmarshal(body, &response)
	if err != nil {
		return "", fmt.Errorf("failed to unmarshal JSON: %w", err)
	}
	logging.LogDebug(fmt.Sprintf("Data: %+v", response))

	for _, m := range response.Models {
		output += fmt.Sprintf("%s\n", m.Name)
	}
	return
}

func Generate(task *structs.PTTaskMessageAllData, modelID string, prompt string) (output string, err error) {
	// Get the OLLAMA_API_ENDPOINT
	OLLAMA_API_ENDPOINT, err := env.Get(task, "API_ENDPOINT")
	if err != nil {
		return "", err
	}

	endpoint := "api/generate"

	request := Request{
		Model:  modelID,
		Prompt: prompt,
		Stream: false,
	}

	// Marshal request into JSON
	reqBody, err := json.Marshal(request)
	if err != nil {
		return "", fmt.Errorf("failed to marshal request into JSON: %s", err)
	}

	parsedBase, err := url.Parse(OLLAMA_API_ENDPOINT)
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
	var response Response
	err = json.Unmarshal(body, &response)
	if err != nil {
		return "", fmt.Errorf("failed to unmarshal JSON: %w", err)
	}
	logging.LogDebug(fmt.Sprintf("Response: %+v", response))

	output = response.Resp

	return
}
