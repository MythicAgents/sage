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


def test_upload_file_uuid_stays_on_file_transport_group():
    schema = [
        _param("filename", "Default", kind="ChooseOne", required=True, choices=["ca.pfx"]),
        _param("path", "Default", required=True),
        _param("file", "New File", kind="File", required=True),
        _param("path", "New File", required=True),
    ]
    file_uuid = "39a20f95-4065-4cdd-9084-ce88c1d132fc"

    result = resolve_params(
        schema,
        {"file": file_uuid, "path": r"C:\Windows\Temp\ca.pfx"},
        command="upload",
    )

    assert result.ok is True
    assert result.group == "New File"
    assert result.params == {"file": file_uuid, "path": r"C:\Windows\Temp\ca.pfx"}
    assert not any("rerouted" in note for note in result.notes)


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


def test_classify_rubeus_missing_service_specification_as_construction():
    output = "[X] One or more '/service:sname/server.domain.com' specifications are needed"

    assert classify_result("execute-assembly", output) == ResultClass.CONSTRUCTION


def test_classify_rubeus_invalid_ticket_argument_as_construction():
    output = "[X]/ticket:X must either be a .kirbi file or a base64 encoded .kirbi"

    assert classify_result("execute-assembly", output) == ResultClass.CONSTRUCTION


def test_classify_normal_task_output_as_success():
    output = "Callback ID | User | Host\n29 | CORP\\alice | WKSTN01\nSuccess"

    assert classify_result("whoami", output) == ResultClass.SUCCESS


def test_classify_make_token_hash_rejection_as_construction_failure():
    output = "Credential material is not a plaintext password."

    assert classify_result("make_token", output) == ResultClass.CONSTRUCTION


def _sharphound_record(timestamp, level, message):
    return f"{timestamp}|{level}|{message}"


def _sharphound_completion(timestamp="2030-01-02T13:03:25.5534065-04:00", clock="1:03 PM"):
    return _sharphound_record(
        timestamp,
        "INFORMATION",
        f"SharpHound Enumeration Completed at {clock} on 1/2/2030! Happy Graphing!",
    )


def _sharphound_http_404(timestamp="2030-01-02T13:03:24.2252855-04:00"):
    return _sharphound_record(
        timestamp,
        "ERROR",
        "HttpRequestException occurred checking NTLM accessibility for URL: "
        "https://host.invalid/service.svc. Exception: "
        "Response status code does not indicate success: 404 (Not Found).",
    )


def _apollo_sharphound_params(**extra):
    return {"Assembly": "SharpHound.exe", "Arguments": "-c All", **extra}


def _merlin_sharphound_params(**extra):
    return {
        "assembly": "SharpHound.exe",
        "args": "-c All",
        "spawnto": "C:\\Windows\\System32\\notepad.exe",
        **extra,
    }


def _sharphound_opsec_suffix():
    return (
        "[SAGE OPSEC] footprint total=6 axes={'disk_artifact': 2, 'new_beacon': 0, "
        "'new_process': 2, 'flagged_tool': 1, 'lateral_hop': 0, 'network_signature': 0, "
        "'reversibility': 1}. This action was recorded to the artifact ledger \u2014 clean it up "
        "at sub-goal completion (list_open_artifacts)."
    )


def test_classify_sharphound_exact_marker_accepts_apollo_and_resolved_merlin():
    cases = (
        ("execute_assembly", _apollo_sharphound_params()),
        ("execute-assembly", _merlin_sharphound_params()),
    )
    for command, parameters in cases:
        assert classify_result(command, _sharphound_completion(), parameters=parameters) == ResultClass.SUCCESS


def test_classify_sharphound_information_bodies_are_intentionally_open():
    output = "\n".join(
        (
            _sharphound_record(
                "2030-01-02T13:03:20.0000000-04:00",
                "INFORMATION",
                "Loaded cache with implementation-specific details",
            ),
            " 6 name to SID mappings.",
            " 3 machine sid mappings.",
            " 8 sid to domain mappings.",
            " 0 global catalog mappings.",
            _sharphound_record(
                "2030-01-02T13:03:21.0000000-04:00",
                "INFORMATION",
                "Saving cache with a different implementation-specific format",
            ),
            " 1 name to SID mappings.",
            " 2 machine sid mappings.",
            " 3 sid to domain mappings.",
            " 4 global catalog mappings.",
            _sharphound_record(
                "2030-01-02T13:03:22.0000000-04:00",
                "INFORMATION",
                "An unrecognized future informational message remains task output",
            ),
            _sharphound_completion(),
        )
    )
    assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.SUCCESS


def test_classify_sharphound_binding_failures_apply_only_to_marker_output():
    marker = _sharphound_completion()
    marker_cases = (
        ("execute_assembly", {}),
        ("execute_assembly", {"Assembly": "Seatbelt.exe"}),
        ("execute_assembly", {"assembly": "SharpHound.exe"}),
        (
            "execute_assembly",
            {"Assembly": "SharpHound.exe", "assembly_file": "SharpHound.exe"},
        ),
        (
            "execute_assembly",
            {"Assembly": "SharpHound.exe", "Assembly-Name": "SharpHound.exe"},
        ),
        ("execute-assembly", {}),
        ("execute-assembly", {"assembly": "Seatbelt.exe"}),
        ("execute-assembly", {"filename": "SharpHound.exe"}),
        (
            "execute-assembly",
            {"assembly": "SharpHound.exe", "file": "SharpHound.exe"},
        ),
        (
            "execute-assembly",
            {"assembly": "SharpHound.exe", "AssemblyFile": "SharpHound.exe"},
        ),
    )
    for command, parameters in marker_cases:
        assert classify_result(command, marker, parameters=parameters) == ResultClass.TRANSIENT

    unrelated = "SharpHound.exe registration completed successfully."
    for command, parameters in marker_cases:
        assert classify_result(command, unrelated, parameters=parameters) == ResultClass.SUCCESS


def test_classify_sharphound_selector_keys_use_unicode_canonical_comparison():
    cases = (
        (
            "execute_assembly",
            _apollo_sharphound_params(),
            (
                "Assembly Name",
                "Assembly.Name",
                "\uff21\uff53\uff53\uff45\uff4d\uff42\uff4c\uff59",
                "Assembly\u200bName",
            ),
        ),
        (
            "execute-assembly",
            _merlin_sharphound_params(),
            (
                "Assembly Name",
                "Assembly.Name",
                "\uff41\uff53\uff53\uff45\uff4d\uff42\uff4c\uff59",
                "assembly\u2060name",
            ),
        ),
    )
    for command, parameters, conflicting_keys in cases:
        for conflicting_key in conflicting_keys:
            conflicting = {**parameters, conflicting_key: "SharpHound.exe"}
            assert (
                classify_result(
                    command,
                    _sharphound_completion(),
                    parameters=conflicting,
                )
                == ResultClass.TRANSIENT
            )


def test_classify_sharphound_exact_binding_requires_completion_for_structured_output():
    outputs = (
        _sharphound_record(
            "2030-01-02T13:03:24.0000000-04:00",
            "WARNING",
            "collector stopped",
        ),
        "2030-01-02T13:03:24.0000000-04:00|INFORMATION|status|echo",
        "2030-01-02T13:03:24.0000000-04:00|WARNING",
        _sharphound_record(
            "2030-01-02T13:03:24.0000000-04:00",
            "INFORMATION",
            "arbitrary information without completion",
        ),
    )
    for command, parameters in (
        ("execute_assembly", _apollo_sharphound_params()),
        ("execute-assembly", _merlin_sharphound_params()),
    ):
        for output in outputs:
            assert classify_result(command, output, parameters=parameters) == ResultClass.TRANSIENT
        assert classify_result(command, "task 201 completed", parameters=parameters) == ResultClass.SUCCESS


def test_classify_sharphound_unrelated_commands_retain_generic_classification():
    marker = _sharphound_completion()
    for command in ("inline_assembly", "execute_Assembly", " execute_assembly"):
        assert classify_result(command, marker, parameters=_apollo_sharphound_params()) == ResultClass.SUCCESS

    assert classify_result("inline_assembly", f"Error issuing command\n{marker}") == ResultClass.TRANSIENT


def test_classify_sharphound_completion_record_is_raw_and_strict():
    valid = _sharphound_completion()
    cases = (
        f" {valid}",
        f"{valid} ",
        valid.replace("|INFORMATION|", "|INFORMATION| "),
        valid.replace("2030-01-02T13:03:25.5534065-04:00", " 2030-01-02T13:03:25.5534065-04:00"),
        valid.replace("-04:00", ""),
        valid.replace("2030-01-02", "2030-13-02"),
        _sharphound_completion(clock="13:03 PM"),
        _sharphound_completion(clock="1:04 PM"),
        valid.replace("|INFORMATION|", "|information|"),
        f"{valid}|echo",
    )
    for output in cases:
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.TRANSIENT


def test_classify_sharphound_marker_echo_near_match_and_duplicate_fail():
    marker_signals = (
        "echo SharpHound Enumeration Completed",
        "wrapper printed Happy Graphing!",
        "sharphound enumeration completed",
        "sharphound\t enumeration   completed",
        "happy\tgraphing!",
        "SharpHound\u200bEnumeration\u2060Completed",
        "SharpHoundEnumerationCompleted",
        "SharpHound---Enumeration...Completed",
        "Happy\u200bGraphing",
        "HappyGraphing",
        "Happy---Graphing!!!",
        "\uff33\uff48\uff41\uff52\uff50\uff28\uff4f\uff55\uff4e\uff44 "
        "\uff25\uff4e\uff55\uff4d\uff45\uff52\uff41\uff54\uff49\uff4f\uff4e "
        "\uff23\uff4f\uff4d\uff50\uff4c\uff45\uff54\uff45\uff44",
    )
    for signal in marker_signals:
        assert (
            classify_result(
                "execute_assembly",
                signal,
                parameters=_apollo_sharphound_params(),
            )
            == ResultClass.TRANSIENT
        )
        structured = "\n".join(
            (
                _sharphound_record(
                    "2030-01-02T13:03:24.0000000-04:00",
                    "INFORMATION",
                    signal,
                ),
                _sharphound_completion(),
            )
        )
        assert (
            classify_result(
                "execute_assembly",
                structured,
                parameters=_apollo_sharphound_params(),
            )
            == ResultClass.TRANSIENT
        )

    cases = (
        "\n".join(
            (
                _sharphound_record(
                    "2030-01-02T13:03:24.0000000-04:00",
                    "INFORMATION",
                    "SharpHound Enumeration Completed soon",
                ),
                _sharphound_completion(),
            )
        ),
        "\n".join((_sharphound_completion(), _sharphound_completion())),
    )
    for output in cases:
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.TRANSIENT


def test_classify_sharphound_unknown_and_malformed_structured_records_fail():
    for level in ("PANIC", "EMERGENCY", "ALERT", "SEVERE", "EXCEPTION", "ERRORS", "CRITICAL", "FATAL"):
        output = "\n".join(
            (
                _sharphound_record("2030-01-02T13:03:24.0000000-04:00", level, "status"),
                _sharphound_completion(),
            )
        )
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.TRANSIENT

    malformed = "\n".join(
        (
            "2030-01-02T13:03:24.0000000-04:00|INFORMATION|status|echo",
            _sharphound_completion(),
        )
    )
    assert classify_result("execute_assembly", malformed, parameters=_apollo_sharphound_params()) == ResultClass.TRANSIENT


def test_classify_sharphound_timestamps_are_nondecreasing_by_absolute_time():
    valid_across_offsets = "\n".join(
        (
            _sharphound_record(
                "2030-01-02T18:00:00.0000000+01:00",
                "INFORMATION",
                "arbitrary information",
            ),
            _sharphound_completion(
                timestamp="2030-01-02T17:01:00.0000000Z",
                clock="5:01 PM",
            ),
        )
    )
    reversed_across_offsets = "\n".join(
        (
            _sharphound_record(
                "2030-01-02T17:02:00.0000000Z",
                "INFORMATION",
                "arbitrary information",
            ),
            _sharphound_completion(
                timestamp="2030-01-02T18:01:00.0000000+01:00",
                clock="6:01 PM",
            ),
        )
    )

    assert (
        classify_result(
            "execute_assembly",
            valid_across_offsets,
            parameters=_apollo_sharphound_params(),
        )
        == ResultClass.SUCCESS
    )
    assert (
        classify_result(
            "execute_assembly",
            reversed_across_offsets,
            parameters=_apollo_sharphound_params(),
        )
        == ResultClass.TRANSIENT
    )


def test_classify_sharphound_timestamp_order_preserves_seventh_fractional_digit():
    increasing = "\n".join(
        (
            _sharphound_record(
                "2030-01-02T13:03:25.5534064-04:00",
                "INFORMATION",
                "arbitrary information",
            ),
            _sharphound_completion(
                timestamp="2030-01-02T13:03:25.5534065-04:00",
            ),
        )
    )
    decreasing = "\n".join(
        (
            _sharphound_record(
                "2030-01-02T13:03:25.5534065-04:00",
                "INFORMATION",
                "arbitrary information",
            ),
            _sharphound_completion(
                timestamp="2030-01-02T13:03:25.5534064-04:00",
            ),
        )
    )
    offset_equal = "\n".join(
        (
            _sharphound_record(
                "2030-01-02T14:03:25.5534065+01:00",
                "INFORMATION",
                "arbitrary information",
            ),
            _sharphound_completion(
                timestamp="2030-01-02T13:03:25.5534065Z",
            ),
        )
    )
    offset_decreasing = "\n".join(
        (
            _sharphound_record(
                "2030-01-02T14:03:25.5534065+01:00",
                "INFORMATION",
                "arbitrary information",
            ),
            _sharphound_completion(
                timestamp="2030-01-02T13:03:25.5534064Z",
            ),
        )
    )

    for output in (increasing, offset_equal):
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.SUCCESS
    for output in (decreasing, offset_decreasing):
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.TRANSIENT


def test_classify_sharphound_timezone_offsets_are_bounded():
    for offset in ("Z", "+00:00", "-00:00", "+23:59", "-23:59"):
        output = _sharphound_completion(
            timestamp=f"2030-01-02T13:03:25.5534065{offset}",
        )
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.SUCCESS

    for sign in ("+", "-"):
        for offset in ("00:60", "00:99", "01:60", "24:00"):
            output = _sharphound_completion(
                timestamp=f"2030-01-02T13:03:25.5534065{sign}{offset}",
            )
            assert (
                classify_result(
                    "execute_assembly",
                    output,
                    parameters=_apollo_sharphound_params(),
                )
                == ResultClass.TRANSIENT
            )


def test_classify_sharphound_exact_404_is_optional_and_must_precede_completion():
    for scheme in ("http", "https"):
        error = _sharphound_http_404().replace("https://", f"{scheme}://")
        output = "\n".join((error, _sharphound_completion()))
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.SUCCESS

    cases = (
        "\n".join((_sharphound_http_404(), _sharphound_http_404(), _sharphound_completion())),
        "\n".join((_sharphound_completion(), _sharphound_http_404(timestamp="2030-01-02T13:03:26-04:00"))),
        "\n".join((_sharphound_http_404().replace("404 (Not Found)", "403 (Forbidden)"), _sharphound_completion())),
        "\n".join((_sharphound_http_404().replace("NTLM accessibility", "NTLM availability"), _sharphound_completion())),
        "\n".join(
            (
                _sharphound_record(
                    "2030-01-02T13:03:24.0000000-04:00",
                    "ERROR",
                    "Unknown collector error",
                ),
                _sharphound_completion(),
            )
        ),
    )
    for output in cases:
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.TRANSIENT


def test_classify_sharphound_raw_line_exceptions_are_exact_and_bounded():
    valid = "\n".join(
        (
            "",
            "Closing writers",
            " 6 name to SID mappings.",
            " 3 machine sid mappings.",
            " 8 sid to domain mappings.",
            " 0 global catalog mappings.",
            _sharphound_completion(),
            "",
            "",
            _sharphound_opsec_suffix(),
        )
    )
    assert classify_result("execute_assembly", valid, parameters=_apollo_sharphound_params()) == ResultClass.SUCCESS

    cases = (
        valid.replace("Closing writers", "Closing writer"),
        valid.replace(" 6 name to SID mappings.", "6 name to SID mappings."),
        valid.replace(" 3 machine sid mappings.", " 3 unknown mappings."),
        f"{valid}\nClosing writers",
        valid.replace("footprint total=6", "footprint total=7"),
        f"{valid}\n{_sharphound_opsec_suffix()}",
    )
    for output in cases:
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.TRANSIENT


def test_classify_sharphound_structured_record_after_completion_fails():
    output = "\n".join(
        (
            _sharphound_completion(),
            _sharphound_record(
                "2030-01-02T13:03:26.0000000-04:00",
                "INFORMATION",
                "post-completion information",
            ),
        )
    )
    assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == ResultClass.TRANSIENT


def test_classify_sharphound_higher_priority_failure_classes_remain_authoritative():
    cases = (
        (
            "Supplied Arguments {'foo': 'bar'} don't match any parameter group for this command",
            ResultClass.CONSTRUCTION,
        ),
        ("Access is denied.", ResultClass.GENUINE),
        ("Error issuing command: Failed to create task", ResultClass.TRANSIENT),
    )
    for prefix, expected in cases:
        output = "\n".join((prefix, _sharphound_completion()))
        assert classify_result("execute_assembly", output, parameters=_apollo_sharphound_params()) == expected

    assert (
        classify_result(
            "execute_assembly",
            _sharphound_completion(),
            "RuntimeError: wait failed",
            _apollo_sharphound_params(),
        )
        == ResultClass.TRANSIENT
    )


def test_genuine_breaker_decision_stops_without_retry():
    assert breaker_decision(ResultClass.GENUINE, 0) == "stop"
