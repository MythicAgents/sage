"""Offline tests for the true-naive raw Mythic toolset + the upgraded bare runner loop.

The preflight validates exact live GraphQL documents and installed SDK signatures. These tests pin
the tool schema set, shipping parity, dispatch routing, and runner behavior.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import bare_mythic_tools as bmt  # noqa: E402
import bare_runner  # noqa: E402


def test_bare_tool_specs_are_mythic_not_commands():
    names = {s["function"]["name"] for s in bmt.bare_tool_specs()}
    # Raw Mythic discovery + tasking are present...
    assert {"list_callbacks", "get_payload_types", "get_commands", "issue_command", "get_task_history",
            "get_task_output", "get_uploaded_files",
            "read_credentials", "add_credential"} <= names
    # ...and Apollo commands are NOT tools anymore (the model discovers them via get_commands).
    assert "mimikatz" not in names and "shell" not in names and "powerpick" not in names
    for s in bmt.bare_tool_specs():
        assert s["type"] == "function"
        assert s["function"]["name"] and "parameters" in s["function"]


def test_raw_mythic_surface_is_a_strict_shipping_subset_with_no_exceptions():
    disposition = bmt.raw_mythic_parity_disposition()
    assert disposition["valid"] is True
    assert disposition["raw_count"] == len(bmt.TOOLS) == 13
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
        type payloadtype { name: String }
        type credential {
          account: String
          realm: String
          type: String
          credential_text: String
          metadata: jsonb
        }
        input String_comparison_exp { _eq: String }
        input payload_bool_exp { uuid: String_comparison_exp }
        input callback_payload_bool_exp { uuid: String_comparison_exp }
        input callback_bool_exp { payload: callback_payload_bool_exp }
        type payload { uuid: String, deleted: Boolean }
        type aggregate_fields { count: Int }
        type callback_aggregate_fields { aggregate: aggregate_fields }
        type update_payload_result { status: String, error: String }
        type Query {
          payloadtype: [payloadtype!]!
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
                  "execute_capability", "build_capability_commands", "ingest_collection",
                  "check_callback_alive", "assess_callback_liveness", "download_tool"):
        assert sauce not in names, f"{sauce} is harness secret sauce and must not reach the bare model"
    for local_surface in ("list_tools_folder", "register_tool", "upload_file_to_target", "sandbox_exec"):
        assert local_surface not in names, f"{local_surface} exposes Sage-local assets or helpers"
    assert "list_callbacks" in names   # raw callback listing IS provided (fixes the cb-pivot gap)


def test_dispatcher_unknown_tool_is_graceful():
    ex = bmt.make_mythic_dispatcher(client=None)
    assert "[unknown tool]" in ex({"tool": "does_not_exist", "args": {}})


def test_naive_profile_has_no_local_asset_path_symbol():
    assert bmt.BARE_TOOL_PROFILE == "naive-raw-mythic-bloodhound-v1"
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
