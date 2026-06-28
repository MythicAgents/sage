"""Deterministic providers for generic capability operations.

The capability layer chooses *what* must happen. A payload command is only one
possible provider for that operation. This module keeps the provider catalog
small and mechanical: native payload commands first, then declared external
tools or OS primitives that preserve the same operation contract.

The first slice covers Kerberos ticket mechanics, DRSUAPI DCSync, one
current-session LDAP read, and read-only structured artifact inspection because
those are the live cross-payload gaps between Apollo and Merlin. Unknown
operations still fall through to the bounded model-assisted mechanic repair
layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class OperationProviderSpec:
    operation: str
    name: str
    kind: str
    contexts: tuple[str, ...]
    priority: int
    command_key: str
    default_command: str
    rationale: str
    context_semantics: str
    tool_key: str = ""
    default_tool: str = ""
    executable_key: str = ""
    default_executable: str = ""
    arguments_key: str = ""
    default_arguments: str = ""
    alternate_live_commands: tuple[str, ...] = ()
    setup_command_key: str = ""
    default_setup_command: str = ""
    alternate_live_setup_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedOperationProvider:
    operation: str
    name: str
    kind: str
    command: str
    context: str
    context_semantics: str
    rationale: str
    tool: str = ""
    executable: str = ""
    arguments: str = ""
    setup_command: str = ""


_PROVIDER_SPECS: dict[str, tuple[OperationProviderSpec, ...]] = {
    "structured-artifact-read": (
        OperationProviderSpec(
            operation="structured-artifact-read",
            name="windows-more-structured-artifact-read",
            kind="os",
            contexts=("current", "current-agent-cache"),
            priority=10,
            command_key="structured_artifact_read_command",
            default_command="shell",
            executable_key="structured_artifact_read_executable",
            default_executable="more.com",
            rationale="Windows more can read a structured artifact without invoking a shell process",
            context_semantics="current-logon-session",
            alternate_live_commands=("run", "shell"),
        ),
    ),
    "drsuapi-dcsync": (
        OperationProviderSpec(
            operation="drsuapi-dcsync",
            name="native-drsuapi-dcsync",
            kind="native",
            contexts=("active-auth-context",),
            priority=10,
            command_key="drsuapi_command",
            default_command="dcsync",
            rationale="payload exposes a native DRSUAPI DCSync command",
            context_semantics="active-logon-session",
        ),
        OperationProviderSpec(
            operation="drsuapi-dcsync",
            name="managed-sharpkatz-dcsync",
            kind="external-tool",
            contexts=("active-auth-context",),
            priority=20,
            command_key="drsuapi_inprocess_invoke_command",
            default_command="",
            tool_key="dcsync_tool",
            default_tool="SharpKatz.exe",
            rationale="managed DRSUAPI tool can replicate secrets in-process under the active logon session",
            context_semantics="active-logon-session",
            alternate_live_commands=(
                "invoke_assembly",
                "invoke-assembly",
            ),
            setup_command_key="drsuapi_inprocess_load_command",
            default_setup_command="",
            alternate_live_setup_commands=("load_assembly", "load-assembly"),
        ),
    ),
    "kerberos-ticket-import": (
        OperationProviderSpec(
            operation="kerberos-ticket-import",
            name="native-current-ticket-cache-import",
            kind="native",
            contexts=("current-agent-cache",),
            priority=10,
            command_key="current_ticket_import_command",
            default_command="ticket_cache_add",
            rationale="payload exposes a native current-cache ticket import command",
            context_semantics="current-logon-session",
        ),
        OperationProviderSpec(
            operation="kerberos-ticket-import",
            name="managed-rubeus-ptt",
            kind="external-tool",
            contexts=("current-agent-cache",),
            priority=20,
            command_key="inprocess_dotnet_invoke_command",
            default_command="invoke-assembly",
            tool_key="kerberos_tool",
            default_tool="Rubeus.exe",
            rationale="managed Kerberos tool can inject a ticket into the current logon session",
            context_semantics="current-logon-session",
            alternate_live_commands=(
                "invoke_assembly",
                "invoke-assembly",
            ),
            setup_command_key="inprocess_dotnet_load_command",
            default_setup_command="load-assembly",
            alternate_live_setup_commands=("load_assembly", "load-assembly"),
        ),
        OperationProviderSpec(
            operation="kerberos-ticket-import",
            name="native-ticket-store-import",
            kind="native",
            contexts=("isolated",),
            priority=10,
            command_key="ticket_import_command",
            default_command="ticket_store_add",
            rationale="payload exposes a native isolated ticket-store import command",
            context_semantics="isolated-ticket-store",
        ),
    ),
    "kerberos-ticket-list": (
        OperationProviderSpec(
            operation="kerberos-ticket-list",
            name="native-current-ticket-cache-list",
            kind="native",
            contexts=("current-agent-cache",),
            priority=10,
            command_key="current_ticket_cache_list_command",
            default_command="ticket_cache_list",
            rationale="payload exposes a native current-cache ticket inventory command",
            context_semantics="current-logon-session",
        ),
        OperationProviderSpec(
            operation="kerberos-ticket-list",
            name="windows-klist-list",
            kind="os",
            contexts=("current-agent-cache", "current"),
            priority=20,
            command_key="current_ticket_list_command",
            default_command="shell",
            executable_key="current_ticket_list_executable",
            default_executable="klist.exe",
            arguments_key="current_ticket_list_arguments",
            default_arguments="",
            rationale="Windows klist can inventory the current logon session",
            context_semantics="current-logon-session",
            alternate_live_commands=("run", "shell"),
        ),
        OperationProviderSpec(
            operation="kerberos-ticket-list",
            name="managed-rubeus-klist",
            kind="external-tool",
            contexts=("current-agent-cache",),
            priority=30,
            command_key="inprocess_dotnet_invoke_command",
            default_command="invoke-assembly",
            tool_key="kerberos_tool",
            default_tool="Rubeus.exe",
            arguments_key="kerberos_ticket_list_arguments",
            default_arguments="klist",
            rationale="managed Kerberos tool can inventory the current logon session in-process",
            context_semantics="current-logon-session",
            alternate_live_commands=(
                "invoke_assembly",
                "invoke-assembly",
            ),
            setup_command_key="inprocess_dotnet_load_command",
            default_setup_command="load-assembly",
            alternate_live_setup_commands=("load_assembly", "load-assembly"),
        ),
        OperationProviderSpec(
            operation="kerberos-ticket-list",
            name="native-ticket-store-list",
            kind="native",
            contexts=("isolated",),
            priority=10,
            command_key="ticket_list_command",
            default_command="ticket_store_list",
            rationale="payload exposes a native isolated ticket-store inventory command",
            context_semantics="isolated-ticket-store",
        ),
    ),
    "kerberos-ticket-purge": (
        OperationProviderSpec(
            operation="kerberos-ticket-purge",
            name="native-current-ticket-cache-purge",
            kind="native",
            contexts=("current-agent-cache",),
            priority=10,
            command_key="current_ticket_cache_purge_command",
            default_command="ticket_cache_purge",
            rationale="payload exposes a native current-cache ticket purge command",
            context_semantics="current-logon-session",
        ),
        OperationProviderSpec(
            operation="kerberos-ticket-purge",
            name="windows-klist-purge",
            kind="os",
            contexts=("current-agent-cache", "current"),
            priority=20,
            command_key="current_ticket_purge_command",
            default_command="shell",
            executable_key="current_ticket_purge_executable",
            default_executable="klist.exe",
            arguments_key="current_ticket_purge_arguments",
            default_arguments="purge",
            rationale="Windows klist can purge the current logon session",
            context_semantics="current-logon-session",
            alternate_live_commands=("run", "shell"),
        ),
        OperationProviderSpec(
            operation="kerberos-ticket-purge",
            name="native-ticket-store-purge",
            kind="native",
            contexts=("isolated",),
            priority=10,
            command_key="ticket_purge_command",
            default_command="ticket_store_purge",
            rationale="payload exposes a native isolated ticket-store purge command",
            context_semantics="isolated-ticket-store",
        ),
    ),
    "kerberos-service-ticket-acquire": (
        OperationProviderSpec(
            operation="kerberos-service-ticket-acquire",
            name="windows-klist-get",
            kind="os",
            contexts=("current-agent-cache", "current"),
            priority=10,
            command_key="current_service_ticket_command",
            default_command="shell",
            executable_key="current_service_ticket_executable",
            default_executable="klist.exe",
            rationale="Windows klist can request a fresh service ticket in the current logon session",
            context_semantics="current-logon-session",
            alternate_live_commands=("run", "shell"),
        ),
    ),
    "ldap-managed-local-admin-secret-read": (
        OperationProviderSpec(
            operation="ldap-managed-local-admin-secret-read",
            name="managed-sharpview-computer-attribute-read",
            kind="external-tool",
            contexts=("current-agent-cache",),
            priority=10,
            command_key="managed_secret_inprocess_invoke_command",
            default_command="",
            tool_key="managed_secret_read_tool",
            default_tool="SharpView.exe",
            rationale="managed directory query tool can read selected computer attributes in-process",
            context_semantics="current-logon-session",
            alternate_live_commands=(
                "invoke_assembly",
                "invoke-assembly",
            ),
            setup_command_key="managed_secret_inprocess_load_command",
            default_setup_command="",
            alternate_live_setup_commands=("load_assembly", "load-assembly"),
        ),
    ),
}


def select_operation_provider(
    operation: Any,
    *,
    config: dict[str, Any] | None = None,
    context: str = "",
    live_commands: Iterable[Any] | None = None,
    exclude_commands: Iterable[Any] = (),
) -> ResolvedOperationProvider | None:
    """Return the first deterministic provider that satisfies one operation.

    `live_commands=None` means adapter-build time: configured/default commands
    are trusted and issue-time live authentication still validates them later.
    Passing `live_commands` means issue-time recovery: only providers backed by
    a command actually present on the callback may be selected.
    """
    operation_name = _normalize(operation)
    provider_specs = list(_PROVIDER_SPECS.get(operation_name, ()))
    if not provider_specs:
        return None
    config_data = dict(config) if isinstance(config, dict) else {}
    disabled = {_normalize(item) for item in _string_list(config_data.get("disabled_operation_providers"))}
    preferred = _provider_order(config_data, operation_name)
    if preferred:
        priority = {name: index for index, name in enumerate(preferred)}
        provider_specs.sort(key=lambda item: (priority.get(item.name, len(priority)), item.priority))
    else:
        provider_specs.sort(key=lambda item: item.priority)

    live_map = _live_command_map(live_commands)
    excluded = {_normalize(item) for item in exclude_commands if _normalize(item)}
    context_name = _normalize(context)
    for spec in provider_specs:
        if spec.name in disabled or context_name not in _provider_contexts(spec, config_data):
            continue
        command = _configured_value(config_data, spec.command_key, spec.default_command)
        if not command:
            continue
        command = _select_live_command(spec, command, live_map)
        if not command or _normalize(command) in excluded:
            continue
        setup_command = _configured_value(config_data, spec.setup_command_key, spec.default_setup_command)
        if setup_command:
            setup_command = _select_live_setup_command(spec, setup_command, live_map)
            if not setup_command:
                continue
        tool = _configured_value(config_data, spec.tool_key, spec.default_tool)
        executable = _configured_value(config_data, spec.executable_key, spec.default_executable)
        arguments = _configured_value(config_data, spec.arguments_key, spec.default_arguments)
        if spec.kind == "external-tool" and not tool:
            continue
        if spec.kind == "os" and not executable:
            continue
        return ResolvedOperationProvider(
            operation=spec.operation,
            name=spec.name,
            kind=spec.kind,
            command=command,
            context=context_name,
            context_semantics=spec.context_semantics,
            rationale=spec.rationale,
            tool=tool,
            executable=executable,
            arguments=arguments,
            setup_command=setup_command,
        )
    return None


def live_provider_candidate(
    command_obj: dict[str, Any],
    *,
    payload_type: Any = "",
    command_surface: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build one deterministic replacement candidate for a missing native binding.

    This is deliberately narrower than model-assisted mechanic repair. It only
    handles catalogued providers and keeps all original runtime placeholders.
    """
    if not isinstance(command_obj, dict):
        return None
    operation = _normalize(command_obj.get("operation"))
    context = _context_from_command_obj(command_obj)
    provider = select_operation_provider(
        operation,
        config={"payload_type": _normalize(payload_type)},
        context=context,
        live_commands=command_surface,
        exclude_commands=(command_obj.get("command"),),
    )
    if provider is None:
        return None
    if provider.setup_command:
        return {
            "command": "",
            "parameters": {},
            "provider": provider.name,
            "provider_kind": provider.kind,
            "provider_context": provider.context_semantics,
            "blocked": True,
            "reason": (
                f"provider '{provider.name}' requires setup command '{provider.setup_command}' before "
                f"'{provider.command}'; rebuild the capability with the payload adapter instead of collapsing "
                "a multi-command provider into one live binding"
            ),
        }
    parameters = _provider_parameters(provider, command_obj)
    if parameters is None:
        return None
    return {
        "command": provider.command,
        "parameters": parameters,
        "provider": provider.name,
        "provider_kind": provider.kind,
        "provider_context": provider.context_semantics,
        "rationale": provider.rationale,
    }


def _provider_parameters(provider: ResolvedOperationProvider, command_obj: dict[str, Any]) -> dict[str, Any] | str | None:
    operation = provider.operation
    if provider.name == "windows-more-structured-artifact-read":
        path = _structured_artifact_path(command_obj.get("parameters"))
        if not path:
            return None
        if _normalize(provider.command) == "run":
            return {"executable": provider.executable, "arguments": path}
        return {"arguments": f"type {path}"}
    if provider.name == "managed-rubeus-ptt":
        ticket_value = _ticket_value(command_obj.get("parameters"))
        if not ticket_value:
            return None
        return {
            "assembly_name": provider.tool,
            "assembly_arguments": f"ptt /ticket:{ticket_value}",
        }
    if provider.name == "windows-klist-list":
        if _normalize(provider.command) == "run":
            return {"executable": provider.executable, "arguments": provider.arguments}
        return {"arguments": "klist"}
    if provider.name == "windows-klist-purge":
        if _normalize(provider.command) == "run":
            return {"executable": provider.executable, "arguments": provider.arguments}
        return {"arguments": "klist purge"}
    if provider.name == "windows-klist-get":
        service = _service_ticket_spn(command_obj.get("parameters"))
        if not service:
            return None
        if _normalize(provider.command) == "run":
            return {"executable": provider.executable, "arguments": f"get {service}"}
        return {"arguments": f"klist get {service}"}
    if provider.kind == "native":
        return command_obj.get("parameters", "")
    return None


def _context_from_command_obj(command_obj: dict[str, Any]) -> str:
    command = _normalize(command_obj.get("command"))
    if command.startswith("ticket-cache-"):
        return "current-agent-cache"
    if command.startswith("ticket-store-"):
        return "isolated"
    consumes = {_normalize(item) for item in list(command_obj.get("consumes") or [])}
    if "kerberos_logon_context" in consumes:
        return "isolated"
    return "current"


def _ticket_value(parameters: Any) -> str:
    if not isinstance(parameters, dict):
        return ""
    for key in ("base64ticket", "base64Ticket", "ticket", "ticket_base64", "credential"):
        value = parameters.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _service_ticket_spn(parameters: Any) -> str:
    if not isinstance(parameters, dict):
        return ""
    service = _text(parameters.get("service") or parameters.get("spn"))
    if service:
        return service
    resource = _text(
        parameters.get("resource")
        or parameters.get("proof_resource")
        or parameters.get("service_resource")
    ).strip().strip('"')
    if not resource:
        return ""
    normalized = resource.replace("\\", "/")
    if normalized.startswith("//"):
        host = normalized.lstrip("/").split("/", 1)[0]
        return f"cifs/{host}" if host else ""
    if "/" in normalized:
        return normalized
    return f"cifs/{normalized}"


def _structured_artifact_path(parameters: Any) -> str:
    if isinstance(parameters, dict):
        path = _text(parameters.get("path") or parameters.get("artifact_path"))
        if path:
            return path
        command_line = _text(
            parameters.get("arguments")
            or parameters.get("args")
            or parameters.get("command")
        )
    else:
        command_line = _text(parameters)
    if not command_line:
        return ""
    command_name, separator, remainder = command_line.partition(" ")
    if separator and _normalize(command_name) in {"type", "more", "more.com"}:
        return remainder.strip()
    return command_line


def _select_live_command(
    spec: OperationProviderSpec,
    configured_command: str,
    live_map: dict[str, str] | None,
) -> str:
    if live_map is None:
        return configured_command
    for candidate in spec.alternate_live_commands:
        normalized_candidate = _normalize(candidate)
        if normalized_candidate in live_map:
            return live_map[normalized_candidate]
    normalized = _normalize(configured_command)
    if normalized in live_map:
        return live_map[normalized]
    return ""


def _select_live_setup_command(
    spec: OperationProviderSpec,
    configured_command: str,
    live_map: dict[str, str] | None,
) -> str:
    if live_map is None:
        return configured_command
    for candidate in spec.alternate_live_setup_commands:
        normalized_candidate = _normalize(candidate)
        if normalized_candidate in live_map:
            return live_map[normalized_candidate]
    normalized = _normalize(configured_command)
    if normalized in live_map:
        return live_map[normalized]
    return ""


def _provider_order(config: dict[str, Any], operation: str) -> list[str]:
    raw = config.get("operation_provider_order")
    if not isinstance(raw, dict):
        return []
    return [_normalize(item) for item in _string_list(raw.get(operation))]


def _provider_contexts(spec: OperationProviderSpec, config: dict[str, Any]) -> set[str]:
    contexts = {_normalize(item) for item in spec.contexts if _normalize(item)}
    raw = config.get("operation_provider_extra_contexts")
    if not isinstance(raw, dict):
        return contexts
    provider_name = _normalize(spec.name)
    for key, value in raw.items():
        if _normalize(key) == provider_name:
            contexts.update(_normalize(item) for item in _string_list(value) if _normalize(item))
            break
    return contexts


def _configured_value(config: dict[str, Any], key: str, default: str) -> str:
    if not key:
        return ""
    if key in config:
        return _text(config.get(key))
    return default


def _live_command_map(commands: Iterable[Any] | None) -> dict[str, str] | None:
    if commands is None:
        return None
    out: dict[str, str] = {}
    for command in commands:
        if isinstance(command, dict):
            name = _text(command.get("cmd"))
        else:
            name = _text(command)
        if name:
            out[_normalize(name)] = name
    return out


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize(value: Any) -> str:
    return " ".join(_text(value).strip().casefold().replace("_", "-").split())


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()
