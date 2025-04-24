package mcp

import (
	"context"
	"fmt"
	"time"

	// Mythic
	"github.com/MythicMeta/MythicContainer/logging"

	// 3rd Party
	"github.com/google/uuid"
	"github.com/mark3labs/mcp-go/client"
	"github.com/mark3labs/mcp-go/mcp"
)

var clients []MCPClient

func NewClient(command string, args []string) (resp string, err error) {
	// Create a new MCP client and connect to the MCP server
	mcpClient, err := client.NewStdioMCPClient(command, []string{}, args...)
	if err != nil {
		return "", fmt.Errorf("failed to create MCP client: %w", err)
	}
	//defer mcpClient.Close()

	// Generate a unique ID for the client
	id := uuid.New()
	resp += fmt.Sprintf("🎉 MCP client ID: %s\n\n", id.String())

	// Initialize the request
	logging.LogDebug("🚀 Initializing MCP client...")
	initRequest := mcp.InitializeRequest{}
	initRequest.Params.ProtocolVersion = mcp.LATEST_PROTOCOL_VERSION
	initRequest.Params.ClientInfo = mcp.Implementation{
		Name:    "Sage MCP Client",
		Version: "1.0.0",
	}

	// Create context with timeout
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	// Initialize the client
	initResult, err := mcpClient.Initialize(ctx, initRequest)
	if err != nil {
		err = fmt.Errorf("failed to initialize MCP client: %w", err)
		return
	}
	logging.LogDebug("✅ MCP client initialized successfully!", "Server Name", initResult.ServerInfo.Name, "Server Version", initResult.ServerInfo.Version)
	resp += fmt.Sprintf("MCP client initialized successfully with server: %s %s\n", initResult.ServerInfo.Name, initResult.ServerInfo.Version)

	// Get the list of tools
	toolsRequest := mcp.ListToolsRequest{}
	tools, err := mcpClient.ListTools(ctx, toolsRequest)
	if err != nil {
		err = fmt.Errorf("😡 Failed to list tools: %w", err)
		return
	}

	resp += "🛠️  Tools:\n"
	for _, tool := range tools.Tools {
		//fmt.Printf("\t- %s\n", tool.Name)
		resp += fmt.Sprintf("- [TOOL]%s: %s\n", tool.Name, tool.Description)
		//fmt.Printf("\tArguments: %s\n\n", tool.InputSchema.Properties)
	}

	// Add the client to the list of clients
	clients = append(clients, MCPClient{
		ID:     id,
		Client: mcpClient,
		Tools:  tools,
	})
	return
}

func GetAllTools() (tools []mcp.Tool) {
	// Iterate over all clients and collect their tools
	for _, client := range clients {
		tools = append(tools, client.Tools.Tools...)
		//fmt.Printf("[TOOL] Name: %s\n,Description: %s\n,InputSchema: %+v", client.Tools.Tools[0].Name, client.Tools.Tools[0].Description, client.Tools.Tools[0].InputSchema)
	}
	return
}

func ExecuteTool(toolName string, args map[string]interface{}) (resp string, err error) {
	// Find the client with the specified tool name
	var mcpClient *client.StdioMCPClient
	for _, client := range clients {
		for _, tool := range client.Tools.Tools {
			if tool.Name == toolName {
				mcpClient = client.Client
				break
			}
		}
		if mcpClient != nil {
			break
		}
	}

	if mcpClient == nil {
		return "", fmt.Errorf("tool %s not found", toolName)
	}

	// Create the MCP Tool Call Request
	fetchRequest := mcp.CallToolRequest{
		Request: mcp.Request{
			Method: "tools/call",
		},
	}
	fetchRequest.Params.Name = toolName
	fetchRequest.Params.Arguments = args

	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Minute)
	defer cancel()

	logging.LogDebug("🚀 Calling MCP tool...", "Args", args, "Tool Name", toolName)
	result, err := mcpClient.CallTool(ctx, fetchRequest)
	if err != nil {
		return "", fmt.Errorf("failed to call tool %s: %w", toolName, err)
	}

	// Process the result
	for _, r := range result.Content {
		switch r.(type) {
		case mcp.TextContent:
			logging.LogDebug("🛠️ MCP Tool Call Result", "Tool Name", toolName, "Result", r.(mcp.TextContent).Text)
			resp += r.(mcp.TextContent).Text
		case mcp.ImageContent:
			err = fmt.Errorf("⚠️ Unhandled mcp.Content Result (ImageContent): %+v", r.(mcp.ImageContent))
		case mcp.EmbeddedResource:
			err = fmt.Errorf("⚠️ Unhandled mcp.Content Result (EmbeddedResource): %+v", r.(mcp.EmbeddedResource))
		default:
			err = fmt.Errorf("⚠️ Unhandled mcp.Content Result (%T): %+v", r, r)
		}
	}

	//resp += fmt.Sprintf("🛠️ Tool Result: %s\n", result.Result)
	return resp, nil
}

type MCPClient struct {
	ID     uuid.UUID
	Client *client.StdioMCPClient
	Tools  *mcp.ListToolsResult
}

type ToolProperties struct {
	Properties map[string]ParameterInfo `json:"properties"`
}

type ParameterInfo struct {
	Type        string `json:"type"`
	Description string `json:"description"`
}
