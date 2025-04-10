package env

type Provider int

const (
	Anthropic Provider = iota
	Bedrock
	OpenAI
)

func Providers() []Provider {
	return []Provider{Anthropic, Bedrock, OpenAI}
}

func ProvidersString() []string {
	return []string{"anthropic", "bedrock", "openai"}
}

func (p Provider) String() string {
	switch p {
	case Anthropic:
		return "anthropic"
	case OpenAI:
		return "openai"
	case Bedrock:
		return "bedrock"
	default:
		return "unknown"
	}
}

const (
	SUPPORTED_OS_SAGE string = "sage"
)
