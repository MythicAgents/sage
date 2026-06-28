import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_capability_adapter as adapter  # noqa: E402
import operation_providers  # noqa: E402


def _surface(*commands):
    return [{"cmd": command, "commandparameters": []} for command in commands]


def test_default_dcsync_prefers_native_provider():
    provider = operation_providers.select_operation_provider(
        "drsuapi-dcsync",
        config={},
        context="active-auth-context",
    )

    assert provider is not None
    assert provider.name == "native-drsuapi-dcsync"
    assert provider.command == "dcsync"
    assert provider.context_semantics == "active-logon-session"


def test_merlin_dcsync_uses_inprocess_sharpkatz_provider():
    provider = operation_providers.select_operation_provider(
        "drsuapi-dcsync",
        config=adapter.MERLIN_MYTHIC_ADAPTER,
        context="active-auth-context",
    )

    assert provider is not None
    assert provider.name == "managed-sharpkatz-dcsync"
    assert provider.kind == "external-tool"
    assert provider.setup_command == "load-assembly"
    assert provider.command == "invoke-assembly"
    assert provider.tool == "SharpKatz.exe"
    assert provider.context_semantics == "active-logon-session"


def test_managed_dcsync_requires_explicit_adapter_opt_in():
    config = dict(adapter.MERLIN_MYTHIC_ADAPTER)
    config.pop("drsuapi_inprocess_load_command")
    config.pop("drsuapi_inprocess_invoke_command")
    provider = operation_providers.select_operation_provider(
        "drsuapi-dcsync",
        config=config,
        context="active-auth-context",
    )

    assert provider is None


def test_default_current_ticket_import_prefers_native_provider():
    provider = operation_providers.select_operation_provider(
        "kerberos-ticket-import",
        config={},
        context="current-agent-cache",
    )

    assert provider is not None
    assert provider.name == "native-current-ticket-cache-import"
    assert provider.command == "ticket_cache_add"


def test_merlin_current_ticket_import_uses_rubeus_provider():
    provider = operation_providers.select_operation_provider(
        "kerberos-ticket-import",
        config=adapter.MERLIN_MYTHIC_ADAPTER,
        context="current-agent-cache",
    )

    assert provider is not None
    assert provider.name == "managed-rubeus-ptt"
    assert provider.kind == "external-tool"
    assert provider.setup_command == "load-assembly"
    assert provider.command == "invoke-assembly"
    assert provider.tool == "Rubeus.exe"
    assert provider.context_semantics == "current-logon-session"


def test_isolated_rubeus_import_requires_explicit_adapter_context_opt_in():
    config = dict(adapter.MERLIN_MYTHIC_ADAPTER)
    config.pop("operation_provider_extra_contexts")
    provider = operation_providers.select_operation_provider(
        "kerberos-ticket-import",
        config=config,
        context="isolated",
    )

    assert provider is None


def test_merlin_isolated_ticket_import_uses_applied_token_rubeus_provider():
    provider = operation_providers.select_operation_provider(
        "kerberos-ticket-import",
        config=adapter.MERLIN_MYTHIC_ADAPTER,
        context="isolated",
    )

    assert provider is not None
    assert provider.name == "managed-rubeus-ptt"
    assert provider.command == "invoke-assembly"
    assert provider.setup_command == "load-assembly"


def test_merlin_isolated_ticket_list_uses_applied_token_klist_provider():
    provider = operation_providers.select_operation_provider(
        "kerberos-ticket-list",
        config=adapter.MERLIN_MYTHIC_ADAPTER,
        context="isolated",
    )

    assert provider is not None
    assert provider.name == "managed-rubeus-klist"
    assert provider.command == "invoke-assembly"
    assert provider.setup_command == "load-assembly"
    assert provider.tool == "Rubeus.exe"
    assert provider.arguments == "klist"


def test_merlin_current_ticket_list_still_prefers_os_klist_provider():
    provider = operation_providers.select_operation_provider(
        "kerberos-ticket-list",
        config=adapter.MERLIN_MYTHIC_ADAPTER,
        context="current",
    )

    assert provider is not None
    assert provider.name == "windows-klist-list"
    assert provider.command == "run"
    assert provider.executable == "klist.exe"


def test_merlin_current_service_ticket_acquire_uses_os_klist_get_provider():
    provider = operation_providers.select_operation_provider(
        "kerberos-service-ticket-acquire",
        config=adapter.MERLIN_MYTHIC_ADAPTER,
        context="current",
    )

    assert provider is not None
    assert provider.name == "windows-klist-get"
    assert provider.command == "run"
    assert provider.executable == "klist.exe"


def test_merlin_structured_artifact_read_uses_os_more_provider():
    provider = operation_providers.select_operation_provider(
        "structured-artifact-read",
        config=adapter.MERLIN_MYTHIC_ADAPTER,
        context="current",
    )

    assert provider is not None
    assert provider.name == "windows-more-structured-artifact-read"
    assert provider.kind == "os"
    assert provider.command == "run"
    assert provider.executable == "more.com"
    assert provider.context_semantics == "current-logon-session"


def test_default_managed_secret_read_has_no_inprocess_provider():
    provider = operation_providers.select_operation_provider(
        "ldap-managed-local-admin-secret-read",
        config={},
        context="current-agent-cache",
    )

    assert provider is None


def test_merlin_managed_secret_read_uses_sharpview_provider():
    provider = operation_providers.select_operation_provider(
        "ldap-managed-local-admin-secret-read",
        config=adapter.MERLIN_MYTHIC_ADAPTER,
        context="current-agent-cache",
    )

    assert provider is not None
    assert provider.name == "managed-sharpview-computer-attribute-read"
    assert provider.kind == "external-tool"
    assert provider.setup_command == "load-assembly"
    assert provider.command == "invoke-assembly"
    assert provider.tool == "SharpView.exe"
    assert provider.context_semantics == "current-logon-session"


def test_live_missing_ticket_cache_add_requires_multistep_provider_rebuild():
    candidate = operation_providers.live_provider_candidate(
        {
            "command": "ticket_cache_add",
            "operation": "kerberos-ticket-import",
            "parameters": {"base64ticket": "{{kerberos_ticket_base64}}"},
            "consumes": ["kerberos_ticket_base64"],
        },
        payload_type="merlin",
        command_surface=_surface("load-assembly", "invoke-assembly", "run"),
    )

    assert candidate is not None
    assert candidate["blocked"] is True
    assert candidate["command"] == ""
    assert candidate["provider"] == "managed-rubeus-ptt"
    assert "requires setup command 'load-assembly'" in candidate["reason"]


def test_live_missing_ticket_cache_purge_prefers_os_run_provider():
    candidate = operation_providers.live_provider_candidate(
        {
            "command": "ticket_cache_purge",
            "operation": "kerberos-ticket-purge",
            "parameters": {"all": True, "serviceName": "", "luid": ""},
        },
        payload_type="merlin",
        command_surface=_surface("run", "shell"),
    )

    assert candidate is not None
    assert candidate["command"] == "run"
    assert candidate["provider"] == "windows-klist-purge"
    assert candidate["parameters"] == {
        "executable": "klist.exe",
        "arguments": "purge",
    }


def test_live_missing_service_ticket_acquire_prefers_os_run_provider():
    candidate = operation_providers.live_provider_candidate(
        {
            "command": "ticket_cache_get",
            "operation": "kerberos-service-ticket-acquire",
            "parameters": {"resource": "\\\\dc01.lab.local\\C$"},
        },
        payload_type="merlin",
        command_surface=_surface("run", "shell"),
    )

    assert candidate is not None
    assert candidate["command"] == "run"
    assert candidate["provider"] == "windows-klist-get"
    assert candidate["parameters"] == {
        "executable": "klist.exe",
        "arguments": "get cifs/dc01.lab.local",
    }


def test_live_missing_structured_artifact_read_prefers_os_run_provider():
    candidate = operation_providers.live_provider_candidate(
        {
            "command": "shell",
            "operation": "structured-artifact-read",
            "parameters": {
                "arguments": (
                    r"type \\lab.local\SYSVOL\lab.local\Policies\{GUID}"
                    r"\Machine\Preferences\ScheduledTasks\ScheduledTasks.xml"
                ),
            },
            "consumes": ["artifact:gpo_immediate_task"],
        },
        payload_type="merlin",
        command_surface=_surface("run", "shell"),
    )

    assert candidate is not None
    assert candidate["command"] == "run"
    assert candidate["provider"] == "windows-more-structured-artifact-read"
    assert candidate["parameters"] == {
        "executable": "more.com",
        "arguments": (
            r"\\lab.local\SYSVOL\lab.local\Policies\{GUID}"
            r"\Machine\Preferences\ScheduledTasks\ScheduledTasks.xml"
        ),
    }
