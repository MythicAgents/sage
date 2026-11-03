"""BloodHound ingest contract: decoding `file_upload` responses, the readiness read, and job-status polling.

`FakeIngestTool` is the BloodHound MCP `file_upload` tool as the LangChain MCP adapter presents it: one text content
block holding the MCP envelope. `patched=True` is the image's MCP (pinned base plus Sage's status patch);
`patched=False` is the pinned six-handler base, whose status read is the exact error recorded on 2026-11-03
(job 683, twenty identical rejections). Other collection tests reuse it so their fixtures speak the real contract.
"""

import asyncio
import json

import pytest

from ai import bloodhound_config as bh
from ai.langgraph import mythic_tools as mt
from ai.mcp import MCPManager

BASE_UNKNOWN_STATUS = (
    "Unknown info_type 'status'. Valid options: end_job, start_job, upload, upload_bytes, "
    "upload_bytes_to_job, upload_to_job"
)
FILE_UPLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        name: {"title": name}
        for name in ("info_type", "file_path", "job_id", "file_name", "file_bytes_base64", "content_type")
    },
}


def block(payload) -> list[dict]:
    """The adapter's representation of one MCP text result."""
    return [{"type": "text", "text": json.dumps(payload)}]


def status_data(job_id: int, status: int, label: str, detail: str = "") -> list[dict]:
    return block({"info_type": "status", "data": {
        "job_id": job_id, "status": status, "status_message": label, "detail": detail,
        "start_time": "2026-11-03T21:28:00Z", "end_time": "0001-01-01T00:00:00Z", "total_files": 1, "failed_files": 0,
    }})


class FakeIngestTool:
    """`file_upload` with the shipped envelope. `statuses` are answered in order for the uploaded job; the last
    one repeats. An item is a `(status, label)` pair, a raw response, or an exception to raise."""

    name = "file_upload"

    def __init__(self, statuses=((2, "Complete"),), *, patched=True, job_id=41, upload=None, schema=None):
        self.args_schema = FILE_UPLOAD_SCHEMA if schema is None else schema
        self.statuses = list(statuses)
        self.patched = patched
        self.job_id = job_id
        self.upload = upload
        self.calls: list[dict] = []

    @property
    def uploads(self) -> int:
        return sum(1 for call in self.calls if call["info_type"] == "upload_bytes")

    @property
    def status_reads(self) -> list:
        return [call.get("job_id") for call in self.calls if call["info_type"] == "status"]

    async def ainvoke(self, args):
        recorded = dict(args)
        if "file_bytes_base64" in recorded:
            recorded["file_bytes_base64"] = len(recorded["file_bytes_base64"])
        self.calls.append(recorded)
        info_type = args.get("info_type")
        if info_type == "upload_bytes":
            if self.upload is not None:
                return self.upload
            return block({"info_type": "upload_bytes", "data": {
                "job_id": self.job_id, "file_name": args.get("file_name"), "status": "upload_complete",
                "content_type": "application/zip", "file_size_bytes": 1,
            }})
        if info_type == "status" and self.patched:
            job_id = args.get("job_id")
            if job_id != self.job_id:
                return block({"info_type": "status", "data": {"job_id": job_id, "status_message": "not_found"}})
            item = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, tuple):
                return status_data(job_id, *item)
            return item
        valid = "end_job, start_job, upload, upload_bytes, upload_bytes_to_job, upload_to_job"
        if self.patched:
            valid = valid.replace("start_job,", "start_job, status,")
        return block({"error": f"Unknown info_type '{info_type}'. Valid options: {valid}"})


def install_bloodhound(monkeypatch, tool, *, server="BloodHound", extra_tools=()):
    """Expose exactly one BloodHound server carrying `tool` (and any extra tools) through MCPManager."""
    monkeypatch.setattr(MCPManager, "get_connected_servers", lambda: [server])
    monkeypatch.setattr(MCPManager, "is_bloodhound_server", lambda name: name == server)
    monkeypatch.setattr(MCPManager, "get_tools_by_server", lambda name: [tool, *extra_tools] if name == server else [])


# --- decoder -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("representation", [
    lambda payload: payload,
    lambda payload: json.dumps(payload),
    lambda payload: block(payload),
])
def test_decoder_accepts_each_adapter_representation(representation):
    payload = {"info_type": "status", "data": {"job_id": 7, "status": 2, "status_message": "Complete"}}
    assert bh.decode_ingest_response(representation(payload), info_type="status", expected_job_id=7)["status"] == 2


def test_decoder_keeps_the_error_text_of_the_base_mcp_status_rejection():
    with pytest.raises(bh.IngestContractError) as raised:
        bh.decode_ingest_response(block({"error": BASE_UNKNOWN_STATUS}), info_type="status", expected_job_id=683)
    assert raised.value.kind == bh.INGEST_ERROR_UNSUPPORTED
    assert "Unknown info_type 'status'" in raised.value.detail


@pytest.mark.parametrize("payload, kind", [
    ({"error": "API error: (HTTP 401) HTTP Error: 401", "info_type": "status",
      "data": {"job_id": 7, "status": 2, "status_message": "Complete"}}, bh.INGEST_ERROR_AUTH),
    ({"errors": ["boom"], "info_type": "status",
      "data": {"job_id": 7, "status": 2, "status_message": "Complete"}}, bh.INGEST_ERROR_TOOL),
    ({"isError": True, "info_type": "status",
      "data": {"job_id": 7, "status": 2, "status_message": "Complete"}}, bh.INGEST_ERROR_TOOL),
    ({"success": False, "info_type": "status",
      "data": {"job_id": 7, "status": 2, "status_message": "Complete"}}, bh.INGEST_ERROR_TOOL),
    ({"info_type": "status", "data": {"job_id": 7, "status": 2, "status_message": "Complete",
                                      "error": "x"}}, bh.INGEST_ERROR_TOOL),
    ({"error": "Connection error: Timed out connecting to BloodHound API"}, bh.INGEST_ERROR_TRANSIENT),
    ({"error": "API error: (HTTP 503) unavailable"}, bh.INGEST_ERROR_TRANSIENT),
    ({"info_type": "status", "data": {"job_id": 8, "status": 2, "status_message": "Complete"}},
     bh.INGEST_ERROR_MISMATCH),
    ({"info_type": "status", "data": {"job_id": True, "status": 2, "status_message": "Complete"}},
     bh.INGEST_ERROR_MISMATCH),
    ({"info_type": "upload_bytes", "data": {"job_id": 7, "status": 2, "status_message": "Complete"}},
     bh.INGEST_ERROR_MALFORMED),
    ({"data": {"job_id": 7, "status": 2, "status_message": "Complete"}}, bh.INGEST_ERROR_MALFORMED),
    ({"info_type": "status", "data": None}, bh.INGEST_ERROR_MALFORMED),
])
def test_decoder_rejects_errors_before_reading_completion_shaped_data(payload, kind):
    with pytest.raises(bh.IngestContractError) as raised:
        bh.decode_ingest_response(block(payload), info_type="status", expected_job_id=7)
    assert raised.value.kind == kind


@pytest.mark.parametrize("response", [
    "not json",
    [{"type": "text", "text": "{"}],
    block({"info_type": "status", "data": {"job_id": 7}}) * 2,
    [],
    [{"type": "image", "base64": "AAAA"}],
    42,
    json.dumps([1, 2]),
])
def test_decoder_rejects_malformed_and_ambiguous_responses(response):
    with pytest.raises(bh.IngestContractError) as raised:
        bh.decode_ingest_response(response, info_type="status", expected_job_id=7)
    assert raised.value.kind == bh.INGEST_ERROR_MALFORMED


def test_diagnostics_are_bounded_and_redact_encoded_bytes():
    secret = "QUJD" * 40
    error = bh.IngestContractError(bh.INGEST_ERROR_TOOL, f"bad upload {secret} " + "x " * 400)
    assert secret not in error.detail and "[redacted]" in error.detail
    assert len(error.detail) <= 300


@pytest.mark.parametrize("status, label, expected", [
    (2, "Complete", "complete"),
    (0, "Ready", "pending"), (1, "Running", "pending"), (6, "Ingesting", "pending"), (7, "Analyzing", "pending"),
    (3, "Canceled", "terminal_failure"), (4, "Timed Out", "terminal_failure"), (5, "Failed", "terminal_failure"),
    (8, "Partially Complete", "terminal_failure"), (-1, "Invalid", "terminal_failure"),
])
def test_status_classification_covers_every_documented_pair(status, label, expected):
    assert bh.classify_ingest_status({"job_id": 7, "status": status, "status_message": label}) == expected


@pytest.mark.parametrize("data", [
    {"job_id": 7, "status": 2, "status_message": "Failed"},
    {"job_id": 7, "status": 5, "status_message": "Complete"},
    {"job_id": 7, "status": 9, "status_message": "Complete"},
    {"job_id": 7, "status": "2", "status_message": "Complete"},
    {"job_id": 7, "status": True, "status_message": "Running"},
    {"job_id": 7, "status_message": "Complete"},
    {"job_id": 7, "status": 2, "status_message": "not_found"},
    {"job_id": 7, "status": 6, "status_message": ""},
])
def test_unknown_or_contradictory_status_pairs_are_protocol_errors(data):
    with pytest.raises(bh.IngestContractError) as raised:
        bh.classify_ingest_status(data)
    assert raised.value.kind == bh.INGEST_ERROR_PROTOCOL


def test_typed_not_found_is_pending_visibility_lag():
    assert bh.classify_ingest_status({"job_id": 7, "status_message": "not_found"}) == "pending"


@pytest.mark.parametrize("job_id", [0, -3, None, True, "41", 4.0])
def test_upload_receipts_need_a_positive_integer_job_id(job_id):
    with pytest.raises(bh.IngestContractError):
        bh.upload_job_id({"job_id": job_id})
    assert bh.upload_job_id({"job_id": 41}) == 41


# --- readiness read ------------------------------------------------------------------------------------------


def test_probe_is_ready_only_on_typed_not_found_for_job_zero(monkeypatch):
    tool = FakeIngestTool()
    install_bloodhound(monkeypatch, tool)
    contract = asyncio.run(bh.probe_ingest_contract())
    assert contract.ready is True and contract.tool is tool and contract.server == "BloodHound"
    assert tool.calls == [{"info_type": "status", "job_id": 0}], "readiness is one status read, never an upload"


def test_probe_fails_against_the_pinned_six_handler_mcp_and_names_the_remedy(monkeypatch):
    tool = FakeIngestTool(patched=False)
    install_bloodhound(monkeypatch, tool)
    contract = asyncio.run(bh.probe_ingest_contract())
    assert contract.ready is False and contract.kind == bh.INGEST_ERROR_UNSUPPORTED
    assert "Unknown info_type 'status'" in contract.reason
    assert "rebuild the image" in contract.operator_text()
    assert tool.calls == [{"info_type": "status", "job_id": 0}]


@pytest.mark.parametrize("response, kind", [
    (block({"error": "API error: (HTTP 401) HTTP Error: 401 Client Error"}), bh.INGEST_ERROR_AUTH),
    (block({"error": "API error: (HTTP 403) HTTP Error: 403 Client Error"}), bh.INGEST_ERROR_AUTH),
    (block({"error": "API error: (HTTP 404) HTTP Error: 404 Client Error: Not Found"}), bh.INGEST_ERROR_TOOL),
    (block({"info_type": "status", "data": {"job_id": 0, "status": 2, "status_message": "Complete"}}),
     bh.INGEST_ERROR_PROTOCOL),
    (block({"info_type": "status", "data": {"job_id": 1, "status_message": "not_found"}}), bh.INGEST_ERROR_MISMATCH),
    ("garbage", bh.INGEST_ERROR_MALFORMED),
])
def test_probe_rejects_every_answer_but_typed_not_found(monkeypatch, response, kind):
    tool = FakeIngestTool(statuses=[response], job_id=0)
    install_bloodhound(monkeypatch, tool)
    contract = asyncio.run(bh.probe_ingest_contract())
    assert contract.ready is False and contract.kind == kind, contract


def test_probe_times_out_inside_its_deadline(monkeypatch):
    class Hanging(FakeIngestTool):
        async def ainvoke(self, args):
            self.calls.append(dict(args))
            await asyncio.sleep(60)

    tool = Hanging()
    install_bloodhound(monkeypatch, tool)
    contract = asyncio.run(bh.probe_ingest_contract(timeout=0.01))
    assert contract.ready is False and contract.kind == bh.INGEST_ERROR_TRANSIENT


def test_probe_requires_the_exact_upload_schema(monkeypatch):
    tool = FakeIngestTool(schema={"type": "object", "properties": {"info_type": {}, "job_id": {}, "file_path": {}}})
    install_bloodhound(monkeypatch, tool)
    contract = asyncio.run(bh.probe_ingest_contract())
    assert contract.ready is False and "file_bytes_base64" in contract.reason
    assert tool.calls == [], "names and schema are checked before any read"


@pytest.mark.parametrize("servers, tools", [
    ([], None),
    (["BloodHound", "BloodHound-2"], None),
    (["BloodHound"], "duplicate"),
    (["BloodHound"], "none"),
])
def test_probe_needs_one_server_and_one_exact_tool(monkeypatch, servers, tools):
    tool = FakeIngestTool()
    listed = {"duplicate": [tool, FakeIngestTool()], "none": []}.get(tools, [tool])
    monkeypatch.setattr(MCPManager, "get_connected_servers", lambda: list(servers))
    monkeypatch.setattr(MCPManager, "is_bloodhound_server", lambda _name: True)
    monkeypatch.setattr(MCPManager, "get_tools_by_server", lambda _name: listed)
    contract = asyncio.run(bh.probe_ingest_contract())
    assert contract.ready is False and tool.calls == []


def test_probe_propagates_cancellation(monkeypatch):
    class Cancelled(FakeIngestTool):
        async def ainvoke(self, args):
            raise asyncio.CancelledError

    install_bloodhound(monkeypatch, Cancelled())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bh.probe_ingest_contract())


# --- job-status polling --------------------------------------------------------------------------------------


class Clock:
    """A monotonic clock advanced only by the poller's own sleeps and by slow reads."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def poll(tool, job_id=41, clock=None):
    clock = clock or Clock()
    return asyncio.run(mt._poll_bloodhound_ingest_job(tool, job_id, sleep=clock.sleep, clock=clock)), clock


def test_poll_completes_on_this_jobs_complete_after_pending_reads():
    tool = FakeIngestTool(statuses=[(6, "Ingesting"), (7, "Analyzing"), (2, "Complete")])
    result, clock = poll(tool)
    assert result["outcome"] == "complete" and result["polls"] == 3
    assert tool.status_reads == [41, 41, 41] and clock.sleeps == [6.0, 6.0]


@pytest.mark.parametrize("pair", [(3, "Canceled"), (4, "Timed Out"), (5, "Failed"),
                                  (8, "Partially Complete"), (-1, "Invalid")])
def test_terminal_failures_stop_on_the_first_read(pair):
    tool = FakeIngestTool(statuses=[pair])
    result, _ = poll(tool)
    assert result["outcome"] == "terminal_failure" and result["polls"] == 1
    assert result["last_known"]["status_message"] == pair[1]


def test_the_base_mcp_rejection_stops_on_the_first_read_instead_of_twenty():
    tool = FakeIngestTool(patched=False)
    result, clock = poll(tool, job_id=683)
    assert result["outcome"] == "error" and result["error_kind"] == bh.INGEST_ERROR_UNSUPPORTED
    assert result["polls"] == 1 and clock.sleeps == [], "a deterministic rejection is not waited on"


@pytest.mark.parametrize("response, kind", [
    (block({"info_type": "status", "data": {"job_id": 99, "status": 2, "status_message": "Complete"}}),
     bh.INGEST_ERROR_MISMATCH),
    (block({"info_type": "status", "data": {"job_id": 41, "status": 2, "status_message": "Failed"}}),
     bh.INGEST_ERROR_PROTOCOL),
    (block({"error": "API error: (HTTP 403) forbidden", "info_type": "status",
            "data": {"job_id": 41, "status": 2, "status_message": "Complete"}}), bh.INGEST_ERROR_AUTH),
    ("{not json", bh.INGEST_ERROR_MALFORMED),
    (block({"info_type": "status", "data": {"job_id": 41, "status": 2, "status_message": "Complete"}}) * 2,
     bh.INGEST_ERROR_MALFORMED),
])
def test_deterministic_errors_never_verify_and_stop_at_once(response, kind):
    tool = FakeIngestTool(statuses=[response])
    result, _ = poll(tool)
    assert result["outcome"] == "error" and result["error_kind"] == kind and result["polls"] == 1


def test_valid_pending_is_bounded_by_the_poll_count():
    tool = FakeIngestTool(statuses=[(1, "Running")])
    result, clock = poll(tool)
    assert result["outcome"] == "pending" and result["polls"] == 20
    assert result["last_known"]["status_message"] == "Running"
    assert clock.sleeps == [6.0] * 19 and clock.now <= 120.0


def test_typed_not_found_is_retried_as_pending():
    tool = FakeIngestTool(statuses=[block({"info_type": "status", "data": {"job_id": 41, "status_message": "not_found"}}),
                                    (2, "Complete")])
    result, _ = poll(tool)
    assert result["outcome"] == "complete" and result["polls"] == 2


def test_slow_reads_spend_the_wall_deadline_not_extra_budget():
    clock = Clock()

    class Slow(FakeIngestTool):
        async def ainvoke(self, args):
            clock.now += 30.0  # each read takes 30 s of the 120 s allowance
            return await super().ainvoke(args)

    tool = Slow(statuses=[(6, "Ingesting")])
    result, _ = poll(tool, clock=clock)
    assert result["outcome"] == "pending"
    assert result["polls"] == 4 and clock.now <= 120.0 + 30.0  # reads at 0, 36, 72, 108 s


def test_transient_errors_retry_within_the_allowance_then_complete():
    tool = FakeIngestTool(statuses=[block({"error": "Connection error: Timed out connecting to BloodHound API"}),
                                    RuntimeError("HTTP 503 temporarily unavailable"),
                                    (2, "Complete")])
    result, _ = poll(tool)
    assert result["outcome"] == "complete" and result["polls"] == 3


def test_exhausted_status_errors_are_distinguishable_from_server_reported_pending():
    tool = FakeIngestTool(statuses=[(6, "Ingesting"), block({"error": "API error: (HTTP 502) bad gateway"})])
    result, _ = poll(tool)
    assert result["outcome"] == "error" and result["error_kind"] == bh.INGEST_ERROR_TRANSIENT
    assert "still unreadable" in result["error_detail"]
    assert result["last_known"]["status_message"] == "Ingesting", "the last known status is retained"


def test_a_read_timeout_is_transient_and_bounded():
    class Hanging(FakeIngestTool):
        async def ainvoke(self, args):
            self.calls.append(dict(args))
            await asyncio.Event().wait()

    async def run():
        tool = Hanging()
        original = bh.INGEST_CALL_TIMEOUT_SECONDS
        bh.INGEST_CALL_TIMEOUT_SECONDS = 0.01
        try:
            clock = Clock()
            return await mt._poll_bloodhound_ingest_job(tool, 41, sleep=clock.sleep, clock=clock), tool
        finally:
            bh.INGEST_CALL_TIMEOUT_SECONDS = original

    result, tool = asyncio.run(run())
    assert result["outcome"] == "error" and result["error_kind"] == bh.INGEST_ERROR_TRANSIENT
    assert len(tool.calls) == 20


def test_polling_propagates_cancellation():
    tool = FakeIngestTool(statuses=[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        poll(tool)
