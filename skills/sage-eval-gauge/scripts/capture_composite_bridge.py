#!/usr/bin/env python3
"""Map one capture compiler result and outer facts into the existing composite consumer."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "Payload_Type/sage"
if str(SOURCE) not in sys.path: sys.path.insert(0, str(SOURCE))


class CaptureCompositeBridgeError(ValueError): pass


def _load(path: Path, name: str):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module);return module


def _write(path:Path,value:Mapping[str,Any])->str:
    raw=json.dumps(dict(value),sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:os.write(fd,raw);os.fsync(fd)
    finally:os.close(fd)
    return hashlib.sha256(raw).hexdigest()


def _derive_outcome(episode:Mapping[str,Any],compiler:Mapping[str,Any],facts:Mapping[str,Any])->tuple[dict[str,bool],list[Mapping[str,Any]]]:
    score=episode.get("scorecard") if isinstance(episode.get("scorecard"),Mapping) else {}
    oracle=episode.get("oracle") if isinstance(episode.get("oracle"),Mapping) else {}
    terminal=facts.get("terminal_snapshot") if isinstance(facts.get("terminal_snapshot"),Mapping) else {}
    objective=oracle.get("success") if type(oracle.get("success")) is bool else None
    if objective is None or score.get("objective_proven") is not objective:
        raise CaptureCompositeBridgeError("independent objective evidence is incomplete")
    proofs=facts.get("proof_envelopes")
    if not isinstance(proofs,list):raise CaptureCompositeBridgeError("proof provenance is incomplete")
    clean=(facts.get("cleanup_complete") is True and terminal.get("terminal") is True
           and terminal.get("inventory_complete") is True and terminal.get("native_tasks_quiescent") is True
           and (episode.get("native_solver_result") or {}).get("producer_settlement",{}).get("native_producer_terminal") is True)
    if score.get("clean_stop") is not clean:raise CaptureCompositeBridgeError("independent clean-stop evidence differs")
    backend_complete=facts.get("backend_inventory_complete") is True
    policy=score.get("policy_identity_valid") is True
    retention=compiler.get("retention")
    artifacts=retention.get("artifacts") if isinstance(retention,Mapping) else None
    artifact_ok=compiler.get("admissible") is True and isinstance(artifacts,Mapping) and bool(artifacts)
    # A negative objective can coexist with admitted proofs of partial progress.
    # The live producer validates every retained proof independently of the oracle.
    proof_complete=(not objective and facts.get("negative_oracle_complete") is True) or (objective and bool(proofs))
    return {"outcome_source":"independently_observed","objective_proven":objective,"clean_stop":clean,
            "proof_provenance_complete":proof_complete,"artifact_integrity_passed":artifact_ok,
            "policy_identity_passed":policy,"backend_provenance_passed":backend_complete},proofs


def _compose_bound(compiler_result:Mapping[str,Any],outer_facts:Mapping[str,Any],*,output_dir:Path,fixture_only:bool)->dict[str,Any]:
    episode=compiler_result.get("redacted_episode_packet")
    risk=compiler_result.get("risk")
    if not isinstance(episode,Mapping) or not isinstance(risk,Mapping):
        raise CaptureCompositeBridgeError("capture compiler result is incomplete")
    counters=risk.get("counters")
    if not isinstance(counters,Mapping):raise CaptureCompositeBridgeError("capture risk inventory is missing")
    names=("boundary_violation_count","secret_exposure_count","lab_literal_count",
           "unauthorized_live_request_count","active_runtime_config_mutation_count")
    row={};risk_complete=True
    for name in names:
        item=counters.get(name)
        if not isinstance(item,Mapping) or type(item.get("complete")) is not bool:
            raise CaptureCompositeBridgeError("capture risk counter is malformed")
        value=item.get("value")
        if item["complete"] and type(value) is not int:raise CaptureCompositeBridgeError("complete capture risk counter has no value")
        row[name]=value if item["complete"] else None;risk_complete=risk_complete and item["complete"]
    identity=(episode.get("scorecard") or {}).get("measurement_identity")
    context=outer_facts.get("row_identity")
    if not isinstance(identity,Mapping) or not isinstance(context,Mapping) or set(context)!={"row_id","surface_id","family_id"}:
        raise CaptureCompositeBridgeError("capture row identity is incomplete")
    derived,proofs=_derive_outcome(episode,compiler_result,outer_facts)
    transactions=outer_facts.get("semantic_transactions")
    if not isinstance(transactions,list) or any(not isinstance(x,str) or not x for x in transactions) \
            or len(set(transactions))!=len(transactions) or (episode.get("scorecard") or {}).get("semantic_transaction_count")!=len(transactions):
        raise CaptureCompositeBridgeError("semantic transaction evidence differs")
    policy_events=outer_facts.get("policy_event_inventory")
    if not isinstance(policy_events,Mapping) or policy_events.get("complete") is not True \
            or type(policy_events.get("model_owned_branch_observed")) is not bool:
        raise CaptureCompositeBridgeError("model branch evidence is incomplete")
    from ai.hillclimb import evaluator_evidence
    projected_row=evaluator_evidence.project_canonical_evidence(
        row,row_id=context["row_id"],surface_id=context["surface_id"],family_id=context["family_id"],
        policy_arm=identity.get("policy_arm"),derived_outcome=derived,proof_envelopes=proofs,
        semantic_transaction_count=len(transactions),
        model_owned_branch_observed=policy_events["model_owned_branch_observed"],
    )
    row={**projected_row["identity"],**projected_row["trace"],"derived_outcome":projected_row["derived_outcome"],
         "proof_envelopes":projected_row["proof_envelopes"],"semantic_transaction_count":projected_row["semantic_transaction_count"],
         "model_owned_branch_observed":projected_row["model_owned_branch_observed"]}
    tradecraft=_load(Path(__file__).with_name("capture_tradecraft_facts.py"),"capture_composite_tradecraft")
    projected=tradecraft.build_tradecraft_facts(
        outer_facts.get("native_actions"),episode_id=str(episode.get("episode_id") or ""),
        native_chat_rows=outer_facts.get("native_chat_rows"),selected_channel=outer_facts.get("selected_channel"),
        selected_request=outer_facts.get("selected_request"),terminal_snapshot=outer_facts.get("terminal_snapshot"),
    )
    observer={"source_version":str(outer_facts.get("observer_version") or ""),"row":row,
              "identity":dict(identity),"facts":projected["facts"],"mutation_facts":projected["mutation_facts"]}
    output_dir=output_dir.resolve();episode_digest=_write(output_dir/"episode.json",episode);observer_digest=_write(output_dir/"observer.json",observer)
    from ai.hillclimb import composite_source_adapter as adapter
    source_version=str((episode.get("requested_identity") or {}).get("source_version") or "")
    specs={"episode":{"path":"episode.json","source_version":source_version,"source_sha256":episode_digest},
           "observer":{"path":"observer.json","source_version":observer["source_version"],"source_sha256":observer_digest}}
    bundle=adapter.assemble_source_bundle(specs,repo_root=output_dir,schema=adapter.BUNDLE_SCHEMA_V2)
    result=adapter.admit_composite_source(bundle,repo_root=output_dir)
    return {"schema":"sage-capture-composite-bridge-result-v1","fixture_only":fixture_only,"admission":result,
            "episode_sha256":episode_digest,"observer_sha256":observer_digest,
            "risk_values_complete":risk_complete}


def compose_capture(
    compiler_result: Mapping[str,Any], outer_facts: Mapping[str,Any], *, output_dir: Path,
) -> dict[str,Any]:
    """Exercise the production source stack for explicitly labelled local fixtures."""
    if outer_facts.get("source_kind")!="test_fixture":
        raise CaptureCompositeBridgeError("live outer-fact producer binding is unavailable")
    return _compose_bound(compiler_result,outer_facts,output_dir=output_dir,fixture_only=True)


def compose_live_capture(compiler_result:Mapping[str,Any],*,episode:Mapping[str,Any],
    contract:Mapping[str,Any],native_first:Mapping[str,Any],native_final:Mapping[str,Any],
    projected_events:Sequence[Mapping[str,Any]],containment_receipt:Mapping[str,Any],
    engagement_first:Mapping[str,Any],engagement_final:Mapping[str,Any],output_dir:Path)->dict[str,Any]:
    """Invoke the actual live producer before the shared source/admission stack."""
    if compiler_result.get("episode_packet_sha256")!=hashlib.sha256(json.dumps(dict(episode),sort_keys=True,
            separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()).hexdigest():
        raise CaptureCompositeBridgeError("live capture packet binding differs")
    live=_load(Path(__file__).with_name("capture_live_facts.py"),"capture_composite_live_facts")
    try:facts=live.derive_live_outer_facts(episode,contract=contract,native_first=native_first,
        native_final=native_final,projected_events=projected_events,containment_receipt=containment_receipt,
        engagement_first=engagement_first,engagement_final=engagement_final)
    except Exception as exc:
        if type(exc).__name__!="LiveCaptureFactsIncomplete":raise
        raise CaptureCompositeBridgeError(str(exc)) from None
    return _compose_bound(compiler_result,facts,output_dir=output_dir,fixture_only=False)
