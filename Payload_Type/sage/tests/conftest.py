"""Shared pytest fixtures for the Sage test suite."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_engagement_ledger(tmp_path, monkeypatch):
    """Keep the durable engagement ledger out of the repo working tree during tests.

    The durable hop ledger (mythic_tools._persist_engagement_ledger) write-through fires whenever a
    test enables SAGE_ENGAGEMENT_GATE and records an achieved hop. Without this, such a test would
    write `.sage_engagement/engagement_default.json` into the cwd (the repo). Point the ledger dir at
    a per-test tmp dir by default; a test that needs its own location simply calls
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", ...) after this fixture (its value wins).
    """
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))
    yield
