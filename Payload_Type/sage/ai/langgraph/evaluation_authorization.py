"""Pure evaluation-authorization schema and arm-blind decision boundary.

This module is intentionally narrower than a production authorization system. It
exists only to make prospective evaluation rows countable: a frozen manifest, a
trusted cell binding, one post-normalization action envelope, and one deterministic
allow/deny/unknown decision. It does not call models, inspect outcomes, or mediate
runtime effects by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


MANIFEST_SCHEMA = "evaluation-authorization-manifest-v1"
CELL_BINDING_SCHEMA = "evaluation-authorization-cell-binding-v1"
ACTION_ENVELOPE_SCHEMA = "evaluation-action-envelope-v1"
DECISION_SCHEMA = "evaluation-authorization-decision-v1"
GATE_VERSION = "evaluation-authorization-gate-v1"

ALLOW = "allow"
DENY = "deny"
UNKNOWN = "unknown"

_DECISIONS = frozenset({ALLOW, DENY, UNKNOWN})
_TARGET_DIMENSIONS = frozenset({"hosts", "domains", "principals", "directory_objects", "trust_edges"})
_AUDIT_ONLY_FIELDS = frozenset({"decision_origin", "policy_decision_id"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).casefold()


def _tuple_text(values: Any, *, lower: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    normalizer = _lower if lower else _text
    return tuple(sorted({normalizer(item) for item in values if normalizer(item)}))


def _mapping_of_tuples(value: Mapping[str, Any] | None, *, lower: bool = False) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        return MappingProxyType({})
    out: dict[str, tuple[str, ...]] = {}
    for key, items in value.items():
        normalized_key = _lower(key)
        if normalized_key not in _TARGET_DIMENSIONS:
            continue
        normalized_items = _tuple_text(items, lower=lower)
        if normalized_items:
            out[normalized_key] = normalized_items
    return MappingProxyType(dict(sorted(out.items())))


def _mapping_of_text(value: Mapping[str, Any] | None, *, lower: bool = False) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        return MappingProxyType({})
    normalizer = _lower if lower else _text
    out = {
        _lower(key): normalizer(item)
        for key, item in value.items()
        if _lower(key) in _TARGET_DIMENSIONS and normalizer(item)
    }
    return MappingProxyType(dict(sorted(out.items())))


def canonical_json_sha256(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _parse_time(value: str) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CallbackSelector:
    callback_id: str
    host: str
    domain: str
    identity: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "callback_id", _text(self.callback_id))
        object.__setattr__(self, "host", _lower(self.host))
        object.__setattr__(self, "domain", _lower(self.domain))
        object.__setattr__(self, "identity", _lower(self.identity))

    def to_dict(self) -> dict[str, str]:
        return {
            "callback_id": self.callback_id,
            "host": self.host,
            "domain": self.domain,
            "identity": self.identity,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CallbackSelector | None":
        if not isinstance(value, Mapping):
            return None
        selector = cls(
            callback_id=value.get("callback_id") or "",
            host=value.get("host") or "",
            domain=value.get("domain") or "",
            identity=value.get("identity") or "",
        )
        return selector if all(selector.to_dict().values()) else None


@dataclass(frozen=True)
class EvaluationAuthorizationManifest:
    manifest_id: str
    version: str
    operator_authorization_id: str
    engagement_id: str
    range_id: str
    snapshot_id: str
    valid_from: str
    valid_until: str
    allowed_cells: tuple[str, ...] = field(default_factory=tuple)
    callbacks: tuple[CallbackSelector, ...] = field(default_factory=tuple)
    target_realms: tuple[str, ...] = field(default_factory=tuple)
    allowed_targets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    allowed_capabilities: tuple[str, ...] = field(default_factory=tuple)
    denied_capabilities: tuple[str, ...] = field(default_factory=tuple)
    allowed_effects: tuple[str, ...] = field(default_factory=tuple)
    denied_effects: tuple[str, ...] = field(default_factory=tuple)
    schema: str = MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _text(self.schema) or MANIFEST_SCHEMA)
        object.__setattr__(self, "manifest_id", _text(self.manifest_id))
        object.__setattr__(self, "version", _text(self.version))
        object.__setattr__(self, "operator_authorization_id", _text(self.operator_authorization_id))
        object.__setattr__(self, "engagement_id", _text(self.engagement_id))
        object.__setattr__(self, "range_id", _text(self.range_id))
        object.__setattr__(self, "snapshot_id", _text(self.snapshot_id))
        object.__setattr__(self, "valid_from", _text(self.valid_from))
        object.__setattr__(self, "valid_until", _text(self.valid_until))
        object.__setattr__(self, "allowed_cells", _tuple_text(self.allowed_cells))
        object.__setattr__(
            self,
            "callbacks",
            tuple(sorted((item for item in self.callbacks if isinstance(item, CallbackSelector)), key=lambda item: json.dumps(item.to_dict(), sort_keys=True))),
        )
        object.__setattr__(self, "target_realms", _tuple_text(self.target_realms, lower=True))
        object.__setattr__(self, "allowed_targets", _mapping_of_tuples(self.allowed_targets, lower=True))
        object.__setattr__(self, "allowed_capabilities", _tuple_text(self.allowed_capabilities, lower=True))
        object.__setattr__(self, "denied_capabilities", _tuple_text(self.denied_capabilities, lower=True))
        object.__setattr__(self, "allowed_effects", _tuple_text(self.allowed_effects, lower=True))
        object.__setattr__(self, "denied_effects", _tuple_text(self.denied_effects, lower=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "manifest_id": self.manifest_id,
            "version": self.version,
            "operator_authorization_id": self.operator_authorization_id,
            "engagement_id": self.engagement_id,
            "range_id": self.range_id,
            "snapshot_id": self.snapshot_id,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "allowed_cells": list(self.allowed_cells),
            "callbacks": [item.to_dict() for item in self.callbacks],
            "target_realms": list(self.target_realms),
            "allowed_targets": {key: list(values) for key, values in self.allowed_targets.items()},
            "allowed_capabilities": list(self.allowed_capabilities),
            "denied_capabilities": list(self.denied_capabilities),
            "allowed_effects": list(self.allowed_effects),
            "denied_effects": list(self.denied_effects),
        }

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "EvaluationAuthorizationManifest | None":
        if not isinstance(value, Mapping):
            return None
        callbacks = tuple(
            selector
            for selector in (CallbackSelector.from_dict(item) for item in value.get("callbacks") or ())
            if selector is not None
        )
        try:
            return cls(
                schema=value.get("schema") or MANIFEST_SCHEMA,
                manifest_id=value.get("manifest_id") or "",
                version=value.get("version") or "",
                operator_authorization_id=value.get("operator_authorization_id") or "",
                engagement_id=value.get("engagement_id") or "",
                range_id=value.get("range_id") or "",
                snapshot_id=value.get("snapshot_id") or "",
                valid_from=value.get("valid_from") or "",
                valid_until=value.get("valid_until") or "",
                allowed_cells=tuple(value.get("allowed_cells") or ()),
                callbacks=callbacks,
                target_realms=tuple(value.get("target_realms") or ()),
                allowed_targets=value.get("allowed_targets") if isinstance(value.get("allowed_targets"), Mapping) else {},
                allowed_capabilities=tuple(value.get("allowed_capabilities") or ()),
                denied_capabilities=tuple(value.get("denied_capabilities") or ()),
                allowed_effects=tuple(value.get("allowed_effects") or ()),
                denied_effects=tuple(value.get("denied_effects") or ()),
            )
        except Exception:
            return None


@dataclass(frozen=True)
class TrustedCellBinding:
    cell_id: str
    cell_authorization_id: str
    manifest_id: str
    manifest_sha256: str
    engagement_id: str
    callback: CallbackSelector
    issued_at: str
    expires_at: str
    schema: str = CELL_BINDING_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _text(self.schema) or CELL_BINDING_SCHEMA)
        object.__setattr__(self, "cell_id", _text(self.cell_id))
        object.__setattr__(self, "cell_authorization_id", _text(self.cell_authorization_id))
        object.__setattr__(self, "manifest_id", _text(self.manifest_id))
        object.__setattr__(self, "manifest_sha256", _lower(self.manifest_sha256))
        object.__setattr__(self, "engagement_id", _text(self.engagement_id))
        object.__setattr__(self, "issued_at", _text(self.issued_at))
        object.__setattr__(self, "expires_at", _text(self.expires_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "cell_id": self.cell_id,
            "cell_authorization_id": self.cell_authorization_id,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "engagement_id": self.engagement_id,
            "callback": self.callback.to_dict(),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class ActionEnvelope:
    manifest_id: str
    manifest_version: str
    manifest_sha256: str
    operator_authorization_id: str
    cell_id: str
    cell_authorization_id: str
    engagement_id: str
    callback: CallbackSelector
    target_fields: Mapping[str, str]
    capability: str
    effects: tuple[str, ...]
    exact_arguments_sha256: str
    transaction_id: str
    decision_origin: str
    policy_decision_id: str = ""
    schema: str = ACTION_ENVELOPE_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _text(self.schema) or ACTION_ENVELOPE_SCHEMA)
        object.__setattr__(self, "manifest_id", _text(self.manifest_id))
        object.__setattr__(self, "manifest_version", _text(self.manifest_version))
        object.__setattr__(self, "manifest_sha256", _lower(self.manifest_sha256))
        object.__setattr__(self, "operator_authorization_id", _text(self.operator_authorization_id))
        object.__setattr__(self, "cell_id", _text(self.cell_id))
        object.__setattr__(self, "cell_authorization_id", _text(self.cell_authorization_id))
        object.__setattr__(self, "engagement_id", _text(self.engagement_id))
        object.__setattr__(self, "target_fields", _mapping_of_text(self.target_fields, lower=True))
        object.__setattr__(self, "capability", _lower(self.capability))
        object.__setattr__(self, "effects", _tuple_text(self.effects, lower=True))
        object.__setattr__(self, "exact_arguments_sha256", _lower(self.exact_arguments_sha256))
        object.__setattr__(self, "transaction_id", _text(self.transaction_id))
        object.__setattr__(self, "decision_origin", _text(self.decision_origin))
        object.__setattr__(self, "policy_decision_id", _text(self.policy_decision_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_sha256": self.manifest_sha256,
            "operator_authorization_id": self.operator_authorization_id,
            "cell_id": self.cell_id,
            "cell_authorization_id": self.cell_authorization_id,
            "engagement_id": self.engagement_id,
            "callback": self.callback.to_dict(),
            "target_fields": dict(self.target_fields),
            "capability": self.capability,
            "effects": list(self.effects),
            "exact_arguments_sha256": self.exact_arguments_sha256,
            "transaction_id": self.transaction_id,
            "decision_origin": self.decision_origin,
            "policy_decision_id": self.policy_decision_id,
        }

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.to_dict())

    def enforcement_projection(self) -> dict[str, Any]:
        payload = self.to_dict()
        for key in _AUDIT_ONLY_FIELDS:
            payload.pop(key, None)
        return payload

    @property
    def enforcement_projection_sha256(self) -> str:
        return canonical_json_sha256(self.enforcement_projection())


@dataclass(frozen=True)
class AuthorizationDecision:
    decision: str
    reason_code: str
    manifest_id: str
    manifest_version: str
    manifest_sha256: str
    action_envelope_sha256: str
    enforcement_projection_sha256: str
    gate_version: str
    operator_authorization_id: str
    cell_authorization_id: str
    cell_id: str
    transaction_id: str
    decision_origin: str
    policy_decision_id: str = ""
    schema: str = DECISION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _text(self.schema) or DECISION_SCHEMA)
        object.__setattr__(self, "decision", _lower(self.decision))
        object.__setattr__(self, "reason_code", _text(self.reason_code))
        object.__setattr__(self, "manifest_id", _text(self.manifest_id))
        object.__setattr__(self, "manifest_version", _text(self.manifest_version))
        object.__setattr__(self, "manifest_sha256", _lower(self.manifest_sha256))
        object.__setattr__(self, "action_envelope_sha256", _lower(self.action_envelope_sha256))
        object.__setattr__(self, "enforcement_projection_sha256", _lower(self.enforcement_projection_sha256))
        object.__setattr__(self, "gate_version", _text(self.gate_version) or GATE_VERSION)
        object.__setattr__(self, "operator_authorization_id", _text(self.operator_authorization_id))
        object.__setattr__(self, "cell_authorization_id", _text(self.cell_authorization_id))
        object.__setattr__(self, "cell_id", _text(self.cell_id))
        object.__setattr__(self, "transaction_id", _text(self.transaction_id))
        object.__setattr__(self, "decision_origin", _text(self.decision_origin))
        object.__setattr__(self, "policy_decision_id", _text(self.policy_decision_id))

    @property
    def decision_id(self) -> str:
        payload = {
            "schema": self.schema,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_sha256": self.manifest_sha256,
            "enforcement_projection_sha256": self.enforcement_projection_sha256,
            "gate_version": self.gate_version,
            "operator_authorization_id": self.operator_authorization_id,
            "cell_authorization_id": self.cell_authorization_id,
            "cell_id": self.cell_id,
            "transaction_id": self.transaction_id,
        }
        return "authz-" + canonical_json_sha256(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "decision_id": self.decision_id,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_sha256": self.manifest_sha256,
            "action_envelope_sha256": self.action_envelope_sha256,
            "enforcement_projection_sha256": self.enforcement_projection_sha256,
            "gate_version": self.gate_version,
            "operator_authorization_id": self.operator_authorization_id,
            "cell_authorization_id": self.cell_authorization_id,
            "cell_id": self.cell_id,
            "transaction_id": self.transaction_id,
            "decision_origin": self.decision_origin,
            "policy_decision_id": self.policy_decision_id,
        }

    def proof_lineage_fields(self) -> dict[str, str]:
        return {
            "authorization_manifest_id": self.manifest_id,
            "authorization_manifest_version": self.manifest_version,
            "authorization_manifest_sha256": self.manifest_sha256,
            "authorization_decision_id": self.decision_id,
            "authorization_decision": self.decision,
            "authorization_reason_code": self.reason_code,
            "action_envelope_sha256": self.action_envelope_sha256,
            "enforcement_projection_sha256": self.enforcement_projection_sha256,
            "authorization_gate_version": self.gate_version,
            "decision_origin": self.decision_origin,
            "policy_decision_id": self.policy_decision_id,
            "operator_authorization_id": self.operator_authorization_id,
            "cell_authorization_id": self.cell_authorization_id,
        }


def build_action_envelope(
    manifest: EvaluationAuthorizationManifest,
    cell_binding: TrustedCellBinding,
    *,
    callback: CallbackSelector,
    target_fields: Mapping[str, Any],
    capability: str,
    effects: tuple[str, ...] | list[str],
    concrete_arguments: Any,
    transaction_id: str,
    decision_origin: str,
    policy_decision_id: str = "",
) -> ActionEnvelope | None:
    """Construct an envelope only after concrete adapter arguments are resolved."""
    normalized_targets = _mapping_of_text(target_fields, lower=True)
    if (
        not isinstance(manifest, EvaluationAuthorizationManifest)
        or not isinstance(cell_binding, TrustedCellBinding)
        or not isinstance(callback, CallbackSelector)
        or not all(callback.to_dict().values())
        or not normalized_targets
        or not _text(capability)
        or not _tuple_text(effects, lower=True)
        or concrete_arguments is None
        or not _text(transaction_id)
        or not _text(decision_origin)
    ):
        return None
    return ActionEnvelope(
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.version,
        manifest_sha256=manifest.sha256,
        operator_authorization_id=manifest.operator_authorization_id,
        cell_id=cell_binding.cell_id,
        cell_authorization_id=cell_binding.cell_authorization_id,
        engagement_id=cell_binding.engagement_id,
        callback=callback,
        target_fields=normalized_targets,
        capability=capability,
        effects=tuple(effects),
        exact_arguments_sha256=canonical_json_sha256(concrete_arguments),
        transaction_id=transaction_id,
        decision_origin=decision_origin,
        policy_decision_id=policy_decision_id,
    )


def _decision(
    result: str,
    reason_code: str,
    manifest: EvaluationAuthorizationManifest,
    cell_binding: TrustedCellBinding,
    envelope: ActionEnvelope | None,
) -> AuthorizationDecision:
    envelope = envelope or ActionEnvelope(
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.version,
        manifest_sha256=manifest.sha256,
        operator_authorization_id=manifest.operator_authorization_id,
        cell_id=cell_binding.cell_id,
        cell_authorization_id=cell_binding.cell_authorization_id,
        engagement_id=cell_binding.engagement_id,
        callback=cell_binding.callback,
        target_fields={},
        capability="",
        effects=(),
        exact_arguments_sha256="",
        transaction_id="",
        decision_origin="",
    )
    return AuthorizationDecision(
        decision=result if result in _DECISIONS else UNKNOWN,
        reason_code=reason_code,
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.version,
        manifest_sha256=manifest.sha256,
        action_envelope_sha256=envelope.sha256,
        enforcement_projection_sha256=envelope.enforcement_projection_sha256,
        gate_version=GATE_VERSION,
        operator_authorization_id=manifest.operator_authorization_id,
        cell_authorization_id=cell_binding.cell_authorization_id,
        cell_id=cell_binding.cell_id,
        transaction_id=envelope.transaction_id,
        decision_origin=envelope.decision_origin,
        policy_decision_id=envelope.policy_decision_id,
    )


def authorize_action(
    manifest: EvaluationAuthorizationManifest,
    cell_binding: TrustedCellBinding,
    envelope: ActionEnvelope | None,
    *,
    now: str,
    seen_enforcement_digests: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> AuthorizationDecision:
    """Return one arm-blind deterministic allow/deny/unknown decision.

    Only the immutable manifest, trusted cell binding, normalized enforcement
    projection, and the caller-supplied current time participate in the decision.
    """
    if not isinstance(manifest, EvaluationAuthorizationManifest) or not isinstance(cell_binding, TrustedCellBinding):
        raise TypeError("manifest and cell_binding are required")
    if envelope is None:
        return _decision(UNKNOWN, "missing_action_envelope", manifest, cell_binding, envelope)
    if manifest.schema != MANIFEST_SCHEMA or cell_binding.schema != CELL_BINDING_SCHEMA or envelope.schema != ACTION_ENVELOPE_SCHEMA:
        return _decision(UNKNOWN, "unsupported_schema", manifest, cell_binding, envelope)
    current = _parse_time(now)
    start = _parse_time(manifest.valid_from)
    end = _parse_time(manifest.valid_until)
    binding_start = _parse_time(cell_binding.issued_at)
    binding_end = _parse_time(cell_binding.expires_at)
    if current is None or start is None or end is None or binding_start is None or binding_end is None:
        return _decision(UNKNOWN, "invalid_time_context", manifest, cell_binding, envelope)
    if current < start or current > end or current < binding_start or current > binding_end:
        return _decision(DENY, "stale_authorization_context", manifest, cell_binding, envelope)
    if (
        cell_binding.manifest_id != manifest.manifest_id
        or cell_binding.manifest_sha256 != manifest.sha256
        or envelope.manifest_id != manifest.manifest_id
        or envelope.manifest_version != manifest.version
        or envelope.manifest_sha256 != manifest.sha256
    ):
        return _decision(DENY, "manifest_binding_mismatch", manifest, cell_binding, envelope)
    if (
        cell_binding.engagement_id != manifest.engagement_id
        or envelope.engagement_id != manifest.engagement_id
        or envelope.engagement_id != cell_binding.engagement_id
    ):
        return _decision(DENY, "engagement_binding_mismatch", manifest, cell_binding, envelope)
    if (
        envelope.cell_id != cell_binding.cell_id
        or envelope.cell_authorization_id != cell_binding.cell_authorization_id
        or envelope.operator_authorization_id != manifest.operator_authorization_id
        or envelope.cell_id not in manifest.allowed_cells
    ):
        return _decision(DENY, "cell_binding_mismatch", manifest, cell_binding, envelope)
    if envelope.callback != cell_binding.callback or envelope.callback not in manifest.callbacks:
        return _decision(DENY, "callback_binding_mismatch", manifest, cell_binding, envelope)
    if envelope.enforcement_projection_sha256 in set(seen_enforcement_digests):
        return _decision(DENY, "replay_detected", manifest, cell_binding, envelope)
    if envelope.capability in manifest.denied_capabilities or any(effect in manifest.denied_effects for effect in envelope.effects):
        return _decision(DENY, "explicit_deny", manifest, cell_binding, envelope)
    if envelope.capability not in manifest.allowed_capabilities or any(effect not in manifest.allowed_effects for effect in envelope.effects):
        return _decision(DENY, "capability_or_effect_not_allowed", manifest, cell_binding, envelope)
    if not envelope.target_fields:
        return _decision(UNKNOWN, "missing_target_context", manifest, cell_binding, envelope)
    for dimension, value in envelope.target_fields.items():
        allowed = manifest.allowed_targets.get(dimension, ())
        if not allowed or value not in allowed:
            return _decision(DENY, f"target_not_allowed:{dimension}", manifest, cell_binding, envelope)
    if manifest.target_realms:
        envelope_domains = {
            value
            for key, value in envelope.target_fields.items()
            if key in {"domains", "trust_edges"}
        }
        if envelope_domains and not any(
            domain in manifest.target_realms
            for domain in envelope_domains
        ):
            return _decision(DENY, "target_realm_not_allowed", manifest, cell_binding, envelope)
    return _decision(ALLOW, "manifest_allows_exact_envelope", manifest, cell_binding, envelope)


def authorization_join_matches(
    decision: AuthorizationDecision,
    envelope: ActionEnvelope,
    *,
    seen_decision_ids: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> tuple[bool, str]:
    if not isinstance(decision, AuthorizationDecision) or not isinstance(envelope, ActionEnvelope):
        return False, "missing_authorization_join"
    if decision.decision != ALLOW:
        return False, "authorization_not_allow"
    if decision.decision_id in set(seen_decision_ids):
        return False, "authorization_decision_replayed"
    if decision.action_envelope_sha256 != envelope.sha256:
        return False, "action_envelope_digest_mismatch"
    if decision.enforcement_projection_sha256 != envelope.enforcement_projection_sha256:
        return False, "enforcement_projection_digest_mismatch"
    if (
        decision.manifest_id != envelope.manifest_id
        or decision.manifest_version != envelope.manifest_version
        or decision.manifest_sha256 != envelope.manifest_sha256
        or decision.operator_authorization_id != envelope.operator_authorization_id
        or decision.cell_authorization_id != envelope.cell_authorization_id
        or decision.cell_id != envelope.cell_id
        or decision.transaction_id != envelope.transaction_id
    ):
        return False, "authorization_lineage_mismatch"
    return True, "authorization_join_valid"

