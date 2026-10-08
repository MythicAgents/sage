# Versioned executable arguments

Sage can validate an enrolled executable selected through a supported Mythic artifact
wrapper. Validation happens before the supervised approval card and again before task
creation. The card contains the final native parameters. A generic call through the same
wrapper receives the same checks as a typed call.

The initial compatible transports are Apollo `execute_assembly` and `inline_assembly`.
Other wrappers require their own parser/transport verification.

This is an argument-construction guarantee. It does not prove that a target exists, that
the operator has access, or that an operation succeeded. Arbitrary shell programs, scripts,
BOFs and unknown tool interfaces remain outside executable-contract validation.

## Discover one operation

The existing `get_all_command_args_for_payloadtype` tool accepts optional
`callback_display_id`, `executable_filename`, and `executable_verb` fields together.
For example, discover the enrolled `enum-cas` operation for an already registered
`Certify.exe` on an Apollo callback. Sage checks the callback, available command, live
parameter group, registered file identity and downloaded artifact SHA-256.

Successful discovery exposes a typed argument object on the existing
`issue_task_and_waitfor_task_output` tool. Only the selected verb's fields are included.
The executable argument field contains `{ "verb": "enum-cas", "options": { ... } }`;
the surrounding selector and argument field names come from the live Mythic schema.
Sage renders this object to the native argument string before requesting approval.

The selection is temporary and clears when the graph rebuilds for a turn. Ordinary
command discovery clears it as well. No new execution tool, slash command, agent or
approval route is introduced. Selecting a contract does not authorize a task.

## Identity and validation

Descriptors live in `Payload_Type/sage/tool_contracts/`. The initial pilot is bound to
the exact Certify hash documented in `certify.json`, with four enrolled verbs. Names
alone do not establish identity: changed bytes under a recognized name are refused,
and renaming enrolled bytes does not remove validation through a supported wrapper.
Unavailable or ambiguous identities cannot establish a validated invocation.

Local validation checks enrolled verbs, declared required fields, primitive types,
string sequences, option sets, explicit dependencies and enums where declared. Unknown
fields, duplicate options and ambiguous argument forms are rejected. Observed host,
account and template values are data, not invented enumerations. The initial integer
type uses the inspected CLR Int32 range; a different interface needs a suitable contract.

The renderer follows `CommandLineToArgvW`, the API used by Apollo's assembly runners.
Spaces, quotes and backslashes are encoded as argument values, without invoking a shell.
The inspection image includes a fixed inert fixture that exercises that API. Its results
check parsing compatibility; they do not establish operational Certify behavior.

Preparation binds the artifact, descriptor and rendered invocation. A change after
approval requires fresh preparation and approval. Recognized parser failures are
construction failures even when Mythic marks the native task completed; raw output is
retained. Equivalent retries cannot evade that failure by reordering options or changing
a wrapper. New compatibility evidence is required before repeating the failed invocation.

## Model schemas and limits

The Operator receives the selected schema on demand. The OpenAI-compatible adapter
carries a per-tool strict flag. Its provider-facing copy moves unsupported numeric
bounds and string-length constraints into descriptions; Sage still enforces the original
constraints locally. Bedrock's strict-schema subset rejects those keywords even when
the underlying JSON Schema is valid.
The installed native Bedrock adapter removes nullability,
so Sage supplies a non-strict typed copy with nullable options represented as optional
properties. Required fields remain required, and omitted options receive the same local
defaults. Other routes receive the typed schema and remain subject to local validation.
Backend support is a property of the
configured provider, model and gateway; adapter serialization alone does not establish
constrained generation. See [Claude strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use)
and [Bedrock structured outputs](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html).

Strict generation supplements local validation. It cannot establish artifact identity,
cross-field semantics, target prerequisites, approval or effects. The enrolled contract
does not automatically turn arbitrary help text into trusted schemas. New executables
need reviewed descriptors and parser/transport tests; unusual grammars need an adapter.

See [off-target inspection](ARTIFACT_INSPECTION.md) for the separate static/help workflow.
