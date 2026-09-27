#!/usr/bin/env python3
"""Read a finite, validated metadata projection of the shared resource ledger."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "Payload_Type" / "sage"
if str(SOURCE) not in sys.path: sys.path.insert(0, str(SOURCE))
from ai import resource_budget


class LedgerSourceError(RuntimeError):
    pass


def _events(raw: bytes) -> list[dict[str, Any]]:
    try:
        values = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (UnicodeError, json.JSONDecodeError):
        raise LedgerSourceError("ledger source is invalid") from None
    if any(not isinstance(item, dict) for item in values): raise LedgerSourceError("ledger source is invalid")
    return values


def read_ledger_source(config_path: str | Path, ledger_path: str | Path, *, episode_id: str | None = None,
                       service_probe_id: str | None = None, starting_seq: int | None = None,
                       measurement_start_ns: int | None = None, measurement_end_ns: int | None = None) -> dict[str, Any]:
    scope_ids=[item for item in (episode_id,service_probe_id) if item is not None]
    if len(scope_ids)!=1 or not isinstance(scope_ids[0],str) or not scope_ids[0] or (starting_seq is not None and
            (type(starting_seq) is not int or starting_seq < 0)
            or measurement_start_ns is not None and (type(measurement_start_ns) is not int or measurement_start_ns<=0)
            or measurement_end_ns is not None and (type(measurement_end_ns) is not int or measurement_end_ns<=0)):
        raise LedgerSourceError("ledger source selection is invalid")
    ledger = resource_budget.ResourceLedger(config_path, ledger_path,service_probe_id=service_probe_id)
    path = Path(ledger_path)
    for _attempt in range(2):
        try: before = path.read_bytes()
        except OSError: raise LedgerSourceError("ledger source is unavailable") from None
        try: status = ledger.status()
        except resource_budget.ResourceBudgetError as exc:
            raise LedgerSourceError(type(exc).__name__) from None
        try: after = path.read_bytes()
        except OSError: raise LedgerSourceError("ledger source is unavailable") from None
        if before == after:
            events = _events(after)
            if status["next_sequence"] != len(events): raise LedgerSourceError("ledger source identity is ambiguous")
            break
    else:
        raise LedgerSourceError("ledger changed during validation")
    episode = status["episodes"].get(episode_id) if episode_id is not None else None
    probe=status.get("service_probes",{}).get(service_probe_id) if service_probe_id is not None else None
    if episode_id is not None and episode is None: raise LedgerSourceError("ledger episode is unavailable")
    if service_probe_id is not None and (not isinstance(probe,dict) or not probe.get("stages")):
        raise LedgerSourceError("ledger service probe is unavailable")
    settlements = {event["reservation_id"]: event for event in events
                   if event.get("type") == "provider_settlement"}

    def project(event: dict[str, Any]) -> dict[str, Any]:
        value = {name: event[name] for name in ("seq", "reservation_id", "operation", "amounts",
                                                 "cleanup", "reserved_epoch")}
        value["episode_id"] = event.get("episode_id");value["service_probe_id"]=event.get("service_probe_id")
        settlement = settlements.get(event["reservation_id"])
        value["settlement_present"] = settlement is not None
        value["actual_tokens"] = None if settlement is None else settlement["actual_tokens"]
        return value

    selected = [project(event) for event in events if event.get("type") == "reservation" and
                (event.get("episode_id")==episode_id if episode_id is not None else event.get("service_probe_id")==service_probe_id)]
    if service_probe_id is not None:
        latest=probe["stages"][-1];activated=latest["started_epoch"]
        starts=[event for event in events if event.get("type")=="diagnostic_stage_start"
                and event.get("probe_id")==service_probe_id]
        start=next((event for event in reversed(starts)
            if event.get("stage")==latest.get("stage") and event.get("started_epoch")==activated),None)
        if start is None:raise LedgerSourceError("ledger service stage identity is ambiguous")
        next_start=next((event["seq"] for event in starts if event["seq"]>start["seq"]),len(events))
        selected=[row for row in selected if start["seq"]<=row["seq"]<next_start]
        allocation=probe.get("allocation",{});action_seconds=allocation.get("stage_active_seconds")
        cleanup_seconds=allocation.get("stage_cleanup_seconds")
        if cleanup_seconds is None and isinstance(allocation.get("stage_total_seconds"),(int,float)) \
                and isinstance(action_seconds,(int,float)):
            cleanup_seconds=allocation["stage_total_seconds"]-action_seconds
        if not isinstance(action_seconds,(int,float)) or not isinstance(cleanup_seconds,(int,float)) \
                or action_seconds<=0 or cleanup_seconds<0:
            action_seconds=latest["active_deadline_epoch"]-activated
            cleanup_seconds=latest["total_deadline_epoch"]-latest["active_deadline_epoch"]
        actual_active_end=latest.get("active_ended_epoch") or latest["active_deadline_epoch"]
        registered={"action_seconds":action_seconds,"settlement_seconds":cleanup_seconds,
                    "provider_token_limit":0,"per_call_token_reservation":0,"payload_task_limit":0}
        episode={"activated_epoch":activated,"active_deadline_epoch":actual_active_end,
                 "total_deadline_epoch":latest["total_deadline_epoch"],"closed_epoch":latest["closed_epoch"],
                 "outstanding_reservations":[],"unknown_provider_usage":False,"provider_overrun":False}
    else:
        registered={name:episode[name] for name in ("action_seconds","settlement_seconds","provider_token_limit","per_call_token_reservation","payload_task_limit")}
        episode["active_deadline_epoch"]=episode["activated_epoch"]+episode["action_seconds"] if episode["activated_epoch"] is not None else None
        episode["total_deadline_epoch"]=episode["active_deadline_epoch"]+episode["settlement_seconds"] if episode["active_deadline_epoch"] is not None else None
    result = {"schema": "sage-resource-ledger-source-v1", "episode_id": episode_id,
              "service_probe_id":service_probe_id,
              "source_sha256": hashlib.sha256(after).hexdigest(), "frozen_config_sha256": status["frozen_config_sha256"],
              "started_epoch": status["started_epoch"], "active_deadline_epoch": status["active_deadline_epoch"],
              "total_deadline_epoch": status["total_deadline_epoch"],
              "episode_registered": registered,
              "episode_activated_epoch": episode["activated_epoch"], "episode_action_deadline_epoch":episode["active_deadline_epoch"],
              "episode_settlement_deadline_epoch": episode["total_deadline_epoch"],
              "episode_closed_epoch": episode["closed_epoch"], "reservations": selected,
              "outstanding_reservations": episode["outstanding_reservations"],
              "unknown_provider_usage": episode["unknown_provider_usage"],
              "provider_overrun": episode["provider_overrun"],
              "global_outstanding_reservations": status["outstanding_reservations"],
              "global_unknown_provider_usage": status["unknown_provider_usage"],
              "global_provider_overrun": status["provider_overrun"]}
    if measurement_end_ns is not None:
        if service_probe_id is not None:
            if starting_seq is None or measurement_start_ns is None:
                raise LedgerSourceError("service measurement source binding missing")
            lower_seq=starting_seq;lower_epoch=measurement_start_ns/1_000_000_000
        else:
            lower_seq=None;lower_epoch=episode["activated_epoch"]
        result["measured_reservations"]=[row for row in selected
            if isinstance(lower_epoch,(int,float)) and not isinstance(lower_epoch,bool)
            and (lower_seq is None or row["seq"]>=lower_seq)
            and lower_epoch<=row["reserved_epoch"]<=measurement_end_ns/1_000_000_000]
        result["measurement_reservation_starting_seq"]=lower_seq
        result["measurement_reservation_start_epoch"]=lower_epoch
        result["measurement_reservation_end_ns"]=measurement_end_ns
    if service_probe_id is not None:
        result["episode_reserved_action_deadline_epoch"]=latest["active_deadline_epoch"]
        result["episode_actual_active_ended_epoch"]=latest.get("active_ended_epoch")
    if starting_seq is not None:
        if starting_seq > status["next_sequence"]: raise LedgerSourceError("global sequence selection is invalid")
        result["global_reservations"] = [project(event) for event in events
            if event.get("type") == "reservation" and event["seq"] >= starting_seq]
        result["global_starting_seq"] = starting_seq
    return result


def main() -> int:
    print("DRY RUN: import read_ledger_source with explicit paths and episode.")
    return 0

if __name__ == "__main__": raise SystemExit(main())
