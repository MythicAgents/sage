"""Read-only Mythic source adapter for operation-scoped assisted memory.

The adapter converts Mythic control-plane rows into inert ``SourceRecord``
values.  It has no callback-tasking, model, tool, or target-network surface.
"""

from __future__ import annotations

import base64
import binascii
import asyncio
from dataclasses import dataclass
import json
from typing import Any, Awaitable, Callable, Mapping

from .operation_memory import (
    IngestResult,
    OperationMemoryStore,
    ResourceDeferral,
    SourceRecord,
)


ExecuteQuery = Callable[[Any, str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class FileDownloadResult:
    """Lossless terminal state from the bounded default Mythic downloader."""

    status: str
    content: bytes | None
    observed_size: int | None

    def __post_init__(self) -> None:
        if self.status not in {
            "inlined",
            "unknown_length",
            "declared_oversize",
            "incomplete",
        }:
            raise ValueError("invalid bounded file-download status")
        if self.status == "inlined":
            if not isinstance(self.content, bytes) or self.observed_size != len(
                self.content
            ):
                raise ValueError("inlined download must retain its exact byte size")
        elif self.content is not None:
            raise ValueError("non-inlined download cannot carry content")
        if self.status == "declared_oversize" and (
            not isinstance(self.observed_size, int)
            or isinstance(self.observed_size, bool)
            or self.observed_size <= 0
        ):
            raise ValueError("declared oversize requires a positive observed size")


DownloadFile = Callable[
    [Any, str, int], Awaitable[bytes | None | FileDownloadResult]
]

STREAM_KEYS = ("callbacks", "tasks", "responses", "credentials", "files")
_INITIAL_TIMESTAMP = "1970-01-01T00:00:00Z"

_QUERIES = {
    "callbacks": """
        query SageMemoryCallbacks($op: Int!, $after_ts: timestamp!, $after_id: Int!, $limit: Int!) {
          callback(
            where: {operation_id: {_eq: $op}, _or: [
              {last_checkin: {_gt: $after_ts}},
              {_and: [{last_checkin: {_eq: $after_ts}}, {id: {_gt: $after_id}}]}
            ]},
            order_by: [{last_checkin: asc}, {id: asc}], limit: $limit
          ) {
            id operation_id display_id agent_callback_id init_callback last_checkin timestamp
            user host pid ip external_ip process_name description active integrity_level
            locked os architecture domain extra_info sleep_info
            payload { payloadtype { name } }
          }
        }
    """,
    "tasks": """
        query SageMemoryTasks($op: Int!, $after_ts: timestamp!, $after_id: Int!, $limit: Int!) {
          task(
            where: {operation_id: {_eq: $op}, _or: [
              {timestamp: {_gt: $after_ts}},
              {_and: [{timestamp: {_eq: $after_ts}}, {id: {_gt: $after_id}}]}
            ]},
            order_by: [{timestamp: asc}, {id: asc}], limit: $limit
          ) {
            id operation_id display_id agent_task_id timestamp command_name params original_params
            display_params status completed stdout stderr comment opsec_pre_blocked opsec_post_blocked
            operator { username }
            callback { display_id }
          }
        }
    """,
    "responses": """
        query SageMemoryResponses($op: Int!, $after_ts: timestamp!, $after_id: Int!, $limit: Int!) {
          response(
            where: {operation_id: {_eq: $op}, _or: [
              {timestamp: {_gt: $after_ts}},
              {_and: [{timestamp: {_eq: $after_ts}}, {id: {_gt: $after_id}}]}
            ]},
            order_by: [{timestamp: asc}, {id: asc}], limit: $limit
          ) {
            id operation_id timestamp response_text sequence_number
            task { display_id command_name callback { display_id } }
          }
        }
    """,
    "credentials": """
        query SageMemoryCredentials($op: Int!, $after_ts: timestamp!, $after_id: Int!, $limit: Int!) {
          credential(
            where: {operation_id: {_eq: $op}, _or: [
              {timestamp: {_gt: $after_ts}},
              {_and: [{timestamp: {_eq: $after_ts}}, {id: {_gt: $after_id}}]}
            ]},
            order_by: [{timestamp: asc}, {id: asc}], limit: $limit
          ) {
            id operation_id timestamp type account realm credential_text comment deleted metadata
            task { display_id command_name callback { display_id } }
          }
        }
    """,
    "files": """
        query SageMemoryFiles($op: Int!, $after_ts: timestamp!, $after_id: Int!, $limit: Int!) {
          filemeta(
            where: {operation_id: {_eq: $op}, _or: [
              {timestamp: {_gt: $after_ts}},
              {_and: [{timestamp: {_eq: $after_ts}}, {id: {_gt: $after_id}}]}
            ]},
            order_by: [{timestamp: asc}, {id: asc}], limit: $limit
          ) {
            id operation_id agent_file_id timestamp complete deleted is_payload is_screenshot
            is_download_from_agent filename_utf8 full_remote_path_utf8 host path md5 sha1 comment
            chunks_received total_chunks chunk_size
            task { display_id command_name callback { display_id } }
          }
        }
    """,
}

_ROW_KEYS = {
    "callbacks": "callback",
    "tasks": "task",
    "responses": "response",
    "credentials": "credential",
    "files": "filemeta",
}


class SourceBoundaryError(RuntimeError):
    """Mythic returned a row that violates the frozen operation/cursor seam."""


@dataclass(frozen=True, order=True)
class StreamCursor:
    timestamp: str = _INITIAL_TIMESTAMP
    record_id: int = 0

    def __post_init__(self) -> None:
        if not str(self.timestamp).strip():
            raise ValueError("cursor timestamp is required")
        if not isinstance(self.record_id, int) or isinstance(self.record_id, bool):
            raise ValueError("cursor record_id must be an integer")
        if self.record_id < 0:
            raise ValueError("cursor record_id cannot be negative")

    @classmethod
    def parse(cls, value: str | None) -> "StreamCursor":
        if not value:
            return cls()
        try:
            payload = json.loads(value)
            return cls(
                timestamp=str(payload["timestamp"]),
                record_id=int(payload["record_id"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid operation-memory stream cursor") from exc

    def encode(self) -> str:
        return json.dumps(
            {"record_id": self.record_id, "timestamp": self.timestamp},
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True)
class SourcePage:
    stream_key: str
    records: tuple[SourceRecord, ...]
    next_cursor: StreamCursor
    has_more: bool


@dataclass(frozen=True)
class StreamSyncResult:
    stream_key: str
    source_count: int
    has_more: bool
    cursor: str
    ingest: IngestResult


async def _default_execute_query(
    client: Any, query: str, variables: Mapping[str, Any]
) -> Mapping[str, Any]:
    from mythic import mythic

    response = await mythic.execute_custom_query(
        client, query, variables=dict(variables)
    )
    if not isinstance(response, Mapping):
        raise SourceBoundaryError("Mythic GraphQL response must be an object")
    return response


async def _default_download_result(
    client: Any, file_uuid: str, max_bytes: int
) -> FileDownloadResult:
    """Fetch only a Mythic response with a known, in-bound exact length.

    The public Mythic helper buffers the complete body before returning, which
    cannot enforce this boundary.  This stays on the same authenticated Mythic
    control-plane endpoint but refuses unknown/oversize lengths before reading.
    """
    import aiohttp
    from mythic import mythic_utilities

    url = f"{client.http}{client.server_ip}:{client.server_port}/direct/download/{file_uuid}"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers=mythic_utilities.get_headers(client),
            ssl=False,
        ) as response:
            response.raise_for_status()
            declared = response.content_length
            if declared is None:
                return FileDownloadResult("unknown_length", None, None)
            if declared > max_bytes:
                return FileDownloadResult("declared_oversize", None, declared)
            try:
                content = await response.content.readexactly(declared)
            except asyncio.IncompleteReadError:
                return FileDownloadResult("incomplete", None, declared)
            if len(content) != declared or not response.content.at_eof():
                return FileDownloadResult("incomplete", None, declared)
            return FileDownloadResult("inlined", content, declared)


async def _default_download_file(
    client: Any, file_uuid: str, max_bytes: int
) -> bytes | None:
    """Compatibility wrapper returning only in-bound bytes to direct callers."""
    return (await _default_download_result(client, file_uuid, max_bytes)).content


def _operation_id(value: Any) -> int:
    try:
        operation_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("operation_id must be a positive integer") from exc
    if isinstance(value, bool) or operation_id <= 0:
        raise ValueError("operation_id must be a positive integer")
    return operation_id


def _required_row_int(row: Mapping[str, Any], name: str) -> int:
    try:
        value = int(row[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceBoundaryError(f"Mythic row has invalid {name}") from exc
    if value < 0:
        raise SourceBoundaryError(f"Mythic row has invalid {name}")
    return value


def _required_timestamp(row: Mapping[str, Any]) -> str:
    timestamp = str(row.get("timestamp") or "").strip()
    if not timestamp:
        raise SourceBoundaryError("Mythic row has no timestamp")
    return timestamp


def _cursor_timestamp(row: Mapping[str, Any], stream_key: str) -> str:
    field = "last_checkin" if stream_key == "callbacks" else "timestamp"
    value = str(row.get(field) or "").strip()
    if not value:
        raise SourceBoundaryError(f"Mythic {stream_key} row has no {field}")
    return value


def _lineage(row: Mapping[str, Any]) -> tuple[str, str]:
    task = row.get("task") if isinstance(row.get("task"), Mapping) else {}
    callback = (
        task.get("callback")
        if isinstance(task.get("callback"), Mapping)
        else {}
    )
    return str(callback.get("display_id") or ""), str(task.get("display_id") or "")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _response_bytes(value: Any) -> tuple[bytes, str]:
    if isinstance(value, bytes):
        return value, "bytes"
    text = str(value or "")
    try:
        return base64.b64decode(text, validate=True), "base64"
    except (binascii.Error, ValueError):
        return text.encode("utf-8"), "utf8-fallback"


class MythicOperationMemorySource:
    """Page operation-scoped Mythic history without issuing callback work."""

    def __init__(
        self,
        client: Any,
        *,
        max_inline_text_bytes: int,
        execute_query: ExecuteQuery | None = None,
        download_file: DownloadFile | None = None,
    ) -> None:
        if max_inline_text_bytes <= 0:
            raise ValueError("max_inline_text_bytes must be positive")
        self.client = client
        self.max_inline_text_bytes = int(max_inline_text_bytes)
        self._execute_query = execute_query or _default_execute_query
        self._download_file = download_file or _default_download_result

    async def fetch_page(
        self,
        operation_id: Any,
        stream_key: str,
        *,
        cursor: StreamCursor | None = None,
        limit: int = 500,
    ) -> SourcePage:
        op = _operation_id(operation_id)
        if stream_key not in STREAM_KEYS:
            raise ValueError(f"unsupported operation-memory stream: {stream_key}")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        current = cursor or StreamCursor()
        response = await self._execute_query(
            self.client,
            _QUERIES[stream_key],
            {
                "op": op,
                "after_ts": current.timestamp,
                "after_id": current.record_id,
                "limit": limit + 1,
            },
        )
        rows = response.get(_ROW_KEYS[stream_key], [])
        if not isinstance(rows, list):
            raise SourceBoundaryError("Mythic GraphQL row collection must be a list")
        normalized: list[tuple[StreamCursor, Mapping[str, Any]]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise SourceBoundaryError("Mythic GraphQL row must be an object")
            row_op = _required_row_int(row, "operation_id")
            if row_op != op:
                raise SourceBoundaryError(
                    f"Mythic returned operation {row_op} while reading operation {op}"
                )
            row_cursor = StreamCursor(
                timestamp=_cursor_timestamp(row, stream_key),
                record_id=_required_row_int(row, "id"),
            )
            if row_cursor <= current:
                raise SourceBoundaryError("Mythic returned a row at or before the cursor")
            normalized.append((row_cursor, row))
        normalized.sort(key=lambda item: item[0])
        if len({item[0] for item in normalized}) != len(normalized):
            raise SourceBoundaryError("Mythic returned duplicate stream cursor values")
        has_more = len(normalized) > limit
        selected = normalized[:limit]
        records = tuple(
            [await self._map_row(op, stream_key, row) for _, row in selected]
        )
        next_cursor = selected[-1][0] if selected else current
        return SourcePage(stream_key, records, next_cursor, has_more)

    async def _map_row(
        self, operation_id: int, stream_key: str, row: Mapping[str, Any]
    ) -> SourceRecord:
        record_id = _required_row_int(row, "id")
        timestamp = _cursor_timestamp(row, stream_key)
        callback_id = task_id = output_id = ""
        metadata = dict(row)
        deferrals: list[ResourceDeferral] = []
        content_kind = "json"
        content = _canonical_json(row)
        record_class = stream_key.removesuffix("s")

        if stream_key == "callbacks":
            callback_id = str(row.get("display_id") or "")
        elif stream_key == "tasks":
            callback = row.get("callback") if isinstance(row.get("callback"), Mapping) else {}
            callback_id = str(callback.get("display_id") or "")
            task_id = str(row.get("display_id") or "")
        elif stream_key == "responses":
            callback_id, task_id = _lineage(row)
            output_id = str(record_id)
            content, encoding = _response_bytes(row.get("response_text"))
            content_kind = "text"
            metadata["source_encoding"] = encoding
            metadata.pop("response_text", None)
            record_class = "task_output"
        elif stream_key == "credentials":
            callback_id, task_id = _lineage(row)
        elif stream_key == "files":
            callback_id, task_id = _lineage(row)
            estimated_size = _required_row_int(row, "chunk_size") * _required_row_int(
                row, "total_chunks"
            )
            source_content_eligible = (
                bool(row.get("complete"))
                and not bool(row.get("deleted"))
                and bool(row.get("is_download_from_agent"))
                and estimated_size > 0
            )
            eligible = (
                source_content_eligible
                and estimated_size <= self.max_inline_text_bytes
            )
            metadata["content_fetch_eligible"] = eligible
            metadata["estimated_content_bytes"] = estimated_size
            if eligible:
                file_uuid = str(row.get("agent_file_id") or "").strip()
                if not file_uuid:
                    raise SourceBoundaryError("eligible Mythic file has no agent_file_id")
                try:
                    downloaded_result = await self._download_file(
                        self.client, file_uuid, self.max_inline_text_bytes
                    )
                except Exception as exc:
                    downloaded_result = None
                    metadata["content_fetch_status"] = (
                        f"error:{type(exc).__name__}"
                    )
                declared_oversize = False
                if isinstance(downloaded_result, FileDownloadResult):
                    metadata["content_fetch_status"] = downloaded_result.status
                    if downloaded_result.observed_size is not None:
                        metadata["observed_content_bytes"] = (
                            downloaded_result.observed_size
                        )
                    declared_oversize = (
                        downloaded_result.status == "declared_oversize"
                    )
                    downloaded = downloaded_result.content
                else:
                    downloaded = downloaded_result
                if declared_oversize:
                    observed_size = int(metadata["observed_content_bytes"])
                    deferrals.append(
                        ResourceDeferral(
                            bound_name="max_inline_text_bytes",
                            limit_value=self.max_inline_text_bytes,
                            observed_value=observed_size,
                            deferred_units=1,
                            detail=(
                                f"files:{record_id} content remains authoritative in "
                                "Mythic and requires explicit selection/rescan"
                            ),
                        )
                    )
                    content = _canonical_json(metadata)
                    content_kind = "json"
                elif downloaded is None:
                    metadata.setdefault("content_fetch_status", "not_inlined")
                    content = _canonical_json(metadata)
                    content_kind = "json"
                elif not isinstance(downloaded, bytes):
                    raise SourceBoundaryError("bounded Mythic file fetch must return bytes or None")
                elif len(downloaded) > self.max_inline_text_bytes:
                    metadata["content_fetch_status"] = "actual_oversize"
                    deferrals.append(
                        ResourceDeferral(
                            bound_name="max_inline_text_bytes",
                            limit_value=self.max_inline_text_bytes,
                            observed_value=len(downloaded),
                            deferred_units=1,
                            detail=(
                                f"files:{record_id} content remains authoritative in "
                                "Mythic and requires explicit selection/rescan"
                            ),
                        )
                    )
                    content = _canonical_json(metadata)
                    content_kind = "json"
                else:
                    content = downloaded
                    metadata["content_fetch_status"] = "inlined"
                    try:
                        content.decode("utf-8")
                    except UnicodeDecodeError:
                        content_kind = "binary"
                    else:
                        content_kind = "text"
            elif source_content_eligible:
                metadata["content_fetch_status"] = "estimated_oversize"
                deferrals.append(
                    ResourceDeferral(
                        bound_name="max_inline_text_bytes",
                        limit_value=self.max_inline_text_bytes,
                        observed_value=estimated_size,
                        deferred_units=1,
                        detail=(
                            f"files:{record_id} content remains authoritative in Mythic "
                            "and requires explicit selection/rescan"
                        ),
                    )
                )
                content = _canonical_json(metadata)
                content_kind = "json"
            else:
                metadata["content_fetch_status"] = "ineligible_metadata_only"
                content = _canonical_json(metadata)
                content_kind = "json"

        return SourceRecord.build(
            operation_id=str(operation_id),
            record_class=record_class,
            source_record_id=str(record_id),
            observed_at_utc=timestamp,
            content=content,
            content_kind=content_kind,
            callback_display_id=callback_id,
            task_display_id=task_id,
            task_output_id=output_id,
            metadata=metadata,
            deferrals=deferrals,
        )


class MythicOperationMemoryIngestor:
    """Advance one bounded page for each Mythic history stream."""

    def __init__(
        self, source: MythicOperationMemorySource, store: OperationMemoryStore
    ) -> None:
        self.source = source
        self.store = store

    async def sync_operation(self, operation_id: Any) -> dict[str, StreamSyncResult]:
        op = str(_operation_id(operation_id))
        snapshot = await self.store.snapshot(op)
        watermarks = snapshot.get("watermarks", {})
        results: dict[str, StreamSyncResult] = {}
        for stream_key in STREAM_KEYS:
            cursor = StreamCursor.parse(watermarks.get(stream_key))
            page = await self.source.fetch_page(
                op,
                stream_key,
                cursor=cursor,
                limit=self.store.limits.backfill_batch_size,
            )
            encoded_cursor = page.next_cursor.encode()
            ingest = await self.store.ingest_batch(
                op,
                page.records,
                stream_key=stream_key,
                next_cursor=encoded_cursor,
                source_has_more=page.has_more,
            )
            results[stream_key] = StreamSyncResult(
                stream_key=stream_key,
                source_count=len(page.records),
                has_more=page.has_more,
                cursor=encoded_cursor,
                ingest=ingest,
            )
        return results
