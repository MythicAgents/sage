"""Assembled fake-control-plane coverage for enrolled executable invocation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ai.langgraph import command_builder, executable_contracts, mythic_tools
from ai.langgraph.mythic_tools import MythicTools


SAGE_ROOT = Path(__file__).resolve().parents[1]
CERTIFY_SOURCE = SAGE_ROOT / "tool_contracts" / "certify.json"


def _schema(*filenames: str):
    return [
        {
            "name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne",
            "choices": list(filenames), "parameter_group_name": "Default", "required": True,
        },
        {
            "name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
            "choices": [], "parameter_group_name": "Default", "required": False,
        },
        {
            "name": "assembly_file", "cli_name": "New Assembly", "type": "File",
            "choices": [], "parameter_group_name": "New Assembly", "required": True,
        },
        {
            "name": "assembly_arguments", "cli_name": "Arguments", "type": "String",
            "choices": [], "parameter_group_name": "New Assembly", "required": False,
        },
    ]


def _fixture(tmp_path: Path, monkeypatch, *, filenames=("Certify.exe",), contents=None):
    contents = contents or {name: b"same exact enrolled bytes" for name in filenames}
    enrolled_sha = hashlib.sha256(next(iter(contents.values()))).hexdigest()
    descriptor = json.loads(CERTIFY_SOURCE.read_text(encoding="utf-8"))
    descriptor["binary"]["sha256"] = enrolled_sha
    root = tmp_path / "contracts"
    root.mkdir()
    (root / "certify.json").write_text(json.dumps(descriptor), encoding="utf-8")
    monkeypatch.setattr(mythic_tools, "_EXECUTABLE_CONTRACT_ROOT", root)

    rows = {
        name: {
            "id": index + 1,
            "agent_file_id": f"00000000-0000-0000-0000-{index + 1:012d}",
            "filename_utf8": name,
            "md5": hashlib.md5(content).hexdigest(),
            "sha1": hashlib.sha1(content).hexdigest(),
            "complete": True,
        }
        for index, (name, content) in enumerate(contents.items())
    }
    by_uuid = {rows[name]["agent_file_id"]: content for name, content in contents.items()}

    client = MythicTools(agent_task_id="executable-integration")
    client.client = SimpleNamespace(http="https://", server_ip="mythic.test", server_port=443)
    client._resolve_payload_type = AsyncMock(return_value="apollo")
    client._authenticate_live_command = AsyncMock(
        return_value={"status": "available", "command": "execute_assembly"}
    )
    client._fetch_command_schema = AsyncMock(return_value=_schema(*filenames))
    client._fetch_command_metadata = AsyncMock(return_value={})
    client._find_uploaded_file_by_name = AsyncMock(side_effect=lambda name: copy.deepcopy(rows.get(name)))
    client._download_registered_file_bytes_bounded = AsyncMock(
        side_effect=lambda agent_file_id: by_uuid[agent_file_id]
    )
    client._callback_tasking_liveness_blocker = AsyncMock(return_value=None)
    client._validate_command_parameters = AsyncMock(return_value=None)
    return client, descriptor, root, rows, by_uuid


def test_discovery_emits_full_strict_schema_and_prepare_renders_native_parameters(tmp_path, monkeypatch):
    client, descriptor, _, _, _ = _fixture(tmp_path, monkeypatch)

    async def grouped(*_args, **_kwargs):
        return {"Default": {"parameters": _schema("Certify.exe")[:2]}}

    monkeypatch.setattr(mythic_tools.command_parameter_schema, "get_command_schema", grouped)
    result = json.loads(asyncio.run(client.get_all_command_args_for_payloadtype(
        "apollo", "execute_assembly", 7, "Certify.exe", "request"
    )))
    assert result["executable_contract"]["status"] == "enrolled"
    schema = client.get_enrolled_issue_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["command"]["enum"] == ["execute_assembly"]
    assert schema["properties"]["callback_display_id"]["enum"] == [7]
    parameters = schema["properties"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["Assembly"]["enum"] == ["Certify.exe"]
    invocation = parameters["properties"]["Arguments"]
    assert invocation["properties"]["verb"]["enum"] == ["request"]
    options = invocation["properties"]["options"]
    assert set(options["required"]) == set(options["properties"])
    assert options["properties"]["template"]["type"] == "string"
    assert {item["type"] for item in options["properties"]["output-pem"]["anyOf"]} == {
        "boolean", "null"
    }

    prepared = asyncio.run(client._prepare_supervised_issue_action({
        "command": "execute_assembly",
        "callback_display_id": 7,
        "parameters": {
            "Assembly": "Certify.exe",
            "Arguments": {
                "verb": "request",
                "options": {
                    **{key: None for key in options["properties"]},
                    "ca": r"SERVER\CA",
                    "template": "Template One",
                    "upn": ["one@example.test", "two@example.test"],
                    "output-pem": True,
                },
            },
        },
    }))
    assert prepared["parameters"] == {
        "Assembly": "Certify.exe",
        "Arguments": (
            'request --ca SERVER\\CA --template "Template One" --upn '
            "one@example.test two@example.test --key-size 2048 --output-pem"
        ),
    }
    binding = prepared["visibility_context"]["_sage_prepared_binding"]["executable"]
    assert binding["invocation"]["artifact_sha256"] == descriptor["binary"]["sha256"]
    assert binding["invocation"]["argv"][0] == "request"


@pytest.mark.parametrize(
    "parameters, expected",
    [
        ("Certify.exe enum-cas --filter-vulnerable", "enum-cas --filter-vulnerable"),
        ('{"Assembly":"Certify.exe","Arguments":"enum-cas --filter-vulnerable"}', None),
        ({
            "Assembly": "Certify.exe",
            "Arguments": {"verb": "request-download", "options": {"ca": r"S\C", "id": 4}},
        }, "request-download --ca S\\C --id 4"),
    ],
)
def test_raw_outer_json_string_and_typed_forms_normalize_before_shared_sink(
    tmp_path, monkeypatch, parameters, expected
):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    if isinstance(parameters, str) and parameters.startswith("{"):
        parameters = json.loads(parameters)
        expected = "enum-cas --filter-vulnerable"
    normalized, binding = asyncio.run(client._validated_executable_parameters(
        "execute_assembly", parameters, 7, _schema("Certify.exe")
    ))
    assert normalized["Assembly"] == "Certify.exe"
    assert normalized["Arguments"] == expected
    assert binding["invocation"]["argument_string"] == expected


@pytest.mark.parametrize(
    "parameters",
    [
        "Certify.exe find",
        "Certify.exe find ",
        "Certify.exe find\n",
        {"Assembly": "Certify.exe", "Arguments": "request --ca S\\C"},
        {"Assembly": "Certify.exe", "Arguments": "enum-cas", "other": "mixed"},
    ],
)
def test_known_raw_legacy_malicious_and_mixed_forms_fail_before_native_issue(tmp_path, monkeypatch, parameters):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    seen = {"issued": 0}

    async def fake_issue(*_args, **_kwargs):
        seen["issued"] += 1
        return {"display_id": 10}

    with patch.object(mythic_tools.mythic, "issue_task", fake_issue):
        result = asyncio.run(client.issue_task_and_waitfor_task_output(
            "execute_assembly", parameters, 7
        ))
    assert result.startswith("STOP") or "construction failure" in result.lower()
    assert seen["issued"] == 0


def test_extensionless_renamed_enrolled_bytes_validate_and_changed_recognized_bytes_fail(tmp_path, monkeypatch):
    exact = b"same exact enrolled bytes"
    client, _, root, _, _ = _fixture(
        tmp_path, monkeypatch,
        filenames=("Certify.exe", "alias"),
        contents={"Certify.exe": exact, "alias": exact},
    )
    normalized, binding = asyncio.run(client._validated_executable_parameters(
        "execute_assembly", {"Assembly": "alias", "Arguments": "enum-cas"}, 7,
        _schema("Certify.exe", "alias"),
    ))
    assert normalized == {"Assembly": "alias", "Arguments": "enum-cas"}
    assert binding["file"]["filename_utf8"] == "alias"

    changed = b"same name changed bytes"
    row = copy.deepcopy(asyncio.run(client._find_uploaded_file_by_name("Certify.exe")))
    client._download_registered_file_bytes_bounded = AsyncMock(return_value=changed)
    _assert_async_rejected(
        client._validated_executable_parameters(
            "execute_assembly", {"Assembly": "Certify.exe", "Arguments": "enum-cas"}, 7,
            _schema("Certify.exe", "alias"),
        ),
        "changed", "bytes",
    )


def _assert_async_rejected(coroutine, *needles):
    with pytest.raises((ValueError, executable_contracts.ExecutableContractError)) as caught:
        asyncio.run(coroutine)
    for needle in needles:
        assert needle in str(caught.value).lower()


def test_download_or_schema_failure_cannot_make_known_or_renamed_artifact_generic(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(
        tmp_path, monkeypatch, filenames=("Certify.exe", "renamed-tool")
    )
    client._authenticate_live_command = AsyncMock(
        side_effect=lambda command, _callback: {"status": "available", "command": command}
    )
    client._download_registered_file_bytes_bounded = AsyncMock(side_effect=ValueError("download failed"))
    _assert_async_rejected(
        client._validated_executable_parameters(
            "execute_assembly", "renamed-tool enum-cas", 7, _schema("renamed-tool")
        ), "download failed"
    )
    _assert_async_rejected(
        client._validated_executable_parameters(
            "execute_assembly", "Certify.exe enum-cas", 7, None
        ), "schema", "unavailable"
    )


def test_ambiguous_wrapper_cannot_hide_known_artifact_in_raw_or_structured_form(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    ambiguous = _schema("Certify.exe")
    ambiguous.append({
        "name": "extra_text", "cli_name": "Extra", "type": "String", "choices": [],
        "parameter_group_name": "Default", "required": False,
    })
    for parameters in (
        "Certify.exe enum-cas",
        {"Assembly": "Certify.exe", "Arguments": "enum-cas"},
    ):
        _assert_async_rejected(
            client._validated_executable_parameters(
                "execute_assembly", parameters, 7, ambiguous
            ), "ambiguous", "wrapper"
        )
def test_unknown_positive_artifact_preserves_generic_and_non_wrapper_is_unaffected(tmp_path, monkeypatch):
    client, _, _, rows, by_uuid = _fixture(tmp_path, monkeypatch)
    unknown = b"independently downloaded unknown binary"
    rows["other"] = {
        "id": 8, "agent_file_id": "00000000-0000-0000-0000-000000000008",
        "filename_utf8": "other", "complete": True,
    }
    by_uuid[rows["other"]["agent_file_id"]] = unknown
    client._find_uploaded_file_by_name = AsyncMock(side_effect=lambda name: copy.deepcopy(rows.get(name)))
    client._download_registered_file_bytes_bounded = AsyncMock(side_effect=lambda uuid: by_uuid[uuid])
    parameters = "other arbitrary --raw"
    assert asyncio.run(client._validated_executable_parameters(
        "execute_assembly", parameters, 7, _schema("other")
    )) == (parameters, None)
    client._fetch_command_schema = AsyncMock(return_value=None)
    assert asyncio.run(client._validated_executable_parameters("whoami", "", 7, None)) == ("", None)


def test_only_apollo_execute_and_inline_assembly_transport_are_enrolled(tmp_path, monkeypatch):
    client, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    client._resolve_payload_type = AsyncMock(return_value="merlin")
    _assert_async_rejected(
        client._validated_executable_parameters(
            "execute_assembly", {"Assembly": "Certify.exe", "Arguments": "enum-cas"}, 7,
            _schema("Certify.exe"),
        ), "only for apollo"
    )
    assert asyncio.run(client._validated_executable_parameters(
        "execute_pe", {"Assembly": "Certify.exe", "Arguments": "enum-cas"}, 7,
        _schema("Certify.exe"),
    ))[1] is None


def test_parser_output_is_construction_only_for_exact_enrolled_binding(tmp_path, monkeypatch):
    exact = b"same exact enrolled bytes"
    client, _, _, _, _ = _fixture(
        tmp_path, monkeypatch,
        filenames=("Certify.exe", "Alias.exe"),
        contents={"Certify.exe": exact, "Alias.exe": exact},
    )
    calls = {"issued": 0}

    async def fake_issue(*_args, **_kwargs):
        calls["issued"] += 1
        return {"display_id": calls["issued"]}

    async def fake_wait(*_args, **_kwargs):
        return "ERROR(S):\r\n  Required option 'unexpected' is missing.\r\n"

    with patch.object(mythic_tools.mythic, "issue_task", fake_issue), patch.object(
        mythic_tools.mythic, "waitfor_for_task_output", fake_wait
    ):
        first = asyncio.run(client.issue_task_and_waitfor_task_output(
            "execute_assembly", "Certify.exe enum-cas", 7
        ))
        second = asyncio.run(client.issue_task_and_waitfor_task_output(
            "inline_assembly", "Alias.exe   enum-cas", 7
        ))
    assert "required option" in first.lower()
    assert second.startswith("STOP — equivalent enrolled executable invocation")
    assert calls["issued"] == 1
    assert command_builder.classify_result(
        "execute_assembly", "Required option 'x' is missing."
    ) == "success"
    assert command_builder.enrolled_executable_parser_failure(
        "Required option 'x' is missing."
    )
