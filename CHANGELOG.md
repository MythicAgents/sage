# Changelog

All notable changes to Sage are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Sage uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, behaviour and configuration may change
between minor releases.

## [Unreleased]

### Breaking changes

- **The OpenAI provider now uses the Responses API by default on OpenAI's own API.** With **OpenAI API** blank in the
  chat, the `SAGE_OPENAI_API` user secret and the environment, a chat whose provider is **OpenAI** and whose endpoint
  is OpenAI's own (`https` on `api.openai.com` or one of its subdomains, such as a data-residency host) now sends
  Responses: stateless (`store: false`), with encrypted reasoning replayed from Sage's history and the Daybreak
  program named (**Off** unless you choose Blue or Red). It used to send Chat Completions. The endpoint is
  **API Endpoint**, else `OPENAI_API_BASE`, else an enabled `LANGSMITH_GATEWAY`, else `OPENAI_BASE_URL`, else
  `https://api.openai.com/v1`. An existing chat keeps its conversation and continues on Responses. **To keep Chat
  Completions, set OpenAI API to Chat Completions** (in the chat, the `SAGE_OPENAI_API` user secret or the
  environment). A chat with provider OpenAI and any other endpoint (a proxy, a gateway or another server) is
  unchanged and stays on Chat Completions. An explicit **OpenAI API** always wins, and Sage still never leaves the
  choice to LangChain's OpenAI adapter, which picks Responses on its own for some models (`-pro` and `codex` models,
  `gpt-5.6-sol`, and any `gpt-6` model with tools) and would send none of Sage's Responses settings.

### Added

- **LiteLLM Gateway** and **Custom (OpenAI-compatible)** providers, for Sage and the Sage Watcher. Choose LiteLLM
  Gateway for a LiteLLM gateway (**API Endpoint** is the gateway URL, **API Key** the gateway key, **Model** the
  gateway's model alias) and Custom for any other OpenAI-compatible server. Both use the same OpenAI-compatible
  adapter as OpenAI and default to Chat Completions; **OpenAI API** can still select Responses, though not every
  gateway or server supports every Responses field. Both need **API Endpoint**: without it Sage refuses the chat
  before any request, so a gateway key is never sent to OpenAI. Reasoning Effort, Provider Retry Policy, OpenAI API,
  OpenAI Daybreak, OpenAI Safety Identifier, resource metering and the strict task-tool schema apply to all three
  providers. Existing `provider: openai` chats that point at a gateway keep working unchanged; switching one to
  LiteLLM Gateway starts a new chat.

### Changed

- Provider-rejection notices and the model debug log name the API a request used and where it came from, for example
  `Responses (default) for OpenAI's API` or `Chat Completions (default) for LiteLLM Gateway`. A request that was already
  on Responses is never advised to set OpenAI API to Responses.

- **Sage now requires Mythic's `Mythic-v4.0.0` branch at commit `d8a94943` or later.** Sage moves to
  mythic-container 0.7.0rc11, which leaves the channel out of a channel-metadata update; Mythic takes it from the
  request being answered. An older Mythic server cannot place those updates, so Sage's channel chips and the Sage
  Watcher's applied profile would not be saved. This minimum stands until a Mythic release tag contains that commit.
- The Sage Watcher records its applied profile as a **Watcher** chip in its owner channel, because Mythic now keeps
  only chip items from a metadata update. A Watcher applied before this upgrade keeps running after a restart, and
  the next `/watcher apply` moves it to the chip.
- The Watcher's bot token check accepts the stored grants Mythic now writes. Mythic stores a token's implied reads
  (`chat.read`, `eventlog.read`) with the scopes it was created with, and Sage used to require the stored grants to
  equal the nine documented scopes, so every Watcher scan stopped as `unconfigured`. The stored grants must now hold
  the nine and nothing beyond those two reads; the effective scopes must still equal all eleven exactly, so the
  Watcher's authority is unchanged.
- **Reissue the Watcher's bot token with `payload.read` added.** Mythic now checks every table a token's request
  reads, and the Watcher's scan reads each callback's payload type, so a token without `payload.read` stopped every
  scan as `degraded` with a transport error. Sage now names the missing scope and stops the scan as `unconfigured`
  instead. The scan no longer reads the operator who issued each task (that would need `operator.read`, which is not
  limited to the operation); task records stored earlier keep it. The token is created with ten scopes and its
  effective scopes must equal all twelve exactly.
- The OpenAI and Anthropic SDKs now connect to providers through httpx2. Provider TLS trust is unchanged when
  Sage's `certs/bundle.pem` or `SSL_CERT_FILE` is set. With neither, the default provider connection trusts the
  operating system's certificate store instead of the `certifi` bundle.
- A provider error that Sage does not explain itself (it does for configuration rejections, an unusable model and a
  context overflow) changes in two ways. For OpenAI it shows the status and the provider's message without the
  error code and parameter, because the OpenAI SDK changed how it words its errors. For OpenAI and Anthropic its
  title names LangChain's error class, for example `OpenAIInvalidRequestError` rather than `BadRequestError`.
  Sage's own notices are unchanged.
- Sage's MCP client moves from the retired `langchain-mcp-adapters` to `langchain.mcp` on MCP 2 and FastMCP, for
  BloodHound and for servers added with `/mcp connect`. Tools, results and the BloodHound ingest-status read behave
  as before. HTTP MCP servers now connect through httpx2 with the same TLS trust (`certs/bundle.pem` or
  `SSL_CERT_FILE`). `/mcp connect` now refuses three settings the new client cannot honor, instead of accepting
  them: `session_kwargs` other than `read_timeout_seconds`, a stdio `encoding` other than UTF-8, and
  `terminate_on_close: false`.
- In supervised mode, the result the agent receives for an action you reject now starts with LangChain's
  ``User rejected the tool call for `<tool>` with reason:`` before Sage's own message, which is unchanged. Sage
  still records the action as rejected, not executed, and ends the step. A conversation paused on an approval card
  before this upgrade still resumes with your latest guidance once.
- LangGraph moves from 1.2.22 to 1.2.24 and `langgraph-prebuilt` from 1.1.0 to 1.1.3, the current releases. In
  supervised mode, a step handed to the same specialist as the step before it still starts with only its own step
  and your request, and the earlier step's result still reaches the Supervisor.

### Security

- Four of the six dependency advisories listed under 0.4.0's known issues are cleared. `dnspython` moves from 2.9.0
  to 2.9.2 and OpenTelemetry from 1.45.1 to 1.45.2 (instrumentation and semantic conventions to 0.66b2).
  `pydantic-ai-slim`, which Phoenix uses, moves from 2.31.1 to 2.67.0; that needs the newer model SDKs, so `openai`
  moves from 2.54.0 to 3.58.0, `langchain-core` from 1.5.3 to 1.6.10, `langchain-openai` from 1.4.2 to 1.7.2 and
  `langchain-anthropic` from 1.5.4 to 1.7.7.
- `strawberry-graphql` moves from 0.320.4 to 0.327.7 through Phoenix 20.21.0 (from 19.19.0), which the new MCP
  client makes possible. Phoenix 20.21.0 caps `anthropic` below 2, so `anthropic` moves from 0.125.0 to 1.17.0, the
  newest 1.x release. LangChain moves to 1.4.6 and LangGraph to 1.2.22.
- The last two advisories are cleared: `dynaconf` moves from 3.2.5 to 3.2.13 and `ujson` from 5.9.0 to 5.13.0,
  through mythic-container 0.7.0rc11 (from 0.7.0rc9).

## [0.4.0] - 2026-11-23

Sage 0.4.0 keeps a chat going across a Sage restart, adds OpenAI's Responses API and Daybreak settings, switches a
chat's model in place, and binds each supervised catalog step to an action that current evidence supports.

### Added

- **Chats continue after a Sage restart.** When Sage restarts with its database kept, an existing channel continues
  on its next request with its conversation, `/mode` override and engagement binding, and Sage says so in the
  channel. Nothing that grants authority carries over: the request's Mythic identity and configuration must match
  what the session was saved under, and secrets are resolved fresh. When Mythic sends the container's startup
  message, Sage settles the requests the restart left open, without a model call or a task. A card still waiting
  stays open for your decision. A card that already shows your decision gets one **Continue saved decision / Stop**
  card in the same request; the decision is applied once, by its own delivery or by Continue, and only after Sage
  re-checks it against the card, the session and the paused action. Stop, or Cancel on the request, discards it.
  Any other open request is closed with a notice. A resumed request keeps its tool and sub-agent cards, step budget
  and in-request denials. An approved action the restart interrupted is never resent: your next message gets the
  exact Mythic task it produced, or is told its outcome is unknown. A chat still needs a fresh start after it ended
  on purpose (stop, cancel, an error, or a changed configuration or token) or when its saved record cannot be read,
  and Sage names the reason. Mythic sends the startup message only to an operation with an active bot account;
  without one, use Cancel on the open request in Mythic to free the channel. A restart never restarts autonomous
  work. Sage keeps each channel's record, with no secrets, in two new `sage.db` tables
  (`sage_chat_channel_sessions` and `sage_chat_channels_without_session`).

- **OpenAI Responses API, Daybreak and Safety Identifier.** Three new chat settings for the OpenAI provider, each
  resolved from the chat field, then a user secret, then the environment: **OpenAI API** (Chat Completions or
  Responses; blank keeps the adapter's usual route), **OpenAI Daybreak** (Off, Blue or Red) and **OpenAI Safety
  Identifier** (an optional stable pseudonym, sent as `safety_identifier`). Models served only on Responses, such as
  OpenAI's -cyber models, now work: keep **API Endpoint** as the base URL and Sage adds `/responses`. Responses
  requests are stateless (`store: false`); Sage sends the conversation and the model's encrypted reasoning itself.
  On Responses, Sage always names the Daybreak program and sends Off unless you choose Blue or Red, so a project's
  own default program is never used implicitly. Any Daybreak value, Off included, requires **OpenAI API =
  Responses**, and all three settings require the OpenAI provider; an invalid combination fails before any
  request. Left unset, they change nothing in existing OpenAI and proxy requests.

- **Provider rejections that say what to change.** When the provider refuses a chat's route, model or reasoning
  effort (for example a Responses-only model on Chat Completions, a Daybreak access error, function tools with a
  reasoning effort on Chat Completions, or an effort the model does not accept), the error names what Sage sent
  and where each value came from, the setting to change, and whether that change continues the chat. A fix made in
  the OpenAI project without changing Sage's settings is retried in the same chat. **Reasoning Effort** is also
  declared as a Mythic user secret, matching its documented fallback.

- **Switch a chat's model in place.** Changing **Model**, clicking the Model chip or running `/model <name>`
  continues the same conversation with the new model; `/model` alone shows the model and where its value comes
  from. Earlier messages, tool calls and results carry over, while the previous model's opaque reasoning is not
  sent to a different model. `/model <name>` writes the chat's Model setting through Mythic, so only the channel's
  creator or an operation lead or admin may change it. A model the provider rejects before it answers leaves the
  chat as it was, with an error naming the model, its source and the fix, so a wrong name is corrected in place.
  Changing **OpenAI API**, **OpenAI Daybreak**, **OpenAI Safety Identifier** or **Reasoning Effort** also continues
  the chat; a new API key, provider or endpoint starts a new one. A change made while an approval card is waiting
  applies only after the card is answered, by the model that raised it. The Model chip replaces the static Model
  setting chip.

- **BloodHound availability on its chip.** The BloodHound chip shows one of four states, with a matching color and a
  one-line reason in its tooltip: not configured, unavailable, degraded (reads answer but ingest does not) or
  connected. After a failed connection the reason includes "Last failure: …" on every later turn. The "BloodHound is
  not connected" notice no longer repeats with each new session; Sage sends one notice per channel only when a
  connection that channel saw working is lost. When the BloodHound agent is reached while BloodHound is not
  configured or cannot answer, it explains that state and says no graph data was read, instead of letting its tools
  fail. A credential set that failed to connect is still not retried until it changes.

- **Coding-agent connection (`/acp`, experimental).** A chat channel can connect to an optional Codex worker over the
  Agent Client Protocol with `/acp connect codex`, list and select its models with `/acp models` and
  `/acp model <id>`, show its status with `/acp` and close it with `/acp disconnect`. This release covers connection
  and model selection only: Sage sends the agent no prompt, advertises no filesystem or terminal capability, passes
  no MCP servers, opens a read-only session and refuses permission requests. You build and start the worker
  yourself with Docker Compose beside the Sage container, following `Payload_Type/sage/container/codex/SETUP.md`,
  and point Sage at it with `SAGE_ACP_SOCKET`; Sage never starts containers. Each channel holds its own connection,
  and only the chosen profile and model are saved (new `sage.db` table `sage_acp_channel_bindings`), so after a
  restart a chat shows `disconnected` until it reconnects. `/stop` also closes the channel's connection.

- **Operator-named DCSync accounts and re-verification.** In ordinary supervised chat (Autonomous Solve off), a
  message containing `dcsync` can name the accounts to DCSync as `account@domain`, `DOMAIN\account`, or a bare name
  when one domain in scope fits. Each is offered when the ledger, replication or admin rights and a live callback
  support it. krbtgt, machine accounts and groups are left out, and a bare name more than one domain could hold gets
  a request to qualify it. An explicit re-verify (`DCSync <account> again`, `Re-verify <account> with DCSync`)
  offers a refresh, and the account counts as proven only when a task from that request proves it anew. A plain
  request for an account already recorded now cites the proving ledger row and task instead of "not supported by
  current evidence".

- **Account Kerberos context from reviewed credential material.** A DCSync whose Mythic task failed only in its
  completion function records the account's credential material, not a working credential. Ordinary supervised
  chat now offers the account Kerberos context step from that material when its stored review cites a Kerberos
  key. The approval card names the material, the account and the callback, and says the material is not proved to
  work. On Accept, Sage re-reads that exact task's output and uses the key the review cited; a changed or withdrawn
  source stops the step before any task. Only the procedure's own verifier records the context, and the material
  never becomes a working credential.

- **Operator-named proof host.** A supervised forge-golden-ticket or Kerberos-context step can carry a proof host
  from the Supervisor's brief when one of your messages in the chat names that host and it lies in the domain the
  step must prove. The approval card shows it, and the approved call runs its service proof there. Any other host,
  including a domain's own name, is dropped without refusing the step, and the step outcome says why. Before, a
  forge for a domain whose controllers BloodHound did not know was approved and then refused with no task.

- **Sage's verification line on a closing.** When a supervised step's catalog procedure ends partial, blocked or
  failed and no later step in the request achieves it, the closing reaches you led by a line Sage writes from its
  own task records: the capability and target, the tasks, the verdict, the effects recorded and those not
  confirmed, and the Mythic status of any task that did not succeed. The model's narrative follows unchanged.

### Changed

- Each supervised specialist now carries its own step limits in its prompt frontmatter (`step_bounds:` with
  `tool_calls`, `active_seconds` and `consecutive_errors`); the shipped values are unchanged at 20, 600 and 3. A
  valid `SAGE_STEP_MAX_*` value still overrides a limit for every specialist; an invalid one is now ignored rather
  than rounded, and a malformed frontmatter block stops Sage from building its agents. A step's outcome records
  the limits it ran under and where each came from.
- A supervised specialist is told, on each model call, how many tool calls and how much active time its step has
  left. A batch of parallel tool calls runs only as many calls as the step has left; the rest are removed before
  anything runs and counted in the outcome.
- A step that reaches a limit no longer stops mid-thought: the specialist gets one last model call with no tools
  offered (at most 30 seconds) and its answer goes to the Supervisor unchanged. If that call returns no text,
  fails or runs out of time, or if its answer would not survive Sage's supervised filtering intact, Sage sends a
  notice saying so instead, and nothing more runs in that step. The step still ends as `budget_exhausted` with the
  limit it reached, and its partial answer never counts as a finished answer when the Supervisor sends the same
  step again.
- **Catalog steps bind to a current candidate.** In ordinary supervised requests the Supervisor sees the catalog
  actions that current evidence supports (the ledger, the live footholds Sage reads from Mythic's active callbacks,
  the account contexts and the request's attempts), and a catalog step other than graph collection must select
  one of them. The specialist then proposes exactly that action without a model call. A selection Sage cannot bind
  ends the request blocked before any card or task, saying that other work remains possible. After Accept, and
  before any task, Sage checks the action again, so support withdrawn or bindings changed while the card waited
  issue nothing. The targets the Supervisor writes beside a bound step are recorded rather than enforced, so their
  wording no longer refuses a valid action, and a step that lists `execute_capability` as an allowed tool is treated
  as a catalog step. The step outcome names the bound procedure and any listed procedure the step does not run. A
  forge brief may name a proved krbtgt row, or several rows of one key set, and binds it even when another key
  ranks higher. Conversation, Autonomous Solve and auto are unchanged.
- **A plan that needs a missing input is refused before its first task.** The capability executor builds the
  command plan first and refuses it when a step it would reach needs an input nothing supplies, for example a
  Kerberos context or forge with no proof host and no domain controller BloodHound knows. The report names each
  missing input and what supplies it and ends with "No task was issued."; nothing is written. Before, the procedure
  ran its preflight, forge, logon and ticket import, then failed at the service proof.
- **Conversation models see only the tools they can run.** In conversation mode, guarded tools, Mythic file staging
  and guarded MCP tools are removed from the model's tool list before each call, by the same rule that refuses
  them, so the model no longer spends calls on tools it cannot run. Other modes keep their menus, and a model that
  names an omitted tool is still refused before anything reaches Mythic.
- **A BloodHound answer ends the request.** In supervised mode, once BloodHound has answered without changing
  anything, Sage dispatches no further BloodHound analysis step in that request: the first re-ask gets the answer
  back, and a second ends the request with it. The request ends as completed when at least one graph read
  succeeded and as a checkpoint otherwise; a BloodHound read that returned an error result no longer counts as
  successful.
- **Supervised closings with no step behind them.** In ordinary supervised chat, a closing that carries one of
  Sage's own step-report forms (the worker completion header, the task-observation wrapper, the step-outcome line
  or the executor-verdict line) when no step ran in the request is withheld behind a fixed notice: Sage has no
  completed step recorded for the request, so it cannot report an execution result. The turn is recorded as
  blocked. A genuine answer with none of those forms, such as an orientation reply or a graph fact, still reaches
  you. The model's text beside a hand-off to a specialist is no longer streamed; the delegation card shows the
  routing.
- **A scope-refused proposal is named.** When a step's scope removes a proposed guarded action before its card, the
  step outcome now names the tool, command or capability, the callback and the refused scope. If re-sending that
  scope ends the request, the closing lists the proposal as neither approved nor executed. No arguments are copied.
- **Credential-store proof needs the source task's own value.** Mythic keeps a credential row's task link when
  someone edits the row, so a completed source task no longer proves the row on its own: the row's current text
  must also be a value that task produced or imported (one of its parameters, a value the credential parser reads
  from its output, a whole token in that output, or a string value of JSON the task printed). An edited row binds
  no forge key and records no achieved ledger row, and reconcile says why. Rows with no task link and rows already
  recorded as achieved are unchanged.
- **Supervised specialists see the reply your follow-up refers to.** Every supervised specialist call now carries
  Sage's previous reply in the chat as one labelled data block before your exact words, so a follow-up such as "do
  option B" reaches the specialist with the menu it answers. Earlier execution reports reach the supervised model
  as attributed, unverified records rather than in its own voice, which makes it less likely to imitate them.
  Stored messages are unchanged.

### Fixed

- An approved supervised action is no longer issued twice after a crash (a 0.3.0 known issue). Sage saves its claim
  on an approval before resuming it, and the Mythic task id as soon as Mythic returns it; after a crash a claimed
  approval is never resumed, and your next message gets the exact task or is told its outcome is unknown.
- A request's final message is created after the delegations it closes, so it no longer appears above earlier
  sub-agent panels (a 0.3.0 known issue).
- A forged-ticket request keeps the target domain it names through every planning pass. A parent-domain forge from
  a child-domain source now checks the parent's replication grant and the ExtraSIDs source against the issuing
  domain, and a cross-forest request is forged for the domain it was approved for (a 0.3.0 known issue).
- A krbtgt credential reference binds the named proved row even when another key ranks higher (a 0.3.0 known
  issue). When nothing is proved for the domain, the refusal says so instead of calling the reference stale.
- A deferred task still settling in Mythic ("Completion Function Running…") is no longer reported as plain
  completed. Sage keeps re-reading it within its bounded window; a task still settling when the window ends is
  reported with Mythic's status and a caveat that what its completion function registers may not be in Mythic yet.
- An accepted `upload_file_by_file_uuid` in supervised mode now issues its task. It was refused as not covered by
  the approval when schema resolution renamed an argument or the proposal carried an explicit null timeout.
- A DCSync that the domain controller refuses after resolving a domain-qualified account is reported as
  replication access denied instead of a bad DN. A step Sage refused before Mythic created a task is named on its
  own "Not issued" line, and the "Task IDs" line lists only tasks Mythic created.
- A SharpHound run that finishes and writes its archive while printing errors for some collection methods is now
  retrieved and ingested; the executor report keeps those diagnostics and says not every collection method is
  proven. Every collection exit now reports a verdict and the phase it reached.
- An approved call that fails after the approval resume closes its own tool card with its real result, instead of
  leaving the card open and ending the request in error after the work had finished.
- When the Supervisor finishes a turn, that turn's tool calls, results and reasoning stay in the chat's history, so
  a reasoning model on Responses keeps its earlier reads after a settings correction or a restart.
- A task whose output Sage read without its Mythic record shows its status as not read, instead of "no status (not
  completed)".
- The README, the `.env` header and the prompts README say where Sage's files appear in Mythic's container file
  browser, that an edited prompt takes effect on the next new chat without a restart, and that a `.env` change
  needs `sudo ./mythic-cli restart sage` on the Mythic host.

### Security

- `pyasn1` moves from 0.6.4 to 0.6.5, clearing four advisories against 0.6.4.
- The token Mythic gives Sage's startup handler is now also used to read the operation's open chat requests and
  settle those a restart left open. It never runs a task or a model call and never applies a decision. See
  [Security and Data Handling](docs/SECURITY_AND_DATA_HANDLING.md).
- Direct OpenAI Responses requests are a new data flow: they carry the chat's content as before, plus the
  configured safety identifier and Daybreak program, and are sent with `store: false`.
- The optional Codex worker's API key sits in `container/codex/.env`, readable by anyone with Mythic access to
  Sage's files and, inside the worker, by anyone with Docker access, as with the key in Sage's own `.env`.

### Upgrade

- `mythic-cli install` replaces Sage's whole service directory, including `.env`, `sage.db`, `.phoenix/` and
  `container/codex/.env`. Archive or copy what you need first (see the README, "Runtime state and clean resets").
  A chat started on 0.3.0 continues on 0.4.0 only when its `sage.db` is kept and Mythic's own request rows match
  the saved prompt of exactly one conversation; otherwise Sage names the reason and asks for a new chat.
- Assign an active bot account to each operation that uses Sage, so Mythic sends the startup message that lets
  Sage settle requests a restart left open. Without one, use Cancel on an open request to free its channel.
- A prompt file without a `step_bounds:` block runs with the shipped limits. Check any `SAGE_STEP_MAX_*` value you
  set: an invalid one is now ignored instead of rounded.
- The new `sage.db` tables are created on first start; there is no migration step.

### Known issues

- **macOS Docker Desktop and OrbStack.** Unchanged from 0.2.0: SQLite cannot lock files on the bind-mounted
  service directory there. Set `PHOENIX_WORKING_DIR` and `SAGE_DATABASE_PATH` as described in the README section
  "SQLite on macOS Docker Desktop / OrbStack"; with that setting the databases do not survive a container rebuild
  or reinstall.
- **Dependency vulnerabilities.** `pip-audit` reports advisories in six shipped packages. `dynaconf` and `ujson`
  are pinned by `mythic-container`, and `strawberry-graphql` by `arize-phoenix`; they clear when those projects
  update their pins. The fix for `pydantic-ai-slim` needs newer OpenAI and Anthropic SDKs than Sage's LangChain
  integrations accept, so it waits for that upgrade. `dnspython` 2.9.2 and `opentelemetry-api` 1.45.2 fix the other
  two and are deferred to after this release.
- **Plain-prose claims are not withheld.** Sage withholds a supervised closing with no step behind it only when it
  carries one of Sage's own step-report forms. A closing that claims a result in plain prose still reaches you, so
  check the tasks in Mythic before relying on a reported result.
- **The Supervisor does not yet plan supervised steps.** It can hand a specialist the whole objective instead of a
  bounded subgoal, so a step runs until one of its limits or an error, and the Supervisor's next choice can rest on
  an incomplete handback.
- **Repeated and empty work.** A request can propose a BloodHound collection again after a successful ingest on
  the same callback and scope; reject the repeat. An "already achieved" capability verdict can be followed by
  empty delegations that end in the no-progress stop.
- **Request endings.** A task whose Mythic completion function failed after the agent answered, or whose output
  arrives only at handback, can end the request as "not terminal". A delegation interrupted between its start and
  its end can end the request in error even when its work is usable. In both cases the tasks and their output in
  Mythic are the record of what ran.
- **Chat display.** The internal `<untrusted_delegated_assignment>` delegation envelope can appear in the visible
  transcript.
- **The ledger does not record every effect.** It records gains that a verifier or a rule in code proves. Work done
  through generic commands, such as `powershell`, `shell` or an assembly no verifier models, leaves no row unless a
  verifier recognises its effect, so a short ledger does not mean nothing was gained.
- **The coding-agent connection is experimental.** `/acp` connects and selects a model only; no coding happens.
  Chats show `disconnected` after a Sage or worker restart until someone reconnects, and reinstalling Sage deletes
  the worker's `.env` and socket directory (see `container/codex/SETUP.md`, "Before you reinstall Sage").
- **Auto mode** remains experimental. See Validation.

### Validation

The maintained offline suite, the repository-boundary guards and the repo-hygiene checks pass on this release, and
the changes are covered by scripted tests of the assembled chat path, with recorded model output replayed through
the real consumers where a change touches what a model reads or writes. Individual changes were spot-checked live
on a GOAD lab during development; those checks are not a release qualification. Any live run this release
receives is reported in the GitHub release notes for 0.4.0, with its image, architecture, Mythic version, model
route, objective and result, and that result covers only that configuration and objective. See
[README Limitations](README.md#limitations) and [Security and Data Handling](docs/SECURITY_AND_DATA_HANDLING.md) for
the complete boundaries.

## [0.3.0] - 2026-11-11

Sage 0.3.0 bounds and scopes each supervised step, reads every step's results from Mythic rather than from the
specialist's summary, and records proven progress in the Engagement State Ledger.

### Added

- When the Supervisor scopes a supervised step to a capability, it now sees what each capability does. The
  delegation tool lists every catalog capability with its effect, the state it already needs, and when to pick it
  over its neighbours, generated from the capability catalog so a new capability appears automatically and nobody
  maintains a second copy. A specialist that declares no guarded tools of its own no longer receives the
  step-scoping fields it could never act on.

- In supervised mode each specialist step is bounded. A step returns control to the Supervisor after its one
  guarded action has run or been rejected, or earlier at 20 tool calls, 10 minutes of active time, or three
  consecutive tool errors. Time awaiting an operator's approval and time inside the guarded action itself,
  including the Mythic task wait, never count as active time. After its action the specialist may still read and
  summarize the result; a further guarded proposal in the same step is removed before an approval card is shown,
  so it needs a new step. Sage then gives the Supervisor a typed outcome for the step: the action ran, was
  rejected, fell outside the step's scope, a limit was reached, a task is still pending, or the specialist
  finished on its own, with the tasks issued and the time spent. Reaching a limit is not a failure; the
  Supervisor chooses the next step, and returning control never cancels a task on the target. Set the limits
  with `SAGE_STEP_MAX_TOOL_CALLS`, `SAGE_STEP_MAX_ACTIVE_SECONDS` and `SAGE_STEP_MAX_CONSECUTIVE_ERRORS`.
  Autonomous mode and conversation requests are unchanged.

- A supervised step's outcome now references everything the step did, read by Sage from Mythic rather than taken
  from the specialist's summary: each task it issued with its recorded state, command, callback and output row
  count; the credentials Mythic links to those tasks or that the step added, by account, realm and type only (never
  the secret); and the graph reads it made. Every issued task reaches the Supervisor with an output excerpt or a
  stated reason it has none (still pending, no output, read failed, or over the excerpt budget). Sage reads an
  excerpt only for a task the specialist did not read itself, within 8,000 characters per step, so output that
  lands after the specialist looked is delivered without another delegation. At most 16 tasks per step are read;
  any beyond that are still listed.

- Supervised steps now record what they proved in the Engagement State Ledger, which tracks progress rather than
  every task. A new callback, whether on a new host or as a new user, records a session on that host. A callback
  at high or SYSTEM integrity, or one whose integrity rises after a step's task on it, records admin or SYSTEM on
  that host.
  A credential whose source task belongs to the step is recorded by account and realm, never with its secret.
  A callback gain is credited to the Sage task issued before the callback first checked in, within 10 minutes
  (`SAGE_STEP_LEDGER_ATTRIBUTION_SECONDS`), so a payload that checks in after its step ended is still credited.
  Each row is proven against Mythic's own callback record or the credential's source task; a gain credited to a
  task that ended in error is kept unproven. A proven row is never downgraded by a weaker observation, and a
  repeated gain updates its row. Losing a callback or changing a token records nothing, and a gain no Sage task can
  be credited with is reported but not recorded. Autonomous mode and conversation requests are unchanged.

- A supervised delegation to the Mythic operator carries a brief that Sage checks before the specialist starts:
  the callback, the hosts, domains or principals the step acts on, the result it should produce, and what its one
  guarded action may use, either catalog procedures or the specialist's guarded tools. The callback must be one
  Sage has observed (and the request's bound callback when it has one), targets must be named by the operator or
  already known, and referenced tasks and credentials must be records Sage holds. A failed check returns the
  reasons to the Supervisor; a second consecutive failure ends the request and tells the operator why. A step
  that only reads Mythic or BloodHound records declares itself `control_plane` and needs no callback, but may not
  propose any guarded action. The specialist still receives the operator's exact words, and before it acts Sage
  tells it what its one guarded action may use, its callback and its targets. The specialist is also offered only
  the guarded tools its step admits, and a step scoped to catalog procedures is not offered technique guidance,
  since Sage runs the procedure; the step boundary still removes any out-of-scope call. When a step is scoped to
  graph collection alone on a callback, with at most one target, Sage proposes that `execute_capability` call
  itself instead of asking the specialist's model to choose again: the callback binds it, a target domain is passed
  through, and without one (or when the target names a host) the collector uses the callback user's current
  domain, so nothing about the network needs to be known in advance. The call reaches the same approval card and
  step checks, and the specialist's model still reads the result. A step with several targets or procedures is
  left to the specialist. When a step scoped to a
  catalog procedure calls a tool the step does not offer, the step-boundary message names the step's procedure and
  tells the Supervisor to re-send the scope if that procedure is what the operator asked for, or else re-scope to
  what the operator asked for; it never suggests widening a procedure step to raw tasking. Such a deviation may be
  retried once under the same scope, and a second ends the request. A step whose `execute_capability` call names a
  capability outside its scope has the wrong scope, and re-sending that scope unchanged ends the request at once.

- The Supervisor can list callbacks itself in conversation and supervised mode, so "which callbacks are alive"
  is answered without a delegation and a brief can name a callback the Supervisor has seen.

- Every delegation outside the capability catalog, that is every step scoped by its guarded tools rather than a
  catalog procedure, is recorded with its title, assignment, expected result and brief, so later reviews can find
  recurring procedures worth adding to the catalog. The capability choices offered to the model come from the
  catalog itself, and a name that is not a catalog procedure is refused.

- In supervised and conversation mode, a request to reach privilege on or take control of a target now consults
  the BloodHound graph before Sage chooses a specialist. Sage reads which domains the graph has collected and,
  for each target domain the graph covers, whether a path exists from the principals Sage controls. It then
  hands that result to the Supervisor as context: covered, partially covered (naming the domains that were never
  collected), no graph, or BloodHound unreachable. A target named without its suffix ("essos") resolves only
  against domains Sage has observed. The read cannot block a request: if BloodHound is down or slow, Sage
  proceeds and says no graph analysis was available. "Administrative control", "full control" and "DA" now count
  as privilege goals.

- `list_staged_tools`, a read-only tool for the operator agent, and `/tools staged` in chat list the binaries in
  Sage's `tools/` drop zone. Before, Sage found a staged binary only when a TTP named its exact filename. Each
  file shows its sha256, its type and architecture read from the file header, the TTPs that document it, and
  whether Mythic already holds the same bytes, so Sage can use that copy without asking to upload it again.
  Renamed and per-architecture builds (`new_sharphound_x64.exe`, `SharpHound.o`) match their tool. A binary no
  TTP documents carries a hint to ask the operator what it is for, and a tool that is staged nowhere returns a
  message asking the operator to add it. `ensure_tool_uploaded` names staged variants when the exact filename
  is missing.

- Every Supervisor and Mythic_Operator model call, in every mode, carries a fresh, bounded view of the
  Engagement State Ledger, read from the store just before the call and never kept in chat history or a
  checkpoint. A new chat starts from what the operation has already proven, and a `/state` correction shows on
  the next call.

- Two model tools work with the Engagement State Ledger. `read_engagement_state` reads rows by number or page,
  and `submit_engagement_evidence` sends up to 20 Mythic task, `cred:<id>` or `file:<id>` citations to the same
  verifiers `/state reconcile` uses. The model never writes, retracts or restores rows itself.

- Rows in the model's ledger view name each source's kind (`cred:<id>` for a credential-store record, `file:<id>`,
  or `unknown:<value>` for a legacy citation), so a credential ID is not read as a row number.

- When a conversation or supervised turn ends, Sage reconciles the operation's completed Mythic tasks from every
  callback and every issuer, one bounded batch per turn, so work an operator ran by hand is recorded once a
  verifier proves it. `/state reconcile` runs the same pass on demand, and `/state reconcile` also accepts
  `file:<id>` citations.

- A rule written in code can record an achieved row derived from rows that already have proof: a domain-wide
  Kerberos context from a callback-scoped one on a live callback, and replication rights from proven Domain
  Admin or Enterprise Admin with a matching context or foothold, or from SYSTEM on a governed GPO linked to the
  domain. The derived row names its rule and premise rows, and a model's inference never counts.

- `/state help` shows the `/state` usage without reading the ledger or calling a model.

### Changed

- A push to `main` now publishes the version image, for example `ghcr.io/mythicagents/sage:0.3.0`, alongside
  `latest` and the `sha-` reference, so the exact release image exists before the release tag. A `v*` tag no
  longer builds anything. It checks that the tag matches `agent_version`, that the tagged commit is on `main`, and
  that the annotated tag's `Image-Digest: sha256:<digest>` line is exactly what the version reference resolves to,
  with both architectures and revision and version labels that match the tag. Only then does a final release
  point its minor reference, for example `:0.3`, at that same manifest. Once a version is tagged, later `main`
  builds leave its reference alone until `agent_version` changes.

- Staging a tool binary into Mythic's file store (`ensure_tool_uploaded`) no longer asks for its own approval in
  supervised mode. It writes only Mythic's operation file store from the local tool drop zone and reaches no
  callback; the command that later runs the tool on a target still needs its approval. A task whose command
  selects a registered file still registers that file before it is issued. Staging stays unavailable in
  conversation mode and in a stopped request.

- `mythic-cli install github` now pulls the published release image from GitHub Container Registry instead of
  building Sage locally. Set `sage_use_build_context` to `true` in Mythic's configuration to keep building from
  source.

- The engagement ledger is now named the Engagement State Ledger and is one shared store per Mythic operation
  in `sage.db`. Every chat channel of an operation reads it fresh and writes only the rows it changes, so two
  sessions no longer erase each other's rows, and a `/state` correction reaches running sessions on their next
  read. Hop numbers are assigned once and never change or get reused: a ledger numbered 23–29 stays 23–29 and
  the next hop is 30.

- Rows are now `achieved` or `pending`. `failed`, `blocked` and `legacy_unverified` are no longer row statuses;
  attempts stay in Mythic's task history. A `pending` row, waiting for its effect or its proof, never satisfies a
  capability's precondition, and an achieved write without an admissible proof envelope is stored as `pending`
  (`proof_missing`).

- Hashes and tickets found in task output or Mythic's credential store are recorded as credential material. A
  material row proves possession, not a working credential, and does not satisfy a capability that needs one.

- `/state remove` now records a retraction (who, when and why) instead of deleting the row; `/state restore`
  undoes it and `/state invalidate` marks a row "no longer holds". Corrected rows stay listed with their numbers
  and are shown as `achieved — operator retracted` or `achieved — no longer holds`; no gate, capability input
  or completion check relies on them. `/state wipe` retracts every row and every current graph fact without
  deleting or renumbering anything.

- `/state set` no longer sets status text. It promotes one pending account-secret row to credential material
  when you cite where the secret sits in a Mythic task's output, only for a task whose completion function
  failed after the agent answered, and it never takes the secret itself.

- `/state reconcile` saves verified rows immediately, with or without `apply`. `apply` now only permits
  importing missing credential material into Mythic's credential store; without it, those imports are
  previewed, because task output is attacker-influenceable.

- `/state` and the end-of-turn table label the stored objective "Stored compatibility objective (operation
  setting, not a channel goal)", and `/state objective` describes it the same way.

- A once-proven achievement stays historical: a later observation adds a "no longer holds" marker with its
  evidence instead of rewriting the row to failed. Sage adds the marker automatically when a later complete
  read of the same directory object contradicts a directory change, when a working session on the same callback
  lifetime changes identity, or when a derived row's premise no longer holds.

- Evaluator observations are stored on the evaluation side (`SAGE_EVAL_OBSERVATIONS_DIR`), never in Sage's
  Engagement State Ledger or model context.

- A supervised objective request whose attack graph is empty, or lacks a domain the objective names, now starts
  with Sage's `collect-graph` step as its first proposal, on the observed callback, for you to approve or reject.
  With a partial graph the step is scoped to the first missing objective domain; with an empty graph it is scoped
  to the objective's domain when the objective names exactly one. Sage makes this proposal once per request; after
  you approve or reject it, or the collection fails, later delegations pass as written, so manual enumeration stays
  available. Conversation and auto modes are unchanged.

- A supervised request that asks only to collect one domain's BloodHound graph, such as "Collect BloodHound data
  for <domain> through the current foothold" or "Run SharpHound against <domain> on callback N", now runs as Sage's
  `collect-graph` procedure. Sage reads the callback inventory and proposes the collection itself, on the callback
  you named or the only active callback it has observed, even when a graph was already collected, and approving it
  runs a new collection. Nothing is bound when the callback is ambiguous, its payload type has no collection
  adapter, or no collection runner is installed. Questions, negations, several domains, other collectors, flags,
  output paths and follow-on actions are routed as before. Supervised Autonomous Solve, conversation and auto are
  unchanged.

- Guidance for a technique the catalog runs (SharpHound collection, GPO abuse, golden ticket) now returns that
  capability and its guarded executor instead of sending the worker to build Apollo commands by hand. When several
  references match a technique equally well, `get_ttp_guidance` lists up to eight candidates and asks for a more
  specific technique instead of returning the alphabetically first. A read-only call repeated with identical
  arguments and an unchanged result gets one no-progress answer; a further identical request is not run and the
  step hands control back.

- Each supervised worker delegation now starts with its own transcript. Earlier delegations stay in the worker
  channel for audit and in the Supervisor's evidence but no longer reach the next worker model call. What a later
  step needs arrives as Sage's data: one-line facts for the request's earlier steps, Mythic's own ids for results
  another worker produced for this worker's sub-delegation, and a reference of command schemas already read.

- Graph refresh records each domain controller in the collected graph with its domain, and a supervised step may
  target that observed host. A host that nothing has observed is still rejected, and the rejection names the
  read-only discovery that must come first.

- A supervised step may name a target you spelled out exactly in an earlier message of the same chat session, so a
  follow-up such as "Yes, run it" works. Earlier messages supply target names only, never a callback, task or
  credential; assistant replies, tool output and other channels never count. The worker still receives your newest
  message, and the guarded action still waits for its own approval card.

- Sage learns each domain's FQDN from the operation's own evidence: the domain named in the output of a Domain
  Admins proof, and BloodHound when a callback's account and host sit in one domain. A Domain Admin proof is
  recorded under that FQDN and footholds carry it; ambiguous pairs and built-in names such as Administrator teach
  nothing. `SAGE_ENGAGEMENT_NETBIOS_MAP` is now needed only to turn learning off (`off`) or to pin names with a JSON
  object.

### Fixed

- A supervised step whose specialist proposes, as its capability, something that is not a catalog procedure (a
  command name such as `whoami`, a misspelling, or nothing) is now treated as the specialist's mistake rather than
  a wrong scope. The proposal is still removed before any approval card. The Supervisor may re-send the same step
  once, as it already could after the specialist called a tool the step does not offer, and a second malformed
  proposal under that scope ends the request. A real catalog procedure that the step's scope excludes still ends
  that scope at once. Sage does not rewrite the proposal: the specialist must propose the step's procedure itself,
  and it reaches an ordinary approval card. Live, a step scoped to graph collection ended the whole request when
  its specialist proposed `whoami` as the capability.

- After you approve a card, the request's final report appears once. The resumed request used to leave the report
  as an unfinished block and post an identical copy below it whenever it closed a tool card opened before the
  approval.

- A specialist's proposal is shown once when its step ends with a refusal. When a step-boundary or
  task-preparation refusal was appended to text the specialist had already streamed, the closing card repeated
  the whole proposal, so it appeared three times. The card now adds only the refusal line; its summary keeps the
  full text.

- A task whose parameters do not fit the live command schema is repaired within the specialist's turn instead of
  ending it. A Mythic file UUID placed on a selector that chooses files by name, such as the one returned by
  staging a tool and passed to Apollo's assembly selector, now resolves to that file's registered name when it is
  a complete, live, operator-uploaded file the selector offers. Other schema refusals (a missing parameter, an
  invalid choice, the wrong parameter group) go back to the specialist under the existing per-request repair
  budget, and you see the refusal only once that budget is spent.

- The graph-collection summary names the tasks the collection issued and the graph effect it recorded. It used to
  say "none issued" and "no effects" after a collection that had run and been ingested.

- The Engagement State Ledger marks a GPO-abuse setup hop achieved once the same GPO transaction proves SYSTEM
  execution through that GPO. Previously `/state` kept the setup hop pending beside the proven execution and
  Domain Admin hops.

- A supervised step may now target a host or GPO that Sage knows from its own attack-graph facts. The scope check on
  a step's brief admitted only targets named in the operator's request, in callback listings, footholds or ledger
  hops, and the domains in graph facts, never the hosts and GPOs those facts name. A step aimed at the domain
  controller on a graph-confirmed GPO path was therefore rejected, even though the capability that performs the
  step finds that same controller from the same fact. The check now reads the GPO-to-host facts and admits the full
  host name and the GPO name (also in its `NAME@DOMAIN` form). A bare host label passes only when exactly one known
  host carries it, so a label seen in one domain is never admitted in another, and GPO GUIDs are still not
  targets.

- A worker step is no longer stopped before it starts by callback listings made earlier. The guard that ends a
  worker stuck re-reading the same Mythic data counted every `list_callbacks` read on the shared client,
  including the Supervisor's own reads while filling supervised briefs and reads from earlier steps. After six
  such reads with no task issued, each new Mythic worker step ended before its first model call and handed back
  nothing; the Supervisor re-sent the step until the no-progress stop ended the request. Each new delegated
  step now starts its own count, while a worker re-reading the same data within one step is still stopped at
  six reads. When the stop fires, the step's handback names the tool, the target, the count and the limit
  instead of returning empty.

- A supervised capability step now grounds its known context from engagement state and, when it still cannot be
  planned, returns to the worker instead of ending the request. The worker proposed a GPO SYSTEM-execution
  capability naming only the callback and GPO; the planner needed the controlled principal and the affected
  domain controller, which Sage already holds, and the request ended with a one-word refusal. Sage now fills
  those from the foothold and the graph before the approval card, so the operator approves a plan that is ready
  to run. When an input genuinely is not in state, the step is handed back with the missing input named in plain
  words, for the worker to supply or obtain, and the planner never invents a value.

- A graph collection that Sage proposes itself in a supervised step now opens its tool card. The collection ran
  end to end, but the call never passed through a model, so its card was never opened and the request ended in
  error when the card closed without having started. Sage's proposal now goes to the same capture callback a
  model's response does, after the step boundary has checked it.

- A supervised step re-sent with only its expected result reworded now counts as a repeat. Live, the Supervisor
  re-sent one step six times with the same title and scope, rewording the result it expected each time, and every
  rewording ran until the request was blocked. Repeats are now compared on the worker, title, capabilities, tools,
  callback, targets and inputs; a change to any of those is still a new step.

- The BloodHound attack-path probe no longer reports "no path" when it cannot place the controlled identity or the
  objective domain in the graph. BloodHound returns the same empty answer for a missing node as for a missing path,
  so an identity projected under the wrong domain read as "no path" to a domain it could reach. The probe now
  checks that each principal and domain exists before asking for a path, and says nothing when one does not. The
  once-per-objective probe also runs again when the controlled principals change.

- A supervised request no longer re-sends a step that already answered until the no-progress stop. When a step
  returns without issuing a task, adding a credential or meeting a denial (an answered read, a report that the
  operator must act, or an "already achieved" capability verdict) and the Supervisor sends that same step again
  unchanged, Sage does not dispatch it: it hands the step's answer back once, and a second repeat ends the request
  at a checkpoint with that answer. A changed step, or any step after a different one, still runs. The earlier
  repeat guard read state the Supervisor's handoff never carried, so it never fired.

- An approved supervised graph collection now collects even when the same callback identity and scope already have
  a verified graph. Before, collection preparation ran, then the collect-once gate skipped the collector and the
  approved collection was reported as failed. Rights granted on directory objects outside Sage cannot be seen
  until a new collection, so the operator's approval is the signal to collect again. The approval card says when
  the identity and scope were already collected. Without that approval the gate still skips a covered collection.

- A supervised step scoped to graph collection can now collect. Collection was offered as a capability but had no
  `execute_capability` procedure, so the specialist could only propose the collector command itself, which the
  step's scope removed before any approval card; the Supervisor re-sent the same scope until the request halted
  for making no progress. `execute_capability` for `collect-graph` now runs Sage's own collection, the same
  procedure autonomous mode uses: one approval card covers the identity checks, staging, the collector, the
  download and the BloodHound ingest, and each task it issues must match that approval's callback and domain.
  A raw command in a capability-scoped step is still removed before a card.

- A request that ends while a specialist's delegation is still open, for example when a capability
  cannot be planned or an identical re-proposal is stopped, now finishes normally. The open delegation
  failed the final consistency check, so a request whose work had finished ended as an error with its
  sub-agent card stuck on "running". Open delegations now close as stopped, as open tool calls already
  did, including one whose card was lost to an interrupted resume.
- A request's final message now appears after everything the request showed. It used to finalize the
  last main-window message in place, and Mythic orders a chat by when each message was created, so a
  checkpoint written into an early progress message sat above the sub-agent panels and tool cards that
  followed it. The final message reuses that message only while nothing newer exists; otherwise it is
  its own message at the end.
- A supervised request no longer stops at a "has not reached a terminal state" checkpoint when the task
  had already finished. The handback read the task once, and output that landed a moment later made a
  finished task look unfinished. It now re-reads for a few seconds and takes completion from the task
  record. A task whose agent completion function failed (Mythic's `Error: completion function` status) is
  reported as finished with output, with a note that the agent may not have registered credentials or
  files from it, and other `error:` statuses on a finished task are reported as failed rather than
  completed.
- Answered requests no longer end in a "no progress" stop. A BloodHound answer reached the Supervisor
  without the graph queries behind it, so the Supervisor treated a finished analysis as unsupported and
  delegated it again until the stop. The queries and bounded excerpts of their results now arrive with the
  answer, as Mythic task output already did. A Mythic_Operator re-delegation in the same request also ended
  before it ran, because an earlier delegation's finished capability result was taken as its own; only a
  result from the current delegation ends it now.
- A forged-ticket request for one domain that also named a different `source_domain` was treated as a
  request for the source domain, so it reported "already achieved" from the source domain's existing proof.
  The named domain is now the ticket's target and the source domain supplies the ticket.
- Sage can answer questions about the Engagement State Ledger rows you see. `/state` and the end-of-turn table
  were shown only to the operator, so a question such as "why is hop 1 pending?" had nothing to refer to.
  The Supervisor and Mythic_Operator now see the ledger, with the same permanent numbers, on every model call,
  so "hop 4" means the same row in `/state`, the end-of-turn table and the model's view. The end-of-turn table
  now uses the same columns and numbers as `/state`, and the `/state` count is labelled "Hops", since it
  includes pending rows.
- The container no longer runs Sage as root. It runs as the owner of the installed service directory, so the
  databases and state Sage writes there belong to that user, and a later `mythic-cli install` no longer fails
  on root-owned files and leaves Sage uninstalled. The first start hands back root-owned files left by earlier
  versions. Python bytecode caches are no longer written into the service directory.
- The Phoenix UI on port 6006 no longer freezes in the container. Sage's span exporter was the process-wide
  tracer, so Phoenix's own server exported the span for each request it handled, synchronously, to the
  collector that the same server thread runs, and stopped answering on its first request. Sage's spans now go
  to Sage's own exporter, with no change to what is traced. `kill -USR1` on the Sage process, or
  `docker kill -s USR1 sage`, now writes every thread's stack to the log, and startup logs which span
  exporter Sage uses.
- The startup version banner now appears in the container logs at Mythic's default log level
  (`DEBUG_LEVEL=warning`); it was logged at INFO and hidden.
- A request's final message now records how the request ended (`completed`, `blocked`, `checkpoint`,
  `stopped`, `cancelled`, or `error`) as `turn_outcome` in its metadata. Mythic stores only `complete`,
  `error`, and `cancelled` as a request's status, so a request Sage stopped on its own, or one that ended
  blocked, still shows as `complete` there; the message content and `turn_outcome` give the real ending.
- A channel in Conversation mode with Autonomous Solve on no longer switches itself to autonomous mode.
  Conversation stays read-only; Sage ignores the toggle there and says so once in the chat. Autonomous
  execution now comes only from Mode = Autonomous.
- Tasking tools are now enabled by the `task.write` scope, which Mythic requires to create a task, instead of
  `callback.write`. A channel token with `task.write` and no `callback.write` no longer loses tasking, and one
  with `callback.write` but no `task.write` no longer fails mid-run. `ingest_collection` only reads files from
  Mythic, so it now needs `file.read`, which `file.write` also grants. Sage's declared channel scopes now
  list `task.write`.
- `sandbox_exec`, the `/sandbox` command, and `download_tool` no longer require a Mythic scope. They run
  locally and make no Mythic call, so a narrow channel token no longer disables them. Model-proposed use
  still needs approval in supervised mode and is refused in conversation mode. `callback.write` is no longer
  among Sage's declared channel scopes, since no tool uses it.
- Sage's declared channel token scopes now list everything it uses: `apitoken.write` (which Mythic requires on
  any AI channel token), the read scopes for the Mythic data it queries, and `credential.write` and
  `eventlog.write`. The unused `tag.write` is gone. Mythic does not enforce this list; it tells the operator
  what to grant, and the docs list the same set.
- Max Steps now caps each chat request. The model-step count used to add up across every request in a channel
  until the session was rebuilt, so a later request could halt after only a few steps while the notice blamed
  "this request". It now starts again with each new request; approving a card continues the budget of the
  request it belongs to.
- A deliberate wait of 300 seconds or more, such as waiting for a Group Policy refresh, no longer fails with
  `NodeTimeoutError: Node 'Mythic_Operator' exceeded its idle timeout`. `wait_for_seconds` and the GPO
  capability's wait now keep the agent's idle timer alive for as long as they wait, as a Mythic task wait
  already did.
- A supervised `execute_capability` proposal whose inputs arrive as a JSON string now reaches its approval card.
  The card's plan preview read `inputs` only when it was already an object, so it planned with no inputs and
  refused with a missing field (for example `system_action` on a GPO execution) even though the capability
  itself accepts the string form.
- Approval cards for raw tasks now show the intended effect with its target filled in, for example
  `graph-built:north.sevenkingdoms.local|baseline` for a SharpHound collection, instead of the template
  `graph-built:{target}`. When the target can't be worked out yet, the card shows only the expected evidence.
- A SharpHound collection scoped to another domain with `-d <domain>` or `--domain=<domain>` is now recognised
  as that domain's collection, as `--Domain <domain>` already was. Before, Sage treated it as a repeat of the
  callback's own baseline collection, so the approval card named the wrong target and Sage could skip the
  collection as already done.
- The BloodHound setup steps in the docs, the BloodHound MCP guidance, and the message Sage shows when
  BloodHound isn't connected now give BloodHound CE's current path for creating an API token: a dedicated
  Power User account, then *Administration → Manage Users → Generate / Revoke API Tokens*. The old
  *Administration → API Tokens* menu no longer exists.
- An admin-backed DCSync proposed directly by a worker no longer skips the Kerberos context refresh that keeps it
  off a ticket issued before the admin grant, which the domain controller refuses with `0x20f7`. Such a DCSync
  now passes the planner's context gate:
  when admin authority backs the replication right and the callback has no proven context for the domain, Sage
  holds the action and offers `ensure-kerberos-context` first. A NetBIOS-keyed proof such as `da:north` now gates
  its FQDN as well.
- A DCSync whose Mythic task ended in a completion-function error is reported as partial and material only, with
  the recovery route named. Valid review-backed krbtgt material now makes `forge-golden-ticket` available, and the
  forge re-reads the key from that row's own proof task. Material never records a working krbtgt key; the forge's
  own verifier decides its effect.
- The staged Apollo foothold launcher (`skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh`) now
  disconnects its interactive session and ends its RDP opener on every exit, reports the callback and the cleanup
  separately, and exits non-zero when a local cleanup fails.

### Security

- OpenTelemetry moves from 1.44.0 to 1.45.1 (instrumentation and semantic conventions to 0.66b1), clearing the
  advisory against `opentelemetry-api` 1.44.0 listed under 0.2.0's known issues. No other dependency changed.

### Upgrade

- Stop Sage and import existing `.sage_engagement/state_*.json` ledgers with
  `skills/sage-goad-reset/scripts/engagement_ledger_migrate.py import --sage-stopped`. Sage reports the
  Engagement State Ledger as unavailable until every legacy file is imported or explicitly excluded. Source
  files are never modified.

- Domain-name learning is on by default. To keep the previous behaviour, where reported NetBIOS names pass through
  unchanged, set `SAGE_ENGAGEMENT_NETBIOS_MAP` to `off`. A JSON object still pins names, and learned pairs now fill
  in the domains it does not name.

### Known issues

- **macOS Docker Desktop and OrbStack.** Unchanged from 0.2.0: SQLite cannot lock files on the bind-mounted
  service directory there. Set `PHOENIX_WORKING_DIR` and `SAGE_DATABASE_PATH` as described in the README section
  "SQLite on macOS Docker Desktop / OrbStack"; with that setting the databases do not survive a container rebuild
  or reinstall.
- **Dependency vulnerabilities.** Some reported vulnerabilities remain because `mythic-container` and
  `arize-phoenix` pin the affected packages. They clear when those projects update their pins.
- **An approved action can be issued twice after a crash.** If Sage stops after issuing an approved supervised
  action but before it records the result, the resumed request can issue that action again. Check the task list
  in Mythic before approving the same action after a restart.
- **A step scoped to the wrong procedure still ends the request.** The recovery under Fixed applies only when the
  specialist names something that is not a catalog procedure. When the Supervisor scopes a step to a real catalog
  procedure that does not serve the request, and the specialist proposes a different action that would, the
  request ends instead of re-scoping.
- **The Supervisor does not yet plan supervised steps.** It can hand a specialist the whole objective instead of a
  bounded subgoal, so a step runs until one of its limits or an error, and the Supervisor's next choice can rest on
  an incomplete handback. Each delegation now starts with a fresh worker transcript, but how a later step should
  carry what earlier steps learned is still being reworked.
- **Repeated and empty work.** A request can propose a BloodHound collection again after a successful ingest on
  the same callback and scope; reject the repeat. An "already achieved" capability verdict can be followed by
  empty delegations, and an advisory request can keep routing after it has answered; both end in the no-progress
  stop.
- **Request endings.** A task whose Mythic completion function failed after the agent answered, or whose output
  arrives only at handback, can end the request as "not terminal". A delegation interrupted between its start and
  its end can end the request in error even when its work is usable. In both cases the tasks and their output in
  Mythic are the record of what ran.
- **Chat display.** A request's final message can appear above earlier sub-agent panels, and the internal
  `<untrusted_delegated_assignment>` delegation envelope can appear in the visible transcript.
- **The ledger does not record every effect.** It records gains that a verifier or a rule in code proves. Work done
  through generic commands, such as `powershell`, `shell` or an assembly no verifier models, leaves no row unless a
  verifier recognises its effect, so a short ledger does not mean nothing was gained.
- **Forging for a parent domain from a child-domain source.** Two forge helpers read the requested domain first, so
  a parent-domain request backed by a child-domain source can skip the parent's replication grant.
- **krbtgt references beyond the top-ranked key.** A krbtgt credential reference is checked only against the
  top-ranked key, so a valid reference to another proven row is refused.
- **Auto mode was not run live on this release.** See Validation.

### Validation

The maintained offline suite, the repository-boundary guards and the repo-hygiene checks pass on this release, and
the supervised changes are covered by scripted tests of the assembled chat path. The release qualification runs
once, in supervised mode, on a locally built image of this exact source against Mythic `v4.0.8`, with an Apollo
foothold on GOAD and Claude Sonnet 4.6 through an OpenAI-compatible LiteLLM route. One objective request asks Sage
to obtain Domain Admin of `north.sevenkingdoms.local` from the current foothold, and if that request ends without
proof, one `proceed` follows in the same chat. The whole episode must fit within 40 minutes, 2 million input
tokens, 50,000 output tokens, USD 30 and 15 approval cards, and the operator only accepts or rejects cards. It
passes only when Mythic task output proves the foothold account's Domain Admin membership in that domain and an
Engagement State Ledger row records it with the same task and callback. Sage 0.3.0 is published only after a
pass, and the GitHub release notes for 0.3.0 report the image, architecture, Mythic version, model route and
measured result. That result
covers that one configuration and objective. The published image is then checked for installation, not
requalified. Auto mode was not run live on this release and remains experimental; earlier auto runs on GOAD were
on development builds. See [README Limitations](README.md#limitations) and
[Security and Data Handling](docs/SECURITY_AND_DATA_HANDLING.md) for the complete boundaries.

## [0.2.0] - 2026-10-23

Sage 0.2.0 makes supervised mode objective-led, makes request endings honest, and ships Sage as a published,
versioned container image.

### Added

- **Objective-led supervised mode.** A supervised channel can pursue a stated objective across turns. Sage still
  proposes one guarded action at a time, and each runs only after you approve its card. Objectives and evidence
  survive approvals, rejections, and specialist handbacks. When BloodHound has data and the request is about an
  attack path, Sage queries the graph for a path to the objective before tasking.
- **Container images on GitHub Container Registry** for `linux/amd64` and `linux/arm64`:
  `ghcr.io/mythicagents/sage:0.2.0` (also `:0.2`), `:latest` for the newest commit on main, and `:sha-<commit>`
  for an exact build. Installing with `mythic-cli install github` works as before.
- **Version reporting.** The startup log reports Sage's version, the image's build commit, and a fingerprint of
  the running source, so you can tell exactly which code a deployment runs. A plain local `mythic-cli` build
  reports `unknown` for the version and commit.
- **`/tools`** lists Sage's tools and their access requirements without a model call, and **`/report`** produces
  an operation-wide identity report.
- **Provider settings.** For the `openai` provider, `SAGE_REASONING_EFFORT` sets the model's reasoning effort
  (unset uses the provider default), and `SAGE_PROVIDER_RETRY_POLICY=transient-v1` retries confirmed temporary
  429/503 provider responses. Both are optional.
- **Database location.** `SAGE_DATABASE_PATH` and `PHOENIX_WORKING_DIR` move the Sage and Phoenix databases off
  the bind-mounted service directory.

### Changed

- Mythic now shows Sage's own release version for the chat container, rather than the Mythic framework version.
- Commands for registered executables are built from each tool's contract, which repairs legacy verbs and option
  syntax before the approval card is shown. Contract identity uses Mythic's own file hash metadata, so a rebuilt
  binary with the same filename is still recognised.
- Approval cards carry decision context and list all three operator actions. Guard errors name their remedy, and
  the engagement ledger is surfaced at the end of each turn.
- All Sage tables share one database, `sage.db`.
- Dependency pins were refreshed to clear most of the fixable known vulnerabilities, including an updated
  `certifi` CA bundle.

### Fixed

- A request that Sage stops on its own (a routing loop, no progress, or a blocker) no longer reports
  "Completed." Every turn now ends as completed, blocked, or checkpoint.
- When a request ends inside a specialist agent, the final message and reason now reach the main Sage window.
- Repeated failures of the same command on the same callback now stop the request at a per-request cap, even
  when credentials or arguments rotate, and repeated failed tasking is detected across requests.
- Startup fails with an error naming the path when SQLite cannot open or lock `sage.db`, instead of failing
  later.

### Known issues

- **macOS Docker Desktop and OrbStack.** SQLite cannot lock files on the bind-mounted service directory there.
  Set `PHOENIX_WORKING_DIR` and `SAGE_DATABASE_PATH` as described in the README section "SQLite on macOS Docker
  Desktop / OrbStack". With that setting, the databases do not survive a container rebuild or reinstall.
- **Dependency vulnerabilities.** Some reported vulnerabilities remain because `mythic-container` and
  `arize-phoenix` pin the affected packages. They clear when those projects update their pins. An advisory against
  `opentelemetry-api` 1.44.0 is fixed in 1.45.1; that update moves the whole OpenTelemetry package family and is
  planned for a follow-up release.
- **Root-owned runtime state.** Sage writes `.sage_engagement*` state into the installed service directory as
  root, which can block a later reinstall by a non-root user. Archive or remove that state with `sudo` before
  reinstalling.

### Validation

The maintained offline suite (9,023 tests), the repository-boundary guards, and the repo-hygiene checks pass on
this release, and the end-of-request fixes are covered by scripted tests of the assembled chat path. The
clean-clone Docker qualification and live-range runs recorded for 0.1.0-beta were **not** repeated, so the
0.1.0-beta support matrix is the most recent live-validated configuration, not a validation of this release.
Auto mode is exercised only against GOAD. See [README Limitations](README.md#limitations) and
[Security and Data Handling](docs/SECURITY_AND_DATA_HANDLING.md) for the complete boundaries.

## [0.1.0-beta] - 2026-08-26

First beta: a bounded assisted-mode capability with a background findings watcher and the locked Sage Watcher
profile. See the [0.1.0-beta release notes](docs/releases/v0.1.0-beta.md) for the validated support matrix and
limits.

[Unreleased]: https://github.com/MythicAgents/sage/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/MythicAgents/sage/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/MythicAgents/sage/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/MythicAgents/sage/compare/v0.1.0-beta...v0.2.0
[0.1.0-beta]: https://github.com/MythicAgents/sage/releases/tag/v0.1.0-beta
