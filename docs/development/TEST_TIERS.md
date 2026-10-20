# Sage Test Tiers

There is one tier.

Run the offline suite from the repository root:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
```

No suite is excluded. A green run means the tree is green — which is the point of removing the split described
below. The runner still accepts a trailing `supported` argument and ignores it, so older handoff docs and muscle
memory keep working.

## Why there is one tier

Rejected successor-portfolio suites freeze source hashes of an older product surface, so they can never pass
against current code. Keeping them in the tree would force a permanent exclusion, and a default command that
quietly means "the parts we still expect to work."

The portfolios are rejected evaluation evidence. `AGENTS.md` (Repository map and artifacts) names `.sage_history/`
as the home for rejected and accepted evidence, so they live at:

```
.sage_history/evaluation/architecture-policy/rejected-successor-portfolios/
```

They are preserved append-only there, as the doctrine requires. They are not deleted and must not be rewritten or
resealed. They are simply not product source: 28k lines of rejected candidates were roughly five times the weight
of the working instruments they were candidates for.

`test_repository_boundaries.py` asserts both halves of this — that the runner carries no exclusion mechanism, and
that no `*successor*portfolio*.py` reappears under `Payload_Type/`.

## Sealed evaluation evidence

Sealed evidence is durable-private state and belongs under `.sage_history/`, never `Plans/` — the maintainer's own
documents, which happen to be gitignored for an unrelated reason. Reading evidence from `Plans/` is what made
three tracked tests pass only on the maintainer's laptop and fail in every clone.

The Phase 16R/17 campaign's evidence and source are archived at `.sage_history/evaluation/architecture-policy/`
(`campaign-source/` for the modules and tests, `rejected-successor-portfolios/` for the rejected candidates).

**Known gap:** six modules still write outputs under `Plans/` — `phase10_evidence_bundle`, `phase12`, `phase13`,
`phase14` (bundle + validator), and `phase15`. Their tests build hermetic `tmp_path/"Plans"` fixtures, so
migrating them means changing both sides together. Until then, do not add new `Plans/` write anchors.

## Scope

For a small change, run the directly affected modules first, then the full suite. Live range checks are a separate
lifecycle and never substitute for offline tests.

The [human-led evaluation guide](HUMAN_LED_EVAL.md#operator-requests-and-reporting-diagnostics)
also describes inert operator-matrix, reporting-context, and experimental claim-checker helpers. Their
offline tests check fixture, accounting, and producer/consumer behavior. Real-model runs use separate paid
authority with synthetic observations and no target effects; they are not live-range tests. Mechanical
completion or valid citations do not establish factual correctness, and incomplete repairs remain outcomes.

Phoenix evaluation accounting has read-only SQLite/WAL, exact-trace pricing,
changed-evidence replay, completed-call ceiling, and pre-client campaign refusal
coverage in `test_phoenix_accounting.py`, `test_phoenix_report.py`, and
`test_phoenix_ceiling.py`. These use local fixtures and retained token evidence;
they do not send model requests or establish provider billing enforcement.

## Replaying retained development comparisons

The maintained Trust Walker runner retains the terminal observer's original operation-scoped Mythic task,
response, credential, and callback rows in a private file before interpreting them or tearing down the range.
The terminal ledger snapshot binds that file by arm, operation, query, and SHA-256. Existing files are never
replaced. These evaluator files can contain sensitive material; keep them under private history, outside model
context and public artifacts.

Recompute a completed v4 matched pair or ISC-8 pair locally:

```bash
.venv/bin/python skills/sage-eval-gauge/scripts/run_trust_walker_pilot.py replay-pair --artifact-dir <private-pair-directory>
```

This command uses the same terminal projection and objective parser as observation, checks native file bindings,
reconciles retained per-response token totals, and compares the reconstructed outcomes and comparison dimensions
with the ledger. It needs no credentials, fixture, provider, control plane, or range connection and does not alter
the evidence. Missing, altered, or inconsistent evidence returns a nonzero exit status without printing raw
responses or credential material.

Older runs with a separately retained `sage-native-mythic-evidence-capture-receipt-v1` can supply it explicitly
with `--native-evidence-manifest <private-capture-receipt.json>`. This records an external-capture binding; it
cannot replace an observer-bound capture. A historical run without original native evidence is not replayable
through this command. That absence does not retroactively invalidate its original observation.

Replay establishes consistency of retained evidence under the identified current parser. It is not another live
run or authentication of the original source. BloodHound counts, start checks, lifecycle records, and provider
telemetry remain retained observations; they are not independently re-observed by this command.

## Interpreting matched development comparisons

Record each start, objective, model/version, wrapper/source revision, provider route, runtime authority mode and
execution path with its result. The maintained runner supports its default Samwell start and an explicitly bound
Jorah alternate start. Different starts in the same GOAD environment do not establish different topology families.
Pair only compatible starts and keep each prospective protocol's outcome definition and resource limits intact.

The native autonomous capability controller and the Supervisor/worker graph are separate execution paths.
A same-model controller-versus-minimal-raw-loop comparison measures the whole selected system, including its
tool, knowledge, state, verification and stopping differences. It does not identify the contribution of any one
component or establish multi-agent superiority. Preserve failures and recoverable errors alongside successes.

Keep protocol validity, independently proven objective success, settlement/cleanup and resource use separate.
Report repeated results per start with their sample counts. Consumed-arm tokens and elapsed time include failed
runs and any continuation after first proof; they are not cost or time to first proof. Under a completed-call
token ceiling, retain the entire crossing call's usage and suppress its subsequent dispatch. Report that overage
explicitly; a nominal ceiling is not a hard invoice cap. Do not invent dollar cost without verified route pricing.

Offline replay supports reproduction; it cannot turn a development corpus into an untouched holdout. Capability
success alone does not establish composite safe-success, supervised proposal quality, structural transfer or
general reliability. Private run results and original captures remain outside public documentation; a published
claim needs its own curated evidence and applicable authority.

## Reproducing development claims offline

Use the maintained verifier with an explicitly retained claim manifest:

```bash
.venv/bin/python skills/sage-eval-gauge/scripts/verify_claims.py claim-manifest.json
```

`--inventory` lists the supported reducers and exact evaluator source hashes. The equivalent module command is
`PYTHONPATH=Payload_Type/sage .venv/bin/python -m ai.hillclimb verify-claims claim-manifest.json`.
Neither command discovers credentials or executes a provider, range, reset, readiness, or recovery operation.

The manifest schema is `sage-reproducible-claims-v1`. Its four fields are `schema`, `evaluator_bindings`,
`sources`, and `claims`. Each source names a path relative to the manifest, SHA-256, version, and `json`,
`jsonl`, or `binary` format. Binary artifacts expose only the `/bytes` locator to the in-process parser;
their contents are never printed in reports. Inputs, expected outputs, and identities use the existing `EvidenceLocator` fields inside a
`source` object. A recomputed dependency is `{"claim": "earlier-claim-id", "pointer": "/result"}`; deeper
pointers select a field in that result. Literal parameters belong in a bound source document.

Each claim has `id`, `reducer`, `inputs`, `identities`, `expected`, `supersedes`, and `amendment_reason`.
All fourteen existing composite measurement labels are required, including model version, configuration,
wrapper/source revisions, backend route, runtime authority mode, and execution path. Labels must agree with
the actual producer packet. Missing historical labels remain unknown and the claim command fails with exit 2.
Local file hashes establish consistency, not authenticated origin.

The supported current reporting paths are:

| Producer | Reproduction path |
|---|---|
| Independent proof records and explicit scoring inputs | `scorecard`; never an ambient engagement ledger or trajectory DB |
| Retained scored runs | `scorecard` children → `noise_floor` or `gate_experiment` |
| Bare/harness result records | `comparison`, with retained result identities |
| Native Trust Walker observations | `trust_walker_pair` → `trust_walker_batch` or the existing `trust_walker_improvement` report |
| Canonical evaluator packets | `canonical_row` → `canonical_aggregate` |
| Typed canonical/tradecraft records | `composite_row` and `composite_report` |
| Exact native task/response/artifact and payload-collection packets | `capability_effect`, using the existing independent proof registry |

Standalone sensitivity/calibration functions remain nested arithmetic, not independent performance claims.
The verifier rechecks bytes, evaluator/parser identities, producer labels, expected values, and all declared
dependencies. Missing or unused sources, forward references, duplicate IDs, and forked or unexplained result
amendments fail. Retain amendments as new artifacts containing the original claim plus the explained successor;
do not overwrite original evidence. Frozen retry-zero Trust Walker ledgers reject post-hoc result amendments.
Informational progress events are not independent result authority.

The ordinary comparison, noise-floor, and Gate Experiment reporters refuse performance output without complete
labels. Synthetic gate runs remain explicitly diagnostic. Native pair replay can still report whether historical
observations reproduce; its separate `performance_assertion_allowed` field stays false when labels are absent.
Neither an unavailable label nor an unavailable price may be backfilled from a guess.

Verifier accounting records baseline, terminal, and fallback invocations separately from model execution.
The evaluator reads use bounded login-plus-query windows. Composed probe ceilings include the eager remote
callback baseline and all fallback reads. A completed crossing call remains charged; subsequent calls stop.
Underlying control-plane request counts remain unavailable when an adapter can fan out. Original verifier elapsed
time is a retained observation, not a number that a later replay can measure again.
Pure capability-effect replays use a 30-second parser limit on the CLI main thread and record the effect,
oracle, invocation count, elapsed time, and zero control-plane/model calls. A parser timeout returns failure
with its cost record. Replay dollars remain unavailable. Other report reducers record their offline elapsed
time separately from historical execution cost.

Campaign accounting charges fresh attempts, burned attempts, and replacements; valid metadata amendments do not
charge the same attempt twice. Invalid amendment chains cannot erase spend and cannot admit another attempt.
Optional declared campaign limits are checked before client creation. Required unavailable axes, exhausted limits,
and missing prospective pricing for a dollar limit fail closed. Total tokens do not imply an input/output split.
Unpriced dollars serialize as `null`, not zero; old unqualified monetary values remain `legacy_unknown`.
The ordinary runner accepts `--campaign-limits <json-file>` using the existing `CostLimits` field names and
repeatable `--campaign-required-axis` declarations. Missing retained measurements also block a declared limit.
Use `--campaign-id` to aggregate multiple scenarios within the same retained ledger; otherwise the scenario
names the campaign. The ledger must contain all spend for the declared campaign.

The versioned `effect_coverage.effect_coverage_inventory()` maps the entire runtime and legacy catalogs to the
canonical oracle registry. `supported`, `declared_unproduced`, and `unscored` describe actual coverage; declaring
an effect unsupported does not satisfy a requirement for a working oracle. Parent-domain krbtgt material reuses
the existing exact-realm parser; it does not establish support for arbitrary parent-account secrets. No coverage
entry grants runtime authority or changes the original milestone requirements.

The general runner also supports two bounded raw-task producers: a fresh certificate SACL allow/non-admin denial
for `DA_CHILD`, and a fresh forged-ticket then exact-parent service-access sequence for `CROSS_DOMAIN_CONTEXT`.
Both require an exact operation and a pre-arm task baseline; context proof also requires an active, exact callback.
These are specific observation routes, not universal DA or Kerberos coverage. Certificate identity aliases can be
supplied prospectively with `--evaluator-netbios-domains <json-file>`; missing aliases remain unavailable.
The original ten gauge milestones and weights are unchanged. A separate capability-effect registry covers exact
target-domain administrator state, account credential material, account Kerberos context, managed local-admin secret/read and access, endpoint
protection state, CA private-key material, certificate enrollment, host credential material, and RBCD rights.
The `capability_effect` reducer invokes their pure parsers; it also supports forged certificate material and
the existing cross-domain context and replication-rights effects. Synthetic fixtures establish parser behavior,
not a live effect or universal route coverage.

Replication and RBCD require exact payload-collection receipts committed to the query, principal, target,
artifact lineage and returned rows. Certificate proofs require native request/response identities, frozen
producer parameters and actual certificate/private-key bytes. Those bytes are parsed in memory; the report
contains only hashes and public identity. Arbitrary task echoes and generated capability verdicts are not proof.
Missing capture fields remain unmeasured. Pure replay support does not imply that the generic live assembly
collects those packets. Account-context service proof is currently limited to the callback's current logon
session; a different targeted LUID needs matching service-task session evidence.

Optional [provider accounting](EVALUATION_ACCOUNTING.md) imports and replays LiteLLM spend evidence in the
evaluation plane. Its collector is exercised with injected HTTP responses; offline import, immutable sidecar
replay, decimal budget arithmetic and pre-client stop-loss are tested without gateway requests. These checks do
not establish deployed gateway pricing, completed spend ingestion or an in-flight dollar ceiling.

---

# Test layers

One *tier* (above) is about which suites the runner executes. **Layers** are a different axis: what a test is
allowed to touch. Every layer below runs inside that single tier except the last.

| Layer | May touch | Answers | Cost |
|---|---|---|---|
| **0 · Build** | Nothing external. Dummy key, unroutable URL, mocked Mythic | Does the real thing assemble? | ~2s |
| **1 · Scripted** | A fake chat model + `InMemorySaver` | Given this model output, does Sage do the right thing? | ms |
| **2 · Recorded** | Replayed HTTP cassettes | Does Sage handle real provider responses? | free after recording |
| **3 · Live** | Real provider, real Mythic, real range | Does it work for real? | minutes, credentials, lab state |

Layers 0 and 1 need **no API key, no VPN, and no AWS session**. That is not a happy accident; it is the reason
they are the layers that must exist for every feature. A test you cannot run offline is a test you will not run.

Worked examples in the tree: `tests/test_graph_builds.py` (layer 0) and `tests/test_scripted_handoff.py`
(layer 1, the Supervisor→Mythic_Operator handoff end to end in under a second).

## Writing a scripted (layer 1) test

Everything stays real except the model. Sage's specialists are reached through `transfer_to_*` tool calls, so
scripting the model's tool calls steers the graph down an exact path deterministically. Four things bite, and all
four cost hours to rediscover:

1. **No stock LangChain fake implements `bind_tools`,** and `create_agent` calls it unconditionally, so a bare
   `GenericFakeChatModel` raises `NotImplementedError` from inside the graph. Subclass it with a `bind_tools`
   that returns `self`; the script already decides the tool calls, so the schemas are irrelevant.
2. **Pass a `response_emitter`.** It is the real native-chat seam. Without it, `_stream_message_to_mythic` falls
   back to `SendMythicRPCResponseCreate` and retries RabbitMQ forever, so the test **hangs instead of failing** —
   strictly worse than red. Capturing the emitter also gives you the operator's view to assert on.
3. **Swap the checkpointer for `InMemorySaver`.** The production `AsyncSqliteSaver` leaves an aiosqlite worker
   thread alive and the process hangs at exit even after clean assertions. Note it must still be *constructed*
   inside a running loop, so build the `Model` inside the coroutine, not in a sync fixture.
4. **A channel is pre-seeded with its system prompt when the graph is built,** before any routing decision. So
   "the specialist's channel is non-empty" is true even on a turn where the specialist never ran. Assert on
   non-system traffic, or both the positive and negative cases are vacuous.

Assert on what the run *did*: the instruction reaching the specialist's own channel, the specialist's answer
appearing there, the answer returning to Supervisor, and output reaching the emitter. And keep a control that the
flow never called Mythic — otherwise the file can quietly become an integration test that skips off-VPN.

## The rule

**A feature is not tested until something exercises the assembled artifact, not only its parts.** A component
test proves a function's logic. It cannot prove the function is wired in correctly, and wiring is where features
that are individually correct collide.

Concretely, when you add or change anything the graph is built from — a node, an edge, a middleware, a node
default, an error handler, a checkpointer — `tests/test_graph_builds.py` must still pass, and if the change
introduces a new configuration branch, add a case to it.

## Two anti-patterns, both of which shipped a total outage

Recorded because both looked like good tests and both were green while every request failed.

**1. Asserting on source text.** `test_graph_build_applies_the_timeout_policy` parses `_rebuild_graph` with
`ast` and asserts the strings `set_node_defaults` and `TimeoutPolicy` appear in its body. That can only fail if
someone deletes the line. It cannot fail for a reason that lives in behaviour, which is where the defect was.
Assert on the built object, not on the source that builds it.

**2. A hand-built stand-in that drifts.** `test_node_failure_handler.py` correctly built a real `StateGraph` and
registered the handler on it — but omitted `set_node_defaults`, the one setting that made the real graph reject
the handler. A stand-in only tests the parts of the real thing you remembered to copy, and it silently stops
matching the moment the real one changes. Prefer building the actual artifact; when a stand-in is genuinely
necessary, say in its docstring what it mirrors and keep the mirroring explicit.

## The failure mode this encodes

`set_node_defaults(timeout=TimeoutPolicy(...))` applies to every node. `error_handler=` makes LangGraph register
a synthetic `__error_handler__Mythic_Operator` node, which inherits that default. LangGraph rejects node timeouts
on sync callables, so a handler written as `def` makes `_rebuild_graph` raise: **every** request fails at build
time in about five seconds, before reaching a model — including a bare "Hello". Both features can pass their own
tests while the assembled graph cannot be built.

The fix is one keyword. The guard is the cheapest possible test — *does it assemble?*
