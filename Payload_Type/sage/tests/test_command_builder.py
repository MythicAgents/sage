import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
from command_builder import ResultClass, breaker_decision, classify_result, resolve_params  # noqa: E402


def _param(cli_name, group="Default", *, name=None, kind="String", required=False, choices=None, default_value=""):
    return {
        "cli_name": cli_name,
        "name": name or cli_name,
        "parameter_group_name": group,
        "type": kind,
        "choices": choices or [],
        "required": required,
        "default_value": default_value,
    }


def test_execute_assembly_prior_keys_resolve_to_default_group_cli_names():
    schema = [
        _param("filename", "Default", name="assembly_name", kind="ChooseOne", required=True, choices=["Rubeus.exe"]),
        _param("arguments", "Default", name="assembly_arguments", required=True),
        _param("file", "New Assembly", name="assembly_file", kind="File", required=True),
        _param("arguments", "New Assembly", name="assembly_arguments", required=True),
    ]

    result = resolve_params(
        schema,
        {"assembly_name": "Rubeus.exe", "assembly_arguments": "klist /nowrap"},
        command="execute_assembly",
    )

    assert result.ok is True
    assert result.group == "Default"
    assert result.params == {"filename": "Rubeus.exe", "arguments": "klist /nowrap"}
    assert "assembly_name" not in result.params
    assert "assembly_arguments" not in result.params


def test_merlin_execute_assembly_cli_differs_from_name_resolves_to_default():
    # LIVE merlin shape: the registered-assembly param is cli_name="assembly" / name="filename"
    # (cli != name). The agent supplies the training-prior key "assembly_name". Static alias
    # assembly_name->filename must resolve via the NAME map to cli "assembly", so the Default
    # (registered) group wins over "New File" (which needs an unsatisfiable uploaded `file`).
    # Regression guard for the live-schema gap that a fake-schema test missed.
    schema = [
        _param("assembly", "Default", name="filename", kind="ChooseOne", required=True, choices=["Rubeus.exe"]),
        _param("args", "Default", name="arguments"),
        _param("spawnto", "Default", name="spawnto", required=True, default_value="C:\\Windows\\System32\\WerFault.exe"),
        _param("file", "New File", name="file", kind="File", required=True),
        _param("args", "New File", name="arguments"),
        _param("spawnto", "New File", name="spawnto", required=True, default_value="C:\\Windows\\System32\\WerFault.exe"),
    ]

    result = resolve_params(
        schema,
        {"assembly_name": "Rubeus.exe", "assembly_arguments": "klist"},
        command="execute-assembly",
    )

    assert result.ok is True
    assert result.group == "Default"
    assert result.group != "New File"
    assert result.params["assembly"] == "Rubeus.exe"
    assert result.params["args"] == "klist"
    assert result.params["spawnto"] == "C:\\Windows\\System32\\WerFault.exe"


def test_execute_assembly_required_default_makes_default_group_satisfiable():
    spawnto = r"C:\Windows\System32\WerFault.exe"
    schema = [
        _param("filename", "Default", name="assembly_name", kind="ChooseOne", required=True, choices=["Rubeus.exe"]),
        _param("args", "Default", required=False),
        _param("spawnto", "Default", required=True, default_value=spawnto),
        _param("spawntoargs", "Default", required=False),
        _param("file", "New File", kind="File", required=True),
        _param("args", "New File", required=False),
        _param("spawnto", "New File", required=True, default_value=spawnto),
        _param("spawntoargs", "New File", required=False),
    ]

    result = resolve_params(
        schema,
        {"assembly_name": "Rubeus.exe", "args": "klist"},
        command="execute_assembly",
    )

    assert result.ok is True
    assert result.group == "Default"
    assert result.group != "New File"
    assert result.params["filename"] == "Rubeus.exe"
    assert result.params["args"] == "klist"
    assert result.params["spawnto"] == spawnto
    assert f"defaulted spawnto={spawnto}" in result.notes


def test_inline_assembly_prior_keys_resolve_to_one_group_without_group_mix():
    schema = [
        _param("filename", "Default", name="assembly_name", kind="ChooseOne", required=True, choices=["Seatbelt.exe"]),
        _param("arguments", "Default", name="assembly_arguments", required=True),
        _param("file", "New Assembly", name="assembly_file", kind="File", required=True),
        _param("arguments", "New Assembly", name="assembly_arguments", required=True),
    ]

    result = resolve_params(
        schema,
        {"assembly_name": "Seatbelt.exe", "assembly_arguments": "-group=system"},
        command="inline_assembly",
    )

    assert result.ok is True
    assert result.group == "Default"
    assert result.params == {"filename": "Seatbelt.exe", "arguments": "-group=system"}
    assert set(result.params) == {"filename", "arguments"}


def test_mimikatz_commands_aliases_to_arguments_and_defaults_spawnto():
    schema = [
        _param("arguments", "Default", required=True),
        _param("spawnto", "Default", required=False, default_value=r"C:\Windows\System32\rundll32.exe"),
    ]
    command_text = "lsadump::dcsync /domain:essos.local /user:krbtgt"

    result = resolve_params(schema, {"commands": command_text}, command="mimikatz")

    assert result.ok is True
    assert result.group == "Default"
    assert result.params == {
        "arguments": command_text,
        "spawnto": r"C:\Windows\System32\rundll32.exe",
    }


def test_mimikatz_required_defaults_auto_fill_without_overriding_supplied_arguments():
    spawnto = r"C:\Windows\System32\WerFault.exe"
    command_text = "lsadump::dcsync /domain:essos.local /user:krbtgt"
    schema = [
        _param("arguments", "Default", required=True, default_value="token::whoami"),
        _param("spawnto", "Default", required=True, default_value=spawnto),
        _param("spawntoargs", "Default", required=False),
    ]

    result = resolve_params(schema, {"commands": command_text}, command="mimikatz")

    assert result.ok is True
    assert result.group == "Default"
    assert result.params["arguments"] == command_text
    assert result.params["spawnto"] == spawnto
    assert f"defaulted spawnto={spawnto}" in result.notes
    assert "defaulted arguments=token::whoami" not in result.notes


def test_apollo_mimikatz_commands_array_wraps_single_command_string():
    schema = [
        _param("Commands", "Default", name="commands", kind="Array", required=True, default_value="[]"),
    ]
    command_text = "kerberos::golden /user:Administrator /domain:north.local /sid:S-1-5-21-1-2-3 /aes256:" + "a" * 64 + " /ptt"

    result = resolve_params(schema, {"commands": command_text}, command="mimikatz")

    assert result.ok is True
    assert result.group == "Default"
    assert result.params == {"Commands": [command_text]}
    assert "mapped 'commands' to 'Commands'" in result.notes


def test_apollo_mimikatz_commands_array_preserves_command_list():
    schema = [
        _param("Commands", "Default", name="commands", kind="Array", required=True, default_value="[]"),
    ]
    commands = ["privilege::debug", "sekurlsa::logonpasswords"]

    result = resolve_params(schema, {"commands": commands}, command="mimikatz")

    assert result.ok is True
    assert result.params == {"Commands": commands}


def test_argumentless_commands_accept_empty_dict_as_empty_params():
    for command in ("ps", "whoami", "rev2self"):
        result = resolve_params([], {}, command=command)

        assert result.ok is True
        assert result.params == {}
        assert result.repair is None


def test_operating_system_choice_is_case_normalized():
    schema = [
        _param(
            "operating_system",
            "Default",
            kind="ChooseOne",
            required=True,
            choices=["Windows", "Linux"],
        )
    ]

    result = resolve_params(schema, {"operating_system": "windows"}, command="create_payload")

    assert result.ok is True
    assert result.group == "Default"
    assert result.params == {"operating_system": "Windows"}


def test_chooseone_out_of_choices_returns_repair_with_param_and_choices():
    schema = [
        _param(
            "operating_system",
            "Default",
            kind="ChooseOne",
            required=True,
            choices=["Windows", "Linux"],
        )
    ]

    result = resolve_params(schema, {"operating_system": "macOS"}, command="create_payload")

    assert result.ok is False
    assert result.params == {"operating_system": "macOS"}
    assert "operating_system" in result.repair
    assert "Windows" in result.repair
    assert "Linux" in result.repair


def test_missing_required_param_returns_repair_without_fabricating_value():
    schema = [
        _param("filename", "Default", required=True),
        _param("arguments", "Default", required=True),
    ]

    result = resolve_params(schema, {"arguments": "triage"}, command="execute_assembly")

    assert result.ok is False
    assert result.group == "Default"
    assert result.params == {"arguments": "triage"}
    assert "filename" in result.repair
    assert "Default" in result.repair
    assert "filename" not in result.params


def test_missing_required_param_without_default_still_returns_repair():
    schema = [_param("target", "Default", required=True)]

    result = resolve_params(schema, {}, command="example")

    assert result.ok is False
    assert result.params == {}
    assert result.repair is not None
    assert "target" in result.repair


def test_file_vs_registered_filename_selects_exact_group():
    schema = [
        _param("filename", "Default", kind="ChooseOne", required=True, choices=["Rubeus.exe"]),
        _param("arguments", "Default", required=False),
        _param("file", "New File", kind="File", required=True),
        _param("arguments", "New File", required=False),
    ]

    # A registered name on the registered selector resolves to the Default group.
    registered = resolve_params(schema, {"filename": "Rubeus.exe", "arguments": "klist"}, command="execute_assembly")
    assert registered.ok is True
    assert registered.group == "Default"
    assert registered.params == {"filename": "Rubeus.exe", "arguments": "klist"}

    # A reference placed on the File/upload arg is REROUTED to the registered selector + Default group. Sage
    # references already-registered tools; routing a registered ref through the "New File" upload group (esp.
    # with a UUID) selects the wrong group and crashes Merlin / misbehaves on Apollo. Regression guard for the
    # cb41 (merlin `file`+UUID) / cb44 (apollo `assembly_file`+UUID) bug.
    rerouted = resolve_params(schema, {"file": "Rubeus.exe", "arguments": "klist"}, command="execute_assembly")
    assert rerouted.ok is True
    assert rerouted.group == "Default"
    assert rerouted.params == {"filename": "Rubeus.exe", "arguments": "klist"}
    assert any("rerouted" in note for note in rerouted.notes)

    # Even a UUID on the upload arg must NOT select the "New File"/upload (crash) path — it reroutes to the
    # registered selector (and then fails choice validation with a repair hint pointing at registered names).
    uuid_ref = resolve_params(schema, {"file": "39a20f95-4065-4cdd-9084-ce88c1d132fc"}, command="execute_assembly")
    assert uuid_ref.group != "New File"


def test_classify_parameter_group_mismatch_as_construction():
    output = "Supplied Arguments {'foo': 'bar'} don't match any parameter group for this command"

    assert classify_result("mimikatz", output) == ResultClass.CONSTRUCTION


def test_classify_access_denied_as_genuine():
    output = "Access is denied.\nSystem error 5 has occurred."

    assert classify_result("net", output) == ResultClass.GENUINE


def test_classify_dcsync_object_not_found_as_genuine():
    output = "Object not found\nGetNCChanges: 0x000020f7"

    assert classify_result("mimikatz", output) == ResultClass.GENUINE


def test_classify_failed_create_task_as_transient():
    output = "Error issuing command 'mimikatz' to agent 29: Failed to create task"

    assert classify_result("mimikatz", output) == ResultClass.TRANSIENT


def test_classify_argumentless_command_failure_as_construction():
    output = "ps takes no command line arguments"

    assert classify_result("ps", output) == ResultClass.CONSTRUCTION


def test_classify_normal_task_output_as_success():
    output = "Callback ID | User | Host\n29 | CORP\\alice | WKSTN01\nSuccess"

    assert classify_result("whoami", output) == ResultClass.SUCCESS


def test_genuine_breaker_decision_stops_without_retry():
    assert breaker_decision(ResultClass.GENUINE, 0) == "stop"
