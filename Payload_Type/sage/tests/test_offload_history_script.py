from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-goad-reset" / "scripts" / "offload_history.py"
SPEC = importlib.util.spec_from_file_location("offload_history", SCRIPT)
offload = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = offload  # dataclasses resolve annotations through it
SPEC.loader.exec_module(offload)

OLD = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d-%H%M")
RECENT = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d-%H%M")


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _host(tmp_path: Path) -> dict[str, Path]:
    """An archive with one old and one recent reset folder, and a runtime directory holding live databases,
    source code and stamped leftovers of both ages."""
    evidence = tmp_path / ".sage_history" / "runtime-db-archive"
    _write(evidence / f"reset-{OLD}" / "local" / "sage.db", "old run")
    _write(evidence / f"reset-{OLD}" / "MANIFEST.json", json.dumps({"items": []}))
    _write(evidence / f"reset-{RECENT}" / "local" / "sage.db", "recent run")
    runtime = tmp_path / "repo" / "Payload_Type" / "sage"
    paths = {
        "evidence": evidence,
        "runtime": runtime,
        "live": _write(runtime / "sage.db", "live"),
        "live_phoenix": _write(runtime / ".phoenix" / "phoenix.db", "live traces"),
        "source": _write(runtime / "sage_entrypoint.py", "print('entry')"),
        "package": _write(runtime / "sage_chat" / "__init__.py", ""),
        "old_db": _write(runtime / f"sage_{OLD}.db", "old db"),
        "old_wal": _write(runtime / f"sage_{OLD}.db-wal", "old wal"),
        "old_phoenix": _write(runtime / ".phoenix" / f"phoenix_{OLD}.db", "old traces"),
        "old_zst": _write(runtime / ".phoenix" / f"phoenix_{OLD[:8]}b.db.zst", "compressed"),
        "old_ledger": _write(runtime / f".sage_engagement_{OLD}" / "seal.json", "{}"),
        "recent_db": _write(runtime / f"sage_{RECENT}.db", "recent db"),
    }
    paths["dest"] = tmp_path / "drive"
    paths["dest"].mkdir()
    return paths


def _run(paths: dict[str, Path], *extra: str) -> int:
    return offload.main(["--to", str(paths["dest"]), "--evidence-root", str(paths["evidence"]),
                         "--repo-root", str(paths["runtime"].parents[1]), "--mythic-env-path", "", *extra],
                        require_other_filesystem=False)


def test_without_yes_it_only_prints_the_plan(tmp_path, capsys):
    paths = _host(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*"))

    assert _run(paths) == 0

    assert sorted(p for p in tmp_path.rglob("*")) == before
    out = capsys.readouterr().out
    assert f"would move runtime-db-archive/reset-{OLD}" in out
    assert f"would move local/sage_{OLD}.db" in out and RECENT not in out


def test_old_items_are_copied_verified_and_removed_and_everything_else_stays(tmp_path, capsys):
    paths = _host(tmp_path)
    dest = paths["dest"]

    assert _run(paths, "--yes") == 0

    assert (dest / "runtime-db-archive" / f"reset-{OLD}" / "local" / "sage.db").read_text() == "old run"
    rotated = dest / "rotated-dbs" / "local"
    assert (rotated / f"sage_{OLD}.db").read_text() == "old db"
    assert (rotated / f"sage_{OLD}.db-wal").read_text() == "old wal"
    assert (rotated / ".phoenix" / f"phoenix_{OLD}.db").read_text() == "old traces"
    assert (rotated / ".phoenix" / f"phoenix_{OLD[:8]}b.db.zst").read_text() == "compressed"
    assert (rotated / f".sage_engagement_{OLD}" / "seal.json").exists()
    for name in ("old_db", "old_wal", "old_phoenix", "old_zst", "old_ledger"):
        assert not paths[name].exists(), name
    assert not (paths["evidence"] / f"reset-{OLD}").exists()
    for name in ("live", "live_phoenix", "source", "package", "recent_db"):
        assert paths[name].exists(), f"{name} must never be offloaded"
    assert (paths["evidence"] / f"reset-{RECENT}").is_dir()
    assert not list(dest.rglob("*.offload-partial"))
    capsys.readouterr()


def test_a_copy_that_does_not_verify_deletes_nothing_of_its_item(tmp_path, monkeypatch, capsys):
    paths = _host(tmp_path)
    real = offload._copy_file

    def corrupt_the_wal(source, target):
        real(source, target)
        if Path(source).name.endswith(".db-wal"):
            Path(target).write_text("flipped byte")

    monkeypatch.setattr(offload, "_copy_file", corrupt_the_wal)

    assert _run(paths, "--yes") == 1

    assert paths["old_db"].exists() and paths["old_wal"].exists(), "a database and its WAL stay together"
    assert not paths["old_phoenix"].exists(), "other items still move"
    assert "does not match" in capsys.readouterr().out


def test_a_different_file_already_at_the_destination_is_never_overwritten(tmp_path, capsys):
    paths = _host(tmp_path)
    clash = _write(paths["dest"] / "rotated-dbs" / "local" / f"sage_{OLD}.db", "someone else's bytes")

    assert _run(paths, "--yes") == 1

    assert clash.read_text() == "someone else's bytes"
    assert paths["old_db"].exists()
    assert "already exists with different content" in capsys.readouterr().out


def test_an_interrupted_run_can_be_repeated(tmp_path, capsys):
    paths = _host(tmp_path)
    _write(paths["dest"] / "rotated-dbs" / "local" / f"sage_{OLD}.db", "old db")  # copied before the stop

    assert _run(paths, "--yes") == 0

    assert not paths["old_db"].exists()
    capsys.readouterr()


def test_a_folder_a_staying_folder_still_references_is_kept(tmp_path, capsys):
    """Resets before 2026-11-16 recorded `already_preserved` instead of a copy; moving the folder that holds
    the bytes, but not the folder that points at them, would leave the reference pointing off the host."""
    paths = _host(tmp_path)
    _write(paths["evidence"] / f"reset-{RECENT}" / "MANIFEST.json", json.dumps({"items": [
        {"path": "installed/sage.db", "kind": "sqlite-backup", "already_preserved": f"reset-{OLD}/local/sage.db"}]}))

    _run(paths, "--yes")

    assert (paths["evidence"] / f"reset-{OLD}").is_dir()
    assert f"kept runtime-db-archive/reset-{OLD}: reset-{RECENT} still references it" in capsys.readouterr().out

    assert _run(paths, "--yes", "--older-than-days", "0") == 0
    assert not (paths["evidence"] / f"reset-{OLD}").exists() and not (paths["evidence"] / f"reset-{RECENT}").exists()
    capsys.readouterr()


def test_a_destination_on_this_filesystem_is_refused(tmp_path):
    paths = _host(tmp_path)
    with pytest.raises(SystemExit, match="same filesystem"):
        offload.main(["--to", str(paths["dest"]), "--evidence-root", str(paths["evidence"]),
                      "--repo-root", str(paths["runtime"].parents[1]), "--mythic-env-path", "", "--yes"])
    assert paths["old_db"].exists()


@pytest.mark.parametrize("to", [None, "missing"], ids=["unset", "not-mounted"])
def test_a_missing_destination_is_refused(tmp_path, monkeypatch, to):
    paths = _host(tmp_path)
    monkeypatch.delenv(offload.OFFLOAD_ENV, raising=False)
    argv = ["--evidence-root", str(paths["evidence"]), "--mythic-env-path", ""]
    if to:
        argv += ["--to", str(tmp_path / to)]
    with pytest.raises(SystemExit, match="destination"):
        offload.main(argv, require_other_filesystem=False)


@pytest.mark.parametrize("name", ["sage.db", "sage_operation_memory.db", "sage_entrypoint.py", "sage_20261116.py",
                                  "sage_chat.db", "phoenix.db", "sage_20261116-1200.db.bak"])
def test_names_that_are_not_stamped_databases_never_match(name):
    assert not offload.ROTATED_DATABASE.fullmatch(name) and not offload.ROTATED_PHOENIX.fullmatch(name)


@pytest.mark.parametrize("name", ["sage_20261116-1200.db", "sage_20261116-1200.db-wal", "sage_20260613a.db.zst",
                                  "sage_operation_memory_20261008-1340-w7-local-attempt.db"])
def test_recorded_stamped_database_names_match(name):
    assert offload.ROTATED_DATABASE.fullmatch(name)
