"""Content-addressed trajectory-v2 dataset manifests.

The split unit is a topology family, not a row.  All engagements from one topology
family remain in one split, and every record keeps its content hash and evidence role
so diagnostic legacy exports cannot leak into training data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Iterable, Mapping

from .schema import (
    EVIDENCE_ROLE_DIAGNOSTIC_ONLY,
    TransitionRecord,
)


SPLITS = ("train", "dev", "sealed")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class DatasetEntry:
    record_hash: str
    run_id: str
    engagement_id: str
    topology_family: str
    transition_outcome: str
    evidence_role: str
    positive_repair_evidence: bool
    diagnostic_only: bool


@dataclass(frozen=True)
class DatasetManifest:
    schema_version: int
    dataset_hash: str
    split_hashes: dict[str, str]
    splits: dict[str, tuple[DatasetEntry, ...]]
    diagnostic_entries: tuple[DatasetEntry, ...] = field(default_factory=tuple)
    excluded_reason_counts: dict[str, int] = field(default_factory=dict)
    validation_failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return not self.validation_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_hash": self.dataset_hash,
            "split_hashes": dict(self.split_hashes),
            "splits": {
                split: [entry.__dict__ for entry in entries]
                for split, entries in self.splits.items()
            },
            "diagnostic_entries": [entry.__dict__ for entry in self.diagnostic_entries],
            "excluded_reason_counts": dict(self.excluded_reason_counts),
            "validation_failures": list(self.validation_failures),
            "valid": self.valid,
        }


def _entry(record: TransitionRecord) -> DatasetEntry:
    return DatasetEntry(
        record_hash=record.content_hash,
        run_id=_text(record.run_id),
        engagement_id=_text(record.engagement_id or record.run_id),
        topology_family=_text(record.topology_family),
        transition_outcome=_text(record.transition_outcome),
        evidence_role=_text(record.evidence_role),
        positive_repair_evidence=record.positive_repair_evidence,
        diagnostic_only=record.is_diagnostic_only,
    )


def _default_split_for_family(topology_family: str) -> str:
    """Deterministic family-level fallback used only when no commitment is supplied."""

    digest = int(hashlib.sha256(topology_family.encode("utf-8")).hexdigest()[:8], 16) % 100
    if digest < 70:
        return "train"
    if digest < 85:
        return "dev"
    return "sealed"


def validate_manifest(manifest: DatasetManifest) -> tuple[str, ...]:
    failures: list[str] = []
    engagement_splits: dict[str, set[str]] = {}
    topology_splits: dict[str, set[str]] = {}
    seen_hashes: set[str] = set()
    for split, entries in manifest.splits.items():
        if split not in SPLITS:
            failures.append(f"unknown_split:{split}")
        for entry in entries:
            if entry.record_hash in seen_hashes:
                failures.append(f"duplicate_record_hash:{entry.record_hash}")
            seen_hashes.add(entry.record_hash)
            if not entry.engagement_id:
                failures.append(f"{entry.record_hash}:missing_engagement_id")
            if not entry.topology_family:
                failures.append(f"{entry.record_hash}:missing_topology_family")
            engagement_splits.setdefault(entry.engagement_id, set()).add(split)
            topology_splits.setdefault(entry.topology_family, set()).add(split)
    for engagement_id, splits in engagement_splits.items():
        if len(splits) > 1:
            failures.append(f"engagement_split_leakage:{engagement_id}")
    for topology_family, splits in topology_splits.items():
        if len(splits) > 1:
            failures.append(f"topology_split_leakage:{topology_family}")
    return tuple(failures)


def build_dataset_manifest(
    records: Iterable[TransitionRecord],
    *,
    topology_commitments: Mapping[str, str] | None = None,
) -> DatasetManifest:
    """Build a content-addressed train/dev/sealed manifest without row-level randomization."""

    commitments = {
        _text(family): _text(split).casefold()
        for family, split in dict(topology_commitments or {}).items()
        if _text(family)
    }
    invalid_commitments = {
        family: split for family, split in commitments.items() if split not in SPLITS
    }
    if invalid_commitments:
        raise ValueError(f"invalid topology split commitments: {invalid_commitments}")

    splits: dict[str, list[DatasetEntry]] = {split: [] for split in SPLITS}
    diagnostics: list[DatasetEntry] = []
    excluded_reason_counts: dict[str, int] = {}
    for record in records:
        entry = _entry(record)
        if entry.diagnostic_only or entry.evidence_role == EVIDENCE_ROLE_DIAGNOSTIC_ONLY:
            diagnostics.append(entry)
            excluded_reason_counts["diagnostic_only"] = excluded_reason_counts.get("diagnostic_only", 0) + 1
            continue
        if not entry.engagement_id:
            diagnostics.append(entry)
            excluded_reason_counts["missing_engagement_id"] = excluded_reason_counts.get("missing_engagement_id", 0) + 1
            continue
        if not entry.topology_family:
            diagnostics.append(entry)
            excluded_reason_counts["missing_topology_family"] = excluded_reason_counts.get("missing_topology_family", 0) + 1
            continue
        split = commitments.get(entry.topology_family) or _default_split_for_family(entry.topology_family)
        splits[split].append(entry)

    frozen_splits = {
        split: tuple(sorted(entries, key=lambda item: item.record_hash))
        for split, entries in splits.items()
    }
    split_hashes = {
        split: _hash([entry.__dict__ for entry in entries])
        for split, entries in frozen_splits.items()
    }
    provisional = DatasetManifest(
        schema_version=2,
        dataset_hash="",
        split_hashes=split_hashes,
        splits=frozen_splits,
        diagnostic_entries=tuple(sorted(diagnostics, key=lambda item: item.record_hash)),
        excluded_reason_counts=excluded_reason_counts,
    )
    failures = validate_manifest(provisional)
    payload = {
        "schema_version": provisional.schema_version,
        "split_hashes": split_hashes,
        "splits": {
            split: [entry.__dict__ for entry in entries]
            for split, entries in frozen_splits.items()
        },
        "diagnostic_entries": [entry.__dict__ for entry in provisional.diagnostic_entries],
        "excluded_reason_counts": excluded_reason_counts,
    }
    return DatasetManifest(
        schema_version=2,
        dataset_hash=_hash(payload),
        split_hashes=split_hashes,
        splits=frozen_splits,
        diagnostic_entries=provisional.diagnostic_entries,
        excluded_reason_counts=excluded_reason_counts,
        validation_failures=failures,
    )
