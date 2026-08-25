import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-goad-reset" / "scripts" / "archive_runtime_dbs.py"
SPEC = importlib.util.spec_from_file_location("archive_runtime_dbs", SCRIPT)
archive_runtime_dbs = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(archive_runtime_dbs)


def make_runtime_dbs(root: Path) -> tuple[Path, Path, Path]:
    sage_db = root / "Payload_Type" / "sage" / "sage.db"
    operation_memory_db = root / "Payload_Type" / "sage" / "sage_operation_memory.db"
    phoenix_db = root / "Payload_Type" / "sage" / ".phoenix" / "phoenix.db"
    phoenix_db.parent.mkdir(parents=True)
    databases = (sage_db, operation_memory_db, phoenix_db)
    for database, content in zip(
        databases,
        ("checkpoint", "operation-memory", "trace"),
        strict=True,
    ):
        database.write_text(content, encoding="utf-8")
        for suffix in ("-wal", "-shm"):
            database.with_name(database.name + suffix).write_text(
                content + suffix,
                encoding="utf-8",
            )
    return databases


def test_archives_active_databases_with_minute_timestamp(tmp_path):
    sage_db, operation_memory_db, phoenix_db = make_runtime_dbs(tmp_path)

    moves = archive_runtime_dbs.archive_runtime_dbs(
        tmp_path,
        timestamp="20260618-1231",
    )

    assert moves == [
        (sage_db, sage_db.with_name("sage_20260618-1231.db")),
        (
            operation_memory_db,
            operation_memory_db.with_name("sage_operation_memory_20260618-1231.db"),
        ),
        (phoenix_db, phoenix_db.with_name("phoenix_20260618-1231.db")),
    ]
    for (source, destination), content in zip(
        moves,
        ("checkpoint", "operation-memory", "trace"),
        strict=True,
    ):
        assert not source.exists()
        assert destination.read_text(encoding="utf-8") == content
        for suffix in ("-wal", "-shm"):
            assert not source.with_name(source.name + suffix).exists()
            assert (
                destination.with_name(destination.name + suffix).read_text(
                    encoding="utf-8"
                )
                == content + suffix
            )


@pytest.mark.parametrize("database_index", range(3))
@pytest.mark.parametrize("suffix", ("", "-wal", "-shm"))
def test_collision_prevents_all_moves(tmp_path, database_index, suffix):
    databases = make_runtime_dbs(tmp_path)
    destinations = (
        databases[0].with_name("sage_20260618-1231.db"),
        databases[1].with_name("sage_operation_memory_20260618-1231.db"),
        databases[2].with_name("phoenix_20260618-1231.db"),
    )
    collision = destinations[database_index].with_name(
        destinations[database_index].name + suffix
    )
    collision.write_text("older", encoding="utf-8")

    with pytest.raises(FileExistsError, match="archive destination already exists"):
        archive_runtime_dbs.archive_runtime_dbs(
            tmp_path,
            timestamp="20260618-1231",
        )

    for database in databases:
        assert database.exists()
        assert database.with_name(database.name + "-wal").exists()
        assert database.with_name(database.name + "-shm").exists()


def test_rejects_non_minute_timestamp(tmp_path):
    with pytest.raises(ValueError, match="YYYYMMDD-HHMM"):
        archive_runtime_dbs.archive_runtime_dbs(
            tmp_path,
            timestamp="20260618",
        )
