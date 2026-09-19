"""Pure retained-evidence evaluators for endpoint protection and RBCD effects.

Both public producers return an ``OracleObservation`` only when every declared
binding is present.  They never consume shipping capability verdicts or caller
labels.  The caller supplies the exact operation/callback/task/target bindings;
RBCD additionally requires a committed payload-derived query receipt.
"""
from __future__ import annotations

import hashlib
import json
import re
import base64
from datetime import datetime
from typing import Any, Mapping

from ai.hillclimb import effect_registry
from ai.langgraph import proof_boundary


_ENDPOINT = "ENDPOINT_PROTECTION_STATE"
_RBCD = "RBCD_RIGHTS"
_RBCD_VERIFIER_ID = "m1-rbcd-rights-query:v1"
_RBCD_VERIFIER_VERSION = "v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: object) -> str:
    return str(value or "").strip()


def _norm(value: object) -> str:
    return _text(value).casefold().rstrip(".")


def _params(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = row.get("original_params")
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _request_exact(value: object) -> Mapping[str, Any] | str | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return parsed if isinstance(parsed, Mapping) else value


def _response_output(value: str) -> str:
    if re.search(r"(?m)^\s*EP_[A-Z0-9_]+=", value):
        return value
    try:
        return base64.b64decode(value, validate=True).decode()
    except (ValueError, UnicodeError):
        return value


def _field_lines(output: object) -> dict[str, str] | None:
    values: dict[str, list[str]] = {}
    for line in str(output or "").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and re.fullmatch(r"EP_[A-Z0-9_]+", key):
            values.setdefault(key, []).append(value.strip())
    if any(len(value) != 1 for value in values.values()):
        return None
    return {key: value[0] for key, value in values.items()}


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    if value.casefold() == "true":
        return True
    if value.casefold() == "false":
        return False
    return None


def _callback_identity(row: Mapping[str, Any]) -> tuple[object, str, str] | None:
    native = row.get("callback") if isinstance(row.get("callback"), Mapping) else {}
    values = []
    for native_key, flat_key in (("display_id", "callback_display_id"), ("host", "callback_host"), ("domain", "callback_domain")):
        left, right = native.get(native_key), row.get(flat_key)
        if left is not None and right is not None and _norm(left) != _norm(right):
            return None
        values.append(left if left is not None else right)
    return values[0], _norm(values[1]), _norm(values[2])


def _endpoint_request(row: Mapping[str, Any], expected_operation_parameters: Mapping[str, Any],
                      expected_adapter: Mapping[str, Any] | None = None) -> tuple[set[str], set[str], bool] | None:
    """Match task command/parameters to the exact native adapter plan."""
    command = _norm(row.get("command_name")).replace("-", "_")
    try:
        from ai.langgraph import capabilities, mythic_capability_adapter
        step = capabilities.CapabilityExecutionStep(operation="endpoint-protection-adjustment",
            parameters=dict(expected_operation_parameters), capability="endpoint-protection-adjustment",
            purpose="independent evaluator request reconstruction", expected_probe="extract_endpoint_protection_probe")
        plan = capabilities.CapabilityExecutionPlan(True, steps=[step])
        commands = mythic_capability_adapter.build_mythic_capability_commands(plan, dict(expected_adapter or {}))
    except Exception:
        return None
    actual_params = _request_exact(row.get("original_params"))
    matches = [item for item in commands.commands if _norm(item.command).replace("-", "_") == command
               and _request_exact(item.parameters) == actual_params and item.expected_probe == step.expected_probe]
    if len(matches) != 1:
        return None
    actions = expected_operation_parameters.get("actions") or ["disable_realtime", "add_exclusion"]
    exclusions = expected_operation_parameters.get("exclusion_paths") or [r"C:\Windows\Temp"]
    if not isinstance(actions, list) or not isinstance(exclusions, list) or not all(isinstance(v, str) for v in actions + exclusions):
        return None
    remote = _norm(expected_operation_parameters.get("method") or "local") in {"remote-wmi", "wmi", "remote"}
    return {value.casefold() for value in actions}, {value.casefold() for value in exclusions}, remote


def endpoint_protection_observation(
    row: Mapping[str, Any], output: object, *, operation_id: int, callback_id: int,
    task_id: int, target_host: str, target_domain: str, baseline_task_ids: set[int],
    expected_operation_parameters: Mapping[str, Any] | None = None, response: Mapping[str, Any] | None = None,
    expected_response_id: int | str | None = None, expected_response_sha256: str = "",
    expected_adapter: Mapping[str, Any] | None = None,
) -> effect_registry.OracleObservation | None:
    """Bind a complete native endpoint task to a parsed postcondition, or return unmeasured."""
    if (not isinstance(row, Mapping) or not isinstance(expected_operation_parameters, Mapping)
            or not isinstance(response, Mapping) or expected_response_id is None or not _HEX64.fullmatch(_norm(expected_response_sha256))
            or type(operation_id) is not int or operation_id <= 0
            or type(callback_id) is not int or callback_id <= 0 or type(task_id) is not int or task_id <= 0
            or task_id in baseline_task_ids or any(type(item) is not int or item <= 0 for item in baseline_task_ids)):
        return None
    callback = _callback_identity(row)
    if (row.get("operation_id") != operation_id or row.get("display_id") != task_id or row.get("completed") is not True
            or callback is None or callback[0] != callback_id
            or (expected_operation_parameters.get("callback_id") is not None
                and str(expected_operation_parameters["callback_id"]) != str(callback_id))):
        return None
    responses = row.get("responses")
    if not isinstance(responses, list) or sum(1 for item in responses if isinstance(item, Mapping) and item.get("id") == expected_response_id) != 1:
        return None
    retained = next(item for item in responses if isinstance(item, Mapping) and item.get("id") == expected_response_id)
    raw_text = retained.get("response_text")
    if not isinstance(raw_text, str) or raw_text != response.get("response_text") or response.get("id") != expected_response_id:
        return None
    if hashlib.sha256(raw_text.encode()).hexdigest() != _norm(expected_response_sha256):
        return None
    text = _response_output(raw_text)
    if text != output:
        return None
    request = _endpoint_request(row, expected_operation_parameters, expected_adapter)
    remote = bool(request and request[2])
    if (not remote and (callback[1] != _norm(target_host) or callback[2] != _norm(target_domain))
            or remote and (_norm(expected_operation_parameters.get("target_host") or expected_operation_parameters.get("host")) != _norm(target_host)
                           or _norm(expected_operation_parameters.get("target_domain") or expected_operation_parameters.get("domain")) != _norm(target_domain))):
        return None
    fields = _field_lines(output)
    if request is None or fields is None or _norm(fields.get("EP_HOST")) != _norm(target_host):
        return None
    low = str(output or "").casefold()
    if any(marker in low for marker in ("ep_error=", "ep_set_error=", "ep_after_error=", "access denied", "tamper")):
        return None
    actions, exclusions, _ = request
    disabled = _bool(fields.get("EP_REALTIME_AFTER")) is False and "disable_realtime" in actions
    requested = _norm(fields.get("EP_REQUESTED_EXCLUSION"))
    excluded = (_bool(fields.get("EP_EXCLUSION_PRESENT")) is True and requested in exclusions)
    inactive = _norm(fields.get("EP_STATUS")) == "already_inactive"
    if not (disabled or excluded or inactive):
        return None
    contract = effect_registry.contract_for(_ENDPOINT)
    digest = hashlib.sha256((json.dumps(dict(row), sort_keys=True, default=str) + "\n" + str(output)).encode()).hexdigest()
    return effect_registry.OracleObservation(
        registry_version=effect_registry.REGISTRY_VERSION, oracle_id=contract.oracle_id,
        origin=effect_registry.ProofOrigin.MYTHIC_TASK_OUTPUT, proof_kind="endpoint_protection_state",
        outcome=True, observation_id=f"operation-{operation_id}:task-{task_id}:sha256:{digest}",
        target=f"{_norm(target_host)}@{_norm(target_domain)}", envelope_id=f"task-{task_id}",
    )


def _rbcd_request(row: Mapping[str, Any], target: str, subject: str) -> bool:
    if _norm(row.get("command_name")).replace("-", "_") != "execute_assembly":
        return False
    params = _params(row)
    if params is None or _norm(params.get("assembly_name") or params.get("assembly")) not in {"standin.exe", "standin"}:
        return False
    tokens = _text(params.get("assembly_arguments")).split()
    if not tokens or tokens[0].casefold() != "--rbcd":
        return False
    flags: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index].casefold()
        if token in {"--rbcd", "--target", "--computer"}:
            name = token[2:]
            value = ""
            if name != "rbcd" and index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                value = tokens[index + 1].casefold()
                index += 1
            flags[name] = value
        index += 1
    return "rbcd" in flags and flags.get("target") == _norm(target) and flags.get("computer") == _norm(subject)


def rbcd_rights_observation(
    row: Mapping[str, Any], edge_rows: object, *, operation_id: int, callback_id: int, task_id: int,
    target: str, subject: str, collection_receipt: object, expected_engagement_id: str,
    expected_ingest_job_id: str, expected_source_artifact_id: str, expected_source_artifact_sha256: str,
    observed_at: str, max_age_seconds: float, expected_collection_callback_id: int | str | None = None,
    expected_collection_task_id: int | str | None = None,
) -> effect_registry.OracleObservation | None:
    """Admit observed exact RBCD state, without attributing its creation to this write."""
    if (not isinstance(row, Mapping) or row.get("operation_id") != operation_id or row.get("display_id") != task_id
            or row.get("callback_display_id") != callback_id or row.get("completed") is not True
            or not _rbcd_request(row, target, subject) or not _HEX64.fullmatch(_norm(expected_source_artifact_sha256))):
        return None
    if not isinstance(edge_rows, list) or not edge_rows:
        return None
    canonical = []
    for item in edge_rows:
        if not isinstance(item, Mapping) or _norm(item.get("subject")) != _norm(subject) or _norm(item.get("target")) != _norm(target) or _norm(item.get("edge")) not in {"allowedtoact", "allowed_to_act", "rbcd"}:
            return None
        canonical.append({"subject": _norm(subject), "target": _norm(target), "edge": _norm(item.get("edge"))})
    verifier_input = {"operation_id": operation_id, "callback_id": callback_id, "task_id": task_id,
                      "target": _norm(target), "subject": _norm(subject), "query": "AllowedToAct"}
    verifier_result = {"status": "success", "rows": canonical}
    envelope = collection_receipt if isinstance(collection_receipt, proof_boundary.ProofEnvelope) else proof_boundary.ProofEnvelope.from_dict(collection_receipt)
    if envelope is None or envelope.persistence_state != proof_boundary.ADMITTED or envelope.origin != proof_boundary.ORIGIN_BLOODHOUND_INGEST:
        return None
    if (expected_collection_callback_id is None or expected_collection_task_id is None
            or envelope.verifier_id != _RBCD_VERIFIER_ID or envelope.verifier_version != _RBCD_VERIFIER_VERSION
            or envelope.ingest_job_id != expected_ingest_job_id or envelope.source_artifact_id != expected_source_artifact_id
            or envelope.callback_id != str(expected_collection_callback_id) or envelope.task_id != str(expected_collection_task_id)
            or _norm(envelope.source_artifact_sha256) != _norm(expected_source_artifact_sha256)):
        return None
    try:
        collection_task_id = int(expected_collection_task_id)
    except (TypeError, ValueError):
        return None
    if collection_task_id <= task_id:
        return None
    admitted = proof_boundary.admit_runtime_envelope(envelope, current_engagement_id=expected_engagement_id,
        expected_verifier_id=_RBCD_VERIFIER_ID,
        expected_verifier_input_sha256=proof_boundary.canonical_commitment_sha256(verifier_input),
        expected_verifier_result_sha256=proof_boundary.canonical_commitment_sha256(verifier_result))
    try:
        age = (datetime.fromisoformat(observed_at.replace("Z", "+00:00")) - datetime.fromisoformat(envelope.captured_at.replace("Z", "+00:00"))).total_seconds()
    except Exception:
        return None
    if not admitted.admitted or age < 0 or age > max_age_seconds:
        return None
    contract = effect_registry.contract_for(_RBCD)
    return effect_registry.OracleObservation(registry_version=effect_registry.REGISTRY_VERSION, oracle_id=contract.oracle_id,
        origin=effect_registry.ProofOrigin.PAYLOAD_BLOODHOUND_FACT, proof_kind="rbcd_rights", outcome=True,
        observation_id=f"{envelope.hash}:{task_id}", target=_norm(target), subject=_norm(subject), envelope_id=envelope.hash)
