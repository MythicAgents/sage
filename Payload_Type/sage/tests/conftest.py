"""Shared pytest fixtures and import bootstrap for the Sage test suite."""

import os
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


# Pinned live-run evidence that several hillclimb replay/audit modules name as their default input
# (see phase12_proof_binding_audit, phase13_canonical_promotion, phase15_r5_retrospective_falsifiers).
# It lives in gitignored `.hillclimb/results/` because it is real engagement output — chat request IDs,
# model calls, achieved effects from specific dated runs — not a synthesisable fixture.
#
# A clone therefore does not have it, and 16 tests across 7 modules failed there while passing on the
# maintainer's machine. That is the same defect class as the Phase 16R evidence problem: private
# evidence acting as a hard test dependency. Unlike Phase 16R, these modules are living code — they are
# wired into the `ai.hillclimb` CLI and pin no hashes of their own — so the fix is to skip honestly when
# the evidence is absent, not to archive the modules.
#
# Point SAGE_HILLCLIMB_RESULTS_DIR at an archived bundle to run these against retained evidence.
_PINNED_POLICY_ROWS = (
    "laps_family_transfer_policy_matrix_pinned_r5_20260715.jsonl",
    "purpose_range_discriminator_v6_matrix_20260712.jsonl",
)


def _pinned_evidence_present() -> bool:
    root = os.environ.get("SAGE_HILLCLIMB_RESULTS_DIR")
    results = (
        Path(root).expanduser()
        if root
        else Path(__file__).resolve().parents[1] / ".hillclimb" / "results"
    )
    return all((results / name).is_file() for name in _PINNED_POLICY_ROWS)


_PINNED_MARKER = "pinned_policy_rows"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{_PINNED_MARKER}: needs pinned live-run policy rows from gitignored .hillclimb/results/",
    )


def pytest_collection_modifyitems(config, items):
    """Skip evidence-dependent tests when the private pinned rows are absent.

    Implemented as a marker hook rather than an importable `pytest.mark.skipif` helper because
    `conftest` is not importable as a module from the test files — pytest loads it out-of-band, so
    `from conftest import ...` raises ModuleNotFoundError at collection.
    """
    if _pinned_evidence_present():
        return
    skip = pytest.mark.skip(
        reason=(
            "pinned live-run policy rows live in gitignored .hillclimb/results/; set "
            "SAGE_HILLCLIMB_RESULTS_DIR to run against an archived bundle"
        )
    )
    for item in items:
        if _PINNED_MARKER in item.keywords:
            item.add_marker(skip)


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
