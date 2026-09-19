"""Pure, request-bound parsers for credential-related evaluator effects.

The functions in this module consume retained Mythic task rows.  They do not
read Mythic or the credential store and never put credential values in an
``OracleObservation``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from .effect_registry import OracleObservation, ProofOrigin, REGISTRY_VERSION, contract_for
    from ..langgraph import credential_artifacts
except ImportError:  # direct evaluator/test import
    from ai.hillclimb.effect_registry import OracleObservation, ProofOrigin, REGISTRY_VERSION, contract_for
    from ai.langgraph import credential_artifacts


_FAILURE_TOKENS = (
    "access is denied", "access denied", "bad password", "invalid credential",
    "logon failure", "network path was not found", "no_result", "kdc_err",
    "failed", "exception", "error:", "[-]",
)
_PLACEHOLDERS = (
    "<redacted>", "replace_me", "placeholder", "changeme", "not-a-secret",
    "password123", "xxxxxxxx", "deadbeef", "todo",
)


def _normalize(value: object) -> str:
    return str(value or "").strip().strip(".").casefold()


def _short_host(value: object) -> str:
    text = _normalize(value).strip("\\/")
    if text.endswith("$"):
        text = text[:-1]
    return text.split(".", 1)[0]


def _target_binding(host: object, domain: object) -> tuple[str, str, str] | None:
    domain_text = _normalize(domain)
    raw_host = _normalize(host).strip("\\/")
    short = _short_host(raw_host)
    if not short or not domain_text:
        return None
    if "." in raw_host and raw_host != f"{short}.{domain_text}":
        return None
    return short, domain_text, f"{short}@{domain_text}"


def _subject_binding(subject: object, host: str, domain: str) -> str:
    text = _normalize(subject)
    if not text:
        return ""
    if text.count("\\") == 1:
        realm, account = text.split("\\", 1)
    elif text.count("@") == 1:
        account, realm = text.split("@", 1)
    else:
        account, realm = text, host
    if not account:
        return ""
    allowed_realms = {host, f"{host}.{domain}", domain}
    return f"{account}@{realm}" if realm in allowed_realms else ""


def _request_params(row: Mapping[str, Any]) -> Mapping[str, Any] | str | None:
    value = row.get("original_params")
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, (Mapping, list)) else None
    return text


def _value(params: Mapping[str, Any], *names: str) -> Any:
    wanted = {name.casefold() for name in names}
    return next((value for key, value in params.items() if str(key).casefold() in wanted), None)


def _argument_text(params: Mapping[str, Any] | str | None, *names: str) -> str:
    if isinstance(params, str):
        return params.strip()
    if isinstance(params, Mapping):
        value = _value(params, *names)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return "\n".join(value)
    return ""


def _display_id(row: Mapping[str, Any]) -> int:
    value = row.get("display_id")
    return value if type(value) is int and value > 0 else 0


def _callback(row: Mapping[str, Any]) -> tuple[int, str, str]:
    nested = row.get("callback") if isinstance(row.get("callback"), Mapping) else {}
    value = row.get("callback_display_id")
    callback_id = value if type(value) is int else nested.get("display_id")
    callback_id = callback_id if type(callback_id) is int and callback_id > 0 else 0
    host = _normalize(row.get("callback_host") or nested.get("host"))
    domain = _normalize(row.get("callback_domain") or nested.get("domain"))
    return callback_id, host, domain


def _eligible_rows(
    rows: Sequence[Mapping[str, Any]], *, operation_id: int, callback_id: int,
    baseline_task_ids: set[int],
) -> list[Mapping[str, Any]] | None:
    if (
        type(operation_id) is not int or operation_id <= 0
        or type(callback_id) is not int or callback_id <= 0
        or not isinstance(rows, (list, tuple)) or not isinstance(baseline_task_ids, set)
        or any(type(value) is not int or value <= 0 for value in baseline_task_ids)
        or any(not isinstance(row, Mapping) for row in rows)
    ):
        return None
    ids = [_display_id(row) for row in rows]
    if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
        return None
    baseline_max = max(baseline_task_ids, default=0)
    eligible = [
        row for row in rows
        if row.get("operation_id") == operation_id
        and row.get("completed") is True
        and _callback(row)[0] == callback_id
        and _display_id(row) not in baseline_task_ids
        and _display_id(row) > baseline_max
    ]
    return sorted(eligible, key=_display_id)


def _has_failure(row: Mapping[str, Any]) -> bool:
    status = _normalize(row.get("status"))
    if status in {"error", "failed", "cancelled", "canceled", "killed"}:
        return True
    output = str(row.get("output") or "").casefold()
    return not output.strip() or any(token in output for token in _FAILURE_TOKENS)


def _flag(arguments: str, name: str) -> str:
    match = re.search(
        rf"(?i)(?:^|\s){re.escape(name)}(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
        arguments,
    )
    return next((part for part in match.groups() if part is not None), "").strip() if match else ""


def _required_domain_dn(text: str, domain: str) -> bool:
    observed = re.findall(r"(?i)(?:^|[,/])\s*dc\s*=\s*([a-z0-9_-]+)", text)
    return [_normalize(value) for value in observed] == domain.split(".")


def _render_bound_managed_secret_script(
    target_host: str, target_domain: str, producer_binding: Mapping[str, Any] | None,
) -> str:
    """Rebuild the PowerShell producer from evaluator-owned, non-code inputs."""
    if not isinstance(producer_binding, Mapping):
        return ""
    domain_controller = producer_binding.get("domain_controller")
    search_base = producer_binding.get("search_base")
    attributes = producer_binding.get("attributes")
    if (
        not isinstance(domain_controller, str) or not domain_controller.strip()
        or not isinstance(search_base, str) or not _required_domain_dn(search_base, _normalize(target_domain))
        or not isinstance(attributes, (list, tuple)) or not attributes
        or any(not isinstance(value, str) or not value.strip() for value in attributes)
    ):
        return ""
    normalized_attributes = {_normalize(value) for value in attributes}
    if not {"ms-mcs-admpwd", "mslaps-password"} <= normalized_attributes:
        return ""
    if not {"dnshostname", "distinguishedname"} & normalized_attributes:
        return ""
    try:
        from ..langgraph.mythic_capability_adapter import _managed_secret_powershell
    except ImportError:
        from ai.langgraph.mythic_capability_adapter import _managed_secret_powershell
    return _managed_secret_powershell(
        domain_controller, search_base, target_host, target_domain, list(attributes),
    )


def recognized_managed_secret_request(
    row: Mapping[str, Any], target_host: str, target_domain: str, *,
    producer_binding: Mapping[str, Any] | None = None,
) -> bool:
    """Recognize only a target-specific current DirectorySearcher/SharpView request."""
    bound = _target_binding(target_host, target_domain)
    params = _request_params(row)
    if bound is None or params is None:
        return False
    host, domain, _ = bound
    fqdn = f"{host}.{domain}"
    command = _normalize(row.get("command_name")).replace("-", "_")
    arguments = _argument_text(params, "assembly_arguments", "arguments")
    if command in {"execute_assembly", "inline_assembly", "invoke_assembly"} and isinstance(params, Mapping):
        assembly = str(_value(params, "assembly_name", "assembly") or "").replace("\\", "/")
        if assembly.rsplit("/", 1)[-1].casefold() not in {"sharpview", "sharpview.exe"}:
            return False
        properties = _flag(arguments, "-Properties").casefold().split(",")
        return bool(
            re.match(r"(?i)^Get-DomainComputer(?:\s|$)", arguments)
            and _normalize(_flag(arguments, "-Identity")) == fqdn
            and _normalize(_flag(arguments, "-Domain")) == domain
            and _required_domain_dn(_flag(arguments, "-SearchBase"), domain)
            and {"ms-mcs-admpwd", "mslaps-password"} & set(properties)
            and _flag(arguments, "-FindOne") == ""
            and re.search(r"(?i)(?:^|\s)-FindOne(?:\s|$)", arguments)
        )
    if command != "powerpick":
        return False
    script = _argument_text(params, "script", "command", "arguments")
    expected = _render_bound_managed_secret_script(host, domain, producer_binding)
    return bool(expected and script == expected)


def _attribute_values(output: str, attr: str) -> list[Any]:
    values: list[Any] = []
    rx = re.compile(rf"(?im)^\s*{re.escape(attr)}\s*[:=]\s*(.*?)\s*$", re.IGNORECASE)
    values.extend(match.group(1).strip().strip("'\"") for match in rx.finditer(output))
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        parsed = None

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).casefold() == attr.casefold():
                    values.extend(item if isinstance(item, list) else [item])
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(parsed)
    return values


def _plaintext_laps_value(value: Any, *, windows_laps: bool) -> str:
    text = str(value or "").strip().strip("'\"")
    if windows_laps and text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return ""
        text = str(parsed.get("p") or "").strip() if isinstance(parsed, Mapping) else ""
    low = text.casefold()
    if len(text) < 4 or any(token in low for token in _PLACEHOLDERS):
        return ""
    if set(low) <= {"0", "x", "*"}:
        return ""
    return text


def _target_output_matches(output: str, host: str, domain: str) -> bool:
    fqdn = f"{host}.{domain}"
    identities: list[bool] = []
    for value in _attribute_values(output, "dNSHostName"):
        identities.append(_normalize(value) == fqdn)
    expected_dcs = domain.split(".")
    for value in _attribute_values(output, "distinguishedName"):
        parts = [_normalize(part).replace(" ", "") for part in str(value).split(",")]
        cn = next((part[3:] for part in parts if part.startswith("cn=")), "")
        dcs = [part[3:] for part in parts if part.startswith("dc=")]
        identities.append(cn == host and dcs == expected_dcs)
    return bool(identities and all(identities))


def _laps_material(
    row: Mapping[str, Any], host: str, domain: str,
    producer_binding: Mapping[str, Any] | None = None,
) -> tuple[str, str] | None:
    if not recognized_managed_secret_request(
        row, host, domain, producer_binding=producer_binding,
    ) or _has_failure(row):
        return None
    output = str(row.get("output") or "")
    if not _target_output_matches(output, host, domain):
        return None
    for attr, windows_laps in (("ms-Mcs-AdmPwd", False), ("msLAPS-Password", True)):
        for value in _attribute_values(output, attr):
            secret = _plaintext_laps_value(value, windows_laps=windows_laps)
            if secret:
                return attr.casefold(), secret
    return None


def _envelope(rows: Sequence[Mapping[str, Any]], *labels: str) -> str:
    safe_rows = [{
        "task": _display_id(row),
        "command": _normalize(row.get("command_name")),
        "output_sha256": hashlib.sha256(str(row.get("output") or "").encode()).hexdigest(),
    } for row in rows]
    payload = json.dumps({"labels": labels, "rows": safe_rows}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _observation(milestone: str, rows: Sequence[Mapping[str, Any]], *, target: str, subject: str,
                 kind: str, detail: str, operation_id: int, callback_id: int) -> OracleObservation:
    ids = ",".join(str(_display_id(row)) for row in rows)
    contract = contract_for(milestone)
    return OracleObservation(
        registry_version=REGISTRY_VERSION,
        oracle_id=contract.oracle_id,
        origin=ProofOrigin.MYTHIC_TASK_OUTPUT,
        proof_kind=kind,
        outcome=True,
        observation_id=f"{kind}:operation-{operation_id}:callback-{callback_id}:tasks-{ids}:{detail}",
        target=target,
        subject=subject,
        envelope_id=_envelope(rows, milestone, target, subject, detail),
    )


def managed_local_admin_secret_candidate(
    rows: Sequence[Mapping[str, Any]], *, operation_id: int, callback_id: int,
    target_host: str, target_domain: str, subject: str = "Administrator",
    baseline_task_ids: set[int], producer_binding: Mapping[str, Any] | None = None,
) -> OracleObservation | None:
    """Return a fresh exact-target plaintext managed-secret observation."""
    bound = _target_binding(target_host, target_domain)
    if bound is None:
        return None
    host, domain, target = bound
    bound_subject = _subject_binding(subject, host, domain)
    eligible = _eligible_rows(rows, operation_id=operation_id, callback_id=callback_id,
                              baseline_task_ids=baseline_task_ids)
    if not bound_subject or eligible is None:
        return None
    for row in reversed(eligible):
        material = _laps_material(row, host, domain, producer_binding)
        if material:
            attribute, _secret = material
            return _observation("MANAGED_LOCAL_ADMIN_SECRET", [row], target=target, subject=bound_subject,
                                kind="managed_local_admin_secret", detail=f"attribute-{attribute}",
                                operation_id=operation_id, callback_id=callback_id)
    return None


def _unc_path(value: object, host: str, domain: str) -> bool:
    path = str(value or "").strip().strip("'\"").replace("/", "\\")
    match = re.fullmatch(r"\\\\([^\\]+)\\c\\?\$", path, re.IGNORECASE)
    return bool(match and _normalize(match.group(1)) in {host, f"{host}.{domain}"})


def recognized_local_admin_access_request(row: Mapping[str, Any], target_host: str, target_domain: str) -> bool:
    bound = _target_binding(target_host, target_domain)
    params = _request_params(row)
    if bound is None or params is None:
        return False
    host, domain, _ = bound
    command = _normalize(row.get("command_name")).replace("-", "_")
    if command == "ls" and isinstance(params, Mapping):
        return _unc_path(_value(params, "path"), host, domain)
    if command == "shell" and isinstance(params, str):
        match = re.fullmatch(r"(?i)dir\s+(.+)", params.strip())
        return bool(match and _unc_path(match.group(1), host, domain))
    return False


def _make_token_lineage(row: Mapping[str, Any], host: str, domain: str, subject: str,
                        credential_id: object, recovered_secret: str) -> bool:
    if _normalize(row.get("command_name")).replace("-", "_") != "make_token":
        return False
    params = _request_params(row)
    if not isinstance(params, Mapping) or _value(params, "netOnly", "net_only") is not True:
        return False
    credential = _value(params, "credential")
    source: Mapping[str, Any]
    if isinstance(credential, Mapping):
        source = credential
    else:
        source = params
    account = _normalize(_value(source, "account", "username", "user"))
    realm = _normalize(_value(source, "realm", "domain"))
    kind = _normalize(_value(source, "type", "credential_type"))
    if account.count("\\") == 1:
        realm, account = account.split("\\", 1)
    expected_account = subject.split("@", 1)[0]
    if account != expected_account or realm not in {host, f"{host}.{domain}", domain} or (kind and kind != "plaintext"):
        return False
    source_id = _value(source, "id", "credential_id")
    if str(credential_id or "").strip():
        return str(source_id or "").strip() == str(credential_id).strip()
    supplied = str(_value(source, "credential", "password") or "")
    return bool(recovered_secret and supplied == recovered_secret)


def _access_output(row: Mapping[str, Any], host: str, domain: str) -> bool:
    if not recognized_local_admin_access_request(row, host, domain) or _has_failure(row):
        return False
    output = str(row.get("output") or "")
    normalized = output.replace("/", "\\").casefold()
    share_hosts = {_normalize(item) for item in re.findall(r"\\\\([^\\\s]+)\\c\$", normalized)}
    classic = bool(share_hosts and share_hosts <= {host, f"{host}.{domain}"} and re.search(
        r"(?i)directory of \\\\|volume in drive \\\\|\"success\"\s*:\s*true|lastwritetime|<dir>", output,
    ))
    try:
        parsed = json.loads(output)
    except ValueError:
        parsed = None
    native = False
    if isinstance(parsed, Mapping) and parsed.get("success") is True:
        output_host = _normalize(parsed.get("host"))
        share = _normalize(parsed.get("name") or parsed.get("directory"))
        files = parsed.get("files")
        named_files = files if isinstance(files, list) else []
        file_share = {
            _normalize(item.get("directory") or item.get("name"))
            for item in named_files if isinstance(item, Mapping)
        }
        native = output_host in {host, f"{host}.{domain}"} and (share == "c$" or "c$" in file_share)
    return bool(classic or native)


def managed_local_admin_access_candidate(
    rows: Sequence[Mapping[str, Any]], *, operation_id: int, callback_id: int,
    target_host: str, target_domain: str, subject: str = "Administrator",
    baseline_task_ids: set[int], credential_id: object = "", recovered_secret: str = "",
    task_window: int = 12,
    managed_secret_producer_binding: Mapping[str, Any] | None = None,
) -> OracleObservation | None:
    """Return a fresh make-token then exact-target service-access observation."""
    bound = _target_binding(target_host, target_domain)
    if bound is None:
        return None
    host, domain, target = bound
    bound_subject = _subject_binding(subject, host, domain)
    eligible = _eligible_rows(rows, operation_id=operation_id, callback_id=callback_id,
                              baseline_task_ids=baseline_task_ids)
    if not bound_subject or eligible is None:
        return None
    recovered = recovered_secret
    if not credential_id and not recovered:
        for row in eligible:
            material = _laps_material(row, host, domain, managed_secret_producer_binding)
            if material:
                recovered = material[1]
    for access in reversed(eligible):
        access_id = _display_id(access)
        if not _access_output(access, host, domain):
            continue
        related = [row for row in eligible if access_id - max(0, int(task_window)) <= _display_id(row) <= access_id]
        for token in reversed(related):
            if _display_id(token) >= access_id:
                continue
            if not _make_token_lineage(token, host, domain, bound_subject, credential_id, recovered):
                continue
            window = [row for row in related if _display_id(token) <= _display_id(row) <= access_id]
            if any(_has_failure(row) for row in window):
                break
            lineage = "credential-id" if str(credential_id or "").strip() else "recovered-value"
            return _observation("MANAGED_LOCAL_ADMIN_ACCESS", [token, access], target=target,
                                subject=bound_subject, kind="managed_local_admin_access",
                                detail=f"lineage-{lineage}", operation_id=operation_id,
                                callback_id=callback_id)
    return None


def recognized_lsass_request(row: Mapping[str, Any], target_host: str, target_domain: str = "") -> bool:
    params = _request_params(row)
    if params is None:
        return False
    command = _normalize(row.get("command_name")).replace("-", "_")
    arguments = _argument_text(params, "commands", "arguments")
    if command == "mimikatz":
        return bool(re.search(r"(?i)(?:^|\s)sekurlsa::logonpasswords(?:\s|$)", arguments.replace("\n", " ")))
    if command == "nanodump":
        explicit = _normalize(_flag(arguments, "--computer") or _flag(arguments, "--host"))
        targets = {_short_host(target_host)}
        if target_domain:
            targets.add(f"{_short_host(target_host)}.{_normalize(target_domain)}")
        return (not explicit or explicit in targets) and bool(
            re.search(r"(?i)(?:^|\s)--(?:write|inmem)(?:\s|$)", arguments)
        )
    return False


def _account_for_domain(subject: object, domain: str) -> tuple[str, str] | None:
    value = _normalize(subject)
    if value.count("\\") == 1:
        qualifier, account = value.split("\\", 1)
        if qualifier != domain.split(".", 1)[0]:
            return None
    elif value.count("@") == 1:
        account, qualifier = value.split("@", 1)
        if qualifier != domain:
            return None
    else:
        return None
    if not account or account == "krbtgt":
        return None
    return account, f"{account}@{domain}"


def _request_account_matches(value: object, account: str, domain: str, *, allow_unqualified: bool) -> bool:
    identity = _normalize(value)
    if identity.count("\\") == 1:
        qualifier, observed_account = identity.split("\\", 1)
        return qualifier == domain.split(".", 1)[0] and observed_account == account
    if identity.count("@") == 1:
        observed_account, qualifier = identity.split("@", 1)
        return qualifier == domain and observed_account == account
    return allow_unqualified and identity == account


def _slash_value(arguments: str, name: str) -> str:
    match = re.search(rf"(?i)(?:^|\s)/{re.escape(name)}\s*:\s*([^\s\"']+)", arguments)
    return match.group(1).strip() if match else ""


def recognized_account_dcsync_request(row: Mapping[str, Any], target_domain: str, subject: str) -> bool:
    """Recognize an exact non-krbtgt native, Mimikatz, or SharpKatz DCSync request."""
    domain = _normalize(target_domain)
    bound = _account_for_domain(subject, domain)
    params = _request_params(row)
    if not domain or bound is None or params is None:
        return False
    account, _ = bound
    command = _normalize(row.get("command_name")).replace("-", "_")
    if command == "dcsync" and isinstance(params, Mapping):
        return (
            _normalize(_value(params, "domain", "realm")) == domain
            and _request_account_matches(_value(params, "user", "account"), account, domain,
                                         allow_unqualified=True)
        )
    arguments = _argument_text(params, "commands", "assembly_arguments", "arguments").strip().strip("\"")
    if command == "mimikatz":
        return bool(
            re.match(r"(?i)^lsadump::dcsync(?:\s|$)", arguments)
            and _normalize(_slash_value(arguments, "domain")) == domain
            and _request_account_matches(_slash_value(arguments, "user"), account, domain,
                                         allow_unqualified=False)
        )
    if command in {"execute_assembly", "inline_assembly", "invoke_assembly"} and isinstance(params, Mapping):
        assembly = str(_value(params, "assembly_name", "assembly") or "").replace("\\", "/")
        return bool(
            assembly.rsplit("/", 1)[-1].casefold() in {"sharpkatz", "sharpkatz.exe"}
            and _normalize(_flag(arguments, "--Command")) == "dcsync"
            and _normalize(_flag(arguments, "--Domain")) == domain
            and _request_account_matches(_flag(arguments, "--User"), account, domain,
                                         allow_unqualified=False)
        )
    return False


def _output_dcsync_accounts(output: str) -> set[str]:
    accounts = {
        _normalize(value)
        for value in re.findall(r"(?im)^\s*Object\s+RDN\s*:\s*([^\s]+)", output)
    }
    for value in re.findall(r"(?im)^([^\s:]+):\d+:[0-9a-f]{32}:[0-9a-f]{32}:::", output):
        identity = _normalize(value)
        accounts.add(identity.rsplit("\\", 1)[-1].split("@", 1)[0])
    return accounts


def account_credential_material_candidate(
    rows: Sequence[Mapping[str, Any]], *, operation_id: int, callback_id: int,
    target_domain: str, subject: str, baseline_task_ids: set[int],
) -> OracleObservation | None:
    """Return usable labeled material from an exact specific-account DCSync."""
    domain = _normalize(target_domain)
    bound = _account_for_domain(subject, domain)
    if bound is None:
        return None
    account, canonical_subject = bound
    eligible = _eligible_rows(rows, operation_id=operation_id, callback_id=callback_id,
                              baseline_task_ids=baseline_task_ids)
    if eligible is None:
        return None
    for row in reversed(eligible):
        if not recognized_account_dcsync_request(row, domain, canonical_subject) or _has_failure(row):
            continue
        output = str(row.get("output") or "")
        output_accounts = _output_dcsync_accounts(output)
        if output_accounts and output_accounts != {account}:
            continue
        materials = credential_artifacts.extract_credential_material(output)
        kinds = sorted({str(item.get("secret_type") or "") for item in materials
                        if item.get("secret_type") in {"ntlm", "aes128", "aes256", "rc4"}})
        if not kinds:
            continue
        return _observation("ACCOUNT_CREDENTIAL_MATERIAL", [row], target=domain,
                            subject=canonical_subject, kind="account_credential_material",
                            detail="material-" + "+".join(kinds), operation_id=operation_id,
                            callback_id=callback_id)
    return None


def host_credential_material_candidate(
    rows: Sequence[Mapping[str, Any]], *, operation_id: int, callback_id: int,
    target_host: str, target_domain: str, subject: str = "",
    baseline_task_ids: set[int],
) -> OracleObservation | None:
    """Return usable labeled NTLM/AES material from an exact callback-host dump."""
    bound = _target_binding(target_host, target_domain)
    if bound is None:
        return None
    host, domain, target = bound
    bound_subject = _subject_binding(subject, host, domain) if subject else target
    eligible = _eligible_rows(rows, operation_id=operation_id, callback_id=callback_id,
                              baseline_task_ids=baseline_task_ids)
    if eligible is None:
        return None
    for row in reversed(eligible):
        _cbid, callback_host, callback_domain = _callback(row)
        if (
            _short_host(callback_host) != host
            or callback_host not in {host, f"{host}.{domain}"}
            or callback_domain != domain
            or not recognized_lsass_request(row, host, domain)
            or _has_failure(row)
        ):
            continue
        materials = credential_artifacts.extract_credential_material(str(row.get("output") or ""))
        kinds = sorted({str(item.get("secret_type") or "") for item in materials
                        if item.get("secret_type") in {"ntlm", "aes128", "aes256"}})
        if not kinds:
            continue
        return _observation("HOST_CREDENTIAL_MATERIAL", [row], target=target, subject=bound_subject,
                            kind="host_credential_material", detail="material-" + "+".join(kinds),
                            operation_id=operation_id, callback_id=callback_id)
    return None


# Semantic aliases used by capability/effect coverage integration.
read_managed_local_admin_secret_candidate = managed_local_admin_secret_candidate
use_managed_local_admin_secret_candidate = managed_local_admin_access_candidate
lsass_dump_candidate = host_credential_material_candidate
