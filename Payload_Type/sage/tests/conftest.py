"""Shared pytest fixtures and import bootstrap for the Sage test suite."""

import sys
from pathlib import Path

import pytest

# Test modules here import Payload_Type/sage top-level packages (`ai`, `sage_chat`, ...). Because
# `Payload_Type/sage/__init__.py` exists, pytest's prepend import mode puts `Payload_Type/` on
# sys.path, not `Payload_Type/sage/` — so those imports do not resolve on their own. Individual
# modules used to each insert the path themselves, which meant the suite only collected as long as
# some alphabetically earlier module happened to do it first. Do it once, here, before any test
# module is imported.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage


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
    monkeypatch.setenv("SAGE_TRAJECTORY_STORE", str(tmp_path / "trajectory" / "transitions.jsonl"))
    yield
