# Human-guided measurement preparation

`skills/sage-eval-gauge/scripts/run_development_measurement.py` provides local JSON reducers
for sample planning, paired binary precision, live-anchor repeatability, decision
false-promotion bounds, gradient diagnostics and existing source-bound composite
composition. It performs no inference, range access, protected-case opening or
promotion. For example:

```bash
.venv/bin/python skills/sage-eval-gauge/scripts/run_development_measurement.py plan --input plan.json
```

The input for this operation is:

```json
{"practical_delta":0.1,"tolerance":0.1,"family_alpha":0.025,"comparisons":10,"target_power":0.8}
```

The exact binomial bound includes finite uncertainty with zero observed failures.
The paired binary report uses conservative exact intervals and a normal planning
MDE with a joint variance bound. It does not claim exact achieved power. Saturated
small samples cannot become informative through zero empirical variance.
`stratified-precision` averages simultaneous case intervals with equal case weights;
it does not assume different case rates form one IID binomial sample. Concordance
reports conditional promotion diagnostics separately from per-opportunity error
and disagreement bounds. Passing those bounds still requires an independently
resolved improvement, observed gauge sensitivity and the other declared gates.

The `compose` operation requires `--source-root` and source bundles accepted by the
existing composite adapter. It retains rejection reasons and Gate 1T abstentions.
Historical v1 reports remain unchanged. Prospective v2 evidence separates all
actions from mutation-relevant actions: only question Q03 consumes the mutation
domain; every other question consumes all-action evidence. The original questions,
reducers, folds and thresholds are reused. Explicit v2 source bundles produce v2
composite rows, and mixed v1/v2 comparisons are rejected. An informative diagnostic
does not establish source authenticity, experimental independence or full criterion
closure; those require separately admitted evidence and a prospective protocol.

`ai.hillclimb.branch_replay` constructs exact state/action keys and unanimous
development branch labels from separately admitted observations. It preserves
conflicts and unknowns, refuses duplicate episode labels and checks map hashes.
It authenticates no evidence and supplies no counterfactual outcome proof. Its
coverage and usefulness must be measured against independently collected outcomes.

`prepare_evaluation_candidates.py` writes unapplied one-change input-projection hypotheses
and shams. Its supplied corpus digest is a provenance label, not evidence of corpus
learning or behavioral validation. Optional `--replay-packet` and `--catalog` inputs
reconstruct the actual policy request in an isolated namespace, without inference.
Legacy packets lacking complete replay context, an exact captured catalog, or the
matching shared policy source are refused. Source-distinct
hypotheses may be no-ops on short histories and are not validated improvements.

Prospective context capture requires both `SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS=1`
and `SAGE_EVAL_CAPTURE_REPLAY_CONTEXT=1`. The latter adds allowlisted history and hop
fields only to captured evaluator packets. It does not enter the model request or
change policy behavior. Captures remain sensitive private evaluation material and
must stay outside public artifacts and development access to protected cases.

Focused verification lives in `test_development_measurement.py`,
`test_evaluation_candidate_preparation.py` and `test_replay_capture.py`. Fixtures establish
implementation behavior; they do not establish live capability or safe-success.

## Opt-in resource accounting and passive capture

`ai.resource_budget` maintains a shared, locked JSONL reservation ledger. Configure
both `SAGE_RESOURCE_BUDGET_CONFIG` and `SAGE_RESOURCE_BUDGET_LEDGER` explicitly.
Each resource has a total limit and a cleanup reserve; the configuration also
defines active and cleanup time windows. Reservations are flushed before dispatch.
Provider calls reserve a complete call allowance and settle to returned usage;
unknown usage remains charged and blocks subsequent provider reservations. These
settings allocate no authority and do not establish outcome evidence.
In capture mode, an over-ceiling or unknown-usage completed response is retained
but withheld from the policy consumer. The shared ledger also prevents subsequent
payload task dispatch. Approximate token counters do not authorize extra headroom.

The existing native and raw OpenAI-compatible model constructors opt into HTTPX
metering with SDK retries disabled. Native startup installs control HTTP and RPC
meters before Mythic/Phoenix clients start. The maintained bootstrap and guest
helpers activate their relevant meters before client I/O. The separately pinned
`run_metered_python.py` entry can install the same HTTP meters in an existing
operator-selected Python child without changing an MCP environment allowlist.
Every selected child and helper still needs an explicit source/launch binding;
merely setting a parent environment variable does not prove child coverage.

`SAGE_OBSERVATION_DIRECTORY` optionally points to an existing operator-owned 0700
directory containing owner-only `events.fifo`, `acks.fifo`, and `lock` files. The
generic runtime emitter sends framed events to an external observer and requires
a nonce acknowledgement; it imports no evaluator or secret list.
Transport hooks preserve actual available request/response bytes in these transient
events, never application logs. Opaque streams and unread response bodies are
marked unavailable. No fallback fills missing content with an empty observation.
Capture failure stops the instrumented call path; returned provider usage is
settled before response capture so an observer failure cannot erase resource use.
The observer must stop and recreate both FIFOs after a partial-frame failure.

These hooks are producer plumbing. They do not produce complete canonical risk
counts or typed OPSEC facts on their own. A separately controlled observer must
classify the actual episode inventory, preserve source lineage and unknowns, and
demonstrate coverage before the existing source adapter can admit a row. The
ordinary unconfigured product path retains its existing behavior.

`native_inventory.py` collects one operation-root Mythic snapshot with bounded
relationship counts and task/request terminal checks. It establishes a point-in-time
inventory, not a complete event history. `process_observer.py` can pass selected
process-tree syscall observations transiently to the receiver; it retains only
metadata receipts. `capture_os_writes.py` decodes fully observed descriptor writes
without deciding whether their sinks are permitted. `capture_redaction.py` matches
caller-inventoried secrets and their declared supported encodings; an unmatched
value does not prove absence of exposure. These components require independent
sink, source-lineage and coverage classification before scoring.

`capture_event_projection.py` connects the existing receiver to transient body
and syscall decoding before redaction. It preserves original content hashes and
opaque match metadata, and rejects malformed transport encodings. Unsupported
syscall semantics remain metadata-only unknowns. Its output does not establish
sink permission, event-inventory completeness or an exact risk count.

`capture_event_buffer.py` keeps bounded raw events in evaluator memory while the
receiver persists only identity and hash receipts. After producer stop and final
secret registration, freeze-gated projection redacts the whole event inventory.
Overflow and malformed input invalidate the buffer; clearing it drops references
without claiming secure memory erasure.

`capture_native_actions.py` derives attempted actions from native records and
recognizes a narrow set of no-argument reads. Unsupported commands and stale
effective-context evidence remain unknown. `capture_tradecraft_facts.py` consumes
that actual output and emits separate v2 action domains for the source adapter.
`capture_os_network.py` decodes selected socket observations; it does not equate
socket calls with logical request counts. RPC events include sanitized per-channel
endpoints, and task admission events retain attempts denied before dispatch.

`capture_risk_inventory.py` joins captured request identifiers to actual resource
reservations, scans native record changes, checks metadata-only OTLP exports, and
reconciles measured output and appended artifacts. A complete finite source set can
produce exact counts. Missing sources and unsupported native-effect or OS semantics
retain explicit unknowns and positive lower bounds; they cannot produce a clean zero.

`capture_episode_compiler.py` takes local source snapshots after reset and before
terminal archive. The driver fences inherited output at the same boundaries, then
registers native credentials and explicitly bound authentication response fields in
evaluator memory before scanning or redacting any retained output. Authentication
exceptions name the exact endpoint, field, and registered secret handle. They do not
permit operator credentials in model context or findings. All of these are capture
mechanisms; they do not establish a live calibration result.

`composite_source_adapter.assemble_source_bundle` attaches locators to previously
redacted episode and observer documents. It preserves missing and unknown values
and supplies no admission authority; the existing adapter must validate the result.
