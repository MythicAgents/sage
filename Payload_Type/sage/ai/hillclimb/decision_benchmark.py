"""Offline paired decision benchmark for comparing LLM judgment on Sage recovery cases.

The live autonomous controller intentionally removes most LLM discretion. This benchmark restores a narrow,
measurable decision surface without touching the range: each model sees the same frozen, redacted packet and
must choose a failure label, repair, next action, and whether graph recollection is warranted.

The fixture set is eval data, not runtime policy. It may contain lab-flavored context, but every scored answer is
normalized to portable failure/repair/action labels and every stored response is redacted before persistence.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable
import uuid

try:  # package import
    from ..trajectory.schema import redact_text
    from . import live_seams
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from trajectory.schema import redact_text  # type: ignore
    import live_seams  # type: ignore


DEFAULT_CASES_PATH = Path(__file__).with_name("decision_cases.json")
DEFAULT_RESULTS_PATH = Path(__file__).resolve().parents[2] / ".hillclimb" / "results" / "decision_benchmark.jsonl"
SYSTEM_PROMPT = (
    "You are evaluating an authorized cyber-range decision. Do not execute tools. "
    "Choose only from the packet's candidate values and return exactly one JSON object with keys "
    "failure_label, repair_kind, next_action, should_recollect, rationale. "
    "Use null only when null is an offered candidate. Keep rationale under 40 words."
)
REQUIRED_ANSWER_KEYS = (
    "failure_label",
    "repair_kind",
    "next_action",
    "should_recollect",
    "rationale",
)
_REFUSAL_PATTERNS = (
    re.compile(r"\bi (?:can(?:not|'t)|won't|will not) (?:help|assist|provide)", re.I),
    re.compile(r"\bi(?:'m| am) unable to (?:help|assist|provide)", re.I),
    re.compile(r"\bsorry[, ]+i (?:can(?:not|'t)|won't)", re.I),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _optional_choice(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _choice_list(value: Any, *, field_name: str) -> tuple[str | None, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a non-empty list")
    choices = tuple(_optional_choice(item) for item in value)
    if len(set(choices)) != len(choices):
        raise ValueError(f"{field_name} contains duplicate choices")
    return choices


def _text_is_redacted(value: str) -> bool:
    return redact_text(value) == value


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_json_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


@dataclass(frozen=True)
class ExpectedDecision:
    failure_label: str | None
    repair_kind: str | None
    next_action: str
    should_recollect: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExpectedDecision":
        if not isinstance(data.get("should_recollect"), bool):
            raise ValueError("expected.should_recollect must be boolean")
        next_action = str(data.get("next_action") or "").strip()
        if not next_action:
            raise ValueError("expected.next_action is required")
        return cls(
            failure_label=_optional_choice(data.get("failure_label")),
            repair_kind=_optional_choice(data.get("repair_kind")),
            next_action=next_action,
            should_recollect=bool(data["should_recollect"]),
        )


@dataclass(frozen=True)
class DecisionCase:
    id: str
    category: str
    objective: str
    state: str
    observation: str
    available_actions: tuple[str, ...]
    candidate_failure_labels: tuple[str | None, ...]
    candidate_repairs: tuple[str | None, ...]
    candidate_next_actions: tuple[str, ...]
    expected: ExpectedDecision
    tags: tuple[str, ...] = ()
    source_note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionCase":
        case_id = str(data.get("id") or "").strip()
        category = str(data.get("category") or "").strip()
        if not case_id or not category:
            raise ValueError("case id and category are required")
        objective = str(data.get("objective") or "").strip()
        state = str(data.get("state") or "").strip()
        observation = str(data.get("observation") or "").strip()
        if not objective or not state or not observation:
            raise ValueError(f"{case_id}: objective, state, and observation are required")
        available_actions = tuple(str(item).strip() for item in data.get("available_actions") or ())
        if not available_actions or any(not item for item in available_actions):
            raise ValueError(f"{case_id}: available_actions must be non-empty strings")
        failure_labels = _choice_list(data.get("candidate_failure_labels"), field_name="candidate_failure_labels")
        repairs = _choice_list(data.get("candidate_repairs"), field_name="candidate_repairs")
        next_actions_raw = _choice_list(data.get("candidate_next_actions"), field_name="candidate_next_actions")
        if any(item is None for item in next_actions_raw):
            raise ValueError(f"{case_id}: candidate_next_actions cannot contain null")
        next_actions = tuple(str(item) for item in next_actions_raw)
        expected = ExpectedDecision.from_dict(dict(data.get("expected") or {}))
        if expected.failure_label not in failure_labels:
            raise ValueError(f"{case_id}: expected failure_label is not a candidate")
        if expected.repair_kind not in repairs:
            raise ValueError(f"{case_id}: expected repair_kind is not a candidate")
        if expected.next_action not in next_actions:
            raise ValueError(f"{case_id}: expected next_action is not a candidate")
        tags = tuple(str(item).strip() for item in data.get("tags") or ())
        source_note = str(data.get("source_note") or "").strip()
        text_fields = [objective, state, observation, source_note, *available_actions, *tags]
        text_fields.extend(str(item) for item in failure_labels if item is not None)
        text_fields.extend(str(item) for item in repairs if item is not None)
        text_fields.extend(next_actions)
        if not all(_text_is_redacted(item) for item in text_fields):
            raise ValueError(f"{case_id}: fixture text contains unredacted secret-like material")
        return cls(
            id=case_id,
            category=category,
            objective=objective,
            state=state,
            observation=observation,
            available_actions=available_actions,
            candidate_failure_labels=failure_labels,
            candidate_repairs=repairs,
            candidate_next_actions=next_actions,
            expected=expected,
            tags=tags,
            source_note=source_note,
        )

    def prompt_packet(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "objective": self.objective,
            "state": self.state,
            "latest_observation": self.observation,
            "available_actions": list(self.available_actions),
            "candidate_failure_labels": list(self.candidate_failure_labels),
            "candidate_repairs": list(self.candidate_repairs),
            "candidate_next_actions": list(self.candidate_next_actions),
            "question": (
                "What is the best normalized diagnosis and immediate next action? "
                "should_recollect means issue another graph collection now."
            ),
        }


@dataclass(frozen=True)
class ModelSpec:
    name: str
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    temperature: float | None = 0.0

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
        }


@dataclass
class CaseScore:
    case_id: str
    category: str
    tags: list[str]
    raw_response: str
    parsed_answer: dict[str, Any] | None
    parse_ok: bool
    schema_ok: bool
    refusal: bool
    failure_label_correct: bool
    repair_kind_correct: bool
    next_action_correct: bool
    should_recollect_correct: bool
    fully_correct: bool
    error: str = ""


@dataclass
class ModelSummary:
    cases: int
    parse_success_rate: float
    schema_success_rate: float
    refusal_rate: float
    failure_label_accuracy: float
    repair_kind_accuracy: float
    next_action_accuracy: float
    should_recollect_accuracy: float
    full_accuracy: float


@dataclass
class ModelResult:
    spec: dict[str, Any]
    summary: ModelSummary
    cases: list[CaseScore]


@dataclass
class BenchmarkRun:
    run_id: str
    created_at: str
    cases_path: str
    cases_hash: str
    model_results: list[ModelResult]


def load_cases(path: str | os.PathLike | None = None) -> list[DecisionCase]:
    case_path = Path(path) if path is not None else DEFAULT_CASES_PATH
    raw = json.loads(case_path.read_text(encoding="utf-8"))
    items = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError(f"{case_path}: expected a non-empty cases list")
    cases = [DecisionCase.from_dict(dict(item)) for item in items]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{case_path}: duplicate case ids")
    return cases


def cases_hash(cases: Iterable[DecisionCase]) -> str:
    return _hash_json([asdict(case) for case in cases])


def build_prompt(case: DecisionCase) -> str:
    return (
        "Decision packet:\n"
        f"{json.dumps(case.prompt_packet(), indent=2, ensure_ascii=True)}\n\n"
        "Return only JSON. Do not add markdown fences."
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _looks_like_refusal(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _REFUSAL_PATTERNS)


def _answer_schema_ok(case: DecisionCase, answer: dict[str, Any] | None) -> bool:
    if not isinstance(answer, dict):
        return False
    if set(answer) != set(REQUIRED_ANSWER_KEYS):
        return False
    if answer.get("failure_label") not in case.candidate_failure_labels:
        return False
    if answer.get("repair_kind") not in case.candidate_repairs:
        return False
    if answer.get("next_action") not in case.candidate_next_actions:
        return False
    if not isinstance(answer.get("should_recollect"), bool):
        return False
    rationale = answer.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        return False
    if len(rationale.split()) > 40:
        return False
    return True


def score_response(case: DecisionCase, raw_response: str, *, error: str = "") -> CaseScore:
    parsed = _extract_json_object(raw_response)
    parse_ok = parsed is not None
    schema_ok = _answer_schema_ok(case, parsed)
    expected = case.expected
    failure_label_correct = bool(schema_ok and parsed.get("failure_label") == expected.failure_label)
    repair_kind_correct = bool(schema_ok and parsed.get("repair_kind") == expected.repair_kind)
    next_action_correct = bool(schema_ok and parsed.get("next_action") == expected.next_action)
    should_recollect_correct = bool(schema_ok and parsed.get("should_recollect") == expected.should_recollect)
    fully_correct = bool(
        failure_label_correct
        and repair_kind_correct
        and next_action_correct
        and should_recollect_correct
    )
    return CaseScore(
        case_id=case.id,
        category=case.category,
        tags=list(case.tags),
        raw_response=redact_text(raw_response),
        parsed_answer=_redact_json_value(parsed) if parsed is not None else None,
        parse_ok=parse_ok,
        schema_ok=schema_ok,
        refusal=_looks_like_refusal(raw_response),
        failure_label_correct=failure_label_correct,
        repair_kind_correct=repair_kind_correct,
        next_action_correct=next_action_correct,
        should_recollect_correct=should_recollect_correct,
        fully_correct=fully_correct,
        error=redact_text(error),
    )


def summarize_case_scores(scores: list[CaseScore]) -> ModelSummary:
    total = len(scores)

    def rate(attr: str) -> float:
        return sum(1 for score in scores if bool(getattr(score, attr))) / total if total else 0.0

    return ModelSummary(
        cases=total,
        parse_success_rate=rate("parse_ok"),
        schema_success_rate=rate("schema_ok"),
        refusal_rate=rate("refusal"),
        failure_label_accuracy=rate("failure_label_correct"),
        repair_kind_accuracy=rate("repair_kind_correct"),
        next_action_accuracy=rate("next_action_correct"),
        should_recollect_accuracy=rate("should_recollect_correct"),
        full_accuracy=rate("fully_correct"),
    )


def _response_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    chunks.append(str(text))
            elif item:
                chunks.append(str(item))
        return "\n".join(chunks)
    return str(content or "")


def make_langchain_invoker(spec: ModelSpec) -> Callable[[DecisionCase, str, str], str]:
    from langchain.chat_models import init_chat_model
    from langchain_core.messages import HumanMessage, SystemMessage

    kwargs: dict[str, Any] = {"model_provider": spec.provider, "model": spec.model}
    if spec.api_key is not None:
        kwargs["api_key"] = spec.api_key
    if spec.base_url is not None:
        kwargs["base_url"] = spec.base_url
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature
    try:
        llm = init_chat_model(**kwargs)
    except TypeError:
        kwargs.pop("temperature", None)
        llm = init_chat_model(**kwargs)

    def invoke(_case: DecisionCase, system: str, prompt: str) -> str:
        response = llm.invoke([SystemMessage(content=system), HumanMessage(content=prompt)])
        return _response_text(response)

    return invoke


def make_dry_run_invoker(_spec: ModelSpec) -> Callable[[DecisionCase, str, str], str]:
    def invoke(case: DecisionCase, _system: str, _prompt: str) -> str:
        return json.dumps({
            "failure_label": case.expected.failure_label,
            "repair_kind": case.expected.repair_kind,
            "next_action": case.expected.next_action,
            "should_recollect": case.expected.should_recollect,
            "rationale": "dry-run expected answer",
        })

    return invoke


def run_benchmark(
    cases: list[DecisionCase],
    specs: list[ModelSpec],
    *,
    cases_path: str | os.PathLike = DEFAULT_CASES_PATH,
    invoker_factory: Callable[[ModelSpec], Callable[[DecisionCase, str, str], str]] = make_langchain_invoker,
    progress: Callable[[str], None] | None = None,
) -> BenchmarkRun:
    if not cases:
        raise ValueError("run_benchmark needs at least one case")
    if not specs:
        raise ValueError("run_benchmark needs at least one model spec")
    log = progress or (lambda _message: None)
    model_results: list[ModelResult] = []
    for spec in specs:
        log(f"[decision-benchmark] model={spec.name} provider={spec.provider} id={spec.model}")
        scores: list[CaseScore] = []
        try:
            invoke = invoker_factory(spec)
        except Exception as exc:
            invoke = None
            factory_error = f"{type(exc).__name__}: {exc}"
        else:
            factory_error = ""
        for index, case in enumerate(cases, start=1):
            log(f"[decision-benchmark] {spec.name} case {index}/{len(cases)} {case.id}")
            if invoke is None:
                scores.append(score_response(case, "", error=factory_error))
                continue
            try:
                raw = invoke(case, SYSTEM_PROMPT, build_prompt(case))
                scores.append(score_response(case, raw))
            except Exception as exc:
                scores.append(score_response(case, "", error=f"{type(exc).__name__}: {exc}"))
        model_results.append(ModelResult(
            spec=spec.public_dict(),
            summary=summarize_case_scores(scores),
            cases=scores,
        ))
    return BenchmarkRun(
        run_id=f"decision-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        created_at=utc_now(),
        cases_path=str(Path(cases_path)),
        cases_hash=cases_hash(cases),
        model_results=model_results,
    )


def append_run(path: str | os.PathLike, run: BenchmarkRun) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(_canonical_json(asdict(run)))
        handle.write("\n")


def load_runs(path: str | os.PathLike) -> list[BenchmarkRun]:
    runs: list[BenchmarkRun] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        model_results = []
        for model in raw.get("model_results") or []:
            summary = ModelSummary(**model["summary"])
            cases = [CaseScore(**case) for case in model.get("cases") or []]
            model_results.append(ModelResult(spec=dict(model["spec"]), summary=summary, cases=cases))
        runs.append(BenchmarkRun(
            run_id=str(raw["run_id"]),
            created_at=str(raw["created_at"]),
            cases_path=str(raw["cases_path"]),
            cases_hash=str(raw["cases_hash"]),
            model_results=model_results,
        ))
    return runs


def combine_runs(runs: list[BenchmarkRun]) -> BenchmarkRun:
    if len(runs) < 2:
        raise ValueError("combine_runs needs at least two benchmark runs")
    case_hashes = {run.cases_hash for run in runs}
    if len(case_hashes) != 1:
        raise ValueError("cannot compare runs with different case hashes")
    model_results = [result for run in runs for result in run.model_results]
    names = [str(result.spec.get("name") or "") for result in model_results]
    if len(set(names)) != len(names):
        raise ValueError("cannot compare runs with duplicate model names")
    return BenchmarkRun(
        run_id="compare-" + "-".join(run.run_id for run in runs),
        created_at=max(run.created_at for run in runs),
        cases_path=runs[0].cases_path,
        cases_hash=runs[0].cases_hash,
        model_results=model_results,
    )


def comparison_report(run: BenchmarkRun) -> dict[str, Any]:
    summaries = {result.spec["name"]: asdict(result.summary) for result in run.model_results}
    try:
        expected_by_id = {case.id: asdict(case.expected) for case in load_cases(run.cases_path)}
    except Exception:
        expected_by_id = {}
    pairwise: list[dict[str, Any]] = []
    for left, right in itertools.combinations(run.model_results, 2):
        left_cases = {case.case_id: case for case in left.cases}
        right_cases = {case.case_id: case for case in right.cases}
        diffs = []
        for case_id in sorted(set(left_cases) & set(right_cases)):
            lcase = left_cases[case_id]
            rcase = right_cases[case_id]
            if (
                lcase.fully_correct != rcase.fully_correct
                or _decision_projection(lcase.parsed_answer) != _decision_projection(rcase.parsed_answer)
                or lcase.refusal != rcase.refusal
            ):
                diffs.append({
                    "case_id": case_id,
                    "expected": expected_by_id.get(case_id),
                    "left_full": lcase.fully_correct,
                    "right_full": rcase.fully_correct,
                    "left_answer": lcase.parsed_answer,
                    "right_answer": rcase.parsed_answer,
                    "left_refusal": lcase.refusal,
                    "right_refusal": rcase.refusal,
                })
        pairwise.append({
            "left": left.spec["name"],
            "right": right.spec["name"],
            "full_accuracy_delta": left.summary.full_accuracy - right.summary.full_accuracy,
            "repair_accuracy_delta": left.summary.repair_kind_accuracy - right.summary.repair_kind_accuracy,
            "next_action_accuracy_delta": left.summary.next_action_accuracy - right.summary.next_action_accuracy,
            "differing_cases": diffs,
        })
    return {
        "run_id": run.run_id,
        "created_at": run.created_at,
        "cases_hash": run.cases_hash,
        "summaries": summaries,
        "pairwise": pairwise,
    }


def _format_rate(value: float) -> str:
    return f"{value:.0%}"


def _decision_projection(answer: dict[str, Any] | None) -> dict[str, Any] | None:
    if answer is None:
        return None
    return {key: answer.get(key) for key in REQUIRED_ANSWER_KEYS if key != "rationale"}


def print_report(run: BenchmarkRun) -> None:
    print(f"run_id={run.run_id}")
    print(f"cases_hash={run.cases_hash}")
    print("\nMODEL SUMMARY")
    print("name\tfull\tlabel\trepair\tnext\trecollect\tparse\tschema\trefusal")
    for result in run.model_results:
        summary = result.summary
        print(
            f"{result.spec['name']}\t{_format_rate(summary.full_accuracy)}\t"
            f"{_format_rate(summary.failure_label_accuracy)}\t"
            f"{_format_rate(summary.repair_kind_accuracy)}\t"
            f"{_format_rate(summary.next_action_accuracy)}\t"
            f"{_format_rate(summary.should_recollect_accuracy)}\t"
            f"{_format_rate(summary.parse_success_rate)}\t"
            f"{_format_rate(summary.schema_success_rate)}\t"
            f"{_format_rate(summary.refusal_rate)}"
        )
    report = comparison_report(run)
    for pair in report["pairwise"]:
        print(
            f"\nPAIR {pair['left']} vs {pair['right']}: "
            f"full_delta={pair['full_accuracy_delta']:+.3f} "
            f"repair_delta={pair['repair_accuracy_delta']:+.3f} "
            f"next_action_delta={pair['next_action_accuracy_delta']:+.3f}"
        )
        for diff in pair["differing_cases"]:
            print(
                f"  {diff['case_id']}: "
                f"{pair['left']} full={diff['left_full']} answer={diff['left_answer']} | "
                f"{pair['right']} full={diff['right_full']} answer={diff['right_answer']}"
            )


def _parse_model_arg(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError("--model must be NAME=MODEL_ID")
    name, model = value.split("=", 1)
    name = name.strip()
    model = model.strip()
    if not name or not model:
        raise ValueError("--model must be NAME=MODEL_ID")
    return name, model


def _load_model_specs_from_json(path: str | os.PathLike, defaults: dict[str, Any], temperature: float | None) -> list[ModelSpec]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError("--models-json must contain a non-empty models list")
    specs = []
    for item in items:
        api_key = os.environ.get(str(item.get("api_key_env") or "")) if item.get("api_key_env") else defaults.get("api_key")
        specs.append(ModelSpec(
            name=str(item.get("name") or "").strip(),
            provider=str(item.get("provider") or defaults.get("provider") or "").strip().lower(),
            model=str(item.get("model") or defaults.get("model") or "").strip(),
            api_key=api_key,
            base_url=str(item.get("base_url") or defaults.get("base_url") or "").strip() or None,
            temperature=float(item.get("temperature", temperature)) if item.get("temperature", temperature) is not None else None,
        ))
    return specs


def build_model_specs(args: argparse.Namespace) -> list[ModelSpec]:
    defaults = live_seams.load_sage_defaults()
    if args.models_json:
        specs = _load_model_specs_from_json(args.models_json, defaults, args.temperature)
    else:
        model_args = args.model or [f"current={defaults.get('model') or ''}"]
        specs = []
        for raw in model_args:
            name, model = _parse_model_arg(raw)
            api_key = os.environ.get(args.api_key_env) if args.api_key_env else defaults.get("api_key")
            specs.append(ModelSpec(
                name=name,
                provider=(args.provider or defaults.get("provider") or "").strip().lower(),
                model=model,
                api_key=api_key,
                base_url=(args.base_url or defaults.get("base_url") or "").strip() or None,
                temperature=args.temperature,
            ))
    for spec in specs:
        if not spec.name or not spec.provider or not spec.model:
            raise ValueError(f"invalid model spec: {spec.public_dict()}")
    if len({spec.name for spec in specs}) != len(specs):
        raise ValueError("model names must be unique")
    return specs


def _select_run(runs: list[BenchmarkRun], run_id: str | None) -> BenchmarkRun:
    if not runs:
        raise ValueError("no benchmark runs found")
    if run_id is None:
        return runs[-1]
    for run in runs:
        if run.run_id == run_id:
            return run
    raise ValueError(f"run_id {run_id!r} not found")


def add_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("decision-benchmark", help="run paired offline model decision evals")
    commands = parser.add_subparsers(dest="decision_command", required=True)

    run = commands.add_parser("run", help="run one or more models against the frozen decision cases")
    run.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    run.add_argument("--results", default=str(DEFAULT_RESULTS_PATH))
    run.add_argument("--model", action="append", default=None, help="repeatable NAME=MODEL_ID; defaults to Sage's current model")
    run.add_argument("--models-json", default=None, help="JSON file with per-model provider/model/base_url/api_key_env fields")
    run.add_argument("--provider", default=None, help="shared provider for --model entries; defaults to Sage .env")
    run.add_argument("--base-url", default=None, help="shared base URL for --model entries; defaults to Sage .env")
    run.add_argument("--api-key-env", default=None, help="shared API-key environment variable for --model entries")
    run.add_argument("--temperature", type=float, default=0.0)
    run.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    run.add_argument("--dry-run", action="store_true", help="exercise the evaluator with expected answers; no model calls")
    run.set_defaults(func=_cmd_run)

    report = commands.add_parser("report", help="print a stored benchmark comparison report")
    report.add_argument("--results", default=str(DEFAULT_RESULTS_PATH))
    report.add_argument("--run-id", default=None)
    report.set_defaults(func=_cmd_report)

    compare = commands.add_parser("compare", help="compare models from separate stored runs with the same case hash")
    compare.add_argument("--results", default=str(DEFAULT_RESULTS_PATH))
    compare.add_argument("--run-id", action="append", required=True, help="repeat for each stored run to compare")
    compare.set_defaults(func=_cmd_compare)


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(args.cases)
        if args.limit is not None:
            cases = cases[: max(int(args.limit), 0)]
        specs = build_model_specs(args)
        run = run_benchmark(
            cases,
            specs,
            cases_path=args.cases,
            invoker_factory=make_dry_run_invoker if args.dry_run else make_langchain_invoker,
            progress=lambda message: print(message, flush=True),
        )
        append_run(args.results, run)
        print_report(run)
        print(f"\nrecorded={args.results}")
        return 0
    except Exception as exc:
        print(f"decision-benchmark run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _cmd_report(args: argparse.Namespace) -> int:
    try:
        run = _select_run(load_runs(args.results), args.run_id)
        print_report(run)
        return 0
    except Exception as exc:
        print(f"decision-benchmark report: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _cmd_compare(args: argparse.Namespace) -> int:
    try:
        stored = load_runs(args.results)
        runs = [_select_run(stored, run_id) for run_id in args.run_id]
        print_report(combine_runs(runs))
        return 0
    except Exception as exc:
        print(f"decision-benchmark compare: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline paired decision benchmark for Sage model comparison")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_cli(subparsers)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
