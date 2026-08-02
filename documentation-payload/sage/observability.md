+++
title = "Observability"
chapter = false
weight = 80
+++

Sage embeds [Arize Phoenix](https://github.com/Arize-ai/phoenix). On startup it launches a local Phoenix
instance and instruments the whole LangChain stack through OpenInference, so **every model call, tool call,
prompt, output, token count, and error is captured as a trace** — with no configuration, and nothing leaving the
host.

This gives you:

- **See exactly what the agent did and why.** Open the Phoenix UI (by default `http://localhost:6006`) and walk
  any run span by span: which agent ran, what it sent the model, what came back, which tools fired, and where a
  step failed.

- **Token accounting** per run and per step — how you catch a context blow-up or a runaway loop before it burns
  your budget.

- **The evidence base for evaluation.** The trace store (`.phoenix/phoenix.db`) is what Sage's eval harness reads
  to score runs, so debugging and measurement share one source of truth.

{{% notice tip %}}
Traces persist in `.phoenix/phoenix.db`. Treat it, and Sage's LangGraph checkpoint database `sage.db`, as
retained runtime state — never delete them to obtain a clean run; archive them instead.
{{% /notice %}}
