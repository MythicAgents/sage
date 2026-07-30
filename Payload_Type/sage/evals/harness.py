"""CLI and orchestration for Sage GOAD evaluations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import phoenix_reader
from ai.langgraph.mythic_tools import assess_callback_liveness


REPO_ROOT = Path(__file__).resolve().parents[3]
SAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = SAGE_ROOT / "evals" / "cases.yaml"
DEFAULT_RESULTS = SAGE_ROOT / "evals" / "results"
DEFAULT_DB = SAGE_ROOT / ".phoenix" / "phoenix.db"
# Empty when unset: no checkout-name guess. resolve_password fails closed naming the variable.
MYTHIC_ENV_PATH = Path(os.environ.get("MYTHIC_ENV_PATH") or "")
MYTHIC_SERVER = "127.0.0.1"
MYTHIC_USER = "mythic_admin"
NATIVE_CHAT_SCRIPTS = REPO_ROOT / "skills" / "sage-live-runner" / "scripts"
if str(NATIVE_CHAT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(NATIVE_CHAT_SCRIPTS))
from native_chat import run_native_chat_turn  # noqa: E402


@dataclass(frozen=True)
class ScoreResult:
    """Binary scoring result with failure notes."""

    passed: bool
    score: float
    errors: list[str]


@dataclass(frozen=True)
class NormalizedCase:
    """Common comparison shape for v1 and v2 case records."""

    case_id: str
    pass_fraction: float
    tokens_mean: float
    tokens_std: float
    seed_count: int


def load_cases(path: str | Path) -> dict[str, Any]:
    """Load and validate an eval cases YAML file."""

    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("cases file must contain a mapping")

    for key in ("apollo_cb", "default_timeout", "forbid", "cases"):
        if key not in data:
            raise ValueError(f"cases file missing required key: {key}")
    if not isinstance(data["forbid"], list) or not data["forbid"]:
        raise ValueError("forbid must be a non-empty list")
    if not isinstance(data["cases"], list) or not data["cases"]:
        raise ValueError("cases must be a non-empty list")

    seen: set[str] = set()
    for case in data["cases"]:
        validate_case(case)
        case_id = case["id"]
        if case_id in seen:
            raise ValueError(f"duplicate case id: {case_id}")
        seen.add(case_id)
    return data


def validate_case(case: dict[str, Any]) -> None:
    """Validate one eval case mapping."""

    if not isinstance(case, dict):
        raise ValueError("case must be a mapping")
    for key in ("id", "category", "prompt"):
        if not case.get(key):
            raise ValueError(f"case missing required key: {key}")
    expect_keys = [key for key in ("expect_all", "expect_any") if key in case]
    if len(expect_keys) != 1:
        raise ValueError(f"case {case.get('id', '<unknown>')} must define exactly one expect_* key")
    values = case[expect_keys[0]]
    if not isinstance(values, list) or not values:
        raise ValueError(f"case {case['id']} {expect_keys[0]} must be a non-empty list")


def resolve_password(env_path: str | Path = MYTHIC_ENV_PATH) -> str:
    """Resolve the Mythic admin password from env or a local Mythic .env file."""

    env_value = os.environ.get("MYTHIC_ADMIN_PASSWORD")
    if env_value:
        return env_value

    path = Path(env_path)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "MYTHIC_ADMIN_PASSWORD" and value.strip():
                return value.strip().strip("'\"")

    raise RuntimeError(
        f"MYTHIC_ADMIN_PASSWORD is not set and no entry was found in {path}"
    )


async def login_to_mythic(password: str) -> Any:
    """Create an authenticated Mythic client."""

    from mythic import mythic

    return await mythic.login(server_ip=MYTHIC_SERVER, username=MYTHIC_USER, password=password)


_phoenix_ready = False


def ensure_phoenix_instrumentation(db_path: str | Path) -> None:
    """Instrument LangChain → Phoenix in THIS process so an in-process (headless) solve emits the traces
    the scorer reads from ``db_path`` — ``main.py`` does exactly this for the container, but a headless
    eval runs the Model here, not there. Idempotent; points ``PHOENIX_WORKING_DIR`` at ``db_path``'s parent
    so the traces land where ``phoenix_reader`` reads them.

    NOTE [DEFERRED-VERIFY]: the working-dir ↔ phoenix.db ↔ OTLP-collector alignment can't be checked
    offline — it MUST be confirmed on the first headless eval run (if traces don't land, every case reads
    empty and scores 0). This is the one piece of the evals headless path that needs a live look.
    """
    global _phoenix_ready
    if _phoenix_ready:
        return
    os.environ.setdefault("PHOENIX_WORKING_DIR", str(Path(db_path).resolve().parent))
    os.environ.setdefault("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "131072")
    import phoenix as px
    from phoenix.otel import register
    from openinference.instrumentation.langchain import LangChainInstrumentor

    try:
        px.launch_app(use_temp_dir=False)  # standalone collector+db when no container is running
    except Exception as exc:  # a container's app may already own the port — instrumentation still works
        print(f"[headless-eval] phoenix launch_app skipped ({exc}); relying on an existing collector", flush=True)
    tracer_provider = register(project_name="Sage", auto_instrument=False, batch=False)
    LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
    _phoenix_ready = True


async def issue_chat_task(client: Any, prompt: str, sage_cb: int) -> Any:
    """Issue Sage's one-shot `query` command (NOT interactive `chat`).

    `chat` is an interactive session that never reaches a terminal/`completed`
    status, so wait_for_task_complete would always time out. `query` runs a
    single prompt and completes, which is what batch evals need. Sage always has
    its tools (there is no tools toggle), so no extra args are required.
    """

    from mythic import mythic

    return await mythic.issue_task(
        mythic=client,
        command_name="query",
        parameters=json.dumps({"prompt": prompt, "verbose": True, "mode": "auto"}),
        callback_display_id=sage_cb,
    )


async def wait_for_task_complete(
    client: Any,
    task_display_id: int,
    timeout_seconds: int,
    poll_interval_seconds: float = 10.0,
) -> str:
    """Poll a Mythic task by display_id until terminal status, returning the final status."""

    from mythic import mythic

    deadline = time.monotonic() + timeout_seconds
    last_status: str | None = None
    task_seen = False

    while time.monotonic() < deadline:
        tasks = await mythic.get_all_tasks(
            mythic=client,
            custom_return_attributes="id display_id status completed",
        )
        if not isinstance(tasks, list):
            raise TypeError(f"Mythic get_all_tasks returned {type(tasks).__name__}, expected list")

        for task in tasks:
            if not isinstance(task, dict):
                continue
            display_id = task.get("display_id")
            try:
                matches_task = int(display_id) == int(task_display_id)
            except (TypeError, ValueError):
                matches_task = False
            if not matches_task:
                continue

            task_seen = True
            raw_status = task.get("status")
            last_status = raw_status if isinstance(raw_status, str) else None
            completed = task.get("completed") is True
            status_lower = last_status.lower() if last_status is not None else ""
            if completed or any(marker in status_lower for marker in ("completed", "error", "processed")):
                return last_status or "completed"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(float(poll_interval_seconds), remaining))

    status_label = last_status if task_seen else None
    raise TimeoutError(
        f"Mythic task {task_display_id} did not reach terminal status within {timeout_seconds}s; "
        f"last status={status_label!r}"
    )


async def wait_for_settled_traces(
    db_path: str | Path,
    pre_rowid: int,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> list[phoenix_reader.TraceSummary]:
    """Poll Phoenix until spans stop growing twice in a row, or time out."""

    deadline = time.monotonic() + timeout_seconds
    unchanged_polls = 0
    last_total: int | None = None
    last_summaries: list[phoenix_reader.TraceSummary] = []

    while time.monotonic() < deadline:
        summaries = phoenix_reader.trace_summaries_since(db_path, pre_rowid)
        total_spans = sum(summary.spans for summary in summaries)
        if total_spans > 0 and total_spans == last_total:
            unchanged_polls += 1
            if unchanged_polls >= 2:
                return summaries
        else:
            unchanged_polls = 0
        last_total = total_spans
        last_summaries = summaries

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(float(poll_interval_seconds), remaining))

    total = sum(summary.spans for summary in last_summaries)
    raise TimeoutError(f"Phoenix traces did not settle within {timeout_seconds}s; last span count={total}")


def score_answer(
    answer: str,
    tool_outputs: str,
    case: dict[str, Any],
    forbid: Sequence[str],
    error_count: int,
) -> ScoreResult:
    """Score a case answer using case-insensitive expectation and safety checks."""

    notes: list[str] = []
    answer_lower = (answer or "").strip().lower()
    scored_text = ((answer or "") + "\n" + (tool_outputs or "")).lower()
    prompt_lower = str(case.get("prompt", "")).strip().lower()

    if not scored_text.strip():
        notes.append("empty scored text")

    if "expect_all" in case:
        expected = [str(value) for value in case["expect_all"]]
        missing = [value for value in expected if value.lower() not in scored_text]
        expect_ok = not missing
        if missing:
            notes.append("missing expected substrings: " + ", ".join(missing))
    else:
        expected = [str(value) for value in case["expect_any"]]
        expect_ok = any(value.lower() in scored_text for value in expected)
        if not expect_ok:
            notes.append("none of expected substrings present: " + ", ".join(expected))

    if prompt_lower and answer_lower == prompt_lower:
        notes.append("answer only echoes prompt")

    present_forbid = [marker for marker in forbid if str(marker).lower() in answer_lower]
    if present_forbid:
        notes.append("forbidden markers present: " + ", ".join(str(marker) for marker in present_forbid))

    if error_count > 0:
        notes.append(f"{error_count} ERROR spans")

    passed = expect_ok and not present_forbid and error_count == 0 and bool(scored_text.strip())
    return ScoreResult(passed=passed, score=1.0 if passed else 0.0, errors=notes)


def grade(answer: str, case: dict[str, Any], binary_score: ScoreResult) -> float:
    """Return optional judge score for an answer."""

    # Live judge evaluation is intentionally disabled in the offline harness path.
    return 1.0 if binary_score.passed else 0.0


async def preflight_apollo(client: Any, apollo_cb: int) -> dict:
    """Assess Apollo callback liveness before issuing eval tasking."""

    return await assess_callback_liveness(client, apollo_cb)


async def run_case(
    client: Any,
    case: dict[str, Any],
    *,
    seed: int = 0,
    db_path: str | Path,
    sage_cb: int | None = None,
    timeout_seconds: int,
    poll_interval_seconds: float,
    forbid: Sequence[str],
    use_judge: bool = False,
    apollo_liveness: dict | None = None,
) -> dict[str, Any]:
    """Run and score one eval case with exception isolation."""

    start = time.monotonic()
    base = {
        "seed": seed,
        "status": "fail",
        "passed": False,
        "score": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "est_fixed_floor": 0,
        "est_variable": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "per_agent_tokens": {},
        "command_histogram": {},
        "recursion_deaths": 0,
        "errors": [],
        "wall_seconds": 0.0,
        "answer_full": "",
        "answer_snippet": "",
        "tool_outputs_chars": 0,
        "tool_outputs_snippet": "",
        "trace_ids": [],
        "spans": [],
        "chat_channel_id": None,
        "chat_request_id": None,
    }

    try:
        if case.get("category") == "apollo-recon" and apollo_liveness is not None:
            if apollo_liveness.get("status") != "alive":
                reason = str(apollo_liveness.get("reason") or "apollo callback is not alive")
                base.update({"status": "skipped", "errors": [reason], "reason": reason})
                return base

        pre_rowid = phoenix_reader.max_trace_rowid(db_path)
        if os.environ.get("SAGE_EVAL_HEADLESS"):
            # Option A (Phase-4 migration): run the solve IN-PROCESS via the chat Model instead of tasking
            # the PayloadType `query`. The scorer reads Phoenix traces, so instrument THIS process first
            # (see the [DEFERRED-VERIFY] note on ensure_phoenix_instrumentation). Ledger key from
            # SAGE_ENGAGEMENT_ID or the eval operation name.
            ensure_phoenix_instrumentation(db_path)
            from ai.hillclimb.headless_solver import run_headless_solve

            _eng = os.environ.get("SAGE_ENGAGEMENT_ID") or "Operation_Chimera_1"
            status = await run_headless_solve(
                case["prompt"], client=client, operation_id=0, engagement_id=_eng,
                timeout=timeout_seconds,
            )
            if str(status).startswith("timeout"):
                base["status"] = "incomplete"
                base["errors"] = [f"incomplete: {status}"]
                base["wall_seconds"] = round(time.monotonic() - start, 3)
                return base
        elif os.environ.get("SAGE_EVAL_LEGACY_PAYLOAD") or sage_cb is not None:
            if sage_cb is None:
                raise RuntimeError("SAGE_EVAL_LEGACY_PAYLOAD requires a Sage callback ID")
            task = await issue_chat_task(client, case["prompt"], sage_cb)
            display_id = _extract_task_display_id(task)
            try:
                await wait_for_task_complete(client, display_id, timeout_seconds, poll_interval_seconds)
            except TimeoutError as exc:
                base["status"] = "incomplete"
                base["errors"] = [f"incomplete: {exc}"]
                try:
                    await wait_for_task_complete(client, display_id, max(1, timeout_seconds // 2), poll_interval_seconds)
                except TimeoutError:
                    print(f"WARN case still running after grace wait; display_id={display_id}", flush=True)
                base["wall_seconds"] = round(time.monotonic() - start, 3)
                return base
        else:
            chat_result = await run_native_chat_turn(
                client,
                case["prompt"],
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                metadata={"eval_case": case["id"], "seed": seed},
            )
            base["chat_channel_id"] = chat_result.get("chat_channel_id")
            base["chat_request_id"] = chat_result.get("chat_request_id")
            status = str(chat_result.get("status") or "").casefold()
            if status not in {"complete", "completed"}:
                base["status"] = "incomplete"
                base["errors"] = [
                    f"incomplete: chat request status={chat_result.get('status')!r} "
                    f"error={chat_result.get('error')!r}"
                ]
                return base

        summaries = await wait_for_settled_traces(db_path, pre_rowid, timeout_seconds, poll_interval_seconds)
        trace_rowids = [summary.rowid for summary in summaries]
        metrics = phoenix_reader.aggregate_metrics(db_path, trace_rowids)
        breakdown = phoenix_reader.token_breakdown(db_path, trace_rowids)
        answer = phoenix_reader.extract_answer_with_fallback(db_path, trace_rowids)
        tool_text = phoenix_reader.tool_outputs(db_path, trace_rowids)
        histogram = phoenix_reader.command_histogram(db_path, trace_rowids)
        spans = phoenix_reader.span_rows(db_path, trace_rowids)
        score = score_answer(answer, tool_text, case, forbid, metrics.error_count)
        if use_judge:
            score = ScoreResult(score.passed, grade(answer, case, score), score.errors)

        base.update(
            {
                "status": "pass" if score.passed else "fail",
                "passed": score.passed,
                "score": score.score,
                "prompt_tokens": breakdown.prompt_tokens,
                "completion_tokens": breakdown.completion_tokens,
                "total_tokens": breakdown.prompt_tokens + breakdown.completion_tokens,
                "est_fixed_floor": breakdown.est_fixed_floor,
                "est_variable": breakdown.est_variable,
                "model_calls": breakdown.model_calls,
                "tool_calls": breakdown.tool_calls,
                "per_agent_tokens": breakdown.per_agent_tokens,
                "errors": score.errors + _format_span_errors(metrics.errors),
                "recursion_deaths": metrics.recursion_deaths,
                "answer_full": answer,
                "answer_snippet": answer[:500],
                "tool_outputs_chars": len(tool_text),
                "tool_outputs_snippet": tool_text[:500],
                "trace_ids": [summary.trace_id for summary in summaries],
                "command_histogram": histogram,
                "spans": spans,
            }
        )
    except Exception as exc:
        base.update({"status": "fail", "errors": [f"{type(exc).__name__}: {exc}"]})
    finally:
        base["wall_seconds"] = round(time.monotonic() - start, 3)
    return base


async def run_eval(
    *,
    cases_path: str | Path = DEFAULT_CASES,
    db_path: str | Path | None = None,
    out_dir: str | Path = DEFAULT_RESULTS,
    sage_cb: int | None = None,
    timeout_seconds: int | None = None,
    only: Sequence[str] | None = None,
    poll_interval_seconds: float = 35.0,
    use_judge: bool = False,
    seeds: int = 1,
) -> dict[str, Any]:
    """Run selected eval cases serially and write JSON and Markdown reports."""

    if seeds < 1:
        raise ValueError("seeds must be at least 1")

    config = load_cases(cases_path)
    selected = select_cases(config["cases"], only)
    configured_sage_cb = config.get("sage_cb")
    resolved_sage_cb = (
        int(sage_cb if sage_cb is not None else configured_sage_cb)
        if sage_cb is not None or configured_sage_cb is not None
        else None
    )
    resolved_apollo_cb = int(config["apollo_cb"])
    resolved_timeout = int(timeout_seconds if timeout_seconds is not None else config["default_timeout"])
    resolved_db = Path(db_path or os.environ.get("PHOENIX_DB", DEFAULT_DB))
    started = utc_timestamp()

    password = resolve_password()
    client = await login_to_mythic(password)
    apollo_liveness = None
    if any(case.get("category") == "apollo-recon" for case in selected):
        apollo_liveness = await preflight_apollo(client, resolved_apollo_cb)

    records: list[dict[str, Any]] = []
    for case in selected:
        seed_records: list[dict[str, Any]] = []
        for seed in range(seeds):
            print(f"RUN {case['id']} seed={seed} ...", flush=True)
            record = await run_case(
                client,
                case,
                seed=seed,
                db_path=resolved_db,
                sage_cb=resolved_sage_cb,
                timeout_seconds=resolved_timeout,
                poll_interval_seconds=poll_interval_seconds,
                forbid=config["forbid"],
                use_judge=use_judge,
                apollo_liveness=apollo_liveness,
            )
            seed_records.append(record)
            status = str(record.get("status") or ("pass" if record["passed"] else "fail")).upper()
            print(f"{status} {case['id']} seed={seed} score={record['score']} tokens={record['total_tokens']}", flush=True)
        records.append(aggregate_case_runs(case, seed_records))

    report = build_report_v2(started, utc_timestamp(), seeds, records)
    write_reports(report, out_dir)
    return report


def select_cases(cases: list[dict[str, Any]], only: Sequence[str] | None) -> list[dict[str, Any]]:
    """Return cases filtered by optional comma-separated ids."""

    if not only:
        return list(cases)
    wanted = {item.strip() for item in only if item.strip()}
    selected = [case for case in cases if case["id"] in wanted]
    missing = wanted - {case["id"] for case in selected}
    if missing:
        raise ValueError("unknown case ids: " + ", ".join(sorted(missing)))
    return selected


def build_report(started: str, finished: str, sage_cb: int, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the top-level JSON report object."""

    total_tokens = sum(int(case.get("tokens", 0)) for case in cases)
    pass_count = sum(1 for case in cases if case.get("passed"))
    count = len(cases)
    return {
        "started": started,
        "finished": finished,
        "sage_cb": sage_cb,
        "pass_rate": pass_count / count if count else 0.0,
        "total_tokens": total_tokens,
        "avg_tokens": total_tokens / count if count else 0.0,
        "cases": cases,
    }


def aggregate_case_runs(case: dict[str, Any], seed_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate serial seed records for one case into the v2 case shape."""

    total_tokens = [int(record.get("total_tokens", 0)) for record in seed_records]
    wall_seconds = [float(record.get("wall_seconds", 0.0)) for record in seed_records]
    statuses = [_seed_status(record) for record in seed_records]
    pass_count = sum(1 for status in statuses if status == "pass")
    completed_count = sum(1 for status in statuses if status in ("pass", "fail"))
    incomplete_count = sum(1 for status in statuses if status == "incomplete")
    skipped_count = sum(1 for status in statuses if status == "skipped")
    return {
        "id": case["id"],
        "category": case["category"],
        "prompt": case["prompt"],
        "pass_fraction": pass_count / completed_count if completed_count else None,
        "seeds": seed_records,
        "incomplete_count": incomplete_count,
        "skipped_count": skipped_count,
        "tokens_mean": statistics.mean(total_tokens) if total_tokens else 0.0,
        "tokens_std": statistics.pstdev(total_tokens) if len(total_tokens) > 1 else 0.0,
        "wall_mean": statistics.mean(wall_seconds) if wall_seconds else 0.0,
    }


def build_report_v2(
    started: str,
    finished: str,
    seeds: int,
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the schema v2 top-level JSON report object."""

    scorable_pass_fractions = [
        float(case["pass_fraction"]) for case in cases if case.get("pass_fraction") is not None
    ]
    return {
        "started": started,
        "finished": finished,
        "execution_surface": "mythic-v4-chat",
        "schema_version": 2,
        "seeds": seeds,
        "pass_rate": statistics.mean(scorable_pass_fractions) if scorable_pass_fractions else 0.0,
        "incomplete_count": sum(int(case.get("incomplete_count", 0)) for case in cases),
        "skipped_count": sum(int(case.get("skipped_count", 0)) for case in cases),
        "mean_sweep_tokens": sum(float(case.get("tokens_mean", 0.0)) for case in cases),
        "cases": cases,
    }


def write_reports(report: dict[str, Any], out_dir: str | Path) -> tuple[Path, Path]:
    """Write timestamped JSON and Markdown reports."""

    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    stamp = safe_timestamp(report["started"])
    json_path = path / f"eval-{stamp}.json"
    md_path = path / f"eval-{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_markdown(report: dict[str, Any]) -> str:
    """Render a Markdown summary table for an eval report."""

    if int(report.get("schema_version", 1)) == 2:
        return render_markdown_v2(report)

    lines = [
        "# Sage Evaluation Baseline",
        "",
        f"- Started: {report['started']}",
        f"- Finished: {report['finished']}",
        f"- Sage callback: {report['sage_cb']}",
        f"- Pass rate: {report['pass_rate']:.2%}",
        f"- Total tokens: {report['total_tokens']}",
        "",
        "| Case | Category | Result | Score | Tokens | Model calls | Errors |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for case in report["cases"]:
        result = "PASS" if case.get("passed") else "FAIL"
        errors = "; ".join(str(error) for error in case.get("errors", []))
        lines.append(
            f"| {case['id']} | {case['category']} | {result} | {float(case.get('score', 0.0)):.2f} | "
            f"{case.get('tokens', 0)} | {case.get('model_calls', 0)} | {errors} |"
        )
    return "\n".join(lines) + "\n"


def render_markdown_v2(report: dict[str, Any]) -> str:
    """Render a Markdown summary table for a schema v2 eval report."""

    pass_rate = report.get("pass_rate")
    pass_rate_cell = "n/a" if pass_rate is None else f"{float(pass_rate):.2%}"
    lines = [
        "# Sage Evaluation Baseline",
        "",
        f"- Started: {report['started']}",
        f"- Finished: {report['finished']}",
        f"- Execution surface: {report.get('execution_surface', 'mythic-v4-chat')}",
        f"- Schema version: {report['schema_version']}",
        f"- Seeds: {report['seeds']}",
        f"- Pass rate: {pass_rate_cell}",
        f"- Incomplete count: {report.get('incomplete_count', 0)}",
        f"- Skipped count: {report.get('skipped_count', 0)}",
        f"- Mean sweep tokens: {float(report['mean_sweep_tokens']):.1f}",
        "",
        "| Case | Category | Pass fraction | Mean tokens | Token std | Mean wall seconds |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for case in report["cases"]:
        raw_pass_fraction = case.get("pass_fraction")
        pass_fraction = "n/a" if raw_pass_fraction is None else f"{float(raw_pass_fraction):.2%}"
        lines.append(
            f"| {case['id']} | {case['category']} | {pass_fraction} | "
            f"{float(case.get('tokens_mean', 0.0)):.1f} | {float(case.get('tokens_std', 0.0)):.1f} | "
            f"{float(case.get('wall_mean', 0.0)):.3f} |"
        )
    return "\n".join(lines) + "\n"


def compare_reports(baseline_path: str | Path, new_path: str | Path) -> str:
    """Return a comparison table for two JSON reports."""

    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    new = json.loads(Path(new_path).read_text(encoding="utf-8"))
    if int(baseline.get("schema_version", 1)) == 2 or int(new.get("schema_version", 1)) == 2:
        return compare_reports_v2(baseline, new)
    return compare_reports_v1(baseline, new)


def compare_reports_v1(baseline: dict[str, Any], new: dict[str, Any]) -> str:
    """Return the original v1 comparison table."""

    baseline_cases = {case["id"]: case for case in baseline.get("cases", [])}
    new_cases = {case["id"]: case for case in new.get("cases", [])}
    case_ids = sorted(set(baseline_cases) | set(new_cases))

    lines = [
        "Case | Pass Transition | Token Delta",
        "--- | --- | ---:",
    ]
    for case_id in case_ids:
        old = baseline_cases.get(case_id, {})
        current = new_cases.get(case_id, {})
        old_status = _status(old)
        new_status = _status(current)
        token_delta = int(current.get("tokens", 0)) - int(old.get("tokens", 0))
        lines.append(f"{case_id} | {old_status}->{new_status} | {token_delta:+d}")

    pass_delta = float(new.get("pass_rate", 0.0)) - float(baseline.get("pass_rate", 0.0))
    token_delta = int(new.get("total_tokens", 0)) - int(baseline.get("total_tokens", 0))
    lines.extend(
        [
            "",
            f"Aggregate pass-rate delta: {pass_delta:+.2%}",
            f"Aggregate total-token delta: {token_delta:+d}",
        ]
    )
    return "\n".join(lines)


def compare_reports_v2(baseline: dict[str, Any], new: dict[str, Any]) -> str:
    """Return a variance-aware comparison table for v2-compatible reports."""

    baseline_cases = {case["id"]: _normalize_case(case) for case in baseline.get("cases", [])}
    new_cases = {case["id"]: _normalize_case(case) for case in new.get("cases", [])}
    case_ids = sorted(set(baseline_cases) | set(new_cases))

    lines = [
        "Case | Pass Fraction Delta | Token Mean Delta | Verdict",
        "--- | ---: | ---: | ---",
    ]
    significant = 0
    within_noise = 0
    total_token_delta = 0.0
    for case_id in case_ids:
        old = baseline_cases.get(case_id, NormalizedCase(case_id, 0.0, 0.0, 0.0, 1))
        current = new_cases.get(case_id, NormalizedCase(case_id, 0.0, 0.0, 0.0, 1))
        pass_delta = current.pass_fraction - old.pass_fraction
        token_delta = current.tokens_mean - old.tokens_mean
        total_token_delta += token_delta
        verdict = _significance_verdict(old, current, token_delta)
        if verdict.startswith("SIGNIFICANT"):
            significant += 1
        else:
            within_noise += 1
        lines.append(f"{case_id} | {pass_delta:+.2%} | {token_delta:+.1f} | {verdict}")

    pass_delta = _report_pass_rate(new) - _report_pass_rate(baseline)
    lines.extend(
        [
            "",
            f"Aggregate pass-rate delta: {pass_delta:+.2%}",
            f"Aggregate mean-token delta: {total_token_delta:+.1f}",
            f"{significant} cases changed significantly, {within_noise} within noise.",
        ]
    )
    return "\n".join(lines)


def print_compare(baseline_path: str | Path, new_path: str | Path) -> None:
    """Print a comparison table for two reports."""

    print(compare_reports(baseline_path, new_path))


def utc_timestamp() -> str:
    """Return a UTC ISO-8601 timestamp."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_timestamp(timestamp: str) -> str:
    """Make a timestamp suitable for result filenames."""

    return timestamp.replace(":", "").replace("-", "").replace("Z", "Z")


def parse_only(value: str | None) -> list[str] | None:
    """Parse a comma-separated --only value."""

    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _status(case: dict[str, Any]) -> str:
    if not case:
        return "MISSING"
    return "PASS" if case.get("passed") else "FAIL"


def _normalize_case(case: dict[str, Any]) -> NormalizedCase:
    case_id = str(case.get("id", ""))
    if "tokens_mean" in case or "pass_fraction" in case:
        return NormalizedCase(
            case_id=case_id,
            pass_fraction=_coerce_optional_float(case.get("pass_fraction"), 0.0),
            tokens_mean=float(case.get("tokens_mean", case.get("tokens", 0))),
            tokens_std=float(case.get("tokens_std", 0.0)),
            seed_count=len(case.get("seeds", [])) or 1,
        )
    return NormalizedCase(
        case_id=case_id,
        pass_fraction=1.0 if case.get("passed") else 0.0,
        tokens_mean=float(case.get("tokens", 0)),
        tokens_std=0.0,
        seed_count=1,
    )


def _significance_verdict(old: NormalizedCase, current: NormalizedCase, token_delta: float) -> str:
    if old.seed_count == 1 and current.seed_count == 1 and old.tokens_std == 0.0 and current.tokens_std == 0.0:
        if token_delta == 0:
            return "within noise"
        return "SIGNIFICANT (n=1, no variance - treat with caution)"
    if abs(token_delta) > old.tokens_std + current.tokens_std:
        return "SIGNIFICANT"
    return "within noise"


def _report_pass_rate(report: dict[str, Any]) -> float:
    if "pass_rate" in report:
        return _coerce_optional_float(report.get("pass_rate"), 0.0)
    cases = [_normalize_case(case) for case in report.get("cases", [])]
    return statistics.mean([case.pass_fraction for case in cases]) if cases else 0.0


def _extract_task_display_id(task: Any) -> int:
    if not isinstance(task, dict) or "display_id" not in task:
        raise ValueError("issue_chat_task did not return a display_id")
    display_id = task.get("display_id")
    if display_id is None:
        raise ValueError("issue_chat_task returned display_id=None")
    return int(display_id)


def _seed_status(record: dict[str, Any]) -> str:
    status = record.get("status")
    if status in ("pass", "fail", "incomplete", "skipped"):
        return str(status)
    if status is None:
        return "pass" if record.get("passed") else "fail"
    return "fail"


def _coerce_optional_float(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)


def _format_span_errors(errors: list[dict[str, str]]) -> list[str]:
    return [
        f"ERROR span {error.get('name', '')}: {error.get('status_message', '')}".strip()
        for error in errors
    ]


def build_parser() -> argparse.ArgumentParser:
    """Build the eval harness command parser."""

    parser = argparse.ArgumentParser(description="Run or compare Sage GOAD evaluations.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run eval cases serially")
    run_parser.add_argument("--cases", default=str(DEFAULT_CASES))
    run_parser.add_argument("--sage-cb", type=int, default=None)
    run_parser.add_argument("--db", default=None)
    run_parser.add_argument("--out", default=str(DEFAULT_RESULTS))
    run_parser.add_argument("--timeout", type=int, default=None)
    run_parser.add_argument("--only", default=None)
    run_parser.add_argument("--judge", action="store_true")
    run_parser.add_argument("--poll-interval", type=float, default=35.0)
    run_parser.add_argument("--seeds", type=int, default=1)

    compare_parser = subparsers.add_parser("compare", help="compare two eval reports")
    compare_parser.add_argument("baseline_json")
    compare_parser.add_argument("new_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""

    args = build_parser().parse_args(argv)
    if args.command == "compare":
        print_compare(args.baseline_json, args.new_json)
        return 0

    db_path = args.db or os.environ.get("PHOENIX_DB", str(DEFAULT_DB))
    asyncio.run(
        run_eval(
            cases_path=args.cases,
            db_path=db_path,
            out_dir=args.out,
            sage_cb=args.sage_cb,
            timeout_seconds=args.timeout,
            only=parse_only(args.only),
            poll_interval_seconds=args.poll_interval,
            use_judge=args.judge,
            seeds=args.seeds,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
