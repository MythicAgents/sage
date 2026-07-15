"""``ChatModelDefinition`` list exposed by the Sage chat container.

Mirrors the legacy ``ChatArguments`` (``container/agent_functions/chat.py``) as typed,
operator-facing chat config: provider/model/mode/max_steps/system_prompt move from CLI args to
UI fields (PRD Section 9). A single ``Sage`` model carries a ``mode`` Choice
(supervised/auto) rather than shipping two near-identical models.
"""

from __future__ import annotations

from mythic_container.ChatBase import (
    ChatModelConfigurationOption,
    ChatModelConfigurationOptionChoice,
    ChatModelConfigurationOptionType as OptType,
    ChatModelDefinition,
    ChatModelMetadata,
)

from .slash import SLASH_COMMANDS

# Declarative: the channel bot-token scopes Sage's action tools need (PRD Section 8A P1 / 14k).
# In Phase 1 tools are unauthenticated (fork [F1]) so nothing enforces these yet; Phase 2 adds the
# `whoami` preflight that disables guarded tools whose scope isn't granted. `chat-ai.write` is the
# one hard requirement even for MVP — it's needed to post responses (Section 5).
_CHANNEL_TOKEN_SCOPES = [
    "chat-ai.write",
    "callback.write",
    "payload.write",
    "file.write",
    "tag.write",
]

# Near-parity with the legacy PayloadType `ChatArguments` (container/agent_functions/chat.py): the
# operator-facing options from "creating Sage" as an agent are restored here as chat config fields.
# Two deliberate omissions: the legacy `prompt` arg is the operator's per-turn message
# (ChatRequest.Prompt), not a config field; and `verbose` is dropped entirely — the collapsible tool
# cards ARE the detailed view now, so the chat container always runs at full detail with no toggle.
# `system_prompt` is a chat-era addition kept alongside.
# Each field resolves Config → Secret → env → default in config.py, so the sensitive fields below
# (API_KEY / API_ENDPOINT / AWS quad) can be filled here OR left blank to fall back to a user secret /
# container env — they are ALSO declared as OptionalUserSecrets on the model metadata.
_CONFIG_OPTIONS = [
    ChatModelConfigurationOption(
        Name="provider",
        DisplayName="Provider",
        DisplayAsChip=True,
        Type=OptType.Choice,
        Description="The model provider to interact with. Bedrock uses the AWS quad below; the others use API Key / API Endpoint.",
        Required=False,
        DefaultValue="openai",
        Choices=[
            ChatModelConfigurationOptionChoice(Label="OpenAI", Value="openai"),
            ChatModelConfigurationOptionChoice(Label="AWS Bedrock", Value="bedrock"),
            ChatModelConfigurationOptionChoice(Label="Anthropic", Value="anthropic"),
            ChatModelConfigurationOptionChoice(Label="Ollama", Value="ollama"),
        ],
    ),
    ChatModelConfigurationOption(
        Name="model",
        DisplayName="Model",
        DisplayAsChip=True,
        Type=OptType.String,
        Description="The model to use for inference from the selected provider, e.g. gpt-5.5-cyber-preview or claude-sonnet-5.",
        Required=False,
    ),
    ChatModelConfigurationOption(
        Name="mode",
        DisplayName="Mode",
        Type=OptType.Choice,
        Description=(
            "Supervised keeps scoped questions on the multi-agent supervisor and runs explicit objectives "
            "through the policy-selected execution kernel with approvals. Autonomous runs the same kernel "
            "unattended."
        ),
        Required=False,
        DefaultValue="supervised",
        Choices=[
            ChatModelConfigurationOptionChoice(Label="Supervised", Value="supervised"),
            ChatModelConfigurationOptionChoice(Label="Autonomous", Value="auto"),
        ],
    ),
    ChatModelConfigurationOption(
        Name="autonomous_solve",
        DisplayName="Autonomous Solve",
        Type=OptType.Boolean,
        Description=(
            "Force deterministic multi-hop solving for this session. In supervised mode, controller moves "
            "still require approval; off leaves scoped prompts on the supervisor while explicit objectives "
            "still use approved solving. Equivalent to Mode=Autonomous when mode is auto."
        ),
        Required=False,
        DefaultValue=False,
    ),
    ChatModelConfigurationOption(
        Name="policy_mode",
        DisplayName="Policy",
        Type=OptType.Choice,
        Description=(
            "Symbolic is the temporary safe default. Hybrid has the model select from the complete deterministic "
            "admissible frontier only at real branches. LLM calls the model on every nonempty frontier."
        ),
        Required=False,
        DefaultValue="symbolic",
        Choices=[
            ChatModelConfigurationOptionChoice(Label="LLM", Value="llm"),
            ChatModelConfigurationOptionChoice(Label="Hybrid", Value="hybrid"),
            ChatModelConfigurationOptionChoice(Label="Symbolic Baseline", Value="symbolic"),
        ],
    ),
    ChatModelConfigurationOption(
        Name="max_steps",
        DisplayName="Max Steps",
        DisplayAsChip=True,
        Type=OptType.Number,
        Description="Global cap on model steps for this run; halts a runaway loop. 0 = unlimited.",
        Required=False,
        DefaultValue=200,
    ),
    ChatModelConfigurationOption(
        Name="system_prompt",
        DisplayName="System Prompt Override",
        Type=OptType.String,
        Description="Optional override for Sage's default operator-assistant system prompt.",
        Required=False,
        MinRows=4,
    ),
    ChatModelConfigurationOption(
        Name="API_ENDPOINT",
        DisplayName="API Endpoint",
        Type=OptType.String,
        Description="[OPTIONAL] The API endpoint to use for the selected provider.",
        Required=False,
        HelpText="Resolution order: this field → the API_ENDPOINT user secret → the container's API_ENDPOINT env var.",
    ),
    ChatModelConfigurationOption(
        Name="API_KEY",
        DisplayName="API Key",
        Type=OptType.String,
        Description="[OPTIONAL] The API key to use for the selected provider.",
        Required=False,
        HelpText=(
            "Stored in plaintext channel config. To keep it hidden, leave this blank and set the API_KEY "
            "user secret instead. Resolution order: this field → API_KEY user secret → container API_KEY env var."
        ),
    ),
    ChatModelConfigurationOption(
        Name="AWS_ACCESS_KEY_ID",
        DisplayName="AWS_ACCESS_KEY_ID",
        Type=OptType.String,
        Description="[OPTIONAL] The AWS Access Key ID (AWS_ACCESS_KEY_ID) to use for Bedrock.",
        Required=False,
    ),
    ChatModelConfigurationOption(
        Name="AWS_SECRET_ACCESS_KEY",
        DisplayName="AWS_SECRET_ACCESS_KEY",
        Type=OptType.String,
        Description="[OPTIONAL] The AWS Secret Access Key (AWS_SECRET_ACCESS_KEY) to use for Bedrock.",
        Required=False,
    ),
    ChatModelConfigurationOption(
        Name="AWS_SESSION_TOKEN",
        DisplayName="AWS_SESSION_TOKEN",
        Type=OptType.String,
        Description="[OPTIONAL] The AWS Session Token (AWS_SESSION_TOKEN) to use for Bedrock.",
        Required=False,
    ),
    ChatModelConfigurationOption(
        Name="AWS_DEFAULT_REGION",
        DisplayName="AWS_DEFAULT_REGION",
        Type=OptType.String,
        Description="[OPTIONAL] The AWS Region (AWS_DEFAULT_REGION) to use for Bedrock.",
        Required=False,
    ),
]

SAGE_MODELS = [
    ChatModelDefinition(
        Name="Sage",
        Description="Sage — AI red-team operator assistant fronting the LangGraph multi-agent runtime.",
        Metadata=ChatModelMetadata(
            Provider="litellm",
            ConfigurationOptions=_CONFIG_OPTIONS,
            # Nothing is a *required* secret: API_KEY/API_ENDPOINT/AWS are now first-class config options
            # (above), so requiring the secret too would force double-entry and fight the Config-first
            # resolution order. They stay declared as OPTIONAL secrets so the secret store remains a valid
            # fallback layer (Config → Secret → env → default) for operators who prefer to hide them.
            RequiredUserSecrets=[],
            OptionalUserSecrets=[
                "API_KEY",
                "API_ENDPOINT",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_DEFAULT_REGION",
            ],
            RequiredChannelAPITokenScopes=_CHANNEL_TOKEN_SCOPES,
            SlashCommands=SLASH_COMMANDS,
        ),
    ),
]
