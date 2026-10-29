# Tools and capabilities reference

> **This is a snapshot, not the source of truth.** The set of tools exposed to the agents is assembled in code —
> the per-agent `get_tools([...])` lists in `Payload_Type/sage/ai/langgraph/model.py`, with guard status from
> `GUARDED_TOOLS` in `mythic_tools.py` and the capability catalog in `capabilities.py`. When this page and the
> code disagree, the code wins. Regenerate or amend this page when the tool surface changes.

This reference describes Sage's read-only and guarded tools, including the separate sandbox tool, plus its
**15-capability** generic attack-planning layer. BloodHound graph tools come from the external
[BloodHound MCP server](https://github.com/mwnickerson/bloodhound_mcp), not from Sage's own code.

Use `/tools` or ask “list your tools” in ordinary Sage chat for the current shipped Mythic tool inventory,
including descriptions and known session access restrictions. This is a deterministic response; it does
not task a callback. `/mcp tools` lists connected MCP tools separately. Callback payload commands are
discovered through Mythic's command metadata and are distinct from Sage's tool functions.

## Read-only tools

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

`get_ttp_guidance` labels library requirements, examples, and agent instructions as reference material.
Its `reference_scope` metadata does not attest that a callback meets those requirements. A resolved payload
type identifies an agent; an MCP connection status describes connectivity, not graph contents. Establish
target applicability from separate observations and keep unobserved prerequisites unknown.

## Guarded / offensive tools

These are approval-gated in supervised mode and each requires a specific Mythic bot-token scope; a tool whose
scope the channel token lacks is disabled up front.

| Tool | What it does | Required scope |
|---|---|---|
| `issue_task_and_waitfor_task_output` | Issue a command on a callback and wait for output (the core tasking path) | `task.write` |
| `execute_capability` | Execute one generic capability action, verify it, record only proven effects | `task.write` |
| `upload_file_by_file_uuid` | Upload a Mythic-stored file to a callback via its upload command | `task.write` |
| `ingest_collection` | Ingest a downloaded SharpHound/AzureHound collection into BloodHound | `file.read` (granted by `file.write`) |
| `add_credential` | Add a credential to Mythic's credential store | `credential.write` |
| `create_payload` | Create a new Mythic payload/agent | `payload.write` |
| `delete_payload` | Soft-delete a junk payload after verifying it has no callbacks | `payload.write` |
| `download_tool` | Download a tool binary from its pinned, sha256-verified source into the drop zone | None (local only; `ensure_tool_uploaded` needs `file.write`) |
| `ensure_tool_uploaded` | Ensure a tool binary is in Mythic's file store (uploads if missing) | `file.write` |
| `sandbox_exec` | Run untrusted code in an isolated ephemeral sandbox container | None (local only) |

For a direct `issue_task_and_waitfor_task_output` proposal in native supervised chat, Sage resolves
deterministic command aliases, parameter names, defaults, and supported context before it displays the
approval card. The displayed command and arguments are the action held for checkpoint resume. At issue
time, Sage rechecks the command schema, declared parameter group, and any selected registered file's
Mythic identity; a changed binding stops the task and needs a fresh proposal and approval. A selected
`token_id` is currently refused before the card because this task transport does not forward it to
Mythic. File registration or upload is a separate guarded action; accepting a task card does not
authorize a hidden upload.

## Tool drop zone

`download_tool` and `ensure_tool_uploaded` operate on `Payload_Type/sage/tools/`, an operator drop zone. The
directory ships **empty** (only a `.keep`) — Sage bundles no binaries. An operator drops in tools such as Rubeus,
Certify, SharpHound, SharpGPOAbuse, SharpDPAPI, or Whisker; a binary placed there is staged into Mythic on
demand, and `download_tool` fetches a pinned, sha256-verified binary from its TTP source into the same folder.

## Payload adapters and command discovery

Sage queries Mythic for commands and argument schemas, including parameter names, CLI aliases, groups and
defaults. Automated capabilities also need a semantic mapping: which commands implement an operation, which
authentication context they use, and how their output proves the intended effect. These mappings live in
`Payload_Type/sage/ai/langgraph/mythic_capability_adapter.py`.

The automated graph-collection controller currently selects only Apollo and Merlin profiles:

| Payload type | Collection profile |
|---|---|
| Apollo | Uses the shared collection defaults: `execute_assembly` with `assembly_name` and `assembly_arguments`, plus `whoami` and `ticket_cache_list` for authentication checks. |
| Merlin | Overrides the defaults: `execute-assembly` with `filename` and `arguments`, and `token` with `method: whoami` for identity. It does not use Apollo's ticket-cache command and has payload-specific transport and file-parameter mappings. |
| Other payload types | No collection profile is selected. Command discovery may still be available, but the controller excludes these footholds from automated collection. |

An adapter defines known execution semantics; the live schema supplies current argument syntax and validation.
Adding another payload requires a suitable collection profile and verification of its assembled execution path.
An existing profile does not imply support for every capability or live qualification. The beta's validated
payload configuration is Apollo; see the [README limitations](../../README.md#limitations).

Mythic parameters can have a native `name` and a different `cli_name`. For example, Apollo declares
`assembly_name`/`assembly_arguments` with CLI aliases `Assembly`/`Arguments`. Sage's resolver translates
these using the advertised schema. In an approved collection, Sage binds a lossless declared alias rename
to the exact pending step and retains both representations. Changed values, added defaults, dropped fields
and ambiguous aliases remain blocked by this boundary. Runner, listing and download steps require matching
Mythic task receipts and successful completion evidence before collection advances. An issuance refusal or
pending task prevents automatic reproposal of the collection approval.

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
