"""Reconcile completed Mythic task history into Sage's engagement ledger.

This module is intentionally pure: callers provide task metadata and decoded output text.
It classifies the task, runs the same verify-on-record extractors used by live Sage tasks,
and returns a ledger-ready record only when the task output proves a modeled effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    from . import credential_artifacts, engagement_state, intent_classifier
except ImportError:
    import credential_artifacts
    import engagement_state
    import intent_classifier


@dataclass(frozen=True)
class ReconciledTask:
    technique: str
    target: str
    status: str
    evidence: dict[str, Any]
    credential_material: tuple[dict[str, str], ...] = field(default_factory=tuple, repr=False)


def reconcile_task(task: dict[str, Any], output: Any, now: str) -> ReconciledTask | None:
    """Return a verified ledger record for a completed Mythic task, or None.

    Reconciliation imports achieved effects only. Failed/manual experiments are useful task history,
    but importing them into the durable ledger creates model-visible drag; failed effects should still be
    inspected from Mythic task history when debugging.
    """
    if not isinstance(task, dict):
        return None
    if not _is_terminal(task):
        return None

    command = _text(task.get("command_name") or task.get("command"))
    params = task.get("original_params")
    if params in (None, ""):
        params = task.get("display_params") or task.get("parameters") or ""
    callback = task.get("callback") if isinstance(task.get("callback"), dict) else {}
    classified = intent_classifier.classify_tool_call(command, params, callback_host=callback.get("host"))
    if not classified:
        # Wrapper commands such as execute_pe/execute_assembly often hide the real command line in task
        # output ("New command line via PEB", "mimikatz(commandline)", etc.). Treat that as observation,
        # not as proof: the verifier below still requires concrete post-action artifacts before import.
        classified = intent_classifier.classify_tool_call(
            command,
            f"{_text(params)}\n{_text(output)}",
            callback_host=callback.get("host"),
        )
    if not classified:
        return None
    technique, target = classified
    target = _text(target).casefold()
    if not target and technique == "domain-admin-membership-check":
        target = _domain_admin_membership_target(callback)
    if not technique or not target:
        return None

    probe = _probe_for_technique(technique, output, callback=callback)
    if probe is None:
        return None
    verdict = engagement_state.verify_effect(technique, target, probe)
    if verdict != "achieved":
        return None

    task_id = task.get("display_id") if task.get("display_id") is not None else task.get("id")
    evidence = {
        "source": "task_history_reconcile",
        "provenance": "operator_task",
        "mythic_task_id": task_id,
        "callback_id": callback.get("display_id") or callback.get("id") or task.get("callback_display_id"),
        "operator": _operator_name(task.get("operator")),
        "command": command,
        "verify_verdict": verdict,
        "verified_on_record": True,
        "artifact_present": True,
        "reconciled_at": now,
        "result_preview": _preview(output),
    }
    material = _credential_material_for_record(technique, target, output)
    return ReconciledTask(
        technique=technique,
        target=target,
        status="achieved",
        evidence=evidence,
        credential_material=tuple(material),
    )


def _probe_for_technique(technique: str, output: Any, *, callback: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if technique in credential_artifacts.CREDENTIAL_TECHNIQUES:
        return dict(credential_artifacts.extract_credential_probe(output))
    if technique in credential_artifacts.GRANT_TECHNIQUES:
        return dict(credential_artifacts.extract_grant_probe(output))
    if technique in credential_artifacts.TICKET_TECHNIQUES:
        return dict(credential_artifacts.extract_ticket_probe(output))
    if technique == "domain-admin-membership-check":
        return _extract_domain_admin_membership_probe(output, callback=callback)
    return None


def _extract_domain_admin_membership_probe(output: Any, *, callback: dict[str, Any] | None = None) -> dict[str, Any]:
    text = _text(output)
    low = text.casefold()
    callback = callback if isinstance(callback, dict) else {}
    candidates = _identity_candidates(callback.get("user") or callback.get("identity"))
    principal_present = any(_contains_identity_token(low, candidate) for candidate in candidates)
    denied = any(marker in low for marker in (
        "access is denied",
        "system error 5",
        "could not be found",
        "not recognized",
    ))
    group_query_succeeded = bool(
        not denied
        and "domain admins" in low
        and (
            "group name" in low
            or "alias name" in low
            or "members" in low
            or "the command completed successfully" in low
        )
    )
    return {
        "domain_admin": bool(group_query_succeeded and principal_present),
        "group_query_succeeded": group_query_succeeded,
        "member_of": ["Domain Admins"] if group_query_succeeded and principal_present else [],
        "principal_present": principal_present,
        "principal_candidates": candidates,
        "access_denied": denied,
    }


def _domain_admin_membership_target(callback: dict[str, Any]) -> str:
    for key in ("forest", "domain", "realm"):
        value = _text(callback.get(key)).casefold()
        if value and "." in value:
            return value
    identity = _text(callback.get("user") or callback.get("identity"))
    if "@" in identity:
        return identity.rsplit("@", 1)[1].casefold()
    return ""


def _identity_candidates(identity: Any) -> list[str]:
    raw = _text(identity)
    out: list[str] = []
    for candidate in (raw, raw.split("\\", 1)[-1] if "\\" in raw else "", raw.split("@", 1)[0] if "@" in raw else ""):
        normalized = candidate.strip()
        if normalized and normalized.casefold() not in {item.casefold() for item in out}:
            out.append(normalized)
    return out


def _contains_identity_token(haystack: str, needle: str) -> bool:
    candidate = _text(needle).casefold()
    if not candidate:
        return False
    text = _text(haystack).casefold()
    if "\\" in candidate:
        return candidate in text
    try:
        import re
        return re.search(rf"(?<![a-z0-9_.-]){re.escape(candidate)}(?![a-z0-9_.-])", text) is not None
    except Exception:
        return candidate in text


def _credential_material_for_record(technique: str, target: str, output: Any) -> list[dict[str, str]]:
    account = ""
    realm = ""
    if technique == "dcsync":
        account = "krbtgt"
        realm = target
    elif technique == "dcsync-user" and "@" in target:
        account, _, realm = target.partition("@")
    if not account or not realm:
        return []
    return credential_artifacts.extract_credential_material(output, account=account, realm=realm)


def _is_terminal(task: dict[str, Any]) -> bool:
    if task.get("completed") is True:
        return True
    status = _text(task.get("status")).casefold()
    return status in {"success", "completed", "error", "failed"}


def _operator_name(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("username") or value.get("name"))
    return _text(value)


def _preview(value: Any, limit: int = 240) -> str:
    text = credential_artifacts.redact_credential_material(_text(value))
    text = " ".join(text.split())
    return text[: limit - 1] + "..." if len(text) > limit else text


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)
