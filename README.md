# Sage

<p align="center">
    <img src="Sage.png">
</p>

Sage is a virtual Mythic agent that that uses an AI agentic system to operate Mythic and Mythic agents running on compromised hosts. Sage does not run on a compromised host, it runs entirely in the Sage container. Sage leverages external AI model providers (e.g., Anthropic, Ollama, OpenAI) for inference and requires API keys for the selected provider.

## Getting Started

> **__NOTE:__** REQUIRES MYTHIC v3.3.1-rc57 OR LATER

### Download & Install

To get started:

1. Clone the [Mythic](https://github.com/its-a-feature/Mythic/) repository
2. Pull down the [Sage](https://github.com/MythicAgents/sage) agent from the MythicAgents organization
3. Start Mythic
4. Navigate to <https://127.0.0.1:7443> and login with a username of `mythic_admin` and password retrieved from the `.env` file

This code snippet will execute most of the getting started steps:
```text
cd ~/
git clone https://github.com/its-a-feature/Mythic
cd Mythic/
sudo make
sudo ./mythic-cli install github https://github.com/MythicAgents/sage
sudo ./mythic-cli start
sudo cat .env | grep MYTHIC_ADMIN_PASSWORD
```

### Model Access & Authentication

Sage uses the following **CASE SENSITIVE** settings/keys to determine how to interact with models:

- `provider` - Who is providing the model (e.g., Anthropic, Amazon Bedrock, LiteLLM, OpenAI, etc.)? 
  - Many model providers (e.g., LiteLLM, Ollama, LM Studio) use the OpenAI API spec; select OpenAI in this case
- `model` - The model string that the provider uses to determine which model to use for inference (e.g., `gpt-4o-mini` or `us.anthropic.claude-3-5-sonnet-20241022-v2:0`)
- `API_ENDPOINT` - Where to send HTTP request for the model provider (e.g. `https://api.openai.com/v1` or `http://127.0.0.1:11434/v1`)
  - This key is not used for Amazon Bedrock calls and can be left blank
  - Can be left blank if using standard API for OpenAI or Anthropic
- `API_KEY` - The API key needed to authenticate to the model provider (e.g., `sk-az1RLw7XUWGXGUBcSgsNT5BlbkFJdbGbUgbbk7BUG9y6ezzb`)
- Amazon Bedrock
  - `AWS_ACCESS_KEY_ID`
  - `AWS_SECRET_ACCESS_KEY`
  - `AWS_SESSION_TOKEN`
  - `AWS_DEFAULT_REGION`


> **__NOTE:__** WHERE SETTINGS AND CREDENTIALS ARE CONFIGURED OR SET MATTERS

 These settings/keys can be provided in 4 different places to provide maximum flexibility. When a command is issued, Sage will look for credentials in this order and stop when the first instance is found:

 1. Task command parameters
 2. **USER** Secrets
 3. Payload build parameters
 4. Payload container system environment variables

 This allows the Sage agent payload to be created with build paramaters so that all operators have access. However, operators can override the the provider, model, and credentials at any time by providing them along side the command that is being issued.

### Create Sage Agent Callback

Sage is a _different_ kind of Mythic agent because it is not an agent that runs on a compromised host. The "agent" is all local and lives on the Mythic server itself. Think of it like a "virtual" agent. Follow these steps to create an agent callback to interact with:

1. Go to the `Payloads` tab in Mythic
2. Click `Actions` -> `Generate New Payload`
3. Select `sage` for the target operating system
4. Click next on the Payload Type screen
5. Fill out the build parameters, if any; See [Model Access & Authentication](#model-access--authentication)
6. Click next on the Select Commands; there are no commands to add
7. Click next on the Select C2 Profiles; Sage does not use a C2 profile
8. Click the `CREATE PAYLOAD` button to build the agent
9. A new callback will be created during the build process
10. Go to the `Active Callbacks` tab in Mythic to interact with Sage

## Model Providers

### Anthropic

In order to interact with Anthropic, you must set the following values:

- `provider` : `Anthropic`
- `model` : `claude-sonnet-4-5-20250929` or `claude-sonnet-4-5`
  - Example [model strings](https://docs.anthropic.com/en/docs/about-claude/models/all-models) to us with Anthropic
- `API_ENDPOINT` : Leave blank
- `API_KEY` :  `sk-ant-api03-abc123XYZ456_DEF789ghi0JKLmno1PQRsTu2vWXyz34AB56CDef78GHIjk9LMN_OPQRSTUVWXYZabcdef0123456789-ABCDEFG`)

### AWS Bedrock

**You must have an AWS account that has Bedrock permissions AND have access to the desired model in your bedrock configuration**

> **__NOTE:__**: From the aws cli, run the following command to get your AWS secrets: `aws sts get-session-token`

In order to interact with Amazon Bedrock, you must set the following values:

- `provider` : `Bedrock`
- `model` : `us.anthropic.claude-3-5-sonnet-20241022-v2:0`
  - Example [model strings](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html)
  - The first part is the inference [region](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html) (e.g., `us` or `global`)
- `API_ENDPOINT` : Leave blank
- `API_KEY` : Leave blank
- `AWS_ACCESS_KEY_ID` : `AKIAI44QH8DHBEXAMPLE`
- `AWS_SECRET_ACCESS_KEY` : `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`
- `AWS_SESSION_TOKEN` : `IQoJb3JpZ2luX2IQoJb3JpZ2luX2IQoJb3JpZ2luX2IQoJb3JpZ2luX2IQoJb3JpZVERYLONGSTRINGEXAMPLE`
- `AWS_DEFAULT_REGION` : `us-east-1`

### OpenAI

Sage can interact with any OpenAI API capable application (e.g., ollama, OpenWeb UI, LM Studio, or LiteLLM)

> Provide a fake API key for providers like LM Studio because it is required to use the OpenAI library

In order to interact with OpenAI's API, you must set the following: Es

- `provider` : `OpenAI`
- `model` : `gpt-4o-mini`
  - [model strings](https://platform.openai.com/docs/models)
- `API_KEY` : `sk-az1RLw7XUWGXGUBcSgsNT5BlbkFJdbGbUgbbk7BUG9y6ezzb`
- `API_ENDPOINT` : OPTIONAL or `https://api.openai.com/v1`

#### ollama

Download and run [ollama Docker image](https://hub.docker.com/r/ollama/ollama) with: `sudo docker run -d -v ollama:/root/.ollama -p 11434:11434 --name ollama ollama/ollama`. You can work with the container directly using `sudo docker exec -it ollama ollama run llama3`

Alternatively create a Docker compose file with

In order to interact with an ollama, you must set the following:

- `provider`: `OpenAI`
- `model`: `qwen3:1.7b`
  - The selected model must support tools
- `API_ENDPOINT`: `http://127.0.0.1:11434/v1`
- `API_KEY`: `dummy-ollama-key`

## Custom SSL Certificates

If your LLM provider requires custom SSL/TLS certificates (e.g., corporate proxy with custom CA, self-signed certificates, or internal certificate authorities), Sage supports loading a custom certificate bundle by setting the `SSL_CERT_FILE` environment variable. This can be useful for an internally hosted LiteLLM instance.

### Setup

1. Create your certificate bundle in PEM format containing all required CA certificates:
   ```bash
   cat root-ca.pem intermediate-ca.pem > bundle.pem
   ```

2. Place the certificate bundle in the Sage certs directory:
   - **Local development**: `Payload_Type/sage/certs/bundle.pem`
   - **Mythic deployment**: `mythic/InstalledServices/sage/Payload_Type/sage/certs/bundle.pem`

3. Restart the Sage container:
   ```bash
   sudo ./mythic-cli start sage
   ```

### Verification

When Sage starts with a custom certificate bundle, you'll see this message in the container logs:
```
[SAGE] Using custom SSL certificate bundle: /Mythic/certs/bundle.pem
```

If no certificate bundle is found, Sage will use system default certificates:
```
[SAGE] No custom SSL certificate bundle found, using system defaults
```

### Certificate Bundle Format

The certificate bundle must be in PEM format with one or more certificates:

```
-----BEGIN CERTIFICATE-----
MIIDXTCCAkWgAwIBAgIJAKJ... (Base64-encoded certificate)
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIDXTCCAkWgAwIBAgIJAKJ... (Another certificate if needed)
-----END CERTIFICATE-----
```

### Troubleshooting

**Issue**: `SSL: CERTIFICATE_VERIFY_FAILED` errors when connecting to LLM provider

**Solution**:
1. Verify your certificate bundle is in PEM format
2. Ensure it contains all required CA certificates (root and intermediate)
3. Check file permissions (must be readable by the Sage container)
4. Verify the certificate path in Sage startup logs

**Issue**: Certificate bundle not detected

**Solution**:
1. Verify file is named exactly `bundle.pem` (case-sensitive)
2. Verify file is in `Payload_Type/sage/certs/` directory
3. Restart the Sage container after adding the certificate

## Commands

Sage provides the following Mythic commands for interacting with AI models and MCP servers.

### chat

Multi-turn interactive chat session with an AI model. Supports back-and-forth conversation with context preserved across messages. New chats means a brand new context.

```
chat -prompt <prompt>
```

**Parameters:**
| Parameter | Required | Description |
|-----------|----------|-------------|
| `prompt` | Yes | The prompt to send to the model |
| `tools` | No | Enable tool use (default: true) |
| `verbose` | No | Show verbose output of all messages (default: false) |
| `provider` | No | Override the model provider |
| `model` | No | Override the model |
| `API_ENDPOINT` | No | Override the API endpoint |
| `API_KEY` | No | Override the API key |
| `AWS_ACCESS_KEY_ID` | No | AWS credentials for Bedrock |
| `AWS_SECRET_ACCESS_KEY` | No | AWS credentials for Bedrock |
| `AWS_SESSION_TOKEN` | No | AWS credentials for Bedrock |
| `AWS_DEFAULT_REGION` | No | AWS region for Bedrock |

**Example:**
```
chat -prompt "Tell me about active callbacks in Mythic"
```

### query

Send a single query to a model and receive a single response. Unlike `chat`, this does not maintain conversation history.

```
query -prompt <prompt>
```

**Parameters:**
| Parameter | Required | Description |
|-----------|----------|-------------|
| `prompt` | Yes | The prompt to send to the model |
| `tools` | No | Enable tool use (default: true) |
| `verbose` | No | Show verbose output (default: false) |
| `provider` | No | Override the model provider |
| `model` | No | Override the model |
| `API_ENDPOINT` | No | Override the API endpoint |
| `API_KEY` | No | Override the API key |
| `AWS_*` | No | AWS credentials for Bedrock |

**Example:**
```
query -prompt "What is the capital of France?"
```

### list

List all available models for the configured provider.

```
list
```

**Parameters:**
| Parameter | Required | Description |
|-----------|----------|-------------|
| `provider` | No | Override the model provider |
| `API_ENDPOINT` | No | Override the API endpoint |
| `API_KEY` | No | Override the API key |

**Example:**
```
list
```

> **Note:** Listing models for Bedrock is not currently supported. Use the AWS CLI instead.

### mcp-connect

Connect to an MCP (Model Context Protocol) server. Supports STDIO, SSE, and Streamable HTTP transports.

```
mcp-connect -name <server_name> -connection_type <stdio|sse|streamable_http> [options]
```

**Parameters:**
| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | Yes | Unique name for the MCP server connection |
| `connection_type` | Yes | Type of connection: `stdio`, `sse`, or `streamable_http` |
| `command` | STDIO | Command to execute for STDIO MCP server |
| `arguments` | STDIO | Array of command arguments |
| `cwd` | No | Working directory for STDIO command |
| `url` | SSE/HTTP | URL for SSE or HTTP streaming connection |
| `headers` | No | HTTP headers (format: `Key: Value`) |
| `timeout` | No | Connection timeout in seconds (default: 30) |
| `sse_read_timeout` | No | SSE read timeout in seconds (default: 300) |
| `terminate_on_close` | No | Terminate HTTP connection on close (default: true) |
| `ssl_verify` | No | Verify SSL certificates (default: true) |

**Examples:**

STDIO connection to Mythic MCP server:
```
mcp-connect -name mythic -connection_type stdio -command uv -arguments --directory -arguments /Mythic/mcp/mythic -arguments run -arguments main.py -arguments mythic_admin -arguments SuperSecretPassword -arguments 192.168.1.100 -arguments 7443
```

SSE connection:
```
mcp-connect -name myserver -connection_type sse -url https://example.com/mcp/sse
```

SSE connection without SSL verification (development only):
```
mcp-connect -name myserver -connection_type sse -url https://example.com/mcp/sse -ssl_verify false
```

### mcp-disconnect

Disconnect from a connected MCP server.

```
mcp-disconnect -name <server_name>
```

**Parameters:**
| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | Yes | Name of the MCP server to disconnect |

**Example:**
```
mcp-disconnect -name mythic
```

### mcp-list

List all connected MCP servers and their available tools with descriptions and parameters.

```
mcp-list
```

**Example Output:**
```
Connected MCP Servers: 1
Total Tools Available: 5
==================================================

Server: mythic
  Connection Type: stdio
  Tools: 5
  Available Tools:
    - list_callbacks
      Description: List all callbacks in Mythic
      Parameters:
        - include_archived (boolean): Include archived callbacks
    - create_task
      Description: Create a new task for a callback
      Parameters:
        - callback_id* (string): The callback ID
        - command* (string): The command to execute
```

### mcp-call

Directly invoke an MCP tool with specified arguments.

> **NOTE**: MCP tool names and arguments are shown after using the `mcp-connect` or `mcp-list` commands

```
mcp-call -tool <tool_name> [-server <server_name>] -args <key> -args <value> ...
```

**Parameters:**
| Parameter | Required | Description |
|-----------|----------|-------------|
| `tool` | Yes | Name of the MCP tool to invoke |
| `server` | No | Server name (required if multiple servers have the same tool names) |
| `args` | No | Tool arguments as alternating key-value pairs |

**Examples:**

Call a tool with no arguments:
```
mcp-call -tool list_callbacks
```

Call a tool with arguments:
```
mcp-call -tool get_callback -args callback_id -args 123
```

Call a tool on a specific server (when tool name conflicts exist):
```
mcp-call -tool search -server mythic -args query -args "admin"
```

> **Note:** Use `mcp-list` to see available tools and their parameter names. Required parameters are marked with `*`.

### exit

Internal Mythic command for callback table functionality. Does nothing when executed directly.

```
exit
```

## Run Sage Locally

Use the following commands to run the Sage container from the command line without using Docker (typicall for testing and troubleshooting):

> **NOTE: Replace the RabbitMQ password with the one from the `.env` file in the root Mythic folder**

```bash
cd sage/Payload_Type/sage
export DEBUG_LEVEL=debug
export MYTHIC_SERVER_HOST="127.0.0.1"
export RABBITMQ_HOST="127.0.0.1"
export RABBITMQ_PASSWORD="K5SHkn1fk2pcT0YkQxTTMgO5gFwjiQ"
python3 main.py
```

## Conversation State & Persistence

Sage maintains conversation state and history using a SQLite database that enables multi-turn conversations and recovery from interruptions.

### What is sage.db?

`sage.db` is a SQLite database used by LangGraph's checkpoint system to persist conversation state. It stores complete conversation histories, agent states, and message flows across all Sage interactions.

### What Does sage.db Store?

The database contains:
- **Conversation Messages**: All user inputs (HumanMessage), AI responses (AIMessage), and tool execution results (ToolMessage)
- **Multi-Agent State**: Isolated message channels for each specialist agent (Supervisor, Generalist, Mythic_Operator, Mythic_Payload)
- **Message Sequences**: Ordering information to maintain conversation flow across agent handoffs
- **Graph State**: Agent counters, recursion limits, and workflow state
- **Thread Identifiers**: Unique conversation threads based on Mythic task IDs

### Thread Identification

Each conversation is identified by a unique thread ID composed of:
```
thread_id = f"{agent_task_id}-{task_id}"
```

Where:
- `agent_task_id`: Mythic's AgentTaskID for the specific callback interaction
- `task_id`: Mythic's Task.ID for the specific command issued

This ensures each Sage task maintains its own isolated conversation context.

### Use Cases

The checkpoint system enables:
1. **Conversation Continuity**: Multi-turn conversations where context is preserved across multiple commands
2. **Recursion Recovery**: When complex tasks hit recursion limits, state is preserved and can be resumed with "continue"
3. **Agent Handoffs**: Supervisor can delegate to specialist agents while maintaining conversation history
4. **Progress Tracking**: Complete audit trail of what agents have done and decided during task execution

### Data Location

- **Local development**: `Payload_Type/sage/sage.db`
- **Mythic deployment**: `mythic/InstalledServices/sage/Payload_Type/sage/sage.db`
- **Note**: This file is excluded from version control but preserved in the repository structure

### Privacy Considerations

The `sage.db` file contains:
- Complete conversation histories with your LLM interactions
- Task details and responses from Mythic API calls
- Tool execution results and agent reasoning

This data persists across Sage container restarts and may contain sensitive operational information. Consider the database contents when sharing the Sage directory or backing up data.

### Maintenance

The database grows over time as conversations accumulate. If you need to clear conversation history:

```bash
# Stop the Sage container first
sudo ./mythic-cli stop sage

# Remove the database file
rm mythic/InstalledServices/sage/Payload_Type/sage/sage.db

# Restart Sage (database will be recreated)
sudo ./mythic-cli start sage
```

**Note**: Deleting sage.db removes all conversation history but does not affect Sage's ability to function. A new database will be created automatically on startup.

## Phoenix Observability

Sage includes integrated observability and tracing powered by [Phoenix](https://github.com/Arize-ai/phoenix) from Arize AI. Phoenix provides real-time monitoring, visualization, and debugging of LLM interactions.

### What is Phoenix?

Phoenix is an open-source observability platform designed specifically for LLM applications. It captures detailed traces of:
- LLM requests and responses
- Token usage and costs
- Latency and performance metrics
- Tool/function calls
- Multi-agent workflows

### Features

- **Real-time Tracing**: Monitor LLM calls as they happen across all Sage sessions
- **Performance Analytics**: Track token usage, response times, and model behavior
- **Multi-Agent Visibility**: Visualize the Sage supervisor and specialist agent interactions
- **No Configuration Required**: Phoenix launches automatically when Sage starts

### Accessing Phoenix

Phoenix automatically starts when Sage launches and is accessible at:
- **URL**: `http://127.0.0.1:6006`
- No authentication required for local development

### Data Storage

Phoenix stores trace data locally at:
- **Location**: `Payload_Type/sage/.phoenix/`
- **Database**: `phoenix.db` (SQLite)
- **Note**: This directory is excluded from version control but preserved in the repository structure

All LLM traces, spans, and observability data are stored in this local database and can be viewed through the Phoenix web interface.

### Learn More

- **GitHub**: [https://github.com/Arize-ai/phoenix](https://github.com/Arize-ai/phoenix)
- **Documentation**: [https://docs.arize.com/phoenix](https://docs.arize.com/phoenix)

## LangSmith

> LangSmith Observability gives you complete visibility into agent behavior with tracing, real-time monitoring, alerting, and high-level insights into usage.

Sage uses LangChain and therefore you can also use [LangSmith](https://www.langchain.com/langsmith/observability) to view AI agent system traces.

To use LangSmith, export the following environment variables before Sage is started:

```
export LANGSMITH_TRACING=true
export LANGSMITH_ENDPOINT=https://api.smith.langchain.com
export LANGSMITH_PROJECT=Sage
export LANGSMITH_API_KEY=lsv2_pt_example_langsmith_api_key
```

## Known Limitations

This project is still early in development and new features and capabilities will be added. Currently, some known limitations are:

- There's no file upload functionality
- Chat sessions are not stream based
- Bedrock provider is limited to Anthropic Claude
