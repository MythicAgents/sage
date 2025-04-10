package message

type Role int

const (
	User Role = iota
	Assistant
	System
)

func (r Role) String() string {
	switch r {
	case User:
		return "user"
	case Assistant:
		return "assistant"
	case System:
		return "system"
	default:
		return "unknown"
	}
}

type Message struct {
	Role    Role   `json:"role"`
	Content string `json:"content"`
}
