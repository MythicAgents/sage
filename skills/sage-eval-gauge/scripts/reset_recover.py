#!/usr/bin/env python3
"""Attest an interrupted, fully staged pair reset without repeating its mutations.

Run with a source snapshot whose identity is the retained matrix contract identity.
This command performs control-plane observations and appends an audited checkpoint;
it never resets the range, starts a producer, or calls a model.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping


PREFIX = (
    "stop-sage", "archive-dbs", "reset-mythic", "ludus-rollback",
    "ludus-poweron", "range-boot", "clock-reachability", "sync-range-time",
    "wipe-bloodhound", "bloodhound-empty", "restart-sage",
    "bootstrap-foothold", "foothold-power", "foothold-launch",
)
READINESS_SECTIONS = (
    "authentication", "bloodhound_api", "bloodhound_mcp", "callbacks",
    "channel", "clock", "foothold", "ludus", "mythic_chat",
    "runtime_databases", "runtime_identity", "sage_deployment",
)


class RecoveryError(Exception):
    pass


def _readiness_failure_labels(readiness: object, clean_bh: object) -> str:
    if not isinstance(readiness, Mapping):
        return "readiness_response"
    labels = [name for name in READINESS_SECTIONS
              if isinstance(readiness.get(name), Mapping)
              and readiness[name].get("ready") is False]
    if not isinstance(clean_bh, Mapping) or clean_bh.get("returncode") != 0 or clean_bh.get("domain_count") != 0:
        labels.append("bloodhound_empty")
    return ", ".join(labels or ["unclassified_readiness"])


def _load_snapshot(source_root: Path):
    source_root = source_root.expanduser().resolve()
    payload = source_root / "Payload_Type/sage"
    script = source_root / "skills/sage-eval-gauge/scripts/run_trust_walker_pilot.py"
    if not script.is_file():
        raise RecoveryError("selected measurement source lacks the pair runner")
    sys.path.insert(0, str(payload))
    spec = importlib.util.spec_from_file_location("frozen_trust_walker_pair", script)
    if spec is None or spec.loader is None:
        raise RecoveryError("selected measurement runner cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if module.ROOT.resolve() != source_root:
        raise RecoveryError("measurement runner loaded from a different source root")
    if Path(module.PAYLOAD).resolve() != payload:
        raise RecoveryError("measurement payload loaded from a different source root")
    if Path(module.live_seams.__file__).resolve() != payload / "ai/hillclimb/live_seams.py":
        raise RecoveryError("measurement dependencies loaded from a different source root")
    from ai.hillclimb import trust_walker_pilot as core
    if Path(core.__file__).resolve() != payload / "ai/hillclimb/trust_walker_pilot.py":
        raise RecoveryError("measurement coordinator loaded from a different source root")
    module.pilot_core = core
    return module


def _single(records: list[dict[str, Any]], kind: str, arm: str | None = None):
    rows = [row for row in records if row.get("record_type") == kind and (arm is None or row.get("arm") == arm)]
    if len(rows) != 1:
        raise RecoveryError(f"expected one retained {kind} record")
    return rows[0]


def _validate_ledger(pair, root: Path, source_root: Path, case_descriptor: Path | None):
    records = pair.read_records(root / "pilot.jsonl")
    contract = pair._replay_contract(records)
    if not isinstance(contract, pair.MatrixPairContract):
        raise RecoveryError("reset recovery requires a retained matrix pair")
    if contract.source_identity != pair._source_identity_at(source_root):
        raise RecoveryError("measurement source differs from the retained pair contract")
    if contract.alternate_start is not None:
        if case_descriptor is None:
            raise RecoveryError("alternate start requires the original case descriptor")
        binding = pair._load_alternate_start(case_descriptor)
        if binding.to_record() != contract.alternate_start.to_record():
            raise RecoveryError("case descriptor differs from the retained alternate start")
        contract = replace(contract, alternate_start=binding)
    elif case_descriptor is not None:
        raise RecoveryError("default start cannot use an alternate case descriptor")
    if len([row for row in records if row.get("record_type") == "resumable_origin"]) != 1:
        raise RecoveryError("pair lacks one resumable origin")
    if len(records) < 2 or records[0].get("record_type") != "contract" or records[1].get("record_type") != "resumable_origin" or records[1].get("schema") != "sage-pair-checkpoint-v1":
        raise RecoveryError("resumable origin is malformed or misplaced")
    if any(row.get("record_type") in {"arm_error", "preserved_state", "support_budget_exhausted"}
           for row in records):
        raise RecoveryError("retained pair has a terminal or exhausted recovery state")
    if any(row.get("record_type") == "comparison" for row in records):
        raise RecoveryError("pair already has a final comparison")
    if any(row.get("record_type") in {"reset_recovery_intent", "reset_recovery_audit", "reset_completed"}
           and row.get("arm") == contract.arm_order[1] for row in records):
        raise RecoveryError("interrupted reset has already been recovered or completed")
    if any(row.get("record_type") in {"arm_started", "producer_intent", "execution_returned", "arm_result"}
           and row.get("arm") == contract.arm_order[1] for row in records):
        raise RecoveryError("producer or measured arm has already started")
    first = contract.arm_order[0]
    if _single(records, "cleanup", first).get("complete") is not True or _single(records, "arm_result", first) is None:
        raise RecoveryError("prior measured arm is not complete and cleaned")
    intent = _single(records, "reset_intent", contract.arm_order[1])
    _single(records, "attempted_start", contract.arm_order[1])
    stages = pair.read_records(root / "reset-stages.jsonl")
    current = [row for row in stages if row.get("arm") == contract.arm_order[1]]
    expected = [(stage, status) for stage in PREFIX for status in ("started", "passed")]
    expected += [("readiness", "started"), ("readiness", "failed")]
    if [(row.get("stage"), row.get("status")) for row in current] != expected:
        raise RecoveryError("reset stage sequence is not the known post-foothold readiness failure")
    if current[-1].get("error_class") != "SystemExit":
        raise RecoveryError("retained readiness failure differs from the recoverable failure")
    if any(row.get("record_type") in {"reset_recovery_intent", "reset_recovery_audit"} for row in records):
        raise RecoveryError("bounded reset recovery was already attempted")
    return contract, records, intent, current[-1]


def _attest(pair, runtime, contract, prior_start: Mapping[str, Any],
            reset_intent: Mapping[str, Any], failed_stage: Mapping[str, Any]):
    pair._validate_protected_at(runtime.protected_manifest, runtime.source_root)
    fixture = pair._fixture_at(runtime.source_root, contract.alternate_start)
    runtime._staged_interactive_foothold()
    pair._validate_external_paths()
    pair._validate_route_smoke(runtime.route_smoke, runtime._configured_route(), contract.route)
    client = asyncio.run(runtime.native.login())
    readiness = runtime._post_reset_readiness(client)
    clean_bh = pair.live_seams.bloodhound_empty_state_evidence()
    if readiness.get("ready") is not True or clean_bh.get("returncode") != 0 or clean_bh.get("domain_count") != 0:
        raise RecoveryError("current range or BloodHound readiness is not clean: " +
                            _readiness_failure_labels(readiness, clean_bh))
    operation_id = getattr(client, "current_operation_id", None)
    if type(operation_id) is not int or operation_id <= 0:
        raise RecoveryError("Mythic operation identity is unavailable")
    snapshot = runtime._operation_snapshot(client, operation_id)
    if snapshot.get("task") != [] or snapshot.get("credential") != []:
        raise RecoveryError("current Mythic operation contains tasks or credentials")
    from mythic import mythic
    async def requests():
        return await asyncio.wait_for(mythic.execute_custom_query(
            client,
            "query ResetRecoverRequests($operationId: Int!) { chat_request(where: {operation_id: {_eq: $operationId}}) { id status } }",
            variables={"operationId": operation_id},
        ), timeout=60)
    request_census = asyncio.run(requests())
    if not isinstance(request_census, Mapping) or request_census.get("chat_request") != []:
        raise RecoveryError("current Mythic operation contains chat requests or request census is unavailable")
    callbacks = snapshot.get("callback")
    if not isinstance(callbacks, list):
        raise RecoveryError("current Mythic callback census is malformed")
    active = [row for row in callbacks if isinstance(row, Mapping) and row.get("active") is True]
    if len(active) != 1:
        raise RecoveryError("current operation does not have exactly one active callback")
    callback_id = active[0].get("display_id")
    if type(callback_id) is not int or callback_id <= 0:
        raise RecoveryError("current callback identity is malformed")
    callback = pair._select_unique_active_callback(snapshot.get("callback"), callback_id)
    foothold_state = readiness.get("foothold") or {}
    selected = [row for row in foothold_state.get("callbacks") or [] if row.get("display_id") == callback_id]
    if len(selected) != 1:
        raise RecoveryError("readiness callback differs from the current operation callback")
    ludus = readiness.get("ludus") or {}
    profile = ludus.get("range_profile") or {}
    if (profile.get("id") != pair.RANGE_PROFILE or
            (profile.get("qualification") or {}).get("state") != "live_qualified" or
            pair.SCENARIO not in (profile.get("scenarios") or [])):
        raise RecoveryError("current range profile is not qualified for this case")
    payload = callback.get("payload") if isinstance(callback.get("payload"), Mapping) else {}
    payload_type = payload.get("payloadtype") if isinstance(payload.get("payloadtype"), Mapping) else {}
    domain = str(callback.get("domain") or "").strip()
    user = str(callback.get("user") or "").strip()
    principal = user if "\\" in user else f"{domain}\\{user}"
    binding = contract.alternate_start
    wanted_principal = binding.principal if binding is not None else r"NORTH\samwell.tarly"
    wanted_host = binding.host if binding is not None else "CASTELBLACK"
    if principal.casefold() != wanted_principal.casefold() or str(callback.get("host") or "").casefold() != wanted_host.casefold():
        raise RecoveryError("current callback belongs to the wrong principal or host")
    runtime.route = runtime._configured_route()
    runtime.safe_route = pair._safe_route_identity(runtime.route)
    dependency_identity = pair._json_hash({
        "topology_identity": profile.get("topology_identity"),
        "external_paths": pair._validate_external_paths(),
        "staged_foothold_fixture": fixture,
    })
    start = {
        "snapshot": binding.snapshot if binding is not None else pair.SNAPSHOT,
        "range_profile": profile.get("id"),
        "inventory": sorted(str(row.get("canonical_name") or "") for row in ludus.get("vms") or []),
        "clock_ready": (readiness.get("clock") or {}).get("ready") is True,
        "mythic_clean": True,
        "bloodhound_clean": True,
        "foothold": {
            "count": 1, "principal": principal,
            "host": str(callback.get("host") or "").upper(),
            "payload_type": str(payload_type.get("name") or selected[0].get("payloadtype") or ""),
        },
        "route": runtime.safe_route,
        "objective_sha256": hashlib.sha256(pair.OBJECTIVE.encode()).hexdigest(),
        "source_identity": pair._source_identity_at(runtime.source_root),
        "tool_contract": pair._tool_contract(),
        "dependency_identity": dependency_identity,
    }
    if binding is not None:
        start["case_identity"] = binding.identity
    identity = pair.matched_start_identity(start, binding, matrix_pair=contract)
    matched, differences = pair.compare_start_manifests(prior_start, identity, alternate_start=binding, matrix_pair=contract)
    if not matched:
        raise RecoveryError("current start differs from completed arm: " + ", ".join(map(str, differences)))
    runtime.current_client = client
    runtime.current_operation_id = operation_id
    runtime.current_callback_id = callback_id
    runtime.current_arm = contract.arm_order[1]
    runtime.recovery_enabled = True
    runtime_identity = pair.validate_runtime_identity(runtime.runtime_identity())
    raw_callback = runtime_identity.get("resume_callback") or {}
    if (raw_callback.get("operation_id") != operation_id or
            raw_callback.get("display_id") != callback_id):
        raise RecoveryError("callback UUID lineage is not bound to the current operation")
    try:
        callback_created = datetime.fromisoformat(str(raw_callback["init_callback"]).replace("Z", "+00:00"))
        reset_started = datetime.fromisoformat(str(reset_intent["recorded_at"]))
        reset_failed = datetime.fromisoformat(str(failed_stage["recorded_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError("callback creation time is unavailable for reset lineage") from exc
    if callback_created.tzinfo is None:
        callback_created = callback_created.replace(tzinfo=UTC)
    if not reset_started <= callback_created <= reset_failed:
        raise RecoveryError("current callback was not created during the interrupted reset")
    return start, runtime_identity, {"operation_id": operation_id, "callback_id": callback_id,
        "callback_uuid_verified": True, "task_count": 0, "credential_count": 0,
        "chat_request_count": 0, "bloodhound_domain_count": 0, "readiness_ready": True}


def recover(source_root: Path, artifact_dir: Path, protected_manifest: Path, route_smoke: Path,
            live_authority_file: Path, authorization_source: str, *, case_descriptor: Path | None = None,
            execute: bool = False) -> dict[str, Any]:
    pair = _load_snapshot(source_root)
    root = pair.validate_artifact_dir(artifact_dir)
    if root != artifact_dir.expanduser().resolve():
        raise RecoveryError("artifact directory resolved unexpectedly")
    lock_path = root.parent.parent / ".driver.lock"
    # The cohort driver holds this lock throughout execution. Recovery must run alone.
    if not lock_path.is_file():
        raise RecoveryError("cohort driver lock file is missing")
    with ExitStack() as stack:
        lock = stack.enter_context(lock_path.open("r+b"))
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError("cohort driver is still active") from exc
        pair_lock = pair.pilot_core._pair_lock_path(root)
        descriptor = os.open(pair_lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        stack.callback(os.close, descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError("pair runner is still active") from exc
        contract, records, reset_intent, failed_stage = _validate_ledger(pair, root, source_root, case_descriptor)
        if not isinstance(authorization_source, str) or not authorization_source.strip():
            raise RecoveryError("explicit reset recovery authorization source is required")
        authority = pair._resolve_live_authority(
            argparse.Namespace(command="run-pair", live_authority_id=None, resume=True,
                               live_authority_file=str(live_authority_file)), contract,
        )
        retained_authority = _single(records, "preflight").get("live_authority") or {}
        if (authority.get("authority_id") != retained_authority.get("authority_id") or
                authority.get("authority_sha256") != retained_authority.get("authority_sha256")):
            raise RecoveryError("current run-pair authority differs from the retained pair authority")
        retained_preflight = _single(records, "preflight")
        if (pair._sha256(protected_manifest) != (retained_preflight.get("protected_source") or {}).get("manifest_sha256") or
                pair._sha256(route_smoke) != (retained_preflight.get("route_smoke") or {}).get("sha256")):
            raise RecoveryError("protected source or route smoke differs from the retained pair preflight")
        prior_start = _single(records, "arm_started", contract.arm_order[0]).get("start_identity")
        if not isinstance(prior_start, Mapping):
            raise RecoveryError("completed arm start identity is missing")
        args = argparse.Namespace(artifact_dir=str(root), protected_manifest=str(protected_manifest),
                                  route_smoke=str(route_smoke), measurement_metadata=None)
        runtime = pair._runtime(args, contract)
        runtime.recovery_enabled = True
        runtime.resuming_pair = True
        started = time.monotonic()
        try:
            start, runtime_identity, evidence = _attest(
                pair, runtime, contract, prior_start, reset_intent, failed_stage)
            failure_start = datetime.fromisoformat(reset_intent["recorded_at"])
            failure_end = datetime.fromisoformat(failed_stage["recorded_at"])
            failed_elapsed = max(0.0, (failure_end - failure_start).total_seconds())
            prior_support = max(float(row.get("setup_cleanup_elapsed_seconds", 0)) for row in records)
            support_elapsed = prior_support + failed_elapsed + max(0.0, time.monotonic() - started)
            if support_elapsed > contract.setup_cleanup_seconds:
                raise RecoveryError("setup and cleanup support allowance was exhausted")
            audit = {"record_type": "reset_recovery_audit", "arm": contract.arm_order[1],
                     "schema": "sage-reset-recovery-v1", "complete": True,
                     "failed_reset_elapsed_seconds": failed_elapsed,
                     "recovery_elapsed_seconds": max(0.0, time.monotonic() - started),
                     "setup_cleanup_elapsed_seconds": support_elapsed,
                     "evidence": evidence,
                     "measurement_source_identity": contract.source_identity}
            if not execute:
                return {"complete": True, "mode": "check", "artifact_dir": str(root),
                        "evidence": evidence, "setup_cleanup_elapsed_seconds": support_elapsed,
                        "next": "repeat with --execute to append the verified reset checkpoint"}
            # One retained commit attempt is deliberate. No checkpoint is written on
            # a failed read-only attestation, so repairable readiness remains retryable.
            pair.append_record(root / "pilot.jsonl", {"record_type": "reset_recovery_intent",
                                "arm": contract.arm_order[1], "schema": "sage-reset-recovery-v1",
                                "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                "authorization_source": authorization_source.strip(),
                                "authority_id": authority.get("authority_id"),
                                "authority_sha256": authority.get("authority_sha256"),
                                "measurement_source_identity": contract.source_identity})
            pair.append_record(root / "pilot.jsonl", audit)
            pair.append_record(root / "pilot.jsonl", {"record_type": "reset_completed",
                               "arm": contract.arm_order[1], "arm_index": 1,
                               "start": start, "runtime_identity": runtime_identity,
                               "setup_cleanup_elapsed_seconds": support_elapsed,
                               "recovered_from_audit": "sage-reset-recovery-v1"})
            return {"complete": True, "artifact_dir": str(root), "audit": audit,
                    "next": "resume the original pair with the same measurement source and contract"}
        except BaseException:
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--protected-manifest", required=True, type=Path)
    parser.add_argument("--route-smoke", required=True, type=Path)
    parser.add_argument("--live-authority-file", required=True, type=Path)
    parser.add_argument("--case-descriptor", type=Path)
    parser.add_argument("--authorization-source", required=True)
    parser.add_argument("--execute", action="store_true", help="Append one verified recovery checkpoint; default only checks.")
    args = parser.parse_args(argv)
    try:
        result = recover(args.source_root, args.artifact_dir, args.protected_manifest, args.route_smoke,
                         args.live_authority_file, args.authorization_source,
                         case_descriptor=args.case_descriptor, execute=args.execute)
    except (RecoveryError, OSError, ValueError, RuntimeError) as exc:
        result = {"complete": False, "error_type": type(exc).__name__, "reason": str(exc)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
