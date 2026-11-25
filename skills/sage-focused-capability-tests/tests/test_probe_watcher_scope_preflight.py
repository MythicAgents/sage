"""Offline checks for the Watcher scope preflight probe's export and verdict (no Go, no Mythic checkout).

The probe's authorizer run (Mythic's own `Authorize` in a scratch copy of a configured Mythic checkout) needs Go and
that checkout; it is exercised by running the script, not here. These tests prove the exported set is every document
the Watcher scan sends, read from Sage's modules, and that the verdict fails closed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/probe_watcher_scope_preflight.py"
SPEC = importlib.util.spec_from_file_location("probe_watcher_scope_preflight", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

from sage_chat import mythic_findings_delivery as delivery  # noqa: E402
from sage_chat import operation_memory_source as source  # noqa: E402


def test_export_holds_every_document_a_watcher_scan_sends_from_sages_modules():
    exported = probe.export_documents(ROOT / "Payload_Type/sage")
    by_name = {row["name"]: row for row in exported["documents"]}
    assert set(by_name) == {
        "whoami", "owner_channel", "active_watchers", *(f"memory_{key}" for key in source.STREAM_KEYS),
        "findings_channel", "create_findings_channel", "create_findings_message", "eventlog",
    }
    for key in source.STREAM_KEYS:
        assert by_name[f"memory_{key}"]["query"] == source._QUERIES[key]
    assert by_name["whoami"]["query"] == delivery._WHOAMI_QUERY
    assert "createOperationEventLog" in by_name["eventlog"]["query"]
    assert by_name["owner_channel"]["variables"]["channel_id"] in {int(k) for k in probe.LOOKUP_FACTS}
    assert by_name["create_findings_message"]["variables"]["channelId"] in {int(k) for k in probe.LOOKUP_FACTS}
    assert exported["requested_scopes"] == sorted(delivery.REQUIRED_WATCHER_SCOPES)
    assert exported["sage_effective_scopes"] == sorted(delivery.REQUIRED_WATCHER_EFFECTIVE_SCOPES)


def _authorized(exported, *, deny=(), closure=None, drop=()):
    return {
        "effective_scopes": exported["sage_effective_scopes"] if closure is None else closure,
        "documents": [{"name": row["name"], "allowed": row["name"] not in deny,
                       "reason": "missing required API token scope: x" if row["name"] in deny else ""}
                      for row in exported["documents"] if row["name"] not in drop],
    }


@pytest.mark.parametrize(
    ("changes", "verdict"),
    [
        ({}, "allowed"),
        ({"deny": {"memory_callbacks"}}, "denied"),
        ({"closure": ["callback.read"]}, "denied"),
        ({"drop": {"eventlog"}}, "denied"),
    ],
)
def test_verdict_allows_only_every_document_on_the_matching_closure(changes, verdict):
    exported = probe.export_documents(ROOT / "Payload_Type/sage")
    result = probe.report(exported, exported["requested_scopes"], _authorized(exported, **changes), "d8a94943")
    assert result["verdict"] == verdict


def test_probe_needs_a_configured_mythic_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("MYTHIC_CLI", raising=False)
    with pytest.raises(probe.ProbeUnavailable, match="MYTHIC_CLI"):
        probe.mythic_dir(None)
    with pytest.raises(probe.ProbeUnavailable, match="authorization/graphql.go"):
        probe.mythic_dir(str(tmp_path))
