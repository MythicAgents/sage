package bedrock

import (
	// Standard
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	// Internal
	"github.com/MythicAgents/sage/Payload_Type/sage/container/pkg/env"

	// Mythic
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"

	//AWS
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	awsbedrock "github.com/aws/aws-sdk-go-v2/service/bedrock"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
)

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

func GetBedrockClient(task *structs.PTTaskMessageAllData) (bedrockClient *awsbedrock.Client, err error) {
	cfg, err := GetAWSConfig(task)
	if err != nil {
		return nil, fmt.Errorf("failed to get AWS Config: %v", err)
	}

	bedrockClient = awsbedrock.NewFromConfig(cfg)
	return bedrockClient, nil
}

func GetBedrockRuntimeClient(task *structs.PTTaskMessageAllData) (bedrockRuntimeClient *bedrockruntime.Client, err error) {
	cfg, err := GetAWSConfig(task)

	bedrockRuntimeClient = bedrockruntime.NewFromConfig(cfg)
	return bedrockRuntimeClient, nil
}

func ListFoundationalModels(task *structs.PTTaskMessageAllData) (models []string, err error) {
	cfg, err := GetAWSConfig(task)
	if err != nil {
		return nil, fmt.Errorf("failed to get AWS Config: %v", err)
	}

	bedrockClient := awsbedrock.NewFromConfig(cfg)

	ctx := context.Background()
	// Get the Foundational Models
	input := &awsbedrock.ListFoundationModelsInput{}
	result, err := bedrockClient.ListFoundationModels(ctx, input)
	if err != nil {
		return nil, fmt.Errorf("failed to list foundational models: %v", err)
	}
	if len(result.ModelSummaries) == 0 {
		models = append(models, "There are no foundation models.")
		return
	}

	for _, modelSummary := range result.ModelSummaries {
		models = append(models, *modelSummary.ModelId)
	}
	return models, nil
}

// Chat invokes the specified Amazon Bedrock model with the given prompt and data.
// The task must include the following parameters (named exactly as shown):
// - model - The model string to use for inference
// - prompt - The prompt string to use for inference
// - data - The data to use for inference
// - AWS_ACCESS_KEY_ID
// - AWS_SECRET_ACCESS_KEY
// - AWS_SESSION_TOKEN
// - AWS_DEFAULT_REGION
func Chat(task *structs.PTTaskMessageAllData, prompt string) (output string, err error) {
	client, err := GetBedrockRuntimeClient(task)
	if err != nil {
		return "", fmt.Errorf("failed to get Bedrock Runtime client: %v", err)
	}

	// Get the model string
	modelID, err := env.Get(task, "model")
	if err != nil {
		return "", err
	}

	// Get the data
	data := []byte{} // Need to implement this to get the data from the task

	// Anthropic Claude requires you to enclose the prompt as follows:
	//prefix := "Human: "
	//postfix := "\n\nAssistant:"
	//wrappedPrompt := prefix + prompt + postfix
	c := []Content{
		{
			Type: "text",
			Text: prompt,
		},
	}

	if len(data) > 0 {
		// Get the file MIME type
		fileType := http.DetectContentType(data[:512])
		logging.LogDebug(fmt.Sprintf("File MIME Type: %v", fileType))

		var fileContent Content

		// See if the file is an image
		if strings.Contains(fileType, "image") {
			fileContent.Type = "image"
		} else {
			fileContent.Type = "document"
		}

		source := Source{
			Type:      "base64",
			MediaType: fileType,
			Data:      base64.StdEncoding.EncodeToString(data),
		}
		fileContent.Source = &source

		c = append(c, fileContent)
	}

	m := Messages{
		Role:    "user",
		Content: c,
	}

	request := ClaudeRequest{
		AnthropicVersion: "bedrock-2023-05-31",
		Messages:         []Messages{m},
		MaxTokens:        200,
	}

	body, err := json.Marshal(request)
	if err != nil {
		return "", fmt.Errorf("failed to marshal request to JSON: %v", err)
	}

	ctx := context.Background()

	logging.LogDebug(fmt.Sprintf("Calling Bedrock Runtime client Invoke Model: %s with body: %v", modelID, string(body)))
	result, err := client.InvokeModel(ctx, &bedrockruntime.InvokeModelInput{
		ModelId:     aws.String(modelID),
		ContentType: aws.String("application/json"),
		Body:        body,
	})

	//logging.LogDebug(fmt.Sprintf("Result: %v, Error: %v", result, err))
	if err != nil {
		errMsg := err.Error()
		if strings.Contains(errMsg, "no such host") {
			err = fmt.Errorf("error: The Bedrock service is not available in the selected region. Please double-check the service availability for your region at https://aws.amazon.com/about-aws/global-infrastructure/regional-product-services/")
			return
		} else if strings.Contains(errMsg, "could not resolve the foundation model") {
			err = fmt.Errorf("error: Could not resolve the foundation model from model identifier: \"%v\". Please verify that the requested model exists and is accessible within the specified region", modelID)
			return
		} else {
			err = fmt.Errorf("error: Couldn't invoke Anthropic Claude. Here's why: %v", err)
			return
		}
	}

	var response ClaudeResponse

	err = json.Unmarshal(result.Body, &response)

	if err != nil {
		err = fmt.Errorf("failed to unmarshal response from JSON: %v", err)
		return
	}

	logging.LogDebug(fmt.Sprintf("Unmarshalled Response: %+v", response))
	output = response.Content[0].Text
	return
}
