#!/usr/bin/env python3
"""Derive source facts for the v2 tradecraft evaluator from native records.

The returned dictionaries deliberately have no evidence locators.  The source
bundle assembler must attach locators after persisting this result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SAGE_SOURCE = ROOT / "Payload_Type" / "sage"
if str(SAGE_SOURCE) not in sys.path:
    sys.path.insert(0, str(SAGE_SOURCE))

from ai.hillclimb import tradecraft_evidence as ev

_SHA = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_METADATA_KEYS = frozenset({"decision_packet", "decision_packet_hash", "replay_context", "_policy_replay_decision"})


class TradecraftFactError(ValueError):
    pass


def _hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _fact(kind: ev.FactKind, truth: ev.Truth, *, subject: str,
          object_: str | None = None, origin: ev.EvidenceOrigin | None = None,
          tx: str | None = None, order: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"fact_kind": kind.value, "truth": truth.value,
                             "origin": (origin or ev.EvidenceOrigin.EVALUATOR_OBSERVER).value,
                             "subject_handle": subject}
    if object_ is not None: value["object_handle"] = object_
    if tx is not None: value["semantic_transaction_id"] = tx
    if order is not None: value["order_index"] = order
    return value


def _strict_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping): raise TradecraftFactError(f"{label} is invalid")
    return value


def _secret_free_record_hashes(records: Any) -> tuple[str, ...]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise TradecraftFactError("source provenance is invalid")
    hashes = []
    for record in records:
        record = _strict_mapping(record, "source record")
        digest = record.get("record_sha256")
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise TradecraftFactError("source record digest is invalid")
        hashes.append(digest.removeprefix("sha256:"))
    return tuple(hashes)


def _contains_metadata(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(set(value) & _METADATA_KEYS) or any(_contains_metadata(item) for item in value.values())
    if isinstance(value, list): return any(_contains_metadata(item) for item in value)
    return False


def _decoded_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping): return value
    if isinstance(value, str):
        try: decoded = json.loads(value)
        except (ValueError, TypeError): return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def build_tradecraft_facts(native_actions: Mapping[str, Any], *, episode_id: str,
                           native_chat_rows: Sequence[Mapping[str, Any]],
                           selected_channel: str, selected_request: str,
                           terminal_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    native_actions = _strict_mapping(native_actions, "native actions")
    terminal_snapshot = _strict_mapping(terminal_snapshot, "terminal snapshot")
    def scalar(item: Any) -> bool:
        return type(item) in (str, int) and item not in ("", 0)
    if not isinstance(episode_id, str) or not episode_id or not scalar(selected_channel) or not scalar(selected_request):
        raise TradecraftFactError("selection is invalid")
    observations = native_actions.get("observations")
    if not isinstance(observations, list) or not isinstance(native_chat_rows, Sequence):
        raise TradecraftFactError("native inventory is invalid")
    source_records = tuple(digest for observation in observations if isinstance(observation, Mapping)
                           for digest in _secret_free_record_hashes(observation.get("source_records")))
    terminal_digest = terminal_snapshot.get("source_sha256")
    if terminal_digest is not None and (not isinstance(terminal_digest, str) or not _SHA.fullmatch(terminal_digest)):
        raise TradecraftFactError("terminal source digest is invalid")

    facts: list[dict[str, Any]] = []
    mutations: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []
    ep = ev.opaque_handle("episode", episode_id)
    structural = native_actions.get("complete") is True and terminal_snapshot.get("inventory_complete") is True \
                 and terminal_snapshot.get("terminal") is True and terminal_digest is not None
    reliable_order = all(isinstance(item, Mapping) and item.get("ordering_reliable") is True
        and type(item.get("order_index")) is int and item["order_index"] >= 0
        for item in observations)
    boundary = ev.Truth.TRUE if structural and (reliable_order or not observations) else ev.Truth.UNKNOWN
    facts.append(_fact(ev.FactKind.TRAJECTORY_BOUNDARY, boundary, subject=ep))
    unknowns.extend(({"record_type": "context_lifecycle_unknown"}, {"record_type": "marker_inventory_unknown"}))

    if not observations and structural:
        subject = ev.opaque_handle("no-action", episode_id)
        tx = ev.transaction_handle("no-action", {"episode": ep})
        sentinel = _fact(ev.FactKind.ACTION_ISSUED, ev.Truth.FALSE, subject=subject, tx=tx, order=0)
        facts.append(sentinel); mutations.append(dict(sentinel))

    for index, observation in enumerate(observations):
        observation = _strict_mapping(observation, "action observation")
        normalized = observation.get("normalization") == "known"
        has_handles = all(isinstance(observation.get(key), str) and observation[key]
                          for key in ("action_handle", "target_handle"))
        attempted = observation.get("attempted")
        result_known = observation.get("terminal_outcome") == "system_identity_observed" \
            and observation.get("terminal_parser") == "ai.hillclimb.live_seams._output_is_system_identity-v1"
        this_order = observation.get("ordering_reliable") is True and type(observation.get("order_index")) is int
        if not this_order:
            unknowns.append({"record_type": "unresolved_action", "truth": ev.Truth.UNKNOWN.value,
                "record_handle": ev.opaque_handle("unresolved-action-record",
                    {"episode": ep, "index": index, "sources": source_records})})
            continue
        if type(attempted) is not bool:
            raise TradecraftFactError("action attempt is invalid")
        order = observation["order_index"] * 2
        tx = ev.transaction_handle("native-action", {"episode": ep, "order": observation["order_index"]})
        if normalized and has_handles:
            subject = ev.opaque_handle("native-action", observation["action_handle"])
            object_ = ev.opaque_handle("native-target", observation["target_handle"])
            truth = ev.Truth.TRUE if attempted else ev.Truth.FALSE
        else:
            subject = ev.opaque_handle("unresolved-action-record",
                {"episode": ep, "index": index, "sources": source_records})
            object_ = None
            truth = ev.Truth.UNKNOWN
            unknowns.append({"record_type": "normalization_unknown", "record_handle": subject})
        all_atom = _fact(ev.FactKind.ACTION_ISSUED, truth,
                         subject=subject, object_=object_, tx=tx, order=order)
        facts.append(all_atom)
        mutation_truth = (ev.Truth.FALSE if observation.get("mutation_relevance") == "transparent_nonmutating"
                          and truth is not ev.Truth.UNKNOWN else ev.Truth.UNKNOWN)
        mutations.append(_fact(ev.FactKind.ACTION_ISSUED, mutation_truth, subject=subject,
                               object_=object_, tx=tx, order=order))
        result = ev.Truth.TRUE if result_known and truth is not ev.Truth.UNKNOWN else ev.Truth.UNKNOWN
        terminal = _fact(ev.FactKind.TERMINAL_RESULT, result, subject=subject, object_=object_, tx=tx, order=order + 1)
        facts.append(terminal)

    chat_valid = True; detected = 0; delivered = 0
    for row_index, row_value in enumerate(native_chat_rows):
        row = _strict_mapping(row_value, "native chat row")
        if row.get("channel_id") != selected_channel or row.get("chat_request_id") != selected_request:
            chat_valid = False
            unknowns.append({"record_type": "unselected_chat_row", "record_sha256": _hash(row)})
            continue
        subject = ev.opaque_handle("native-delivery", {"episode": ep, "row_sha256": _hash(row)})
        parts = (row.get("message"), row.get("tool_output"), row.get("metadata"))
        if any(part not in (None, "", {}, []) for part in parts):
            delivered += 1
            atom = _fact(ev.FactKind.DELIVERED_CONTENT, ev.Truth.TRUE, subject=subject,
                         origin=ev.EvidenceOrigin.NATIVE_DELIVERABLE)
            facts.append(atom)
        parsed = [_decoded_mapping(part) for part in parts]
        if any(_contains_metadata(item) for item in parsed if item is not None):
            detected += 1
            atom = _fact(ev.FactKind.EVALUATION_METADATA_PRESENT, ev.Truth.TRUE, subject=subject,
                         origin=ev.EvidenceOrigin.NATIVE_DELIVERABLE)
            facts.append(atom)
        # Opaque or missing metadata prevents a claim that the key taxonomy was exhaustively scanned.
        if any(item is None and part not in (None, "", {}, []) for item, part in zip(parsed, parts)):
            chat_valid = False

    delivery_complete = terminal_snapshot.get("chat_inventory_complete") is True and terminal_snapshot.get("terminal") is True and chat_valid
    taxonomy_complete = delivery_complete and terminal_snapshot.get("metadata_taxonomy_complete") is True
    delivery_subject = ev.opaque_handle("delivery-boundary", {"episode": ep, "channel": selected_channel,
                                                                 "request": selected_request})
    for kind, truth in ((ev.FactKind.DELIVERY_BOUNDARY, ev.Truth.TRUE if delivery_complete else ev.Truth.UNKNOWN),
                        (ev.FactKind.EVALUATION_METADATA_TAXONOMY,
                         ev.Truth.TRUE if taxonomy_complete else ev.Truth.UNKNOWN)):
        atom = _fact(kind, truth, subject=delivery_subject)
        facts.append(atom)
    if detected and not taxonomy_complete:
        unknowns.append({"record_type": "metadata_taxonomy_incomplete", "detected_records": detected})
    if delivered == 0: unknowns.append({"record_type": "delivered_content_unknown"})

    result = {"schema": "sage.capture-tradecraft-facts-v1", "episode_handle": ep,
              "facts": facts, "mutation_facts": mutations, "unknowns": unknowns,
              "source_record_sha256": list(source_records),
              "terminal_source_sha256": terminal_digest}
    result["projection_input_sha256"] = _hash({key: result[key] for key in result if key != "projection_input_sha256"})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build source facts from caller-verified native records")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("DRY RUN")
        return 0
    parser.error("this library requires an evaluator caller")


if __name__ == "__main__": raise SystemExit(main())
