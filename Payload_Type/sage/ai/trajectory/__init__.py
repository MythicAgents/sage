"""Trajectory learning primitives for Sage autonomy.

This package turns historical Sage/Phoenix/Mythic artifacts into normalized
transition records that can be replayed and used for repair-policy ranking.
"""

from .schema import (
    TransitionCommand,
    TransitionObservation,
    TransitionRecord,
    TransitionRepair,
    TransitionVerifier,
    redact_text,
)
from .labeler import FailureClassification, classify_observation
from .runtime import TrajectoryRepairBridge, default_store_path, runtime_enabled

__all__ = [
    "FailureClassification",
    "TrajectoryRepairBridge",
    "TransitionCommand",
    "TransitionObservation",
    "TransitionRecord",
    "TransitionRepair",
    "TransitionVerifier",
    "classify_observation",
    "default_store_path",
    "redact_text",
    "runtime_enabled",
]
