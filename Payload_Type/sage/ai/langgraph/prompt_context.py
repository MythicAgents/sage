"""
prompt_context — runtime values injected into agent prompts (ROADMAP Phase 3, Layer 1).

This is the ONE place that defines every variable a prompt file may interpolate and how it is
computed from the live Model. Each provider takes the Model instance and returns the string that
fills the matching ``{placeholder}`` in ``prompts/<agent>.md`` (see ``prompts/README.md`` for the
per-agent variable table). model.py calls these instead of building the strings inline, so adding
or understanding a prompt variable happens here, not scattered across the agent builders.

To expose a NEW variable: add a provider function here, register it in ``PROVIDERS``, reference
its ``{name}`` in the prompt body, and list it under that file's ``variables:`` frontmatter.

DEFERRED (Layer 2, post-demo): a pluggable system that auto-discovers operator-supplied provider
functions from a drop-in location. That is arbitrary code execution inside the Sage container and
needs a deliberate security model + context contract — see the
Phase-3 ISA. For now the provider set is fixed and code-reviewed.
"""

try:  # match model.py's logger so warnings surface in Sage logs
    from mythic_container.logging import logger
except Exception:  # pragma: no cover - fallback for standalone/unit-test contexts
    import logging
    logger = logging.getLogger(__name__)

from ai.mcp import MCPManager


def _one_line_description(description) -> str:
    if description is None:
        return ""
    text = " ".join(str(description).replace("\r", " ").replace("\n", " ").split())
    if len(text) > 80:
        text = text[:77].rstrip() + "..."
    return text


def _command_index_line(entry, description=None) -> str:
    command = None
    if isinstance(entry, dict):
        command = entry.get("cmd") or entry.get("command") or entry.get("name")
        description = entry.get("description", description)
    else:
        command = entry

    if command is None:
        command = entry

    command_text = str(command)
    description_text = _one_line_description(description)
    if description_text:
        return f"- {command_text}: {description_text}"
    return f"- {command_text}"


def _command_entries(commands):
    if isinstance(commands, dict):
        for key in ("commands", "command"):
            value = commands.get(key)
            if isinstance(value, list):
                return [(entry, None) for entry in value]
        return [
            (name, detail.get("description", "") if isinstance(detail, dict) else "")
            for name, detail in commands.items()
        ]
    if isinstance(commands, list):
        return [(entry, None) for entry in commands]
    return [(commands, None)]


def commands_text(model) -> str:
    """``{commands_text}`` (Mythic_Operator): compact command index per pre-loaded payload."""
    text = ""
    if model._cached_commands:
        for payload_name, commands in model._cached_commands.items():
            lines = []
            for entry, description in _command_entries(commands):
                lines.append(_command_index_line(entry, description))
            commands_index = "\n".join(lines)
            text += f"\n### Available Commands for '{payload_name}' Payload (index — names + summaries):\n{commands_index}\n"
        text += (
            "\n**Note:** This is an index only. Before issuing any command that takes parameters, call "
            "get_all_command_args_for_payloadtype('<payload>', '<command>') for that ONE command's exact "
            "schema — its `default_value`, `required`, `choices`, and `parameter_group_name` are "
            "authoritative. Never issue a command with empty parameters: send the resolved JSON object "
            "(`{}` at minimum), never an empty string — agents parse the parameter blob and an empty "
            "string is a parse error, whereas `{}` lets the agent apply its own declared defaults. Use "
            "get_all_command_names_for_payloadtype('<payload>') to discover commands for payloads not "
            "listed here, and get_all_commands_for_payloadtype('<payload>') only when you genuinely need "
            "every command's schema at once.\n"
        )
    return text


def installed_payloads_text(model) -> str:
    """``{installed_payloads_text}`` (Mythic_Payload): installed payload types (agents)."""
    if model._payload_names:
        return "\n".join([f"        - {payload}" for payload in model._payload_names])
    return "        - (No payload data available)"


def installed_c2_profiles_text(model) -> str:
    """``{installed_c2_profiles_text}`` (Mythic_Payload): installed C2 profiles with descriptions."""
    if model._c2_profiles:
        return "\n".join([f"        - {profile['name']}: {profile['description']}" for profile in model._c2_profiles])
    return "        - (No C2 profile data available)"


def servers_text(model) -> str:
    """``{servers_text}`` (MCP_Manager): connected MCP servers + a preview of their tools."""
    connected = MCPManager.get_connected_servers()
    if connected:
        summary = MCPManager.get_tools_summary()
        text = f"\n**Currently Connected MCP Servers:** {len(connected)}\n"
        for server_name in connected:
            server_info = summary.get("server_summaries", {}).get(server_name, {})
            tool_count = server_info.get("tool_count", 0)
            tool_names = server_info.get("tool_names", [])
            tools_preview = ', '.join(tool_names[:5])
            if len(tool_names) > 5:
                tools_preview += '...'
            text += f"- {server_name}: {tool_count} tools ({tools_preview})\n"
        return text
    return "\n**No MCP servers currently connected.** Inform the user to use `mcp-connect` command first.\n"


# Registry — the single discoverable list of every prompt variable and its provider.
# (Also the hook for the deferred Layer-2 auto-discovery system.)
PROVIDERS = {
    "commands_text": commands_text,
    "installed_payloads_text": installed_payloads_text,
    "installed_c2_profiles_text": installed_c2_profiles_text,
    "servers_text": servers_text,
}


def resolve(model, *names) -> dict:
    """Resolve named variables into a kwargs dict for ``load_prompt(...)``.

    Unknown names are skipped with a warning; a provider that raises yields ``""`` so a single
    bad provider can never block an agent build. model.py currently calls the providers directly
    for clarity, but this helper is the frontmatter-driven path for Layer 2.
    """
    out = {}
    for name in names:
        provider = PROVIDERS.get(name)
        if provider is None:
            logger.warning(f"prompt_context: no provider registered for variable '{name}'")
            continue
        try:
            out[name] = provider(model)
        except Exception as e:
            logger.error(f"prompt_context: provider '{name}' raised ({e}); using empty string")
            out[name] = ""
    return out
