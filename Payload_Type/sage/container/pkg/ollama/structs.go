package ollama

import (
	"time"
)

type Request struct {
	Model   string  `json:"model"`
	Prompt  string  `json:"prompt"`
	Stream  bool    `json:"stream"`
	Suffix  string  `json:"suffix"`
	Options Options `json:"options"`
}

type Response struct {
	Model              string `json:"model"`
	CreatedAt          string `json:"created_at"`
	Resp               string `json:"response"`
	Done               bool   `json:"done"`
	Context            []int  `json:"context"`
	TotalDuration      int64  `json:"total_duration"`
	LoadDuration       int64  `json:"load_duration"`
	PromptEvalCount    int    `json:"prompt_eval_count"`
	PromptEvalDuration int64  `json:"prompt_eval_duration"`
	EvalCount          int    `json:"eval_count"`
	EvalDuration       int64  `json:"eval_duration"`
}

type Options struct {
	Temperature float64 `json:"temperature"`
}

// ModelList represents the top-level structure containing the array of models
type ModelList struct {
	Models []Model `json:"models"`
}

// Model represents each individual model in the list
type Model struct {
	Name       string    `json:"name"`
	ModifiedAt time.Time `json:"modified_at"`
	Size       int64     `json:"size"`
	Digest     string    `json:"digest"`
	Details    Details   `json:"details"`
}

// Details represents the model details structure
type Details struct {
	Format            string   `json:"format"`
	Family            string   `json:"family"`
	Families          []string `json:"families"`
	ParameterSize     string   `json:"parameter_size"`
	QuantizationLevel string   `json:"quantization_level"`
}
