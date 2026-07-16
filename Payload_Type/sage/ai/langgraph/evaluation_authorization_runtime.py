"""Eval-only runtime mediation for the frozen evaluation authorization boundary.

The pure decision function lives in :mod:`evaluation_authorization`.  This module
adds only the runtime bookkeeping needed by Phase 17/18 evaluation sessions:

* load one private frozen manifest + trusted cell binding;
* build an envelope from already-normalized adapter arguments;
* call the frozen arm-blind authority exactly once per covered mutation attempt;
* retain an append-only allow/deny/unknown event ledger for proof joins and audits.

It is disabled unless ``SAGE_EVAL_AUTHORIZATION_MODE`` is truthy.  Normal Sage
sessions therefore do not acquire a new execution gate or any new prompt-visible
state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping

try:
    from . import evaluation_authorization as auth
except ImportError:
    import evaluation_authorization as auth


RUNTIME_CONTEXT_SCHEMA = "evaluation-authorization-runtime-context-v1"
RUNTIME_EVENT_SCHEMA = "evaluation-authorization-runtime-event-v1"
AUTHORIZATION_MODE_ENV = "SAGE_EVAL_AUTHORIZATION_MODE"
AUTHORIZATION_CONTEXT_PATH_ENV = "SAGE_EVAL_AUTHORIZATION_CONTEXT_PATH"
AUTHORIZATION_CONTEXT_JSON_ENV = "SAGE_EVAL_AUTHORIZATION_CONTEXT_JSON"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    return _text(value).casefold() in _TRUE_VALUES


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvaluationAuthorizationTerminal(RuntimeError):
    """Raised before a covered mutation when eval authorization fails closed."""

    def __init__(self, outcome: "RuntimeAuthorizationOutcome"):
        self.outcome = outcome
        super().__init__(outcome.reason_code)


def _load_json_text(value: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(value)
    except Exception:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _load_json_path(path: str) -> Mapping[str, Any] | None:
    try:
        return _load_json_text(Path(path).expanduser().read_text(encoding="utf-8"))
    except Exception:
        return None


@dataclass(frozen=True)
class RuntimeAuthorizationOutcome:
    """One final-boundary authorization result.

    ``enabled=False`` means evaluation authorization is intentionally inactive and
    callers should preserve normal product behavior.  ``enabled=True`` with
    ``allowed=False`` is a fail-closed evaluation terminal.
    """

    enabled: bool
    allowed: bool
    reason_code: str
    decision: auth.AuthorizationDecision | None = None
    envelope: auth.ActionEnvelope | None = None
    join_valid: bool = False
    join_reason: str = ""
    gate_available: bool = True

    @property
    def authorization(self) -> dict[str, str]:
        if (
            self.enabled
            and self.allowed
            and self.join_valid
            and isinstance(self.decision, auth.AuthorizationDecision)
        ):
            return self.decision.proof_lineage_fields()
        return {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "enabled": self.enabled,
            "allowed": self.allowed,
            "gate_available": self.gate_available,
            "reason_code": self.reason_code,
            "join_valid": self.join_valid,
            "join_reason": self.join_reason,
        }
        if self.decision is not None:
            payload["decision"] = self.decision.to_dict()
        if self.envelope is not None:
            payload["action_envelope"] = self.envelope.to_dict()
        return payload


@dataclass
class EvaluationAuthorizationRuntime:
    """Mutable runtime holder around the immutable Phase 16 authorization inputs."""

    enabled: bool
    manifest: auth.EvaluationAuthorizationManifest | None = None
    cell_binding: auth.TrustedCellBinding | None = None
    source: str = ""
    unavailable_reason: str = ""
    seen_enforcement_digests: set[str] = field(default_factory=set)
    seen_decision_ids: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    schema: str = RUNTIME_CONTEXT_SCHEMA

    @property
    def available(self) -> bool:
        return (
            self.enabled
            and isinstance(self.manifest, auth.EvaluationAuthorizationManifest)
            and isinstance(self.cell_binding, auth.TrustedCellBinding)
            and not self.unavailable_reason
        )

    @classmethod
    def disabled(cls, *, source: str = "") -> "EvaluationAuthorizationRuntime":
        return cls(enabled=False, source=source or "disabled")

    @classmethod
    def unavailable(cls, reason: str, *, source: str = "") -> "EvaluationAuthorizationRuntime":
        return cls(enabled=True, source=source or "unavailable", unavailable_reason=_text(reason) or "authorization_gate_unavailable")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any] | None,
        *,
        enabled: bool = True,
        source: str = "dict",
    ) -> "EvaluationAuthorizationRuntime":
        if not enabled:
            return cls.disabled(source=source)
        if not isinstance(value, Mapping):
            return cls.unavailable("authorization_context_missing", source=source)
        manifest_value = (
            value.get("authorization_manifest")
            if isinstance(value.get("authorization_manifest"), Mapping)
            else value.get("manifest")
        )
        binding_value = (
            value.get("trusted_cell_binding")
            if isinstance(value.get("trusted_cell_binding"), Mapping)
            else value.get("cell_binding")
        )
        manifest = auth.EvaluationAuthorizationManifest.from_dict(manifest_value)
        binding = _trusted_cell_binding_from_dict(binding_value)
        if manifest is None:
            return cls.unavailable("authorization_manifest_invalid", source=source)
        if binding is None:
            return cls.unavailable("trusted_cell_binding_invalid", source=source)
        return cls(enabled=True, manifest=manifest, cell_binding=binding, source=source)

    @classmethod
    def from_env(cls) -> "EvaluationAuthorizationRuntime":
        if not _truthy(os.environ.get(AUTHORIZATION_MODE_ENV)):
            return cls.disabled(source="env:disabled")
        path = _text(os.environ.get(AUTHORIZATION_CONTEXT_PATH_ENV))
        if path:
            return cls.from_dict(_load_json_path(path), source=f"env:path:{path}")
        raw = _text(os.environ.get(AUTHORIZATION_CONTEXT_JSON_ENV))
        if raw:
            return cls.from_dict(_load_json_text(raw), source="env:json")
        return cls.unavailable("authorization_context_missing", source="env")

    def authorize(
        self,
        *,
        callback: auth.CallbackSelector | None,
        target_fields: Mapping[str, Any],
        capability: str,
        effects: tuple[str, ...] | list[str],
        concrete_arguments: Any,
        transaction_id: str,
        decision_origin: str,
        policy_decision_id: str = "",
        now: str | None = None,
        boundary: str = "",
    ) -> RuntimeAuthorizationOutcome:
        if not self.enabled:
            return RuntimeAuthorizationOutcome(
                enabled=False,
                allowed=True,
                reason_code="evaluation_authorization_disabled",
                gate_available=False,
            )
        if not self.available:
            outcome = RuntimeAuthorizationOutcome(
                enabled=True,
                allowed=False,
                reason_code=self.unavailable_reason or "authorization_gate_unavailable",
                gate_available=False,
            )
            self._record_event(outcome, boundary=boundary, capability=capability, transaction_id=transaction_id)
            return outcome
        assert self.manifest is not None
        assert self.cell_binding is not None
        envelope = auth.build_action_envelope(
            self.manifest,
            self.cell_binding,
            callback=callback,
            target_fields=target_fields,
            capability=capability,
            effects=tuple(effects),
            concrete_arguments=concrete_arguments,
            transaction_id=transaction_id,
            decision_origin=decision_origin,
            policy_decision_id=policy_decision_id,
        )
        decision = auth.authorize_action(
            self.manifest,
            self.cell_binding,
            envelope,
            now=now or _utc_now(),
            seen_enforcement_digests=self.seen_enforcement_digests,
        )
        join_valid = False
        join_reason = ""
        if envelope is not None and decision.decision == auth.ALLOW:
            join_valid, join_reason = auth.authorization_join_matches(
                decision,
                envelope,
                seen_decision_ids=self.seen_decision_ids,
            )
        elif decision.decision == auth.ALLOW:
            join_reason = "missing_authorization_join"
        outcome = RuntimeAuthorizationOutcome(
            enabled=True,
            allowed=decision.decision == auth.ALLOW and join_valid,
            reason_code=decision.reason_code if decision.decision != auth.ALLOW or join_valid else join_reason,
            decision=decision,
            envelope=envelope,
            join_valid=join_valid,
            join_reason=join_reason,
            gate_available=True,
        )
        self._record_event(outcome, boundary=boundary, capability=capability, transaction_id=transaction_id)
        if outcome.allowed and envelope is not None:
            self.seen_enforcement_digests.add(envelope.enforcement_projection_sha256)
            self.seen_decision_ids.add(decision.decision_id)
        return outcome

    def _record_event(
        self,
        outcome: RuntimeAuthorizationOutcome,
        *,
        boundary: str,
        capability: str,
        transaction_id: str,
    ) -> None:
        event = {
            "schema": RUNTIME_EVENT_SCHEMA,
            "sequence": len(self.events) + 1,
            "captured_at": _utc_now(),
            "boundary": _text(boundary),
            "capability": _text(capability).casefold(),
            "transaction_id": _text(transaction_id),
            "enabled": outcome.enabled,
            "gate_available": outcome.gate_available,
            "allowed": outcome.allowed,
            "reason_code": outcome.reason_code,
            "join_valid": outcome.join_valid,
            "join_reason": outcome.join_reason,
        }
        if outcome.decision is not None:
            event["decision"] = outcome.decision.to_dict()
        if outcome.envelope is not None:
            event["action_envelope"] = outcome.envelope.to_dict()
        self.events.append(event)

    def audit_snapshot(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "enabled": self.enabled,
            "available": self.available,
            "source": self.source,
            "unavailable_reason": self.unavailable_reason,
            "manifest_id": self.manifest.manifest_id if self.manifest is not None else "",
            "manifest_sha256": self.manifest.sha256 if self.manifest is not None else "",
            "cell_id": self.cell_binding.cell_id if self.cell_binding is not None else "",
            "cell_authorization_id": self.cell_binding.cell_authorization_id if self.cell_binding is not None else "",
            "seen_enforcement_digests": sorted(self.seen_enforcement_digests),
            "seen_decision_ids": sorted(self.seen_decision_ids),
            "events": list(self.events),
        }


def _trusted_cell_binding_from_dict(value: Any) -> auth.TrustedCellBinding | None:
    if not isinstance(value, Mapping):
        return None
    callback = auth.CallbackSelector.from_dict(value.get("callback"))
    if callback is None or not callback.is_runtime_bound:
        return None
    try:
        return auth.TrustedCellBinding(
            schema=value.get("schema") or auth.CELL_BINDING_SCHEMA,
            cell_id=value.get("cell_id") or "",
            cell_authorization_id=value.get("cell_authorization_id") or "",
            manifest_id=value.get("manifest_id") or "",
            manifest_sha256=value.get("manifest_sha256") or "",
            engagement_id=value.get("engagement_id") or "",
            callback=callback,
            issued_at=value.get("issued_at") or "",
            expires_at=value.get("expires_at") or "",
        )
    except Exception:
        return None
