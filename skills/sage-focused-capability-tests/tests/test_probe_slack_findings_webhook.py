from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_slack_findings_webhook.py"
SPEC = importlib.util.spec_from_file_location("probe_slack_findings_webhook", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def _args(**overrides):
    values = {
        "send": True,
        "webhook_env": probe.SLACK_FINDINGS_WEBHOOK_ENV,
        "channel_id": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_missing_send_acknowledgement_refuses_before_loading_or_delivery(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        probe,
        "load_sage_dotenv",
        lambda *_args: (_ for _ in ()).throw(AssertionError("dotenv loaded")),
    )

    async def unexpected_sender():
        raise AssertionError("sender called")

    result = asyncio.run(
        probe.run_probe(_args(send=False), sender=unexpected_sender)
    )

    assert result == 2
    assert "--send" in capsys.readouterr().err


def test_missing_webhook_reports_only_the_environment_name(monkeypatch, capsys):
    monkeypatch.setattr(probe, "load_sage_dotenv", lambda *_args: [])
    monkeypatch.delenv(probe.SLACK_FINDINGS_WEBHOOK_ENV, raising=False)

    async def unexpected_sender():
        raise AssertionError("sender called")

    result = asyncio.run(probe.run_probe(_args(), sender=unexpected_sender))

    captured = capsys.readouterr()
    assert result == 2
    assert probe.SLACK_FINDINGS_WEBHOOK_ENV in captured.err
    assert "https://" not in captured.err


def test_alternate_channel_bound_webhook_is_selected_without_printing_secret(
    monkeypatch, capsys
):
    alternate_env = "SAGE_FINDINGS_SLACK_WEBHOOK_URL_SECURITY"
    alternate_url = "https://hooks.slack.invalid/services/private-test-secret"
    original_url = "https://hooks.slack.invalid/services/original-test-secret"
    monkeypatch.setattr(probe, "load_sage_dotenv", lambda *_args: [])
    monkeypatch.setenv(alternate_env, alternate_url)
    monkeypatch.setenv(probe.SLACK_FINDINGS_WEBHOOK_ENV, original_url)
    monkeypatch.delenv(probe.SLACK_FINDINGS_CHANNEL_ID_ENV, raising=False)
    observed = []

    async def sender():
        observed.append(probe.os.environ[probe.SLACK_FINDINGS_WEBHOOK_ENV])
        return True

    result = asyncio.run(
        probe.run_probe(_args(webhook_env=alternate_env), sender=sender)
    )

    captured = capsys.readouterr()
    assert result == 0
    assert observed == [alternate_url]
    assert probe.os.environ[probe.SLACK_FINDINGS_WEBHOOK_ENV] == original_url
    assert alternate_env in captured.out
    assert alternate_url not in captured.out + captured.err
    assert original_url not in captured.out + captured.err


def test_legacy_channel_override_is_passed_and_clearly_qualified(
    monkeypatch, capsys
):
    webhook = "https://hooks.slack.invalid/services/legacy-test-secret"
    channel_id = "C1234567890"
    monkeypatch.setattr(probe, "load_sage_dotenv", lambda *_args: [])
    monkeypatch.setenv(probe.SLACK_FINDINGS_WEBHOOK_ENV, webhook)
    monkeypatch.delenv(probe.SLACK_FINDINGS_CHANNEL_ID_ENV, raising=False)
    observed = []

    async def sender():
        observed.append(probe.findings_change_notice_payload())
        return True

    result = asyncio.run(
        probe.run_probe(_args(channel_id=channel_id), sender=sender)
    )

    captured = capsys.readouterr()
    assert result == 0
    assert observed == [
        {
            "text": "Sage findings changed. Open Mythic to review.",
            "channel": channel_id,
        }
    ]
    assert "Legacy" in captured.out
    assert "Modern Slack app" in captured.out
    assert "cannot prove" in captured.out
    assert webhook not in captured.out + captured.err
    assert probe.SLACK_FINDINGS_CHANNEL_ID_ENV not in probe.os.environ


def test_invalid_channel_refuses_before_delivery(monkeypatch, capsys):
    monkeypatch.setattr(probe, "load_sage_dotenv", lambda *_args: [])
    monkeypatch.setenv(
        probe.SLACK_FINDINGS_WEBHOOK_ENV,
        "https://hooks.slack.invalid/services/legacy-test-secret",
    )

    async def unexpected_sender():
        raise AssertionError("sender called")

    result = asyncio.run(
        probe.run_probe(_args(channel_id="#sage-findings"), sender=unexpected_sender)
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "Invalid legacy channel ID" in captured.err
    assert "#sage-findings" not in captured.err
