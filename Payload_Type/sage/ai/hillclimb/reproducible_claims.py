"""Offline reproduction of source-bound development measurement claims.

This is a reporting consumer, not an evidence authenticator or a promotion gate.
It runs only named pure reducers / the existing local native-observation replay.
All inputs and labels resolve through the existing SourceBinding vocabulary.
No credentials, environment discovery, provider clients, or live adapters run.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import signal
import threading
from time import monotonic
from typing import Any, Mapping

from .composite_gauge import validate_measurement_identity
from .experiment_contracts import select_comparison_results
from .tradecraft_evidence import EvidenceLocator, SourceBinding, canonical_sha256, resolve_locator

SCHEMA = "sage-reproducible-claims-v1"
REPORT_SCHEMA = "sage-reproducible-claims-report-v1"
ROOT = Path(__file__).resolve().parents[4]
IDENTITY_FIELDS = frozenset({
    "protocol_id", "pair_id", "logical_seed", "policy_arm", "model_provider", "model_id",
    "model_version", "backend_route", "generation_config", "wrapper_id", "wrapper_revision",
    "authority_mode", "execution_path", "source_version",
})
EXECUTION_PATHS = frozenset({"policy_selected_controller", "supervisor_worker", "true_naive_raw_tools"})


class ClaimError(ValueError):
    pass


def _bounded_effect_replay(inputs: dict, *, wall_limit: float = 30.0) -> tuple[dict, dict]:
    """Bound pure parser work on the offline CLI thread and retain failed cost."""
    from .range_state import VerifierCost
    from .effect_registry import contract_for
    if (threading.current_thread() is not threading.main_thread()
            or not hasattr(signal, "setitimer") or signal.getitimer(signal.ITIMER_REAL)[0]):
        raise ClaimError("bounded effect replay requires an unoccupied main-thread timer")
    effect = inputs.get("effect", "")
    cost = VerifierCost(effect=effect, oracle_id=contract_for(effect).oracle_id,
                        raw_probe_invocations=1, control_plane_reads=0,
                        control_plane_reads_reason="pure offline parser; no control-plane client",
                        declared_wall_seconds=wall_limit, declared_control_plane_reads=0,
                        completeness="complete", completeness_reason="offline parser invocation measured")
    old_handler = signal.getsignal(signal.SIGALRM)
    started = monotonic()
    def expired(_signal, _frame):
        raise TimeoutError("offline effect replay wall limit exceeded")
    try:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, wall_limit)
        result = replay_capability_effect(**inputs)
        cost.limit_disposition = "within"
    except Exception as exc:
        cost.failed_invocations = 1
        cost.timeout_invocations = int(isinstance(exc, TimeoutError))
        cost.limit_disposition = "exceeded" if cost.timeout_invocations else "within"
        error = ClaimError("bounded effect replay failed")
        error.verifier_cost = cost.to_record()
        error.verifier_cost["wall_seconds"] = monotonic() - started
        raise error from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
    cost.wall_seconds = monotonic() - started
    return result, cost.to_record()


def evaluator_bindings(repo_root: Path = ROOT) -> dict[str, str]:
    """Bind the evaluator program, including parsers and operator projections.

    The complete evaluator tree is intentional: an imported helper changing also
    changes this identity. Historical recipes retain their old binding unchanged.
    """
    prefixes = ("Payload_Type/sage/ai/hillclimb", "skills/sage-eval-gauge/scripts")
    files = {p for prefix in prefixes for p in (repo_root / prefix).rglob("*.py")}
    files.update(repo_root / p for p in (
        "Payload_Type/sage/ai/langgraph/effect_proofs.py",
        "Payload_Type/sage/ai/langgraph/effect_ledger.py",
        "Payload_Type/sage/ai/langgraph/proof_boundary.py",
        "Payload_Type/sage/ai/langgraph/credential_artifacts.py",
        "Payload_Type/sage/ai/langgraph/effect_parsers.py",
        "Payload_Type/sage/ai/langgraph/capabilities.py",
        "Payload_Type/sage/ai/langgraph/mythic_native_adapter.py",
        "Payload_Type/sage/ai/langgraph/mythic_capability_adapter.py",
    ) if (repo_root / p).is_file())
    return {str(p.relative_to(repo_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}


def measurement_labels(identities: Any) -> dict[str, Any]:
    """Explicit incomplete status for legacy reports; never manufacture labels."""
    errors, valid = [], []
    if not isinstance(identities, list) or not identities:
        errors.append("measurement identities unavailable")
    else:
        for index, identity in enumerate(identities):
            try:
                value = validate_measurement_identity(identity)
                if value["execution_path"] not in EXECUTION_PATHS:
                    raise ClaimError("unknown execution_path")
                if any(value[key].casefold() in {"unknown", "unavailable", "unspecified"}
                       for key in IDENTITY_FIELDS - {"logical_seed", "generation_config"}):
                    raise ClaimError("unknown measurement label")
                valid.append(value)
            except (TypeError, ValueError, AttributeError) as exc:
                errors.append(f"identity {index}: {exc}")
    return {"performance_assertion_allowed": not errors, "identities": valid,
            "identity_failures": errors}


def comparison_labels(identities: Any) -> dict[str, Any]:
    """Validate same-model comparison labels while retaining arm asymmetries."""
    labels = measurement_labels(identities)
    if not labels["performance_assertion_allowed"]:
        return labels
    shared = ("protocol_id", "model_provider", "model_id", "model_version", "backend_route",
              "authority_mode", "source_version")
    failures = ["incompatible retained measurement label: " + key for key in shared
                if len({identity[key] for identity in labels["identities"]}) != 1]
    pairs: dict[str, list[dict]] = {}
    for identity in labels["identities"]:
        pairs.setdefault(identity["pair_id"], []).append(identity)
    arms = {identity["policy_arm"] for identity in labels["identities"]}
    if len(arms) > 1 and any(
        len({identity["logical_seed"] for identity in pair}) != 1
        or {identity["policy_arm"] for identity in pair} != arms or len(pair) != len(arms)
        for pair in pairs.values()
    ):
        failures.append("incompatible pair membership or logical seed")
    return {**labels, "performance_assertion_allowed": not failures, "identity_failures": failures}


def _load_script(name: str):
    path = ROOT / "skills/sage-eval-gauge/scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_claims_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _reduce(name: str, inputs: dict[str, Any]) -> Any:
    # No user-supplied import name or executable path is accepted here.
    from . import fitness, composite_gauge, gate_experiment
    reducers = {
        "canonical_row": fitness.canonical_row_verdict,
        "canonical_aggregate": fitness.canonical_aggregate_verdict,
        "composite_row": composite_gauge.build_composite_row,
        "composite_report": composite_gauge.build_paired_report,
    }
    if name in reducers:
        args = {key: value for key, value in inputs.items()
                if not (name == "canonical_row" and key == "measurement_identity")}
        result = reducers[name](**args)
    elif name == "trust_walker_pair":
        result = _load_script("run_trust_walker_pilot").replay_pair(**inputs)
    elif name == "comparison":
        result = _load_script("summarize_comparison").summarize(**inputs)
    elif name == "trust_walker_batch":
        result = reduce_trust_walker_batch(**inputs)
    elif name == "trust_walker_improvement":
        result = _load_script("run_trust_walker_pilot").reduce_improvement_replays(**inputs)
    elif name == "scorecard":
        result = replay_scorecard(**inputs)
    elif name == "capability_effect":
        result = replay_capability_effect(**inputs)
    elif name == "noise_floor":
        from .reliability import noise_floor
        result = noise_floor([fitness.ScoreCard(**card) for card in inputs["scorecards"]],
                             **{k: v for k, v in inputs.items() if k != "scorecards"})
    elif name == "gate_experiment":
        if {"write_record", "results_dir"} & set(inputs):
            raise ClaimError("claim replay cannot write gate records")
        result = gate_experiment.aggregate_gate_experiment(
            {name: [fitness.ScoreCard(**card) for card in cards]
             for name, cards in inputs["cards_by_config"].items()},
            **{k: v for k, v in inputs.items() if k != "cards_by_config"}, write_record=False,
        ).to_dict()
    else:
        raise ClaimError(f"unsupported offline reducer: {name}")
    return asdict(result) if is_dataclass(result) else result


REDUCERS = ("canonical_row", "canonical_aggregate", "composite_row", "composite_report",
            "comparison", "scorecard", "noise_floor", "gate_experiment",
            "trust_walker_pair", "trust_walker_batch", "trust_walker_improvement", "capability_effect")


def replay_capability_effect(effect: str, evidence: dict, *, expected_target: str,
                             measurement_identity: dict, expected_subject: str = "") -> dict:
    """Replay current pure effect producers without tasking or changing gauge weights.

    Raw observations and artifact bytes stay inside the process. Only the typed,
    redacted proof result is returned. A missing capture is an unmeasured effect,
    never a negative observation or a generated successful capability verdict.
    """
    from . import (account_context, certificate_material, credential_effects,
                   directory_endpoint_effects, effect_registry, replication_rights,
                   ticket_context, trust_walker_outcome)
    validate_measurement_identity(measurement_identity)
    producers = {
        "ACCOUNT_KERBEROS_CONTEXT": account_context.account_kerberos_context_observation,
        "MANAGED_LOCAL_ADMIN_SECRET": credential_effects.managed_local_admin_secret_candidate,
        "MANAGED_LOCAL_ADMIN_ACCESS": credential_effects.managed_local_admin_access_candidate,
        "HOST_CREDENTIAL_MATERIAL": credential_effects.host_credential_material_candidate,
        "ENDPOINT_PROTECTION_STATE": directory_endpoint_effects.endpoint_protection_observation,
        "RBCD_RIGHTS": directory_endpoint_effects.rbcd_rights_observation,
        "REPLICATION_RIGHTS_PARENT": replication_rights.parent_replication_rights_observation,
        "CROSS_DOMAIN_CONTEXT": ticket_context.cross_domain_context_candidate,
    }
    if hasattr(credential_effects, "account_credential_material_candidate"):
        producers["ACCOUNT_CREDENTIAL_MATERIAL"] = credential_effects.account_credential_material_candidate
    if not expected_target or not isinstance(evidence, dict):
        raise ClaimError("effect replay requires an exact target and evidence packet")
    args = dict(evidence)
    if "baseline_task_ids" in args:
        values = args["baseline_task_ids"]
        if not isinstance(values, (list, set)) or any(type(v) is not int or v <= 0 for v in values):
            raise ClaimError("invalid pre-arm task inventory")
        args["baseline_task_ids"] = set(values)
    if effect in {"DA_CHILD", "DA_PARENT"}:
        observation = trust_walker_outcome.domain_admin_observation(effect=effect, **args)
    elif effect in {"CERT_FORGED", "CA_KEY_EXPORTED", "CERTIFICATE_ENROLLED"}:
        observation = certificate_material.certificate_material_observation(effect, **args)
    elif effect in producers:
        observation = producers[effect](**args)
    else:
        raise ClaimError("no offline effect producer for this effect")
    proof = (effect_registry.unavailable_result(effect) if observation is None else
             effect_registry.admit_observation(effect, observation, expected_target=expected_target,
                                               expected_subject=expected_subject))
    return proof.to_record()


def replay_scorecard(run_record: dict, scenario: dict, proof_results: dict,
                     process: dict | None = None, verifier_costs: dict | None = None) -> dict:
    """Replay C1/C1b/C2 from explicit retained inputs, never an ambient ledger/DB."""
    from . import effect_registry, fitness
    from .process_state import ProcessSignals
    from .range_state import GroundTruth, Milestone, MilestoneSpec, Scenario, VerifierCost, _target_for
    if set(scenario) - {"name", "engagement_id", "objective", "domains", "spec_overrides", "milestone_subset"}:
        raise ClaimError("recorded scenario cannot install callable probes")
    config = dict(scenario)
    if "spec_overrides" in config:
        config["spec_overrides"] = {Milestone[k]: MilestoneSpec(**v) for k, v in config["spec_overrides"].items()}
    if config.get("milestone_subset") is not None:
        config["milestone_subset"] = tuple(Milestone[k] for k in config["milestone_subset"])
    selected = Scenario(**config)
    if not proof_results:
        raise ClaimError("empty recorded proof collection")
    proofs = {Milestone[k]: effect_registry.admit_record(Milestone[k], v, expected_target=_target_for(selected, Milestone[k]))
              for k, v in proof_results.items()}
    flags = {key: value.proven for key, value in proofs.items()}
    truth = GroundTruth(selected.name, flags, max((key for key, value in flags.items() if value), default=Milestone.FOOTHOLD),
                        [key for key, value in proofs.items() if value.disposition.value in {"invalid", "unmeasured"}], proofs)
    truth.verifier_costs = {Milestone[k]: VerifierCost.from_record(v) for k, v in (verifier_costs or {}).items()}
    result = fitness.score(run_record, truth, ProcessSignals(**process) if process is not None else None, scenario=selected)
    return asdict(result)


def reduce_trust_walker_batch(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce replay outputs only after checking the exact current parser binding."""
    if not pairs:
        raise ClaimError("empty pair collection")
    runner = ROOT / "skills/sage-eval-gauge/scripts/run_trust_walker_pilot.py"
    parser = ROOT / "Payload_Type/sage/ai/hillclimb/trust_walker_outcome.py"
    expected = {"projection_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
                "outcome_parser_sha256": hashlib.sha256(parser.read_bytes()).hexdigest()}
    ledgers, rows = set(), []
    for pair in pairs:
        if any(pair.get(k) != v for k, v in expected.items()):
            raise ClaimError("aggregate evaluator/parser binding is stale")
        if pair.get("reproduced") is not True or not pair.get("dimensions") or not all(pair["dimensions"].values()):
            raise ClaimError("aggregate input is not a complete pair replay")
        ledger = pair.get("ledger_sha256")
        if not isinstance(ledger, str) or len(ledger) != 64 or ledger in ledgers:
            raise ClaimError("missing or duplicate aggregate pair identity")
        ledgers.add(ledger)
        arms = pair.get("arms", [])
        if len(arms) != 2 or len({arm["arm"] for arm in arms}) != 2:
            raise ClaimError("aggregate pair must contain two distinct arms")
        for arm in arms:
            if any(type(arm.get(k)) is not int or arm[k] < 0
                   for k in ("model_calls", "total_tokens", "native_tasks")):
                raise ClaimError("aggregate resource axis is unavailable")
            disposition = arm["outcome"]["disposition"]
            if disposition not in {"proven", "disproven"}:
                raise ClaimError("aggregate outcome is not measured")
            rows.append({"ledger_sha256": ledger, "arm": arm["arm"],
                         "source_identity": arm.get("source_identity"), "outcome": disposition,
                         **{k: arm[k] for k in ("model_calls", "total_tokens", "native_tasks")}})
    return {"schema": "sage-trust-walker-replay-aggregate-v1", "pairs": len(pairs), "rows": rows,
            "arms": {arm: {"n": sum(r["arm"] == arm for r in rows),
                            "successes": sum(r["arm"] == arm and r["outcome"] == "proven" for r in rows)}
                     for arm in sorted({r["arm"] for r in rows})},
            "resources": {k: sum(r[k] for r in rows) for k in ("model_calls", "total_tokens", "native_tasks")},
            "monetary_cost_usd": None, "monetary_cost_status": "unavailable", **expected}


def _pointer(document: Any, pointer: str) -> Any:
    # Reuse the validated locator parser even for recomputed claim outputs.
    document = {"result": document}
    binding = SourceBinding("claim", "recomputed", canonical_sha256(document), document)
    return resolve_locator(EvidenceLocator("claim", pointer, "recomputed", binding.source_sha256), {"claim": binding})


def verify_claims(path: str | Path) -> dict[str, Any]:
    """Verify all declared claims, including superseded revisions, in one command.

    No output value is printed on failure. A binding proves local consistency,
    not authenticity of the retained observations or historical elapsed time.
    """
    manifest_path = Path(path).resolve()
    manifest = json.loads(manifest_path.read_text())
    # Provider accounting is a resource-only sidecar, not a performance claim.
    # Keep the existing one-command replay entrypoint for these new numbers too.
    from . import accounting_report, phoenix_report
    if isinstance(manifest, dict) and manifest.get("schema") in {accounting_report.SCHEMA, phoenix_report.SCHEMA}:
        bundle = accounting_report.verify_bundle(manifest_path)
        return {"verified": True, "accounting_only": True,
                "performance_assertion_allowed": False, "report": bundle["report"]}
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "evaluator_bindings", "sources", "claims"}:
        raise ClaimError("claim manifest fields differ from the schema")
    if manifest["schema"] != SCHEMA or manifest["evaluator_bindings"] != evaluator_bindings():
        raise ClaimError("claim evaluator binding is missing or stale")
    bindings, used, source_paths = {}, set(), {}
    for ref, spec in manifest["sources"].items():
        if set(spec) != {"path", "sha256", "version", "format"} or spec["format"] not in {"json", "jsonl", "binary"}:
            raise ClaimError(f"source binding is malformed: {ref}")
        source_path = (manifest_path.parent / spec["path"]).resolve()
        raw = source_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"].removeprefix("sha256:"):
            raise ClaimError(f"source bytes changed: {ref}")
        document = ({"bytes": raw} if spec["format"] == "binary" else
                    json.loads(raw) if spec["format"] == "json" else
                    [json.loads(line) for line in raw.splitlines() if line.strip()])
        bindings[ref] = SourceBinding(ref, spec["version"], digest, document)
        source_paths[ref] = source_path
    outputs, identities, superseded, reports, reducer_by_id = {}, {}, set(), [], {}

    def resolve(value):
        if isinstance(value, dict) and set(value) == {"source"}:
            locator = EvidenceLocator.from_mapping(value["source"])
            used.add(locator.source_ref)
            return resolve_locator(locator, bindings)
        if isinstance(value, dict) and set(value) == {"claim", "pointer"}:
            if value["claim"] not in outputs:
                raise ClaimError("orphan or forward claim dependency")
            return _pointer(outputs[value["claim"]], value["pointer"])
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        raise ClaimError("claim inputs must be source-bound; literals belong in retained source documents")

    claims = manifest["claims"]
    if not isinstance(claims, list) or not claims:
        raise ClaimError("empty claim collection")
    for claim in claims:
        if set(claim) != {"id", "reducer", "inputs", "identities", "expected", "supersedes", "amendment_reason"}:
            raise ClaimError("unknown asserted-result fields")
        key = claim["id"]
        if not isinstance(key, str) or not key.strip() or key in outputs:
            raise ClaimError("missing or duplicate claim ID")
        identity = resolve(claim["identities"])
        labels = measurement_labels(identity)
        if not labels["performance_assertion_allowed"]:
            raise ClaimError("missing or stale measurement labels: " + "; ".join(labels["identity_failures"]))
        predecessor = claim["supersedes"]
        if predecessor is not None:
            if predecessor not in outputs or predecessor in superseded or not claim["amendment_reason"]:
                raise ClaimError("orphan, forked, or unexplained result amendment")
            for field in ("protocol_id", "pair_id", "policy_arm"):
                if [r[field] for r in identity] != [r[field] for r in identities[predecessor]]:
                    raise ClaimError("amendment crosses measurement identity")
            superseded.add(predecessor)
        elif claim["amendment_reason"] is not None:
            raise ClaimError("amendment reason has no prior result")
        inputs = resolve(claim["inputs"])
        if claim["reducer"] in {"trust_walker_batch", "trust_walker_improvement", "noise_floor"}:
            field, child = (("pairs", "trust_walker_pair") if claim["reducer"] != "noise_floor"
                            else ("scorecards", "scorecard"))
            refs = claim["inputs"].get(field) if isinstance(claim["inputs"], dict) else None
            if not isinstance(refs, list) or not refs or any(
                not isinstance(ref, dict) or set(ref) != {"claim", "pointer"}
                or ref["pointer"] != "/result" or reducer_by_id.get(ref["claim"]) != child for ref in refs
            ):
                raise ClaimError("aggregate requires recomputed producer claims, not generated pass flags")
            parent_labels = [label for ref in refs for label in identities[ref["claim"]]]
            if canonical_sha256(identity) != canonical_sha256(parent_labels):
                raise ClaimError("aggregate labels differ from its recomputed producer claims")
        if claim["reducer"] == "scorecard":
            if identity != [inputs["run_record"].get("measurement_identity")]:
                raise ClaimError("scorecard labels differ from retained run metadata")
        if claim["reducer"] == "capability_effect" and identity != [inputs.get("measurement_identity")]:
            raise ClaimError("effect labels differ from retained producer metadata")
        if claim["reducer"] == "comparison":
            scoped_records = select_comparison_results(inputs["records"], inputs["scenario"])
            retained_labels = [row.get("measurement_identity") for row in scoped_records]
            if identity != retained_labels:
                raise ClaimError("comparison labels differ from retained result metadata")
            if any(not isinstance(row.get("card"), dict)
                   or row["card"].get("measurement_identity") != row.get("measurement_identity")
                   for row in scoped_records):
                raise ClaimError("comparison card labels differ from retained result metadata")
            if not comparison_labels(retained_labels)["performance_assertion_allowed"]:
                raise ClaimError("comparison contains incompatible retained measurement labels")
        if claim["reducer"] == "composite_row" and identity != [inputs.get("identity")]:
            raise ClaimError("composite labels differ from replay inputs")
        if claim["reducer"] == "canonical_row":
            if (identity != [inputs.get("measurement_identity")]
                    or len(identity) != 1 or identity[0]["policy_arm"] != inputs.get("policy_arm")):
                raise ClaimError("canonical row labels differ from its producer packet or policy arm")
        if claim["reducer"] == "canonical_aggregate":
            parent_labels = []
            for field in ("row_verdicts", "causal_row_verdicts", "transfer_row_verdicts"):
                if field not in inputs:
                    continue
                refs = claim["inputs"].get(field)
                if not isinstance(refs, list) or any(
                    not isinstance(ref, dict) or set(ref) != {"claim", "pointer"}
                    or ref["pointer"] != "/result" or reducer_by_id.get(ref["claim"]) != "canonical_row" for ref in refs
                ):
                    raise ClaimError("canonical aggregate requires recomputed row claims")
                parent_labels.extend(label for ref in refs for label in identities[ref["claim"]])
            if not parent_labels or identity != parent_labels:
                raise ClaimError("canonical aggregate labels differ from its row claims")
        if claim["reducer"] == "gate_experiment":
            raw_inputs = claim["inputs"]
            groups = raw_inputs.get("cards_by_config") if isinstance(raw_inputs, dict) else None
            if not isinstance(groups, dict) or not groups:
                raise ClaimError("gate aggregate requires explicit recomputed ScoreCard groups")
            parent_labels = []
            for refs in groups.values():
                if not isinstance(refs, list) or not refs or any(
                    not isinstance(ref, dict) or set(ref) != {"claim", "pointer"}
                    or ref["pointer"] != "/result" or reducer_by_id.get(ref["claim"]) != "scorecard" for ref in refs
                ):
                    raise ClaimError("gate aggregate requires recomputed ScoreCard claims")
                parent_labels.extend(label for ref in refs for label in identities[ref["claim"]])
            if identity != parent_labels:
                raise ClaimError("gate aggregate labels differ from its ScoreCard claims")
        if claim["reducer"] == "composite_report" and identity != [row.get("identity") for row in inputs.get("records", [])]:
            raise ClaimError("composite report labels differ from its row identities")
        # Paths for native replay are bound to the retained pilot ledger itself,
        # not taken from an arbitrary executable or a live command recipe.
        if claim["reducer"] == "trust_walker_pair":
            if set(claim["inputs"]) != {"ledger"}:
                raise ClaimError("pair replay requires a bound ledger")
            locator = EvidenceLocator.from_mapping(claim["inputs"]["ledger"]["source"])
            ledger_path = source_paths[locator.source_ref]
            if ledger_path.name != "pilot.jsonl" or inputs["ledger"] != bindings[locator.source_ref].document[0]:
                raise ClaimError("pair replay must bind its original ledger header")
            inputs = {"artifact_dir": ledger_path.parent}
        replay_started = monotonic()
        verifier_cost = None
        if claim["reducer"] == "capability_effect":
            result, verifier_cost = _bounded_effect_replay(inputs)
        else:
            result = _reduce(claim["reducer"], inputs)
        replay_wall_seconds = monotonic() - replay_started
        if claim["reducer"] == "trust_walker_pair":
            if (result.get("performance_assertion_allowed") is not True
                    or canonical_sha256(identity) != canonical_sha256([arm.get("measurement_identity") for arm in result["arms"]])):
                raise ClaimError("pair labels are missing or differ from its retained producer")
            by_arm = {r["policy_arm"]: r for r in identity}
            if set(by_arm) != {a["arm"] for a in result["arms"]}:
                raise ClaimError("pair labels do not cover its measured arms")
            for arm in result["arms"]:
                label = by_arm[arm["arm"]]
                if (label["source_version"] != arm["source_identity"]
                        or label["model_id"] != result["contract"]["model"]
                        or label["model_provider"] != result["contract"]["provider"]):
                    raise ClaimError("pair measurement labels contradict retained execution")
        expected = resolve(claim["expected"])
        if canonical_sha256(result) != canonical_sha256(expected):
            raise ClaimError(f"asserted result does not reproduce: {key}")
        outputs[key], identities[key] = result, identity
        reducer_by_id[key] = claim["reducer"]
        reports.append({"id": key, "reducer": claim["reducer"], "result_sha256": canonical_sha256(result),
                        "measurement": labels, "supersedes": predecessor,
                        "amendment_reason": claim["amendment_reason"],
                        **({"verifier_cost": verifier_cost} if verifier_cost is not None else {}),
                        "offline_replay_cost": {"wall_seconds": replay_wall_seconds,
                                                "control_plane_reads": 0, "model_calls": 0,
                                                "monetary_cost_usd": None,
                                                "monetary_cost_status": "unavailable"}})
    if used != set(bindings):
        raise ClaimError("orphan source records are not consumed by any asserted result")
    return {"schema": REPORT_SCHEMA, "verified": True, "claims": reports,
            "effective_claims": [key for key in outputs if key not in superseded],
            "evaluator_sha256": canonical_sha256(manifest["evaluator_bindings"]),
            "source_sha256": {ref: binding.source_sha256 for ref, binding in bindings.items()},
            "limitations": ["Local consistency only; no source authentication or live validation."]}
