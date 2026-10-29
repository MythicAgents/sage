+++
title = "Tools Reference"
chapter = false
weight = 45
+++

Sage exposes **32 Mythic tools** to its agents: **22 read-only** and **10 guarded**. Guarded tools change target
or Mythic state and are approval-gated in supervised mode; read-only tools never prompt. This page lists every
tool. For the higher-level view, see [Tools and Capabilities](/agents/sage/tools-and-capabilities/).

{{% notice info %}}
Each guarded tool requires a specific Mythic bot-token scope (`task.write`, `payload.write`,
`credential.write`, `file.write`, or `file.read`). A tool whose scope your chat token lacks is disabled up front rather than
failing mid-run.

For every Sage tool to be available, give the channel token `chat-ai.write`, `apitoken.write`, `callback.read`,
`response.read`, `operation.read`, `operator.read`, `task.write`, `file.write`, `payload.write`,
`credential.write`, and `eventlog.write` (each `.write` also grants its `.read`). Mythic requires the first two
on any AI channel token. Sage's session token inherits the channel token's scopes, and Mythic rejects a query
the token has no read scope for.
{{% /notice %}}

## Read-only tools

### Recon and situational awareness

| Tool | What it does |
|---|---|
| `list_callbacks` | Slim per-callback status (host, identity, integrity, liveness) |
| `get_task_history_for_callback` | A callback's full task history as JSON |
| `get_all_task_output_by_task_id` | All output for a given Mythic task ID |
| `get_operations` | List all Mythic operations |
| `list_open_artifacts` | Uncleaned artifacts this run dropped, for OPSEC cleanup |
| `wait_for_seconds` | A bounded Sage-side pause; issues no tasking |

### Payloads and C2

| Tool | What it does |
|---|---|
| `get_payload_names` | List all payload type names |
| `get_all_payload_info` | Info about all payload *types* in Mythic |
| `get_all_payloads` | Info about all built payloads |
| `get_all_command_names_for_payloadtype` | Command names for a payload type |
| `get_all_command_args_for_payloadtype` | One command's argument schema, including defaults |
| `get_all_commands_for_payloadtype` | All commands for a payload type |
| `get_c2_profiles_for_payload` | C2 profiles available for a payload type |
| `get_callback_c2_config` | Configured C2 parameter values for a live callback |
| `get_payload_c2_config` | Configured C2 parameter values for a built payload |
| `download_payload` | Download a built payload for reuse; returns a Mythic file reference |

### Files, credentials, capability, and tradecraft

| Tool | What it does |
|---|---|
| `get_all_uploaded_files` | List all files uploaded to Mythic |
| `read_credentials` | Read the operation's Mythic credential store (can expose raw secrets) |
| `build_capability_commands` | Build deterministic Mythic parameters for a capability, without executing |
| `get_ttp_guidance` | Match a plain-language goal to Sage's TTP library; returns args, examples, guidance |
| `get_ttp_full_reference` | Full reference section for a TTP |
| `list_ttp_categories` | Sage's TTP library grouped by category |
| `list_staged_tools` | Binaries in the `tools/` drop zone, or every variant of one tool: type, architecture, matching TTPs, and Mythic presence |

## Guarded tools

| Tool | What it does | Scope |
|---|---|---|
| `issue_task_and_waitfor_task_output` | Issue a command on a callback and wait for output — the core tasking path | `task.write` |
| `execute_capability` | Execute one generic capability action, verify it, and record only proven effects | `task.write` |
| `upload_file_by_file_uuid` | Upload a Mythic-stored file to a callback | `task.write` |
| `ingest_collection` | Ingest a downloaded SharpHound/AzureHound collection into BloodHound | `file.read` (granted by `file.write`) |
| `sandbox_exec` | Run untrusted code in an isolated, ephemeral sandbox container | None (local only) |
| `add_credential` | Add a credential to Mythic's credential store | `credential.write` |
| `create_payload` | Create a new Mythic payload | `payload.write` |
| `delete_payload` | Soft-delete a junk payload after verifying it has no callbacks | `payload.write` |
| `ensure_tool_uploaded` | Ensure a tool binary is in Mythic's file store, uploading it from the drop zone if missing | `file.write` |
| `download_tool` | Download a pinned, sha256-verified tool binary from its TTP source into the drop zone | None (local only; `ensure_tool_uploaded` needs `file.write`) |
