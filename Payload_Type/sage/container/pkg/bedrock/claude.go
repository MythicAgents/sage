package bedrock

// Each model provider defines their own individual request and response formats.
// For the format, ranges, and default values for the different models, refer to:
// https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters.html

// ClaudeRequest is the request format for the Claude model using the Messages API.
// https://docs.anthropic.com/en/api/messages
type ClaudeRequest struct {
	AnthropicVersion string     `json:"anthropic_version"`        // The version of the Anthropic API you want to use.
	AnthropicBeta    []string   `json:"anthropic_beta,omitempty"` // Optional header to specify the beta version(s) you want to use.
	MaxTokens        int        `json:"max_tokens"`               // The maximum number of tokens to generate before stopping.
	Messages         []Messages `json:"messages"`
	StopSequence     []string   `json:"stop_sequence,omitempty"` // Custom text sequences that will cause the model to stop generating.
	Stream           bool       `json:"stream,omitempty"`
	System           string     `json:"system,omitempty"`
	Temperature      float64    `json:"temperature,omitempty"`
}

type ClaudeResponse struct {
	ID           string    `json:"id"`
	Content      []Content `json:"content"`
	Model        string    `json:"model"`
	Role         string    `json:"role"`
	StopReason   string    `json:"stop_reason"`
	StopSequence string    `json:"stop_sequence"`
	Type         string    `json:"type"`
	Usage        Usage     `json:"usage"`
}

type Messages struct {
	Role    string    `json:"role"`
	Content []Content `json:"content"`
}

type Content struct {
	Type   string  `json:"type"`
	Text   string  `json:"text"`
	Source *Source `json:"source,omitempty"`
}

type Image struct {
	Type      string `json:"type"`
	MediaType string `json:"media_type"`
	Data      []byte `json:"data"`
}

type Usage struct {
	InputTokens  int `json:"input_tokens"`
	OutputTokens int `json:"output_tokens"`
}

type Source struct {
	Type      string `json:"type"`
	MediaType string `json:"media_type"`
	Data      string `json:"data"`
}
