"""Reconcile completed Mythic task history into Sage's engagement ledger.

This module is intentionally pure: callers provide task metadata and decoded output text.
It classifies the task, runs the same verify-on-record extractors used by live Sage tasks,
and returns a ledger-ready record only when the task output proves a modeled effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    from . import (
        credential_artifacts, effect_observations, engagement_state, intent_classifier, proof_boundary, task_evidence,
    )
except ImportError:
    import credential_artifacts
    import effect_observations
    import engagement_state
    import intent_classifier
    import proof_boundary
    import task_evidence


@dataclass(frozen=True)
class ReconciledTask:
    technique: str
    target: str
    status: str
    evidence: dict[str, Any]
    credential_material: tuple[dict[str, str], ...] = field(default_factory=tuple, repr=False)


def reconcile_task(
    task: dict[str, Any],
    output: Any,
    now: str,
    *,
    engagement_id: str = "",
    transaction_id: str = "",
) -> ReconciledTask | None:
    """Return a verified ledger record for a completed Mythic task, or None.

    Reconciliation imports achieved effects only. Failed/manual experiments are useful task history,
    but importing them into the durable ledger creates model-visible drag; failed effects should still be
    inspected from Mythic task history when debugging.
    """
    classified = _classify(task, output)
    if classified is None:
        return None
    technique, target, command, callback = classified

    probe = _probe_for_technique(technique, output, callback=callback)
    if probe is None:
        return None
    verdict = engagement_state.verify_effect(technique, target, probe)
    if verdict != "achieved":
        return None

    task_id = task.get("display_id") if task.get("display_id") is not None else task.get("id")
    # The shared terminal-success rule: Mythic's task record decides, as for every live writer. Output presence
    # comes from the output this pass read.
    record = task_evidence.task_record_from_row(task, has_output=bool(_text(output).strip()))
    task_class = task_evidence.classify_task(record)
    terminal_status = task_evidence.envelope_terminal_status(task_class, record)
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
        "task_classification": task_class,
        "mythic_task_status": (record.status[: task_evidence.MAX_STATUS_CHARS] if record is not None else ""),
    }
    if engagement_id:
        transaction_id = _text(transaction_id) or f"operator-task:{task_id}"
        evidence["transaction_id"] = transaction_id
        evidence["terminal_task_status"] = terminal_status
        envelope = proof_boundary.make_runtime_task_envelope(
            engagement_id=engagement_id,
            callback_id=evidence["callback_id"],
            task_id=task_id,
            terminal_status=terminal_status,
            command=command,
            verifier_id=f"task_reconciler:{technique}",
            captured_at=now,
            transaction_id=transaction_id,
            verifier_input={
                "technique": technique,
                "target": target,
                "probe": probe,
            },
            verifier_result={
                "verdict": verdict,
                "technique": technique,
                "target": target,
            },
            metadata={"reconciled": True, "task_classification": task_class},
        )
        evidence["proof_envelope"] = envelope.to_dict()
    material = _credential_material_for_record(technique, target, output)
    return ReconciledTask(
        technique=technique,
        target=target,
        status="achieved",
        evidence=evidence,
        credential_material=tuple(material),
    )


def _probe_for_technique(technique: str, output: Any, *, callback: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The structured probe a modeled technique's own task output supplies, or None when that output cannot prove it.

    A directory write (the DCSync-rights grant, RBCD) proves nothing by its own success message: it becomes a
    ``directory_change`` candidate (:func:`directory_observations`) that a later read of the object must prove.
    Graph collection is proven only by the completed BloodHound ingest of its artifact, which records it itself.
    """
    if technique in credential_artifacts.CREDENTIAL_TECHNIQUES:
        return dict(credential_artifacts.extract_credential_probe(output))
    if technique in credential_artifacts.TICKET_TECHNIQUES:
        return dict(credential_artifacts.extract_ticket_probe(output))
    if technique == "domain-admin-membership-check":
        return _extract_domain_admin_membership_probe(output, callback=callback)
    if technique == "gpo-abuse":
        return _gpo_system_exec_probe(output)
    return None


def _gpo_system_exec_probe(output: Any) -> dict[str, Any]:
    """The raw writer's GPO rule: only observed SYSTEM execution achieves; a GPO modification alone stays partial."""
    try:
        from . import capabilities
    except ImportError:  # pragma: no cover
        import capabilities  # type: ignore
    probe = capabilities.extract_gpo_system_exec_probe(output)
    # verify_effect's achieved_any for gpo-abuse is these two execution signals, exactly as the capability verifier
    # (verify_gpo_controlled_system_exec) achieves: a GPO modification or scheduled-task artifact stays partial.
    return {
        "system_command_succeeded": bool(probe.get("system_command_succeeded")),
        "system_callback_observed": bool(probe.get("system_callback_observed")),
        "gpo_modified": bool(probe.get("gpo_modified")),
        "scheduled_task_present": bool(probe.get("scheduled_task_present")),
    }


_GRANT_IDENTITY_RE = None


def _granted_principal(output: Any, params: Any) -> str:
    """The principal a StandIn grant names: its returned identity ("... to object for NETBIOS\\name"), else the
    ``--grant``/``--ntaccount`` parameter."""
    import re

    global _GRANT_IDENTITY_RE
    if _GRANT_IDENTITY_RE is None:
        _GRANT_IDENTITY_RE = re.compile(r"(?im)\bfor\s+(?P<principal>[^\s\\]+\\[^\s]+)\s*$")
    match = _GRANT_IDENTITY_RE.search(_text(output))
    if match:
        return match.group("principal")
    return intent_classifier.flag_value(params, "grant") or intent_classifier.flag_value(params, "ntaccount")


def directory_observations(task: dict[str, Any], output: Any, now: str) -> list:
    """Directory observations a terminal task supplies: an awaiting ``directory_change`` join for a successful
    directory write, and a ``directory_read`` observation when its output is a supported read of one object."""
    out = []
    record = task_evidence.task_record_from_row(task, has_output=bool(_text(output).strip()))
    terminal_status = task_evidence.envelope_terminal_status(task_evidence.classify_task(record), record)
    read = effect_observations.directory_read_observation(task, output, terminal_status, now)
    if read is not None:
        out.append(read)
    classified = _classify(task, output)
    if classified is None:
        return out
    technique, target, _command, _callback = classified
    params = task.get("original_params") or task.get("display_params") or task.get("parameters") or ""
    if technique in credential_artifacts.GRANT_TECHNIQUES:
        granted_object = intent_classifier.flag_value(params, "object")
        # Replication rights are an extended right on the domain naming-context head. A grant whose object is an OU
        # or a leaf is not a domain-DCSync grant, so it opens no ds-replication-rights join.
        if credential_artifacts.grant_write_reported(output) and effect_observations.is_domain_head_dn(granted_object):
            join = effect_observations.directory_change_join(
                task, prop=effect_observations.PROPERTY_REPLICATION, technique=technique, target=target,
                obj=granted_object, principal=_granted_principal(output, params), observed_at=now,
            )
            if join is not None:
                out.append(join)
    elif technique == "rbcd-standin":
        text = _text(output)
        if text.strip() and "[!]" not in text and "access is denied" not in text.casefold():
            join = effect_observations.directory_change_join(
                task, prop=effect_observations.PROPERTY_RBCD, technique=technique, target=target, obj=target,
                principal_sid=intent_classifier.flag_value(params, "sid"), observed_at=now,
            )
            if join is not None:
                out.append(join)
    return out


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


def _classify(task: Any, output: Any) -> tuple[str, str, str, dict] | None:
    """(technique, target, command, callback) for a completed task whose intent is a modeled technique, else None."""
    if not isinstance(task, dict) or not _is_terminal(task):
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
    return technique, target, command, callback


def task_intent(task: dict[str, Any]) -> tuple[str, str] | None:
    """(technique, target) a terminal task's command and parameters name, else None. Parameters only: a wrapper
    command whose real command line appears only in its output is not classified here."""
    classified = _classify(task, "")
    return (classified[0], classified[1]) if classified is not None else None


def review_candidate(task: dict[str, Any], output: Any) -> tuple[str, str, dict] | None:
    """(technique, target, lineage evidence) for a credential task the verifier above did not credit, whose output
    the credential review may still read: Mythic classified it a completion-function error with output.

    The hash verifier is one tool's locator; it must not decide whether a completion-error task is reviewed. This
    carries no proof and no output text: only the task's lineage and classification, so the review can attach its own
    observation to the task's contribution.
    """
    classified = _classify(task, output)
    if classified is None:
        return None
    technique, target, command, callback = classified
    if technique not in credential_artifacts.CREDENTIAL_TECHNIQUES or not _text(output).strip():
        return None
    if task_evidence.canonical_effect(engagement_state._technique_effect(technique, target)) is None:
        return None  # a host-scoped dump (lsass) is no account's secret: nothing the review could prove
    record = task_evidence.task_record_from_row(task, has_output=True)
    task_class = task_evidence.classify_task(record)
    if task_class != task_evidence.POSTPROCESSING_ERROR_WITH_OUTPUT:
        return None
    task_id = task.get("display_id") if task.get("display_id") is not None else task.get("id")
    return technique, target, {
        "source": "task_history_reconcile",
        "provenance": "operator_task",
        "mythic_task_id": task_id,
        "callback_id": callback.get("display_id") or callback.get("id") or task.get("callback_display_id"),
        "command": command,
        "task_classification": task_class,
        "transaction_id": f"operator-task:{task_id}",
    }


def _is_terminal(task: dict[str, Any]) -> bool:
    if task.get("completed") is True:
        return True
    status = _text(task.get("status")).casefold()
    return status in {"success", "completed", "error", "failed"}


def _operator_name(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("username") or value.get("name"))
    return _text(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)
