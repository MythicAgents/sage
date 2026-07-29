from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "artifact_retention.py"
)
SPEC = importlib.util.spec_from_file_location("sage_artifact_retention", SCRIPT)
retention = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(retention)


def test_write_json_artifact_is_private_and_manifested(tmp_path, monkeypatch):
    durable = tmp_path / "history"
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(durable))

    path, record = retention.write_json_artifact(
        "contracts/cyber-runner",
        "contract.json",
        {"objective": "test"},
        artifact_type="cyber-runner-contract",
        context="unit test",
        root=tmp_path,
    )

    assert path.is_file()
    assert path.is_relative_to(durable)
    assert path.stat().st_mode & 0o777 == 0o600
    assert durable.stat().st_mode & 0o777 == 0o700
    assert record["sha256"] == retention._sha256_file(path)
    records = retention._manifest_records(tmp_path)
    assert records[-1]["artifact_path"] == path.relative_to(durable).as_posix()


def test_promote_copies_without_deleting_and_records_source(
    tmp_path, monkeypatch
):
    durable = tmp_path / "history"
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(durable))
    source = tmp_path / "review.md"
    source.write_text("decision\n", encoding="utf-8")

    result = retention.promote(
        [source],
        category="migrated/reviews",
        artifact_type="decision-review",
        context="unit test",
        root=tmp_path,
    )

    assert result["ok"] is True
    assert source.read_text(encoding="utf-8") == "decision\n"
    assert retention.source_is_recorded(source, root=tmp_path) is True
    source.write_text("changed\n", encoding="utf-8")
    assert retention.source_is_recorded(source, root=tmp_path) is False


def test_directory_promotion_is_verified_and_manifested(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(tmp_path / "history"))
    source = tmp_path / "panel-review"
    source.mkdir()
    (source / "packet.md").write_text("packet\n", encoding="utf-8")
    (source / "run.json").write_text("{}\n", encoding="utf-8")

    result = retention.promote(
        [source],
        category="migrated/panels",
        artifact_type="panel-review",
        context="unit test",
        root=tmp_path,
    )

    assert result["artifact_count"] == 2
    assert result["directory_count"] == 1
    assert retention.source_is_recorded(source, root=tmp_path) is True


def test_record_existing_indexes_a_durable_directory(tmp_path, monkeypatch):
    durable = tmp_path / "history"
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(durable))
    directory = retention.allocate_artifact_path(
        "panels/external",
        "review",
        root=tmp_path,
    )
    directory.mkdir()
    (directory / "packet.md").write_text("packet\n", encoding="utf-8")
    (directory / "run.json").write_text("{}\n", encoding="utf-8")

    result = retention.record_existing(
        [directory],
        category="panels/external",
        artifact_type="external-panel-review",
        context="unit test",
        root=tmp_path,
    )

    assert result["artifact_count"] == 2
    assert result["directory_count"] == 1
    assert retention._manifest_records(tmp_path)[-1]["record_kind"] == "directory"


def test_write_json_cli_accepts_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(tmp_path / "history"))
    monkeypatch.setattr(
        retention.sys,
        "stdin",
        type("Input", (), {"read": lambda self: '{"status":"complete"}'})(),
    )

    assert (
        retention.main(
            [
                "write-json",
                "--category",
                "handoffs",
                "--name",
                "handoff.json",
                "--artifact-type",
                "task-handoff",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert json.loads(Path(output["path"]).read_text())["status"] == "complete"


@pytest.mark.parametrize(
    "name",
    (".env", "api-token.json", "operator-payload.bin", "private-key.pem"),
)
def test_promotion_rejects_secret_and_payload_shaped_names(
    tmp_path, monkeypatch, name
):
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(tmp_path / "history"))
    source = tmp_path / name
    source.write_text("sensitive\n", encoding="utf-8")

    with pytest.raises(retention.RetentionError, match="refusing"):
        retention.promote(
            [source],
            category="migrated/evidence",
            artifact_type="evidence",
            context="unit test",
            root=tmp_path,
        )
