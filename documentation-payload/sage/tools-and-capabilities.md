+++
title = "Tools and Capabilities"
chapter = false
weight = 40
+++

Sage exposes **32 Mythic tools** to its agents — **22 read-only** (enumerate callbacks, payloads, tasks, files,
and credentials; retrieve tradecraft guidance) and **10 guarded/offensive** (issue a command, execute a
capability, create a payload, add a credential, ingest a collection, run in the sandbox). The read-only/guarded
split is the safety story: only the guarded ten are approval-gated in supervised mode, and only they can change
target or Mythic state. For a per-tool breakdown, see the [Tools Reference](/agents/sage/tools-reference/).

| Area | Example tools | |
|---|---|---|
| Observe | `list_callbacks`, `get_task_history_for_callback`, `get_all_uploaded_files` | read-only |
| Tasking | `issue_task_and_waitfor_task_output` | guarded |
| Capabilities | `execute_capability`, `build_capability_commands` | guarded / read-only |
| Payloads | `create_payload`, `download_payload` | guarded / read-only |
| BloodHound ingest | `ingest_collection` | guarded |
| Credentials | `read_credentials`, `add_credential` | read-only / guarded |
| Tradecraft | `get_ttp_guidance`, `list_ttp_categories` | read-only |
| Sandbox | `sandbox_exec` | guarded |

## The capability chain

On top of the tools sits a **15-step generic capability layer**: typed actions with preconditions, effects, and
structured verifiers that the planner chains from observed state. It models a full attack chain generically — for
example: collect graph → GPO-controlled SYSTEM execution → grant directory (DCSync) rights → DCSync krbtgt →
forge a golden ticket → ADCS abuse. **Each step unlocks only from verified proof of the previous one**; nothing
is recorded on assertion alone. These capabilities are range-agnostic by design — there is no GOAD path baked in.

Capability mechanics bind to the **live payload's advertised command schema** at execution time, so the same
generic capability drives Apollo, Merlin, Poseidon, or any Mythic agent. Sage is not tied to one payload.

## Tradecraft references

Tradecraft is backed by a curated corpus of **237 tradecraft references** that the agents retrieve against
through `get_ttp_guidance`, covering the AD attack surface: recon/graph, ADCS/ESC, Kerberos, credential access,
coercion/relay, delegation, lateral movement, privesc, evasion, and persistence.

## Tool drop zone

`Payload_Type/sage/tools/` is an operator drop zone for tool binaries. It ships **empty** — Sage bundles no
offensive binaries. Drop a binary in (for example `Rubeus.exe`, `Certify.exe`, `SharpHound.exe`,
`SharpGPOAbuse.exe`, `SharpDPAPI.exe`, or `Whisker.exe`) and Sage discovers it and stages it into Mythic on
demand instead of making you upload it by hand. The `download_tool` capability can also fetch a pinned,
sha256-verified binary from its TTP source straight into this folder.
