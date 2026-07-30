import importlib
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


OBJECTIVE = "Collect and ingest the current graph, then read available credentials."


def _load_contract():
    return importlib.import_module("ai.langgraph.objective_contract")


def _resolved_contract(*, payload_type="apollo", adapter=None, callback_id=7, forest="corp.local"):
    mod = _load_contract()
    return mod.compile_objective_contract(
        OBJECTIVE,
        stored_objective_trigger=True,
        objective_is_open_ended=False,
    ).resolve_collection_scope(
        turn_id="turn-contract-fixture",
        callback_display_id=callback_id,
        payload_type=payload_type,
        forest=forest,
        adapter={} if adapter is None else adapter,
    )


def _runner_call(contract, arguments, *, callback_id=7, selector="assembly_name", args_key="assembly_arguments"):
    return {
        "command": contract.collection_profile.runner_command,
        "parameters": {selector: "SharpHound.exe", args_key: arguments},
        "callback_display_id": callback_id,
    }


def _canonical_arguments(contract):
    return contract.to_payload()["collection_task_spec"]["preferred_collection_task"]["parameters"][
        contract.collection_profile.runner_args_param
    ]


def _authoritative_ingest_evidence(contract, **overrides):
    metadata = {
        "filename_utf8": f"20260722_bloodhound_{contract.collection_token}.zip",
        "is_download_from_agent": True,
        "complete": True,
        "deleted": False,
        "task": {
            "display_id": 999,
            "command_name": contract.collection_profile.download_command,
            "callback": {"display_id": 7},
        },
    }
    for key, value in overrides.items():
        if key == "source_callback_display_id":
            metadata["task"]["callback"]["display_id"] = value
        elif key == "source_filename":
            metadata["filename_utf8"] = value
        elif key == "source_task_display_id":
            metadata["task"]["display_id"] = value
        elif key == "source_command":
            metadata["task"]["command_name"] = value
        elif key == "source_is_download_from_agent":
            metadata["is_download_from_agent"] = value
        elif key == "source_complete":
            metadata["complete"] = value
        elif key == "source_deleted":
            metadata["deleted"] = value
    return {"source_metadata": metadata}


def test_graph_credential_report_contract_is_unresolved_until_live_scope_is_bound():
    mod = _load_contract()
    contract = mod.compile_objective_contract(
        OBJECTIVE,
        stored_objective_trigger=True,
        objective_is_open_ended=False,
    ).bind_turn("turn-1")

    assert contract.scope_kind == "bounded_report"
    assert contract.required_outcomes == ("graph_ingested", "credentials_reported")
    assert contract.allowed_action_families == (
        "issue_task_and_waitfor_task_output",
        "ingest_collection",
        "read_credentials",
    )
    assert contract.stop_conditions == ("graph_ingested", "credentials_reported")
    assert contract.evidence_requirements == (
        "bloodhound_ingest",
        "mythic_credential_store",
    )
    assert contract.engine == "supervisor_graph"
    assert contract.approval_policy == "turn_hitl"
    assert contract.task_scope == "sharphound_collection"
    assert contract.scope_resolution == "unresolved"
    assert len(contract.collection_token) == 16
    assert contract.allows_action("read_credentials") is False
    assert contract.allows_guarded_tool_call("ingest_collection", {"callback_display_id": 7}) is False
    assert "unresolved collection scope" in contract.denial_reason("ingest_collection")


def test_contract_credential_report_requires_one_unfiltered_store_read():
    contract = _resolved_contract()

    assert contract.allows_guarded_tool_call("read_credentials", {}) is True
    assert contract.allows_guarded_tool_call(
        "read_credentials",
        {"realm": "", "account": ""},
    ) is True
    assert contract.allows_guarded_tool_call(
        "read_credentials",
        {"realm": "corp.local", "account": ""},
    ) is False
    assert contract.allows_guarded_tool_call(
        "read_credentials",
        {"unexpected": "value"},
    ) is False


def test_turn_token_is_deterministic_for_one_turn_and_distinct_between_turns():
    mod = _load_contract()
    base = mod.compile_objective_contract(
        OBJECTIVE,
        stored_objective_trigger=True,
        objective_is_open_ended=False,
    )
    assert base.bind_turn("turn-a").collection_token == base.bind_turn("turn-a").collection_token
    assert base.bind_turn("turn-a").collection_token != base.bind_turn("turn-b").collection_token


def test_resolved_contract_exposes_exact_objective_outcomes_engine_scope_and_task_spec():
    contract = _resolved_contract()
    payload = contract.to_payload()

    assert payload["objective_text"] == OBJECTIVE
    assert payload["required_outcomes"] == ["graph_ingested", "credentials_reported"]
    assert payload["allowed_capability_families"] == []
    assert payload["allowed_action_families"] == [
        "issue_task_and_waitfor_task_output",
        "ingest_collection",
        "read_credentials",
    ]
    assert payload["stop_conditions"] == ["graph_ingested", "credentials_reported"]
    assert payload["evidence_requirements"] == ["bloodhound_ingest", "mythic_credential_store"]
    assert payload["engine"] == "supervisor_graph"
    assert payload["approval_policy"] == "turn_hitl"
    assert payload["resolved_scope"] == {
        "status": "resolved",
        "reason": "unique supported live foothold",
        "callback_display_id": 7,
        "forest": "corp.local",
    }
    task_spec = payload["collection_task_spec"]
    assert task_spec["callback_display_id"] == 7
    assert task_spec["collection_token"] == contract.collection_token
    assert task_spec["assembly_identity"] == {
        "registered_filename": "SharpHound.exe",
        "parameter_group": "registered_file_selector",
        "upload_group_allowed": False,
    }


@pytest.mark.parametrize(
    "arguments_template",
    (
        "-c All --CollectAllProperties --SearchForest --OutputDirectory C:\\Users\\Public --ZipFilename {zip}",
        "--ZIPFILENAME '{zip}' --outputdirectory \"C:\\Users\\Public\" --SEARCHFOREST "
        "--COLLECTALLPROPERTIES --CollectionMethods ALL",
        "--ZipFilename={zip} -o=\"C:\\Users\\Public\" --SearchForest "
        "--CollectAllProperties -c=All",
    ),
)
def test_sharphound_semantic_parser_accepts_safe_order_quote_case_and_alias_variants(arguments_template):
    contract = _resolved_contract()
    zip_name = f"bloodhound_{contract.collection_token}.zip"
    arguments = arguments_template.format(zip=zip_name)
    call = _runner_call(contract, arguments, selector="Assembly", args_key="Arguments")

    assert contract.allows_guarded_tool_call("issue_task_and_waitfor_task_output", call) is True


@pytest.mark.parametrize(
    "mutator",
    (
        lambda args, _token: args + " --Stealth",
        lambda args, _token: args.replace(r"C:\Users\Public", r"C:\Windows\Temp"),
        lambda args, _token: args.replace("bloodhound_", "unrelated_"),
        lambda args, _token: args + " && whoami",
        lambda args, _token: args + " --Loop",
        lambda args, _token: args.replace("--SearchForest", "--SearchForest --Domain other.local"),
        lambda args, _token: args.replace("--SearchForest", "--Domain corp.local --DomainController dc.external"),
        lambda args, _token: args.replace("--CollectAllProperties ", ""),
    ),
)
def test_sharphound_semantic_parser_rejects_extra_flags_alt_dirs_shell_and_unlinked_zip(mutator):
    contract = _resolved_contract()
    arguments = mutator(_canonical_arguments(contract), contract.collection_token)
    assert contract.allows_guarded_tool_call(
        "issue_task_and_waitfor_task_output",
        _runner_call(contract, arguments),
    ) is False


@pytest.mark.parametrize(
    "zip_name_template",
    (
        "bloodhound_{token}.zip",
        "20260722_bloodhound_{token}.zip",
        "20260722112233_bloodhound_{token}.zip",
    ),
)
def test_token_collection_basename_valid_controls_cross_runner_caller_and_filemeta(zip_name_template):
    contract = _resolved_contract()
    canonical_name = f"bloodhound_{contract.collection_token}.zip"
    zip_name = zip_name_template.format(token=contract.collection_token)
    arguments = _canonical_arguments(contract).replace(canonical_name, zip_name)
    caller_args = {"file_uuid": "file-123", "file_name": zip_name}

    assert contract.allows_guarded_tool_call(
        "issue_task_and_waitfor_task_output",
        _runner_call(contract, arguments),
    ) is True
    assert contract.allows_ingest_collection(caller_args) is True
    assert contract.allows_resolved_ingest(
        caller_args,
        **_authoritative_ingest_evidence(contract, source_filename=zip_name),
    ) is True


@pytest.mark.parametrize(
    "zip_name_template",
    (
        r"subdir\bloodhound_{token}.zip",
        "subdir/bloodhound_{token}.zip",
        r"..\bloodhound_{token}.zip",
        "../bloodhound_{token}.zip",
        r".\bloodhound_{token}.zip",
        "./bloodhound_{token}.zip",
        "/tmp/bloodhound_{token}.zip",
        r"C:\Users\Public\bloodhound_{token}.zip",
        r"C:bloodhound_{token}.zip",
        r"\\server\share\bloodhound_{token}.zip",
        "bloodhound_{token}.zip/child",
        "bloodhound_{token}.zip.bak",
        "bloodhound_{token}0.zip",
        "bloodhound_0{token}.zip",
        "2026072_bloodhound_{token}.zip",
        "202607221122334_bloodhound_{token}.zip",
    ),
)
@pytest.mark.parametrize("field", ("runner", "caller", "filemeta"))
def test_token_collection_filename_rejects_paths_traversal_and_near_matches(field, zip_name_template):
    contract = _resolved_contract()
    canonical_name = f"bloodhound_{contract.collection_token}.zip"
    zip_name = zip_name_template.format(token=contract.collection_token)

    if field == "runner":
        arguments = _canonical_arguments(contract).replace(canonical_name, zip_name)
        admitted = contract.allows_guarded_tool_call(
            "issue_task_and_waitfor_task_output",
            _runner_call(contract, arguments),
        )
    elif field == "caller":
        admitted = contract.allows_ingest_collection({
            "file_uuid": "file-123",
            "file_name": zip_name,
        })
    else:
        admitted = contract.allows_resolved_ingest(
            {"file_uuid": "file-123"},
            **_authoritative_ingest_evidence(contract, source_filename=zip_name),
        )

    assert admitted is False


def test_download_path_is_separately_bound_to_public_directory_and_valid_basename():
    contract = _resolved_contract()
    token = contract.collection_token
    valid_paths = (
        rf"C:\Users\Public\bloodhound_{token}.zip",
        rf"C:\Users\Public\20260722_bloodhound_{token}.zip",
    )
    invalid_paths = (
        rf"C:\Windows\Temp\bloodhound_{token}.zip",
        rf"C:\Users\Public\subdir\bloodhound_{token}.zip",
        rf"C:\Users\Public\..\bloodhound_{token}.zip",
        rf"\\server\share\bloodhound_{token}.zip",
        f"/tmp/bloodhound_{token}.zip",
    )

    for path in valid_paths:
        assert contract.allows_mythic_task(
            contract.collection_profile.download_command,
            {contract.collection_profile.download_path_param: path},
            callback_display_id=7,
        ) is True
    for path in invalid_paths:
        assert contract.allows_mythic_task(
            contract.collection_profile.download_command,
            {contract.collection_profile.download_path_param: path},
            callback_display_id=7,
        ) is False


@pytest.mark.parametrize("forest", ("", "unknown", "(unknown forest)"))
def test_unknown_forest_allows_supported_search_forest_but_never_caller_supplied_domain(forest):
    contract = _resolved_contract(forest=forest)
    arguments = _canonical_arguments(contract)

    assert "--SearchForest" in arguments
    assert contract.allows_guarded_tool_call(
        "issue_task_and_waitfor_task_output",
        _runner_call(contract, arguments),
    ) is True
    assert contract.allows_guarded_tool_call(
        "issue_task_and_waitfor_task_output",
        _runner_call(contract, arguments.replace("--SearchForest", "--Domain victim.external")),
    ) is False
    assert contract.allows_ingest_collection({
        "file_uuid": "file-123",
        "collection_scope_domain": "victim.external",
    }) is False


def test_known_forest_ingest_scope_requires_exact_domain_and_rejects_suffix_collision():
    contract = _resolved_contract(forest="branch.local")

    assert contract.allows_ingest_collection({
        "file_uuid": "file-123",
        "collection_scope_domain": "BRANCH.LOCAL",
    }) is True
    assert contract.allows_ingest_collection({
        "file_uuid": "file-123",
        "collection_scope_domain": "zeta.branch.local",
    }) is False
    assert contract.allows_ingest_collection({
        "file_uuid": "file-123",
        "collection_scope_domain": "local",
    }) is False


def test_profile_without_search_forest_support_needs_a_resolved_forest():
    unresolved = _resolved_contract(
        forest="",
        adapter={"collection_search_forest_supported": False},
    )
    resolved = _resolved_contract(
        forest="corp.local",
        adapter={"collection_search_forest_supported": False},
    )

    assert unresolved.scope_resolution == "unresolved"
    arguments = _canonical_arguments(resolved)
    assert "--Domain corp.local" in arguments
    assert "--SearchForest" not in arguments
    assert resolved.allows_guarded_tool_call(
        "issue_task_and_waitfor_task_output",
        _runner_call(resolved, arguments),
    ) is True


def test_registered_assembly_selector_cannot_switch_name_or_upload_group():
    contract = _resolved_contract()
    arguments = _canonical_arguments(contract)
    wrong_name = _runner_call(contract, arguments)
    wrong_name["parameters"]["assembly_name"] = "Rubeus.exe"
    upload_group = {
        "command": "execute_assembly",
        "parameters": {"assembly_file": "uuid-1", "assembly_arguments": arguments},
        "callback_display_id": 7,
    }

    assert contract.allows_guarded_tool_call("issue_task_and_waitfor_task_output", wrong_name) is False
    assert contract.allows_guarded_tool_call("issue_task_and_waitfor_task_output", upload_group) is False


def test_apollo_auth_preflights_collection_and_artifact_tasks_are_bound_to_one_callback_without_token_override():
    contract = _resolved_contract()
    token = contract.collection_token
    allowed = (
        ("whoami", ""),
        ("ticket_cache_list", {"luid": "", "getSystemTickets": False}),
        ("rev2self", ""),
        ("ls", {"path": r"C:\Users\Public"}),
        ("download", {"path": rf"C:\Users\Public\20260722_bloodhound_{token}.zip"}),
    )
    for command, parameters in allowed:
        assert contract.allows_mythic_task(
            command,
            parameters,
            callback_display_id=7,
        ), command

    assert contract.allows_mythic_task("whoami", "", callback_display_id=8) is False
    assert contract.allows_mythic_task("whoami", "", callback_display_id=7, token_id=3) is False
    assert contract.allows_mythic_task("shell", "whoami", callback_display_id=7) is False
    assert contract.allows_mythic_task(
        "download",
        {"path": r"C:\Users\Public\secret.zip"},
        callback_display_id=7,
    ) is False


def test_merlin_profile_admits_only_its_existing_collection_schema():
    adapter_mod = importlib.import_module("ai.langgraph.mythic_capability_adapter")
    adapter = adapter_mod.collection_adapter_for_payload_type("merlin")
    contract = _resolved_contract(payload_type="merlin", adapter=adapter)
    task = contract.to_payload()["collection_task_spec"]["preferred_collection_task"]

    assert task["command"] == "execute-assembly"
    assert set(task["parameters"]) == {"filename", "arguments"}
    assert contract.allows_guarded_tool_call(
        "issue_task_and_waitfor_task_output",
        {**task, "callback_display_id": 7},
    ) is True
    assert contract.allows_mythic_task(
        "token",
        {"method": "whoami"},
        callback_display_id=7,
    ) is True
    assert contract.allows_mythic_task("rev2Self", "", callback_display_id=7) is True
    assert contract.allows_mythic_task(
        "download",
        {"file": rf"C:\Users\Public\20260722_bloodhound_{contract.collection_token}.zip"},
        callback_display_id=7,
    ) is True


def test_ingest_accepts_uuid_or_token_filtered_callback_and_sink_links_authoritative_filemeta():
    contract = _resolved_contract()
    token = contract.collection_token
    uuid_args = {
        "file_uuid": "file-123",
        "callback_display_id": 7,
        "file_name": f"20260722_bloodhound_{token}.zip",
    }
    callback_args = {"callback_display_id": 7, "name_contains": token}

    assert contract.allows_ingest_collection(uuid_args) is True
    assert contract.allows_ingest_collection({"file_uuid": "file-123", "callback_display_id": 7}) is True
    assert contract.allows_ingest_collection({"file_uuid": "file-123"}) is True
    assert contract.allows_ingest_collection(callback_args) is True
    assert contract.allows_ingest_collection({"callback_display_id": 7}) is False
    assert contract.allows_ingest_collection({"callback_display_id": 7, "name_contains": "zip"}) is False
    assert contract.allows_ingest_collection({**uuid_args, "callback_display_id": 8}) is False
    assert contract.allows_ingest_collection({
        **callback_args,
        "file_name": "unrelated.zip",
    }) is False
    assert contract.allows_resolved_ingest(
        {"file_uuid": "file-123"},
        **_authoritative_ingest_evidence(contract),
    ) is True
    assert contract.allows_resolved_ingest(
        uuid_args,
        **_authoritative_ingest_evidence(contract),
    ) is True
    assert contract.allows_resolved_ingest(
        uuid_args,
        **_authoritative_ingest_evidence(contract, source_callback_display_id=8),
    ) is False
    assert contract.allows_resolved_ingest(
        uuid_args,
        **_authoritative_ingest_evidence(contract, source_filename="unrelated.zip"),
    ) is False


@pytest.mark.parametrize(
    "overrides",
    (
        {"source_command": "shell"},
        {"source_is_download_from_agent": False},
        {"source_task_display_id": None},
        {"source_complete": False},
        {"source_deleted": True},
    ),
)
def test_final_ingest_rejects_invalid_authoritative_download_metadata(overrides):
    contract = _resolved_contract()

    assert contract.allows_resolved_ingest(
        {"file_uuid": "file-123"},
        **_authoritative_ingest_evidence(contract, **overrides),
    ) is False


def test_open_ended_admin_objective_preserves_controller_capability_and_task_freedom():
    mod = _load_contract()
    contract = mod.compile_objective_contract(
        "Obtain administrative control of corp.local.",
        stored_objective_trigger=False,
        objective_is_open_ended=True,
    )

    assert contract.scope_kind == "open_ended"
    assert contract.engine == "controller"
    assert contract.approval_policy == "controller_hitl"
    assert contract.preserves_capability_freedom is True
    assert contract.allows_capability("gpo-controlled-system-exec") is True
    assert contract.allows_guarded_tool(
        "execute_capability",
        capability_name="gpo-controlled-system-exec",
    ) is True
    assert contract.allows_mythic_task(
        "execute_assembly",
        {"assembly_name": "Rubeus.exe", "assembly_arguments": "triage"},
    ) is True


@pytest.mark.parametrize(
    ("objective", "expected_scope", "expected_outcomes"),
    (
        (
            "Collect and ingest the graph, but do not read available credentials.",
            "bounded_report",
            ("graph_ingested",),
        ),
        (
            "Collect and ingest the graph; read credentials, but do not report credentials.",
            "bounded_report",
            ("graph_ingested",),
        ),
        (
            "Do not collect or ingest the graph; only read available credentials.",
            "unclassified",
            (),
        ),
        ("Never collect and ingest the graph.", "unclassified", ()),
        ("Without collecting the graph, read available credentials.", "unclassified", ()),
        ("Explain how to collect and ingest the graph.", "unclassified", ()),
        ("Please explain how to collect and ingest the graph.", "unclassified", ()),
        ("Could you explain how to collect and ingest the graph?", "unclassified", ()),
        ("Summarize collecting the graph and reading credentials.", "unclassified", ()),
        ("How would Sage collect and ingest the graph?", "unclassified", ()),
        ("Collect and ingest the graph.", "bounded_report", ("graph_ingested",)),
        (
            "Collect and ingest the graph, then read available credentials.",
            "bounded_report",
            ("graph_ingested", "credentials_reported"),
        ),
    ),
)
def test_objective_compiler_negation_information_and_positive_near_controls(
    objective,
    expected_scope,
    expected_outcomes,
):
    contract = _load_contract().compile_objective_contract(
        objective,
        stored_objective_trigger=True,
        objective_is_open_ended=True,
    )

    assert contract.scope_kind == expected_scope
    assert contract.required_outcomes == expected_outcomes


@pytest.mark.parametrize(
    ("objective", "expected_scope", "expected_outcomes"),
    (
        ("You should not collect and ingest the graph.", "unclassified", ()),
        ("You must not collect and ingest the graph.", "unclassified", ()),
        ("You may not collect and ingest the graph.", "unclassified", ()),
        ("Read credentials except collect and ingest the graph.", "unclassified", ()),
        ("Collect not the graph.", "unclassified", ()),
        ("Collect and ingest not the graph.", "unclassified", ()),
        (
            "Collect and ingest the graph, but read no credentials.",
            "bounded_report",
            ("graph_ingested",),
        ),
        (
            "Collect and ingest the graph, but read credentials should not be done.",
            "bounded_report",
            ("graph_ingested",),
        ),
        (
            "Collect and ingest the graph, but read credentials except admin.",
            "bounded_report",
            ("graph_ingested",),
        ),
    ),
)
def test_objective_compiler_fails_closed_for_deontic_subordinate_and_postpositive_negation(
    objective,
    expected_scope,
    expected_outcomes,
):
    contract = _load_contract().compile_objective_contract(
        objective,
        stored_objective_trigger=True,
        objective_is_open_ended=True,
    )

    assert contract.scope_kind == expected_scope
    assert contract.required_outcomes == expected_outcomes


@pytest.mark.parametrize(
    ("objective", "expected_outcomes"),
    (
        ("Collect and ingest the graph after discovery.", ("graph_ingested",)),
        (
            "Collect and ingest the graph, then read available credentials after ingest.",
            ("graph_ingested", "credentials_reported"),
        ),
        (
            "Collect and ingest the graph, then read available credentials with filters removed.",
            ("graph_ingested", "credentials_reported"),
        ),
    ),
)
def test_objective_compiler_preserves_positive_suffix_near_controls(objective, expected_outcomes):
    contract = _load_contract().compile_objective_contract(
        objective,
        stored_objective_trigger=True,
        objective_is_open_ended=True,
    )

    assert contract.scope_kind == "bounded_report"
    assert contract.required_outcomes == expected_outcomes


def test_stored_open_ended_objective_keeps_supervisor_graph_engine():
    mod = _load_contract()
    contract = mod.compile_objective_contract(
        "Obtain administrative control of corp.local.",
        stored_objective_trigger=True,
        objective_is_open_ended=True,
    )

    assert contract.scope_kind == "open_ended"
    assert contract.engine == "supervisor_graph"
    assert contract.approval_policy == "turn_hitl"
    assert contract.allows_capability("gpo-controlled-system-exec") is True
