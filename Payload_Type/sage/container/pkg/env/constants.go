package env

type Provider int

const (
	OpenAI Provider = iota
	Bedrock
)

func Providers() []Provider {
	return []Provider{OpenAI, Bedrock}
}

func ProvidersString() []string {
	return []string{"openai", "bedrock"}
}

func (p Provider) String() string {
	switch p {
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
