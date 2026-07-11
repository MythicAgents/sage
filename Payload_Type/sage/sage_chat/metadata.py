"""Live channel-metadata chips for the Mythic chat header (Phase 2).

Static config chips (Provider / Model / Max Steps) are declared on the config options via
``DisplayAsChip=True`` (see ``models.py``). This module builds the DYNAMIC set — controls and counts that
change during a session — which the chat handler publishes each turn via ``turn.update_channel_metadata``.

Mythic stores the payload under the channel's ``ai_metadata.channel_metadata``; operators choose which
chips to show and how to color them with a compact display string in the channel edit dialog (see
``CHAT_CONTAINERS.md`` → "Channel Display Metadata").
"""

from __future__ import annotations

from typing import Any


# Tools declared on a tasking agent's frontmatter that are NOT Mythic C2/tasking actions — control/handoff
# tools and the TTP-library lookups. Excluded from the Mythic-tools count. (`transfer_*` handled by prefix.)
_NON_MYTHIC_TOOLS = frozenset({
    "summarize_and_handback", "handback_to_supervisor", "request_continuation", "respond_to_user",
    "get_ttp_guidance", "get_ttp_full_reference", "list_ttp_categories",
})
# The agents that actually task Mythic (BloodHound/MCP_Manager use MCP tools; Generalist/Supervisor none).
_MYTHIC_TASKING_AGENTS = ("mythic_operator", "mythic_payload")


def _mythic_tool_universe() -> set[str]:
    """Distinct Mythic C2/tasking tools declared across the tasking agents' prompt frontmatter,
    excluding control/handoff and TTP-library tools (which aren't Mythic tasking actions)."""
    try:
        from ai.langgraph.prompt_loader import get_prompt_tools
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.prompt_loader import get_prompt_tools  # type: ignore
    universe: set[str] = set()
    for agent in _MYTHIC_TASKING_AGENTS:
        try:
            for t in get_prompt_tools(agent):
                if t in _NON_MYTHIC_TOOLS or t.startswith("transfer_"):
                    continue
                universe.add(t)
        except Exception:
            continue
    return universe


def scope_usable_mythic_tools(model: Any) -> int:
    """Count of Mythic tools the CURRENT bot token can actually invoke (Phase 2, option b): the declared
    Mythic tool universe minus the scope-gated (`disabled_tools`) set on the live model's client. The
    universe is static; `disabled_tools` is per-session (set by the token's scope preflight), so this
    number reflects the operator's real reach. No client yet → the full universe (nothing gated)."""
    universe = _mythic_tool_universe()
    disabled = getattr(getattr(model, "mythic_client", None), "disabled_tools", None) or set()
    try:
        return len(universe - set(disabled))
    except Exception:
        return len(universe)


def _runtime_rounds(model: Any) -> int:
    """Accumulate model calls across graph-agent and controller-policy execution paths.

    ``_global_step_count`` is cumulative but only covers calls routed through create_agent middleware.
    ``_policy_model_calls`` covers direct controller policy calls and resets at the start of each controller
    solve. Track deltas from both sources so the header remains monotonic across mixed chat turns.
    """
    graph_calls = int(getattr(model, "_global_step_count", 0) or 0)
    policy_calls = int(getattr(model, "_policy_model_calls", 0) or 0)
    if not hasattr(model, "_metadata_rounds_total"):
        try:
            model._metadata_rounds_total = graph_calls + policy_calls
            model._metadata_graph_calls_seen = graph_calls
            model._metadata_policy_calls_seen = policy_calls
            return int(model._metadata_rounds_total)
        except (AttributeError, TypeError):
            return graph_calls + policy_calls

    graph_seen = int(getattr(model, "_metadata_graph_calls_seen", 0) or 0)
    policy_seen = int(getattr(model, "_metadata_policy_calls_seen", 0) or 0)
    graph_delta = max(0, graph_calls - graph_seen)
    policy_delta = policy_calls - policy_seen if policy_calls >= policy_seen else policy_calls
    try:
        model._metadata_rounds_total = (
            int(getattr(model, "_metadata_rounds_total", 0) or 0) + graph_delta + policy_delta
        )
        model._metadata_graph_calls_seen = graph_calls
        model._metadata_policy_calls_seen = policy_calls
        return int(model._metadata_rounds_total)
    except (AttributeError, TypeError):
        return graph_calls + policy_calls


def build_channel_metadata(model: Any) -> dict[str, Any]:
    """Build the ``{"items": [...]}`` channel-metadata payload from the running model + MCP singleton.

    Every field is best-effort and self-guarding: a live header must never break a chat turn, so any
    lookup failure degrades that chip to 0/off rather than raising.
    """
    try:
        from ai.mcp import MCPManager
    except ImportError:  # pragma: no cover
        from ..ai.mcp import MCPManager  # type: ignore

    try:
        summary = MCPManager.get_tools_summary() or {}
    except Exception:
        summary = {}
    mcp_tools = int(summary.get("total_tools", 0) or 0)
    mcp_servers = int(summary.get("connected_servers", 0) or 0)

    rounds = _runtime_rounds(model)

    try:
        bh_connected = any("bloodhound" in s.lower() for s in MCPManager.get_connected_servers())
    except Exception:
        bh_connected = False

    # Config-value chips (Policy / Mode / Autonomous) — rendered as metadata chips (NOT via DisplayAsChip)
    # so they can carry a color: Mythic locks config chips to neutral, but metadata chips honor `color`.
    # Accent them so they stand out from the neutral config chips (Provider / Model / Max Steps) + counts.
    mode = str(getattr(model, "mode", "") or "supervised")
    autonomous = bool(getattr(model, "_autonomous_solve", False))
    policy_mode = str(getattr(model, "policy_mode", "") or "llm")
    policy_color = "success" if policy_mode == "llm" else "warning"
    mode_color = "info" if mode == "supervised" else "warning"
    autonomous_color = "warning" if autonomous else "neutral"

    items = [
        {"key": "cfg_policy", "label": "Policy", "value": policy_mode, "color": policy_color, "order": 1,
         "tooltip": "Semantic capability selection backend"},
        {"key": "cfg_mode", "label": "Mode", "value": mode, "color": mode_color, "order": 2,
         "click": "/mode", "click_confirmation_text": "Run /mode to show or change Sage's mode?",
         "tooltip": "Supervised or autonomous — click to run /mode"},
        {"key": "cfg_autonomous", "label": "Autonomous", "value": autonomous,
         "display_value": "on" if autonomous else "off", "color": autonomous_color, "order": 3,
         "tooltip": "Autonomous solve forced this session"},
        {"key": "mythic_tools", "label": "Mythic Tools", "value": scope_usable_mythic_tools(model),
         "order": 4, "tooltip": "Mythic tools the current bot token can invoke (scope-gated)"},
        {"key": "mcp_servers", "label": "MCP Servers", "value": mcp_servers, "order": 10,
         "click": "/mcp", "click_confirmation_text": "Run /mcp to list connected MCP servers and their tools?",
         "tooltip": "Connected MCP servers — click to run /mcp"},
        {"key": "mcp_tools", "label": "MCP Tools", "value": mcp_tools, "order": 20,
         "tooltip": "Tools across connected MCP servers"},
        {"key": "rounds", "label": "Rounds", "value": rounds, "order": 30,
         "tooltip": "Model calls observed across graph-agent and controller execution"},
        {"key": "bloodhound", "label": "BloodHound", "value": bh_connected,
         "display_value": "connected" if bh_connected else "off",
         "status": "success" if bh_connected else "neutral", "order": 40,
         "click": "/bloodhound", "click_confirmation_text": "Run /bloodhound to (re)connect or show BloodHound status?",
         "tooltip": "BloodHound MCP connection — click to run /bloodhound"},
    ]
    return {"items": items}
