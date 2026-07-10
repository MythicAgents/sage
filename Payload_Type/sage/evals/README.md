# Sage GOAD Evaluation Harness

This harness runs scripted prompts through fresh locked Mythic v4 Sage chat channels and scores each case from
Phoenix trace sqlite data. A Sage payload callback is not required.

## Files

- `cases.yaml`: 10 GOAD subtasks plus Apollo and scoring configuration.
- `phoenix_reader.py`: pure read-only sqlite access for traces, spans, answers, token metrics, and command histograms.
- `harness.py`: async Mythic orchestration, settle polling, scoring, report writing, and report comparison.
- `results/`: ignored run artifacts, except `.gitkeep`.

## Running A Live Baseline

From the repo root:

```bash
/home/john/dev/sage/.venv/bin/python Payload_Type/sage/evals/harness.py run --cases Payload_Type/sage/evals/cases.yaml --db Payload_Type/sage/.phoenix/phoenix.db --out Payload_Type/sage/evals/results --seeds 3 --poll-interval 35
```

The live run resolves credentials from `MYTHIC_ADMIN_PASSWORD`, `MYTHIC_ENV_PATH`,
`/home/john/dev/mythic_v4/.env`, then the legacy v3 `.env`.

Useful live-run options:

- `--only list_callbacks,shares`: run a subset of case ids.
- `--sage-cb 15`: legacy payload-path compatibility only.
- `--timeout 240`: override per-case wall-clock timeout.
- `--poll-interval 35`: override Phoenix settle polling cadence.
- `--seeds 3`: run each selected case three times. The default is `--seeds 1`.
- `--judge`: enable the placeholder judge score path.

Cases and seeds run serially, with one chat request in flight at a time. Each case/seed gets a fresh channel.

For a live baseline, run the command above with a human-chosen seed count such as `run --seeds 3`.

## Report Schema

Live `run` writes schema v2 JSON:

- Top level: `schema_version: 2`, `execution_surface: mythic-v4-chat`, `started`, `finished`, `seeds`, `pass_rate`, `mean_sweep_tokens`, and `cases`.
- Each case: `id`, `category`, `prompt`, `pass_fraction`, `seeds`, `tokens_mean`, `tokens_std`, and `wall_mean`.
- Each seed record also includes `chat_channel_id` and `chat_request_id`.
- Each persisted span row: `agent`, `trace_id`, `name`, `span_kind`, `prompt`, `completion`, and `status_code`.

Token fields are estimates from Phoenix per-call token columns. `est_fixed_floor` is the minimum prompt-token count among model-call spans (`prompt > 0`), or zero when there are no model calls. `est_variable` is `max(0, prompt_tokens - est_fixed_floor * model_calls)`. `per_agent_tokens` attributes prompt plus completion tokens to the owning trace root span name, such as `Supervisor`, `Mythic_Operator`, or `MCP_Manager`.

`tool_calls` is a Phoenix-span heuristic. The reader counts spans named like `issue_task_and_waitfor_task_output%`, spans whose name ends in `.tool`, or spans with `span_kind`/span-kind attributes equal to `TOOL`. The `.tool` suffix is intentionally included because offline fixtures and many Phoenix tool spans use that naming pattern.

Full `answer_full` text and raw `spans` are persisted per seed so a future binary-to-graded offline replay path can re-score with a judge without spending live Mythic/Phoenix tokens. The current `--judge` flag remains a placeholder for that future graded path.

## Comparing Runs

```bash
/home/john/dev/sage/.venv/bin/python Payload_Type/sage/evals/harness.py compare Payload_Type/sage/evals/results/eval-baseline.json Payload_Type/sage/evals/results/eval-new.json
```

Compare mode preserves the old v1 output when both inputs are v1 reports. When either input is schema v2, it normalizes v1/v2 cases and prints pass-fraction deltas, per-case `tokens_mean` deltas, and a variance verdict.

The v2 significance rule is a crude noise band: if `abs(mean_b - mean_a) > (std_a + std_b)`, the case is `SIGNIFICANT`; otherwise it is `within noise`. Single-seed inputs with zero variance do not crash: a nonzero delta is reported as `SIGNIFICANT (n=1, no variance - treat with caution)`, while a zero delta remains `within noise`.

## Offline Verification

The included tests build a temporary sqlite fixture and mock Mythic and Phoenix reader functions. They do not touch the live lab or real Phoenix DB.

```bash
/home/john/dev/sage/.venv/bin/python -m pytest Payload_Type/sage/tests/test_eval_harness.py -q
```
