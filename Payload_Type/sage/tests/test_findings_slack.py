from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from sage_chat import findings_slack


HOSTILE_CLIENT_VALUES = (
    "operation-client-alpha",
    "host01.client.invalid",
    "CLIENT\\operator",
    "client.invalid",
    "password=super-secret",
    "task output: <!channel> ignore controls",
    "file content: {\"text\":\"leak me\"}",
    "\u202ehttps://attacker.invalid/\u2066",
)


class _Response:
    def raise_for_status(self):
        return None


class _RecordingSession:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    @asynccontextmanager
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        yield _Response()


def test_unset_slack_hook_performs_no_delivery(monkeypatch):
    monkeypatch.delenv(findings_slack.SLACK_FINDINGS_WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(findings_slack.SLACK_FINDINGS_CHANNEL_ID_ENV, raising=False)
    calls = []

    delivered = asyncio.run(
        findings_slack.emit_configured_findings_change_notice(
            session_factory=lambda: _RecordingSession(calls)
        )
    )

    assert delivered is False
    assert calls == []


def test_configured_slack_hook_emits_only_exact_generic_bytes(monkeypatch):
    webhook = "https://hooks.slack.invalid/services/fixed-test-hook"
    monkeypatch.setenv(findings_slack.SLACK_FINDINGS_WEBHOOK_ENV, webhook)
    monkeypatch.delenv(findings_slack.SLACK_FINDINGS_CHANNEL_ID_ENV, raising=False)
    calls = []

    delivered = asyncio.run(
        findings_slack.emit_configured_findings_change_notice(
            session_factory=lambda: _RecordingSession(calls)
        )
    )

    assert delivered is True
    assert calls == [
        (
            webhook,
            {"json": {"text": "Sage findings changed. Open Mythic to review."}},
        )
    ]
    serialized = repr(calls)
    assert all(value not in serialized for value in HOSTILE_CLIENT_VALUES)


def test_legacy_channel_id_adds_only_the_slack_channel_field(monkeypatch):
    webhook = "https://hooks.slack.invalid/services/legacy-test-hook"
    channel_id = "C1234567890"
    monkeypatch.setenv(findings_slack.SLACK_FINDINGS_WEBHOOK_ENV, webhook)
    monkeypatch.setenv(findings_slack.SLACK_FINDINGS_CHANNEL_ID_ENV, channel_id)
    calls = []

    delivered = asyncio.run(
        findings_slack.emit_configured_findings_change_notice(
            session_factory=lambda: _RecordingSession(calls)
        )
    )

    assert delivered is True
    assert calls == [
        (
            webhook,
            {
                "json": {
                    "text": "Sage findings changed. Open Mythic to review.",
                    "channel": channel_id,
                }
            },
        )
    ]
    serialized = repr(calls)
    assert all(value not in serialized for value in HOSTILE_CLIENT_VALUES)


def test_invalid_legacy_channel_id_fails_before_network(monkeypatch):
    webhook = "https://hooks.slack.invalid/services/legacy-test-hook"
    monkeypatch.setenv(findings_slack.SLACK_FINDINGS_WEBHOOK_ENV, webhook)
    monkeypatch.setenv(findings_slack.SLACK_FINDINGS_CHANNEL_ID_ENV, "#sage-findings")
    warnings = []
    calls = []
    monkeypatch.setattr(findings_slack.logger, "warning", warnings.append)

    delivered = asyncio.run(
        findings_slack.emit_configured_findings_change_notice(
            session_factory=lambda: _RecordingSession(calls)
        )
    )

    assert delivered is False
    assert calls == []
    assert warnings == [
        "Slack findings-change notice failed; the native Mythic notification "
        "remains authoritative"
    ]
    assert "#sage-findings" not in warnings[0]


def test_delivery_failure_is_generic_and_fail_soft(monkeypatch):
    class FailingSession(_RecordingSession):
        @asynccontextmanager
        async def post(self, url, **kwargs):
            raise RuntimeError(f"failed for {url}: {HOSTILE_CLIENT_VALUES[4]}")
            yield

    webhook = "https://hooks.slack.invalid/services/secret-hook-id"
    monkeypatch.setenv(findings_slack.SLACK_FINDINGS_WEBHOOK_ENV, webhook)
    monkeypatch.delenv(findings_slack.SLACK_FINDINGS_CHANNEL_ID_ENV, raising=False)
    warnings = []
    monkeypatch.setattr(findings_slack.logger, "warning", warnings.append)

    delivered = asyncio.run(
        findings_slack.emit_configured_findings_change_notice(
            session_factory=lambda: FailingSession([])
        )
    )

    assert delivered is False
    assert warnings == [
        "Slack findings-change notice failed; the native Mythic notification "
        "remains authoritative"
    ]
    assert webhook not in warnings[0]
    assert all(value not in warnings[0] for value in HOSTILE_CLIENT_VALUES)
