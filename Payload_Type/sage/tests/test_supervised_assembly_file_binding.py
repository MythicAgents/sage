"""Enrolled executable bytes cannot bypass their CLI contract through inline File tasking."""

import asyncio
import copy
import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from ai.langgraph import mythic_tools
from tests.test_executable_invocation_integration import _fixture, _schema


def _inline_fixture(tmp_path, monkeypatch, *, filename="Certify.exe"):
    content = b"same exact enrolled bytes"
    client, descriptor, _root, rows, by_uuid = _fixture(
        tmp_path, monkeypatch, filenames=(filename,), contents={filename: content},
    )
    client._authenticate_live_command = AsyncMock(return_value={
        "status": "available", "command": "inline_assembly",
    })
    client._fetch_command_schema = AsyncMock(return_value=_schema(filename))
    row = rows[filename]
    metadata = {
        **row, "deleted": False, "is_payload": False, "is_download_from_agent": False,
    }
    client._get_file_metadata = AsyncMock(
        side_effect=lambda file_uuid: copy.deepcopy(metadata)
        if file_uuid == row["agent_file_id"] else None
    )
    client._download_registered_file_bytes_bounded = AsyncMock(
        side_effect=lambda file_uuid: by_uuid[file_uuid]
    )
    return client, descriptor, row, metadata, by_uuid


def _direct_issue(client, row, arguments):
    sink = AsyncMock(side_effect=AssertionError("enrolled File bypass reached task sink"))
    with patch.object(client, "_turn_authority_issue_blocker", return_value=""), patch.object(
        client, "_issue_mythic_task", sink,
    ):
        result = asyncio.run(client.issue_task_and_waitfor_task_output(
            "inline_assembly",
            {"assembly_file": row["agent_file_id"], "assembly_arguments": arguments},
            7,
        ))
    return result, sink


def test_named_invalid_verb_and_same_enrolled_bytes_by_file_both_fail_before_sink(
    tmp_path, monkeypatch,
):
    client, _descriptor, row, _metadata, _by_uuid = _inline_fixture(
        tmp_path, monkeypatch
    )
    with pytest.raises(Exception, match="not enrolled"):
        asyncio.run(client._prepare_supervised_issue_action({
            "command": "inline_assembly", "callback_display_id": 7,
            "parameters": {"assembly_name": "Certify.exe", "assembly_arguments": "forge"},
        }))

    result, sink = _direct_issue(client, row, "forge")
    assert "enrolled executable bytes must use the registered selector" in result
    sink.assert_not_awaited()


def test_valid_enrolled_bytes_file_route_directs_to_registered_typed_route(
    tmp_path, monkeypatch,
):
    client, _descriptor, row, _metadata, _by_uuid = _inline_fixture(
        tmp_path, monkeypatch
    )
    result, sink = _direct_issue(client, row, "enum-cas")
    assert "enrolled executable bytes must use the registered selector" in result
    sink.assert_not_awaited()


def test_renamed_enrolled_bytes_fail_before_file_sink(tmp_path, monkeypatch):
    client, _descriptor, row, _metadata, _by_uuid = _inline_fixture(
        tmp_path, monkeypatch, filename="Renamed.exe"
    )
    result, sink = _direct_issue(client, row, "find")
    assert "enrolled executable bytes must use the registered selector" in result
    sink.assert_not_awaited()


def test_recognized_filename_with_unknown_bytes_fails_before_file_sink(
    tmp_path, monkeypatch,
):
    client, descriptor, row, metadata, by_uuid = _inline_fixture(tmp_path, monkeypatch)
    unknown = b"changed unknown Certify bytes"
    metadata.update(
        md5=hashlib.md5(unknown).hexdigest(), sha1=hashlib.sha1(unknown).hexdigest()
    )
    by_uuid[row["agent_file_id"]] = unknown
    assert descriptor["binary"]["sha256"] != hashlib.sha256(unknown).hexdigest()

    result, sink = _direct_issue(client, row, "find")
    assert "recognized executable filename has changed or unsupported bytes" in result
    sink.assert_not_awaited()


def test_unknown_filename_and_unenrolled_bytes_reach_new_assembly_group(
    tmp_path, monkeypatch,
):
    client, descriptor, row, metadata, by_uuid = _inline_fixture(
        tmp_path, monkeypatch, filename="Unknown.exe"
    )
    unknown = b"unknown unmanaged assembly bytes"
    metadata.update(
        md5=hashlib.md5(unknown).hexdigest(), sha1=hashlib.sha1(unknown).hexdigest()
    )
    by_uuid[row["agent_file_id"]] = unknown
    assert descriptor["binary"]["sha256"] != hashlib.sha256(unknown).hexdigest()
    sink = AsyncMock(side_effect=RuntimeError("inert task sink reached"))

    with patch.object(client, "_turn_authority_issue_blocker", return_value=""), patch.object(
        client, "_issue_mythic_task", sink,
    ):
        asyncio.run(client.issue_task_and_waitfor_task_output(
            "inline_assembly",
            {"assembly_file": row["agent_file_id"], "assembly_arguments": "triage"},
            7,
        ))

    sink.assert_awaited_once()
    assert sink.call_args.args[:3] == (
        "inline_assembly",
        {"New Assembly": row["agent_file_id"], "Arguments": "triage"},
        7,
    )
    assert sink.call_args.kwargs["parameter_group_name"] == "New Assembly"
