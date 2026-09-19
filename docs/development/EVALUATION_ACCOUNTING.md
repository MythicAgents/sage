# Evaluation accounting

Accounting is opt-in development tooling under `ai.hillclimb` and the eval-gauge
operator scripts. Normal Sage chat, graph construction, model clients, prompts,
and tools do not import or activate it. No production configuration or runtime
source change is required. Phoenix token estimates work entirely offline and do
not require LiteLLM administrator access, spend-log permission, or an Enterprise
license. The optional gateway-spend adapter supports LiteLLM; future providers
implement `AccountingProvider.normalize_rows` and register a configuration name
in `create_accounting_provider`. The ledger and reporting interface stay the same.
No LiteLLM SDK or new runtime dependency is installed.

## Phoenix token estimates

Use exact trace IDs from a retained Phoenix database and an explicit rate card.
The reader opens SQLite read-only, including committed WAL records, and selects
only model/provider identifiers, span metadata and token counters. It never
selects prompts, responses, events or arbitrary attributes. Existing Phoenix
cost columns are not required. No database is auto-discovered or changed.

A rate card has this shape; these are example values, not actual prices:

```json
{
  "schema": "sage.eval.phoenix-rates.v1",
  "price_provenance": {
    "source_id": "retained-model-rate-card",
    "route_model_identity": "declared-route-and-model-version",
    "currency": "USD",
    "effective_date_or_version": "rate-card-version"
  },
  "models": [{
    "provider": "observed-instrumentation-provider",
    "model": "exact-observed-model-alias",
    "input_usd_per_token": 0.000003,
    "output_usd_per_token": 0.000015,
    "cache_read_usd_per_token": 0.0000003,
    "cache_write_usd_per_token": 0.00000375
  }]
}
```

Use the provider and model values actually recorded in Phoenix. An OpenAI SDK
instrumentation provider can coexist with a declared Bedrock route. This mapping
prices the observed alias; it does not independently attest the upstream route
or reconstruct historical effective billing. Referenced pricing artifacts use
the same `artifact_path`/`artifact_sha256` provenance fields described below.
Both the rate card and referenced provenance are checked on replay.

The selection configuration contains:

```json
{
  "campaign_id": "evaluation-campaign",
  "database_path": "/configured/archive/phoenix.db",
  "pricing_path": "/configured/evaluation/rates.json",
  "method": "conservative",
  "capture_complete": true,
  "attempts": [{
    "attempt_id": "retained-attempt-id",
    "trace_ids": ["exact-retained-trace-id"],
    "expected_model_calls": 6
  }]
}
```

`capture_complete` declares that instrumentation has finished exporting the
selected attempt's spans. Check the expected call count against retained runner
telemetry; when a typed ledger has that count, the adapter checks agreement.
Exact trace selection excludes unrelated runs. Duplicate selections, nested LLM
instrumentation, unfinished calls, missing counts, mismatched models or missing
rates cannot become a complete estimate. Failed calls with known usage count.
Uninstrumented provider retries remain outside this logical-span accounting.

`conservative` prices every inclusive prompt token at the largest configured
input/cache rate, plus completion tokens at the output rate. The rates must
describe exclusive token categories; reasoning is already included in completion.
This provides an upper estimate under that rate card when cache splits are
unknown. It is not a provider invoice or a guarantee against price changes.
`cache_aware` instead uses the retained cache-read/write breakdown and refuses a
complete estimate when either category is unknown. Missing categories are never
invented as zero. All monetary outputs remain explicitly `estimated` through the
campaign summary; changing the rate card requires a new sidecar.

```bash
.venv/bin/python skills/sage-eval-gauge/scripts/accounting.py phoenix --config <phoenix-selection.json> --ledger <attempts.jsonl> --output <new-accounting-bundle.json>
```

For a standalone trace report omit `--ledger`; it cannot drive campaign admission.
Pilot ledgers use `--ledger-kind pilot`. Replay and `check-budget` below accept
both Phoenix and gateway bundles. Replay rereads only selected trace evidence;
unrelated new traces do not invalidate a report, but changed or missing selected
usage does. The original database and result ledger stay untouched.

## Evidence and scope

The optional LiteLLM adapter accounts **gateway-reported model spend in USD**. This is calculated
provider accounting, not an AWS invoice or proof of a particular effective rate.
Freeze the actual model/deployment and pricing provenance for each campaign.
Bedrock cache, region/profile, service-tier, and effective-rate configuration must
be represented correctly at the gateway. Missing prices or usage are unknown,
never silently zero. An explicit measured zero is accepted only with provenance.

Every provider record must belong to exactly one attempt. Choose either:

- Exact provider spend-log `request_id` values retained by the evaluator. A
  client-generated decision ID or `x-litellm-call-id` is not automatically the
  spend log's `request_id`; establish that mapping before using it.
- A dedicated evaluation key's SHA-256 identifier, a half-open UTC request-start
  window, and an explicit count of upstream attempts, including retries/failures.
  Windows for the same key cannot overlap. Normal Sage runtime need not emit new
  metadata for this mode. A shared key with unrelated traffic is unsuitable.

The request count must come from complete gateway evidence, not Sage's logical
model-call count: one client call can involve several upstream attempts. Unknown
counts, missing charges, unassigned rows, conflicting duplicates, and wrong
models/deployments prevent a complete total. Exact duplicated pagination rows
are counted once. Failed attempts with reported charges still count. The report
retains a **priced subtotal of assigned rows** for diagnosis when the full total
is unavailable; that subtotal must not be used as the campaign total.

Spend writes can arrive after inference completes. `coverage.complete: true` is
an explicit operator assertion that the export is settled and complete for its
declared interval. Reaching an empty API page proves pagination completion, not
that all in-flight requests or delayed gateway writes have finished. Check this
before marking coverage complete. Native LiteLLM timestamps without an offset
are interpreted as UTC; selector and coverage timestamps require an offset.

Only allowlisted accounting fields survive import. Prompts, responses, raw keys,
and arbitrary metadata are discarded. Observed API URLs are retained only as
SHA-256 fingerprints, preventing path-embedded credentials from leaking. A retained key identifier must be a
64-character hexadecimal digest. Store generated bundles in private evaluation
history. They contain non-secret but potentially sensitive operation/model IDs.

## Offline import and replay

Create an accounting manifest with the following structure. All values below
are examples, not pricing or run evidence:

```json
{
  "provider": "litellm",
  "campaign_id": "evaluation-campaign",
  "binding": {
    "expected_model": "bedrock/exact-upstream-model-version",
    "expected_model_id": "exact-litellm-deployment-id",
    "route_model_identity": "declared-bedrock-route-and-model-version",
    "price_provenance": {
      "source_id": "retained-gateway-pricing-snapshot-reference",
      "route_model_identity": "declared-bedrock-route-and-model-version",
      "currency": "USD",
      "effective_date_or_version": "retained-pricing-map-version"
    }
  },
  "coverage": {
    "complete": true,
    "start_time": "2026-01-01T00:00:00Z",
    "end_time": "2026-01-02T00:00:00Z"
  },
  "attempts": [
    {"attempt_id": "retained-attempt-id", "request_ids": ["provider-spend-request-id"]}
  ]
}
```

`expected_model` binds the observed upstream model, not merely its routing alias;
`expected_model_id` binds the LiteLLM deployment. `expected_api_base` can optionally
bind an observed API base too. Bedrock logs may omit that field. The route identity
is a declared deployment/pricing mapping; it is not independent route attestation.
`PriceProvenance` also supports an absolute `artifact_path` and `artifact_sha256`;
the referenced non-secret pricing artifact must exist and match on every replay.
Existing pricing evidence can be referenced without copying credentials into the manifest.

For key/window attribution, replace `request_ids` with `api_key_hash`,
`start_time`, `end_time`, and `expected_request_count` in the attempt selector.

Import a complete JSON array of exported spend rows:

```bash
.venv/bin/python skills/sage-eval-gauge/scripts/accounting.py reconcile --manifest <manifest.json> --spend <spend-rows.json> --ledger <attempts.jsonl> --output <new-accounting-bundle.json>
```

The output is created with private permissions and refuses to overwrite an
existing file. Malformed/conflicting input exits 2 without a bundle. Well-formed
incomplete input retains an unavailable report and exits 2. A single v2 response
page is not accepted as a complete export.

For a maintained Trust Walker `pilot.jsonl`, add `--ledger-kind pilot` and use the
retained arm names as attempt IDs. The sidecar must cover every started arm,
including failed arms. It supplements model costs without changing historical
outcomes, resource-completeness flags, or comparison eligibility. Omit `--ledger`
only for a standalone provider-cost report; that report cannot drive a campaign
budget decision.

Replay is offline and detects changed accounting inputs, evaluator sources,
asserted totals, or missing/changed linked ledgers:

```bash
.venv/bin/python skills/sage-eval-gauge/scripts/accounting.py verify <accounting-bundle.json>
.venv/bin/python skills/sage-eval-gauge/scripts/verify_claims.py <accounting-bundle.json>
```

The shared claim verifier explicitly marks these as accounting-only reports,
without performance-assertion eligibility. Hashes establish local consistency;
they are not authentication or an adversary-resistant billing system.

## Explicit read-only collection

`collect` is the only command here that contacts a gateway. Configure an HTTPS
proxy origin without `/v1`, the **name** of an operator credential environment
variable, and a bounded UTC interval:

```json
{
  "base_url": "https://gateway.example.test",
  "api_key_env": "SAGE_EVAL_ACCOUNTING_API_KEY",
  "start_date": "2026-01-01 00:00:00",
  "end_date": "2026-01-02 00:00:00",
  "page_size": 1000,
  "max_pages": 20,
  "timeout_seconds": 10
}
```

Optional `ca_bundle` identifies a configured CA file. Optional `api_key_hash`
filters a dedicated key and must match every window selector. Model filtering
is refused by this command because it could hide charged fallback attempts.
Use a credential with spend-log read permission. There is no automatic `.env`
loading, Sage inference-key fallback, key creation, reset, or budget mutation.

```bash
.venv/bin/python skills/sage-eval-gauge/scripts/accounting.py collect --manifest <manifest.json> --gateway-config <gateway.json> --ledger <attempts.jsonl> --output <new-accounting-bundle.json>
```

The collector issues only `GET /spend/logs/v2`, follows pages until empty data,
and ignores capped advertised totals. Timeouts, redirects, malformed responses,
oversized pages, and exhausted page limits fail closed. Raw responses stay in
process memory; only scrubbed accounting evidence is written. This work was
verified with injected HTTP responses, not against a deployed gateway.

## Budget checks and their limits

`check-budget` rederives the sidecar, validates its original typed attempt ledger,
and supplies an in-memory cost view to the existing campaign stop-loss function.
Burned attempts and replacements retain their charges; valid amendments do not
double-charge. Original ledger bytes and cost-amendment protections are unchanged.
`--limits` is existing `CostLimits` JSON; `--next-price` is prospective USD
`PriceProvenance` JSON for the selected next route. No dollar amount is defaulted.

```bash
.venv/bin/python skills/sage-eval-gauge/scripts/accounting.py check-budget <accounting-bundle.json> --limits <limits.json> --next-price <price-provenance.json> --required-axis monetary_cost_usd
```

The command exits 2 for exhausted/exceeded limits or unavailable evidence. A
successful decision means only that the retained spend permits another attempt;
it does not authorize or execute it. The dollar axis here covers model spend.
Verifier wall/probe costs and any infrastructure charges remain separate measures.

The existing `run_gauge_live` evaluation CLI optionally accepts
`--campaign-accounting-bundle <bundle>` and `--campaign-price-provenance <price>`
together with `--campaign-limits <limits>`. It rechecks the exact bound ledger and
campaign **before constructing clients**. Appending another result makes the
previous sidecar stale, so reconcile into a new bundle before the next attempt.
With these flags absent, existing evaluation behavior is unchanged.
A new campaign with no attempts can use `--campaign-price-provenance` and
`--campaign-limits` before its first run, without inventing an empty provider
export. Once unpriced attempts exist, their cost evidence is required.

This pre-attempt check is **not an in-flight hard dollar ceiling**. Phoenix reads
recorded calls after execution. Retain the eval runner's token/time/action limits;
a token threshold checked after a completed call can be crossed by that call.
Any claimed conservative per-run dollar maximum must include this crossing-call
headroom and declared retry/concurrency bounds. Gateway budget enforcement is an
optional separate mechanism, not a prerequisite for Phoenix estimates. The
accounting adapter does not configure a gateway or change runtime request handling.

For an existing bounded `run_gauge_live` LLM row, add `--run-estimate-rates <rates.json>`
and include positive `max_input_tokens` and `max_output_tokens` in that model's
rate-card entry. These are the exact priced model's request maxima, from retained
model documentation/configuration. The runner matches its resolved provider/model,
rejects legacy, symbolic, null-model or missing-bound configurations before client
creation, and retains the derived ceiling in its existing per-run protocol. Both
the raw loop and controller row await one logical model response at a time.
No existing token limit, runtime model setting or strategy changes.
An estimated campaign balance requires this per-run declaration before admission;
the standalone `check-budget` command still only evaluates the next-attempt balance.

For completed-call token ceiling `C`, let `q_in` be the largest input/cache rate,
`q_out` the output rate, and `I`/`O` the model's request maxima. The conservative
USD maximum is `(C - 1) * max(q_in, q_out) + I*q_in + O*q_out`, rounded upward.
The existing independent token terminator enforces this bound on completed
logical responses, including the crossing call. A Phoenix sidecar rederives any
such declaration found in its linked result ledger. Older records without a
declaration remain unknown. Observed calls exceeding request maxima, calls after
the token crossing, or a total above the derived ceiling mark the declaration
`violated` and block another campaign attempt. The actual spent estimate remains
recorded; a protocol violation does not erase cost. Hidden retries and failed requests without returned
usage are outside the estimate; it is not a provider billing hard cap. The
campaign stop-loss can cross its threshold by one bounded run because it checks
before the next attempt rather than reserving funds.

Provider references: [LiteLLM spend tracking](https://docs.litellm.ai/docs/proxy/cost_tracking),
[budget enforcement](https://docs.litellm.ai/docs/proxy/users), and
[custom pricing](https://docs.litellm.ai/docs/proxy/custom_pricing).
