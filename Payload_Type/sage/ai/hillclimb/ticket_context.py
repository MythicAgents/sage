"""Pure retained-Mythic evidence parser for the cross-domain ticket context."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from ai.hillclimb.effect_registry import OracleObservation, ProofOrigin, REGISTRY_VERSION, contract_for
from ai.langgraph.credential_artifacts import extract_ticket_probe


def _request_params(row: Mapping[str, Any]) -> Mapping[str, Any] | str | None:
    value = row.get("original_params")
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return text


def _value(params: Mapping[str, Any], *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for key, value in params.items():
        if str(key).casefold() in wanted and isinstance(value, str):
            return value.strip()
    return ""


def _domain_flag(arguments: str) -> str:
    values = re.findall(r"(?i)(?:^|\s)/domain:([^\s]+)", arguments)
    return values[0].strip().casefold() if len(values) == 1 else ""


def _unc_resource(value: str, target: str) -> str:
    text = value.strip().strip('"')
    match = re.fullmatch(r"\\\\[^\\/]+\\c\$", text, re.IGNORECASE)
    if not match:
        return ""
    host = text[2:].split("\\", 1)[0].casefold()
    wanted = target.casefold()
    return text.casefold() if host == wanted or ("." in host and host.split(".", 1)[1] == wanted) else ""


def recognized_forge_request(row: Mapping[str, Any], target: str) -> bool:
    """Accept only the retained request shapes emitted by existing ticket builders."""
    command = str(row.get("command_name") or "").strip().casefold().replace("-", "_")
    params = _request_params(row)
    if not isinstance(params, Mapping):
        return False
    if command == "execute_assembly":
        assembly = _value(params, "assembly_name", "assembly", "Assembly")
        arguments = _value(params, "assembly_arguments")
        basename = assembly.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].casefold()
        if basename not in {"rubeus", "rubeus.exe"}:
            return False
        return bool(re.match(r"(?i)^golden(?:\s|$)", arguments) and _domain_flag(arguments) == target.casefold())
    if command == "mimikatz":
        arguments = _value(params, "commands")
        arguments = arguments.strip().strip('"')
        return bool(re.match(r"(?i)^kerberos::golden(?:\s|$)", arguments)
                    and _domain_flag(arguments) == target.casefold())
    return False


def recognized_access_request(row: Mapping[str, Any], target: str) -> bool:
    """Bind a successful listing to an exact existing shell or native-ls request."""
    command = str(row.get("command_name") or "").strip().casefold()
    params = _request_params(row)
    resource = ""
    if command == "shell" and isinstance(params, str):
        match = re.fullmatch(r"(?i)dir\s+(\\\\[^\s]+)", params.strip())
        resource = _unc_resource(match.group(1), target) if match else ""
    elif command == "ls" and isinstance(params, Mapping):
        resource = _unc_resource(_value(params, "path"), target)
    return bool(resource and resource in str(row.get("output") or "").casefold())


def _display_id(row: Mapping[str, Any]) -> int:
    value = row.get("display_id")
    return value if type(value) is int and value > 0 else 0


def _callback_id(row: Mapping[str, Any]) -> int | None:
    value = row.get("callback_display_id")
    if type(value) is int:
        return value
    callback = row.get("callback")
    value = callback.get("display_id") if isinstance(callback, Mapping) else None
    return value if type(value) is int else None


def _active(row: Mapping[str, Any]) -> bool:
    callback = row.get("callback")
    return bool(row.get("callback_active") is True or (isinstance(callback, Mapping) and callback.get("active") is True))


def cross_domain_context_candidate(
    rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...], *, operation_id: int,
    callback_id: int, target: str, baseline_task_ids: set[int], task_window: int = 12,
) -> OracleObservation | None:
    """Return one forged-ticket then parent-admin-share proof, or no candidate."""
    if type(operation_id) is not int or operation_id <= 0 or type(callback_id) is not int or callback_id <= 0:
        return None
    if (not isinstance(rows, (list, tuple)) or not isinstance(baseline_task_ids, set)
            or not isinstance(target, str) or not target.strip()
            or any(type(value) is not int or value <= 0 for value in baseline_task_ids)):
        return None
    ids = [_display_id(row) for row in rows if isinstance(row, Mapping)]
    if not ids or any(value == 0 for value in ids) or len(ids) != len(set(ids)):
        return None
    baseline_max = max(baseline_task_ids) if baseline_task_ids else 0
    eligible = [row for row in rows if isinstance(row, Mapping)
                and row.get("operation_id") == operation_id and row.get("completed") is True
                and _callback_id(row) == callback_id
                and _display_id(row) not in baseline_task_ids and _display_id(row) > baseline_max]
    if not any(_active(row) for row in eligible):
        return None
    eligible.sort(key=_display_id)
    window = max(0, int(task_window))
    for anchor in eligible:
        anchor_id = _display_id(anchor)
        access = extract_ticket_probe(str(anchor.get("output") or ""), expected_domain=target)
        if access.get("service_access_proven") is not True:
            continue
        if not recognized_access_request(anchor, target):
            continue
        related = [row for row in eligible if anchor_id - window <= _display_id(row) <= anchor_id]
        for forge in reversed(related):
            forge_id = _display_id(forge)
            if forge_id >= anchor_id:
                continue
            probe = extract_ticket_probe(str(forge.get("output") or ""), expected_domain=target)
            if probe.get("ticket_forged") is not True:
                continue
            if not recognized_forge_request(forge, target):
                continue
            proof_rows = [row for row in related if forge_id <= _display_id(row) <= anchor_id]
            # The most recent forge defines the existing observation window.
            # Include intermediate failures; an import attempt is not usable
            # context, and literal /ptt is not required by every working lane.
            combined = extract_ticket_probe(
                "\n".join(str(row.get("output") or "") for row in proof_rows), expected_domain=target,
            )
            if (combined.get("ticket_error") is not False
                    or combined.get("ticket_valid") is not True
                    or combined.get("service_access_proven") is not True
                    or not (combined.get("ticket_forged") or combined.get("tgt_present"))):
                break
            envelope = hashlib.sha256(json.dumps([
                {"task_id": _display_id(row), "output": str(row.get("output") or "")}
                for row in proof_rows
            ], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            task_ids = ",".join(str(_display_id(row)) for row in proof_rows)
            contract = contract_for("CROSS_DOMAIN_CONTEXT")
            return OracleObservation(
                registry_version=REGISTRY_VERSION, oracle_id=contract.oracle_id,
                origin=ProofOrigin.MYTHIC_TASK_OUTPUT, proof_kind="cross_domain_context", outcome=True,
                observation_id=f"operation-{operation_id}:callback-{callback_id}:tasks-{task_ids}",
                target=target, envelope_id=f"sha256:{envelope}",
            )
    return None
