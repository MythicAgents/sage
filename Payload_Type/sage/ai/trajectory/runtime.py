"""Runtime bridge from capability failures to trajectory repair policy."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from .labeler import classify_observation, repair_for_label
from .replay import FrequencyRepairPolicy, RepairDecision
from .schema import (
    EVIDENCE_ROLE_DIAGNOSTIC_ONLY,
    LABEL_SOURCE_CLASSIFIER,
    OUTCOME_DIAGNOSTIC_ONLY,
    TransitionCommand,
    TransitionObservation,
    TransitionRecord,
    TransitionRepair,
    TransitionVerifier,
    load_jsonl,
    redact_text,
    write_jsonl,
)

_DISABLED_VALUES = {"0", "false", "no", "off", "disabled"}
_SECRET_KEYS = {
    "aes",
    "aes128",
    "aes256",
    "base64ticket",
    "ca_cert_password",
    "ca_certificate_password",
    "ca_pfx_password",
    "certificate_password",
    "credential",
    "credential_text",
    "existingticket",
    "forged_certificate_password",
    "forged_pfx_password",
    "key",
    "local_admin_password",
    "managed_local_admin_secret",
    "new_cert_password",
    "ntlm",
    "password",
    "pfx_password",
    "rc4",
    "secret",
    "ticket",
    "ticket_base64",
}


def default_store_path() -> Path:
    """Return the repo-local runtime transition store path."""
    return Path(__file__).resolve().parents[2] / ".trajectory" / "transitions.jsonl"


def runtime_enabled() -> bool:
    """Return whether runtime trajectory logging/repair recall is enabled."""
    if os.environ.get("SAGE_TRAJECTORY_DISABLE", "").strip().casefold() in {"1", "true", "yes", "on"}:
        return False
    configured = os.environ.get("SAGE_TRAJECTORY_ENABLED")
    if configured is None:
        return True
    return configured.strip().casefold() not in _DISABLED_VALUES


class TrajectoryRepairBridge:
    """Append redacted failure transitions and recall likely repairs."""

    def __init__(self, store_path: str | Path | None = None, *, enabled: bool | None = None):
        self.store_path = Path(store_path).expanduser() if store_path else default_store_path()
        self.enabled = runtime_enabled() if enabled is None else bool(enabled)

    @classmethod
    def from_env(cls) -> "TrajectoryRepairBridge":
        return cls(os.environ.get("SAGE_TRAJECTORY_STORE") or None)

    def record_failure(
        self,
        *,
        action: Any,
        inputs: dict[str, Any] | None,
        callback_id: str | int | None,
        reason: Any,
        issued: Iterable[dict[str, Any]] | None = None,
        verifier_status: str = "failed",
        source: str = "execute_capability",
        build_payload: dict[str, Any] | None = None,
        policy_decision: dict[str, Any] | None = None,
        transaction_id: str = "",
        proof_envelope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "recorded": False}

        issued_rows = [dict(item) for item in (issued or []) if isinstance(item, dict)]
        observation = self._observation_text(reason, issued_rows, build_payload)
        record = self.build_failure_record(
            action=action,
            inputs=inputs or {},
            callback_id=callback_id,
            observation=observation,
            issued=issued_rows,
            verifier_status=verifier_status,
            source=source,
            policy_decision=policy_decision or {},
            transaction_id=transaction_id,
            proof_envelope=proof_envelope or {},
        )
        prior = self._load_prior()
        self.record_transition(record)
        # Runtime recall stays advisory. Diagnostic proposed repairs may be surfaced to the
        # operator, but replay/training defaults never count them as verified positive labels.
        decision = FrequencyRepairPolicy([*prior, record], include_diagnostic=True).choose(record)
        return self._result_payload(record, decision)

    def build_failure_record(
        self,
        *,
        action: Any,
        inputs: dict[str, Any],
        callback_id: str | int | None,
        observation: str,
        issued: list[dict[str, Any]],
        verifier_status: str,
        source: str,
        policy_decision: dict[str, Any] | None = None,
        transaction_id: str = "",
        proof_envelope: dict[str, Any] | None = None,
    ) -> TransitionRecord:
        classification = classify_observation(observation)
        repair_data = repair_for_label(classification.label)
        repair = (
            TransitionRepair(kind=repair_data[0], retry_budget=repair_data[1], notes=repair_data[2])
            if repair_data
            else None
        )
        capability = self._capability_name(action, inputs, observation)
        action_map = self._action_dict(action)
        policy_decision = policy_decision if isinstance(policy_decision, dict) else {}
        proof_envelope = proof_envelope if isinstance(proof_envelope, dict) else {}
        proof_id = self._proof_hash(proof_envelope)
        command_records = tuple(self._command_record(item) for item in issued if self._command_record(item))
        return TransitionRecord(
            run_id=(
                os.environ.get("SAGE_TRAJECTORY_RUN_ID")
                or os.environ.get("SAGE_ENGAGEMENT_ID")
                or "runtime"
            ),
            source_files=(source,),
            objective=os.environ.get("SAGE_ENGAGEMENT_OBJECTIVE", ""),
            capability=capability,
            inputs=self._redact_structure(inputs),
            observations=(
                TransitionObservation(
                    kind="runtime_failure",
                    labels=classification.labels,
                    excerpt=redact_text(" ".join(str(observation).split()))[:1000],
                    source=source,
                ),
            ),
            verifier=TransitionVerifier(
                status=verifier_status or "failed",
                labels=classification.labels,
                evidence={
                    "classification": classification.label,
                    "callback_id": str(callback_id or ""),
                },
                verifier_id=str(proof_envelope.get("verifier_id") or ""),
                verifier_version=str(proof_envelope.get("verifier_version") or "v1"),
                proof_ids=((proof_id,) if proof_id else ()),
                admissible_proof=False,
            ),
            failure_label=classification.label,
            repair=repair,
            env_fingerprint={
                "source": source,
                "callback_id": str(callback_id or ""),
            },
            state_before={
                "target": str(action_map.get("target") or ""),
                "effects_requested": list(action_map.get("effects") or []),
                "features": self._features_for_label(classification.label),
            },
            commands=command_records,
            state_after={"effects_added": [], "effects_failed": [capability] if capability else []},
            episode_id=str(policy_decision.get("episode_id") or ""),
            engagement_id=str(os.environ.get("SAGE_ENGAGEMENT_ID") or ""),
            decision_id=str(policy_decision.get("decision_id") or ""),
            transaction_id=str(transaction_id or policy_decision.get("transaction_id") or ""),
            callback_id=str(callback_id or ""),
            task_ids=tuple(
                str(item.get("task_id"))
                for item in issued
                if isinstance(item, dict) and item.get("task_id") not in (None, "")
            ),
            proof_ids=((proof_id,) if proof_id else ()),
            proof_envelope=dict(proof_envelope),
            policy_version=str(policy_decision.get("policy_version") or ""),
            effective_backend=str(policy_decision.get("effective_backend") or ""),
            effective_request_id=str(
                (policy_decision.get("response_metadata") or {}).get("request_id")
                if isinstance(policy_decision.get("response_metadata"), dict)
                else ""
            ),
            raw_frontier_hash=str(policy_decision.get("candidate_set_hash") or ""),
            admissible_frontier_hash=str(policy_decision.get("ordered_frontier_hash") or ""),
            semantic_candidate_ids=tuple(policy_decision.get("semantic_candidate_ids") or ()),
            normalized_state_before={
                "target": str(action_map.get("target") or ""),
                "effects_requested": list(action_map.get("effects") or []),
                "features": self._features_for_label(classification.label),
            },
            normalized_state_after={"effects_added": [], "effects_failed": [capability] if capability else []},
            label_source=LABEL_SOURCE_CLASSIFIER,
            evidence_role=EVIDENCE_ROLE_DIAGNOSTIC_ONLY,
            outcome_source=OUTCOME_DIAGNOSTIC_ONLY,
            transition_outcome=(verifier_status or "failed").casefold(),
            proof_envelope_ref=proof_id,
        )

    def record_transition(self, record: TransitionRecord) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(str(self.store_path), [record], append=True)

    def rank_repairs(self, record: TransitionRecord) -> RepairDecision | None:
        return FrequencyRepairPolicy(self._load_prior(), include_diagnostic=True).choose(record)

    def _load_prior(self) -> list[TransitionRecord]:
        if not self.store_path.exists():
            return []
        try:
            return load_jsonl(str(self.store_path))
        except Exception:
            return []

    def _result_payload(self, record: TransitionRecord, decision: RepairDecision | None) -> dict[str, Any]:
        repair_payload = asdict(record.repair) if record.repair else None
        decision_payload = asdict(decision) if decision else None
        return {
            "enabled": True,
            "recorded": True,
            "store": str(self.store_path),
            "failure_label": record.failure_label,
            "labels": list(record.observations[0].labels if record.observations else ()),
            "repair": repair_payload,
            "decision": decision_payload,
            "evidence": list(record.verifier.labels),
            "observation_excerpt": (record.observations[0].excerpt if record.observations else "")[:400],
        }

    def _observation_text(
        self,
        reason: Any,
        issued: list[dict[str, Any]],
        build_payload: dict[str, Any] | None,
    ) -> str:
        parts: list[str] = [redact_text(reason)]
        if isinstance(build_payload, dict):
            for key in ("reason", "missing", "verdict"):
                if build_payload.get(key):
                    parts.append(redact_text(build_payload.get(key)))
        for item in issued:
            for key in ("failure_reason", "verify_reason", "reason", "_output", "output_preview", "output"):
                if item.get(key):
                    parts.append(redact_text(item.get(key)))
        return "\n".join(part for part in parts if part)

    def _command_record(self, item: dict[str, Any]) -> TransitionCommand | None:
        command = str(item.get("command") or item.get("command_name") or item.get("payload_command") or "").strip()
        if not command:
            return None
        parameters = item.get("parameters")
        return TransitionCommand(
            payload_command=command,
            adapter=str(item.get("adapter") or "mythic"),
            constructed_from_builder=True,
            argument_features=tuple(self._argument_features(parameters)),
            parameters_redacted=self._redact_structure(parameters if isinstance(parameters, dict) else {"raw": parameters}),
        )

    def _argument_features(self, parameters: Any) -> list[str]:
        text = redact_text(parameters).casefold()
        features: list[str] = []
        if "\\\\" in text or "\\c$" in text or "\\sysvol" in text:
            features.append("service_or_unc_target")
        if "start-sleep" in text or "wait" in text or "ping -n" in text:
            features.append("bounded_wait")
        if "/user:" in text or "user" in text or "account" in text:
            features.append("principal_argument")
        if "/user:" in text and "\\" not in text.split("/user:", 1)[1].split()[0]:
            features.append("unqualified_principal")
        if "/user:" in text and "\\" in text.split("/user:", 1)[1].split()[0]:
            features.append("netbios_qualified_principal")
        return features

    def _capability_name(self, action: Any, inputs: dict[str, Any], observation: str) -> str:
        action_map = self._action_dict(action)
        for key in ("name", "capability"):
            value = action_map.get(key)
            if value:
                return str(value)
        value = inputs.get("capability")
        if value:
            return str(value)
        lower = observation.casefold()
        if "dcsync" in lower or "drsr" in lower or "cracknames" in lower:
            return "dcsync-account"
        if "gpo" in lower or "scheduledtasks.xml" in lower or "group policy" in lower:
            return "gpo-controlled-system-exec"
        return "unknown-capability"

    def _action_dict(self, action: Any) -> dict[str, Any]:
        if isinstance(action, dict):
            return action
        if is_dataclass(action):
            try:
                return asdict(action)
            except Exception:
                return {}
        out: dict[str, Any] = {}
        for key in ("name", "capability", "target", "effects", "intent"):
            if hasattr(action, key):
                out[key] = getattr(action, key)
        return out

    def _redact_structure(self, value: Any, key: str = "") -> Any:
        if key.casefold() in _SECRET_KEYS:
            return "<secret>"
        if isinstance(value, dict):
            return {str(k): self._redact_structure(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact_structure(item, key) for item in value]
        if isinstance(value, str):
            return redact_text(value)[:2000]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return redact_text(value)[:2000]

    def _features_for_label(self, label: str) -> list[str]:
        return {
            "ambiguous_account_name": ["directory_operation", "ambiguous_principal_resolution"],
            "delayed_effect": ["delayed_control_plane", "proof_absent"],
            "unresolved_gpo_identity": ["gpo_identity_unresolved", "exact_guid_required"],
            "dcsync_bad_dn_or_context": ["drsuapi_reached", "dcsync_target_or_context_unresolved"],
            "access_denied": ["privileged_operation", "access_denied"],
            "wrong_security_context": ["credential_or_ticket_available", "context_not_applied"],
            "command_size_limit": ["constructed_command_too_large", "staging_or_shortening_required"],
            "command_template_error": ["deterministic_builder_template", "escaping_or_runtime_syntax"],
            "directory_bind_error": ["directory_write", "writable_dc_bind_required"],
            "schema_or_argument_mismatch": ["payload_schema_required"],
        }.get(label, [label])

    @staticmethod
    def _proof_hash(proof: dict[str, Any]) -> str:
        if not isinstance(proof, dict) or not proof:
            return ""
        payload = dict(proof)
        payload.pop("persistence_state", None)
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()
