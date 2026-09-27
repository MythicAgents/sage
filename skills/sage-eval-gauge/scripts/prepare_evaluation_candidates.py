#!/usr/bin/env python3
"""Prepare isolated policy-input candidate patches; never modify runtime source.

The candidate set is a cold-start hypothesis set, not five validated improvements.
Its development source is the existing policy state schema and a separately frozen
development corpus. Behavioral distinctness and near-delta coverage require later
authorized measurement. Applying a patch requires explicit architecture authority.
"""
from __future__ import annotations

import argparse
import ast
import copy
import difflib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys


def replay_requests(source: str, packet: dict, catalog: list[dict]) -> dict:
    """Rebuild actual request_payload calls without inference or runtime mutation.

    Historical truncated packets cannot reconstruct unseen input; they refuse.
    The extended context is capture-only and never itself becomes model input.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"))
    from ai.langgraph import policy
    if Path(policy.__file__).read_text() != source:
        raise ValueError("replay baseline differs from the loaded shared policy helpers")
    context = packet.get("replay_context")
    if not isinstance(context, dict) or context.get("schema") != "m3-policy-input-replay-v1":
        raise ValueError("complete prospective replay_context missing; legacy packet is not reusable")
    captured_catalog = context.get("capability_catalog")
    if not isinstance(captured_catalog, list) or captured_catalog != catalog:
        raise ValueError("replay capability_catalog is missing or differs from the captured catalog")
    normalized = packet["normalized_state"]
    state = SimpleNamespace(
        footholds=[SimpleNamespace(**item) for item in normalized["footholds"]],
        graph_facts=list(normalized["graph_facts"]),
        hops=[SimpleNamespace(**item) for item in context["hops"]],
        achieved_effects=lambda: set(normalized["achieved_effects"]),
    )
    history = [SimpleNamespace(**item) for item in context["history"]]
    candidates = [SimpleNamespace(**item) for item in packet["admissible_frontier"]]
    if policy._normalized_state_payload(state) != normalized or policy._prior_decisions_payload(history) != packet["prior_decisions"]:
        raise ValueError("full context does not reproduce the original visible state/history")
    if packet.get("selection_contract") != policy.SELECTION_CONTRACT_LLM:
        raise ValueError("candidate request replay requires the declared LLM policy path")
    def request(text):
        module = ast.parse(text)
        helpers = [node for node in module.body if isinstance(node, ast.FunctionDef)
                   and node.name in {"_normalized_state_payload", "_prior_decisions_payload"}]
        classes = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "LLMPolicy"]
        methods = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name == "request_payload"]
        selected = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *helpers, *methods], type_ignores=[])
        namespace = dict(vars(policy))
        exec(compile(ast.fix_missing_locations(selected), "<isolated-policy-input-replay>", "exec"), namespace)
        owner = SimpleNamespace(catalog=catalog, mode=policy.POLICY_LLM, selection_contract=policy.SELECTION_CONTRACT_LLM)
        return namespace["request_payload"](owner, packet["objective"], state, candidates, history, packet["budgets"])
    baseline = request(source)
    rows = []
    for name, before, after, _ in VARIANTS:
        value = request(variant_source(source, name, before, after))
        rows.append({"id": name, "request": value, "input_changed": value != baseline})
    rows += [{"id": name, "request": baseline, "input_changed": False}
             for name in ("whitespace-sham", "label-sham")]
    return {"baseline_request": baseline, "variants": rows, "provider_requests": 0,
            "outcomes_measured": False, "live_authorized": False}

POLICY_PATH = "Payload_Type/sage/ai/langgraph/policy.py"
VARIANTS = (
    ("history-12", "for item in history[-8:]", "for item in history[-12:]",
     "Four additional actual prior decisions may preserve relevant context or add distraction."),
    ("outcomes-24", 'for item in values("hops")[-12:]', 'for item in values("hops")[-24:]',
     "A longer observed-outcome window may preserve failed-route context or dilute current evidence."),
    ("outcome-effect-links", '"status": str(getattr(item, "status", "") or ""),',
     '"status": str(getattr(item, "status", "") or ""),\n'
     '                "satisfied_effects": list(getattr(item, "satisfied_effects", []) or []),',
     "Link existing satisfied effects to their observed outcomes without adding facts."),
    ("outcome-preconditions", '"status": str(getattr(item, "status", "") or ""),',
     '"status": str(getattr(item, "status", "") or ""),\n'
     '                "preconditions": list(getattr(item, "preconditions", []) or []),',
     "Expose existing hop preconditions alongside results; no new admissibility rule."),
    ("history-expectations", '"capability": item.selected_capability,',
     '"capability": item.selected_capability,\n'
     '            "expected_evidence": item.expected_evidence,',
     "Carry the prior model's short evidence expectation, explicitly not proof, into its next decision."),
)


def variant_source(source: str, name: str, before: str, after: str) -> str:
    function_name = "_prior_decisions_payload" if name.startswith("history-") else "_normalized_state_payload"
    nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
             and node.name == function_name]
    if len(nodes) != 1:
        raise ValueError(f"{name}: source function changed; inspect before rebinding")
    node, lines = nodes[0], source.splitlines(True)
    original = "".join(lines[node.lineno - 1:node.end_lineno])
    if original.count(before) != 1:
        raise ValueError(f"{name}: source seam changed; inspect before rebinding")
    candidate = "".join(lines[:node.lineno - 1]) + original.replace(before, after, 1) + "".join(lines[node.end_lineno:])
    count = candidate_change_count(source, candidate, function_name)
    if count != 1:
        raise ValueError(f"{name}: unsupported or multi-edit attribution")
    return candidate


def candidate_change_count(source: str, candidate: str, function_name: str) -> int:
    """Derive attribution under the closed one-constant/one-dictionary-field grammar."""
    original_module, candidate_module = ast.parse(source), ast.parse(candidate)
    original = next((n for n in original_module.body if isinstance(n, ast.FunctionDef)
                     and n.name == function_name), None)
    changed = next((n for n in candidate_module.body if isinstance(n, ast.FunctionDef)
                    and n.name == function_name), None)
    if original is None or changed is None:
        raise ValueError("candidate helper changed")
    outside = copy.deepcopy(candidate_module)
    outside.body[outside.body.index(next(n for n in outside.body if isinstance(n, ast.FunctionDef)
                                         and n.name == function_name))] = copy.deepcopy(original)
    if ast.dump(outside, include_attributes=False) != ast.dump(original_module, include_attributes=False):
        raise ValueError("candidate changed outside the selected helper")
    stripped = copy.deepcopy(changed); insertions = 0
    original_dicts = [n for n in ast.walk(original) if isinstance(n, ast.Dict)]
    changed_dicts = [n for n in ast.walk(stripped) if isinstance(n, ast.Dict)]
    if len(original_dicts) != len(changed_dicts):
        raise ValueError("unsupported dictionary transformation")
    changed_keys = [n for n in ast.walk(stripped) if isinstance(n, ast.Dict)]
    for before_dict, after_dict in zip(original_dicts, changed_keys, strict=True):
        if len(after_dict.keys) < len(before_dict.keys):
            raise ValueError("dictionary field removal is unsupported")
        before_keys = [ast.dump(k, include_attributes=False) for k in before_dict.keys]
        after_keys = [ast.dump(k, include_attributes=False) for k in after_dict.keys]
        extra = [i for i, key in enumerate(after_keys) if key not in before_keys]
        if len(after_keys) != len(before_keys) + len(extra):
            continue
        for i in reversed(extra):
            del after_dict.keys[i]; del after_dict.values[i]
        insertions += len(extra)
    if ast.dump(stripped, include_attributes=False) == ast.dump(original, include_attributes=False):
        return insertions
    original_constants = [n for n in ast.walk(original) if isinstance(n, ast.Constant)]
    changed_constants = [n for n in ast.walk(stripped) if isinstance(n, ast.Constant)]
    if len(original_constants) != len(changed_constants):
        raise ValueError("unsupported candidate transformation")
    mismatches = [(a, b) for a, b in zip(original_constants, changed_constants, strict=True)
                  if type(a.value) is not type(b.value) or a.value != b.value]
    restored = copy.deepcopy(stripped)
    for a, b in zip(original_constants,
                    [n for n in ast.walk(restored) if isinstance(n, ast.Constant)], strict=True):
        b.value = a.value
    if ast.dump(restored, include_attributes=False) != ast.dump(original, include_attributes=False):
        raise ValueError("unsupported candidate transformation")
    return insertions + len(mismatches)


def build_candidates(source: str) -> list[dict]:
    ast.parse(source)
    outputs = []
    for name, before, after, hypothesis in VARIANTS:
        candidate = variant_source(source, name, before, after)
        ast.parse(candidate)
        patch = "".join(difflib.unified_diff(source.splitlines(True), candidate.splitlines(True),
                                            fromfile=f"a/{POLICY_PATH}", tofile=f"b/{POLICY_PATH}"))
        change_count = candidate_change_count(source, candidate,
            "_prior_decisions_payload" if name.startswith("history-") else "_normalized_state_payload")
        outputs.append({"id": name, "kind": "working_hypothesis", "change_count": change_count,
                        "hypothesis": hypothesis, "patch": patch,
                        "source_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
                        "behaviorally_validated": False})
    sham = source.replace("def _prior_decisions_payload(", "\n\ndef _prior_decisions_payload(", 1)
    if ast.dump(ast.parse(sham), include_attributes=False) != ast.dump(ast.parse(source), include_attributes=False):
        raise ValueError("whitespace sham changed the executable AST")
    outputs.append({"id": "whitespace-sham", "kind": "sham", "change_count": 0,
                    "hypothesis": "Executable AST is identical; no behavioral improvement expected.",
                    "patch": "".join(difflib.unified_diff(source.splitlines(True), sham.splitlines(True),
                                fromfile=f"a/{POLICY_PATH}", tofile=f"b/{POLICY_PATH}")),
                    "source_sha256": hashlib.sha256(sham.encode()).hexdigest(),
                    "behaviorally_validated": False})
    outputs.append({"id": "label-sham", "kind": "sham", "change_count": 0,
                    "hypothesis": "Only evaluator-side opaque candidate label changes; policy input is identical.",
                    "patch": "", "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "behaviorally_validated": False})
    return outputs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--development-corpus-sha256", required=True)
    parser.add_argument("--replay-packet", type=Path)
    parser.add_argument("--catalog", type=Path)
    args = parser.parse_args(argv)
    try:
        if len(args.development_corpus_sha256) != 64 or any(x not in "0123456789abcdef" for x in args.development_corpus_sha256):
            raise ValueError("development corpus must have an explicit SHA-256 binding")
        source = (args.source_root / POLICY_PATH).read_text()
        document = {"schema": "m3-cold-start-candidates-v1", "baseline_path": POLICY_PATH,
                    "baseline_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "development_corpus_sha256": args.development_corpus_sha256,
                    "variants": build_candidates(source), "applied_to_runtime": False,
                    "architecture_approval_required": True, "live_authorized": False,
                    "near_delta_coverage_established": False}
        if bool(args.replay_packet) != bool(args.catalog):
            raise ValueError("--replay-packet and --catalog must be provided together")
        if args.replay_packet:
            document["request_replay"] = replay_requests(source,
                json.loads(args.replay_packet.read_text()), json.loads(args.catalog.read_text()))
        with args.output.open("x") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
    except (OSError, ValueError, SyntaxError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 2
    print(json.dumps({"status": "prepared_unapplied", "variants": len(document["variants"]),
                      "baseline_sha256": document["baseline_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
