"""C1b tradecraft/process layer tests (eval gauge Phase 0).

Pins: failure-class counts, unclassified_rate (gauge health), run filtering, missing/empty
store safety, and the read-only guarantee.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "trajectory"))

import schema as traj_schema  # noqa: E402
import process_state as ps  # noqa: E402


def _rec(run_id, label, status="failed", capability="dcsync"):
    return traj_schema.TransitionRecord(
        run_id=run_id,
        source_files=(),
        objective="goad",
        capability=capability,
        observations=(),
        verifier=traj_schema.TransitionVerifier(status=status),
        failure_label=label,
    )


def _seed(tmp_path, monkeypatch, records):
    store = tmp_path / "transitions.jsonl"
    traj_schema.write_jsonl(str(store), records)
    monkeypatch.setenv("SAGE_TRAJECTORY_STORE", str(store))
    return str(store)


def test_failure_class_counts_and_unclassified_rate(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [
        _rec("r1", "ambiguous_account_name"),
        _rec("r1", "ambiguous_account_name"),
        _rec("r1", "wrong_security_context"),
        _rec("r1", "unclassified"),
    ])
    sig = ps.read_process_signals("r1")
    assert sig.total_transitions == 4
    assert sig.failure_class_counts["ambiguous_account_name"] == 2
    assert sig.failure_class_counts["wrong_security_context"] == 1
    assert sig.unclassified_count == 1
    assert abs(sig.unclassified_rate - 0.25) < 1e-9
    assert sig.gap_register  # blind spots are documented, not silent


def test_run_filtering(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [
        _rec("r1", "ambiguous_account_name"),
        _rec("r1", "delayed_effect"),
        _rec("r2", "unclassified"),
    ])
    assert ps.read_process_signals("r1").total_transitions == 2
    assert ps.read_process_signals("r2").total_transitions == 1
    assert ps.read_process_signals(None).total_transitions == 3  # whole store


def test_empty_label_counts_as_unclassified(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [_rec("r1", ""), _rec("r1", "access_denied")])
    sig = ps.read_process_signals("r1")
    assert sig.unclassified_count == 1
    assert sig.failure_class_counts.get("access_denied") == 1


def test_missing_store_is_zero_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_TRAJECTORY_STORE", str(tmp_path / "nope.jsonl"))
    sig = ps.read_process_signals("r1")
    assert sig.total_transitions == 0
    assert sig.unclassified_rate == 0.0


def test_reader_does_not_mutate_store(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch, [_rec("r1", "ambiguous_account_name")])
    before = ps.store_hash(store)
    ps.read_process_signals("r1")
    after = ps.store_hash(store)
    assert before == after
