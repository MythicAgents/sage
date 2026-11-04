import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-goad-reset" / "scripts" / "archive_reset_evidence.py"
SPEC = importlib.util.spec_from_file_location("archive_reset_evidence", SCRIPT)
evidence = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(evidence)


def _live_wal_db(path: Path, rows: int) -> sqlite3.Connection:
    """A WAL database with committed rows that are still only in the WAL, held open like a running service."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    return conn


def _install(tmp_path: Path) -> tuple[Path, Path, list[sqlite3.Connection]]:
    mythic = tmp_path / "mythic"
    installed = mythic / "InstalledServices" / "sage"
    conns = [
        _live_wal_db(installed / "sage.db", 5),
        _live_wal_db(installed / "sage_20261029-1902.db", 2),
        _live_wal_db(installed / ".phoenix" / "phoenix.db", 3),
    ]
    records = installed / ".sage_engagement" / "decision_records"
    records.mkdir(parents=True)
    (records / "chat_3_request_1.seal.json").write_text('{"sealed": true}')
    (installed / "sage.db.controller.1.4.lock").write_text("")
    (mythic / ".env").write_text("POSTGRES_USER=x\n")
    return mythic / ".env", installed, conns


class _FakeDocker:
    """Stands in for `docker exec`: pg_dump writes bytes; pg_restore --list reports table data."""

    def __init__(self, *, dump_code=0, listing="1; 0 0 TABLE DATA public task\n2; 0 0 TABLE DATA public response\n"):
        self.calls, self.dump_code, self.listing = [], dump_code, listing

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if "pg_restore" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=self.listing.encode(), stderr=b"")
        kwargs["stdout"].write(b"PGDMP-fake")
        return subprocess.CompletedProcess(argv, self.dump_code, stderr=b"auth failed" if self.dump_code else b"")


def _rows(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def test_live_wal_databases_are_copied_with_uncheckpointed_rows_and_sources_untouched(tmp_path):
    env, installed, conns = _install(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in installed.rglob("*") if p.is_file()}
    docker = _FakeDocker()

    manifest = evidence.preserve_evidence(
        tmp_path / "out", installed=evidence.installed_service_dir(str(env)),
        postgres_container="mythic_postgres", run=docker)

    out = tmp_path / "out" / "installed"
    assert _rows(out / "sage.db") == 5
    assert _rows(out / "sage_20261029-1902.db") == 2
    assert _rows(out / ".phoenix" / "phoenix.db") == 3
    assert (out / ".sage_engagement" / "decision_records" / "chat_3_request_1.seal.json").read_text() == '{"sealed": true}'
    assert not (out / "sage.db.controller.1.4.lock").exists()
    assert {p: p.stat().st_mtime_ns for p in installed.rglob("*") if p.is_file()} == before
    paths = {item["path"]: item for item in manifest["items"]}
    assert paths["mythic_db.pgdump"]["table_data_entries"] == 2
    assert all(len(item["sha256"]) == 64 for item in manifest["items"])
    assert json.loads((tmp_path / "out" / "MANIFEST.json").read_text())["skipped"] == []
    for conn in conns:
        conn.close()


def test_mythic_dump_keeps_the_password_inside_the_container(tmp_path):
    docker = _FakeDocker()
    evidence.dump_mythic("mythic_postgres", tmp_path / "d.pgdump", run=docker)

    dump_argv = docker.calls[0]
    assert dump_argv[:3] == ["docker", "exec", "mythic_postgres"]
    assert '"$POSTGRES_PASSWORD"' in dump_argv[-1]
    assert not any("PGPASSWORD=" in part and "$" not in part for part in dump_argv)


@pytest.mark.parametrize("docker", [_FakeDocker(dump_code=1), _FakeDocker(listing="")], ids=["dump-fails", "empty-dump"])
def test_failed_mythic_dump_blocks_the_local_archive(tmp_path, docker, monkeypatch):
    monkeypatch.delenv("SAGE_DATABASE_PATH", raising=False)
    env, _installed, conns = _install(tmp_path)
    local_db = tmp_path / "repo" / "Payload_Type" / "sage" / "sage.db"
    local_db.parent.mkdir(parents=True)
    local_db.write_text("checkpoint")

    with pytest.raises(SystemExit, match="nothing was archived or reset"):
        evidence.main(["--repo-root", str(tmp_path / "repo"), "--evidence-root", str(tmp_path / "ev"),
                       "--timestamp", "20261030-0900", "--mythic-env-path", str(env)], run=docker)

    assert local_db.read_text() == "checkpoint"
    for conn in conns:
        conn.close()


def test_success_preserves_evidence_then_archives_local_databases(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SAGE_DATABASE_PATH", raising=False)
    env, _installed, conns = _install(tmp_path)
    local_db = tmp_path / "repo" / "Payload_Type" / "sage" / "sage.db"
    local_db.parent.mkdir(parents=True)
    local_db.write_text("checkpoint")

    evidence.main(["--repo-root", str(tmp_path / "repo"), "--evidence-root", str(tmp_path / "ev"),
                   "--timestamp", "20261030-0900", "--mythic-env-path", str(env)], run=_FakeDocker())

    summary = json.loads(capsys.readouterr().out)
    assert summary["evidence_dir"].endswith("reset-20261030-0900")
    assert summary["archived"] == [[str(local_db), str(local_db.with_name("sage_20261030-0900.db"))]]
    assert not local_db.exists()
    for conn in conns:
        conn.close()


def test_existing_destination_is_never_overwritten(tmp_path):
    (tmp_path / "ev" / "reset-20261030-0900").mkdir(parents=True)
    with pytest.raises(SystemExit, match="already exists"):
        evidence.main(["--repo-root", str(tmp_path), "--evidence-root", str(tmp_path / "ev"),
                       "--timestamp", "20261030-0900", "--skip-mythic-dump"], run=_FakeDocker())


def test_missing_install_and_explicit_skip_are_recorded_not_silent(tmp_path):
    docker = _FakeDocker()
    manifest = evidence.preserve_evidence(tmp_path / "out", installed=evidence.installed_service_dir(""),
                                          postgres_container=None, run=docker)

    assert docker.calls == []
    assert len(manifest["skipped"]) == 2
    assert manifest["items"] == []



def test_installed_target_moves_the_containers_state_and_leaves_the_repo_alone(tmp_path, capsys):
    """2026-10-29 and 2026-11-03: `archive_runtime_dbs.py --sage-db <installed>/sage.db` still moved the
    local phoenix.db and left the container's phoenix.db and engagement ledger in place, where
    Mythic's reused channel and operation ids made the next run start from stale state."""
    env_path, installed, conns = _install(tmp_path)
    for conn in conns:
        conn.close()
    repo = tmp_path / "repo"
    local_db = repo / "Payload_Type" / "sage" / ".phoenix" / "phoenix.db"
    local_db.parent.mkdir(parents=True)
    local_db.write_text("local tmux trace")

    assert evidence.main([
        "--repo-root", str(repo), "--evidence-root", str(tmp_path / "evidence"),
        "--timestamp", "20261104-1200", "--mythic-env-path", str(env_path),
        "--skip-mythic-dump", "--target", "installed",
    ]) == 0

    assert not (installed / "sage.db").exists() and (installed / "sage_20261104-1200.db").exists()
    assert not (installed / ".phoenix" / "phoenix.db").exists()
    assert (installed / ".phoenix" / "phoenix_20261104-1200.db").exists()
    assert not (installed / ".sage_engagement").exists()
    assert (installed / ".sage_engagement_20261104-1200" / "decision_records" / "chat_3_request_1.seal.json").exists()
    assert local_db.read_text() == "local tmux trace", "the local tmux databases belong to a different Sage"
    report = json.loads(capsys.readouterr().out)
    assert report["target"] == "installed"


def test_installed_target_refuses_without_an_install(tmp_path):
    with pytest.raises(SystemExit, match="MYTHIC_ENV_PATH"):
        evidence.main([
            "--repo-root", str(tmp_path), "--evidence-root", str(tmp_path / "evidence"),
            "--mythic-env-path", "", "--skip-mythic-dump", "--target", "installed",
        ])
