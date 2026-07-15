"""Trajectory learning primitives for Sage autonomy.

This package turns historical Sage/Phoenix/Mythic artifacts into normalized
transition records that can be replayed and used for repair-policy ranking.
"""

from .schema import (
    EVIDENCE_ROLE_DIAGNOSTIC_ONLY,
    EVIDENCE_ROLE_EMPIRICAL_NEGATIVE,
    EVIDENCE_ROLE_EMPIRICAL_OUTCOME,
    LABEL_SOURCE_BLOODHOUND,
    LABEL_SOURCE_CLASSIFIER,
    LABEL_SOURCE_DIAGNOSTIC_ONLY,
    LABEL_SOURCE_HUMAN,
    LABEL_SOURCE_MYTHIC_PROOF,
    SCHEMA_VERSION,
    TransitionCommand,
    TransitionObservation,
    TransitionRecord,
    TransitionRepair,
    TransitionVerifier,
    redact_text,
)
from .dataset import DatasetEntry, DatasetManifest, build_dataset_manifest, validate_manifest
from .labeler import FailureClassification, classify_observation
from .runtime import TrajectoryRepairBridge, default_store_path, runtime_enabled

__all__ = [
    "FailureClassification",
    "DatasetEntry",
    "DatasetManifest",
    "EVIDENCE_ROLE_DIAGNOSTIC_ONLY",
    "EVIDENCE_ROLE_EMPIRICAL_NEGATIVE",
    "EVIDENCE_ROLE_EMPIRICAL_OUTCOME",
    "LABEL_SOURCE_BLOODHOUND",
    "LABEL_SOURCE_CLASSIFIER",
    "LABEL_SOURCE_DIAGNOSTIC_ONLY",
    "LABEL_SOURCE_HUMAN",
    "LABEL_SOURCE_MYTHIC_PROOF",
    "SCHEMA_VERSION",
    "TrajectoryRepairBridge",
    "TransitionCommand",
    "TransitionObservation",
    "TransitionRecord",
    "TransitionRepair",
    "TransitionVerifier",
    "classify_observation",
    "build_dataset_manifest",
    "default_store_path",
    "redact_text",
    "runtime_enabled",
    "validate_manifest",
]
