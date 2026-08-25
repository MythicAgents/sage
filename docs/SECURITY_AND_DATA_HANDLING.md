# Security and Data Handling

Sage runs inside the trusted Mythic/Sage control plane. It does not create a secret-free enclave within that
deployment: **all Mythic-derived content is client data**, including identifiers and metadata, task parameters
and output, credentials and secret values, file content, binaries, dumps, findings, and native notifications.
Operators must deploy Mythic, Sage, LiteLLM, model providers, BloodHound, and observability storage under controls
appropriate for that data.

Sage must never connect directly to target LDAP, SMB, Kerberos, WinRM, RPC, HTTP, or similar services. Target
activity is issued as a Mythic payload task. Valid proof comes from Mythic task output/artifacts, Mythic
credential-store state, or BloodHound facts derived from payload-collected artifacts.

## Data-flow and retention matrix

| Surface | Client data that can reach it | Persistence and retention | Access and erasure responsibility |
|---|---|---|---|
| Mythic source records | Callback metadata, task parameters/output, credentials, files, binaries, dumps, chat messages, and operation identity | Mythic is authoritative and retains data under its own database/file policies | Restrict Mythic access by operation and role. Sage operation-memory wipe never changes Mythic records; erase them with Mythic-owned procedures when authorized. |
| BloodHound CE and MCP | Payload-collected graph facts, names, domains, hosts, users, groups, edges, collection files, and query results | BloodHound CE retains its graph; the MCP subprocess may hold request/result values in memory and logs | Protect CE tokens and database access. BloodHound erasure is separate from Sage, Mythic, and operation-memory erasure. |
| Sage process and model context | Any selected Mythic/BloodHound value, including raw credentials, task output, file text, and explicitly selected binaries or dumps supported by the model route | In memory for request/session lifetime; content can also flow to checkpoints and traces below | Run Sage inside the trusted deployment, restrict container/process access, and stop/rotate sessions when access is revoked. Do not assume prompt text is redacted. |
| OpenAI-compatible LiteLLM route | Prompts, messages, tool inputs/results, credentials, file content, and model-supported binary/dump inputs selected for a call | Determined by the operator's LiteLLM configuration, logging, caches, and upstream route | The operator must select and configure a LiteLLM/upstream route compatible with client rules. Sage does not verify provider residency, training, or retention policy. |
| Upstream model provider | The content LiteLLM forwards, potentially including every client-data class above | Determined by the upstream provider and contract | Provider compliance is operator-owned. Disable provider logging/retention where required and verify the effective upstream route; do not infer it from the model name. |
| LangGraph checkpoint database (`sage.db`) | Conversation messages, model/tool content, pending approvals, request/session state, and values required to resume | SQLite state persists across Sage restarts | Restrict filesystem access. Operation-memory wipe does not erase checkpoints; checkpoint lifecycle and erasure are separate operator actions. Archive before reset according to repository procedures. |
| Phoenix trace database (`.phoenix/phoenix.db`) | Prompts, model inputs/outputs, tool calls/results, attributes, errors, and token metadata; values may include credentials and other client data | SQLite plus WAL persists across restarts until archived or removed under operator policy | Restrict Phoenix UI/filesystem access. Read WAL-inclusively. Operation-memory wipe does not erase traces; archive or erase them separately when authorized. |
| Operation memory (`sage_operation_memory.db`) | Operation/callback/task/output/file identifiers and metadata, content hashes, bounded inline text/JSON, watermarks, degradation records, findings, evidence pointers, notification ledger/outbox, and the Watcher owner/generation/provider/model/source labels/cadence/pause/lifecycle record | Operation-scoped SQLite persists across Sage restarts. It stores no Watcher endpoint, provider credential, Mythic token, or user-secret value. Automatically inlined source text is capped at 65,536 bytes per record | `OperationMemoryStore.wipe_operation(operation_id)` cascades deletion of Sage-derived rows for exactly one operation and leaves other operations and Mythic source records unchanged. The beta exposes this store operation but no native chat wipe command; operators must use an authorized maintenance path. |
| Background findings model call | Already-admitted typed candidate identity, type, title, state, confidence ceiling, time, exact evidence pointers, and missing assumptions | Provider/LiteLLM retention applies. The direct reasoner has no tools and its output is admitted only through the existing deterministic finding boundary | Configure the `SAGE_WATCHER_*` route under the same client-data controls as other Sage model traffic. A failed or malformed response leaves the prior view intact and marks watcher health degraded. |
| Watcher explanation model call | A bounded operator request plus the admitted canonical finding view | Provider/LiteLLM retention applies. A separate stateless one-node graph has no tools or checkpointer and returns explanation/citation schema only; it cannot task, validate, approve, or mutate Watcher state | Use the locked `Sage Watcher` owner channel. `/stop` cancels only that channel's active explanation; scheduler pause/resume is a separate typed control. |
| Native Mythic findings view and notifications | Full finding titles, states, evidence pointers, rationale, missing assumptions, suggested validation, operation identity, and any client-derived values represented there | Bot-authored updates persist in the standard `#sage-findings` channel under Mythic policy. Generic event-feed alerts persist separately; dedupe/retry state is retained in operation memory | This is the authoritative human review surface. Apply Mythic access control and retention; Slack is not a substitute for it. Verify the persistent watcher bot with `/watcher status`. |
| Application logs | Operational metadata, diagnostics, tool/model errors, and any values explicitly logged by a component | Container/log-driver retention is deployment-specific | Never log `.env` contents, process environments, RabbitMQ credentials, model API keys, webhook URLs, or raw credential material merely to prove readiness. Restrict and rotate logs. |
| Optional Slack findings hook | **Only** `Sage findings changed. Open Mythic to review.` plus an optional operator-configured legacy C/G channel ID. No operation name, finding content, host, user, domain, credential, task output, file content, or other client-derived value is accepted by the egress function | Slack retains the generic message and routing metadata under workspace policy; Sage retains no Slack response body | Configure `SAGE_FINDINGS_SLACK_WEBHOOK_URL` only for an approved workspace. Modern Slack app webhooks are bound to their installed channel, so use one URL per destination. `SAGE_FINDINGS_SLACK_CHANNEL_ID` is only for a legacy custom-integration webhook. Protect webhook URLs as secrets and apply Slack retention separately. Delivery failure is fail-soft and logs no URL or exception content. |

## Background watcher execution

A watcher scan is one operation-scoped, read-only poll of Mythic's callback, task, response/task-output,
credential, and file streams. Scanning never creates a payload task or contacts a target. Each poll attempt
increments the displayed `scans` counter; model inference is separate and occurs only when durable evidence has
changed. The resulting tool-free model call is bounded, and `/watcher status` reports its timestamp separately
as `last model reasoning`.

The operator creates and locks a `Sage Watcher` AI channel, then runs `/watcher apply` there. Creating or editing a
channel is inert until apply. The route uses only the eight `SAGE_WATCHER_*` provider/model/endpoint/credential
keys with Sage Chat's first-non-empty algorithm; no unprefixed fallback exists. User-secret values are hydrated
only in the active process. After restart a generation that depended on one becomes `credentials-required` until
the owner reapplies it; UI/environment-backed profiles may auto-resume after exact binding checks.

Cadence defaults to 300 seconds, accepts 5 through 86,400, and persists with pause state in the applied profile.
Only the exact active locked owner may apply, scan, pause, resume, or change cadence. `/watcher status` never creates
a loop. The onStart token may read the channel profile and bootstrap `#sage-findings`, then is discarded. The
persistent background bot must present the exact frozen read/delivery scopes; wildcard and excess scopes fail
closed.

## Resource boundaries

Operation memory exposes five commented runtime settings in `Payload_Type/sage/.env`:

| Setting | Default | Exhaustion behavior |
|---|---:|---|
| `SAGE_OPERATION_MEMORY_MAX_MODEL_INPUT_TOKENS` | 100,000 | Marks analysis degraded and leaves deferred/rescan work visible. |
| `SAGE_OPERATION_MEMORY_MAX_INLINE_TEXT_BYTES` | 65,536 | Does not inline the oversized record; Mythic remains authoritative and an explicit selection/rescan is required. |
| `SAGE_OPERATION_MEMORY_MAX_MODEL_CALLS_PER_UPDATE` | 5 | Marks analysis degraded and preserves queued/deferred work. |
| `SAGE_OPERATION_MEMORY_BACKFILL_BATCH_SIZE` | 500 | Continues through bounded pages and reports remaining work. |
| `SAGE_OPERATION_MEMORY_MAX_QUEUED_UPDATES` | 100 | Rejects silent overflow, marks degradation, and requires rescan/deferred processing. |

These are resource ceilings, not authority controls. They do not authorize a callback task or direct target
connection. An explicitly selected full binary or dump may still reach a provider-supported model/file tool; the
inline-text cap does not certify that content as safe.

## Operation offboarding checklist

1. Stop Sage before manipulating runtime stores and archive active databases when the repository workflow
   requires preservation.
2. Wipe the exact operation from operation memory through an authorized maintenance path and verify other
   operation snapshots are unchanged.
3. Apply separate retention or erasure actions to Mythic records/files/chat, `sage.db`, Phoenix and its WAL,
   BloodHound, LiteLLM, the upstream provider, logs, backups, and Slack as required by the client agreement.
4. Revoke Mythic, BloodHound, provider, and Slack credentials and verify the effective runtime configuration.

No single Sage command erases every copy across these independently owned systems.
