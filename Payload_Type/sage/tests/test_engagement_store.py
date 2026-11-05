"""Engagement State Ledger store: the Phase 3 probes that the session-level regression tests do not cover.

Real sessions, the real store and the real proof boundary; only Mythic/BloodHound transport is stubbed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.langgraph import engagement_ledger as el
from ai.langgraph import engagement_state as es
from ai.langgraph import engagement_store as store_mod
from ai.langgraph import mythic_tools
from ai.trajectory import exporter as trajectory_exporter
from sage_chat import slash
from sage_chat.headless import build_chat_request
from tests.esl_fixtures import channel34_ledger, seed_ledger, task_proof, write_legacy_ledger

ROOT = Path(__file__).resolve().parents[3]
NOW = "2026-11-04T18:00:00+00:00"


def _session(namespace="store-probes"):
    mythic_tools.SAGE_ENGAGEMENT_ID = namespace
    return mythic_tools.MythicTools(agent_task_id="probe")


@pytest.fixture(autouse=True)
def _restore_namespace(monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", mythic_tools.SAGE_ENGAGEMENT_ID)


def _hop(technique, target, effect, status="achieved", **evidence):
    return {"id": f"{technique}:{target}", "technique": technique, "target": target, "effect": effect,
            "status": status, "evidence": dict(evidence), "preconditions": [], "satisfied_effects": [effect],
            "source": "test", "timestamp": NOW, "proof_envelope": {}}


def _state(tools, arg, channel_id=5):
    model = SimpleNamespace(mythic_client=tools, mode="supervised", provider="t", model="t", state={})
    return asyncio.run(slash._handle_state(model, build_chat_request("x", channel_id=channel_id, operation_id=0), arg))


# ── concurrency: independent processes and one shared identity ───────────────────────────────────────────────

_WRITER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
import engagement_store as s
store = s.EngagementStore(sys.argv[2], legacy_dir=sys.argv[3])
binding = store.bind_namespace("multi-process")
for i in range(int(sys.argv[5])):
    hop = {"technique": "probe", "target": f"{sys.argv[4]}-{i}", "status": "pending", "effect": f"x:{sys.argv[4]}-{i}"}
    store.apply(binding, s.hop_mutations([], [hop]))
    time.sleep(0.002)
"""


def test_independent_processes_never_lose_each_others_rows(tmp_path):
    database = tmp_path / "sage.db"
    langgraph = str(ROOT / "Payload_Type" / "sage" / "ai" / "langgraph")
    procs = [
        subprocess.Popen([sys.executable, "-c", _WRITER, langgraph, str(database), str(tmp_path / "legacy"), name, "25"])
        for name in ("a", "b", "c")
    ]
    assert [proc.wait(timeout=120) for proc in procs] == [0, 0, 0]
    store = store_mod.EngagementStore(database, legacy_dir=str(tmp_path / "legacy"))
    rows = store.snapshot(store.bind_namespace("multi-process")).hop_rows()
    assert len(rows) == 75
    assert sorted(row.number for row in rows) == list(range(1, 76))


def test_concurrent_updates_to_one_identity_keep_one_row_both_observations_and_the_correction():
    first, second = _session(), _session()
    seed_ledger(first, hops=[_hop("gpo-abuse", "srv02", "system:srv02", "pending", note="seed")])
    a_before, b_before = list(first._engagement_hops), list(second._engagement_hops)  # both read revision 1
    a_after = es.record_effect_result(es.EngagementState("t", hops=a_before), "gpo-abuse", "srv02",
                                      "system:srv02", "pending", {"note": "from A"}, NOW)
    b_after = es.record_effect_result(es.EngagementState("t", hops=b_before), "gpo-abuse", "srv02",
                                      "system:srv02", "pending", {"note": "from B"}, NOW)
    assert first._ledger_commit_hops(a_before, a_after.hops)
    _state(first, "remove 1 --reason operator says stale")
    assert second._ledger_commit_hops(b_before, b_after.hops)  # stale revision: merged onto the current row

    [row] = first._ledger_snapshot().hop_rows()
    assert row.number == 1 and row.revision == 3 and row.payload["evidence"]["note"] == "from B"
    assert row.retraction and row.retraction["reason"] == "operator says stale"
    history = first._ledger_store().history(first._require_ledger_binding(), row_id=row.row_id)
    notes = [event["payload"]["evidence"].get("note") for event in history if event["kind"] in {"create", "update"}]
    assert notes == ["seed", "from A", "from B"]
    assert "merged onto current revision 2" in history[-1]["reason"]


def test_historical_achievement_is_not_replaced_by_a_later_pending_observation():
    tools = _session()
    seed_ledger(tools, hops=[_hop("dcsync", "lab.local", "krbtgt-hash:lab.local", artifact_present=True)])
    before = list(tools._engagement_hops)
    later = es.record_effect_result(es.EngagementState("t", hops=before), "dcsync", "lab.local",
                                    "krbtgt-hash:lab.local", "pending", {"note": "re-observed"}, NOW)
    assert tools._ledger_commit_hops(before, later.hops)
    [row] = tools._ledger_snapshot().hop_rows()
    assert row.payload["status"] == "achieved" and row.revision == 1
    history = tools._ledger_store().history(tools._require_ledger_binding(), row_id=row.row_id)
    assert [event["kind"] for event in history] == ["create", "observation"]


# ── corrections: rebuild, undo, invalidation, implications ────────────────────────────────────────────────────

def test_reconcile_replay_and_projection_rebuild_keep_numbers_and_retractions(monkeypatch):
    tools = _session()
    seed_ledger(tools, hops=[_hop("gpo-abuse", "a", "system:a"), _hop("gpo-abuse", "b", "system:b")])
    _state(tools, "remove 2")
    replay = [store_mod.RowMutation("hop", "gpo-abuse:b", _hop("gpo-abuse", "b", "system:b", "pending"), 1, "replay-1")]
    binding = tools._require_ledger_binding()
    first = tools._ledger_store().apply(binding, replay)
    second = tools._ledger_store().apply(binding, replay)  # identical evidence replay
    assert first.observations and not second.observations and second.unchanged  # history once, never twice
    snapshot = tools._ledger_snapshot()
    assert [(row.number, bool(row.retraction)) for row in snapshot.hop_rows()] == [(1, False), (2, True)]
    rebuilt = tools._ledger_store().rebuild_heads(binding)
    for row in snapshot.rows:
        assert rebuilt[row.row_id]["payload"] == row.payload
        assert bool(rebuilt[row.row_id]["retraction"]) == bool(row.retraction)


def test_restore_rechecks_proof_and_never_clears_a_separate_invalidation():
    tools = _session()
    seed_ledger(tools, hops=[_hop("dcsync", "lab.local", "krbtgt-hash:lab.local")])            # proven (1)
    seed_ledger(tools, prove=False, hops=[_hop("golden-ticket", "lab.local", "da:lab.local")])  # unproven (2)
    seed_ledger(tools, hops=[_hop("dcsync-user", "x@lab.local", "creds:x@lab.local")])          # proven (3)
    _state(tools, "remove 1,2,3")
    _state(tools, "invalidate 3 --reason password rotated")
    assert tools._engagement_hops == []
    shown = _state(tools, "restore 1,2,3")
    assert "Restored 3 hop(s)" in shown
    eligible = {hop.technique: (hop.status, hop.pending_reason) for hop in tools._engagement_hops}
    assert eligible == {"dcsync": ("achieved", ""), "golden-ticket": ("pending", "proof_missing")}  # unproven stays unproven
    corrected = {hop.technique: hop.corrections for hop in tools._ledger_view().corrected_hops}
    assert corrected["dcsync-user"]["no_longer_holds"]["reason"] == "password rotated"
    assert "da:lab.local" not in es.EngagementState("t", hops=tools._engagement_hops, engagement_id=tools._eng_key(),
                                                    runtime_scope=True).achieved_effects()


def test_invalidated_achievement_stays_visible_but_cannot_satisfy_preconditions_or_completion():
    tools = _session()
    seed_ledger(tools, hops=[_hop("capability:dcsync-account", "account=cersei;domain=lab.local",
                                  "creds:cersei@lab.local")])
    objective = "obtain credentials for cersei@lab.local"

    def complete():
        return es.objective_effects_complete(es.EngagementState(
            objective, hops=list(tools._engagement_hops), engagement_id=tools._eng_key(), runtime_scope=True))

    assert complete() is True
    _state(tools, "invalidate 1 --reason credential revoked")
    assert complete() is False
    shown = _state(tools, "")
    assert "`achieved — no longer holds`" in shown and "credential revoked" in shown


def test_corrected_or_expired_premises_never_reenter_through_implications_or_graph_facts(monkeypatch):
    tools = _session()
    seed_ledger(tools, hops=[_hop("domain-admin-membership-check", "lab.local", "da:lab.local")],
                graph_facts=[es.GraphFact("gpo-domain:policy:lab.local", "bloodhound:cypher", NOW, 600)])
    foothold = es.Foothold("3", "apollo", "ws01", "lab.local", "LAB\\op", "medium", True, "test", NOW)

    def predicates():
        view = tools._ledger_view()
        return es.EngagementState("t", footholds=[foothold], hops=view.hops, graph_facts=view.graph_facts,
                                  engagement_id=tools._eng_key(), runtime_scope=False).satisfied_predicates()

    assert {"ds-replication-rights:lab.local", "gpo-domain:policy:lab.local"} <= predicates()
    _state(tools, "remove 1")
    assert "ds-replication-rights:lab.local" not in predicates()
    _state(tools, "wipe")
    assert "gpo-domain:policy:lab.local" not in predicates()
    # A fact observed again after the wipe is a new observation.
    assert tools._ledger_commit_facts([es.GraphFact("gpo-domain:policy:lab.local", "bloodhound:cypher",
                                                    "2026-11-04T19:00:00+00:00", 600)], complete=True)
    assert "gpo-domain:policy:lab.local" in predicates()

    monkeypatch.setenv("SAGE_ENGAGEMENT_HOP_TTL_HOURS", "1")
    old = _hop("collect-graph", "lab|baseline", "graph-built:lab|baseline", graph_verified=True)
    old["timestamp"] = "2020-01-01T00:00:00+00:00"
    seed_ledger(tools, hops=[old])
    view = tools._ledger_view()
    assert all(hop.technique != "collect-graph" for hop in view.hops)
    assert any(hop.technique == "collect-graph" for hop in view.corrected_hops)
    assert any(row["technique"] == "collect-graph" for row in tools._ledger_snapshot().display_hops())


def test_partial_and_empty_graph_refreshes_remove_nothing():
    first, second = _session(), _session()
    fact = lambda predicate: es.GraphFact(predicate, "bloodhound:cypher", NOW, 600)  # noqa: E731
    assert first._ledger_commit_facts([fact("generic-write:gpo:a"), fact("gpo-domain:a:lab.local")], complete=True)
    assert second._ledger_commit_facts([fact("objective-path-found:lab.local")], complete=False)  # partial
    assert first._ledger_commit_facts([], complete=False)                                        # empty
    current = {f.predicate for f in second._engagement_graph_facts}
    assert current == {"generic-write:gpo:a", "gpo-domain:a:lab.local", "objective-path-found:lab.local"}
    assert first._ledger_commit_facts([fact("generic-write:gpo:a")], complete=True)  # a full refresh
    assert {f.predicate for f in first._engagement_graph_facts} == {"generic-write:gpo:a"}
    assert len(first._ledger_snapshot().fact_rows()) == 3  # the others are stored history, not current


def test_dataclass_round_trip_and_same_identity_replacement_keep_store_metadata():
    tools = _session()
    seed_ledger(tools, hops=[_hop("gpo-abuse", "a", "system:a")])
    _state(tools, "remove 1")
    [hop] = tools._ledger_view().corrected_hops
    back = es.hops_from_dicts(json.loads(json.dumps(es.hops_to_dicts([hop]))))[0]
    assert (back.number, back.row_id, back.revision, bool(back.corrections.get("retracted"))) == (1, 1, 1, True)
    replaced = es.record_effect_result(es.EngagementState("t", hops=[back]), "gpo-abuse", "a", "system:a",
                                       "pending", {}, NOW).hops[0]
    assert (replaced.number, replaced.row_id, replaced.revision) == (1, 1, 1)
    assert replaced.corrections == back.corrections


# ── proof, selectors and failures ──────────────────────────────────────────────────────────────────────────────

def test_missing_or_foreign_proof_never_becomes_active_achievement():
    tools = _session()
    foreign = _hop("dcsync", "other.local", "krbtgt-hash:other.local", mythic_task_id="9", callback_id="3")
    foreign["proof_envelope"] = task_proof("another-operation", task_id="9")
    seed_ledger(tools, prove=False, hops=[_hop("dcsync", "lab.local", "krbtgt-hash:lab.local"), foreign])
    assert {(hop.status, hop.pending_reason) for hop in tools._engagement_hops} == {("pending", "proof_missing")}
    assert es.EngagementState("t", hops=tools._engagement_hops, engagement_id=tools._eng_key(),
                              runtime_scope=True).achieved_effects() == set()


@pytest.mark.parametrize("selector, changed", [
    ("dcsync:lab", 0), ("DCSYNC:LAB.LOCAL", 1), ("krbtgt-hash:lab.local", 1), ("dcsync", 1), ("2", 0), ("01", 1),
])
def test_selectors_are_exact_numbers_or_exact_labels(selector, changed):
    tools = _session()
    seed_ledger(tools, hops=[_hop("dcsync", "lab.local", "krbtgt-hash:lab.local")])
    assert f"Retracted {changed} hop(s)" in _state(tools, f"remove {selector}")


def test_a_locked_store_reports_unrecorded_and_never_authorizes_or_fabricates(monkeypatch):
    tools = _session()
    seed_ledger(tools, hops=[_hop("gpo-abuse", "a", "system:a")])
    assert type(tools._ledger_store()) is store_mod.EngagementStore
    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT_MS", 50)
    blocker = sqlite3.connect(tools._ledger_store().path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        tools._last_issued_task_display_id = "77"
        tools._last_issued_callback_id = "3"
        tools._last_issued_task_terminal_status = "completed"
        tools._last_issued_command = "shell"
        tools._pending_engagement_hop = ("gpo-abuse", "b", NOW)
        tools._record_engagement_success("whoami\r\nnt authority\\system\r\n", task_id="77", callback_id="3",
                                         terminal_status="completed", command="shell")
        assert "could not be recorded" in tools._ledger_last_write_error
    finally:
        blocker.rollback()
        blocker.close()
    assert [hop.target for hop in tools._engagement_hops] == ["a"]  # nothing half-installed

    def unavailable(_binding):
        raise store_mod.LedgerUnavailable("database disk image is malformed")

    monkeypatch.setattr(tools._ledger_store(), "snapshot", unavailable)
    with pytest.raises(store_mod.LedgerUnavailable, match="malformed"):
        tools._engagement_hops  # a database error is not an empty ledger

    async def no_footholds(_tools, _now):
        return []

    from ai.langgraph import access_reconciler
    monkeypatch.setattr(access_reconciler, "reconcile_access", no_footholds)
    tools.client = object()
    verdict = asyncio.run(tools._engagement_issue_hook("dcsync", {"domain": "lab.local"}, 3))
    assert verdict.startswith("[engagement-gate] dcsync not attempted: the engagement ledger is unavailable")
    assert tools._pending_engagement_hop is None


def test_a_correction_during_the_gates_awaits_is_seen_by_its_final_decision(monkeypatch):
    from ai.langgraph import access_reconciler

    tools = _session()
    tools.client = object()
    seed_ledger(tools, hops=[_hop("dcsync-rights-grant", "lab.local", "ds-replication-rights:lab.local")],
                graph_facts=[es.GraphFact("domain:lab.local", "bloodhound:domain_info", NOW, 600)])
    foothold = es.Foothold("3", "apollo", "ws01", "other.local", "OTHER\\op", "medium", True, "test", NOW)

    async def reconcile(_tools, _now):
        return [foothold]

    async def no_refresh(_now, **_kwargs):
        return None

    async def operator_retracts_during_await(_now):
        # Another channel's `/state remove` commits while this gate is awaiting.
        changed = tools._ledger_store().retract(tools._require_ledger_binding(), ["1"], reason="rights were removed",
                                                actor=store_mod.Actor(kind="operator")).changed
        assert changed
        return []

    monkeypatch.setattr(access_reconciler, "reconcile_access", reconcile)
    monkeypatch.setattr(tools, "_refresh_graph_facts_if_stale", no_refresh)
    monkeypatch.setattr(tools, "_corroboration_facts", operator_retracts_during_await)
    verdict = asyncio.run(tools._engagement_issue_hook("dcsync", {"domain": "lab.local"}, 3))
    assert verdict and "DCSync of lab.local not attempted" in verdict


def test_objective_and_hop_writes_never_overwrite_each_other():
    tools = _session()
    store, binding = tools._ledger_store(), tools._require_ledger_binding()
    errors = []

    def objectives():
        try:
            for i in range(30):
                store.update_objective(binding, f"objective {i}", "operator")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def hops():
        try:
            for i in range(30):
                store.apply(binding, store_mod.hop_mutations([], [_hop("probe", f"t{i}", f"x:{i}", "pending")]))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=objectives), threading.Thread(target=hops)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert errors == []
    snapshot = store.snapshot(binding)
    assert len(snapshot.hop_rows()) == 30 and snapshot.objective_setting() == ("objective 29", "operator")
    assert "objective" not in store.export(binding)


# ── offline migration, startup detection and exports ───────────────────────────────────────────────────────────

def test_unimported_legacy_files_make_the_ledger_unavailable_until_migrated(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(legacy))
    write_legacy_ledger(legacy, channel34_ledger())
    with pytest.raises(store_mod.LedgerUnavailable, match="engagement_ledger_migrate.py import"):
        store_mod.EngagementStore().bind_namespace("anything")
    store_mod.import_legacy_directory(legacy)
    assert store_mod.EngagementStore().bind_namespace("anything").engagement_key == "anything"


def _legacy(tmp_path, data, name=None):
    path = tmp_path / "legacy" / (name or f"state_{data['engagement_id']}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["hops"].append(dict(d["hops"][0])), "duplicate hop identity"),
    (lambda d: d["hops"][1].update(number=23), "duplicate hop number"),
    (lambda d: d["hops"][1].update(number=0), "invalid number"),
    (lambda d: d["hops"].append({"technique": "", "target": ""}), "no technique/target"),
])
def test_invalid_legacy_imports_fail_atomically_and_keep_their_source(tmp_path, mutate, message):
    data = channel34_ledger()
    mutate(data)
    path = _legacy(tmp_path, data)
    before = path.read_bytes()
    with pytest.raises(store_mod.LedgerImportError, match=message):
        store_mod.import_legacy_file(path, database=tmp_path / "sage.db")
    assert path.read_bytes() == before
    connection = sqlite3.connect(tmp_path / "sage.db")
    assert connection.execute("SELECT COUNT(*) FROM esl_operations").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM esl_imports").fetchone()[0] == 0


def test_import_numbering_destination_conflicts_and_evaluator_data(tmp_path):
    data = channel34_ledger()
    data["hops"].append(dict(data["hops"][0], technique="late", target="row", id="late:row"))
    data["hops"][-1].pop("number")
    data["ground_truth_probes"] = {"DA_CHILD": {"disposition": "proven"}}
    path = _legacy(tmp_path, data)
    database = tmp_path / "sage.db"
    assert store_mod.import_legacy_file(path, database=database) == "imported"
    assert store_mod.import_legacy_file(path, database=database) == "unchanged"
    store = store_mod.EngagementStore(database, legacy_dir=str(tmp_path / "legacy"))
    binding = store.find_binding(data["engagement_id"])
    snapshot = store.snapshot(binding)
    assert [row.number for row in snapshot.hop_rows()] == [23, 24, 25, 26, 27, 28, 29, 30]
    assert snapshot.next_hop_number == 31
    exported = json.dumps(store.export(binding))
    assert "ground_truth_probes" not in exported and "DA_CHILD" not in exported
    # The same operation in a second file is a destination conflict, never a merge.
    twin = _legacy(tmp_path, channel34_ledger(), name="state_copy.json")
    with pytest.raises(store_mod.LedgerImportError, match="already holds operation"):
        store_mod.import_legacy_file(twin, database=database)
    with pytest.raises(store_mod.LedgerImportError, match="malformed JSON"):
        bad = tmp_path / "legacy" / "state_bad.json"
        bad.write_text("{not json")
        store_mod.import_legacy_file(bad, database=database)


def test_exports_keep_corrections_and_readers_never_count_retracted_rows_as_success(tmp_path):
    tools = _session("export-readers")
    seed_ledger(tools, hops=[_hop("dcsync", "lab.local", "krbtgt-hash:lab.local", mythic_task_id="9",
                                  callback_id="3")])
    _state(tools, "remove 1 --reason wrong domain")
    path = store_mod.export_operation(tools._require_ledger_binding(), tmp_path / "exports")
    manifest = store_mod.manifest_exports(tmp_path / "exports")
    assert manifest == {"export-readers": path}
    data = json.loads(path.read_text())
    assert data["hops"][0]["corrections"]["retracted"]["reason"] == "wrong domain"

    [record] = trajectory_exporter.export_ledger_artifact(path)
    assert "ledger_operator_retracted" in record.verifier.labels
    assert record.verifier.admissible_proof is False
    assert record.verifier.evidence["correction"]["kind"] == "operator_retracted"

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ledger_yield", ROOT / "skills" / "sage-trace-analysis" / "scripts" / "ledger_yield.py")
    ledger_yield = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ledger_yield)
    export = {"requests": [{"id": 1, "created_at": "2026-11-04T17:00:00Z", "completed_at": "2026-11-04T19:00:00Z"}],
              "tasks": [{"display_id": 9, "command_name": "dcsync", "status": "completed",
                         "timestamp": "2026-11-04T18:00:00Z"}]}
    report = ledger_yield.build_report(data, export)
    assert report["requests"][0]["proven_rows"] == 0 and report["corrected_rows"] == 1
    historical = ledger_yield.build_report(channel34_ledger(), export)  # unversioned JSON stays readable
    assert historical["ledger_rows"] == 7 and historical["next_hop_number"] == 30


def _load_archiver():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "archive_runtime_dbs", ROOT / "skills" / "sage-goad-reset" / "scripts" / "archive_runtime_dbs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("include_ledger", [False, True], ids=["between-arms", "reset"])
def test_archive_exports_the_archived_cut_and_carries_the_ledger_only_between_arms(tmp_path, include_ledger):
    archiver = _load_archiver()
    runtime = tmp_path / "Payload_Type" / "sage"
    runtime.mkdir(parents=True)
    database = runtime / "sage.db"
    store = store_mod.EngagementStore(database, legacy_dir=str(tmp_path / "none"))
    binding = store.bind_namespace("archived-op")
    store.apply(binding, store_mod.hop_mutations([], [_hop("gpo-abuse", "a", "system:a", "pending")]))
    store.retract(binding, ["1"], reason="test", actor=store_mod.Actor(kind="operator"))
    store.update_objective(binding, "keep me", "operator")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE checkpoints (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO checkpoints (value) VALUES ('thread state')")
    (runtime / ".sage_engagement" / "decision_records").mkdir(parents=True)
    (runtime / ".sage_engagement" / "decision_records" / "seal.json").write_text("{}")

    moves = archiver.archive_runtime_dbs(tmp_path, timestamp="20261104-2200", sage_database_path=database,
                                         include_ledger=include_ledger)

    archived = runtime / "sage_20261104-2200.db"
    exports = runtime / "esl_exports_20261104-2200"
    assert (archived, exports) in moves
    [export_path] = list(exports.glob("*.esl.json"))
    exported = json.loads(export_path.read_text())
    archived_store = store_mod.EngagementStore(archived, legacy_dir=str(tmp_path / "none"))
    assert exported["revision"] == archived_store.snapshot(archived_store.find_binding("archived-op")).revision
    assert exported["legacy_settings"] == {"objective": "keep me", "objective_source": "operator"}
    if include_ledger:
        assert not database.exists()
        assert (runtime / ".sage_engagement_20261104-2200" / "decision_records" / "seal.json").exists()
    else:
        fresh = store_mod.EngagementStore(database, legacy_dir=str(tmp_path / "none"))
        carried = fresh.snapshot(fresh.find_binding("archived-op"))
        assert [(row.number, bool(row.retraction)) for row in carried.hop_rows()] == [(1, True)]
        assert carried.objective_setting() == ("keep me", "operator")
        fresh.apply(fresh.find_binding("archived-op"),
                    store_mod.hop_mutations([], [_hop("gpo-abuse", "b", "system:b", "pending")]))
        assert [row.number for row in fresh.snapshot(fresh.find_binding("archived-op")).hop_rows()] == [1, 2]
        with sqlite3.connect(database) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "checkpoints" not in tables
        assert (runtime / ".sage_engagement" / "decision_records" / "seal.json").exists()


def test_operator_invalidation_over_an_automatic_marker_stays_sticky_after_a_new_achievement():
    """Cold-review reproduction: an operator "no longer holds" must record even when an automatic marker exists."""
    tools = _session()
    seed_ledger(tools, prove=False, hops=[_hop("golden-ticket", "north.local", "da:north.local")])
    binding = tools._require_ledger_binding()
    tools._ledger_store().mark_no_longer_holds(binding, ["golden-ticket:north.local"], reason="contradicted",
                                               evidence={"source": "contradiction-downgrade"})
    shown = _state(tools, "invalidate 1 --reason operator confirmed it is gone")
    assert "Marked 1 hop(s)" in shown
    seed_ledger(tools, hops=[_hop("golden-ticket", "north.local", "da:north.local", ticket_valid=True)])  # re-proof
    assert all(hop.effect != "da:north.local" for hop in tools._engagement_hops)
    [corrected] = tools._ledger_view().corrected_hops
    marker = corrected.corrections["no_longer_holds"]
    assert marker["scope"] == "operator" and marker["reason"] == "operator confirmed it is gone"
    # An automatic marker alone lapses once a new proof moves the head (the contradiction "reopens" the row).
    seed_ledger(tools, hops=[_hop("dcsync", "north.local", "krbtgt-hash:north.local")])
    tools._ledger_store().mark_no_longer_holds(binding, ["dcsync:north.local"], reason="contradicted",
                                               evidence={"source": "contradiction-downgrade"})
    assert all(hop.technique != "dcsync" for hop in tools._engagement_hops)
    seed_ledger(tools, hops=[_hop("dcsync", "north.local", "krbtgt-hash:north.local", artifact_present=True)])
    assert any(hop.technique == "dcsync" for hop in tools._engagement_hops)


def test_import_records_match_by_name_and_content_across_mount_paths(tmp_path):
    host = tmp_path / "host" / "legacy"
    container = tmp_path / "container" / "legacy"
    source = write_legacy_ledger(host, channel34_ledger())
    database = tmp_path / "sage.db"
    assert store_mod.import_legacy_file(source, database=database) == "imported"
    container.mkdir(parents=True)
    (container / source.name).write_bytes(source.read_bytes())
    store = store_mod.EngagementStore(database, legacy_dir=str(container))
    assert store.pending_legacy_files() == []          # the same file, seen at the container's mount path
    assert store_mod.import_legacy_file(container / source.name, database=database) == "unchanged"
    changed = json.loads(source.read_text())
    changed["hops"][0]["status"] = "failed"
    (container / source.name).write_text(json.dumps(changed))
    assert store.pending_legacy_files() == [str(container / source.name)]
    with pytest.raises(store_mod.LedgerImportError, match="changed after it was imported"):
        store_mod.import_legacy_file(container / source.name, database=database)


def test_reads_never_create_a_database_or_its_ledger_schema(tmp_path):
    missing = tmp_path / "absent.db"
    store = store_mod.EngagementStore(missing, legacy_dir=str(tmp_path / "legacy"))
    binding = store_mod.OperationBinding(store_operation_id=1, engagement_key="x")
    with pytest.raises(store_mod.LedgerUnavailable):
        store.snapshot(binding)
    assert not missing.exists()
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE checkpoints (id INTEGER)")
    write_legacy_ledger(tmp_path / "legacy", channel34_ledger())
    assert len(store_mod.EngagementStore(other, legacy_dir=str(tmp_path / "legacy")).pending_legacy_files()) == 1
    with sqlite3.connect(other) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"checkpoints"}
