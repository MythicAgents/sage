from __future__ import annotations

import asyncio
import sqlite3

import pytest

from runtime_db import DATABASE_ENV, DatabaseUnavailable, probe_database
from sage_chat import session as chat_session
from sage_chat.headless import build_chat_request


def test_probe_creates_missing_parent_and_enables_wal(tmp_path):
    target = tmp_path / "missing" / "dir" / "sage.db"

    assert probe_database(target) == target
    with sqlite3.connect(target) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_probe_defaults_to_configured_path(monkeypatch, tmp_path):
    target = tmp_path / "configured" / "sage.db"
    monkeypatch.setenv(DATABASE_ENV, str(target))

    assert probe_database() == target
    assert target.is_file()


@pytest.mark.parametrize("blocker", ["directory", "parent_is_file"])
def test_probe_names_path_and_setting_when_sqlite_cannot_open(tmp_path, blocker):
    if blocker == "directory":
        target = tmp_path / "sage.db"
        target.mkdir()
    else:
        (tmp_path / "file").write_text("not a directory")
        target = tmp_path / "file" / "sage.db"

    with pytest.raises(DatabaseUnavailable) as caught:
        probe_database(target)

    assert str(target) in str(caught.value)
    assert DATABASE_ENV in str(caught.value)
    # Existing handlers that catch SQLite's operational errors still see one.
    assert isinstance(caught.value, sqlite3.OperationalError)


def test_channel_marker_error_names_database_path(monkeypatch, tmp_path):
    target = tmp_path / "sage.db"
    target.mkdir()
    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", str(target))

    async def claim():
        try:
            await chat_session.claim_channel_marker(build_chat_request("hello"))
        finally:
            # aiosqlite's worker thread reports the failed connect after the await; let it land before
            # asyncio.run closes the loop, or a later test inherits an unhandled-thread warning.
            await asyncio.sleep(0.05)

    with pytest.raises(DatabaseUnavailable, match="unable to open database file") as caught:
        asyncio.run(claim())

    assert str(target) in str(caught.value)
    assert DATABASE_ENV in str(caught.value)


def test_channel_marker_still_claims_once_on_usable_database(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    request = build_chat_request("hello", channel_id=7)

    assert asyncio.run(chat_session.claim_channel_marker(request)) is True
    assert asyncio.run(chat_session.claim_channel_marker(request)) is False
