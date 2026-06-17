"""Deterministic failure labeling for Sage trajectory observations."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .schema import redact_text


@dataclass(frozen=True)
class FailureClassification:
    label: str
    labels: tuple[str, ...]
    evidence: tuple[str, ...]


_SIGNATURES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "ambiguous_account_name",
        re.compile(r"ERROR_NOT_UNIQUE|CrackNames\s+\(name status\):\s*0x0*3\b", re.I),
        "Directory account name was ambiguous; qualify the principal with the target domain NetBIOS name.",
    ),
    (
        "command_template_error",
        re.compile(
            r"specified wildcard character pattern is not valid|"
            r"cannot call a method on a null-valued expression",
            re.I,
        ),
        "A deterministic command template produced invalid runtime syntax or object access.",
    ),
    (
        "directory_bind_error",
        re.compile(
            r"retrieving member \"Put\".*(?:operations error|referral)|"
            r"A referral was returned from the server|"
            r"An operations error occurred",
            re.I | re.S,
        ),
        "A directory write bound to the wrong LDAP target or context; use a concrete writable DC bind.",
    ),
    (
        "unresolved_gpo_identity",
        re.compile(r"GPO (?:identity unresolved|not found)|could not resolve .*GPO|GPO not found in SYSVOL policy root", re.I),
        "The GPO display name was not resolved to the exact policy GUID; resolve the GPO object before retrying.",
    ),
    (
        "delayed_effect",
        re.compile(r"GPO setup pending|Group Policy to apply|artifact write|version bump|ScheduledTasks\.xml", re.I),
        "The action changed a delayed policy artifact but did not yet prove target-side execution.",
    ),
    (
        "dcsync_bad_dn_or_context",
        re.compile(r"GetNCChanges:\s*0x0*20f7\b|GetNCChanges:.*\b8439\b|DS_DRA_BAD_DN", re.I),
        "DCSync reached DRSUAPI but failed object/context resolution; rebuild the target and prove the required context.",
    ),
    (
        "access_denied",
        re.compile(r"DS_DRA_ACCESS_DENIED|8453|0x0*20f5|Access is denied|STATUS_ACCESS_DENIED", re.I),
        "The target denied the requested operation.",
    ),
    (
        "wrong_security_context",
        re.compile(
            r"wrong context|"
            r"no tickets?\s+(?:in|found|available|present|cached|cache)|"
            r"there (?:is|are)\s+no tickets?|"
            r"KRB_AP_ERR|KDC_ERR_BADOPTION|not in current logon session",
            re.I,
        ),
        "The command likely ran without the required token or Kerberos context.",
    ),
    (
        "command_size_limit",
        re.compile(r"command line is too long|argument list too long|command length|maximum command line", re.I),
        "The constructed command exceeded payload or OS command-length limits.",
    ),
    (
        "missing_artifact",
        re.compile(r"file not found|cannot find path|not registered|no such file|missing artifact", re.I),
        "The referenced binary, file, credential, or proof artifact was missing.",
    ),
    (
        "schema_or_argument_mismatch",
        re.compile(r"Supplied Arguments.*match any parameter group|invalid parameters|unknown key|required parameter|ChooseOne", re.I),
        "The tool command failed before execution because constructed parameters did not match the payload schema.",
    ),
    (
        "tool_not_registered",
        re.compile(r"assembly.*not registered|not been uploaded|register.*tool|No file.*registered", re.I),
        "The payload tool or assembly needs upload/registration before use.",
    ),
    (
        "edr_or_payload_killed",
        re.compile(r"Defender|AMSI|killed|blocked by policy|malware|quarantine|process terminated", re.I),
        "Endpoint protection or process death likely prevented execution.",
    ),
    (
        "transient_infra",
        re.compile(r"timed out|timeout|no output returned|callback.*dead|502|connection reset|temporary failure", re.I),
        "Infrastructure or callback timing prevented a reliable observation.",
    ),
    (
        "verifier_false_positive",
        re.compile(r"false[- ]?achieved|contradicted by|zero ACE|no proof|not SYSTEM execution proof", re.I),
        "The ledger or planner treated an effect as achieved without sufficient proof.",
    ),
    (
        "objective_or_state_drift",
        re.compile(r"opaque objective|wrong objective|state drift|stale ledger|old callback|dead callback", re.I),
        "Planner state or objective context drifted from the live engagement.",
    ),
    (
        "repeated_no_progress",
        re.compile(r"recursion|step limit|same blocker|no progress|repeated", re.I),
        "The agent repeated actions or reasoning without verifier progress.",
    ),
)


def classify_observation(value: Any) -> FailureClassification:
    text = redact_text(value)
    labels: list[str] = []
    evidence: list[str] = []
    for label, pattern, note in _SIGNATURES:
        match = pattern.search(text)
        if not match:
            continue
        labels.append(label)
        snippet = " ".join(text[max(match.start() - 80, 0) : match.end() + 120].split())
        evidence.append(f"{label}: {note} Evidence: {snippet}")
    if not labels:
        return FailureClassification("unclassified", ("unclassified",), ())
    return FailureClassification(labels[0], tuple(dict.fromkeys(labels)), tuple(evidence))


def repair_for_label(label: str) -> tuple[str, int, str] | None:
    repairs = {
        "ambiguous_account_name": (
            "qualify_principal_with_target_netbios",
            1,
            "Retry once with target-domain NETBIOS qualification, then require credential verifier proof.",
        ),
        "delayed_effect": (
            "bounded_poll_wait_for_verifier",
            1,
            "Wait/poll for the delayed effect and run proof checks until success or timeout.",
        ),
        "unresolved_gpo_identity": (
            "resolve_gpo_guid_then_retry",
            1,
            "Resolve the controlled GPO to its exact GUID via BloodHound, LDAP displayName/name, or SharpGPOAbuse output before retrying.",
        ),
        "dcsync_bad_dn_or_context": (
            "rebuild_dcsync_target_and_materialize_context",
            1,
            "Canonicalize the DCSync domain/DC/principal and prove or materialize the required Kerberos context before retrying once.",
        ),
        "access_denied": (
            "verify_privilege_or_security_context",
            0,
            "Do not blind-retry; inspect replication rights, token, or Kerberos context.",
        ),
        "wrong_security_context": (
            "establish_required_execution_context",
            1,
            "Materialize or prove the required token/Kerberos context before retrying the capability.",
        ),
        "command_size_limit": (
            "stage_or_shorten_command",
            1,
            "Retry with a shorter deterministic command, staged script/file, or payload-supported upload/execute path.",
        ),
        "command_template_error": (
            "fix_deterministic_builder_template",
            0,
            "Fix the deterministic command template or adapter escaping; do not retry the same generated command.",
        ),
        "directory_bind_error": (
            "bind_to_writable_domain_controller",
            1,
            "Resolve and bind LDAP writes to a concrete writable domain controller before retrying once.",
        ),
        "schema_or_argument_mismatch": (
            "rebuild_with_payload_schema",
            1,
            "Rebuild command parameters from the live payload schema instead of hand-editing raw args.",
        ),
        "missing_artifact": (
            "materialize_or_stage_missing_artifact",
            1,
            "Resolve, stage, or register the missing artifact before retrying.",
        ),
        "tool_not_registered": (
            "register_tool_then_retry",
            1,
            "Upload/register the tool through the supported artifact path, then retry once.",
        ),
        "edr_or_payload_killed": (
            "switch_execution_method_or_surface_blocker",
            0,
            "Treat as an environment blocker unless a lower-risk execution method is available.",
        ),
        "transient_infra": (
            "refresh_liveness_and_retry_once",
            1,
            "Refresh callback/liveness state and retry once if the callback is healthy.",
        ),
        "verifier_false_positive": (
            "invalidate_effect_and_require_probe",
            0,
            "Do not trust the prior ledger row; require a concrete verifier probe before proceeding.",
        ),
        "objective_or_state_drift": (
            "reconcile_objective_and_live_state",
            0,
            "Refresh objective, callback, graph, and ledger state before choosing another action.",
        ),
        "repeated_no_progress": (
            "stop_replanning_and_surface_blocker",
            0,
            "Stop repeating the same action; hand back the blocker or switch capability family.",
        ),
    }
    return repairs.get(label)
