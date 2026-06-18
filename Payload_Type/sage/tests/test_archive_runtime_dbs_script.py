import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-goad-reset" / "scripts" / "archive_runtime_dbs.py"
SPEC = importlib.util.spec_from_file_location("archive_runtime_dbs", SCRIPT)
archive_runtime_dbs = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(archive_runtime_dbs)


def make_runtime_dbs(root: Path) -> tuple[Path, Path]:
    sage_db = root / "Payload_Type" / "sage" / "sage.db"
    phoenix_db = root / "Payload_Type" / "sage" / ".phoenix" / "phoenix.db"
    phoenix_db.parent.mkdir(parents=True)
    sage_db.write_text("checkpoint", encoding="utf-8")
    phoenix_db.write_text("trace", encoding="utf-8")
    return sage_db, phoenix_db


def test_archives_active_databases_with_minute_timestamp(tmp_path):
    sage_db, phoenix_db = make_runtime_dbs(tmp_path)

    moves = archive_runtime_dbs.archive_runtime_dbs(
        tmp_path,
        timestamp="20260618-1231",
    )

    assert moves == [
        (sage_db, sage_db.with_name("sage_20260618-1231.db")),
        (phoenix_db, phoenix_db.with_name("phoenix_20260618-1231.db")),
    ]
    assert not sage_db.exists()
    assert not phoenix_db.exists()
    assert moves[0][1].read_text(encoding="utf-8") == "checkpoint"
    assert moves[1][1].read_text(encoding="utf-8") == "trace"


def test_collision_prevents_all_moves(tmp_path):
    sage_db, phoenix_db = make_runtime_dbs(tmp_path)
    sage_db.with_name("sage_20260618-1231.db").write_text("older", encoding="utf-8")

    with pytest.raises(FileExistsError, match="archive destination already exists"):
        archive_runtime_dbs.archive_runtime_dbs(
            tmp_path,
            timestamp="20260618-1231",
        )

    assert sage_db.exists()
    assert phoenix_db.exists()


def test_rejects_non_minute_timestamp(tmp_path):
    with pytest.raises(ValueError, match="YYYYMMDD-HHMM"):
        archive_runtime_dbs.archive_runtime_dbs(
            tmp_path,
            timestamp="20260618",
        )
