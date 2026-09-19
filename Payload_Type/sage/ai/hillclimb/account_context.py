"""Independent parsing of retained, account-bound Kerberos context evidence."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from . import effect_registry
from .ticket_context import _active, _callback_id, _display_id, _request_params
from ai.langgraph.credential_artifacts import extract_ticket_probe


def _principal(value: object) -> tuple[str, str]:
    return effect_registry.parse_identity(value)


def _value(params: Mapping, *names: str):
    folded = {str(key).casefold(): value for key, value in params.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return None


def _output_remote_principal(output: str) -> tuple[str, str] | None:
    match = re.search(
        r"(?i)successfully impersonated .+? for local access and (.+?) for remote access(?:\.|\r?$)",
        output,
        re.MULTILINE,
    )
    return _principal(match.group(1)) if match else None


def _logon_matches(row: Mapping, account: str, domain: str) -> bool:
    if str(row.get("command_name") or "").casefold() != "make_token":
        return False
    params = _request_params(row)
    if not isinstance(params, Mapping) or _value(params, "netOnly", "netonly") is not True:
        return False
    credential = _value(params, "credential")
    if isinstance(credential, Mapping):
        principal = (str(_value(credential, "account") or "").casefold(),
                     str(_value(credential, "realm") or "").casefold())
    else:
        principal = _principal(_value(params, "username", "user"))
    if principal != (account, domain):
        return False
    output = str(row.get("output") or "")
    explicit_remote = _output_remote_principal(output)
    if explicit_remote is not None:
        return explicit_remote == (account, domain)
    return bool(re.search(
        r"(?i)successfully set primary identity.+for local access.+impersonation identity.+for remote access",
        output,
    ))


def _import_matches(row: Mapping) -> bool:
    if str(row.get("command_name") or "").casefold() not in {"ticket_store_add", "ticket_cache_add"}:
        return False
    params = _request_params(row)
    if not isinstance(params, Mapping):
        return False
    keys = {str(key).casefold() for key in params}
    if "base64ticket" not in keys or not keys <= {"base64ticket", "luid"}:
        return False
    ticket = _value(params, "base64ticket")
    if not isinstance(ticket, str) or not ticket.strip():
        return False
    output = str(row.get("output") or "")
    return bool(re.search(r"(?i)(?:added ticket|ticket successfully imported)", output)) and not re.search(
        r"(?i)(?:ticket_store_add failed|ticket_cache_add failed|failed to add ticket)", output,
    )


def _inventory_matches(row: Mapping, account: str, domain: str) -> bool:
    params = _request_params(row)
    command = str(row.get("command_name") or "").casefold()
    if command in {"ticket_store_list", "ticket_cache_list"} and isinstance(params, Mapping):
        keys = {str(key).casefold() for key in params}
        if "luid" not in keys or not keys <= {"luid", "getsystemtickets"} or _value(params, "getSystemTickets") not in {None, False}:
            return False
    if not ((command in {"ticket_store_list", "ticket_cache_list"} and isinstance(params, Mapping))
            or (command == "shell" and isinstance(params, str) and params in {"klist", "klist.exe"})):
        return False
    # Match parsed Client/Server fields in one cache entry. A username substring
    # elsewhere in the response cannot turn another principal's TGT into proof.
    output = str(row.get("output") or "")
    blocks = re.split(r"(?im)(?=^\s*(?:#\d+>\s*)?Client(?:\s+Name)?\s*:)", output)
    for block in blocks:
        client = re.findall(r"(?im)^\s*(?:#\d+>\s*)?Client(?:\s+Name)?\s*:\s*([^\r\n]+)", block)
        server = re.findall(r"(?im)^\s*Server(?:\s+Name)?\s*:\s*([^\r\n]+)", block)
        if len(client) != 1 or len(server) != 1:
            continue
        who = re.fullmatch(r"([^\s@]+)\s*@\s*([^\s]+)", client[0].strip())
        service = re.fullmatch(r"krbtgt/([^\s@]+)(?:\s*@\s*([^\s]+))?", server[0].strip(), re.I)
        if (who and service and (who[1].casefold(), who[2].casefold()) == (account, domain)
                and service[1].casefold() == domain and (not service[2] or service[2].casefold() == domain)):
            return True
    return False


def _service_matches(row: Mapping, domain: str) -> bool:
    params = _request_params(row)
    command = row.get("command_name")
    path = ""
    if command == "shell" and isinstance(params, str):
        match = re.fullmatch(r"(?i)dir\s+(\\\\[^\s]+)", params.strip())
        path = match[1] if match else ""
    elif command == "ls" and isinstance(params, Mapping):
        path = str(params.get("path") or "")
    match = re.fullmatch(r"\\\\([^\\/]+)\\(c\$|admin\$|sysvol|netlogon)", path, re.I)
    if not match:
        return False
    host = match[1].casefold()
    if host != domain and ("." not in host or host.split(".", 1)[1] != domain):
        return False
    output = str(row.get("output") or "")
    if path.casefold() not in output.casefold():
        return False
    # SYSVOL/NETLOGON are intentional account-authentication resources; unlike
    # the separate forged-admin-context oracle this claim is not administrator access.
    parsed = extract_ticket_probe(output)
    return parsed.get("service_access_proven") is True and parsed.get("ticket_error") is False


def _session_binding(row: Mapping) -> tuple[bool, str]:
    """Return one explicit session id, with empty meaning the current session.

    The current builder leaves native ticket-store LUID values empty after
    ``make_token`` changes the callback's token.  A non-empty explicit target is
    usable only when every explicit producer agrees; conflicting output/request
    metadata is unavailable rather than evidence about the default context.
    """
    values: list[str] = []
    params = _request_params(row)
    if isinstance(params, Mapping):
        for name in ("luid", "targetLuid", "logonId", "session_id", "target_context"):
            value = _value(params, name)
            text = str(value or "").strip().casefold()
            if text and text not in {"current", "default"} and not text.startswith("{{"):
                values.append(text)
    output = str(row.get("output") or "")
    for pattern in (
        r"(?im)^\s*Current LUID\s*:\s*(0x[0-9a-f]+)\s*$",
        r"(?im)^\s*(?:Target LUID|LogonID|Logon ID)\s*:\s*(0x[0-9a-f]+)\s*$",
    ):
        values.extend(match.casefold() for match in re.findall(pattern, output))
    return len(set(values)) <= 1, (values[0] if values else "")


def _default_current_requests(import_row: Mapping, inventory: Mapping, access: Mapping) -> bool:
    for row in (import_row, inventory, access):
        params = _request_params(row)
        if isinstance(params, Mapping):
            for name in ("luid", "targetLuid", "logonId", "session_id", "target_context"):
                value = str(_value(params, name) or "").strip().casefold()
                if value and value not in {"current", "default"} and not value.startswith("{{"):
                    return False
    return True


def account_kerberos_context_observation(rows, *, operation_id: int, callback_id: int,
                                         account: str, domain: str, baseline_task_ids: set[int]):
    """Observe the existing isolated make-token/import/cache/service route.

    Missing retained context creation is unavailable; it cannot be inferred from
    a service listing or a runtime account-context effect token.
    """
    if (type(operation_id) is not int or operation_id <= 0 or type(callback_id) is not int or callback_id <= 0
            or not isinstance(rows, list) or not isinstance(baseline_task_ids, set)
            or any(type(value) is not int or value <= 0 for value in baseline_task_ids)):
        return None
    account, domain = str(account).strip().casefold(), effect_registry.normalize_binding(domain)
    if not account or not domain or any(not isinstance(row, Mapping) or not _display_id(row) for row in rows):
        return None
    ids = [_display_id(row) for row in rows]
    if len(ids) != len(set(ids)):
        return None
    fresh = sorted((row for row in rows if row.get("operation_id") == operation_id
                    and row.get("completed") is True and _callback_id(row) == callback_id
                    and _display_id(row) > max(baseline_task_ids, default=0)), key=_display_id)
    if not any(_active(row) for row in fresh):
        return None
    for end, access in enumerate(fresh):
        if not _service_matches(access, domain):
            continue
        contexts = [i for i, row in enumerate(fresh[:end]) if row.get("command_name") == "make_token"]
        if not contexts:
            continue
        start = contexts[-1]
        if not _logon_matches(fresh[start], account, domain):
            continue
        window = fresh[start:end + 1]
        combined = extract_ticket_probe("\n".join(str(row.get("output") or "") for row in window))
        if combined.get("ticket_error") is not False:
            continue
        imports = [i for i, row in enumerate(window[1:-1], 1) if _import_matches(row)]
        lineage = None
        for imported in imports:
            for inventory in window[imported + 1:-1]:
                if not _inventory_matches(inventory, account, domain):
                    continue
                selected = (window[0], window[imported], inventory, access)
                sessions = [_session_binding(row) for row in selected]
                if any(not valid for valid, _value in sessions):
                    continue
                explicit = {value for _valid, value in sessions if value}
                if len(explicit) > 1 or (explicit and not _default_current_requests(*selected[1:])):
                    continue
                lineage = selected
                break
            if lineage is not None:
                break
        if lineage is None:
            continue
        digest = hashlib.sha256(json.dumps(window, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        contract = effect_registry.contract_for("ACCOUNT_KERBEROS_CONTEXT")
        return effect_registry.OracleObservation(
            registry_version=effect_registry.REGISTRY_VERSION, oracle_id=contract.oracle_id,
            origin=effect_registry.ProofOrigin.MYTHIC_TASK_OUTPUT, proof_kind="account_kerberos_context",
            outcome=True, observation_id=f"operation-{operation_id}:callback-{callback_id}:sha256:{digest}",
            target=domain, subject=f"{account}@{domain}", envelope_id=f"sha256:{digest}",
        )
    return None
