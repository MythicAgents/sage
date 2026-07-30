from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
retention = importlib.import_module("artifact_retention")
guard = importlib.import_module("retention_guard")


def _payload(transcript: Path) -> dict[str, str]:
    return {
        "hook_event_name": "Stop",
        "session_id": "session-test",
        "transcript_path": str(transcript),
        "cwd": str(transcript.parent),
    }


def test_guard_warns_for_existing_unrecorded_high_value_tmp_path(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(tmp_path / "history"))
    source = Path("/tmp") / f"sage-retention-test-review-{tmp_path.name}.md"
    source.write_text("review\n", encoding="utf-8")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"message": f"Use {source} in the decision."}) + "\n",
        encoding="utf-8",
    )
    try:
        result = guard.run_hook(_payload(transcript))
        assert "Sage retention warning" in result["systemMessage"]
        assert str(source) in result["systemMessage"]
    finally:
        source.unlink(missing_ok=True)


def test_guard_is_quiet_after_exact_source_is_promoted(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(tmp_path / "history"))
    source = Path("/tmp") / f"sage-retention-test-contract-{tmp_path.name}.json"
    source.write_text("{}\n", encoding="utf-8")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(str(source), encoding="utf-8")
    try:
        retention.promote(
            [source],
            category="migrated/contracts",
            artifact_type="contract",
            context="unit test",
            root=tmp_path,
        )
        assert guard.run_hook(_payload(transcript)) == {}
    finally:
        source.unlink(missing_ok=True)


def test_guard_ignores_payload_and_pytest_scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(tmp_path / "history"))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "/tmp/sage_payloads/review-payload.bin\n"
        "/tmp/pytest-of-user/test_contract/result.json\n"
        "/tmp/sage-isc49-review-lease-probe-20260725/result.json\n",
        encoding="utf-8",
    )
    assert guard.run_hook(_payload(transcript)) == {}
