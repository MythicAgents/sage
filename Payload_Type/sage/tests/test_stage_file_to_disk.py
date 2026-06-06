import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage

from ai.langgraph import mythic_tools  # noqa: E402
from ai.langgraph.mythic_tools import MythicTools  # noqa: E402


def _client():
    tools = MythicTools("agent-task-id")
    tools.client = object()
    return tools


def test_resolve_supported_os_matches_case_and_reports_options(monkeypatch):
    execute = AsyncMock(return_value={"payloadtype": [{"supported_os": ["Windows"]}]})
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)
    tools = _client()

    assert asyncio.run(tools._resolve_supported_os("apollo", "windows")) == ("Windows", ["Windows"])
    assert asyncio.run(tools._resolve_supported_os("apollo", "linux")) == (None, ["Windows"])

    execute.return_value = {}
    assert asyncio.run(tools._resolve_supported_os("apollo", "windows")) == (None, None)


def test_create_payload_failure_returns_json_error_without_raising(monkeypatch):
    execute = AsyncMock(return_value={})
    create = AsyncMock(side_effect=Exception('null value found for non-nullable type: "String!"'))
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)
    monkeypatch.setattr(mythic_tools.mythic, "create_payload", create)

    result = json.loads(asyncio.run(_client().create_payload(
        payload_type_name="sage",
        filename="sage.bin",
        operating_system="linux",
        c2_profiles=[{"c2_profile": "http", "c2_profile_parameters": {}}],
        build_parameters=[],
    )))

    assert result["status"] == "error"
    assert result["tool"] == "create_payload"
    assert result["payload_type_name"] == "sage"
    assert result["filename"] == "sage.bin"
    assert result["hint"]


def test_create_payload_success_returns_payload_info(monkeypatch):
    execute = AsyncMock(return_value={})
    create = AsyncMock(return_value={
        "uuid": "payload-uuid",
        "filemetum": {"agent_file_id": "file-uuid"},
        "build_phase": "success",
    })
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)
    monkeypatch.setattr(mythic_tools.mythic, "create_payload", create)

    result = json.loads(asyncio.run(_client().create_payload(
        payload_type_name="sage",
        filename="sage.bin",
        operating_system="linux",
        c2_profiles=[{"c2_profile": "http", "c2_profile_parameters": {}}],
        build_parameters=[],
    )))

    assert result.get("status") != "error"
    assert result["uuid"] == "payload-uuid"
    assert result["payload_uuid"] == "payload-uuid"
    assert result["agent_file_id"] == "file-uuid"


def test_create_payload_normalizes_operating_system_to_supported_os(monkeypatch):
    execute = AsyncMock(return_value={"payloadtype": [{"supported_os": ["Windows"]}]})
    create = AsyncMock(return_value={
        "uuid": "payload-uuid",
        "filemetum": {"agent_file_id": "file-uuid"},
        "build_phase": "success",
    })
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)
    monkeypatch.setattr(mythic_tools.mythic, "create_payload", create)

    result = json.loads(asyncio.run(_client().create_payload(
        payload_type_name="apollo",
        filename="apollo.exe",
        operating_system="windows",
        c2_profiles=[{"c2_profile": "http", "c2_profile_parameters": {}}],
        build_parameters=[],
    )))

    assert result["uuid"] == "payload-uuid"
    assert create.await_args.kwargs["operating_system"] == "Windows"


def test_create_payload_rejects_unsupported_known_operating_system(monkeypatch):
    execute = AsyncMock(return_value={"payloadtype": [{"supported_os": ["Windows"]}]})
    create = AsyncMock(return_value={"uuid": "payload-uuid"})
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)
    monkeypatch.setattr(mythic_tools.mythic, "create_payload", create)

    result = json.loads(asyncio.run(_client().create_payload(
        payload_type_name="apollo",
        filename="apollo.exe",
        operating_system="Solaris",
        c2_profiles=[{"c2_profile": "http", "c2_profile_parameters": {}}],
        build_parameters=[],
    )))

    assert result["status"] == "error"
    assert "Windows" in result["hint"]
    create.assert_not_awaited()


def test_latest_download_for_callback_returns_newest_matching_row(monkeypatch):
    rows = [
        {"agent_file_id": "new-txt", "filename_utf8": "notes.txt", "timestamp": "2026-06-05T00:00:03Z"},
        {"agent_file_id": "new-zip", "filename_utf8": "essos_collection.ZIP", "timestamp": "2026-06-05T00:00:02Z"},
        {"agent_file_id": "old-zip", "filename_utf8": "old.zip", "timestamp": "2026-06-05T00:00:01Z"},
    ]
    execute = AsyncMock(return_value={"filemeta": rows})
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)

    result = asyncio.run(_client()._latest_download_for_callback(28, "zip"))

    assert result["agent_file_id"] == "new-zip"
    assert execute.await_args.kwargs["variables"] == {"cbid": 28}


def test_latest_download_for_callback_filter_and_empty_results(monkeypatch):
    execute = AsyncMock(return_value={"filemeta": [{"agent_file_id": "one", "filename_utf8": "notes.txt"}]})
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)

    assert asyncio.run(_client()._latest_download_for_callback(28, "zip")) is None

    execute.return_value = {"filemeta": []}
    assert asyncio.run(_client()._latest_download_for_callback(28, "")) is None


def test_stage_file_to_disk_with_uuid_stages_and_reports_filename(monkeypatch):
    download = AsyncMock(return_value=b"payload-bytes")
    execute = AsyncMock(side_effect=AssertionError("resolver should not be used for direct uuid"))
    monkeypatch.setattr(mythic_tools.mythic, "download_file", download)
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)

    result = json.loads(asyncio.run(_client().stage_file_to_disk(file_uuid="abc")))

    assert result["status"] == "staged"
    assert result["file_uuid"] == "abc"
    assert result["filename"] == "abc.zip"
    assert result["resolved_by"] == "uuid"
    assert Path(result["path"]).read_bytes() == b"payload-bytes"


def test_stage_file_to_disk_with_callback_resolves_uuid_and_stages(monkeypatch):
    row = {
        "agent_file_id": "resolved-uuid",
        "filename_utf8": "/var/lib/mythic/essos_collection.zip",
        "timestamp": "2026-06-05T00:00:00Z",
    }
    execute = AsyncMock(return_value={"filemeta": [row]})
    download = AsyncMock(return_value=b"zip-bytes")
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)
    monkeypatch.setattr(mythic_tools.mythic, "download_file", download)

    result = json.loads(asyncio.run(_client().stage_file_to_disk(callback_display_id=28)))

    assert result["status"] == "staged"
    assert result["file_uuid"] == "resolved-uuid"
    assert result["filename"] == "essos_collection.zip"
    assert result["resolved_by"].startswith("callback:")
    assert result["source_filename"] == row["filename_utf8"]
    assert result["timestamp"] == row["timestamp"]
    assert Path(result["path"]).read_bytes() == b"zip-bytes"


def test_stage_file_to_disk_without_uuid_or_callback_returns_error():
    result = json.loads(asyncio.run(_client().stage_file_to_disk()))

    assert result["status"] == "error"
    assert "file_uuid or callback" in result["error"]


def test_stage_file_to_disk_callback_empty_after_subscription_timeout_returns_download_first(monkeypatch):
    execute = AsyncMock(return_value={"filemeta": []})
    sleep = AsyncMock()
    download = AsyncMock(side_effect=AssertionError("download should not run without resolved filemeta"))

    async def timeout_subscription(*args, **kwargs):
        raise asyncio.TimeoutError
        yield

    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)
    monkeypatch.setattr(mythic_tools.mythic, "download_file", download)
    monkeypatch.setattr(mythic_tools.mythic, "subscribe_new_downloaded_files", timeout_subscription, raising=False)
    monkeypatch.setattr(mythic_tools.asyncio, "sleep", sleep)

    result = json.loads(asyncio.run(_client().stage_file_to_disk(callback_display_id=99)))

    assert result["status"] == "error"
    assert result["callback_display_id"] == 99
    assert "Run the Mythic `download`" in result["error"]
    assert execute.await_count == 2
    sleep.assert_not_awaited()


def test_stage_file_to_disk_callback_subscription_event_resolves_and_stages(monkeypatch):
    row = {
        "agent_file_id": "event-resolved-uuid",
        "filename_utf8": "/var/lib/mythic/event_collection.zip",
        "timestamp": "2026-06-05T00:00:01Z",
    }
    execute = AsyncMock(side_effect=[{"filemeta": []}, {"filemeta": [row]}])
    download = AsyncMock(return_value=b"event-zip-bytes")

    async def one_event_subscription(*args, **kwargs):
        yield [{"id": 1}]

    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute)
    monkeypatch.setattr(mythic_tools.mythic, "download_file", download)
    monkeypatch.setattr(mythic_tools.mythic, "subscribe_new_downloaded_files", one_event_subscription, raising=False)

    result = json.loads(asyncio.run(_client().stage_file_to_disk(callback_display_id=28)))

    assert result["status"] == "staged"
    assert result["file_uuid"] == "event-resolved-uuid"
    assert result["filename"] == "event_collection.zip"
    assert result["resolved_by"].startswith("callback:")
    assert result["source_filename"] == row["filename_utf8"]
    assert result["timestamp"] == row["timestamp"]
    assert Path(result["path"]).read_bytes() == b"event-zip-bytes"
    assert execute.await_count == 2
