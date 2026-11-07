"""Shared pytest fixtures and import bootstrap for the Sage test suite."""

import os
import sys
from pathlib import Path

import pytest

# Test modules here import Payload_Type/sage top-level packages (`ai`, `sage_chat`, ...). Because
# `Payload_Type/sage/__init__.py` exists, pytest's prepend import mode puts `Payload_Type/` on
# sys.path, not `Payload_Type/sage/` — so those imports do not resolve on their own. Inserting the
# path per module makes collection depend on some alphabetically earlier module having done it
# first. Do it once, here, before any test module is imported.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage


# Pinned live-run evidence that several hillclimb replay/audit modules name as their default input
# (see phase12_proof_binding_audit, phase13_canonical_promotion, phase15_r5_retrospective_falsifiers).
# It lives in gitignored `.hillclimb/results/` because it is real engagement output — chat request IDs,
# model calls, achieved effects from specific dated runs — not a synthesisable fixture.
#
# A clone therefore does not have it, and 16 tests across 7 modules failed there while passing on the
# maintainer's machine. That is the defect class of private evidence acting as a hard test
# dependency. Unlike archived evaluation modules, these modules are living code — they are
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
    config.addinivalue_line(
        "markers",
        "serial: timing-sensitive; the offline suite runs it after the parallel pass, with nothing else running",
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
def _drop_log_handlers_on_closed_streams():
    """Remove mythic-logger handlers left bound to an earlier test's closed capture stream.

    `logging_fix.ensure_logger_initialized` binds a handler to whatever `sys.stdout` is the first time it
    runs, which inside a test is pytest's per-test capture. Once that test ends the stream is closed, and
    every later graph node's `force_flush_all_handlers()` raises "I/O operation on closed file", failing
    tests depending only on which test happened to run first in the process. Dropping the dead handler
    lets the logger re-initialize against the current stream.
    """
    from mythic_container.logging import logger as mythic_logger

    for handler in list(mythic_logger.handlers):
        if getattr(getattr(handler, "stream", None), "closed", False):
            mythic_logger.removeHandler(handler)
    yield


@pytest.fixture(autouse=True)
def _isolate_engagement_ledger(tmp_path, monkeypatch):
    """Keep the engagement ledger and legacy state directory out of the repo working tree during tests.

    The ledger lives in sage.db: point SAGE_DATABASE_PATH and the legacy state directory at a per-test tmp dir.
    Sessions without a Mythic operation bind an explicit isolated namespace, the supported offline binding;
    tests of operation binding override SAGE_ENGAGEMENT_ID / mythic_tools.SAGE_ENGAGEMENT_ID themselves.
    """
    import sys

    monkeypatch.setenv("SAGE_ENGAGEMENT_ID", "sage-test-isolated")
    for module_name in ("ai.langgraph.mythic_tools", "mythic_tools"):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "SAGE_ENGAGEMENT_ID"):
            monkeypatch.setattr(module, "SAGE_ENGAGEMENT_ID", "sage-test-isolated")
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))
    monkeypatch.setenv("SAGE_EVAL_OBSERVATIONS_DIR", str(tmp_path / "evaluator-observations"))
    monkeypatch.setenv("SAGE_TRAJECTORY_STORE", str(tmp_path / "trajectory" / "transitions.jsonl"))
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(tmp_path / "sage.db"))
    # Native chat marks first-seen channels in Sage's runtime SQLite file. Test
    # channel IDs are reused across modules; never let one test's marker make
    # another test appear to be a resumed production channel.
    from sage_chat import session as chat_session

    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    yield


@pytest.fixture(autouse=True)
def mythic_task_records(monkeypatch):
    """Stub Mythic's task-record transport for the live writers' shared task classifier.

    Every ledger writer classifies a task from this record, never from the output class. Without an entry a task
    reads as Mythic reports an ordinary finished task: agent processed, responses present,
    final status ``success``. Set ``mythic_task_records[<display id>]`` to a task row (GraphQL field names) to
    serve a specific record, or to None for an unreadable one.
    """
    from ai.langgraph import mythic_tools
    from tests.esl_fixtures import AGENT_PROCESSED

    records: dict = {}

    async def fetch(client, task_display_id):
        key = int(task_display_id)
        if key in records:
            row = records[key]
            return dict(row, display_id=row.get("display_id", key)) if isinstance(row, dict) else row
        return {"display_id": key, "completed": True, "status": "success", "responses": [{"id": 1}],
                **AGENT_PROCESSED}

    # Some modules import ``mythic_tools`` flat (ai/langgraph on sys.path), a second module object.
    for module in {mythic_tools, sys.modules.get("mythic_tools")} - {None}:
        monkeypatch.setattr(module, "_fetch_task_record", fetch)
    return records
