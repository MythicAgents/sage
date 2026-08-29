"""Offline tests for the true-naive raw Mythic toolset + the upgraded bare runner loop.

The preflight validates exact live GraphQL documents and installed SDK signatures. These tests pin
the tool schema set, shipping parity, dispatch routing, and runner behavior.
"""
import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import bare_mythic_tools as bmt  # noqa: E402
import bare_runner  # noqa: E402


def test_bare_tool_specs_are_mythic_not_commands():
    names = {s["function"]["name"] for s in bmt.bare_tool_specs()}
    # Raw Mythic discovery + tasking are present...
    assert {"list_callbacks", "get_payload_types", "get_commands", "issue_command", "get_task_history",
            "get_task_output", "get_uploaded_files",
            "read_credentials", "add_credential", "get_all_payload_info",
            "get_c2_profiles_for_payload", "get_callback_c2_config",
            "get_payload_c2_config", "ingest_collection"} <= names
    # ...and Apollo commands are NOT tools anymore (the model discovers them via get_commands).
    assert "mimikatz" not in names and "shell" not in names and "powerpick" not in names
    for s in bmt.bare_tool_specs():
        assert s["type"] == "function"
        assert s["function"]["name"] and "parameters" in s["function"]


def test_raw_mythic_surface_is_a_strict_shipping_subset_with_no_exceptions():
    disposition = bmt.raw_mythic_parity_disposition()
    assert disposition["valid"] is True
    assert disposition["raw_count"] == len(bmt.TOOLS) == 18
    assert disposition["raw_count"] <= disposition["shipping_count"]
    assert disposition["exceptions"] == {}
    assert {"download_file", "download_payload"}.isdisjoint(bmt.TOOLS)
    assert bmt.shipping_visible_mythic_tool_names() == bmt.SHIPPING_MYTHIC_TOOL_NAMES


def test_raw_mythic_parity_guard_rejects_an_added_tool(monkeypatch):
    monkeypatch.setitem(bmt.TOOLS, "raw_only_extra", {"fn": None})
    disposition = bmt.raw_mythic_parity_disposition()
    assert disposition["valid"] is False
    assert disposition["undeclared"] == ["raw_only_extra"]


def test_raw_mythic_parity_guard_rejects_missing_shipping_capability():
    shipping = set(bmt.SHIPPING_MYTHIC_TOOL_NAMES) - {"read_credentials"}
    disposition = bmt.raw_mythic_parity_disposition(shipping)
    assert disposition["valid"] is False
    assert disposition["missing_shipping"] == {"read_credentials": "read_credentials"}


def test_bidirectional_reverse_audit_is_exhaustive_and_implemented():
    disposition = bmt.raw_mythic_bidirectional_disposition()
    assert disposition == {
        "valid": True,
        "shipping_count": 31,
        "classified_count": 31,
        "counts": {
            "ADD_RAW": 5,
            "ALREADY_COMPOSABLE": 4,
            "ALREADY_DIRECT": 13,
            "DO_NOT_ADD_TREATMENT": 9,
        },
        "failures": {},
    }


def test_bidirectional_guard_rejects_missing_required_raw_coverage(monkeypatch):
    removed = bmt.TOOLS.pop("get_payload_c2_config")
    try:
        disposition = bmt.raw_mythic_bidirectional_disposition()
    finally:
        bmt.TOOLS["get_payload_c2_config"] = removed
    assert disposition["valid"] is False
    assert disposition["failures"]["get_payload_c2_config"] == (
        "missing raw coverage ['get_payload_c2_config']"
    )


def test_installed_mythic_sdk_matches_every_runtime_wrapper_contract():
    disposition = bmt.raw_mythic_sdk_disposition()
    assert disposition == {
        "valid": True,
        "checked": len(bmt.RAW_MYTHIC_SDK_CONTRACT),
        "failures": {},
    }


def test_sdk_guard_catches_async_generator_treated_as_coroutine():
    class FakeMythic:
        pass

    for name in bmt.RAW_MYTHIC_SDK_CONTRACT:
        async def ordinary(**_kwargs):
            return None
        setattr(FakeMythic, name, ordinary)

    # This is the exact class of the live get_all_uploaded_files defect.
    async def wrong_coroutine(mythic):
        return []
    FakeMythic.get_all_uploaded_files = wrong_coroutine

    disposition = bmt.raw_mythic_sdk_disposition(FakeMythic)
    assert disposition["valid"] is False
    assert disposition["failures"]["get_all_uploaded_files"] == (
        "expected async_generator, observed coroutine"
    )


def test_uploaded_files_consumes_installed_sdk_async_generator(monkeypatch):
    from mythic import mythic

    async def rows(*, mythic):
        yield {"agent_file_id": "one"}
        yield {"agent_file_id": "two"}

    monkeypatch.setattr(mythic, "get_all_uploaded_files", rows)
    assert json.loads(asyncio.run(bmt._get_uploaded_files(object()))) == [
        {"agent_file_id": "one"},
        {"agent_file_id": "two"},
    ]


def _raw_graphql_introspection():
    from graphql import build_schema, introspection_from_schema

    schema = build_schema("""
        scalar jsonb
        enum order_by { asc desc }
        type buildparameter {
          id: Int
          name: String
          parameter_type: String
          choices: String
          default_value: String
          description: String
        }
        type payloadtype {
          name: String
          agent_type: String
          supported_os: [String!]
          buildparameters: [buildparameter!]
        }
        type c2profileparameter {
          name: String
          description: String
          parameter_type: String
          required: Boolean
          default_value: String
          choices: String
        }
        type c2profile {
          name: String
          description: String
          is_p2p: Boolean
          c2profileparameters: [c2profileparameter!]
        }
        type payloadtypec2profile { payloadtype: payloadtype, c2profile: c2profile }
        type c2profileparametersinstance {
          value: String
          instance_name: String
          c2profileparameter: c2profileparameter
          c2profile: c2profile
        }
        type callback {
          display_id: Int
          host: String
          user: String
          c2profileparametersinstances: [c2profileparametersinstance!]
        }
        type task { display_id: Int, command_name: String, callback: callback }
        type filemeta {
          agent_file_id: String
          filename_utf8: String
          path: String
          timestamp: String
          complete: Boolean
          deleted: Boolean
          is_payload: Boolean
          is_download_from_agent: Boolean
          task: task
        }
        type credential {
          account: String
          realm: String
          type: String
          credential_text: String
          metadata: jsonb
        }
        input String_comparison_exp { _eq: String }
        input Int_comparison_exp { _eq: Int }
        input Boolean_comparison_exp { _eq: Boolean }
        input payloadtype_bool_exp { name: String_comparison_exp }
        input payloadtypec2profile_bool_exp { payloadtype: payloadtype_bool_exp }
        input callback_display_bool_exp { display_id: Int_comparison_exp }
        input task_callback_bool_exp { callback: callback_display_bool_exp }
        input filemeta_bool_exp {
          complete: Boolean_comparison_exp
          deleted: Boolean_comparison_exp
          is_payload: Boolean_comparison_exp
          is_download_from_agent: Boolean_comparison_exp
          task: task_callback_bool_exp
        }
        input filemeta_order_by { id: order_by }
        input payload_bool_exp { uuid: String_comparison_exp }
        input callback_payload_bool_exp { uuid: String_comparison_exp }
        input callback_bool_exp { payload: callback_payload_bool_exp }
        type payload { uuid: String, deleted: Boolean }
        type aggregate_fields { count: Int }
        type callback_aggregate_fields { aggregate: aggregate_fields }
        type update_payload_result { status: String, error: String }
        type Query {
          payloadtype: [payloadtype!]!
          payloadtypec2profile(where: payloadtypec2profile_bool_exp): [payloadtypec2profile!]!
          callback(where: callback_display_bool_exp): [callback!]!
          filemeta(where: filemeta_bool_exp, order_by: filemeta_order_by, limit: Int): [filemeta!]!
          credential: [credential!]!
          payload(where: payload_bool_exp): [payload!]!
          callback_aggregate(where: callback_bool_exp): callback_aggregate_fields
        }
        type Mutation {
          updatePayload(payload_uuid: String!, deleted: Boolean!): update_payload_result
        }
    """)
    return introspection_from_schema(schema, descriptions=False)


def test_exact_runtime_graphql_documents_validate_as_one_closed_set(monkeypatch):
    from mythic import mythic

    async def introspect(_client, _query, variables=None):
        return _raw_graphql_introspection()

    monkeypatch.setattr(mythic, "execute_custom_query", introspect)
    disposition = asyncio.run(bmt.raw_mythic_graphql_disposition(object()))
    assert disposition == {
        "valid": True,
        "checked": len(bmt.RAW_MYTHIC_GRAPHQL_DOCUMENTS),
        "failures": {},
    }


def test_graphql_guard_rejects_a_planted_missing_field(monkeypatch):
    from mythic import mythic

    async def introspect(_client, _query, variables=None):
        return _raw_graphql_introspection()

    monkeypatch.setattr(mythic, "execute_custom_query", introspect)
    monkeypatch.setitem(
        bmt.RAW_MYTHIC_GRAPHQL_DOCUMENTS,
        "planted_drift",
        "query Planted { credential { credential_type } }",
    )
    disposition = asyncio.run(bmt.raw_mythic_graphql_disposition(object()))
    assert disposition["valid"] is False
    assert "credential_type" in disposition["failures"]["planted_drift"][0]


def test_excluded_secret_sauce_not_exposed():
    names = set(bmt.TOOLS)
    for sauce in ("get_ttp_guidance", "get_ttp_full_reference", "list_ttp_categories",
                  "execute_capability", "build_capability_commands",
                  "check_callback_alive", "assess_callback_liveness", "download_tool"):
        assert sauce not in names, f"{sauce} is harness secret sauce and must not reach the bare model"
    for local_surface in ("list_tools_folder", "register_tool", "upload_file_to_target", "sandbox_exec"):
        assert local_surface not in names, f"{local_surface} exposes Sage-local assets or helpers"
    assert "list_callbacks" in names   # raw callback listing IS provided (fixes the cb-pivot gap)


def _collection_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("users.json", '{"data": [{"name": "EXAMPLE"}]}')
    return out.getvalue()


def test_new_metadata_wrappers_execute_exact_runtime_calls(monkeypatch):
    from mythic import mythic
    calls = []

    async def query(_client, document, variables=None):
        calls.append((document, variables))
        return {"ok": True}

    async def payload_by_uuid(*, mythic, payload_uuid, custom_return_attributes):
        calls.append((payload_uuid, custom_return_attributes))
        return {"uuid": payload_uuid, "c2profileparametersinstances": []}

    monkeypatch.setattr(mythic, "execute_custom_query", query)
    monkeypatch.setattr(mythic, "get_payload_by_uuid", payload_by_uuid)
    assert json.loads(asyncio.run(bmt._get_all_payload_info(object()))) == {"ok": True}
    assert json.loads(asyncio.run(bmt._get_c2_profiles_for_payload(object(), "apollo"))) == {"ok": True}
    assert json.loads(asyncio.run(bmt._get_callback_c2_config(object(), 7))) == {"ok": True}
    payload = json.loads(asyncio.run(bmt._get_payload_c2_config(object(), "payload-1")))
    assert payload["uuid"] == "payload-1"
    assert any(variables == {"payload_type_name": "apollo"} for _, variables in calls if isinstance(variables, dict))
    assert any(variables == {"id": 7} for _, variables in calls if isinstance(variables, dict))


def test_ingest_collection_dispatches_real_zip_bytes_without_exposing_them(monkeypatch):
    from mythic import mythic
    blob = _collection_zip()
    received = {}

    async def download(*, mythic, file_uuid):
        assert file_uuid == "file-1"
        return blob

    async def ingest(name, data):
        received.update(name=name, data=data)
        return {"job_id": 41, "status_message": "Running"}

    monkeypatch.setattr(mythic, "download_file", download)
    dispatcher = bmt.make_mythic_dispatcher(object(), collection_ingestor=ingest)
    result = json.loads(dispatcher({"tool": "ingest_collection", "args": {"file_uuid": "file-1"}}))
    assert received == {"name": "file-1.zip", "data": blob}
    assert result["source"]["bytes"] == len(blob)
    assert result["bloodhound"]["job_id"] == 41
    assert "UEsDB" not in json.dumps(result)


def test_ingest_collection_resolves_latest_matching_callback_file(monkeypatch):
    from mythic import mythic
    blob = _collection_zip()

    async def query(_client, document, variables=None):
        assert document == bmt._LATEST_COLLECTION_QUERY
        assert variables == {"cbid": 9}
        return {"filemeta": [
            {
                "agent_file_id": "newest", "filename_utf8": "notes.txt", "path": "/files/notes",
                "complete": True, "deleted": False, "is_payload": False,
                "is_download_from_agent": True,
                "task": {"display_id": 12, "command_name": "download",
                         "callback": {"display_id": 9}},
            },
            {
                "agent_file_id": "collection", "filename_utf8": "forest.zip", "path": "/files/zip",
                "complete": True, "deleted": False, "is_payload": False,
                "is_download_from_agent": True,
                "task": {"display_id": 11, "command_name": "download",
                         "callback": {"display_id": 9}},
            },
        ]}

    async def download(*, mythic, file_uuid):
        assert file_uuid == "collection"
        return blob

    async def ingest(name, data):
        return {"job_id": 8}

    monkeypatch.setattr(mythic, "execute_custom_query", query)
    monkeypatch.setattr(mythic, "download_file", download)
    result = json.loads(asyncio.run(bmt._ingest_collection(
        object(), callback_display_id=9, collection_ingestor=ingest
    )))
    assert result["source"]["resolved_by"] == "callback:9"
    assert result["source"]["file_uuid"] == "collection"


def test_ingest_collection_rejects_ambiguous_missing_and_malformed_sources(monkeypatch):
    from mythic import mythic

    async def bad_download(*, mythic, file_uuid):
        return b"not-a-zip"

    async def ingest(_name, _data):
        raise AssertionError("invalid source must not reach BloodHound")

    monkeypatch.setattr(mythic, "download_file", bad_download)
    for kwargs in ({}, {"file_uuid": "x", "callback_display_id": 2}):
        try:
            asyncio.run(bmt._ingest_collection(object(), collection_ingestor=ingest, **kwargs))
        except ValueError as exc:
            assert "exactly one" in str(exc)
        else:
            raise AssertionError("ambiguous source was accepted")
    try:
        asyncio.run(bmt._ingest_collection(object(), file_uuid="x", collection_ingestor=ingest))
    except ValueError as exc:
        assert "collection ZIP" in str(exc)
    else:
        raise AssertionError("malformed collection was accepted")


def _collection_source_row(**updates):
    row = {
        "agent_file_id": "collection-file",
        "filename_utf8": "forest_collection.zip",
        "path": "/mythic/files/collection-file",
        "complete": True,
        "deleted": False,
        "is_payload": False,
        "is_download_from_agent": True,
        "task": {
            "display_id": 41,
            "command_name": "download",
            "callback": {"display_id": 7},
        },
    }
    row.update(updates)
    return row


def test_download_preflight_exercises_task_collection_sdk_path(monkeypatch):
    from mythic import mythic
    blob = _collection_zip()
    calls = []

    async def query(_client, document, variables=None):
        calls.append((document, variables))
        return {"filemeta": [_collection_source_row()]}

    async def ok_download(*, mythic, file_uuid):
        calls.append(("download", file_uuid))
        return blob

    monkeypatch.setattr(mythic, "execute_custom_query", query)
    monkeypatch.setattr(mythic, "download_file", ok_download)
    result = asyncio.run(bmt.raw_mythic_download_disposition(object()))
    assert result == {"valid": True, "checked": 1, "bytes_received": len(blob)}
    assert calls == [(bmt._DOWNLOAD_PROBE_QUERY, {}), ("download", "collection-file")]


def test_download_preflight_rejects_noncollection_source_class(monkeypatch):
    from mythic import mythic

    invalid_rows = [
        None,
        _collection_source_row(agent_file_id=""),
        _collection_source_row(filename_utf8="payload.exe"),
        _collection_source_row(path=""),
        _collection_source_row(complete=False),
        _collection_source_row(deleted=True),
        _collection_source_row(is_payload=True),
        _collection_source_row(is_download_from_agent=False),
        _collection_source_row(task=None),
        _collection_source_row(task={"display_id": 41, "command_name": "download", "callback": None}),
    ]

    async def download_must_not_run(**_kwargs):
        raise AssertionError("invalid source reached the official SDK download")

    monkeypatch.setattr(mythic, "download_file", download_must_not_run)
    for row in invalid_rows:
        async def query(_client, _document, variables=None, *, selected=row):
            return {"filemeta": [selected]}
        monkeypatch.setattr(mythic, "execute_custom_query", query)
        assert asyncio.run(bmt.raw_mythic_download_disposition(object())) == {
            "valid": False,
            "reason": "no valid task-produced collection file is available for download preflight",
        }

    async def omitted(_client, _document, variables=None):
        return {"filemeta": None}
    monkeypatch.setattr(mythic, "execute_custom_query", omitted)
    assert asyncio.run(bmt.raw_mythic_download_disposition(object())) == {
        "valid": False,
        "reason": "collection download preflight omitted the file list",
    }


def test_callback_collection_resolution_rejects_mismatched_provenance(monkeypatch):
    from mythic import mythic

    rows = [
        _collection_source_row(task={
            "display_id": 41, "command_name": "download", "callback": {"display_id": 8}
        }),
        _collection_source_row(path=""),
    ]

    async def query(_client, _document, variables=None):
        return {"filemeta": rows}

    monkeypatch.setattr(mythic, "execute_custom_query", query)
    try:
        asyncio.run(bmt._resolve_collection_source(
            object(), file_uuid="", callback_display_id=7, name_contains="zip"
        ))
    except LookupError as exc:
        assert "no completed non-deleted callback download" in str(exc)
    else:
        raise AssertionError("callback-mismatched collection source was accepted")


def test_download_preflight_rejects_empty_malformed_and_undownloadable_bytes(monkeypatch):
    from mythic import mythic

    async def query(_client, _document, variables=None):
        return {"filemeta": [_collection_source_row()]}

    monkeypatch.setattr(mythic, "execute_custom_query", query)
    invalid_blobs = [b"", b"not-a-zip"]
    empty_json_zip = io.BytesIO()
    with zipfile.ZipFile(empty_json_zip, "w") as archive:
        archive.writestr("users.json", "")
    invalid_blobs.append(empty_json_zip.getvalue())

    for blob in invalid_blobs:
        async def invalid_download(*, mythic, file_uuid, selected=blob):
            return selected
        monkeypatch.setattr(mythic, "download_file", invalid_download)
        assert asyncio.run(bmt.raw_mythic_download_disposition(object())) == {
            "valid": False,
            "reason": "download preflight did not return a valid collection ZIP",
        }

    async def broken(*, mythic, file_uuid):
        raise RuntimeError("redirect")

    monkeypatch.setattr(mythic, "download_file", broken)
    failed = asyncio.run(bmt.raw_mythic_download_disposition(object()))
    assert failed == {
        "valid": False,
        "reason": "task-produced collection download preflight failed (RuntimeError)",
    }


def test_dispatcher_unknown_tool_is_graceful():
    ex = bmt.make_mythic_dispatcher(client=None)
    assert "[unknown tool]" in ex({"tool": "does_not_exist", "args": {}})


def test_naive_profile_has_no_local_asset_path_symbol():
    assert bmt.BARE_TOOL_PROFILE == "naive-raw-mythic-bloodhound-v2"
    assert not hasattr(bmt, "TOOLS_DIR")
    prompt = bare_runner.DEFAULT_SYSTEM.casefold()
    assert "list_tools_folder" not in prompt
    assert "register_tool" not in prompt
    assert "upload_file_to_target" not in prompt
    assert "ttp" not in prompt


def test_runner_unlimited_runs_past_old_cap():
    """max_steps=0 means unlimited — it must run well past the old hardcoded 40-step cap."""
    calls = {"n": 0}
    def model_fn(system, tools, history):
        calls["n"] += 1
        return {"tool": "issue_command", "args": {}} if calls["n"] <= 45 else {"final": "done"}
    r = bare_runner.BareModelRunner(model_fn, lambda d: "obs", max_steps=0).run("obj")
    assert r.stopped == "done" and r.steps == 45


def test_runner_optional_step_cap_still_works():
    r = bare_runner.BareModelRunner(lambda s, t, h: {"tool": "x", "args": {}}, lambda d: "obs",
                                    max_steps=3).run("obj")
    assert r.stopped == "budget" and r.steps == 3


def test_runner_wallclock_timeout_is_not_a_step_cap():
    r = bare_runner.BareModelRunner(lambda s, t, h: {"tool": "x", "args": {}}, lambda d: "obs",
                                    max_steps=0, timeout=0).run("obj")
    assert r.stopped == "timeout" and r.steps == 0


def test_runner_live_logger_captures_action_and_observation():
    logs = []
    def model_fn(s, t, h):
        return {"final": "ok"} if any("ACTION" in m for m in logs) else {"tool": "list_callbacks", "args": {}}
    bare_runner.BareModelRunner(model_fn, lambda d: "OBS-DATA", max_steps=0, logger=logs.append).run("obj")
    assert any("BARE START" in m for m in logs)
    assert any("ACTION: list_callbacks" in m for m in logs)
    assert any("OBSERVATION" in m and "OBS-DATA" in m for m in logs)


def test_runner_graceful_interrupt_on_model_call():
    """Ctrl-C during model thinking -> stop with a partial transcript (the range still gets scored)."""
    def model_fn(s, t, h):
        raise KeyboardInterrupt()
    r = bare_runner.BareModelRunner(model_fn, lambda d: "obs", max_steps=0).run("obj")
    assert r.stopped == "interrupted"


def test_runner_graceful_interrupt_on_tool_call():
    def tool_exec(d):
        raise KeyboardInterrupt()
    r = bare_runner.BareModelRunner(lambda s, t, h: {"tool": "x", "args": {}}, tool_exec, max_steps=0).run("obj")
    assert r.stopped == "interrupted" and r.steps == 0
