# Tools and capabilities reference

> **This is a snapshot, not the source of truth.** The set of tools exposed to the agents is assembled in code —
> the per-agent `get_tools([...])` lists in `Payload_Type/sage/ai/langgraph/model.py`, with guard status from
> `GUARDED_TOOLS` in `mythic_tools.py` and the capability catalog in `capabilities.py`. When this page and the
> code disagree, the code wins. Regenerate or amend this page when the tool surface changes.

Sage exposes **32 Mythic tools** to its agents (22 read-only, 10 guarded/offensive) plus a **15-capability**
generic attack-planning layer. BloodHound graph tools are **not** counted here — they come from the external
[BloodHound MCP server](https://github.com/mwnickerson/bloodhound_mcp), not from Sage's own code.

## Read-only tools (22)

These never prompt for approval and cannot change target or Mythic state.

| Tool | What it does |
|---|---|
| `list_callbacks` | Slim per-callback status (one cheap query, minimal fields) |
| `get_all_payload_info` | Info about all payload *types* in Mythic |
| `get_all_payloads` | Info about all built payloads registered in Mythic |
| `get_payload_names` | List all payload type names |
| `get_c2_profiles_for_payload` | C2 profiles available for a payload type |
| `get_callback_c2_config` | Configured C2 parameter values for a live callback |
| `get_payload_c2_config` | Configured C2 parameter values for a built payload |
| `download_payload` | Download a built payload for reuse; returns a Mythic file reference |
| `get_all_command_names_for_payloadtype` | Command names for a payload type |
| `get_all_command_args_for_payloadtype` | One command's argument schema, including defaults |
| `get_all_commands_for_payloadtype` | All commands for a payload type |
| `wait_for_seconds` | Bounded Sage-side pause, no tasking |
| `get_task_history_for_callback` | A callback's full task history as JSON |
| `list_open_artifacts` | Uncleaned artifacts this run dropped (for OPSEC cleanup) |
| `get_all_task_output_by_task_id` | All output for a given Mythic task ID |
| `get_all_uploaded_files` | List all files uploaded to Mythic |
| `get_operations` | List all Mythic operations |
| `read_credentials` | Read the operation's Mythic credential store (can expose raw secrets) |
| `build_capability_commands` | Build deterministic Mythic parameters for a capability (does not execute) |
| `get_ttp_guidance` | Match a plain-language goal to Sage's TTP library; returns args, examples, guidance |
| `get_ttp_full_reference` | Full reference section for a TTP |
| `list_ttp_categories` | Sage's TTP library grouped by category |

## Guarded / offensive tools (10)

These are approval-gated in supervised mode and each requires a specific Mythic bot-token scope; a tool whose
scope the channel token lacks is disabled up front.

| Tool | What it does | Required scope |
|---|---|---|
| `issue_task_and_waitfor_task_output` | Issue a command on a callback and wait for output (the core tasking path) | `callback.write` |
| `execute_capability` | Execute one generic capability action, verify it, record only proven effects | `callback.write` |
| `upload_file_by_file_uuid` | Upload a Mythic-stored file to a callback via its upload command | `callback.write` |
| `ingest_collection` | Ingest a downloaded SharpHound/AzureHound collection into BloodHound | `callback.write` |
| `add_credential` | Add a credential to Mythic's credential store | `credential.write` |
| `create_payload` | Create a new Mythic payload/agent | `payload.write` |
| `delete_payload` | Soft-delete a junk payload after verifying it has no callbacks | `payload.write` |
| `download_tool` | Download a tool binary from its pinned, sha256-verified source into the drop zone | `file.write` |
| `ensure_tool_uploaded` | Ensure a tool binary is in Mythic's file store (uploads if missing) | `file.write` |
| `sandbox_exec` | Run untrusted code in an isolated ephemeral sandbox container | `callback.write` |

## Tool drop zone

`download_tool` and `ensure_tool_uploaded` operate on `Payload_Type/sage/tools/`, an operator drop zone. The
directory ships **empty** (only a `.keep`) — Sage bundles no binaries. An operator drops in tools such as Rubeus,
Certify, SharpHound, SharpGPOAbuse, SharpDPAPI, or Whisker; a binary placed there is staged into Mythic on
demand, and `download_tool` fetches a pinned, sha256-verified binary from its TTP source into the same folder.

## Generic capabilities (15)

The capability layer represents actions as typed tuples with **preconditions**, **effects**, and structured
**verifiers**. Candidates are derived at runtime from observed BloodHound + Mythic state, and each capability's
effect satisfies the next one's precondition — a dependency graph, not a fixed script. These are range-agnostic
by design: GOAD is a benchmark, not a source of hardcoded strategy.

| Capability | Precondition → effect |
|---|---|
| `collect-graph` | From an authorized foothold → collect and ingest directory-graph observations into BloodHound |
| `gpo-controlled-system-exec` | Write control of a GPO + a live foothold in its domain → verified SYSTEM execution |
| `grant-directory-rights` | Verified SYSTEM execution → grant directory replication (DCSync) rights |
| `dcsync-krbtgt` | Verified replication authority → retrieve krbtgt key material |
| `dcsync-account` | Verified replication authority → retrieve a specific account's credential material |
| `forge-golden-ticket` | Verified domain key material (krbtgt) → create and apply a Kerberos ticket (incl. cross-forest) |
| `ensure-kerberos-context` | A domain needing auth → establish/refresh a callback-scoped Kerberos context for it |
| `ensure-account-kerberos-context` | Account credential material → establish a callback-scoped Kerberos context for that account |
| `read-managed-local-admin-secret` | A graph-proven path → read a managed local-admin secret (e.g. LAPS-style) |
| `use-managed-local-admin-secret` | A managed local-admin secret → use it on its authorized host |
| `execute-as-local-admin` | Verified local-admin access → turn it into a live execution context |
| `endpoint-protection-adjustment` | Execution blocked by endpoint protection → apply a bounded, required EP change |
| `adcs-ca-private-key-export` | A verified administrative execution context → export a CA private key |
| `adcs-esc-certificate-enroll` | A graph-proven ADCS escalation path → enroll a certificate |
| `adcs-certificate-auth` | Verified certificate material → authenticate to obtain domain control |

Every capability has a structured verifier, and effects are recorded to the engagement ledger **only on proof** —
never on the model's assertion that an action succeeded.
