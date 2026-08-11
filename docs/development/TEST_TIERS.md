# Sage Test Tiers

There is one tier.

Run the offline suite from the repository root:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
```

No suite is excluded. A green run means the tree is green — which is the point of removing the split described
below. The runner still accepts a trailing `supported` argument and ignores it, so older handoff docs and muscle
memory keep working.

## What changed, and why

This file previously documented two tiers. `supported` ran the tree minus four append-only rejected
successor-portfolio suites; `retired` ran only those four and "is not expected to be green."

Those suites froze source hashes of an older product surface, so they could never pass against current code. The
exclusion was therefore permanent, and the default command's name — "supported" — quietly meant "the parts we
still expect to work."

The portfolios are rejected evaluation evidence. `AGENTS.md` § Durable Artifact Retention names `.sage_history/`
as the home for "accepted or rejected evaluation evidence," so they now live at:

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

## The incident this encodes (2026-08-10)

`set_node_defaults(timeout=TimeoutPolicy(...))` applies to every node. `error_handler=` makes LangGraph register
a synthetic `__error_handler__Mythic_Operator` node, which inherits that default. LangGraph rejects node timeouts
on sync callables, and the handler shipped as `def`. Result: `_rebuild_graph` raised, so **every** request failed
at build time in about five seconds, before reaching a model — including a bare "Hello". Both features had passing
tests. The suite was 3955 green. The defect was found by a human typing into the chat window.

The fix was one keyword. The gap was that the cheapest possible test — *does it assemble?* — did not exist.

