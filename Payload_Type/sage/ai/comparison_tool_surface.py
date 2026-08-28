"""Neutral tool-surface identities shared by shipping Sage and evaluation baselines.

This module contains names only.  It does not construct tools, grant authority, or import the
shipping harness.  Keeping the names here lets the true-naive comparison prove that it exposes no
capability which the shipping Sage agents do not also receive.
"""

SHIPPING_MYTHIC_OPERATOR_TOOL_NAMES = (
    "list_callbacks",
    "get_all_command_args_for_payloadtype",
    "get_all_command_names_for_payloadtype",
    "get_all_commands_for_payloadtype",
    "wait_for_seconds",
    "issue_task_and_waitfor_task_output",
    "get_task_history_for_callback",
    "get_all_task_output_by_task_id",
    "upload_file_by_file_uuid",
    "get_all_uploaded_files",
    "get_operations",
    "read_credentials",
    "add_credential",
    "execute_capability",
    "build_capability_commands",
    "get_ttp_guidance",
    "get_ttp_full_reference",
    "list_ttp_categories",
    "ensure_tool_uploaded",
    "download_tool",
    "ingest_collection",
    "list_open_artifacts",
)

SHIPPING_MYTHIC_PAYLOAD_TOOL_NAMES = (
    "get_payload_names",
    "create_payload",
    "get_all_payload_info",
    "get_all_payloads",
    "get_all_command_args_for_payloadtype",
    "get_all_command_names_for_payloadtype",
    "get_all_commands_for_payloadtype",
    "get_c2_profiles_for_payload",
    "get_callback_c2_config",
    "get_payload_c2_config",
    "download_payload",
    "delete_payload",
)

SHIPPING_MYTHIC_TOOL_NAMES = frozenset(
    SHIPPING_MYTHIC_OPERATOR_TOOL_NAMES + SHIPPING_MYTHIC_PAYLOAD_TOOL_NAMES
)

# This is the exact LLM-visible BloodHound MCP surface.  Raw Cypher, file upload, and composite
# management tools remain deterministic/harness-owned facilities rather than extra naive tools.
SHIPPING_BLOODHOUND_TOOL_NAMES = frozenset({
    "domain_info",
    "user_info",
    "group_info",
    "computer_info",
    "ou_info",
    "gpo_info",
    "graph_analysis",
    "adcs_info",
    "data_quality",
})
